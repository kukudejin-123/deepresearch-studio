"""
任务生命周期存储模块

原版用内存 dict 跟踪后台任务，服务重启后任务记录全部丢失，也无法查询历史。
本模块用 SQLite 记录每个 thread_id 对应任务的状态流转：

    running -> completed / failed / cancelled

任务状态独立于 LangGraph 检查点：检查点保存的是 Agent 执行上下文，
本模块保存的是任务级元数据（提交时间、结束时间、错误信息），
用于任务列表查询、失败排查和后续接入任务队列。
"""

import sqlite3
from pathlib import Path
from typing import Any, Optional

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 任务状态枚举值（数据库中以字符串存储）
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    thread_id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class TaskStore:
    """基于 SQLite 的任务生命周期存储，线程安全（check_same_thread=False + 短连接操作）"""

    def __init__(self, db_path: str | Path):
        """
        :param db_path: SQLite 数据库文件路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        # 每次操作独立连接：任务写入频率低，避免长连接在 Windows 下的文件锁问题
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE_SQL)

    def create(self, thread_id: str, query: str) -> None:
        """
        登记一个新任务（状态为 running）

        :param thread_id: 任务会话 ID
        :param query: 用户提交的任务内容
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (thread_id, query, status, created_at, updated_at)
                VALUES (?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
                ON CONFLICT(thread_id) DO UPDATE SET
                    query = excluded.query,
                    status = excluded.status,
                    error = NULL,
                    updated_at = excluded.updated_at
                """,
                (thread_id, query, STATUS_RUNNING),
            )
        logger.info("任务已登记: thread_id=%s", thread_id)

    def set_status(
        self,
        thread_id: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """
        更新任务状态

        :param thread_id: 任务会话 ID
        :param status: 新状态（running/completed/failed/cancelled）
        :param error: 失败或取消时的原因说明
        """
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, error = ?, updated_at = datetime('now', 'localtime')
                WHERE thread_id = ?
                """,
                (status, error, thread_id),
            )

    def get(self, thread_id: str) -> Optional[dict[str, Any]]:
        """
        查询单个任务记录

        :param thread_id: 任务会话 ID
        :return: 任务字典；不存在时返回 None
        """
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE thread_id = ?", (thread_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """
        查询最近的任务列表

        :param limit: 返回条数上限
        :return: 任务字典列表，按更新时间倒序（同一秒内的按提交顺序倒序）
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]
