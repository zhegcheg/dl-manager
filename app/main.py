"""
FastAPI Application Factory
"""
import os
import sys
import asyncio
import logging
from contextlib import asynccontextmanager
from collections import deque

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from app.db.database import init, get_log_config
from app.routers import tasks, sources, config
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.queue import try_start_next, cleanup_finished
from app.events import broadcast_worker

# 全局日志队列（最多保留 10000 条）
system_log_queue = deque(maxlen=10000)
system_log_clients = set()

class SSELogHandler(logging.Handler):
    """自定义日志 Handler，将日志写入内存队列并广播给 SSE 客户端"""
    def emit(self, record):
        log_msg = self.format(record)
        system_log_queue.append(log_msg)
        # 通知所有 SSE 客户端
        for client in list(system_log_clients):
            try:
                client.put_nowait(log_msg)
            except:
                pass

# 配置 Python logging
logger = logging.getLogger("dl-manager")
logger.setLevel(logging.DEBUG)

# 控制台输出
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
console_handler.setFormatter(console_fmt)
logger.addHandler(console_handler)

# SSE 输出
sse_handler = SSELogHandler()
sse_handler.setLevel(logging.DEBUG)
sse_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
sse_handler.setFormatter(sse_fmt)
logger.addHandler(sse_handler)

# 文件输出 Handler（延迟初始化，在 lifespan 中根据配置添加）
file_handler = None

def setup_file_handler(log_path: str, log_level: str):
    """根据配置添加或更新文件日志 Handler"""
    global file_handler
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR
    }
    level = level_map.get(log_level.upper(), logging.INFO)
    
    # 移除旧的 file handler
    if file_handler:
        logger.removeHandler(file_handler)
    
    # 创建目录
    from pathlib import Path
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    
    # 添加新的文件 handler
    file_handler = logging.FileHandler(log_path, encoding='utf-8')
    file_handler.setLevel(level)
    file_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)
    logger.info(f"日志文件输出已启用: {log_path} (级别: {log_level})")

def update_log_level(log_level: str):
    """动态更新日志级别（控制台和文件 Handler）"""
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR
    }
    level = level_map.get(log_level.upper(), logging.INFO)
    console_handler.setLevel(level)
    if file_handler:
        file_handler.setLevel(level)
    logger.info(f"日志级别已更新为: {log_level}")

# 将全局 logger 暴露给其他模块
import app
app.logger = logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动/关闭时执行"""
    # 增大默认线程池（64），避免同步端点与 SSE run_in_executor 争用线程
    import concurrent.futures
    loop = asyncio.get_event_loop()
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="worker")
    )
    # startup
    init()
    # 读取日志配置并应用
    try:
        log_cfg = get_log_config()
        log_level = log_cfg.get("log_level", "INFO")
        log_path = log_cfg.get("log_path", "")
        if log_path:
            setup_file_handler(log_path, log_level)
        else:
            # 使用默认路径
            from pathlib import Path
            default_log_path = str(Path.home() / ".dl-manager" / "logs" / "dl-manager.log")
            setup_file_handler(default_log_path, log_level)
    except Exception as e:
        logger.warning(f"读取日志配置失败，使用默认设置: {e}")
    
    start_scheduler()
    cleanup_finished()
    try_start_next()
    # 启动 SSE 事件广播后台任务
    broadcast_task = asyncio.create_task(broadcast_worker())
    logger.info("DL Manager 启动成功")
    yield
    # shutdown
    broadcast_task.cancel()
    stop_scheduler()
    logger.info("DL Manager 已关闭")


async def system_logs_sse():
    """SSE 端点：推送系统日志"""
    queue = asyncio.Queue()
    system_log_clients.add(queue)
    try:
        # 先发送历史日志
        for log in system_log_queue:
            yield f"data: {log}\n\n"
        # 持续推送新日志
        while True:
            try:
                log = await asyncio.wait_for(queue.get(), timeout=30)
                yield f"data: {log}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"  # 心跳保持连接
    finally:
        system_log_clients.discard(queue)


# 添加系统日志路由
from fastapi import APIRouter
system_router = APIRouter()

@system_router.get("/api/system/logs")
async def get_system_logs():
    """获取系统日志（SSE 推送）"""
    return StreamingResponse(system_logs_sse(), media_type="text/event-stream")


def create_app() -> FastAPI:
    app = FastAPI(title="DL Manager", lifespan=lifespan)

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 路由
    app.include_router(tasks.router)
    app.include_router(sources.router)
    app.include_router(config.router)
    app.include_router(system_router)

    # 静态文件
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    web_dir = os.path.join(base_dir, "web")
    nas_dir = os.getenv("NAS_MEDIA_DIR", "/mnt/fn-nas-imovie")

    if os.path.exists(nas_dir):
        app.mount("/nas", StaticFiles(directory=nas_dir, html=False), name="nas")
    if os.path.exists(web_dir):
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app
