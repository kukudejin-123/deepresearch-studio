"""
LLM 评审与评测执行器测试

- parse_judge_response：结构化解析的各种边界（合法 JSON / markdown 包裹 /
  越界分数 / 缺失维度 / 非 JSON）；
- compute_weighted_score：权重计算；
- EvalRunner：用假执行器 + 假评审跑完整三层判定，不调用任何外部服务；
- 报告渲染。
"""

import pytest

from app.eval.dataset import EvalCase
from app.eval.judge import (
    build_judge_prompt,
    compute_weighted_score,
    make_mock_judge,
    parse_judge_response,
)
from app.eval.report import render_comparison_report, render_run_report
from app.eval.runner import EvalRunner


def _case(**overrides) -> EvalCase:
    base = {"id": "t-001", "category": "database_query", "query": "查询库存"}
    base.update(overrides)
    return EvalCase(**base)


class TestParseJudgeResponse:
    """评审响应解析"""

    def test_parses_plain_json(self):
        """标准 JSON 直接解析"""
        scores = parse_judge_response(
            '{"correctness": 0.9, "completeness": 0.8, "groundedness": 0.7, "reasoning": "ok"}'
        )

        assert scores["correctness"] == 0.9
        assert scores["reasoning"] == "ok"

    def test_parses_fenced_json(self):
        """markdown 代码块包裹的 JSON"""
        scores = parse_judge_response(
            '```json\n{"correctness": 1.0, "completeness": 0.5, "groundedness": 0.0}\n```'
        )

        assert scores["correctness"] == 1.0
        assert scores["groundedness"] == 0.0

    def test_parses_json_with_surrounding_text(self):
        """JSON 前后有其他文字时仍能定位"""
        scores = parse_judge_response(
            '评审结论如下：\n{"correctness": 0.6, "completeness": 0.6, "groundedness": 0.6}\n以上。'
        )

        assert scores["completeness"] == 0.6

    @pytest.mark.parametrize(
        "text",
        [
            "这不是 JSON",
            '{"correctness": 0.9}',  # 缺少维度
            '{"correctness": 1.5, "completeness": 0.5, "groundedness": 0.5}',  # 越界
            '{"correctness": "高", "completeness": 0.5, "groundedness": 0.5}',  # 类型错误
        ],
    )
    def test_rejects_invalid_responses(self, text):
        """非法响应全部抛出 ValueError"""
        with pytest.raises(ValueError):
            parse_judge_response(text)


class TestWeightedScore:
    """加权分数"""

    def test_respects_weights(self):
        """按权重加权"""
        scores = {"correctness": 1.0, "completeness": 0.0, "groundedness": 0.0}
        weights = {"correctness": 0.8, "completeness": 0.1, "groundedness": 0.1}

        assert compute_weighted_score(scores, weights) == 0.8

    def test_zero_weights_falls_back_to_average(self):
        """权重全零时退化为算术平均，避免除零"""
        scores = {"correctness": 0.6, "completeness": 0.4, "groundedness": 0.2}

        assert compute_weighted_score(scores, {}) == 0.4


class TestJudgePrompt:
    """评审提示词构造"""

    def test_includes_case_and_answer(self):
        """提示词包含任务、补充说明与回答"""
        case = _case(rubric_hints="注意数据准确性")
        prompt = build_judge_prompt(case, "阿莫西林库存 5000", [])

        assert "查询库存" in prompt
        assert "注意数据准确性" in prompt
        assert "阿莫西林库存 5000" in prompt

    def test_handles_empty_events(self):
        """无执行事件时给出占位说明"""
        prompt = build_judge_prompt(_case(), "回答", [])

        assert "无执行事件记录" in prompt


