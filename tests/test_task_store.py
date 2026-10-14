"""
任务生命周期存储测试

验证 create -> set_status -> get/list 的完整状态流转。
"""

import pytest

from app.api.task_store import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    TaskStore,
)


class TestTaskStore:
    """TaskStore 行为验证"""

    @pytest.fixture
    def store(self, tmp_path):
        return TaskStore(tmp_path / "tasks.db")

    def test_create_registers_running_task(self, store):
        """新建任务默认状态为 running，并记录 query"""
        store.create("thread-1", "查询药品库存")

        task = store.get("thread-1")

        assert task is not None
        assert task["status"] == STATUS_RUNNING
        assert task["query"] == "查询药品库存"
        assert task["error"] is None
        assert task["created_at"]

    def test_set_status_updates_record(self, store):
        """状态更新后 error 字段同步写入"""
        store.create("thread-1", "任务")
        store.set_status("thread-1", STATUS_FAILED, error="连接超时")

        task = store.get("thread-1")

        assert task["status"] == STATUS_FAILED
        assert task["error"] == "连接超时"

    def test_full_lifecycle(self, store):
        """running -> completed 的完整流转"""
        store.create("thread-1", "任务")
        assert store.get("thread-1")["status"] == STATUS_RUNNING

        store.set_status("thread-1", STATUS_COMPLETED)
        assert store.get("thread-1")["status"] == STATUS_COMPLETED

    def test_cancelled_status(self, store):
        """取消状态可记录"""
        store.create("thread-1", "任务")
        store.set_status("thread-1", STATUS_CANCELLED)

        assert store.get("thread-1")["status"] == STATUS_CANCELLED

    def test_recreate_same_thread_resets(self, store):
        """同一 thread_id 重新提交时重置状态与错误"""
        store.create("thread-1", "旧任务")
        store.set_status("thread-1", STATUS_FAILED, error="旧错误")
        store.create("thread-1", "新任务")

        task = store.get("thread-1")

        assert task["status"] == STATUS_RUNNING
        assert task["query"] == "新任务"
        assert task["error"] is None

    def test_get_missing_returns_none(self, store):
        """查询不存在的任务返回 None"""
        assert store.get("no-such-thread") is None

    def test_list_orders_by_update_time(self, store):
        """列表按更新时间倒序返回"""
        store.create("thread-1", "任务1")
        store.set_status("thread-1", STATUS_COMPLETED)
        store.create("thread-2", "任务2")

        tasks = store.list()

        assert len(tasks) == 2
        assert tasks[0]["thread_id"] == "thread-2"

    def test_list_respects_limit(self, store):
        """limit 参数限制返回条数"""
        for index in range(5):
            store.create(f"thread-{index}", f"任务{index}")

        assert len(store.list(limit=2)) == 2
