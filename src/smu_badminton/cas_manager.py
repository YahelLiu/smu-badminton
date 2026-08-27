import threading
from typing import Any, Dict, List, Optional, Tuple
from enum import Enum
import time
import uuid
import logging

# 从各模块导入
from .cas_login import login_with_retry
from .token_profile import get_cached_token, cache_token_for_user, refresh_token_for_user, token_exp_epoch
from .booking_api import (
    fetch_resource_time_id,
    make_appointment,
    list_appointments_for_account,
    check_resource_time_slot_capacity,
    solve_and_verify_slide_captcha,
    resolve_user_info,
    find_my_appointment_id,
    check_appointment_cancel_time,
    update_appointment_state,
)
from .http_utils import (
    get_network_time,
    get_target_datetime_from_network,
    ClockSync,
)

# 导入核心工具模块
from .core_utils import (
    get_db_pool,
    handle_errors,
    db_operation,
    success_response,
    obfuscate_password,
    deobfuscate_password,
)

# 配置日志
logger = logging.getLogger(__name__)


# ============= 抢票节奏常量（2026-08-27 对上游实测定案）=============
#
# 实测结论（scripts/test_captcha_reuse.py）：
# 1. 上游对 saveAppointmentInformationAll 按账号限流：约 2 连发内安全，
#    第 3 发立即触发「频繁调用接口，禁用3分钟」→ 并发枪数硬上限为 2；
# 2. 滑块验证码一次性消费：同一凭证第二次提交返回「验证码不能重复使用」，
#    且第一枪即被烧掉 → 每枪必须自带独立凭证，在预取窗口内解好几份备着；
# 3. 验证码求解单次成功率约 70%，失败为 checkCaptcha 4001，重试即可，
#    重试冗余放在预取窗口（时间充裕），不放 T-0；
# 4. 服务端先验验证码后走业务，无效凭证返回「系统异常」。

# 并发提交枪数上限（受上游账号级频控约束）
MAX_UPSTREAM_BURST = 2

# 距目标时刻多少秒唤醒，进入预取窗口（登录/预取/验证码池构建）
PREFETCH_WINDOW_SEC = 75

# 验证码池构建最晚截止点（距目标时刻秒数）；此后不再尝试解新码
CAPTCHA_HARD_STOP_BEFORE_SEC = 35

# 抢票请求超时秒数（快速失败；预约接口正常 <1s 返回）
RUSH_REQUEST_TIMEOUT_SEC = 4

# token exp 距目标时刻不足该秒数时提前重新登录
TOKEN_EXP_BUFFER_SEC = 120


def _classify_upstream_response(resp) -> str:
    """对 saveAppointmentInformationAll 的响应分类（展开 GraphQL 包体）。

    Returns:
        success / banned / captcha_error / other
    """
    if not isinstance(resp, dict):
        return "other"
    inner = resp
    data = resp.get("data")
    if isinstance(data, dict) and isinstance(data.get("saveAppointmentInformationAll"), dict):
        inner = data["saveAppointmentInformationAll"]
    code = str(inner.get("code", "")).lower()
    if code in ("0", "success"):
        return "success"
    msgs = " ".join(str(m) for m in inner.get("messages") or [])
    if any(k in msgs for k in ("频繁", "禁用", "禁止", "解禁")):
        return "banned"
    if "验证码" in msgs or "captcha" in msgs.lower():
        return "captcha_error"
    return "other"


# ============= 等待辅助（定时任务共用） =============

def _sleep_coarse(diff_sec: float) -> None:
    """长等待的分级休眠策略，适应跨天等待场景。"""
    if diff_sec > 3 * 24 * 60 * 60:  # > 3天
        time.sleep(12 * 60 * 60)  # 休眠12小时
    elif diff_sec > 24 * 60 * 60:  # > 1天
        time.sleep(6 * 60 * 60)  # 休眠6小时
    elif diff_sec > 6 * 60 * 60:  # > 6小时
        time.sleep(2 * 60 * 60)  # 休眠2小时
    elif diff_sec > 2 * 60 * 60:  # > 2小时
        time.sleep(30 * 60)  # 休眠30分钟
    elif diff_sec > 30 * 60:  # > 30分钟
        time.sleep(5 * 60)  # 休眠5分钟
    elif diff_sec > 10 * 60:  # > 10分钟
        time.sleep(60)  # 休眠1分钟
    else:
        time.sleep(10)


def _sleep_fine(diff_sec: float) -> None:
    """临近目标时间的精细休眠。"""
    if diff_sec > 10:
        time.sleep(5)
    elif diff_sec > 1:
        time.sleep(0.5)
    else:
        time.sleep(0.1)


