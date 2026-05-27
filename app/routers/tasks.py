"""
任务相关 API 路由
"""
import json
import shutil
import asyncio
import time
import psutil
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.db.database import (
    get_task, list_tasks, update_task, delete_task,
    create_task, get_task_log_path,
    reset_task_for_manual_retry, get_download_config,
)
from app.services.queue import get_active_downloads, is_downloading, try_start_next
from app.events import subscribe, unsubscribe

router = APIRouter()

# 注意：运行中的进程统一由 app.services.queue._running 管理，
# 不再在本模块维护独立字典，避免双轨制不一致和内存泄漏。

# ====== 系统资源监控 ======

# 用于计算网络速度的全局变量
_net_io_last = {"bytes_sent": 0, "bytes_recv": 0, "time": 0}

# 缓存的 CPU 百分比（后台线程更新，避免阻塞事件循环）
_cpu_percent_cache = {"value": 0.0, "time": 0}

import concurrent.futures
_stats_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="stats")

# 系统统计结果缓存（避免多个 SSE 订阅者重复计算 psutil）
_stats_result_cache = {"data": None, "time": 0}
_STATS_CACHE_TTL = 4  # 缓存 4 秒

def _update_cpu_cache():
    """在后台线程更新 CPU 缓存（阻塞 0.5s，但不影响事件循环）"""
    import time as _t
    now = _t.time()
    if now - _cpu_percent_cache["time"] > 1.5:  # 最多每 1.5s 更新一次
        _cpu_percent_cache["value"] = psutil.cpu_percent(interval=0.5)
        _cpu_percent_cache["time"] = now

@router.get("/api/system/stats")
def get_system_stats():
    """获取系统资源使用情况"""
    global _net_io_last
    
    # 检查缓存是否有效
    now = time.time()
    if _stats_result_cache["data"] and (now - _stats_result_cache["time"]) < _STATS_CACHE_TTL:
        return _stats_result_cache["data"]
    
    # 在后台线程更新 CPU 缓存，不阻塞当前线程
    _stats_executor.submit(_update_cpu_cache)
    cpu_percent = _cpu_percent_cache["value"]
    cpu_count = psutil.cpu_count()
    
    # 内存
    mem = psutil.virtual_memory()
    
    # 磁盘（下载目录）
    try:
        cfg = get_download_config()
        download_dir = cfg.get("download_dir", "/tmp")
        disk = psutil.disk_usage(download_dir)
    except:
        disk = psutil.disk_usage("/")
    
    # 网络速度（计算两次采样的差值）
    net_io = psutil.net_io_counters()
    now = time.time()
    if _net_io_last["time"] > 0:
        dt = now - _net_io_last["time"]
        if dt > 0:
            upload_speed = (net_io.bytes_sent - _net_io_last["bytes_sent"]) / dt
            download_speed = (net_io.bytes_recv - _net_io_last["bytes_recv"]) / dt
        else:
            upload_speed = download_speed = 0
    else:
        upload_speed = download_speed = 0
    _net_io_last = {
        "bytes_sent": net_io.bytes_sent,
        "bytes_recv": net_io.bytes_recv,
        "time": now
    }
    
    result = {
        "cpu": {
            "percent": cpu_percent,
            "count": cpu_count,
        },
        "memory": {
            "total": mem.total,
            "used": mem.used,
            "percent": mem.percent,
            "total_gb": round(mem.total / (1024**3), 1),
            "used_gb": round(mem.used / (1024**3), 1),
        },
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
            "percent": disk.percent,
            "total_gb": round(disk.total / (1024**3), 1),
            "used_gb": round(disk.used / (1024**3), 1),
            "free_gb": round(disk.free / (1024**3), 1),
        },
        "network": {
            "upload_speed": round(upload_speed / 1024, 1),  # KB/s
            "download_speed": round(download_speed / 1024, 1),  # KB/s
            "bytes_sent_total": net_io.bytes_sent,
            "bytes_recv_total": net_io.bytes_recv,
        }
    }
    
    # 更新缓存
    _stats_result_cache["data"] = result
    _stats_result_cache["time"] = time.time()
    
    return result


@router.get("/api/system/stats/stream")
async def stream_system_stats():
    """SSE 推送系统资源使用情况"""
    import asyncio
    loop = asyncio.get_event_loop()
    async def generate():
        while True:
            # 在线程池中执行阻塞的 psutil 调用
            stats = await loop.run_in_executor(_stats_executor, get_system_stats)
            yield f"data: {json.dumps(stats, ensure_ascii=False)}\n\n"
            await asyncio.sleep(5)
    return StreamingResponse(generate(), media_type="text/event-stream")

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
            # 连接时立即推送一次当前任务列表（在线程池中执行同步 DB 查询）
            loop = asyncio.get_event_loop()
            tasks = await loop.run_in_executor(None, list_tasks)
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


