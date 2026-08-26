"""
CAS 认证流程模块。

包含 CAS 登录、重定向解析、OIDC token 提取等功能。

接口规范：
- LoginResult 为统一返回结构
- extract_oidc_tokens 失败返回 None（而非空字典）
- 所有公开函数参数顺序：必需参数在前，可选参数在后
"""
import base64
import json
import requests
from urllib.parse import urlparse, parse_qs, urlencode, quote, urljoin, unquote
from lxml import html
from .cas_ocr import predict_validate_code
import os
import time
import logging
from typing import Any, Dict, Tuple, Optional
from enum import Enum
from dataclasses import dataclass

from .config import (
    WF_ORIGIN,
    WF_HOME_URL,
    CAS_ORIGIN,
    CAS_CAPTCHA_URL,
    OAUTH_CLIENT_ID,
)

logger = logging.getLogger(__name__)


class LoginErrorType(Enum):
    """登录错误类型。"""
    SUCCESS = "success"  # 登录成功
    CAPTCHA_ERROR = "captcha_error"  # 验证码错误
    PASSWORD_ERROR = "password_error"  # 密码错误
    NETWORK_ERROR = "network_error"  # 网络错误
    UNKNOWN_ERROR = "unknown_error"  # 未知错误


@dataclass
class LoginResult:
    """登录结果。

    Attributes:
        error_type: 错误类型
        tokens: 成功时返回的 token 字典，包含 access_token 和 id_token
        session: requests.Session 对象（可用于后续请求）
        message: 错误信息
    """
    error_type: LoginErrorType = LoginErrorType.UNKNOWN_ERROR
    tokens: Optional[Dict[str, str]] = None
    session: Optional[requests.Session] = None
    message: str = ""

    @property
    def success(self) -> bool:
        """是否登录成功。"""
        return self.error_type == LoginErrorType.SUCCESS and self.tokens is not None


def _debug(msg: str):
    """调试日志输出（需要从外部导入 BOOKING_DEBUG）。"""
    from .config import BOOKING_DEBUG
    if BOOKING_DEBUG:
        logger.debug(msg)


def _absolute_url(base_url: str, maybe_relative: str) -> str:
    if not maybe_relative:
        return ""
    return urljoin(base_url, maybe_relative)


def _build_wf_authorize_url(ret_url: str | None = None) -> str:
    ret = ret_url or f"{WF_ORIGIN}/yy-sys/pc/home"
    callback = f"{WF_ORIGIN}/yy-sys/oidc-callback?retUrl={ret}"
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": callback,
        "response_type": "id_token token",
        "scope": "data openid process task app submit process_edit start profile",
        "state": os.urandom(16).hex(),
        "nonce": os.urandom(16).hex(),
    }
    return f"{WF_ORIGIN}/sso/oauth2/authorize?{urlencode(params, quote_via=quote)}"


