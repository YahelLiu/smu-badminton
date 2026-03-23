import typing as t

from cas_login_requests import (
    login_with_retry,
    get_token_cached,
    fetch_resource_time_id,
    make_appointment,
    get_network_time,
    get_target_datetime_from_network,
    list_appointments_for_account,
)
import threading
from typing import Any, Dict, Tuple, List
import time
import uuid
import os
import sqlite3
import logging

# 导入核心工具模块
from core_utils import (
    DatabasePool,
    DatabaseError,
    handle_errors,
    db_operation,
    retry_on_error,
    BookingError,
    JobNotFoundError,
)

# 配置日志
logger = logging.getLogger(__name__)


class BookingJob:
    def __init__(self, thread: threading.Thread, cancel_event: threading.Event, meta: Dict[str, Any]):
        self.thread = thread
        self.cancel_event = cancel_event
        self.meta = meta  # {type: 'immediate'|'scheduled', created_at, params}


class BookingManager:
    """
    预约任务管理器：负责创建、跟踪、终止任务
    
    改进说明：
    - 使用线程安全的数据库连接池（DatabasePool）
    - 使用统一的错误处理装饰器
    - 结构化日志记录
    """
    def __init__(self) -> None:
        self._jobs: Dict[str, BookingJob] = {}
        self._lock = threading.Lock()
        
        # 确保数据库目录存在
        script_dir = os.path.dirname(os.path.abspath(__file__))
        db_dir = os.path.join(script_dir, "data")
        os.makedirs(db_dir, exist_ok=True)
        
        # 使用新的数据库连接池（线程安全）
        self._db_path = os.path.join(db_dir, "data.db")
        self._db_pool = DatabasePool(self._db_path, max_retries=3)
        
        logger.info(f"预约管理器初始化完成，数据库路径: {self._db_path}")

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
        
        # 先从DB取参数，以便后续清理本地预约记录
        job_row = None
        if delete_local_booking:
            job_row = self._get_job_row(job_id)  # 装饰器会处理异常
        
        if job:
            job.cancel_event.set()
            # 使用较短的超时避免阻塞API响应
            job.thread.join(timeout=0.5)
            # 从内存移除
            with self._lock:
                self._jobs.pop(job_id, None)
        
        # 标记取消（使用装饰器处理异常）
        self._update_job_row_status(job_id, "cancelled")
        
        # 清除本地预约记录
        if delete_local_booking and job_row:
            self._delete_local_booking(
                username=job_row.get("username", ""),
                bookdate=job_row.get("bookdate", ""),
                kssj=job_row.get("kssj", ""),
                jssj=job_row.get("jssj", ""),
                resources_name=job_row.get("resources_name", ""),
            )
        
        # 删除任务持久化记录
        self._delete_job_row(job_id)
        
        logger.info(f"任务已停止: {job_id}")
        return bool(job)

    def _register(self, thread: threading.Thread, cancel_event: threading.Event, meta: Dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = BookingJob(thread, cancel_event, meta)
        return job_id

    # ---- 数据库操作方法（使用连接池和装饰器） ----
    
    @db_operation
    def _persist_job_row(self, job_id: str, *, login_url: str, captcha_url: str, username: str, password: str, bookdate: str, kssj: str, jssj: str, resources_name: str, target_time_str: str, num_threads: int, status: str):
        """持久化任务到数据库"""
        with self._db_pool.get_connection() as conn:
            conn.execute(
                "INSERT INTO scheduled_jobs (job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, login_url, captcha_url, username, password, bookdate, kssj, jssj, resources_name, target_time_str, num_threads, status, time.time()),
            )
        logger.debug(f"任务已持久化: {job_id}")

    @db_operation
    def _update_job_row_status(self, job_id: str, status: str):
        """更新任务状态"""
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

    @db_operation
    def _delete_local_booking(self, *, username: str, bookdate: str, kssj: str, jssj: str, resources_name: str):
        """删除本地预约记录"""
        with self._db_pool.get_connection() as conn:
            conn.execute(
                "DELETE FROM local_bookings WHERE username=? AND bookdate=? AND kssj=? AND jssj=? AND resources_name=?",
                (username, bookdate, kssj, jssj, resources_name),
            )
        logger.debug(f"本地预约记录已删除: {username} - {bookdate} {kssj}-{jssj}")

    @db_operation
    def _delete_scheduled_by_params(self, *, username: str, bookdate: str, kssj: str, jssj: str, resources_name: str) -> int:
        """根据参数删除定时任务"""
        with self._db_pool.get_connection() as conn:
            cur = conn.execute(
                "SELECT job_id FROM scheduled_jobs WHERE username=? AND bookdate=? AND kssj=? AND jssj=? AND resources_name=?",
                (username, bookdate, kssj, jssj, resources_name),
            )
            rows = [r[0] for r in cur.fetchall()]
            if rows:
                conn.executemany("DELETE FROM scheduled_jobs WHERE job_id=?", [(jid,) for jid in rows])
            logger.debug(f"删除了 {len(rows)} 条定时任务记录")
            return len(rows)

    @handle_errors(default_return=0, log_error=True, error_message="按参数停止任务失败")
    def stop_by_params(self, *, username: str, bookdate: str, kssj: str, jssj: str, resources_name: str) -> int:
        """
        根据预约参数停止任务
        
        流程：
        1. 停止内存中的活跃任务
        2. 删除数据库中的定时任务记录
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
        
        # 2. 删除数据库中的定时任务记录
        deleted = self._delete_scheduled_by_params(
            username=username, 
            bookdate=bookdate, 
            kssj=kssj, 
            jssj=jssj, 
            resources_name=resources_name
        )
        stopped += deleted
        
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
        """获取定时任务列表"""
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
            # Recreate scheduled job using stored params
            self.start_scheduled_booking(
                login_url=login_url,
                captcha_url=captcha_url,
                username=username,
                password=password or "",
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

        def run():
            if cancel_event.is_set():
                return
            tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
            if not tokens or not tokens.get("access_token"):
                return
            if cancel_event.is_set():
                return
            access_token = tokens["access_token"]
            # 限制：同一用户同一天只能预约一次
            try:
                if list_appointments_for_account(access_token, bookdate):
                    return
            except Exception:
                pass
            result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj)
            if not result:
                return
            resource_id, time_id = result
            if cancel_event.is_set():
                return
            make_appointment(access_token, time_id, resource_id, bookdate, kssj, jssj)

        th = threading.Thread(target=run, daemon=True)
        meta = {
            "type": "immediate",
            "created_at": time.time(),
            "params": {"bookdate": bookdate, "kssj": kssj, "jssj": jssj, "resources_name": resources_name},
        }
        job_id = self._register(th, cancel_event, meta)
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
        # 去重：恢复场景不进行去重检查，直接使用原 job_id 恢复
        if resume_job_id is None:
            # 1) 先查DB（包含跨进程持久化）
            try:
                with self._db_lock:
                    conn = self._db_connect()
                    try:
                        cur = conn.execute(
                            "SELECT job_id FROM scheduled_jobs WHERE username=? AND bookdate=? AND kssj=? AND jssj=? AND resources_name=? AND status IN ('scheduled','waiting','running')",
                            (username, bookdate, kssj, jssj, resources_name),
                        )
                        row = cur.fetchone()
                    finally:
                        conn.close()
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
                try:
                    self._update_job_row_status(job_id, "cancelled")
                    # 删除local_bookings记录
                    self._delete_local_booking(
                        username=username,
                        bookdate=bookdate,
                        kssj=kssj,
                        jssj=jssj,
                        resources_name=resources_name
                    )
                except Exception:
                    pass
                return

            # 登录
            tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
            if not tokens or not tokens.get("access_token"):
                try:
                    self._update_job_row_status(job_id, "failed")
                    # 删除local_bookings记录
                    self._delete_local_booking(
                        username=username,
                        bookdate=bookdate,
                        kssj=kssj,
                        jssj=jssj,
                        resources_name=resources_name
                    )
                except Exception:
                    pass
                return
            access_token = tokens["access_token"]

            # 限制：同一用户同一天只能预约一次
            try:
                if list_appointments_for_account(access_token, bookdate):
                    try:
                        self._update_job_row_status(job_id, "skipped")
                        # 删除local_bookings记录
                        self._delete_local_booking(
                            username=username,
                            bookdate=bookdate,
                            kssj=kssj,
                            jssj=jssj,
                            resources_name=resources_name
                        )
                    except Exception:
                        pass
                    return
            except Exception:
                pass

            # 预取资源/时间段
            result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj)
            if not result:
                try:
                    self._update_job_row_status(job_id, "failed")
                    # 删除local_bookings记录
                    self._delete_local_booking(
                        username=username,
                        bookdate=bookdate,
                        kssj=kssj,
                        jssj=jssj,
                        resources_name=resources_name
                    )
                except Exception:
                    pass
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
                    try:
                        self._update_job_row_status(job_id, "cancelled")
                        # 删除local_bookings记录
                        self._delete_local_booking(
                            username=username,
                            bookdate=bookdate,
                            kssj=kssj,
                            jssj=jssj,
                            resources_name=resources_name
                        )
                    except Exception:
                        pass
                    return
                barrier.wait()
                resp = make_appointment(access_token, time_id, resource_id, bookdate, kssj, jssj)
                with results_lock:
                    results.append({"thread": tid, "response": resp})
                try:
                    self._update_job_row_status(job_id, "done")
                except Exception:
                    pass

            threads = [threading.Thread(target=worker, args=(i + 1,)) for i in range(num_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        th = threading.Thread(target=run, daemon=True)
        meta = {
            "type": "scheduled",
            "created_at": time.time(),
            "params": {
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
    tokens = get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900)
    if not tokens or not tokens.get("access_token"):
        return {"ok": False, "error": "login_failed"}

    access_token = tokens["access_token"]

    # 限制：同一用户同一天只能预约一次
    try:
        my_edges = list_appointments_for_account(access_token, bookdate)
        if my_edges:
            return {"ok": False, "error": "user_already_booked_today"}
    except Exception:
        pass

    result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj)
    if not result:
        return {"ok": False, "error": "resource_or_time_not_found"}

    resource_id, time_id = result
    resp_json = make_appointment(access_token, time_id, resource_id, bookdate, kssj, jssj)
    return {"ok": True, "data": resp_json}


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

    # 限制：同一用户同一天只能预约一次
    try:
        my_edges = list_appointments_for_account(access_token, bookdate)
        if my_edges:
            return {"ok": False, "error": "user_already_booked_today"}
    except Exception:
        pass

    # 3) 使用新token预取资源与时间段ID
    result = fetch_resource_time_id(access_token, bookdate, resources_name, kssj, jssj)
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
        resp = make_appointment(access_token, time_id, resource_id, bookdate, kssj, jssj)
        with results_lock:
            results.append({"thread": thread_id, "response": resp})

    threads = [threading.Thread(target=worker, args=(i + 1,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return {"ok": True, "data": {"threads": num_threads, "results": results}}



