"""
评测存储与 CLI 测试

- EvalStore：保存/查询运行、回归对比（分数下降、由通过转失败）；
- CLI：--mock 冒烟模式跑通“加载数据集 -> 执行 -> 评审 -> 存储 -> 报告”全链路，
  以及 compare / list 子命令与 CI 门禁退出码。
"""

import pytest

from app.eval.store import EvalStore


def _result(case_id: str, passed: bool, score: float, **extra) -> dict:
    base = {
        "case_id": case_id,
        "category": "database_query",
        "passed": passed,
        "weighted_score": score,
        "deterministic_passed": passed,
        "event_check_passed": True,
        "scores": {"correctness": score},
        "answer_excerpt": "回答摘要",
        "failure_reason": "" if passed else "缺少关键信息",
    }
    base.update(extra)
    return base


@pytest.fixture
def store(tmp_path):
    return EvalStore(tmp_path / "eval.db")


class TestEvalStore:
    """评测结果存储"""

    def test_save_and_get_run(self, store):
        """保存后可查询运行摘要"""
        summary = store.save_run(
            "run_a", "docs/eval/cases.yaml", "qwen-max", [_result("t-001", True, 0.9)]
        )

        assert summary["total"] == 1
        assert summary["passed"] == 1
        assert summary["pass_rate"] == 1.0

        run = store.get_run("run_a")
        assert run is not None
        assert run["avg_score"] == 0.9

    def test_get_missing_run_returns_none(self, store):
        """查询不存在的运行返回 None"""
        assert store.get_run("no-such-run") is None

    def test_list_runs_orders_by_time(self, store):
        """运行列表按时间倒序"""
        store.save_run("run_a", "d.yaml", "m", [_result("t-001", True, 0.9)])
        store.save_run("run_b", "d.yaml", "m", [_result("t-001", True, 0.8)])

        runs = store.list_runs()

        assert len(runs) == 2
        assert runs[0]["run_id"] == "run_b"

    def test_save_run_overwrites_same_id(self, store):
        """同一 run_id 重复保存时覆盖，不重复计数"""
        store.save_run("run_a", "d.yaml", "m", [_result("t-001", True, 0.9)])
        summary = store.save_run("run_a", "d.yaml", "m", [_result("t-001", False, 0.2)])

        assert summary["passed"] == 0
        assert len(store.get_results("run_a")) == 1

    def test_compare_detects_score_regression(self, store):
        """分数下降超过阈值判定回归"""
        store.save_run("run_a", "d.yaml", "m", [_result("t-001", True, 0.9)])
        store.save_run("run_b", "d.yaml", "m", [_result("t-001", True, 0.5)])

        comparison = store.compare_runs("run_a", "run_b", score_drop_threshold=0.1)

        assert comparison["has_regression"] is True
        assert comparison["regressions"][0]["case_id"] == "t-001"
        assert comparison["regressions"][0]["score_delta"] == pytest.approx(-0.4, abs=0.01)

    def test_compare_detects_flip_to_fail(self, store):
        """由通过转失败判定回归（即使分数下降未超阈值）"""
        store.save_run("run_a", "d.yaml", "m", [_result("t-001", True, 0.65)])
        store.save_run("run_b", "d.yaml", "m", [_result("t-001", False, 0.62)])

        comparison = store.compare_runs("run_a", "run_b", score_drop_threshold=0.1)

        assert comparison["has_regression"] is True
        assert comparison["regressions"][0]["flipped_to_fail"] is True

    def test_compare_no_regression_on_improvement(self, store):
        """分数上升不算回归，计入改善"""
        store.save_run("run_a", "d.yaml", "m", [_result("t-001", False, 0.5)])
        store.save_run("run_b", "d.yaml", "m", [_result("t-001", True, 0.9)])

        comparison = store.compare_runs("run_a", "run_b")

        assert comparison["has_regression"] is False
        assert len(comparison["improvements"]) == 1

    def test_compare_ignores_new_cases(self, store):
        """基线中不存在的新用例不参与回归判定"""
        store.save_run("run_a", "d.yaml", "m", [_result("t-001", True, 0.9)])
        store.save_run(
            "run_b", "d.yaml", "m", [_result("t-001", True, 0.9), _result("t-002", False, 0.3)]
        )

        comparison = store.compare_runs("run_a", "run_b")

        assert comparison["has_regression"] is False


