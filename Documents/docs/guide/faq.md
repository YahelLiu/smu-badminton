# 常见问题

## 安装与部署

### Q: 启动时提示 "未找到 index.html"

A: 确保 `templates/` 目录存在且包含 `index.html` 文件。如果是从源码运行，请确认在项目根目录下启动服务。

### Q: 启动时提示 NCNN 模型加载失败

A: 本地 OCR 模式（`OCR_MODE=local`）需要模型文件。请确认 `model/` 目录下包含所有 6 个模型文件（3 组 .param + .bin）。如果不想使用本地推理，可切换为远程模式：

```env
OCR_MODE=http
OCR_HTTP_HOST=127.0.0.1
OCR_HTTP_PORT=21600
```

### Q: Docker 部署时模型文件如何处理

A: 模型文件通过 Docker volume 只读挂载，不打包进镜像。确保宿主机上 `./model/` 目录包含模型文件，docker-compose 会自动挂载。

### Q: 使用默认 SECRET_KEY 的警告可以忽略吗

A: 不建议忽略。默认 `SECRET_KEY` 是公开的，使用它存储的密码可被轻易解码。生产环境务必在 `.env` 中设置自定义密钥：

```env
SECRET_KEY=your-random-secret-key-here
```

## 登录与认证

### Q: 验证码识别经常失败怎么办

A: 验证码识别准确率受图片质量影响。可以尝试：

1. 使用远程 OCR 服务（`OCR_MODE=http`），部署专用的 shmtu-cas-ocr-server
2. 前端切换为手动输入验证码模式
3. 检查验证码 URL 是否正确（`CAS_CAPTCHA_URL` 配置项）

### Q: Token 缓存多久过期

A: 默认 900 秒（15 分钟），可通过 `TOKEN_CACHE_TTL_SEC` 环境变量调整。过期后系统会自动重新登录获取新 Token。

### Q: 登录失败 error_type 含义

| error_type | 原因 | 处理方式 |
|-----------|------|---------|
| `captcha_error` | 验证码识别错误 | 切换手动输入 |
| `password_error` | 用户名或密码错误 | 检查凭据 |
| `network_error` | 网络连接失败 | 检查网络 |
| `unknown_error` | 未知错误 | 查看服务端日志 |

## 预约功能

### Q: 提示 "每人每天只能预约一次"

A: 系统限制同一用户同一天只能有一个预约记录。这是数据库层面的 UNIQUE 约束，包括：
- `local_bookings` 表的 `(bookdate, resources_name, kssj, jssj)` 唯一约束
- 预约前检查当天是否已有预约任务或记录

### Q: 定时预约的目标时间怎么计算

A: 目标时间 = `预约日期 - 7 天 + 目标时间`。例如预约 12 月 18 日的场地，目标时间 21:00:00，则系统将在 12 月 11 日 21:00:00 发起预约。这对应学校系统提前 7 天开放预约的规则。

### Q: 定时预约线程数设置多少合适

A: 线程数范围为 1-5，默认 5。线程数越多并发请求越密集，但受限于服务器处理能力和网络条件。通常 3-5 即可满足抢场需求。

### Q: 服务重启后定时预约任务会丢失吗

A: 不会。所有定时任务都持久化到 SQLite 的 `scheduled_jobs` 表。服务启动时会自动调用 `load_pending_jobs()` 恢复状态为 `scheduled` 或 `running` 的任务。

### Q: 提示 "resource_locked_processing" 是什么意思

A: 表示该资源正在被其他预约请求处理中。这是系统的资源锁机制，防止同一资源被重复预约。等待几秒后重试即可。

## 可用性查询

### Q: 可用性查询响应头中的 X-Avail-Cache 含义

| 值 | 含义 |
|----|------|
| `HIT-PUBLIC` | 公共缓存命中，仅查询了用户自己的预约记录，响应更快 |
| `MISS` | 缓存未命中，执行了完整查询（资源 + 时间槽 + 预约记录） |

公共缓存 TTL 为 60 秒，所有用户共享场地时间槽数据。

### Q: 为什么可用性查询有时快有时慢

A: 首次查询（缓存 MISS）需要请求多个 GraphQL 接口获取完整数据，耗时较长。后续 60 秒内的查询（缓存 HIT-PUBLIC）仅需查询预约记录，速度显著提升。

## API 使用

### Q: 请求返回 429 状态码

A: 表示请求频率超过限流阈值。默认限制：

| 接口 | 限制 |
|------|------|
| 预约/可用性接口 | 10 秒内最多 30 次 |
| 任务接口 | 60 秒内最多 300 次 |

降低请求频率或调整 `RATE_LIMIT_MAX` / `RATE_LIMIT_JOBS_MAX` 环境变量。

### Q: 如何配置可信代理

A: 如果服务部署在反向代理（如 Nginx）后面，需配置可信代理 IP 以正确获取客户端真实 IP：

```env
TRUSTED_PROXIES=127.0.0.1,10.0.0.1
```

未配置时，限流将使用直接连接的 IP，可能导致所有请求被视为同一 IP。

## Docker 部署

### Q: Docker 容器健康检查失败

A: 检查以下几点：
1. 确认服务已正常启动（查看容器日志 `docker logs smu-badminton`）
2. 确认端口映射正确（默认 5000）
3. 确认 `.env` 文件已正确挂载

### Q: Docker 部署后数据会丢失吗

A: 使用 docker-compose 部署时，数据存储在 Docker volume `smu-badminton-data` 中，不会因容器重建而丢失。但直接使用 `docker run` 且未挂载 volume 时，数据会在容器删除时丢失。
