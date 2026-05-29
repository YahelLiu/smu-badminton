"""用户账号保存单元测试。"""
import pytest
from smu_badminton.token_profile import (
    save_user_account,
    get_user_account,
    delete_user_account,
)
from smu_badminton.core_utils import init_db_tables


@pytest.fixture(autouse=True)
def setup_db():
    """每个测试前初始化数据库。"""
    init_db_tables()


def test_save_and_get_user_account():
    """测试保存和获取用户账号。"""
    username = "test_user_001"
    password = "test_password_123"
    login_url = "https://example.com/login"
    captcha_url = "https://example.com/captcha"

    # 保存
    ok = save_user_account(username, password, login_url, captcha_url)
    assert ok is True

    # 获取
    account = get_user_account(username)
    assert account is not None
    assert account["username"] == username
    assert account["password"] == password
    assert account["login_url"] == login_url
    assert account["captcha_url"] == captcha_url

    # 清理
    delete_user_account(username)


def test_save_user_account_update():
    """测试更新用户账号（同一用户名会更新）。"""
    username = "test_user_002"

    # 第一次保存
    save_user_account(username, "password1", "url1", "captcha1")
    account = get_user_account(username)
    assert account["password"] == "password1"

    # 第二次保存（更新）
    save_user_account(username, "password2", "url2", "captcha2")
    account = get_user_account(username)
    assert account["password"] == "password2"
    assert account["login_url"] == "url2"

    # 清理
    delete_user_account(username)


def test_get_nonexistent_user():
    """测试获取不存在的用户。"""
    account = get_user_account("nonexistent_user_xyz")
    assert account is None


def test_delete_user_account():
    """测试删除用户账号。"""
    username = "test_user_003"
    save_user_account(username, "password")

    # 删除
    ok = delete_user_account(username)
    assert ok is True

    # 确认已删除
    account = get_user_account(username)
    assert account is None


def test_save_empty_username():
    """测试保存空用户名。"""
    ok = save_user_account("", "password")
    assert ok is False


def test_save_empty_password():
    """测试保存空密码。"""
    ok = save_user_account("test_user_004", "")
    assert ok is False
