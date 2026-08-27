# SMU Badminton

上海海事大学羽毛球场预约系统，提供 Web 界面与 REST API，支持 CAS 统一认证、即时预约、定时抢场、验证码 OCR、本地任务持久化与基础监控能力。

仓库内已有一套 VitePress 文档，`README.md` 的目标是让你先把服务跑起来，再快速找到更细的说明。

## 功能概览

- CAS 统一认证登录，支持自动 OCR 或手动输入验证码，token 支持静默续期（免反复重登）
- 即时预约：异步任务化提交，格子实时反馈「抢位中」，结果自动轮询刷新
- 定时预约抢场流水线：时钟偏移校准（T-0 前零对时请求）、验证码预取池、≤2 发并发上限、成功即停
- 取消即撤销：对接学校官方接口真实取消预约（非仅删除本地记录）
- 场地可用性查询，60 秒公共缓存 + single-flight 合并在途查询
- SQLite 持久化存储本地预约记录和任务状态
- Web 页面、REST API、健康检查与基础指标接口

## 技术栈

- Python >= 3.11
- FastAPI + Uvicorn
- SQLite（WAL 模式）
- Requests + BeautifulSoup + lxml
- ddddocr（算术验证码整图识别，纯本地推理，无需额外服务或模型文件）
- pycryptodome（captchaCode AES 加密复现）
- VitePress（项目文档站）

## 目录结构

```text
src/smu_badminton/      FastAPI 服务与核心业务逻辑
  routes_auth.py        认证路由（验证码/登录/登出/续期）
  routes_booking.py     预约路由（即时/定时/可用性/本地记录）
  routes_jobs.py        任务路由（任务列表/停止/metrics）
  routes_config.py      配置路由（读取/热更新）
  cas_login.py          CAS 认证流程（含验证码 JSON + token 同源抓取）
  cas_ocr.py            ddddocr 算术验证码识别
  cas_manager.py        BookingManager：任务编排与持久化
  booking_api.py        资源/时段/预约的 GraphQL 调用
  token_profile.py      token 与用户账号凭据缓存
  core_utils.py         线程安全 SQLite 连接池、异常、密码混淆
  config.py             .env 配置加载
templates/              Web 页面模板（index.html / jobs.html）
static/                 前端静态资源
tests/                  单元测试与集成测试
scripts/                上游行为实测脚本（验证码复用性/频控阈值）
Documents/docs/         VitePress 文档
.env.example            示例环境变量
docker-compose.yml      Docker Compose 启动配置
Dockerfile              镜像构建文件
```

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/YahelLiu/smu-badminton.git
cd smu-badminton
```

### 2. 安装依赖

推荐使用可编辑安装：

```bash
pip install -e .
```

开发测试依赖：

```bash
pip install -e ".[dev]"
```

也可以使用：

```bash
pip install -r requirements.txt
```

> 若使用 [uv](https://github.com/astral-sh/uv) 管理环境，用 `uv sync` 同步运行依赖，`uv sync --extra dev` 同步含测试依赖。

> ddddocr 在部分国内镜像源（如清华 tuna）可能未收录，安装失败时改用官方源：
> `pip install ddddocr -i https://pypi.org/simple`

### 3. 配置环境变量

复制示例配置：

```bash
cp .env.example .env
```

最少建议确认这些配置：

```env
CAS_ORIGIN=https://sso.shmtu.edu.cn
CAS_CAPTCHA_URL=https://sso.shmtu.edu.cn/cas/captcha
WF_ORIGIN=https://wf.shmtu.edu.cn
WF_API_URL=https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys
OAUTH_CLIENT_ID=kwxKbMKq3Nafw2mApFZz
BADMINTON_TYPE_ID=93c2a115-5c73-4e30-bb6a-dfcc5404e46f
```

生产或多人使用时，额外建议设置：

```env
SECRET_KEY=replace-this-with-your-own-secret
AUTHORIZED_USERS=202540510004
TRUSTED_PROXIES=127.0.0.1
```

> CAS 登录页已由 `cas.shmtu.edu.cn` 迁至 `sso.shmtu.edu.cn`，验证码接口 `/cas/captcha`
> 也随之迁移并改为返回 JSON `{image, token, expiresAt}`。代码会自动按登录页 host
> 推导同源验证码 URL，因此 `.env` 中旧 `cas.` 地址残留也不会跨 host 抓取失败。

### 4. 启动服务

本地开发默认端口为 `5002`：

```bash
python -m smu_badminton.server_fastapi
```

也可以直接使用 `uvicorn`：

```bash
uvicorn smu_badminton.server_fastapi:app --host 0.0.0.0 --port 5002 --reload
```

启动后可访问：

- Web 首页：`http://localhost:5002/`
- 任务监控页：`http://localhost:5002/jobs`
- 健康检查：`http://localhost:5002/health`
- Swagger：`http://localhost:5002/docs`

开启详细日志：

```bash
BOOKING_DEBUG=1 python -m smu_badminton.server_fastapi
```

## Docker 部署

### Docker Compose

```bash
docker-compose up --build
```

默认会将服务暴露在 `http://localhost:5000`。

