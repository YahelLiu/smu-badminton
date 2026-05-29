"""Token 缓存单元测试。"""
from smu_badminton.token_profile import clear_token_cache


def test_clear_token_cache_single():
    """测试清理单个用户的 token 缓存。"""
    # 不应该报错
    clear_token_cache("nonexistent_user")


def test_clear_token_cache_all():
    """测试清理所有 token 缓存。"""
    # 不应该报错
    clear_token_cache()


def test_get_cached_token_miss():
    """测试 token 缓存未命中。"""
    from smu_badminton.token_profile import get_cached_token
    # 无缓存用户应该返回 None
    result = get_cached_token("nonexistent_user")
    assert result is None
