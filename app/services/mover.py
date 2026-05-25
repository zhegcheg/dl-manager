"""
文件转移逻辑（异步）- 跨平台方案
"""
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from app.db.database import update_task, get_task
from app.db.database import get_download_config

MEDIA_DIR = Path(os.getenv("NAS_MEDIA_DIR", "/mnt/fn-nas-imovie"))

def _cleanup_source_file(task_id: str):
    """转移完成后清理源文件和空的订阅源子目录"""
    t = get_task(task_id)
    if not t:
        return
    # 删除已转移的源文件
    src_file = t.get("file", "")
    if src_file and Path(src_file).exists():
        try:
            Path(src_file).unlink()
        except Exception:
            pass
    # 如果订阅源子目录为空，一并清理
    dl_dir = t.get("download_dir", "")
    if dl_dir:
        dl_path = Path(dl_dir)
        if dl_path.exists():
            try:
                # 仅当目录为空时才删除（避免误删其他文件）
                if not any(dl_path.iterdir()):
                    dl_path.rmdir()
            except Exception:
                pass


def _do_copy(task_id: str, src: Path, dest: Path):
    """后台复制线程 - 跨平台方案"""
    import sys
    try:
        # 如果目标文件已存在，比较大小：谁大保留谁
        if dest.exists():
            dest_size = dest.stat().st_size
            src_size = src.stat().st_size
            if dest_size >= src_size:
                src.unlink()
                update_task(task_id, status="completed", stage="completed",
                           progress=100, move_speed="skipped (目标更大)")
                return
            else:
                dest.unlink()

        total_size = src.stat().st_size
        start = time.time()

        # Windows: 使用 Python 原生复制
        if sys.platform == 'win32':
            _copy_with_progress(task_id, src, dest, total_size, start)
        else:
            # Linux: 使用 dd 命令
            _copy_with_dd(task_id, src, dest, total_size, start)

    except Exception as e:
        update_task(task_id, error=f"转移失败: {e}")


def _copy_with_progress(task_id: str, src: Path, dest: Path, total_size: int, start: float):
    """Python 原生复制，带进度汇报"""
    chunk_size = 4 * 1024 * 1024  # 4MB
    copied = 0
    
    with open(src, 'rb') as fsrc, open(dest, 'wb') as fdst:
        while True:
            data = fsrc.read(chunk_size)
            if not data:
                break
            fdst.write(data)
            copied += len(data)
            elapsed = time.time() - start
            progress = min(int(copied * 100 / total_size), 99)
            speed_mbps = copied / (1024 * 1024) / elapsed if elapsed > 0 else 0
            update_task(task_id,
                       stage="moving",
                       progress=progress,
                       move_speed=f"{speed_mbps:.1f}MB/s",
                       move_elapsed=f"{int(elapsed)}s")

    # 复制完成
    update_task(task_id, status="completed", stage="completed",
               progress=100, move_speed="done", final_path=str(dest))
    src.unlink()
    _cleanup_source_file(task_id)


def _copy_with_dd(task_id: str, src: Path, dest: Path, total_size: int, start: float):
    """Linux cp 命令复制，带进度汇报（解决 dd stdout 管道丢失问题）"""
    proc = subprocess.Popen(
        ['cp', '--reflink=auto', str(src), str(dest)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True
    )

    # 后台线程监控进度（cp 不支持 progress 输出，靠轮询文件大小）
    def monitor_progress():
        last_size = 0
        while proc.poll() is None:
            try:
                if dest.exists():
                    cur_size = dest.stat().st_size
                    if cur_size != last_size:
                        elapsed = time.time() - start
                        speed_mbps = cur_size / (1024 * 1024) / elapsed if elapsed > 0 else 0
                        progress = min(int(cur_size * 100 / total_size), 99)
                        update_task(task_id,
                                   stage="moving",
                                   progress=progress,
                                   move_speed=f"{speed_mbps:.1f}MB/s",
                                   move_elapsed=f"{int(elapsed)}s")
                        last_size = cur_size
                    time.sleep(0.5)
            except Exception:
                pass

    monitor_thread = threading.Thread(target=monitor_progress, daemon=True)
    monitor_thread.start()

    proc.wait()
    stdout, stderr = proc.communicate()

    if proc.returncode != 0:
        if dest.exists():
            dest.unlink()
        update_task(task_id, error=f"转移失败: cp exit={proc.returncode}\n{stderr[:200]}")
        return

    update_task(task_id, status="completed", stage="completed",
               progress=100, move_speed="done", final_path=str(dest))
    src.unlink()
    _cleanup_source_file(task_id)


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

    dest = dest_dir / dest_name

    # CIFS 文件名上限 255 字节（单个文件名组件，不含路径）
    name_bytes = len(dest_name.encode('utf-8'))
    if name_bytes > 250:  # 留 5 字节安全余量
        base, ext = os.path.splitext(dest_name)
        ext_bytes = len(ext.encode('utf-8'))
        max_base_bytes = 250 - ext_bytes
        # 按字符逐个截断，避免破坏多字节 UTF-8 字符
        truncated = ""
        current_bytes = 0
        for ch in base:
            ch_bytes = len(ch.encode('utf-8'))
            if current_bytes + ch_bytes > max_base_bytes:
                break
            truncated += ch
            current_bytes += ch_bytes
        dest_name = truncated + ext
        dest = dest_dir / dest_name

    # 启动后台复制线程
    t = threading.Thread(target=_do_copy, args=(task_id, src, dest), daemon=True)
    t.start()

    return True, "复制已启动"