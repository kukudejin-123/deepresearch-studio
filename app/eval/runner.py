"""
评测执行器

把评测集里的每个用例真正跑一遍 Agent，收集回答与执行事件，
按“确定性检查 + 事件链路检查 + LLM 评审”三层判定用例是否通过。

执行方式复用主链路 `run_deep_agent`：执行事件天然落库到 EventStore，
因此执行过程无需额外埋点——跑完后按 thread_id 读回即可。

依赖注入点：
- agent_runner：默认走真实 Agent；测试时可替换为假执行器；
- judge：默认走真实 LLM 评审；测试时用 make_mock_judge。
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.eval.dataset import EvalCase, run_deterministic_checks, run_event_checks
from app.eval.judge import compute_weighted_score, make_llm_judge
from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 用例通过所需的加权分数下限（可用 EVAL_PASS_THRESHOLD 覆盖）
DEFAULT_PASS_THRESHOLD = 0.6

AgentRunner = Callable[..., Awaitable[tuple[str, list[dict]]]]
JudgeFn = Callable[[EvalCase, str, list[dict]], dict[str, Any]]


@dataclass
class CaseResult:
    """单个用例的评测结果"""

    case_id: str
    category: str
    passed: bool
    weighted_score: float
    deterministic_passed: bool
    event_check_passed: bool
    scores: dict[str, Any] = field(default_factory=dict)
    answer_excerpt: str = ""
    failure_reason: str = ""
    events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为可入库的字典（不含完整事件，避免存储膨胀）"""
        return {
            "case_id": self.case_id,
            "category": self.category,
            "passed": self.passed,
            "weighted_score": self.weighted_score,
            "deterministic_passed": self.deterministic_passed,
            "event_check_passed": self.event_check_passed,
            "scores": self.scores,
            "answer_excerpt": self.answer_excerpt,
            "failure_reason": self.failure_reason,
        }


def default_agent_runner_factory() -> AgentRunner:
    """
    构造默认的 Agent 执行器（跑真实 Agent + 从事件存储读回过程）

    用例的 seed_files 会被预置到 updated/session_{thread_id}/，
    run_deep_agent 启动时自动复制进工作目录，文件读取类用例由此获得输入。

    :return: agent_runner(query, seed_files) -> (answer, events)
    """

    async def run(query: str, seed_files: Optional[list[str]] = None) -> tuple[str, list[dict]]:
        import shutil

        from app.agent.main_agent import project_root_path, run_deep_agent
        from app.api.context import set_thread_context
        from app.events import get_event_store

        thread_id = f"eval_{uuid.uuid4().hex[:10]}"
        # 写入 thread 上下文：monitor 据此把事件归到该 thread
        set_thread_context(thread_id)

        # 预置种子文件（文件读取类用例的输入）
        if seed_files:
            updated_dir = project_root_path / "updated" / f"session_{thread_id}"
            updated_dir.mkdir(parents=True, exist_ok=True)
            for seed in seed_files:
                seed_path = Path(seed)
                if not seed_path.is_absolute():
                    seed_path = project_root_path / "examples" / "test_docs" / seed
                if seed_path.exists():
                    shutil.copy2(seed_path, updated_dir / seed_path.name)
                else:
                    logger.warning("种子文件不存在，已跳过: %s", seed)

        try:
            await run_deep_agent(query, thread_id, user_id=None)
        finally:
            pass

        events = get_event_store().list(thread_id)
        answer = ""
        for event in events:
            if event.get("event") == "task_result":
                answer = str((event.get("data") or {}).get("result", ""))
        return answer, events

    return run


