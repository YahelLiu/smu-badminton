from fastapi import FastAPI, BackgroundTasks
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
import asyncio
from typing import Dict, Tuple, Optional, List, Any
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
import threading
import uuid
import time as _time
import os
import sqlite3
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 获取项目根目录（用于构建绝对路径）
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logger.info(f"项目根目录: {BASE_DIR}")

from cas_manager import book_badminton_slot, schedule_booking, booking_manager
from cas_login_requests import login_with_retry, compute_availability_for_date, get_token_cached, clear_token_cache
from config import get_frontend_config, CAS_LOGIN_URL, CAS_CAPTCHA_URL
from datetime import datetime, timezone, timedelta
_JOB_RETENTION_SEC = int(os.getenv("JOB_RETENTION_SEC", "3600"))  # 已完成任务保留时长，默认1小时
try:
    import orjson  # noqa: F401
    from fastapi.responses import ORJSONResponse as _DefaultResponse
except Exception:
    _DefaultResponse = None


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
    login_url: str
    captcha_url: str
    username: str
    password: str
    bookdate: str

class AvailabilityResponse(BookResponse):
    pass

class JobImmediateRequest(BookRequest):
    pass

class JobScheduledRequest(ScheduleRequest):
    pass

class JobsListResponse(BaseModel):
    ok: bool
    data: Optional[dict] = None

# 新增：本地预约请求模型
class LocalBookingRequest(BaseModel):
    username: str = Field(..., description="用户名")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期")
    resources_name: str = Field(..., description="资源名称")
    kssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="开始时间")
    jssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="结束时间")

# 新增：按参数停止任务请求模型
class StopByParamsRequest(BaseModel):
    username: str = Field(..., description="用户名")
    bookdate: str = Field(..., pattern=r"\d{4}-\d{2}-\d{2}", description="预约日期")
    kssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="开始时间")
    jssj: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="结束时间")
    resources_name: str = Field(..., description="资源名称")
    current_username: str = Field(..., description="当前操作用户名，用于权限验证")


class StopJobRequest(BaseModel):
    current_username: str = Field(..., description="当前操作用户名，用于权限验证")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段：恢复未完成的定时任务
    try:
        booking_manager.load_pending_jobs()
        logger.info("成功加载待处理任务")
    except Exception as e:
        logger.warning(f"加载待处理任务失败: {e}")
    
    _cleanup_task = asyncio.create_task(_availability_cleanup())
    _lock_cleanup_task = asyncio.create_task(_locks_cleanup())
    _jobs_cleanup_task = asyncio.create_task(_jobs_cleanup())
    try:
        yield
    finally:
        _cleanup_task.cancel()
        _lock_cleanup_task.cancel()
        _jobs_cleanup_task.cancel()
        # 关闭数据库连接池
        _close_db_pool()


if _DefaultResponse:
    app = FastAPI(title="Badminton Booking API", version="1.0.0", lifespan=lifespan, default_response_class=_DefaultResponse)
else:
    app = FastAPI(title="Badminton Booking API", version="1.0.0", lifespan=lifespan)

# CORS（开发环境放开，生产环境请按需收敛）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
try:
    app.add_middleware(GZipMiddleware, minimum_size=500)
except Exception:
    pass

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

app.add_middleware(MetricsMiddleware)

_rate_limits: Dict[str, Dict[str, Tuple[float, float]]] = {}
_rate_lock = asyncio.Lock()

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_per_window: int = 30, window_sec: int = 10):
        super().__init__(app)
        # 默认限流参数，可通过环境变量覆盖
        try:
            import os as _os
            default_max = int(_os.getenv("RATE_LIMIT_MAX", str(max_per_window)))
            default_window = int(_os.getenv("RATE_LIMIT_WINDOW", str(window_sec)))
            jobs_max = int(_os.getenv("RATE_LIMIT_JOBS_MAX", "300"))
            jobs_window = int(_os.getenv("RATE_LIMIT_JOBS_WINDOW", "60"))
        except Exception:
            default_max, default_window = max_per_window, window_sec
            jobs_max, jobs_window = 300, 60

        self.default_max = default_max
        self.default_window = default_window
        self.jobs_max = jobs_max
        self.jobs_window = jobs_window

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
                    logger.warning(f"429 Too Many Requests - ip={ip} path={path} count={count} window={window} max={max_per_window}")
                    # 返回更友好的 JSON 提示（仍然 429）
                    return JSONResponse(status_code=429, content={
                        "ok": False,
                        "error": "Too Many Requests",
                        "hint": "Request rate exceeded. Please slow down your polling.",
                        "limit": max_per_window,
                        "window_sec": window,
                        "path": path,
                        "ip": ip,
                    })
        return await call_next(request)

