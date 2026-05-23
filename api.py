"""
FastAPI HTTP API
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import subprocess
import threading
from pathlib import Path

from task_db import (
    create_task, get_task, list_tasks, update_task, delete_task,
    get_task_log_path, get_task_dir, reset_task_for_retry,
    add_source, list_sources, get_source, update_source, delete_source,
    get_scheduler_config, set_scheduler_config,
    get_download_config, set_download_config
)
from scheduler import reschedule
from ntrh_downloader import start_download
from mover import move_to_media_library
from queue_manager import get_active_downloads, is_downloading, try_start_next, apply_max_concurrent

app = APIRouter()

class TaskCreate(BaseModel):
    id: str
    name: str
    m3u8_url: str
    headers: str = ""
    key: str = ""
    iv: str = ""

class SourceCreate(BaseModel):
    name: str
    url: str
    feed_type: str = "jable"

class SourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    feed_type: Optional[str] = None
    enabled: Optional[bool] = None

running_procs = {}  # legacy, keep for manual stop

@app.get("/healthy")
def health():
    return {"status": "ok", "active_downloads": get_active_downloads()}

@app.get("/api/tasks")
def get_tasks(status: Optional[str] = None):
    tasks = list_tasks(status=status)
    for t in tasks:
        t.pop("m3u8_url", None)
    return {"total": len(tasks), "list": tasks}

@app.get("/api/tasks/{task_id}")
def get_task_detail(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {"data": task}

@app.post("/api/tasks")
def create_new_task(body: TaskCreate):
    task = create_task(body.id, body.name, body.m3u8_url,
                      body.headers, body.key, body.iv)
    return {"data": [task]}

@app.post("/api/tasks/{task_id}/start")
def start_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["status"] in ("downloading", "merging", "moving"):
        raise HTTPException(400, "Task already running")
    if is_downloading(task_id):
        raise HTTPException(400, "Task already running")
    # 检查队列容量
    from queue_manager import get_active_downloads, register_download
    from task_db import get_download_config
    cfg = get_download_config()
    max_concurrent = int(cfg.get("max_concurrent", "2"))
    if get_active_downloads() >= max_concurrent:
        return {"message": f"队列已满（{max_concurrent}），任务已加入等待队列"}
    proc = start_download(task_id, task["m3u8_url"], task["headers"],
                          task["key"], task["iv"])
    running_procs[task_id] = proc  # 保留用于手动 stop

    def monitor_and_move():
        proc.wait()
        running_procs.pop(task_id, None)
        from queue_manager import unregister_download
        unregister_download(task_id)
        t = get_task(task_id)
        if t and t["status"] == "completed" and t.get("file"):
            update_task(task_id, stage="moving", progress=0)
            ok2, final = move_to_media_library(task_id, t["file"], t["name"] + ".mp4")
            if ok2:
                update_task(task_id, status="completed", stage="completed", progress=100)
            else:
                update_task(task_id, status="failed", error=f"Move failed: {final}")

    t = threading.Thread(target=monitor_and_move, daemon=True)
    t.start()
    return {"message": "Download started"}

@app.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: str):
    from queue_manager import is_downloading as qm_is_running, unregister_download
    proc = running_procs.get(task_id)
    if proc:
        proc.terminate()
        running_procs.pop(task_id, None)
    if qm_is_running(task_id):
        unregister_download(task_id)
    subprocess.run(["pkill", "-f", f"N_m3u8DL-RE.*{task_id}"], capture_output=True)
    update_task(task_id, status="stopped", stage="stopped")
    return {"message": "Stopped"}

@app.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str):
    """重试卡死的任务，最多3次，超过则失败"""
    from queue_manager import is_downloading as qm_is_running, unregister_download
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    # 清理可能残留的旧进程
    proc = running_procs.get(task_id)
    if proc:
        proc.terminate()
        running_procs.pop(task_id, None)
    if qm_is_running(task_id):
        unregister_download(task_id)
    # 重置并增加重试计数（设为 waiting，加入队列）
    ok = reset_task_for_retry(task_id)
    if not ok:
        return {"message": "重试次数已达上限（3次），请删除任务或手动重置", "max_reached": True}
    # 加入队列，等待空闲时启动
    from queue_manager import try_start_next
    try_start_next()
    return {"message": "任务已加入队列", "retry_count": task.get("retry_count", 0) + 1}


@app.delete("/api/tasks/{task_id}")
def remove_task(task_id: str):
    from queue_manager import is_downloading as qm_is_running, unregister_download
    proc = running_procs.get(task_id)
    if proc:
        proc.terminate()
        running_procs.pop(task_id, None)
    if qm_is_running(task_id):
        unregister_download(task_id)
    subprocess.run(["pkill", "-f", f"N_m3u8DL-RE.*{task_id}"], capture_output=True)
    task_dir = get_task_dir(task_id)
    import shutil
    if task_dir.exists():
        shutil.rmtree(task_dir)
    log_path = get_task_log_path(task_id)
    if log_path.exists():
        log_path.unlink()
    delete_task(task_id)
    return {"message": "Deleted"}

@app.get("/api/tasks/{task_id}/logs")
async def task_logs(task_id: str):
    from fastapi.responses import StreamingResponse
    log_path = get_task_log_path(task_id)

    async def event_generator():
        if not log_path.exists():
            yield "data: log not found\n\n"
            return
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if not line:
                    import asyncio
                    await asyncio.sleep(0.5)
                    continue
                yield f"data: {line.strip()}\n\n"

    return StreamingResponse(event_generator(),
                          media_type="text/event-stream")

# ── 订阅源 ──
@app.get("/api/sources")
def get_sources():
    return {"list": list_sources()}

@app.post("/api/sources")
def create_source(body: SourceCreate):
    src = add_source(body.name, body.url, body.feed_type)
    return {"data": src}

@app.put("/api/sources/{source_id}")
def put_source(source_id: int, body: SourceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    src = update_source(source_id, **fields)
    if not src:
        raise HTTPException(404, "Source not found")
    return {"data": src}

@app.patch("/api/sources/{source_id}")
def patch_source(source_id: int, body: SourceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    src = update_source(source_id, **fields)
    if not src:
        raise HTTPException(404, "Source not found")
    return {"data": src}

@app.delete("/api/sources/{source_id}")
def del_source(source_id: int):
    delete_source(source_id)
    return {"message": "Deleted"}

# ── 调度器 ──
@app.get("/api/scheduler")
def get_scheduler():
    return {"data": get_scheduler_config()}

@app.post("/api/scheduler")
def set_scheduler(key: str, value: str):
    set_scheduler_config(key, value)
    if key in ("rss_cron", "rss_enabled"):
        reschedule()
    return {"message": "Updated", "key": key, "value": value}

# ── 手动触发 RSS ──
@app.post("/api/rss/poll")
def trigger_rss():
    from rss_poller import poll_all_sources
    from queue_manager import try_start_next
    new_tasks = poll_all_sources()
    # RSS 轮询后自动尝试启动等待中的任务
    started = try_start_next()
    return {"message": f"RSS 轮询完成，新增 {len(new_tasks)} 个任务，已启动 {started} 个", "count": len(new_tasks), "started": started}

# ── 下载配置 ──
@app.get("/api/config")
def get_config():
    scheduler = get_scheduler_config()
    download = get_download_config()
    return {"data": {**scheduler, **download}}

@app.post("/api/config")
def post_config(key: str, value: str):
    if key in ("rss_cron", "rss_enabled"):
        set_scheduler_config(key, value)
        reschedule()
    elif key in ("download_dir", "max_concurrent", "thread_count"):
        set_download_config(key, value)
    return {"message": "Updated", "key": key, "value": value}

@app.post("/api/config/batch")
def batch_update_config(body: dict):
    """批量更新多个配置项（用于保存按钮）"""
    results = {}
    for key in ("download_dir", "max_concurrent", "thread_count", "rss_cron", "rss_enabled"):
        if key in body:
            value = str(body[key]).strip()
            if key in ("max_concurrent", "thread_count"):
                try:
                    v = int(value)
                    if key == "max_concurrent":
                        v = max(1, min(10, v))
                    elif key == "thread_count":
                        v = max(1, min(16, v))
                    value = str(v)
                except ValueError:
                    results[key] = "invalid number"
                    continue
            if key in ("rss_cron", "rss_enabled"):
                set_scheduler_config(key, value)
                if key == "rss_cron":
                    reschedule()
            else:
                set_download_config(key, value)
            results[key] = value
    return {"message": "Updated", "data": results}


MEDIA_DIR = Path("/mnt/fn-nas-imovie")

@app.get("/api/media/{filename:path}")
def serve_media(filename: str):
    """从 NAS 提供视频文件（支持 Range 请求用于拖拽播放）"""
    # 安全检查：禁止路径穿越
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(403, "Access denied")
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(file_path), media_type="video/mp4", filename=filename)


@app.post("/api/start-waiting")
def start_waiting_tasks():
    count = try_start_next()
    return {"message": f"已启动 {count} 个任务", "count": count}

@app.post("/api/config/apply")
def apply_config(body: dict):
    """
    批量更新配置并动态调整运行中的任务。
    如果 max_concurrent 减小，立即停止最后启动的任务。
    """
    results = {}
    stopped = 0
    for key in ("max_concurrent", "thread_count", "download_dir"):
        if key in body:
            value = str(body[key]).strip()
            if key == "max_concurrent":
                try:
                    v = max(1, min(10, int(value)))
                    value = str(v)
                    set_download_config(key, value)
                    # 动态调整运行中的任务
                    stopped = apply_max_concurrent(v)
                except ValueError:
                    results[key] = "invalid"
                    continue
            elif key == "thread_count":
                try:
                    v = max(1, min(16, int(value)))
                    value = str(v)
                    set_download_config(key, value)
                except ValueError:
                    results[key] = "invalid"
                    continue
            else:
                set_download_config(key, value)
            results[key] = value
    msg = "已保存"
    if stopped > 0:
        msg += f"，已停止 {stopped} 个任务以适配新限制"
    return {"message": msg, "stopped": stopped, "data": results}