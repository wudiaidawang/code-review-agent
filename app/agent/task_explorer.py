"""V22 Task Explorer — 全局优先队列探查 + 确定性补缺 + LLM 一次 retool.

Core control flow:
  Phase 2: Global priority-queue exploration (all root tasks get one round each)
  Phase 3: Rule-based contract check (only verified evidence)
  Phase 4: Deterministic gap fill (if contract not met)
  Phase 5: LLM one-shot retool (if contract met but gaps remain)
  Phase 6: Synthesis (only verified evidence)

Evidence trust chain:
  Tool raw evidence → EvidenceVerifier.verify() →
    verified_evidence (discovery, contract, synthesis)
    candidate_evidence (audit only)
"""

from __future__ import annotations

import ast as _ast
import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from app.models.evidence import Evidence
from app.models.location import CodeLocation
from app.models.target import (
    GapStrategy, InvestigationTask, TaskRole, TaskStatus,
    TargetSpec, WorkOrder,
)
from app.agent.evidence_closure import (
    AnswerTarget, EvidenceVerifier, SearchScope, SlotKind, TargetKind,
    _EXCLUDE_DIRS, _default_scope_for_target, check_minimum_evidence_contract,
    targets_from_tasks, SLOT_TO_TOOL,
)
from app.agent.symbol_resolver import ResolvedSymbol, SymbolResolverV2, resolved_to_evidence

# ── 工单上限 ──────────────────────────────────────────────────────
MAX_ORDERS_PER_TASK = 4

# ── GLM 调用参数 ───────────────────────────────────────────────────
_LLM_CALL_KWARGS = {"timeout": 60, "extra_body": {"thinking": {"type": "disabled"}}}


# ═══════════════════════════════════════════════════════════════════
# ExplorationState
# ═══════════════════════════════════════════════════════════════════

@dataclass
class ExplorationState:
    """V22 全局探索状态 — 三阶段独立预算 + 任务树 + 证据可信链."""

    question: str
    question_type: str = "locate"
    repo_path: str = ""
    repo_revision: str = "HEAD"

    # ── 预算上限 ──────────────────────────────────────────────────
    max_main_steps: int = 12
    max_gap_steps: int = 3
    max_retool_steps: int = 4
    max_subtree_depth: int = 2

    # ── 已使用（每次 consume_budget 真实扣除）─────────────────────
    main_steps_used: int = 0
    gap_steps_used: int = 0
    retool_steps_used: int = 0

    # ── 当前阶段 ──────────────────────────────────────────────────
    current_phase: str = "MAIN"

    # ── 任务树 ────────────────────────────────────────────────────
    all_tasks: list[InvestigationTask] = field(default_factory=list)
    pending_tasks: list[InvestigationTask] = field(default_factory=list)
    task_by_id: dict[str, InvestigationTask] = field(default_factory=dict)

    # ── 证据可信链 ────────────────────────────────────────────────
    verified_evidence: dict[str, list[Evidence]] = field(default_factory=dict)
    candidate_evidence: dict[str, list[tuple[Evidence, str]]] = field(default_factory=dict)
    all_evidence: dict[str, Evidence] = field(default_factory=dict)

    # ── 工具执行记录 ──────────────────────────────────────────────
    completed_action_keys: set[str] = field(default_factory=set)
    traces: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)

    # ── 合同判定缓存 ──────────────────────────────────────────────
    contract_met_before: bool = False
    contract_met_after_gap: bool = False
    contract_met_after_retool: bool = False
    final_contract_met: bool = False
    stop_reason: str = ""

    # ── retool 状态 ───────────────────────────────────────────────
    retool_used: bool = False
    retool_task: InvestigationTask | None = None
    answers: list[str] = field(default_factory=list)

    # ── required_claims ──────────────────────────────────────────
    required_claims: list[str] = field(default_factory=list)
    covered_claims: list[int] = field(default_factory=list)
    uncovered_claims: list[int] = field(default_factory=list)
    claim_coverage_evidence_ids: tuple[str, ...] = field(default_factory=tuple)

    # ── V24: Planner 输出（relation 驱动）────────────────────────
    planner_output: Any | None = None  # PlannerOutput, 延迟类型引用

    # ── 预算扣账 ──────────────────────────────────────────────────

    def consume_budget(self, phase: str, amount: int = 1) -> bool:
        """扣账。返回 True 表示扣成功，False 表示该 phase 预算已耗尽。"""
        if phase == "MAIN":
            if self.main_steps_used + amount > self.max_main_steps:
                return False
            self.main_steps_used += amount
            return True
        elif phase == "GAP":
            if self.gap_steps_used + amount > self.max_gap_steps:
                return False
            self.gap_steps_used += amount
            return True
        elif phase == "RETOOL":
            if self.retool_steps_used + amount > self.max_retool_steps:
                return False
            self.retool_steps_used += amount
            return True
        return False

    # ── 任务管理 ──────────────────────────────────────────────────

    def register_task(self, task: InvestigationTask) -> None:
        """注册任务到 all_tasks，不加入 pending 队列。"""
        if task.id not in self.task_by_id:
            self.task_by_id[task.id] = task
            self.all_tasks.append(task)

    def enqueue_tasks(self, tasks: list[InvestigationTask]) -> None:
        """入全局队列。注册到 all_tasks，追加到 pending。"""
        for t in tasks:
            if t.id not in self.task_by_id:
                self.task_by_id[t.id] = t
                self.all_tasks.append(t)
                self.pending_tasks.append(t)

    def pop_next(self) -> InvestigationTask | None:
        """优先级: ROOT > REQUIRED > AUXILIARY; 同级按 subtree_depth 升序."""
        if not self.pending_tasks:
            return None

        def _key(t: InvestigationTask):
            role_order = {TaskRole.ROOT: 0, TaskRole.REQUIRED: 1, TaskRole.AUXILIARY: 2}
            return (role_order.get(t.role, 9), t.subtree_depth, t.priority)

        self.pending_tasks.sort(key=_key)
        return self.pending_tasks.pop(0)

    def has_pending(self) -> bool:
        return bool(self.pending_tasks)

    def is_duplicate(self, task: InvestigationTask) -> bool:
        """检查是否有同 required_slots + 同目标的已注册任务。"""
        for t in self.all_tasks:
            if t.target == task.target and t.required_slots == task.required_slots:
                return True
        return False

    # ── 证据管理 ──────────────────────────────────────────────────

    def add_verified_evidence(self, ev: Evidence, task_id: str) -> None:
        self.verified_evidence.setdefault(task_id, []).append(ev)
        if ev.id not in self.all_evidence:
            self.all_evidence[ev.id] = ev
            # Claim coverage is a snapshot over verified evidence.  New
            # evidence invalidates it; candidate evidence never does.
            self.claim_coverage_evidence_ids = ()

    def add_candidate_evidence(self, ev: Evidence, task_id: str, reason: str) -> None:
        """Record rejected raw output for audit only.

        Candidates must never enter ``all_evidence``: that collection is the
        verified-only evidence ledger consumed by contracts and synthesis.
        """
        self.candidate_evidence.setdefault(task_id, []).append((ev, reason))

    def get_verified_evidence(self) -> dict[str, list[Evidence]]:
        return dict(self.verified_evidence)

    def record_step(self, work_order: WorkOrder, raw_evidence: list[Evidence],
                    error: str = "") -> None:
        ev_count = len(raw_evidence)
        if error:
            outcome = f"error: {error[:120]}"
            status = "tool_error"
        elif ev_count:
            outcome = f"found {ev_count} evidence"
            status = "success"
        else:
            outcome = "no evidence"
            status = "no_evidence"
        self.steps.append({
            "tool": work_order.tool_hint or "unknown",
            "target": work_order.target,
            "action": work_order.search_kind,
            "outcome": outcome,
            "status": status,
            "params": {
                "target": work_order.target,
                "search_kind": work_order.search_kind,
                "task_id": work_order.task_id,
            },
            "evidence_count": ev_count,
        })

    def mark_completed(self, task: InvestigationTask, task_status: TaskStatus) -> None:
        task.status = task_status.value
        self.completed_action_keys.add(f"task:{task.id}")


