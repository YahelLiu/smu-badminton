"""Token 缓存和刷新单元测试。"""
import pytest
from smu_badminton.token_profile import (
    cache_token_for_user,
    get_cached_token,
    find_user_by_access_token,
    clear_token_cache,
)


def test_cache_and_get_token():
    """测试缓存和获取 token。"""
    username = "test_token_user_001"
    tokens = {
        "access_token": "test_access_token_001",
        "id_token": "test_id_token_001"
    }

    # 缓存
    cache_token_for_user(username, tokens)

    # 获取
    cached = get_cached_token(username)
    assert cached is not None
    assert cached["access_token"] == "test_access_token_001"
    assert cached["id_token"] == "test_id_token_001"

    # 清理
    clear_token_cache(username)


def test_get_cached_token_miss():
    """测试获取不存在的 token 缓存。"""
    cached = get_cached_token("nonexistent_token_user")
    assert cached is None


def test_find_user_by_access_token():
    """测试通过 access_token 查找用户。"""
    username = "test_token_user_002"
    tokens = {
        "access_token": "test_access_token_002",
        "id_token": "test_id_token_002"
    }

    cache_token_for_user(username, tokens)

    # 查找
    found_username, id_token = find_user_by_access_token("test_access_token_002")
    assert found_username == username
    assert id_token == "test_id_token_002"

    # 清理
    clear_token_cache(username)


def test_find_user_by_access_token_miss():
    """测试通过不存在的 access_token 查找。"""
    found_username, id_token = find_user_by_access_token("nonexistent_token")
    assert found_username == ""
    assert id_token == ""


def test_clear_token_cache_single():
    """测试清理单个用户的 token 缓存。"""
    username = "test_token_user_003"
    tokens = {"access_token": "token_003", "id_token": "id_003"}

    cache_token_for_user(username, tokens)
    clear_token_cache(username)

    cached = get_cached_token(username)
    assert cached is None


def test_clear_token_cache_all():
    """测试清理所有 token 缓存。"""
    cache_token_for_user("user1", {"access_token": "t1", "id_token": "i1"})
    cache_token_for_user("user2", {"access_token": "t2", "id_token": "i2"})

    clear_token_cache()

    assert get_cached_token("user1") is None
    assert get_cached_token("user2") is None
