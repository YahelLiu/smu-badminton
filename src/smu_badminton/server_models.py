"""
Pydantic 模型、中间件和资源锁管理模块。
"""
import asyncio
import threading
import uuid
import time as _time
import logging
from typing import Dict, Tuple, Optional, List, Any

from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import AUTHORIZED_USERS, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW, RATE_LIMIT_JOBS_MAX, RATE_LIMIT_JOBS_WINDOW

logger = logging.getLogger(__name__)


# ============= Pydantic 模型 =============

class BookRequest(BaseModel):
    login_url: str = Field(..., description="CAS 登录URL")
    captcha_url: str = Field(..., description="验证码URL")
    username: str = Field(..., description="学号/用户名")
    password: str = Field(..., description="密码")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期 YYYY-MM-DD")
    kssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="开始时间 HH:MM")
    jssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="结束时间 HH:MM")
    resources_name: str = Field(..., description="资源名称，如 羽毛球13号场地")


class BookResponse(BaseModel):
    ok: bool
    data: Optional[dict] = None
    error: Optional[str] = None


class ScheduleRequest(BookRequest):
    target_time_str: str = Field(..., description="目标开抢时间，格式 HH:MM:SS")
    num_threads: int = Field(5, ge=1, le=20, description="并发线程数")
    run_async: bool = Field(False, description="是否后台异步执行（立即返回）")


class ScheduleResponse(BookResponse):
    pass


class AvailabilityRequest(BaseModel):
    token: str = Field(..., description="访问令牌")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期 YYYY-MM-DD")


class AvailabilityResponse(BookResponse):
    pass


class JobImmediateRequest(BookRequest):
    pass


class JobScheduledRequest(ScheduleRequest):
    pass


class JobsListResponse(BaseModel):
    ok: bool
    data: Optional[dict] = None


class LocalBookingRequest(BaseModel):
    username: str = Field(..., description="用户名")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期")
    resources_name: str = Field(..., description="资源名称")
    kssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="开始时间")
    jssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="结束时间")


class StopByParamsRequest(BaseModel):
    username: str = Field(..., description="用户名")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期")
    kssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="开始时间")
    jssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="结束时间")
    resources_name: str = Field(..., description="资源名称")
    current_username: str = Field(..., description="当前操作用户名，用于权限验证")


class StopJobRequest(BaseModel):
    current_username: str = Field(..., description="当前操作用户名，用于权限验证")


class UpdateConfigRequest(BaseModel):
    login_url: str = Field(..., description="新的 CAS 登录 URL")


class CaptchaRequest(BaseModel):
    login_url: str = Field("", description="CAS 登录URL（可选）")
    captcha_url: str = Field("", description="验证码URL（可选）")


class CaptchaResponse(BaseModel):
    ok: bool
    data: Optional[dict] = None
    error: Optional[str] = None


class LoginRequest(BaseModel):
    login_url: str = Field("", description="CAS 登录URL（可选）")
    captcha_url: str = Field("", description="验证码URL（可选）")
    username: str = Field(..., description="学号/用户名")
    password: str = Field(..., description="密码")
    captcha_code: Optional[str] = Field(None, description="手动输入的验证码（可选）")
    session_id: Optional[str] = Field(None, description="验证码会话ID（用于复用session）")


class LoginResponse(BaseModel):
    ok: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    error_type: Optional[str] = Field(None, description="错误类型: captcha_error, password_error, network_error, unknown_error")
    need_manual_captcha: bool = Field(False, description="是否需要手动输入验证码")


class LogoutRequest(BaseModel):
    username: str = Field(..., description="用户名")


# ============= 指标收集 =============

_metrics: Dict[str, Dict[str, float]] = {}
_metrics_lock = asyncio.Lock()


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = _time.perf_counter()
        response: Response = await call_next(request)
        duration = (_time.perf_counter() - start) * 1000.0
        key = request.url.path
        async with _metrics_lock:
            m = _metrics.setdefault(key, {"count": 0.0, "total_ms": 0.0, "max_ms": 0.0})
            m["count"] += 1.0
            m["total_ms"] += duration
            if duration > m["max_ms"]:
                m["max_ms"] = duration
        return response


