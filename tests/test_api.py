"""
FastAPI 接口层测试（第一期安全改造后）

用 TestClient 驱动完整 HTTP/WebSocket 链路，run_deep_agent 被替换为可控的
假实现，避免触发真实模型调用。覆盖：

- 未认证访问被 401 拒绝；
- 任务提交/查询/取消，以及跨用户资源隔离（404）；
- 上传文件的类型白名单、文件名清洗、大小上限；
- 文件列表/下载的用户边界与 token 查询参数下载；
- 任务提交限流（429）；
- WebSocket 的 token 认证与 thread 归属校验。
"""

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.api import server as server_module
from app.api.rate_limit import task_rate_limiter
from app.api.task_store import STATUS_CANCELLED, STATUS_RUNNING


@pytest.fixture
def client(auth_client, monkeypatch):
    """
    已登录 + run_deep_agent 被替换为立即返回的假协程

    避免触发真实模型调用；取消测试另用长任务版本覆盖。
    """

    async def fake_run(task_query, session_id, user_id=None):
        return None

    monkeypatch.setattr(server_module, "run_deep_agent", fake_run)
    return auth_client


@pytest.fixture
def slow_client(auth_client, monkeypatch):
    """已登录 + run_deep_agent 替换为长时间挂起的协程，用于取消测试"""

    async def slow_run(task_query, session_id, user_id=None):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # 与真实 run_deep_agent 一致：取消时同步更新任务存储
            server_module.task_store.set_status(session_id, STATUS_CANCELLED)
            raise

    monkeypatch.setattr(server_module, "run_deep_agent", slow_run)
    return auth_client


def _current_user_id(client) -> int:
    """查询当前登录用户的 ID"""
    response = client.get("/api/auth/me")
    return response.json()["user"]["id"]


class TestAuthRequired:
    """未认证访问"""

    def test_task_requires_auth(self):
        """未携带 JWT 提交任务返回 401"""
        with TestClient(server_module.app) as raw_client:
            response = raw_client.post("/api/task", json={"query": "hi"})

        assert response.status_code == 401

    def test_files_requires_auth(self):
        """未认证查询文件列表返回 401"""
        with TestClient(server_module.app) as raw_client:
            response = raw_client.get("/api/files", params={"path": "app/output"})

        assert response.status_code == 401


