"""Pytest 配置和共享 fixtures。"""
import pytest
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
src_path = project_root / "src"
sys.path.insert(0, str(src_path))


@pytest.fixture(autouse=True)
def test_env(monkeypatch):
    """设置测试环境变量。"""
    monkeypatch.setenv("BOOKING_DEBUG", "0")
    monkeypatch.setenv("TOKEN_PROFILE_TTL_SEC", "3600")
    yield
