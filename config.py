"""
配置模块 - 从 .env 文件加载配置
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件
env_path = Path(__file__).parent / '.env'
load_dotenv(env_path)

# CAS 登录配置
CAS_ORIGIN = os.getenv('CAS_ORIGIN', 'https://cas.shmtu.edu.cn')
CAS_CAPTCHA_URL = os.getenv('CAS_CAPTCHA_URL', 'https://cas.shmtu.edu.cn/cas/captcha')
CAS_LOGIN_URL = os.getenv('CAS_LOGIN_URL', '')

# 微服务平台配置
WF_ORIGIN = os.getenv('WF_ORIGIN', 'https://wf.shmtu.edu.cn')
WF_API_URL = os.getenv('WF_API_URL', 'https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys')

# OAuth 配置
OAUTH_CLIENT_ID = os.getenv('OAUTH_CLIENT_ID', 'kwxKbMKq3Nafw2mApFZz')

# 羽毛球场地资源类型ID
BADMINTON_TYPE_ID = os.getenv('BADMINTON_TYPE_ID', '93c2a115-5c73-4e30-bb6a-dfcc5404e46f')

# 导出所有配置的便捷函数
def get_config():
    """获取所有配置的字典形式"""
    return {
        'cas_origin': CAS_ORIGIN,
        'cas_captcha_url': CAS_CAPTCHA_URL,
        'cas_login_url': CAS_LOGIN_URL,
        'wf_origin': WF_ORIGIN,
        'wf_api_url': WF_API_URL,
        'oauth_client_id': OAUTH_CLIENT_ID,
        'badminton_type_id': BADMINTON_TYPE_ID,
    }

def get_frontend_config():
    """获取前端需要的配置（用于注入到HTML）"""
    return {
        'login_url': CAS_LOGIN_URL,
        'captcha_url': CAS_CAPTCHA_URL,
    }