app.add_middleware(RateLimitMiddleware)

async def _jobs_cleanup():
    """定期清理历史任务记录，删除已结束状态且超过保留时长的记录。"""
    statuses = ("done", "failed", "cancelled", "skipped")
    while True:
        try:
            await asyncio.sleep(3600)  # 每60分钟清理一次
            cutoff = _time.time() - float(_JOB_RETENTION_SEC)
            with _db_lock:
                conn = _get_db_connection()
                cur = conn.execute(
                    f"DELETE FROM scheduled_jobs WHERE status IN ({','.join(['?']*len(statuses))}) AND created_at < ?",
                    (*statuses, cutoff),
                )
                conn.commit()
                deleted = cur.rowcount if cur.rowcount is not None else 0
            if deleted:
                logger.info(f"清理历史任务: 删除 {deleted} 条超过保留期({_JOB_RETENTION_SEC}s) 的记录")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"历史任务清理失败: {e}")

@app.get("/")
async def index():
    # 直接返回前端页面（仅 index.html）
    html_path = os.path.join(BASE_DIR, "templates", "index.html")
    logger.info(f"请求首页，HTML路径: {html_path}, 存在: {os.path.exists(html_path)}")
    if not os.path.exists(html_path):
        logger.error(f"HTML 文件不存在: {html_path}")
        return {"error": "index.html not found", "path": html_path, "base_dir": BASE_DIR}
    return FileResponse(html_path, media_type="text/html; charset=utf-8")

@app.get("/favicon.ico")
async def favicon():
    # Avoid noisy 404s when browser auto-requests favicon
    return Response(status_code=204)


@app.get("/api/config")
async def get_config():
    """获取前端需要的配置信息（login_url, captcha_url）"""
    # 每次都重新导入config模块以获取最新值
    import config
    import importlib
    importlib.reload(config)
    
    return {
        "ok": True,
        "data": config.get_frontend_config()
    }


@app.get("/health")
async def health():
    return {"ok": True}


class UpdateConfigRequest(BaseModel):
    login_url: str = Field(..., description="新的 CAS Login URL")


@app.post("/api/config/update")
async def update_config(req: UpdateConfigRequest):
    """更新配置文件中的 login_url并自动重载配置"""
    try:
        env_path = os.path.join(BASE_DIR, ".env")
        
        # 读取现有的 .env 文件
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        else:
            lines = []
        
        # 查找并替换 CAS_LOGIN_URL
        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith('CAS_LOGIN_URL='):
                new_lines.append(f'CAS_LOGIN_URL={req.login_url}\n')
                found = True
            else:
                new_lines.append(line)
        
        # 如果没找到，追加到文件末尾
        if not found:
            if new_lines and not new_lines[-1].endswith('\n'):
                new_lines.append('\n')
            new_lines.append(f'CAS_LOGIN_URL={req.login_url}\n')
        
        # 写回文件
        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)
        
        logger.info(f"配置已更新: CAS_LOGIN_URL")
        
        # 自动重载配置模块（无需重启服务器）
        try:
            import importlib
            import sys
            
            # 重载 dotenv 以读取新的 .env 文件
            from dotenv import load_dotenv
            load_dotenv(env_path, override=True)
            
            # 重载 config 模块
            if 'config' in sys.modules:
                importlib.reload(sys.modules['config'])
            
            # 重新导入获取最新配置
            import config
            new_login_url = config.CAS_LOGIN_URL
            
            logger.info(f"配置已重载，新值: {new_login_url[:80] if len(new_login_url) > 80 else new_login_url}...")
            
            return {
                "ok": True,
                "data": {
                    "message": "配置已保存并自动重载，立即生效",
                    "path": env_path,
                    "reloaded": True,
                    "new_value": new_login_url[:100]
                }
            }
        except Exception as reload_err:
            logger.warning(f"重载配置失败: {reload_err}，需要重启服务器")
            return {
                "ok": True,
                "data": {
                    "message": "配置已保存，但重载失败，请重启服务器",
                    "path": env_path,
                    "reloaded": False
                }
            }
    except Exception as e:
        logger.error(f"更新配置失败: {e}")
        return {
            "ok": False,
            "error": f"更新失败: {str(e)}"
        }