# ============= 限流中间件 =============

_rate_limits: Dict[str, Dict[str, Tuple[float, float]]] = {}
_rate_lock = asyncio.Lock()


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_per_window: int = 30, window_sec: int = 10):
        super().__init__(app)
        # 从 config 获取限流参数
        self.default_max = RATE_LIMIT_MAX
        self.default_window = RATE_LIMIT_WINDOW
        self.jobs_max = RATE_LIMIT_JOBS_MAX
        self.jobs_window = RATE_LIMIT_JOBS_WINDOW

        # 受保护接口集合
        self.protected_paths = {
            "/api/book",
            "/api/book/schedule",
            "/api/availability",
            "/api/jobs",
        }

    @staticmethod
    def _get_client_ip(request: Request) -> str:
        # 优先从代理头部获取真实 IP
        xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
        if xff:
            # 取第一个非空 IP
            ip = xff.split(",")[0].strip()
            if ip:
                return ip
        xreal = request.headers.get("x-real-ip") or request.headers.get("X-Real-IP")
        if xreal:
            return xreal.strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.protected_paths or path.startswith("/api/jobs"):
            # 针对 /api/jobs 使用更宽松的限流窗口
            if path.startswith("/api/jobs"):
                max_per_window = self.jobs_max
                window = self.jobs_window
            else:
                max_per_window = self.default_max
                window = self.default_window

            ip = self._get_client_ip(request)
            now = _time.time()
            async with _rate_lock:
                user_map = _rate_limits.setdefault(ip, {})
                count, start_ts = user_map.get(path, (0.0, now))
                if now - start_ts > window:
                    count, start_ts = 0.0, now
                count += 1.0
                user_map[path] = (count, start_ts)
                if count > max_per_window:
                    logger.warning(f"429 请求过于频繁 - ip={ip} path={path} count={count} window={window} max={max_per_window}")
                    # 返回更友好的 JSON 提示（仍然 429）
                    return JSONResponse(status_code=429, content={
                        "ok": False,
                        "error": "请求过于频繁",
                        "hint": "请求频率超限，请降低轮询频率。",
                        "limit": max_per_window,
                        "window_sec": window,
                        "path": path,
                        "ip": ip,
                    })
        return await call_next(request)


# ============= 资源锁管理 =============

_LOCK_MAX_AGE_SEC = 300  # 锁最大存活时间（5分钟）

# 异步锁
_locks: Dict[Tuple[str, str, str, str], asyncio.Lock] = {}
_locks_guard = asyncio.Lock()
_lock_timestamps: Dict[Tuple[str, str, str, str], float] = {}

# 线程锁
_tlocks: Dict[Tuple[str, str, str, str], threading.Lock] = {}
_tlocks_guard = threading.Lock()


async def _acquire_lock(key: Tuple[str, str, str, str]) -> asyncio.Lock:
    """获取或创建资源锁。"""
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
            _lock_timestamps[key] = _time.time()
        return lock


def _get_tlock(key: Tuple[str, str, str, str]) -> threading.Lock:
    """获取或创建线程锁。"""
    with _tlocks_guard:
        lock = _tlocks.get(key)
        if lock is None:
            lock = threading.Lock()
            _tlocks[key] = lock
        return lock


async def _locks_cleanup():
    """定期清理过期的锁，防止内存泄漏。"""
    while True:
        await asyncio.sleep(120)  # 每2分钟清理一次
        now = _time.time()

        # 清理异步锁
        async with _locks_guard:
            expired_keys = [
                k for k, ts in _lock_timestamps.items()
                if now - ts > _LOCK_MAX_AGE_SEC and k in _locks and not _locks[k].locked()
            ]
            for k in expired_keys:
                _locks.pop(k, None)
                _lock_timestamps.pop(k, None)
            if expired_keys:
                logger.info(f"清理了 {len(expired_keys)} 个过期异步锁")

        # 清理线程锁
        with _tlocks_guard:
            tlock_keys = list(_tlocks.keys())
            expired_tlocks = [k for k in tlock_keys if not _tlocks[k].locked()]
            # 只清理未锁定的，保留最多100个
            if len(expired_tlocks) > 100:
                for k in expired_tlocks[:-100]:
                    _tlocks.pop(k, None)
                logger.info(f"清理了 {len(expired_tlocks) - 100} 个过期线程锁")

        # 清理可用性缓存锁
        async with _availability_guard:
            avail_keys = list(_availability_locks.keys())
            # 保留最多50个
            if len(avail_keys) > 50:
                for k in avail_keys[:-50]:
                    if k not in _availability_cache:  # 没有缓存的锁可以删除
                        _availability_locks.pop(k, None)


