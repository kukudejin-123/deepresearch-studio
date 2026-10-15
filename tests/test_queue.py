"""
任务队列测试

- MemoryTaskQueue：提交、状态查询、取消（不需要外部依赖）；
- ArqTaskQueue：对本地 Redis 的入队/状态/取消（Redis 不可用时自动跳过，
  CI 中通过 services 提供 Redis）。

同时固化一个真实缺陷的回归：arq 的 Job.abort() 默认无限等待 worker
确认，取消接口必须显式传超时，否则没有 worker 时请求被挂死。
"""

import asyncio
import socket

import pytest

from app.queue import get_task_queue
from app.queue import memory_queue as memory_queue_module
from app.queue.memory_queue import MemoryTaskQueue


def _redis_reachable() -> bool:
    """探测本地 Redis 端口是否可达"""
    try:
        with socket.create_connection(("127.0.0.1", 6379), timeout=0.5):
            return True
    except OSError:
        return False


REDIS_AVAILABLE = _redis_reachable()


@pytest.fixture
def memory_queue():
    return MemoryTaskQueue()


class TestMemoryQueue:
    """进程内队列"""

    async def test_submit_returns_submission(self, memory_queue):
        """提交返回 job_id 与队列类型"""
        submission = await memory_queue.submit("查询库存", "t-mem-1", user_id=1)

        assert submission.thread_id == "t-mem-1"
        assert submission.queue_type == "memory"

    async def test_get_status_running_then_done(self, memory_queue, monkeypatch):
        """运行中查询 running，结束后查询 done"""
        started = asyncio.Event()

        async def quick_run(query, session_id, user_id=None):
            await started.wait()
            return None

        monkeypatch.setattr(memory_queue_module, "run_deep_agent", quick_run)
        submission = await memory_queue.submit("任务", "t-mem-2", None)
        assert await memory_queue.get_job_status(submission.job_id) == {"status": "running"}

        started.set()
        await asyncio.sleep(0.1)
        # 任务结束后从登记表移除（避免内存泄漏），最终状态以 TaskStore 为准
        assert await memory_queue.get_job_status(submission.job_id) is None

    async def test_cancel_running_task(self, memory_queue, monkeypatch):
        """取消挂起中的任务返回 cancelled，且协程真的收到取消"""
        cancelled_flag = asyncio.Event()

        async def cancellable_run(query, session_id, user_id=None):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled_flag.set()
                raise

        monkeypatch.setattr(memory_queue_module, "run_deep_agent", cancellable_run)
        submission = await memory_queue.submit("任务", "t-mem-3", None)
        # 让出一步事件循环，确保任务真正启动后再取消
        # （未启动的任务被取消时协程体不会执行，except 分支也就不会跑）
        await asyncio.sleep(0)
        result = await memory_queue.cancel(submission.job_id)

        assert result == "cancelled"
        assert cancelled_flag.is_set()

    async def test_cancel_missing_task(self, memory_queue):
        """取消不存在的任务返回 not_found"""
        assert await memory_queue.cancel("no-such-job") == "not_found"


@pytest.fixture
def arq_queue(monkeypatch):
    """强制走 arq 工厂分支（Redis 不可用时相关用例自动跳过）"""
    import app.queue as queue_package

    monkeypatch.setenv("TASK_QUEUE", "arq")
    monkeypatch.setenv("TASK_QUEUE_REDIS_URL", "redis://127.0.0.1:6379/0")
    queue_package._queue = None
    queue = get_task_queue()
    yield queue
    # 只重置单例；连接池随测试进程退出释放（同步 fixture 中无法 await 关闭）
    queue_package._queue = None


@pytest.mark.skipif(not REDIS_AVAILABLE, reason="本地 Redis 不可达")
class TestArqQueue:
    """Redis + arq 队列（需要本地 Redis）"""

    async def test_submit_and_status(self, arq_queue):
        """入队后状态为 queued，取消返回 cancelled（不允许无限等待）"""
        submission = await arq_queue.submit("任务", "t-arq-1", user_id=1)
        assert submission.queue_type == "arq"

        status = await arq_queue.get_job_status("t-arq-1")
        assert status is not None and status["status"] in {"queued", "deferred"}

        # 回归：abort 必须带超时，否则没有 worker 时取消会挂死
        result = await asyncio.wait_for(arq_queue.cancel("t-arq-1"), timeout=10)
        assert result == "cancelled"


class TestArqCancelRegression:
    """arq 取消超时回归（不依赖真实 Redis）"""

    async def test_cancel_passes_explicit_timeout(self, monkeypatch):
        """
        取消接口必须给 Job.abort 传正数超时

        伪造记录调用参数的 Job，确认取消路径不会无限等待。
        """
        from arq.jobs import JobStatus

        from app.queue import arq_queue as arq_module

        class FakeJob:
            def __init__(self):
                self.abort_timeout = "unset"

            async def status(self):
                return JobStatus.queued

            async def abort(self, *, timeout=None, poll_delay=0.5):
                self.abort_timeout = timeout
                return True

        fake_job = FakeJob()
        monkeypatch.setattr(arq_module, "Job", lambda *args, **kwargs: fake_job)

        queue = arq_module.ArqTaskQueue("redis://127.0.0.1:6379/0")

        class FakePool:
            pass

        queue._pool = FakePool()

        result = await asyncio.wait_for(queue.cancel("t-fake"), timeout=5)

        assert result == "cancelled"
        assert isinstance(fake_job.abort_timeout, (int, float))
        assert fake_job.abort_timeout > 0
