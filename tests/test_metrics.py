"""
可观测性测试

- 指标记录函数对 Counter/Gauge/Histogram 的更新；
- /metrics 端点返回 Prometheus 文本格式；
- 指标中间件记录请求计数。

注意：prometheus 的 Collector 是进程级全局对象，多个测试文件会共享
（例如接口测试提交任务也会计数，且 Counter 不支持负增长、无法“还原”）。
因此这里全部使用“前后增量”断言，不依赖绝对值，也不做状态重置。
"""

from fastapi.testclient import TestClient

from app.api import metrics as metrics_module
from app.api.server import app


def _counter_value(counter, **labels) -> float:
    """读取某个标签组合的计数器值（不存在返回 0）"""
    # prometheus _metrics 的 key 是标签值元组（按 labelnames 声明顺序），
    # 例如 Counter(..., ['status']) 的 key 是 ('submitted',)
    key = tuple(labels[name] for name in counter._labelnames)
    metric = counter._metrics.get(key)
    return metric._value.get() if metric is not None else 0.0


class TestMetricRecording:
    """指标记录函数（增量断言）"""

    def test_task_submitted_increments(self):
        """提交使总数与活跃数 +1"""
        active_before = metrics_module.TASKS_ACTIVE._value.get()
        submitted_before = _counter_value(metrics_module.TASKS_TOTAL, status="submitted")

        metrics_module.record_task_submitted()

        assert metrics_module.TASKS_ACTIVE._value.get() == active_before + 1
        assert (
            _counter_value(metrics_module.TASKS_TOTAL, status="submitted") == submitted_before + 1
        )

    def test_task_completed_decrements_active(self):
        """完成后活跃数回落，完成计数 +1"""
        metrics_module.record_task_submitted()
        active_before = metrics_module.TASKS_ACTIVE._value.get()
        completed_before = _counter_value(metrics_module.TASKS_TOTAL, status="completed")

        metrics_module.record_task_completed(12.5)

        assert metrics_module.TASKS_ACTIVE._value.get() == active_before - 1
        assert (
            _counter_value(metrics_module.TASKS_TOTAL, status="completed") == completed_before + 1
        )

    def test_task_failed_records_status(self):
        """失败状态被记录"""
        failed_before = _counter_value(metrics_module.TASKS_TOTAL, status="failed")

        metrics_module.record_task_failed(1.0)

        assert _counter_value(metrics_module.TASKS_TOTAL, status="failed") == failed_before + 1

    def test_task_cancelled_records_status(self):
        """取消状态被记录"""
        cancelled_before = _counter_value(metrics_module.TASKS_TOTAL, status="cancelled")

        metrics_module.record_task_cancelled(2.0)

        assert (
            _counter_value(metrics_module.TASKS_TOTAL, status="cancelled") == cancelled_before + 1
        )

    def test_tool_call_counter(self):
        """工具调用按工具名计数"""
        search_before = _counter_value(metrics_module.TOOL_CALLS_TOTAL, tool="internet_search")

        metrics_module.record_tool_call("internet_search")
        metrics_module.record_tool_call("internet_search")
        metrics_module.record_tool_call("execute_sql_query")

        assert (
            _counter_value(metrics_module.TOOL_CALLS_TOTAL, tool="internet_search")
            == search_before + 2
        )
        assert _counter_value(metrics_module.TOOL_CALLS_TOTAL, tool="execute_sql_query") >= 1

    def test_duration_histogram_records_observation(self):
        """耗时直方图记录观测值（count 行 +1）"""
        before = _histogram_count(metrics_module.TASK_DURATION_SECONDS)

        metrics_module.record_task_completed(12.5)

        assert _histogram_count(metrics_module.TASK_DURATION_SECONDS) == before + 1


def _histogram_count(histogram) -> float:
    """读取直方图的观测总数（从渲染文本的 _count 行解析）"""
    rendered = metrics_module.render_metrics()[0].decode()
    prefix = "deepresearch_task_duration_seconds_count"
    for line in rendered.splitlines():
        if line.startswith(prefix):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


class TestMetricsEndpoint:
    """/metrics 接口"""

    def test_metrics_endpoint_format(self):
        """端点返回 Prometheus 文本格式与指标内容"""
        with TestClient(app) as client:
            response = client.get("/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        body = response.text
        assert "deepresearch_tasks_total" in body
        assert "deepresearch_tasks_active" in body

    def test_request_counter_via_middleware(self):
        """中间件记录接口请求计数（路径用路由模板）"""
        http_before = _counter_value(
            metrics_module.HTTP_REQUESTS_TOTAL, method="GET", path="/metrics", status="200"
        )

        with TestClient(app) as client:
            client.get("/metrics")

        assert (
            _counter_value(
                metrics_module.HTTP_REQUESTS_TOTAL, method="GET", path="/metrics", status="200"
            )
            == http_before + 1
        )
