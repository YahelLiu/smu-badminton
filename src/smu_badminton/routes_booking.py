"""
预约相关路由模块。

包含：立即预约、定时预约、可用性查询、本地预约记录。
"""
import asyncio
import logging
import sqlite3
import time as _time
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool

from .server_models import (
    BookRequest, BookResponse,
    ScheduleRequest, ScheduleResponse,
    AvailabilityRequest, AvailabilityResponse,
    LocalBookingRequest,
    get_resource_lock,
    _avail_public_cache, _avail_public_lock, _avail_public_ttl_sec,
)
from .cas_manager import book_badminton_slot, booking_manager
from .token_profile import find_user_by_access_token
from .booking_api import (
    list_resources_by_account, _fetch_all_time_slots, _shared_session,
    _build_my_bookings_map, _merge_bookings, list_appointments_for_account,
)
from .core_utils import get_db_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["booking"])


# ============= 本地预约记录辅助（委托 BookingManager，供本模块与其他路由复用） =============

def _day_booking_conflict(username: str, bookdate: str) -> str | None:
    """检查同一用户同一天是否已有任务或预约记录，有则返回错误信息。"""
    return booking_manager.day_booking_conflict(username, bookdate)


def _insert_local_booking(username: str, bookdate: str, resources_name: str, kssj: str, jssj: str) -> str | None:
    """写入本地预约记录，成功返回 None，失败返回错误码。"""
    return booking_manager.add_local_booking(username, bookdate, resources_name, kssj, jssj)


def _delete_local_booking(username: str, bookdate: str, resources_name: str, kssj: str, jssj: str) -> None:
    """删除本地预约记录（预约失败或任务启动失败时回滚）。"""
    try:
        with get_db_pool().get_connection() as conn:
            conn.execute(
                "DELETE FROM local_bookings WHERE username=? AND bookdate=? AND resources_name=? AND kssj=? AND jssj=?",
                (username, bookdate, resources_name, kssj, jssj),
            )
    except Exception as e:
        logger.warning(f"删除本地预约记录失败: {e}")


# ============= 路由定义 =============

@router.post("/book", response_model=BookResponse)
async def api_book(req: BookRequest) -> BookResponse:
    """立即预约。"""
    resource_key = (req.resources_name, req.bookdate, req.kssj, req.jssj)
    lock = await get_resource_lock(resource_key)

    if lock.locked():
        return BookResponse(ok=False, error="resource_locked_processing")

    # 检查：同一用户同一天只能预约一个
    conflict = _day_booking_conflict(req.username, req.bookdate)
    if conflict:
        return BookResponse(ok=False, error=conflict)

    async with lock:
        err = _insert_local_booking(req.username, req.bookdate, req.resources_name, req.kssj, req.jssj)
        if err:
            return BookResponse(ok=False, error=err)

        result = await run_in_threadpool(
            book_badminton_slot,
            login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
            password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
            resources_name=req.resources_name,
        )

        if not result.get("ok"):
            _delete_local_booking(req.username, req.bookdate, req.resources_name, req.kssj, req.jssj)
            return BookResponse(ok=False, error=result.get("error") or "unknown_error")

        return BookResponse(ok=True, data=result.get("data") or {})


@router.post("/book/schedule", response_model=ScheduleResponse)
async def api_book_schedule(req: ScheduleRequest) -> ScheduleResponse:
    """定时预约（统一走后台任务；run_async 字段仅为兼容旧客户端保留）。"""
    resource_key = (req.resources_name, req.bookdate, req.kssj, req.jssj)
    lock = await get_resource_lock(resource_key)

    if lock.locked():
        return ScheduleResponse(ok=False, error="resource_locked_processing")

    # 去重检查：同一用户同一天只能预约一个
    conflict = _day_booking_conflict(req.username, req.bookdate)
    if conflict:
        return ScheduleResponse(ok=False, error=conflict)

    err = _insert_local_booking(req.username, req.bookdate, req.resources_name, req.kssj, req.jssj)
    if err:
        return ScheduleResponse(ok=False, error=err)

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
        _delete_local_booking(req.username, req.bookdate, req.resources_name, req.kssj, req.jssj)
        return ScheduleResponse(ok=False, error=f"start_failed: {str(e)}")


@router.post("/local_bookings")
async def api_save_local_booking(req: LocalBookingRequest):
    """保存本地预约记录。"""
    err = _insert_local_booking(req.username, req.bookdate, req.resources_name, req.kssj, req.jssj)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True}


@router.get("/local_bookings")
async def api_list_local_bookings(bookdate: str, response: Response, limit: int | None = None, offset: int = 0, fields: str | None = None):
    """列出本地预约记录。

    过期记录由后台任务周期清理（server_fastapi._stale_local_bookings_cleanup），
    请求路径不再做全表扫描。
    """
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
    if fields:
        allow = {"username", "bookdate", "resources_name", "kssj", "jssj", "created_at"}
        wanted = [f.strip() for f in fields.split(",") if f.strip() in allow]
        if wanted:
            rows = [{k: v for k, v in row.items() if k in wanted} for row in rows]
    response.headers["X-LBookings-Count"] = str(len(rows))
    return {"ok": True, "data": {"list": rows}}


_inflight_slots: dict[str, "asyncio.Task"] = {}


def _load_public_slots_blocking(token: str, bookdate: str, id_token: str):
    """阻塞式完整查询：资源列表 + 全部时间槽（single-flight 的执行体）。"""
    session = _shared_session()
    try:
        resources = list_resources_by_account(token, bookdate, None, id_token, "", session)
        if not resources:
            return None
        return _fetch_all_time_slots(token, bookdate, resources, id_token, session)
    finally:
        session.close()


@router.post("/availability", response_model=AvailabilityResponse)
async def api_availability(req: AvailabilityRequest, request: Request, response: Response) -> AvailabilityResponse:
    """
    查询场地可用性。使用公共缓存：场地时间槽数据所有用户共享（60s TTL），
    仅 bookedByMe 按用户单独查询。

    公共缓存 MISS 时启用 single-flight：同一日期只有一个在途全量查询，
    其余请求共享其结果，避免 60s 缓存过期瞬间的雷群效应。
    """
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

    # 2. 公共缓存 MISS：single-flight 执行全量查询
    logger.info("[缓存] 公共缓存未命中: %s", bookdate)
    task = _inflight_slots.get(bookdate)
    leader = False
    if task is None or task.done():
        task = asyncio.create_task(run_in_threadpool(_load_public_slots_blocking, token, bookdate, id_token))
        _inflight_slots[bookdate] = task
        leader = True
    else:
        logger.info("[缓存] single-flight 复用在途查询: %s", bookdate)

    try:
        slots_data = await asyncio.shield(task)
    except Exception as e:
        logger.warning("全量可用性查询失败: %s", e)
        return AvailabilityResponse(ok=False, error="login_failed")
    finally:
        if leader and _inflight_slots.get(bookdate) is task:
            _inflight_slots.pop(bookdate, None)

    if slots_data is None:
        return AvailabilityResponse(ok=False, error="login_failed")

    async with _avail_public_lock:
        _avail_public_cache[bookdate] = {"data": slots_data, "_ts": now}

    # 查本人的预约记录，合并 bookedByMe（独立短会话）
    session = _shared_session()
    try:
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
