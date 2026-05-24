"""
下载队列管理器：控制并发数，自动启动等待中的任务
"""
import threading
import time
from task_db import get_task, list_tasks, update_task, get_download_config



_running = {}  # task_id -> proc
_order = []    # task_id 启动顺序（用于按序停止）
_lock = threading.Lock()

def get_active_downloads() -> int:
    """返回当前活跃下载数"""
    with _lock:
        return len(_running)

def is_downloading(task_id: str) -> bool:
    with _lock:
        return task_id in _running

def register_download(task_id: str, proc):
    with _lock:
        _running[task_id] = proc
        if task_id not in _order:
            _order.append(task_id)

def unregister_download(task_id: str):
    with _lock:
        _running.pop(task_id, None)
        if task_id in _order:
            _order.remove(task_id)
    # 锁外调用，避免死锁
    try_start_next()

def try_start_next() -> int:
    """
    尝试启动下一个等待中的任务（不超过 max_concurrent）
    还会检测卡死的 downloading 任务并重置它们
    返回: 启动了几个任务
    """
    cfg = get_download_config()
    max_concurrent = int(cfg.get("max_concurrent", "2"))

    started = 0

    # 先清理已死的进程 + 检测卡死任务
    with _lock:
        dead = [tid for tid, proc in _running.items() if proc.poll() is not None]
    for tid in dead:
        _running.pop(tid, None)
        # 检测：进程死了但DB里还是 downloading → 卡死
        t = get_task(tid)
        if t and t["status"] == "downloading":
            retry_count = t.get("retry_count", 0)
            if retry_count < 3:
                # 重试：重置状态+启动
                update_task(tid, status="waiting", stage="waiting", progress=0,
                           speed="", segments="", error="", retry_count=retry_count + 1)
                started += 1
            else:
                # 超过3次，不自动重试，等待人工处理
                update_task(tid, status="failed", stage="failed", error="下载进程异常退出，自动重试3次后放弃")

    with _lock:
        active = len(_running)

    if active >= max_concurrent:
        return started

    # 找 waiting 任务，按创建时间正序
    waiting = list_tasks(status="waiting", limit=50)
    for task in waiting:
        if is_downloading(task["id"]):
            continue
        # 启动下载
        from ntrh_downloader import start_download
        proc = start_download(
            task["id"],
            task["m3u8_url"],
            task["headers"],
            task["key"],
            task["iv"]
        )
        register_download(task["id"], proc)
        started += 1
        with _lock:
            active += 1
        if active >= max_concurrent:
            break

    return started

def apply_max_concurrent(new_limit: int) -> int:
    """
    应用新的最大并发数。停掉最后启动的多余任务。
    返回: 停止的任务数
    """
    stopped = 0
    tids = []
    with _lock:
        excess = max(0, len(_running) - new_limit)
        if excess <= 0:
            return 0
        all_tids = list(_running.keys())
        tids = all_tids[-excess:]
    for tid in tids:
        with _lock:
            proc = _running.pop(tid, None)
            if tid in _order:
                _order.remove(tid)
        if proc:
            try:
                proc.terminate()
            except:
                pass
        update_task(tid, status="stopped", stage="stopped")
        stopped += 1
    return stopped


def cleanup_finished():
    """服务启动时恢复状态：重置卡死任务、恢复合并/转移"""
    with _lock:
        dead = [tid for tid, proc in _running.items() if proc.poll() is not None]
    for tid in dead:
        unregister_download(tid)
    
    from task_db import list_tasks, update_task, get_task, get_download_config
    from pathlib import Path
    import shutil
    
    for t in list_tasks():
        tid = t["id"]
        
        # 下载中 → 重置为 waiting（进程已死）
        if t["status"] == "downloading":
            update_task(tid, status="waiting", stage="waiting", progress=0,
                       speed="", segments="", error="")
            print(f"[恢复] {tid}: 重置 downloading → waiting")
        
        # 转移中且已完成复制（move=done）→ 检查 final_path 并标记完成
        elif t["stage"] == "moving" and t.get("move_speed") == "done":
            fp = t.get("final_path", "")
            if fp and Path(fp).exists():
                update_task(tid, stage="completed", progress=100)
                print(f"[恢复] {tid}: 转移已完成，标记完成")
            else:
                # 没有 final_path 或文件不存在，重新转移
                update_task(tid, stage="moving", progress=0, move_speed="", move_elapsed="")
                print(f"[恢复] {tid}: 重新启动转移")
        
        # 转移中（未完成）→ 重新启动转移
        elif t["stage"] == "moving" and not t.get("move_speed") == "done":
            cfg = get_download_config()
            download_dir = cfg.get("download_dir", "")
            mp4_path = Path(download_dir) / f"{tid}.mp4"
            if mp4_path.exists():
                from mover import move_to_media_library
                name = t.get("name", tid) or tid
                update_task(tid, stage="moving", progress=0, move_speed="", move_elapsed="")
                move_to_media_library(tid, str(mp4_path), name + ".mp4")
                print(f"[恢复] {tid}: 重启转移")
            else:
                # 本地 mp4 不存在（可能已删除），标记失败
                update_task(tid, status="failed", stage="failed", error="转移中断：源文件不存在")
                print(f"[恢复] {tid}: 源文件不存在，标记失败")
        
        # 合并中 → 尝试重新合并
        elif t["stage"] == "merging" or (t["status"] == "failed" and "合并" in t.get("error", "")):
            from merger import merge_ts_to_mp4
            cfg = get_download_config()
            download_dir = cfg.get("download_dir", "")
            task_dir = Path(download_dir) / tid
            
            # 搜索分片目录
            seg_dir = None
            for d in [task_dir / "0____", task_dir / tid / "0____"]:
                if d.exists():
                    seg_dir = d
                    break
            if not seg_dir:
                if task_dir.exists():
                    for sub in sorted(task_dir.iterdir()):
                        if sub.is_dir():
                            check = sub / "0____"
                            if check.exists():
                                seg_dir = check
                                break
            
            if seg_dir:
                ts_files = list(seg_dir.glob("[0-9]*.ts"))
                if ts_files:
                    print(f"[恢复] {tid}: 发现 {len(ts_files)} 片，尝试合并")
                    update_task(tid, stage="merging", progress=0)
                    flat = Path(download_dir) / f"{tid}.mp4"
                    ok, result = merge_ts_to_mp4(seg_dir, tid, flat)
                    if ok:
                        if flat.exists() and flat.stat().st_size > 0:
                            update_task(tid, status="completed", stage="completed", progress=100, file=str(flat))
                            if task_dir.exists():
                                shutil.rmtree(task_dir, ignore_errors=True)
                            print(f"[恢复] {tid}: 合并成功")
                            
                            # 合并后尝试转移
                            name = t.get("name", tid) or tid
                            from mover import move_to_media_library
                            update_task(tid, stage="moving", progress=0)
                            move_to_media_library(tid, str(flat), name + ".mp4")
                            print(f"[恢复] {tid}: 启动转移")
                        else:
                            update_task(tid, status="failed", stage="failed", error=f"合并后文件不存在")
                    else:
                        update_task(tid, status="failed", stage="failed", error=f"合并失败: {result}")
                else:
                    update_task(tid, status="failed", stage="failed", error="分片不存在")
            else:
                update_task(tid, status="failed", stage="failed", error="分片目录不存在")