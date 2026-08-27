# 快速开始

## 环境要求

- **Python** >= 3.11
- **pip** (Python 包管理器)
- **Git** (用于克隆仓库)
- 可选：**Docker** & **Docker Compose** (用于容器化部署)

## 安装步骤

### 1. 克隆项目

```bash
git clone https://github.com/YahelLiu/smu-badminton.git
cd smu-badminton
```

### 2. 安装依赖

推荐使用可编辑模式安装，方便开发调试：

```bash
pip install -e .
```

或使用 requirements.txt 安装：

```bash
pip install -r requirements.txt
```

> 若使用 [uv](https://github.com/astral-sh/uv) 管理环境，用 `uv sync` 同步依赖。
>
> ddddocr 在部分国内镜像源（如清华 tuna）可能未收录，安装失败时改用官方源：
> `pip install ddddocr -i https://pypi.org/simple`

### 3. 配置环境变量

复制 `.env.example` 为 `.env` 并填写必要配置：

```bash
cp .env.example .env
```

最小配置需要修改以下项：

```env
# CAS 登录地址（已迁移至 sso.shmtu.edu.cn，通常无需修改，使用默认值即可）
CAS_ORIGIN=https://sso.shmtu.edu.cn
CAS_CAPTCHA_URL=https://sso.shmtu.edu.cn/cas/captcha

# 微服务平台地址（通常无需修改）
WF_ORIGIN=https://wf.shmtu.edu.cn
WF_API_URL=https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys

# OAuth 客户端 ID（通常无需修改）
OAUTH_CLIENT_ID=kwxKbMKq3Nafw2mApFZz

# 羽毛球场地资源类型 ID（通常无需修改）
BADMINTON_TYPE_ID=93c2a115-5c73-4e30-bb6a-dfcc5404e46f
```

生产环境额外建议：

```env
SECRET_KEY=your-random-secret-key
AUTHORIZED_USERS=202540510004
TRUSTED_PROXIES=127.0.0.1
```

### 4. 启动服务

开发模式启动（端口 5002，可通过 `UVICORN_RELOAD=1` 开启自动重载）：

```bash
python -m smu_badminton.server_fastapi
```

或使用 uvicorn 直接启动：

```bash
uvicorn smu_badminton.server_fastapi:app --host 0.0.0.0 --port 5002 --reload
```

启动成功后访问 `http://localhost:5002` 即可使用 Web 界面。

> 验证码识别使用 ddddocr 本地整图识别，首次调用时惰性加载 onnx 模型，无需预先准备任何模型文件。

### 5. 调试模式

设置环境变量 `BOOKING_DEBUG=1` 可开启详细的预约日志输出：

```bash
BOOKING_DEBUG=1 python -m smu_badminton.server_fastapi
```

## 验证安装

启动服务后，访问健康检查接口确认服务正常运行：

```bash
curl http://localhost:5002/health
```

返回 `{"ok": true}` 表示服务启动成功。

## 下一步

- [安装部署](/guide/install) — 了解 Docker 部署和生产环境配置
- [CAS 认证](/guide/cas-auth) — 了解统一认证流程和验证码处理
- [预约功能](/guide/booking) — 了解即时预约和定时预约的区别
- [API 文档](/guide/api) — 查看所有 REST API 端点详情
