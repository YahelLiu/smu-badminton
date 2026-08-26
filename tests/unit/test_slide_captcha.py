"""滑块验证码缺口识别单元测试。"""
import pytest
import numpy as np
from unittest.mock import patch


def test_decode_base64_image_valid():
    """测试解码有效的 base64 图片。"""
    from smu_badminton.slide_captcha import decode_base64_image

    # 创建一个小的测试图片并编码为 base64
    import cv2
    import base64

    img = np.zeros((50, 50, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".png", img)
    b64_str = base64.b64encode(buf).decode()

    result = decode_base64_image(b64_str)
    assert result is not None
    assert result.shape == (50, 50, 3)


def test_decode_base64_image_with_data_uri():
    """测试解码带 data URI 前缀的 base64 图片。"""
    from smu_badminton.slide_captcha import decode_base64_image

    import cv2
    import base64

    img = np.zeros((50, 50, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".png", img)
    b64_str = base64.b64encode(buf).decode()

    # 带 data URI 前缀
    data_uri = f"data:image/png;base64,{b64_str}"

    result = decode_base64_image(data_uri)
    assert result is not None
    assert result.shape == (50, 50, 3)


def test_decode_base64_image_invalid():
    """测试解码无效的 base64 字符串。"""
    from smu_badminton.slide_captcha import decode_base64_image

    result = decode_base64_image("not_valid_base64!!!")
    assert result is None


def test_solve_slide_captcha_invalid_input():
    """测试无效输入的缺口识别。"""
    from smu_badminton.slide_captcha import solve_slide_captcha

    # 无效的 base64
    result = solve_slide_captcha("invalid_bg", "invalid_tpl")
    assert result is None


def test_solve_slide_captcha_with_mock_images():
    """测试缺口识别（使用模拟图片）。"""
    from smu_badminton.slide_captcha import solve_slide_captcha, decode_base64_image

    import cv2
    import base64

    # 创建背景图（200x50，左侧有一个白色矩形缺口区域）
    bg = np.zeros((50, 200, 3), dtype=np.uint8)
    # 在 x=120 处画一个白色矩形（模拟缺口）
    bg[10:40, 120:150] = 255

    # 创建拼图小块（30x30，白色）
    tpl = np.zeros((50, 50, 3), dtype=np.uint8)
    tpl[10:40, 10:40] = 255

    # 编码为 base64
    _, bg_buf = cv2.imencode(".png", bg)
    bg_b64 = base64.b64encode(bg_buf).decode()
    _, tpl_buf = cv2.imencode(".png", tpl)
    tpl_b64 = base64.b64encode(tpl_buf).decode()

    result = solve_slide_captcha(bg_b64, tpl_b64)
    # 应该返回一个合理的 X 坐标
    assert result is not None
    assert 0 <= result <= 200
