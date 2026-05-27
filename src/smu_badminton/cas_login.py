"""
CAS 认证流程模块。

包含 CAS 登录、重定向解析、OIDC token 提取等功能。
"""
import requests
from urllib.parse import urlparse, parse_qs, urlencode, quote, urljoin, unquote
from lxml import html
from .cas_ocr import predict_validate_code
import os
import time
import logging
from typing import Any, Dict, Tuple, Optional
from enum import Enum

from .config import (
    WF_ORIGIN,
    WF_HOME_URL,
    CAS_ORIGIN,
    CAS_CAPTCHA_URL,
    OAUTH_CLIENT_ID,
    CAS_LOGIN_STABLE_FIRST,
)

logger = logging.getLogger(__name__)


class LoginErrorType(Enum):
    """登录错误类型。"""
    SUCCESS = "success"  # 登录成功
    CAPTCHA_ERROR = "captcha_error"  # 验证码错误
    PASSWORD_ERROR = "password_error"  # 密码错误
    NETWORK_ERROR = "network_error"  # 网络错误
    UNKNOWN_ERROR = "unknown_error"  # 未知错误


class LoginResult:
    """登录结果。"""
    def __init__(self, error_type: LoginErrorType, tokens: Optional[Dict] = None,
                 session: Optional[requests.Session] = None, message: str = ""):
        self.error_type = error_type
        self.tokens = tokens
        self.session = session
        self.message = message

    @property
    def success(self) -> bool:
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


def _resolve_cas_login_url(session: requests.Session, login_url: str | None, timeout: int = 20) -> str:
    """沿着 WF -> SSO 重定向链解析 CAS 登录 URL。"""
    start = (login_url or "").strip()
    lower_start = start.lower()

    # 直接就是 CAS 登录地址
    if "cas.shmtu.edu.cn/cas/login" in lower_start:
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
        if "cas.shmtu.edu.cn/cas/login" in current.lower():
            return current

        location = _absolute_url(current, resp.headers.get("Location", ""))
        if not location:
            if resp.status_code == 200 and "cas.shmtu.edu.cn/cas/login" in current.lower():
                return current
            break

        if "cas.shmtu.edu.cn/cas/login" in location.lower():
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


def extract_oidc_tokens(url):
    """从 URL 的 fragment 或 query 参数中提取 OIDC token。"""
    parsed_url = urlparse(url)

    # 检查 URL fragment 是否包含 token
    if parsed_url.fragment:
        fragment_params = parse_qs(parsed_url.fragment)

        tokens = {}
        if 'access_token' in fragment_params:
            tokens['access_token'] = fragment_params['access_token'][0]
        if 'id_token' in fragment_params:
            tokens['id_token'] = fragment_params['id_token'][0]

        return tokens

    # 检查 URL query 参数是否包含 token
    query_params = parse_qs(parsed_url.query)
    tokens = {}
    if 'access_token' in query_params:
        tokens['access_token'] = query_params['access_token'][0]
    if 'id_token' in query_params:
        tokens['id_token'] = query_params['id_token'][0]

    return tokens if tokens else None


def get_captcha_and_params(login_url, captcha_url):
    """从 WF 流程解析 CAS 登录地址，并获取验证码与 execution。"""
    session = requests.Session()
    captcha_url = (captcha_url or CAS_CAPTCHA_URL).strip()

    cas_login_url = _resolve_cas_login_url(session, login_url, timeout=20)

    # 1) 拉取 CAS 登录页并解析隐藏参数
    resp = session.get(cas_login_url, timeout=20)
    tree = html.fromstring(resp.text)

    execution_candidates = tree.xpath("//input[@name='execution']/@value")
    if not execution_candidates:
        execution_candidates = tree.xpath('//*[@id="login-form-controls"]//input[@name="execution"]/@value')
    if not execution_candidates:
        raise RuntimeError("execution not found on CAS login page")
    execution_value = execution_candidates[0]

    # 2) 下载验证码并在内存中 OCR
    captcha_response = session.get(captcha_url, timeout=15)
    result, *_ = predict_validate_code(captcha_response.content)

    return session, cas_login_url, execution_value, result


