"""
yt-dlp 下载器（子进程模式）
支持多线程分片下载、断点续传、代理配置、自动合并为 MP4
yt-dlp 在独立进程中运行，避免 GIL 竞争影响 Web UI 响应
"""
import threading
import time
import shutil
import logging
import subprocess
import sys
import json
import os
import re
from pathlib import Path
from typing import Optional
from datetime import datetime
from app.db.database import update_task, get_task, get_download_config, get_source_download_dir, get_task_temp_dir, get_task_log_path
from app.services.queue import register_download, unregister_download

logger = logging.getLogger("dl-manager")


POLL_INTERVAL = 2  # DB 更新频率（秒）

# ffmpeg 可用性缓存
_ffmpeg_available: Optional[bool] = None


def _check_ffmpeg() -> bool:
    """检查 ffmpeg 是否可用（结果缓存，支持 Windows 常见安装路径自动检测）"""
    global _ffmpeg_available
    if _ffmpeg_available is not None:
        return _ffmpeg_available
    
    ffmpeg_path = _get_ffmpeg_path()
    if ffmpeg_path:
        try:
            r = subprocess.run([ffmpeg_path, '-version'], capture_output=True, timeout=5)
            _ffmpeg_available = (r.returncode == 0)
            if _ffmpeg_available:
                logger.info(f"[ffmpeg] 检测到 ffmpeg 可用: {ffmpeg_path}")
        except Exception:
            _ffmpeg_available = False
    else:
        _ffmpeg_available = False
    
    if not _ffmpeg_available:
        logger.warning("[ffmpeg] 未检测到 ffmpeg！分片下载后将无法合并为 MP4")
    return _ffmpeg_available


