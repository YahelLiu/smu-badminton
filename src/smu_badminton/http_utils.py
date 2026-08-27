"""
HTTP 工具模块。

包含：
- HTTP 重试逻辑
- 网络时间同步
"""
import requests
import time
import logging
from typing import Any, Dict, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


# ============= HTTP 重试逻辑 =============

def _is_ssl_error(error: Exception) -> bool:
    """判断是否为 SSL 相关错误。"""
    error_str = str(error).lower()
    return 'ssl' in error_str or 'eof' in error_str or 'protocol' in error_str


def request_with_retry(
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    timeout: int = 8,
    **kwargs
) -> Optional[requests.Response]:
    """
    通用 HTTP 请求重试，使用指数退避策略。

    Args:
        method: HTTP 方法 (GET/POST/...)
        url: 请求 URL
        max_retries: 最大重试次数
        timeout: 请求超时秒数
        **kwargs: 传递给 requests.request 的其他参数

    Returns:
        响应对象，失败返回 None
    """
    for attempt in range(max_retries):
        try:
            response = requests.request(method, url, timeout=timeout, **kwargs)
            if response.status_code == 200:
                return response
            logger.warning(
                "request failed method=%s status=%d, retrying (%d/%d)",
                method,
                response.status_code,
                attempt + 1,
                max_retries,
            )
        except requests.RequestException as e:
            logger.warning(
                "request exception method=%s error=%s, retrying (%d/%d)",
                method,
                e,
                attempt + 1,
                max_retries,
            )
            if _is_ssl_error(e) and attempt >= 1:
                logger.error("SSL error detected, failing fast")
                break
        # 指数退避: 1s, 2s, 4s...
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)
    logger.error("request failed too many times, method=%s url=%s", method, url)
    return None


def requests_post_with_retry(
    url: str,
    json: Any,
    headers: Dict[str, str],
    max_retries: int = 3,
    timeout: int = 8
) -> Optional[requests.Response]:
    """
    带重试的 POST 请求。

    Args:
        url: 请求 URL
        json: JSON 请求体
        headers: 请求头
        max_retries: 最大重试次数
        timeout: 请求超时秒数

    Returns:
        响应对象，失败返回 None
    """
    return request_with_retry(
        "POST",
        url,
        json=json,
        headers=headers,
        max_retries=max_retries,
        timeout=timeout,
    )


def requests_get_with_retry(
    url: str,
    max_retries: int = 3,
    timeout: int = 8
) -> Optional[requests.Response]:
    """
    带重试的 GET 请求。

    Args:
        url: 请求 URL
        max_retries: 最大重试次数
        timeout: 请求超时秒数

    Returns:
        响应对象，失败返回 None
    """
    return request_with_retry(
        "GET",
        url,
        max_retries=max_retries,
        timeout=timeout,
    )


# ============= 网络时间同步 =============

_BEIJING_TZ = timezone(timedelta(hours=8))


def get_network_time() -> Optional[datetime]:
    """
    获取网络时间（美团时间服务）。

    Returns:
        北京时间 datetime，失败返回 None
    """
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
        dt_local = dt_utc.astimezone(_BEIJING_TZ)
        return dt_local
    except Exception as e:
        logger.error("获取网络时间失败: %s", e)
        return None


def get_current_beijing_time(max_retries: int = 3) -> datetime:
    """
    获取当前北京时间，网络时间失败时回退到本地时间。

    Args:
        max_retries: 最大重试次数

    Returns:
        北京时间 datetime（网络时间或本地时间）
    """
    for _ in range(max_retries):
        now = get_network_time()
        if now is not None:
            return now
        time.sleep(1)
    logger.warning("network time unavailable, fallback to local Beijing time")
    return datetime.now(_BEIJING_TZ)


def get_target_datetime_from_network(
    target_time_str: str,
    bookdate: Optional[str] = None
) -> datetime:
    """
    计算目标抢票时间。

    如果提供了 bookdate（预约日期），则目标时间为 bookdate - 7 天 + target_time_str。
    例如：预约 2025-12-18，target_time_str='21:00:00'，
    则目标时间为 2025-12-11 21:00:00。

    如果未提供 bookdate，则使用当前网络日期 + target_time_str（兼容旧逻辑）。

    Args:
        target_time_str: 目标时间字符串 (HH:MM:SS)
        bookdate: 预约日期 (YYYY-MM-DD)，可选

    Returns:
        目标时间 datetime
    """
    if bookdate:
        # 预约日期前 7 天的指定时间
        book_date_obj = datetime.strptime(bookdate, "%Y-%m-%d")
        target_date = book_date_obj - timedelta(days=7)
        target_date_str = target_date.strftime("%Y-%m-%d")
    else:
        # 兼容旧逻辑：使用当前网络日期（有回退）
        now = get_current_beijing_time()
        target_date_str = now.strftime("%Y-%m-%d")

    full_datetime_str = f"{target_date_str} {target_time_str}"
    target_time = datetime.strptime(full_datetime_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_BEIJING_TZ)
    return target_time


# ============= 本地时钟偏移校准 =============

class ClockSync:
    """本地时钟与网络时间的偏移校准。

    抢票等待循环若每轮都请求网络对时，等待精度就等于该 HTTP 的 RTT 抖动
    （几十到几百 ms，且有被限流风险）。正确做法：在预取窗口测一次
    「网络时间 − 本地时间」偏移量，之后等待循环全部使用 本地钟 + offset，
    关键窗口内不再发出任何非预约请求。
    """

    _SYNC_URL = "https://cube.meituan.com/ipromotion/cube/toc/component/base/getServerCurrentTime"

    def __init__(self) -> None:
        self.offset_sec: Optional[float] = None
        self.synced_at: float = 0.0

    def _measure_once(self) -> Optional[float]:
        """单次采样：服务器毫秒时间戳 − 本地发包/收包中点。"""
        try:
            t0 = time.time()
            response = requests.get(self._SYNC_URL, timeout=3)
            t1 = time.time()
            if response.status_code != 200:
                return None
            ts_ms = int(response.json()["data"])
            return ts_ms / 1000.0 - (t0 + t1) / 2.0
        except Exception as e:
            logger.warning("网络对时采样失败: %s", e)
            return None

    def sync(self, samples: int = 3) -> Optional[float]:
        """采样多次取中位数作为偏移。全部失败返回 None 并保持旧值。"""
        offsets = []
        for _ in range(samples):
            off = self._measure_once()
            if off is not None:
                offsets.append(off)
            time.sleep(0.05)
        if offsets:
            offsets.sort()
            self.offset_sec = offsets[len(offsets) // 2]
            self.synced_at = time.time()
            logger.info("[性能] 时钟偏移校准: %+.3fs（样本 %d 个）", self.offset_sec, len(offsets))
        else:
            logger.warning("网络对时全部失败，继续沿用偏移值: %r", self.offset_sec)
        return self.offset_sec

    def now(self) -> datetime:
        """当前北京时间 = 本地钟 + 校准偏移。未同步时即本机时间。"""
        base = time.time() + (self.offset_sec or 0.0)
        return datetime.fromtimestamp(base, tz=_BEIJING_TZ)

    @property
    def is_synced(self) -> bool:
        return self.offset_sec is not None
