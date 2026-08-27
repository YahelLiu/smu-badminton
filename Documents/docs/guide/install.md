# 安装部署

## pip 安装（本地开发）

### 前置依赖

- Python >= 3.11
- 系统级依赖：OpenCV 运行时库（libgl1, libglib2.0-0, libgomp1 等）

### 安装步骤

```bash
# 克隆项目
git clone https://github.com/YahelLiu/smu-badminton.git
cd smu-badminton

# 可编辑模式安装（推荐开发使用）
pip install -e .

# 或安装开发依赖
pip install -e ".[dev]"
```

> 若使用 [uv](https://github.com/astral-sh/uv) 管理环境，用 `uv sync` 同步运行依赖，
> `uv sync --extra dev` 同步含测试依赖。
>
> ddddocr 在部分国内镜像源（如清华 tuna）可能未收录，安装失败时改用官方源：
> `pip install ddddocr -i https://pypi.org/simple`

### 启动开发服务器

```bash
# 默认端口 5002
python -m smu_badminton.server_fastapi

# 自定义端口
SERVER_PORT=8080 python -m smu_badminton.server_fastapi

# 调试模式
BOOKING_DEBUG=1 python -m smu_badminton.server_fastapi

# 自动重载（开发用）
UVICORN_RELOAD=1 python -m smu_badminton.server_fastapi
```

## Docker 部署（生产环境）

### 使用 docker-compose（推荐）

这是最简单的部署方式，一条命令即可启动：

```bash
docker-compose up --build
```

服务将在 `http://localhost:5000` 启动。

### docker-compose 配置说明

```yaml
services:
  smu-badminton:
    build: .
    container_name: smu-badminton
    restart: unless-stopped
    ports:
      - "5000:5000"
    volumes:
      # 数据库持久化（Docker volume）
      - smu-badminton-data:/app/data
      # 环境变量配置
      - ./.env:/app/.env:ro
    environment:
      - TZ=Asia/Shanghai
      - PYTHONUNBUFFERED=1
      - SERVER_PORT=5000
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:5000/health', timeout=5)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s

volumes:
  smu-badminton-data:
```

### 手动 Docker 构建

```bash
# 构建镜像
docker build -t smu-badminton .

# 运行容器
docker run -d \
  --name smu-badminton \
  -p 5000:5000 \
  -v ./data:/app/data \
  -v ./.env:/app/.env:ro \
  -e TZ=Asia/Shanghai \
  -e SERVER_PORT=5000 \
  smu-badminton
```

### Docker 镜像说明

Dockerfile 基于 `python:3.11-slim`，安装了以下系统级依赖：

| 依赖包 | 用途 |
|--------|------|
| libgl1, libglib2.0-0, libgomp1 | OpenCV 运行时 |
| libsm6, libxext6, libxrender1 | OpenCV GUI 支持 |
| libxml2-dev, libxslt-dev | lxml 解析 |
| gcc, g++ | 编译 Python C 扩展 |

镜像内置了健康检查（`/health` 端点），每 30 秒检查一次。

## 端口说明

| 环境 | 端口 | 配置方式 |
|------|------|----------|
| 本地开发 | 5002 | 默认值，可通过 `SERVER_PORT` 环境变量覆盖 |
| Docker/生产 | 5000 | docker-compose 中 `SERVER_PORT=5000` |

## 数据存储

### 数据库

系统使用 SQLite 存储数据，数据库文件位置：

- **Docker 环境**：`/app/data/`（通过 Docker volume 持久化）
- **本地开发**：项目根目录下的 `data/` 目录

SQLite 以 WAL（Write-Ahead Logging）模式运行，支持并发读写。

## 运行测试

```bash
# 运行全部测试
python -m pytest tests/ -v

# 仅运行单元测试
python -m pytest tests/unit/ -v

# 仅运行集成测试
python -m pytest tests/integration/ -v

# 运行单个测试文件
python -m pytest tests/unit/test_obfuscate.py -v

# 运行单个测试用例
python -m pytest tests/unit/test_obfuscate.py::test_roundtrip -v
```
