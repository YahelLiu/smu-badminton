# 配置参数

## 环境变量

所有配置通过 `.env` 文件或环境变量设置。复制 `.env.example` 为 `.env` 后根据需要修改。

### CAS 认证配置

> CAS 登录页已由 `cas.shmtu.edu.cn` 迁至 `sso.shmtu.edu.cn`，验证码接口 `/cas/captcha`
> 也随之迁移并改为返回 JSON `{image, token, expiresAt}`。代码会按登录页 host 推导同源
> 验证码 URL，`.env` 中残留旧 `cas.` 地址也不会跨 host 抓取失败。

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `CAS_ORIGIN` | `https://sso.shmtu.edu.cn` | CAS 认证平台地址 |
| `CAS_CAPTCHA_URL` | `https://sso.shmtu.edu.cn/cas/captcha` | 验证码接口 URL（返回 JSON：image base64 + token） |
| `CAS_LOGIN_URL` | WF 首页地址 | CAS 登录入口 URL（默认通过 WF 首页发起 OAuth2 授权再重定向到 CAS） |

### 微服务平台配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `WF_ORIGIN` | `https://wf.shmtu.edu.cn` | 微服务平台地址 |
| `WF_API_URL` | `https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys` | GraphQL API 地址 |
| `WF_HOME_URL` | `{WF_ORIGIN}/yy-sys/pc/home` | 微服务平台首页 URL |
| `WF_SSO_AUTHORIZE_PATH` | `/sso/oauth2/authorize` | OAuth2 授权路径 |

### OAuth 配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `OAUTH_CLIENT_ID` | `kwxKbMKq3Nafw2mApFZz` | OAuth2 客户端标识 |

### 资源配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `BADMINTON_TYPE_ID` | `93c2a115-5c73-4e30-bb6a-dfcc5404e46f` | 羽毛球场地资源类型 ID |

### 验证码识别

验证码 OCR 由 [ddddocr](https://github.com/sml2h3/ddddocr) 在本地整图识别完成，无需额外模型文件，
也不存在 `OCR_MODE` / `OCR_HTTP_*` / `OCR_TCP_*` 等远程 OCR 配置（这些选项已随 NCNN 管线一并移除）。
首次调用时 ddddocr 会惰性加载内置 onnx 模型，无需任何运行参数。若要改用手动输入验证码，
前端直接传 `captcha_code` 即可，与配置无关。

### 安全配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SECRET_KEY` | `smu-badminton-default-key` | 密码混淆密钥（**生产环境务必修改**） |
| `AUTHORIZED_USERS` | `202540510004` | 授权用户列表，逗号分隔 |
| `TRUSTED_PROXIES` | （空） | 可信代理 IP 列表，逗号分隔（用于 X-Forwarded-For 验证） |

> **安全警告**：使用默认 `SECRET_KEY` 时系统会发出警告。虽然混淆不是加密，但自定义密钥可以增加密码暴露的难度。

### 限流配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `RATE_LIMIT_MAX` | `30` | 默认限流窗口内最大请求数 |
| `RATE_LIMIT_WINDOW` | `10` | 默认限流窗口（秒） |
| `RATE_LIMIT_JOBS_MAX` | `300` | 任务接口限流窗口内最大请求数 |
| `RATE_LIMIT_JOBS_WINDOW` | `60` | 任务接口限流窗口（秒） |

### 运行时配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SERVER_PORT` | `5002`（开发）/ `5000`（Docker） | 服务监听端口 |
| `BOOKING_DEBUG` | `0` | 调试模式，设为 `1` 开启详细预约日志 |
| `UVICORN_RELOAD` | `0` | Uvicorn 自动重载，开发时设为 `1` |
| `TOKEN_CACHE_TTL_SEC` | `900` | Token 缓存有效期（秒） |
| `TOKEN_PROFILE_TTL_SEC` | `3600` | 用户 Profile 缓存有效期（秒） |
| `JOB_RETENTION_SEC` | `3600` | 已完成任务保留时间（秒） |

### 数据路径配置

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DATA_DIR` | `/app/data`（Docker）/ 项目目录 `data/`（本地） | 数据存储目录 |

### 用户信息默认值

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `DEFAULT_DEPT_CODE` | （空） | 默认部门代码 |
| `DEFAULT_DEPT_NAME` | （空） | 默认部门名称 |
| `DEFAULT_DEPT_NAME_EN` | （空） | 默认部门英文名称 |
| `DEFAULT_USER_EMAIL` | （空） | 默认用户邮箱 |
| `DEFAULT_USER_PHONE` | （空） | 默认用户电话 |

## .env.example 完整内容

```env
# CAS 登录配置（已迁移至 sso.shmtu.edu.cn）
CAS_ORIGIN=https://sso.shmtu.edu.cn
CAS_CAPTCHA_URL=https://sso.shmtu.edu.cn/cas/captcha
CAS_LOGIN_URL=https://sso.shmtu.edu.cn/cas/login?service=...

# 微服务平台配置
WF_ORIGIN=https://wf.shmtu.edu.cn
WF_API_URL=https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys

# OAuth 配置
OAUTH_CLIENT_ID=kwxKbMKq3Nafw2mApFZz

# 羽毛球场地资源类型ID
BADMINTON_TYPE_ID=93c2a115-5c73-4e30-bb6a-dfcc5404e46f
```

## 配置热更新

`CAS_LOGIN_URL` 支持通过 API 热更新，无需重启服务：

```
POST /api/config/update
```

更新后配置自动重载，立即生效。如果重载失败，需要手动重启服务。
