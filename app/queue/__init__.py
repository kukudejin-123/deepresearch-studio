"""
任务队列工厂

按环境变量选择实现：
- TASK_QUEUE=memory（默认）：进程内 asyncio 队列；
- TASK_QUEUE=arq：Redis + arq 分布式队列，需要 TASK_QUEUE_REDIS_URL。
"""

import os
from typing import Optional

from app.queue.base import SubmittedTask, TaskQueue
from app.queue.memory_queue import MemoryTaskQueue
from app.utils.logging_config import get_logger

logger = get_logger(__name__)

_queue: Optional[TaskQueue] = None


def get_task_queue() -> TaskQueue:
    """
    获取进程级任务队列单例

    :return: MemoryTaskQueue 或 ArqTaskQueue
    """
    global _queue
    if _queue is not None:
        return _queue

    queue_type = os.getenv("TASK_QUEUE", "memory").lower()
    if queue_type == "arq":
        redis_url = os.getenv("TASK_QUEUE_REDIS_URL", "redis://127.0.0.1:6379/0")
        # 延迟导入：未安装 arq 时 memory 模式不受影响
        from app.queue.arq_queue import ArqTaskQueue

        _queue = ArqTaskQueue(redis_url)
        logger.info("任务队列: arq（Redis: %s）", redis_url)
    else:
        _queue = MemoryTaskQueue()
        logger.info("任务队列: memory（进程内执行）")
    return _queue


__all__ = ["MemoryTaskQueue", "SubmittedTask", "TaskQueue", "get_task_queue"]
