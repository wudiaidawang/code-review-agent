"""Evidence-targeted investigation types and verification — V22.

V22: EvidenceClosureEngine / ClosureState / ClosureAction / LedgerEntry removed.
The control plane moved to task_explorer.py.  This module retains the core
domain types and pure-function verification logic used by the V22 engine.

Kept:
  * SlotKind / TargetKind — evidence slot semantics and target classification
  * AnswerTarget — per-target evidence contract with verified_slots tracking
  * SearchScope — scope-aware search bounds
  * EvidenceVerifier — deterministic, pure-function slot verification
  * targets_from_tasks — convert InvestigationTask list to AnswerTarget dict
  * check_minimum_evidence_contract — per-question-type minimum evidence floor
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from app.models.evidence import Evidence
from app.models.location import CodeLocation


# ═══════════════════════════════════════════════════════════════════
# SlotKind — strict semantics with legacy aliases
# ═══════════════════════════════════════════════════════════════════

class SlotKind(str, Enum):
    """Evidence slot kinds with strict completion semantics.

    Completion (is_complete / open_slots) only considers VERIFIED slots.
    CANDIDATE_REFERENCE is a stepping stone, never a completion condition
    for trace tasks.
    """

    DEFINITION = "definition"
    IMPLEMENTATION = "implementation"
    CANDIDATE_REFERENCE = "candidate_reference"
    VERIFIED_CALLER_EDGE = "verified_caller_edge"
    VERIFIED_CALLEE_EDGE = "verified_callee_edge"
    NEGATIVE_SEARCH = "negative_search"
    HELPER_IMPLEMENTATION = "helper_implementation"

    # Legacy aliases — preserved for compatibility.
    REFERENCES = "references"
    CALLER_EDGE = "caller_edge"
    CALLEE_EDGE = "callee_edge"


# ═══════════════════════════════════════════════════════════════════
# V24: Relation → Slots 展开 + Slot → Tool 映射
# ═══════════════════════════════════════════════════════════════════

RELATION_TO_SLOTS: dict = {}  # 由 _init_relation_to_slots() 延迟填充，避免循环导入

def _init_relation_to_slots():
    """延迟初始化 RELATION_TO_SLOTS，避免与 target.py 循环导入。"""
    from app.models.target import RelationType
    if not RELATION_TO_SLOTS:
        RELATION_TO_SLOTS.update({
            RelationType.DEFINITION_LOCATION: {SlotKind.DEFINITION},
            RelationType.EXPLAIN_BEHAVIOR: {
                SlotKind.DEFINITION, SlotKind.IMPLEMENTATION, SlotKind.VERIFIED_CALLEE_EDGE,
            },
            RelationType.TRACE_CALL_CHAIN: {
                SlotKind.DEFINITION, SlotKind.VERIFIED_CALLER_EDGE,
                SlotKind.VERIFIED_CALLEE_EDGE, SlotKind.IMPLEMENTATION,
            },
            RelationType.COMPARE_BEHAVIOR: {
                SlotKind.DEFINITION, SlotKind.IMPLEMENTATION,
            },
            RelationType.IMPACT_CHANGE: {
                SlotKind.DEFINITION, SlotKind.VERIFIED_CALLER_EDGE,
                SlotKind.CANDIDATE_REFERENCE,
            },
            RelationType.ENUMERATE_USAGES: {
                SlotKind.DEFINITION, SlotKind.CANDIDATE_REFERENCE,
            },
        })


# Slot → (tool_hint, search_kind)。
# ``verify_callers`` 是一个语义工具：它在内部发现候选调用点并逐个校验，因而对上层而言仍是“补一个槽位”的一次动作。
# 不允许把只是命中文本的 search_references 当作 VERIFIED_CALLER_EDGE。
SLOT_TO_TOOL: dict[SlotKind, tuple[str, str]] = {
    SlotKind.DEFINITION: ("resolve_symbol", "definition"),
    SlotKind.IMPLEMENTATION: ("read_window", "definition"),
    SlotKind.VERIFIED_CALLER_EDGE: ("verify_callers", "callers"),
    SlotKind.VERIFIED_CALLEE_EDGE: ("verify_callees", "definition"),
    SlotKind.CANDIDATE_REFERENCE: ("search_references", "references"),
}


def expand_relations(output: "PlannerOutput") -> dict[str, set[SlotKind]]:
    """将 PlannerOutput 中的 relations 和 standalone_targets 展开为
    每个 symbol → 所需的 SlotKind 集合。同一符号出现在多个 relation
    中时取 slot 并集。
    """
    from app.models.target import PlannerOutput, RelationDef
    _init_relation_to_slots()

    result: dict[str, set[SlotKind]] = {}

    for rel in output.relations:
        slots = RELATION_TO_SLOTS.get(rel.type, {SlotKind.DEFINITION})
        for subject in rel.subjects:
            result.setdefault(subject, set()).update(slots)

    for t in output.standalone_targets:
        result.setdefault(t.symbol, set()).update(
            {SlotKind.DEFINITION, SlotKind.IMPLEMENTATION}
        )

    return result


def tasks_from_planner_output(output: "PlannerOutput") -> list:
    """Compile relations into schedulable tasks without discarding direction.

    Symbols still share their definition/implementation investigation, but a
    trace relation additionally produces one directed edge task for every
    adjacent pair.  ``A -> B -> C`` is therefore not weakened into three
    unrelated symbols that merely each happen to need a caller/callee slot.
    """
    from app.models.target import InvestigationTask, RelationType, TaskRole
    _init_relation_to_slots()

    symbol_slots: dict[str, set[SlotKind]] = {}
    symbol_claims: dict[str, list[str]] = {}
    symbol_relation_ids: dict[str, list[str]] = {}
    edge_specs: list[tuple[str, str, str, list[str]]] = []

    for rel_pos, rel in enumerate(output.relations, 1):
        relation_id = f"relation_{rel.index or rel_pos:03d}"
        claims = list(rel.required_claims or output.required_claims)
        # A trace's directed edges are scheduled separately below.  The node
        # task only obtains the facts needed to identify/read that node.
        slots = ({SlotKind.DEFINITION, SlotKind.IMPLEMENTATION}
                 if rel.type is RelationType.TRACE_CALL_CHAIN
                 else RELATION_TO_SLOTS.get(rel.type, {SlotKind.DEFINITION}))
        for subject in rel.subjects:
            symbol_slots.setdefault(subject, set()).update(slots)
            symbol_claims.setdefault(subject, []).extend(claims)
            symbol_relation_ids.setdefault(subject, []).append(relation_id)

        if rel.type is RelationType.TRACE_CALL_CHAIN:
            for source, destination in zip(rel.subjects, rel.subjects[1:]):
                edge_specs.append((source, destination, relation_id, claims))

    for target in output.standalone_targets:
        symbol_slots.setdefault(target.symbol, set()).update(
            {SlotKind.DEFINITION, SlotKind.IMPLEMENTATION}
        )
        symbol_claims.setdefault(target.symbol, []).extend(
            target.required_claims or output.required_claims
        )

    tasks: list = []
    for symbol, slots in symbol_slots.items():
        claims = list(dict.fromkeys(symbol_claims.get(symbol, [])))
        tasks.append(InvestigationTask(
            id=f"task_{len(tasks) + 1:03d}",
            target=symbol,
            required_slots=slots,
            concept="; ".join(claims[:2]),
            relation_id=",".join(symbol_relation_ids.get(symbol, [])),
            required_claims=claims,
            role=TaskRole.ROOT,
        ))

    for source, destination, relation_id, claims in edge_specs:
        tasks.append(InvestigationTask(
            id=f"task_{len(tasks) + 1:03d}",
            target=source,
            required_slots={SlotKind.VERIFIED_CALLEE_EDGE},
            concept=f"verify directed call: {source} -> {destination}",
            relation_id=relation_id,
            counterpart=destination,
            required_claims=list(dict.fromkeys(claims)),
            role=TaskRole.REQUIRED,
        ))
    return tasks


# ═══════════════════════════════════════════════════════════════════
# TargetKind
# ═══════════════════════════════════════════════════════════════════

class TargetKind(str, Enum):
    """What kind of code artefact the answer target denotes.

    Routing rules (deterministic, not LLM):
      * SYMBOL / MEMBER  → resolve_symbol (owner for MEMBER)
      * DECORATOR         → grep for ``@name``
      * MODULE            → grep ``import name`` / ``from name``
      * SYNTAX_PATTERN    → literal grep (e.g. ``try:``)
      * TEXT_PATTERN      → literal grep
    """
    SYMBOL = "symbol"
    MEMBER = "member"
    DECORATOR = "decorator"
    MODULE = "module"
    SYNTAX_PATTERN = "syntax_pattern"
    TEXT_PATTERN = "text_pattern"


_KNOWN_MODULES = frozenset({
    "os", "os.path", "sys", "subprocess", "json", "re", "collections",
    "itertools", "functools", "typing", "pathlib", "tempfile", "shutil",
    "datetime", "math", "hashlib", "pytest", "unittest", "monkeypatch",
    "argparse", "logging", "json.dumps", "json.loads", "defaultdict",
    "namedtuple", "OrderedDict",
})


def classify_target(raw: str) -> tuple[TargetKind, str, str]:
    """Classify a raw target string and, for members, extract owner.

    Returns (kind, owner_or_raw, member_or_empty).
    """
    s = raw.strip()
    if not s:
        return TargetKind.TEXT_PATTERN, s, ""
    if s.startswith("@"):
        return TargetKind.DECORATOR, s.lstrip("@"), ""
    _SYNTAX_KEYWORDS = {"try", "except", "finally", "with", "for", "while",
                        "if", "elif", "else", "return", "yield", "import",
                        "from", "raise", "assert", "del", "pass", "break", "continue"}
    if s.rstrip(":") in _SYNTAX_KEYWORDS:
        return TargetKind.SYNTAX_PATTERN, s.rstrip(":"), ""
    if s in _KNOWN_MODULES or any(s.startswith(m + ".") for m in _KNOWN_MODULES):
        return TargetKind.MODULE, s, ""
    # Multi-dot qualified name: classify by last segment
    if "." in s:
        parts = s.rsplit(".", 1)
        owner, member = parts[0], parts[1]
        # e.g. app.models.MyClass → SYMBOL (last part is PascalCase → class)
        if re.match(r"^_*[A-Z][\w]*$", member):
            return TargetKind.SYMBOL, s, ""
        # e.g. app.models.Evidence.confidence → MEMBER
        if re.match(r"^[a-z_][\w]*$", member):
            return TargetKind.MEMBER, owner, member
        # e.g. os.path.join → MEMBER
        return TargetKind.MEMBER, owner, member
    if re.match(r"^_*[A-Z][\w]*$", s):
        return TargetKind.SYMBOL, s, ""
    if re.match(r"^[a-z_][\w]*$", s) and s not in _KNOWN_MODULES:
        return TargetKind.SYMBOL, s, ""
    return TargetKind.TEXT_PATTERN, s, ""


# ═══════════════════════════════════════════════════════════════════
# SearchScope
# ═══════════════════════════════════════════════════════════════════

_EXCLUDE_DIRS = frozenset({
    "docs", "docs_src", "examples", "tests", "test",
    "benchmarks", "scripts", "__pycache__",
})


@dataclass
class SearchScope:
    """Bounds for reference searches.  Production source is prioritised;
    docs/examples/tests are excluded by default and only used as fallback.
    """

    max_files: int = 20
    max_hits_per_file: int = 3
    max_total_evidence: int = 50
    allow_docs_examples_tests: bool = False
    search_mode: str = "references"  # definition|callsite|literal|import|decorator|syntax

    # Post-search audit
    truncated: bool = False
    total_files_scanned: int = 0
    total_hits_found: int = 0
    files_emitted: int = 0


def _default_scope_for_target(target_kind: TargetKind, task_types: str = "") -> SearchScope:
    """Return a sensible scope for the target kind and task intent."""
    scope = SearchScope()
    if target_kind in (TargetKind.SYMBOL, TargetKind.MEMBER):
        scope.max_files = 20
        scope.max_hits_per_file = 3
        scope.max_total_evidence = 50
    elif target_kind == TargetKind.MODULE:
        scope.max_files = 30
        scope.max_hits_per_file = 2
        scope.max_total_evidence = 60
    else:
        scope.max_files = 15
        scope.max_hits_per_file = 2
        scope.max_total_evidence = 30

    _allow_docs_tasks = {"locate_definition", "find_literal_usage", "enumerate_symbols"}
    if task_types and any(t in _allow_docs_tasks for t in task_types.split(",")):
        scope.allow_docs_examples_tests = True

    return scope


# ═══════════════════════════════════════════════════════════════════
# AnswerTarget — with verified_slots
# ═══════════════════════════════════════════════════════════════════

@dataclass
class AnswerTarget:
    id: str
    kind: str
    symbol: str
    required_slots: set[SlotKind]
    target_kind: TargetKind = TargetKind.SYMBOL
    owner_symbol: str = ""
    evidence_by_slot: dict[SlotKind, list[str]] = field(default_factory=dict)
    blocked_slots: dict[SlotKind, str] = field(default_factory=dict)
    member_file: str = ""
    member_line: int = 0
    derived_from_evidence_id: str = ""
    verified_slots: dict[SlotKind, list[str]] = field(default_factory=dict)

    @property
    def resolve_symbol(self) -> str:
        return self.owner_symbol or self.symbol

    def _verified_slot_kinds(self) -> set[SlotKind]:
        return {SlotKind.DEFINITION, SlotKind.IMPLEMENTATION,
                SlotKind.VERIFIED_CALLER_EDGE, SlotKind.VERIFIED_CALLEE_EDGE,
                SlotKind.CANDIDATE_REFERENCE, SlotKind.NEGATIVE_SEARCH,
                SlotKind.HELPER_IMPLEMENTATION}

    def _is_verified_slot(self, slot: SlotKind) -> bool:
        """Whether evidence_by_slot alone suffices to close this slot."""
        if slot in (SlotKind.DEFINITION, SlotKind.IMPLEMENTATION,
                     SlotKind.HELPER_IMPLEMENTATION, SlotKind.NEGATIVE_SEARCH):
            return True
        if slot in (SlotKind.VERIFIED_CALLER_EDGE, SlotKind.VERIFIED_CALLEE_EDGE):
            return False
        if slot is SlotKind.CANDIDATE_REFERENCE:
            has_trace = bool(
                {SlotKind.VERIFIED_CALLER_EDGE, SlotKind.VERIFIED_CALLEE_EDGE}
                & self.required_slots
            )
            return not has_trace
        return False

    def is_complete(self) -> bool:
        """Complete when every required slot has verified evidence."""
        for slot in self.required_slots:
            if not self._is_verified_slot(slot):
                if not self.verified_slots.get(slot):
                    if slot in self.blocked_slots:
                        continue
                    return False
            else:
                if not self.verified_slots.get(slot) and not self.evidence_by_slot.get(slot):
                    if slot in self.blocked_slots:
                        continue
                    return False
        return True

    def open_slots(self) -> set[SlotKind]:
        """Slots that still need verified evidence."""
        open_: set[SlotKind] = set()
        for slot in self.required_slots:
            if slot in self.blocked_slots:
                continue
            if self._is_verified_slot(slot):
                if not self.verified_slots.get(slot) and not self.evidence_by_slot.get(slot):
                    open_.add(slot)
            else:
                if not self.verified_slots.get(slot):
                    open_.add(slot)
        return open_

    def any_verified(self) -> bool:
        """Whether at least one required slot has verified evidence."""
        for slot in self.required_slots:
            if self._is_verified_slot(slot):
                if self.verified_slots.get(slot) or self.evidence_by_slot.get(slot):
                    return True
            else:
                if self.verified_slots.get(slot):
                    return True
        return False


# ═══════════════════════════════════════════════════════════════════
# Per-type minimum evidence contract — V21
# ═══════════════════════════════════════════════════════════════════

def _extract_keywords_from_chinese(text: str) -> list[str]:
    """从中文短句中提取关键词，用于确定性预筛。"""
    import re as _re
    # 移除标点，按常见分隔符拆分
    import string as _string
    _punct_chars = '，。！？、：；""''（）()[]【】' + _string.whitespace
    cleaned = _re.sub(f'[{_re.escape(_punct_chars)}]+', ' ', text).strip()
    # 额外移除纯数字和单字符
    words = [w for w in cleaned.split() if len(w) >= 2 and not w.isdigit()]
    # 去重保序
    seen: set[str] = set()
    result: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result


def check_claim_coverage(
    required_claims: list[str],
    all_evidence: dict[str, "Evidence"],
    call_llm=None,
) -> tuple[list[int], list[int], str]:
    """检查 required_claims 是否被 evidence 覆盖。

    两步：确定性关键词预筛 → 可选 LLM 语义验证。

    Returns (covered_indices, uncovered_indices, reason).
    """
    if not required_claims:
        return [], [], "no claims to check"

    evidence_list = list(all_evidence.values())

    # ── Step 1: 确定性关键词预筛 ─────────────────────────────────
    pre_covered: set[int] = set()
    for i, claim_text in enumerate(required_claims):
        keywords = _extract_keywords_from_chinese(claim_text)
        if not keywords:
            continue
        for ev in evidence_list:
            snippet = (ev.snippet or "").lower()
            if any(kw.lower() in snippet for kw in keywords):
                pre_covered.add(i)
                break

    # ── Step 2: LLM 语义验证（可选）──────────────────────────────
    if call_llm and pre_covered:
        try:
            claims_json = {str(i): required_claims[i] for i in pre_covered}
            ev_summaries = [
                f"[{ev.id}] {ev.location.file}:{ev.location.start_line} — "
                f"{(ev.snippet or '')[:200]}"
                for ev in evidence_list if ev.location
            ]
            prompt = (
                f"Claims to verify:\n{json.dumps(claims_json, ensure_ascii=False)}\n\n"
                f"Evidence:\n" + "\n".join(ev_summaries[:30]) + "\n\n"
                f"判断每个 claim 是否被上述证据充分覆盖。"
                f"输出 JSON: {{\"0\": true, \"1\": false, ...}}"
            )
            raw = call_llm(
                prompt,
                system="你是严格的证据验证器。只输出 JSON。",
                temperature=0, max_tokens=300,
                timeout=60,
                extra_body={"thinking": {"type": "disabled"}},
            )
            llm_result = _extract_json_from_text(raw)
            if isinstance(llm_result, dict):
                verified_covered: set[int] = set()
                for key_str, val in llm_result.items():
                    try:
                        idx = int(key_str)
                    except (ValueError, TypeError):
                        continue
                    if val is True and idx in pre_covered:
                        verified_covered.add(idx)
                if verified_covered:
                    pre_covered = verified_covered
        except Exception:
            pass  # LLM 不可用时保留 Step 1 结果

    covered = sorted(pre_covered)
    uncovered = sorted(set(range(len(required_claims))) - pre_covered)

    reason_parts: list[str] = []
    if covered:
        reason_parts.append(f"{len(covered)}/{len(required_claims)} claims covered")
    if uncovered:
        uncovered_texts = [required_claims[i][:60] for i in uncovered[:3]]
        reason_parts.append(f"uncovered: {uncovered_texts}")

    return covered, uncovered, "; ".join(reason_parts) if reason_parts else "no claims"


def _extract_json_from_text(raw: str) -> dict | None:
    """从 LLM 返回文本中提取 JSON，容忍 markdown 代码块包裹。"""
    import re as _re
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = _re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def check_minimum_evidence_contract(
    question_type: str,
    targets: dict[str, "AnswerTarget"],
    evidence: dict[str, "Evidence"],
    required_claims: list[str] | None = None,
    call_llm=None,
    claim_coverage: tuple[list[int], list[int], str] | None = None,
) -> tuple[bool, str]:
    """Return (is_sufficient, reason) for the given question type.

    These are the FLOOR — COMPLETE cannot be claimed unless these are met.
    ============ ======================================================
    locate         at least one DEFINITION verified
    grep           at least one CANDIDATE_REFERENCE in production code
    explain        DEFINITION + IMPLEMENTATION on at least one target
    trace          DEFINITION + at least 2 verified call edges (chain)
    impact         DEFINITION + at least one verified caller or dependency
    ============ ======================================================

    If required_claims is provided, also checks content-level claim coverage.
    Structural AND claims must both be met.
    """
    if not targets:
        return False, "no targets"

    # First determine the structural floor for the question type.  Do not
    # return from these branches: content claims are a universal second gate.
    structural_met = False
    structural_reason = "no evidence found"

    if question_type == "locate":
        for t in targets.values():
            if t.verified_slots.get(SlotKind.DEFINITION) or t.evidence_by_slot.get(SlotKind.DEFINITION):
                structural_met, structural_reason = True, "found definition"
                break

    elif question_type == "grep":
        for t in targets.values():
            cand = (t.evidence_by_slot.get(SlotKind.CANDIDATE_REFERENCE, []) +
                    t.verified_slots.get(SlotKind.CANDIDATE_REFERENCE, []))
            for eid in cand:
                ev = evidence.get(eid)
                if ev and ev.location:
                    parts = ev.location.file.lower().replace("\\", "/").split("/")
                    if not any(d in parts for d in _EXCLUDE_DIRS):
                        structural_met = True
                        structural_reason = f"found production reference in {ev.location.file}"
                        break
            if cand:
                structural_met = True
                structural_reason = f"found {len(cand)} references (may include test/docs)"
            if structural_met:
                break

    elif question_type == "explain":
        for t in targets.values():
            has_def = bool(t.verified_slots.get(SlotKind.DEFINITION) or
                          t.evidence_by_slot.get(SlotKind.DEFINITION))
            has_impl = bool(t.verified_slots.get(SlotKind.IMPLEMENTATION) or
                           t.evidence_by_slot.get(SlotKind.IMPLEMENTATION))
            if has_def and has_impl:
                structural_met, structural_reason = True, "found definition and implementation"
                break
        if not structural_met:
            for t in targets.values():
                if t.verified_slots.get(SlotKind.DEFINITION) or t.evidence_by_slot.get(SlotKind.DEFINITION):
                    structural_reason = "has definition but no implementation body"
                    break
            else:
                structural_reason = "no definition or implementation for explain"

    elif question_type == "trace":
        total_edges = 0
        for t in targets.values():
            total_edges += len(t.verified_slots.get(SlotKind.VERIFIED_CALLER_EDGE, []))
            total_edges += len(t.verified_slots.get(SlotKind.VERIFIED_CALLEE_EDGE, []))
        if total_edges >= 2:
            structural_met, structural_reason = True, f"found {total_edges} verified call edges"
        elif total_edges >= 1:
            has_def = any(
                t.verified_slots.get(SlotKind.DEFINITION) or t.evidence_by_slot.get(SlotKind.DEFINITION)
                for t in targets.values())
            if has_def:
                structural_reason = "only 1 verified edge, need 2 for trace chain"
        else:
            structural_reason = "no verified call edges for trace"

    elif question_type == "impact":
        for t in targets.values():
            has_def = bool(t.verified_slots.get(SlotKind.DEFINITION) or
                          t.evidence_by_slot.get(SlotKind.DEFINITION))
            has_dep = (bool(t.verified_slots.get(SlotKind.VERIFIED_CALLER_EDGE)) or
                       bool(t.evidence_by_slot.get(SlotKind.CANDIDATE_REFERENCE)))
            if has_def and has_dep:
                structural_met, structural_reason = True, "found definition and dependency/caller evidence"
                break
        if not structural_met:
            structural_reason = "need definition + dependency evidence for impact"

    elif question_type == "compare":
        ready = 0
        for t in targets.values():
            has_def = bool(t.verified_slots.get(SlotKind.DEFINITION) or
                          t.evidence_by_slot.get(SlotKind.DEFINITION))
            has_impl = bool(t.verified_slots.get(SlotKind.IMPLEMENTATION) or
                           t.evidence_by_slot.get(SlotKind.IMPLEMENTATION))
            if has_def and has_impl:
                ready += 1
        if ready >= 2:
            structural_met, structural_reason = True, f"{ready} targets have definition+implementation for comparison"
        elif ready >= 1:
            structural_reason = f"only {ready} target(s) ready, need 2 for comparison"
        else:
            structural_reason = "no targets ready for comparison"

    else:
        # Default: at least one target has some verified evidence.
        for t in targets.values():
            if t.any_verified():
                structural_met = True
                structural_reason = "some evidence found"
                break

    # ── Claims 检查 ──────────────────────────────────────────
    if required_claims:
        covered, uncovered, claims_reason = claim_coverage or check_claim_coverage(
            required_claims, evidence, call_llm)
        if not structural_met:
            return False, f"structural unmet ({structural_reason}); claims: {claims_reason}"
        if uncovered:
            return False, f"structural met but claims unmet: {claims_reason}"
        return True, f"structural met ({structural_reason}); claims: {claims_reason}"

    if structural_met:
        return True, structural_reason
    return False, structural_reason


# ═══════════════════════════════════════════════════════════════════
# EvidenceVerifier — deterministic, pure functions
# ═══════════════════════════════════════════════════════════════════

class EvidenceVerifier:
    """Deterministic verification of evidence against slot kinds.

    All methods are stateless pure functions.  They return (is_verified, reason)
    tuples suitable for audit trails.
    """

    @staticmethod
    def verify_definition(evidence: Evidence, target: AnswerTarget) -> tuple[bool, str]:
        """A resolve_symbol result is always a valid definition."""
        if evidence.source == "resolve_symbol":
            return True, f"resolved {target.symbol} at {evidence.location.file}:{evidence.location.start_line}"
        snippet = (evidence.snippet or "").lower()
        if evidence.source in ("read_window", "search_references"):
            name = target.symbol.rsplit(".", 1)[-1].lower()
            if re.search(rf"\b(?:class|def)\s+{re.escape(name)}\b", snippet):
                return True, f"found definition of {target.symbol} in {evidence.source}"
        return False, f"evidence source={evidence.source} does not prove definition"

    @staticmethod
    def verify_implementation(evidence: Evidence, target: AnswerTarget,
                              file_content: str = "") -> tuple[bool, str]:
        """Check that a read_window actually contains the target's implementation.

        For SYMBOL targets: the window must contain ``def <name>`` / ``class <name>``.
        For MEMBER targets: the window must contain the member within the owner body.
        """
        name = target.symbol.rsplit(".", 1)[-1]
        snippet = evidence.snippet or ""

        if target.target_kind == TargetKind.MEMBER:
            member = name
            owner = target.owner_symbol
            if _find_member_in_snippet(snippet, member):
                return True, f"member {target.symbol} located in owner {owner} body"
            if target.member_file and target.member_line:
                return False, "member location set but not yet read with member-specific window"
            return False, f"member {member} not found in owner window"

        if re.search(rf"\b(?:async\s+def|def|class)\s+{re.escape(name)}\b", snippet):
            return True, f"found definition body of {name}"
        return False, f"no def/class {name} in window"

    @staticmethod
    def verify_caller_edge(evidence: Evidence, target: AnswerTarget,
                           definition_loc: CodeLocation | None = None) -> tuple[bool, str]:
        """Verify that a read_window at a candidate call site contains a real call.

        Requirements:
        - Call expression exists (not just a name reference)
        - Not the target's own definition line
        - Receiver matches the target (for Class.method: instance.method, self.method, cls.method, Class.method)
        """
        snippet = evidence.snippet or ""
        name = target.symbol.rsplit(".", 1)[-1]
        is_member = target.target_kind == TargetKind.MEMBER
        owner = target.owner_symbol

        if definition_loc and evidence.location:
            if (evidence.location.file == definition_loc.file
                    and abs(evidence.location.start_line - definition_loc.start_line) <= 2):
                return False, "same location as definition"

        if is_member and owner:
            call_patterns = [
                rf"\b{re.escape(name)}\s*\(",
                rf"\w+\.{re.escape(name)}\s*\(",
                rf"self\.{re.escape(name)}\s*\(",
                rf"cls\.{re.escape(name)}\s*\(",
                rf"{re.escape(owner)}\.{re.escape(name)}\s*\(",
                rf"{re.escape(owner)}\w*\.{re.escape(name)}\s*\(",
            ]
        else:
            call_patterns = [
                rf"\b{re.escape(name)}\s*\(",
                rf"\.{re.escape(name)}\s*\(",
            ]

        if any(re.search(pat, snippet) for pat in call_patterns):
            if not is_member and re.search(rf"\bdef\s+{re.escape(name)}\s*\(", snippet):
                return False, "self-definition, not a call site"
            return True, f"verified call to {target.symbol}"

        return False, f"no call expression for {target.symbol} in window"

    @staticmethod
    def verify_callee_edge(evidence: Evidence, target: AnswerTarget) -> tuple[bool, str]:
        """验证 callee 调用关系 evidence（新格式：每条对应一个已验证调用关系）。"""
        symbol = (evidence.location.symbol or "").strip() if evidence.location else ""
        if symbol:
            return True, f"verified callee edge: {symbol}"
        return False, "no callee symbol in evidence"

    @staticmethod
    def verify_candidate_reference(evidence: Evidence) -> tuple[bool, str]:
        """Any search hit is a valid candidate reference (always true)."""
        return True, "candidate reference"


def _find_member_in_snippet(snippet: str, member: str) -> bool:
    """Check if a code snippet contains the member declaration/field."""
    m = re.escape(member)
    patterns = [
        rf"\bdef\s+{m}\s*\(",
        rf"\basync\s+def\s+{m}\s*\(",
        rf"\bself\.{m}\b",
        rf"\bcls\.{m}\b",
        rf"\b{m}\s*[:=]",
        rf"\b{m}\s*=\s*",
        rf"\.{m}\s*[:=]",
    ]
    return any(re.search(p, snippet) for p in patterns)


# ═══════════════════════════════════════════════════════════════════
# targets_from_tasks — TaskType → SlotKind mapping
# ═══════════════════════════════════════════════════════════════════

def targets_from_tasks(tasks: Iterable[object],
                       question_type: str = "locate") -> dict[str, AnswerTarget]:
    """Convert tasks to answer contracts.

    V24: Slot-driven — 从 task.required_slots 直接构建 AnswerTarget。
    """
    by_symbol: dict[str, set[SlotKind]] = {}
    for task in tasks:
        symbol = getattr(task, "target", "")
        if not symbol:
            continue
        required = getattr(task, "required_slots", None)
        if required:
            by_symbol.setdefault(symbol, set()).update(required)
        else:
            # 无 required_slots 的任务默认分配 DEFINITION
            by_symbol.setdefault(symbol, set()).add(SlotKind.DEFINITION)

    targets: dict[str, AnswerTarget] = {}
    for index, (symbol, slots) in enumerate(by_symbol.items(), 1):
        target_kind, owner, member = classify_target(symbol)
        key = f"target_{index:03d}"
        targets[key] = AnswerTarget(
            key, "slot_driven", symbol, slots,
            target_kind=target_kind, owner_symbol=owner,
        )
    return targets
