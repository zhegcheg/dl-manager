"""
文件转移逻辑（异步）- dd 方案
"""
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from task_db import update_task
from task_db import get_download_config

MEDIA_DIR = Path("/mnt/fn-nas-imovie")

def _do_copy(task_id: str, src: Path, dest: Path):
    """后台复制线程 - dd 方案"""
    try:
        # 如果目标文件已存在，比较大小：谁大保留谁
        if dest.exists():
            dest_size = dest.stat().st_size
            src_size = src.stat().st_size
            if dest_size >= src_size:
                # 目标更大或一样，保留目标，删除源文件
                src.unlink()
                update_task(task_id, status="completed", stage="completed",
                           progress=100, move_speed="skipped (目标更大)")
                return
            else:
                # 源文件更大，删除目标重新复制
                dest.unlink()

        # 启动 dd 进程，status=progress 输出进度
        proc = subprocess.Popen(
            ["dd", "if=" + str(src), "of=" + str(dest), "bs=4M", "status=progress"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )

        # 读取 dd 输出，解析进度
        # dd status=progress 输出格式：
        # "196608000 bytes (197 MB, 188 MiB) copied, 4 s, 49.0 MB/s"
        total_size = src.stat().st_size
        start = time.time()

        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            if not line:
                continue
            # dd status=progress 输出格式（中文/英文两种）：
            #   1048576字节（1.0 MB，1.0 MiB）已复制，0.000522839 s，2.0 GB/s
            #   1048576 bytes (1.0 MB, 1.0 MiB) copied, 0.000429594 s, 2.4 GB/s
            copied_bytes = 0
            m = re.search(r'([\d.]+)\s*字节', line)  # 中文: 1048576字节
            if m:
                copied_bytes = float(m.group(1))
            else:
                m = re.search(r'([\d.]+)\s*bytes', line, re.IGNORECASE)  # 英文: 1048576 bytes
                if m:
                    copied_bytes = float(m.group(1))
            if m:
                copied_bytes = float(m.group(1))
                elapsed = time.time() - start
                progress = min(int(copied_bytes * 100 / total_size), 99)
                speed_mbps = copied_bytes / (1024 * 1024) / elapsed if elapsed > 0 else 0
                update_task(task_id,
                           stage="moving",
                           progress=progress,
                           move_speed=f"{speed_mbps:.1f}MB/s",
                           move_elapsed=f"{int(elapsed)}s")

        proc.wait()
        if proc.returncode != 0:
            # dd 失败，清理目标文件
            if dest.exists():
                dest.unlink()
            update_task(task_id, error=f"转移失败: dd exit={proc.returncode}")
            return

        # dd 成功，更新状态为 completed
        update_task(task_id, status="completed", stage="completed",
                   progress=100, move_speed="done", final_path=str(dest))
        # 清理源文件（flat path）和源任务目录
        src.unlink()
        cfg = get_download_config()
        download_dir = cfg.get("download_dir", str(Path.home() / ".jable-dl-server" / "tasks"))
        task_dir = Path(download_dir) / task_id
        if task_dir.exists():
            import shutil
            shutil.rmtree(task_dir, ignore_errors=True)

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

    # CIFS 文件名上限约 255 字符，截断到 120 安全（base+ext 都要截断）
    base, ext = os.path.splitext(dest_name)
    if len(dest_name) > 200:
        dest_name = base[:120] + ext

    dest = dest_dir / dest_name

    # 启动后台复制线程
    t = threading.Thread(target=_do_copy, args=(task_id, src, dest), daemon=True)
    t.start()

    return True, "复制已启动"