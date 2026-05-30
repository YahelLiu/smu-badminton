"""
预约相关路由模块。

包含：立即预约、定时预约、可用性查询、本地预约记录。
"""
import logging
import sqlite3
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Dict

from fastapi import APIRouter, BackgroundTasks, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .server_models import (
    BookRequest, BookResponse,
    ScheduleRequest, ScheduleResponse,
    AvailabilityRequest, AvailabilityResponse,
    LocalBookingRequest,
    get_resource_lock,
)
from .cas_manager import book_badminton_slot, schedule_booking, booking_manager
from .token_profile import find_user_by_access_token
from .booking_api import (
    list_resources_by_account, _fetch_all_time_slots, _shared_session,
    _build_my_bookings_map, _merge_bookings, list_appointments_for_account,
)
from .core_utils import get_db_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["booking"])


# ============= 路由定义 =============

@router.post("/book", response_model=BookResponse)
async def api_book(req: BookRequest) -> BookResponse:
    """立即预约。"""
    resource_key = (req.resources_name, req.bookdate, req.kssj, req.jssj)
    lock = await get_resource_lock(resource_key)

    if lock.locked():
        return BookResponse(ok=False, error="resource_locked_processing")

    # 检查：同一用户同一天只能预约一个任务
    try:
        db_jobs = booking_manager.list_scheduled_jobs(username=req.username)
        for j in db_jobs:
            if (
                j.get("bookdate") == req.bookdate
                and j.get("status") in ("scheduled", "running")
            ):
                return BookResponse(ok=False, error="您当天已有预约任务，每人每天只能预约一次")
    except Exception:
        pass

    # 检查本地预约记录是否已存在（同一用户同一天）
    try:
        with get_db_pool().get_connection(auto_commit=False) as conn:
            cur = conn.execute(
                "SELECT 1 FROM local_bookings WHERE username=? AND bookdate=? LIMIT 1",
                (req.username, req.bookdate),
            )
            if cur.fetchone():
                return BookResponse(ok=False, error="您当天已有预约记录，每人每天只能预约一次")
    except Exception as e:
        logger.error(f"查询本地预约记录失败: {e}")

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


@router.post("/book/schedule", response_model=ScheduleResponse)
async def api_book_schedule(req: ScheduleRequest, background_tasks: BackgroundTasks) -> ScheduleResponse:
    """定时预约。"""
    resource_key = (req.resources_name, req.bookdate, req.kssj, req.jssj)
    lock = await get_resource_lock(resource_key)

    if lock.locked():
        return ScheduleResponse(ok=False, error="resource_locked_processing")

    # 去重检查：同一用户同一天只能预约一个任务
    try:
        db_jobs = booking_manager.list_scheduled_jobs(username=req.username)
        for j in db_jobs:
            if (
                j.get("bookdate") == req.bookdate
                and j.get("status") in ("scheduled", "running")
            ):
                return ScheduleResponse(ok=False, error="您当天已有预约任务，每人每天只能预约一次")
    except Exception:
        pass

    # 检查本地预约记录是否已存在（同一用户同一天）
    try:
        with get_db_pool().get_connection(auto_commit=False) as conn:
            cur = conn.execute(
                "SELECT 1 FROM local_bookings WHERE username=? AND bookdate=? LIMIT 1",
                (req.username, req.bookdate),
            )
            if cur.fetchone():
                return ScheduleResponse(ok=False, error="您当天已有预约记录，每人每天只能预约一次")
    except Exception as e:
        logger.error(f"查询本地预约记录失败: {e}")

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
        try:
            job_id = booking_manager.start_scheduled_booking(
                login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
                password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
                resources_name=req.resources_name, target_time_str=req.target_time_str, num_threads=req.num_threads,
            )
            return ScheduleResponse(ok=True, data={"scheduled": True, "job_id": job_id})
        except Exception as e:
            # 启动失败时回滚本地预约
            logger.error(f"启动定时预约失败: {e}")
            try:
                with get_db_pool().get_connection() as conn:
                    conn.execute(
                        "DELETE FROM local_bookings WHERE username=? AND bookdate=? AND resources_name=? AND kssj=? AND jssj=?",
                        (req.username, req.bookdate, req.resources_name, req.kssj, req.jssj),
                    )
            except Exception as rollback_err:
                logger.warning(f"回滚本地预约记录失败: {rollback_err}")
            return ScheduleResponse(ok=False, error=f"start_failed: {str(e)}")

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


