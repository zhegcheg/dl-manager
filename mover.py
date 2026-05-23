"""
文件转移逻辑
"""
import shutil
import time
import threading
import re
from pathlib import Path
from task_db import get_task, update_task

MEDIA_DIR = Path("/mnt/fn-nas-imovie")

def move_to_media_library(task_id: str, file_path: str, final_name: str = None) -> tuple[bool, str]:
    """
    移动文件到媒体库
    返回: (成功, 最终路径 或 错误信息)
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
    
    dest = dest_dir / dest_name
    
    # 移动文件（带进度）
    def track_progress(src_path, dest_path):
        start = time.time()
        last_mb = 0
        while True:
            try:
                if not dest.exists():
                    break
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
                    break
            except Exception:
                pass
            time.sleep(1)
    
    t = threading.Thread(target=track_progress, args=(src, dest), daemon=True)
    t.start()
    
    try:
        shutil.copy2(src, dest)
        update_task(task_id, stage="moving", progress=100, move_speed="done", final_path=str(dest))
        src.unlink()  # 删除源文件
        return True, str(dest)
    except Exception as e:
        return False, str(e)