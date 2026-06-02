# API 文档

## 概述

SMU Badminton 提供 RESTful API，所有接口返回 JSON 格式数据。基础路径为 `/api`，健康检查端点为 `/health`。

### 通用响应格式

```json
{
  "ok": true,
  "data": { ... }
}
```

失败时：

```json
{
  "ok": false,
  "error": "错误描述"
}
```

### 限流策略

| 接口 | 限流窗口 | 最大请求数 |
|------|---------|-----------|
| `/api/book`, `/api/book/schedule`, `/api/availability` | 10 秒 | 30 次 |
| `/api/jobs/*` | 60 秒 | 300 次 |

超过限流返回 HTTP 429：

```json
{
  "ok": false,
  "error": "请求过于频繁",
  "hint": "请求频率超限，请降低轮询频率。",
  "limit": 30,
  "window_sec": 10,
  "path": "/api/book",
  "ip": "x.x.x.x"
}
```

---

## 认证相关

### POST /api/captcha

获取验证码图片，用于手动输入验证码场景。

**请求体：**

```json
{
  "login_url": "",       // 可选，CAS 登录 URL，默认使用配置值
  "captcha_url": ""      // 可选，验证码 URL，默认使用配置值
}
```

**成功响应：**

```json
{
  "ok": true,
  "data": {
    "captcha_image": "data:image/png;base64,iVBORw0KGgo...",  // base64 编码的验证码图片
    "session_id": "a1b2c3d4-..."                               // 验证码会话 ID，登录时需传回
  }
}
```

**说明：**
- 验证码会话有效期 300 秒（5 分钟）
- 同一会话的验证码图片可用于后续 `/api/login` 请求
- 过期会话会被自动清理

---

### POST /api/login

用户登录。不提供 `captcha_code` 时自动使用 OCR 识别验证码；提供 `captcha_code` 时使用手动输入的验证码。

**请求体：**

```json
{
  "login_url": "",              // 可选，CAS 登录 URL
  "captcha_url": "",            // 可选，验证码 URL
  "username": "202540510004",   // 必填，学号
  "password": "your_password",  // 必填，密码
  "captcha_code": "42",         // 可选，手动输入的验证码答案
  "session_id": "a1b2c3d4-..."  // 可选，验证码会话 ID（复用已有 session）
}
```

**成功响应：**

```json
{
  "ok": true,
  "data": {
    "access_token": "eyJhbGciOiJSUzI1NiIs..."
  }
}
```

**失败响应：**

```json
{
  "ok": false,
  "error": "验证码错误",
  "error_type": "captcha_error",
  "need_manual_captcha": true
}
```

**error_type 取值：**

| 值 | 说明 |
|----|------|
| `captcha_error` | 验证码识别错误，建议切换手动输入 |
| `password_error` | 用户名或密码错误 |
| `network_error` | 网络连接失败 |
| `unknown_error` | 未知错误 |

**说明：**
- 登录成功后 Token 自动缓存（TTL 900 秒）
- 登录成功后账号密码自动保存（密码混淆存储）
- 当 `need_manual_captcha` 为 `true` 时，前端应展示手动验证码输入界面

---

### POST /api/logout

登出并清除 Token 缓存。

**请求体：**

```json
{
  "username": "202540510004"   // 必填，要登出的用户名
}
```

**响应：**

```json
{
  "ok": true
}
```

---

### GET /api/auth/check

检查用户是否在授权列表中。

**查询参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `username` | string | 是 | 用户名 |

**响应：**

```json
{
  "ok": true,
  "authorized": true
}
```

---

## 预约相关

### POST /api/availability

查询场地可用性。使用公共缓存机制：场地时间槽数据所有用户共享（60s TTL），仅 `bookedByMe` 按用户单独查询。

**请求体：**

```json
{
  "token": "eyJhbGciOiJSUzI1NiIs...",   // 必填，访问令牌
  "bookdate": "2025-12-18"               // 必填，预约日期，格式 YYYY-MM-DD
}
```

**成功响应：**

```json
{
  "ok": true,
  "data": {
    "list": [
      {
        "resources_name": "羽毛球13号场地",
        "kssj": "18:00",
        "jssj": "19:00",
        "available": true,
        "bookedByMe": false
      }
    ]
  }
}
```

**响应头：**

| Header | 说明 |
|--------|------|
| `X-Avail-Cache` | 缓存状态：`HIT-PUBLIC`（公共缓存命中）或 `MISS`（缓存未命中） |
| `X-Avail-TotalMs` | 查询总耗时（毫秒） |
| `X-Avail-ListLen` | 返回的时间槽数量 |

**缓存行为：**
- **HIT-PUBLIC**：仅查询预约记录（1 个请求），合并 `bookedByMe`
- **MISS**：完整查询（资源列表 + 时间槽 + 预约记录），然后存入公共缓存

---

### POST /api/book

立即预约场地。

**请求体：**

