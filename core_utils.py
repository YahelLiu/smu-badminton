"""
核心工具模块 - 统一的错误处理和数据库管理

设计理念：
1. 自定义异常类 - 区分不同的错误类型，便于上层处理
2. 统一错误处理装饰器 - 减少重复的 try-except 代码
3. 线程安全的数据库连接池 - 解决并发问题
4. 结构化日志 - 便于问题追踪
"""

import logging
import os
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


# ============= 全局数据库连接池单例 =============

_db_pool_instance: Optional['DatabasePool'] = None
_db_pool_lock = threading.Lock()


def get_db_pool(db_path: str = None) -> 'DatabasePool':
    """
    获取全局数据库连接池单例

    Args:
        db_path: 数据库路径，默认为 data/data.db

    Returns:
        DatabasePool 实例
    """
    global _db_pool_instance
    with _db_pool_lock:
        if _db_pool_instance is None:
            if db_path is None:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                db_path = os.path.join(base_dir, "data", "data.db")
            # 确保目录存在
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            _db_pool_instance = DatabasePool(db_path)
        return _db_pool_instance


def init_db_tables():
    """初始化所有数据库表"""
    pool = get_db_pool()
    with pool.get_connection() as conn:
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

    logger.info("数据库表初始化完成")


def close_db_pool():
    """关闭全局数据库连接池"""
    global _db_pool_instance
    with _db_pool_lock:
        if _db_pool_instance:
            _db_pool_instance.close_all()
            _db_pool_instance = None
            logger.info("全局数据库连接池已关闭")


# ============= 密码混淆工具 =============

import base64

def obfuscate_password(password: str) -> str:
    """
    混淆密码（可逆）
    用于存储时保护密码，不是真正的加密，但能防止明文泄露

    Args:
        password: 原始密码

    Returns:
        混淆后的字符串
    """
    if not password:
        return ""
    key = os.getenv("SECRET_KEY", "smu-badminton-default-key")
    # XOR + base64
    encoded = ''.join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(password))
    return base64.b64encode(encoded.encode()).decode()


def deobfuscate_password(obfuscated: str) -> str:
    """
    还原混淆的密码

    Args:
        obfuscated: 混淆后的字符串

    Returns:
        原始密码
    """
    if not obfuscated:
        return ""
    key = os.getenv("SECRET_KEY", "smu-badminton-default-key")
    try:
        decoded = base64.b64decode(obfuscated.encode()).decode()
        return ''.join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(decoded))
    except Exception:
        # 如果解密失败，可能是明文密码（兼容旧数据）
        return obfuscated


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

# 3. 使用全局数据库连接池
from core_utils import get_db_pool, init_db_tables

# 启动时初始化表
init_db_tables()

# 使用连接池
pool = get_db_pool()
with pool.get_connection() as conn:
    conn.execute("INSERT INTO bookings VALUES (?, ?)", (user_id, date))

# 4. 使用重试装饰器
@retry_on_error(max_retries=3, delay=1.0, exceptions=(sqlite3.OperationalError,))
def save_booking(data):
    # 如果数据库锁定，会自动重试
    pass
"""
