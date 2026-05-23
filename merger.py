"""
ffmpeg 合并逻辑
N_m3u8DL-RE 创建的文件: 0000.ts ~ 2557.ts (zero-padded)
"""
import subprocess
import re
import threading
from pathlib import Path
from task_db import update_task

def merge_ts_to_mp4(seg_dir: Path, task_id: str) -> tuple[bool, str]:
    """
    使用 ffmpeg concat demuxer 合并 TS 为 MP4
    seg_dir: 包含 0000.ts ~ xxxx.ts 的目录 (即 task_dir/task_id/0____)
    返回: (成功, 文件路径 或 错误信息)
    """
    ts_files = sorted(seg_dir.glob("[0-9]*.ts"), key=lambda p: int(p.stem))

    if not ts_files:
        return False, "No [0-9]*.ts files found"

    # 清理临时文件
    for f in seg_dir.glob("*.ts.tmp"):
        f.unlink()
    core = seg_dir / "core"
    if core.exists():
        core.unlink()

    # 写 concat list
    concat_list = seg_dir / "concat_list.txt"
    with open(concat_list, 'w') as f:
        for ts in ts_files:
            f.write(f"file '{ts.resolve()}'\n")

    output_path = seg_dir.parent / f"{task_id}.mp4"

    def parse_progress(proc):
        while proc.poll() is None:
            line = proc.stderr.readline().decode('utf-8', errors='ignore')
            if not line:
                continue
            if "frame=" in line:
                m = re.search(r'size=\s*(\d+)kB', line)
                if m:
                    size_mb = int(m.group(1)) / 1024
                    progress = min(int(size_mb * 100 / 2048), 99)
                    update_task(task_id, progress=progress, stage="merging")

    proc = subprocess.Popen([
        "ffmpeg", "-y", "-hide_banner",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(output_path)
    ], stderr=subprocess.PIPE, stdout=subprocess.PIPE)

    t = threading.Thread(target=parse_progress, args=(proc,), daemon=True)
    t.start()
    proc.wait()

    try:
        concat_list.unlink()
    except Exception:
        pass

    if proc.returncode != 0:
        return False, f"ffmpeg exited with code {proc.returncode}"

    return True, str(output_path)