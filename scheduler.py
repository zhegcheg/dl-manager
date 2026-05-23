"""
APScheduler 调度器：每日凌晨 4:00 执行 RSS 轮询
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from task_db import get_scheduler_config, set_scheduler_config
from rss_poller import poll_all_sources

scheduler = BackgroundScheduler()
_scheduler_lock = threading.Lock()

def tick_rss():
    with _scheduler_lock:
        new_tasks = poll_all_sources()
        print(f"[scheduler] RSS 轮询完成，新增 {len(new_tasks)} 个任务")
        return new_tasks

def start_scheduler():
    if scheduler.running:
        return
    config = get_scheduler_config()
    cron_expr = config.get("rss_cron", "0 4 * * *")
    enabled = config.get("rss_enabled", "true") == "true"
    if enabled:
        parts = cron_expr.split()
        if len(parts) == 5:
            minute, hour = parts[0], parts[1]
            trigger = CronTrigger(hour=hour, minute=minute, timezone="Asia/Shanghai")
            scheduler.add_job(tick_rss, trigger=trigger, id="rss_daily", name="每日 RSS 轮询")
            print(f"[scheduler] 已注册每日 {hour}:{minute.zfill(2)} RSS 轮询")
    scheduler.start()
    print("[scheduler] 调度器已启动")

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        print("[scheduler] 调度器已停止")

def reschedule():
    if not scheduler.running:
        return
    scheduler.remove_job("rss_daily", quiet=True)
    config = get_scheduler_config()
    enabled = config.get("rss_enabled", "true") == "true"
    if enabled:
        cron_expr = config.get("rss_cron", "0 4 * * *")
        parts = cron_expr.split()
        if len(parts) == 5:
            minute, hour = parts[0], parts[1]
            trigger = CronTrigger(hour=hour, minute=minute, timezone="Asia/Shanghai")
            scheduler.add_job(tick_rss, trigger=trigger, id="rss_daily", name="每日 RSS 轮询")
            print(f"[scheduler] 已更新调度: {hour}:{minute.zfill(2)}")