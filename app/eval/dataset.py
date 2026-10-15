"""
Agent 评测数据集模块

第三期质量体系的核心数据层：把“这个 Agent 好不好”拆成可执行、可回归的
评测用例。每个用例包含：

- 任务内容（query）与分类（网络检索 / 数据库 / 知识库 / 文件 / 多源综合）；
- 确定性期望：答案必须包含的关键信息、不得出现的错误信息、
  期望触发的事件类型（用于校验工具链路真的被走到）；
- 评审维度提示（rubric_hints）：交给 LLM 评审的补充说明。

确定性检查不调用 LLM，可在 CI 中零成本运行；LLM 评审按需触发。
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 用例分类（与系统的检索来源一一对应）
CATEGORIES = (
    "network_search",
    "database_query",
    "knowledge_base",
    "file_reading",
    "multi_source",
    "file_generation",
)


@dataclass
class EvalCase:
    """单个评测用例"""

    id: str
    category: str
    query: str
    # 确定性检查：答案必须包含的子串（大小写不敏感）
    must_include: list[str] = field(default_factory=list)
    # 确定性检查：答案不得包含的子串
    must_not_include: list[str] = field(default_factory=list)
    # 期望出现的事件类型（如 tool_start / assistant_call），用于校验链路
    expected_events: list[str] = field(default_factory=list)
    # 期望调用的工具名（匹配 monitor 事件的 tool_name 参数）
    expected_tools: list[str] = field(default_factory=list)
    # 执行前预置到会话目录的文件（相对 examples/test_docs 或绝对路径），
    # 用于文件读取类用例；run_deep_agent 会把它复制进工作目录
    seed_files: list[str] = field(default_factory=list)
    # 交给 LLM 评审的补充说明
    rubric_hints: str = ""
    # 评审维度权重（correctness / completeness / groundedness）
    weights: dict[str, float] = field(
        default_factory=lambda: {"correctness": 0.4, "completeness": 0.3, "groundedness": 0.3}
    )

    def validate(self) -> list[str]:
        """
        校验用例自身的合法性

        :return: 错误信息列表；空列表表示通过
        """
        errors: list[str] = []
        if not self.id:
            errors.append("缺少用例 ID")
        if self.category not in CATEGORIES:
            errors.append(
                f"用例 {self.id}: 分类 '{self.category}' 不合法，可选: {'、'.join(CATEGORIES)}"
            )
        if not self.query or not self.query.strip():
            errors.append(f"用例 {self.id}: 任务内容为空")
        return errors


def load_dataset(path: str | Path) -> list[EvalCase]:
    """
    从 YAML 文件加载评测集

    文件格式：
        cases:
          - id: net-001
            category: network_search
            query: ...
            must_include: [...]
            ...

    :param path: 数据集文件路径
    :return: 用例列表（按 id 排序）
    :raises ValueError: 文件格式错误或用例校验失败
    """
    dataset_path = Path(path)
    if not dataset_path.exists():
        raise ValueError(f"评测集文件不存在: {dataset_path}")

    with open(dataset_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict) or "cases" not in raw:
        raise ValueError("评测集格式错误: 顶层必须是包含 'cases' 键的映射")

    raw_cases = raw["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("评测集格式错误: 'cases' 必须是非空列表")

    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            raise ValueError(f"第 {index + 1} 个用例不是映射结构")

        case = EvalCase(
            id=str(item.get("id", "")).strip(),
            category=str(item.get("category", "")).strip(),
            query=str(item.get("query", "")).strip(),
            must_include=[str(x) for x in item.get("must_include", [])],
            must_not_include=[str(x) for x in item.get("must_not_include", [])],
            expected_events=[str(x) for x in item.get("expected_events", [])],
            expected_tools=[str(x) for x in item.get("expected_tools", [])],
            seed_files=[str(x) for x in item.get("seed_files", [])],
            rubric_hints=str(item.get("rubric_hints", "")),
            weights=dict(item.get("weights", {}) or {}),
        )

        errors = case.validate()
        if errors:
            raise ValueError("；".join(errors))
        if case.id in seen_ids:
            raise ValueError(f"用例 ID 重复: {case.id}")
        seen_ids.add(case.id)
        cases.append(case)

    logger.info("评测集加载完成: %d 个用例（来源: %s）", len(cases), dataset_path)
    return sorted(cases, key=lambda c: c.id)


def run_deterministic_checks(case: EvalCase, answer: str) -> dict[str, Any]:
    """
    执行确定性检查（不调用 LLM）

    :param case: 评测用例
    :param answer: Agent 给出的答案文本
    :return: 检查结果字典，包含 passed 与逐项明细
    """
    normalized = _normalize(answer)

    missing = [kw for kw in case.must_include if _normalize(kw) not in normalized]
    leaked = [kw for kw in case.must_not_include if _normalize(kw) in normalized]

    return {
        "passed": not missing and not leaked,
        "missing_keywords": missing,
        "leaked_keywords": leaked,
    }


def run_event_checks(
    case: EvalCase,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    校验执行事件是否覆盖期望的工具链路

    :param case: 评测用例
    :param events: 该次执行产生的 monitor 事件列表
    :return: 检查结果字典
    """
    event_types = {str(e.get("event", "")) for e in events}
    tool_names = {str((e.get("data") or {}).get("tool_name", "")) for e in events}

    missing_events = [e for e in case.expected_events if e not in event_types]
    # expected_tools 采用“全部命中”语义：列出的每个工具都需出现
    # （大小写不敏感的子串匹配，用例里写“网络搜索”或“internet_search”均可），
    # 多源检索用例据此校验多条链路都被真正走到。
    # 注意：是“每个工具名作为子串出现在某个观测工具名中”，
    # 不能用集合成员判断（那要求完全相等）。
    normalized_tools = [name.lower() for name in tool_names]
    missing_tools = [
        expected
        for expected in case.expected_tools
        if not any(expected.lower() in name for name in normalized_tools)
    ]

    return {
        "passed": not missing_events and not missing_tools,
        "missing_events": missing_events,
        "missing_tools": missing_tools,
        "expected_tools_hit": not missing_tools,
        "observed_tools": sorted(name for name in tool_names if name),
    }


def _normalize(text: str) -> str:
    """归一化文本：小写 + 压缩空白，降低匹配噪声"""
    return re.sub(r"\s+", "", str(text).lower())


def dataset_stats(cases: list[EvalCase]) -> dict[str, int]:
    """按分类统计用例数量"""
    stats = {category: 0 for category in CATEGORIES}
    for case in cases:
        stats[case.category] = stats.get(case.category, 0) + 1
    return stats
