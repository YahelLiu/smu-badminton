import typing as t
import threading
from typing import Any, Dict, List, Optional
from enum import Enum
import time
import uuid
import logging

# 从各模块导入
from .cas_login import login_with_retry
from .token_profile import get_cached_token, cache_token_for_user
from .booking_api import (
    fetch_resource_time_id,
    make_appointment,
    list_appointments_for_account,
    check_resource_time_slot_capacity,
    find_resource_detail,
)
from .http_utils import (
    get_network_time,
    get_target_datetime_from_network,
)

# 导入核心工具模块
from .core_utils import (
    get_db_pool,
    DatabaseError,
    handle_errors,
    db_operation,
    BookingError,
    success_response,
    obfuscate_password,
    deobfuscate_password,
)

# 配置日志
logger = logging.getLogger(__name__)


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

class JobState(str, Enum):
    """任务状态枚举。

    状态转换规则：
    - scheduled -> waiting -> running -> done
    - scheduled -> waiting -> cancelled
    - running -> failed
    - running -> skipped
    """
    SCHEDULED = "scheduled"   # 已创建，等待到达预登录窗口
    WAITING = "waiting"       # 已到达预登录窗口，正在登录/预取
    RUNNING = "running"       # 已到达目标时间，正在执行预约
    DONE = "done"             # 预约成功（终态）
    FAILED = "failed"         # 预约失败（终态）
    SKIPPED = "skipped"       # 已有预约，跳过（终态）
    CANCELLED = "cancelled"   # 已取消（终态）


# 终态集合（不可被覆盖）
TERMINAL_STATES = {JobState.DONE, JobState.FAILED, JobState.SKIPPED, JobState.CANCELLED}