def _is_cas_login_url(url: str) -> bool:
    """判断 URL 是否为 CAS 登录页。

    兼容迁移前的 cas.shmtu.edu.cn 与迁移后的 sso.shmtu.edu.cn：只要 netloc 属于
    shmtu.edu.cn 且 path 以 /cas/login 开头即认定，日后再次更换主机名也不会失配。
    用 urlparse 取 netloc/path，避免被 service= 等查询参数里的子串误判。
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.netloc.lower().endswith("shmtu.edu.cn") and parsed.path.startswith("/cas/login")


def _resolve_cas_login_url(session: requests.Session, login_url: str | None, timeout: int = 20) -> str:
    """沿着 WF -> SSO 重定向链解析 CAS 登录 URL。"""
    start = (login_url or "").strip()
    lower_start = start.lower()

    # 直接就是 CAS 登录地址（兼容 cas./sso. 主机）
    if _is_cas_login_url(start):
        return start

    # 已经是 oauth2 authorize 地址
    if "/sso/oauth2/authorize" in lower_start:
        pass
    # 已经是 sso 登录地址
    elif "/sso/login" in lower_start:
        pass
    # yy-sys 或其他 wf 页面：基于 retUrl 重建 authorize 地址
    elif start and lower_start.startswith(WF_ORIGIN.lower()):
        start = _build_wf_authorize_url(ret_url=start)
    # 输入为空或无效时兜底
    else:
        start = _build_wf_authorize_url(ret_url=WF_HOME_URL)

    current = start
    headers = {"User-Agent": "Mozilla/5.0"}
    for _ in range(10):
        resp = session.get(current, headers=headers, timeout=timeout, allow_redirects=False)
        if _is_cas_login_url(current):
            return current

        location = _absolute_url(current, resp.headers.get("Location", ""))
        if not location:
            if resp.status_code == 200 and _is_cas_login_url(current):
                return current
            break

        if _is_cas_login_url(location):
            return location
        current = location

    raise RuntimeError("Cannot resolve CAS login URL from WF redirect chain")


def follow_redirects(session, start_url):
    """沿重定向链跟随并返回最终 URL 与响应体。"""
    current_url = start_url
    max_redirects = 10
    redirect_count = 0

    while redirect_count < max_redirects:
        resp = session.get(current_url, allow_redirects=False, timeout=20)
        if resp.status_code in (301, 302, 303) and 'Location' in resp.headers:
            current_url = _absolute_url(current_url, resp.headers['Location'])
            redirect_count += 1
        else:
            return current_url, resp.text

    return current_url, ""


def extract_oidc_tokens(url: str) -> Optional[Dict[str, str]]:
    """
    从 URL 的 fragment 或 query 参数中提取 OIDC token。

    Args:
        url: 包含 token 的 URL

    Returns:
        包含 access_token 和 id_token 的字典，失败返回 None（不返回空字典）
    """
    parsed_url = urlparse(url)
    tokens: Dict[str, str] = {}

    # 检查 URL fragment 是否包含 token
    if parsed_url.fragment:
        fragment_params = parse_qs(parsed_url.fragment)
        if 'access_token' in fragment_params:
            tokens['access_token'] = fragment_params['access_token'][0]
        if 'id_token' in fragment_params:
            tokens['id_token'] = fragment_params['id_token'][0]

    # 如果 fragment 中没有，检查 URL query 参数
    if not tokens and parsed_url.query:
        query_params = parse_qs(parsed_url.query)
        if 'access_token' in query_params:
            tokens['access_token'] = query_params['access_token'][0]
        if 'id_token' in query_params:
            tokens['id_token'] = query_params['id_token'][0]

    return tokens if tokens else None


def _extract_tokens_after_login(session: requests.Session, cas_login_url: str, post_resp: requests.Response) -> Optional[Dict]:
    """从登录响应中提取 OIDC tokens。

    处理两种情况：
    1. 直接从重定向 URL 的 fragment 中提取
    2. 通过 ticket 交换 token

    Args:
        session: 已认证的会话
        cas_login_url: CAS 登录 URL
        post_resp: 登录 POST 响应

    Returns:
        tokens 字典或 None
    """
    # 检查是否登录成功（重定向）
    if post_resp.status_code not in (301, 302, 303) or "Location" not in post_resp.headers:
        return None

    redirect_url = _absolute_url(cas_login_url, post_resp.headers["Location"])
    final_url, _ = follow_redirects(session, redirect_url)

    # 尝试直接从 URL 提取 token
    tokens = extract_oidc_tokens(final_url)
    if tokens:
        return tokens

    # 尝试通过 ticket 交换 token
    parsed = urlparse(final_url)
    query = parse_qs(parsed.query)
    if "redirect_uri" in query and "ticket" in query:
        redirect_uri = unquote(query["redirect_uri"][0])
        ticket = query["ticket"][0]
        oidc_url = f"{redirect_uri}&ticket={ticket}"
        try:
            r2 = session.get(oidc_url, allow_redirects=True, timeout=20)
            tokens = extract_oidc_tokens(r2.url)
            if tokens:
                return tokens
        except Exception as e:
            _debug(f"token exchange via ticket failed: {e}")

    return None


def _derive_captcha_url(cas_login_url: str, captcha_url: str | None) -> str:
    """推导验证码 URL，确保与登录页同源。

    验证码是 session 绑定的（JSESSIONID）：必须从登录页所在 host 抓取，否则提交的
    validateCode/captchaToken 与服务端记录不符，必判验证码错误。优先用显式传入的
    captcha_url（仅当其 host 与登录页一致）；否则从解析到的登录页 host 推导，避免
    .env 残留旧 cas. 地址导致跨 host 抓取（这正是旧版登录必挂的根因之一）。
    """
    login_host = urlparse(cas_login_url).netloc.lower() if cas_login_url else ""
    if captcha_url and captcha_url.strip():
        cap_host = urlparse(captcha_url.strip()).netloc.lower()
        if not login_host or cap_host == login_host:
            return captcha_url.strip()
    if login_host:
        scheme = urlparse(cas_login_url).scheme or "https"
        return f"{scheme}://{login_host}/cas/captcha"
    return CAS_CAPTCHA_URL


def _fetch_captcha_challenge(session: requests.Session, captcha_url: str, headers: dict | None = None) -> Tuple[bytes, str]:
    """获取验证码挑战，返回 (image_bytes, token)。

    新版 /cas/captcha 返回 JSON: {"image":"data:image/png;base64,...","token":"v1...","expiresAt":<ms>}；
    旧版直接返回 PNG bytes（token 为空串）。对两者均兼容。带 ?_=<ms> 缓存破坏；
    session 自带 JSESSIONID，须与登录页同源（由 _derive_captcha_url 保证）。
    """
    url = (captcha_url or CAS_CAPTCHA_URL).strip()
    params = {"_": str(int(time.time() * 1000))}
    resp = session.get(url, params=params, headers=headers, timeout=15)
    content_type = resp.headers.get("Content-Type", "").lower()
    # 新版 JSON（按 Content-Type 或首字符嗅探）
    if "json" in content_type or resp.content[:1] == b"{":
        try:
            obj = resp.json()
        except Exception:
            obj = {}
        data_url = obj.get("image", "") or ""
        token = obj.get("token", "") or ""
        if data_url.startswith("data:") and "," in data_url:
            b64 = data_url.split(",", 1)[1]
            try:
                return base64.b64decode(b64), token
            except Exception:
                return b"", token
        # 兜底：image 字段直接是裸 base64
        try:
            return base64.b64decode(data_url), token
        except Exception:
            return b"", token
    # 旧版：直接是图片 bytes
    return resp.content, ""


def _prepare_login_session_core(login_url: str, captcha_url: str | None = None) -> Tuple[requests.Session, str, str, bytes, str, str]:
    """准备登录会话的核心逻辑。

    Args:
        login_url: 登录入口 URL
        captcha_url: 验证码 URL（可选，仅当 host 与登录页一致时采用）

    Returns:
        Tuple: (session, cas_login_url, execution_value, captcha_image_bytes, captcha_token, login_page_html)
    """
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    }

    # 解析 CAS 登录 URL
    cas_login_url = _resolve_cas_login_url(session, login_url, timeout=20)

    # 获取登录页面（同时绑定 JSESSIONID）
    login_resp = session.get(cas_login_url, headers=headers, timeout=20)
    login_page_html = login_resp.text

    # 提取 execution 参数
    execution_value = _stable_extract_execution(login_page_html)
    if not execution_value:
        raise RuntimeError("无法获取 execution 参数")

    # 获取验证码（新版 JSON：image base64 + token），须与登录页同源
    captcha_url = _derive_captcha_url(cas_login_url, captcha_url)
    captcha_image, captcha_token = _fetch_captcha_challenge(session, captcha_url, headers=headers)

    return session, cas_login_url, execution_value, captcha_image, captcha_token, login_page_html


def get_captcha_and_params(login_url, captcha_url):
    """从 WF 流程解析 CAS 登录地址，并获取验证码、token 与 execution。"""
    session, cas_login_url, execution_value, captcha_image, captcha_token, _ = _prepare_login_session_core(login_url, captcha_url)
    # OCR 识别验证码
    result, *_ = predict_validate_code(captcha_image)
    return session, cas_login_url, execution_value, result, captcha_token


# cas_login 已移除，统一使用 cas_login_stable


# ---- 稳定登录兜底实现 ----
from lxml import html as lxml_html


def _stable_extract_execution(html_text: str):
    tree = lxml_html.fromstring(html_text)
    xps = [
        "//input[@name='execution']/@value",
        "//input[@id='execution']/@value",
        "//*[@id='login-form-controls']//input[@name='execution']/@value",
        "//form//input[@name='execution']/@value",
    ]
    for xp in xps:
        vals = tree.xpath(xp)
        if vals:
            return vals[0]
    return None


def _stable_detect_event_order(html_text: str):
    """检测登录事件顺序。

    注意：CAS 服务器只支持 submit, resetPassword, forgotUsername
    不支持 passwordlessLogin，所以只返回 submit
    """
    return ["submit"]


def _stable_download_captcha(session: requests.Session, cas_login_url: str, captcha_url: str | None) -> Tuple[str, str]:
    """获取并 OCR 验证码，返回 (code, token)。

    验证码 URL 从 cas_login_url 推导以保证同源（见 _derive_captcha_url）。
    """
    url = _derive_captcha_url(cas_login_url, captcha_url)
    img, token = _fetch_captcha_challenge(session, url)
    code, *_ = predict_validate_code(img)
    return code, token


def cas_login_stable(login_url, captcha_url, username, password) -> LoginResult:
    """更稳健的 WF->SSO->CAS 重定向链登录实现。"""
    session = None
    last_error_type = LoginErrorType.UNKNOWN_ERROR
    last_message = "登录失败"

    for _attempt in range(1, 4):
        session = requests.Session()
        headers_get = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
        }
        try:
            cas_url = _resolve_cas_login_url(session, login_url, timeout=20)
            login_resp = session.get(cas_url, headers=headers_get, timeout=20)
        except Exception as e:
            _debug(f"cas_login_stable resolve/get failed: {e}")
            last_error_type = LoginErrorType.NETWORK_ERROR
            last_message = str(e)
            continue

        execution_value = _stable_extract_execution(login_resp.text)
        if not execution_value:
            last_message = "无法获取 execution 参数"
            continue

        event_order = _stable_detect_event_order(login_resp.text)
        captcha, captcha_token = _stable_download_captcha(session, cas_url, captcha_url)
        headers_post = {
            "User-Agent": headers_get["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": CAS_ORIGIN,
            "Referer": cas_url,
        }

        for evt in event_order:
            data = {
                "username": username,
                "password": password,
                "execution": execution_value,
                "_eventId": evt,
                "geolocation": "",
                "validateCode": captcha,
                "captchaToken": captcha_token,
                "deviceFingerprint": "",
            }
            try:
                post_resp = session.post(cas_url, data=data, headers=headers_post, allow_redirects=False, timeout=20)
            except Exception as e:
                last_error_type = LoginErrorType.NETWORK_ERROR
                last_message = str(e)
                continue

            tokens = _extract_tokens_after_login(session, cas_url, post_resp)
            if tokens:
                return LoginResult(LoginErrorType.SUCCESS, tokens=tokens, session=session)

            # 检测错误类型
            if post_resp.status_code == 200:
                last_error_type = _detect_login_error(post_resp.text)
                last_message = f"登录失败: {last_error_type.value}"

        time.sleep(0.5)

    return LoginResult(last_error_type, session=session or requests.Session(), message=last_message)


def login_with_retry(login_url, captcha_url, username, password, max_retries=3) -> Optional[Dict]:
    """带重试的登录。返回 tokens 或 None。

    总尝试次数 = max_retries × 3 (cas_login_stable 内部重试)
    默认 3 × 3 = 9 次，控制在 10 次以内。
    """
    for attempt in range(1, max_retries + 1):
        _debug(f"login attempt {attempt}/{max_retries}")

        try:
            result = cas_login_stable(login_url, captcha_url, username, password)
        except Exception as e:
            _debug(f"cas_login_stable exception: {e}")
            time.sleep(0.3)
            continue

        if result.success and result.tokens and result.tokens.get("access_token"):
            return result.tokens

        # 密码错误不重试
        if result.error_type == LoginErrorType.PASSWORD_ERROR:
            logger.warning("密码错误，停止重试")
            return None

        time.sleep(0.3)

    return None


# ============= 新的登录逻辑：支持错误检测和手动验证码 =============

def _detect_login_error(html_text: str) -> LoginErrorType:
    """检测登录错误类型。

    Args:
        html_text: 登录响应的 HTML 内容

    Returns:
        LoginErrorType: 错误类型
    """
    # 检查验证码错误
    if "验证码错误" in html_text:
        logger.info("检测到验证码错误")
        return LoginErrorType.CAPTCHA_ERROR

    # 检查密码错误
    if "认证失败" in html_text or "用户名或密码不正确" in html_text:
        logger.info("检测到密码错误")
        return LoginErrorType.PASSWORD_ERROR

    # 检查其他可能的错误提示
    if "验证码必须输入" in html_text or "Captcha is a required field" in html_text:
        logger.info("检测到验证码必须输入")
        return LoginErrorType.CAPTCHA_ERROR

    # 打印部分 HTML 用于调试 - 查找错误信息
    # 查找 loginErrorsPanel 或其他错误提示
    import re
    error_match = re.search(r'<div[^>]*id="loginErrorsPanel"[^>]*>.*?<p>(.*?)</p>', html_text, re.DOTALL)
    if error_match:
        error_msg = error_match.group(1).strip()
        logger.warning(f"CAS 错误信息: {error_msg}")

    logger.warning(f"未识别的错误类型，HTML片段: {html_text[:1000] if len(html_text) > 1000 else html_text}")

    return LoginErrorType.UNKNOWN_ERROR


def prepare_login_session(login_url: str, captcha_url: str | None = None) -> Tuple[requests.Session, str, str, bytes, str, str]:
    """准备登录会话，获取验证码图片和必要的参数。

    Args:
        login_url: 登录入口 URL
        captcha_url: 验证码 URL（可选）

    Returns:
        Tuple: (session, cas_login_url, execution_value, captcha_image_bytes, captcha_token, login_page_html)
    """
    session, cas_login_url, execution_value, captcha_image, captcha_token, login_page_html = _prepare_login_session_core(login_url, captcha_url)
    logger.info(f"验证码图片获取成功, 大小: {len(captcha_image)} bytes")
    return session, cas_login_url, execution_value, captcha_image, captcha_token, login_page_html


def attempt_login_with_captcha(
    session: requests.Session,
    cas_login_url: str,
    execution_value: str,
    username: str,
    password: str,
    captcha_code: str,
    login_page_html: str | None = None,
    captcha_token: str = ""
) -> LoginResult:
    """使用指定的验证码尝试登录。

    Args:
        session: 已初始化的会话
        cas_login_url: CAS 登录 URL
        execution_value: execution 参数值
        username: 用户名
        password: 密码
        captcha_code: 验证码（计算结果）
        login_page_html: 登录页面 HTML（可选，用于检测事件顺序）
        captcha_token: 验证码挑战 token（与图片同源抓取，须成对提交，默认空串兜底）

    Returns:
        LoginResult: 登录结果
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": CAS_ORIGIN,
        "Referer": cas_login_url,
    }

    # 检测事件顺序（使用传入的 HTML 或重新获取）
    if login_page_html:
        event_order = _stable_detect_event_order(login_page_html)
    else:
        login_page_resp = session.get(cas_login_url, headers=headers, timeout=20)
        event_order = _stable_detect_event_order(login_page_resp.text)

    for evt in event_order:
        data = {
            "username": username,
            "password": password,
            "execution": execution_value,
            "_eventId": evt,
            "geolocation": "",
            "validateCode": captcha_code,
            "captchaToken": captcha_token,
            "deviceFingerprint": "",
        }

        try:
            post_resp = session.post(
                cas_login_url, data=data, headers=headers, allow_redirects=False, timeout=20
            )
            logger.info(f"登录请求发送: status={post_resp.status_code}")
        except Exception as e:
            logger.warning(f"登录请求异常: {e}")
            return LoginResult(LoginErrorType.NETWORK_ERROR, message=str(e))

        # 尝试提取 token
        tokens = _extract_tokens_after_login(session, cas_login_url, post_resp)
        if tokens:
            return LoginResult(LoginErrorType.SUCCESS, tokens=tokens, session=session)

        # 登录失败，检查错误类型
        error_type = _detect_login_error(post_resp.text)
        logger.info(f"登录响应状态码: {post_resp.status_code}, 错误类型: {error_type}")
        return LoginResult(error_type, session=session, message=f"登录失败: {error_type.value}")

    return LoginResult(LoginErrorType.UNKNOWN_ERROR, session=session, message="未知错误")


