"""结构化调查目标与动作协议 — 禁止自由文本进入控制流

TargetSpec / Requirement / StepStatus / SufficiencyJudgment 等类型，
确保 "缺什么、查什么、目标是什么" 全部通过结构化枚举和数据类表达，
LLM 可以解释原因，但控制流只依赖这些结构化类型。

V15: 新增 TaskType / InvestigationTask / WorkOrder / StateDecision，
支持 Task-driven Stateful ReAct 架构。
"""

import re
from dataclasses import dataclass, field
from enum import Enum, auto


# ── Requirement 枚举 ────────────────────────────────────────────

class Requirement(Enum):
    """问题的需求类型。每个问题可同时有多个 Requirement。"""
    LOCATE_SYMBOL = auto()        # 定位符号定义位置
    READ_IMPLEMENTATION = auto()  # 读取方法/函数实现体
    EXPLAIN_BEHAVIOR = auto()     # 解释运行时行为/用途
    COMPARE_SYMBOLS = auto()      # 比较两个符号的异同
    TRACE_CALLER = auto()         # 追踪谁调用了该符号
    TRACE_CALLEE = auto()         # 追踪该符号调用了谁
    ENUMERATE_SYMBOLS = auto()    # 枚举所有满足条件的符号
    ANALYZE_IMPACT = auto()       # 分析修改该符号的影响范围
    FIND_LITERAL_USAGE = auto()   # 查找字面量/字符串的使用位置


# ── V24: RelationType — Planner 输出的关系类型 ──────────────────────

class RelationType(str, Enum):
    """Planner 输出的调查目标类型。替代旧 TaskType 枚举，
    表达"需要确认什么语义关系"而非"用什么工具"。"""
    DEFINITION_LOCATION = "definition_location"
    EXPLAIN_BEHAVIOR = "explain_behavior"
    TRACE_CALL_CHAIN = "trace_call_chain"
    COMPARE_BEHAVIOR = "compare_behavior"
    IMPACT_CHANGE = "impact_change"
    ENUMERATE_USAGES = "enumerate_usages"


# ── StepStatus 枚举 ─────────────────────────────────────────────

class StepStatus(Enum):
    """工具执行状态。与状态机决策分离——NO_EVIDENCE 不必然等于 STOP。"""
    SUCCESS_WITH_EVIDENCE = "success_with_evidence"
    NO_EVIDENCE = "no_evidence"
    NO_PROGRESS = "no_progress"
    TOOL_ERROR = "tool_error"
    ACTION_REJECTED = "action_rejected"


# ── 结构化数据类 ────────────────────────────────────────────────

@dataclass
class TargetSpec:
    """结构化的符号目标。所有搜索/定位操作都必须通过 TargetSpec，
    禁止将自然语言字符串直接作为 grep/search 参数。"""
    qualified_symbol: str        # "Context.invoke"
    owner_symbol: str            # "Context"
    member_symbol: str           # "invoke"
    symbol_kind: str = "function"  # "method"|"class"|"function"|"variable"
    file_hint: str | None = None

    def __post_init__(self):
        if not self.owner_symbol:
            self.owner_symbol = self.member_symbol

    def to_dict(self) -> dict:
        return {
            "qualified_symbol": self.qualified_symbol,
            "owner_symbol": self.owner_symbol,
            "member_symbol": self.member_symbol,
            "symbol_kind": self.symbol_kind,
            "file_hint": self.file_hint,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TargetSpec":
        return cls(
            qualified_symbol=d.get("qualified_symbol", ""),
            owner_symbol=d.get("owner_symbol", d.get("member_symbol", "")),
            member_symbol=d.get("member_symbol", ""),
            symbol_kind=d.get("symbol_kind", "function"),
            file_hint=d.get("file_hint"),
        )


@dataclass
class MissingRequirement:
    """LLM 判断的缺失需求——尚需收集什么类型的证据。"""
    type: str     # "method_body"|"caller_edge"|"dependency_relation"|
                  # "literal_usage"|"implementation"|"definition"
    symbol: str   # "Context.invoke"（必须是代码标识符）


@dataclass
class SuggestedAction:
    """LLM 建议的下一步动作。所有字段结构化，不包含自然语言 query。"""
    tool: str            # "resolve_symbol"|"read_window"|"search_references"|"dependency"
    symbol: str          # "Context.invoke"（代码标识符）
    search_kind: str     # "definition"|"references"|"literal"|"callers"
    file_hint: str | None = None
    line: int = 0


@dataclass
class SufficiencyJudgment:
    """LLM 充分性判断的结构化结果。"""
    sufficient: bool
    reason: str
    missing_requirements: list[MissingRequirement] = field(default_factory=list)
    suggested_actions: list[SuggestedAction] = field(default_factory=list)
    # V16.1: LLM 复议权限 — 当 LLM 认为当前证据方向不对/不充分时，可请求触发 replan
    replan_requested: bool = False
    replan_rationale: str = ""


@dataclass
class ClaimCitation:
    """答案中的一条声明与其支撑证据的映射。"""
    text: str
    evidence_ids: list[str] = field(default_factory=list)


@dataclass
class RequiredClaim:
    """问题必须回答的内容点。与结构槽位互补——结构槽位证明"证据类型齐了"，
    RequiredClaim 证明"问题内容回答齐了"。由 Query Planner LLM 从问题语义生成。
    """
    id: str                       # "claim_001"
    text: str                     # "send 方法的入口职责和签名"
    keywords: list[str] = field(default_factory=list)  # 确定性预筛关键词
    covered: bool = False
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "keywords": self.keywords,
            "covered": self.covered,
            "evidence_refs": self.evidence_refs,
        }