# ═══════════════════════════════════════════════════════════════════
# AST 调用提取（verify_callees 核心逻辑）
# ═══════════════════════════════════════════════════════════════════

def _collect_calls_in_function(tree: _ast.AST, func_lineno: int,
                                class_name: str | None = None,
                                base_classes: list[str] | None = None) -> list[dict]:
    """在函数体中提取所有直接 ast.Call，跳过嵌套 class/def。

    返回 list[dict]: {callee_expr, line, receiver_type, callee_normalized}
    """
    # 搜索包含 func_lineno 的最内层 FunctionDef（避免 find_node_at_line 误返回 arg/Expr）
    candidates: list[tuple[int, _ast.AST]] = []
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            if node.lineno <= func_lineno <= (node.end_lineno or node.lineno):
                span = (node.end_lineno or node.lineno) - node.lineno
                candidates.append((span, node))
    if not candidates:
        return []
    candidates.sort(key=lambda x: x[0])
    func_node = candidates[0][1]

    bases = base_classes or []
    calls: list[dict] = []
    for node in func_node.body:
        _walk_for_calls(node, class_name, bases, calls)
    return calls


def _walk_for_calls(node: _ast.AST, class_name: str | None,
                     base_classes: list[str], calls: list[dict]) -> None:
    """Collect every call expression while excluding nested declarations."""
    if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
        return

    if isinstance(node, _ast.Call):
        info = _extract_call_info(node, class_name, base_classes)
        if info:
            calls.append(info)

    # Generic traversal covers Return, BinOp, keyword arguments,
    # comprehensions, context managers, and control flow.  The declaration
    # guard above prevents calls from nested functions/classes leaking into
    # the current function's call chain.
    for child in _ast.iter_child_nodes(node):
        _walk_for_calls(child, class_name, base_classes, calls)


def _extract_call_info(call_node: _ast.Call, class_name: str | None,
                        base_classes: list[str]) -> dict | None:
    """从单个 ast.Call 节点提取调用信息，包括 receiver 分类和 owner 标准化。"""
    func = call_node.func
    expr_str, receiver_type, normalized = _classify_and_normalize(
        func, class_name, base_classes)
    if not expr_str:
        return None
    return {
        "callee_expr": expr_str,
        "line": call_node.lineno,
        "receiver_type": receiver_type,
        "callee_normalized": normalized,
    }


def _classify_and_normalize(func_node, class_name: str | None,
                              base_classes: list[str]) -> tuple[str, str, str]:
    """分析接收者类型并标准化为可解析符号。

    Returns (expr_str, receiver_type, normalized_or_empty).
    """
    if isinstance(func_node, _ast.Attribute):
        receiver = _get_attr_receiver_name(func_node.value)
        method = func_node.attr

        if receiver == "self" and class_name:
            return (f"self.{method}", "self", f"{class_name}.{method}")
        if receiver == "cls" and class_name:
            return (f"cls.{method}", "cls", f"{class_name}.{method}")
        if receiver == "super":
            base = base_classes[0] if base_classes else ""
            normalized = f"{base}.{method}" if base else ""
            return (f"super().{method}", "super", normalized)
        if receiver:
            normalized = f"{receiver}.{method}"
            return (f"{receiver}.{method}", "variable", normalized)
        return (f".{method}", "bare", method)

    if isinstance(func_node, _ast.Name):
        return (func_node.id, "bare", func_node.id)

    # 链式调用等复杂情况
    return ("", "bare", "")


def _get_attr_receiver_name(node) -> str:
    """递归提取属性访问链的根名称。"""
    if isinstance(node, _ast.Name):
        return node.id
    if isinstance(node, _ast.Attribute):
        return f"{_get_attr_receiver_name(node.value)}.{node.attr}"
    if isinstance(node, _ast.Call):
        # super() / get_self() 等调用返回
        if isinstance(node.func, _ast.Name):
            return node.func.id  # super, get_self, etc.
        return ""
    return ""


def _symbols_equivalent(observed: str, expected: str) -> bool:
    """Match resolver-normalized names without accepting arbitrary siblings."""
    observed = observed.strip().lstrip(".")
    expected = expected.strip().lstrip(".")
    if not observed or not expected:
        return False
    if observed == expected:
        return True
    # Resolver normalization can add/remove an import owner.  Accept only a
    # namespace suffix match, never a bare same-name method from another type.
    return observed.endswith("." + expected) or expected.endswith("." + observed)


def _get_class_context(tree: _ast.AST, func_lineno: int) -> tuple[str | None, list[str]]:
    """查找函数所属的类定义，返回 (class_name, base_classes)。"""
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.ClassDef,)):
            continue
        if node.lineno <= func_lineno <= (node.end_lineno or node.lineno):
            bases: list[str] = []
            for base in node.bases:
                if isinstance(base, _ast.Name):
                    bases.append(base.id)
                elif isinstance(base, _ast.Attribute):
                    bases.append(_get_attr_receiver_name(base))
            return node.name, bases
    return None, []


