# SMU 羽毛球预约系统 - 任务系统规范化

## 背景

当前任务系统存在以下问题：
1. **即时任务无持久化** - 即时任务（immediate）只存在内存中，完成后没有任何记录
2. **状态显示混乱** - 任务完成后，用户无法看到历史任务状态
3. **即时任务和预约显示相同** - 即时任务完成后应该有独立的"完成"提示，而不是和预约记录混在一起

---

## 当前问题分析

### 任务生命周期对比

| 阶段 | 即时任务 (immediate) | 定时任务 (scheduled) |
|------|---------------------|---------------------|
| 创建 | 内存 `_jobs` 字典 | 内存 + 数据库 `scheduled_jobs` 表 |
| 执行 | 立即执行 | 等待到目标时间执行 |
| 完成 | ❌ 无记录，直接消失 | ✅ 更新数据库状态为 done/failed |
| 查询 | 只能查到活跃任务 | 可查历史任务 |

### 代码层面问题

**`start_immediate_booking()` 缺少：**
1. 数据库持久化调用
2. 完成后的状态更新

**`list_jobs()` 只返回活跃任务：**
```python
# 过滤已结束线程，顺便回收
if not job.thread.is_alive() or job.cancel_event.is_set():
    continue  # 跳过已完成的任务
```

**`list_scheduled_jobs()` 只查数据库：**
- 只返回 `scheduled_jobs` 表的数据
- 即时任务从未写入该表

---

## 实施方案

### 方案：复用 `scheduled_jobs` 表存储所有任务

将 `scheduled_jobs` 表改名为任务表，同时存储即时任务和定时任务。

**优点：**
- 改动最小
- 不需要新建表
- 历史代码逻辑可复用

### 修改步骤

#### 1. 修改 `start_immediate_booking()` - 添加数据库持久化

**文件：** `cas_manager.py`

```python
def start_immediate_booking(self, ...) -> str:
    # ... 现有代码 ...

    job_id = self._register(th, cancel_event, meta)

    # 新增：持久化即时任务到数据库
    self._persist_job_row(
        job_id,
        login_url=login_url,
        captcha_url=captcha_url,
        username=username,
        password=password,
        bookdate=bookdate,
        kssj=kssj,
        jssj=jssj,
        resources_name=resources_name,
        target_time_str="",  # 即时任务无目标时间
        num_threads=1,
        status="running",
    )

    th.start()
    return job_id
```

#### 2. 修改即时任务执行逻辑 - 添加状态更新

**文件：** `cas_manager.py` 的 `run()` 函数

```python
def run():
    if cancel_event.is_set():
        self._update_job_row_status(job_id, "cancelled")
        return

    # ... 登录逻辑 ...
    if not tokens:
        self._update_job_row_status(job_id, "failed")
        return

    # ... 预约逻辑 ...
    result = fetch_resource_time_id(...)
    if not result:
        self._update_job_row_status(job_id, "failed")
        return

    # 预约成功
    resp = make_appointment(...)
    if resp and resp.get("code") == "success":
        self._update_job_row_status(job_id, "done")
    else:
        self._update_job_row_status(job_id, "failed")
```

#### 3. 修改 `list_jobs()` - 返回所有活跃任务（保持不变）

当前逻辑已经正确，只返回活跃任务。

#### 4. 修改 `list_scheduled_jobs()` - 返回所有历史任务

**文件：** `cas_manager.py`

```python
def list_all_jobs(self, username: str | None = None) -> List[Dict[str, Any]]:
    """获取所有任务（包括即时任务和定时任务）"""
    with self._db_pool.get_connection(auto_commit=False) as conn:
        if username:
            cur = conn.execute(
                """SELECT job_id, username, bookdate, kssj, jssj, resources_name,
                          target_time_str, num_threads, status, created_at
                   FROM scheduled_jobs
                   WHERE username=?
                   ORDER BY created_at DESC""",
                (username,),
            )
        else:
            cur = conn.execute(
                """SELECT job_id, username, bookdate, kssj, jssj, resources_name,
                          target_time_str, num_threads, status, created_at
                   FROM scheduled_jobs
                   ORDER BY created_at DESC"""
            )
        # ... 返回格式化结果 ...
```

#### 5. 修改 API `/api/jobs` - 合并返回

**文件：** `server_fastapi.py`

```python
@app.get("/api/jobs", response_model=JobsListResponse)
async def api_jobs_list():
    jobs = booking_manager.list_jobs()  # 内存中的活跃任务
    db_jobs = booking_manager.list_all_jobs()  # 数据库中的所有任务（含历史）
    return {"ok": True, "data": {"jobs": jobs, "db_jobs": db_jobs}}
```

#### 6. 前端 `jobs.js` - 区分任务类型显示

即时任务完成显示 "✅ 预约成功"，定时任务完成显示 "🎯 抢票成功"。

---

## 关键文件修改清单

| 文件 | 修改内容 |
|------|----------|
| `cas_manager.py` | 1. `start_immediate_booking()` 添加数据库持久化 |
| `cas_manager.py` | 2. 即时任务 `run()` 添加状态更新 |
| `cas_manager.py` | 3. `list_scheduled_jobs()` 重命名为 `list_all_jobs()` |
| `server_fastapi.py` | 4. `/api/jobs` 调用新方法 |
| `static/js/jobs.js` | 5. 前端区分即时/定时任务显示 |

---

## 验证方案

1. 创建一个即时任务，观察：
   - 数据库 `scheduled_jobs` 表有新记录
   - 任务完成后状态变为 `done` 或 `failed`
2. 访问 `/jobs` 页面：
   - 能看到即时任务的历史记录
   - 状态显示正确
3. 创建一个定时任务，观察：
   - 功能不受影响
   - 任务列表正确合并显示
