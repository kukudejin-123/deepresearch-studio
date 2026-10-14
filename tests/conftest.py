"""
pytest 全局配置与共享 fixture

在任何 app 模块被导入前设置测试环境变量，保证测试不依赖真实 API Key、
MySQL、Tavily 等外部服务；LLM 相关测试统一使用惰性初始化的假配置。
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
    str(Path(tempfile.gettempdir()) / "deepsearch_agents_test" / "checkpoints.db"),
)


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
    )

    directory = tmp_path / "session_test"
    directory.mkdir(parents=True, exist_ok=True)

    dir_token = set_session_context(str(directory))
    thread_token = set_thread_context("test-thread-id")
    yield directory
    reset_session_context(dir_token, thread_token)


@pytest.fixture
def reset_checkpointer():
    """
    重置 checkpointer 模块单例，保证每个测试按当前环境变量重新创建
    """
    from app.agent import checkpointer as checkpointer_module

    checkpointer_module._checkpointer = None
    yield checkpointer_module
    checkpointer_module.close_checkpointer()


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
