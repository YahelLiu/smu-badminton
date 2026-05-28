"""
兼容层模块 - 保持向后兼容。

包含：
- HTTP 重试逻辑
- Token 缓存
- 网络时间同步
- 遗留的并发预约函数
- 向后兼容导出
"""
import requests
import os
import time
import threading
import logging
from typing import Any, Dict
from datetime import datetime, timezone, timedelta

from .config import (
    WF_HOME_URL,
    CAS_LOGIN_URL,
    CAS_CAPTCHA_URL,
    BOOKING_DEBUG,
)

logger = logging.getLogger(__name__)


# ============= 调试日志 =============

def _debug(msg: str):
    if BOOKING_DEBUG:
        logger.debug(msg)


# ============= HTTP 重试逻辑 =============

def _is_ssl_error(error: Exception) -> bool:
    """判断是否为 SSL 相关错误"""
    error_str = str(error).lower()
    return 'ssl' in error_str or 'eof' in error_str or 'protocol' in error_str


def requests_post_with_retry(url, json, headers, max_retries=3, timeout=8):
    """带重试的 POST 请求，使用指数退避策略"""
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=json, headers=headers, timeout=timeout)
            if response.status_code == 200:
                return response
            else:
                logger.warning("request failed status=%d, retrying (%d/%d)", response.status_code, attempt + 1, max_retries)
        except Exception as e:
            logger.warning("request exception=%s, retrying (%d/%d)", e, attempt + 1, max_retries)
            # SSL 错误快速失败，不继续重试
            if _is_ssl_error(e) and attempt >= 1:
                logger.error("SSL error detected, failing fast")
                break
        # 指数退避: 1s, 2s, 4s...
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    logger.error("request failed too many times, giving up")
    return None


def requests_get_with_retry(url, max_retries=3, timeout=8):
    """带重试的 GET 请求，使用指数退避策略"""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=timeout)
            if response.status_code == 200:
                return response
            else:
                logger.warning("request failed status=%d, retrying (%d/%d)", response.status_code, attempt + 1, max_retries)
        except Exception as e:
            logger.warning("request exception=%s, retrying (%d/%d)", e, attempt + 1, max_retries)
            # SSL 错误快速失败
            if _is_ssl_error(e) and attempt >= 1:
                logger.error("SSL error detected, failing fast")
                break
        # 指数退避
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    logger.error("request failed too many times, giving up")
    return None


# ============= 网络时间同步 =============

def get_network_time():
    """获取网络时间（美团时间服务）。"""
    url = "https://cube.meituan.com/ipromotion/cube/toc/component/base/getServerCurrentTime"
    try:
        response = requests_get_with_retry(url)
        if response is None:
            return None
        response.raise_for_status()
        json_data = response.json()
        timestamp_ms = int(json_data['data'])
        timestamp_s = timestamp_ms / 1000
        dt_utc = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
        dt_local = dt_utc.astimezone(timezone(timedelta(hours=8)))  # 假设东八区
        return dt_local
    except Exception as e:
        logger.error("获取网络时间失败: %s", e)
        return None


def get_target_datetime_from_network(target_time_str, bookdate=None):
    """
    计算目标抢票时间。

    如果提供了 bookdate（预约日期），则目标时间为 bookdate - 7 天 + target_time_str。
    例如：预约 2025-12-18，target_time_str='21:00:00'，
    则目标时间为 2025-12-11 21:00:00。

    如果未提供 bookdate，则使用当前网络日期 + target_time_str（兼容旧逻辑）。
    """
    beijing_tz = timezone(timedelta(hours=8))

    if bookdate:
        # 预约日期前 7 天的指定时间
        book_date_obj = datetime.strptime(bookdate, "%Y-%m-%d")
        target_date = book_date_obj - timedelta(days=7)
        target_date_str = target_date.strftime("%Y-%m-%d")
    else:
        # 兼容旧逻辑：使用当前网络日期
        now = None
        while now is None:
            now = get_network_time()
            if now is None:
                time.sleep(1)
        target_date_str = now.strftime("%Y-%m-%d")

    full_datetime_str = f"{target_date_str} {target_time_str}"
    target_time = datetime.strptime(full_datetime_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=beijing_tz)
    return target_time


# ============= Token 缓存 =============

_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}
_TOKEN_LOCK = threading.Lock()


def get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900):
    """获取缓存的 token 或重新登录。"""
    t0 = time.time()

    now = time.time()
    tokens_cached = None
    with _TOKEN_LOCK:
        entry = _TOKEN_CACHE.get(username)
        if entry and now - float(entry.get("ts", 0)) < float(ttl_seconds) and entry.get("tokens") and entry["tokens"].get("access_token"):
            tokens_cached = entry["tokens"]
    if tokens_cached:
        # 缓存 profile
        from .booking_api import _cache_profile_from_tokens
        _cache_profile_from_tokens(tokens_cached)
        logger.info(f"[性能] Token 缓存命中: {(time.time()-t0)*1000:.0f}ms")
        return tokens_cached

    logger.info("[性能] Token 缓存未命中，开始登录...")
    t1 = time.time()
    tokens = login_with_retry(login_url, captcha_url, username, password, max_retries=5)
    t2 = time.time()
    logger.info(f"[性能] CAS 登录耗时: {(t2-t1)*1000:.0f}ms")

    if not tokens or not tokens.get("access_token"):
        return None

    with _TOKEN_LOCK:
        _TOKEN_CACHE[username] = {"tokens": tokens, "ts": now}
    from .booking_api import _cache_profile_from_tokens
    _cache_profile_from_tokens(tokens)
    logger.info(f"[性能] get_token_cached 总耗时: {(time.time()-t0)*1000:.0f}ms")
    return tokens


