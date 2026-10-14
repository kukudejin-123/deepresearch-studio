"""
会话检查点（checkpointer）管理模块

原版使用 InMemorySaver，会话执行状态只保存在内存中，服务重启后
历史会话全部丢失。本模块提供两种可切换的检查点实现：

- sqlite（默认）：SqliteSaver 持久化到 app/data/checkpoints.db，
  服务重启后可以按 thread_id 恢复历史会话上下文；
- memory：InMemorySaver，仅供本地调试或无持久化要求的场景使用。

通过 .env 中的 CHECKPOINTER_TYPE 切换。
"""

import os
import sqlite3
from pathlib import Path
from typing import Optional, Union

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 当前文件位于 app/agent/checkpointer.py，parents[2] 即项目根目录
PROJECT_ROOT = Path(__file__).parents[2]

# 检查点数据库默认落在 app/data/ 下，与向量库目录保持在一起，便于统一纳入 .gitignore
DEFAULT_DB_PATH = PROJECT_ROOT / "app" / "data" / "checkpoints.db"

# 全局单例：主智能体组装和服务生命周期共用同一个检查点实例
_checkpointer: Optional[BaseCheckpointSaver] = None


def get_checkpointer() -> BaseCheckpointSaver:
    """
    获取全局复用的检查点实例

    :return: SqliteSaver 或 InMemorySaver
    """
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    checkpointer_type = os.getenv("CHECKPOINTER_TYPE", "sqlite").lower()
    if checkpointer_type == "memory":
        _checkpointer = InMemorySaver()
        logger.info("检查点类型: memory（服务重启后会话历史将丢失）")
        return _checkpointer

    db_path = Path(os.getenv("CHECKPOINTER_DB", str(DEFAULT_DB_PATH)))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # check_same_thread=False 允许 FastAPI 线程池与事件循环共用连接；
    # SqliteSaver 内部已有 threading.Lock 串行化写操作
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()

    _checkpointer = saver
    logger.info("检查点类型: sqlite（数据库文件: %s）", db_path)
    return _checkpointer


def close_checkpointer() -> None:
    """
    关闭检查点连接，服务停机时调用

    仅关闭 SQLite 连接；InMemorySaver 无需清理。
    """
    global _checkpointer
    if _checkpointer is None:
        return

    conn: Union[sqlite3.Connection, None] = getattr(_checkpointer, "conn", None)
    if conn is not None:
        try:
            conn.close()
            logger.info("SQLite 检查点连接已关闭")
        except sqlite3.Error as e:
            logger.warning("关闭 SQLite 检查点连接失败: %s", e)

    _checkpointer = None