def cas_login(login_url, captcha_url, username, password):
    """通过 WF->SSO->CAS 重定向链登录并获取 OIDC token。"""
    session, cas_login_url, execution_value, captcha = get_captcha_and_params(login_url, captcha_url)

    data = {
        'username': username,
        'password': password,
        'execution': execution_value,
        '_eventId': 'submit',
        'validateCode': captcha,
    }

    post_resp = session.post(cas_login_url, data=data, allow_redirects=False, timeout=20)
    if post_resp.status_code not in (301, 302, 303) or 'Location' not in post_resp.headers:
        _debug(f"cas_login failed status={post_resp.status_code}")
        return session, None

    redirect_url = _absolute_url(cas_login_url, post_resp.headers['Location'])
    final_url, _ = follow_redirects(session, redirect_url)
    tokens_direct = extract_oidc_tokens(final_url)
    if tokens_direct:
        return session, tokens_direct

    parsed = urlparse(final_url)
    query = parse_qs(parsed.query)
    if 'redirect_uri' in query and 'ticket' in query:
        redirect_uri = unquote(query['redirect_uri'][0])
        ticket = query['ticket'][0]
        oidc_url = f"{redirect_uri}&ticket={ticket}"
        resp = session.get(oidc_url, allow_redirects=True, timeout=20)
        tokens = extract_oidc_tokens(resp.url)
        if tokens:
            return session, tokens

    _debug("cas_login finished but tokens not found")
    return session, None


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


def _stable_download_captcha(session: requests.Session, captcha_url: str) -> str:
    captcha_url = (captcha_url or CAS_CAPTCHA_URL).strip()
    # 在内存中 OCR 验证码
    r = session.get(captcha_url, timeout=15)
    code, *_ = predict_validate_code(r.content)
    return code


def cas_login_stable(login_url, captcha_url, username, password):
    """更稳健的 WF->SSO->CAS 重定向链登录实现。"""
    session = None
    for _attempt in range(1, 4):
        session = requests.Session()
        headers_get = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        }
        try:
            cas_url = _resolve_cas_login_url(session, login_url, timeout=20)
            login_resp = session.get(cas_url, headers=headers_get, timeout=20)
        except Exception as e:
            _debug(f"cas_login_stable resolve/get failed: {e}")
            continue

        execution_value = _stable_extract_execution(login_resp.text)
        if not execution_value:
            continue

        event_order = _stable_detect_event_order(login_resp.text)
        captcha = _stable_download_captcha(session, captcha_url)
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
                "validateCode": captcha,
            }
            post_resp = session.post(cas_url, data=data, headers=headers_post, allow_redirects=False, timeout=20)
            if post_resp.status_code in (301, 302, 303) and "Location" in post_resp.headers:
                redirect_url = _absolute_url(cas_url, post_resp.headers["Location"])
                final_url, _ = follow_redirects(session, redirect_url)
                tokens_direct = extract_oidc_tokens(final_url)
                if tokens_direct:
                    return session, tokens_direct

                parsed = urlparse(final_url)
                query = parse_qs(parsed.query)
                if "redirect_uri" in query and "ticket" in query:
                    redirect_uri = unquote(query["redirect_uri"][0])
                    ticket = query["ticket"][0]
                    oidc_url = f"{redirect_uri}&ticket={ticket}"
                    r2 = session.get(oidc_url, allow_redirects=True, timeout=20)
                    tokens = extract_oidc_tokens(r2.url)
                    if tokens:
                        return session, tokens
                return session, None

        time.sleep(0.5)

    return session or requests.Session(), None


def login_with_retry(login_url, captcha_url, username, password, max_retries=5):
    """带重试的登录。"""
    prefer_stable_first = CAS_LOGIN_STABLE_FIRST
    primary = cas_login_stable if prefer_stable_first else cas_login
    secondary = cas_login if prefer_stable_first else cas_login_stable

    for attempt in range(1, max_retries + 1):
        _debug(f"login attempt {attempt}/{max_retries}")

        for login_func, tag in ((primary, "primary"), (secondary, "fallback")):
            try:
                _debug(f"login path={tag}, func={login_func.__name__}")
                _session, tokens = login_func(login_url, captcha_url, username, password)
            except Exception as e:
                _debug(f"login func={login_func.__name__} exception: {e}")
                tokens = None

            if tokens and tokens.get("access_token"):
                return tokens

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