```json
{
  "login_url": "https://...",             // 必填，CAS 登录 URL
  "captcha_url": "https://...",           // 必填，验证码 URL
  "username": "202540510004",             // 必填，学号
  "password": "your_password",            // 必填，密码
  "bookdate": "2025-12-18",              // 必填，预约日期 YYYY-MM-DD
  "kssj": "18:00",                       // 必填，开始时间 HH:MM
  "jssj": "19:00",                       // 必填，结束时间 HH:MM
  "resources_name": "羽毛球13号场地"       // 必填，资源名称
}
```

**成功响应：**

```json
{
  "ok": true,
  "data": { ... }
}
```

**失败响应：**

```json
{
  "ok": false,
  "error": "resource_already_booked"
}
```

**error 取值：**

| 值 | 说明 |
|----|------|
| `resource_locked_processing` | 资源正在被其他请求处理中 |
| `resource_already_booked` | 资源已被预约（UNIQUE 约束冲突） |
| `login_failed` | 登录失败 |
| `resource_or_time_not_found` | 未找到对应的资源或时间段 |
| `user_already_booked_today` | 该用户当天已有预约记录 |
| `captcha_verify_required` | 资源需要预约验证码，无法自动预约 |
| `capacity_check_failed` | 时段容量检查失败 |
| `database_error` | 数据库操作失败 |

**说明：**
- 同一用户同一天只能预约一次（数据库 UNIQUE 约束 `(bookdate, resources_name, kssj, jssj)` 保障）
- 预约失败时自动回滚本地预约记录
- 内置前置校验：检查验证码要求和时段容量

---

### POST /api/book/schedule

定时预约场地。在指定目标时间点自动发起多线程并发预约。

**请求体：**

```json
{
  "login_url": "https://...",             // 必填，CAS 登录 URL
  "captcha_url": "https://...",           // 必填，验证码 URL
  "username": "202540510004",             // 必填，学号
  "password": "your_password",            // 必填，密码
  "bookdate": "2025-12-18",              // 必填，预约日期 YYYY-MM-DD
  "kssj": "18:00",                       // 必填，开始时间 HH:MM
  "jssj": "19:00",                       // 必填，结束时间 HH:MM
  "resources_name": "羽毛球13号场地",      // 必填，资源名称
  "target_time_str": "21:00:00",         // 必填，目标开抢时间 HH:MM:SS
  "num_threads": 5,                       // 可选，并发线程数 1-5，默认 5
  "run_async": true                       // 可选，是否后台异步执行，默认 false
}
```

**同步模式响应（run_async=false）：**

```json
{
  "ok": true,
  "data": {
    "threads": 5,
    "results": [
      { "thread": 1, "response": { "code": "success" } },
      { "thread": 2, "response": { "code": "success" } }
    ]
  }
}
```

**异步模式响应（run_async=true）：**

```json
{
  "ok": true,
  "data": {
    "scheduled": true,
    "job_id": "a1b2c3d4e5f6..."
  }
}
```

**说明：**
- 目标时间 = `bookdate - 7 天 + target_time_str`
- 异步模式立即返回 `job_id`，可通过 `/api/jobs` 查询任务状态
- 启动失败时自动回滚本地预约记录

---

## 任务相关

### GET /api/jobs

列出所有预约任务。

**响应：**

```json
{
  "ok": true,
  "data": {
    "jobs": [
      {
        "job_id": "a1b2c3d4...",
        "alive": true,
        "type": "scheduled",
        "created_at": 1734000000.0,
        "username": "202540510004",
        "params": { ... }
      }
    ],
    "db_jobs": [
      {
        "job_id": "a1b2c3d4...",
        "username": "202540510004",
        "bookdate": "2025-12-18",
        "kssj": "18:00",
        "jssj": "19:00",
        "resources_name": "羽毛球13号场地",
        "target_time_str": "21:00:00",
        "num_threads": 5,
        "status": "scheduled",
        "created_at": 1734000000.0,
        "type": "scheduled"
      }
    ]
  }
}
```

---

### POST /api/jobs/immediate

创建即时预约任务（异步执行）。

**请求体：**

