"""
评测结果存储与回归对比模块

每次评测运行（run）产生一组用例结果，落库后可：

- 按 run 查询历史成绩，观察质量变化趋势；
- 对比任意两次 run，标记分数下降或由通过变失败的用例（回归）；
- 生成通过率 / 平均分摘要，供 CI 门禁使用。
"""

import sqlite3
from pathlib import Path
from typing import Any, Optional

from app.utils.logging_config import get_logger

logger = get_logger(__name__)

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id TEXT PRIMARY KEY,
    dataset_path TEXT NOT NULL,
    judge_model TEXT NOT NULL,
    total INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    avg_score REAL NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_RESULTS_SQL = """
CREATE TABLE IF NOT EXISTS eval_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    category TEXT NOT NULL,
    passed INTEGER NOT NULL,
    weighted_score REAL NOT NULL,
    deterministic_passed INTEGER NOT NULL,
    event_check_passed INTEGER NOT NULL,
    scores TEXT NOT NULL DEFAULT '{}',
    answer_excerpt TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (run_id, case_id)
)
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_eval_results_run
ON eval_results (run_id)
"""


class EvalStore:
    """基于 SQLite 的评测结果存储（WAL 模式，支持并发读写）"""

    def __init__(self, db_path: str | Path):
        """
        :param db_path: SQLite 数据库文件路径
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(_CREATE_TABLES_SQL)
            conn.execute(_CREATE_RESULTS_SQL)
            conn.execute(_CREATE_INDEX_SQL)

    def save_run(
        self,
        run_id: str,
        dataset_path: str,
        judge_model: str,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        保存一次评测运行

        :param run_id: 运行 ID（通常为时间戳或 CI run number）
        :param dataset_path: 数据集文件路径
        :param judge_model: 评审模型名
        :param results: 用例结果列表（见 runner.CaseResult.to_dict）
        :return: 运行摘要
        """
        total = len(results)
        passed = sum(1 for r in results if r.get("passed"))
        avg_score = (
            round(sum(float(r.get("weighted_score", 0.0)) for r in results) / total, 3)
            if total
            else 0.0
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO eval_runs (
                    run_id, dataset_path, judge_model, total, passed, avg_score, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                ON CONFLICT(run_id) DO UPDATE SET
                    total = excluded.total,
                    passed = excluded.passed,
                    avg_score = excluded.avg_score,
                    created_at = excluded.created_at
                """,
                (run_id, dataset_path, judge_model, total, passed, avg_score),
            )
            for result in results:
                conn.execute(
                    """
                    INSERT INTO eval_results (
                        run_id, case_id, category, passed, weighted_score,
                        deterministic_passed, event_check_passed, scores,
                        answer_excerpt, failure_reason, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))
                    ON CONFLICT(run_id, case_id) DO UPDATE SET
                        passed = excluded.passed,
                        weighted_score = excluded.weighted_score,
                        deterministic_passed = excluded.deterministic_passed,
                        event_check_passed = excluded.event_check_passed,
                        scores = excluded.scores,
                        answer_excerpt = excluded.answer_excerpt,
                        failure_reason = excluded.failure_reason,
                        created_at = excluded.created_at
                    """,
                    (
                        run_id,
                        result["case_id"],
                        result.get("category", ""),
                        1 if result.get("passed") else 0,
                        float(result.get("weighted_score", 0.0)),
                        1 if result.get("deterministic_passed") else 0,
                        1 if result.get("event_check_passed") else 0,
                        json_dumps_safe(result.get("scores", {})),
                        str(result.get("answer_excerpt", ""))[:500],
                        str(result.get("failure_reason", ""))[:500],
                    ),
                )

        summary = {
            "run_id": run_id,
            "total": total,
            "passed": passed,
            "pass_rate": round(passed / total, 3) if total else 0.0,
            "avg_score": avg_score,
        }
        logger.info(
            "评测运行已保存: run_id=%s, 通过 %d/%d, 平均分 %.3f",
            run_id,
            passed,
            total,
            avg_score,
        )
        return summary

    def get_run(self, run_id: str) -> Optional[dict[str, Any]]:
        """查询运行摘要；不存在返回 None"""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM eval_runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """查询最近的运行摘要（按时间倒序，同一秒内按提交顺序）"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM eval_runs ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_results(self, run_id: str) -> list[dict[str, Any]]:
        """查询某次运行的全部用例结果"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM eval_results WHERE run_id = ? ORDER BY case_id", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def compare_runs(
        self,
        baseline_run_id: str,
        current_run_id: str,
        score_drop_threshold: float = 0.1,
    ) -> dict[str, Any]:
        """
        对比两次运行，检测回归

        回归判定：加权分数下降超过阈值，或由通过变为失败。

        :param baseline_run_id: 基线运行 ID
        :param current_run_id: 当前运行 ID
        :param score_drop_threshold: 分数下降阈值
        :return: 对比结果（含回归用例列表）
        """
        baseline = {r["case_id"]: r for r in self.get_results(baseline_run_id)}
        current = {r["case_id"]: r for r in self.get_results(current_run_id)}

        regressions: list[dict[str, Any]] = []
        improvements: list[dict[str, Any]] = []

        for case_id, current_row in current.items():
            baseline_row = baseline.get(case_id)
            if baseline_row is None:
                continue

            score_delta = current_row["weighted_score"] - baseline_row["weighted_score"]
            flipped_to_fail = bool(baseline_row["passed"]) and not bool(current_row["passed"])

            if flipped_to_fail or score_delta <= -score_drop_threshold:
                regressions.append(
                    {
                        "case_id": case_id,
                        "baseline_score": baseline_row["weighted_score"],
                        "current_score": current_row["weighted_score"],
                        "score_delta": round(score_delta, 3),
                        "flipped_to_fail": flipped_to_fail,
                        "failure_reason": current_row["failure_reason"],
                    }
                )
            elif score_delta >= score_drop_threshold or (
                not baseline_row["passed"] and current_row["passed"]
            ):
                improvements.append(
                    {
                        "case_id": case_id,
                        "baseline_score": baseline_row["weighted_score"],
                        "current_score": current_row["weighted_score"],
                        "score_delta": round(score_delta, 3),
                    }
                )

        baseline_run = self.get_run(baseline_run_id) or {}
        current_run = self.get_run(current_run_id) or {}

        return {
            "baseline_run_id": baseline_run_id,
            "current_run_id": current_run_id,
            "baseline_pass_rate": baseline_run.get("passed", 0)
            / max(baseline_run.get("total", 1), 1),
            "current_pass_rate": current_run.get("passed", 0) / max(current_run.get("total", 1), 1),
            "regressions": regressions,
            "improvements": improvements,
            "has_regression": bool(regressions),
        }


def json_dumps_safe(payload: Any) -> str:
    """安全 JSON 序列化（失败时返回空对象）"""
    import json

    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"
