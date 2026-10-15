"""
事件持久化与分发包

- EventStore：任务事件落库（SQLite），支持断线回放；
- RedisEventBridge：跨进程事件桥（worker -> API -> WebSocket）；
- get_event_store / get_event_bridge：进程级单例工厂。
"""

import os
from pathlib import Path

from app.events.bridge import RedisEventBridge
from app.events.store import EventStore

__all__ = ["EventStore", "RedisEventBridge", "get_event_bridge", "get_event_store"]

# 当前文件位于 app/events/__init__.py，parents[1] 即 app 目录
_APP_ROOT = Path(__file__).resolve().parents[1]

_event_store: EventStore | None = None
_event_bridge: RedisEventBridge | None = None


def get_event_store() -> EventStore:
    """获取进程级 EventStore 单例（默认 app/data/events.db）"""
    global _event_store
    if _event_store is None:
        db_path = os.getenv("EVENT_DB", str(_APP_ROOT / "data" / "events.db"))
        _event_store = EventStore(db_path)
    return _event_store


def get_event_bridge() -> RedisEventBridge | None:
    """
    获取 Redis 事件桥（未配置 TASK_QUEUE_REDIS_URL 时返回 None）

    只有 arq 队列模式才需要跨进程桥；单机 memory 模式下 monitor 直接
    推送到进程内 WebSocket 管理器。
    """
    global _event_bridge
    if _event_bridge is None:
        redis_url = os.getenv("TASK_QUEUE_REDIS_URL")
        if not redis_url:
            return None
        _event_bridge = RedisEventBridge(redis_url)
    return _event_bridge