# startup 生命周期已改为 lifespan，上面已处理

@app.get("/jobs")
async def jobs_page():
    """任务监控页面 - 通过前端登录验证"""
    html_path = os.path.join(BASE_DIR, "templates", "jobs.html")
    if not os.path.exists(html_path):
        logger.error(f"HTML 文件不存在: {html_path}")
        return {"error": "jobs.html not found", "path": html_path}
    return FileResponse(html_path)


# 资源级互斥锁：key = (resources_name, bookdate, kssj, jssj)
_locks: Dict[Tuple[str, str, str, str], asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _acquire_lock(key: Tuple[str, str, str, str]) -> asyncio.Lock:
    async with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _locks[key] = lock
            _lock_timestamps[key] = _time.time()
        return lock

# 线程锁（用于后台线程任务互斥，不阻塞事件循环）
_tlocks: Dict[Tuple[str, str, str, str], threading.Lock] = {}
_tlocks_guard = threading.Lock()

def _get_tlock(key: Tuple[str, str, str, str]) -> threading.Lock:
    with _tlocks_guard:
        lock = _tlocks.get(key)
        if lock is None:
            lock = threading.Lock()
            _tlocks[key] = lock
        return lock

# 简易任务管理（内存级）
_jobs: Dict[str, dict] = {}
_jobs_guard = threading.Lock()

# 锁清理配置
_LOCK_MAX_AGE_SEC = 300  # 锁最大存活时间（5分钟）
_lock_timestamps: Dict[Tuple[str, str, str, str], float] = {}  # 记录锁创建时间

async def _locks_cleanup():
    """定期清理过期的锁，防止内存泄漏"""
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

def _new_job(resource_key: Tuple[str, str, str, str]) -> str:
    job_id = uuid.uuid4().hex
    with _jobs_guard:
        _jobs[job_id] = {
            "status": "scheduled",
            "created_at": _time.time(),
            "resource_key": resource_key,
            "logs": ["job scheduled"],
            "result": None,
        }
    return job_id

def _set_job(job_id: str, **kwargs):
    with _jobs_guard:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(kwargs)

def _append_log(job_id: str, msg: str):
    with _jobs_guard:
        job = _jobs.get(job_id)
        if not job:
            return
        job.setdefault("logs", []).append(msg)


def _get_token_cached(login_url: str, captcha_url: str, username: str, password: str) -> Optional[str]:
    tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
    if not tokens or not tokens.get("access_token"):
        return None
    return tokens["access_token"]


@app.post("/api/book", response_model=BookResponse)
async def api_book(req: BookRequest) -> BookResponse:
    resource_key = (req.resources_name, req.bookdate, req.kssj, req.jssj)
    lock = await _acquire_lock(resource_key)

    if lock.locked():
        # 有请求正在处理该资源时间段
        return BookResponse(ok=False, error="resource_locked_processing")

    async with lock:
        # 并发验证：先尝试插入本地预约记录，利用UNIQUE约束防止重复预约
        try:
            with _db_lock:
                conn = _get_db_connection()
                conn.execute(
                    "INSERT INTO local_bookings (username, bookdate, resources_name, kssj, jssj, created_at) VALUES (?,?,?,?,?,?)",
                    (req.username, req.bookdate, req.resources_name, req.kssj, req.jssj, _time.time()),
                )
                conn.commit()
        except sqlite3.IntegrityError:
            # UNIQUE约束失败，说明该资源已被其他用户预约
            return BookResponse(ok=False, error="resource_already_booked")
        except Exception as e:
            logger.error(f"插入本地预约记录失败: {e}")
            return BookResponse(ok=False, error="database_error")

        result = await run_in_threadpool(
            book_badminton_slot,
            login_url=req.login_url,
            captcha_url=req.captcha_url,
            username=req.username,
            password=req.password,
            bookdate=req.bookdate,
            kssj=req.kssj,
            jssj=req.jssj,
            resources_name=req.resources_name,
        )

        if not result.get("ok"):
            # 预约失败，删除本地记录
            try:
                with _db_lock:
                    conn = _get_db_connection()
                    conn.execute(
                        "DELETE FROM local_bookings WHERE username=? AND bookdate=? AND resources_name=? AND kssj=? AND jssj=?",
                        (req.username, req.bookdate, req.resources_name, req.kssj, req.jssj),
                    )
                    conn.commit()
            except Exception as e:
                logger.warning(f"删除失败的本地预约记录失败: {e}")
            return BookResponse(ok=False, error=result.get("error") or "unknown_error")

        return BookResponse(ok=True, data=result.get("data") or {})


@app.post("/api/book/schedule", response_model=ScheduleResponse)
async def api_book_schedule(req: ScheduleRequest, background_tasks: BackgroundTasks) -> ScheduleResponse:
    resource_key = (req.resources_name, req.bookdate, req.kssj, req.jssj)
    lock = await _acquire_lock(resource_key)

    if lock.locked():
        return ScheduleResponse(ok=False, error="resource_locked_processing")

    # 去重：如果相同参数的任务已存在于DB，则直接返回该job_id
    try:
        db_jobs = booking_manager.list_scheduled_jobs(username=req.username)
        for j in db_jobs:
            if (
                j.get("bookdate") == req.bookdate
                and j.get("kssj") == req.kssj
                and j.get("jssj") == req.jssj
                and j.get("resources_name") == req.resources_name
                and j.get("status") in ("scheduled", "waiting", "running")
            ):
                return ScheduleResponse(ok=True, data={"scheduled": True, "job_id": j.get("job_id"), "duplicate": True})
    except Exception:
        pass

    # 并发验证：先尝试插入本地预约记录，利用UNIQUE约束防止重复预约
    try:
        with _db_lock:
            conn = _get_db_connection()
            conn.execute(
                "INSERT INTO local_bookings (username, bookdate, resources_name, kssj, jssj, created_at) VALUES (?,?,?,?,?,?)",
                (req.username, req.bookdate, req.resources_name, req.kssj, req.jssj, _time.time()),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        # UNIQUE约束失败，说明该资源已被其他用户预约
        return ScheduleResponse(ok=False, error="resource_already_booked")
    except Exception as e:
        logger.error(f"插入本地预约记录失败: {e}")
        return ScheduleResponse(ok=False, error="database_error")

    if req.run_async:
        # 使用统一的 BookingManager 启动后台任务
        job_id = booking_manager.start_scheduled_booking(
            login_url=req.login_url,
            captcha_url=req.captcha_url,
            username=req.username,
            password=req.password,
            bookdate=req.bookdate,
            kssj=req.kssj,
            jssj=req.jssj,
            resources_name=req.resources_name,
            target_time_str=req.target_time_str,
            num_threads=req.num_threads,
        )
        return ScheduleResponse(ok=True, data={"scheduled": True, "job_id": job_id})

    # 同步执行：注意将会等待至目标时间，浏览器可能超时
    async with lock:
        result = await run_in_threadpool(
            schedule_booking,
            login_url=req.login_url,
            captcha_url=req.captcha_url,
            username=req.username,
            password=req.password,
            bookdate=req.bookdate,
            kssj=req.kssj,
            jssj=req.jssj,
            resources_name=req.resources_name,
            target_time_str=req.target_time_str,
            num_threads=req.num_threads,
        )
        if not result.get("ok"):
            return ScheduleResponse(ok=False, error=result.get("error") or "unknown_error")
        return ScheduleResponse(ok=True, data=result.get("data") or {})


@app.get("/api/schedule/{job_id}")
async def api_schedule_status(job_id: str):
    with _jobs_guard:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "job_not_found"}
        return {"ok": True, "data": job}


# 使用SQLite持久化本地预约记录（连接池模式）
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "data.db")
_db_lock = threading.Lock()
_db_pool: Optional[sqlite3.Connection] = None
_db_pool_lock = threading.Lock()