# 允许的状态转换
VALID_TRANSITIONS: Dict[JobState, set] = {
    JobState.SCHEDULED: {JobState.WAITING, JobState.CANCELLED},
    JobState.WAITING: {JobState.RUNNING, JobState.FAILED, JobState.SKIPPED, JobState.CANCELLED},
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

    @db_operation
    def _update_job_row_status(self, job_id: str, status: str):
        """更新任务状态（兼容旧接口，建议使用 _safe_update_status）。"""
        with self._db_pool.get_connection() as conn:
            conn.execute("UPDATE scheduled_jobs SET status=? WHERE job_id=?", (status, job_id))
        logger.debug(f"任务状态已更新: {job_id} -> {status}")

    @db_operation
    def _delete_job_row(self, job_id: str):
        """删除任务记录"""
        with self._db_pool.get_connection() as conn:
            conn.execute("DELETE FROM scheduled_jobs WHERE job_id=?", (job_id,))
        logger.debug(f"任务记录已删除: {job_id}")

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
                "UPDATE scheduled_jobs SET status='cancelled' WHERE username=? AND bookdate=? AND kssj=? AND jssj=? AND resources_name=? AND status IN ('scheduled','waiting','running')",
                (username, bookdate, kssj, jssj, resources_name),
            )
            affected = cur.rowcount if cur.rowcount is not None else 0
            logger.debug(f"按参数标记 cancelled: {affected} 条任务记录")
            return affected

    @handle_errors(default_return=0, log_error=True, error_message="按参数停止任务失败")
    def stop_by_params(self, *, username: str, bookdate: str, kssj: str, jssj: str, resources_name: str) -> int:
        """
        根据预约参数停止任务
        
        流程：
        1. 停止内存中的活跃任务
        2. 将数据库中的对应任务标记为 cancelled（保留历史记录）
        3. 删除本地预约记录（统一删除一次）
        
        Returns:
            总共停止的任务数量
        """
        stopped = 0
        
        # 1. 停止内存中的任务（不让它们删除local_booking，统一在最后删除）
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
        
        # 2. 将数据库中的对应任务标记为 cancelled（仅处理仍在等待/运行中的任务）
        cancelled = self._cancel_scheduled_by_params(
            username=username, 
            bookdate=bookdate, 
            kssj=kssj, 
            jssj=jssj, 
            resources_name=resources_name
        )
        stopped += cancelled
        
        # 3. 删除本地预约记录（统一删除一次）
        self._delete_local_booking(
            username=username, 
            bookdate=bookdate, 
            kssj=kssj, 
            jssj=jssj, 
            resources_name=resources_name
        )
        
        logger.info(f"按参数停止任务完成: {stopped} 个任务, {username} - {bookdate} {kssj}-{jssj}")
        return stopped

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
                "SELECT job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status FROM scheduled_jobs WHERE status IN ('scheduled','waiting','running')"
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
            try:
                if cancel_event.is_set():
                    self._safe_update_status(job_id, JobState.CANCELLED)
                    return
                tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
                if not tokens or not tokens.get("access_token"):
                    self._safe_update_status(job_id, JobState.FAILED)
                    return
                if cancel_event.is_set():
                    self._safe_update_status(job_id, JobState.CANCELLED)
                    return
                access_token = tokens["access_token"]
                id_token = tokens.get("id_token", "")
                # 限制：同一用户同一天只能预约一次
                try:
                    if list_appointments_for_account(access_token, bookdate, id_token=id_token):
                        self._safe_update_status(job_id, JobState.SKIPPED)
                        return
                except Exception:
                    pass
                result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj, id_token=id_token)
                if not result:
                    self._safe_update_status(job_id, JobState.FAILED)
                    return
                resource_id, time_id = result
                if cancel_event.is_set():
                    self._safe_update_status(job_id, JobState.CANCELLED)
                    return
                resp = make_appointment(access_token, time_id, resource_id, bookdate, kssj, jssj, id_token=id_token)
                # 根据预约结果更新状态
                if resp and isinstance(resp, dict):
                    code = resp.get("code", "")
                    if code == "success" or code == "0":
                        self._safe_update_status(job_id, JobState.DONE)
                    else:
                        self._safe_update_status(job_id, JobState.FAILED)
                else:
                    self._safe_update_status(job_id, JobState.FAILED)
            finally:
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
                        "SELECT job_id FROM scheduled_jobs WHERE username=? AND bookdate=? AND kssj=? AND jssj=? AND resources_name=? AND status IN ('scheduled','waiting','running')",
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
            # 等到预登录窗口
            target_time = get_target_datetime_from_network(target_time_str, bookdate)
            prelogin_delta_sec = 5 * 60
            while not cancel_event.is_set():
                now = get_network_time()
                if now:
                    diff_sec = (target_time - now).total_seconds()
                    if diff_sec <= prelogin_delta_sec:
                        break
                    # 分级休眠策略，适应长时间跨天等待
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
                else:
                    time.sleep(1)
            if cancel_event.is_set():
                self._cleanup_job(job_id, JobState.CANCELLED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                return

            # 登录
            tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
            if not tokens or not tokens.get("access_token"):
                self._cleanup_job(job_id, JobState.FAILED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                return
            access_token = tokens["access_token"]

            # 限制：同一用户同一天只能预约一次
            try:
                if list_appointments_for_account(access_token, bookdate, id_token=tokens.get("id_token", "")):
                    self._cleanup_job(job_id, JobState.SKIPPED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                    return
            except Exception:
                pass

            # 预取资源/时间段
            result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj, id_token=tokens.get("id_token", ""))
            if not result:
                self._cleanup_job(job_id, JobState.FAILED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                return
            resource_id, time_id = result

            barrier = threading.Barrier(num_threads)
            results_lock = threading.Lock()
            results: List[Dict[str, Any]] = []

            def worker(tid: int):
                # 等待到目标时间
                target_local = get_target_datetime_from_network(target_time_str, bookdate)
                while not cancel_event.is_set():
                    now = get_network_time()
                    if now:
                        diff = (target_local - now).total_seconds()
                        if diff <= 0:
                            break
                        elif diff > 10:
                            time.sleep(5)
                        elif diff > 1:
                            time.sleep(0.5)
                        else:
                            time.sleep(0.1)
                    else:
                        time.sleep(1)
                if cancel_event.is_set():
                    # 只有第一个 worker 执行清理
                    if tid == 1:
                        self._cleanup_job(job_id, JobState.CANCELLED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                    return

                # 添加 barrier 超时
                try:
                    barrier.wait(timeout=30.0)
                except threading.BrokenBarrierError:
                    logger.warning(f"Worker {tid} barrier 超时，任务可能已失败")
                    with results_lock:
                        results.append({"thread": tid, "response": None, "success": False, "error": "barrier_timeout"})
                    if tid == 1:
                        self._cleanup_job(job_id, JobState.FAILED, username=username, bookdate=bookdate, kssj=kssj, jssj=jssj, resources_name=resources_name)
                    return

                resp = make_appointment(access_token, time_id, resource_id, bookdate, kssj, jssj, id_token=tokens.get("id_token", ""))
                success = resp and isinstance(resp, dict) and resp.get("code") in ("success", "0")
                with results_lock:
                    results.append({"thread": tid, "response": resp, "success": success})
                # 不再在 worker 中更新状态，由主线程统一判断

            threads = [threading.Thread(target=worker, args=(i + 1,)) for i in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # 所有 worker 完成后，统一判断状态
            success_count = sum(1 for r in results if r.get("success"))
            if success_count > 0:
                self._safe_update_status(job_id, JobState.DONE)
            else:
                self._safe_update_status(job_id, JobState.FAILED)

            # 清理内存
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
    import uuid

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
        _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, "", 1, "failed", created_at)
        return {"ok": False, "error": "resource_or_time_not_found"}

    resource_id, time_id = result

    # 即时预约前置校验：检查验证码要求 + 时段容量
    resource_detail = find_resource_detail(access_token, resource_id, id_token=id_token)
    if resource_detail and resource_detail.get("open_captcha_verify") == "1":
        logger.warning("资源需要预约验证码，无法自动预约")
        _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, "", 1, "failed", created_at)
        return {"ok": False, "error": "captcha_verify_required"}

    capacity_result = check_resource_time_slot_capacity(
        access_token, resource_id, [time_id], bookdate, kssj, jssj, id_token=id_token
    )
    if capacity_result and capacity_result.get("code") != "0":
        logger.warning("时段容量检查失败: %s", capacity_result)
        _save_job_record(job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, "", 1, "failed", created_at)
        return {"ok": False, "error": "capacity_check_failed", "detail": capacity_result}

    resp_json = make_appointment(access_token, time_id, resource_id, bookdate, kssj, jssj, id_token=id_token)

    # 判断预约是否成功
    if resp_json and isinstance(resp_json, dict):
        code = resp_json.get("code", "")
        status = "done" if (code == "success" or code == "0") else "failed"
    else:
        status = "failed"

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


def schedule_booking(
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
) -> Dict[str, Any]:
    """Login, prefetch resource/time, and start N threads to book exactly at target time.

    Returns a dict with per-thread results.
    """
    # 1) 先计算目标时间，并在距离目标时间5分钟时再登录刷新token
    target_time = get_target_datetime_from_network(target_time_str, bookdate)
    prelogin_delta_sec = 5 * 60
    while True:
        now = get_network_time()
        if now:
            diff_sec = (target_time - now).total_seconds()
            if diff_sec <= prelogin_delta_sec:
                break
            # 分级休眠策略，适应长时间跨天等待
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
        else:
            time.sleep(1)

    # 2) 到达预登录窗口，进行登录以获取新token
    tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
    if not tokens or not tokens.get("access_token"):
        return {"ok": False, "error": "login_failed"}
    access_token = tokens["access_token"]
    id_token = tokens.get("id_token", "")

    # 限制：同一用户同一天只能预约一次
    try:
        my_edges = list_appointments_for_account(access_token, bookdate, id_token=id_token)
        if my_edges:
            return {"ok": False, "error": "user_already_booked_today"}
    except Exception:
        pass

    # 3) 使用新token预取资源与时间段ID
    result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj, id_token=id_token)
    if not result:
        return {"ok": False, "error": "resource_or_time_not_found"}
    resource_id, time_id = result

    barrier = threading.Barrier(num_threads)
    results: List[Dict[str, Any]] = []
    results_lock = threading.Lock()

    def worker(thread_id: int):
        # wait until target time using network time helpers
        target_time_local = get_target_datetime_from_network(target_time_str, bookdate)
        while True:
            now = get_network_time()
            if now:
                diff_sec = (target_time_local - now).total_seconds()
                if diff_sec <= 0:
                    break
                elif diff_sec > 10:
                    time.sleep(5)
                elif diff_sec > 1:
                    time.sleep(0.5)
                else:
                    time.sleep(0.1)
            else:
                time.sleep(1)

        # synchronize all threads to fire together
        barrier.wait()
        resp = make_appointment(access_token, time_id, resource_id, bookdate, kssj, jssj, id_token=id_token)
        with results_lock:
            results.append({"thread": thread_id, "response": resp})

    threads = [threading.Thread(target=worker, args=(i + 1,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return success_response({"threads": num_threads, "results": results})



