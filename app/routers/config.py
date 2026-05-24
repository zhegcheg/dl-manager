"""
配置 / 代理 / 队列状态 / 媒体 相关 API 路由
"""
import os
import os as _os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.db.database import (
    get_task, list_tasks, update_task,
    get_scheduler_config, set_scheduler_config,
    get_download_config, set_download_config,
    get_proxy_config, set_proxy_config,
)
from app.services.queue import get_active_downloads, apply_max_concurrent
from app.services.scheduler import reschedule

router = APIRouter()


# ── 健康检查 ──
@router.get("/healthy")
def health():
    return {"status": "ok", "active_downloads": get_active_downloads()}


# ── 调度器 ──
@router.get("/api/scheduler")
def get_scheduler():
    return {"data": get_scheduler_config()}


@router.post("/api/scheduler")
def set_scheduler(key: str, value: str):
    set_scheduler_config(key, value)
    if key in ("rss_cron", "rss_enabled"):
        reschedule()
    return {"message": "Updated", "key": key, "value": value}


# ── 下载配置 ──
@router.get("/api/config")
def get_config():
    scheduler = get_scheduler_config()
    download = get_download_config()
    proxy = get_proxy_config()
    return {"data": {**scheduler, **download, **proxy}}


@router.post("/api/config")
def post_config(key: str, value: str):
    if key in ("rss_cron", "rss_enabled"):
        set_scheduler_config(key, value)
        reschedule()
    elif key in ("download_dir", "temp_dir", "max_concurrent", "thread_count", "move_to_nas"):
        set_download_config(key, value)
    return {"message": "Updated", "key": key, "value": value}


@router.post("/api/config/batch")
def batch_update_config(body: dict):
    """批量更新多个配置项（用于保存按钮）"""
    results = {}
    for key in ("download_dir", "temp_dir", "max_concurrent", "thread_count", "move_to_nas", "rss_cron", "rss_enabled"):
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


@router.post("/api/config/apply")
def apply_config(body: dict):
    """批量更新配置并动态调整运行中的任务"""
    results = {}
    stopped = 0
    for key in ("max_concurrent", "thread_count", "download_dir", "temp_dir", "move_to_nas"):
        if key in body:
            value = str(body[key]).strip()
            if key == "max_concurrent":
                try:
                    v = max(1, min(10, int(value)))
                    value = str(v)
                    set_download_config(key, value)
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


# ── 代理配置 ──
@router.get("/api/proxy")
def get_proxy():
    return {"data": get_proxy_config()}


@router.post("/api/proxy")
def save_proxy(body: dict):
    """保存代理配置"""
    results = {}
    for key in ("enabled", "type", "host", "port"):
        if key in body:
            value = str(body[key]).strip()
            if key == "enabled":
                value = "true" if value.lower() in ("true", "1", "on") else "false"
            elif key == "type":
                if value not in ("http", "socks5"):
                    value = "http"
            elif key == "port":
                try:
                    port = int(value)
                    if not (1 <= port <= 65535):
                        port = 7890
                    value = str(port)
                except ValueError:
                    results[key] = "invalid"
                    continue
            set_proxy_config(key, value)
            results[key] = value
    return {"message": "已保存", "data": results}


# ── 队列状态 ──
@router.get("/api/queue/status")
def queue_status():
    """获取队列状态概览"""
    cfg = get_download_config()
    max_concurrent = int(cfg.get("max_concurrent", "2"))
    active = get_active_downloads()
    waiting = len(list_tasks(status="waiting"))
    downloading = len(list_tasks(status="downloading"))
    return {
        "active": active,
        "waiting": waiting,
        "downloading": downloading,
        "max_concurrent": max_concurrent,
        "available_slots": max(0, max_concurrent - active),
    }


# ── 媒体文件 ──
MEDIA_DIR = Path(os.getenv("NAS_MEDIA_DIR", "/mnt/fn-nas-imovie"))


@router.get("/api/media/by-id/{task_id}")
@router.head("/api/media/by-id/{task_id}")
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
        except Exception:
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

    return StreamingResponse(file_iterator(), status_code=status_code, headers=headers)


@router.get("/api/media/{filename:path}")
def serve_media(filename: str):
    """从 NAS 提供视频文件"""
    if ".." in filename or filename.startswith("/"):
        raise HTTPException(403, "Access denied")
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(file_path), media_type="video/mp4", filename=filename)