# ═══════════════════════════════════════════════════════════════════
# ToolExecutor
# ═══════════════════════════════════════════════════════════════════

class ToolExecutor:
    """执行工具调用，返回原始 Evidence 列表。

    从 evidence_closure.py 的 EvidenceClosureEngine 提取工具执行方法，
    去除了对 ClosureState/ClosureAction 的依赖。
    """

    def __init__(self, repo_path: str):
        self.repo_path = Path(repo_path)
        self._verifier = EvidenceVerifier()
        self._resolver = SymbolResolverV2(repo_path)

    def execute(self, work_order: WorkOrder, question_type: str = "locate") -> list[Evidence]:
        """根据 WorkOrder 执行工具调用，返回原始 evidence。"""
        tool = work_order.tool_hint or self._infer_tool(work_order)
        target = work_order.target

        if tool == "resolve_symbol":
            return list(self._resolve(target))
        elif tool == "read_window":
            return list(self._read_window(target, work_order, question_type))
        elif tool == "search_references":
            return list(self._search_references(target, work_order))
        elif tool == "verify_callers":
            return list(self._verify_callers(target, work_order))
        elif tool == "verify_callsite":
            return list(self._verify_callsite(target, work_order))
        elif tool == "verify_callees":
            # verify_callees 已由 _execute_task 拦截处理（通过 _execute_verify_callees_with_resolved）
            # 不应走此路径；若到达此处说明调度逻辑有 bug
            raise RuntimeError(
                "verify_callees must be handled by _execute_verify_callees_with_resolved, "
                "not via execute() dispatch")
        elif tool == "search":
            return list(self._search_references(target, work_order))
        else:
            return list(self._resolve(target))

    @staticmethod
    def _infer_tool(wo: WorkOrder) -> str:
        """从 search_kind 推断默认工具。"""
        kind = wo.search_kind
        if kind == "definition":
            return "resolve_symbol"
        if kind in ("callers", "references", "literal"):
            return "search_references"
        return "resolve_symbol"

    # ── resolve ────────────────────────────────────────────────────

    @staticmethod
    def _parse_owner(symbol: str) -> tuple[str, str]:
        """解析 owner-qualified symbol。返回 (owner, member)。"""
        if "." in symbol:
            parts = symbol.rsplit(".", 1)
            return parts[0], parts[1]
        if "::" in symbol:
            parts = symbol.rsplit("::", 1)
            return parts[0], parts[1]
        return "", symbol

    def _resolve(self, symbol: str) -> Iterable[Evidence]:
        """通过 SymbolResolverV2 定位符号定义位置。"""
        resolved = self._resolver.resolve(symbol)
        if resolved is None:
            return
        ev = resolved_to_evidence(resolved, str(self.repo_path))
        if ev is not None:
            yield ev

    # ── read_window ────────────────────────────────────────────────

    def _read_window(self, target: str, wo: WorkOrder,
                     question_type: str = "locate") -> Iterable[Evidence]:
        """读取代码窗口，大小取决于问题类型。"""
        file = wo.file_hint or ""
        line = wo.line or 0
        if not file or not line:
            # Try to resolve first
            for ev in self._resolve(target):
                file = ev.location.file
                line = ev.location.start_line
                break
        if not file or not line:
            return

        try:
            text = (self.repo_path / file).read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            return

        lines = text.splitlines()

        # Window size by question type
        if question_type == "explain":
            before, after = 5, 120
        elif question_type in ("trace", "impact"):
            before, after = 5, 80
        else:
            before, after = 20, 60

        start = max(1, line - before)
        end = min(len(lines), line + after)

        # Build snippet with ~3000 char limit
        chars = 0
        char_limit = 3000
        kept_lines: list[str] = []
        for i in range(start, end + 1):
            line_str = f"{i}| {lines[i - 1]}"
            if chars + len(line_str) > char_limit and kept_lines:
                stripped = lines[i - 1].strip()
                if stripped and not stripped.startswith(
                    ("def ", "class ", "return", "raise", "if ", "for ")
                ):
                    continue
                break
            kept_lines.append(line_str)
            chars += len(line_str) + 1

        snippet = "\n".join(kept_lines)
        final_end = start + len(kept_lines) - 1
        ev = Evidence(
            kind="code", source="read_window",
            location=CodeLocation(file=file, start_line=line,
                                  end_line=final_end, symbol=target),
            snippet=snippet, confidence=1.0,
        )
        ev.set_deterministic_id("HEAD", file, line, final_end, snippet)
        yield ev

    # ── search_references ──────────────────────────────────────────

    def _search_references(self, term: str, wo: WorkOrder) -> Iterable[Evidence]:
        """Grep 仓库搜索引用。"""
        search_kind = wo.search_kind
        tkind = TargetKind.SYMBOL
        # Determine if this is a decorator/module/syntax search
        if term.startswith("@"):
            tkind = TargetKind.DECORATOR
            pattern = re.compile(rf"@{re.escape(term.lstrip('@'))}\b")
        elif search_kind == "literal":
            pattern = re.compile(re.escape(term))
        elif search_kind == "callers":
            needle = term.rsplit(".", 1)[-1]
            pattern = re.compile(rf"\b{re.escape(needle)}\s*\(")
        else:
            needle = term.rsplit(".", 1)[-1]
            pattern = re.compile(rf"\b{re.escape(needle)}\b")

        scope = SearchScope()
        scope.max_files = 20
        scope.max_total_evidence = 50
        # Allow docs/examples/tests for literal/reference searches (grep-type tasks)
        if search_kind in ("literal", "references"):
            scope.allow_docs_examples_tests = True

        raw_hits: list[tuple[Path, int, str]] = []
        files_scanned = 0
        for path in self.repo_path.rglob("*.py"):
            rel = path.relative_to(self.repo_path)
            # Only exclude hidden directories *inside* the investigated repo.
            # The absolute workspace may itself contain a hidden temp directory.
            if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
                continue
            parts = [p.lower() for p in rel.parts]
            if not scope.allow_docs_examples_tests:
                if any(d in parts for d in _EXCLUDE_DIRS):
                    continue
            files_scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except (FileNotFoundError, OSError):
                continue
            for no, line_text in enumerate(text.splitlines(), 1):
                if pattern.search(line_text):
                    raw_hits.append((rel, no, line_text.strip()))

        scope.total_files_scanned = files_scanned
        scope.total_hits_found = len(raw_hits)

        # Sort: non-excluded dirs first
        def _priority(rel_path: Path) -> int:
            parts_str = str(rel_path).lower().replace("\\", "/")
            if not any(d in parts_str.split("/") for d in _EXCLUDE_DIRS):
                return 0
            return 1

        raw_hits.sort(key=lambda x: (_priority(x[0]), str(x[0]), x[1]))

        emitted = 0
        files_used: dict[str, int] = {}
        for rel_path, line_no, line_text in raw_hits:
            file_key = str(rel_path)
            if len(files_used) >= scope.max_files:
                break
            if files_used.get(file_key, 0) >= scope.max_hits_per_file:
                continue
            if emitted >= scope.max_total_evidence:
                break
            files_used.setdefault(file_key, 0)
            files_used[file_key] += 1
            emitted += 1
            ev = Evidence(
                kind="code", source="search_references",
                location=CodeLocation(file=str(rel_path), start_line=line_no,
                                      symbol=term),
                snippet=line_text, confidence=0.8,
            )
            ev.set_deterministic_id("HEAD", str(rel_path), line_no, line_no, line_text)
            yield ev

    def _verify_callers(self, target: str, wo: WorkOrder) -> Iterable[Evidence]:
        """Find candidate callers and return only call expressions that verify.

        Candidate grep hits are deliberately not emitted into the verified ledger:
        a comment, a definition, or a similarly named symbol is not a caller
        edge.  This keeps the SlotKind.VERIFIED_CALLER_EDGE contract honest.
        """
        candidate_order = WorkOrder(
            task_id=wo.task_id,
            description=f"candidate callers for {target}",
            target=target,
            tool_hint="search_references",
            search_kind="callers",
        )
        for candidate in self._search_references(target, candidate_order):
            if not candidate.location:
                continue
            verify_order = WorkOrder(
                task_id=wo.task_id,
                description=f"verify caller for {target}",
                target=target,
                tool_hint="verify_callsite",
                file_hint=candidate.location.file,
                line=candidate.location.start_line,
            )
            for verified in self._verify_callsite(target, verify_order):
                snippet = verified.snippet or ""
                name = target.rsplit(".", 1)[-1]
                if (re.search(rf"\\b{re.escape(name)}\\s*\\(", snippet)
                        and not re.search(rf"\\bdef\\s+{re.escape(name)}\\s*\\(", snippet)):
                    yield verified

    # ── verify_callsite ────────────────────────────────────────────

    def _verify_callsite(self, target: str, wo: WorkOrder) -> Iterable[Evidence]:
        """读取候选调用点并验证调用表达式。"""
        file = wo.file_hint or ""
        line = wo.line or 0
        if not file or not line:
            resolved = self._resolver.resolve(target)
            if resolved:
                file = resolved.file
                line = resolved.line
        if not file or not line:
            return
        try:
            text = (self.repo_path / file).read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            return
        lines = text.splitlines()
        start, end = max(1, line - 5), min(len(lines), line + 5)
        snippet = "\n".join(f"{i}| {lines[i - 1]}" for i in range(start, end + 1))
        ev = Evidence(
            kind="code", source="verify_callsite",
            location=CodeLocation(file=file, start_line=line, end_line=end,
                                  symbol=target),
            snippet=snippet, confidence=1.0,
        )
        ev.set_deterministic_id("HEAD", file, line, end, snippet)
        yield ev

    # ── verify_callees ─────────────────────────────────────────────

    def _verify_callees(self, resolved: ResolvedSymbol,
                        caller_target: str,
                        state: ExplorationState | None = None,
                        expected_callee: str = "") -> Iterable[Evidence]:
        """AST 提取函数内部调用 → owner 标准化 → SymbolResolverV2 验证。

        从已定位的 ResolvedSymbol 出发，解析函数 AST 并逐条输出 VERIFIED_CALLEE_EDGE。
        只输出成功验证的 callee；未验证的直接丢弃。
        """
        if not resolved.is_valid() or not resolved.file:
            return

        # 1. 获取 AST
        tree = self._resolver.get_ast(resolved.file)
        if tree is None:
            return

        # 2. 确定类上下文
        class_name, base_classes = _get_class_context(tree, resolved.line)

        # 3. 收集调用
        calls = _collect_calls_in_function(
            tree, resolved.line, class_name, base_classes)

        total_extracted = len(calls)
        verified_count = 0

        # 4. 逐个验证
        for ci in calls:
            normalized = ci.get("callee_normalized", "")
            if not normalized:
                continue

            # A trace edge must prove this exact planned hop.  Do not allow a
            # different callee in the same function body to close the slot.
            if expected_callee and not _symbols_equivalent(normalized, expected_callee):
                continue

            # 尝试用 SymbolResolverV2 验证 callee 是否真实存在
            callee_resolved = self._resolver.resolve(normalized)
            if callee_resolved is None:
                continue

            verified_count += 1
            callsite_line = ci["line"]
            snippet = f"{caller_target} → {normalized}() at {resolved.file}:{callsite_line}"
            ev = Evidence(
                kind="code", source="verify_callees",
                location=CodeLocation(file=resolved.file,
                                      start_line=callsite_line,
                                      end_line=callsite_line,
                                      symbol=normalized),
                snippet=snippet, confidence=1.0,
            )
            ev.set_deterministic_id("HEAD", resolved.file, callsite_line,
                                     callsite_line, snippet)
            yield ev

        # 5. 记录统计
        if state is not None:
            state.traces.append(
                f"verify_callees: {total_extracted} extracted, "
                f"{verified_count} verified for {caller_target}"
                f" -> {expected_callee or '*'}")

    # ── 辅助 ───────────────────────────────────────────────────────

    def _read_snippet(self, file: str, line: int, before: int = 2,
                      after: int = 5) -> str:
        try:
            text = (self.repo_path / file).read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            return ""
        lines = text.splitlines()
        s = max(1, line - before)
        e = min(len(lines), line + after)
        return "\n".join(f"{i}| {lines[i - 1]}" for i in range(s, e + 1))


