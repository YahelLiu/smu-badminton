"""
任务相关路由模块。

包含：任务列表、停止任务、metrics。
"""
import logging
import os
from typing import Dict

from fastapi import APIRouter
from fastapi.responses import FileResponse

from .server_models import (
    JobImmediateRequest, JobScheduledRequest,
    JobsListResponse, StopByParamsRequest, StopJobRequest,
    _jobs, _jobs_guard, _metrics, _metrics_lock,
)
from .cas_manager import booking_manager
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

@router.post("/api/jobs/immediate", response_model=JobsListResponse)
async def api_jobs_immediate(req: JobImmediateRequest):
    """创建即时预约任务。"""
    job_id = booking_manager.start_immediate_booking(
        login_url=req.login_url, captcha_url=req.captcha_url, username=req.username,
        password=req.password, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj,
        resources_name=req.resources_name,
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


@router.get("/api/jobs", response_model=JobsListResponse)
async def api_jobs_list():
    """列出所有任务。"""
    jobs = booking_manager.list_jobs()
    db_jobs = booking_manager.list_scheduled_jobs()
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


@router.post("/api/jobs/stop_by_params", response_model=JobsListResponse)
async def api_jobs_stop_by_params(req: StopByParamsRequest):
    """根据参数停止任务。

    权限检查：current_username 必须与 username 匹配。
    """
    if req.current_username != req.username:
        logger.warning(f"用户 {req.current_username} 尝试取消 {req.username} 的任务（权限拒绝）")
        return {"ok": False, "data": {"error": "permission_denied", "message": "无权取消其他用户的预约任务"}}

    stopped = booking_manager.stop_by_params(
        username=req.username, bookdate=req.bookdate, kssj=req.kssj, jssj=req.jssj, resources_name=req.resources_name
    )
    return {"ok": True, "data": {"stopped": stopped}}


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