def _get_db_connection() -> sqlite3.Connection:
    """获取数据库连接（单例模式，复用连接）"""
    global _db_pool
    with _db_pool_lock:
        if _db_pool is None:
            # 确保数据库目录存在
            db_dir = os.path.dirname(DB_PATH)
            os.makedirs(db_dir, exist_ok=True)
            
            _db_pool = sqlite3.connect(DB_PATH, check_same_thread=False)
            _db_pool.execute("PRAGMA journal_mode=WAL;")
            _db_pool.execute("PRAGMA synchronous=NORMAL;")
            _db_pool.execute("PRAGMA busy_timeout=2000;")
            logger.info("数据库连接已创建")
        return _db_pool

def _close_db_pool():
    """关闭数据库连接池"""
    global _db_pool
    with _db_pool_lock:
        if _db_pool:
            _db_pool.close()
            _db_pool = None
            logger.info("数据库连接已关闭")

def _init_db():
    """统一初始化所有数据库表"""
    with _db_lock:
        conn = _get_db_connection()
        
        # 创建本地预约记录表
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                bookdate TEXT NOT NULL,
                resources_name TEXT NOT NULL,
                kssj TEXT NOT NULL,
                jssj TEXT NOT NULL,
                created_at REAL NOT NULL,
                UNIQUE(bookdate, resources_name, kssj, jssj)
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_bookings_bookdate ON local_bookings(bookdate);"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_bookings_comp ON local_bookings(username, bookdate, kssj, jssj, resources_name);"
        )
        
        # 创建定时任务表
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                login_url TEXT,
                captcha_url TEXT,
                username TEXT,
                password TEXT,
                bookdate TEXT,
                kssj TEXT,
                jssj TEXT,
                resources_name TEXT,
                target_time_str TEXT,
                num_threads INTEGER,
                status TEXT,
                created_at REAL NOT NULL
            );
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON scheduled_jobs(status);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created ON scheduled_jobs(created_at);")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_params ON scheduled_jobs(username, bookdate, kssj, jssj, resources_name);"
        )
        
        conn.commit()
        logger.info("数据库表初始化完成")

