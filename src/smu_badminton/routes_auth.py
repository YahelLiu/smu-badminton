"""
认证相关路由模块。

包含：验证码、登录、登出、权限检查。
"""
import base64
import threading
import time as _time
import uuid
import logging
from typing import Dict

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool

from .server_models import (
    CaptchaRequest, CaptchaResponse,
    LoginRequest, LoginResponse,
    LogoutRequest,
)
from .cas_login import (
    prepare_login_session,
    login_with_auto_captcha,
    login_with_manual_captcha,
    attempt_login_with_captcha,
    LoginErrorType,
)
from .token_profile import cache_token_for_user, clear_token_cache, save_user_account
from .config import CAS_LOGIN_URL, CAS_CAPTCHA_URL, AUTHORIZED_USERS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["auth"])

# ============= 验证码会话存储 =============

_captcha_sessions: Dict[str, dict] = {}
_captcha_sessions_lock = threading.Lock()


def _cleanup_captcha_sessions():
    """清理过期的验证码会话。"""
    now = _time.time()
    with _captcha_sessions_lock:
        expired = [sid for sid, data in _captcha_sessions.items() if now - data["ts"] > 300]
        for sid in expired:
            session_data = _captcha_sessions.pop(sid, None)
            # 关闭关联的 requests.Session
            if session_data and "session" in session_data:
                try:
                    session_data["session"].close()
                except Exception:
                    pass


# ============= 路由定义 =============

@router.get("/auth/check")
async def check_auth(username: str):
    """检查用户权限。"""
    return {"ok": True, "authorized": username in AUTHORIZED_USERS}


@router.post("/captcha", response_model=CaptchaResponse)
async def get_captcha(req: CaptchaRequest):
    """获取验证码图片（base64 编码）。

    返回验证码图片的 base64 编码，用于前端显示。
    """
    # 清理过期会话
    _cleanup_captcha_sessions()

    try:
        login_url = req.login_url or CAS_LOGIN_URL
        captcha_url = req.captcha_url or CAS_CAPTCHA_URL

        session, cas_login_url, execution_value, captcha_image, captcha_token, login_page_html = await run_in_threadpool(
            prepare_login_session, login_url, captcha_url
        )

        # 将验证码图片转为 base64
        captcha_base64 = base64.b64encode(captcha_image).decode('utf-8')

        # 存储会话信息供后续登录使用（token 留服务端，前端只拿图片+session_id）
        session_id = str(uuid.uuid4())
        with _captcha_sessions_lock:
            _captcha_sessions[session_id] = {
                "session": session,
                "cas_login_url": cas_login_url,
                "execution_value": execution_value,
                "captcha_token": captcha_token,
                "login_page_html": login_page_html,
                "ts": _time.time()
            }

        return CaptchaResponse(ok=True, data={
            "captcha_image": f"data:image/png;base64,{captcha_base64}",
            "session_id": session_id
        })
    except Exception as e:
        logger.error(f"获取验证码失败: {e}")
        return CaptchaResponse(ok=False, error=str(e))


@router.post("/login", response_model=LoginResponse)
async def api_login(req: LoginRequest):
    """登录接口。

    如果不提供 captcha_code，则自动使用 OCR 识别验证码。
    如果提供 captcha_code，则使用手动输入的验证码。

    返回值：
    - success: 登录成功，返回 tokens
    - error_type: captcha_error（验证码错误）, password_error（密码错误）等
    - need_manual_captcha: 是否需要手动输入验证码
    """
    # 清理过期会话
    _cleanup_captcha_sessions()

    try:
        login_url = req.login_url or CAS_LOGIN_URL
        captcha_url = req.captcha_url or CAS_CAPTCHA_URL

        if req.captcha_code:
            # 使用手动输入的验证码登录
            # 检查是否有缓存的 session
            session_data = None
            with _captcha_sessions_lock:
                session_data = _captcha_sessions.get(req.session_id) if req.session_id else None

            if session_data:
                # 使用已有的 session 和 execution（token 与图片同源抓取，须成对提交）
                result = await run_in_threadpool(
                    attempt_login_with_captcha,
                    session_data["session"],
                    session_data["cas_login_url"],
                    session_data["execution_value"],
                    req.username,
                    req.password,
                    req.captcha_code,
                    session_data.get("login_page_html"),
                    captcha_token=session_data.get("captcha_token", "")
                )
            else:
                # 没有缓存的 session，重新创建
                result = await run_in_threadpool(
                    login_with_manual_captcha,
                    login_url, captcha_url, req.username, req.password, req.captcha_code
                )
        else:
            # 使用 OCR 自动识别验证码登录
            result = await run_in_threadpool(
                login_with_auto_captcha,
                login_url, captcha_url, req.username, req.password
            )

        # 登录后删除使用的 session
        if req.session_id:
            with _captcha_sessions_lock:
                _captcha_sessions.pop(req.session_id, None)

        if result.success:
            # 缓存 token
            cache_token_for_user(req.username, result.tokens)
            # 保存账号密码（登录成功后）
            save_user_account(req.username, req.password, login_url, captcha_url)

            return LoginResponse(
                ok=True,
                data={"access_token": result.tokens.get("access_token")}
            )

        # 登录失败
        error_type = result.error_type.value if result.error_type else "unknown_error"
        need_manual = result.error_type == LoginErrorType.CAPTCHA_ERROR

        return LoginResponse(
            ok=False,
            error=result.message,
            error_type=error_type,
            need_manual_captcha=need_manual
        )

    except Exception as e:
        logger.error(f"登录失败: {e}")
        return LoginResponse(ok=False, error=str(e), error_type="network_error")


@router.post("/logout")
async def api_logout(req: LogoutRequest):
    """登出接口，清除 token 缓存。"""
    clear_token_cache(req.username)
    logger.info(f"用户已登出，token 缓存已清除")
    return {"ok": True}
