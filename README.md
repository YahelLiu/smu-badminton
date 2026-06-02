# SMU Badminton

上海海事大学羽毛球场预约系统，提供 Web 界面与 REST API，支持 CAS 统一认证、即时预约、定时抢场、验证码 OCR、本地任务持久化与基础监控能力。

仓库内已经有一套完整文档，`README.md` 的目标是让你先把服务跑起来，再快速找到更细的说明。

## 功能概览

- CAS 统一认证登录，支持自动 OCR 或手动输入验证码
- 即时预约：提交后立即执行
- 定时预约：支持后台任务、到点并发抢场、服务重启后恢复未完成任务
- 场地可用性查询，带 60 秒公共缓存
- SQLite 持久化存储本地预约记录和任务状态
- Web 页面、REST API、健康检查与基础指标接口

## 技术栈

- Python 3.11
- FastAPI + Uvicorn
- SQLite
- Requests + BeautifulSoup + lxml
- OpenCV + NCNN（本地 OCR 模式）
- VitePress（项目文档站）

## 目录结构

```text
src/smu_badminton/      FastAPI 服务与核心业务逻辑
templates/              Web 页面模板
static/                 前端静态资源
tests/                  单元测试与集成测试
Documents/docs/         VitePress 文档
.env.example            示例环境变量
docker-compose.yml      Docker Compose 启动配置
Dockerfile              镜像构建文件
```

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/a645162/shmtu-terminal.git
cd shmtu-terminal/Server/smu-badminton
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

### 3. 配置环境变量

复制示例配置：

```bash
cp .env.example .env
```

最少建议确认这些配置：

```env
CAS_ORIGIN=https://cas.shmtu.edu.cn
CAS_CAPTCHA_URL=https://cas.shmtu.edu.cn/cas/captcha
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

### 4. 准备 OCR

默认 OCR 模式为本地 `local`，需要在 `model/` 下准备 NCNN 模型文件：

```text
model/
  resnet34_digit_latest.fp32.param
  resnet34_digit_latest.fp32.bin
  resnet18_operator_latest.fp32.param
  resnet18_operator_latest.fp32.bin
  resnet18_equal_symbol_latest.fp32.param
  resnet18_equal_symbol_latest.fp32.bin
```

如果不想在本机部署模型，可以在 `.env` 中显式切到远程 OCR：

```env
OCR_MODE=http
OCR_HTTP_HOST=127.0.0.1
OCR_HTTP_PORT=21600
OCR_TIMEOUT=10
```

或：

```env
OCR_MODE=tcp
OCR_TCP_HOST=127.0.0.1
OCR_TCP_PORT=21601
OCR_TIMEOUT=10
```

### 5. 启动服务

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

- 挂载 `./model` 到容器内 `/app/model`
- 挂载 `./.env` 到容器内 `/app/.env`
- 使用 Docker volume 持久化 `/app/data`
- 对 `/health` 做健康检查

### 手动构建

```bash
docker build -t smu-badminton .
docker run -d \
  --name smu-badminton \
  -p 5000:5000 \
  -v ./model:/app/model:ro \
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
| `OCR_MODE` | `local` | `local` / `http` / `tcp` |
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

- `POST /api/login`：登录并获取 Token
- `POST /api/availability`：查询场地可用性
- `POST /api/book`：即时预约
- `POST /api/book/schedule`：定时预约
- `GET /api/jobs`：查询任务列表
- `POST /api/jobs/{job_id}/stop`：停止指定任务
- `GET /api/local_bookings`：查看本地预约记录
- `GET /api/config` / `POST /api/config/update`：读取或更新配置
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
- [Documents/docs/guide/booking.md](Documents/docs/guide/booking.md)
- [Documents/docs/guide/ocr-captcha.md](Documents/docs/guide/ocr-captcha.md)
- [Documents/docs/guide/api.md](Documents/docs/guide/api.md)

## 注意事项

- 本地 OCR 模型文件不在仓库中，需要单独准备。
- 如果使用默认 `SECRET_KEY`，密码混淆安全性较弱，不适合生产环境。
- 定时预约任务会落到 SQLite，服务重启后会自动恢复待执行任务。
- 同一用户同一天只能有一个预约，系统会做本地去重和资源锁保护。

## License

MIT
