# OCR 验证码识别

## 概述

上海海事大学 CAS 登录使用算术验证码（如 `3 + 5 = ?`），SMU Badminton 集成了 OCR 自动识别功能，支持本地推理和远程服务两种方式。

## 验证码类型

CAS 验证码为算术表达式图片，格式为 `数字 运算符 数字 = ?`，其中：

- **数字**：0-9 的单个数字
- **运算符**：加（+）、减（-）、乘（*）
- **等号**：可能为中文等号或符号等号

系统需要识别表达式并计算结果作为验证码答案。

## 三种 OCR 模式

### 本地 NCNN 模式（local）

默认模式，在本地使用 NCNN 推理框架运行 ResNet 模型。

**优点：**
- 无需额外服务部署
- 延迟最低
- 无网络依赖

**缺点：**
- 需要 OpenCV 和 NCNN 运行时
- 占用本机 CPU 资源
- 模型文件需单独准备（约数十 MB）

**配置：**

```env
OCR_MODE=local
```

**模型文件：**

```
model/
  resnet34_digit_latest.fp32.param       # 数字识别模型参数
  resnet34_digit_latest.fp32.bin         # 数字识别模型权重
  resnet18_operator_latest.fp32.param    # 运算符识别模型参数
  resnet18_operator_latest.fp32.bin      # 运算符识别模型权重
  resnet18_equal_symbol_latest.fp32.param  # 等号类型检测模型参数
  resnet18_equal_symbol_latest.fp32.bin    # 等号类型检测模型权重
```

> 模型文件被 gitignore，需单独获取并放入 `model/` 目录。模型采用懒加载机制，仅首次调用时加载。

### 远程 HTTP API 模式（http）

通过 RESTful API 调用远程 OCR 服务（如 shmtu-cas-ocr-server）。

**优点：**
- 不占用本机资源
- 可共享给多个实例使用
- 支持独立扩缩容

**缺点：**
- 需要额外部署 OCR 服务
- 网络延迟
- 服务不可用时降级

**配置：**

```env
OCR_MODE=http
OCR_HTTP_HOST=127.0.0.1
OCR_HTTP_PORT=21600
OCR_TIMEOUT=10
```

**API 协议：**

```
POST /api/ocr
Content-Type: application/json

{
  "imageBase64": "<base64 编码的验证码图片>"
}
```

**响应格式：**

```json
{
  "success": true,
  "expression": "3 + 5 = 8",
  "result": 8,
  "equalSymbol": 1,
  "operator": 2,
  "digit1": 3,
  "digit2": 5
}
```

### 远程 TCP API 模式（tcp）

通过自定义 TCP 协议调用远程 OCR 服务。

**优点：**
- 比 HTTP 延迟更低
- 协议简单高效

**缺点：**
- 需要额外部署 OCR 服务
- 无 HTTP 标准化支持

**配置：**

```env
OCR_MODE=tcp
OCR_TCP_HOST=127.0.0.1
OCR_TCP_PORT=21601
OCR_TIMEOUT=10
```

**TCP 协议：**

1. 客户端发送原始图片字节 + `<END>` 标记
2. 服务端返回表达式字符串（如 `3 + 5 = 8`）后关闭连接
3. 客户端解析 `=` 后面的数字作为结果

## 识别流程

### 本地 NCNN 识别流程

```
1. 预处理图片
   - 灰度化 -> 二值化（阈值 200）-> 合并为三通道
   |
2. 检测等号类型
   - 裁剪右侧区域（70%-100%）-> ResNet18 分类
   - 0: 中文等号 -> 使用 KEY_POINT_CHS 分割点
   - 其他: 符号等号 -> 使用 KEY_POINT_SYMBOL 分割点
   |
3. 分割图片区域
   - 根据 key_point 裁剪出：数字1、运算符、数字2
   |
4. 分别识别
   - 数字1 -> ResNet34 分类（0-9）
   - 运算符 -> ResNet18 分类（+, -, *）
   - 数字2 -> ResNet34 分类（0-9）
   |
5. 计算结果
   - 根据识别的数字和运算符计算算术结果
```

### 预处理参数

| 参数 | 值 | 说明 |
|------|----|------|
| 二值化阈值 | 200 | 灰度图二值化阈值 |
| 输入尺寸 | 224x224 | 模型输入图片尺寸 |
| 均值 | [123.675, 116.28, 103.53] | ImageNet 标准均值 |
| 标准差倒数 | [1/58.395, 1/57.12, 1/57.375] | ImageNet 标准差倒数 |

### 运算符映射

| 分类值 | 运算符 |
|--------|--------|
| 0, 1 | +（加） |
| 2, 3 | -（减） |
| 4, 5 | *（乘） |

## 容错机制

- 本地模型懒加载：仅在首次调用时加载，避免启动时阻塞
- 远程服务超时：`OCR_TIMEOUT` 控制超时时间（默认 10 秒）
- 无效模式回退：`OCR_MODE` 值无效时自动回退为 `local`
- 登录重试：OCR 识别失败（验证码错误）时，前端可切换手动输入模式