# ── V24: Planner 输出类型 ──────────────────────────────────────────

@dataclass
class PlannerTarget:
    """Planner 输出的独立调查目标（不涉及关系对比的符号）。"""
    symbol: str
    required_claims: list[str] = field(default_factory=list)
    file_hint: str | None = None


@dataclass
class RelationDef:
    """Planner 输出的符号间关系定义。"""
    type: RelationType
    subjects: list[str]
    required_claims: list[str] = field(default_factory=list)
    index: int = 0


@dataclass
class PlannerOutput:
    """Planner 的完整输出：问题类型 + 关系 + 独立目标 + 全局 claims。

    替代旧 (list[InvestigationTask], list[str]) 返回格式。
    """
    question_type: str = "locate"  # "locate"|"explain"|"trace"|"compare"|"impact"|"grep"
    relations: list[RelationDef] = field(default_factory=list)
    standalone_targets: list[PlannerTarget] = field(default_factory=list)
    required_claims: list[str] = field(default_factory=list)


# ── V15: Task-driven ReAct 类型 ──────────────────────────────────


class TaskType(Enum):
    """调查任务的类型枚举。与 Requirement 一一对应，但粒度更细。"""
    LOCATE_DEFINITION = auto()       # 定位符号定义位置
    READ_IMPLEMENTATION = auto()     # 读取方法/函数实现体
    FIND_CALLERS = auto()            # 追踪谁调用了该符号
    FIND_CALLEES = auto()            # 追踪该符号调用了谁
    FIND_DEPENDENTS = auto()         # 查找依赖/被依赖模块
    FIND_LITERAL_USAGE = auto()      # 查找字面量/字符串使用位置
    EXPLAIN_BEHAVIOR = auto()        # 解释运行时行为/用途
    COMPARE_SYMBOLS = auto()         # 比较两个符号的异同
    ENUMERATE_SYMBOLS = auto()       # 枚举所有满足条件的符号
    ANALYZE_IMPACT = auto()          # 分析修改该符号的影响范围


# ── V22: Task 角色与状态 ─────────────────────────────────────────

class TaskRole(str, Enum):
    """Task 在调查流程中的角色，决定合同判定范围。"""
    ROOT = "root"            # query_planner 初始拆解
    REQUIRED = "required"    # 确定性依赖（如 locate_definition → read_implementation）
    AUXILIARY = "auxiliary"  # 动态发现 — 辅助证据，不扩大合同
    GAP = "gap"              # 确定性补缺
    RETOOL = "retool"        # LLM 一次补缺


