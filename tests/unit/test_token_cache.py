"""Token 缓存单元测试。"""
from smu_badminton.cas_login_requests import clear_token_cache


def test_clear_token_cache_single():
    """测试清理单个用户的 token 缓存。"""
    # 不应该报错
    clear_token_cache("nonexistent_user")


def test_clear_token_cache_all():
    """测试清理所有 token 缓存。"""
    # 不应该报错
    clear_token_cache()


def test_get_token_cached_miss():
    """测试 token 缓存未命中。"""
    from smu_badminton.cas_login_requests import get_token_cached
    # 无效凭证应该返回 None
    result = get_token_cached("fake_url", "fake_url", "nonexistent_user", "wrong_password", ttl_seconds=900)
    assert result is None