# ═══════════════════════════════════════════════════════════════════
# 工单填写 — fill_work_orders (Phase 2) + _deterministic_work_orders
# ═══════════════════════════════════════════════════════════════════

def fill_work_orders(task: InvestigationTask,
                     state: ExplorationState | None = None,
                     call_llm=None) -> list[WorkOrder]:
    """V24: Slot 驱动工单生成 — 每次只为一个未闭合 slot 生成一个 WorkOrder。

    1. 如果 task 有 required_slots（V24 新路径）：找出第一个未闭合的 slot，
       用 SLOT_TO_TOOL 生成 1 个 WorkOrder。
    2. 如果所有 slot 已闭合，返回空列表。
    没有 required_slots 的旧 Task 不再生成工单；V24 的调度契约是“工单必须绑定槽位”。
    """
    # V24: Slot 驱动路径
    required_slots = getattr(task, "required_slots", None)
    if required_slots:
        # 确定哪些 slot 已闭合（查看 state 中该 symbol 的 AnswerTarget）
        filled_slots: set = set()
        if state is not None:
            # 从 verified_evidence 快速判断 — 检查该 task 对应的已验证证据
            verified_for_task = state.verified_evidence.get(task.id, [])
            for ev in verified_for_task:
                src = getattr(ev, "source", "")
                if src == "resolve_symbol":
                    filled_slots.add(SlotKind.DEFINITION)
                elif src == "read_window":
                    filled_slots.add(SlotKind.IMPLEMENTATION)
                elif src == "search_references":
                    filled_slots.add(SlotKind.CANDIDATE_REFERENCE)
                elif src == "verify_callsite":
                    filled_slots.add(SlotKind.VERIFIED_CALLER_EDGE)
                elif src == "verify_callees":
                    filled_slots.add(SlotKind.VERIFIED_CALLEE_EDGE)

        # 找第一个未闭合的 slot
        for slot in (SlotKind.DEFINITION, SlotKind.IMPLEMENTATION,
                     SlotKind.VERIFIED_CALLER_EDGE, SlotKind.VERIFIED_CALLEE_EDGE,
                     SlotKind.CANDIDATE_REFERENCE):
            if slot in required_slots and slot not in filled_slots:
                tool_hint, search_kind = SLOT_TO_TOOL.get(
                    slot, ("resolve_symbol", "definition"))
                return [WorkOrder(
                    task_id=task.id,
                    description=(
                        f"prove directed edge {task.target} -> {task.counterpart}"
                        if slot is SlotKind.VERIFIED_CALLEE_EDGE and task.counterpart
                        else f"{slot.value} for {task.target}"
                    ),
                    target=task.target,
                    tool_hint=tool_hint,
                    search_kind=search_kind,
                    relation_id=task.relation_id,
                    counterpart=task.counterpart,
                    required_claims=list(task.required_claims),
                )]
        # 所有 slot 已闭合
        return []

    return []