class TestTaskApi:
    """任务提交与查询接口"""

    def test_start_task_returns_thread_id(self, client):
        """POST /api/task 返回 started 与 thread_id"""
        response = client.post("/api/task", json={"query": "查询库存"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "started"
        assert body["thread_id"]

    def test_started_task_is_registered_with_user(self, client):
        """提交的任务写入任务存储，状态为 running 且绑定当前用户"""
        response = client.post("/api/task", json={"query": "查询库存"})
        thread_id = response.json()["thread_id"]

        detail = client.get(f"/api/task/{thread_id}")

        assert detail.status_code == 200
        task = detail.json()["task"]
        assert task["status"] == STATUS_RUNNING
        assert task["query"] == "查询库存"
        assert task["user_id"] == _current_user_id(client)

    def test_task_list_only_contains_own_tasks(self, client):
        """任务列表只包含当前用户的任务"""
        client.post("/api/task", json={"query": "任务1"})

        response = client.get("/api/tasks")

        assert response.status_code == 200
        tasks = response.json()["tasks"]
        assert len(tasks) >= 1
        assert all(task["user_id"] == _current_user_id(client) for task in tasks)

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

    def test_task_rate_limit(self, client):
        """超过频率限制的任务提交返回 429"""
        rate_key = f"task:{_current_user_id(client)}"
        task_rate_limiter.reset(rate_key)

        for _ in range(task_rate_limiter.max_requests):
            assert client.post("/api/task", json={"query": "限流测试"}).status_code == 200

        blocked = client.post("/api/task", json={"query": "限流测试"})

        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers


class TestResourceIsolation:
    """跨用户资源隔离"""

    def test_other_user_cannot_read_task(self, auth_client, second_auth_client):
        """其他用户查询任务返回 404（不暴露资源存在性）"""
        thread_id = auth_client.post("/api/task", json={"query": "私有任务"}).json()["thread_id"]

        response = second_auth_client.get(f"/api/task/{thread_id}")

        assert response.status_code == 404

    def test_other_user_cannot_cancel_task(self, auth_client, second_auth_client):
        """其他用户取消任务返回 404"""
        thread_id = auth_client.post("/api/task", json={"query": "私有任务"}).json()["thread_id"]

        response = second_auth_client.post(f"/api/task/{thread_id}/cancel")

        assert response.status_code == 404

    def test_other_user_files_not_visible(self, auth_client, second_auth_client):
        """其他用户的输出目录不可见"""
        user_id = _current_user_id(auth_client)
        session_dir = server_module.output_dir / f"user_{user_id}" / "session_private"
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "secret.md").write_text("私有内容", encoding="utf-8")

        response = second_auth_client.get("/api/files", params={"path": str(session_dir)})

        assert response.status_code == 403
        assert "拒绝访问" in response.json()["detail"]

    def test_other_user_websocket_rejected(self, auth_client, second_auth_client):
        """其他用户的 token 无法连接该 thread 的 WebSocket"""
        thread_id = auth_client.post("/api/task", json={"query": "私有任务"}).json()["thread_id"]
        token = second_auth_client.headers["Authorization"].split(" ", 1)[1]

        with pytest.raises(Exception):
            with second_auth_client.websocket_connect(f"/ws/{thread_id}?token={token}"):
                pass

    def test_other_user_download_with_token_rejected(self, auth_client, second_auth_client):
        """他人用 token 查询参数下载本人的文件被 403 拒绝"""
        user_id = _current_user_id(auth_client)
        session_dir = server_module.output_dir / f"user_{user_id}" / "session_download_private"
        session_dir.mkdir(parents=True, exist_ok=True)
        target = session_dir / "secret.md"
        target.write_text("私有产物", encoding="utf-8")
        other_token = second_auth_client.headers["Authorization"].split(" ", 1)[1]

        with TestClient(server_module.app) as raw_client:
            response = raw_client.get(
                "/api/download",
                params={"path": str(target), "token": other_token},
            )

        assert response.status_code == 403


class TestUploadApi:
    """文件上传接口"""

    def test_upload_saves_into_user_session_dir(self, client):
        """上传文件落在当前用户 + 会话对应的目录"""
        content = "# 上传内容".encode("utf-8")
        response = client.post(
            "/api/upload",
            files={"files": ("笔记.md", content, "text/markdown")},
            data={"thread_id": "upload_thread"},
        )

        assert response.status_code == 200
        assert response.json()["files"] == ["笔记.md"]
        saved = (
            server_module.updated_dir
            / f"user_{_current_user_id(client)}"
            / "session_upload_thread"
            / "笔记.md"
        )
        assert saved.exists()

    def test_upload_rejects_disallowed_type(self, client):
        """白名单外类型返回 400"""
        response = client.post(
            "/api/upload",
            files={"files": ("evil.exe", b"MZ...", "application/octet-stream")},
            data={"thread_id": "upload_thread"},
        )

        assert response.status_code == 400
        assert "不支持的文件类型" in response.json()["detail"]

    def test_upload_sanitizes_filename(self, client):
        """含路径成分的文件名被清洗后落盘"""
        response = client.post(
            "/api/upload",
            files={"files": ("../../../etc/passwd.txt", b"x", "text/plain")},
            data={"thread_id": "upload_thread"},
        )

        assert response.status_code == 200
        assert response.json()["files"] == ["passwd.txt"]

    def test_upload_rejects_oversize_file(self, client, monkeypatch):
        """超过大小上限返回 413 且不残留半成品"""
        monkeypatch.setenv("UPLOAD_MAX_MB", "0.001")  # 1KB
        content = b"x" * 4096

        response = client.post(
            "/api/upload",
            files={"files": ("big.md", content, "text/markdown")},
            data={"thread_id": "upload_thread"},
        )

        assert response.status_code == 413
        saved_dir = (
            server_module.updated_dir / f"user_{_current_user_id(client)}" / "session_upload_thread"
        )
        assert not (saved_dir / "big.md").exists()


class TestFileApi:
    """文件列表与下载接口"""

    @pytest.fixture
    def user_output_file(self, client):
        """在当前用户输出目录下造一个测试文件"""
        user_id = _current_user_id(client)
        session_dir = server_module.output_dir / f"user_{user_id}" / "session_files_test"
        session_dir.mkdir(parents=True, exist_ok=True)
        target = session_dir / "报告.md"
        target.write_text("报告内容", encoding="utf-8")
        return session_dir, target

    def test_list_files_returns_metadata(self, client, user_output_file):
        """输出目录中的文件出现在列表里，带大小字段"""
        session_dir, _ = user_output_file

        response = client.get("/api/files", params={"path": str(session_dir)})

        assert response.status_code == 200
        names = [f["name"] for f in response.json()["files"]]
        assert "报告.md" in names

    def test_download_returns_file(self, client, user_output_file):
        """下载输出目录内的文件返回 200 与内容"""
        _, target = user_output_file

        response = client.get("/api/download", params={"path": str(target)})

        assert response.status_code == 200
        assert "报告内容" in response.text

    def test_download_rejects_outside_path(self, client):
        """用户目录之外的路径被拒绝（403）"""
        response = client.get("/api/download", params={"path": "C:/Windows/win.ini"})

        assert response.status_code == 403
        assert "拒绝访问" in response.json()["detail"]

    def test_download_with_token_query_param(self, client, user_output_file):
        """浏览器直链下载：token 走查询参数（无 Authorization 头）"""
        _, target = user_output_file
        token = client.headers["Authorization"].split(" ", 1)[1]

        with TestClient(server_module.app) as raw_client:
            response = raw_client.get(
                "/api/download",
                params={"path": str(target), "token": token},
            )

        assert response.status_code == 200
        assert "报告内容" in response.text


class TestWebSocket:
    """WebSocket 认证"""

    def test_ping_pong_with_token(self, client):
        """携带 token 连接后心跳正常"""
        thread_id = client.post("/api/task", json={"query": "ws 任务"}).json()["thread_id"]
        token = client.headers["Authorization"].split(" ", 1)[1]

        with client.websocket_connect(f"/ws/{thread_id}?token={token}") as websocket:
            websocket.send_text("ping")
            assert websocket.receive_json()["type"] == "pong"

    def test_rejected_without_token(self, client):
        """无 token 的连接被拒绝"""
        thread_id = client.post("/api/task", json={"query": "ws 任务"}).json()["thread_id"]

        with pytest.raises(Exception):
            with client.websocket_connect(f"/ws/{thread_id}"):
                pass