_init_db()

@app.post("/api/local_bookings")
async def api_save_local_booking(req: LocalBookingRequest):
    """保存本地预约记录（已废弃，预约时会自动插入）"""
    try:
        with _db_lock:
            conn = _get_db_connection()
            conn.execute(
                "INSERT INTO local_bookings (username, bookdate, resources_name, kssj, jssj, created_at) VALUES (?,?,?,?,?,?)",
                (req.username, req.bookdate, req.resources_name, req.kssj, req.jssj, _time.time()),
            )
            conn.commit()
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "resource_already_booked"}
    except Exception as e:
        logger.error(f"保存本地预约记录失败: {e}")
        return {"ok": False, "error": "database_error"}
    return {"ok": True}

@app.get("/api/local_bookings")
async def api_list_local_bookings(bookdate: str, response: Response, limit: Optional[int] = None, offset: int = 0, fields: Optional[str] = None, clean: int = 1):
    t0 = _time.perf_counter()
    # 清理指定日期已过期的本地预约记录（结束时间 < 当前时间）
    try:
        beijing_tz = timezone(timedelta(hours=8))
        now_dt = datetime.now(beijing_tz)
        if clean:
            with _db_lock:
                conn = _get_db_connection()
                cur = conn.execute(
                    "SELECT id, kssj, jssj FROM local_bookings WHERE bookdate = ?",
                    (bookdate,),
                )
                to_delete = []
                for _id, _kssj, _jssj in cur.fetchall():
                    try:
                        end_dt = datetime.strptime(f"{bookdate} {_jssj}", "%Y-%m-%d %H:%M").replace(tzinfo=beijing_tz)
                        if end_dt < now_dt:
                            to_delete.append(_id)
                    except ValueError:
                        continue
                if to_delete:
                    conn.executemany("DELETE FROM local_bookings WHERE id = ?", [(i,) for i in to_delete])
                    conn.commit()
                    logger.info(f"清理了 {len(to_delete)} 条过期预约记录")
    except Exception as e:
        logger.warning(f"清理过期记录失败: {e}")

    t1 = _time.perf_counter()
    with _db_lock:
        conn = _get_db_connection()
        if limit is not None:
            cur = conn.execute(
                "SELECT username, bookdate, resources_name, kssj, jssj, created_at FROM local_bookings WHERE bookdate = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (bookdate, int(limit), int(offset)),
            )
        else:
            cur = conn.execute(
                "SELECT username, bookdate, resources_name, kssj, jssj, created_at FROM local_bookings WHERE bookdate = ? ORDER BY created_at DESC",
                (bookdate,),
            )
        rows = [
            {
                "username": r[0],
                "bookdate": r[1],
                "resources_name": r[2],
                "kssj": r[3],
                "jssj": r[4],
                "created_at": r[5],
            }
            for r in cur.fetchall()
        ]
    t2 = _time.perf_counter()
    if fields:
        allow = {"username", "bookdate", "resources_name", "kssj", "jssj", "created_at"}
        wanted = [f.strip() for f in fields.split(",") if f.strip() in allow]
        if wanted:
            rows = [{k: v for k, v in row.items() if k in wanted} for row in rows]
    response.headers["X-LBookings-CleanMs"] = f"{(t1 - t0) * 1000.0:.2f}"
    response.headers["X-LBookings-QueryMs"] = f"{(t2 - t1) * 1000.0:.2f}"
    response.headers["X-LBookings-Count"] = str(len(rows))
    response.headers["X-LBookings-Limit"] = str(limit if limit is not None else -1)
    response.headers["X-LBookings-Offset"] = str(offset)
    return {"ok": True, "data": {"list": rows}}


