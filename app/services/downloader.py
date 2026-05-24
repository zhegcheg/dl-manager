"""
yt-dlp 下载器
支持多线程分片下载、断点续传、代理配置、自动合并为 MP4
"""
import threading
import time
import shutil
from pathlib import Path
from typing import Optional
import yt_dlp
from app.db.database import update_task, get_task, get_download_config, get_proxy_config
from app.services.queue import register_download, unregister_download


POLL_INTERVAL = 1  # DB 更新频率（秒），避免写入太频繁


class DownloadThread:
    """下载线程包装，提供类似 subprocess.Popen 的接口"""

    def __init__(self, task_id: str, thread: Optional[threading.Thread] = None):
        self._thread = thread
        self._task_id = task_id
        self._stop_flag = threading.Event()
        self._exit_code: Optional[int] = None

    def poll(self) -> Optional[int]:
        """返回退出码，None 表示仍在运行"""
        if self._thread is None or not self._thread.is_alive():
            return self._exit_code if self._exit_code is not None else 0
        return None
    
    def terminate(self):
        """请求停止下载"""
        self._stop_flag.set()
    
    def wait(self, timeout: float = None):
        """等待下载完成"""
        self._thread.join(timeout=timeout)
    
    @property
    def stop_flag(self) -> threading.Event:
        return self._stop_flag
    
    def set_exit_code(self, code: int):
        self._exit_code = code


def make_progress_hook(task_id: str, download_thread: DownloadThread):
    """创建 yt-dlp 进度回调"""
    last_update = 0.0
    
    def progress_hook(d):
        nonlocal last_update
        
        # 检查是否需要停止
        if download_thread.stop_flag.is_set():
            raise yt_dlp.utils.DownloadCancelled("Download cancelled by user")
        
        now = time.time()
        if now - last_update < POLL_INTERVAL:
            return
        last_update = now
        
        if d['status'] == 'downloading':
            # 提取进度信息
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            progress = (downloaded / total * 100) if total > 0 else 0
            
            # 提取速度
            speed = d.get('speed')
            speed_str = format_speed(speed) if speed else ""
            
            # 提取分片信息
            fragment_index = d.get('fragment_index', 0)
            fragment_count = d.get('fragment_count', 0)
            segments_str = f"{fragment_index}/{fragment_count}" if fragment_count > 0 else ""
            
            update_task(task_id, progress=progress, speed=speed_str, segments=segments_str)
            
        elif d['status'] == 'finished':
            update_task(task_id, progress=100, speed="", segments="", stage="merging")
    
    return progress_hook


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


