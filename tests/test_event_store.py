"""
任务事件存储测试

覆盖事件的追加、seq 单调递增、增量查询、跨任务隔离与清理。
"""

import pytest

from app.events.store import EventStore


class TestEventStore:
    """EventStore 行为验证"""

    @pytest.fixture
    def store(self, tmp_path):
        return EventStore(tmp_path / "events.db")

    def test_append_returns_monotonic_seq(self, store):
        """同一任务内 seq 单调递增"""
        seq1 = store.append("t-1", "tool_start", "开始执行工具: X", {"tool_name": "X"})
        seq2 = store.append("t-1", "task_result", "完成", {"result": "ok"})
        seq3 = store.append("t-1", "error", "失败", {"detail": "boom"})

        assert (seq1, seq2, seq3) == (1, 2, 3)

    def test_list_returns_events_in_order(self, store):
        """查询按 seq 升序返回，字段完整"""
        store.append("t-1", "tool_start", "开始执行工具: X", {"tool_name": "X"})
        store.append("t-1", "task_result", "完成", {"result": "ok"})

        events = store.list("t-1")

        assert [e["event"] for e in events] == ["tool_start", "task_result"]
        assert events[0]["data"]["tool_name"] == "X"
        assert events[0]["seq"] == 1
        assert events[0]["timestamp"]

    def test_incremental_query_after_seq(self, store):
        """增量查询只返回 seq 更大的事件（断线续传）"""
        store.append("t-1", "tool_start", "a")
        store.append("t-1", "tool_start", "b")
        store.append("t-1", "tool_start", "c")

        events = store.list("t-1", after_seq=2)

        assert [e["message"] for e in events] == ["c"]

    def test_threads_are_isolated(self, store):
        """不同任务的事件互不干扰，seq 各自计数"""
        store.append("t-1", "tool_start", "a")
        store.append("t-1", "tool_start", "b")
        store.append("t-2", "tool_start", "c")

        assert len(store.list("t-1")) == 2
        assert len(store.list("t-2")) == 1
        assert store.last_seq("t-2") == 1

    def test_last_seq_empty_thread(self, store):
        """没有事件的任务 last_seq 为 0"""
        assert store.last_seq("no-such-thread") == 0

    def test_delete_thread(self, store):
        """清理接口删除指定任务的全部事件"""
        store.append("t-1", "tool_start", "a")
        store.append("t-2", "tool_start", "b")

        deleted = store.delete_thread("t-1")

        assert deleted == 1
        assert store.list("t-1") == []
        assert len(store.list("t-2")) == 1

    def test_list_respects_limit(self, store):
        """limit 参数限制返回条数"""
        for index in range(10):
            store.append("t-1", "tool_start", f"事件{index}")

        assert len(store.list("t-1", limit=3)) == 3
