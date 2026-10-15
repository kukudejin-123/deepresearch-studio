"""
Prometheus 指标模块

第二期可观测性改造：把任务生命周期与接口调用的核心指标暴露为
Prometheus 文本格式（GET /metrics），可被 Prometheus / Grafana 直接采集。

指标清单：
- deepresearch_tasks_total{status}     任务提交计数（按最终状态）
- deepresearch_tasks_active            当前活跃任务数
- deepresearch_tool_calls_total{tool}  工具调用计数
- deepresearch_http_requests_total{method,path,status}  接口请求计数
- deepresearch_task_duration_seconds   任务执行耗时分布

未接入 Prometheus 服务时，这些指标也可以直接 curl /metrics 查看，
用于本地排查与容量评估。
"""

import time
from contextlib import contextmanager
from typing import Generator

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 任务提交计数：status 标签为 submitted/completed/failed/cancelled
TASKS_TOTAL = Counter(
    "deepresearch_tasks_total",
    "任务总数（按最终状态）",
    ["status"],
)

# 当前活跃任务数（已提交未结束）
TASKS_ACTIVE = Gauge(
    "deepresearch_tasks_active",
    "当前活跃任务数",
)

# 工具调用计数：tool 标签为工具名
TOOL_CALLS_TOTAL = Counter(
    "deepresearch_tool_calls_total",
    "工具调用总数",
    ["tool"],
)

# HTTP 请求计数
HTTP_REQUESTS_TOTAL = Counter(
    "deepresearch_http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
)

# 任务执行耗时（秒）
TASK_DURATION_SECONDS = Histogram(
    "deepresearch_task_duration_seconds",
    "任务执行耗时（秒）",
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
)


def record_task_submitted() -> None:
    """任务提交：计数 + 活跃数 +1"""
    TASKS_TOTAL.labels(status="submitted").inc()
    TASKS_ACTIVE.inc()


def record_task_completed(duration_seconds: float) -> None:
    """任务完成：计数 + 活跃数 -1 + 耗时记录"""
    TASKS_TOTAL.labels(status="completed").inc()
    TASKS_ACTIVE.dec()
    TASK_DURATION_SECONDS.observe(duration_seconds)


def record_task_failed(duration_seconds: float) -> None:
    """任务失败"""
    TASKS_TOTAL.labels(status="failed").inc()
    TASKS_ACTIVE.dec()
    TASK_DURATION_SECONDS.observe(duration_seconds)


def record_task_cancelled(duration_seconds: float = 0.0) -> None:
    """任务取消"""
    TASKS_TOTAL.labels(status="cancelled").inc()
    TASKS_ACTIVE.dec()
    if duration_seconds:
        TASK_DURATION_SECONDS.observe(duration_seconds)


def record_tool_call(tool_name: str) -> None:
    """工具调用计数"""
    TOOL_CALLS_TOTAL.labels(tool=tool_name).inc()


@contextmanager
def task_timer() -> Generator[None, None, None]:
    """计时上下文：with task_timer() as ... 记录任务耗时"""
    started = time.monotonic()
    try:
        yield
    finally:
        TASK_DURATION_SECONDS.observe(time.monotonic() - started)


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    接口计数中间件

    记录每个请求的 method / 路径模板 / 状态码；路径使用路由模板
    （如 /api/task/{thread_id}）而不是具体 ID，避免标签基数爆炸。
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        route = request.scope.get("route")
        path_template = getattr(route, "path", request.url.path)
        try:
            HTTP_REQUESTS_TOTAL.labels(
                method=request.method,
                path=path_template,
                status=str(response.status_code),
            ).inc()
        except Exception as e:
            # 指标失败绝不影响业务响应
            logger.debug("指标记录失败: %s", e)
        return response


def render_metrics() -> tuple[bytes, str]:
    """
    渲染 Prometheus 文本格式指标

    :return: (响应体, Content-Type)
    """
    return generate_latest(), CONTENT_TYPE_LATEST
