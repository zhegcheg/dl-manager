"""
ffmpeg 合并逻辑
N_m3u8DL-RE 创建的文件: 0000.ts ~ 2557.ts (zero-padded)

改进:
- 编码跳变检测：采样 ffprobe 判断是否需要转码
- concat copy + aac_adtstoasc（最快）
- re-encode 保底（libx264+aac）
- 更好的进度追踪
"""
import logging
import subprocess
import re
import threading
import os
from pathlib import Path
from app.db.database import update_task

logger = logging.getLogger("dl-manager")

# 估算文件大小（MB），用于进度计算
EST_MP4_SIZE_MB = 2048


def merge_ts_to_mp4(seg_dir: Path, task_id: str, output_path: Path) -> tuple[bool, str]:
    """
    使用 ffmpeg concat demuxer 合并 TS 为 MP4
    seg_dir:      包含 0000.ts ~ xxxx.ts 的目录 (即 task_dir/task_id/0____)
    task_id:      任务 ID
    output_path:  合并后 MP4 输出到的目标路径（由调用方指定，通常是 task_dir/task_id.mp4）
    返回: (成功, 文件路径 或 错误信息)
    """
    ts_files = sorted(seg_dir.glob("[0-9]*.ts"), key=lambda p: int(p.stem))

    if not ts_files:
        return False, "No [0-9]*.ts files found"

    total_segments = len(ts_files)
    logger.info(f"[merger] {task_id}: 开始合并 {total_segments} 个 TS 片段")

    # === 检查源流连续性：采样 ffprobe 判断是否有编码跳变 ===
    samples = [ts_files[0], ts_files[len(ts_files)//2], ts_files[-1]]
    prev_probe = ""
    has_discontinuity = False
    for i, ts in enumerate(samples):
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name,width,height",
                 "-of", "csv=p=0", str(ts)],
                capture_output=True, text=True, timeout=10
            )
            probe_result = r.stdout.strip()
            if i > 0 and prev_probe and probe_result != prev_probe:
                has_discontinuity = True
                logger.warning(f"[merger] {task_id}: 检测到编码跳变 (前={prev_probe} 中/后={probe_result})，跳过 concat copy")
                break
            prev_probe = probe_result
        except Exception:
            pass

    output_path = Path(output_path)  # 统一转为 Path

    if has_discontinuity:
        # 直接走 re-encode
        ok, err = _do_reencode(seg_dir, ts_files, task_id, output_path)
        return ok, err

    # 先尝试 concat copy
    ok, _ = _do_concat_copy(seg_dir, ts_files, task_id, output_path)
    if ok:
        return True, str(output_path)

    # copy 失败，尝试 re-encode
    logger.warning(f"[merger] {task_id}: concat copy 失败，尝试 re-encode...")
    return _do_reencode(seg_dir, ts_files, task_id, output_path)


def _do_concat_copy(seg_dir: Path, ts_files: list, task_id: str, output_path: Path) -> tuple[bool, str]:
    """策略1：concat copy + AAC filter，返回 (成功, 错误信息)"""
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

    update_task(task_id, progress=0, stage="merging")

    cmd_copy = [
        "ffmpeg", "-y", "-hide_banner",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:v", "copy", "-c:a", "copy",
        "-bsf:a", "aac_adtstoasc",
        str(output_path)
    ]

    logger.info(f"[merger] {task_id}: 执行 concat copy (with aac_adtstoasc)...")
    proc = subprocess.Popen(cmd_copy, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)

    stderr_lines = []
    for line in iter(proc.stderr.readline, b''):
        stderr_lines.append(line)
        line_text = line.decode('utf-8', errors='ignore')
        if "frame=" in line_text:
            m = re.search(r'size=\s*(\d+)kB', line_text)
            if m:
                size_mb = int(m.group(1)) / 1024
                progress = min(int(size_mb * 100 / EST_MP4_SIZE_MB), 99)
                update_task(task_id, progress=progress, stage="merging")

    proc.wait()
    stderr_text = b''.join(stderr_lines).decode('utf-8', errors='ignore')

    try:
        concat_list.unlink()
    except Exception:
        pass

    if proc.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        logger.info(f"[merger] {task_id}: concat copy 成功! size={output_path.stat().st_size}")
        return True, str(output_path)

    return False, stderr_text[-500:]


def _do_reencode(seg_dir: Path, ts_files: list, task_id: str, output_path: Path) -> tuple[bool, str]:
    """策略2：re-encode (libx264+aac)，返回 (成功, 错误信息)"""
    # 重新写 concat list
    concat_list = seg_dir / "concat_list.txt"
    with open(concat_list, 'w') as f:
        for ts in ts_files:
            f.write(f"file '{ts.resolve()}'\n")

    update_task(task_id, progress=0, stage="merging_reencode")

    cmd_reencode = [
        "ffmpeg", "-y", "-hide_banner",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path)
    ]

    logger.info(f"[merger] {task_id}: 执行 re-encode (libx264+aac)...")
    proc2 = subprocess.Popen(cmd_reencode, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)

    stderr2_lines = []
    for line in iter(proc2.stderr.readline, b''):
        stderr2_lines.append(line)
        line_text = line.decode('utf-8', errors='ignore')
        if "frame=" in line_text:
            tm = re.search(r'time=(\d+):(\d+):(\d+)\.\d+', line_text)
            if tm:
                h, m, s = int(tm.group(1)), int(tm.group(2)), int(tm.group(3))
                total_sec = h * 3600 + m * 60 + s
                est_total = EST_MP4_SIZE_MB * 1024 / 1.6
                progress = min(int(total_sec * 100 / est_total), 99)
                update_task(task_id, progress=progress, stage="merging_reencode")

    proc2.wait()
    stderr2_text = b''.join(stderr2_lines).decode('utf-8', errors='ignore')

    try:
        concat_list.unlink()
    except Exception:
        pass

    if proc2.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
        logger.info(f"[merger] {task_id}: re-encode 成功! size={output_path.stat().st_size}")
        return True, str(output_path)

    err_msg = stderr2_text[-800:] if stderr2_text else f"ffmpeg exit {proc2.returncode}"
    return False, err_msg[:300]