# ============= 任务管理（内存级） =============

_jobs: Dict[str, dict] = {}
_jobs_guard = threading.Lock()


def _new_job(resource_key: Tuple[str, str, str, str]) -> str:
    """创建新任务。"""
    job_id = uuid.uuid4().hex
    with _jobs_guard:
        _jobs[job_id] = {
            "status": "scheduled",
            "created_at": _time.time(),
            "resource_key": resource_key,
            "logs": ["任务已创建"],
            "result": None,
        }
    return job_id


def _set_job(job_id: str, **kwargs):
    """更新任务状态。"""
    with _jobs_guard:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(kwargs)


def _append_log(job_id: str, msg: str):
    """追加任务日志。"""
    with _jobs_guard:
        job = _jobs.get(job_id)
        if not job:
            return
        job.setdefault("logs", []).append(msg)


# ============= 可用性缓存 =============

_availability_cache: Dict[Tuple[str, str], Dict[str, object]] = {}
_availability_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
_availability_guard = asyncio.Lock()
_availability_ttl_sec = 30.0


async def _availability_cleanup():
    """定期清理可用性缓存。"""
    while True:
        await asyncio.sleep(60)
        now = _time.time()
        async with _availability_guard:
            keys = list(_availability_cache.keys())
        for k in keys:
            lk = await _get_avail_lock(k)
            async with lk:
                entry = _availability_cache.get(k)
                if not entry:
                    continue
                ts = float(entry.get("ts", 0))
                if now - ts > 10 * _availability_ttl_sec:
                    _availability_cache.pop(k, None)


async def _get_avail_lock(key: Tuple[str, str]) -> asyncio.Lock:
    """获取可用性缓存锁。"""
    async with _availability_guard:
        lk = _availability_locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            _availability_locks[key] = lk
        return lk


def _convert_to_minimal(data: List[dict]) -> List[dict]:
    """将完整数据转换为精简格式。"""
    return [
        {
            "resources_id": r.get("resources_id"),
            "resources_name": r.get("resources_name"),
            "slots": [
                {
                    "kssj": s.get("kssj"),
                    "jssj": s.get("jssj"),
                    "canAppointmentNumber": s.get("canAppointmentNumber"),
                }
                for s in (r.get("slots") or [])
            ],
        }
        for r in data
    ]


# ============= 向后兼容导出 =============

__all__ = [
    # 模型
    "BookRequest", "BookResponse", "ScheduleRequest", "ScheduleResponse",
    "AvailabilityRequest", "AvailabilityResponse", "JobImmediateRequest", "JobScheduledRequest",
    "JobsListResponse", "LocalBookingRequest", "StopByParamsRequest", "StopJobRequest",
    "UpdateConfigRequest", "LogoutRequest",
    # 中间件
    "MetricsMiddleware", "RateLimitMiddleware",
    # 锁管理
    "_acquire_lock", "_get_tlock", "_locks_cleanup", "_locks", "_tlocks", "_locks_guard", "_tlocks_guard", "_lock_timestamps", "_LOCK_MAX_AGE_SEC",
    # 任务管理
    "_new_job", "_set_job", "_append_log", "_jobs", "_jobs_guard",
    # 可用性缓存
    "_availability_cache", "_availability_locks", "_availability_guard", "_availability_ttl_sec",
    "_availability_cleanup", "_get_avail_lock", "_convert_to_minimal",
    # 指标
    "_metrics", "_metrics_lock",
    # 限流
    "_rate_limits", "_rate_lock",
]