def _wait_until(
    target_time,
    wake_delta_sec: float,
    cancel_event: Optional[threading.Event] = None,
    now_fn=None
) -> bool:
    """等待直到距 target_time 不足 wake_delta_sec 秒。

    now_fn 为空时沿用旧行为：每轮调用 get_network_time()（HTTP 对时，精度受 RTT 抖动）。
    抢票关键窗口应传入 ClockSync.now（本地钟 + 预校准偏移，无 HTTP）。

    Returns:
        True 表示到达唤醒窗口；False 表示被取消。
    """
    while cancel_event is None or not cancel_event.is_set():
        if now_fn is not None:
            now = now_fn()
        else:
            now = get_network_time()
        if now:
            diff_sec = (target_time - now).total_seconds()
            if diff_sec <= wake_delta_sec:
                return True
            if wake_delta_sec > 0:
                _sleep_coarse(diff_sec)
            elif diff_sec > 10:
                # 可取消时分片休眠，保证 stop 响应在 1s 内
                time.sleep(1.0 if cancel_event is not None else 5.0)
            elif diff_sec > 1:
                time.sleep(0.5)
            else:
                time.sleep(0.1)
        else:
            time.sleep(1)
    return False


# ============= Token 获取（缓存或登录）=============

def get_token_cached(
    login_url: str,
    captcha_url: str,
    username: str,
    password: str,
    ttl_seconds: int = 900
) -> Optional[Dict[str, str]]:
    """
    获取缓存的 token 或重新登录。

    Args:
        login_url: CAS 登录 URL
        captcha_url: 验证码 URL
        username: 用户名
        password: 密码
        ttl_seconds: 缓存 TTL 秒数

    Returns:
        token 字典（包含 access_token 和 id_token），失败返回 None
    """
    t0 = time.time()

    tokens_cached = get_cached_token(username, ttl_seconds)
    if tokens_cached:
        logger.info("[性能] Token 缓存命中: %.0fms", (time.time() - t0) * 1000)
        return tokens_cached

    logger.info("[性能] Token 缓存未命中，开始登录...")
    t1 = time.time()
    tokens = login_with_retry(login_url, captcha_url, username, password, max_retries=3)
    t2 = time.time()
    logger.info("[性能] CAS 登录耗时: %.0fms", (t2 - t1) * 1000)

    if not tokens or not tokens.get("access_token"):
        return None

    cache_token_for_user(username, tokens)
    logger.info("[性能] get_token_cached 总耗时: %.0fms", (time.time() - t0) * 1000)
    return tokens


# ============= 任务状态机 =============

def _get_slide_captcha(access_token: str) -> Tuple[str, str]:
    """获取滑块验证码（如果需要）。

    Returns:
        (captcha_id, captcha_code) 元组
    """
    try:
        result = solve_and_verify_slide_captcha(access_token)
        if result:
            return result
    except Exception as e:
        logger.error("滑块验证码处理异常: %s", e)
    return "", ""


class JobState(str, Enum):
    """任务状态枚举。

    状态转换规则：
    - scheduled -> running -> done
    - scheduled -> cancelled
    - running -> failed
    - running -> skipped
    """
    SCHEDULED = "scheduled"   # 已创建，等待执行（包括休眠和预登录阶段）
    RUNNING = "running"       # 已到达目标时间，正在执行预约
    DONE = "done"             # 预约成功（终态）
    FAILED = "failed"         # 预约失败（终态）
    SKIPPED = "skipped"       # 已有预约，跳过（终态）
    CANCELLED = "cancelled"   # 已取消（终态）


# 终态集合（不可被覆盖）
TERMINAL_STATES = {JobState.DONE, JobState.FAILED, JobState.SKIPPED, JobState.CANCELLED}

# 允许的状态转换
VALID_TRANSITIONS: Dict[JobState, set] = {
    JobState.SCHEDULED: {JobState.RUNNING, JobState.CANCELLED, JobState.FAILED, JobState.SKIPPED},
    JobState.RUNNING: {JobState.DONE, JobState.FAILED, JobState.SKIPPED},
    JobState.DONE: set(),
    JobState.FAILED: set(),
    JobState.SKIPPED: set(),
    JobState.CANCELLED: set(),
}


class BookingJob:
    def __init__(self, thread: threading.Thread, cancel_event: threading.Event, meta: Dict[str, Any]):
        self.thread = thread
        self.cancel_event = cancel_event
        self.meta = meta  # {type: 'immediate'|'scheduled', created_at, params}


