# 羽毛球预约系统 Docker 镜像
# 基于 Python 3.11 slim 版本

FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖
# - libgl1: OpenCV 需要
# - libglib2.0-0: OpenCV 需要
# - libsm6, libxext6, libxrender1: OpenCV GUI 依赖
# - libxml2-dev, libxslt-dev: lxml 需要
# - gcc, g++: 编译 Python 包需要
# - libgomp1: OpenCV 运行时需要
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libxml2-dev \
    libxslt-dev \
    libgomp1 \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 先安装核心依赖，避免一次性安装失败
RUN pip install --no-cache-dir --upgrade pip setuptools wheel

# 分步安装依赖，便于排查问题
RUN pip install --no-cache-dir \
    fastapi==0.116.1 \
    uvicorn==0.35.0 \
    pydantic==2.11.9 \
    requests==2.32.4 \
    python-dotenv==1.1.1

# 安装其他依赖（如果失败不影响核心功能）
RUN pip install --no-cache-dir -r requirements.txt || true

# 复制应用代码
COPY src/ /app/src/
COPY model/ /app/model/
COPY templates/ /app/templates/
COPY static/ /app/static/
COPY .env /app/.env

# 安装为可编辑包
RUN pip install --no-cache-dir -e .

# 确保必要的目录存在
RUN mkdir -p /app/data

# 暴露端口
EXPOSE 5000

# 健康检查 - 使用 wget 更可靠
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health', timeout=5)" || exit 1

# 启动命令
CMD ["python", "-m", "smu_badminton.server_fastapi"]
