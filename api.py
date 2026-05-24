"""
FastAPI HTTP API
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import subprocess
import threading
from pathlib import Path

from task_db import (
    create_task, get_task, list_tasks, update_task, delete_task,
    get_task_log_path, get_task_dir, reset_task_for_auto_retry, reset_task_for_manual_retry,
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


@app.post("/api/tasks/from-url")
def create_task_from_url(body: dict):
    """只提供 video_url，自动抓取页面标题和 m3u8，生成任务"""
    from rss_poller import extract_jable_info
    video_url = body.get("url") or body.get("video_url")
    if not video_url:
        raise HTTPException(400, "url or video_url required")
    info = extract_jable_info(video_url)
    if not info or not info.get("m3u8_url"):
        raise HTTPException(502, f"无法从页面提取 m3u8: {video_url}")
    task = create_task(info["id"], info["name"], info["m3u8_url"],
                      info.get("headers", ""), info.get("key", ""), info.get("iv", ""))
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
    """手动重试：不受次数限制，重置 retry_count=0，并给出上次失败原因"""
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
    # 手动重试：重置计数为 0，返回错误原因供用户参考
    last_error = reset_task_for_manual_retry(task_id)
    # 加入队列，等待空闲时启动
    from queue_manager import try_start_next
    try_start_next()
    return {
        "message": "任务已加入队列（手动重试，不限次数）",
        "last_error": last_error,
        "retry_count": 0
    }


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
    # 删除扁平化后的 mp4 文件
    flat_mp4 = Path(get_download_config().get("download_dir", "")) / f"{task_id}.mp4"
    if flat_mp4.exists():
        flat_mp4.unlink()
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

@app.get("/api/media/by-id/{task_id}")
@app.get("/api/media/by-id/{task_id}")
@app.head("/api/media/by-id/{task_id}")
def serve_media_by_id(task_id: str, request: Request):
    """根据任务 ID 从 NAS 流式传输视频文件"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    fp = task.get("final_path", "") or task.get("file", "")
    if not fp:
        raise HTTPException(404, "File path not found")
    file_path = Path(fp)
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    # 直接用 FileResponse 流式传输（不支持 Range 但可播放）
    import os as _os
    stat_result = file_path.stat()
    file_size = stat_result.st_size
    range_header = request.headers.get("range")
    
    start, end = 0, file_size - 1
    status_code = 200
    
    if range_header:
        status_code = 206
        try:
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if len(parts) > 1 and parts[1] else file_size - 1
        except:
            start, end = 0, file_size - 1
    
    length = end - start + 1
    
    def file_iterator():
        with _os.fdopen(_os.open(str(file_path), _os.O_RDONLY), "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk_size = min(65536, remaining)
                data = f.read(chunk_size)
                if not data:
                    break
                remaining -= len(data)
                yield data
    
    headers = {
        "Content-Type": "video/mp4",
        "Content-Length": str(length),
        "Content-Disposition": "inline",
        "Accept-Ranges": "bytes",
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    
    from fastapi.responses import StreamingResponse
    return StreamingResponse(file_iterator(), status_code=status_code, headers=headers)

@app.get("/api/media/{filename:path}")
def serve_media(filename: str):
    """从 NAS 提供视频文件（支持 Range 请求用于拖拽播放）"""
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