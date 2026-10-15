"""
评测 CLI

用法：
    # 跑评测（真实 Agent + 真实 LLM 评审）
    uv run python -m app.eval run --dataset docs/eval/cases.yaml

    # 冒烟测试框架本身（假执行器 + 假评审，不调用任何外部服务）
    uv run python -m app.eval run --dataset docs/eval/cases.yaml --mock

    # 对比两次运行，检测回归
    uv run python -m app.eval compare --baseline <run_id> --current <run_id>

    # 查看历史运行
    uv run python -m app.eval list
"""

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

from app.utils.logging_config import get_logger, setup_logging

logger = get_logger(__name__)

# 评测库默认路径（可用 EVAL_DB 覆盖）
_DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "eval.db"


def _get_store():
    from app.eval.store import EvalStore

    return EvalStore(os.getenv("EVAL_DB", str(_DEFAULT_DB)))


def cmd_run(args: argparse.Namespace) -> int:
    """执行评测运行"""
    from app.eval.dataset import load_dataset
    from app.eval.judge import default_judge_model, make_mock_judge
    from app.eval.report import render_run_report
    from app.eval.runner import EvalRunner

    setup_logging("INFO")

    try:
        cases = load_dataset(args.dataset)
    except ValueError as e:
        print(f"评测集加载失败: {e}", file=sys.stderr)
        return 2

    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [case for case in cases if case.id in wanted]
        if not cases:
            print(f"没有匹配的用例: {args.cases}", file=sys.stderr)
            return 2

    if args.mock:
        # 冒烟模式：假执行器返回固定答案，假评审返回固定分数
        async def fake_agent_runner(query: str):
            return "这是一个用于冒烟测试的固定回答。", []

        runner = EvalRunner(agent_runner=fake_agent_runner, judge=make_mock_judge())
        judge_model = "mock"
    else:
        runner = EvalRunner()
        judge_model = default_judge_model()

    run_id = args.run_id or f"run_{uuid.uuid4().hex[:10]}"
    print(f"开始评测: run_id={run_id}, 用例数={len(cases)}, 评审模型={judge_model}")

    results = asyncio.run(runner.run_all(cases, concurrency=args.concurrency))
    summary = _get_store().save_run(
        run_id=run_id,
        dataset_path=str(args.dataset),
        judge_model=judge_model,
        results=[result.to_dict() for result in results],
    )

    report = render_run_report(summary, [r.to_dict() for r in results], run_id)
    print(report)

    if args.report_path:
        Path(args.report_path).write_text(report, encoding="utf-8")
        print(f"报告已写入: {args.report_path}")

    # 通过率低于阈值时以非零退出码结束，便于 CI 门禁
    if summary["pass_rate"] < args.min_pass_rate:
        print(
            f"通过率 {summary['pass_rate']:.1%} 低于门禁 {args.min_pass_rate:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    """对比两次运行，检测回归"""
    from app.eval.report import render_comparison_report

    setup_logging("INFO")
    store = _get_store()

    if store.get_run(args.baseline) is None:
        print(f"基线运行不存在: {args.baseline}", file=sys.stderr)
        return 2
    if store.get_run(args.current) is None:
        print(f"当前运行不存在: {args.current}", file=sys.stderr)
        return 2

    comparison = store.compare_runs(args.baseline, args.current)
    print(render_comparison_report(comparison))

    return 1 if comparison["has_regression"] else 0


def cmd_list(args: argparse.Namespace) -> int:
    """列出历史运行"""
    setup_logging("INFO")
    runs = _get_store().list_runs(limit=args.limit)
    if not runs:
        print("暂无评测记录")
        return 0

    print(f"{'run_id':<24} {'通过':>10} {'平均分':>8}  时间")
    for run in runs:
        print(
            f"{run['run_id']:<24} "
            f"{run['passed']:>4}/{run['total']:<5} "
            f"{run['avg_score']:>8.3f}  {run['created_at']}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    """构造 CLI 参数解析器"""
    parser = argparse.ArgumentParser(prog="app.eval", description="DeepResearch Studio 评测工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="执行评测运行")
    run_parser.add_argument("--dataset", required=True, help="评测集 YAML 路径")
    run_parser.add_argument("--run-id", default=None, help="运行 ID（默认自动生成）")
    run_parser.add_argument("--cases", default=None, help="只跑指定用例（逗号分隔）")
    run_parser.add_argument("--concurrency", type=int, default=1, help="并发数（默认 1）")
    run_parser.add_argument("--mock", action="store_true", help="冒烟模式：不调用外部服务")
    run_parser.add_argument("--report-path", default=None, help="报告输出路径")
    run_parser.add_argument(
        "--min-pass-rate", type=float, default=0.8, help="通过率门禁（默认 0.8）"
    )
    run_parser.set_defaults(func=cmd_run)

    compare_parser = subparsers.add_parser("compare", help="对比两次运行检测回归")
    compare_parser.add_argument("--baseline", required=True, help="基线运行 ID")
    compare_parser.add_argument("--current", required=True, help="当前运行 ID")
    compare_parser.set_defaults(func=cmd_compare)

    list_parser = subparsers.add_parser("list", help="列出历史运行")
    list_parser.add_argument("--limit", type=int, default=20)
    list_parser.set_defaults(func=cmd_list)

    return parser


def main() -> int:
    """CLI 入口"""
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
