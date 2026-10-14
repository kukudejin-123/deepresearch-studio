"""
用户认证与授权模块

第一期“安全与多用户基础”的核心：提供注册/登录、JWT 签发与校验、
FastAPI 依赖注入，以及按 user_id 隔离资源的能力。

设计要点：
1. 密码哈希使用标准库 pbkdf2_hmac（SHA-256 + 随机盐 + 10 万次迭代），
   不引入 bcrypt/passlib 等额外依赖；
2. JWT 使用 PyJWT，密钥来自 JWT_SECRET 环境变量，生产环境必须修改默认值；
3. AUTH_ENABLED=false 时退化为单一开发用户，方便本地无登录调试；
4. 用户表与任务/检查点共用 SQLite，但独立建表，互不影响。
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 当前文件位于 app/api/auth.py，parents[1] 即 app 目录
_APP_ROOT = Path(__file__).resolve().parents[1]

# 用户数据库默认路径；测试环境通过 AUTH_DB 覆盖
DEFAULT_AUTH_DB = _APP_ROOT / "data" / "users.db"

# 开发默认密钥：仅用于本地调试，启动时会打警告；生产必须通过 JWT_SECRET 覆盖
_DEFAULT_JWT_SECRET = "dev-only-insecure-secret-change-me"

# 密码派生参数：迭代次数越高越难暴力破解，10 万次在本机约几十毫秒，可接受
_PBKDF2_ITERATIONS = 100_000
_SALT_BYTES = 16

# 用户名与密码的格式约束
USERNAME_MIN, USERNAME_MAX = 3, 32
PASSWORD_MIN, PASSWORD_MAX = 6, 72

JWT_ALGORITHM = "HS256"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

# FastAPI 的 Bearer 方案；auto_error=False 让我们自己控制 401 的内容
# 同时导出给下载接口复用：浏览器 <a href> 下载走查询参数，
# 其他接口走 Authorization 头，两者共用同一个 scheme 定义。
bearer_scheme = HTTPBearer(auto_error=False)


class UserStore:
    """基于 SQLite 的用户存储，负责建表、注册、查询和密码校验"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_table()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE_SQL)

    @staticmethod
    def _hash_password(password: str, salt_hex: str) -> str:
        """用 PBKDF2-SHA256 派生密码哈希"""
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            _PBKDF2_ITERATIONS,
        )
        return digest.hex()

    def create_user(self, username: str, password: str) -> dict[str, Any]:
        """
        注册新用户

        :param username: 用户名（唯一）
        :param password: 明文密码（入库前哈希）
        :return: 用户信息字典（不含密码哈希）
        :raises ValueError: 用户名已被占用
        """
        salt_hex = secrets.token_hex(_SALT_BYTES)
        password_hash = self._hash_password(password, salt_hex)
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO users (username, password_hash, salt, created_at)
                    VALUES (?, ?, ?, datetime('now', 'localtime'))
                    """,
                    (username, password_hash, salt_hex),
                )
                user_id = cursor.lastrowid
        except sqlite3.IntegrityError as e:
            raise ValueError("用户名已被注册") from e

        logger.info("新用户注册: id=%s, username=%s", user_id, username)
        return {"id": user_id, "username": username}

    def verify_user(self, username: str, password: str) -> Optional[dict[str, Any]]:
        """
        校验用户名和密码

        :return: 用户信息字典；校验失败返回 None
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, password_hash, salt FROM users WHERE username = ?",
                (username,),
            ).fetchone()

        if row is None:
            return None

        expected = self._hash_password(password, row["salt"])
        # hmac.compare_digest 防时序侧信道
        if not hmac.compare_digest(expected, row["password_hash"]):
            return None

        return {"id": row["id"], "username": row["username"]}

    def get_user(self, user_id: int) -> Optional[dict[str, Any]]:
        """
        按 ID 查询用户

        :return: 用户信息字典；不存在返回 None
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, username, created_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return dict(row) if row else None


def _get_jwt_secret() -> str:
    """读取 JWT 密钥；使用默认值时打印警告"""
    secret = os.getenv("JWT_SECRET")
    if not secret:
        logger.warning("未配置 JWT_SECRET，正在使用不安全的开发默认值；生产环境必须设置该变量")
        return _DEFAULT_JWT_SECRET
    return secret


def auth_enabled() -> bool:
    """是否启用认证；AUTH_ENABLED=false 时使用开发用户"""
    return os.getenv("AUTH_ENABLED", "true").lower() not in {"false", "0", "no"}


def get_user_store() -> UserStore:
    """获取用户存储实例（每次按当前环境变量创建，便于测试隔离）"""
    db_path = os.getenv("AUTH_DB", str(DEFAULT_AUTH_DB))
    return UserStore(db_path)


def create_access_token(user_id: int, username: str) -> str:
    """
    签发 JWT 访问令牌

    :param user_id: 用户 ID
    :param username: 用户名
    :return: 编码后的 JWT 字符串
    """
    expire_minutes = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + timedelta(minutes=expire_minutes),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict[str, Any]]:
    """
    解析并校验 JWT

    :param token: JWT 字符串
    :return: 载荷字典；无效或过期返回 None
    """
    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as e:
        logger.debug("JWT 校验失败: %s", e)
        return None


# 开发模式下的默认用户：认证关闭时所有资源都归属该虚拟用户
DEV_USER = {"id": 0, "username": "dev"}


def validate_registration(username: str, password: str) -> Optional[str]:
    """
    校验注册输入

    :return: 错误信息；通过时返回 None
    """
    if not (USERNAME_MIN <= len(username) <= USERNAME_MAX):
        return f"用户名长度需在 {USERNAME_MIN}-{USERNAME_MAX} 个字符之间"
    if not all(ch.isalnum() or ch in "_-" for ch in username):
        return "用户名只能包含字母、数字、下划线和连字符"
    if not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        return f"密码长度需在 {PASSWORD_MIN}-{PASSWORD_MAX} 个字符之间"
    return None


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> dict[str, Any]:
    """
    FastAPI 依赖：解析当前登录用户

    - AUTH_ENABLED=false 时直接返回开发用户；
    - 否则从 Authorization: Bearer <token> 解析 JWT，并确认用户仍存在。
    """
    if not auth_enabled():
        return dict(DEV_USER)

    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")

    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="登录凭证无效或已过期")

    user = get_user_store().get_user(int(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在或已被删除")

    return {"id": user["id"], "username": user["username"]}
