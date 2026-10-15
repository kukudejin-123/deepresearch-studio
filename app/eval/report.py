"""
评测报告生成

把一次评测运行的结果渲染为 Markdown 报告，供人工审查与 CI artifact 使用。
包含：总体通过率/平均分、分类通过情况、失败用例明细、回归对比。
"""

from typing import Any

from app.eval.dataset import CATEGORIES


def render_run_report(
    summary: dict[str, Any],
    results: list[dict[str, Any]],
    run_id: str,
) -> str:
    """
    渲染单次评测运行的 Markdown 报告

    :param summary: 运行摘要（run_id/total/passed/pass_rate/avg_score）
    :param results: 用例结果列表
    :param run_id: 运行 ID
    :return: Markdown 文本
    """
    lines: list[str] = []
    lines.append(f"# 评测报告 · {run_id}")
    lines.append("")
    lines.append(f"- 用例总数: **{summary.get('total', 0)}**")
    lines.append(f"- 通过: **{summary.get('passed', 0)}**")
    lines.append(f"- 通过率: **{summary.get('pass_rate', 0):.1%}**")
    lines.append(f"- 平均加权分: **{summary.get('avg_score', 0):.3f}**")
    lines.append("")

    # 分类通过情况
    lines.append("## 分类通过情况")
    lines.append("")
    lines.append("| 分类 | 总数 | 通过 | 通过率 |")
    lines.append("| --- | --- | --- | --- |")
    for category in CATEGORIES:
        category_results = [r for r in results if r.get("category") == category]
        if not category_results:
            continue
        total = len(category_results)
        passed = sum(1 for r in category_results if r.get("passed"))
        lines.append(f"| {category} | {total} | {passed} | {passed / total:.1%} |")
    lines.append("")

    # 失败用例明细
    failed = [r for r in results if not r.get("passed")]
    if failed:
        lines.append("## 失败用例")
        lines.append("")
        for result in failed:
            lines.append(
                f"- **{result.get('case_id')}**（{result.get('category')}，"
                f"加权分 {result.get('weighted_score', 0):.2f}）: "
                f"{result.get('failure_reason', '未知原因')}"
            )
        lines.append("")
    else:
        lines.append("## 失败用例")
        lines.append("")
        lines.append("全部通过。")
        lines.append("")

    return "\n".join(lines)


def render_comparison_report(comparison: dict[str, Any]) -> str:
    """
    渲染两次运行对比的 Markdown 报告

    :param comparison: compare_runs 的返回值
    :return: Markdown 文本
    """
    lines: list[str] = []
    lines.append("# 评测回归对比")
    lines.append("")
    lines.append(f"- 基线运行: `{comparison['baseline_run_id']}`")
    lines.append(f"- 当前运行: `{comparison['current_run_id']}`")
    lines.append(
        f"- 通过率: {comparison['baseline_pass_rate']:.1%} -> {comparison['current_pass_rate']:.1%}"
    )
    lines.append("")

    if comparison["regressions"]:
        lines.append(f"## 回归用例（{len(comparison['regressions'])}）")
        lines.append("")
        for item in comparison["regressions"]:
            flag = "（由通过转失败）" if item.get("flipped_to_fail") else ""
            lines.append(
                f"- **{item['case_id']}**{flag}: "
                f"{item['baseline_score']:.2f} -> {item['current_score']:.2f} "
                f"（{item['score_delta']:+.2f}）"
            )
            if item.get("failure_reason"):
                lines.append(f"  - 原因: {item['failure_reason']}")
        lines.append("")
    else:
        lines.append("## 回归用例")
        lines.append("")
        lines.append("未检测到回归。")
        lines.append("")

    if comparison["improvements"]:
        lines.append(f"## 改善用例（{len(comparison['improvements'])}）")
        lines.append("")
        for item in comparison["improvements"]:
            lines.append(
                f"- {item['case_id']}: {item['baseline_score']:.2f} -> "
                f"{item['current_score']:.2f}（{item['score_delta']:+.2f}）"
            )
        lines.append("")

    return "\n".join(lines)
