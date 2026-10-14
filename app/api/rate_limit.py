"""
接口限流模块

第一期安全改造的一部分：用内存滑动窗口限制按用户/IP 的请求频率，
防止 LLM 费用被打爆、登录接口被暴力破解。

当前为进程内实现（重启后计数清零）；多实例部署时需要换成
Redis 共享计数，已在生产化规划（第二期）中列出。
"""

import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from app.utils.logging_config import get_logger

logger = get_logger(__name__)


class RateLimiter:
    """滑动窗口限流器：window_seconds 内最多允许 max_requests 次"""

    def __init__(self, max_requests: int, window_seconds: float):
        """
        :param max_requests: 窗口内允许的最大请求数
        :param window_seconds: 窗口长度（秒）
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> Tuple[bool, float]:
        """
        检查并记录一次请求

        :param key: 限流键，例如 "task:user:3" 或 "login:127.0.0.1:alice"
        :return: (是否允许, 需等待的秒数)；被拒绝时等待秒数大于 0
        """
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            # 弹出窗口外的旧记录
            while hits and now - hits[0] >= self.window_seconds:
                hits.popleft()

            if len(hits) >= self.max_requests:
                retry_after = self.window_seconds - (now - hits[0])
                return False, max(retry_after, 0.1)

            hits.append(now)
            return True, 0.0

    def reset(self, key: str) -> None:
        """清空某个键的计数（测试或管理用途）"""
        with self._lock:
            self._hits.pop(key, None)


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量，非法值回退默认值"""
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        logger.warning("环境变量 %s 不是合法整数，使用默认值 %s", name, default)
        return default


# 任务提交限流：默认每用户 60 秒 10 次，可通过环境变量调整
task_rate_limiter = RateLimiter(
    max_requests=_env_int("TASK_RATE_LIMIT", 10),
    window_seconds=_env_int("TASK_RATE_WINDOW_SECONDS", 60),
)

# 登录限流：默认每 IP+用户名 60 秒 5 次，缓解暴力破解
login_rate_limiter = RateLimiter(
    max_requests=_env_int("LOGIN_RATE_LIMIT", 5),
    window_seconds=_env_int("LOGIN_RATE_WINDOW_SECONDS", 60),
)
