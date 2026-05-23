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
                from ntrh_downloader import start_download
                proc = start_download(tid, t["m3u8_url"], t["headers"],
                                      t["key"], t["iv"])
                register_download(tid, proc)
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
    """清理已结束的进程记录，并重置 DB 中卡死的 downloading 任务"""
    with _lock:
        dead = [tid for tid, proc in _running.items() if proc.poll() is not None]
    for tid in dead:
        unregister_download(tid)
    
    # 服务重启时：所有 status=downloading 的任务视为卡死，重置为 waiting
    from task_db import list_tasks, update_task
    for t in list_tasks():
        if t["status"] == "downloading":
            update_task(t["id"], status="waiting", stage="waiting", progress=0,
                       speed="", segments="", error="")
            print(f"[cleanup] reset stuck task {t['id']} to waiting")