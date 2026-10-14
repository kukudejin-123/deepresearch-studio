"""
用户认证模块测试

覆盖注册/登录/查询当前用户全流程，以及各种异常路径：
重复用户名、错误密码、非法输入、无效 JWT、未认证访问。

每个测试使用唯一用户名，避免 SQLite 用户库在测试间产生注册冲突。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.api import auth as auth_module
from app.api.auth import create_access_token, decode_access_token, validate_registration
from app.api.rate_limit import login_rate_limiter
from app.api.server import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def unique_username() -> str:
    """生成全局唯一的测试用户名"""
    return f"user_{uuid.uuid4().hex[:10]}"


def _register(client, username: str, password: str = "secret123"):
    return client.post("/api/auth/register", json={"username": username, "password": password})


def _login(client, username: str, password: str = "secret123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


class TestRegistration:
    """注册接口"""

    def test_register_returns_token_and_user(self, client):
        """注册成功返回 JWT 和用户信息"""
        username = unique_username()
        response = _register(client, username)

        assert response.status_code == 201
        body = response.json()
        assert body["token"]
        assert body["user"]["username"] == username
        assert body["user"]["id"] >= 1

    def test_duplicate_username_rejected(self, client):
        """重复用户名返回 409"""
        username = unique_username()
        _register(client, username)

        response = _register(client, username)

        assert response.status_code == 409
        assert "已被注册" in response.json()["detail"]

    def test_short_username_rejected(self, client):
        """用户名过短返回 400"""
        response = _register(client, "ab")

        assert response.status_code == 400

    def test_invalid_username_chars_rejected(self, client):
        """用户名含非法字符返回 400"""
        response = _register(client, "bad name!")

        assert response.status_code == 400

    def test_short_password_rejected(self, client):
        """密码过短返回 400"""
        response = _register(client, unique_username(), password="123")

        assert response.status_code == 400


class TestLogin:
    """登录接口"""

    def test_login_success(self, client):
        """正确凭证登录成功"""
        username = unique_username()
        _register(client, username)

        response = _login(client, username)

        assert response.status_code == 200
        assert response.json()["token"]

    def test_wrong_password_rejected(self, client):
        """密码错误返回 401，且不暴露是用户不存在还是密码错误"""
        username = unique_username()
        _register(client, username)

        response = _login(client, username, password="wrong-password")

        assert response.status_code == 401
        assert response.json()["detail"] == "用户名或密码错误"

    def test_unknown_user_rejected(self, client):
        """不存在的用户返回相同提示（防用户枚举）"""
        response = _login(client, unique_username())

        assert response.status_code == 401
        assert response.json()["detail"] == "用户名或密码错误"

    def test_rate_limited_after_repeated_failures(self, client):
        """连续失败登录触发限流（429）"""
        username = unique_username()
        rate_key = f"login:testclient:{username}"
        login_rate_limiter.reset(rate_key)

        for _ in range(login_rate_limiter.max_requests):
            _login(client, username, password="wrong")

        response = _login(client, username, password="wrong")

        assert response.status_code == 429
        assert "Retry-After" in response.headers


class TestCurrentUser:
    """当前用户查询"""

    def test_me_with_token(self, client):
        """携带 JWT 可以查询自己的信息"""
        username = unique_username()
        token = _register(client, username).json()["token"]

        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert response.status_code == 200
        assert response.json()["user"]["username"] == username

    def test_me_without_token(self, client):
        """未携带 JWT 返回 401"""
        response = client.get("/api/auth/me")

        assert response.status_code == 401

    def test_me_with_invalid_token(self, client):
        """伪造 JWT 返回 401"""
        response = client.get("/api/auth/me", headers={"Authorization": "Bearer not-a-real-jwt"})

        assert response.status_code == 401

    def test_token_roundtrip(self):
        """JWT 签发与解析往返一致"""
        token = create_access_token(42, "carol")

        payload = decode_access_token(token)

        assert payload is not None
        assert payload["sub"] == "42"
        assert payload["username"] == "carol"


class TestValidationHelpers:
    """注册输入校验"""

    def test_valid_input(self):
        """合法输入返回 None"""
        assert validate_registration("alice_01", "secret123") is None

    @pytest.mark.parametrize(
        "username,password",
        [
            ("ab", "secret123"),
            ("a" * 33, "secret123"),
            ("bad name", "secret123"),
            ("alice", "123"),
            ("alice", "a" * 73),
        ],
    )
    def test_invalid_inputs(self, username, password):
        """非法输入均返回错误信息"""
        assert validate_registration(username, password) is not None


class TestDevMode:
    """AUTH_ENABLED=false 的开发模式"""

    def test_dev_user_fallback(self, monkeypatch):
        """关闭认证时 get_current_user 返回开发用户"""
        monkeypatch.setattr(auth_module, "auth_enabled", lambda: False)

        import asyncio

        user = asyncio.run(auth_module.get_current_user(None, None))

        assert user["username"] == "dev"
        assert user["id"] == 0