class TaskStatus(str, Enum):
    """Task 执行后的证据状态。"""
    VERIFIED = "verified"
    PARTIAL = "partial"
    NO_EVIDENCE = "no_evidence"
    FAILED = "failed"
    SKIPPED_DUPLICATE = "skipped_duplicate"


@dataclass
class GapStrategy:
    """确定性补缺策略 — 告诉引擎用不同工具/范围重试。"""
    preferred_tool: str
    scope_override: str | None = None     # "allow_all" | None
    file_hint: str | None = None
    search_kind: str = "definition"


@dataclass
class InvestigationTask:
    """一个独立的调查任务。代表"为了回答问题必须完成的事实任务"。
    可由 LLM Query Planner 生成，也可由确定性规则生成。

    V22: 新增 role/subtree_depth/parent_task_id/discovered_by/priority/
    attempt_count/strategy_override 字段，支持全局优先队列调度与子树执行。
    """
    id: str                       # "task_001"
    target: str                   # 代码标识符或概念名
    type: str = ""                # 旧 TaskType 值（V24 废弃，改用 required_slots）
    concept: str = ""             # 自然语言说明
    depends_on: list[str] = field(default_factory=list)  # 前置任务 ID
    status: str = "pending"       # pending|in_progress|completed|failed
    # V24: slot 驱动字段
    required_slots: set = field(default_factory=set)  # 需填充的 SlotKind 集合
    # V22: 调度与跟踪字段
    role: TaskRole = TaskRole.ROOT
    subtree_depth: int = 0
    parent_task_id: str = ""
    discovered_by: str = ""
    priority: int = 0
    attempt_count: int = 0
    strategy_override: GapStrategy | None = None
    # V25: preserve the directed fact a slot exists to prove.
    relation_id: str = ""
    counterpart: str = ""
    required_claims: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "type": self.type,
            "target": self.target,
            "concept": self.concept,
            "depends_on": list(self.depends_on),
            "status": self.status,
            "role": self.role.value,
            "subtree_depth": self.subtree_depth,
            "parent_task_id": self.parent_task_id,
            "discovered_by": self.discovered_by,
            "priority": self.priority,
            "attempt_count": self.attempt_count,
            "relation_id": self.relation_id,
            "counterpart": self.counterpart,
            "required_claims": list(self.required_claims),
            "required_slots": sorted(
                [getattr(s, "value", str(s)) for s in self.required_slots]
            ),
        }
        if self.strategy_override:
            d["strategy_override"] = {
                "preferred_tool": self.strategy_override.preferred_tool,
                "scope_override": self.strategy_override.scope_override,
                "file_hint": self.strategy_override.file_hint,
                "search_kind": self.strategy_override.search_kind,
            }
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "InvestigationTask":
        # V24 stores SlotKind values as JSON strings. Restore the enum here:
        # fill_work_orders() compares against SlotKind members, so raw strings
        # would make a resumed investigation lose all actionable slots.
        # Keep this import local to avoid the target/evidence_closure import
        # cycle during module initialization.
        from app.agent.evidence_closure import SlotKind

        required_slots = set()
        for raw_slot in d.get("required_slots", []):
            try:
                required_slots.add(
                    raw_slot if isinstance(raw_slot, SlotKind) else SlotKind(raw_slot)
                )
            except (TypeError, ValueError):
                # Persisted state is untrusted input. An unknown legacy value
                # must not masquerade as a satisfiable evidence requirement.
                continue

        gs = None
        if "strategy_override" in d and d["strategy_override"]:
            gs_raw = d["strategy_override"]
            gs = GapStrategy(
                preferred_tool=gs_raw.get("preferred_tool", ""),
                scope_override=gs_raw.get("scope_override"),
                file_hint=gs_raw.get("file_hint"),
                search_kind=gs_raw.get("search_kind", "definition"),
            )
        role_raw = d.get("role", "root")
        try:
            role = TaskRole(role_raw)
        except ValueError:
            role = TaskRole.ROOT
        return cls(
            id=d.get("id", ""),
            target=d.get("target", ""),
            type=d.get("type", ""),
            concept=d.get("concept", ""),
            depends_on=list(d.get("depends_on", [])),
            status=d.get("status", "pending"),
            role=role,
            subtree_depth=d.get("subtree_depth", 0),
            parent_task_id=d.get("parent_task_id", ""),
            discovered_by=d.get("discovered_by", ""),
            priority=d.get("priority", 0),
            attempt_count=d.get("attempt_count", 0),
            strategy_override=gs,
            relation_id=d.get("relation_id", ""),
            counterpart=d.get("counterpart", ""),
            required_claims=[
                c for c in d.get("required_claims", [])
                if isinstance(c, str) and c.strip()
            ],
            required_slots=required_slots,
        )