class BookingManager:
    """
    预约任务管理器：负责创建、跟踪、终止任务

    改进说明：
    - 使用统一的全局数据库连接池（get_db_pool）
    - 使用统一的错误处理装饰器
    - 结构化日志记录
    """
    def __init__(self) -> None:
        self._jobs: Dict[str, BookingJob] = {}
        self._lock = threading.Lock()

        # 使用全局数据库连接池
        self._db_pool = get_db_pool()

        logger.info("预约管理器初始化完成")

    @handle_errors(default_return=[], log_error=True, error_message="获取任务列表失败")
    def list_jobs(self) -> List[Dict[str, Any]]:
        """获取所有活跃任务列表"""
        with self._lock:
            out: List[Dict[str, Any]] = []
            for job_id, job in self._jobs.items():
                # 过滤已结束线程，顺便回收
                if not job.thread.is_alive() or job.cancel_event.is_set():
                    continue
                out.append({
                    "job_id": job_id,
                    "alive": job.thread.is_alive(),
                    "type": job.meta.get("type"),
                    "created_at": job.meta.get("created_at"),
                    "username": job.meta.get("username") or job.meta.get("params", {}).get("username"),
                    "params": job.meta.get("params", {}),
                })
            return out

    def stop_job(self, job_id: str, delete_local_booking: bool = True) -> bool:
        """
        停止任务

        Args:
            job_id: 任务ID
            delete_local_booking: 是否删除本地预约记录（默认True）

        Returns:
            是否成功停止任务
        """
        with self._lock:
            job = self._jobs.get(job_id)

        # 先从DB取参数
        job_row = self._get_job_row(job_id)

        if job:
            job.cancel_event.set()
            # 使用较短的超时避免阻塞API响应
            job.thread.join(timeout=0.5)
            # 从内存移除
            with self._lock:
                self._jobs.pop(job_id, None)

        # 安全标记取消（检查状态机）
        self._safe_update_status(job_id, JobState.CANCELLED)

        # 清除本地预约记录
        if delete_local_booking and job_row:
            self._delete_local_booking(
                username=job_row.get("username", ""),
                bookdate=job_row.get("bookdate", ""),
                kssj=job_row.get("kssj", ""),
                jssj=job_row.get("jssj", ""),
                resources_name=job_row.get("resources_name", ""),
            )

        logger.info(f"任务已停止: {job_id}")
        return bool(job) or bool(job_row)

    def _register(self, thread: threading.Thread, cancel_event: threading.Event, meta: Dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = BookingJob(thread, cancel_event, meta)
        return job_id

    # ---- 数据库操作方法（使用连接池和装饰器） ----
    
    @db_operation
    def _persist_job_row(self, job_id: str, *, login_url: str, captcha_url: str, username: str, password: str, bookdate: str, kssj: str, jssj: str, resources_name: str, target_time_str: str, num_threads: int, status: str):
        """持久化任务到数据库（密码混淆存储）"""
        with self._db_pool.get_connection() as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, login_url, captcha_url, username, obfuscate_password(password), bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status, time.time()),
            )
        logger.debug(f"任务已持久化: {job_id}")

    @db_operation
    def _get_current_status(self, job_id: str) -> str | None:
        """获取任务当前状态。"""
        with self._db_pool.get_connection(auto_commit=False) as conn:
            cur = conn.execute("SELECT status FROM scheduled_jobs WHERE job_id=?", (job_id,))
            row = cur.fetchone()
            return row[0] if row else None

    def _safe_update_status(self, job_id: str, new_status: JobState) -> bool:
        """
        安全更新任务状态，遵守状态机规则。

        Args:
            job_id: 任务ID
            new_status: 新状态

        Returns:
            是否成功更新（False 表示终态不可更改或无效转换）
        """
        try:
            current = self._get_current_status(job_id)
            if not current:
                logger.warning(f"任务不存在: {job_id}")
                return False

            # 解析当前状态
            try:
                current_state = JobState(current)
            except ValueError:
                logger.warning(f"任务状态异常: {job_id} -> {current}")
                return False

            # 检查是否为终态
            if current_state in TERMINAL_STATES:
                logger.debug(f"任务已处于终态，不可更改: {job_id} -> {current_state.value}")
                return False

            # 检查状态转换是否合法
            if new_status not in VALID_TRANSITIONS.get(current_state, set()):
                logger.warning(f"非法状态转换: {job_id} {current_state.value} -> {new_status.value}")
                return False

            # 执行更新
            with self._db_pool.get_connection() as conn:
                conn.execute("UPDATE scheduled_jobs SET status=? WHERE job_id=?", (new_status.value, job_id))
            logger.debug(f"任务状态已更新: {job_id} {current_state.value} -> {new_status.value}")
            return True
        except Exception as e:
            logger.error(f"更新任务状态失败: {job_id}, {e}")
            return False

    @handle_errors(default_return=None, log_error=True)
    def _get_job_row(self, job_id: str) -> Dict[str, Any] | None:
        """获取任务记录"""
        with self._db_pool.get_connection(auto_commit=False) as conn:
            cur = conn.execute(
                "SELECT job_id, username, bookdate, kssj, jssj, resources_name FROM scheduled_jobs WHERE job_id=?",
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "job_id": row[0],
                "username": row[1],
                "bookdate": row[2],
                "kssj": row[3],
                "jssj": row[4],
                "resources_name": row[5],
            }

    def get_job_owner(self, job_id: str) -> str | None:
        """获取任务的所有者用户名。"""
        row = self._get_job_row(job_id)
        return row.get("username") if row else None

    @db_operation
    def _delete_local_booking(self, *, username: str, bookdate: str, kssj: str, jssj: str, resources_name: str):
        """删除本地预约记录"""
        with self._db_pool.get_connection() as conn:
            conn.execute(
                "DELETE FROM local_bookings WHERE username=? AND bookdate=? AND kssj=? AND jssj=? AND resources_name=?",
                (username, bookdate, kssj, jssj, resources_name),
            )
        logger.debug(f"本地预约记录已删除: {username} - {bookdate} {kssj}-{jssj}")

    def _cleanup_job(self, job_id: str, status: JobState, *, username: str, bookdate: str, kssj: str, jssj: str, resources_name: str):
        """
        统一的任务清理方法。

        Args:
            job_id: 任务ID
            status: 最终状态 (cancelled/failed/skipped/done)
            username, bookdate, kssj, jssj, resources_name: 用于删除本地预约记录
        """
        try:
            self._safe_update_status(job_id, status)
            self._delete_local_booking(
                username=username,
                bookdate=bookdate,
                kssj=kssj,
                jssj=jssj,
                resources_name=resources_name
            )
            # 从内存移除
            with self._lock:
                self._jobs.pop(job_id, None)
        except Exception as e:
            logger.warning(f"清理任务失败: {job_id}, {e}")

    @db_operation
    def _cancel_scheduled_by_params(self, *, username: str, bookdate: str, kssj: str, jssj: str, resources_name: str) -> int:
        """根据参数将仍在等待/运行中的任务标记为 cancelled"""
        with self._db_pool.get_connection() as conn:
            cur = conn.execute(
                "UPDATE scheduled_jobs SET status='cancelled' WHERE username=? AND bookdate=? AND kssj=? AND jssj=? AND resources_name=? AND status IN ('scheduled','running')",
                (username, bookdate, kssj, jssj, resources_name),
            )
            affected = cur.rowcount if cur.rowcount is not None else 0
            logger.debug(f"按参数标记 cancelled: {affected} 条任务记录")
            return affected

    @handle_errors(default_return={"stopped": 0}, log_error=True, error_message="按参数停止任务失败")
    def stop_by_params(
        self,
        *,
        username: str, bookdate: str, kssj: str, jssj: str, resources_name: str,
        access_token: str = "", id_token: str = "",
    ) -> Dict[str, Any]:
        """
        根据预约参数停止任务，并尽力撤销上游已生效的预约。

        流程：
        1. 停止内存中的活跃任务
        2. 将数据库中的对应任务标记为 cancelled（保留历史记录）
        3. 删除本地预约记录
        4. 上游撤销：凭 access_token 或服务端保存的账号静默重登，
           定位匹配的预约后调用官方 checkAppointmentCancelTime + updateAppointmentInformationState

        Returns:
            {"stopped": int, "upstream_status": "cancelled"|"none"|"skipped"|"failed", "message": str}
        """
        # 1. 停止内存中的任务
        stopped = 0
        ids = self.find_job_ids_by_params(
            username=username,
            bookdate=bookdate,
            kssj=kssj,
            jssj=jssj,
            resources_name=resources_name
        )
        for jid in ids:
            if self.stop_job(jid, delete_local_booking=False):
                stopped += 1

        # 2/3. 标记 cancelled + 删除本地记录
        stopped += self._cancel_scheduled_by_params(
            username=username,
            bookdate=bookdate,
            kssj=kssj,
            jssj=jssj,
            resources_name=resources_name
        )
        self._delete_local_booking(
            username=username,
            bookdate=bookdate,
            kssj=kssj,
            jssj=jssj,
            resources_name=resources_name
        )

        result: Dict[str, Any] = {"stopped": stopped, "upstream_status": "skipped", "message": ""}

        # 4. 上游真取消：优先用调用方 token；否则尝试用保存的账号静默重登
        if not access_token:
            refreshed = refresh_token_for_user(username)
            if refreshed and refreshed.get("access_token"):
                access_token = refreshed["access_token"]
                id_token = refreshed.get("id_token", "")

        if not access_token:
            result["upstream_status"] = "skipped"
            result["message"] = "无可用登录凭据，仅取消了本地排队"
            logger.info("按参数停止: %s 无可用凭据，跳过上游撤销", username)
            return result

        try:
            appt_id = find_my_appointment_id(access_token, bookdate, kssj, jssj, resources_name, id_token=id_token)
            if not appt_id:
                result["upstream_status"] = "none"
                result["message"] = "学校侧没有该时段的有效预约"
                return result

            allowed, chk_msg = check_appointment_cancel_time(access_token, appt_id, id_token=id_token)
            if not allowed:
                result["upstream_status"] = "failed"
                result["message"] = f"当前不允许取消: {chk_msg or '未知原因'}（预约ID {appt_id}）"
                return result

            ok, upd_msg = update_appointment_state(access_token, appt_id, id_token=id_token)
            if ok:
                result["upstream_status"] = "cancelled"
                result["message"] = "学校侧预约已撤销"
            else:
                result["upstream_status"] = "failed"
                result["message"] = f"撤销失败: {upd_msg}（预约ID {appt_id}）"
        except Exception as e:
            logger.warning("上游撤销异常: %s", e)
            result["upstream_status"] = "failed"
            result["message"] = f"撤销异常: {e}"
        return result

    @handle_errors(default_return=[], log_error=True, error_message="获取定时任务列表失败")
    def list_scheduled_jobs(self, username: str | None = None) -> List[Dict[str, Any]]:
        """获取所有任务列表（包括即时任务和定时任务）"""
        with self._db_pool.get_connection(auto_commit=False) as conn:
            if username:
                cur = conn.execute(
                    "SELECT job_id, username, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status, created_at FROM scheduled_jobs WHERE username=? ORDER BY created_at DESC",
                    (username,),
                )
            else:
                cur = conn.execute(
                    "SELECT job_id, username, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status, created_at FROM scheduled_jobs ORDER BY created_at DESC"
                )
            rows = [
                {
                    "job_id": r[0],
                    "username": r[1],
                    "bookdate": r[2],
                    "kssj": r[3],
                    "jssj": r[4],
                    "resources_name": r[5],
                    "target_time_str": r[6],
                    "num_threads": r[7],
                    "status": r[8],
                    "created_at": r[9],
                    # 根据 target_time_str 判断任务类型
                    "type": "scheduled" if r[6] else "immediate",
                }
                for r in cur.fetchall()
            ]
        return rows

    @handle_errors(default_return=None, log_error=True, error_message="加载待处理任务失败")
    def load_pending_jobs(self) -> None:
        """从数据库恢复待处理的定时任务（服务重启后调用）"""
        with self._db_pool.get_connection(auto_commit=False) as conn:
            cur = conn.execute(
                "SELECT job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status FROM scheduled_jobs WHERE status IN ('scheduled','running')"
            )
            rows = cur.fetchall()
        for r in rows:
            job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, _status = r
            # Skip if already live in memory
            with self._lock:
                if job_id in self._jobs:
                    continue
            # 还原混淆的密码
            original_password = deobfuscate_password(password) if password else ""
            # Recreate scheduled job using stored params
            self.start_scheduled_booking(
                login_url=login_url,
                captcha_url=captcha_url,
                username=username,
                password=original_password,
                bookdate=bookdate,
                kssj=kssj,
                jssj=jssj,
                resources_name=resources_name,
                target_time_str=target_time_str,
                num_threads=num_threads or 5,
                resume_job_id=job_id,
            )

    def find_job_ids_by_params(self, *, username: str, bookdate: str, kssj: str, jssj: str, resources_name: str) -> List[str]:
        matches: List[str] = []
        with self._lock:
            for jid, job in self._jobs.items():
                params = job.meta.get("params", {})
                if (
                    params.get("bookdate") == bookdate
                    and params.get("kssj") == kssj
                    and params.get("jssj") == jssj
                    and params.get("resources_name") == resources_name
                ):
                    # username is not in meta params for in-memory; fallback to DB match below
                    matches.append(jid)
        # Cross-check DB to ensure same username
        db_jobs = self.list_scheduled_jobs(username=username)
        db_set = set()
        for j in db_jobs:
            if (
                j.get("bookdate") == bookdate
                and j.get("kssj") == kssj
                and j.get("jssj") == jssj
                and j.get("resources_name") == resources_name
            ):
                db_set.add(j.get("job_id"))
        # intersect if db has entries
        if db_set:
            if matches:
                matches = [jid for jid in matches if jid in db_set]
            else:
                matches = list(db_set)
        return list(dict.fromkeys(matches))

    def day_booking_conflict(self, username: str, bookdate: str) -> str | None:
        """检查同一用户同一天是否已有任务或预约记录，有则返回错误信息。"""
        try:
            for j in self.list_scheduled_jobs(username=username):
                if (
                    j.get("bookdate") == bookdate
                    and j.get("status") in ("scheduled", "running")
                ):
                    return "您当天已有预约任务，每人每天只能预约一次"
        except Exception:
            pass
        try:
            with get_db_pool().get_connection(auto_commit=False) as conn:
                cur = conn.execute(
                    "SELECT 1 FROM local_bookings WHERE username=? AND bookdate=? LIMIT 1",
                    (username, bookdate),
                )
                if cur.fetchone():
                    return "您当天已有预约记录，每人每天只能预约一次"
        except Exception as e:
            logger.error(f"查询本地预约记录失败: {e}")
        return None

    def add_local_booking(self, username: str, bookdate: str, resources_name: str, kssj: str, jssj: str) -> str | None:
        """写入本地预约记录，成功返回 None，失败返回错误码。"""
        import sqlite3
        try:
            with get_db_pool().get_connection() as conn:
                conn.execute(
                    "INSERT INTO local_bookings (username, bookdate, resources_name, kssj, jssj, created_at) VALUES (?,?,?,?,?,?)",
                    (username, bookdate, resources_name, kssj, jssj, time.time()),
                )
        except sqlite3.IntegrityError:
            return "resource_already_booked"
        except Exception as e:
            logger.error(f"插入本地预约记录失败: {e}")
            return "database_error"
        return None

    def remove_local_booking(self, username: str, bookdate: str, resources_name: str, kssj: str, jssj: str) -> None:
        """删除本地预约记录（对外薄封装）。"""
        self._delete_local_booking(
            username=username, bookdate=bookdate,
            resources_name=resources_name, kssj=kssj, jssj=jssj,
        )

    def start_immediate_booking(
        self,
        *,
        login_url: str,
        captcha_url: str,
        username: str,
        password: str,
        bookdate: str,
        kssj: str,
        jssj: str,
        resources_name: str,
        rollback_local_on_fail: bool = False,
    ) -> str:
        cancel_event = threading.Event()

        # 先注册获取 job_id
        meta = {
            "type": "immediate",
            "created_at": time.time(),
            "username": username,
            "params": {
                "username": username,
                "bookdate": bookdate,
                "kssj": kssj,
                "jssj": jssj,
                "resources_name": resources_name,
            },
        }

        def run():
            final_status = None

            def _set(state):
                nonlocal final_status
                final_status = state
                self._safe_update_status(job_id, state)

            try:
                if cancel_event.is_set():
                    _set(JobState.CANCELLED)
                    return
                tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
                if not tokens or not tokens.get("access_token"):
                    _set(JobState.FAILED)
                    return
                if cancel_event.is_set():
                    _set(JobState.CANCELLED)
                    return
                access_token = tokens["access_token"]
                id_token = tokens.get("id_token", "")
                # 限制：同一用户同一天只能预约一次
                try:
                    if list_appointments_for_account(access_token, bookdate, id_token=id_token):
                        _set(JobState.SKIPPED)
                        return
                except Exception:
                    pass
                result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj, id_token=id_token)
                if not result:
                    _set(JobState.FAILED)
                    return
                resource_id, time_id, open_captcha_verify = result
                if cancel_event.is_set():
                    _set(JobState.CANCELLED)
                    return
                user_info = resolve_user_info(access_token, id_token=id_token)
                # 获取滑块验证码（如果需要）
                captcha_id, captcha_code = "", ""
                if open_captcha_verify == "1":
                    logger.info("资源需要滑块验证码，自动处理")
                    captcha_id, captcha_code = _get_slide_captcha(access_token)
                resp = make_appointment(
                    access_token, time_id, resource_id, bookdate, kssj, jssj,
                    id_token=id_token, captcha_id=captcha_id, captcha_code=captcha_code,
                    user_info=user_info, allow_retry=False, timeout_seconds=8,
                )
                ok = isinstance(resp, dict) and str(resp.get("code", "")).lower() in ("0", "success")
                if ok:
                    _set(JobState.DONE)
                else:
                    logger.info("[任务 %s] 即时预约未成功 kind=%s", job_id[:8], _classify_upstream_response(resp))
                    _set(JobState.FAILED)
            finally:
                # 未成功的即时任务回滚本地占位记录（成功则保留为有效记录）
                if rollback_local_on_fail and final_status in (JobState.FAILED, JobState.SKIPPED, JobState.CANCELLED):
                    try:
                        self._delete_local_booking(
                            username=username, bookdate=bookdate,
                            resources_name=resources_name, kssj=kssj, jssj=jssj,
                        )
                    except Exception as e:
                        logger.warning(f"即时任务本地记录清理失败: {e}")
                # 任务完成后从内存移除
                with self._lock:
                    self._jobs.pop(job_id, None)

        th = threading.Thread(target=run, daemon=True)
        job_id = self._register(th, cancel_event, meta)

        # 持久化即时任务到数据库
        self._persist_job_row(
            job_id,
            login_url=login_url,
            captcha_url=captcha_url,
            username=username,
            password=password,
            bookdate=bookdate,
            kssj=kssj,
            jssj=jssj,
            resources_name=resources_name,
            target_time_str="",  # 即时任务无目标时间
            num_threads=1,
            status="running",
        )

        th.start()
        return job_id

    def start_scheduled_booking(
        self,
        *,
        login_url: str,
        captcha_url: str,
        username: str,
        password: str,
        bookdate: str,
        kssj: str,
        jssj: str,
        resources_name: str,
        target_time_str: str,
        num_threads: int = 5,
        resume_job_id: str | None = None,
    ) -> str:
        # 校验并限制线程数（业务层 clamp）
        num_threads = max(1, min(5, num_threads))

        # 去重：恢复场景不进行去重检查，直接使用原 job_id 恢复
        if resume_job_id is None:
            # 1) 先查DB（包含跨进程持久化）
            try:
                with self._db_pool.get_connection(auto_commit=False) as conn:
                    cur = conn.execute(
                        "SELECT job_id FROM scheduled_jobs WHERE username=? AND bookdate=? AND kssj=? AND jssj=? AND resources_name=? AND status IN ('scheduled','running')",
                        (username, bookdate, kssj, jssj, resources_name),
                    )
                    row = cur.fetchone()
                if row and row[0]:
                    return row[0]
            except Exception:
                pass

        # 2) 再查内存（进程内）
        with self._lock:
            for jid, job in self._jobs.items():
                params = job.meta.get("params", {})
                if (
                    job.meta.get("username") == username
                    and params.get("bookdate") == bookdate
                    and params.get("kssj") == kssj
                    and params.get("jssj") == jssj
                    and params.get("resources_name") == resources_name
                ):
                    return jid

        cancel_event = threading.Event()

        def run():
            # ========== 计算目标时刻；长等待到预取窗口 ==========
            target_time = get_target_datetime_from_network(target_time_str, bookdate)
            clock = ClockSync()

            if not _wait_until(target_time, PREFETCH_WINDOW_SEC, cancel_event):
                self._cleanup_job(job_id, JobState.CANCELLED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                return

            # 进入关键窗口：测「网络-本地」时钟偏移，此后等待不再发任何 HTTP
            clock.sync(samples=3)

            # ========== 登录（含 JWT exp 预检，杜绝 T-0 触发重新登录）==========
            tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
            if not tokens or not tokens.get("access_token"):
                self._cleanup_job(job_id, JobState.FAILED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                return
            access_token = tokens["access_token"]
            id_token = tokens.get("id_token", "")

            exp_epoch = token_exp_epoch(access_token)
            if exp_epoch is not None and exp_epoch < target_time.timestamp() + TOKEN_EXP_BUFFER_SEC:
                logger.info("[任务 %s] token exp=%d 临近/早于目标时刻，提前刷新", job_id[:8], int(exp_epoch))
                refreshed = refresh_token_for_user(username)
                if refreshed and refreshed.get("access_token"):
                    tokens = refreshed
                    access_token = tokens["access_token"]
                    id_token = tokens.get("id_token", "")
                elif exp_epoch <= time.time():
                    logger.error("[任务 %s] token 已过期且刷新失败", job_id[:8])
                    self._cleanup_job(job_id, JobState.FAILED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                    return

            # 预热连接池：预取阶段的查询全部走同一 Session，T-0 直接复用
            session = _shared_session()

            # ========== 一天一约去重检查（复用 session）==========
            try:
                if list_appointments_for_account(access_token, bookdate, id_token=id_token, session=session):
                    self._cleanup_job(job_id, JobState.SKIPPED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                    return
            except Exception:
                pass

            # ========== 预取资源/时段 ID 与用户信息（省掉 T-0 的 RTT）==========
            result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj, id_token=id_token, session=session)
            if not result:
                self._cleanup_job(job_id, JobState.FAILED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                return
            resource_id, time_id, open_captcha_verify = result

            user_info = resolve_user_info(access_token, id_token=id_token)

            self._safe_update_status(job_id, JobState.RUNNING)

            # ========== 验证码预取池：一次性凭证，每枪一份，失败自动重试 ==========
            shots = max(1, min(num_threads, MAX_UPSTREAM_BURST))
            captcha_pool: List[Tuple[str, str]] = []
            need_captcha = open_captcha_verify == "1"
            if need_captcha:
                # 池构建最晚到 T-35s：留出对齐/发射的余量，不再临阵解新码
                pool_deadline = target_time.timestamp() - CAPTCHA_HARD_STOP_BEFORE_SEC
                logger.info("[任务 %s] 资源需要滑块验证码：构建预取池（目标 %d 份）", job_id[:8], shots)
                while len(captcha_pool) < shots and time.time() < pool_deadline:
                    if cancel_event.is_set():
                        break
                    try:
                        creds = solve_and_verify_slide_captcha(access_token)
                    except Exception as e:
                        logger.warning("[任务 %s] 滑块验证码求解异常: %s", job_id[:8], e)
                        creds = None
                    if creds:
                        captcha_pool.append(creds)
                        logger.info("[任务 %s] 验证码预取进度 %d/%d", job_id[:8], len(captcha_pool), shots)
                    else:
                        time.sleep(0.2)  # 单次成功率约70%，快速重试
                if len(captcha_pool) < shots:
                    logger.warning("[任务 %s] 验证码池未攒满 %d/%d 份", job_id[:8], len(captcha_pool), shots)

            # ========== 发射线程：本地钟对齐 T-0、各带一份凭证、成功即停 ==========
            barrier = threading.Barrier(shots)
            success_event = threading.Event()
            results_lock = threading.Lock()
            results: List[Dict[str, Any]] = []

            def worker(tid: int):
                creds = captcha_pool[tid] if tid < len(captcha_pool) else ("", "")
                if need_captcha and not creds[0]:
                    logger.warning("[任务 %s] 线程%d 无验证码凭证，仍将尝试提交", job_id[:8], tid)

                # 关键窗口：用校准后的本地钟等待，零 HTTP
                if not _wait_until(target_time, 0, cancel_event, now_fn=clock.now):
                    if tid == 0:
                        self._cleanup_job(job_id, JobState.CANCELLED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                    return

                try:
                    barrier.wait(timeout=20.0)
                except threading.BrokenBarrierError:
                    logger.warning("[任务 %s] 线程%d barrier 超时", job_id[:8], tid)
                    with results_lock:
                        results.append({"tid": tid, "ok": False, "kind": "barrier_timeout"})
                    if tid == 0:
                        self._cleanup_job(job_id, JobState.FAILED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                    return

                # 任一枪命中即收工，避免多余请求消耗频控额度
                if success_event.is_set():
                    with results_lock:
                        results.append({"tid": tid, "ok": False, "kind": "skipped_after_success"})
                    return

                resp = make_appointment(
                    access_token, time_id, resource_id, bookdate, kssj, jssj,
                    id_token=id_token,
                    captcha_id=creds[0], captcha_code=creds[1],
                    user_info=user_info,
                    session=session,
                    allow_retry=False,
                    timeout_seconds=RUSH_REQUEST_TIMEOUT_SEC,
                )
                ok = isinstance(resp, dict) and str(resp.get("code", "")).lower() in ("0", "success")
                kind = _classify_upstream_response(resp)
                logger.info("[任务 %s] 线程%d 提交结果 ok=%s kind=%s", job_id[:8], tid, ok, kind)
                if ok:
                    success_event.set()
                with results_lock:
                    results.append({"tid": tid, "ok": ok, "kind": kind})

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(shots)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # ========== 汇总 ==========
            success_count = sum(1 for r in results if r.get("ok"))
            kinds = [r.get("kind") for r in results]
            if any(k == "banned" for k in kinds):
                logger.warning(
                    "[任务 %s] 触发上游频控封禁（3 分钟）。如需补约请等解禁后另行处理",
                    job_id[:8],
                )
            if success_count > 0:
                self._safe_update_status(job_id, JobState.DONE)
            else:
                self._safe_update_status(job_id, JobState.FAILED)

            with self._lock:
                self._jobs.pop(job_id, None)

        th = threading.Thread(target=run, daemon=True)
        meta = {
            "type": "scheduled",
            "created_at": time.time(),
            "params": {
                "username": username,
                "bookdate": bookdate, "kssj": kssj, "jssj": jssj,
                "resources_name": resources_name, "target_time_str": target_time_str,
                "num_threads": num_threads,
            },
            "username": username,
        }
        if resume_job_id:
            job_id = resume_job_id
            with self._lock:
                self._jobs[job_id] = BookingJob(th, cancel_event, meta)
        else:
            job_id = self._register(th, cancel_event, meta)
            try:
                self._persist_job_row(job_id, login_url=login_url, captcha_url=captcha_url, username=username, password=password, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name, target_time_str=target_time_str, num_threads=num_threads, status="scheduled")
            except Exception:
                pass
        th.start()
        return job_id


# 单例管理器（可在其他模块导入使用）
booking_manager = BookingManager()


def book_badminton_slot(
    *,
    login_url: str,
    captcha_url: str,
    username: str,
    password: str,
    bookdate: str,
    kssj: str,
    jssj: str,
    resources_name: str,
) -> Dict[str, Any]:
    """Login, locate resource/time, and place a booking once.

    Returns the response JSON from the booking API or an error dict.
    """
    # 生成 job_id 用于追踪
    job_id = uuid.uuid4().hex
    created_at = time.time()

    tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
    if not tokens or not tokens.get("access_token"):
        # 登录失败也记录
        _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, "", 1, "failed", created_at)
        return {"ok": False, "error": "login_failed"}

    access_token = tokens["access_token"]
    id_token = tokens.get("id_token", "")

    # 限制：同一用户同一天只能预约一次
    try:
        my_edges = list_appointments_for_account(access_token, bookdate, id_token=id_token)
        if my_edges:
            _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, "", 1, "skipped", created_at)
            return {"ok": False, "error": "user_already_booked_today"}
    except Exception:
        pass

    result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj, id_token=id_token)
    if not result:
        logger.warning("fetch_resource_time_id 返回 None: bookdate=%s, resources_name=%s, kssj=%s, jssj=%s", bookdate, resources_name, kssj, jssj)
        _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, "", 1, "failed", created_at)
        return {"ok": False, "error": "resource_or_time_not_found"}

    resource_id, time_id, open_captcha_verify = result
    logger.info("获取到资源: resource_id=%s, time_id=%s, open_captcha_verify=%s", resource_id[:20] if resource_id else "", time_id[:20] if time_id else "", open_captcha_verify)

    # 即时预约前置校验：时段容量 + 滑块验证码
    captcha_id = ""
    captcha_code = ""
    if open_captcha_verify == "1":
        logger.info("资源需要滑块验证码，自动处理")
        captcha_id, captcha_code = _get_slide_captcha(access_token)
        if not captcha_id:
            logger.error("滑块验证码处理失败")
            _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, "", 1, "failed", created_at)
            return {"ok": False, "error": "captcha_verify_failed"}

    capacity_result = check_resource_time_slot_capacity(
        access_token, resource_id, [time_id], bookdate, kssj, jssj, id_token=id_token
    )
    if capacity_result and capacity_result.get("code") != "0":
        logger.warning("时段容量检查失败: %s", capacity_result)
        _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, "", 1, "failed", created_at)
        return {"ok": False, "error": "capacity_check_failed", "detail": capacity_result}

    resp_json = make_appointment(access_token, time_id, resource_id, bookdate, kssj, jssj, id_token=id_token, captcha_id=captcha_id, captcha_code=captcha_code, allow_retry=False, timeout_seconds=8)

    # 判断预约是否成功
    logger.info("make_appointment 返回: %s", resp_json)
    if resp_json and isinstance(resp_json, dict):
        code = resp_json.get("code", "")
        status = "done" if (code == "success" or code == "0") else "failed"
        if status == "failed":
            logger.warning("预约失败: code=%s, messages=%s", code, resp_json.get("messages"))
    else:
        status = "failed"
        logger.warning("make_appointment 返回无效响应: %s", resp_json)

    _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, "", 1, status, created_at)

    return success_response(resp_json)


def _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status, created_at):
    """保存任务记录到数据库（密码混淆存储）"""
    try:
        with get_db_pool().get_connection() as conn:
            conn.execute(
                """INSERT INTO scheduled_jobs
                   (job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job_id, login_url, captcha_url, username, obfuscate_password(password), bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status, created_at),
            )
    except Exception as e:
        logger.warning(f"写入任务记录失败: {e}")


