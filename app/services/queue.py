"""
下载队列管理器
- 优先级队列（priority DESC, created_at ASC）
- 智能重试（有进度→同URL重试，无进度→刷新URL重试，最多3次）
- 并发控制
"""
import logging
import threading
import time
from datetime import datetime, timedelta
from app.db.database import get_task, list_tasks, update_task, get_download_config

logger = logging.getLogger("dl-manager")


_running = {}  # task_id -> proc/thread
_order = []    # task_id 启动顺序（用于按序停止）
_lock = threading.Lock()

# 指数退避重试时间表（秒）
RETRY_BACKOFF = [60, 300, 900]  # 1min, 5min, 15min

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
    - 按优先级排序（priority DESC, created_at ASC）
    - 智能重试（指数退避）
    返回: 启动了几个任务
    """
    cfg = get_download_config()
    max_concurrent = int(cfg.get("max_concurrent", "2"))

    started = 0

    # 先清理已完成的线程 + 检测卡死任务
    with _lock:
        dead = [tid for tid, proc in _running.items() if proc.poll() is not None]
        for tid in dead:
            _running.pop(tid, None)
            if tid in _order:
                _order.remove(tid)
    for tid in dead:
        t = get_task(tid)
        if not t:
            continue

        # 进程死亡但状态仍是 downloading → 外部杀死（重启/手动停），不算失败
        if t["status"] == "downloading":
            logger.info(f"[队列] {tid}: 进程外部终止（状态仍为 downloading），直接重试")
            update_task(tid, status="waiting", stage="waiting",
                        speed="", segments="", error="")
            started += 1
            continue

        # 状态为 failed → yt-dlp 报错（401/超时等），启动自动重试流程
        if t["status"] != "failed":
            continue

        # 检查是否是自动重试触发的失败（error 含“自动重试”字样）
        # 或普通失败 → 统一走自动重试逻辑
        error_msg = t.get("error", "")

        # 只对有 video_url 的任务启用自动重试（才能刷新 URL）
        video_url = t.get("video_url", "")
        if not video_url and not t.get("source_id"):
            # 无法刷新 URL 的失败任务，保持 failed 状态
            continue

        retry_count = t.get("retry_count", 0) + 1

        if retry_count > 3:
            # 3 次自动重试全部失败
            update_task(tid, error=f"自动重试 3 次后放弃: {error_msg}")
            logger.warning(f"[队列] {tid}: 自动重试 3 次后放弃")
            continue

        # 刷新 URL：从 video_url 重新获取 m3u8/key/iv
        logger.info(f"[队列] {tid}: 第 {retry_count}/3 次自动重试，刷新 URL")
        try:
            from app.services.downloader import refresh_m3u8_url
            fresh = refresh_m3u8_url(tid)
            if fresh:
                logger.info(f"[队列] {tid}: URL 刷新成功")
            else:
                logger.warning(f"[队列] {tid}: URL 刷新失败，使用缓存")
        except Exception as e:
            logger.warning(f"[队列] {tid}: URL 刷新异常 - {e}")

        backoff = RETRY_BACKOFF[min(retry_count - 1, len(RETRY_BACKOFF) - 1)]
        retry_after = (datetime.utcnow() + timedelta(seconds=backoff)).isoformat() + "Z"
        update_task(tid, status="waiting", stage="waiting", progress=0,
                    speed="", segments="", error="",
                    retry_count=retry_count, retry_after=retry_after)
        started += 1

    with _lock:
        active = len(_running)

    if active >= max_concurrent:
        return started

    # 找 waiting 任务，按优先级排序（priority DESC, created_at ASC）
    waiting = list_tasks(status="waiting", limit=50, order_by="priority DESC, created_at ASC")
    for task in waiting:
        if is_downloading(task["id"]):
            continue
        
        # 检查是否在退避时间内（智能重试）
        if not _can_start_now(task):
            continue
        
        # 启动下载
        from app.services.downloader import start_download
        try:
            proc = start_download(
                task["id"],
                task["m3u8_url"],
                task["headers"],
                task["key"],
                task["iv"]
            )
            if proc is None:
                # start_download 内部已标记失败，跳过
                continue
            register_download(task["id"], proc)
            started += 1
            with _lock:
                active += 1
            if active >= max_concurrent:
                break
        except Exception as e:
            # 启动失败，标记为失败并继续
            update_task(task["id"], status="failed", stage="failed", error=f"启动下载失败: {e}")
            logger.error(f"[try_start_next] Failed to start {task['id']}: {e}")
            continue

    return started


def _can_start_now(task: dict) -> bool:
    """检查任务现在是否可以开始（考虑退避时间）"""
    retry_count = task.get("retry_count", 0)
    if retry_count == 0:
        return True  # 首次尝试，无需等待
    
    retry_after = task.get("retry_after")
    if not retry_after:
        return True  # 没有退避时间，可以开始
    
    try:
        retry_time = datetime.fromisoformat(retry_after.replace("Z", "+00:00"))
        now = datetime.utcnow().replace(tzinfo=retry_time.tzinfo)
        return now >= retry_time
    except (ValueError, AttributeError):
        return True  # 解析失败，允许开始

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
    """服务启动时恢复状态：重置卡死任务、恢复合并/转移（非阻塞）"""
    with _lock:
        dead = [tid for tid, proc in _running.items() if proc.poll() is not None]
    for tid in dead:
        unregister_download(tid)
    
    from app.db.database import list_tasks, update_task, get_task, get_download_config
    from pathlib import Path
    import shutil
    
    all_tasks = list_tasks()
    need_recover = []
    
    for t in all_tasks:
        tid = t["id"]
        
        # 下载中 → 重置为 waiting（进程已死，yt-dlp 支持断点续传）
        if t["status"] == "downloading":
            update_task(tid, status="waiting", stage="waiting", progress=0,
                       speed="", segments="", error="")
            logger.info(f"[恢复] {tid}: 下载中断 → 重置为 waiting（将通过 try_start_next 自动续传）")
        
        # 转移中且已完成复制（move_speed=done）→ 检查 final_path 并标记完成
        elif t["stage"] == "moving" and t.get("move_speed") == "done":
            fp = t.get("final_path", "")
            if fp and Path(fp).exists():
                update_task(tid, stage="completed", progress=100)
                logger.info(f"[恢复] {tid}: 转移已完成，标记完成")
            else:
                # final_path 不存在，尝试从 download_dir 重新转移
                need_recover.append(("move", t))
                logger.warning(f"[恢复] {tid}: 转移标记完成但目标文件缺失，加入重新转移队列")
        
        # 转移中（未完成）→ 加入恢复队列
        elif t["stage"] == "moving":
            need_recover.append(("move", t))
            logger.warning(f"[恢复] {tid}: 转移中断，加入恢复队列")
        
        # 合并中（含 re-encode）→ 加入恢复队列
        elif t["stage"] in ("merging", "merging_reencode"):
            need_recover.append(("merge", t))
            logger.warning(f"[恢复] {tid}: 合并中断(stage={t['stage']})，加入恢复队列")
        
        # 合并失败 → 也尝试恢复
        elif t["status"] == "failed" and "合并" in t.get("error", ""):
            need_recover.append(("merge", t))
            logger.info(f"[恢复] {tid}: 合并失败，加入恢复队列重试")
    
    # 非阻塞恢复：在后台线程中执行耗时的合并/转移操作
    if need_recover:
        t = threading.Thread(target=_recover_tasks, args=(need_recover,), daemon=True)
        t.start()
        logger.info(f"[恢复] 已启动后台恢复线程，共 {len(need_recover)} 个任务待恢复")


def _recover_tasks(recover_list: list):
    """后台线程：逐个恢复合并/转移任务（避免阻塞启动）"""
    import shutil
    from pathlib import Path
    from app.db.database import update_task, get_download_config
    from app.services.merger import merge_ts_to_mp4
    from app.services.mover import move_to_media_library
    
    for action, t in recover_list:
        tid = t["id"]
        try:
            if action == "merge":
                _recover_merge(tid, t)
            elif action == "move":
                _recover_move(tid, t)
        except Exception as e:
            logger.error(f"[恢复] {tid}: 恢复失败 - {e}")
            update_task(tid, status="failed", stage="failed", error=f"恢复失败: {e}")


def _recover_merge(tid: str, t: dict):
    """恢复合并任务
    路径解析优先级：task["download_dir"] > 全局配置
    """
    import shutil
    from pathlib import Path
    from app.db.database import update_task, get_download_config
    from app.services.merger import merge_ts_to_mp4
    from app.services.mover import move_to_media_library
    
    # 优先使用任务记录中的 download_dir（含订阅源子目录），fallback 到全局配置
    download_dir = t.get("download_dir", "")
    if not download_dir:
        cfg = get_download_config()
        download_dir = cfg.get("download_dir", "")
    
    task_dir = Path(download_dir) / tid
    
    # 搜索分片目录
    seg_dir = None
    for d in [task_dir / "0____", task_dir / tid / "0____"]:
        if d.exists():
            seg_dir = d
            break
    if not seg_dir and task_dir.exists():
        for sub in sorted(task_dir.iterdir()):
            if sub.is_dir():
                check = sub / "0____"
                if check.exists():
                    seg_dir = check
                    break
    
    if not seg_dir:
        update_task(tid, status="failed", stage="failed", error="分片目录不存在，无法恢复合并")
        logger.error(f"[恢复] {tid}: 分片目录不存在")
        return
    
    ts_files = list(seg_dir.glob("[0-9]*.ts"))
    if not ts_files:
        update_task(tid, status="failed", stage="failed", error="分片文件不存在，无法恢复合并")
        logger.error(f"[恢复] {tid}: 无 TS 分片文件")
        return
    
    logger.info(f"[恢复] {tid}: 发现 {len(ts_files)} 个分片，开始合并...")
    update_task(tid, stage="merging", progress=0)
    flat = Path(download_dir) / f"{tid}.mp4"
    ok, result = merge_ts_to_mp4(seg_dir, tid, flat)
    
    if ok and flat.exists() and flat.stat().st_size > 0:
        update_task(tid, status="completed", stage="completed", progress=100, file=str(flat))
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)
        logger.info(f"[恢复] {tid}: 合并成功")
        
        # 合并后尝试转移
        name = t.get("name", tid) or tid
        update_task(tid, stage="moving", progress=0)
        move_to_media_library(tid, str(flat), name + ".mp4")
        logger.info(f"[恢复] {tid}: 启动转移")
    else:
        update_task(tid, status="failed", stage="failed", error=f"合并失败: {result}")
        logger.error(f"[恢复] {tid}: 合并失败 - {result}")


def _recover_move(tid: str, t: dict):
    """恢复转移任务
    路径解析优先级：task["file"] > task["download_dir"]/tid.mp4 > task["temp_dir"]/tid.mp4 > 全局配置
    """
    import shutil
    from pathlib import Path
    from app.db.database import update_task, get_download_config
    from app.services.mover import move_to_media_library
    
    mp4_path = None

    # 1. 优先使用 task["file"]（下载完成时记录的完整路径）
    task_file = t.get("file", "")
    if task_file and Path(task_file).exists():
        mp4_path = Path(task_file)

    # 2. 尝试 task["download_dir"] / tid.mp4
    if not mp4_path:
        dl_dir = t.get("download_dir", "")
        if dl_dir:
            candidate = Path(dl_dir) / f"{tid}.mp4"
            if candidate.exists():
                mp4_path = candidate

    # 3. 尝试 task["temp_dir"] / tid.mp4（可能在移动到 download_dir 前崩溃）
    if not mp4_path:
        temp_dir = t.get("temp_dir", "")
        if temp_dir:
            candidate = Path(temp_dir) / f"{tid}.mp4"
            if candidate.exists():
                # 先移到 download_dir
                dl_dir = t.get("download_dir", "")
                if not dl_dir:
                    cfg = get_download_config()
                    dl_dir = cfg.get("download_dir", "")
                if dl_dir:
                    dest = Path(dl_dir) / f"{tid}.mp4"
                    Path(dl_dir).mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.move(str(candidate), str(dest))
                    except Exception:
                        shutil.copy2(str(candidate), str(dest))
                        candidate.unlink()
                    mp4_path = dest
                    logger.info(f"[恢复] {tid}: 从 temp_dir 恢复到 download_dir")

    # 4. 最后 fallback 到全局配置
    if not mp4_path:
        cfg = get_download_config()
        download_dir = cfg.get("download_dir", "")
        temp_dir_global = cfg.get("temp_dir", "")
        candidate = Path(download_dir) / f"{tid}.mp4"
        if candidate.exists():
            mp4_path = candidate
        elif temp_dir_global:
            temp_mp4 = Path(temp_dir_global) / f"{tid}.mp4"
            if temp_mp4.exists():
                dest = Path(download_dir) / f"{tid}.mp4"
                try:
                    shutil.move(str(temp_mp4), str(dest))
                except Exception:
                    shutil.copy2(str(temp_mp4), str(dest))
                    temp_mp4.unlink()
                mp4_path = dest
                logger.info(f"[恢复] {tid}: 从全局 temp_dir 恢复到 download_dir")

    if mp4_path and mp4_path.exists():
        name = t.get("name", tid) or tid
        update_task(tid, stage="moving", progress=0, move_speed="", move_elapsed="",
                   file=str(mp4_path))
        move_to_media_library(tid, str(mp4_path), name + ".mp4")
        logger.info(f"[恢复] {tid}: 重启转移")
    else:
        update_task(tid, status="failed", stage="failed", error="转移中断：源文件不存在")
        logger.error(f"[恢复] {tid}: 本地 mp4 不存在，标记失败")