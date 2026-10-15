"""
任务事件持久化模块

monitor 推送到前端的事件（工具调用、助手调度、结果、异常）原本是“阅后即焚”
的：WebSocket 一断，执行过程就看不到了，页面刷新也会丢失全部上下文。

本模块把事件按 thread_id 持久化到 SQLite，支持：

- 断线重连 / 页面刷新后按 seq 增量回放；
- 任务结束后完整复盘一次研究的全过程；
- 与 Redis pub/sub 桥配合，把 worker 进程产生的事件同步到 API 进程。
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE (thread_id, seq)
)
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_task_events_thread_seq
ON task_events (thread_id, seq)
"""


class EventStore:
    """基于 SQLite 的任务事件存储（append-only，按 thread_id + seq 查询）"""

    def __init__(self, db_path: str | Path):
        """
        :param db_path: SQLite 数据库文件路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self) -> None:
        with self._connect() as conn:
            # WAL 模式 + busy_timeout：API 进程与 arq worker 进程会并发写库，
            # 回滚日志模式下容易出现 "database is locked"
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)

    def append(
        self,
        thread_id: str,
        event: str,
        message: str,
        data: Optional[dict[str, Any]] = None,
    ) -> int:
        """
        追加一条事件，返回该事件的 seq

        seq 在 thread_id 内单调递增（取当前最大值 + 1），前端按 seq 去重和续传。
        """
        payload = json.dumps(data or {}, ensure_ascii=False)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM task_events WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            next_seq = row["max_seq"] + 1
            conn.execute(
                """
                INSERT INTO task_events (thread_id, seq, event, message, data, created_at)
                VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
                """,
                (thread_id, next_seq, event, message, payload),
            )
        return next_seq

    def list(
        self,
        thread_id: str,
        after_seq: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """
        查询某个任务的事件（增量）

        :param thread_id: 任务会话 ID
        :param after_seq: 只返回 seq 大于该值的事件（断线续传）
        :param limit: 返回条数上限
        :return: 事件字典列表，按 seq 升序
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT seq, event, message, data, created_at
                FROM task_events
                WHERE thread_id = ? AND seq > ?
                ORDER BY seq ASC
                LIMIT ?
                """,
                (thread_id, after_seq, limit),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "event": row["event"],
                "message": row["message"],
                "data": json.loads(row["data"]),
                "timestamp": row["created_at"],
            }
            for row in rows
        ]

    def last_seq(self, thread_id: str) -> int:
        """查询某个任务当前的最大 seq（0 表示没有事件）"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM task_events WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return int(row["max_seq"])

    def delete_thread(self, thread_id: str) -> int:
        """
        删除某个任务的全部事件（管理/清理用途）

        :return: 删除的条数
        """
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM task_events WHERE thread_id = ?", (thread_id,))
            return cursor.rowcount
