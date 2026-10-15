"""
arq Worker 入口

独立的 Agent 执行进程，通过 Redis 队列接收任务：

    arq app.worker.tasks.WorkerSettings

与 API 进程的关系：
- 任务状态：共享 SQLite TaskStore（app/data/checkpoints.db）；
- 执行事件：写入 SQLite EventStore，并经 Redis pub/sub 桥实时推送到
  API 进程的 WebSocket（见 app.events.bridge）；
- 会话检查点：默认共享同一 SQLite 文件；配置 CHECKPOINTER_TYPE=postgres
  时由各 worker 共享 PostgreSQL（生产推荐）。

worker 内没有 WebSocket 连接，因此 monitor 的 websocket_manager 为 None，
事件自动走“落库 + Redis 桥”路径。
"""

import os
from typing import Any, Dict, Optional

from arq import func
from arq.connections import RedisSettings

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 与 API 侧 ArqTaskQueue 的 TASK_FUNCTION_NAME 对应
TASK_FUNCTION_NAME = "run_agent_task"

# 任务超时：深度研究任务可能较长，默认 30 分钟
_TASK_TIMEOUT_SECONDS = 1800


async def run_agent_task(
    ctx: Dict[str, Any],
    query: str,
    thread_id: str,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    执行一个 DeepAgents 研究任务

    :param ctx: arq 上下文（含 job_try 等重试信息）
    :param query: 用户任务内容
    :param thread_id: 任务会话 ID（与 arq job_id 一致）
    :param user_id: 任务归属用户
    :return: 任务结果摘要（存入 arq job result）
    """
    from app.agent.main_agent import run_deep_agent, task_store
    from app.api.task_store import STATUS_FAILED

    job_try = ctx.get("job_try", 1)
    logger.info(
        "worker 接收任务: thread_id=%s, user_id=%s, 第 %d 次尝试",
        thread_id,
        user_id,
        job_try,
    )

    try:
        await run_deep_agent(query, thread_id, user_id)
    except Exception as e:
        # run_deep_agent 内部已把失败状态写入 TaskStore；这里再包一层，
        # 保证 arq 侧重试耗尽后状态一定落地
        task = task_store.get(thread_id)
        if task is None or task.get("status") != STATUS_FAILED:
            task_store.set_status(thread_id, STATUS_FAILED, error=str(e))
        logger.exception("worker 任务执行失败: thread_id=%s", thread_id)
        raise

    task = task_store.get(thread_id)
    return {
        "thread_id": thread_id,
        "status": task.get("status") if task else "unknown",
    }


async def on_startup(ctx: Dict[str, Any]) -> None:
    """worker 启动钩子：初始化日志，记录队列信息"""
    from app.utils.logging_config import setup_logging

    setup_logging("INFO")
    redis_url = os.getenv("TASK_QUEUE_REDIS_URL", "redis://127.0.0.1:6379/0")
    logger.info("arq worker 启动完成，Redis: %s", redis_url)


class WorkerSettings:
    """arq worker 配置（arq CLI 直接读取这些类属性）"""

    # arq 的 func() 第一个参数是位置参数，不能用 @func(name=...) 这种
    # 纯关键字装饰器写法；这里显式构造 Function 对象并设置任务选项
    functions = [
        func(
            run_agent_task,
            name=TASK_FUNCTION_NAME,
            timeout=_TASK_TIMEOUT_SECONDS,
            max_tries=2,
        )
    ]

    # 连接设置在导入期解析（仅构造配置对象，不建立连接）
    redis_settings = RedisSettings.from_dsn(
        os.getenv("TASK_QUEUE_REDIS_URL", "redis://127.0.0.1:6379/0")
    )

    # 并发执行的任务数：Agent 任务重 IO，可适当放宽
    max_jobs = 4

    on_startup = on_startup
