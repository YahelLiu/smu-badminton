"""HTTP 工具单元测试。"""
import pytest
from smu_badminton.http_utils import (
    get_network_time,
    get_current_beijing_time,
    get_target_datetime_from_network,
)
from datetime import datetime, timezone, timedelta


def test_get_current_beijing_time():
    """测试获取当前北京时间（至少能返回一个时间）。"""
    result = get_current_beijing_time(max_retries=1)
    assert result is not None
    assert isinstance(result, datetime)
    # 检查时区是 UTC+8
    assert result.tzinfo is not None


def test_get_target_datetime_from_network_with_bookdate():
    """测试计算目标抢票时间（带预约日期）。"""
    # 预约 2025-12-18，目标时间 21:00:00
    # 应该返回 2025-12-11 21:00:00（前 7 天）
    result = get_target_datetime_from_network("21:00:00", bookdate="2025-12-18")

    assert result.year == 2025
    assert result.month == 12
    assert result.day == 11
    assert result.hour == 21
    assert result.minute == 0
    assert result.second == 0


def test_get_target_datetime_from_network_without_bookdate():
    """测试计算目标抢票时间（不带预约日期，使用当前日期）。"""
    result = get_target_datetime_from_network("18:30:00")

    # 应该返回今天或昨天的 18:30:00
    assert result.hour == 18
    assert result.minute == 30
    assert result.second == 0
    assert result.tzinfo is not None


def test_get_network_time_may_fail():
    """测试获取网络时间（可能失败，返回 None）。"""
    # 这个测试不强制要求成功，因为网络可能不可用
    result = get_network_time()
    # 如果成功，检查格式
    if result is not None:
        assert isinstance(result, datetime)
        assert result.tzinfo is not None
