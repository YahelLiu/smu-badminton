"""
Configuration utilities loaded from .env.
"""

import os
from pathlib import Path
from urllib.parse import quote, urlencode

from dotenv import load_dotenv


# Load .env in project root (src/smu_badminton/../../.env)
env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

# 项目根目录
BASE_DIR = Path(__file__).parent.parent.parent

# WF platform config
WF_ORIGIN = os.getenv("WF_ORIGIN", "https://wf.shmtu.edu.cn")
WF_API_URL = os.getenv("WF_API_URL", "https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys")
WF_HOME_URL = os.getenv("WF_HOME_URL", f"{WF_ORIGIN}/yy-sys/pc/home")
WF_SSO_AUTHORIZE_PATH = os.getenv("WF_SSO_AUTHORIZE_PATH", "/sso/oauth2/authorize")

# CAS config
CAS_ORIGIN = os.getenv("CAS_ORIGIN", "https://cas.shmtu.edu.cn")
CAS_CAPTCHA_URL = os.getenv("CAS_CAPTCHA_URL", "https://cas.shmtu.edu.cn/cas/captcha")
# Backward compatibility: keep field name, but default entry is WF home now.
CAS_LOGIN_URL = os.getenv("CAS_LOGIN_URL", WF_HOME_URL)

# OAuth config
OAUTH_CLIENT_ID = os.getenv("OAUTH_CLIENT_ID", "kwxKbMKq3Nafw2mApFZz")

# Resource type id
BADMINTON_TYPE_ID = os.getenv("BADMINTON_TYPE_ID", "93c2a115-5c73-4e30-bb6a-dfcc5404e46f")

# ========== Runtime config ==========
BOOKING_DEBUG = os.getenv("BOOKING_DEBUG", "0").lower() in {"1", "true", "yes", "on"}
TOKEN_PROFILE_TTL_SEC = int(os.getenv("TOKEN_PROFILE_TTL_SEC", "3600"))
TOKEN_CACHE_TTL_SEC = int(os.getenv("TOKEN_CACHE_TTL_SEC", "900"))
JOB_RETENTION_SEC = int(os.getenv("JOB_RETENTION_SEC", "3600"))

# ========== Security config ==========
_SECRET_KEY_DEFAULT = "smu-badminton-default-key"
SECRET_KEY = os.getenv("SECRET_KEY", _SECRET_KEY_DEFAULT)

# 安全检查：SECRET_KEY 使用默认值时发出警告
if SECRET_KEY == _SECRET_KEY_DEFAULT:
    import warnings
    warnings.warn(
        "使用默认 SECRET_KEY 不安全！请在 .env 中设置 SECRET_KEY 环境变量。",
        UserWarning
    )
    # 使用 logger 需要先配置
    import logging
    logging.getLogger(__name__).warning("警告：使用默认 SECRET_KEY，存储的密码可被轻易解码！")

AUTHORIZED_USERS = set(os.getenv("AUTHORIZED_USERS", "202540510004").split(","))

# 可信代理 IP 列表（用于 X-Forwarded-For 验证）
# 只有来自可信代理的请求才会信任 X-Forwarded-For 头
# 示例：TRUSTED_PROXIES=127.0.0.1,10.0.0.1
TRUSTED_PROXIES = set(os.getenv("TRUSTED_PROXIES", "").split(",")) if os.getenv("TRUSTED_PROXIES") else set()

# ========== Rate limit config ==========
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "30"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "10"))
RATE_LIMIT_JOBS_MAX = int(os.getenv("RATE_LIMIT_JOBS_MAX", "300"))
RATE_LIMIT_JOBS_WINDOW = int(os.getenv("RATE_LIMIT_JOBS_WINDOW", "60"))

# ========== OCR config ==========
# OCR 模式: local (本地 NCNN), http (远程 RESTful API), tcp (远程 TCP API)
OCR_MODE = os.getenv("OCR_MODE", "local").lower()
if OCR_MODE not in ("local", "http", "tcp"):
    import warnings
    warnings.warn(f"无效的 OCR_MODE='{OCR_MODE}'，仅支持 local/http/tcp，回退为 local", UserWarning)
    logging.getLogger(__name__).warning(f"无效的 OCR_MODE='{OCR_MODE}'，仅支持 local/http/tcp，回退为 local")
    OCR_MODE = "local"
# 远程 OCR HTTP 服务配置
OCR_HTTP_HOST = os.getenv("OCR_HTTP_HOST", "127.0.0.1")
OCR_HTTP_PORT = int(os.getenv("OCR_HTTP_PORT", "21600"))
# 远程 OCR TCP 服务配置
OCR_TCP_HOST = os.getenv("OCR_TCP_HOST", "127.0.0.1")
OCR_TCP_PORT = int(os.getenv("OCR_TCP_PORT", "21601"))
# 远程 OCR 请求超时（秒）
OCR_TIMEOUT = int(os.getenv("OCR_TIMEOUT", "10"))

# ========== Server config ==========
UVICORN_RELOAD = os.getenv("UVICORN_RELOAD", "0").lower() in {"1", "true", "yes", "on"}

# ========== Data path config ==========
# Docker 环境使用 /app/data，本地开发使用项目目录下的 data
DATA_DIR = os.getenv("DATA_DIR", "/app/data" if os.path.exists("/app/data") else str(BASE_DIR / "data"))

# ========== User info defaults ==========
DEFAULT_DEPT_CODE = os.getenv("DEFAULT_DEPT_CODE", "")
DEFAULT_DEPT_NAME = os.getenv("DEFAULT_DEPT_NAME", "")
DEFAULT_DEPT_NAME_EN = os.getenv("DEFAULT_DEPT_NAME_EN", "")
DEFAULT_USER_EMAIL = os.getenv("DEFAULT_USER_EMAIL", "")
DEFAULT_USER_PHONE = os.getenv("DEFAULT_USER_PHONE", "")


def build_wf_authorize_url(ret_url: str | None = None, state: str | None = None, nonce: str | None = None) -> str:
    """Build WF oauth2 authorize URL. If not authenticated it will redirect to CAS login."""
    ret = ret_url or WF_HOME_URL
    callback = f"{WF_ORIGIN}/yy-sys/oidc-callback?retUrl={ret}"
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": callback,
        "response_type": "id_token token",
        "scope": "data openid process task app submit process_edit start profile",
        "state": state or os.urandom(16).hex(),
        "nonce": nonce or os.urandom(16).hex(),
    }
    return f"{WF_ORIGIN}{WF_SSO_AUTHORIZE_PATH}?{urlencode(params, quote_via=quote)}"


def get_config():
    """Return all config fields."""
    return {
        "cas_origin": CAS_ORIGIN,
        "cas_captcha_url": CAS_CAPTCHA_URL,
        "cas_login_url": CAS_LOGIN_URL,
        "wf_origin": WF_ORIGIN,
        "wf_api_url": WF_API_URL,
        "wf_home_url": WF_HOME_URL,
        "oauth_client_id": OAUTH_CLIENT_ID,
        "badminton_type_id": BADMINTON_TYPE_ID,
    }


def get_frontend_config():
    """Return frontend-required settings."""
    return {
        "login_url": WF_HOME_URL,
        "authorize_url": build_wf_authorize_url(WF_HOME_URL),
        "captcha_url": CAS_CAPTCHA_URL,
    }