def clear_token_cache(username: str = None):
    """清理 token 缓存；若指定 username，仅清理该用户。"""
    from .booking_api import _TOKEN_PROFILE_CACHE
    with _TOKEN_LOCK:
        if username:
            entry = _TOKEN_CACHE.pop(username, None)
            if entry and entry.get("tokens") and entry["tokens"].get("access_token"):
                _TOKEN_PROFILE_CACHE.pop(entry["tokens"]["access_token"], None)
        else:
            _TOKEN_CACHE.clear()
            _TOKEN_PROFILE_CACHE.clear()


# ============= 遗留的并发预约函数 =============

# 线程数，固定为 5，避免同一资源时段过多并发
num_threads = 5
barrier = threading.Barrier(num_threads)


def book_task_with_network_date(thread_id, target_time_str, token, bookdate, kssj, jssj, resource_id, time_id):
    """单个线程的预约任务。"""
    target_time = get_target_datetime_from_network(target_time_str, bookdate)
    logger.info("线程%d 的目标时间：%s", thread_id, target_time)
    while True:
        now = get_network_time()
        if now:
            diff_sec = (target_time - now).total_seconds()
            if diff_sec <= 0:
                break
            elif diff_sec > 10:
                logger.debug("时间差过大，休息 5S")
                time.sleep(5)
            elif diff_sec > 1:
                logger.debug("休息 0.5S")
                time.sleep(0.5)
            else:
                logger.debug("休息 0.1S")
                time.sleep(0.1)
        else:
            logger.warning("未获取到网络时间，请检查对应操作")
            time.sleep(1)

    logger.info("thread %d reached target time, waiting barrier", thread_id)
    barrier.wait()
    logger.info("thread %d starts booking", thread_id)
    response = make_appointment(token, time_id, resource_id, bookdate, kssj, jssj)
    logger.info("thread %d booking response: %s", thread_id, response)


def run_concurrent_booking_threads(target_time_str, token, bookdate, kssj, jssj, resources_name):
    """运行并发预约线程。"""
    result = fetch_resource_time_id(token, bookdate, resources_name, kssj, jssj)
    if not result:
        logger.error("failed to get resource_id/time_id")
        return
    resource_id, time_id = result

    threads = []
    for i in range(num_threads):
        t = threading.Thread(
            target=book_task_with_network_date,
            args=(i+1, target_time_str, token, bookdate, kssj, jssj, resource_id, time_id)
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    logger.info("all booking threads finished")


def test_user_info(token):
    """测试用户信息获取功能"""
    logger.info("测试用户信息获取...")
    user_info = get_user_info_from_appointment(token)
    if user_info:
        logger.info("成功获取用户信息:")
        for key, value in user_info.items():
            if key != 'participant_info':
                logger.info("  %s: %s", key, value)
        logger.info("  participant_info:")
        for key, value in user_info['participant_info'].items():
            logger.info("    %s: %s", key, value)
    else:
        logger.warning("获取用户信息失败")


# ============= 向后兼容导出 =============

from .cas_login import (
    login_with_retry,
    cas_login,
    cas_login_stable,
    extract_oidc_tokens,
)

from .booking_api import (
    fetch_resource_time_id,
    make_appointment,
    compute_availability_for_date,
    list_appointments_for_account,
    resolve_user_info,
    get_user_info_from_appointment,
    check_resource_time_slot_capacity,
    find_resource_detail,
)


# ============= __main__ 测试代码 =============

if __name__ == '__main__':
    # 配置信息（从环境变量读取）
    LOGIN_URL = os.getenv("LOGIN_URL", WF_HOME_URL or CAS_LOGIN_URL)
    CAPTCHA_URL = CAS_CAPTCHA_URL
    USERNAME = os.getenv('CAS_USERNAME', '202300000000')
    PASSWORD = os.getenv('CAS_PASSWORD', 'XXXXXXX')
    bookdate = "2025-12-06"
    kssj = '16:00'
    jssj = '17:00'
    resources_name = "羽毛球10号场地"
    target_time_str = "12:12:00"  # 你想抢票的准点时间

    # 登录获取 token
    logger.info("开始登录...")
    tokens = login_with_retry(LOGIN_URL, CAPTCHA_URL, USERNAME, PASSWORD, max_retries=5)

    if tokens and tokens.get('access_token'):
        logger.info("="*50)
        logger.info("登录成功！开始测试用户信息获取...")

        # 测试用户信息获取
        test_user_info(tokens['access_token'])

        logger.info("="*50)
        logger.info("开始抢票流程...")

        # 开始抢票
        run_concurrent_booking_threads(target_time_str, tokens['access_token'], bookdate, kssj, jssj, resources_name)
    else:
        logger.error("login failed, cannot continue")
