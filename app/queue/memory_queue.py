"""
进程内任务队列（memory 模式）

默认实现：用 asyncio.create_task 在 API 进程内直接执行 Agent 任务。
适合本地开发、单机部署；重启后运行中任务丢失（已由 TaskStore 记录状态，
可查询但不会自动恢复执行）。
"""

import asyncio
from typing import Any, Optional

from app.agent.main_agent import run_deep_agent
from app.queue.base import SubmittedTask


class MemoryTaskQueue:
    """进程内 asyncio 任务队列"""

    queue_type = "memory"

    def __init__(self):
        # thread_id -> 后台 asyncio.Task
        self._tasks: dict[str, asyncio.Task] = {}

    def _forget(self, thread_id: str, task: asyncio.Task) -> None:
        """任务结束后清理登记；仅当登记的仍是同一个 task 时才删除"""
        if self._tasks.get(thread_id) is task:
            self._tasks.pop(thread_id, None)

    async def submit(
        self,
        query: str,
        thread_id: str,
        user_id: Optional[int],
    ) -> SubmittedTask:
        """
        在进程内启动后台任务

        同一 thread_id 只保留一个活跃任务，新任务先取消旧任务，
        避免并发写同一会话目录。
        """
        old_task = self._tasks.get(thread_id)
        if old_task and not old_task.done():
            old_task.cancel()

        task = asyncio.create_task(run_deep_agent(query, thread_id, user_id))
        self._tasks[thread_id] = task
        task.add_done_callback(lambda finished: self._forget(thread_id, finished))

        return SubmittedTask(
            job_id=thread_id,
            thread_id=thread_id,
            queue_type=self.queue_type,
        )

    async def cancel(self, job_id: str) -> str:
        """取消进程内任务（与改造前的接口行为一致）"""
        task = self._tasks.get(job_id)
        if not task or task.done():
            self._tasks.pop(job_id, None)
            return "not_found"

        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except asyncio.CancelledError:
            self._forget(job_id, task)
            return "cancelled"
        except asyncio.TimeoutError:
            return "cancelling"
        except Exception:
            self._forget(job_id, task)
            return "cancelled"

        self._forget(job_id, task)
        return "cancelled"

    async def get_job_status(self, job_id: str) -> Optional[dict[str, Any]]:
        """查询进程内任务的运行状态"""
        task = self._tasks.get(job_id)
        if task is None:
            return None
        if task.done():
            return {"status": "done", "cancelled": task.cancelled()}
        return {"status": "running"}