@dataclass
class WorkOrder:
    """LLM 填写的语义工单：说明"查什么、为什么查"。
    程序（Tool Adapter）负责把工单翻译成具体的工具参数。
    注意：WorkOrder 不含自然语言 query——target 必须是代码标识符。
    """
    task_id: str                  # 关联的 InvestigationTask.id
    description: str              # LLM 解释"为什么查这个"（用于日志/审计）
    target: str                   # 代码标识符或概念名
    tool_hint: str = ""           # LLM 建议的工具（可选，程序可覆盖）
    search_kind: str = "definition"  # definition|references|callers|literal
    file_hint: str | None = None
    line: int = 0
    # Audit-only relation constraints. These are deterministic executor input,
    # never free-form tool instructions from the Planner.
    relation_id: str = ""
    counterpart: str = ""
    required_claims: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "target": self.target,
            "tool_hint": self.tool_hint,
            "search_kind": self.search_kind,
            "file_hint": self.file_hint,
            "line": self.line,
            "relation_id": self.relation_id,
            "counterpart": self.counterpart,
            "required_claims": list(self.required_claims),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "WorkOrder":
        return cls(
            task_id=d.get("task_id", ""),
            description=d.get("description", ""),
            target=d.get("target", ""),
            tool_hint=d.get("tool_hint", ""),
            search_kind=d.get("search_kind", "definition"),
            file_hint=d.get("file_hint"),
            line=d.get("line", 0),
            relation_id=d.get("relation_id", ""),
            counterpart=d.get("counterpart", ""),
            required_claims=[
                c for c in d.get("required_claims", [])
                if isinstance(c, str) and c.strip()
            ],
        )


@dataclass
class StateDecision:
    """LLM 在每轮 ReAct 循环中的决策输出。
    包含"继续还是回答"、已完成的任务、新发现的任务、以及下一张工单。
    """
    action: str                   # "continue"|"answer"
    reason: str                   # 中文解释
    completed_tasks: list[str] = field(default_factory=list)  # 已完成的任务 ID
    new_tasks: list[dict] = field(default_factory=list)       # [{type, target, concept}, ...]
    work_orders: list[WorkOrder] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "completed_tasks": list(self.completed_tasks),
            "new_tasks": list(self.new_tasks),
            "work_orders": [wo.to_dict() for wo in self.work_orders],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StateDecision":
        wo_list = [WorkOrder.from_dict(w) for w in d.get("work_orders", [])]
        return cls(
            action=d.get("action", "answer"),
            reason=d.get("reason", ""),
            completed_tasks=list(d.get("completed_tasks", [])),
            new_tasks=list(d.get("new_tasks", [])),
            work_orders=wo_list,
        )


# ── V15: WorkOrder 合法值校验 ──────────────────────────────────

_VALID_WO_TOOL_HINTS = frozenset({
    "resolve_symbol", "read_window", "search_references", "dependency",
    "search", "search_filename", "python_ast", "git", "knowledge",
    "",  # 空表示让 Tool Adapter 从 search_kind 推断
})

_VALID_WO_SEARCH_KINDS = frozenset({
    "definition", "references", "literal", "callers",
})