# ====== 批量操作接口（必须在 {task_id} 路由之前定义，避免路由冲突） ======

class BatchTaskRequest(BaseModel):
    ids: list[str]


class FromUrlRequest(BaseModel):
    url: str = ""
    video_url: str = ""
    download_dir: str = ""
    referer: str = ""
    headers: str = ""
    title_selector: str = ""
    m3u8_selector: str = ""
    video_id_pattern: str = ""
    key_selector: str = ""
    iv_selector: str = ""


class FromM3u8Request(BaseModel):
    m3u8_url: str = ""
    url: str = ""
    name: str = ""
    id: str = ""
    headers: str = ""
    key: str = ""
    iv: str = ""
    download_dir: str = ""


class UpdateTaskRequest(BaseModel):
    priority: Optional[int] = None


@router.post("/api/tasks/batch/start")
def batch_start_tasks(body: BatchTaskRequest):
    """批量开始任务"""
    from app.services.downloader import start_download
    cfg = get_download_config()
    max_concurrent = int(cfg.get("max_concurrent", "2"))

    success = []
    failed = []
    queued = []

    for task_id in body.ids:
        try:
            task = get_task(task_id)
            if not task:
                failed.append({"id": task_id, "reason": "任务不存在"})
                continue
            # 同时检查 status 和 stage，防止 moving 阶段被重复启动
            if task["status"] in ("downloading",) or task.get("stage") in ("merging", "moving"):
                failed.append({"id": task_id, "reason": "任务已在运行"})
                continue
            if is_downloading(task_id):
                failed.append({"id": task_id, "reason": "任务已在运行"})
                continue

            if get_active_downloads() >= max_concurrent:
                update_task(task_id, status="waiting", stage="waiting")
                queued.append(task_id)
            else:
                proc = start_download(task_id, task["m3u8_url"], task["headers"],
                                      task.get("key", ""), task.get("iv", ""))
                if proc is None:
                    failed.append({"id": task_id, "reason": "启动下载失败"})
                    continue
                success.append(task_id)
        except Exception as e:
            failed.append({"id": task_id, "reason": str(e)})

    try_start_next()
    return {
        "message": f"成功 {len(success)} 个，排队 {len(queued)} 个，失败 {len(failed)} 个",
        "success": success,
        "queued": queued,
        "failed": failed
    }


@router.post("/api/tasks/batch/stop")
def batch_stop_tasks(body: BatchTaskRequest):
    """批量暂停任务"""
    from app.services.queue import unregister_download, _running

    success = []
    failed = []

    for task_id in body.ids:
        try:
            task = get_task(task_id)
            if not task:
                failed.append({"id": task_id, "reason": "任务不存在"})
                continue

            # 停止进程（统一从 _running 管理）
            proc = _running.get(task_id)
            if proc:
                proc.terminate()
            unregister_download(task_id)

            update_task(task_id, status="stopped", stage="stopped")
            success.append(task_id)
        except Exception as e:
            failed.append({"id": task_id, "reason": str(e)})

    try_start_next()
    return {
        "message": f"成功 {len(success)} 个，失败 {len(failed)} 个",
        "success": success,
        "failed": failed
    }


@router.post("/api/tasks/batch/retry")
def batch_retry_tasks(body: BatchTaskRequest):
    """批量重试失败任务（仅重置状态，m3u8/key刷新由下载器启动时异步完成）"""
    from app.services.queue import unregister_download, _running

    success = []
    failed = []

    for task_id in body.ids:
        try:
            task = get_task(task_id)
            if not task:
                failed.append({"id": task_id, "reason": "任务不存在"})
                continue

            # 停止可能存在的进程
            proc = _running.get(task_id)
            if proc:
                proc.terminate()
            unregister_download(task_id)

            # 如果 video_url 为空，尝试从 source_id 恢复（供下载器刷新用）
            video_url = task.get("video_url", "")
            if not video_url and task.get("source_id"):
                from app.db.database import get_source
                source = get_source(task.get("source_id"))
                if source:
                    refresh_pattern = source.get("refresh_url_pattern", "")
                    if refresh_pattern:
                        video_url = refresh_pattern.replace("{task_id}", task_id)
                        update_task(task_id, video_url=video_url)
                        logger.info(f"[批量重试] {task_id}: 从订阅源配置恢复 video_url")

            reset_task_for_manual_retry(task_id)
            success.append(task_id)
        except Exception as e:
            failed.append({"id": task_id, "reason": str(e)})

    try_start_next()
    return {
        "message": f"成功 {len(success)} 个，失败 {len(failed)} 个",
        "success": success,
        "failed": failed
    }


