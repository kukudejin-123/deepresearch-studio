"""
检查点（checkpointer）模块测试

验证两种模式：
1. sqlite：put/get 往返、配置切换、连接关闭；
2. memory：未配置数据库时回退 InMemorySaver。
"""

import uuid

from app.agent.checkpointer import close_checkpointer, get_checkpointer


def _make_checkpoint() -> dict:
    """构造一个最小可用的 LangGraph 检查点对象"""
    return {
        "v": 1,
        "id": str(uuid.uuid4()),
        "ts": "2026-10-14T00:00:00+00:00",
        "channel_values": {"messages": ["你好", "世界"]},
        "channel_versions": {},
        "versions_seen": {},
    }


def _make_metadata() -> dict:
    return {"source": "input", "step": 0, "writes": None, "parents": {}}


class TestSqliteCheckpointer:
    """SQLite 持久化检查点验证"""

    def test_roundtrip_put_and_get(self, tmp_path, monkeypatch, reset_checkpointer):
        """写入的检查点可以按更新后的 config 读回"""
        monkeypatch.setenv("CHECKPOINTER_TYPE", "sqlite")
        monkeypatch.setenv("CHECKPOINTER_DB", str(tmp_path / "cp.db"))

        saver = get_checkpointer()
        config = {"configurable": {"thread_id": "t-1", "checkpoint_ns": ""}}

        new_config = saver.put(config, _make_checkpoint(), _make_metadata(), {})
        stored = saver.get_tuple(new_config)

        assert stored is not None
        assert stored.checkpoint["channel_values"]["messages"] == ["你好", "世界"]

    def test_singleton_reuses_instance(self, tmp_path, monkeypatch, reset_checkpointer):
        """同一进程内多次获取返回同一实例，避免重复建表"""
        monkeypatch.setenv("CHECKPOINTER_TYPE", "sqlite")
        monkeypatch.setenv("CHECKPOINTER_DB", str(tmp_path / "cp.db"))

        assert get_checkpointer() is get_checkpointer()

    def test_threads_are_isolated(self, tmp_path, monkeypatch, reset_checkpointer):
        """不同 thread_id 的检查点互不覆盖"""
        monkeypatch.setenv("CHECKPOINTER_TYPE", "sqlite")
        monkeypatch.setenv("CHECKPOINTER_DB", str(tmp_path / "cp.db"))
        saver = get_checkpointer()

        cfg_a = {"configurable": {"thread_id": "thread-a", "checkpoint_ns": ""}}
        cfg_b = {"configurable": {"thread_id": "thread-b", "checkpoint_ns": ""}}
        checkpoint_a = _make_checkpoint()
        checkpoint_a["channel_values"] = {"messages": ["A"]}
        checkpoint_b = _make_checkpoint()
        checkpoint_b["channel_values"] = {"messages": ["B"]}

        new_a = saver.put(cfg_a, checkpoint_a, _make_metadata(), {})
        new_b = saver.put(cfg_b, checkpoint_b, _make_metadata(), {})

        assert saver.get_tuple(new_a).checkpoint["channel_values"]["messages"] == ["A"]
        assert saver.get_tuple(new_b).checkpoint["channel_values"]["messages"] == ["B"]

    def test_close_releases_singleton(self, tmp_path, monkeypatch, reset_checkpointer):
        """关闭后单例被清空，下次获取按当前环境重新创建"""
        monkeypatch.setenv("CHECKPOINTER_TYPE", "sqlite")
        monkeypatch.setenv("CHECKPOINTER_DB", str(tmp_path / "cp.db"))
        get_checkpointer()

        close_checkpointer()

        from app.agent import checkpointer as module

        assert module._checkpointer is None


class TestMemoryCheckpointer:
    """内存检查点回退验证"""

    def test_memory_type(self, monkeypatch, reset_checkpointer):
        """CHECKPOINTER_TYPE=memory 时返回 InMemorySaver"""
        from langgraph.checkpoint.memory import InMemorySaver

        monkeypatch.setenv("CHECKPOINTER_TYPE", "memory")

        assert isinstance(get_checkpointer(), InMemorySaver)
