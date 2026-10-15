"""
pytest 全局配置与共享 fixture

在任何 app 模块被导入前设置测试环境变量，保证测试不依赖真实 API Key、
MySQL、Tavily 等外部服务；LLM 相关测试统一使用惰性初始化的假配置。

安全相关测试的默认姿态与生产一致：AUTH_ENABLED=true，
所有资源接口都需要 JWT；用户数据库落在临时目录，测试间互不影响。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 测试环境变量：key 全部为假值，仅用于让各模块完成惰性初始化
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy")
os.environ.setdefault("OPENAI_BASE_URL", "https://example.invalid/v1")
os.environ.setdefault("LLM_QWEN_MAX", "qwen-max")
os.environ.setdefault("EMBEDDING_MODEL", "text-embedding-v3-test")
os.environ.setdefault("TAVILY_API_KEY", "tvly-test-dummy")
# 默认使用内存检查点，SQLite 文件统一落到系统临时目录，避免污染项目目录
os.environ.setdefault("CHECKPOINTER_TYPE", "memory")
os.environ.setdefault(
    "CHECKPOINTER_DB",
    str(Path(tempfile.gettempdir()) / "deepresearch_studio_test" / "checkpoints.db"),
)
# 认证默认开启（与生产一致）；测试 JWT 使用固定长密钥，避免依赖环境
os.environ.setdefault("AUTH_ENABLED", "true")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-pytest-only-32-bytes-long")
os.environ.setdefault(
    "AUTH_DB",
    str(Path(tempfile.gettempdir()) / "deepresearch_studio_test" / "users.db"),
)
# 事件库同样落到临时目录，避免测试污染项目目录
os.environ.setdefault(
    "EVENT_DB",
    str(Path(tempfile.gettempdir()) / "deepresearch_studio_test" / "events.db"),
)
# 默认使用进程内队列；arq 相关用例自行切到 arq 并探测 Redis
os.environ.setdefault("TASK_QUEUE", "memory")


@pytest.fixture
def session_dir(tmp_path):
    """
    提供临时会话目录，并写入 ContextVar

    文件类工具依赖 get_session_context() 拿当前会话目录，
    测试期间需要真实设置上下文，结束后自动恢复。
    """
    from app.api.context import (
        reset_session_context,
        set_session_context,
        set_thread_context,
        set_user_context,
    )

    directory = tmp_path / "session_test"
    directory.mkdir(parents=True, exist_ok=True)

    dir_token = set_session_context(str(directory))
    thread_token = set_thread_context("test-thread-id")
    user_token = set_user_context(None)
    yield directory
    reset_session_context(dir_token, thread_token, user_token)


@pytest.fixture
def reset_checkpointer():
    """
    重置 checkpointer 模块单例，保证每个测试按当前环境变量重新创建；
    结束后关闭连接（异步测试内直接 await，同步上下文用 asyncio.run 收尾）。
    """
    import asyncio

    from app.agent import checkpointer as checkpointer_module

    checkpointer_module._checkpointer = None
    yield checkpointer_module

    if checkpointer_module._checkpointer is not None:
        try:
            asyncio.run(checkpointer_module.close_checkpointer())
        except RuntimeError:
            pass


def _register_and_login(client, username: str, password: str = "secret123") -> str:
    """注册用户并返回 JWT（内部辅助）"""
    response = client.post("/api/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return response.json()["token"]


@pytest.fixture
def auth_client():
    """
    带登录态的 TestClient

    自动注册一个唯一用户并把 JWT 写入客户端默认请求头，
    模拟“已登录用户”访问所有资源接口。
    """
    import uuid

    from fastapi.testclient import TestClient

    from app.api.server import app

    with TestClient(app) as client:
        username = f"alice_{uuid.uuid4().hex[:8]}"
        token = _register_and_login(client, username)
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


@pytest.fixture
def second_auth_client():
    """
    另一个登录用户的 TestClient

    用于跨用户资源隔离测试：第二个用户访问第一个用户的任务/文件应被拒绝。
    """
    import uuid

    from fastapi.testclient import TestClient

    from app.api.server import app

    with TestClient(app) as client:
        username = f"bob_{uuid.uuid4().hex[:8]}"
        token = _register_and_login(client, username)
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


@pytest.fixture(autouse=True)
def _reset_monitor_singleton():
    """
    每个测试结束后重置 monitor 单例状态

    ToolMonitor 和 ConnectionManager 都是模块级单例；API 测试的 lifespan
    会把 WebSocket 管理器绑定到（随后关闭的）事件循环上，不清理会导致
    后续工具测试向已关闭的循环投递事件。
    """
    from app.api.monitor import manager, monitor

    yield
    monitor.websocket_manager = None
    manager.loop = None
    manager.active_connections.clear()
