#!/usr/bin/env python3
"""
Jable Download Manager - Main Entry
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from task_db import init
from api import app as api_app
from scheduler import start_scheduler, stop_scheduler
from queue_manager import try_start_next, cleanup_finished

init()

main_app = FastAPI(title="DL Manager")
main_app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
main_app.include_router(api_app)

web_dir = os.path.join(os.path.dirname(__file__), "web")
if os.path.exists(web_dir):
    main_app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

_start_scheduler_called = False

@main_app.on_event("startup")
def on_startup():
    global _start_scheduler_called
    if not _start_scheduler_called:
        _start_scheduler_called = True
        start_scheduler()
        # 服务启动时清理僵尸进程 + 自动启动等待任务
        cleanup_finished()
        try_start_next()

@main_app.on_event("shutdown")
def on_shutdown():
    stop_scheduler()

if __name__ == "__main__":
    uvicorn.run(main_app, host="0.0.0.0", port=8899, log_level="info")