def _convert_to_minimal(data: List[dict]) -> List[dict]:
    """将完整数据转换为精简格式"""
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


_availability_cache: Dict[Tuple[str, str], Dict[str, object]] = {}
_availability_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
_availability_guard = asyncio.Lock()
_availability_ttl_sec = 30.0

async def _availability_cleanup():
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
    async with _availability_guard:
        lk = _availability_locks.get(key)
        if lk is None:
            lk = asyncio.Lock()
            _availability_locks[key] = lk
        return lk

# 不再在模块导入时使用 asyncio.create_task 装饰器创建任务。
# 可通过 lifespan 启动/停止 `_availability_cleanup`，已在 lifespan 中创建该任务。

@app.post("/api/availability", response_model=AvailabilityResponse)
async def api_availability(req: AvailabilityRequest, request: Request, response: Response) -> AvailabilityResponse:
    t0 = _time.perf_counter()
    nocache = request.query_params.get("nocache") == "1"
    minimal = request.query_params.get("min") == "1"
    token = _get_token_cached(req.login_url, req.captcha_url, req.username, req.password)
    if not token:
        return AvailabilityResponse(ok=False, error="login_failed")
    cache_key = (req.username, req.bookdate)
    now = _time.time()
    t1 = _time.perf_counter()
    if not nocache:
        lk = await _get_avail_lock(cache_key)
        async with lk:
            entry = _availability_cache.get(cache_key)
            if entry and now - float(entry.get("ts", 0)) < 30.0:
                data_cached = entry.get("data") or []
                if minimal:
                    out_list = entry.get("data_min") or []
                    if not out_list:
                        out_list = _convert_to_minimal(data_cached)
                        entry["data_min"] = out_list
                else:
                    out_list = data_cached
                t_total = (_time.perf_counter() - t0) * 1000.0
                response.headers["X-Avail-TokenMs"] = f"{(t1 - t0) * 1000.0:.2f}"
                response.headers["X-Avail-Cache"] = "HIT"
                response.headers["X-Avail-ComputeMs"] = "0.00"
                response.headers["X-Avail-TotalMs"] = f"{t_total:.2f}"
                response.headers["X-Avail-ListLen"] = str(len(out_list))
                return AvailabilityResponse(ok=True, data={"list": out_list})
    data = await run_in_threadpool(compute_availability_for_date, token, req.bookdate)
    t2 = _time.perf_counter()
    lk = await _get_avail_lock(cache_key)
    async with lk:
        _availability_cache[cache_key] = {"ts": now, "data": data, "data_min": None}
    out_list = data
    if minimal:
        out_list = _convert_to_minimal(data)
        async with lk:
            entry = _availability_cache.get(cache_key)
            if entry:
                entry["data_min"] = out_list
    response.headers["X-Avail-TokenMs"] = f"{(t1 - t0) * 1000.0:.2f}"
    response.headers["X-Avail-Cache"] = "MISS"
    response.headers["X-Avail-ComputeMs"] = f"{(t2 - t1) * 1000.0:.2f}"
    response.headers["X-Avail-TotalMs"] = f"{(_time.perf_counter() - t0) * 1000.0:.2f}"
    response.headers["X-Avail-ListLen"] = str(len(out_list))
    return AvailabilityResponse(ok=True, data={"list": out_list})


