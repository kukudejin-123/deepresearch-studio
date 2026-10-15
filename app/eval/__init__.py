"""
Agent 评测体系（第三期质量保障）

把“Agent 好不好”变成可执行、可回归的工程问题：

- dataset：评测集定义与加载（YAML），含确定性期望与评审提示；
- judge：LLM-as-judge 按 rubric 打分（correctness / completeness / groundedness）；
- runner：执行器，跑真实 Agent 并收集回答与执行事件，三层判定；
- store：SQLite 结果存储，支持运行间对比与回归检测；
- report：Markdown 报告渲染；
- CLI：python -m app.eval run / compare / list。
"""

from app.eval.dataset import EvalCase, load_dataset
from app.eval.runner import CaseResult, EvalRunner

__all__ = ["CaseResult", "EvalCase", "EvalRunner", "load_dataset"]
