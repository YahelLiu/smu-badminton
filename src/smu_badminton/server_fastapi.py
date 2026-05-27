"""
FastAPI 应用主模块。

包含应用创建、路由定义和生命周期管理。
"""
from fastapi import FastAPI, BackgroundTasks
from contextlib import asynccontextmanager
import asyncio
import os
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict

from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi import Request, Response
from starlette.staticfiles import StaticFiles

from .server_models import (
    BookRequest, BookResponse, ScheduleRequest, ScheduleResponse,
    AvailabilityRequest, AvailabilityResponse, JobImmediateRequest, JobScheduledRequest,
    JobsListResponse, LocalBookingRequest, StopByParamsRequest, StopJobRequest,
    UpdateConfigRequest, LogoutRequest,
    CaptchaRequest, CaptchaResponse, LoginRequest, LoginResponse,
    MetricsMiddleware, RateLimitMiddleware,
    _acquire_lock, _locks_cleanup,
    _jobs, _jobs_guard,
    _availability_cache, _availability_locks, _availability_guard, _availability_ttl_sec,
    _availability_cleanup, _get_avail_lock, _convert_to_minimal,
    _metrics, _metrics_lock,
)
from .cas_manager import book_badminton_slot, schedule_booking, booking_manager
from .cas_login_requests import login_with_retry, compute_availability_for_date, get_token_cached, clear_token_cache
from .cas_login import (
    prepare_login_session, login_with_auto_captcha, login_with_manual_captcha,
    LoginErrorType
)
from .config import get_frontend_config, CAS_LOGIN_URL, CAS_CAPTCHA_URL, JOB_RETENTION_SEC, AUTHORIZED_USERS, UVICORN_RELOAD
from .core_utils import get_db_pool, init_db_tables, close_db_pool

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 获取项目根目录 (从 src/smu_badminton/ 向上两级)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logger.info(f"项目根目录: {BASE_DIR}")

# 尝试使用 orjson 加速
try:
    import orjson  # noqa: F401
    from fastapi.responses import ORJSONResponse as _DefaultResponse
except Exception:
    _DefaultResponse = None


# ============= 生命周期管理 =============

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动阶段：初始化数据库表
    init_db_tables()
    # 恢复未完成的定时任务
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
        close_db_pool()


async def _jobs_cleanup():
    """定期清理历史任务记录。"""
    import time as _time
    statuses = ("done", "failed", "cancelled", "skipped")
    while True:
        try:
            await asyncio.sleep(3600)  # 每60分钟清理一次
            cutoff = _time.time() - float(JOB_RETENTION_SEC)
            with get_db_pool().get_connection() as conn:
                cur = conn.execute(
                    f"DELETE FROM scheduled_jobs WHERE status IN ({','.join(['?']*len(statuses))}) AND created_at < ?",
                    (*statuses, cutoff),
                )
                deleted = cur.rowcount if cur.rowcount is not None else 0
            if deleted:
                logger.info(f"清理历史任务: 删除 {deleted} 条超过保留期({JOB_RETENTION_SEC}s) 的记录")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"历史任务清理失败: {e}")


# ============= FastAPI 应用创建 =============

if _DefaultResponse:
    app = FastAPI(title="羽毛球预约接口", version="1.0.0", lifespan=lifespan, default_response_class=_DefaultResponse)
else:
    app = FastAPI(title="羽毛球预约接口", version="1.0.0", lifespan=lifespan)

# CORS 配置
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

app.add_middleware(MetricsMiddleware)
app.add_middleware(RateLimitMiddleware)


# ============= 辅助函数 =============

import time as _time


def _get_token_cached(login_url: str, captcha_url: str, username: str, password: str) -> str | None:
    """获取缓存的 token。"""
    tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
    if not tokens or not tokens.get("access_token"):
        return None
    return tokens["access_token"]


# ============= 路由定义 =============

@app.get("/")
async def index():
    """首页。"""
    html_path = os.path.join(BASE_DIR, "templates", "index.html")
    logger.info(f"请求首页，HTML路径: {html_path}, 存在: {os.path.exists(html_path)}")
    if not os.path.exists(html_path):
        logger.error(f"HTML 文件不存在: {html_path}")
        return {"error": "未找到 index.html", "path": html_path, "base_dir": BASE_DIR}
    return FileResponse(html_path, media_type="text/html; charset=utf-8")


