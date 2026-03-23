"""
核心工具模块 - 统一的错误处理和数据库管理

设计理念：
1. 自定义异常类 - 区分不同的错误类型，便于上层处理
2. 统一错误处理装饰器 - 减少重复的 try-except 代码
3. 线程安全的数据库连接池 - 解决并发问题
4. 结构化日志 - 便于问题追踪
"""

import logging
import sqlite3
import threading
import time
from functools import wraps
from typing import Optional, Callable, Any, TypeVar
from contextlib import contextmanager

# 配置日志
logger = logging.getLogger(__name__)


# ============= 自定义异常类 =============

class BookingError(Exception):
    """预约系统基础异常"""
    def __init__(self, message: str, code: str = "UNKNOWN_ERROR", details: Optional[dict] = None):
        self.message = message
        self.code = code
        self.details = details or {}
        super().__init__(self.message)


class DatabaseError(BookingError):
    """数据库操作异常"""
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, "DATABASE_ERROR", details)


class LoginError(BookingError):
    """登录失败异常"""
    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(message, "LOGIN_ERROR", details)


class ResourceLockedError(BookingError):
    """资源被锁定异常"""
    def __init__(self, message: str = "资源正在被其他请求处理", details: Optional[dict] = None):
        super().__init__(message, "RESOURCE_LOCKED", details)


class ResourceAlreadyBookedError(BookingError):
    """资源已被预约异常"""
    def __init__(self, message: str = "该时间段已被预约", details: Optional[dict] = None):
        super().__init__(message, "RESOURCE_ALREADY_BOOKED", details)


class JobNotFoundError(BookingError):
    """任务不存在异常"""
    def __init__(self, job_id: str):
        super().__init__(f"任务不存在: {job_id}", "JOB_NOT_FOUND", {"job_id": job_id})


class PermissionDeniedError(BookingError):
    """权限不足异常"""
    def __init__(self, message: str = "权限不足", details: Optional[dict] = None):
        super().__init__(message, "PERMISSION_DENIED", details)


# ============= 错误处理装饰器 =============

T = TypeVar('T')


def handle_errors(
    default_return: Any = None,
    log_error: bool = True,
    raise_on_error: bool = False,
    error_message: str = "操作失败"
):
    """
    统一错误处理装饰器
    
    Args:
        default_return: 发生错误时的默认返回值
        log_error: 是否记录错误日志
        raise_on_error: 是否重新抛出异常
        error_message: 错误消息前缀
    
    用法示例:
        @handle_errors(default_return={}, log_error=True)
        def risky_operation():
            # 可能抛出异常的代码
            pass
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except BookingError as e:
                # 业务异常，记录详细信息
                if log_error:
                    logger.error(
                        f"{error_message} - {func.__name__}: {e.message}",
                        extra={"code": e.code, "details": e.details}
                    )
                if raise_on_error:
                    raise
                return default_return
            except Exception as e:
                # 未预期的异常
                if log_error:
                    logger.exception(f"{error_message} - {func.__name__}: {str(e)}")
                if raise_on_error:
                    raise
                return default_return
        return wrapper
    return decorator


def db_operation(func: Callable) -> Callable:
    """
    数据库操作装饰器 - 专门处理数据库相关错误
    
    用法示例:
        @db_operation
        def save_booking(conn, data):
            conn.execute("INSERT INTO ...", data)
            conn.commit()
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sqlite3.IntegrityError as e:
            # UNIQUE 约束失败等
            logger.warning(f"数据库完整性错误 - {func.__name__}: {str(e)}")
            raise DatabaseError(f"数据冲突: {str(e)}", {"type": "integrity_error"})
        except sqlite3.OperationalError as e:
            # 数据库锁定、表不存在等
            logger.error(f"数据库操作错误 - {func.__name__}: {str(e)}")
            raise DatabaseError(f"数据库操作失败: {str(e)}", {"type": "operational_error"})
        except sqlite3.Error as e:
            # 其他数据库错误
            logger.error(f"数据库未知错误 - {func.__name__}: {str(e)}")
            raise DatabaseError(f"数据库错误: {str(e)}", {"type": "unknown_db_error"})
    return wrapper


# ============= 线程安全的数据库连接池 =============

