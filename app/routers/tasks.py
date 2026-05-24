"""
任务相关 API 路由
"""
import shutil
import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.db.database import (
    get_task, list_tasks, update_task, delete_task,
    create_task, get_task_log_path, get_task_dir,
    reset_task_for_manual_retry, get_download_config,
)
from app.services.queue import get_active_downloads, is_downloading, try_start_next

router = APIRouter()

running_procs = {}  # legacy, keep for manual stop


class TaskCreate(BaseModel):
    id: str
    name: str
    m3u8_url: str
    headers: str = ""
    key: str = ""
    iv: str = ""


@router.get("/api/tasks")
def get_tasks(status: Optional[str] = None):
    tasks = list_tasks(status=status)
    for t in tasks:
        t.pop("m3u8_url", None)
    return {"total": len(tasks), "list": tasks}


@router.get("/api/tasks/{task_id}")
def get_task_detail(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {"data": task}


@router.post("/api/tasks")
def create_new_task(body: TaskCreate):
    task = create_task(body.id, body.name, body.m3u8_url,
                       body.headers, body.key, body.iv)
    return {"data": [task]}


@router.post("/api/tasks/from-url")
def create_task_from_url(body: dict):
    """只提供 video_url，自动抓取页面标题和 m3u8，生成任务"""
    from app.services.rss_poller import extract_jable_info, fetch_jable_m3u8_key
    video_url = body.get("url") or body.get("video_url")
    if not video_url:
        raise HTTPException(400, "url or video_url required")
    import traceback
    try:
        info = extract_jable_info(video_url)
    except Exception as e:
        print(f"[from-url] extract_jable_info failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(502, f"页面解析失败: {e}")
    if not info or not info.get("m3u8_url"):
        raise HTTPException(502, f"无法从页面提取 m3u8: {video_url}")
    if not info.get("key"):
        key, iv = fetch_jable_m3u8_key(info["m3u8_url"])
        info["key"] = key
        info["iv"] = iv
    task = create_task(info["id"], info["name"], info["m3u8_url"],
                       info.get("headers", ""), info.get("key", ""), info.get("iv", ""))
    try_start_next()
    return {"data": [task]}


@router.post("/api/tasks/{task_id}/start")
def start_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task["status"] in ("downloading", "merging", "moving"):
        raise HTTPException(400, "Task already running")
    if is_downloading(task_id):
        raise HTTPException(400, "Task already running")
    from app.services.queue import get_active_downloads, register_download
    from app.db.database import get_download_config
    from app.services.downloader import start_download
    cfg = get_download_config()
    max_concurrent = int(cfg.get("max_concurrent", "2"))
    if get_active_downloads() >= max_concurrent:
        return {"message": f"队列已满（{max_concurrent}），任务已加入等待队列"}
    proc = start_download(task_id, task["m3u8_url"], task["headers"],
                          task["key"], task["iv"])
    running_procs[task_id] = proc
    return {"message": "Download started"}


@router.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: str):
    from app.services.queue import is_downloading as qm_is_running, unregister_download, _running
    proc = running_procs.get(task_id)
    if proc:
        proc.terminate()
        running_procs.pop(task_id, None)
    if qm_is_running(task_id):
        proc = _running.get(task_id)
        if proc:
            proc.terminate()
        unregister_download(task_id)
    update_task(task_id, status="stopped", stage="stopped")
    try_start_next()
    return {"message": "Stopped"}


@router.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str):
    """手动重试：不受次数限制，重置 retry_count=0"""
    from app.services.queue import is_downloading as qm_is_running, unregister_download
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    proc = running_procs.get(task_id)
    if proc:
        proc.terminate()
        running_procs.pop(task_id, None)
    if qm_is_running(task_id):
        unregister_download(task_id)
    last_error = reset_task_for_manual_retry(task_id)
    try_start_next()
    return {
        "message": "任务已加入队列（手动重试，不限次数）",
        "last_error": last_error,
        "retry_count": 0
    }


@router.delete("/api/tasks/{task_id}")
def remove_task(task_id: str):
    from app.services.queue import is_downloading as qm_is_running, unregister_download, _running
    proc = running_procs.get(task_id)
    if proc:
        proc.terminate()
        running_procs.pop(task_id, None)
    if qm_is_running(task_id):
        proc = _running.get(task_id)
        if proc:
            proc.terminate()
        unregister_download(task_id)
    task_dir = get_task_dir(task_id)
    if task_dir.exists():
        shutil.rmtree(task_dir)
    flat_mp4 = Path(get_download_config().get("download_dir", "")) / f"{task_id}.mp4"
    if flat_mp4.exists():
        flat_mp4.unlink()
    log_path = get_task_log_path(task_id)
    if log_path.exists():
        log_path.unlink()
    delete_task(task_id)
    return {"message": "Deleted"}


@router.get("/api/tasks/{task_id}/logs")
async def task_logs(task_id: str):
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
                    await asyncio.sleep(0.5)
                    continue
                yield f"data: {line.strip()}\n\n"

    return StreamingResponse(event_generator(),
                             media_type="text/event-stream")


@router.post("/api/start-waiting")
def start_waiting_tasks():
    count = try_start_next()
    return {"message": f"已启动 {count} 个任务", "count": count}


@router.patch("/api/tasks/{task_id}")
def update_task_info(task_id: str, body: dict):
    """更新任务信息（目前支持优先级）"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    updates = {}
    if "priority" in body:
        try:
            priority = int(body["priority"])
            priority = max(-100, min(100, priority))
            updates["priority"] = priority
        except ValueError:
            raise HTTPException(400, "priority must be a number")

    if not updates:
        raise HTTPException(400, "No valid fields to update")

    update_task(task_id, **updates)
    return {"message": "Updated", "data": get_task(task_id)}