class TestEvalRunner:
    """评测执行器（假执行器 + 假评审）"""

    async def test_passing_case(self):
        """三层全部通过时用例通过"""

        async def agent_runner(query, seed_files=None):
            return "阿莫西林胶囊库存为 5000，位于北京一号库。", [
                {
                    "event": "tool_start",
                    "data": {"tool_name": "数据库表名查询工具：list_sql_tables"},
                }
            ]

        case = _case(must_include=["阿莫西林"], expected_tools=["数据库"])
        runner = EvalRunner(agent_runner=agent_runner, judge=make_mock_judge())

        result = await runner.run_case(case)

        assert result.passed is True
        assert result.deterministic_passed is True
        assert result.event_check_passed is True
        assert result.weighted_score == 0.8

    async def test_failing_case_missing_keyword(self):
        """缺少关键信息时用例失败并给出原因"""

        async def agent_runner(query, seed_files=None):
            return "库存查询完成。", []

        case = _case(must_include=["阿莫西林"])
        runner = EvalRunner(agent_runner=agent_runner, judge=make_mock_judge())

        result = await runner.run_case(case)

        assert result.passed is False
        assert "阿莫西林" in result.failure_reason

    async def test_failing_case_missing_tool(self):
        """工具链路未走到时用例失败"""

        async def agent_runner(query, seed_files=None):
            return "阿莫西林胶囊库存 5000。", []

        case = _case(must_include=["阿莫西林"], expected_tools=["数据库"])
        runner = EvalRunner(agent_runner=agent_runner, judge=make_mock_judge())

        result = await runner.run_case(case)

        assert result.passed is False
        assert result.event_check_passed is False

    async def test_low_score_fails(self):
        """加权分低于阈值时失败"""

        async def agent_runner(query, seed_files=None):
            return "阿莫西林胶囊库存 5000。", []

        low_judge = make_mock_judge({"correctness": 0.3, "completeness": 0.3, "groundedness": 0.3})
        runner = EvalRunner(agent_runner=agent_runner, judge=low_judge, pass_threshold=0.6)

        result = await runner.run_case(_case(must_include=["阿莫西林"]))

        assert result.passed is False
        assert "低于阈值" in result.failure_reason

    async def test_judge_error_marks_failure_but_keeps_deterministic(self):
        """评审失败不阻塞：用例失败但确定性结果仍有效"""

        async def agent_runner(query, seed_files=None):
            return "阿莫西林胶囊库存 5000。", []

        def broken_judge(case, answer, events):
            raise RuntimeError("judge api down")

        runner = EvalRunner(agent_runner=agent_runner, judge=broken_judge)
        result = await runner.run_case(_case(must_include=["阿莫西林"]))

        assert result.passed is False
        assert result.deterministic_passed is True
        assert "评审失败" in result.failure_reason

    async def test_agent_error_recorded(self):
        """Agent 执行异常时记录失败原因，不抛出"""

        async def broken_agent_runner(query, seed_files=None):
            raise RuntimeError("llm unreachable")

        runner = EvalRunner(agent_runner=broken_agent_runner, judge=make_mock_judge())
        result = await runner.run_case(_case())

        assert result.passed is False
        assert "agent_error" in result.failure_reason

    async def test_run_all_with_concurrency(self):
        """并发执行保持输入顺序"""

        async def agent_runner(query, seed_files=None):
            return "阿莫西林胶囊库存 5000。", []

        cases = [_case(id=f"t-{i}", must_include=["阿莫西林"]) for i in range(5)]
        runner = EvalRunner(agent_runner=agent_runner, judge=make_mock_judge())

        results = await runner.run_all(cases, concurrency=3)

        assert [r.case_id for r in results] == [f"t-{i}" for i in range(5)]

    async def test_seed_files_forwarded(self):
        """种子文件被传给执行器"""
        received = {}

        async def agent_runner(query, seed_files=None):
            received["seed_files"] = seed_files
            return "阿莫西林胶囊库存 5000。", []

        runner = EvalRunner(agent_runner=agent_runner, judge=make_mock_judge())
        await runner.run_case(_case(must_include=["阿莫西林"], seed_files=["sub_dir/x.md"]))

        assert received["seed_files"] == ["sub_dir/x.md"]


class TestReports:
    """报告渲染"""

    def test_run_report_contains_summary(self):
        """运行报告包含通过率与失败用例"""
        summary = {"total": 2, "passed": 1, "pass_rate": 0.5, "avg_score": 0.65}
        results = [
            {
                "case_id": "t-001",
                "category": "database_query",
                "passed": True,
                "weighted_score": 0.8,
                "failure_reason": "",
            },
            {
                "case_id": "t-002",
                "category": "network_search",
                "passed": False,
                "weighted_score": 0.5,
                "failure_reason": "缺少关键信息: ['2026']",
            },
        ]

        report = render_run_report(summary, results, "run_x")

        assert "run_x" in report
        assert "50.0%" in report
        assert "t-002" in report
        assert "缺少关键信息" in report

    def test_comparison_report_lists_regressions(self):
        """对比报告列出回归用例"""
        comparison = {
            "baseline_run_id": "run_a",
            "current_run_id": "run_b",
            "baseline_pass_rate": 1.0,
            "current_pass_rate": 0.5,
            "regressions": [
                {
                    "case_id": "t-001",
                    "baseline_score": 0.9,
                    "current_score": 0.5,
                    "score_delta": -0.4,
                    "flipped_to_fail": True,
                    "failure_reason": "缺少关键信息",
                }
            ],
            "improvements": [],
        }

        report = render_comparison_report(comparison)

        assert "t-001" in report
        assert "由通过转失败" in report
        assert "run_a" in report