def validate_planner_output(raw: dict) -> list[str]:
    """校验新版 Planner 输出（V24 relation 格式）。"""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["output is not a dict"]

    # question_type（必填）
    qt = raw.get("question_type", "")
    _valid_qtypes = {"locate", "explain", "trace", "compare", "impact", "grep"}
    if qt not in _valid_qtypes:
        errors.append(
            f"question_type={qt!r} not in {sorted(_valid_qtypes)}"
        )

    # relations（至少一条或 standalone_targets 至少一个）
    relations = raw.get("relations", [])
    targets = raw.get("standalone_targets", [])

    if not isinstance(relations, list):
        errors.append("relations must be array")
    if not isinstance(targets, list):
        errors.append("standalone_targets must be array")
    if len(relations) == 0 and len(targets) == 0:
        errors.append("at least one relation or standalone_target is required")

    for i, rel in enumerate(relations):
        if not isinstance(rel, dict):
            errors.append(f"relations[{i}] is not an object")
            continue
        rt = rel.get("type", "")
        try:
            RelationType(rt)
        except ValueError:
            errors.append(
                f"relations[{i}].type={rt!r} not a valid RelationType"
            )
        subjects = rel.get("subjects", [])
        if not isinstance(subjects, list) or len(subjects) == 0:
            errors.append(f"relations[{i}].subjects must be non-empty array")
        for j, s in enumerate(subjects):
            if not isinstance(s, str) or not s.strip():
                errors.append(f"relations[{i}].subjects[{j}] must be non-empty string")

    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            errors.append(f"standalone_targets[{i}] is not an object")
            continue
        sym = t.get("symbol", "")
        if not isinstance(sym, str) or not sym.strip():
            errors.append(f"standalone_targets[{i}].symbol must be non-empty string")

    # required_claims（可选全局）
    claims = raw.get("required_claims")
    if claims is not None:
        if not isinstance(claims, list):
            errors.append("required_claims must be array")
        else:
            for i, c in enumerate(claims):
                if not isinstance(c, str) or not c.strip():
                    errors.append(f"required_claims[{i}] must be non-empty string")

    return errors


def validate_state_decision_output(raw: dict) -> list[str]:
    """校验 State Decision 的 LLM 输出是否符合 Schema。"""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["output is not a dict"]

    for key in ("action", "reason", "work_orders"):
        if key not in raw:
            errors.append(f"missing required key: {key}")
    if errors:
        return errors

    action = raw.get("action", "")
    if action not in ("continue", "answer"):
        errors.append(f"action must be 'continue' or 'answer', got {action!r}")

    if action == "answer":
        wo = raw.get("work_orders", [])
        if isinstance(wo, list) and len(wo) > 0:
            errors.append("work_orders must be empty when action=answer")
    else:
        wo = raw.get("work_orders", [])
        if not isinstance(wo, list) or len(wo) == 0:
            errors.append("work_orders must be non-empty when action=continue")

    # 校验 work_orders
    wo_list = raw.get("work_orders")
    if isinstance(wo_list, list):
        for i, w in enumerate(wo_list):
            if not isinstance(w, dict):
                errors.append(f"work_orders[{i}] is not an object")
                continue
            w_target = w.get("target", "")
            if not isinstance(w_target, str) or not w_target.strip():
                errors.append(f"work_orders[{i}].target is empty")
            w_tool = w.get("tool_hint", "")
            if w_tool not in _VALID_WO_TOOL_HINTS:
                errors.append(
                    f"work_orders[{i}].tool_hint={w_tool!r} not in {sorted(_VALID_WO_TOOL_HINTS)}"
                )
            w_kind = w.get("search_kind", "definition")
            if w_kind not in _VALID_WO_SEARCH_KINDS:
                errors.append(
                    f"work_orders[{i}].search_kind={w_kind!r} not in {sorted(_VALID_WO_SEARCH_KINDS)}"
                )
            # 非 literal search 的 target 必须是代码标识符
            if w_kind != "literal":
                w_symbol = w.get("target", "")
                if not _SYMBOL_PATTERN.match(w_symbol):
                    errors.append(
                        f"work_orders[{i}].target={w_symbol!r} is not a valid code identifier"
                    )

    return errors


# ── LLM 输出 JSON Schema ────────────────────────────────────────

# 合法的 missing_requirements.type 值
_VALID_MR_TYPES = frozenset({
    "method_body", "caller_edge", "dependency_relation",
    "literal_usage", "implementation", "definition",
})

# 合法的 suggested_actions.tool 值
_VALID_SA_TOOLS = frozenset({
    "resolve_symbol", "read_window", "search_references", "dependency",
})

