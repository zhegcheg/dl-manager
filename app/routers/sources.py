"""
订阅源相关 API 路由
"""
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.database import (
    add_source, list_sources, get_source, update_source, delete_source,
)

router = APIRouter()


class SourceCreate(BaseModel):
    name: str
    url: str
    feed_type: str = "webpage"
    poll_cron: str = "0 4 * * *"
    page_url_pattern: str = ""
    title_selector: str = ""
    m3u8_selector: str = ""
    video_id_pattern: str = ""
    referer: str = ""
    headers: str = ""
    key_selector: str = ""
    iv_selector: str = ""
    refresh_url_pattern: str = ""


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    feed_type: Optional[str] = None
    enabled: Optional[bool] = None
    poll_cron: Optional[str] = None
    page_url_pattern: Optional[str] = None
    title_selector: Optional[str] = None
    m3u8_selector: Optional[str] = None
    video_id_pattern: Optional[str] = None
    referer: Optional[str] = None
    headers: Optional[str] = None
    key_selector: Optional[str] = None
    iv_selector: Optional[str] = None
    refresh_url_pattern: Optional[str] = None


@router.get("/api/sources")
def get_sources():
    return {"list": list_sources()}


@router.post("/api/sources")
def create_source(body: SourceCreate):
    data = body.model_dump()
    name = data.pop("name")
    url = data.pop("url")
    feed_type = data.pop("feed_type")
    src = add_source(name, url, feed_type, **data)
    # 为新源注册定时任务
    from app.services.scheduler import refresh_source_job
    refresh_source_job(src["id"])
    return {"data": src}


@router.put("/api/sources/{source_id}")
def put_source(source_id: int, body: SourceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    src = update_source(source_id, **fields)
    if not src:
        raise HTTPException(404, "Source not found")
    # 更新定时任务
    from app.services.scheduler import refresh_source_job
    refresh_source_job(source_id)
    return {"data": src}


@router.patch("/api/sources/{source_id}")
def patch_source(source_id: int, body: SourceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    src = update_source(source_id, **fields)
    if not src:
        raise HTTPException(404, "Source not found")
    # 更新定时任务（启用/禁用时特别重要）
    from app.services.scheduler import refresh_source_job
    refresh_source_job(source_id)
    return {"data": src}


@router.delete("/api/sources/{source_id}")
def del_source(source_id: int):
    # 先移除定时任务
    from app.services.scheduler import remove_source_job
    remove_source_job(source_id)
    delete_source(source_id)
    return {"message": "Deleted"}


@router.post("/api/rss/poll")
def trigger_rss():
    """手动触发所有订阅源轮询"""
    from app.services.rss_poller import poll_all_sources
    from app.services.queue import try_start_next
    new_tasks = poll_all_sources()
    started = try_start_next()
    return {
        "message": f"轮询完成，新增 {len(new_tasks)} 个任务，已启动 {started} 个",
        "count": len(new_tasks),
        "started": started,
    }


@router.post("/api/sources/{source_id}/poll")
def trigger_source_poll(source_id: int):
    """手动触发单个订阅源轮询"""
    source = get_source(source_id)
    if not source:
        raise HTTPException(404, "Source not found")
    from app.services.rss_poller import poll_webpage_source, poll_rss_source
    from app.services.queue import try_start_next
    feed_type = source.get("feed_type", "webpage")
    if feed_type == "rss":
        new_tasks = poll_rss_source(source)
    else:
        new_tasks = poll_webpage_source(source)
    started = try_start_next()
    return {
        "message": f"源 '{source['name']}' 轮询完成，新增 {len(new_tasks)} 个任务，已启动 {started} 个",
        "count": len(new_tasks),
        "started": started,
    }
