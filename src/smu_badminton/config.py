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
LOCK_MAX_AGE_SEC = int(os.getenv("LOCK_MAX_AGE_SEC", "300"))

# ========== Security config ==========
SECRET_KEY = os.getenv("SECRET_KEY", "smu-badminton-default-key")
AUTHORIZED_USERS = set(os.getenv("AUTHORIZED_USERS", "202540510004").split(","))

# ========== Rate limit config ==========
RATE_LIMIT_MAX = int(os.getenv("RATE_LIMIT_MAX", "30"))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "10"))
RATE_LIMIT_JOBS_MAX = int(os.getenv("RATE_LIMIT_JOBS_MAX", "300"))
RATE_LIMIT_JOBS_WINDOW = int(os.getenv("RATE_LIMIT_JOBS_WINDOW", "60"))

# ========== CAS login strategy ==========
CAS_LOGIN_STABLE_FIRST = os.getenv("CAS_LOGIN_STABLE_FIRST", "0").lower() in {"1", "true", "yes", "on"}

# ========== Server config ==========
UVICORN_RELOAD = os.getenv("UVICORN_RELOAD", "0").lower() in {"1", "true", "yes", "on"}

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