class TestEvalCli:
    """评测 CLI（--mock 冒烟模式，不调用外部服务）"""

    def test_run_mock_mode(self, tmp_path, monkeypatch):
        """冒烟模式跑通全链路并生成报告"""
        from app.eval import __main__ as cli

        dataset = tmp_path / "cases.yaml"
        dataset.write_text(
            """
cases:
  - id: t-001
    category: database_query
    query: 查询阿莫西林的库存
    must_include: ["冒烟"]
  - id: t-002
    category: network_search
    query: 检索 2026 年趋势
    must_include: ["不存在的关键词"]
""",
            encoding="utf-8",
        )
        report_path = tmp_path / "report.md"
        monkeypatch.setenv("EVAL_DB", str(tmp_path / "eval.db"))
        monkeypatch.setattr(
            "sys.argv",
            [
                "app.eval",
                "run",
                "--dataset",
                str(dataset),
                "--run-id",
                "cli_run",
                "--mock",
                "--report-path",
                str(report_path),
                "--min-pass-rate",
                "0.0",
            ],
        )

        exit_code = cli.main()

        assert exit_code == 0
        assert report_path.exists()
        report = report_path.read_text(encoding="utf-8")
        assert "cli_run" in report
        # t-002 的关键词不存在，应出现在失败用例里
        assert "t-002" in report

    def test_run_mock_gate_failure_exit_code(self, tmp_path, monkeypatch):
        """通过率低于门禁时退出码为 1（CI 门禁）"""
        from app.eval import __main__ as cli

        dataset = tmp_path / "cases.yaml"
        dataset.write_text(
            """
cases:
  - id: t-001
    category: database_query
    query: 查询库存
    must_include: ["一定不出现的关键词"]
""",
            encoding="utf-8",
        )
        monkeypatch.setenv("EVAL_DB", str(tmp_path / "eval.db"))
        monkeypatch.setattr(
            "sys.argv",
            ["app.eval", "run", "--dataset", str(dataset), "--mock", "--min-pass-rate", "0.8"],
        )

        assert cli.main() == 1

    def test_run_with_invalid_dataset(self, tmp_path, monkeypatch):
        """数据集非法时退出码为 2"""
        from app.eval import __main__ as cli

        dataset = tmp_path / "bad.yaml"
        dataset.write_text("not_cases: 1", encoding="utf-8")
        monkeypatch.setenv("EVAL_DB", str(tmp_path / "eval.db"))
        monkeypatch.setattr("sys.argv", ["app.eval", "run", "--dataset", str(dataset), "--mock"])

        assert cli.main() == 2

    def test_compare_command(self, tmp_path, monkeypatch):
        """compare 子命令输出回归报告，有回归时退出码为 1"""
        from app.eval import __main__ as cli

        store = EvalStore(tmp_path / "eval.db")
        store.save_run("run_a", "d.yaml", "m", [_result("t-001", True, 0.9)])
        store.save_run("run_b", "d.yaml", "m", [_result("t-001", True, 0.5)])
        monkeypatch.setenv("EVAL_DB", str(tmp_path / "eval.db"))
        monkeypatch.setattr(
            "sys.argv", ["app.eval", "compare", "--baseline", "run_a", "--current", "run_b"]
        )

        assert cli.main() == 1

    def test_compare_missing_run(self, tmp_path, monkeypatch):
        """对比不存在的运行退出码为 2"""
        from app.eval import __main__ as cli

        monkeypatch.setenv("EVAL_DB", str(tmp_path / "eval.db"))
        monkeypatch.setattr(
            "sys.argv", ["app.eval", "compare", "--baseline", "no_a", "--current", "no_b"]
        )

        assert cli.main() == 2

    def test_list_command(self, tmp_path, monkeypatch):
        """list 子命令输出历史运行"""
        from app.eval import __main__ as cli

        store = EvalStore(tmp_path / "eval.db")
        store.save_run("run_a", "d.yaml", "m", [_result("t-001", True, 0.9)])
        monkeypatch.setenv("EVAL_DB", str(tmp_path / "eval.db"))
        monkeypatch.setattr("sys.argv", ["app.eval", "list"])

        assert cli.main() == 0
