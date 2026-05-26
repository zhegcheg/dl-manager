"""
文件转移逻辑（异步）- 跨平台方案
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from app.db.database import update_task, get_task
from app.db.database import get_download_config

MEDIA_DIR = Path(os.getenv("NAS_MEDIA_DIR", "/mnt/fn-nas-imovie"))

def _get_media_dir():
    """运行时从 DB 读取 NAS 转移路径，fallback 到环境变量"""
    try:
        cfg = get_download_config()
        dest = cfg.get("nas_dest_dir", "").strip()
        if dest:
            return Path(dest)
    except Exception:
        pass
    return MEDIA_DIR

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
            # Linux: 使用 pv (Pipe Viewer) 命令，自带精确进度输出
            _copy_with_pv(task_id, src, dest, total_size, start)

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


# pv 进度行正则: 匹配 elapsed_time 和 transfer_rate 和 percentage
# 示例: " 1.2GiB 0:00:12 [ 105MiB/s] [======>          ] 35% ETA 0:00:22"
_PV_PROGRESS_RE = re.compile(
    r'(\d+:\d+:\d+)\s+'          # elapsed time
    r'\[\s*([\d.]+\s*\S+/s)\]\s+' # transfer rate
    r'\[.*?\]\s*'                  # progress bar
    r'(\d+)%'                      # percentage
)


def _copy_with_pv(task_id: str, src: Path, dest: Path, total_size: int, start: float):
    """Linux pv 命令复制，自带精确进度输出（百分比、速度、ETA）"""
    proc = subprocess.Popen(
        ['pv', '-pterab', '-s', str(total_size), str(src)],
        stdout=open(dest, 'wb'),
        stderr=subprocess.PIPE,
    )

    # 实时解析 pv 的 stderr 进度输出
    for raw_line in proc.stderr:
        try:
            line = raw_line.decode('utf-8', errors='replace').strip()
            m = _PV_PROGRESS_RE.search(line)
            if m:
                elapsed_str = m.group(1)   # "0:00:12"
                speed_str = m.group(2).strip()  # "105MiB/s"
                progress = min(int(m.group(3)), 99)
                # 解析 elapsed 秒数
                parts = elapsed_str.split(':')
                elapsed_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                update_task(task_id,
                           stage="moving",
                           progress=progress,
                           move_speed=speed_str,
                           move_elapsed=f"{elapsed_sec}s")
        except Exception:
            pass

    proc.wait()
    if proc.returncode != 0:
        if dest.exists():
            dest.unlink()
        update_task(task_id, error=f"转移失败: pv exit={proc.returncode}")
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

    dest_dir = _get_media_dir()
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