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
    feed_type: str = "jable"


class SourceUpdate(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    feed_type: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/api/sources")
def get_sources():
    return {"list": list_sources()}


@router.post("/api/sources")
def create_source(body: SourceCreate):
    src = add_source(body.name, body.url, body.feed_type)
    return {"data": src}


@router.put("/api/sources/{source_id}")
def put_source(source_id: int, body: SourceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    src = update_source(source_id, **fields)
    if not src:
        raise HTTPException(404, "Source not found")
    return {"data": src}


@router.patch("/api/sources/{source_id}")
def patch_source(source_id: int, body: SourceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    src = update_source(source_id, **fields)
    if not src:
        raise HTTPException(404, "Source not found")
    return {"data": src}


@router.delete("/api/sources/{source_id}")
def del_source(source_id: int):
    delete_source(source_id)
    return {"message": "Deleted"}


@router.post("/api/rss/poll")
def trigger_rss():
    from app.services.rss_poller import poll_all_sources
    from app.services.queue import try_start_next
    new_tasks = poll_all_sources()
    started = try_start_next()
    return {
        "message": f"RSS 轮询完成，新增 {len(new_tasks)} 个任务，已启动 {started} 个",
        "count": len(new_tasks),
        "started": started,
    }
