"""
LLM 评审模块（LLM-as-judge）

用大模型按 rubric 对 Agent 的回答打分，是 Agent 项目质量评估的标准做法。
评分维度：

- correctness（正确性）：事实与数据是否准确；
- completeness（完整性）：是否覆盖了任务要求的各个方面；
- groundedness（有据性）：结论是否有检索来源支撑，而非凭空编造。

设计要点：
1. 评审 prompt 与用例的 rubric_hints 组合，输出强制 JSON，解析失败可重试一次；
2. 评审模型通过 EVAL_JUDGE_MODEL 配置（默认与对话模型一致），走同一
   OpenAI 兼容端点；
3. judge 以可注入的 callable 形式提供，测试中用 MockJudge 替代，不调真实 API。
"""

import json
import os
import re
from typing import Any, Callable, Optional

from app.eval.dataset import EvalCase
from app.utils.logging_config import get_logger

logger = get_logger(__name__)

# 评分上下界
SCORE_MIN, SCORE_MAX = 0.0, 1.0

JUDGE_SYSTEM_PROMPT = (
    "你是一个严格的 AI Agent 输出评审专家。"
    "根据给定的任务、Agent 回答和执行过程，按三个维度打分（0~1 的一位小数）：\n"
    "\n"
    "- correctness: 事实与数据是否准确，有无编造；\n"
    "- completeness: 是否完整回应了任务的每个方面；\n"
    "- groundedness: 关键结论是否有检索来源（工具调用结果）支撑。\n"
    "\n"
    "评判要求：\n"
    "1. 只依据给定材料评判，不引入外部知识；\n"
    "2. 回答中的工具调用过程可用于判断 groundedness；\n"
    "3. 宁严勿松：编造事实、答非所问应给低分。\n"
    "\n"
    "只输出 JSON，格式：\n"
    '{"correctness": 0.0, "completeness": 0.0, "groundedness": 0.0, '
    '"reasoning": "一句话说明"}'
)


def build_judge_prompt(case: EvalCase, answer: str, events: list[dict[str, Any]]) -> str:
    """
    构造评审用户提示词

    :param case: 评测用例
    :param answer: Agent 回答
    :param events: 执行事件（截断，避免 prompt 过长）
    :return: 评审提示词
    """
    event_lines = []
    for event in events[-30:]:
        event_type = event.get("event", "")
        message = str(event.get("message", ""))[:120]
        event_lines.append(f"- {event_type}: {message}")
    event_text = "\n".join(event_lines) if event_lines else "（无执行事件记录）"

    return f"""【任务分类】{case.category}
【任务内容】{case.query}
【评审补充说明】{case.rubric_hints or "无"}
【Agent 回答】
{answer[:4000]}
【执行过程（工具调用等）】
{event_text}

请按三个维度打分并只输出 JSON。"""


def parse_judge_response(text: str) -> dict[str, Any]:
    """
    解析评审响应为结构化分数

    容忍 markdown 代码块包裹与前后杂质文本；解析失败抛出 ValueError。

    :param text: 评审模型原始输出
    :return: {"correctness": float, "completeness": float, "groundedness": float, "reasoning": str}
    """
    candidate = text.strip()
    # 去掉 markdown 代码块包裹
    fenced = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()

    # 定位第一个 { 到最后一个 }，容忍模型前后多说几句话
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"评审响应中未找到 JSON 对象: {text[:200]}")

    try:
        payload = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError as e:
        raise ValueError(f"评审响应 JSON 解析失败: {e}") from e

    scores: dict[str, Any] = {}
    for dimension in ("correctness", "completeness", "groundedness"):
        value = payload.get(dimension)
        if not isinstance(value, (int, float)):
            raise ValueError(f"评审响应缺少维度分数: {dimension}")
        scores[dimension] = round(float(value), 3)
        if not (SCORE_MIN <= scores[dimension] <= SCORE_MAX):
            raise ValueError(f"评审分数越界: {dimension}={scores[dimension]}")

    scores["reasoning"] = str(payload.get("reasoning", ""))[:500]
    return scores


def compute_weighted_score(scores: dict[str, float], weights: dict[str, float]) -> float:
    """
    按用例权重计算加权总分

    权重为空（用例未配置）时退化为三个维度的算术平均，避免除零。

    :param scores: 各维度分数
    :param weights: 各维度权重
    :return: 加权总分（0~1）
    """
    effective_weights = weights or {
        dimension: 1.0 for dimension in ("correctness", "completeness", "groundedness")
    }
    total = sum(effective_weights.values()) or 1.0
    weighted = sum(scores.get(dim, 0.0) * weight for dim, weight in effective_weights.items())
    return round(weighted / total, 3)


def default_judge_model() -> str:
    """默认评审模型（与对话模型一致，可用 EVAL_JUDGE_MODEL 覆盖）"""
    return os.getenv("EVAL_JUDGE_MODEL") or os.getenv("LLM_QWEN_MAX", "qwen-max")


def make_llm_judge() -> Callable[[EvalCase, str, list[dict]], dict[str, Any]]:
    """
    构造基于真实 LLM 的评审函数

    :return: judge(case, answer, events) -> scores dict
    """

    def judge(case: EvalCase, answer: str, events: list[dict]) -> dict[str, Any]:
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model=default_judge_model(),
            temperature=0,
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
        )
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": build_judge_prompt(case, answer, events)},
        ]
        response = model.invoke(messages)
        try:
            return parse_judge_response(response.content)
        except ValueError:
            # 首次解析失败：追加一条纠正消息重试一次
            retry = model.invoke(
                messages
                + [
                    {"role": "assistant", "content": response.content},
                    {
                        "role": "user",
                        "content": "上次输出不是合法 JSON。请只输出要求的 JSON，不要任何其他文字。",
                    },
                ]
            )
            return parse_judge_response(retry.content)

    return judge


def make_mock_judge(
    scores: Optional[dict[str, float]] = None,
) -> Callable[[EvalCase, str, list[dict]], dict[str, Any]]:
    """
    构造固定分数的评审函数（测试用）

    :param scores: 固定分数；默认全 0.8
    :return: judge 函数
    """
    fixed = scores or {"correctness": 0.8, "completeness": 0.8, "groundedness": 0.8}

    def judge(case: EvalCase, answer: str, events: list[dict]) -> dict[str, Any]:
        result = dict(fixed)
        result["reasoning"] = "mock judge"
        return result

    return judge
