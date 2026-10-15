"""
评测数据集测试

覆盖 YAML 加载、用例校验、确定性检查与事件链路检查。
所有检查都不调用 LLM，可在 CI 中零成本运行。
"""

import pytest
import yaml

from app.eval.dataset import (
    CATEGORIES,
    dataset_stats,
    load_dataset,
    run_deterministic_checks,
    run_event_checks,
)

VALID_CASE = {
    "id": "db-001",
    "category": "database_query",
    "query": "查询阿莫西林的库存",
    "must_include": ["阿莫西林"],
    "must_not_include": ["无法查询"],
    "expected_events": ["tool_start"],
    "expected_tools": ["数据库"],
}


@pytest.fixture
def dataset_file(tmp_path):
    """构造临时评测集文件"""

    def _write(cases):
        path = tmp_path / "cases.yaml"
        path.write_text(yaml.safe_dump({"cases": cases}, allow_unicode=True), encoding="utf-8")
        return path

    return _write


class TestLoadDataset:
    """评测集加载"""

    def test_loads_valid_cases(self, dataset_file):
        """合法评测集正常加载，字段完整"""
        cases = load_dataset(
            dataset_file([VALID_CASE, dict(VALID_CASE, id="net-001", category="network_search")])
        )

        assert len(cases) == 2
        # 按 id 排序
        assert [c.id for c in cases] == ["db-001", "net-001"]
        assert cases[0].must_include == ["阿莫西林"]
        assert cases[0].expected_tools == ["数据库"]

    def test_rejects_missing_file(self, tmp_path):
        """文件不存在抛出 ValueError"""
        with pytest.raises(ValueError, match="不存在"):
            load_dataset(tmp_path / "no-such.yaml")

    def test_rejects_wrong_top_level(self, tmp_path):
        """顶层缺少 cases 键时报错"""
        path = tmp_path / "bad.yaml"
        path.write_text("other: 1", encoding="utf-8")

        with pytest.raises(ValueError, match="cases"):
            load_dataset(path)

    def test_rejects_empty_cases(self, tmp_path):
        """空 cases 列表报错"""
        path = tmp_path / "empty.yaml"
        path.write_text(yaml.safe_dump({"cases": []}), encoding="utf-8")

        with pytest.raises(ValueError, match="非空"):
            load_dataset(path)

    def test_rejects_invalid_category(self, dataset_file):
        """非法分类报错并提示可选值"""
        with pytest.raises(ValueError, match="分类"):
            load_dataset(dataset_file([dict(VALID_CASE, category="not_a_category")]))

    def test_rejects_duplicate_id(self, dataset_file):
        """重复用例 ID 报错"""
        with pytest.raises(ValueError, match="重复"):
            load_dataset(dataset_file([VALID_CASE, dict(VALID_CASE)]))

    def test_rejects_empty_query(self, dataset_file):
        """空任务内容报错"""
        with pytest.raises(ValueError, match="任务内容为空"):
            load_dataset(dataset_file([dict(VALID_CASE, query="  ")]))

    def test_loads_seed_files_and_hints(self, dataset_file):
        """种子文件与评审提示正确加载"""
        cases = load_dataset(
            dataset_file(
                [
                    dict(
                        VALID_CASE,
                        category="file_reading",
                        seed_files=["sub_dir/测试文件.md"],
                        rubric_hints="基于文件内容",
                    )
                ]
            )
        )

        assert cases[0].seed_files == ["sub_dir/测试文件.md"]
        assert cases[0].rubric_hints == "基于文件内容"


class TestDeterministicChecks:
    """确定性检查（不调用 LLM）"""

    def _case(self, **overrides):
        from app.eval.dataset import EvalCase

        base = {
            "id": "t-001",
            "category": "database_query",
            "query": "查询库存",
        }
        base.update(overrides)
        return EvalCase(**base)

    def test_passes_when_all_keywords_present(self):
        """关键信息齐全时通过"""
        case = self._case(must_include=["阿莫西林", "库存"])

        result = run_deterministic_checks(case, "阿莫西林胶囊当前库存为 5000")

        assert result["passed"] is True
        assert result["missing_keywords"] == []

    def test_fails_when_keyword_missing(self):
        """缺少关键信息时失败并列出缺失项"""
        case = self._case(must_include=["布洛芬"])

        result = run_deterministic_checks(case, "阿莫西林胶囊库存 5000")

        assert result["passed"] is False
        assert result["missing_keywords"] == ["布洛芬"]

    def test_fails_when_forbidden_keyword_present(self):
        """出现禁用语时失败"""
        case = self._case(must_not_include=["无法查询"])

        result = run_deterministic_checks(case, "无法查询该数据")

        assert result["passed"] is False
        assert result["leaked_keywords"] == ["无法查询"]

    def test_matching_ignores_case_and_whitespace(self):
        """匹配忽略大小写与空白差异"""
        case = self._case(must_include=["Qwen Max"])

        result = run_deterministic_checks(case, "使用 qwen   max 模型")

        assert result["passed"] is True


class TestEventChecks:
    """执行链路检查"""

    def _case(self, **overrides):
        from app.eval.dataset import EvalCase

        base = {"id": "t-001", "category": "multi_source", "query": "综合分析"}
        base.update(overrides)
        return EvalCase(**base)

    def _events(self, *tool_names):
        return [{"event": "tool_start", "data": {"tool_name": name}} for name in tool_names]

    def test_passes_when_all_expected_tools_hit(self):
        """期望工具全部出现时通过（子串匹配）"""
        case = self._case(expected_tools=["数据库", "知识库"])

        result = run_event_checks(
            case,
            self._events(
                "数据库表名查询工具：list_sql_tables", "知识库检索工具：search_knowledge_base"
            ),
        )

        assert result["passed"] is True
        assert result["missing_tools"] == []

    def test_fails_when_tool_missing(self):
        """多源用例中任一工具未出现即失败"""
        case = self._case(expected_tools=["网络搜索", "数据库"])

        result = run_event_checks(case, self._events("数据库表名查询工具：list_sql_tables"))

        assert result["passed"] is False
        assert result["missing_tools"] == ["网络搜索"]

    def test_fails_when_event_type_missing(self):
        """期望事件类型缺失时失败"""
        case = self._case(expected_events=["assistant_call"])

        result = run_event_checks(case, self._events("网络搜索工具"))

        assert result["passed"] is False
        assert result["missing_events"] == ["assistant_call"]

    def test_no_expectations_always_passes(self):
        """无期望时直接通过"""
        result = run_event_checks(self._case(), self._events("任意工具"))

        assert result["passed"] is True


class TestDatasetStats:
    """数据集统计"""

    def test_counts_by_category(self, dataset_file):
        """按分类统计数量"""
        cases = load_dataset(
            dataset_file(
                [
                    VALID_CASE,
                    dict(VALID_CASE, id="net-001", category="network_search"),
                    dict(VALID_CASE, id="net-002", category="network_search"),
                ]
            )
        )

        stats = dataset_stats(cases)

        assert stats["database_query"] == 1
        assert stats["network_search"] == 2
        assert stats["knowledge_base"] == 0
        assert set(stats) == set(CATEGORIES)
