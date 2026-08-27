# 羽毛球预约系统 Docker 镜像

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖
# - libgl1, libglib2.0-0, libgomp1: OpenCV 运行时
# - libsm6, libxext6, libxrender1: OpenCV GUI
# - libxml2-dev, libxslt-dev: lxml
# - gcc, g++: 编译 Python C 扩展
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

# 先安装依赖（利用 Docker 层缓存）
COPY requirements.txt pyproject.toml ./
COPY src/ /app/src/
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e .

# 复制运行时文件
COPY templates/ /app/templates/
COPY static/ /app/static/

RUN mkdir -p /app/data

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health', timeout=5)" || exit 1

CMD ["python", "-m", "smu_badminton.server_fastapi"]