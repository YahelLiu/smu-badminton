"""CAS 算式验证码识别（数字 运算符 数字 = ?）。

历史实现用自训 NCNN ResNet 三模型（数字 / 运算符 / 等号类型）+ 固定比例切分。
网站换字体后这三个自训模型失效（认不出新字体的运算符与数字），且无训练设施可重训，
固定比例切分对新版面也不再对——双重失效。现改用 ddddocr 整图识别 + 正则提取首个
`数字 运算符 数字` 求值，绕开切分与自训模型。ddddocr 对数字与常见运算符识别稳定，
样本（9 + 3 = ?）实测输出 '9+32'，正则正确抓到 9+3。

接口 (result, expr, eq_sym, op_code, d1, d2) 与旧实现保持一致，上层 cas_login
(`result, *_ = predict_validate_code(...)`) 无需改动。
"""
import re
import threading
from pathlib import Path

import ddddocr

# ddddocr 实例惰性加载（onnx 模型加载较重；InferenceSession 推理用锁串行化以策安全）。
_OCR: "ddddocr.DdddOcr | None" = None
_OCR_LOCK = threading.Lock()


def _get_ocr() -> "ddddocr.DdddOcr":
    global _OCR
    if _OCR is None:
        with _OCR_LOCK:
            if _OCR is None:
                _OCR = ddddocr.DdddOcr(show_ad=False)
    return _OCR


# 算式验证码格式恒为: 单数字 运算符 单数字 = ?  (仅 + - *)
# 抓首个 `数字 运算符 数字` 三元组即可，结尾的 = ? 噪声被忽略。
# 运算符字符类覆盖 ddddocr 可能的输出: + - * 以及 * 的常见误读 x X ×。
_CAPTCHA_RE = re.compile(r"(\d)\s*([+\-*/xX×÷])\s*(\d)")

_OP_NORMALIZE = {
    "+": "+",
    "-": "-",
    "*": "*",
    "x": "*",
    "X": "*",
    "×": "*",
}

# 运算符类型码，与旧 get_operator_str_by_int 保持一致(0->+, 2->-, 4->*)。
_OP_CODE = {"+": 0, "-": 2, "*": 4}


def _read_bytes(img_input) -> bytes:
    """img_input: str(文件路径，支持中文路径) 或 bytes(图片字节数据)。"""
    if isinstance(img_input, (bytes, bytearray)):
        return bytes(img_input)
    if isinstance(img_input, str):
        # 用 pathlib 读字节，规避 cv2.imread 不支持非 ASCII(中文)路径的问题。
        return Path(img_input).read_bytes()
    raise ValueError("img_input must be str (file path) or bytes")


def _apply_op(op: str, a: int, b: int) -> int:
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    raise ValueError(f"unsupported operator: {op}")


def predict_validate_code(img_input):
    """识别算式验证码，返回 (result, expr, eq_sym, op_code, d1, d2)。

    img_input: str(文件路径) 或 bytes(图片字节数据)。

    用 ddddocr 整图识别后，正则提取首个 `数字 运算符 数字` 求值。

    重要: 本函数绝不抛异常。识别失败时返回 result=-1 的哨兵值——
    cas_login 的 `except Exception` 会把任何异常当成 NETWORK_ERROR 硬失败而非验证码重试，
    所以必须走「返回错误答案 -> 服务端判 CAPTCHA_ERROR -> 重试」的容错路径。
    """
    img_bytes = _read_bytes(img_input)
    raw = _get_ocr().classification(img_bytes)

    m = _CAPTCHA_RE.search(raw)
    if not m:
        # 抓不到 `数字 运算符 数字`: 返回哨兵，让上层 CAPTCHA_ERROR 重试。
        return -1, f"OCR 识别失败: {raw!r}", 0, 0, 0, 0

    d1 = int(m.group(1))
    op = _OP_NORMALIZE.get(m.group(2))
    d2 = int(m.group(3))
    if op is None:
        return -1, f"OCR 未知运算符: {raw!r}", 0, 0, d1, d2

    result = _apply_op(op, d1, d2)
    expr = f"{d1} {op} {d2} = {result}"
    return result, expr, 0, _OP_CODE[op], d1, d2


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python -m smu_badminton.cas_ocr <验证码图片路径>")
        sys.exit(2)
    r, expr, *_ = predict_validate_code(sys.argv[1])
    print(f"识别: result={r}  expr={expr}")
