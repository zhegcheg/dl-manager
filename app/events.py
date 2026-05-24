"""
任务变更事件总线 (SSE)
- 线程安全：worker 线程调用 mark_dirty() 标记变更
- 后台任务定期广播完整任务列表到所有 SSE 订阅者
"""
import asyncio
import threading

# SSE 订阅者（每个是一个 asyncio.Queue）
_subscribers: set = set()
_subscribers_lock = threading.Lock()

# 脏标记：有任务变更时置 True，广播后清 False
_dirty = False
_dirty_lock = threading.Lock()


def mark_dirty():
    """标记有任务变更（线程安全，供 worker 线程调用）"""
    global _dirty
    with _dirty_lock:
        _dirty = True


async def subscribe() -> asyncio.Queue:
    """新增一个 SSE 订阅者，返回其消息队列"""
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    with _subscribers_lock:
        _subscribers.add(q)
    return q


async def unsubscribe(q: asyncio.Queue):
    """移除 SSE 订阅者"""
    with _subscribers_lock:
        _subscribers.discard(q)


async def broadcast_worker():
    """
    后台广播任务（在 lifespan 中启动）
    每 500ms 检查是否有变更，有则推送完整任务列表给所有订阅者
    """
    global _dirty
    from app.db.database import list_tasks

    while True:
        await asyncio.sleep(0.5)

        with _dirty_lock:
            if not _dirty:
                continue
            _dirty = False

        # 获取完整任务列表（去掉 m3u8_url 减小体积）
        tasks = list_tasks()
        for t in tasks:
            t.pop("m3u8_url", None)

        payload = {"total": len(tasks), "list": tasks}

        with _subscribers_lock:
            dead = []
            for q in _subscribers:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # 队列满的订阅者可能已断开，标记清理
                    dead.append(q)
            for q in dead:
                _subscribers.discard(q)