def _ts():
    """当前时间戳字符串，用于日志行前缀"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def refresh_m3u8_url(task_id: str) -> str:
    """
    下载前刷新 m3u8 URL 和 AES key/iv。
    优先使用订阅源配置的 refresh_url_pattern 模板，
    回退到任务保存的 video_url（如 RSS <link>），
    重新解析页面获取最新的 m3u8/key/iv，防止 key 过期。
    """
    try:
        from app.services.rss_poller import resolve_video_info
        from app.db.database import get_source
        task = get_task(task_id)
        if not task:
            return ""

        # 从任务关联的订阅源获取配置
        source_id = task.get("source_id")
        source_config = get_source(source_id) if source_id else None

        # 构建刷新 URL：优先使用订阅源配置的模板
        refresh_pattern = (source_config or {}).get("refresh_url_pattern", "")
        video_url = ""
        if refresh_pattern:
            video_url = refresh_pattern.replace("{task_id}", task_id)
        
        # 回退：使用任务保存的 video_url（如 RSS <link>）
        if not video_url:
            video_url = task.get("video_url", "")
        
        if not video_url:
            logger.debug(f"[refresh_m3u8_url] {task_id}: 无 refresh_url_pattern 且无 video_url，跳过刷新")
            return ""

        logger.info(f"[refresh_m3u8_url] {task_id}: 使用 video_url={video_url} 重新解析")
        info = resolve_video_info(video_url, source_config=source_config)
        if info and info.get("m3u8_url"):
            # 同步更新 AES 密钥和 m3u8
            updates = {"m3u8_url": info["m3u8_url"]}
            if info.get("key"):
                updates["key"] = info["key"]
            if info.get("iv"):
                updates["iv"] = info["iv"]
            update_task(task_id, **updates)
            logger.info(f"[refresh_m3u8_url] {task_id}: 已刷新 m3u8/key/iv")
            return info["m3u8_url"]
    except Exception as e:
        logger.warning(f"[refresh_m3u8_url] {task_id}: 刷新失败 - {e}")
    return ""


class DownloadThread:
    """下载进程包装，提供类似 subprocess.Popen 的接口（内部使用子进程运行 yt-dlp）"""

    def __init__(self, task_id: str, process: Optional[subprocess.Popen] = None):
        self._process = process
        self._task_id = task_id
        self._stop_flag = threading.Event()
        self._exit_code: Optional[int] = None
        self._monitor_thread: Optional[threading.Thread] = None

    def poll(self) -> Optional[int]:
        """返回退出码，None 表示仍在运行"""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return None
        return self._exit_code if self._exit_code is not None else 0
    
    def terminate(self):
        """请求停止下载"""
        self._stop_flag.set()
        if self._process and self._process.poll() is None:
            try:
                self._process.terminate()
            except Exception:
                pass
    
    def wait(self, timeout: float = None):
        """等待下载完成"""
        if self._monitor_thread:
            self._monitor_thread.join(timeout=timeout)
    
    @property
    def stop_flag(self) -> threading.Event:
        return self._stop_flag
    
    def set_exit_code(self, code: int):
        self._exit_code = code


def format_speed(speed: float) -> str:
    """格式化下载速度"""
    if speed is None:
        return ""
    if speed >= 1024 * 1024 * 1024:
        return f"{speed / (1024 * 1024 * 1024):.2f}GB/s"
    if speed >= 1024 * 1024:
        return f"{speed / (1024 * 1024):.2f}MB/s"
    if speed >= 1024:
        return f"{speed / 1024:.2f}KB/s"
    return f"{speed:.2f}B/s"


def _get_ffmpeg_path() -> Optional[str]:
    """获取 ffmpeg 可执行文件路径（支持 Windows 常见安装路径）"""
    # 首先尝试 PATH 中的 ffmpeg
    ffmpeg_exe = shutil.which('ffmpeg')
    if ffmpeg_exe:
        return ffmpeg_exe
    
    # Windows 下尝试常见安装路径
    if os.name == 'nt':
        common_paths = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"D:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1.1-full_build\bin\ffmpeg.exe"),
        ]
        for path in common_paths:
            if os.path.isfile(path):
                return path
    
    return None


def _build_ytdlp_cmd(m3u8_url: str, temp_dir: str, task_id: str, thread_count: int,
                     headers: str = "") -> list:
    """构建 yt-dlp CLI 命令"""
    python_exe = sys.executable
    cmd = [
        python_exe, '-u', '-m', 'yt_dlp',
        m3u8_url,
        '-o', f'{temp_dir}/{task_id}.%(ext)s',
        '--concurrent-fragments', str(thread_count),
        '--continue',
        '--merge-output-format', 'mp4',
        '--console-title',
        '--retries', '10',
        '--fragment-retries', '10',
        '--socket-timeout', '30',
        '--newline',
        '--no-warnings',  # 抑制 WARNING 输出，避免干扰 JSON 进度解析
        '--progress-template',
        'download:{"progress":"%(progress._percent_str)s","speed":"%(progress._speed_str)s","frag_idx":"%(progress.fragment_index)s","frag_cnt":"%(progress._total_frags)s","eta":"%(progress._eta_str)s"}',
    ]
    
    # 如果找到 ffmpeg，指定其位置（帮助 yt-dlp 找到 ffmpeg 和 ffprobe）
    ffmpeg_path = _get_ffmpeg_path()
    if ffmpeg_path:
        cmd.extend(['--ffmpeg-location', os.path.dirname(ffmpeg_path)])
    
    if headers:
        try:
            for line in headers.split('\n'):
                line = line.strip()
                if ':' in line:
                    key, value = line.split(':', 1)
                    cmd.extend(['--add-header', f'{key.strip()}:{value.strip()}'])
        except Exception:
            pass
    return cmd


# ANSI 转义码正则（用于清理 Linux 终端控制字符）
_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b\(B')


def _strip_ansi(text: str) -> str:
    """移除 ANSI 转义码"""
    return _ANSI_ESCAPE_RE.sub('', text)


def _monitor_and_postprocess(proc: subprocess.Popen, download_handle: DownloadThread,
                             task_id: str, m3u8_url: str, download_dir: str,
                             temp_dir: str, log_file, write_log):
    """监控 yt-dlp 子进程输出，解析进度，完成后执行文件移动和 NAS 转移"""
    last_update = 0.0
    last_log_progress = -10
    non_json_lines = 0  # 非 JSON 行计数器，用于诊断

    last_progress = 0.0
    max_progress = 0.0
    try:
        while True:
            raw_line = proc.stdout.readline()
            if not raw_line:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
                continue

            # 检查取消标志
            if download_handle.stop_flag.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                write_log("用户取消下载", "WARN")
                update_task(task_id, status="stopped", stage="stopped", error="用户取消下载")
                download_handle.set_exit_code(1)
                return

            line = _strip_ansi(raw_line).strip()
            if not line:
                continue

            # 解析 JSON 进度输出
            if line.startswith('{'):
                try:
                    data = json.loads(line)
                    now = time.time()
                    if now - last_update < POLL_INTERVAL:
                        continue
                    last_update = now

                    progress_str = data.get('progress', '0').strip().rstrip('%')
                    try:
                        progress = float(progress_str)
                    except (ValueError, TypeError):
                        progress = 0
                    speed_str = (data.get('speed') or '').strip()
                    try:
                        frag_idx = int(data.get('frag_idx', 0))
                    except (ValueError, TypeError):
                        frag_idx = 0
                    try:
                        frag_cnt = int(data.get('frag_cnt', 0))
                    except (ValueError, TypeError):
                        frag_cnt = 0
                    segments_str = f"{frag_idx}/{frag_cnt}" if frag_cnt > 0 else ""

                    last_progress = progress
                    if progress > max_progress:
                        max_progress = progress

                    update_task(task_id, progress=progress, speed=speed_str, segments=segments_str)

                    if progress - last_log_progress >= 10:
                        last_log_progress = progress
                        write_log(f"进度: {progress:.1f}% | 速度: {speed_str} | 分片: {segments_str}")
                except (json.JSONDecodeError, KeyError):
                    pass
            else:
                # 记录前几条非 JSON 行，帮助诊断输出格式问题
                non_json_lines += 1
                if non_json_lines <= 5:
                    write_log(f"[yt-dlp] {line[:200]}")

        # 等待进程结束
        proc.wait()
        stderr_output = proc.stderr.read() if proc.stderr else ''

        if proc.returncode == 0:
            # 严格完成度检查：yt-dlp 返回 0 但进度未到达 100% 的情况（如网络中断后异常退出）
            if max_progress > 0 and max_progress < 95.0:
                # 进度远低于 100%，直接判定为失败（即使 ffprobe 可能通过，文件也很可能不完整）
                err = f"下载不完整: yt-dlp 报告的最高进度仅 {max_progress:.1f}%，必须到达 ~100% 才算完成"
                write_log(err, "ERROR")
                update_task(task_id, status="failed", stage="failed", error=err)
                download_handle.set_exit_code(1)
                return
            elif max_progress > 0 and max_progress < 99.0:
                write_log(f"警告: yt-dlp 退出码为 0，但最高进度仅 {max_progress:.1f}%，将通过 ffprobe 验证文件完整性", "WARN")
            write_log("yt-dlp 下载完成")
            update_task(task_id, progress=100, speed="", segments="", stage="merging")
            _post_download(task_id, download_dir, temp_dir, write_log, download_handle, max_progress=max_progress)
        elif download_handle.stop_flag.is_set():
            pass  # 已处理
        else:
            error_msg = stderr_output.strip().split('\n')[-1][:200] if stderr_output else f"yt-dlp 退出码: {proc.returncode}"
            # 检查是否有已合并的 .mp4.part 文件（yt-dlp 下载完成但 ffmpeg 合并步骤失败）
            part_file = Path(temp_dir) / f"{task_id}.mp4.part"
            if part_file.exists() and part_file.stat().st_size > 50 * 1024 * 1024:  # 提高到 50MB
                write_log(f"yt-dlp 退出码非 0 ({proc.returncode})，但发现 .mp4.part 文件 ({part_file.stat().st_size / (1024*1024):.1f} MB)")
                write_log(f"原始错误: {error_msg}")
                write_log("尝试从 .mp4.part 文件恢复...")
                # 严格验证：.mp4.part 文件也必须通过 ffprobe 验证
                ok, verify_msg = _verify_video_integrity(str(part_file))
                if ok:
                    write_log(f".mp4.part 文件验证通过: {verify_msg}")
                    update_task(task_id, progress=100, speed="", segments="", stage="merging")
                    _post_download(task_id, download_dir, temp_dir, write_log, download_handle, max_progress=max_progress)
                else:
                    write_log(f".mp4.part 文件验证失败: {verify_msg}，标记为下载失败", "ERROR")
                    update_task(task_id, status="failed", stage="failed", error=f"下载失败(文件不完整): {error_msg}; 验证失败: {verify_msg}")
                    download_handle.set_exit_code(1)
            else:
                write_log(f"下载失败: {error_msg}", "ERROR")
                logger.error(f"[下载] 任务 {task_id} 失败: {error_msg}")
                update_task(task_id, status="failed", stage="failed", error=f"下载失败: {error_msg}")
                download_handle.set_exit_code(1)

    except Exception as e:
        write_log(f"监控异常: {e}", "ERROR")
        import traceback
        write_log(f"异常跟踪:\n{traceback.format_exc()}", "ERROR")
        logger.error(f"[下载] 任务 {task_id} 监控异常: {e}")
        update_task(task_id, status="failed", stage="failed", error=f"异常: {e}")
        download_handle.set_exit_code(1)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    finally:
        try:
            log_file.close()
        except Exception:
            pass
        unregister_download(task_id)


def _post_download(task_id: str, download_dir: str, temp_dir: str,
                   write_log, download_handle: DownloadThread, max_progress: float = 0):
    """下载完成后处理：查找合并文件、移动到下载目录、可选 NAS 转移"""
    # 诊断：列出 temp_dir 和 download_dir 中与当前任务相关的文件
    for dir_label, dir_path in [("temp_dir", temp_dir), ("download_dir", download_dir)]:
        try:
            all_files = list(Path(dir_path).glob(f"{task_id}*"))
            total_size = sum(f.stat().st_size for f in all_files if f.is_file())
            write_log(f"{dir_label} 中任务文件: {len(all_files)} 个, 总计 {total_size / (1024*1024):.2f} MB")
            ext_count: dict = {}
            for f in all_files:
                if f.is_file():
                    ext = f.suffix or '(no ext)'
                    ext_count[ext] = ext_count.get(ext, 0) + 1
            for ext, cnt in sorted(ext_count.items(), key=lambda x: -x[1]):
                write_log(f"  {ext}: {cnt} 个文件")
        except Exception as e:
            write_log(f"列出 {dir_label} 失败: {e}", "WARN")

    # 查找合并后的视频文件（优先在下载目录中找，再在 temp 目录中找）
    # 搜索顺序: download_dir > temp_dir，格式优先 mp4 > mkv > webm
    video_path = None
    search_dirs = [Path(download_dir), Path(temp_dir)]

    # 1. 精确匹配: {task_id}.ext
    for search_dir in search_dirs:
        for ext in ('.mp4', '.mkv', '.webm', '.mov'):
            candidate = search_dir / f"{task_id}{ext}"
            if candidate.exists() and candidate.stat().st_size > 0:
                video_path = candidate
                write_log(f"在 {search_dir.name}/ 中找到视频: {candidate.name}")
                break
        if video_path:
            break

    # 2. 回退：模糊匹配（带格式后缀如 {task_id}.f1234.mp4）
    if not video_path:
        for search_dir in search_dirs:
            for ext in ('.mp4', '.mkv', '.webm'):
                candidates = [f for f in search_dir.glob(f"{task_id}*{ext}")
                              if f.is_file() and f.stat().st_size > 0
                              and '.part' not in f.name]
                if candidates:
                    candidates.sort(key=lambda f: f.stat().st_size, reverse=True)
                    video_path = candidates[0]
                    write_log(f"在 {search_dir.name}/ 中模糊匹配到视频: {video_path.name}")
                    break
            if video_path:
                break

    # 回退：检查 .mp4.part 文件（yt-dlp 已合并但未完成最终输出）
    if not video_path:
        part_file = Path(temp_dir) / f"{task_id}.mp4.part"
        if part_file.exists() and part_file.stat().st_size > 50 * 1024 * 1024:  # 大于 50MB 才认为是有效文件
            write_log(f"发现未完成的 .mp4.part 文件 ({part_file.stat().st_size / (1024*1024):.2f} MB)，尝试 ffmpeg remux")
            output_mp4 = Path(temp_dir) / f"{task_id}.mp4"
            try:
                proc = subprocess.run(
                    ['ffmpeg', '-y', '-hide_banner', '-i', str(part_file),
                     '-c', 'copy', '-movflags', '+faststart', str(output_mp4)],
                    capture_output=True, text=True, timeout=600
                )
                if proc.returncode == 0 and output_mp4.exists() and output_mp4.stat().st_size > 0:
                    write_log(f"remux 成功: {output_mp4.name} ({output_mp4.stat().st_size / (1024*1024):.2f} MB)")
                    video_path = output_mp4
                else:
                    # remux 失败，直接重命名（大部分播放器可播放 .part 文件）
                    write_log(f"remux 失败，直接重命名 .part 文件", "WARN")
                    part_file.rename(output_mp4)
                    video_path = output_mp4
            except FileNotFoundError:
                write_log("ffmpeg 未安装，直接重命名 .part 文件", "WARN")
                part_file.rename(output_mp4)
                video_path = output_mp4
            except subprocess.TimeoutExpired:
                write_log("ffmpeg remux 超时，直接重命名 .part 文件", "WARN")
                if output_mp4.exists():
                    output_mp4.unlink()
                part_file.rename(output_mp4)
                video_path = output_mp4
            except Exception as e:
                write_log(f"remux 异常: {e}，直接重命名", "WARN")
                part_file.rename(output_mp4)
                video_path = output_mp4

    if not video_path:
        # 检查是否只有 part-Frag 分片文件（说明 ffmpeg 合并失败）
        part_files = list(Path(temp_dir).glob(f"{task_id}*.part-Frag*"))
        if part_files:
            if not _check_ffmpeg():
                err = "下载完成但 ffmpeg 未安装，无法合并分片文件为 MP4。请安装 ffmpeg 后重试"
            else:
                err = f"下载完成但合并失败，temp_dir 中有 {len(part_files)} 个分片文件"
            write_log(err, "ERROR")
            update_task(task_id, status="failed", stage="failed", error=err)
        else:
            write_log("下载完成但未找到任何视频文件", "ERROR")
            update_task(task_id, status="failed", stage="failed",
                        error="下载完成但未找到视频文件")
        download_handle.set_exit_code(1)
        return

    # === 视频完整性验证：必须用 ffprobe 验证文件可正常解析 ===
    write_log(f"开始验证视频文件完整性: {video_path.name}")
    ok, verify_msg = _verify_video_integrity(str(video_path))
    if ok:
        write_log(f"视频验证通过: {verify_msg}")
    else:
        write_log(f"视频验证失败: {verify_msg}", "ERROR")
        # 如果 yt-dlp 进度未到 100%，明确提示文件不完整
        if max_progress > 0 and max_progress < 99.0:
            err_detail = f"下载不完整(最高进度 {max_progress:.1f}%)，文件验证失败: {verify_msg}"
        else:
            err_detail = f"文件验证失败: {verify_msg}"
        update_task(task_id, status="failed", stage="failed", error=err_detail)
        download_handle.set_exit_code(1)
        return

    write_log(f"视频文件就绪: {video_path.name} ({video_path.stat().st_size / (1024*1024):.2f} MB)")
    write_log(f"视频文件完整路径: {video_path}")
    write_log(f"目标下载目录: {download_dir}")

    # 如果视频已在下载目录中（非temp_dir），跳过移动
    if str(video_path.parent) == str(Path(download_dir).resolve()):
        write_log(f"视频文件已在下载目录中，无需移动")
        final_mp4 = video_path
    else:
        # 移动到下载目录
        write_log(f"开始移动文件到: {download_dir}")
        # 文件名长度保护（CIFS 文件名上限 255 字节，Windows MAX_PATH 260 字符）
        mp4_name = video_path.name
        name_bytes = len(mp4_name.encode('utf-8'))
        if name_bytes > 250 or len(mp4_name) > 200:
            stem = video_path.stem
            suffix = video_path.suffix
            # 按字符截断，兼顾字节和字符长度限制
            max_chars = min(180, len(stem))
            while max_chars > 0:
                test = stem[:max_chars] + suffix
                if len(test.encode('utf-8')) <= 250 and len(test) <= 200:
                    break
                max_chars -= 1
            mp4_name = stem[:max_chars] + suffix
        final_mp4 = Path(download_dir) / mp4_name
        # 如果目标文件已存在，比较大小：谁大保留谁
        if final_mp4.exists():
            existing_size = final_mp4.stat().st_size
            new_size = video_path.stat().st_size
            if existing_size >= new_size:
                write_log(f"目标文件已存在且更大 ({existing_size / (1024*1024):.2f} MB >= {new_size / (1024*1024):.2f} MB)，保留已有文件，删除新文件")
                try:
                    video_path.unlink()
                except Exception:
                    pass
                # final_mp4 保持为已有的目标文件
            else:
                write_log(f"目标文件已存在但更小 ({existing_size / (1024*1024):.2f} MB < {new_size / (1024*1024):.2f} MB)，删除旧文件并移动新文件")
                try:
                    final_mp4.unlink()
                except Exception:
                    pass
                try:
                    shutil.move(str(video_path), str(final_mp4))
                    write_log(f"文件已移动到: {final_mp4}")
                except Exception as move_err:
                    write_log(f"移动失败: {move_err}，回退到复制", "WARN")
                    try:
                        shutil.copy2(str(video_path), str(final_mp4))
                        video_path.unlink()
                        write_log(f"文件已复制到: {final_mp4}")
                    except Exception as copy_err:
                        write_log(f"复制也失败: {copy_err}", "ERROR")
                        # 最后手段：直接使用 temp_dir 中的文件
                        final_mp4 = video_path
                        write_log(f"回退使用 temp_dir 中的文件: {final_mp4}", "WARN")
        else:
            try:
                shutil.move(str(video_path), str(final_mp4))
                write_log(f"文件已移动到: {final_mp4}")
            except Exception as move_err:
                write_log(f"移动失败: {move_err}，回退到复制", "WARN")
                try:
                    shutil.copy2(str(video_path), str(final_mp4))
                    video_path.unlink()
                    write_log(f"文件已复制到: {final_mp4}")
                except Exception as copy_err:
                    write_log(f"复制也失败: {copy_err}", "ERROR")
                    # 最后手段：直接使用 temp_dir 中的文件
                    final_mp4 = video_path
                    write_log(f"回退使用 temp_dir 中的文件: {final_mp4}", "WARN")

    # 只有当 final_mp4 不在 temp_dir 中时才清理临时文件
    if str(final_mp4.parent) != str(Path(temp_dir).resolve()):
        _cleanup_temp(temp_dir, task_id)
        write_log("临时文件已清理")
    else:
        write_log(f"文件保留在 temp_dir 中（移动失败回退），跳过清理")
    update_task(task_id, status="completed", stage="completed",
                progress=100, file=str(final_mp4))
    write_log(f"任务完成 - 最终文件: {final_mp4}")

    # 检查是否启用 NAS 转移（未启用则跳过）
    cfg_now = get_download_config()
    if cfg_now.get("move_to_nas", "true") == "true":
        t = get_task(task_id)
        if t:
            write_log("开始 NAS 转移...")
            update_task(task_id, stage="moving", progress=0, move_speed="", move_elapsed="")
            from app.services.mover import move_to_media_library
            move_to_media_library(task_id, str(final_mp4), t["name"] + ".mp4")
            write_log("NAS 转移完成")
    else:
        write_log("NAS 转移已禁用，跳过")

    download_handle.set_exit_code(0)


def start_download(task_id: str, m3u8_url: str, headers: str = "", key: str = "", iv: str = ""):
    """
    启动 yt-dlp 下载任务（子进程模式）
    
    流程: yt-dlp 子进程下载分片到 temp_dir -> 合并为 MP4 -> 移动到 download_dir -> 转移到 NAS
    yt-dlp 在独立进程中运行，不影响主进程事件循环
    """
    # 获取任务记录
    task = get_task(task_id)
    if not task:
        logger.error(f"[下载] 任务 {task_id} 记录不存在")
        update_task(task_id, status="failed", stage="failed", error="任务记录不存在")
        return None

    logger.info(f"[下载] 开始任务 {task_id}: {task.get('name', '未知')}")

    # download_dir：任务记录 > 按订阅源计算 > 全局配置
    download_dir = task.get("download_dir", "")
    if not download_dir:
        source_id = task.get("source_id")
        download_dir = get_source_download_dir(source_id)
        if download_dir:
            logger.info(f"[下载] 使用订阅源下载目录: {download_dir}")
        else:
            logger.warning(f"[下载] 未配置下载目录，使用默认路径")

    # temp_dir：任务记录 > 全局计算
    temp_dir = task.get("temp_dir", "")
    if not temp_dir:
        temp_dir = get_task_temp_dir()
        logger.debug(f"[下载] 使用临时目录: {temp_dir}")

    # 确保目录存在
    try:
        Path(download_dir).mkdir(parents=True, exist_ok=True)
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"[下载] 创建目录失败: {e}")
        update_task(task_id, status="failed", stage="failed", error=f"创建目录失败: {e}")
        return None

    update_task(task_id, download_dir=download_dir, temp_dir=temp_dir)

    # 获取线程数
    cfg = get_download_config()
    thread_count = int(cfg.get("thread_count", "8"))

    # 创建下载句柄
    download_handle = DownloadThread(task_id)

    # 日志文件
    log_path = get_task_log_path(task_id)
    log_file = open(log_path, 'w', encoding='utf-8', buffering=1)

    def write_log(msg: str, level: str = "INFO"):
        try:
            log_file.write(f"[{_ts()}] [{level}] {msg}\n")
            log_file.flush()
        except Exception:
            pass

    write_log(f"任务开始 - ID: {task_id}")
    write_log(f"m3u8 URL: {m3u8_url}")
    if key:
        write_log(f"AES key: {key[:16]}...")
    if iv:
        write_log(f"AES iv: {iv}")
    write_log(f"下载目录: {download_dir}")
    write_log(f"线程数: {thread_count}")
    write_log("下载模式: 子进程（GIL 隔离）")

    # === 前置检查：是否已有合并完成的视频文件（避免重复下载浪费流量） ===
    existing_video = None
    for search_dir in [Path(download_dir), Path(temp_dir)]:
        for ext in ('.mp4', '.mkv', '.webm', '.mov'):
            candidate = search_dir / f"{task_id}{ext}"
            if candidate.exists() and candidate.stat().st_size > 1024 * 1024:  # >1MB 才认为有效
                existing_video = candidate
                break
        if existing_video:
            break
    # 回退：模糊匹配
    if not existing_video:
        for search_dir in [Path(download_dir), Path(temp_dir)]:
            for ext in ('.mp4', '.mkv', '.webm'):
                candidates = [f for f in search_dir.glob(f"{task_id}*{ext}")
                              if f.is_file() and f.stat().st_size > 1024 * 1024
                              and '.part' not in f.name]
                if candidates:
                    candidates.sort(key=lambda f: f.stat().st_size, reverse=True)
                    existing_video = candidates[0]
                    break
            if existing_video:
                break

    if existing_video:
        write_log(f"发现已存在的视频文件: {existing_video} ({existing_video.stat().st_size / (1024*1024):.2f} MB)")
        logger.info(f"[下载] 任务 {task_id}: 视频已存在，跳过下载")

        # === 已有文件也要验证完整性 ===
        write_log("开始验证已有视频文件完整性...")
        ok, verify_msg = _verify_video_integrity(str(existing_video))
        if ok:
            write_log(f"已有文件验证通过: {verify_msg}")
        else:
            write_log(f"已有文件验证失败: {verify_msg}，不跳过下载，重新下载", "WARN")
            # 删除损坏的已有文件，继续正常下载流程
            try:
                existing_video.unlink()
            except Exception:
                pass
            # 不返回，继续执行后面的下载逻辑
            existing_video = None

    if existing_video:
        write_log("跳过下载，直接使用已有文件")
        # 确保文件在下载目录中
        if str(existing_video.parent) == str(Path(download_dir).resolve()):
            final_mp4 = existing_video
            write_log(f"文件已在下载目录: {final_mp4}")
        else:
            # 从 temp_dir 移动到 download_dir
            mp4_name = existing_video.name
            final_mp4 = Path(download_dir) / mp4_name
            if final_mp4.exists() and final_mp4.stat().st_size >= existing_video.stat().st_size:
                write_log(f"下载目录已有同名文件且更大，跳过移动")
                existing_video.unlink()
            else:
                try:
                    shutil.move(str(existing_video), str(final_mp4))
                    write_log(f"文件已移动到: {final_mp4}")
                except Exception as e:
                    write_log(f"移动失败: {e}，保留原位", "WARN")
                    final_mp4 = existing_video

        # 清理临时文件（仅当最终文件不在 temp_dir 时）
        if str(final_mp4.parent) != str(Path(temp_dir).resolve()):
            _cleanup_temp(temp_dir, task_id)
            write_log("临时文件已清理")

        update_task(task_id, status="completed", stage="completed",
                    progress=100, file=str(final_mp4))
        write_log(f"任务完成 - 最终文件: {final_mp4}")

        # NAS 转移
        cfg_now = get_download_config()
        if cfg_now.get("move_to_nas", "true") == "true":
            t = get_task(task_id)
            if t:
                write_log("开始 NAS 转移...")
                update_task(task_id, stage="moving", progress=0, move_speed="", move_elapsed="")
                from app.services.mover import move_to_media_library
                move_to_media_library(task_id, str(final_mp4), t["name"] + ".mp4")
                write_log("NAS 转移已启动")
        else:
            write_log("NAS 转移已禁用，跳过")

        download_handle.set_exit_code(0)
        log_file.close()
        return download_handle

    # ffmpeg 前置检查
    if not _check_ffmpeg():
        write_log("警告: ffmpeg 未安装！分片下载后可能无法合并为 MP4", "WARN")

    # 构建 yt-dlp 命令
    cmd = _build_ytdlp_cmd(m3u8_url, temp_dir, task_id, thread_count, headers)
    write_log(f"yt-dlp 命令: {' '.join(cmd)}")

    # 更新任务状态
    update_task(task_id, status="downloading", stage="downloading", progress=0, error="")
    logger.info(f"[下载] 启动 yt-dlp 子进程")

    # 注册下载
    register_download(task_id, download_handle)

    # 启动 yt-dlp 子进程（设置 PYTHONUNBUFFERED 确保 stdout 不被缓冲）
    env = os.environ.copy()
    env['PYTHONUNBUFFERED'] = '1'
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        download_handle._process = proc
    except Exception as e:
        write_log(f"启动 yt-dlp 子进程失败: {e}", "ERROR")
        update_task(task_id, status="failed", stage="failed", error=f"启动失败: {e}")
        download_handle.set_exit_code(1)
        log_file.close()
        unregister_download(task_id)
        return download_handle

    # 启动监控线程（解析进度 + 后处理）
    monitor = threading.Thread(
        target=_monitor_and_postprocess,
        args=(proc, download_handle, task_id, m3u8_url, download_dir, temp_dir, log_file, write_log),
        daemon=True,
    )
    download_handle._monitor_thread = monitor
    monitor.start()

    return download_handle


def _verify_video_integrity(file_path: str) -> tuple[bool, str]:
    """使用 ffprobe 验证视频文件完整性（检查是否有有效的视频流和时长）"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name,duration,bit_rate',
             '-show_entries', 'format=duration,bit_rate,size',
             '-of', 'json', file_path],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return False, f"ffprobe 失败: {result.stderr.strip()[:200]}"

        probe_data = json.loads(result.stdout)
        streams = probe_data.get('streams', [])
        if not streams:
            return False, "无视频流"

        # 检查时长是否合理（> 1秒）
        duration = None
        for source in [probe_data.get('format', {}), streams[0]]:
            d = source.get('duration')
            if d:
                try:
                    duration = float(d)
                    if duration > 0:
                        break
                except (ValueError, TypeError):
                    pass

        if duration is None or duration < 1.0:
            return False, f"视频时长异常: {duration}"

        # 检查文件大小是否合理（> 1MB）
        size = Path(file_path).stat().st_size
        if size < 1024 * 1024:
            return False, f"文件过小: {size / 1024:.1f} KB"

        return True, f"codec={streams[0].get('codec_name')}, duration={duration:.1f}s, size={size / (1024*1024):.1f}MB"
    except Exception as e:
        return False, f"验证异常: {e}"


def _cleanup_temp(temp_dir: str, task_id: str):
    """清理 temp_dir 中指定 task_id 的残留分片文件"""
    import glob
    pattern = f"{temp_dir}/{task_id}*"
    for f in glob.glob(pattern):
        try:
            p = Path(f)
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass



