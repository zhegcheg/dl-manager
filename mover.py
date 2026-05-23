"""
文件转移逻辑（异步）
"""
import subprocess
import time
import threading
from pathlib import Path
from task_db import update_task

MEDIA_DIR = Path("/mnt/fn-nas-imovie")

def _do_copy(task_id: str, src: Path, dest: Path):
    """后台复制线程"""
    try:
        subprocess.run(["cp", str(src), str(dest)], check=True, timeout=600)
        update_task(task_id, stage="moving", progress=100, move_speed="done", final_path=str(dest))
        src.unlink()  # 复制完成，删除源文件
    except Exception as e:
        update_task(task_id, error=f"转移失败: {e}")

def move_to_media_library(task_id: str, file_path: str, final_name: str = None, on_done=None) -> tuple[bool, str]:
    """
    后台异步移动文件到媒体库
    返回: (True, "已启动")
    """
    src = Path(file_path)
    if not src.exists():
        return False, f"Source file not found: {file_path}"
    
    dest_dir = MEDIA_DIR
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    if final_name:
        dest_name = final_name
    else:
        dest_name = src.name
    # CIFS 文件名上限约 255 字符，截断到 120 安全
    base, ext = os.path.splitext(dest_name)
    dest_name = base[:120] + ext
    
    dest = dest_dir / dest_name
    
    # 启动后台复制线程
    t = threading.Thread(target=_do_copy, args=(task_id, src, dest), daemon=True)
    t.start()
    
    # 启动进度追踪线程
    def track():
        start = time.time()
        while True:
            try:
                if not dest.exists():
                    time.sleep(2)
                    continue
                size_mb = dest.stat().st_size / (1024*1024)
                elapsed = time.time() - start
                if size_mb > 0 and elapsed > 0:
                    speed = size_mb / elapsed
                    progress = min(int(size_mb * 100 / (src.stat().st_size / (1024*1024))), 99)
                    update_task(task_id,
                              stage="moving",
                              progress=progress,
                              move_speed=f"{speed:.1f}MB/s",
                              move_elapsed=f"{int(elapsed)}s")
                if size_mb >= src.stat().st_size / (1024*1024) - 1:
                    update_task(task_id, move_speed="done")
                    if on_done:
                        on_done()
                    break
            except Exception:
                pass
            time.sleep(2)
    
    tt = threading.Thread(target=track, daemon=True)
    tt.start()
    
    return True, "复制已启动"