与 [POST /api/book](#post-api-book) 的请求体格式相同。

**响应：**

```json
{
  "ok": true,
  "data": {
    "job_id": "a1b2c3d4e5f6..."
  }
}
```

---

### POST /api/jobs/scheduled

创建定时预约任务（异步执行）。

**请求体：**

与 [POST /api/book/schedule](#post-api-book-schedule) 的请求体格式相同。

**响应：**

```json
{
  "ok": true,
  "data": {
    "job_id": "a1b2c3d4e5f6..."
  }
}
```

---

### GET /api/schedule/{job_id}

获取指定任务的状态。

**路径参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `job_id` | string | 任务 ID |

**响应：**

```json
{
  "ok": true,
  "data": {
    "status": "running",
    "created_at": 1734000000.0,
    "logs": ["任务已创建", "已到达预登录窗口"],
    "result": null
  }
}
```

---

### POST /api/jobs/{job_id}/stop

停止指定任务。需要权限验证：只有任务的所有者才能停止。

**路径参数：**

| 参数 | 类型 | 说明 |
|------|------|------|
| `job_id` | string | 任务 ID |

**请求体：**

```json
{
  "current_username": "202540510004"   // 必填，当前操作用户名
}
```

**响应：**

```json
{
  "ok": true,
  "data": {
    "job_id": "a1b2c3d4e5f6..."
  }
}
```

**错误响应：**

```json
{
  "ok": false,
  "data": {
    "error": "permission_denied",
    "message": "无权停止他人的任务"
  }
}
```

---

### POST /api/jobs/stop_by_params

根据预约参数停止任务。需要权限验证：`current_username` 必须与 `username` 匹配。

**请求体：**

```json
{
  "username": "202540510004",             // 必填，任务所属用户名
  "bookdate": "2025-12-18",             // 必填，预约日期
  "kssj": "18:00",                       // 必填，开始时间
  "jssj": "19:00",                       // 必填，结束时间
  "resources_name": "羽毛球13号场地",      // 必填，资源名称
  "current_username": "202540510004"      // 必填，当前操作用户名
}
```

**响应：**

```json
{
  "ok": true,
  "data": {
    "stopped": 2
  }
}
```

---

## 本地预约记录

### POST /api/local_bookings

保存本地预约记录。

**请求体：**

```json
{
  "username": "202540510004",            // 必填，用户名
  "bookdate": "2025-12-18",            // 必填，预约日期 YYYY-MM-DD
  "resources_name": "羽毛球13号场地",     // 必填，资源名称
  "kssj": "18:00",                      // 必填，开始时间 HH:MM
  "jssj": "19:00"                       // 必填，结束时间 HH:MM
}
```

**响应：**

```json
{
  "ok": true
}
```

---

### GET /api/local_bookings

列出本地预约记录。

**查询参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `bookdate` | string | 是 | 预约日期 YYYY-MM-DD |
| `limit` | int | 否 | 返回记录数量限制 |
| `offset` | int | 否 | 偏移量，默认 0 |
| `fields` | string | 否 | 返回字段过滤，逗号分隔（如 `username,bookdate`） |
| `clean` | int | 否 | 是否清理过期记录，默认 1 |

**响应：**

```json
{
  "ok": true,
  "data": {
    "list": [
      {
        "username": "202540510004",
        "bookdate": "2025-12-18",
        "resources_name": "羽毛球13号场地",
        "kssj": "18:00",
        "jssj": "19:00",
        "created_at": 1734000000.0
      }
    ]
  }
}
```

**响应头：**

| Header | 说明 |
|--------|------|
| `X-LBookings-CleanMs` | 清理过期记录耗时（毫秒） |
| `X-LBookings-QueryMs` | 查询耗时（毫秒） |
| `X-LBookings-Count` | 返回记录数量 |
| `X-LBookings-Limit` | 限制数量（-1 表示无限制） |
| `X-LBookings-Offset` | 偏移量 |

**说明：**
- `clean=1`（默认）时，会自动清理已过期（结束时间早于当前时间）的预约记录
- `fields` 参数支持过滤返回字段，可选值：`username`, `bookdate`, `resources_name`, `kssj`, `jssj`, `created_at`

---

## 配置相关

### GET /api/config

获取前端所需的配置信息。

**响应：**

```json
{
  "ok": true,
  "data": {
    "login_url": "https://wf.shmtu.edu.cn/yy-sys/pc/home",
    "authorize_url": "https://wf.shmtu.edu.cn/sso/oauth2/authorize?...",
    "captcha_url": "https://cas.shmtu.edu.cn/cas/captcha"
  }
}
```

---

### POST /api/config/update

更新 CAS 登录 URL 配置。需要权限验证：用户必须在 `AUTHORIZED_USERS` 列表中。

**请求体：**

```json
{
  "login_url": "https://cas.shmtu.edu.cn/cas/login?service=...",   // 必填，新的 CAS 登录 URL
  "current_username": "202540510004"                                 // 必填，当前操作用户名
}
```

**响应：**

```json
{
  "ok": true,
  "data": {
    "message": "配置已保存并自动重载，立即生效",
    "path": "/app/.env",
    "reloaded": true
  }
}
```

---

## 监控相关

### GET /api/metrics

获取请求指标统计。

**响应：**

```json
{
  "ok": true,
  "data": {
    "/api/availability": {
      "count": 150,
      "avg_ms": 245.6,
      "max_ms": 1200.3
    },
    "/api/book": {
      "count": 23,
      "avg_ms": 890.1,
      "max_ms": 3500.0
    }
  }
}
```

---

### GET /health

健康检查端点，无需认证。

**响应：**

```json
{
  "ok": true
}
```