def start_download(task_id: str, m3u8_url: str, headers: str = "", key: str = "", iv: str = ""):
    """
    启动 yt-dlp 下载任务
    
    流程: yt-dlp 下载分片到 temp_dir -> 合并为 MP4 -> 移动到 download_dir -> 转移到 NAS
    
    参数:
        task_id: 任务 ID
        m3u8_url: m3u8 播放列表 URL
        headers: 自定义 HTTP 头（格式: "Key1: Value1\\nKey2: Value2"）
        key: HLS 解密密钥（hex）
        iv: HLS 解密 IV（hex）
    
    返回:
        DownloadThread 对象（提供 poll/terminate/wait 接口）
    """
    cfg = get_download_config()
    download_dir = cfg.get("download_dir", str(Path.home() / ".jable-dl-server" / "tasks"))
    temp_dir = cfg.get("temp_dir", str(Path.home() / ".jable-dl-server" / "temp"))
    thread_count = int(cfg.get("thread_count", "8"))
    
    # 检查任务是否有自定义 download_dir
    task = get_task(task_id)
    if task and task.get("download_dir"):
        download_dir = task["download_dir"]
    
    # 确保目录存在
    Path(download_dir).mkdir(parents=True, exist_ok=True)
    Path(temp_dir).mkdir(parents=True, exist_ok=True)
    
    # 创建下载线程对象
    download_thread = DownloadThread(task_id)
    
    # 构建 yt-dlp 选项 — 分片下载到 temp_dir
    ydl_opts = {
        'outtmpl': f'{temp_dir}/{task_id}.%(ext)s',
        'concurrent_fragment_downloads': thread_count,  # 多线程分片下载
        'continuedl': True,  # 断点续传
        'merge_output_format': 'mp4',  # 自动合并为 MP4
        'progress_hooks': [make_progress_hook(task_id, download_thread)],
        'noprogress': True,  # 禁用控制台进度条
        'quiet': False,  # 允许输出日志
        'no_warnings': False,
        'retries': 10,  # 自动重试次数
        'fragment_retries': 10,  # 分片重试次数
        'socket_timeout': 30,  # 套接字超时
        'http_chunk_size': 10485760,  # 10MB 分块下载
    }
    
    # 添加代理支持
    proxy_cfg = get_proxy_config()
    if proxy_cfg.get("enabled") == "true" and proxy_cfg.get("host"):
        proxy_type = proxy_cfg.get("type", "http")
        host = proxy_cfg.get("host", "")
        port = proxy_cfg.get("port", "7890")
        if proxy_type == "socks5":
            ydl_opts['proxy'] = f"socks5://{host}:{port}"
        else:
            ydl_opts['proxy'] = f"http://{host}:{port}"
    
    # 添加自定义 HTTP 头
    if headers:
        http_headers = {}
        for line in headers.split("\n"):
            line = line.strip()
            if not line or ':' not in line:
                continue
            k, v = line.split(":", 1)
            http_headers[k.strip()] = v.strip()
        if http_headers:
            ydl_opts['http_headers'] = http_headers
    
    # 更新任务状态
    update_task(task_id, status="downloading", stage="downloading", progress=0, error="")
    
    # 注册下载
    register_download(task_id, download_thread)
    
    def _download_worker():
        """下载工作线程"""
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                # 开始下载
                info = ydl.extract_info(m3u8_url, download=True)
                
                # 检查是否被取消
                if download_thread.stop_flag.is_set():
                    update_task(task_id, status="stopped", stage="stopped", error="用户取消下载")
                    download_thread.set_exit_code(1)
                    return
                
                # 下载成功
                if info:
                    # 获取输出文件路径（在 temp_dir 中）
                    filename = ydl.prepare_filename(info)
                    mp4_path = Path(filename).with_suffix('.mp4')
                    
                    if mp4_path.exists() and mp4_path.stat().st_size > 0:
                        # 从 temp_dir 移动到 download_dir
                        final_mp4 = Path(download_dir) / mp4_path.name
                        try:
                            shutil.move(str(mp4_path), str(final_mp4))
                        except Exception as move_err:
                            # 如果移动失败（跨设备），回退到复制
                            shutil.copy2(str(mp4_path), str(final_mp4))
                            mp4_path.unlink()
                        
                        # 清理 temp_dir 中可能残留的分片文件
                        _cleanup_temp(temp_dir, task_id)
                        
                        update_task(task_id, status="completed", stage="completed",
                                  progress=100, file=str(final_mp4))
                        
                        # 检查是否启用 NAS 转移
                        cfg_now = get_download_config()
                        if cfg_now.get("move_to_nas", "true") == "true":
                            # 启动异步转移到 NAS
                            t = get_task(task_id)
                            if t:
                                update_task(task_id, stage="moving", progress=0, move_speed="", move_elapsed="")
                                from app.services.mover import move_to_media_library
                                move_to_media_library(task_id, str(final_mp4), t["name"] + ".mp4")
                        else:
                            # 不转移，直接完成
                            download_thread.set_exit_code(0)
                            return
                        
                        download_thread.set_exit_code(0)
                        return
                
                # 文件未生成
                update_task(task_id, status="failed", stage="failed", error="下载完成但文件未生成")
                download_thread.set_exit_code(1)
                
        except yt_dlp.utils.DownloadCancelled as e:
            update_task(task_id, status="stopped", stage="stopped", error=str(e))
            download_thread.set_exit_code(1)
            
        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e).split('\n')[0][:200]  # 截取错误信息
            update_task(task_id, status="failed", stage="failed", error=f"下载失败: {error_msg}")
            download_thread.set_exit_code(1)
            
        except Exception as e:
            update_task(task_id, status="failed", stage="failed", error=f"未知错误: {e}")
            download_thread.set_exit_code(1)
            
        finally:
            unregister_download(task_id)
    
    # 创建并启动工作线程
    worker = threading.Thread(target=_download_worker, daemon=True)
    download_thread._thread = worker
    worker.start()
    
    return download_thread


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



