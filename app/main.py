"""
FastAPI Application Factory
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.db.database import init
from app.routers import tasks, sources, config
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.queue import try_start_next, cleanup_finished


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动/关闭时执行"""
    # startup
    init()
    start_scheduler()
    cleanup_finished()
    try_start_next()
    yield
    # shutdown
    stop_scheduler()


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

    # 静态文件
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    web_dir = os.path.join(base_dir, "web")
    nas_dir = "/mnt/fn-nas-imovie"

    if os.path.exists(nas_dir):
        app.mount("/nas", StaticFiles(directory=nas_dir, html=False), name="nas")
    if os.path.exists(web_dir):
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app
