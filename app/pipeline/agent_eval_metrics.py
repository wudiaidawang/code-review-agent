"""Agent 评测指标 — 5 项指标衡量 InvestigationAgent 的真实表现。

指标：
1. 任务完成率 — 答案含预期关键词 + 引用了预期文件（LLM-free 规则判定）
2. 证据可追溯率 — 答案含 file:line 格式的引用
3. 平均工具步数 — 按问题类型分组的平均工具步数
4. 预算超限率 — 达到预算上限的样本占比
5. 续问效率 — 分别报告相对成本、非加权节省率和按工具步数加权的节省率
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field


_BUDGET_TYPES = {"steps", "files", "tokens"}


def detect_budget_exceeded(record: dict) -> tuple[bool, str | None]:
    """从 trace、StepRecord 或最终状态统一判断预算耗尽。

    返回 (budget_exceeded, budget_type)。三种证据源任一命中即为 True；
    类型优先使用结构化 StepRecord/final state，缺失时才解析 trace。
    """
    for step in record.get("steps", []):
        if step.get("decision") == "BUDGET":
            reason = step.get("budget_reason")
            return True, reason if reason in _BUDGET_TYPES else None
    for state_key in ("final_state", "state"):
        state = record.get(state_key) or {}
        if state.get("status") == "BUDGET":
            reason = state.get("budget_type") or state.get("budget_reason")
            return True, reason if reason in _BUDGET_TYPES else None
    for trace in record.get("trace", []):
        match = re.search(r"budget_exhausted:\s*(steps|files|tokens)", trace)
        if match:
            return True, match.group(1)
    return False, None


@dataclass
class AgentEvalMetrics:
    """Investigation Agent 5 项评测指标。"""

    total_samples: int = 0
    total_initial: int = 0
    total_follow_up: int = 0

    task_completion_rate: float = 0.0
    strict_completion_rate: float = 0.0
    evidence_retrieval_rate: float = 0.0
    citation_grounded_rate: float = 0.0
    evidence_traceability_rate: float = 0.0
    avg_tool_steps: dict = field(default_factory=dict)
    overall_avg_tool_steps: float = 0.0
    budget_overrun_rate: float = 0.0
    budget_overrun_by_type: dict = field(default_factory=dict)
    # 保留该字段作为非加权节省率，避免将 follow_up/initial 的成本比误称为节省率。
    follow_up_savings_rate: float = 0.0
    follow_up_relative_cost: float = 0.0
    follow_up_weighted_savings_rate: float = 0.0
    follow_up_per_sample: list[dict] = field(default_factory=list)

    per_sample: list[dict] = field(default_factory=list)

    @staticmethod
    def compute(records: list[dict]) -> "AgentEvalMetrics":
        total = len(records)
        if total == 0:
            return AgentEvalMetrics()

        initial_records = [r for r in records if not r.get("is_follow_up", False)]
        follow_up_records = [r for r in records if r.get("is_follow_up", False)]

        # 1. 任务完成率
        completed = sum(1 for r in records if _judge_completion(r))
        completion_rate = completed / total if total > 0 else 0.0

        evidence_retrieval_rate = sum(
            1 for r in records if _expected_file_retrieved(r)
        ) / total
        citation_grounded_rate = sum(
            1 for r in records if _citations_grounded(r)
        ) / total

        # 2. 证据可追溯率
        traceable = sum(1 for r in records if _has_evidence_citations(r))
        traceability_rate = traceable / total if total > 0 else 0.0

        # 3. 平均工具步数（按 question_type 分组，仅首次调查）
        qtype_steps = defaultdict(list)
        for r in initial_records:
            qtype = r.get("question_type", "unknown")
            qtype_steps[qtype].append(r["step_count"])
        avg_steps = {
            qt: round(sum(v) / len(v), 2) for qt, v in qtype_steps.items()
        }
        overall_avg = (
            round(sum(r["step_count"] for r in initial_records) / len(initial_records), 2)
            if initial_records else 0.0
        )

        # 4. 预算超限率
        budget_flags = [detect_budget_exceeded(r) for r in records]
        overrun_count = sum(exceeded for exceeded, _ in budget_flags)
        overrun_rate = overrun_count / total if total > 0 else 0.0
        overrun_by_type = defaultdict(int)
        for exceeded, reason in budget_flags:
            if exceeded:
                overrun_by_type[reason or "unknown"] += 1

        # 5. 续问效率。relative_cost = follow_up / initial；saving = 1 - cost。
        fu_savings = []
        groups = defaultdict(list)
        for r in records:
            gid = r.get("follow_up_group", "")
            if gid:
                groups[gid].append(r)
        for gid, chain in groups.items():
            chain.sort(key=lambda x: x.get("is_follow_up", False))
            if len(chain) >= 2:
                initial_steps = chain[0]["step_count"]
                for cr in chain[1:]:
                    fu_steps = cr["step_count"]
                    ratio = round(fu_steps / initial_steps, 4) if initial_steps > 0 else 1.0
                    fu_savings.append({
                        "group_id": gid,
                        "initial_steps": initial_steps,
                        "follow_up_steps": fu_steps,
                        "relative_cost": ratio,
                        "savings_rate": round(1 - ratio, 4),
                    })
        relative_cost = (
            round(sum(f["relative_cost"] for f in fu_savings) / len(fu_savings), 4)
            if fu_savings else 0.0
        )
        savings_rate = round(1 - relative_cost, 4) if fu_savings else 0.0
        total_initial_steps = sum(f["initial_steps"] for f in fu_savings)
        total_follow_up_steps = sum(f["follow_up_steps"] for f in fu_savings)
        weighted_savings_rate = (
            round(1 - (total_follow_up_steps / total_initial_steps), 4)
            if total_initial_steps else 0.0
        )

        return AgentEvalMetrics(
            total_samples=total,
            total_initial=len(initial_records),
            total_follow_up=len(follow_up_records),
            task_completion_rate=round(completion_rate, 4),
            strict_completion_rate=round(completion_rate, 4),
            evidence_retrieval_rate=round(evidence_retrieval_rate, 4),
            citation_grounded_rate=round(citation_grounded_rate, 4),
            evidence_traceability_rate=round(traceability_rate, 4),
            avg_tool_steps=avg_steps,
            overall_avg_tool_steps=overall_avg,
            budget_overrun_rate=round(overrun_rate, 4),
            budget_overrun_by_type=dict(overrun_by_type),
            follow_up_savings_rate=savings_rate,
            follow_up_relative_cost=relative_cost,
            follow_up_weighted_savings_rate=weighted_savings_rate,
            follow_up_per_sample=fu_savings,
            per_sample=records,
        )

    def to_dict(self) -> dict:
        return {
            "total_samples": self.total_samples,
            "total_initial": self.total_initial,
            "total_follow_up": self.total_follow_up,
            "task_completion_rate": self.task_completion_rate,
            "strict_completion_rate": self.strict_completion_rate,
            "evidence_retrieval_rate": self.evidence_retrieval_rate,
            "citation_grounded_rate": self.citation_grounded_rate,
            "evidence_traceability_rate": self.evidence_traceability_rate,
            "avg_tool_steps": self.avg_tool_steps,
            "overall_avg_tool_steps": self.overall_avg_tool_steps,
            "budget_overrun_rate": self.budget_overrun_rate,
            "budget_overrun_by_type": self.budget_overrun_by_type,
            "follow_up_savings_rate": self.follow_up_savings_rate,
            "follow_up_relative_cost": self.follow_up_relative_cost,
            "follow_up_weighted_savings_rate": self.follow_up_weighted_savings_rate,
            "follow_up_per_sample": self.follow_up_per_sample,
        }

    def summary(self) -> str:
        """生成 Markdown 格式的评测摘要。"""
        lines = [
            "# Agent 评测报告",
            "",
            f"**样本总数**: {self.total_samples}（首次 {self.total_initial} + 续问 {self.total_follow_up}）",
            "",
            "## 核心指标",
            "",
            f"| 指标 | 值 |",
            f"|------|-----|",
            f"| 严格完成率（关键词 + 预期文件） | {self.strict_completion_rate:.1%} |",
            f"| 证据检索率（预期文件） | {self.evidence_retrieval_rate:.1%} |",
            f"| 引用扎根率（回答引用可回链 Evidence） | {self.citation_grounded_rate:.1%} |",
            f"| 证据可追溯率 | {self.evidence_traceability_rate:.1%} |",
            f"| 平均工具步数（总体） | {self.overall_avg_tool_steps} |",
            f"| 预算超限率 | {self.budget_overrun_rate:.1%} |",
            f"| 续问相对成本（越低越好） | {self.follow_up_relative_cost:.2%} |",
            f"| 续问节省率（非加权） | {self.follow_up_savings_rate:.2%} |",
            f"| 续问节省率（按步骤加权） | {self.follow_up_weighted_savings_rate:.2%} |",
            "",
            "## 按问题类型的平均工具步数",
            "",
        ]
        if self.avg_tool_steps:
            for qt in ["locate", "explain", "trace", "impact", "grep"]:
                if qt in self.avg_tool_steps:
                    lines.append(f"- **{qt}**: {self.avg_tool_steps[qt]} 步")
        else:
            lines.append("(无数据)")

        if self.budget_overrun_by_type:
            lines.append("")
            lines.append("## 预算超限明细")
            lines.append("")
            for reason, count in self.budget_overrun_by_type.items():
                lines.append(f"- **{reason}**: {count} 次")

        if self.follow_up_per_sample:
            lines.append("")
            lines.append("## 续问节省明细")
            lines.append("")
            for fu in self.follow_up_per_sample:
                lines.append(
                    f"- **{fu['group_id']}**: 首次 {fu['initial_steps']} 步 → "
                    f"续问 {fu['follow_up_steps']} 步 (相对成本 {fu['relative_cost']:.1%}，"
                    f"节省率 {fu['savings_rate']:.1%})"
                )

        return "\n".join(lines)


def _judge_completion(record: dict) -> bool:
    """规则判定：answer 包含预期关键词 + 引用了预期文件。"""
    answer = (record.get("answer") or "").lower()
    expected_kw = record.get("expected_answer_keywords", [])
    evidence_files = set()
    for ev in record.get("evidence", []):
        loc = ev.get("location", {}) or {}
        f = loc.get("file", "")
        if f:
            evidence_files.add(f)

    kw_ok = all(kw.lower() in answer for kw in expected_kw) if expected_kw else False
    expected_files = set(record.get("expected_evidence_files", []))
    file_ok = bool(expected_files & evidence_files) if expected_files else True

    return kw_ok and file_ok


def _has_evidence_citations(record: dict) -> bool:
    """判定答案中是否包含文件路径+行号引用。"""
    answer = record.get("answer", "")
    patterns = [
        r'[\w/]+\.\w+:\d+',            # file.py:123
        r'[\w/]+\.\w+.*?line\s*\d+',   # file.py at line 123
    ]
    return any(re.search(p, answer) for p in patterns)


def _expected_file_retrieved(record: dict) -> bool:
    expected = set(record.get("expected_evidence_files", []))
    if not expected:
        return True
    actual = {
        (e.get("location") or {}).get("file", "") for e in record.get("evidence", [])
    }
    return bool(expected & actual)


def _citations_grounded(record: dict) -> bool:
    """回答中每个 file:line 引用必须能回链到同文件的 Agent Evidence。"""
    answer = record.get("answer", "")
    citations = re.findall(r"([\w./-]+\.[\w]+):(\d+)", answer)
    if not citations:
        return False
    evidence = {
        ((e.get("location") or {}).get("file", ""), str((e.get("location") or {}).get("start_line", "")))
        for e in record.get("evidence", [])
    }
    return all((file, line) in evidence for file, line in citations)
