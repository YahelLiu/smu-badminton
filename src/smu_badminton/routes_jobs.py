"""
任务相关路由模块。

包含：任务列表、停止任务、metrics。
"""
import logging
import os

from fastapi import APIRouter
from fastapi.responses import FileResponse

from .server_models import (
    JobImmediateRequest, JobScheduledRequest,
    JobsListResponse, StopByParamsRequest, StopJobRequest,
    get_resource_lock,
    _jobs, _jobs_guard, _metrics, _metrics_lock,
)
from .cas_manager import booking_manager
from .token_profile import find_user_by_access_token
from .config import BASE_DIR

logger = logging.getLogger(__name__)

router = APIRouter(tags=["jobs"])


# ============= 页面路由 =============

@router.get("/jobs")
async def jobs_page():
    """任务监控页面。"""
    html_path = os.path.join(BASE_DIR, "templates", "jobs.html")
    if not os.path.exists(html_path):
        logger.error(f"HTML 文件不存在: {html_path}")
        return {"error": "未找到 jobs.html", "path": html_path}
    return FileResponse(html_path)


# ============= API 路由 =============

@router.post("/api/jobs/immediate")
async def api_jobs_immediate(req: JobImmediateRequest):
    """创建即时预约任务（异步：立即返回 job_id，结果经任务轮询获取）。

    与同步的 /api/book 共享同一套前置校验与本地占位记录；
    任务失败/跳过时由后台线程回滚占位记录。
    """
    resource_key = (req.resources_name, req.bookdate, req.kssj, req.jssj)
    lock = await get_resource_lock(resource_key)
    if lock.locked():
        return {"ok": False, "error": "resource_locked_processing"}

    conflict = booking_manager.day_booking_conflict(req.username, req.bookdate)
    if conflict:
        return {"ok": False, "error": conflict}

    insert_err = booking_manager.add_local_booking(
        req.username, req.bookdate, req.resources_name, req.kssj, req.jssj
    )
    if insert_err:
        return {"ok": False, "error": insert_err}

    job_id = booking_manager.start_immediate_booking(
        login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
        password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
        resources_name=req.resources_name, rollback_local_on_fail=True,
    )
    return {"ok": True, "data": {"job_id": job_id}}


@router.post("/api/jobs/scheduled", response_model=JobsListResponse)
async def api_jobs_scheduled(req: JobScheduledRequest):
    """创建定时预约任务。"""
    job_id = booking_manager.start_scheduled_booking(
        login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
        password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
        resources_name=req.resources_name, target_time_str=req.target_time_str, num_threads=req.num_threads,
    )
    return {"ok": True, "data": {"job_id": job_id}}


@router.get("/api/jobs")
async def api_jobs_list(username: str | None = None):
    """列出任务。

    username 参数可选：提供时仅返回该用户的任务（前端轮询使用），
    不提供时返回全部（任务监控页使用）。
    """
    jobs = booking_manager.list_jobs()
    db_jobs = booking_manager.list_scheduled_jobs()
    if username:
        jobs = [j for j in jobs if (j.get("username") or j.get("params", {}).get("username")) == username]
        db_jobs = [j for j in db_jobs if j.get("username") == username]
    return {"ok": True, "data": {"jobs": jobs, "db_jobs": db_jobs}}


@router.get("/api/schedule/{job_id}")
async def api_schedule_status(job_id: str):
    """获取任务状态。"""
    with _jobs_guard:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "job_not_found"}
        return {"ok": True, "data": job}


@router.post("/api/jobs/{job_id}/stop", response_model=JobsListResponse)
async def api_jobs_stop(job_id: str, req: StopJobRequest):
    """停止任务。

    权限检查：
    - 必须提供 current_username
    - 任务的 owner 必须与 current_username 匹配
    - 如果获取 owner 失败，返回 job_lookup_failed
    - 如果任务不存在，返回 job_not_found
    """
    owner = None
    try:
        owner = booking_manager.get_job_owner(job_id)
    except Exception as e:
        logger.warning(f"获取任务所有者失败: {job_id}, {e}")
        return {"ok": False, "data": {"error": "job_lookup_failed", "message": "无法获取任务信息"}}

    if owner is None:
        return {"ok": False, "data": {"error": "job_not_found", "message": "任务不存在"}}

    if req.current_username != owner:
        logger.warning(f"权限拒绝：用户 {req.current_username} 试图停止 {owner} 的任务 {job_id}")
        return {"ok": False, "data": {"error": "permission_denied", "message": "无权停止他人的任务"}}

    ok = booking_manager.stop_job(job_id)
    return {"ok": ok, "data": {"job_id": job_id}}


@router.post("/api/jobs/stop_by_params")
async def api_jobs_stop_by_params(req: StopByParamsRequest):
    """根据参数停止任务，并尽力撤销学校侧已生效的预约。

    权限检查：current_username 必须与 username 匹配。
    """
    if req.current_username != req.username:
        logger.warning(f"用户 {req.current_username} 尝试取消 {req.username} 的任务（权限拒绝）")
        return {"ok": False, "data": {"error": "permission_denied", "message": "无权取消其他用户的预约任务"}}

    id_token = ""
    if req.access_token:
        _, id_token = find_user_by_access_token(req.access_token)

    result = booking_manager.stop_by_params(
        username=req.username, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
        resources_name=req.resources_name,
        access_token=req.access_token,
        id_token=id_token,
    )
    logger.info(
        "按参数停止: %s - %s %s-%s stopped=%s upstream=%s",
        req.username, req.bookdate, req.kssj, req.jssj, result.get("stopped"), result.get("upstream_status"),
    )
    return {"ok": True, "data": result}


@router.get("/api/metrics")
async def api_metrics():
    """获取请求指标。"""
    async with _metrics_lock:
        out = {
            k: {
                "count": int(v["count"]),
                "avg_ms": (v["total_ms"] / v["count"]) if v["count"] else 0.0,
                "max_ms": v["max_ms"]
            }
            for k, v in _metrics.items()
        }
    return {"ok": True, "data": out}