`docker-compose.yml` 中做了这些事情：

- 挂载 `./.env` 到容器内 `/app/.env`
- 使用 Docker volume 持久化 `/app/data`
- 对 `/health` 做健康检查

### 手动构建

```bash
docker build -t smu-badminton .
docker run -d \
  --name smu-badminton \
  -p 5000:5000 \
  -v ./data:/app/data \
  -v ./.env:/app/.env:ro \
  -e TZ=Asia/Shanghai \
  -e SERVER_PORT=5000 \
  smu-badminton
```

## 常用环境变量

除了 `.env.example` 里的基础配置，运行时还常用这些变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SERVER_PORT` | 本地 `5002` / Docker `5000` | 服务监听端口 |
| `BOOKING_DEBUG` | `0` | 设为 `1` 输出详细预约日志 |
| `UVICORN_RELOAD` | `0` | 设为 `1` 开启自动重载 |
| `TOKEN_CACHE_TTL_SEC` | `900` | Token 缓存时间 |
| `TOKEN_PROFILE_TTL_SEC` | `3600` | 用户 Profile 缓存时间 |
| `JOB_RETENTION_SEC` | `3600` | 历史任务保留时间 |
| `DATA_DIR` | 自动判断 | SQLite 数据目录 |
| `RATE_LIMIT_MAX` | `30` | 默认接口限流数 |
| `RATE_LIMIT_WINDOW` | `10` | 默认限流窗口秒数 |
| `RATE_LIMIT_JOBS_MAX` | `300` | 任务接口限流数 |
| `RATE_LIMIT_JOBS_WINDOW` | `60` | 任务接口限流窗口秒数 |

完整配置说明见 [Documents/docs/guide/config.md](Documents/docs/guide/config.md)。

## API 概览

服务主要提供这些接口：

- `POST /api/captcha`：获取验证码图片（base64 + session_id）
- `POST /api/login`：登录并获取 Token（不带 captcha_code 则自动 OCR）
- `POST /api/auth/refresh`：用服务端保存的账号静默续期
- `GET /api/auth/check`：检查用户是否在授权列表
- `POST /api/logout`：清除 Token 缓存
- `POST /api/availability`：查询场地可用性
- `POST /api/book`：即时预约（同步语义，供程序化调用）
- `POST /api/jobs/immediate`：即时预约（异步任务，前端默认）
- `POST /api/book/schedule`：定时预约（统一后台执行）
- `POST /api/jobs/scheduled`：定时预约任务（异步）
- `GET /api/jobs`：查询任务列表，支持 `?username=` 过滤
- `GET /api/schedule/{job_id}`：查询单个任务状态
- `POST /api/jobs/{job_id}/stop`：停止指定任务
- `POST /api/jobs/stop_by_params`：按参数停止并尽力撤销学校侧预约
- `GET/POST /api/local_bookings`：查看/保存本地预约记录
- `GET /api/config` / `POST /api/config/update`：读取或热更新配置
- `GET /api/metrics`：查看指标
- `GET /health`：健康检查

详细请求与响应格式见 [Documents/docs/guide/api.md](Documents/docs/guide/api.md)。

## 测试

运行全部测试：

```bash
python -m pytest tests/ -v
```

仅运行单元测试：

```bash
python -m pytest tests/unit/ -v
```

仅运行集成测试：

```bash
python -m pytest tests/integration/ -v
```

## 文档站

项目文档基于 VitePress，源码位于 `Documents/docs/`。

本地预览：

```bash
npm install
npm run docs:dev
```

构建静态文档：

```bash
npm run docs:build
```

建议先读：

- [Documents/docs/guide/quick-start.md](Documents/docs/guide/quick-start.md)
- [Documents/docs/guide/install.md](Documents/docs/guide/install.md)
- [Documents/docs/guide/config.md](Documents/docs/guide/config.md)
- [Documents/docs/guide/cas-auth.md](Documents/docs/guide/cas-auth.md)
- [Documents/docs/guide/booking.md](Documents/docs/guide/booking.md)
- [Documents/docs/guide/ocr-captcha.md](Documents/docs/guide/ocr-captcha.md)
- [Documents/docs/guide/api.md](Documents/docs/guide/api.md)
- [Documents/docs/guide/faq.md](Documents/docs/guide/faq.md)

## 注意事项

- 验证码识别使用 ddddocr 本地整图识别，无需额外模型文件或远程 OCR 服务。
- 如果使用默认 `SECRET_KEY`，密码混淆安全性较弱，不适合生产环境。
- 定时预约任务会落到 SQLite，服务重启后会自动恢复待执行任务。
- 同一用户同一天只能有一个预约，系统会做本地去重和资源锁保护。
- 上游对预约接口按账号限流：约 2 连发内安全，第 3 发触发 3 分钟封禁，
  因此并发开火数硬上限为 2（实测方法见 `scripts/test_captcha_reuse.py`）。
- 滑块验证码为一次性凭证（复用会被拒绝），抢票流水线在预取窗口为每枪各解一份。
- 「取消」会尽力同步撤销学校侧预约；该账号从未在本系统登录过时仅能清除本地排队。

## License

MIT
