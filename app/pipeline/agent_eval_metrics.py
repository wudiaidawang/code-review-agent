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


def _norm_path(p: str) -> str:
    """归一化文件路径，统一使用正斜杠，用于跨平台比较。"""
    return p.replace("\\", "/")


_BUDGET_TYPES = {"steps", "files", "tokens"}


def detect_budget_exceeded(record: dict) -> tuple[bool, str | None]:
    """从 trace、StepRecord 或最终状态统一判断预算耗尽。

    返回 (budget_exceeded, budget_type)。三种证据源任一命中即为 True；
    类型优先使用结构化 StepRecord/final state，缺失时才解析 trace。
    """
    for step in record.get("steps", []):
        decision = step.get("decision")
        if decision == "BUDGET" or decision in {"STOP_STEP_LIMIT", "STOP_FILE_LIMIT", "STOP_TOKEN_LIMIT"}:
            reason = step.get("budget_reason")
            if not reason and decision.startswith("STOP_"):
                reason = {"STOP_STEP_LIMIT": "steps", "STOP_FILE_LIMIT": "files",
                          "STOP_TOKEN_LIMIT": "tokens"}[decision]
            return True, reason if reason in _BUDGET_TYPES else None
        if step.get("status") == "budget_exhausted":
            phase = step.get("action", "")
            return True, phase or "unknown"
    for state_key in ("final_state", "state"):
        state = record.get(state_key) or {}
        status = state.get("status")
        if status == "BUDGET" or status in {"STOP_STEP_LIMIT", "STOP_FILE_LIMIT", "STOP_TOKEN_LIMIT"}:
            reason = state.get("budget_type") or state.get("budget_reason")
            if not reason and status.startswith("STOP_"):
                reason = {"STOP_STEP_LIMIT": "steps", "STOP_FILE_LIMIT": "files",
                          "STOP_TOKEN_LIMIT": "tokens"}[status]
            return True, reason if reason in _BUDGET_TYPES else None
    for trace_item in record.get("trace", []):
        match = re.search(r"budget_exhausted:\s*(steps|files|tokens)", trace_item)
        if match:
            return True, match.group(1)
        # 检测分阶段预算耗尽
        phase_match = re.search(r"budget: (\w+) exhausted", trace_item)
        if phase_match:
            return True, phase_match.group(1)
    return False, None


def detect_phase_budget_exhaustion(record: dict) -> dict[str, bool]:
    """检测各阶段预算是否耗尽。返回 {phase: exhausted} dict。"""
    phases = {"MAIN": False, "GAP": False, "RETOOL": False}
    for step in record.get("steps", []):
        if step.get("status") == "budget_exhausted":
            phase = step.get("action", "")
            if phase in phases:
                phases[phase] = True
    for trace_item in record.get("trace", []):
        match = re.search(r"budget: (\w+) exhausted", trace_item)
        if match and match.group(1) in phases:
            phases[match.group(1)] = True
    return phases