def prepare_login_session(login_url: str, captcha_url: str | None = None) -> Tuple[requests.Session, str, str, bytes, str]:
    """准备登录会话，获取验证码图片和必要的参数。

    Args:
        login_url: 登录入口 URL
        captcha_url: 验证码 URL（可选）

    Returns:
        Tuple: (session, cas_login_url, execution_value, captcha_image_bytes, login_page_html)
    """
    session = requests.Session()
    captcha_url = (captcha_url or CAS_CAPTCHA_URL).strip()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    }

    # 解析 CAS 登录 URL
    cas_login_url = _resolve_cas_login_url(session, login_url, timeout=20)

    # 获取登录页面
    login_resp = session.get(cas_login_url, headers=headers, timeout=20)
    login_page_html = login_resp.text

    # 提取 execution 参数
    execution_value = _stable_extract_execution(login_page_html)
    if not execution_value:
        raise RuntimeError("无法获取 execution 参数")

    # 获取验证码图片 - 使用同一个 session 确保验证码与登录页面匹配
    captcha_resp = session.get(captcha_url, headers=headers, timeout=15)
    captcha_image = captcha_resp.content
    logger.info(f"验证码图片获取成功, 大小: {len(captcha_image)} bytes, URL: {captcha_url}")

    return session, cas_login_url, execution_value, captcha_image, login_page_html


def attempt_login_with_captcha(
    session: requests.Session,
    cas_login_url: str,
    execution_value: str,
    username: str,
    password: str,
    captcha_code: str,
    login_page_html: str | None = None
) -> LoginResult:
    """使用指定的验证码尝试登录。

    Args:
        session: 已初始化的会话
        cas_login_url: CAS 登录 URL
        execution_value: execution 参数值
        username: 用户名
        password: 密码
        captcha_code: 验证码
        login_page_html: 登录页面 HTML（可选，用于检测事件顺序）

    Returns:
        LoginResult: 登录结果
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
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
            "validateCode": captcha_code,
        }

        try:
            post_resp = session.post(
                cas_login_url, data=data, headers=headers, allow_redirects=False, timeout=20
            )
            logger.info(f"登录请求发送: username={username}, captcha={captcha_code}, execution={execution_value[:20]}...")
            logger.info(f"登录响应状态码: {post_resp.status_code}, Location: {post_resp.headers.get('Location', 'None')}")
        except Exception as e:
            logger.warning(f"登录请求异常: {e}")
            return LoginResult(LoginErrorType.NETWORK_ERROR, message=str(e))

        # 检查是否登录成功（重定向）
        if post_resp.status_code in (301, 302, 303) and "Location" in post_resp.headers:
            redirect_url = _absolute_url(cas_login_url, post_resp.headers["Location"])
            final_url, _ = follow_redirects(session, redirect_url)
            tokens_direct = extract_oidc_tokens(final_url)
            if tokens_direct:
                return LoginResult(LoginErrorType.SUCCESS, tokens=tokens_direct, session=session)

            # 尝试通过 ticket 获取 token
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
                        return LoginResult(LoginErrorType.SUCCESS, tokens=tokens, session=session)
                except Exception as e:
                    logger.warning(f"获取 token 失败: {e}")

            # 有重定向但没有 token，可能是其他问题
            return LoginResult(LoginErrorType.UNKNOWN_ERROR, session=session, message="重定向成功但未获取到 token")

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
            session, cas_login_url, execution_value, captcha_image, login_page_html = prepare_login_session(
                login_url, captcha_url
            )

            # OCR 识别验证码
            captcha_code, expr, *_ = predict_validate_code(captcha_image)
            logger.info(f"OCR 识别验证码: {captcha_code}, 表达式: {expr}, 图片大小: {len(captcha_image)} bytes")

            # 尝试登录
            result = attempt_login_with_captcha(
                session, cas_login_url, execution_value, username, password, captcha_code, login_page_html
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
        session, cas_login_url, execution_value, _, login_page_html = prepare_login_session(
            login_url, captcha_url
        )

        # 使用手动输入的验证码登录
        result = attempt_login_with_captcha(
            session, cas_login_url, execution_value, username, password, captcha_code, login_page_html
        )

        return result

    except Exception as e:
        logger.error(f"手动验证码登录异常: {e}")
        return LoginResult(LoginErrorType.NETWORK_ERROR, message=str(e))
