"""
FastAPI 应用主模块。

包含应用创建、生命周期管理、中间件配置和路由挂载。
"""
from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio
import os
import logging

from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response, ORJSONResponse
from starlette.staticfiles import StaticFiles

from .server_models import MetricsMiddleware, RateLimitMiddleware, _locks_cleanup
from .cas_manager import booking_manager
from .core_utils import init_db_tables, close_db_pool
from .config import JOB_RETENTION_SEC, BASE_DIR, UVICORN_RELOAD, TRUSTED_PROXIES

# 导入路由模块
from .routes_auth import router as auth_router
from .routes_booking import router as booking_router
from .routes_jobs import router as jobs_router
from .routes_config import router as config_router

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


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

    _lock_cleanup_task = asyncio.create_task(_locks_cleanup())
    _jobs_cleanup_task = asyncio.create_task(_jobs_cleanup())
    _lbookings_cleanup_task = asyncio.create_task(_stale_local_bookings_cleanup())
    try:
        yield
    finally:
        _lock_cleanup_task.cancel()
        _jobs_cleanup_task.cancel()
        _lbookings_cleanup_task.cancel()
        # 关闭数据库连接池
        close_db_pool()


async def _stale_local_bookings_cleanup():
    """周期清理已过场的本地预约记录（原 GET /local_bookings 内联逻辑，移到后台）。"""
    import time as _time
    from datetime import datetime, timezone, timedelta

    from .core_utils import get_db_pool

    beijing_tz = timezone(timedelta(hours=8))
    while True:
        try:
            await asyncio.sleep(600)  # 每10分钟清理一次
            now_dt = datetime.now(beijing_tz)
            with get_db_pool().get_connection() as conn:
                cur = conn.execute("SELECT id, bookdate, jssj FROM local_bookings")
                to_delete = []
                for row_id, bookdate, jssj in cur.fetchall():
                    try:
                        end_dt = datetime.strptime(f"{bookdate} {jssj}", "%Y-%m-%d %H:%M").replace(tzinfo=beijing_tz)
                        if end_dt < now_dt:
                            to_delete.append((row_id,))
                    except ValueError:
                        continue
                if to_delete:
                    conn.executemany("DELETE FROM local_bookings WHERE id = ?", to_delete)
                    logger.info(f"清理了 {len(to_delete)} 条过期预约记录")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"过期预约记录清理失败: {e}")


async def _jobs_cleanup():
    """定期清理历史任务记录。"""
    import time as _time
    from .core_utils import get_db_pool

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

app = FastAPI(title="羽毛球预约接口", version="1.0.0", lifespan=lifespan, default_response_class=ORJSONResponse)

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

# 挂载路由
app.include_router(auth_router)
app.include_router(booking_router)
app.include_router(jobs_router)
app.include_router(config_router)


# ============= 基础路由 =============

@app.get("/")
async def index():
    """首页。"""
    html_path = os.path.join(BASE_DIR, "templates", "index.html")
    if not os.path.exists(html_path):
        logger.error(f"HTML 文件不存在: {html_path}")
        return {"error": "未找到 index.html", "path": html_path, "base_dir": BASE_DIR}
    return FileResponse(html_path, media_type="text/html; charset=utf-8")


@app.get("/favicon.ico")
async def favicon():
    """favicon。"""
    return Response(status_code=204, media_type="image/x-icon")


@app.get("/health")
async def health():
    """健康检查。"""
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
    # 注意：待处理任务由 lifespan 统一加载，这里不重复调用，
    # 否则 reload 模式下父进程会额外跑一份抢票线程。
    import uvicorn
    uvicorn.run(
        "smu_badminton.server_fastapi:app",
        host="0.0.0.0",
        port=int(os.getenv("SERVER_PORT", "5002")),
        reload=UVICORN_RELOAD,
        proxy_headers=True,
        # 使用可信代理列表，而非 "*"
        forwarded_allow_ips=",".join(TRUSTED_PROXIES) if TRUSTED_PROXIES else ""
    )
