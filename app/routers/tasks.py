"""
任务相关 API 路由
"""
import json
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
from app.events import subscribe, unsubscribe

router = APIRouter()

running_procs = {}  # legacy, keep for manual stop


class TaskCreate(BaseModel):
    id: str
    name: str
    m3u8_url: str
    headers: str = ""
    key: str = ""
    iv: str = ""
    download_dir: str = ""


@router.get("/api/tasks/events")
async def task_events():
    """SSE 端点：任务变更时推送完整任务列表"""
    q = await subscribe()

    async def event_generator():
        try:
            # 连接时立即推送一次当前任务列表
            tasks = list_tasks()
            for t in tasks:
                t.pop("m3u8_url", None)
            yield f"data: {json.dumps({'total': len(tasks), 'list': tasks})}\n\n"

            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(payload)}\n\n"
                except asyncio.TimeoutError:
                    # 每 15 秒发送心跳保持连接
                    yield ": heartbeat\n\n"
        finally:
            await unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


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
                       body.headers, body.key, body.iv, download_dir=body.download_dir)
    return {"data": [task]}


@router.post("/api/tasks/from-url")
def create_task_from_url(body: dict):
    """只提供 video_url，自动抓取页面标题和 m3u8，生成任务"""
    from app.services.rss_poller import resolve_video_info
    video_url = body.get("url") or body.get("video_url")
    if not video_url:
        raise HTTPException(400, "url or video_url required")
    download_dir = body.get("download_dir", "")

    # 支持传入解析规则（可选），用于通用网页解析
    source_config = {}
    for key in ["referer", "headers", "title_selector", "m3u8_selector",
                "video_id_pattern", "key_selector", "iv_selector"]:
        if body.get(key):
            source_config[key] = body[key]

    import traceback
    try:
        info = resolve_video_info(video_url, source_config=source_config if source_config else None)
    except Exception as e:
        print(f"[from-url] resolve_video_info failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(502, f"页面解析失败: {e}")
    if not info:
        raise HTTPException(502, f"无法从页面提取 m3u8: {video_url}")
    task = create_task(info["id"], info["name"], info["m3u8_url"],
                       info.get("headers", ""), info.get("key", ""), info.get("iv", ""),
                       download_dir=download_dir)
    try_start_next()
    return {"data": [task]}


@router.post("/api/tasks/from-m3u8")
def create_task_from_m3u8(body: dict):
    """直接提供 m3u8 URL 创建任务，无需解析页面"""
    import uuid
    m3u8_url = body.get("m3u8_url") or body.get("url")
    if not m3u8_url:
        raise HTTPException(400, "m3u8_url required")
    name = body.get("name", "").strip()
    if not name:
        # 从 URL 提取名称
        from urllib.parse import urlparse
        parsed = urlparse(m3u8_url)
        path_parts = parsed.path.strip('/').split('/')
        name = path_parts[-1].replace('.m3u8', '') if path_parts else str(uuid.uuid4())[:8]
    task_id = body.get("id", "").strip()
    if not task_id:
        task_id = str(uuid.uuid4())[:12]
    headers = body.get("headers", "")
    key = body.get("key", "")
    iv = body.get("iv", "")
    download_dir = body.get("download_dir", "")
    task = create_task(task_id, name, m3u8_url, headers, key, iv, download_dir=download_dir)
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
    # 清理 temp_dir 中的残留
    cfg = get_download_config()
    temp_dir = cfg.get("temp_dir", "")
    if temp_dir:
        import glob
        for f in glob.glob(f"{temp_dir}/{task_id}*"):
            try:
                p = Path(f)
                if p.is_file():
                    p.unlink()
            except Exception:
                pass
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
