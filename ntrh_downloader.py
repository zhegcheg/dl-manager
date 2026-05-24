"""
N_m3u8DL-RE 下载器包装
基于 MediaGo 方案：实时解析 stdout，提取进度和速度
"""
import subprocess
import threading
import re
from pathlib import Path
from task_db import update_task, get_download_config
from merger import merge_ts_to_mp4
from queue_manager import register_download, unregister_download
from mover import move_to_media_library

N_M3U8DL_RE = "/tmp/N_m3u8DL-RE"
POLL_INTERVAL = 1  # DB 更新频率（秒），避免写入太频繁

# MediaGo 正则方案
P_PERCENT = re.compile(r'([\d.]+)%')
P_SPEED = re.compile(r'([\d.]+[GMK]Bps)')
P_SEGMENTS = re.compile(r'(\d+)/(\d+)')
P_READY = re.compile(r'保存文件名:')
P_ERROR = re.compile(r'ERROR')

def parse_line(line):
    """解析 N_m3u8DL-RE 输出行，返回 (percent, speed, segments) 或 None
    仅在有百分比的进度行上提取段数，避免误匹配 URL 中的 /数字/数字
    """
    result = {}
    m = P_PERCENT.search(line)
    if m:
        result['progress'] = float(m.group(1))
        # 有百分比时才尝试提取段数（避免误匹配 URL 路径）
        seg = P_SEGMENTS.search(line)
        if seg:
            result['segments'] = f"{seg.group(1)}/{seg.group(2)}"
    m = P_SPEED.search(line)
    if m:
        result['speed'] = m.group(1)
    return result if result else None

