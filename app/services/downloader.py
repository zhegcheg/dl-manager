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
from pathlib import Path
from typing import Optional
from datetime import datetime
from app.db.database import update_task, get_task, get_download_config, get_source_download_dir, get_task_temp_dir, get_task_log_path
from app.services.queue import register_download, unregister_download

logger = logging.getLogger("dl-manager")


POLL_INTERVAL = 2  # DB 更新频率（秒）


def _ts():
    """当前时间戳字符串，用于日志行前缀"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def refresh_m3u8_url(task_id: str) -> str:
    """
    下载前刷新 m3u8 URL。
    通过任务关联的订阅源配置构建刷新 URL，不再硬编码站点。
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
        if not refresh_pattern:
            logger.debug(f"[refresh_m3u8_url] {task_id}: 未配置 refresh_url_pattern，跳过刷新")
            return ""

        video_url = refresh_pattern.replace("{task_id}", task_id)
        info = resolve_video_info(video_url, source_config=source_config)
        if info and info.get("m3u8_url"):
            # 同步更新 AES 密钥
            if info.get("key"):
                update_task(task_id, key=info["key"])
            if info.get("iv"):
                update_task(task_id, iv=info["iv"])
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


def _build_ytdlp_cmd(m3u8_url: str, temp_dir: str, task_id: str, thread_count: int,
                     headers: str = "") -> list:
    """构建 yt-dlp CLI 命令"""
    python_exe = sys.executable
    cmd = [
        python_exe, '-m', 'yt_dlp',
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
        '--progress-template',
        'download:{"progress":"%(progress._percent_str)s","speed":"%(progress._speed_str)s","frag_idx":"%(progress.fragment_index)s","frag_cnt":"%(progress._total_frags)s","eta":"%(progress._eta_str)s"}',
    ]
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


def _monitor_and_postprocess(proc: subprocess.Popen, download_handle: DownloadThread,
                             task_id: str, m3u8_url: str, download_dir: str,
                             temp_dir: str, log_file, write_log):
    """监控 yt-dlp 子进程输出，解析进度，完成后执行文件移动和 NAS 转移"""
    last_update = 0.0
    last_log_progress = -10

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

            line = raw_line.strip()
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

                    update_task(task_id, progress=progress, speed=speed_str, segments=segments_str)

                    if progress - last_log_progress >= 10:
                        last_log_progress = progress
                        write_log(f"进度: {progress:.1f}% | 速度: {speed_str} | 分片: {segments_str}")
                except (json.JSONDecodeError, KeyError):
                    pass

        # 等待进程结束
        proc.wait()
        stderr_output = proc.stderr.read() if proc.stderr else ''

        if proc.returncode == 0:
            write_log("yt-dlp 下载完成")
            update_task(task_id, progress=100, speed="", segments="", stage="merging")
            _post_download(task_id, download_dir, temp_dir, write_log, download_handle)
        elif download_handle.stop_flag.is_set():
            pass  # 已处理
        else:
            error_msg = stderr_output.strip().split('\n')[-1][:200] if stderr_output else f"yt-dlp 退出码: {proc.returncode}"
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
                   write_log, download_handle: DownloadThread):
    """下载完成后处理：移动文件、NAS 转移"""
    # 查找下载的 mp4 文件
    mp4_files = list(Path(temp_dir).glob(f"{task_id}*.mp4"))
    if not mp4_files:
        write_log("下载完成但未找到 MP4 文件", "ERROR")
        update_task(task_id, status="failed", stage="failed", error="下载完成但未找到 MP4 文件")
        download_handle.set_exit_code(1)
        return

    mp4_path = mp4_files[0]
    write_log(f"yt-dlp 下载完成，文件: {mp4_path.name}")
    if mp4_path.exists():
        write_log(f"文件大小: {mp4_path.stat().st_size / (1024*1024):.2f} MB")

    if mp4_path.stat().st_size == 0:
        write_log("文件大小为 0", "ERROR")
        update_task(task_id, status="failed", stage="failed", error="文件大小为 0")
        download_handle.set_exit_code(1)
        return

    write_log(f"开始移动文件到: {download_dir}")
    # 文件名长度保护（Windows MAX_PATH 260 字符限制）
    mp4_name = mp4_path.name
    if len(mp4_name) > 200:
        stem = mp4_path.stem[:180]
        mp4_name = stem + mp4_path.suffix
    final_mp4 = Path(download_dir) / mp4_name
    # 防止文件名冲突
    if final_mp4.exists():
        stem = final_mp4.stem
        suffix = final_mp4.suffix
        counter = 1
        while final_mp4.exists():
            final_mp4 = Path(download_dir) / f"{stem}_{counter}{suffix}"
            counter += 1
    try:
        shutil.move(str(mp4_path), str(final_mp4))
        write_log(f"文件已移动到: {final_mp4}")
    except Exception as move_err:
        write_log(f"移动失败，回退到复制: {move_err}", "WARN")
        shutil.copy2(str(mp4_path), str(final_mp4))
        mp4_path.unlink()
        write_log(f"文件已复制到: {final_mp4}")

    _cleanup_temp(temp_dir, task_id)
    write_log("临时文件已清理")
    update_task(task_id, status="completed", stage="completed",
                progress=100, file=str(final_mp4))
    write_log("任务完成 - 状态已更新为 completed")

    # 检查是否启用 NAS 转移
    cfg_now = get_download_config()
    if cfg_now.get("move_to_nas", "true") == "true":
        t = get_task(task_id)
        if t:
            write_log("开始 NAS 转移...")
            update_task(task_id, stage="moving", progress=0, move_speed="", move_elapsed="")
            from app.services.mover import move_to_media_library
            move_to_media_library(task_id, str(final_mp4), t["name"] + ".mp4")
            write_log("NAS 转移完成")

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

    logger.info(f"[下载] 开始任务 {task_id}: {task.get('title', '未知')}")

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

    # 下载前刷新 m3u8 URL
    fresh_url = refresh_m3u8_url(task_id)
    if fresh_url:
        m3u8_url = fresh_url
        logger.info(f"[download] {task_id}: 已刷新 m3u8 URL")
    else:
        logger.warning(f"[download] {task_id}: 刷新失败，使用缓存 URL")

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
    write_log(f"下载目录: {download_dir}")
    write_log(f"线程数: {thread_count}")
    write_log("下载模式: 子进程（GIL 隔离）")

    # 刷新 m3u8 URL（如果需要）
    refresh_url = task.get("refresh_url", "")
    if refresh_url:
        try:
            refreshed_url = refresh_m3u8_url(task_id)
            if refreshed_url:
                m3u8_url = refreshed_url
                write_log(f"m3u8 URL 已刷新")
        except Exception as e:
            write_log(f"刷新 m3u8 URL 异常: {e}", "WARN")

    # 构建 yt-dlp 命令
    cmd = _build_ytdlp_cmd(m3u8_url, temp_dir, task_id, thread_count, headers)
    write_log(f"yt-dlp 命令: {' '.join(cmd)}")

    # 更新任务状态
    update_task(task_id, status="downloading", stage="downloading", progress=0, error="")
    logger.info(f"[下载] 启动 yt-dlp 子进程")

    # 注册下载
    register_download(task_id, download_handle)

    # 启动 yt-dlp 子进程
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
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



