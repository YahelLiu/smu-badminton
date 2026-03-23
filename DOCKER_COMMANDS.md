# Docker 部署指南

## 快速启动

### 一键部署（构建+启动）
```bash
docker-compose up -d --build
```

### 查看日志
```bash
docker-compose logs -f
# 或只看最新100行
docker-compose logs -f --tail=100
```

### 访问服务
- Web界面: http://localhost:5000
- 任务列表: http://localhost:5000/jobs

## 常用命令

### 1. 构建镜像
```bash
docker-compose build
```

### 2. 启动容器（后台运行）
```bash
docker-compose up -d
```

### 3. 查看容器状态
```bash
docker-compose ps
```

### 4. 停止容器
```bash
docker-compose stop
```

### 5. 停止并删除容器
```bash
docker-compose down
```

### 6. 重启容器
```bash
docker-compose restart
```

### 7. 进入容器（调试用）
```bash
docker-compose exec badminton-booking bash
```

### 8. 重新构建并启动（修改代码后）
```bash
docker-compose up -d --build
```

## 数据持久化

数据库文件会自动保存到宿主机当前目录：
- `data.db` - 主数据库文件
- `data.db-wal` - WAL日志文件  
- `data.db-shm` - 共享内存文件
- `data/` - 数据目录
- `model/` - 模型文件目录

## 健康检查

容器会每30秒自动检查服务健康状态，查看健康状态：

```bash
docker inspect badminton-booking | grep -A 10 Health
# 或
docker ps
```

## 故障排查

### 查看详细错误日志
```bash
docker-compose logs --tail=200 badminton-booking
```

### 完全重建
```bash
# 停止并删除容器
docker-compose down

# 重新构建（不使用缓存）
docker-compose build --no-cache

# 启动
docker-compose up -d
```

### 进入容器调试
```bash
docker-compose exec badminton-booking bash
# 然后可以手动运行 Python 命令检查
python -c "import fastapi; print('FastAPI OK')"
python server_fastapi.py
```

## 端口冲突

如果5000端口已被占用，修改 `docker-compose.yml`：

```yaml
ports:
  - "8080:5000"  # 改为其他端口
```

## 更新代码

```bash
# 停止容器
docker-compose down

# 拉取最新代码（如果使用git）
git pull

# 重新构建并启动
docker-compose up -d --build
```

## 清理

```bash
# 删除容器和网络
docker-compose down

# 删除容器、网络和卷
docker-compose down -v

# 删除镜像
docker rmi badminton-booking
```