def start_download(task_id: str, m3u8_url: str, headers: str = "", key: str = "", iv: str = ""):
    cfg = get_download_config()
    download_dir = cfg.get("download_dir", str(Path.home() / ".jable-dl-server" / "tasks"))
    thread_count = cfg.get("thread_count", "8")

    task_dir = Path(download_dir) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        N_M3U8DL_RE, m3u8_url,
        "--save-dir", str(Path(download_dir)),
        "--save-name", task_id,
        "--thread-count", thread_count,
        "--log-level", "INFO",
        "--auto-select",
        "--ui-language", "zh-CN",
        "--skip-merge",
        "--download-retry-count", "5",         # 每个分片异常重试5次
        "--check-segments-count",              # 下载后校验分片数量（默认开）
        "--use-ffmpeg-concat-demuxer",         # 用 concat demuxer 合并（更快更稳）
    ]

    if key:
        cmd += ["--custom-hls-key", key]
    if iv:
        cmd += ["--custom-hls-iv", iv]

    for line in headers.split("\n"):
        line = line.strip()
        if not line:
            continue
        if ':' in line:
            k, v = line.split(":", 1)
            cmd += ["--header", f"{k.strip()}: {v.strip()}"]

    update_task(task_id, status="downloading", stage="downloading", progress=0, error="")

    # === 关键改动：捕获 stdout 用于实时解析 ===
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # stderr 合并到 stdout
        cwd=str(task_dir),
        universal_newlines=True,
        bufsize=1  # 行缓冲
    )
    register_download(task_id, proc)

    def read_output():
        """逐行读取 stdout，解析进度/速度"""
        last_update = 0.0
        try:
            for line in iter(proc.stdout.readline, ''):
                line = line.strip()
                if not line:
                    continue
                parsed = parse_line(line)
                if parsed:
                    now = __import__('time').time()
                    if now - last_update >= POLL_INTERVAL:
                        last_update = now
                        update_task(task_id, **parsed)
        except (ValueError, IOError):
            pass
        finally:
            if proc.stdout:
                proc.stdout.close()

    def watch_process():
        """等待进程退出，更新最终状态"""
        read_output()  # 先读完剩余输出
        exit_code = proc.poll()
        unregister_download(task_id)

        # N_m3u8DL-RE 输出: download_dir/task_id/task_id.mp4
        # 需移到: download_dir/task_id.mp4
        import shutil
        nested_mp4 = task_dir / f"{task_id}.mp4"
        flat_dir = Path(download_dir)
        flat_mp4 = flat_dir / f"{task_id}.mp4"
        mp4_file = None

        if nested_mp4.exists() and nested_mp4.stat().st_size > 0:
            shutil.move(str(nested_mp4), str(flat_mp4))
            mp4_file = flat_mp4
        elif flat_mp4.exists() and flat_mp4.stat().st_size > 0:
            mp4_file = flat_mp4
        else:
            # 兜底：搜索任何 .mp4
            mp4s = list(task_dir.rglob("*.mp4"))
            if mp4s:
                src = mp4s[0]
                shutil.move(str(src), str(flat_mp4))
                mp4_file = flat_mp4

        if exit_code == 0 and mp4_file and mp4_file.stat().st_size > 0:
            # 先标记文件位置，再开始转移
            update_task(task_id, status="completed", stage="completed",
                      progress=100, file=str(mp4_file))
            # 转移到 NAS 媒体库（转移过程中 stage 会被 mover 改为 moving）
            from task_db import get_task
            t = get_task(task_id)
            if t:
                # 启动异步转移到 NAS（不阻塞）
                update_task(task_id, stage="moving", progress=0, move_speed="", move_elapsed="")
                move_to_media_library(task_id, str(mp4_file), t["name"] + ".mp4")
            # 清理空的任务目录和嵌套子目录
            if task_dir.exists():
                shutil.rmtree(task_dir, ignore_errors=True)
            return

        # 退出码非0或文件未生成 — 尝试合并分片
        # 搜索 0____ 分片目录（N_m3u8DL-RE 在 --save-name 子目录中创建）
        seg_0dir = task_dir / "0____"
        if not seg_0dir.exists():
            for sub in sorted(task_dir.iterdir()):
                if sub.is_dir():
                    check = sub / "0____"
                    if check.exists():
                        seg_0dir = check
                        break
        
        if seg_0dir.exists():
            ts_files = list(seg_0dir.glob("[0-9]*.ts"))
            if ts_files:
                # 片段太少时不合并（小于20%直接放弃）
                # 从 segments 字符串提取总数
                seg_count = len(ts_files)
                total_est = max(seg_count * 5, 2000)  # 估算总数
                if seg_count < total_est * 0.2:
                    update_task(task_id, status="failed", stage="failed", error=f"下载不完整 ({seg_count}片)，放弃合并")
                    return
                update_task(task_id, stage="merging", progress=0)
                # merger 输出到 seg_0dir.parent（task_dir/task_id/），但检查路径是 task_dir/（差一级）
                # 先把之前可能存在的输出文件移到正确位置
                correct_path = task_dir / f"{task_id}.mp4"
                nested_out = seg_0dir.parent / f"{task_id}.mp4"
                if not correct_path.exists() and nested_out.exists():
                    import shutil
                    shutil.move(str(nested_out), str(correct_path))
                ok, result = merge_ts_to_mp4(seg_0dir, task_id, correct_path)
                if ok:
                    merged = task_dir / f"{task_id}.mp4"
                    flat_path = Path(download_dir) / f"{task_id}.mp4"
                    if not flat_path.exists() and merged.exists():
                        import shutil
                        shutil.move(str(merged), str(flat_path))
                    if flat_path.exists() and flat_path.stat().st_size > 0:
                        update_task(task_id, status="completed", stage="completed",
                                  progress=100, file=str(flat_path))
                        from task_db import get_task
                        t = get_task(task_id)
                        if t:
                            move_to_media_library(task_id, str(flat_path), t["name"] + ".mp4")
                    else:
                        update_task(task_id, status="failed", stage="failed", error=f"合并后文件不存在 (exit={exit_code})")
                else:
                    update_task(task_id, status="failed", stage="failed", error=f"Merge failed: {result} (exit={exit_code})")
                return
        # 没有任何分片 → 失败
        update_task(task_id, status="failed", stage="failed", error=f"下载进程异常退出 (code={exit_code})")

    # 启动两个线程：一个读输出，一个等进程
    t = threading.Thread(target=watch_process, daemon=True)
    t.start()
    return proc