@app.get("/favicon.ico")
async def favicon():
    from starlette.responses import Response
    return Response(status_code=204, media_type="image/x-icon")


@app.get("/api/config")
async def get_config():
    """获取前端配置。"""
    from . import config as config_module
    import importlib
    importlib.reload(config_module)
    return {"ok": True, "data": config_module.get_frontend_config()}


@app.post("/api/captcha", response_model=CaptchaResponse)
async def get_captcha(req: CaptchaRequest):
    """获取验证码图片（base64 编码）。

    返回验证码图片的 base64 编码，用于前端显示。
    """
    import base64

    try:
        login_url = req.login_url or CAS_LOGIN_URL
        captcha_url = req.captcha_url or CAS_CAPTCHA_URL

        session, cas_login_url, execution_value, captcha_image, login_page_html = await run_in_threadpool(
            prepare_login_session, login_url, captcha_url
        )

        # 将验证码图片转为 base64
        captcha_base64 = base64.b64encode(captcha_image).decode('utf-8')

        # 存储会话信息供后续登录使用（使用临时存储）
        import uuid
        session_id = str(uuid.uuid4())
        _captcha_sessions[session_id] = {
            "session": session,
            "cas_login_url": cas_login_url,
            "execution_value": execution_value,
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


# 临时存储验证码会话（5分钟过期）
_captcha_sessions: Dict[str, dict] = {}


def _cleanup_captcha_sessions():
    """清理过期的验证码会话。"""
    now = _time.time()
    expired = [sid for sid, data in _captcha_sessions.items() if now - data["ts"] > 300]
    for sid in expired:
        del _captcha_sessions[sid]


@app.post("/api/login", response_model=LoginResponse)
async def api_login(req: LoginRequest):
    """登录接口。

    如果不提供 captcha_code，则自动使用 OCR 识别验证码。
    如果提供 captcha_code，则使用手动输入的验证码。

    返回值：
    - success: 登录成功，返回 tokens
    - error_type: captcha_error（验证码错误）, password_error（密码错误）等
    - need_manual_captcha: 是否需要手动输入验证码
    """
    try:
        login_url = req.login_url or CAS_LOGIN_URL
        captcha_url = req.captcha_url or CAS_CAPTCHA_URL

        if req.captcha_code:
            # 使用手动输入的验证码登录
            # 检查是否有缓存的 session
            session_data = _captcha_sessions.get(req.session_id) if hasattr(req, 'session_id') and req.session_id else None

            if session_data:
                # 使用已有的 session 和 execution
                result = await run_in_threadpool(
                    attempt_login_with_captcha,
                    session_data["session"],
                    session_data["cas_login_url"],
                    session_data["execution_value"],
                    req.username,
                    req.password,
                    req.captcha_code,
                    session_data.get("login_page_html")
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

        if result.success:
            # 缓存 token
            from .cas_login_requests import _TOKEN_CACHE, _TOKEN_LOCK
            with _TOKEN_LOCK:
                _TOKEN_CACHE[req.username] = {"tokens": result.tokens, "ts": _time.time()}

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


@app.get("/api/auth/check")
async def check_auth(username: str):
    """检查用户权限。"""
    return {"ok": True, "authorized": username in AUTHORIZED_USERS}


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/api/config/update")
async def update_config(req: UpdateConfigRequest):
    """更新配置。"""
    try:
        env_path = os.path.join(BASE_DIR, ".env")

        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        else:
            lines = []

        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith('CAS_LOGIN_URL='):
                new_lines.append(f'CAS_LOGIN_URL={req.login_url}\n')
                found = True
            else:
                new_lines.append(line)

        if not found:
            if new_lines and not new_lines[-1].endswith('\n'):
                new_lines.append('\n')
            new_lines.append(f'CAS_LOGIN_URL={req.login_url}\n')

        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

        logger.info(f"配置已更新: CAS_LOGIN_URL")

        try:
            import importlib
            from dotenv import load_dotenv
            from . import config as config_module
            load_dotenv(env_path, override=True)
            importlib.reload(config_module)
            new_login_url = config_module.CAS_LOGIN_URL
            logger.info(f"配置已重载，新值: {new_login_url[:80] if len(new_login_url) > 80 else new_login_url}...")
            return {"ok": True, "data": {"message": "配置已保存并自动重载，立即生效", "path": env_path, "reloaded": True, "new_value": new_login_url[:100]}}
        except Exception as reload_err:
            logger.warning(f"重载配置失败: {reload_err}，需要重启服务器")
            return {"ok": True, "data": {"message": "配置已保存，但重载失败，请重启服务器", "path": env_path, "reloaded": False}}
    except Exception as e:
        logger.error(f"更新配置失败: {e}")
        return {"ok": False, "error": f"更新失败: {str(e)}"}


@app.get("/jobs")
async def jobs_page():
    """任务监控页面。"""
    html_path = os.path.join(BASE_DIR, "templates", "jobs.html")
    if not os.path.exists(html_path):
        logger.error(f"HTML 文件不存在: {html_path}")
        return {"error": "未找到 jobs.html", "path": html_path}
    return FileResponse(html_path)


@app.post("/api/book", response_model=BookResponse)
async def api_book(req: BookRequest) -> BookResponse:
    """立即预约。"""
    resource_key = (req.resources_name, req.bookdate, req.kssj, req.jssj)
    lock = await _acquire_lock(resource_key)

    if lock.locked():
        return BookResponse(ok=False, error="resource_locked_processing")

    async with lock:
        try:
            with get_db_pool().get_connection() as conn:
                conn.execute(
                    "INSERT INTO local_bookings (username, bookdate, resources_name, kssj, jssj, created_at) VALUES (?,?,?,?,?,?)",
                    (req.username, req.bookdate, req.resources_name, req.kssj, req.jssj, _time.time()),
                )
        except sqlite3.IntegrityError:
            return BookResponse(ok=False, error="resource_already_booked")
        except Exception as e:
            logger.error(f"插入本地预约记录失败: {e}")
            return BookResponse(ok=False, error="database_error")

        result = await run_in_threadpool(
            book_badminton_slot,
            login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
            password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
            resources_name=req.resources_name,
        )

        if not result.get("ok"):
            try:
                with get_db_pool().get_connection() as conn:
                    conn.execute(
                        "DELETE FROM local_bookings WHERE username=? AND bookdate=? AND resources_name=? AND kssj=? AND jssj=?",
                        (req.username, req.bookdate, req.resources_name, req.kssj, req.jssj),
                    )
            except Exception as e:
                logger.warning(f"删除失败的本地预约记录失败: {e}")
            return BookResponse(ok=False, error=result.get("error") or "unknown_error")

        return BookResponse(ok=True, data=result.get("data") or {})


@app.post("/api/book/schedule", response_model=ScheduleResponse)
async def api_book_schedule(req: ScheduleRequest, background_tasks: BackgroundTasks) -> ScheduleResponse:
    """定时预约。"""
    resource_key = (req.resources_name, req.bookdate, req.kssj, req.jssj)
    lock = await _acquire_lock(resource_key)

    if lock.locked():
        return ScheduleResponse(ok=False, error="resource_locked_processing")

    # 去重检查
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

    try:
        with get_db_pool().get_connection() as conn:
            conn.execute(
                "INSERT INTO local_bookings (username, bookdate, resources_name, kssj, jssj, created_at) VALUES (?,?,?,?,?,?)",
                (req.username, req.bookdate, req.resources_name, req.kssj, req.jssj, _time.time()),
            )
    except sqlite3.IntegrityError:
        return ScheduleResponse(ok=False, error="resource_already_booked")
    except Exception as e:
        logger.error(f"插入本地预约记录失败: {e}")
        return ScheduleResponse(ok=False, error="database_error")

    if req.run_async:
        job_id = booking_manager.start_scheduled_booking(
            login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
            password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
            resources_name=req.resources_name, target_time_str=req.target_time_str, num_threads=req.num_threads,
        )
        return ScheduleResponse(ok=True, data={"scheduled": True, "job_id": job_id})

    async with lock:
        result = await run_in_threadpool(
            schedule_booking,
            login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
            password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
            resources_name=req.resources_name, target_time_str=req.target_time_str, num_threads=req.num_threads,
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


@app.post("/api/local_bookings")
async def api_save_local_booking(req: LocalBookingRequest):
    try:
        with get_db_pool().get_connection() as conn:
            conn.execute(
                "INSERT INTO local_bookings (username, bookdate, resources_name, kssj, jssj, created_at) VALUES (?,?,?,?,?,?)",
                (req.username, req.bookdate, req.resources_name, req.kssj, req.jssj, _time.time()),
            )
    except sqlite3.IntegrityError:
        return {"ok": False, "error": "resource_already_booked"}
    except Exception as e:
        logger.error(f"保存本地预约记录失败: {e}")
        return {"ok": False, "error": "database_error"}
    return {"ok": True}


@app.get("/api/local_bookings")
async def api_list_local_bookings(bookdate: str, response: JSONResponse, limit: int | None = None, offset: int = 0, fields: str | None = None, clean: int = 1):
    t0 = _time.perf_counter()
    try:
        beijing_tz = timezone(timedelta(hours=8))
        now_dt = datetime.now(beijing_tz)
        if clean:
            with get_db_pool().get_connection() as conn:
                cur = conn.execute("SELECT id, kssj, jssj FROM local_bookings WHERE bookdate = ?", (bookdate,))
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
                    logger.info(f"清理了 {len(to_delete)} 条过期预约记录")
    except Exception as e:
        logger.warning(f"清理过期记录失败: {e}")

    t1 = _time.perf_counter()
    with get_db_pool().get_connection() as conn:
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
        rows = [{"username": r[0], "bookdate": r[1], "resources_name": r[2], "kssj": r[3], "jssj": r[4], "created_at": r[5]} for r in cur.fetchall()]
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


@app.post("/api/availability", response_model=AvailabilityResponse)
async def api_availability(req: AvailabilityRequest, request: Request, response: Response) -> AvailabilityResponse:
    t0 = _time.perf_counter()
    nocache = request.query_params.get("nocache") == "1"
    minimal = request.query_params.get("min") == "1"
    token = req.token
    if not token:
        return AvailabilityResponse(ok=False, error="token_required")
    cache_key = (token[:20], req.bookdate)  # 用 token 前20字符作为缓存 key
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


@app.post("/api/jobs/immediate", response_model=JobsListResponse)
async def api_jobs_immediate(req: JobImmediateRequest):
    job_id = booking_manager.start_immediate_booking(
        login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
        password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
        resources_name=req.resources_name,
    )
    return {"ok": True, "data": {"job_id": job_id}}


@app.post("/api/jobs/scheduled", response_model=JobsListResponse)
async def api_jobs_scheduled(req: JobScheduledRequest):
    job_id = booking_manager.start_scheduled_booking(
        login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
        password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
        resources_name=req.resources_name, target_time_str=req.target_time_str, num_threads=req.num_threads,
    )
    return {"ok": True, "data": {"job_id": job_id}}


@app.get("/api/jobs", response_model=JobsListResponse)
async def api_jobs_list():
    jobs = booking_manager.list_jobs()
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
        pass
    if owner and req.current_username != owner:
        logger.warning(f"权限拒绝：用户 {req.current_username} 试图停止 {owner} 的任务 {job_id}")
        return {"ok": False, "data": {"error": "permission_denied", "message": "无权停止他人的任务"}}
    ok = booking_manager.stop_job(job_id)
    return {"ok": ok, "data": {"job_id": job_id}}


@app.post("/api/jobs/stop_by_params", response_model=JobsListResponse)
async def api_jobs_stop_by_params(req: StopByParamsRequest):
    if req.current_username != req.username:
        logger.warning(f"用户 {req.current_username} 尝试取消 {req.username} 的任务（参数校验未通过）")
        return {"ok": False, "data": {"error": "permission_denied", "message": "无权取消其他用户的预约任务"}}
    stopped = booking_manager.stop_by_params(
        username=req.username, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj, resources_name=req.resources_name
    )
    return {"ok": True, "data": {"stopped": stopped}}


@app.post("/api/logout")
async def api_logout(req: LogoutRequest):
    clear_token_cache(req.username)
    logger.info(f"用户 {req.username} 已登出，token 缓存已清除")
    return {"ok": True}


# ============= 静态文件挂载 =============

try:
    static_dir = os.path.join(BASE_DIR, "static")
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    logger.info(f"静态文件目录: {static_dir}")
except Exception as e:
    logger.warning(f"静态文件挂载失败: {e}")


# ============= 入口 =============

if __name__ == "__main__":
    import uvicorn
    try:
        booking_manager.load_pending_jobs()
        logger.info("成功加载待处理任务")
    except Exception as e:
        logger.warning(f"加载待处理任务失败: {e}")
    uvicorn.run(
        "smu_badminton.server_fastapi:app",
        host="0.0.0.0",
        port=5002,
        reload=UVICORN_RELOAD,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )
