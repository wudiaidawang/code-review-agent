"""V1.1 Investigation Agent — V22 Task-driven 6-phase exploration.

V22: EvidenceClosureEngine replaced by task_explorer.py 全局优先队列探查.
Six phases: Query→LLM decompose→Global queue→Contract check→Gap fill→Retool→Synthesis.
"""

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field

from app.models.evidence import Evidence
from app.models.location import CodeLocation
from app.models.target import (
    TargetSpec, MissingRequirement, SuggestedAction, SufficiencyJudgment,
    ClaimCitation, Requirement, StepStatus,
    InvestigationTask, WorkOrder, StateDecision,
    TaskRole, TaskStatus, GapStrategy,
)
from app.core.workspace import WorkspaceManager
from app.tools.llm_tool import chat
from app.tools.search_tool import SearchTool
from app.tools.ast_tool import ASTTool
from app.tools.dependency_tool import DependencyTool
from app.tools.git_tool import GitTool
from app.tools.contract import ToolRequest
from app.pipeline.knowledge_retriever import StaticKnowledge
from app.agent.evidence_closure import (
    SlotKind, targets_from_tasks, check_minimum_evidence_contract,
    AnswerTarget,
)
from app.agent.task_explorer import (
    ExplorationState, ToolExecutor,
    fill_work_orders, _deterministic_work_orders,
    discover_tasks, gap_analyzer, _deterministic_gap_fill,
    _execute_task, _execute_task_subtree, _task_status,
    _build_retool_task, _determine_stop_reason,
    MAX_ORDERS_PER_TASK,
)

# ── 常量 ──────────────────────────────────────────────────────────

_KEYWORD_STOP_WORDS = frozenset({
    "the", "is", "are", "where", "what", "how", "does", "do", "in",
    "of", "and", "or", "not", "for", "with", "from", "that", "this",
    "why", "when", "which", "all", "any", "used", "use", "defined",
    "definition", "code", "function", "class", "method", "file",
})
_GENERIC_SEARCH_TERMS = frozenset({
    "app", "application", "python", "true", "false", "none", "null",
    "config", "configuration", "module", "package", "project", "system",
})

_LLM_CALL_KWARGS = {"timeout": 60, "extra_body": {"thinking": {"type": "disabled"}}}

_LOW_PRIORITY_CONTEXT_DIRS = frozenset({
    "docs", "doc", "docs_src", "examples", "example",
    "tests", "test", "benchmarks", "scripts",
})

_SLOT_CN: dict[SlotKind, str] = {
    SlotKind.DEFINITION: "定义位置",
    SlotKind.IMPLEMENTATION: "完整实现体",
    SlotKind.CANDIDATE_REFERENCE: "候选引用",
    SlotKind.VERIFIED_CALLER_EDGE: "已验证调用者",
    SlotKind.VERIFIED_CALLEE_EDGE: "已验证被调用者",
    SlotKind.HELPER_IMPLEMENTATION: "关键辅助函数实现",
    SlotKind.NEGATIVE_SEARCH: "已确认不存在",
}

_HYPOTHESIS_TEMPLATES = {
    "locate":  "符号 {kw} 定义在某个 .py 文件中",
    "explain": "符号 {kw} 是一个函数/类，其作用可通过代码和 AST 推断",
    "trace":   "符号 {kw} 被其他函数调用，也调用了其他函数",
    "impact":  "修改符号 {kw} 会影响其调用者和被调用者",
    "grep":    "仓库中有若干处使用了模式 {kw}",
}

_TOOL_PRIORITY = {
    "locate":  ["search", "search_filename", "resolve_symbol", "python_ast"],
    "explain": ["search", "search_filename", "resolve_symbol", "python_ast", "dependency", "knowledge"],
    "trace":   ["search", "search_filename", "resolve_symbol", "python_ast", "dependency", "git"],
    "impact":  ["search", "search_filename", "resolve_symbol", "python_ast", "dependency", "git"],
    "grep":    ["search", "search_filename"],
}

_DEDUP_KEYS = {
    "search":       lambda p: ("query", "search_type"),
    "search_filename": lambda p: ("query", "search_type"),
    "python_ast":   lambda p: ("files",),
    "dependency":   lambda p: ("files",),
    "git":          lambda p: ("base_ref", "head_ref"),
    "knowledge":    lambda p: ("query",),
}


# ── 辅助函数 ──────────────────────────────────────────────────────

def _get_repo_commit(repo_path: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path, text=True, encoding="utf-8",
        ).strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "HEAD"


def _normalize_search_keyword(value: str) -> TargetSpec | None:
    candidate = value.strip().strip("`'\"()[]{} ")
    if not candidate:
        return None
    if "::" in candidate:
        parts = candidate.rsplit("::", 1)
    elif "." in candidate:
        parts = candidate.rsplit(".", 1)
    else:
        parts = [candidate]
    if len(parts) == 2:
        owner, member = parts[0].strip(), parts[1].strip()
    else:
        owner, member = "", parts[0].strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", member):
        return None
    if member.lower() in _KEYWORD_STOP_WORDS | _GENERIC_SEARCH_TERMS:
        return None
    if member[0].isupper():
        kind = "class"
    elif "_" in member:
        kind = "variable"
    else:
        kind = "method"
    return TargetSpec(
        qualified_symbol=candidate if (owner or "." in candidate) else member,
        owner_symbol=owner or member,
        member_symbol=member,
        symbol_kind=kind,
    )


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class StepRecord:
    step: int
    tool: str
    params: dict = field(default_factory=dict)
    status: str = "success"
    evidence_count: int = 0
    hypothesis_before: str = ""
    hypothesis_after: str = ""
    decision: str = ""
    budget_reason: str = ""
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "step": self.step, "tool": self.tool, "params": self.params,
            "status": self.status, "evidence_count": self.evidence_count,
            "hypothesis_before": self.hypothesis_before,
            "hypothesis_after": self.hypothesis_after,
            "decision": self.decision, "budget_reason": self.budget_reason,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass(frozen=True)
class ActionCandidate:
    """向后兼容：V15-V21 的行动候选类型。V22 task_explorer 不再使用此类。"""
    key: str
    gap: str
    tool: str
    target: str
    expected_evidence: str
    params: dict = field(default_factory=dict)
    depth: int = 1
    value: int = 0

    def to_dict(self) -> dict:
        return {
            "key": self.key, "gap": self.gap, "tool": self.tool,
            "target": self.target, "expected_evidence": self.expected_evidence,
            "params": dict(self.params), "depth": self.depth, "value": self.value,
        }


@dataclass
class InvestigationResult:
    question: str
    answer: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    files_visited: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    plan: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    investigation_id: str = ""
    is_follow_up: bool = False
    reused_evidence_refs: list[str] = field(default_factory=list)
    claims: list[ClaimCitation] = field(default_factory=list)
    duration_ms: float = 0.0
    # ── V22 诊断字段 ──────────────────────────────────────────────
    planned_tasks: list[dict] = field(default_factory=list)
    all_tasks: list[dict] = field(default_factory=list)
    work_orders: list[dict] = field(default_factory=list)
    verified_evidence_summary: dict[str, list[str]] = field(default_factory=dict)
    candidate_evidence_count: dict[str, int] = field(default_factory=dict)
    required_slots: dict[str, list[str]] = field(default_factory=dict)
    closed_slots: dict[str, list[str]] = field(default_factory=dict)
    open_slots: dict[str, list[str]] = field(default_factory=dict)
    contract_met_before: bool = False
    contract_met_after: bool = False
    final_contract_met: bool = False
    stop_reason: str = ""
    retool_triggered: bool = False
    # ── V23 Claims 诊断 ──────────────────────────────────────────
    required_claims: list[str] = field(default_factory=list)
    covered_claims: list[int] = field(default_factory=list)
    uncovered_claims: list[int] = field(default_factory=list)
    claim_coverage_rate: float = 0.0

    def to_dict(self) -> dict:
        d = {
            "question": self.question, "answer": self.answer,
            "evidence": [e.to_dict() for e in self.evidence],
            "files_visited": self.files_visited, "findings": self.findings,
            "plan": self.plan, "trace": self.trace, "steps": self.steps,
            "investigation_id": self.investigation_id,
            "is_follow_up": self.is_follow_up,
            "reused_evidence_refs": self.reused_evidence_refs,
            "claims": [{"text": c.text, "evidence_ids": c.evidence_ids} for c in self.claims],
            "duration_ms": round(self.duration_ms, 1),
            # V22 诊断
            "planned_tasks": self.planned_tasks,
            "all_tasks": self.all_tasks,
            "work_orders": self.work_orders,
            "verified_evidence_summary": self.verified_evidence_summary,
            "candidate_evidence_count": self.candidate_evidence_count,
            "required_slots": self.required_slots,
            "closed_slots": self.closed_slots,
            "open_slots": self.open_slots,
            "contract_met_before": self.contract_met_before,
            "contract_met_after": self.contract_met_after,
            "final_contract_met": self.final_contract_met,
            "stop_reason": self.stop_reason,
            "retool_triggered": self.retool_triggered,
            # V23 Claims 诊断
            "required_claims": self.required_claims,
            "covered_claims": self.covered_claims,
            "uncovered_claims": self.uncovered_claims,
            "claim_coverage_rate": self.claim_coverage_rate,
        }
        return d


class InvestigationStore:
    """调查会话持久化存储."""

    def __init__(self):
        self._sessions: dict[str, dict] = {}

    def save(self, investigation_id: str, state: ExplorationState) -> None:
        self._sessions[investigation_id] = {
            "question": state.question,
            "question_type": state.question_type,
            "evidence": [
                ev.to_dict() for ev_list in state.all_evidence.values()
                for ev in [ev_list] if hasattr(ev, 'to_dict')
            ],
            "steps": list(state.steps),
            "trace": list(state.traces),
            "all_evidence": {k: v.to_dict() for k, v in state.all_evidence.items()},
            "verified_evidence": {
                k: [ev.to_dict() for ev in v]
                for k, v in state.verified_evidence.items()
            },
            "tasks": [t.to_dict() for t in state.all_tasks],
        }

    def load(self, investigation_id: str) -> dict | None:
        return self._sessions.get(investigation_id)

    def delete(self, investigation_id: str) -> None:
        self._sessions.pop(investigation_id, None)

    @property
    def session_count(self) -> int:
        return len(self._sessions)


# ═══════════════════════════════════════════════════════════════════
# InvestigationAgent — V22
# ═══════════════════════════════════════════════════════════════════

