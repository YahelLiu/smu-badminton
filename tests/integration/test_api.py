"""API 集成测试。"""
from fastapi.testclient import TestClient


def test_health():
    """测试健康检查端点。"""
    from smu_badminton.server_fastapi import app
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_config():
    """测试配置端点。"""
    from smu_badminton.server_fastapi import app
    client = TestClient(app)
    r = client.get("/api/config")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "login_url" in r.json()["data"]


def test_jobs_list():
    """测试任务列表端点。"""
    from smu_badminton.server_fastapi import app
    client = TestClient(app)
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_metrics():
    """测试指标端点。"""
    from smu_badminton.server_fastapi import app
    client = TestClient(app)
    # 先请求一个端点产生指标
    client.get("/health")
    r = client.get("/api/metrics")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_auth_check():
    """测试权限检查端点。"""
    from smu_badminton.server_fastapi import app
    client = TestClient(app)
    r = client.get("/api/auth/check?username=test_user")
    assert r.status_code == 200
    assert "authorized" in r.json()


def test_index():
    """测试首页。"""
    from smu_badminton.server_fastapi import app
    client = TestClient(app)
    r = client.get("/")
    # 可能返回 HTML 或 404（如果模板不存在）
    assert r.status_code in [200, 404]


def test_captcha_api():
    """测试验证码获取 API。"""
    from smu_badminton.server_fastapi import app
    client = TestClient(app)
    r = client.post("/api/captcha", json={})
    # 验证码获取可能会因为网络原因失败，所以只检查返回格式
    data = r.json()
    assert "ok" in data
    # 如果成功，检查数据格式
    if data["ok"]:
        assert "captcha_image" in data["data"]
        assert "session_id" in data["data"]
        assert data["data"]["captcha_image"].startswith("data:image/png;base64,")