class DatabasePool:
    """
    线程安全的 SQLite 连接池
    
    设计说明：
    - 使用 threading.local() 为每个线程维护独立的连接
    - 避免跨线程共享连接导致的 SQLite 错误
    - 支持上下文管理器，自动提交/回滚
    """
    
    def __init__(self, db_path: str, max_retries: int = 3):
        self.db_path = db_path
        self.max_retries = max_retries
        self._local = threading.local()  # 每个线程独立的存储
        self._lock = threading.Lock()
        logger.info(f"数据库连接池已创建: {db_path}")
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取当前线程的数据库连接"""
        # 检查当前线程是否已有连接
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = self._create_connection()
        
        # 验证连接是否有效
        try:
            self._local.connection.execute("SELECT 1")
        except (sqlite3.ProgrammingError, sqlite3.OperationalError):
            # 连接已失效，重新创建
            self._local.connection = self._create_connection()
        
        return self._local.connection
    
    def _create_connection(self) -> sqlite3.Connection:
        """创建新的数据库连接"""
        retry_count = 0
        last_error = None
        
        while retry_count < self.max_retries:
            try:
                conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10.0)
                # 优化配置
                conn.execute("PRAGMA journal_mode=WAL;")  # Write-Ahead Logging 模式
                conn.execute("PRAGMA synchronous=NORMAL;")  # 平衡性能和安全性
                conn.execute("PRAGMA busy_timeout=5000;")  # 5秒超时
                conn.execute("PRAGMA cache_size=-64000;")  # 64MB 缓存
                
                logger.debug(f"数据库连接已创建 (线程: {threading.current_thread().name})")
                return conn
            except sqlite3.OperationalError as e:
                last_error = e
                retry_count += 1
                if retry_count < self.max_retries:
                    time.sleep(0.1 * retry_count)  # 指数退避
                    logger.warning(f"数据库连接失败，重试 {retry_count}/{self.max_retries}")
        
        raise DatabaseError(
            f"无法连接到数据库，已重试 {self.max_retries} 次",
            {"path": self.db_path, "error": str(last_error)}
        )
    
    @contextmanager
    def get_connection(self, auto_commit: bool = True):
        """
        获取数据库连接的上下文管理器
        
        用法示例:
            with db_pool.get_connection() as conn:
                conn.execute("INSERT INTO ...", data)
                # 自动提交或回滚
        """
        conn = self._get_connection()
        try:
            yield conn
            if auto_commit:
                conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"数据库事务回滚: {str(e)}")
            raise
    
    def execute(self, sql: str, params: tuple = (), auto_commit: bool = True) -> sqlite3.Cursor:
        """
        执行 SQL 语句（简化接口）
        
        Args:
            sql: SQL 语句
            params: 参数
            auto_commit: 是否自动提交
        """
        with self.get_connection(auto_commit=auto_commit) as conn:
            return conn.execute(sql, params)
    
    def close_all(self):
        """关闭所有连接（应用关闭时调用）"""
        if hasattr(self._local, 'connection') and self._local.connection:
            try:
                self._local.connection.close()
                self._local.connection = None
                logger.info("数据库连接已关闭")
            except Exception as e:
                logger.warning(f"关闭数据库连接失败: {e}")


# ============= 统一的响应格式 =============

def success_response(data: Any = None, message: str = "操作成功") -> dict:
    """成功响应"""
    return {
        "ok": True,
        "message": message,
        "data": data
    }


def error_response(error: BookingError) -> dict:
    """错误响应"""
    return {
        "ok": False,
        "error": error.code,
        "message": error.message,
        "details": error.details
    }


def error_response_from_exception(e: Exception) -> dict:
    """从异常创建错误响应"""
    if isinstance(e, BookingError):
        return error_response(e)
    else:
        # 未知异常
        return {
            "ok": False,
            "error": "UNKNOWN_ERROR",
            "message": str(e),
            "details": {}
        }


# ============= 重试装饰器 =============

def retry_on_error(max_retries: int = 3, delay: float = 0.5, exceptions: tuple = (Exception,)):
    """
    失败重试装饰器
    
    Args:
        max_retries: 最大重试次数
        delay: 重试延迟（秒）
        exceptions: 需要重试的异常类型
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_retries:
                        logger.warning(
                            f"{func.__name__} 失败，重试 {attempt + 1}/{max_retries}: {str(e)}"
                        )
                        time.sleep(delay * (attempt + 1))  # 指数退避
                    else:
                        logger.error(f"{func.__name__} 失败，已达最大重试次数")
            
            raise last_exception
        return wrapper
    return decorator


# ============= 使用示例（注释） =============

"""
# 1. 使用自定义异常
def check_permission(user_id: str, resource_id: str):
    if not has_permission(user_id, resource_id):
        raise PermissionDeniedError("无权访问该资源", {"user_id": user_id, "resource_id": resource_id})

# 2. 使用错误处理装饰器
@handle_errors(default_return={}, log_error=True)
def get_user_bookings(user_id: str):
    # 这里的异常会被自动捕获和记录
    return fetch_from_db(user_id)

# 3. 使用数据库连接池
db_pool = DatabasePool("data/data.db")

with db_pool.get_connection() as conn:
    conn.execute("INSERT INTO bookings VALUES (?, ?)", (user_id, date))

# 4. 使用重试装饰器
@retry_on_error(max_retries=3, delay=1.0, exceptions=(sqlite3.OperationalError,))
def save_booking(data):
    # 如果数据库锁定，会自动重试
    pass
"""