class InvestigationAgent:
    """V22: Task-driven 6-phase exploration with global priority queue."""

    def __init__(self, call_llm=None, store: InvestigationStore | None = None):
        self.call_llm = call_llm or chat
        self.store = store or InvestigationStore()

    # ── 主入口 ──────────────────────────────────────────────────────

    def investigate(self, repo_path: str, question: str) -> InvestigationResult:
        """V22 6-phase main flow.

        Phase 1: LLM decompose → InvestigationTask list
        Phase 2: Global priority-queue exploration
        Phase 3: Rule-based contract check (verified evidence only)
        Phase 4: Deterministic gap fill (if contract not met)
        Phase 5: LLM one-shot retool (if contract met, gaps remain)
        Phase 6: Synthesis (verified evidence only)
        """
        t0 = time.perf_counter()
        abs_path = os.path.abspath(repo_path)

        # ── Phase 1: LLM 分解任务（V24: relation 驱动）──────────────
        tasks, required_claims, planner_output = self._plan_question(question)
        if not tasks:
            return InvestigationResult(
                question=question,
                answer="无法将问题分解为调查任务，请提供更具体的问题。")

        state = ExplorationState(
            question=question,
            question_type=planner_output.question_type,
            repo_path=abs_path,
            repo_revision=_get_repo_commit(abs_path),
        )
        state.required_claims = required_claims
        state.planner_output = planner_output
        for t in tasks:
            t.role = TaskRole.ROOT
            t.subtree_depth = 0
        state.enqueue_tasks(tasks)

        tool_executor = ToolExecutor(abs_path)

        # ── Phase 2: 全局优先队列探查 ──────────────────────────────
        state.current_phase = "MAIN"
        while (state.has_pending()
               and state.main_steps_used < state.max_main_steps):
            task = state.pop_next()
            if task is None:
                break
            _execute_task(task, state, allow_children=True,
                          tool_executor=tool_executor)
        state.traces.append(
            f"phase2_done: main_steps={state.main_steps_used} "
            f"tasks={len(state.all_tasks)} pending={len(state.pending_tasks)}")

        # ── Phase 3: 规则判定（只用 verified evidence）─────────────
        contract_met, reason = self._judge_contract(state)
        state.contract_met_before = contract_met
        state.traces.append(f"phase3_contract: met={contract_met} reason={reason}")

        # ── Phase 4: 确定性补缺 ────────────────────────────────────
        if not contract_met:
            state.current_phase = "GAP"
            required_tasks = [t for t in state.all_tasks
                            if t.role in (TaskRole.ROOT, TaskRole.REQUIRED)]
            targets = targets_from_tasks(required_tasks, state.question_type)
            gap_tasks = _deterministic_gap_fill(targets, state)
            for gt in gap_tasks[:3]:
                gt.role = TaskRole.GAP
                gt.subtree_depth = 0
                state.register_task(gt)
                _execute_task_subtree(gt, state,
                                      max_depth=state.max_subtree_depth,
                                      budget_phase="GAP",
                                      tool_executor=tool_executor)
            contract_met, reason = self._judge_contract(state)
            state.contract_met_after_gap = contract_met
            state.traces.append(
                f"phase4_gap: gap_tasks={len(gap_tasks)} "
                f"met={contract_met} reason={reason}")

        # ── Phase 5: LLM 一次补缺 ──────────────────────────────────
        # LLM 仅能在规则已经跑完主队列和确定性补缺后，对现有证据缺口申请一次补充工具调用。
        # 不要把“合同已满足”当成件：它恰恰会让实际缺证时的 retool 永远不触发。
        # Draft first, then let the LLM audit a concrete answer against the
        # claims.  This grants one bounded planning request, not a free-form
        # recall/rewrite loop: the returned task is still schema-validated and
        # executed by the deterministic scheduler.
        state.stop_reason = _determine_stop_reason(state, contract_met)
        provisional_result = self._synthesize_v22(question, state, abs_path)
        state.traces.append("phase5_answer_audit: provisional draft created")

        # An answer audit may request one Task only for an explicit ledger
        # deficit.  Without this gate it became a near-mandatory extra tool
        # call even for fully closed locate answers.
        if (not state.retool_used and state.all_evidence
                and (not contract_met or state.uncovered_claims)):
            state.current_phase = "RETOOL"
            gap = gap_analyzer(
                question, state, call_llm=self.call_llm,
                draft_answer=provisional_result.answer,
            )
            if gap.get("action") == "add_one_task":
                retool_task = _build_retool_task(gap, state)
                retool_task.role = TaskRole.RETOOL
                retool_task.subtree_depth = 0
                state.retool_task = retool_task
                state.retool_used = True
                state.register_task(retool_task)
                _execute_task_subtree(retool_task, state,
                                      max_depth=state.max_subtree_depth,
                                      budget_phase="RETOOL",
                                      tool_executor=tool_executor)
                contract_met, reason = self._judge_contract(state)
                state.contract_met_after_retool = contract_met
                state.traces.append(
                    f"phase5_retool: met={contract_met} reason={reason}")
            else:
                state.traces.append(
                    f"phase5_answer_audit: no task action={gap.get('action')}")

        # ── Phase 6: 合成（只用 verified evidence）─────────────────
        state.stop_reason = _determine_stop_reason(state, contract_met)
        state.final_contract_met = contract_met
        state.traces.append(f"phase6_stop: {state.stop_reason}")

        result = (self._synthesize_v22(question, state, abs_path)
                  if state.retool_used else provisional_result)
        result.duration_ms = (time.perf_counter() - t0) * 1000
        result.trace = list(state.traces)
        result.steps = list(state.steps)
        result.plan = [s.get("tool", "") for s in state.steps]
        self._populate_v22_diagnostics(result, state)

        inv_id = self._new_investigation_id(question)
        result.investigation_id = inv_id
        self.store.save(inv_id, state)
        return result

    # ── 续问入口 ──────────────────────────────────────────────────

    def follow_up(self, repo_path: str, investigation_id: str,
                  question: str) -> InvestigationResult:
        """续问：加载已有证据 → 新问题分解 → 补缺 → 合成。"""
        t0 = time.perf_counter()
        abs_path = os.path.abspath(repo_path)

        session = self.store.load(investigation_id)
        if session is None:
            result = InvestigationResult(
                question=question, answer="会话不存在，无法续问。",
                investigation_id=investigation_id, is_follow_up=True)
            result.duration_ms = (time.perf_counter() - t0) * 1000
            return result

        tasks, required_claims, planner_output = self._plan_question(question)
        targets = targets_from_tasks(tasks, planner_output.question_type)
        if not targets:
            result = InvestigationResult(
                question=question,
                answer="无法确认：续问未能生成可验证的调查目标。",
                investigation_id=investigation_id, is_follow_up=True)
            result.duration_ms = (time.perf_counter() - t0) * 1000
            return result

        # 恢复已有证据
        existing_ev: dict[str, Evidence] = {}
        for ev_dict in session.get("evidence", []):
            try:
                ev = Evidence.from_dict(ev_dict)
                if ev.id:
                    existing_ev[ev.id] = ev
            except Exception:
                continue

        # 构建 state 并注入已有证据
        state = ExplorationState(
            question=question,
            question_type=planner_output.question_type,
            repo_path=abs_path,
            repo_revision=_get_repo_commit(abs_path),
        )
        state.required_claims = required_claims
        state.planner_output = planner_output
        for t in tasks:
            t.role = TaskRole.ROOT
            t.subtree_depth = 0
        state.enqueue_tasks(tasks)
        # 注入已有证据
        state.all_evidence = dict(existing_ev)
        for ev in existing_ev.values():
            state.add_verified_evidence(ev, "follow_up_reused")

        tool_executor = ToolExecutor(abs_path)

        # Phase 2: 探查
        state.current_phase = "MAIN"
        while (state.has_pending()
               and state.main_steps_used < state.max_main_steps):
            task = state.pop_next()
            if task is None:
                break
            _execute_task(task, state, allow_children=True,
                          tool_executor=tool_executor)

        # Phase 3-5: 合同判定 + 补缺
        contract_met, reason = self._judge_contract(state)
        if not contract_met:
            state.current_phase = "GAP"
            required_tasks = [t for t in state.all_tasks
                            if t.role in (TaskRole.ROOT, TaskRole.REQUIRED)]
            targets = targets_from_tasks(required_tasks, state.question_type)
            gap_tasks = _deterministic_gap_fill(targets, state)
            for gt in gap_tasks[:3]:
                gt.role = TaskRole.GAP
                gt.subtree_depth = 0
                state.register_task(gt)
                _execute_task_subtree(gt, state,
                                      max_depth=state.max_subtree_depth,
                                      budget_phase="GAP",
                                      tool_executor=tool_executor)
            contract_met, _ = self._judge_contract(state)

        # 先形成可审阅草稿。LLM 不是再次接管流程，而是仅可提交一张
        # schema-validated Task（调查任务）；规则执行它，然后才重新合成。
        state.stop_reason = _determine_stop_reason(state, contract_met)
        provisional_result = self._synthesize_v22(question, state, abs_path)
        state.traces.append("follow_up_answer_audit: provisional draft created")

        if (not state.retool_used and state.all_evidence
                and (not contract_met or state.uncovered_claims)):
            state.current_phase = "RETOOL"
            gap = gap_analyzer(
                question, state, call_llm=self.call_llm,
                draft_answer=provisional_result.answer,
            )
            if gap.get("action") == "add_one_task":
                retool_task = _build_retool_task(gap, state)
                retool_task.role = TaskRole.RETOOL
                retool_task.subtree_depth = 0
                state.retool_task = retool_task
                state.retool_used = True
                state.register_task(retool_task)
                _execute_task_subtree(retool_task, state,
                                      max_depth=state.max_subtree_depth,
                                      budget_phase="RETOOL",
                                      tool_executor=tool_executor)
                contract_met, reason = self._judge_contract(state)
                state.contract_met_after_retool = contract_met
                state.traces.append(
                    f"follow_up_retool: met={contract_met} reason={reason}")
            else:
                state.traces.append(
                    f"follow_up_answer_audit: no task action={gap.get('action')}")

        state.stop_reason = _determine_stop_reason(state, contract_met)
        state.final_contract_met = contract_met
        result = (self._synthesize_v22(question, state, abs_path)
                  if state.retool_used else provisional_result)
        result.investigation_id = investigation_id
        result.is_follow_up = True
        self._populate_v22_diagnostics(result, state)
        matched_refs = [
            ev.id for ev in existing_ev.values()
            if any(
                (ev.snippet or "").lower().find(
                    t.target.rsplit(".", 1)[-1].lower()) >= 0
                for t in tasks)
        ]
        result.reused_evidence_refs = matched_refs[:10]
        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    # ── Phase 1: 任务分解 ──────────────────────────────────────────

    def _plan_question(self, question: str) -> tuple[list[InvestigationTask], list[str], "PlannerOutput"]:
        """V24: 调用新版 query_planner → PlannerOutput → expand → tasks。"""
        from app.agent.query_planner import query_planner
        from app.agent.evidence_closure import tasks_from_planner_output
        planner_output = query_planner(question, call_llm=self.call_llm)
        tasks = tasks_from_planner_output(planner_output)
        return tasks, planner_output.required_claims, planner_output

    # ── Phase 3: 合同判定 ─────────────────────────────────────────

    @staticmethod
    def _populate_target_slots(state: ExplorationState,
                                targets: dict) -> None:
        """将 state.verified_evidence 按 source 分类填充到 targets 的 slot。"""
        required_tasks = [t for t in state.all_tasks
                         if t.role in (TaskRole.ROOT, TaskRole.REQUIRED,
                                       TaskRole.GAP, TaskRole.RETOOL)]
        for t in required_tasks:
            ev_list = state.verified_evidence.get(t.id, [])
            for ev in ev_list:
                for tid, target in targets.items():
                    if target.symbol not in t.target and t.target not in target.symbol:
                        continue
                    if ev.source == "resolve_symbol":
                        target.evidence_by_slot.setdefault(
                            SlotKind.DEFINITION, []).append(ev.id)
                        target.verified_slots.setdefault(
                            SlotKind.DEFINITION, []).append(ev.id)
                    elif ev.source == "read_window":
                        target.evidence_by_slot.setdefault(
                            SlotKind.IMPLEMENTATION, []).append(ev.id)
                        target.verified_slots.setdefault(
                            SlotKind.IMPLEMENTATION, []).append(ev.id)
                    elif ev.source == "search_references":
                        target.evidence_by_slot.setdefault(
                            SlotKind.CANDIDATE_REFERENCE, []).append(ev.id)
                        target.verified_slots.setdefault(
                            SlotKind.CANDIDATE_REFERENCE, []).append(ev.id)
                    elif ev.source == "verify_callsite":
                        target.verified_slots.setdefault(
                            SlotKind.VERIFIED_CALLER_EDGE, []).append(ev.id)
                    elif ev.source == "verify_callees":
                        target.verified_slots.setdefault(
                            SlotKind.VERIFIED_CALLEE_EDGE, []).append(ev.id)

    def _judge_contract(self, state: ExplorationState) -> tuple[bool, str]:
        """只用 verified evidence 判定合同（结构 + 内容 claims）。"""
        required_tasks = [t for t in state.all_tasks
                         if t.role in (TaskRole.ROOT, TaskRole.REQUIRED,
                                       TaskRole.GAP, TaskRole.RETOOL)]
        targets = targets_from_tasks(required_tasks, state.question_type)
        self._populate_target_slots(state, targets)
        # Run content coverage exactly once per verified-evidence snapshot.
        # The old path asked the LLM once inside the contract and again for
        # state diagnostics, allowing the two decisions to disagree.
        claim_coverage = None
        if state.required_claims:
            from app.agent.evidence_closure import check_claim_coverage
            fingerprint = tuple(sorted(state.all_evidence))
            if state.claim_coverage_evidence_ids != fingerprint:
                covered, uncovered, coverage_reason = check_claim_coverage(
                    state.required_claims, state.all_evidence, self.call_llm)
                state.covered_claims = covered
                state.uncovered_claims = uncovered
                state.claim_coverage_evidence_ids = fingerprint
            claim_coverage = (
                state.covered_claims, state.uncovered_claims,
                f"{len(state.covered_claims)}/{len(state.required_claims)} claims covered",
            )

        met, reason = check_minimum_evidence_contract(
            state.question_type, targets, state.all_evidence,
            required_claims=state.required_claims if state.required_claims else None,
            call_llm=self.call_llm,
            claim_coverage=claim_coverage,
        )
        return met, reason

    # ── Phase 6: 合成 ─────────────────────────────────────────────

    def _synthesize_v22(self, question: str, state: ExplorationState,
                        repo_path: str) -> InvestigationResult:
        """V24 合成：按 relation type 分发到专用合成方法。

        grep/enumerate 类型使用确定性全量输出，不依赖 LLM 挑选子集。
        """
        # V24: Relation-type-aware 分发
        planner_output = getattr(state, "planner_output", None)
        if planner_output and planner_output.relations:
            main_rel = planner_output.relations[0]
            from app.models.target import RelationType
            if main_rel.type == RelationType.COMPARE_BEHAVIOR:
                return self._synthesize_compare(question, state, repo_path)
            elif main_rel.type == RelationType.TRACE_CALL_CHAIN:
                return self._synthesize_trace(question, state, repo_path)
            elif main_rel.type == RelationType.EXPLAIN_BEHAVIOR:
                return self._synthesize_explain(question, state, repo_path)
            elif main_rel.type == RelationType.IMPACT_CHANGE:
                return self._synthesize_impact(question, state, repo_path)
            elif main_rel.type == RelationType.ENUMERATE_USAGES:
                evidence = list(state.all_evidence.values())
                ev_by_id = {ev.id: ev for ev in evidence if ev.location}
                required_tasks = [t for t in state.all_tasks
                                  if t.role in (TaskRole.ROOT, TaskRole.REQUIRED,
                                                TaskRole.GAP, TaskRole.RETOOL)]
                targets = targets_from_tasks(required_tasks, state.question_type)
                self._populate_target_slots(state, targets)
                return self._synthesize_grep(
                    question, state, evidence, ev_by_id, targets)
            # DEFINITION_LOCATION falls through to default

        # 默认：slot-by-slot 合成（向后兼容）
        return self._synthesize_default(question, state, repo_path)

    def _synthesize_explain(self, question: str, state: ExplorationState,
                            repo_path: str) -> InvestigationResult:
        """解释行为：DEFINITION + IMPLEMENTATION + VERIFIED_CALLEE_EDGE。"""
        return self._synthesize_default(question, state, repo_path,
                                        relation_guide="解释该符号的实际行为，说明关键流程和调用关系。")

    def _synthesize_compare(self, question: str, state: ExplorationState,
                            repo_path: str) -> InvestigationResult:
        """对比两个符号：各取 DEFINITION + IMPLEMENTATION，逐维度对比。"""
        return self._synthesize_default(question, state, repo_path,
                                        relation_guide="对比两个符号在指定维度上的差异，逐项引用证据。")

    def _synthesize_trace(self, question: str, state: ExplorationState,
                          repo_path: str) -> InvestigationResult:
        """追踪调用链：DEFINITION + CALLER + CALLEE + IMPLEMENTATION。"""
        return self._synthesize_default(question, state, repo_path,
                                        relation_guide="追踪从入口到出口的完整调用链，逐跳引用证据。")

    def _synthesize_impact(self, question: str, state: ExplorationState,
                            repo_path: str) -> InvestigationResult:
        """分析影响：DEFINITION + CALLER + CANDIDATE_REFERENCE。"""
        return self._synthesize_default(question, state, repo_path,
                                        relation_guide="分析修改该符号的影响范围，列出调用者和引用位置。")

    def _synthesize_grep(self, question: str, state: ExplorationState,
                          repo_path: str) -> InvestigationResult:
        """确定性全量输出（grep/enumerate 类型）。"""
        evidence = list(state.all_evidence.values())
        ev_by_id = {ev.id: ev for ev in evidence if ev.location}
        required_tasks = [t for t in state.all_tasks
                         if t.role in (TaskRole.ROOT, TaskRole.REQUIRED,
                                       TaskRole.GAP, TaskRole.RETOOL)]
        targets = targets_from_tasks(required_tasks, state.question_type)
        self._populate_target_slots(state, targets)
        return self._synthesize_grep(question, state, evidence, ev_by_id, targets)

    def _synthesize_default(self, question: str, state: ExplorationState,
                            repo_path: str,
                            relation_guide: str = "") -> InvestigationResult:
        """默认 slot-by-slot 合成 — 保持 V22 行为。

        V24: 新增 relation_guide 参数，在 prompt 引导语前插入关系指南。
        """
        evidence = list(state.all_evidence.values())
        ev_by_id = {ev.id: ev for ev in evidence if ev.location}

        # ── 构建 targets + slot 状态 ─────────────────────────────────
        required_tasks = [t for t in state.all_tasks
                         if t.role in (TaskRole.ROOT, TaskRole.REQUIRED,
                                       TaskRole.GAP, TaskRole.RETOOL)]
        targets = targets_from_tasks(required_tasks, state.question_type)

        # 填充 verified evidence 到每个 target 的 slot
        self._populate_target_slots(state, targets)

        # ── 构建 slot-aware 上下文 ──────────────────────────────────
        task_blocks: list[str] = []
        slot_checklist: list[str] = []
        all_slot_ev_refs: dict[str, Evidence] = {}  # 所有 slot 证据引用
        # Keep relation-specific claims attached to the evidence collected for
        # that task; synthesis must not infer this mapping from a flat pool.
        claim_evidence: dict[str, list[Evidence]] = {}

        # grep/enumerate 类型：展示全部证据，不截断
        is_grep_type = state.question_type == "grep"
        ev_per_task_cap = 0 if is_grep_type else 5  # 0 = 无上限

        for i, task in enumerate(state.all_tasks, 1):
            ev_list = state.verified_evidence.get(task.id, [])
            if not ev_list:
                continue

            slot_names = ", ".join(s.value for s in (task.required_slots or set())) or "default"
            block = [f"## 任务 {i}：{task.target}", f"Slots：[{slot_names}]"]
            if task.concept:
                block.append(f"意图：{task.concept}")

            block.append("证据：")
            capped = ev_list if ev_per_task_cap == 0 else ev_list[:ev_per_task_cap]
            for j, ev in enumerate(capped):
                if ev.location:
                    loc_str = f"{ev.location.file}:{ev.location.start_line}"
                    snippet_preview = (ev.snippet or "").replace("\n", " ")[:200]
                    block.append(f"  [{ev.id}] {ev.source} {loc_str} — {snippet_preview}")
                    all_slot_ev_refs[ev.id] = ev
            if ev_per_task_cap == 0 and len(ev_list) > 0:
                block.append(f"  （共 {len(ev_list)} 条证据）")

            for claim in task.required_claims:
                claim_evidence.setdefault(claim, []).extend(
                    ev for ev in ev_list if ev.location
                )

            task_blocks.append("\n".join(block))

        # ── 构建 slot checklist ─────────────────────────────────────
        for tid, target in targets.items():
            cn_label = _SLOT_CN
            slots_info: list[str] = []
            slot_all_confirmed = True
            for slot_kind in target.required_slots:
                verified = target.verified_slots.get(slot_kind, [])
                ev_slot = target.evidence_by_slot.get(slot_kind, [])
                all_eids = list(set(verified + ev_slot))
                if all_eids:
                    slots_info.append(
                        f"  [{slot_kind.value}] 已确认: {', '.join(all_eids[:3])}")
                    for eid in all_eids[:3]:
                        if eid in ev_by_id:
                            all_slot_ev_refs[eid] = ev_by_id[eid]
                else:
                    slots_info.append(f"  [{slot_kind.value}] 缺失")
                    slot_all_confirmed = False

            status_icon = "✓" if slot_all_confirmed else "⚠"
            slot_checklist.append(
                f"{status_icon} {target.symbol}: "
                + "; ".join(s.name for s in target.required_slots))
            slot_checklist.extend(slots_info)

        has_confirmed = bool(task_blocks)

        # ── EMPTY tier ───────────────────────────────────────────────
        if not has_confirmed:
            return InvestigationResult(
                question=question,
                evidence=evidence,
                answer=f"无法回答：未找到与问题相关的证据。"
                       f"终止原因：{state.stop_reason}。",
                trace=list(state.traces),
                steps=list(state.steps),
                files_visited=sorted({
                    ev.location.file for ev in evidence if ev.location
                }),
            )

        # ── GREP tier: 确定性全量输出 ──────────────────────────────────
        if is_grep_type:
            return self._synthesize_grep(question, state, evidence,
                                         ev_by_id, targets)

        # ── SLOT-AWARE context ──────────────────────────────────────
        context = "\n\n".join(task_blocks)
        checklist_text = "\n".join(slot_checklist) if slot_checklist else ""

        # ── Claims checklist ──────────────────────────────────────
        claims_checklist_text = ""
        if state.required_claims:
            claims_lines = ["## 必须回答的内容点"]
            for i, claim_text in enumerate(state.required_claims):
                status = "✓" if i not in state.uncovered_claims else "⚠ 证据不足"
                claims_lines.append(f"  - [{status}] {claim_text}")
            claims_lines.append("")
            claims_lines.append(
                "请确保每个内容点都在回答中有所体现。"
                "对于标 ⚠ 的内容点，如果确实无法找到证据，请在回答末尾标注「未找到相关证据」。"
            )
            claims_checklist_text = "\n".join(claims_lines)

        # ── LLM guide：按 slot 逐项回答 ──────────────────────────────
        claim_evidence_text = ""
        if claim_evidence:
            packet_lines = ["## Claim → Evidence packets"]
            for claim, claim_evs in claim_evidence.items():
                deduped: list[Evidence] = []
                seen_eids: set[str] = set()
                for ev in claim_evs:
                    if ev.id not in seen_eids:
                        seen_eids.add(ev.id)
                        deduped.append(ev)
                refs = [
                    f"[{ev.id}] {ev.location.file}:{ev.location.start_line}"
                    for ev in deduped[:4] if ev.location
                ]
                if refs:
                    packet_lines.append(
                        f"- Claim: {claim}\n  Evidence: {', '.join(refs)}"
                    )
            if len(packet_lines) > 1:
                packet_lines.append(
                    "Each Claim must be answered explicitly using only its listed evidence."
                )
                claim_evidence_text = "\n".join(packet_lines)

        qtype = state.question_type
        if state.stop_reason in ("COMPLETE", "COMPLETE_AFTER_RETOOL"):
            guide = (
                f"问题类型：{qtype}。\n\n"
                "## 回答要求（逐项检查）\n"
                "对下面「Slots 状态」中每个「已确认」的项，都必须在回答中用至少一句话覆盖，"
                "并引用对应的证据编号。不得跳过任何已确认的 slot。\n\n"
                "格式：每个事实句必须引用证据编号，如 [ev_1]。"
                "不要编造证据中没有的事实。"
            )
        else:
            guide = (
                f"问题类型：{qtype}。\n\n"
                "## 回答要求（逐项检查）\n"
                "对下面「Slots 状态」中每个「已确认」的项，都必须在回答中用至少一句话覆盖，"
                "并引用对应的证据编号。\n"
                "对于标 ⚠ 的缺失项，在回答末尾标注「尚待确认：...」说明具体缺口。\n\n"
                "格式：每个事实句必须引用证据编号，如 [ev_1]。"
                "不要编造证据中没有的事实。"
            )

        prompt_parts = [f"原始问题：{question}\n"]
        if relation_guide:
            prompt_parts.append(f"关系指南：{relation_guide}\n")
        prompt_parts.extend([
            context,
            "",
            f"## Slots 状态\n{checklist_text}",
        ])
        if claims_checklist_text:
            prompt_parts.extend(["", claims_checklist_text])
        if claim_evidence_text:
            prompt_parts.extend(["", claim_evidence_text])
        prompt_parts.extend(["", guide])
        prompt = "\n".join(prompt_parts)
        try:
            raw = (self.call_llm(
                prompt, system="你是严格证据约束的代码调查答复器。对每个已确认 slot 必须给出回答。",
                temperature=0, max_tokens=1000, **_LLM_CALL_KWARGS) or "").strip()
        except Exception:
            raw = ""

        if not raw:
            # LLM unavailable — deterministic slot-by-slot summary
            lines = [f"问题：{question}\n"]
            for tid, target in targets.items():
                lines.append(f"\n## {target.symbol}")
                for slot_kind in target.required_slots:
                    verified = target.verified_slots.get(slot_kind, [])
                    ev_slot = target.evidence_by_slot.get(slot_kind, [])
                    all_eids = list(set(verified + ev_slot))
                    label = _SLOT_CN.get(slot_kind, slot_kind.value)
                    if all_eids:
                        for eid in all_eids[:3]:
                            ev = ev_by_id.get(eid)
                            if ev and ev.location:
                                lines.append(
                                    f"  [{label}] {ev.location.file}:{ev.location.start_line} "
                                    f"— {(ev.snippet or '')[:200]}")
                    else:
                        lines.append(f"  [{label}] 尚待确认")
            return InvestigationResult(
                question=question, evidence=evidence,
                answer="\n".join(lines),
                trace=list(state.traces), steps=list(state.steps),
                files_visited=sorted({
                    ev.location.file for ev in evidence if ev.location
                }),
            )

        # Split into sentences; keep only those that cite evidence
        sentences = re.split(r"(?<=[。！？\n])", raw)
        grounded: list[str] = []
        for sent in sentences:
            refs = set(re.findall(r"\[(ev_\w+)\]", sent))
            if refs & ev_by_id.keys():
                for rid in refs:
                    if rid in ev_by_id:
                        loc = ev_by_id[rid].location
                        sent = sent.replace(
                            f"[{rid}]", f"{loc.file}:{loc.start_line}")
                grounded.append(sent)

        if grounded:
            final_answer = "".join(grounded)
        else:
            final_answer = "\n".join(
                f"- {ev.location.file}:{ev.location.start_line} — {ev.snippet[:240]}"
                for ev in evidence if ev.location)

        # ── 覆盖检查：确保所有 slot 证据都被引用 ───────────────────────
        cited_files_lines: set[tuple[str, int]] = set()
        for m in re.finditer(r"([\w./-]+\.[\w]+):(\d+)", final_answer, re.ASCII):
            cited_files_lines.add((m.group(1), int(m.group(2))))

        missing_ev: list[Evidence] = []
        for eid, ev in all_slot_ev_refs.items():
            if ev.location:
                key = (ev.location.file, ev.location.start_line)
                if key not in cited_files_lines:
                    missing_ev.append(ev)

        if missing_ev:
            supplement = ["\n\n## 补充证据（答案未覆盖的已验证证据）"]
            by_file_missing: dict[str, list[Evidence]] = {}
            for ev in missing_ev:
                by_file_missing.setdefault(ev.location.file, []).append(ev)
            for fname in sorted(by_file_missing.keys()):
                supplement.append(f"\n### {fname}")
                for ev in by_file_missing[fname][:8]:  # 每个文件最多8条补充
                    snippet_preview = (ev.snippet or "").replace("\n", " ")[:200]
                    supplement.append(
                        f"  - L{ev.location.start_line}: {snippet_preview}")
            final_answer += "\n".join(supplement)

        return InvestigationResult(
            question=question, evidence=evidence,
            answer=final_answer,
            trace=list(state.traces), steps=list(state.steps),
            files_visited=sorted({
                ev.location.file for ev in evidence if ev.location
            }),
        )

    # ── grep 确定性合成 ─────────────────────────────────────────────

    @staticmethod
    def _synthesize_grep(question: str, state: ExplorationState,
                         evidence: list[Evidence],
                         ev_by_id: dict[str, Evidence],
                         targets: dict) -> InvestigationResult:
        """grep/enumerate 类型：确定性全量输出，不依赖 LLM 挑选子集。

        流程：
        1. 收集所有 verified evidence（CANDIDATE_REFERENCE slot）
        2. 按 file + line 去重
        3. 按文件分组，全部输出
        """
        # 收集所有 CANDIDATE_REFERENCE evidence
        seen: set[tuple[str, int]] = set()
        by_file: dict[str, list[tuple[int, str, str]]] = {}  # file → [(line, ev_id, snippet)]

        for tid, target in targets.items():
            all_eids = list(set(
                target.verified_slots.get(SlotKind.CANDIDATE_REFERENCE, []) +
                target.evidence_by_slot.get(SlotKind.CANDIDATE_REFERENCE, [])
            ))
            for eid in all_eids:
                ev = ev_by_id.get(eid)
                if not ev or not ev.location:
                    continue
                key = (ev.location.file, ev.location.start_line)
                if key in seen:
                    continue
                seen.add(key)
                by_file.setdefault(ev.location.file, []).append(
                    (ev.location.start_line, eid, (ev.snippet or "")[:200])
                )

        # 按文件分组输出
        lines = [f"问题：{question}\n"]
        total_hits = sum(len(v) for v in by_file.values())
        lines.append(f"共 {len(by_file)} 个文件，{total_hits} 处匹配：\n")

        for fname in sorted(by_file.keys()):
            entries = sorted(by_file[fname], key=lambda x: x[0])
            lines.append(f"\n## {fname}（{len(entries)} 处）")
            for line_no, eid, snippet in entries:
                snippet_clean = snippet.replace("\n", " ").strip()[:150]
                lines.append(f"  - L{line_no}: {snippet_clean}")

        answer = "\n".join(lines)
        return InvestigationResult(
            question=question, evidence=evidence,
            answer=answer,
            trace=list(state.traces), steps=list(state.steps),
            files_visited=sorted({
                ev.location.file for ev in evidence if ev.location
            }),
        )

    # ── V22 诊断字段填充 ────────────────────────────────────────────

    def _populate_v22_diagnostics(self, result: InvestigationResult,
                                   state: ExplorationState) -> None:
        """从 ExplorationState 提取 V22 诊断信息到 result。"""
        # 任务摘要
        result.planned_tasks = [
            {"id": t.id, "target": t.target,
             "slots": [s.value for s in (t.required_slots or set())],
             "role": t.role, "subtree_depth": t.subtree_depth,
             "concept": t.concept}
            for t in state.all_tasks
            if t.role in (TaskRole.ROOT, TaskRole.REQUIRED)
        ]
        result.all_tasks = [
            {"id": t.id, "target": t.target,
             "slots": [s.value for s in (t.required_slots or set())],
             "role": t.role, "subtree_depth": t.subtree_depth}
            for t in state.all_tasks
        ]

        # work_orders — 从 steps 中提取
        result.work_orders = [
            {"tool": s.get("tool", ""), "target": s.get("target", ""),
             "action": s.get("action", ""), "outcome": s.get("outcome", ""),
             "evidence_count": s.get("evidence_count", 0)}
            for s in state.steps
        ]

        # 证据摘要
        result.verified_evidence_summary = {
            tid: [ev.id for ev in ev_list]
            for tid, ev_list in state.verified_evidence.items()
        }
        result.candidate_evidence_count = {
            tid: len(ev_list)
            for tid, ev_list in state.candidate_evidence.items()
        }

        # slot 状态 — 从 targets 提取（先填充 verified_slots）
        required_tasks = [t for t in state.all_tasks
                         if t.role in (TaskRole.ROOT, TaskRole.REQUIRED,
                                       TaskRole.GAP, TaskRole.RETOOL)]
        targets = targets_from_tasks(required_tasks, state.question_type)
        self._populate_target_slots(state, targets)
        for tid, target in targets.items():
            req_names = [s.name for s in target.required_slots]
            closed_names = [s.name for s in target.required_slots
                           if target.verified_slots.get(s)]
            open_names = [s.name for s in target.required_slots
                         if not target.verified_slots.get(s)]
            result.required_slots[tid] = req_names
            result.closed_slots[tid] = closed_names
            result.open_slots[tid] = open_names

        # 合同判定 + 终止原因
        result.contract_met_before = state.contract_met_before
        result.contract_met_after = (
            state.contract_met_after_gap or state.contract_met_after_retool
        )
        result.final_contract_met = state.final_contract_met
        result.stop_reason = state.stop_reason
        result.retool_triggered = state.retool_used

        # ── V23 Claims 诊断 ──────────────────────────────────────────
        result.required_claims = list(state.required_claims)
        result.covered_claims = sorted(
            set(range(len(state.required_claims))) - set(state.uncovered_claims)
        )
        result.uncovered_claims = list(state.uncovered_claims)
        total_claims = len(state.required_claims)
        if total_claims > 0:
            result.claim_coverage_rate = len(result.covered_claims) / total_claims
        else:
            result.claim_coverage_rate = 1.0 if total_claims == 0 else 0.0

    # ── 关键词提取 ──────────────────────────────────────────────────

    @staticmethod
    def _extract_keywords(question: str) -> list[TargetSpec]:
        quoted = re.findall(r'["\']([^"\']+)["\']', question)
        if quoted:
            result: list[TargetSpec] = []
            seen: set[str] = set()
            for value in quoted:
                spec = _normalize_search_keyword(value)
                if spec and spec.qualified_symbol not in seen:
                    result.append(spec)
                    seen.add(spec.qualified_symbol)
                    if len(result) >= 3:
                        return result
            if result:
                return result
        qualified = re.findall(
            r'\b([A-Z][a-zA-Z0-9_]*\.[a-z_][a-zA-Z0-9_]*)\b', question)
        if qualified:
            result: list[TargetSpec] = []
            seen: set[str] = set()
            for value in qualified:
                spec = _normalize_search_keyword(value)
                if spec and spec.qualified_symbol not in seen:
                    result.append(spec)
                    seen.add(spec.qualified_symbol)
                    if len(result) >= 3:
                        return result
            if result:
                return result
        identifiers = re.findall(
            r'\b([A-Z][a-zA-Z0-9_]*|[a-z]+_[a-z_]+|[a-z][a-z0-9_]{2,})\b',
            question)
        if identifiers:
            result: list[TargetSpec] = []
            seen: set[str] = set()
            for value in identifiers:
                spec = _normalize_search_keyword(value)
                if spec and spec.qualified_symbol not in seen:
                    result.append(spec)
                    seen.add(spec.qualified_symbol)
                    if len(result) >= 3:
                        return result
            if result:
                return result
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{2,}', question)
        result: list[TargetSpec] = []
        seen: set[str] = set()
        for value in words:
            spec = _normalize_search_keyword(value)
            if spec and spec.qualified_symbol not in seen:
                result.append(spec)
                seen.add(spec.qualified_symbol)
                if len(result) >= 3:
                    return result
        return result

    # ── 辅助方法 ──────────────────────────────────────────────────

    @staticmethod
    def _extract_claims_from_answer(answer: str,
                                     evidence: list[Evidence]) -> list[ClaimCitation]:
        if not answer:
            return []
        citations = re.findall(r"([\w./-]+\.[\w]+):(\d+)", answer)
        if not citations:
            return []
        loc_to_id: dict[tuple[str, str], str] = {}
        for ev in evidence:
            if ev.location and ev.location.file:
                loc_to_id[(ev.location.file, str(ev.location.start_line))] = ev.id
        claims: list[ClaimCitation] = []
        seen: set[str] = set()
        for file, line in citations:
            ev_id = loc_to_id.get((file, line), "")
            if ev_id and ev_id not in seen:
                claims.append(ClaimCitation(
                    text=f"reference to {file}:{line}",
                    evidence_ids=[ev_id],
                ))
                seen.add(ev_id)
        return claims

    @staticmethod
    def _rank_context_files(files, evidence: list, keywords: list) -> list[str]:
        hits: dict[str, int] = {}
        snippets: dict[str, list[str]] = {}
        for ev in evidence:
            if ev.location and ev.location.file:
                fname = ev.location.file
                hits[fname] = hits.get(fname, 0) + 1
                if ev.snippet:
                    snippets.setdefault(fname, []).append(ev.snippet)
        kw_strs = [kw.member_symbol if isinstance(kw, TargetSpec) else str(kw)
                   for kw in keywords if kw]
        def_pats = [re.compile(rf"\b(?:class|def)\s+{re.escape(kw)}\b", re.IGNORECASE)
                    for kw in kw_strs]

        def is_low_priority(fname: str) -> bool:
            segs = fname.replace("\\", "/").split("/")[:-1]
            return any(seg in _LOW_PRIORITY_CONTEXT_DIRS for seg in segs)

        def has_definition(fname: str) -> bool:
            return any(p.search(s) for s in snippets.get(fname, ()) for p in def_pats)

        return sorted(files, key=lambda f: (
            is_low_priority(f), not has_definition(f), -hits.get(f, 0), f))

    @staticmethod
    def _find_definition_lines(content: str, keywords: list, limit: int = 5) -> list[int]:
        if not keywords:
            return []
        kw_strs = [kw.member_symbol if isinstance(kw, TargetSpec) else str(kw)
                   for kw in keywords if kw]
        pats = [re.compile(rf"^\s*(?:class|def|async\s+def|function)\s+{re.escape(kw)}\b",
                           re.IGNORECASE)
                for kw in kw_strs]
        out: list[int] = []
        for i, line in enumerate(content.splitlines(), 1):
            if any(p.match(line) for p in pats):
                out.append(i)
                if len(out) >= limit:
                    break
        return out

    @staticmethod
    def _extract_windows(content: str, hit_lines: list[int], radius: int = 30,
                         max_windows: int = 3,
                         priority_lines: list[int] | None = None) -> str:
        lines = content.splitlines()
        total = len(lines)
        weights: dict[int, int] = {}
        for l in hit_lines or []:
            if isinstance(l, int) and 1 <= l <= total:
                weights[l] = max(weights.get(l, 0), 1)
        for l in priority_lines or []:
            if isinstance(l, int) and 1 <= l <= total:
                weights[l] = 4
        if not weights:
            return ""
        intervals: list[list[int]] = []
        for l in sorted(weights):
            s, e = max(1, l - radius), min(total, l + radius)
            if intervals and s <= intervals[-1][1] + 1:
                intervals[-1][1] = max(intervals[-1][1], e)
                intervals[-1][2] += weights[l]
            else:
                intervals.append([s, e, weights[l]])
        if len(intervals) > max_windows:
            intervals = sorted(intervals, key=lambda iv: (-iv[2], iv[0]))[:max_windows]
            intervals.sort(key=lambda iv: iv[0])
        parts = []
        for s, e, _ in intervals:
            parts.append("\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(s, e + 1)))
        return "\n  ...\n".join(parts)

    @staticmethod
    def _select_synthesis_evidence(evidence: list, max_items: int = 20,
                                   per_file_cap: int = 3) -> list:
        ranked = sorted(evidence, key=lambda e: -e.confidence)
        selected: list = []
        overflow: list = []
        per_file: dict[str, int] = {}
        for ev in ranked:
            fname = ev.location.file if ev.location else ""
            if per_file.get(fname, 0) >= per_file_cap:
                overflow.append(ev)
                continue
            selected.append(ev)
            per_file[fname] = per_file.get(fname, 0) + 1
            if len(selected) >= max_items:
                return selected
        for ev in overflow:
            if len(selected) >= max_items:
                break
            selected.append(ev)
        return selected

    @staticmethod
    def _fallback_answer(question: str, evidence_lines: list[str]) -> str:
        parts = ["（LLM 不可用，以下为调查结果摘要）\n"]
        parts.append(f"问题: {question}")
        if evidence_lines:
            parts.append("\n关键证据:")
            parts.extend(f"- {line}" for line in evidence_lines[:10])
        return "\n".join(parts)

    @staticmethod
    def _new_investigation_id(question: str) -> str:
        raw = f"{question}|{time.time()}|{uuid.uuid4().hex[:6]}"
        return "inv_" + hashlib.md5(raw.encode()).hexdigest()[:12]

    @staticmethod
    def _read_python_files(repo_path: str, files_visited: set,
                           files_read: int, files_max: int) -> list[tuple[str, str]]:
        remaining = files_max - files_read
        py_files = [f for f in files_visited if f.endswith(".py")][:min(10, remaining)]
        if not py_files:
            return []
        files: list[tuple[str, str]] = []
        workspace = WorkspaceManager()
        try:
            for fpath in py_files:
                try:
                    content = workspace.read_file_at_ref(repo_path, "HEAD", fpath)
                    files.append((fpath, content))
                except ValueError:
                    continue
        except Exception:
            pass
        return files

    @staticmethod
    def _estimate_tokens(char_count: int) -> int:
        return max(1, char_count // 4)

    @staticmethod
    def _match_existing_evidence(session: dict, question: str,
                                 keywords: list) -> list[str]:
        refs: list[str] = []
        kw_lower = {k.member_symbol.lower() if isinstance(k, TargetSpec) else str(k).lower()
                     for k in keywords}
        for ev_dict in session.get("evidence", []):
            snippet = (ev_dict.get("snippet", "") or "").lower()
            source = (ev_dict.get("source", "") or "").lower()
            loc = ev_dict.get("location", {}) or {}
            file = (loc.get("file", "") or "").lower()
            text = f"{snippet} {source} {file}"
            if any(kw in text for kw in kw_lower):
                loc_str = f"{loc.get('file', '?')}:{loc.get('start_line', 0)}"
                refs.append(f"[{ev_dict.get('source', '?')}] {loc_str}")
        return refs
