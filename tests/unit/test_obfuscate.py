"""密码混淆单元测试。"""
from smu_badminton.core_utils import obfuscate_password, deobfuscate_password


def test_obfuscate_roundtrip():
    """测试混淆和还原。"""
    pw = "test_password_123"
    obf = obfuscate_password(pw)
    assert obf != pw
    assert deobfuscate_password(obf) == pw


def test_obfuscate_empty():
    """测试空字符串。"""
    assert obfuscate_password("") == ""
    assert deobfuscate_password("") == ""


def test_obfuscate_special_chars():
    """测试特殊字符。"""
    pw = "测试密码!@#$%^&*()"
    obf = obfuscate_password(pw)
    assert deobfuscate_password(obf) == pw


def test_deobfuscate_invalid():
    """测试无效输入返回原文。"""
    # 不是有效的 base64 字符串应该返回原文
    result = deobfuscate_password("not_valid_base64!!!")
    assert result == "not_valid_base64!!!"
