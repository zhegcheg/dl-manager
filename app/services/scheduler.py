"""
APScheduler 调度器：每个订阅源独立的定时轮询

每个 subscription_source 有自己的 poll_cron 字段（cron 表达式），
调度器为每个启用的源创建独立的 job。
"""
import logging
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError
from app.db.database import list_sources, get_source

logger = logging.getLogger("dl-manager")

scheduler = BackgroundScheduler()
_scheduler_lock = threading.Lock()

# job ID 前缀，便于管理
_JOB_PREFIX = "source_poll_"


def _poll_single_source(source_id: int):
    """轮询单个订阅源"""
    source = get_source(source_id)
    if not source or not source.get("enabled"):
        return
    try:
        from app.services.rss_poller import poll_webpage_source, poll_rss_source
        feed_type = source.get("feed_type", "webpage")
        if feed_type == "rss":
            new_tasks = poll_rss_source(source)
        elif feed_type == "m3u8_direct":
            return
        else:
            new_tasks = poll_webpage_source(source)
        if new_tasks:
            from app.services.queue import try_start_next
            try_start_next()
        logger.info(f"[scheduler] 源 '{source['name']}' 轮询完成，新增 {len(new_tasks)} 个任务")
    except Exception as e:
        logger.error(f"[scheduler] 源 '{source.get('name', source_id)}' 轮询失败: {e}")


def _source_job_id(source_id: int) -> str:
    return f"{_JOB_PREFIX}{source_id}"


def _parse_cron(cron_expr: str):
    """解析 cron 表达式（5 段），返回 CronTrigger 或 None"""
    if not cron_expr:
        return None
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return None
    minute, hour, day, month, day_of_week = parts
    try:
        return CronTrigger(
            minute=minute, hour=hour, day=day, month=month,
            day_of_week=day_of_week, timezone="Asia/Shanghai"
        )
    except Exception as e:
        logger.warning(f"[scheduler] 无效 cron 表达式 '{cron_expr}': {e}")
        return None


def _add_source_job(source: dict):
    """为单个订阅源添加调度任务"""
    source_id = source["id"]
    cron_expr = source.get("poll_cron", "0 4 * * *")
    if not source.get("enabled"):
        return

    trigger = _parse_cron(cron_expr)
    if not trigger:
        logger.warning(f"[scheduler] 源 '{source['name']}' 的 cron 无效: {cron_expr}，跳过")
        return

    job_id = _source_job_id(source_id)
    scheduler.add_job(
        _poll_single_source, trigger=trigger,
        args=[source_id], id=job_id,
        name=f"轮询: {source['name']}",
        replace_existing=True,
    )
    logger.info(f"[scheduler] 已注册源 '{source['name']}' 定时轮询 (cron: {cron_expr})")


def start_scheduler():
    """启动调度器，为所有启用的订阅源创建 job"""
    if scheduler.running:
        return
    scheduler.start()

    sources = list_sources(enabled_only=True)
    for src in sources:
        _add_source_job(src)
    logger.info(f"[scheduler] 调度器已启动，已注册 {len(sources)} 个订阅源")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[scheduler] 调度器已停止")


def refresh_source_job(source_id: int):
    """刷新单个订阅源的调度（添加/更新/删除后调用）"""
    if not scheduler.running:
        return
    job_id = _source_job_id(source_id)
    try:
        scheduler.remove_job(job_id)
    except JobLookupError:
        pass

    source = get_source(source_id)
    if source and source.get("enabled"):
        _add_source_job(source)


def remove_source_job(source_id: int):
    """移除单个订阅源的调度"""
    if not scheduler.running:
        return
    job_id = _source_job_id(source_id)
    try:
        scheduler.remove_job(job_id)
    except JobLookupError:
        pass


def refresh_all_jobs():
    """刷新所有订阅源的调度（全量重建）"""
    if not scheduler.running:
        return
    # 移除所有现有源 job
    for job in scheduler.get_jobs():
        if job.id.startswith(_JOB_PREFIX):
            try:
                scheduler.remove_job(job.id)
            except JobLookupError:
                pass
    # 重新添加
    sources = list_sources(enabled_only=True)
    for src in sources:
        _add_source_job(src)


# 保留向后兼容：旧的 reschedule 函数名
def reschedule():
    """向后兼容：等价于 refresh_all_jobs"""
    refresh_all_jobs()