# 任务管理接口
@app.post("/api/jobs/immediate", response_model=JobsListResponse)
async def api_jobs_immediate(req: JobImmediateRequest):
    job_id = booking_manager.start_immediate_booking(
        login_url=req.login_url,
        captcha_url=req.captcha_url,
        username=req.username,
        password=req.password,
        bookdate=req.bookdate,
        kssj=req.kssj,
        jssj=req.jssj,
        resources_name=req.resources_name,
    )
    return {"ok": True, "data": {"job_id": job_id}}


@app.post("/api/jobs/scheduled", response_model=JobsListResponse)
async def api_jobs_scheduled(req: JobScheduledRequest):
    job_id = booking_manager.start_scheduled_booking(
        login_url=req.login_url,
        captcha_url=req.captcha_url,
        username=req.username,
        password=req.password,
        bookdate=req.bookdate,
        kssj=req.kssj,
        jssj=req.jssj,
        resources_name=req.resources_name,
        target_time_str=req.target_time_str,
        num_threads=req.num_threads,
    )
    return {"ok": True, "data": {"job_id": job_id}}


@app.get("/api/jobs", response_model=JobsListResponse)
async def api_jobs_list():
    jobs = booking_manager.list_jobs()
    # merge DB scheduled jobs
    db_jobs = booking_manager.list_scheduled_jobs()
    return {"ok": True, "data": {"jobs": jobs, "db_jobs": db_jobs}}

@app.get("/api/metrics")
async def api_metrics():
    async with _metrics_lock:
        out = {k: {"count": int(v["count"]), "avg_ms": (v["total_ms"] / v["count"]) if v["count"] else 0.0, "max_ms": v["max_ms"]} for k, v in _metrics.items()}
    return {"ok": True, "data": out}


@app.post("/api/jobs/{job_id}/stop", response_model=JobsListResponse)
async def api_jobs_stop(job_id: str, req: StopJobRequest):
    owner = None
    try:
        owner = booking_manager.get_job_owner(job_id)
    except Exception:
        owner = None
    # 如果能识别到所有者，必须与当前用户匹配
    if owner and req.current_username != owner:
        logger.warning(f"权限拒绝：用户 {req.current_username} 试图停止 {owner} 的任务 {job_id}")
        return {"ok": False, "data": {"error": "permission_denied", "message": "无权停止他人的任务"}}
    ok = booking_manager.stop_job(job_id)
    return {"ok": ok, "data": {"job_id": job_id}}


@app.post("/api/jobs/stop_by_params", response_model=JobsListResponse)
async def api_jobs_stop_by_params(req: StopByParamsRequest):
    """按参数停止任务 - 需要验证用户权限"""
    # 权限验证：只允许当前用户取消自己名下的任务（基于DB校验）
    if req.current_username != req.username:
        logger.warning(f"用户 {req.current_username} 尝试取消 {req.username} 的任务（参数校验未通过）")
        return {"ok": False, "data": {"error": "permission_denied", "message": "无权取消其他用户的预约任务"}}
    
    stopped = booking_manager.stop_by_params(
        username=req.username, 
        bookdate=req.bookdate, 
        kssj=req.kssj, 
        jssj=req.jssj, 
        resources_name=req.resources_name
    )
    return {"ok": True, "data": {"stopped": stopped}}


class LogoutRequest(BaseModel):
    username: str = Field(..., description="用户名")


@app.post("/api/logout")
async def api_logout(req: LogoutRequest):
    """登出 - 清除服务端 token 缓存"""
    clear_token_cache(req.username)
    logger.info(f"用户 {req.username} 已登出，token 缓存已清除")
    return {"ok": True}


# 静态文件挂载（必须放在所有路由之后）
try:
    app.mount("/static", StaticFiles(directory=BASE_DIR), name="static")
    logger.info(f"静态文件目录: {BASE_DIR}")
except Exception as e:
    logger.warning(f"静态文件挂载失败: {e}")


# 运行： uvicorn server_fastapi:app --host 0.0.0.0 --port 5000 --reload

if __name__ == "__main__":
    import uvicorn
    try:
        booking_manager.load_pending_jobs()
        logger.info("成功加载待处理任务")
    except Exception as e:
        logger.warning(f"加载待处理任务失败: {e}")
    uvicorn.run(
        "server_fastapi:app",
        host="0.0.0.0",
        port=5000,
        reload=True,
        # 使用代理头部（X-Forwarded-For / X-Real-IP）打印真实客户端 IP
        proxy_headers=True,
        forwarded_allow_ips="*"
    )