def login_with_auto_captcha(
    login_url: str,
    captcha_url: str | None,
    username: str,
    password: str,
    max_ocr_attempts: int = 2
) -> LoginResult:
    """使用 OCR 自动识别验证码登录。

    流程：
    1. 第一次尝试：OCR 识别验证码登录
    2. 如果验证码错误，第二次尝试：重新获取验证码，OCR 识别后登录
    3. 如果仍然验证码错误，返回需要手动输入验证码的提示

    Args:
        login_url: 登录入口 URL
        captcha_url: 验证码 URL
        username: 用户名
        password: 密码
        max_ocr_attempts: 最大 OCR 尝试次数（默认 2 次）

    Returns:
        LoginResult: 登录结果
    """
    for attempt in range(1, max_ocr_attempts + 1):
        logger.info(f"OCR 登录尝试 {attempt}/{max_ocr_attempts}")

        try:
            # 准备登录会话
            session, cas_login_url, execution_value, captcha_image, captcha_token, login_page_html = prepare_login_session(
                login_url, captcha_url
            )

            # OCR 识别验证码
            captcha_code, expr, *_ = predict_validate_code(captcha_image)
            logger.info(f"OCR 识别验证码: 表达式={expr}, 图片大小={len(captcha_image)} bytes")

            # 尝试登录
            result = attempt_login_with_captcha(
                session, cas_login_url, execution_value, username, password, captcha_code, login_page_html,
                captcha_token=captcha_token
            )

            if result.success:
                logger.info("登录成功")
                return result

            # 如果是密码错误，直接返回，不需要重试
            if result.error_type == LoginErrorType.PASSWORD_ERROR:
                logger.warning("密码错误，停止重试")
                return result

            # 如果是验证码错误，继续尝试
            if result.error_type == LoginErrorType.CAPTCHA_ERROR:
                logger.warning(f"验证码错误，尝试 {attempt + 1}/{max_ocr_attempts}")
                continue

            # 其他错误，返回结果
            return result

        except Exception as e:
            logger.error(f"登录过程异常: {e}")
            return LoginResult(LoginErrorType.NETWORK_ERROR, message=str(e))

    # OCR 尝试次数用完，需要手动输入验证码
    logger.info("OCR 尝试次数用完，需要手动输入验证码")
    return LoginResult(
        LoginErrorType.CAPTCHA_ERROR,
        message="验证码识别失败，需要手动输入"
    )


def login_with_manual_captcha(
    login_url: str,
    captcha_url: str | None,
    username: str,
    password: str,
    captcha_code: str
) -> LoginResult:
    """使用手动输入的验证码登录。

    Args:
        login_url: 登录入口 URL
        captcha_url: 验证码 URL
        username: 用户名
        password: 密码
        captcha_code: 手动输入的验证码

    Returns:
        LoginResult: 登录结果
    """
    try:
        # 准备登录会话
        session, cas_login_url, execution_value, _, captcha_token, login_page_html = prepare_login_session(
            login_url, captcha_url
        )

        # 使用手动输入的验证码登录
        result = attempt_login_with_captcha(
            session, cas_login_url, execution_value, username, password, captcha_code, login_page_html,
            captcha_token=captcha_token
        )

        return result

    except Exception as e:
        logger.error(f"手动验证码登录异常: {e}")
        return LoginResult(LoginErrorType.NETWORK_ERROR, message=str(e))
