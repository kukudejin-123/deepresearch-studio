"""
Redis + arq 分布式任务队列（arq 模式）

生产模式实现：API 进程只把任务入队到 Redis，真正的 Agent 执行在独立的
arq worker 进程（app.worker.tasks）中完成。

相对进程内队列的优势：
- API 进程重启不丢已入队任务（Redis 持久化队列）；
- 多 worker 水平扩展，任务自动分发；
- 执行进程与接口进程隔离，接口延迟不再受长任务影响。

代价与配套设计：
- WebSocket 连接在 API 进程、事件产生于 worker 进程，因此事件经
  Redis pub/sub 桥转发（见 app.events.bridge）；
- 任务状态以 TaskStore（SQLite）为准，arq 的 job 状态作为补充；
- 运行中任务的取消采用协作式：取消接口把状态置为 cancelled，
  worker 在流式执行过程中定期检查并主动结束。
"""

import asyncio
from typing import Any, Optional

from arq import ArqRedis
from arq.jobs import Job, JobStatus, ResultNotFound

from app.queue.base import SubmittedTask
from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# arq 任务函数名（worker 侧同名的异步函数）
TASK_FUNCTION_NAME = "run_agent_task"

# abort 等待 worker 确认的最长时间（秒）。
# arq 的 Job.abort() 默认无限等待任务结果——没有 worker 消费时会把
# HTTP 取消请求永久挂死，因此必须显式设置超时；中止信号在等待前已写入
# Redis，超时只意味着“无人确认”，不影响已排队任务被真正中止。
_ABORT_WAIT_SECONDS = 2.0


class ArqTaskQueue:
    """基于 Redis + arq 的任务队列"""

    queue_type = "arq"

    def __init__(self, redis_url: str):
        """
        :param redis_url: Redis 连接串，例如 redis://127.0.0.1:6379/0
        """
        self.redis_url = redis_url
        self._pool: Optional[ArqRedis] = None

    async def _get_pool(self) -> ArqRedis:
        """获取 arq Redis 连接池（惰性创建）"""
        if self._pool is None:
            from arq import create_pool
            from arq.connections import RedisSettings

            self._pool = await create_pool(RedisSettings.from_dsn(self.redis_url))
        return self._pool

    async def submit(
        self,
        query: str,
        thread_id: str,
        user_id: Optional[int],
    ) -> SubmittedTask:
        """
        把任务入队

        使用 thread_id 作为 arq job_id，任务记录与队列任务天然对应，
        查询/取消都不需要额外的映射表。
        """
        try:
            pool = await self._get_pool()
            job = await pool.enqueue_job(
                TASK_FUNCTION_NAME,
                query,
                thread_id,
                user_id,
                _job_id=thread_id,
            )
        except Exception as e:
            # Redis 不可用时入队失败，交由接口层转 503，任务不会静默丢失
            logger.error("任务入队失败: thread_id=%s, error=%s", thread_id, e)
            raise

        logger.info("任务已入队: job_id=%s, thread_id=%s", job.job_id if job else None, thread_id)
        return SubmittedTask(
            job_id=thread_id,
            thread_id=thread_id,
            queue_type=self.queue_type,
        )

    async def cancel(self, job_id: str) -> str:
        """
        取消任务

        arq 的 abort 只能中止“还在队列里”的任务；已开始执行的任务依赖
        协作式取消（TaskStore 状态 + worker 定期检查），这里先把队列侧的
        任务中止，再返回 cancelling 让接口层置位取消状态。
        """
        try:
            pool = await self._get_pool()
            job = Job(job_id, pool)
            status = await job.status()
            if status == JobStatus.not_found:
                return "not_found"
            if status in (JobStatus.queued, JobStatus.deferred):
                try:
                    # 必须显式传超时：abort 内部会等待任务结果，
                    # 无 worker 消费时默认行为是无限等待，会挂死接口请求
                    await job.abort(timeout=_ABORT_WAIT_SECONDS)
                except (asyncio.TimeoutError, ResultNotFound):
                    # 中止信号已写入 Redis；超时仅表示没有 worker 确认
                    logger.info("任务中止信号已发送，等待 worker 确认超时: %s", job_id)
                return "cancelled"
            # 运行中：无法强杀，走协作式取消
            return "cancelling"
        except Exception as e:
            logger.warning("任务取消失败: job_id=%s, error=%s", job_id, e)
            return "not_found"

    async def get_job_status(self, job_id: str) -> Optional[dict[str, Any]]:
        """查询 arq 侧的任务状态"""
        try:
            pool = await self._get_pool()
            job = Job(job_id, pool)
            status = await job.status()
            return {"status": status.value if hasattr(status, "value") else str(status)}
        except Exception as e:
            logger.warning("任务状态查询失败: job_id=%s, error=%s", job_id, e)
            return None

    async def close(self) -> None:
        """关闭连接池"""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