@router.post("/api/tasks/batch/delete")
def batch_delete_tasks(body: BatchTaskRequest):
    """批量删除任务"""
    from app.services.queue import unregister_download, _running

    success = []
    failed = []

    for task_id in body.ids:
        try:
            # 停止进程
            proc = _running.get(task_id)
            if proc:
                proc.terminate()
            unregister_download(task_id)

            # 清理文件（避免 get_task_dir 创建目录）
            cfg = get_download_config()
            download_dir = cfg.get("download_dir", str(Path.home() / ".dl-manager" / "tasks"))
            task_dir = Path(download_dir) / task_id
            if task_dir.exists():
                shutil.rmtree(task_dir)
            flat_mp4 = Path(download_dir) / f"{task_id}.mp4"
            if flat_mp4.exists():
                flat_mp4.unlink()
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
            success.append(task_id)
        except Exception as e:
            failed.append({"id": task_id, "reason": str(e)})

    return {
        "message": f"成功 {len(success)} 个，失败 {len(failed)} 个",
        "success": success,
        "failed": failed
    }


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
def create_task_from_url(body: FromUrlRequest):
    """只提供 video_url，自动抓取页面标题和 m3u8，生成任务"""
    from app.services.rss_poller import resolve_video_info
    video_url = body.url or body.video_url
    if not video_url:
        raise HTTPException(400, "url or video_url required")

    # 支持传入解析规则（可选），用于通用网页解析
    source_config = {}
    for key in ["referer", "headers", "title_selector", "m3u8_selector",
                "video_id_pattern", "key_selector", "iv_selector"]:
        val = getattr(body, key, "")
        if val:
            source_config[key] = val

    import traceback
    try:
        info = resolve_video_info(video_url, source_config=source_config if source_config else None)
    except Exception as e:
        logger.info(f"[from-url] resolve_video_info failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(502, f"页面解析失败: {e}")
    if not info:
        raise HTTPException(502, f"无法从页面提取 m3u8: {video_url}")
    task = create_task(info["id"], info["name"], info["m3u8_url"],
                       info.get("headers", ""), info.get("key", ""), info.get("iv", ""),
                       download_dir=body.download_dir, video_url=video_url)
    try_start_next()
    return {"data": [task]}


@router.post("/api/tasks/from-m3u8")
def create_task_from_m3u8(body: FromM3u8Request):
    """直接提供 m3u8 URL 创建任务，无需解析页面"""
    import uuid
    m3u8_url = body.m3u8_url or body.url
    if not m3u8_url:
        raise HTTPException(400, "m3u8_url required")
    name = body.name.strip()
    if not name:
        from urllib.parse import urlparse
        parsed = urlparse(m3u8_url)
        path_parts = parsed.path.strip('/').split('/')
        name = path_parts[-1].replace('.m3u8', '') if path_parts else str(uuid.uuid4())[:8]
    task_id = body.id.strip()
    if not task_id:
        task_id = str(uuid.uuid4())[:12]
    task = create_task(task_id, name, m3u8_url, body.headers, body.key, body.iv, download_dir=body.download_dir)
    try_start_next()
    return {"data": [task]}


@router.post("/api/tasks/{task_id}/start")
def start_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    # 同时检查 status 和 stage，防止 moving 阶段被重复启动
    if task["status"] in ("downloading",) or task.get("stage") in ("merging", "moving"):
        raise HTTPException(400, "Task already running")
    if is_downloading(task_id):
        raise HTTPException(400, "Task already running")
    from app.services.queue import get_active_downloads
    from app.db.database import get_download_config
    from app.services.downloader import start_download
    cfg = get_download_config()
    max_concurrent = int(cfg.get("max_concurrent", "2"))
    if get_active_downloads() >= max_concurrent:
        return {"message": f"队列已满（{max_concurrent}），任务已加入等待队列"}
    proc = start_download(task_id, task["m3u8_url"], task["headers"],
                          task["key"], task["iv"])
    if proc is None:
        raise HTTPException(500, "启动下载失败")
    return {"message": "Download started"}


@router.post("/api/tasks/{task_id}/stop")
def stop_task(task_id: str):
    from app.services.queue import unregister_download, _running
    proc = _running.get(task_id)
    if proc:
        proc.terminate()
    unregister_download(task_id)
    update_task(task_id, status="stopped", stage="stopped")
    try_start_next()
    return {"message": "Stopped"}


@router.post("/api/tasks/{task_id}/retry")
def retry_task(task_id: str):
    """手动重试：不受次数限制，重置 retry_count=0（m3u8/key刷新由下载器启动时异步完成）"""
    from app.services.queue import unregister_download, _running

    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    proc = _running.get(task_id)
    if proc:
        proc.terminate()
    unregister_download(task_id)

    # 如果 video_url 为空，尝试从 source_id 恢复（供下载器刷新用）
    video_url = task.get("video_url", "")
    if not video_url and task.get("source_id"):
        from app.db.database import get_source
        source = get_source(task.get("source_id"))
        if source:
            refresh_pattern = source.get("refresh_url_pattern", "")
            if refresh_pattern:
                video_url = refresh_pattern.replace("{task_id}", task_id)
                update_task(task_id, video_url=video_url)
                logger.info(f"[重试] {task_id}: 从订阅源配置恢复 video_url")

    # 手动重试时刷新 m3u8/key/iv（用户主动点击，大概率 key 已过期）
    try:
        from app.services.downloader import refresh_m3u8_url
        fresh = refresh_m3u8_url(task_id)
        if fresh:
            logger.info(f"[重试] {task_id}: key/iv 刷新成功")
        else:
            logger.info(f"[重试] {task_id}: key/iv 刷新失败，使用原有值")
    except Exception as e:
        logger.warning(f"[重试] {task_id}: key/iv 刷新异常 - {e}")

    last_error = reset_task_for_manual_retry(task_id)
    try_start_next()
    return {
        "message": "任务已加入队列（手动重试，不限次数）",
        "last_error": last_error,
        "retry_count": 0
    }


@router.delete("/api/tasks/{task_id}")
def remove_task(task_id: str):
    from app.services.queue import unregister_download, _running
    proc = _running.get(task_id)
    if proc:
        proc.terminate()
    unregister_download(task_id)
    # 避免 get_task_dir 创建目录，直接构造路径
    cfg = get_download_config()
    download_dir = cfg.get("download_dir", str(Path.home() / ".dl-manager" / "tasks"))
    task_dir = Path(download_dir) / task_id
    if task_dir.exists():
        try:
            shutil.rmtree(task_dir)
        except Exception:
            pass
    flat_mp4 = Path(download_dir) / f"{task_id}.mp4"
    if flat_mp4.exists():
        try:
            flat_mp4.unlink()
        except Exception:
            pass
    # 清理 temp_dir 中的残留
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
        try:
            log_path.unlink()
        except Exception:
            pass
    delete_task(task_id)
    return {"message": "Deleted"}


@router.get("/api/tasks/{task_id}/logs")
async def task_logs(task_id: str):
    log_path = get_task_log_path(task_id)

    async def event_generator():
        # 等待日志文件创建（最多等待 30 秒）
        wait_count = 0
        while not log_path.exists() and wait_count < 60:
            await asyncio.sleep(0.5)
            wait_count += 1
        
        if not log_path.exists():
            yield "data: 日志文件不存在（任务可能尚未开始下载）\n\n"
            return
        
        # 打开日志文件，从末尾开始监听
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            f.seek(0, 2)  # 跳到文件末尾
            while True:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.5)
                    # 检查文件是否被删除
                    if not log_path.exists():
                        yield "data: [日志文件已清理]\n\n"
                        break
                    continue
                yield f"data: {line.rstrip()}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/api/tasks/{task_id}/logs/history")