@router.post("/local_bookings")
async def api_save_local_booking(req: LocalBookingRequest):
    """保存本地预约记录。"""
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


@router.get("/local_bookings")
async def api_list_local_bookings(bookdate: str, response: JSONResponse, limit: int | None = None, offset: int = 0, fields: str | None = None, clean: int = 1):
    """列出本地预约记录。"""
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


@router.post("/availability", response_model=AvailabilityResponse)
async def api_availability(req: AvailabilityRequest, request: Request, response: Response) -> AvailabilityResponse:
    """
    查询场地可用性。使用公共缓存：场地时间槽数据所有用户共享（60s TTL），
    仅 bookedByMe 按用户单独查询。
    """
    from .server_models import _avail_public_cache, _avail_public_lock, _avail_public_ttl_sec

    t0 = _time.perf_counter()
    token = req.token
    bookdate = req.bookdate
    if not token:
        return AvailabilityResponse(ok=False, error="token_required")

    # 从 token 缓存获取 id_token 和用户名
    username, id_token = find_user_by_access_token(token)

    now = _time.time()

    # 1. 查公共缓存（场地时间槽数据，所有用户共享）
    public_entry = _avail_public_cache.get(bookdate)
    if public_entry and now - public_entry["_ts"] < _avail_public_ttl_sec:
        # 缓存命中：只需查预约记录（1 个请求），合并 bookedByMe
        logger.info("[缓存] 公共缓存命中: %s", bookdate)
        slots_data = public_entry["data"]
        session = _shared_session()
        try:
            my_edges = await run_in_threadpool(list_appointments_for_account, token, bookdate, id_token, session)
            my_map = _build_my_bookings_map(my_edges)
            out_list = _merge_bookings(slots_data, my_map)
            t_total = (_time.perf_counter() - t0) * 1000.0
            response.headers["X-Avail-Cache"] = "HIT-PUBLIC"
            response.headers["X-Avail-TotalMs"] = f"{t_total:.2f}"
            response.headers["X-Avail-ListLen"] = str(len(out_list))
            return AvailabilityResponse(ok=True, data={"list": out_list})
        finally:
            session.close()

    # 2. 公共缓存 MISS：完整查询（资源列表 + 时间槽 + 预约记录）
    logger.info("[缓存] 公共缓存未命中: %s", bookdate)
    session = _shared_session()
    try:
        resources = await run_in_threadpool(list_resources_by_account, token, bookdate, None, id_token, "", session)

        if not resources:
            return AvailabilityResponse(ok=False, error="login_failed")

        slots_data = await run_in_threadpool(_fetch_all_time_slots, token, bookdate, resources, id_token, session)

        # 存入公共缓存
        async with _avail_public_lock:
            _avail_public_cache[bookdate] = {"data": slots_data, "_ts": now}

        # 查预约记录，合并 bookedByMe
        my_edges = await run_in_threadpool(list_appointments_for_account, token, bookdate, id_token, session)
        my_map = _build_my_bookings_map(my_edges)
        out_list = _merge_bookings(slots_data, my_map)

        t_total = (_time.perf_counter() - t0) * 1000.0
        response.headers["X-Avail-Cache"] = "MISS"
        response.headers["X-Avail-TotalMs"] = f"{t_total:.2f}"
        response.headers["X-Avail-ListLen"] = str(len(out_list))
        return AvailabilityResponse(ok=True, data={"list": out_list})
    finally:
        session.close()
