"""
配置相关路由模块。

包含：配置读取、配置更新。
"""
import importlib
import logging
import os

from fastapi import APIRouter
from dotenv import load_dotenv

from .server_models import UpdateConfigRequest
from .config import AUTHORIZED_USERS, BASE_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["config"])


# ============= 路由定义 =============

@router.get("/config")
async def get_config():
    """获取前端配置。"""
    from . import config as config_module
    importlib.reload(config_module)
    return {"ok": True, "data": config_module.get_frontend_config()}


@router.post("/config/update")
async def update_config(req: UpdateConfigRequest):
    """更新配置（需要授权用户）。

    权限检查：current_username 必须在 AUTHORIZED_USERS 中。
    """
    # 权限检查
    if req.current_username not in AUTHORIZED_USERS:
        logger.warning("权限拒绝：用户试图更新配置")
        return {"ok": False, "error": "permission_denied", "message": "无权更新配置"}

    try:
        env_path = os.path.join(BASE_DIR, ".env")

        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
        else:
            lines = []

        found = False
        new_lines = []
        for line in lines:
            if line.strip().startswith('CAS_LOGIN_URL='):
                new_lines.append(f'CAS_LOGIN_URL={req.login_url}\n')
                found = True
            else:
                new_lines.append(line)

        if not found:
            if new_lines and not new_lines[-1].endswith('\n'):
                new_lines.append('\n')
            new_lines.append(f'CAS_LOGIN_URL={req.login_url}\n')

        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(new_lines)

        logger.info("配置已更新: CAS_LOGIN_URL")

        try:
            from . import config as config_module
            load_dotenv(env_path, override=True)
            importlib.reload(config_module)
            logger.info("配置已重载，立即生效")
            return {"ok": True, "data": {"message": "配置已保存并自动重载，立即生效", "path": env_path, "reloaded": True}}
        except Exception as reload_err:
            logger.warning(f"重载配置失败: {reload_err}，需要重启服务器")
            return {"ok": True, "data": {"message": "配置已保存，但重载失败，请重启服务器", "path": env_path, "reloaded": False}}
    except Exception as e:
        logger.error(f"更新配置失败: {e}")
        return {"ok": False, "error": f"更新失败: {str(e)}"}