def task_logs_history(task_id: str, page: int = 1, page_size: int = 100, search: str = ""):
    """获取任务历史日志（分页）"""
    log_path = get_task_log_path(task_id)
    if not log_path.exists():
        return {"list": [], "total": 0, "page": page, "page_size": page_size}
    
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except:
        return {"list": [], "total": 0, "page": page, "page_size": page_size}
    
    # 反转：最新的在前面；过滤空行
    lines = [l.rstrip() for l in reversed(lines) if l.strip()]
    
    # 搜索过滤
    if search:
        lines = [l for l in lines if search.lower() in l.lower()]
    
    total = len(lines)
    # 页码越界保护
    max_page = max(1, (total + page_size - 1) // page_size)
    if page > max_page:
        page = max_page
    start = (page - 1) * page_size
    end = start + page_size
    
    return {
        "list": lines[start:end],
        "total": total,
        "page": page,
        "page_size": page_size
    }


@router.post("/api/start-waiting")
def start_waiting_tasks():
    count = try_start_next()
    return {"message": f"已启动 {count} 个任务", "count": count}


@router.patch("/api/tasks/{task_id}")
def update_task_info(task_id: str, body: UpdateTaskRequest):
    """更新任务信息（目前支持优先级）"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    updates = {}
    if body.priority is not None:
        priority = max(-100, min(100, body.priority))
        updates["priority"] = priority

    if not updates:
        raise HTTPException(400, "No valid fields to update")

    update_task(task_id, **updates)
    return {"message": "Updated", "data": get_task(task_id)}