@dataclass
class AgentEvalMetrics:
    """Investigation Agent 评测指标。"""

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
    budget_exhaustion_rate: float = 0.0
    budget_exhaustion_by_phase: dict = field(default_factory=dict)
    # 续问诊断值（质量约束的节省率在 post-Judge 阶段计算）
    follow_up_relative_cost: float = 0.0
    follow_up_per_sample: list[dict] = field(default_factory=list)

    # LLM Judge 语义评测指标
    semantic_completion_rate: float = 0.0
    semantic_partial_rate: float = 0.0
    semantic_incorrect_rate: float = 0.0
    semantic_unjudgeable_rate: float = 0.0
    semantic_any_correct_rate: float = 0.0
    keyword_mismatch_semantic_correct: int = 0
    judge_available_count: int = 0

    # StepStatus 分布指标
    no_evidence_rate: float = 0.0
    no_progress_rate: float = 0.0
    tool_error_rate: float = 0.0
    action_rejected_rate: float = 0.0
    fallback_recovery_rate: float = 0.0
    step_status_distribution: dict = field(default_factory=dict)

    # V23 Claims 指标
    avg_claim_coverage_rate: float = 0.0

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

        # 4b. 分阶段预算耗尽率
        phase_exhaustion: dict[str, int] = defaultdict(int)
        any_exhaustion_count = 0
        for r in records:
            phases = detect_phase_budget_exhaustion(r)
            exhausted_any = False
            for phase, exhausted in phases.items():
                if exhausted:
                    phase_exhaustion[phase] += 1
                    exhausted_any = True
            if exhausted_any:
                any_exhaustion_count += 1
        exhaustion_rate = any_exhaustion_count / total if total else 0.0
        exhaustion_by_phase = {
            phase: round(count / total, 4) if total else 0.0
            for phase, count in phase_exhaustion.items()
        }

        # 5. 续问诊断值（仅相对成本，不含节省率——节省率在 post-Judge 计算）
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
                    })
        relative_cost = (
            round(sum(f["relative_cost"] for f in fu_savings) / len(fu_savings), 4)
            if fu_savings else 0.0
        )

        # 6. LLM Judge 语义评测
        judge_available = [r for r in records
                           if r.get("llm_judge") and r["llm_judge"].get("judge_error_type") is None]
        judge_total = len(judge_available) or 1
        sem_complete = sum(1 for r in judge_available if r["llm_judge"]["verdict"] == "correct")
        sem_partial = sum(1 for r in judge_available if r["llm_judge"]["verdict"] == "partially_correct")
        sem_incorrect = sum(1 for r in judge_available if r["llm_judge"]["verdict"] == "incorrect")
        sem_unjudgeable = sum(1 for r in judge_available if r["llm_judge"]["verdict"] == "unjudgeable")

        # 规则判错但 LLM 判对（诊断 Agent 能力 vs Judge 误判）
        kw_mismatch = 0
        for r in records:
            rule_ok = _judge_completion(r)
            llm_j = r.get("llm_judge", {}) or {}
            if not rule_ok and llm_j.get("judge_error_type") is None:
                if llm_j.get("verdict") in ("correct", "partially_correct"):
                    kw_mismatch += 1

        # 7. StepStatus 分布
        all_statuses: list[str] = []
        fallback_count = 0
        fallback_success_count = 0
        for r in records:
            prev_no_evidence = False
            for step in r.get("steps", []):
                all_statuses.append(step.get("status", ""))
            for trace_item in r.get("trace", []):
                if "fallback:" in trace_item:
                    fallback_count += 1
            # 检查 fallback 后是否有后续证据产出（简化：trace 中有 fallback 且最终有 evidence）
            has_fallback = any("fallback:" in t for t in r.get("trace", []))
            if has_fallback and r.get("evidence"):
                fallback_success_count += 1

        total_steps = len(all_statuses) or 1
        status_counts: dict[str, int] = defaultdict(int)
        for s in all_statuses:
            status_counts[s] += 1

        # 8. Claims 覆盖率
        claim_rates = [r.get("claim_coverage_rate", 0.0) for r in records
                       if r.get("required_claims")]
        avg_claim_coverage = (
            round(sum(claim_rates) / len(claim_rates), 4)
            if claim_rates else 0.0
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
            budget_exhaustion_rate=round(exhaustion_rate, 4),
            budget_exhaustion_by_phase=exhaustion_by_phase,
            follow_up_relative_cost=relative_cost,
            follow_up_per_sample=fu_savings,
            semantic_completion_rate=round(sem_complete / judge_total, 4),
            semantic_partial_rate=round(sem_partial / judge_total, 4),
            semantic_incorrect_rate=round(sem_incorrect / judge_total, 4),
            semantic_unjudgeable_rate=round(sem_unjudgeable / judge_total, 4),
            semantic_any_correct_rate=round((sem_complete + sem_partial) / judge_total, 4),
            keyword_mismatch_semantic_correct=kw_mismatch,
            judge_available_count=len(judge_available),
            no_evidence_rate=round(status_counts.get("no_evidence", 0) / total_steps, 4),
            no_progress_rate=round(status_counts.get("no_progress", 0) / total_steps, 4),
            tool_error_rate=round(status_counts.get("tool_error", 0) / total_steps, 4),
            action_rejected_rate=round(status_counts.get("action_rejected", 0) / total_steps, 4),
            fallback_recovery_rate=round(
                fallback_success_count / max(fallback_count, 1), 4
            ) if fallback_count > 0 else 0.0,
            step_status_distribution=dict(status_counts),
            avg_claim_coverage_rate=avg_claim_coverage,
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
            "budget_exhaustion_rate": self.budget_exhaustion_rate,
            "budget_exhaustion_by_phase": self.budget_exhaustion_by_phase,
            "follow_up_relative_cost": self.follow_up_relative_cost,
            "follow_up_per_sample": self.follow_up_per_sample,
            "semantic_completion_rate": self.semantic_completion_rate,
            "semantic_partial_rate": self.semantic_partial_rate,
            "semantic_incorrect_rate": self.semantic_incorrect_rate,
            "semantic_unjudgeable_rate": self.semantic_unjudgeable_rate,
            "semantic_any_correct_rate": self.semantic_any_correct_rate,
            "keyword_mismatch_semantic_correct": self.keyword_mismatch_semantic_correct,
            "judge_available_count": self.judge_available_count,
            "no_evidence_rate": self.no_evidence_rate,
            "no_progress_rate": self.no_progress_rate,
            "tool_error_rate": self.tool_error_rate,
            "action_rejected_rate": self.action_rejected_rate,
            "fallback_recovery_rate": self.fallback_recovery_rate,
            "step_status_distribution": self.step_status_distribution,
            "avg_claim_coverage_rate": self.avg_claim_coverage_rate,
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
            f"| 分阶段预算耗尽率（任一阶段） | {self.budget_exhaustion_rate:.1%} |",
            f"| 续问相对成本（越低越好） | {self.follow_up_relative_cost:.2%} |",
            f"| 平均 Claims 覆盖率（内容级） | {self.avg_claim_coverage_rate:.1%} |",
            "",
            "## LLM Judge 语义评测",
            "",
            f"| 指标 | 值 |",
            f"|------|-----|",
            f"| 语义完成率（verdict=correct） | {self.semantic_completion_rate:.1%} |",
            f"| 语义部分正确率（verdict=partially_correct） | {self.semantic_partial_rate:.1%} |",
            f"| 语义任意正确率（correct + partially_correct） | {self.semantic_any_correct_rate:.1%} |",
            f"| 语义错误率（verdict=incorrect） | {self.semantic_incorrect_rate:.1%} |",
            f"| 不可评判率（verdict=unjudgeable） | {self.semantic_unjudgeable_rate:.1%} |",
            f"| Judge 有效样本数 | {self.judge_available_count} |",
            "",
            "## 诊断：规则误判（关键字不匹配但语义正确）",
            "",
            f"规则判错但 LLM Judge 判对的样本数: **{self.keyword_mismatch_semantic_correct}**",
            "",
            "## StepStatus 分布",
            "",
            f"| 状态 | 占比 |",
            f"|------|------|",
            f"| no_evidence | {self.no_evidence_rate:.1%} |",
            f"| no_progress | {self.no_progress_rate:.1%} |",
            f"| tool_error | {self.tool_error_rate:.1%} |",
            f"| action_rejected | {self.action_rejected_rate:.1%} |",
            f"| fallback_recovery | {self.fallback_recovery_rate:.1%} |",
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

        if self.budget_exhaustion_by_phase:
            lines.append("")
            lines.append("## 分阶段预算耗尽率")
            lines.append("")
            for phase, rate in self.budget_exhaustion_by_phase.items():
                lines.append(f"- **{phase}**: {rate:.1%}")

        if self.follow_up_per_sample:
            lines.append("")
            lines.append("## 续问诊断")
            lines.append("")
            for fu in self.follow_up_per_sample:
                lines.append(
                    f"- **{fu['group_id']}**: 首次 {fu['initial_steps']} 步 → "
                    f"续问 {fu['follow_up_steps']} 步 (相对成本 {fu['relative_cost']:.1%})"
                )

        return "\n".join(lines)


def _judge_completion(record: dict) -> bool:
    """规则判定：answer 包含预期关键词 + 引用了预期文件。"""
    answer = _norm_path((record.get("answer") or "").lower())
    expected_kw = record.get("expected_answer_keywords", [])
    evidence_files = set()
    for ev in record.get("evidence", []):
        loc = ev.get("location", {}) or {}
        f = _norm_path(loc.get("file", ""))
        if f:
            evidence_files.add(f)

    kw_ok = all(kw.lower() in answer for kw in expected_kw) if expected_kw else False
    expected_files = {_norm_path(f) for f in record.get("expected_evidence_files", [])}
    file_ok = bool(expected_files & evidence_files) if expected_files else True

    return kw_ok and file_ok


def _has_evidence_citations(record: dict) -> bool:
    """判定答案中是否包含文件路径+行号引用。"""
    answer = _norm_path(record.get("answer", ""))
    patterns = [
        r'[\w./-]+\.\w+:\d+',            # file.py:123
        r'[\w./-]+\.\w+.*?line\s*\d+',   # file.py at line 123
    ]
    return any(re.search(p, answer, re.ASCII) for p in patterns)


def _expected_file_retrieved(record: dict) -> bool:
    expected = {_norm_path(f) for f in record.get("expected_evidence_files", [])}
    if not expected:
        return True
    actual = {
        _norm_path((e.get("location") or {}).get("file", "")) for e in record.get("evidence", [])
    }
    return bool(expected & actual)


def _citations_grounded(record: dict) -> bool:
    """回答中每个 file:line 引用必须能回链到同文件的 Agent Evidence。"""
    answer = _norm_path(record.get("answer", ""))
    citations = re.findall(r"([\w./-]+\.[\w]+):(\d+)", answer, re.ASCII)
    if not citations:
        return False
    evidence = {
        (_norm_path((e.get("location") or {}).get("file", "")), str((e.get("location") or {}).get("start_line", "")))
        for e in record.get("evidence", [])
    }
    return all((file, line) in evidence for file, line in citations)
