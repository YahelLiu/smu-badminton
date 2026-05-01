# SMU 羽毛球预约系统 - 安全性改进计划

## 背景

当前系统存在安全问题：
1. **密码明文存储** - `scheduled_jobs` 表存储明文密码
2. **硬编码授权用户** - `jobs.js` 中写死授权学号
3. **CORS 配置宽松** - 生产环境允许 `*` 来源

### 约束条件

- Token 有效期不确定，不适合持久化到数据库
- 用户不能每次操作都输入密码
- 密码需要存储（用于定时任务），但要加密/混淆

---

## 改进方案

### 1. 密码混淆存储

**方案：** 使用对称加密（AES）或简单混淆存储密码

**修改文件：** `cas_manager.py`、`core_utils.py`

**实现：**
```python
# core_utils.py - 新增
import base64
import os

# 简单混淆方案（可逆）
def obfuscate_password(password: str) -> str:
    """混淆密码"""
    key = os.getenv("SECRET_KEY", "default-secret-key")
    # 简单 XOR + base64
    encoded = ''.join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(password))
    return base64.b64encode(encoded.encode()).decode()

def deobfuscate_password(obfuscated: str) -> str:
    """还原密码"""
    key = os.getenv("SECRET_KEY", "default-secret-key")
    decoded = base64.b64decode(obfuscated.encode()).decode()
    return ''.join(chr(ord(c) ^ ord(key[i % len(key)])) for i, c in enumerate(decoded))
```

**修改 `cas_manager.py`：**
- `_persist_job_row()` - 存储前调用 `obfuscate_password()`
- `load_pending_jobs()` - 读取后调用 `deobfuscate_password()`
- `book_badminton_slot()` - 存储前混淆密码

---

### 2. 授权用户配置化

**方案：** 移除前端硬编码，改为后端配置

**修改文件：** `static/js/jobs.js`、`server_fastapi.py`

**实现：**

**后端新增 API：**
```python
# server_fastapi.py
AUTHORIZED_USERS = os.getenv("AUTHORIZED_USERS", "202540510004").split(",")

@app.get("/api/auth/check")
async def check_auth(username: str):
    """检查用户是否有管理员权限"""
    return {"ok": True, "authorized": username in AUTHORIZED_USERS}
```

**前端修改：**
```javascript
// jobs.js - 替换硬编码检查
async function checkAuthorization(username) {
    const resp = await fetch(`/api/auth/check?username=${username}`);
    const data = await resp.json();
    return data.authorized;
}
```

---

### 3. CORS 配置收紧

**方案：** 使用环境变量配置允许的来源

**修改文件：** `server_fastapi.py`

**实现：**
```python
# 开发环境默认值，生产环境通过环境变量配置
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:5001,http://127.0.0.1:5001").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

---

## 关键文件修改清单

| 文件 | 修改内容 |
|------|----------|
| `core_utils.py` | 新增 `obfuscate_password()` 和 `deobfuscate_password()` |
| `cas_manager.py` | 存储前混淆密码，读取后还原密码 |
| `server_fastapi.py` | 新增 `/api/auth/check` API，CORS 配置使用环境变量 |
| `static/js/jobs.js` | 移除硬编码授权用户，改为调用 API 检查 |

---

## 验证方案

1. **密码混淆测试：**
   ```python
   from core_utils import obfuscate_password, deobfuscate_password
   original = "my_password"
   obfuscated = obfuscate_password(original)
   assert deobfuscate_password(obfuscated) == original
   ```

2. **定时任务测试：**
   - 创建定时任务，检查数据库密码字段是否已混淆
   - 重启服务，检查定时任务能否正常恢复执行

3. **授权检查测试：**
   - 访问 `/jobs` 页面，非授权用户应被拒绝
   - 授权用户应能正常访问

---

## 注意事项

- **SECRET_KEY** 建议在生产环境设置随机值
- **AUTHORIZED_USERS** 可配置多个用户，用逗号分隔
- 混淆方案不是真正的加密，但能防止明文泄露
