"""
任务生命周期存储模块

原版用内存 dict 跟踪后台任务，服务重启后任务记录全部丢失，也无法查询历史。
本模块用 SQLite 记录每个 thread_id 对应任务的状态流转：

    running -> completed / failed / cancelled

任务状态独立于 LangGraph 检查点：检查点保存的是 Agent 执行上下文，
本模块保存的是任务级元数据（提交时间、结束时间、错误信息、归属用户），
用于任务列表查询、失败排查、跨用户资源隔离和后续接入任务队列。
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
    user_id INTEGER,
    query TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class TaskStore:
    """基于 SQLite 的任务生命周期存储，线程安全（短连接操作 + SQLite 自身锁）"""

    def __init__(self, db_path: str | Path):
        """
        :param db_path: SQLite 数据库文件路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        # 每次操作独立连接：任务写入频率低，避免长连接在 Windows 下的文件锁问题
        # timeout + WAL：API 进程与 arq worker 进程并发写同一文件
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self) -> None:
        with self._connect() as conn:
            # WAL 模式支持多进程读写并发，busy_timeout 避免瞬时写锁直接报错
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(_CREATE_TABLE_SQL)
            self._migrate_user_id(conn)

    @staticmethod
    def _migrate_user_id(conn: sqlite3.Connection) -> None:
        """
        旧版数据库补 user_id 列

        早期版本 tasks 表没有 user_id 字段；ALTER TABLE ADD COLUMN 是
        SQLite 支持的原位迁移，重复执行时忽略“列已存在”错误。
        """
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        if "user_id" not in columns:
            conn.execute("ALTER TABLE tasks ADD COLUMN user_id INTEGER")
            logger.info("tasks 表已增加 user_id 列（旧库迁移）")

    def create(self, thread_id: str, query: str, user_id: Optional[int] = None) -> None:
        """
        登记一个新任务（状态为 running）

        :param thread_id: 任务会话 ID
        :param query: 用户提交的任务内容
        :param user_id: 任务归属用户；匿名任务为 None
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (thread_id, user_id, query, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, datetime('now', 'localtime'), datetime('now', 'localtime'))
                ON CONFLICT(thread_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    query = excluded.query,
                    status = excluded.status,
                    error = NULL,
                    updated_at = excluded.updated_at
                """,
                (thread_id, user_id, query, STATUS_RUNNING),
            )
        logger.info("任务已登记: thread_id=%s, user_id=%s", thread_id, user_id)

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

    def count_running(self, user_id: Optional[int] = None) -> int:
        """
        统计运行中的任务数

        :param user_id: 只统计该用户的任务；None 表示全部
        :return: running 状态的任务数量
        """
        with self._connect() as conn:
            if user_id is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM tasks WHERE status = ?", (STATUS_RUNNING,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS c FROM tasks WHERE status = ? AND user_id = ?",
                    (STATUS_RUNNING, user_id),
                ).fetchone()
        return int(row["c"])

    def list(
        self,
        user_id: Optional[int] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        查询最近的任务列表

        :param user_id: 只返回该用户的任务；None 表示不过滤（管理用途）
        :param limit: 返回条数上限
        :return: 任务字典列表，按更新时间倒序（同一秒内的按提交顺序倒序）
        """
        with self._connect() as conn:
            if user_id is None:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM tasks WHERE user_id = ?
                    ORDER BY updated_at DESC, rowid DESC LIMIT ?
                    """,
                    (user_id, limit),
                ).fetchall()
        return [dict(row) for row in rows]
