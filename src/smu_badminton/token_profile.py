"""
Token 缓存模块。

负责：
- Token 缓存（access_token/id_token）
- JWT 解析、用户信息提取
- Profile 缓存
- 用户账号保存
"""
import time
import json
import base64
import threading
import logging
from typing import Any, Dict, Optional, Tuple

from .config import (
    TOKEN_PROFILE_TTL_SEC,
    TOKEN_CACHE_TTL_SEC,
    CAS_LOGIN_URL,
    CAS_CAPTCHA_URL,
    DEFAULT_DEPT_CODE,
    DEFAULT_DEPT_NAME,
    DEFAULT_DEPT_NAME_EN,
    DEFAULT_USER_EMAIL,
    DEFAULT_USER_PHONE,
)

logger = logging.getLogger(__name__)


# ============= Token 缓存 =============

_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}
_TOKEN_LOCK = threading.Lock()


# ============= Profile 缓存 =============

_TOKEN_PROFILE_CACHE: Dict[str, Dict[str, Any]] = {}
_TOKEN_PROFILE_LOCK = threading.Lock()


def decode_jwt_payload(token: str) -> Dict[str, Any] | None:
    """解析 JWT payload（不校验签名）。"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def profile_from_claims(claims: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """从 JWT claims 提取用户 profile。"""
    if not claims:
        return None

    user_code = (
        claims.get("userCode")
        or claims.get("userName")
        or claims.get("account")
        or claims.get("loginName")
        or claims.get("sub")
    )
    if not user_code:
        return None

    display_name = claims.get("name") or claims.get("realName") or claims.get("cn") or str(user_code)
    dept_code = claims.get("deptCode") or DEFAULT_DEPT_CODE
    dept_name = claims.get("deptName") or DEFAULT_DEPT_NAME
    dept_name_en = claims.get("deptNameEn") or DEFAULT_DEPT_NAME_EN
    email = claims.get("email") or DEFAULT_USER_EMAIL or f"{user_code}@stu.shmtu.edu.cn"
    phone = claims.get("phone") or claims.get("mobile") or claims.get("telephone") or DEFAULT_USER_PHONE

    return {
        "user_code": str(user_code),
        "display_name": str(display_name),
        "dept_code": str(dept_code),
        "dept_name": str(dept_name),
        "dept_name_en": str(dept_name_en),
        "email": str(email),
        "phone": str(phone),
    }


def token_exp_epoch(access_token: str) -> Optional[float]:
    """读取 access_token JWT 里的 exp（Unix 秒）。无法解析返回 None。"""
    claims = decode_jwt_payload(access_token)
    if not claims:
        return None
    exp = claims.get("exp")
    try:
        return float(exp) if exp else None
    except (TypeError, ValueError):
        return None


def _cleanup_profile_cache():
    """清理过期的 profile 缓存项。"""
    now = time.time()
    expired = [
        token for token, entry in _TOKEN_PROFILE_CACHE.items()
        if now - float(entry.get("ts", 0)) >= float(TOKEN_PROFILE_TTL_SEC)
    ]
    for token in expired:
        _TOKEN_PROFILE_CACHE.pop(token, None)
    if expired:
        logger.debug("清理了 %d 个过期 token profile 缓存", len(expired))


def cache_profile_from_tokens(tokens: Dict[str, Any] | None):
    """从 tokens 缓存用户 profile。"""
    if not tokens:
        return
    access_token = tokens.get("access_token")
    if not access_token:
        return

    profile = (
        profile_from_claims(decode_jwt_payload(tokens.get("id_token", "")))
        or profile_from_claims(decode_jwt_payload(access_token))
    )
    if not profile:
        return

    with _TOKEN_PROFILE_LOCK:
        _cleanup_profile_cache()
        _TOKEN_PROFILE_CACHE[access_token] = {"profile": profile, "ts": time.time()}


def get_profile_by_access_token(access_token: str) -> Dict[str, Any] | None:
    """通过 access_token 获取缓存的 profile。"""
    now = time.time()
    with _TOKEN_PROFILE_LOCK:
        entry = _TOKEN_PROFILE_CACHE.get(access_token)
        if entry and now - float(entry.get("ts", 0)) < float(TOKEN_PROFILE_TTL_SEC):
            return entry.get("profile")
    return profile_from_claims(decode_jwt_payload(access_token))


def build_user_info_from_profile(profile: Dict[str, Any] | None) -> Dict[str, Any] | None:
    """从 profile 构建预约所需的用户信息结构。"""
    if not profile:
        return None

    user_code = profile.get("user_code", "")
    display_name = profile.get("display_name", user_code)
    dept_code = profile.get("dept_code", "")
    dept_name = profile.get("dept_name", "")
    dept_name_en = profile.get("dept_name_en", "")
    email = profile.get("email", "")
    phone = profile.get("phone", "")

    participant_info = {
        "participant_id": user_code,
        "participant_name": display_name,
        "participant_dept_id": dept_code,
        "participant_dept_name": dept_name,
        "mobile": phone,
        "email": email,
        "operate_user_id": user_code,
        "operate_user_name": display_name,
    }
    return {
        "created_user": user_code,
        "created_user_name": display_name,
        "appointment_user": user_code,
        "appointment_user_name": display_name,
        "dept_code": dept_code,
        "dept_name": dept_name,
        "dept_name_en": dept_name_en,
        "email": email,
        "phone": phone,
        "participant_info": participant_info,
    }


def clear_profile_cache(access_token: str = None):
    """清理 profile 缓存；若指定 access_token，仅清理该 token。"""
    with _TOKEN_PROFILE_LOCK:
        if access_token:
            _TOKEN_PROFILE_CACHE.pop(access_token, None)
        else:
            _TOKEN_PROFILE_CACHE.clear()


# ============= Token 缓存操作 =============

def _cleanup_token_cache(now: float, ttl_seconds: float) -> None:
    """清理过期的 token 缓存项。

    注意：调用者必须持有 _TOKEN_LOCK。
    """
    expired_users = [
        username
        for username, entry in _TOKEN_CACHE.items()
        if now - float(entry.get("ts", 0)) >= float(ttl_seconds)
    ]
    for username in expired_users:
        _TOKEN_CACHE.pop(username, None)
    if expired_users:
        logger.debug("清理了 %d 个过期 token 缓存", len(expired_users))


def get_cached_token(username: str, ttl_seconds: int = None) -> Optional[Dict[str, str]]:
    """
    获取缓存的 token（不触发登录）。

    Args:
        username: 用户名
        ttl_seconds: 缓存 TTL 秒数，默认使用 TOKEN_CACHE_TTL_SEC

    Returns:
        token 字典（包含 access_token 和 id_token），未找到返回 None
    """
    if ttl_seconds is None:
        ttl_seconds = TOKEN_CACHE_TTL_SEC

    with _TOKEN_LOCK:
        now = time.time()
        _cleanup_token_cache(now, ttl_seconds)
        entry = _TOKEN_CACHE.get(username)
        if entry and entry.get("tokens") and entry["tokens"].get("access_token"):
            return entry["tokens"]
    return None


def cache_token_for_user(username: str, tokens: Dict[str, Any]) -> None:
    """
    缓存用户的 token（登录成功后调用）。

    Args:
        username: 用户名
        tokens: 包含 access_token 和 id_token 的字典
    """
    with _TOKEN_LOCK:
        _TOKEN_CACHE[username] = {"tokens": tokens, "ts": time.time()}
    cache_profile_from_tokens(tokens)


def find_user_by_access_token(access_token: str) -> Tuple[str, str]:
    """
    通过 access_token 查找用户名和 id_token。

    Args:
        access_token: 访问令牌

    Returns:
        (username, id_token) 元组，未找到返回 ("", "")
    """
    with _TOKEN_LOCK:
        for username, entry in _TOKEN_CACHE.items():
            if entry.get("tokens", {}).get("access_token") == access_token:
                id_token = entry["tokens"].get("id_token", "")
                return username, id_token
    return "", ""


def clear_token_cache(username: Optional[str] = None) -> None:
    """
    清理 token 缓存。

    Args:
        username: 若指定，仅清理该用户；若为 None，清理所有
    """
    with _TOKEN_LOCK:
        if username:
            entry = _TOKEN_CACHE.pop(username, None)
            if entry and entry.get("tokens") and entry["tokens"].get("access_token"):
                clear_profile_cache(entry["tokens"]["access_token"])
        else:
            _TOKEN_CACHE.clear()
            clear_profile_cache()


# ============= 用户账号保存 =============

def save_user_account(
    username: str,
    password: str,
    login_url: str = None,
    captcha_url: str = None
) -> bool:
    """
    保存用户账号（登录成功后调用）。

    密码会进行混淆处理，非明文存储。

    Args:
        username: 用户名
        password: 密码（明文）
        login_url: 登录 URL，可选
        captcha_url: 验证码 URL，可选

    Returns:
        是否保存成功
    """
    from .core_utils import get_db_pool, obfuscate_password

    if not username or not password:
        return False

    login_url = login_url or CAS_LOGIN_URL
    captcha_url = captcha_url or CAS_CAPTCHA_URL
    obfuscated_pwd = obfuscate_password(password)
    now = time.time()

    try:
        with get_db_pool().get_connection() as conn:
            # 使用 UPSERT（INSERT OR REPLACE）
            conn.execute(
                """
                INSERT INTO user_accounts (username, password, login_url, captcha_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    password = excluded.password,
                    login_url = excluded.login_url,
                    captcha_url = excluded.captcha_url,
                    updated_at = excluded.updated_at
                """,
                (username, obfuscated_pwd, login_url, captcha_url, now, now)
            )
        logger.info("用户账号已保存: %s", username)
        return True
    except Exception as e:
        logger.error("保存用户账号失败: %s", e)
        return False


def get_user_account(username: str) -> Optional[Dict[str, str]]:
    """
    获取保存的用户账号信息。

    Args:
        username: 用户名

    Returns:
        包含 username, password（明文）, login_url, captcha_url 的字典，未找到返回 None
    """
    from .core_utils import get_db_pool, deobfuscate_password

    if not username:
        return None

    try:
        with get_db_pool().get_connection() as conn:
            cur = conn.execute(
                "SELECT username, password, login_url, captcha_url FROM user_accounts WHERE username = ?",
                (username,)
            )
            row = cur.fetchone()
            if row:
                return {
                    "username": row[0],
                    "password": deobfuscate_password(row[1]),
                    "login_url": row[2],
                    "captcha_url": row[3],
                }
    except Exception as e:
        logger.error("获取用户账号失败: %s", e)
    return None


def delete_user_account(username: str) -> bool:
    """
    删除用户账号。

    Args:
        username: 用户名

    Returns:
        是否删除成功
    """
    from .core_utils import get_db_pool

    try:
        with get_db_pool().get_connection() as conn:
            conn.execute("DELETE FROM user_accounts WHERE username = ?", (username,))
        return True
    except Exception as e:
        logger.error("删除用户账号失败: %s", e)
        return False


def refresh_token_for_user(username: str, max_attempts: int = 2) -> Optional[Dict[str, str]]:
    """
    刷新用户 token（从数据库获取账号密码并重新登录）。

    用于 token 过期时自动重新登录。

    Args:
        username: 用户名
        max_attempts: 最大登录尝试次数，默认 2 次

    Returns:
        新的 token 字典，失败返回 None
    """
    from .cas_login import login_with_retry

    account = get_user_account(username)
    if not account:
        logger.warning("无法刷新 token：未找到用户账号 %s", username)
        return None

    login_url = account.get("login_url") or CAS_LOGIN_URL
    captcha_url = account.get("captcha_url") or CAS_CAPTCHA_URL
    password = account.get("password")

    if not password:
        logger.warning("无法刷新 token：用户 %s 密码为空", username)
        return None

    # 尝试登录（最多 max_attempts 次）
    for attempt in range(max_attempts):
        logger.info("刷新 token 尝试 %d/%d: %s", attempt + 1, max_attempts, username)
        tokens = login_with_retry(login_url, captcha_url, username, password, max_retries=3)
        if tokens and tokens.get("access_token"):
            cache_token_for_user(username, tokens)
            logger.info("刷新 token 成功: %s", username)
            return tokens
        logger.warning("刷新 token 失败 (attempt %d/%d): %s", attempt + 1, max_attempts, username)

    logger.error("刷新 token 最终失败: %s（已尝试 %d 次）", username, max_attempts)
    return None