# 合法的 suggested_actions.search_kind 值
_VALID_SA_SEARCH_KINDS = frozenset({
    "definition", "references", "literal", "callers",
})

# 代码标识符正则（允许点号连接的限定名）
_SYMBOL_PATTERN = re.compile(r'^[A-Za-z_][A-Za-z0-9_.]*$')


def validate_llm_sufficiency_output(raw: dict) -> list[str]:
    """校验 LLM 充分性判断输出是否符合结构化 Schema。
    返回错误列表；空列表 = 通过。
    """
    errors: list[str] = []

    if not isinstance(raw, dict):
        return ["output is not a dict"]

    # required keys
    for key in ("sufficient", "missing_requirements", "suggested_actions", "reason"):
        if key not in raw:
            errors.append(f"missing required key: {key}")
    if errors:
        return errors

    # V16.1: 可选复议字段校验（不强制要求，但若出现则校验类型）
    if "replan_requested" in raw and not isinstance(raw["replan_requested"], bool):
        errors.append("replan_requested must be boolean")
    if "replan_rationale" in raw and not isinstance(raw["replan_rationale"], str):
        errors.append("replan_rationale must be string")

    # sufficient 必须是 bool
    if not isinstance(raw["sufficient"], bool):
        errors.append("sufficient must be boolean")

    # reason 必须是 string
    if not isinstance(raw.get("reason"), str):
        errors.append("reason must be string")

    # missing_requirements 必须是 list
    mr_list = raw.get("missing_requirements")
    if not isinstance(mr_list, list):
        errors.append("missing_requirements must be array")
    else:
        for i, mr in enumerate(mr_list):
            if not isinstance(mr, dict):
                errors.append(f"missing_requirements[{i}] is not an object")
                continue
            mr_type = mr.get("type", "")
            if mr_type not in _VALID_MR_TYPES:
                errors.append(
                    f"missing_requirements[{i}].type={mr_type!r} not in {sorted(_VALID_MR_TYPES)}"
                )
            mr_symbol = mr.get("symbol", "")
            if not isinstance(mr_symbol, str) or not _SYMBOL_PATTERN.match(mr_symbol):
                errors.append(
                    f"missing_requirements[{i}].symbol={mr_symbol!r} is not a valid code identifier"
                )

    # suggested_actions 必须是 list
    sa_list = raw.get("suggested_actions")
    if not isinstance(sa_list, list):
        errors.append("suggested_actions must be array")
    else:
        for i, sa in enumerate(sa_list):
            if not isinstance(sa, dict):
                errors.append(f"suggested_actions[{i}] is not an object")
                continue
            sa_tool = sa.get("tool", "")
            if sa_tool not in _VALID_SA_TOOLS:
                errors.append(
                    f"suggested_actions[{i}].tool={sa_tool!r} not in {sorted(_VALID_SA_TOOLS)}"
                )
            sa_symbol = sa.get("symbol", "")
            if not isinstance(sa_symbol, str) or not _SYMBOL_PATTERN.match(sa_symbol):
                errors.append(
                    f"suggested_actions[{i}].symbol={sa_symbol!r} is not a valid code identifier"
                )
            sa_kind = sa.get("search_kind", "")
            if sa_kind not in _VALID_SA_SEARCH_KINDS:
                errors.append(
                    f"suggested_actions[{i}].search_kind={sa_kind!r} not in {sorted(_VALID_SA_SEARCH_KINDS)}"
                )
            # search_kind=literal 时允许非标识符 symbol，其他情况必须
            if sa_kind != "literal" and not _SYMBOL_PATTERN.match(sa_symbol):
                errors.append(
                    f"suggested_actions[{i}]: non-literal search requires valid code identifier symbol"
                )
            sa_line = sa.get("line", 0)
            if not isinstance(sa_line, int) or sa_line < 0:
                errors.append(f"suggested_actions[{i}].line must be non-negative integer")
            sa_file = sa.get("file_hint")
            if sa_file is not None and not isinstance(sa_file, str):
                errors.append(f"suggested_actions[{i}].file_hint must be string or null")

    return errors
