"""
FastAPI 接口层测试

用 TestClient 驱动完整 HTTP/WebSocket 链路，run_deep_agent 被替换为可控的
假实现，验证：任务提交与查询、取消、文件上传、文件列表/下载的安全边界、
WebSocket 心跳。
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api import server as server_module
from app.api.task_store import STATUS_CANCELLED, STATUS_RUNNING


@pytest.fixture
def client(monkeypatch):
    """
    带 lifespan 的 TestClient

    run_deep_agent 被替换为立即返回的假协程，避免触发真实模型调用；
    取消测试另用长任务版本覆盖。
    """

    async def fake_run(task_query, session_id):
        return None

    monkeypatch.setattr(server_module, "run_deep_agent", fake_run)

    with TestClient(server_module.app) as test_client:
        yield test_client


@pytest.fixture
def slow_client(monkeypatch):
    """run_deep_agent 替换为长时间挂起的协程，用于取消测试"""

    async def slow_run(task_query, session_id):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # 与真实 run_deep_agent 一致：取消时同步更新任务存储
            server_module.task_store.set_status(session_id, STATUS_CANCELLED)
            raise

    monkeypatch.setattr(server_module, "run_deep_agent", slow_run)

    with TestClient(server_module.app) as test_client:
        yield test_client


class TestTaskApi:
    """任务提交与查询接口"""

    def test_start_task_returns_thread_id(self, client):
        """POST /api/task 返回 started 与 thread_id"""
        response = client.post("/api/task", json={"query": "查询库存"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "started"
        assert body["thread_id"]

    def test_started_task_is_registered(self, client):
        """提交的任务写入任务存储，状态为 running"""
        response = client.post("/api/task", json={"query": "查询库存"})
        thread_id = response.json()["thread_id"]

        detail = client.get(f"/api/task/{thread_id}")

        assert detail.status_code == 200
        assert detail.json()["task"]["status"] == STATUS_RUNNING
        assert detail.json()["task"]["query"] == "查询库存"

    def test_task_list(self, client):
        """GET /api/tasks 返回任务列表"""
        client.post("/api/task", json={"query": "任务1"})
        client.post("/api/task", json={"query": "任务2"})

        response = client.get("/api/tasks")

        assert response.status_code == 200
        assert len(response.json()["tasks"]) >= 2

    def test_missing_task_returns_404(self, client):
        """查询不存在的任务返回 404"""
        response = client.get("/api/task/no-such-id")

        assert response.status_code == 404

    def test_cancel_running_task(self, slow_client):
        """取消挂起中的任务返回 cancelled"""
        response = slow_client.post("/api/task", json={"query": "长任务"})
        thread_id = response.json()["thread_id"]

        cancel = slow_client.post(f"/api/task/{thread_id}/cancel")

        assert cancel.status_code == 200
        assert cancel.json()["status"] in {"cancelled", "cancelling"}

        detail = slow_client.get(f"/api/task/{thread_id}")
        assert detail.json()["task"]["status"] == STATUS_CANCELLED

    def test_cancel_missing_task_returns_404(self, client):
        """取消不存在的任务返回 404"""
        response = client.post("/api/task/no-such-id/cancel")

        assert response.status_code == 404


class TestFileApi:
    """文件上传、列表与下载接口"""

    def test_upload_saves_into_session_dir(self, client):
        """上传文件落在 updated/session_{thread_id} 下"""
        content = "# 上传内容".encode("utf-8")
        response = client.post(
            "/api/upload",
            files={"files": ("笔记.md", content, "text/markdown")},
            data={"thread_id": "upload_thread"},
        )

        assert response.status_code == 200
        assert response.json()["files"] == ["笔记.md"]
        saved = server_module.updated_dir / "session_upload_thread" / "笔记.md"
        assert saved.exists()

    def test_list_files_returns_metadata(self, client):
        """输出目录中的文件出现在列表里，带大小字段"""
        session_output = server_module.output_dir / "session_files_test"
        session_output.mkdir(parents=True, exist_ok=True)
        (session_output / "报告.md").write_text("内容", encoding="utf-8")

        response = client.get("/api/files", params={"path": str(session_output)})

        assert response.status_code == 200
        names = [f["name"] for f in response.json()["files"]]
        assert "报告.md" in names

    def test_list_files_rejects_outside_path(self, client):
        """output 目录之外的路径被拒绝（路径遍历防护）"""
        response = client.get("/api/files", params={"path": "C:/Windows"})

        assert "拒绝访问" in response.json()["error"]

    def test_download_returns_file(self, client):
        """下载输出目录内的文件返回 200 与内容"""
        session_output = server_module.output_dir / "session_download_test"
        session_output.mkdir(parents=True, exist_ok=True)
        target = session_output / "报告.md"
        target.write_text("下载内容", encoding="utf-8")

        response = client.get("/api/download", params={"path": str(target)})

        assert response.status_code == 200
        assert "下载内容" in response.text

    def test_download_rejects_outside_path(self, client):
        """下载 output 目录之外的路径被拒绝"""
        response = client.get("/api/download", params={"path": "C:/Windows/win.ini"})

        assert "拒绝访问" in response.json()["error"]


class TestWebSocket:
    """WebSocket 心跳验证"""

    def test_ping_pong(self, client):
        """客户端发送 ping，服务端回复 pong"""
        with client.websocket_connect("/ws/ws-test-thread") as websocket:
            websocket.send_text("ping")
            data = websocket.receive_json()

        assert data["type"] == "pong"
