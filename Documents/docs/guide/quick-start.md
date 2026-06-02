# 快速开始

## 环境要求

- **Python** >= 3.11
- **pip** (Python 包管理器)
- **Git** (用于克隆仓库)
- 可选：**Docker** & **Docker Compose** (用于容器化部署)

## 安装步骤

### 1. 克隆项目

```bash
git clone https://github.com/a645162/shmtu-terminal.git
cd shmtu-terminal/Server/smu-badminton
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

### 3. 配置环境变量

复制 `.env.example` 为 `.env` 并填写必要配置：

```bash
cp .env.example .env
```

最小配置需要修改以下项：

```env
# CAS 登录地址（通常无需修改，使用默认值即可）
CAS_ORIGIN=https://cas.shmtu.edu.cn

# 微服务平台地址（通常无需修改）
WF_ORIGIN=https://wf.shmtu.edu.cn
WF_API_URL=https://wf.shmtu.edu.cn/bus/graphql/apps_yy_sys

# OAuth 客户端 ID（通常无需修改）
OAUTH_CLIENT_ID=kwxKbMKq3Nafw2mApFZz

# 羽毛球场地资源类型 ID（通常无需修改）
BADMINTON_TYPE_ID=93c2a115-5c73-4e30-bb6a-dfcc5404e46f
```

### 4. 准备 OCR 模型文件

将 NCNN 模型文件放入 `model/` 目录：

```
model/
  resnet34_digit_latest.fp32.param
  resnet34_digit_latest.fp32.bin
  resnet18_operator_latest.fp32.param
  resnet18_operator_latest.fp32.bin
  resnet18_equal_symbol_latest.fp32.param
  resnet18_equal_symbol_latest.fp32.bin
```

> 模型文件较大且被 gitignore，需要单独获取。

### 5. 启动服务

开发模式启动（端口 5002，自动重载）：

```bash
python -m smu_badminton.server_fastapi
```

或使用 uvicorn 直接启动：

```bash
uvicorn smu_badminton.server_fastapi:app --host 0.0.0.0 --port 5000 --reload
```

启动成功后访问 `http://localhost:5002` 即可使用 Web 界面。

### 6. 调试模式

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
