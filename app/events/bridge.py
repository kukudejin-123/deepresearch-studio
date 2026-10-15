"""
Redis 事件桥模块

任务执行从 API 进程移到 arq worker 后，出现一个典型的分布式问题：
WebSocket 连接在 API 进程，而 monitor 事件产生于 worker 进程。

本模块用 Redis Pub/Sub 把两者连起来：

    worker 进程: monitor._emit -> RedisBridge.publish(thread_id, payload)
    API 进程:    RedisBridge.subscriber_loop() -> ConnectionManager.send_to_thread()

同时事件写入 SQLite EventStore，供断线回放。

未配置 Redis 时（TASK_QUEUE=memory），桥不启用，monitor 直接走进程内
WebSocket 管理器，行为与单机版一致。
"""

import asyncio
import contextlib
import json
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as aioredis

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 事件频道前缀，完整频道名为 {prefix}{thread_id}
CHANNEL_PREFIX = "deepresearch:events:"


class RedisEventBridge:
    """
    基于 Redis Pub/Sub 的事件桥

    发布端（worker）与订阅端（API）各自持有实例，互不依赖进程内状态。
    """

    def __init__(self, redis_url: str):
        """
        :param redis_url: Redis 连接串，例如 redis://127.0.0.1:6379/0
        """
        self.redis_url = redis_url
        self._publish_client: Optional[aioredis.Redis] = None
        self._subscriber: Optional[aioredis.Redis] = None
        self._task: Optional[asyncio.Task] = None

    async def _get_publish_client(self) -> aioredis.Redis:
        """发布客户端（惰性创建，进程内复用连接）"""
        if self._publish_client is None:
            self._publish_client = aioredis.from_url(self.redis_url)
        return self._publish_client

    async def publish(self, thread_id: str, payload: dict[str, Any]) -> None:
        """
        发布一条事件到对应 thread 的频道

        发布失败只记日志，不阻断任务执行——事件已同时落库，丢一条实时推送
        不影响回放完整性。
        """
        try:
            client = await self._get_publish_client()
            await client.publish(
                f"{CHANNEL_PREFIX}{thread_id}",
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception as e:
            logger.warning("事件发布失败: thread_id=%s, error=%s", thread_id, e)

    async def subscriber_loop(
        self,
        thread_ids_provider: Callable[[], list[str]],
        handler: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """
        订阅循环：动态订阅活跃 thread 的频道并转发事件

        :param thread_ids_provider: 返回当前需要订阅的 thread_id 列表
            （由调用方从 WebSocket 连接注册表实时获取）
        :param handler: 事件回调 (thread_id, payload)
        """
        self._subscriber = aioredis.from_url(self.redis_url)
        pubsub = self._subscriber.pubsub()
        subscribed: set[str] = set()

        try:
            while True:
                # 动态订阅：新建立的 WebSocket 连接对应的 thread 需要及时订阅
                wanted = set(thread_ids_provider())
                for thread_id in wanted - subscribed:
                    await pubsub.subscribe(f"{CHANNEL_PREFIX}{thread_id}")
                    subscribed.add(thread_id)
                for thread_id in subscribed - wanted:
                    await pubsub.unsubscribe(f"{CHANNEL_PREFIX}{thread_id}")
                    subscribed.discard(thread_id)

                # 零订阅时不能调用 get_message：redis-py 会抛
                # "pubsub connection not set" 并中断整个订阅循环——
                # API 启动后、首个 WebSocket 建立前正是这个状态
                if not subscribed:
                    await asyncio.sleep(0.5)
                    continue

                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is None or message.get("type") != "message":
                    continue

                channel = message["channel"]
                if isinstance(channel, bytes):
                    channel = channel.decode("utf-8")
                thread_id = channel[len(CHANNEL_PREFIX) :]
                try:
                    payload = json.loads(message["data"])
                except (json.JSONDecodeError, TypeError):
                    continue
                await handler(thread_id, payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Redis 订阅循环异常: %s", e)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.close()

    def start_subscriber(
        self,
        thread_ids_provider: Callable[[], list[str]],
        handler: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> asyncio.Task:
        """在 API 进程启动订阅循环（后台任务）"""
        self._task = asyncio.create_task(self.subscriber_loop(thread_ids_provider, handler))
        logger.info("Redis 事件桥订阅循环已启动")
        return self._task

    async def close(self) -> None:
        """关闭订阅任务与连接"""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for client in (self._publish_client, self._subscriber):
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
        self._publish_client = None
        self._subscriber = None
        logger.info("Redis 事件桥已关闭")