class EvalRunner:
    """评测执行器：按顺序（或有限并发）跑完整个评测集"""

    def __init__(
        self,
        agent_runner: Optional[AgentRunner] = None,
        judge: Optional[JudgeFn] = None,
        pass_threshold: float = DEFAULT_PASS_THRESHOLD,
    ):
        """
        :param agent_runner: Agent 执行器；默认走真实 Agent
        :param judge: 评审函数；默认走真实 LLM 评审
        :param pass_threshold: 通过所需的加权分数下限
        """
        self.agent_runner = agent_runner or default_agent_runner_factory()
        self.judge = judge or make_llm_judge()
        self.pass_threshold = pass_threshold

    async def run_case(self, case: EvalCase) -> CaseResult:
        """
        执行单个评测用例

        :param case: 评测用例
        :return: 用例结果
        """
        logger.info("评测用例开始: %s（%s）", case.id, case.category)

        # 1. 执行 Agent（携带用例的种子文件，文件读取类用例的输入）
        try:
            answer, events = await self.agent_runner(case.query, case.seed_files)
        except Exception as e:
            logger.warning("评测用例执行失败: %s, error=%s", case.id, e)
            return CaseResult(
                case_id=case.id,
                category=case.category,
                passed=False,
                weighted_score=0.0,
                deterministic_passed=False,
                event_check_passed=False,
                failure_reason=f"agent_error: {e}",
            )

        # 2. 确定性检查（不依赖 LLM）
        deterministic = run_deterministic_checks(case, answer)
        event_check = run_event_checks(case, events)

        # 3. LLM 评审（失败不阻塞：记录原因，确定性结果仍然有效）
        judge_error = ""
        try:
            scores = self.judge(case, answer, events)
        except Exception as e:
            logger.warning("评审失败: %s, error=%s", case.id, e)
            judge_error = str(e)
            scores = {
                "correctness": 0.0,
                "completeness": 0.0,
                "groundedness": 0.0,
                "reasoning": judge_error,
            }

        weighted = compute_weighted_score(scores, case.weights)
        passed = (
            deterministic["passed"]
            and event_check["passed"]
            and not judge_error
            and weighted >= self.pass_threshold
        )

        failure_reason = _build_failure_reason(
            deterministic, event_check, judge_error, weighted, self.pass_threshold
        )

        return CaseResult(
            case_id=case.id,
            category=case.category,
            passed=passed,
            weighted_score=weighted,
            deterministic_passed=deterministic["passed"],
            event_check_passed=event_check["passed"],
            scores=scores,
            answer_excerpt=answer[:300],
            failure_reason=failure_reason,
            events=events,
        )

    async def run_all(
        self,
        cases: list[EvalCase],
        concurrency: int = 1,
    ) -> list[CaseResult]:
        """
        执行整个评测集

        :param cases: 用例列表
        :param concurrency: 并发数（Agent 执行较重，默认顺序执行）
        :return: 用例结果列表（按输入顺序）
        """
        if concurrency <= 1:
            return [await self.run_case(case) for case in cases]

        semaphore = asyncio.Semaphore(concurrency)

        async def run_with_limit(case: EvalCase) -> CaseResult:
            async with semaphore:
                return await self.run_case(case)

        return await asyncio.gather(*(run_with_limit(case) for case in cases))


def _build_failure_reason(
    deterministic: dict,
    event_check: dict,
    judge_error: str,
    weighted: float,
    threshold: float,
) -> str:
    """汇总失败原因（空字符串表示通过）"""
    reasons: list[str] = []
    if deterministic["missing_keywords"]:
        reasons.append(f"缺少关键信息: {deterministic['missing_keywords']}")
    if deterministic["leaked_keywords"]:
        reasons.append(f"包含禁用语: {deterministic['leaked_keywords']}")
    if event_check["missing_events"]:
        reasons.append(f"缺少期望事件: {event_check['missing_events']}")
    if case_tools_missing(event_check):
        reasons.append("未命中期望工具")
    if judge_error:
        reasons.append(f"评审失败: {judge_error[:100]}")
    elif weighted < threshold:
        reasons.append(f"加权分 {weighted} 低于阈值 {threshold}")
    return "；".join(reasons)


def case_tools_missing(event_check: dict) -> bool:
    """事件检查中期望工具未命中"""
    return event_check.get("expected_tools_hit") is False
