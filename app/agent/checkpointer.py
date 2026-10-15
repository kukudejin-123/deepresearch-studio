"""
会话检查点（checkpointer）管理模块

原版使用 InMemorySaver，会话执行状态只保存在内存中，服务重启后
历史会话全部丢失。本模块提供三种可切换的检查点实现：

- sqlite（默认）：AsyncSqliteSaver 持久化到 app/data/checkpoints.db，
  服务重启后可以按 thread_id 恢复历史会话上下文；适合单机/开发；
- postgres：AsyncPostgresSaver 连接 PostgreSQL，多 worker 进程共享
  会话状态，是 arq 分布式队列模式下的推荐配置；
- memory：InMemorySaver，仅供本地调试或无持久化要求的场景使用。

通过 .env 中的 CHECKPOINTER_TYPE 切换。

注意：主智能体通过 astream 异步执行，LangGraph 只会调用检查点的异步接口
（aput/aget），因此持久化模式必须使用异步实现（AsyncSqliteSaver /
AsyncPostgresSaver），同步实现在异步图里会直接报错。
"""

import os
import sqlite3
from pathlib import Path
from typing import Optional

import aiosqlite
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 当前文件位于 app/agent/checkpointer.py，parents[2] 即项目根目录
PROJECT_ROOT = Path(__file__).parents[2]

# 检查点数据库默认落在 app/data/ 下，与向量库目录保持在一起，便于统一纳入 .gitignore
DEFAULT_DB_PATH = PROJECT_ROOT / "app" / "data" / "checkpoints.db"

# PostgreSQL 连接串（CHECKPOINTER_TYPE=postgres 时必填）
DEFAULT_POSTGRES_DSN = "postgresql://postgres:postgres@127.0.0.1:5432/deepresearch"

# 全局单例：主智能体组装和服务生命周期共用同一个检查点实例
_checkpointer: Optional[BaseCheckpointSaver] = None

# 记录 from_conn_string 上下文管理器，停机时需要正常退出以关闭连接
_checkpointer_cm = None


async def _create_sqlite_checkpointer() -> AsyncSqliteSaver:
    """创建 SQLite 检查点（默认）"""
    db_path = Path(os.getenv("CHECKPOINTER_DB", str(DEFAULT_DB_PATH)))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = await aiosqlite.connect(str(db_path))
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    logger.info("检查点类型: sqlite（数据库文件: %s）", db_path)
    return saver


async def _create_postgres_checkpointer() -> "AsyncPostgresSaver":  # noqa: F821
    """
    创建 PostgreSQL 检查点（生产推荐）

    DSN 通过 CHECKPOINTER_POSTGRES_DSN 配置。使用 from_conn_string 的
    异步上下文管理器创建，句柄保留在模块级，停机时正常退出。

    langgraph-checkpoint-postgres / psycopg 为可选依赖（extras: postgres），
    只在真正使用 postgres 模式时才需要安装，默认 sqlite 模式不受影响。
    """
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as e:
        raise RuntimeError(
            "使用 postgres 检查点需要安装可选依赖: "
            "uv sync --extra postgres"
            "（或 pip install langgraph-checkpoint-postgres psycopg[binary]）"
        ) from e

    dsn = os.getenv("CHECKPOINTER_POSTGRES_DSN", DEFAULT_POSTGRES_DSN)
    global _checkpointer_cm
    _checkpointer_cm = AsyncPostgresSaver.from_conn_string(dsn)
    saver = await _checkpointer_cm.__aenter__()
    await saver.setup()
    logger.info("检查点类型: postgres（DSN: %s）", _mask_dsn(dsn))
    return saver


def _mask_dsn(dsn: str) -> str:
    """脱敏 DSN 中的密码，避免日志泄露"""
    import re

    return re.sub(r"://([^:/]+):([^@]+)@", r"://\1:***@", dsn)


async def get_checkpointer() -> BaseCheckpointSaver:
    """
    获取全局复用的检查点实例（异步创建）

    :return: AsyncSqliteSaver / AsyncPostgresSaver / InMemorySaver
    """
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    checkpointer_type = os.getenv("CHECKPOINTER_TYPE", "sqlite").lower()
    if checkpointer_type == "memory":
        _checkpointer = InMemorySaver()
        logger.info("检查点类型: memory（服务重启后会话历史将丢失）")
        return _checkpointer

    if checkpointer_type == "postgres":
        _checkpointer = await _create_postgres_checkpointer()
        return _checkpointer

    _checkpointer = await _create_sqlite_checkpointer()
    return _checkpointer


async def close_checkpointer() -> None:
    """
    关闭检查点连接，服务停机时调用

    SQLite 关闭 aiosqlite 连接；PostgreSQL 退出 from_conn_string 上下文；
    InMemorySaver 无需清理。
    """
    global _checkpointer, _checkpointer_cm
    if _checkpointer is None:
        return

    if _checkpointer_cm is not None:
        try:
            await _checkpointer_cm.__aexit__(None, None, None)
            logger.info("PostgreSQL 检查点连接已关闭")
        except Exception as e:
            logger.warning("关闭 PostgreSQL 检查点连接失败: %s", e)
        _checkpointer_cm = None
    else:
        conn = getattr(_checkpointer, "conn", None)
        if conn is not None:
            try:
                await conn.close()
                logger.info("SQLite 检查点连接已关闭")
            except sqlite3.Error as e:
                logger.warning("关闭 SQLite 检查点连接失败: %s", e)

    _checkpointer = None
