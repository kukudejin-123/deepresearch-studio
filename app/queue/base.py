"""
任务队列抽象层

第二期改造的核心：把 Agent 执行从 FastAPI 请求进程里解耦出来。

抽象出一个 TaskQueue 协议，两种实现：

- MemoryTaskQueue：进程内 asyncio 后台任务（默认，开发/单机模式），
  行为与改造前完全一致；
- ArqTaskQueue：基于 Redis + arq 的分布式队列（生产模式），
  API 进程只负责入队，真正的 Agent 执行在独立 worker 进程中完成，
  服务重启不丢已入队任务，天然支持多 worker 水平扩展。

通过 TASK_QUEUE 环境变量切换（memory / arq）。
"""

from dataclasses import dataclass
from typing import Any, Optional, Protocol


@dataclass
class SubmittedTask:
    """任务提交结果"""

    job_id: str
    thread_id: str
    queue_type: str


class TaskQueue(Protocol):
    """任务队列协议：提交、取消、状态查询"""

    queue_type: str

    async def submit(
        self,
        query: str,
        thread_id: str,
        user_id: Optional[int],
    ) -> SubmittedTask:
        """提交一个 Agent 任务"""
        ...

    async def cancel(self, job_id: str) -> str:
        """
        取消任务

        :return: cancelled / cancelling / not_found
        """
        ...

    async def get_job_status(self, job_id: str) -> Optional[dict[str, Any]]:
        """查询队列侧的任务状态；无法查询时返回 None"""
        ...
