# OCR 验证码识别

## 概述

上海海事大学 CAS 登录使用算术验证码（如 `9 + 3 = ?`），SMU Badminton 在本地用
[ddddocr](https://github.com/sml2h3/ddddocr) 整图识别 + 正则提取算式并求值，无需额外服务部署。

## 验证码类型

CAS 验证码为算术表达式图片，格式恒为 `数字 运算符 数字 = ?`，其中：

- **数字**：0-9 单个数字
- **运算符**：加（+）、减（-）、乘（*）
- 结尾 `= ?` 为固定噪声，识别时忽略

系统识别表达式并计算结果作为验证码答案。

## 实现位置

识别逻辑全部在 `src/smu_badminton/cas_ocr.py` 的 `predict_validate_code(img_input)`，
被 `src/smu_badminton/cas_login.py` 在登录流程中调用。接口返回
`(result, expr, eq_sym, op_code, d1, d2)`，上层只用 `result`（答案）与 `expr`（日志）。

## 识别流程

```
1. 读图(img_input 为 str 路径或 bytes；路径用 pathlib 读字节，规避 cv2 中文路径问题)
   |
2. ddddocr 整图分类 -> 原始字符串(如 '9+32')
   - DdddOcr(show_ad=False) 惰性单例加载，onnxruntime 本地推理
   |
3. 正则提取首个 `数字 运算符 数字`
   - 正则: (\d)\s*([+\-*/xX×÷])\s*(\d)
   - 取首个三元组，忽略结尾 = ? 噪声('9+32' -> 9, +, 3)
   |
4. 运算符归一化
   - + -> +；- -> -；* / x / X / × -> *
   |
5. 求值返回
   - result = d1 OP d2；expr = "d1 OP d2 = result"
   - 失败(抓不到三元组 / 未知运算符): 返回 result=-1 哨兵，绝不抛异常
```

### 运算符映射

返回元组里的 `op_code` 与历史一致：

| op_code | 运算符 |
|--------|--------|
| 0 | +（加） |
| 2 | -（减） |
| 4 | *（乘） |

## 容错机制

- **不抛异常**：识别失败时返回 `result=-1`。cas_login 的 `except Exception` 会把异常
  当成 NETWORK_ERROR 硬失败而非验证码重试，因此必须走「错误答案 → 服务端判 CAPTCHA_ERROR → 重试」路径。
- **登录重试**：`login_with_auto_captcha` 默认 OCR 2 次 + 外层 `login_with_retry` 3 次，
  偶发识别失败会被重试消化；全部失败则回退手动输入验证码。
- **惰性加载**：`DdddOcr` 实例首次调用时创建，避免启动阻塞。

## 依赖

```bash
pip install ddddocr
```

> 清华 tuna 镜像可能不收录 ddddocr；镜像装失败时用官方源：
> `pip install ddddocr -i https://pypi.org/simple`

## 历史实现（已弃用）

此前用自训 NCNN ResNet 三模型（数字 `resnet34_digit_*` / 运算符 `resnet18_operator_*` /
等号类型 `resnet18_equal_symbol_*`）+ 固定比例切分（`KEY_POINT_CHS` / `KEY_POINT_SYMBOL`），
并依赖灰度二值化（阈值 200）、224x224、ImageNet 均值/标准差倒数预处理。网站换字体后这三个
自训模型失效（认不出新字体的运算符/数字）且无法重训（无训练设施），固定比例切分对新版面也错位，
故整体替换为 ddddocr 整图方案。`model/` 目录（gitignored）可能仍存有旧模型文件但不再加载，
`ncnn` 依赖已从 requirements.txt 移除。

> 本文档曾描述 local / http / tcp 三种 `OCR_MODE` 远程模式——这些在当前代码中**未实现**，
> `OCR_MODE` 环境变量不存在，仅有上述本地 ddddocr 单一路径。