def _deterministic_work_orders(task: InvestigationTask) -> list[WorkOrder]:
    """Gap task 使用 strategy_override 生成替代工单。

    与 fill_work_orders 的关键区别：
    - 使用不同的 tool（strategy_override.preferred_tool）
    - 可能扩大 scope（scope_override="allow_all"）
    - 不经过 LLM
    """
    gs = task.strategy_override
    if not gs:
        return fill_work_orders(task)

    orders: list[WorkOrder] = []
    orders.append(WorkOrder(
        task_id=task.id,
        description=f"gap: {task.target} (strategy={gs.preferred_tool})",
        target=task.target,
        tool_hint=gs.preferred_tool,
        search_kind=gs.search_kind,
        file_hint=gs.file_hint,
    ))
    return orders


# ═══════════════════════════════════════════════════════════════════
# 动态任务发现 — discover_tasks (Phase 2, 仅从 verified evidence)
# ═══════════════════════════════════════════════════════════════════

def discover_tasks(verified_evidence: list[Evidence],
                   parent_task: InvestigationTask,
                   state: ExplorationState) -> list[InvestigationTask]:
    """从已验证证据中发现新的调查任务（V24: slot 驱动）。

    规则驱动（不依赖 LLM）：
    - read_window 中发现的调用 → DEFINITION slot 任务
    - verify_callees 中提取的被调用者 → DEFINITION slot 任务

    仅使用 verified evidence，确保发现链可信。
    """
    # Dynamic discovery is only useful when the answer itself needs a graph
    # expansion.  For locate/explain/compare/grep it turns ordinary local
    # calls (``repr``, ``get``, ``ValueError``...) into budget-consuming
    # auxiliary roots without closing an answer requirement.
    if state.question_type not in {"trace", "impact"}:
        state.traces.append(
            f"discovery skipped: question_type={state.question_type} is not graph expansion")
        return []

    new_tasks: list[InvestigationTask] = []
    discovered_symbols: set[str] = set()

    for ev in verified_evidence:
        snippet = ev.snippet or ""

        if ev.source == "verify_callees":
            callee = (ev.location.symbol or "").strip() if ev.location else ""
            if callee and callee not in discovered_symbols and callee != parent_task.target:
                discovered_symbols.add(callee)
                if len(new_tasks) < 3:
                    new_tasks.append(InvestigationTask(
                        id=f"discovered_{len(state.all_tasks) + len(new_tasks) + 1:03d}",
                        target=callee,
                        required_slots={SlotKind.DEFINITION},
                        concept=f"从 {parent_task.target} 调用关系中发现的符号",
                        role=TaskRole.AUXILIARY,
                        parent_task_id=parent_task.id,
                        discovered_by=parent_task.id,
                    ))

        elif ev.source == "read_window":
            # 从 read_window 文本窗口提取被调用函数
            calls = set(re.findall(r"\b([\w.]+)\s*\(", snippet))
            _non_callee = {
                "print", "len", "range", "isinstance", "hasattr", "getattr",
                "setattr", "super", "int", "str", "list", "dict", "set", "tuple",
                "bool", "float", "type", "object", "enumerate", "zip", "map",
                "filter", "sorted", "reversed", "any", "all", "min", "max",
                "sum", "abs", "round", "open", "input", "format", "join", "split",
                "replace", "strip", "append", "extend", "get", "keys", "values",
                "items", "update", "pop", "copy", "clear", "read", "write",
                "close", "self", "cls", "if", "for", "while", "return", "yield",
                "assert", "raise", "is", "not", "in", "and", "or",
            }
            calls -= _non_callee
            # Filter to project-internal symbols (PascalCase or dotted)
            for c in calls:
                if re.match(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$", c):
                    if c not in discovered_symbols and c != parent_task.target:
                        discovered_symbols.add(c)
                        if len(new_tasks) < 3:  # limit discovery
                            new_tasks.append(InvestigationTask(
                                id=f"discovered_{len(state.all_tasks) + len(new_tasks) + 1:03d}",
                                target=c,
                                required_slots={SlotKind.DEFINITION},
                                concept=f"从 {parent_task.target} 实现中发现的符号",
                                role=TaskRole.AUXILIARY,
                                parent_task_id=parent_task.id,
                                discovered_by=parent_task.id,
                            ))

    return new_tasks


# ═══════════════════════════════════════════════════════════════════
# Gap 分析 — gap_analyzer (Phase 5) + _deterministic_gap_fill
# ═══════════════════════════════════════════════════════════════════

# 确定性补缺策略表
_GAP_STRATEGIES: dict[SlotKind, GapStrategy] = {
    SlotKind.DEFINITION: GapStrategy(
        preferred_tool="search_references", search_kind="definition",
        scope_override="allow_all"),
    SlotKind.IMPLEMENTATION: GapStrategy(
        preferred_tool="read_window", search_kind="definition"),
    SlotKind.VERIFIED_CALLER_EDGE: GapStrategy(
        preferred_tool="verify_callers", search_kind="callers",
        scope_override="allow_all"),
    SlotKind.VERIFIED_CALLEE_EDGE: GapStrategy(
        preferred_tool="verify_callees"),
    SlotKind.CANDIDATE_REFERENCE: GapStrategy(
        preferred_tool="search_references", search_kind="references",
        scope_override="allow_all"),
}


def gap_analyzer(question: str, state: ExplorationState,
                 call_llm=None, draft_answer: str = "") -> dict:
    """LLM 分析当前证据缺口，决定是否需要一次 retool。

    返回 {"action": "add_one_task"|"done", "new_task": {...}|None, "reason": str}
    """
    # 默认：如果没有 LLM，返回 done
    if call_llm is None:
        return {"action": "done", "new_task": None, "reason": "no LLM available"}

    # 构建能让审阅器判断“草稿是否真缺证据”的最小证据清单。这里给的是
    # 已验证 packet（证据包），不是完整工作流，也不允许模型自由决定工具。
    verified_count = sum(len(v) for v in state.verified_evidence.values())
    tasks_summary = ", ".join(
        f"{t.id}:{','.join(sorted(s.value for s in t.required_slots))}:{t.target}"
        for t in state.all_tasks[:10]
    )
    evidence_summary = "\n".join(
        f"- {ev.id}: {ev.source} {ev.location.file if ev.location else ''}"
        f":{ev.location.start_line if ev.location else ''} "
        f"{(ev.location.symbol if ev.location else '')} "
        f"{(ev.snippet or '').replace(chr(10), ' ')[:180]}"
        for ev in list(state.all_evidence.values())[:16]
    ) or "（无）"

    draft_section = (
        f"\n草稿答案（审阅其是否漏答，而不是复述它）：\n{draft_answer}\n"
        if draft_answer else ""
    )
    prompt = (
        f"问题：{question}\n"
        f"问题类型：{state.question_type}\n"
        f"已执行任务：{tasks_summary}\n"
        f"已验证证据：{verified_count} 条\n"
        f"证据摘要：\n{evidence_summary}\n"
        f"任务数：{len(state.all_tasks)}\n"
        f"必答 claims：{state.required_claims}\n"
        f"{draft_section}\n"
        f"审阅草稿与必答 claims。如果草稿遗漏且现有证据不能支持该 claim，"
        f"只建议一个最重要、最小、可由既有工具验证的补充任务；否则回答 done。"
        f"如果没有明显缺口，回答 done。\n"
        f"输出 JSON: {{\"action\": \"add_one_task\"|\"done\", "
        f"\"target\": \"<symbol>\", "
        f"\"slot\": \"definition|implementation|verified_caller_edge|verified_callee_edge|candidate_reference\", "
        f"\"counterpart\": \"<仅有向 callee 边的目标，可空>\", "
        f"\"reason\": \"<缺失的 claim 和原因>\"}}"
    )

    try:
        import json as _json
        raw = call_llm(
            prompt,
            system="你是代码调查证据缺口分析器。只建议一个最重要的补充任务。",
            temperature=0, max_tokens=300, **_LLM_CALL_KWARGS,
        )
        if isinstance(raw, str) and raw.strip():
            raw = raw.strip()
            try:
                data = _json.loads(raw)
            except _json.JSONDecodeError:
                m = re.search(r"\{[\s\S]*\}", raw)
                if m:
                    data = _json.loads(m.group(0))
                else:
                    return {"action": "done", "new_task": None,
                            "reason": "JSON parse failed"}
            if data.get("action") != "add_one_task":
                return {"action": "done", "new_task": None,
                        "reason": data.get("reason", "")}

            # LLM 只有“提交一张已限定类型的工单”的权限。字段不完整、
            # slot 不在枚举内，或边任务缺少另一端时，一律拒绝而非猜测执行。
            target = data.get("target")
            try:
                slot = SlotKind(data.get("slot"))
            except (TypeError, ValueError):
                return {"action": "done", "new_task": None,
                        "reason": "invalid retool slot"}
            if not isinstance(target, str) or not target.strip():
                return {"action": "done", "new_task": None,
                        "reason": "missing retool target"}
            if slot == SlotKind.VERIFIED_CALLEE_EDGE:
                counterpart = data.get("counterpart")
                if not isinstance(counterpart, str) or not counterpart.strip():
                    return {"action": "done", "new_task": None,
                            "reason": "callee edge needs counterpart"}
            return {"action": "add_one_task", "new_task": data,
                    "reason": data.get("reason", "")}
    except Exception:
        pass

    return {"action": "done", "new_task": None, "reason": "LLM error"}


def _deterministic_gap_fill(targets: dict[str, AnswerTarget],
                             state: ExplorationState) -> list[InvestigationTask]:
    """从合同缺口确定性生成补缺任务，带 strategy_override。

    1. 结构槽位缺口（open_slots）→ 对应 tool + strategy
    2. 内容 claims 缺口（uncovered_claims）→ find_literal_usage 搜索关键词
    """
    gap_tasks: list[InvestigationTask] = []
    idx = len(state.all_tasks)

    # 结构槽位缺口
    for tid, target in targets.items():
        open_slots = target.open_slots()
        for slot in open_slots:
            strategy = _GAP_STRATEGIES.get(slot)
            if not strategy:
                continue
            idx += 1
            gap_tasks.append(InvestigationTask(
                id=f"gap_{idx:03d}",
                target=target.symbol,
                required_slots={slot},
                concept=f"补缺: {slot.value} for {target.symbol}",
                role=TaskRole.GAP,
                subtree_depth=0,
                strategy_override=strategy,
            ))

    # A natural-language claim is not a safe search query. The former
    # fallback turned every uncovered claim into broad keyword searches and
    # exhausted GAP before a useful relation/slot could be verified. Claim
    # deficits proceed to the one schema-validated RETOOL task instead;
    # deterministic GAP remains structural-only.
    # Direct graph evidence has materially higher value than re-reading an
    # already located symbol or broad reference search.  The caller executes
    # this order incrementally and may stop after the first useful result.
    slot_priority = {
        SlotKind.VERIFIED_CALLEE_EDGE: 0,
        SlotKind.VERIFIED_CALLER_EDGE: 0,
        SlotKind.IMPLEMENTATION: 1,
        SlotKind.DEFINITION: 2,
        SlotKind.CANDIDATE_REFERENCE: 3,
    }
    gap_tasks.sort(key=lambda t: slot_priority.get(next(iter(t.required_slots)), 9))
    return gap_tasks[:3]


def _build_retool_task(gap_result: dict, state: ExplorationState) -> InvestigationTask:
    """从 gap_analyzer 结果构建 retool task。"""
    data = gap_result.get("new_task", {})
    idx = len(state.all_tasks) + 1
    # gap_analyzer 已完成 schema 校验；保留此 fallback 只为直接调用该 helper
    # 的旧调用方，正常控制流绝不把无效 LLM 输出降级成猜测性 read。
    try:
        slots = {SlotKind(data.get("slot", ""))}
    except (TypeError, ValueError):
        slots = {SlotKind.DEFINITION, SlotKind.IMPLEMENTATION}
    return InvestigationTask(
        id=f"retool_{idx:03d}",
        target=data.get("target", ""),
        required_slots=slots,
        concept=data.get("reason", "LLM retool"),
        role=TaskRole.RETOOL,
        subtree_depth=0,
        counterpart=data.get("counterpart", ""),
        required_claims=[data.get("reason", "")] if data.get("reason") else [],
    )


# ═══════════════════════════════════════════════════════════════════
# 执行函数 — _execute_task + _execute_task_subtree
# ═══════════════════════════════════════════════════════════════════


def _execute_verify_callees_with_resolved(
    tool_executor: ToolExecutor,
    wo: WorkOrder,
    verified_this_task: list[Evidence],
    task: InvestigationTask,
    state: ExplorationState,
) -> list[Evidence]:
    """从前序 resolve_symbol evidence 提取 ResolvedSymbol，调用 verify_callees。"""
    # 优先使用本次动作的定位结果；重入队后则从该 task 的证据池复用。
    resolved_ev = None
    for ev in verified_this_task:
        if ev.source == "resolve_symbol" and ev.location:
            resolved_ev = ev
            break

    if resolved_ev is None:
        for ev in state.verified_evidence.get(task.id, []):
            if ev.source == "resolve_symbol" and ev.location:
                resolved_ev = ev
                break

    if resolved_ev is None:
        state.traces.append(
            f"verify_callees skipped: no resolve_symbol evidence for {task.target}")
        return []

    # 构造 ResolvedSymbol
    resolved = ResolvedSymbol(
        requested_name=wo.target,
        canonical_name=resolved_ev.location.symbol or wo.target,
        file=resolved_ev.location.file,
        line=resolved_ev.location.start_line,
        end_line=resolved_ev.location.end_line,
        kind="function",
        owner=resolved_ev.location.symbol.rsplit(".", 1)[-2] if resolved_ev.location.symbol and "." in resolved_ev.location.symbol else "",
    )

    return list(tool_executor._verify_callees(
        resolved, wo.target, state, expected_callee=wo.counterpart))


def _execute_task(task: InvestigationTask, state: ExplorationState, *,
                  allow_children: bool = True,
                  tool_executor: ToolExecutor | None = None) -> None:
    """Phase 2: 执行一个 Task 的工单链。子任务入全局队列，不在此函数内递归。

    Args:
        task: 要执行的任务
        state: 全局探索状态
        allow_children: 是否允许从 verified evidence 发现新子任务
        tool_executor: 工具执行器
    """
    if tool_executor is None:
        tool_executor = ToolExecutor(state.repo_path)

    task.attempt_count += 1

    if task.strategy_override:
        orders = _deterministic_work_orders(task)
    else:
        orders = fill_work_orders(task, state)
        orders = orders[:MAX_ORDERS_PER_TASK]

    verified_this_task: list[Evidence] = []

    for wo in orders:
        if not state.consume_budget(state.current_phase, 1):
            state.traces.append(f"budget: {state.current_phase} exhausted for {task.id}")
            state.steps.append({
                "tool": "budget_exhausted",
                "target": task.target,
                "action": state.current_phase,
                "outcome": f"budget exhausted in phase {state.current_phase}",
                "status": "budget_exhausted",
                "params": {"task_id": task.id, "phase": state.current_phase},
                "evidence_count": 0,
            })
            break

        try:
            # verify_callees: 从前序 resolve_symbol evidence 构造 ResolvedSymbol
            if wo.tool_hint == "verify_callees":
                raw_evidence = _execute_verify_callees_with_resolved(
                    tool_executor, wo, verified_this_task, task, state)
            else:
                raw_evidence = tool_executor.execute(wo, state.question_type)
            tool_error = ""
        except Exception as exc:
            err_msg = f"{wo.tool_hint}:{wo.target}:{exc!r}"
            state.traces.append(f"tool_error: {err_msg}")
            raw_evidence = []
            tool_error = err_msg

        for ev in raw_evidence:
            # EvidenceVerifier 验证
            ok, reason = _verify_evidence(ev, task, wo)
            if ok:
                state.add_verified_evidence(ev, task.id)
                verified_this_task.append(ev)
            else:
                state.add_candidate_evidence(ev, task.id, reason)

        state.record_step(wo, raw_evidence, error=tool_error)

    if allow_children and verified_this_task:
        new_children = discover_tasks(verified_this_task, task, state)
        for child in new_children:
            child.role = TaskRole.AUXILIARY
            child.subtree_depth = task.subtree_depth + 1
            child.parent_task_id = task.id
            child.discovered_by = task.id
            if not state.is_duplicate(child):
                state.enqueue_tasks([child])

    # V24: Slot 驱动 — 检查是否还有未闭合 slot，若有则重新入队
    required_slots = getattr(task, "required_slots", None)
    if required_slots and verified_this_task:
        # 有已验证证据说明至少一个 slot 被填充了，检查是否还有缺口
        remaining_orders = fill_work_orders(task, state)
        if remaining_orders:
            # 还有未闭合 slot → 重新入队，不标记完成
            state.pending_tasks.append(task)
            state.traces.append(
                f"re-enqueue {task.id}: {len(required_slots)} slots required, "
                f"{len(remaining_orders)} orders remain")
            return

    state.mark_completed(task, _task_status(task, verified_this_task))


def _execute_task_subtree(root_task: InvestigationTask, state: ExplorationState, *,
                          max_depth: int = 2, budget_phase: str = "GAP",
                          tool_executor: ToolExecutor | None = None) -> None:
    """Phase 4/5: 执行一个 task 及其确定性子任务，直到子树闭合或 phase 预算耗尽。

    与 _execute_task 的关键区别：
    - 子任务在本地 queue 中立即执行（不回到全局队列）
    - 使用独立 phase 预算
    - 子树深度用 subtree_depth 相对计数
    """
    if tool_executor is None:
        tool_executor = ToolExecutor(state.repo_path)

    local_queue: list[InvestigationTask] = [root_task]
    prev_phase = state.current_phase
    state.current_phase = budget_phase

    while local_queue:
        task = local_queue.pop(0)
        task.attempt_count += 1

        if task.strategy_override:
            orders = _deterministic_work_orders(task)
        else:
            orders = fill_work_orders(task, state)
            orders = orders[:MAX_ORDERS_PER_TASK]

        verified_this_task: list[Evidence] = []

        for wo in orders:
            if not state.consume_budget(budget_phase, 1):
                state.traces.append(
                    f"budget: {budget_phase} exhausted for subtree {root_task.id}")
                state.steps.append({
                    "tool": "budget_exhausted",
                    "target": task.target,
                    "action": budget_phase,
                    "outcome": f"budget exhausted in phase {budget_phase}",
                    "status": "budget_exhausted",
                    "params": {"task_id": task.id, "phase": budget_phase},
                    "evidence_count": 0,
                })
                break

            try:
                if wo.tool_hint == "verify_callees":
                    raw_evidence = _execute_verify_callees_with_resolved(
                        tool_executor, wo, verified_this_task, task, state)
                else:
                    raw_evidence = tool_executor.execute(wo, state.question_type)
                tool_error = ""
            except Exception as exc:
                err_msg = f"{wo.tool_hint}:{wo.target}:{exc!r}"
                state.traces.append(f"tool_error: {err_msg}")
                raw_evidence = []
                tool_error = err_msg

            for ev in raw_evidence:
                ok, reason = _verify_evidence(ev, task, wo)
                if ok:
                    state.add_verified_evidence(ev, task.id)
                    verified_this_task.append(ev)
                else:
                    state.add_candidate_evidence(ev, task.id, reason)

            state.record_step(wo, raw_evidence, error=tool_error)

        new_children = discover_tasks(verified_this_task, task, state)
        for child in new_children:
            child.role = TaskRole.AUXILIARY
            child.subtree_depth = task.subtree_depth + 1
            child.parent_task_id = task.id
            child.discovered_by = task.id
            if child.subtree_depth <= max_depth and not state.is_duplicate(child):
                state.register_task(child)
                local_queue.append(child)

        # 子树也需要遍历同一 task 的剩余槽位，否则 retool 任务只会完成第一步。
        if task.required_slots and verified_this_task and fill_work_orders(task, state):
            local_queue.insert(0, task)
            continue

        state.mark_completed(task, _task_status(task, verified_this_task))

    state.current_phase = prev_phase


def _verify_evidence(ev: Evidence, task: InvestigationTask,
                     wo: WorkOrder) -> tuple[bool, str]:
    """根据 task 类型和 work order 验证 evidence。

    简化版验证：只要 evidence 有内容就通过。
    更严格的验证由 EvidenceVerifier 的静态方法处理。
    """
    if not ev or not ev.snippet:
        return False, "empty evidence"
    if not ev.location or not ev.location.file:
        return False, "no location"

    # 基本验证通过
    source = ev.source
    if source == "resolve_symbol":
        snippet = (ev.snippet or "").lower()
        name = wo.target.rsplit(".", 1)[-1].lower()
        if re.search(rf"\b(?:class|def)\s+{re.escape(name)}\b", snippet):
            return True, f"verified definition of {wo.target}"
        return True, f"resolved {wo.target}"  # resolve result always accepted

    if source == "read_window":
        return True, "read window"

    if source == "search_references":
        return True, "reference found"

    if source == "verify_callsite":
        snippet = ev.snippet or ""
        name = wo.target.rsplit(".", 1)[-1]
        if re.search(rf"\b{re.escape(name)}\s*\(", snippet):
            if not re.search(rf"\bdef\s+{re.escape(name)}\s*\(", snippet):
                return True, f"verified call to {wo.target}"
        return False, f"no call expression for {wo.target}"

    if source == "verify_callees":
        # 新格式：每条 evidence 代表一个已验证的调用关系
        # location.symbol 包含标准化的 callee 名称
        symbol = (ev.location.symbol or "").strip() if ev.location else ""
        if symbol:
            if wo.counterpart and not _symbols_equivalent(symbol, wo.counterpart):
                return False, (
                    f"verified {symbol}, expected directed callee {wo.counterpart}"
                )
            return True, f"verified callee: {symbol}"
        return False, "no callee symbol"

    return True, "accepted"


def _task_status(task: InvestigationTask,
                 verified_evidence: list[Evidence]) -> TaskStatus:
    """根据已验证证据确定任务状态。"""
    if verified_evidence:
        return TaskStatus.VERIFIED
    if task.attempt_count > 0:
        return TaskStatus.NO_EVIDENCE
    return TaskStatus.FAILED


# ═══════════════════════════════════════════════════════════════════
# 停止原因判定
# ═══════════════════════════════════════════════════════════════════

def _determine_stop_reason(state: ExplorationState, contract_met: bool) -> str:
    """根据最终状态确定停止原因。"""
    if contract_met:
        if state.retool_used:
            return "COMPLETE_AFTER_RETOOL"
        return "COMPLETE"
    if state.main_steps_used >= state.max_main_steps:
        return "STOP_MAIN_BUDGET"
    if not state.has_pending():
        return "STOP_NO_PENDING"
    return "PARTIAL"
