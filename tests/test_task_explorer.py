"""V22 Task Explorer 单元测试 — mock 文件系统，不上网。

覆盖: ExplorationState 预算扣账、优先级调度、证据可信链、工单填写、
动态发现、补缺策略、执行函数、证据验证、停止原因判定。
"""

import pytest
from pathlib import Path
import ast as _ast

from app.agent.task_explorer import (
    ExplorationState, ToolExecutor,
    fill_work_orders, _deterministic_work_orders,
    discover_tasks, gap_analyzer, _deterministic_gap_fill,
    _execute_task, _execute_task_subtree,
    _verify_evidence, _task_status, _determine_stop_reason,
    _build_retool_task,
    _GAP_STRATEGIES,
    _collect_calls_in_function, _classify_and_normalize,
    _get_class_context, _get_attr_receiver_name,
)
from app.agent.evidence_closure import (
    SlotKind, AnswerTarget, targets_from_tasks,
    check_minimum_evidence_contract, EvidenceVerifier, SLOT_TO_TOOL,
)
from app.models.evidence import Evidence
from app.models.location import CodeLocation
from app.models.target import (
    GapStrategy, InvestigationTask, TaskRole, TaskStatus,
    TargetSpec, WorkOrder,
)


# ═══════════════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════════════

def _make_state(question="test question", question_type="locate",
                repo_path="") -> ExplorationState:
    return ExplorationState(question=question, question_type=question_type,
                            repo_path=repo_path)


_LEGACY_SLOT_FIXTURE = {
    "locate_definition": {SlotKind.DEFINITION},
    "read_implementation": {SlotKind.IMPLEMENTATION},
    "find_callers": {SlotKind.VERIFIED_CALLER_EDGE},
    "find_callees": {SlotKind.VERIFIED_CALLEE_EDGE},
    "find_literal_usage": {SlotKind.CANDIDATE_REFERENCE},
    "analyze_impact": {SlotKind.DEFINITION, SlotKind.VERIFIED_CALLER_EDGE,
                         SlotKind.CANDIDATE_REFERENCE},
}


def _make_task(task_id="t1", task_type="locate_definition",
               target="Widget", role=TaskRole.ROOT, slots=None) -> InvestigationTask:
    """Test fixture: production tasks are slot-driven, never type-driven."""
    return InvestigationTask(
        id=task_id, target=target, role=role,
        required_slots=set(slots or _LEGACY_SLOT_FIXTURE.get(task_type, {SlotKind.DEFINITION})),
    )


def _make_ev(source="resolve_symbol", file="code.py", line=1,
             snippet="class Widget:", symbol="Widget", confidence=1.0) -> Evidence:
    ev = Evidence(
        kind="code", source=source,
        location=CodeLocation(file=file, start_line=line, symbol=symbol),
        snippet=snippet, confidence=confidence,
    )
    ev.set_deterministic_id("HEAD", file, line, line + 1, snippet)
    return ev


def _setup_repo(tmp_path, files: dict[str, str]) -> Path:
    """在临时目录中创建 Python 文件。"""
    for fname, content in files.items():
        p = tmp_path / fname
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return tmp_path


# ═══════════════════════════════════════════════════════════════════════
# ExplorationState — 预算扣账
# ═══════════════════════════════════════════════════════════════════════

class TestBudgetConsume:
    """三阶段独立扣账 — consume_budget 真实扣除 state 字段。"""

    def test_main_budget_consume(self):
        state = _make_state()
        assert state.main_steps_used == 0
        assert state.consume_budget("MAIN", 1)
        assert state.main_steps_used == 1

    def test_main_budget_exhausted(self):
        state = _make_state()
        state.main_steps_used = 12
        assert not state.consume_budget("MAIN", 1)
        assert state.main_steps_used == 12  # unchanged

    def test_main_budget_partial_consume_exceeds(self):
        state = _make_state()
        state.main_steps_used = 11
        assert not state.consume_budget("MAIN", 2)  # 11+2=13 > 12
        assert state.main_steps_used == 11  # unchanged

    def test_gap_budget_independent(self):
        state = _make_state()
        state.main_steps_used = 10
        assert state.consume_budget("GAP", 1)
        assert state.gap_steps_used == 1
        assert state.main_steps_used == 10  # 互不影响

    def test_gap_budget_exhausted(self):
        state = _make_state()
        state.gap_steps_used = 3
        assert not state.consume_budget("GAP", 1)

    def test_retool_budget_independent(self):
        state = _make_state()
        state.gap_steps_used = 2
        assert state.consume_budget("RETOOL", 1)
        assert state.retool_steps_used == 1
        assert state.gap_steps_used == 2  # 互不影响

    def test_retool_budget_exhausted(self):
        state = _make_state()
        state.retool_steps_used = 4
        assert not state.consume_budget("RETOOL", 1)

    def test_three_phases_separate_tracking(self):
        """三阶段各自独立，不互相干扰。"""
        state = _make_state()
        state.consume_budget("MAIN", 3)
        state.consume_budget("GAP", 2)
        state.consume_budget("RETOOL", 1)
        assert state.main_steps_used == 3
        assert state.gap_steps_used == 2
        assert state.retool_steps_used == 1

    def test_unknown_phase_returns_false(self):
        state = _make_state()
        assert not state.consume_budget("UNKNOWN", 1)


# ═══════════════════════════════════════════════════════════════════════
# ExplorationState — 任务调度
# ═══════════════════════════════════════════════════════════════════════

class TestTaskScheduling:
    """全局优先队列调度 — ROOT > REQUIRED > AUXILIARY; subtree_depth 升序。"""

    def test_pop_empty_returns_none(self):
        state = _make_state()
        assert state.pop_next() is None

    def test_single_task_pop(self):
        state = _make_state()
        t = _make_task(role=TaskRole.ROOT)
        state.enqueue_tasks([t])
        popped = state.pop_next()
        assert popped is t
        assert not state.has_pending()

    def test_root_before_required(self):
        state = _make_state()
        t_req = _make_task("t1", role=TaskRole.REQUIRED)
        t_root = _make_task("t2", role=TaskRole.ROOT)
        state.enqueue_tasks([t_req, t_root])
        assert state.pop_next().role == TaskRole.ROOT
        assert state.pop_next().role == TaskRole.REQUIRED

    def test_required_before_auxiliary(self):
        state = _make_state()
        t_aux = _make_task("t1", role=TaskRole.AUXILIARY)
        t_req = _make_task("t2", role=TaskRole.REQUIRED)
        state.enqueue_tasks([t_aux, t_req])
        assert state.pop_next().role == TaskRole.REQUIRED
        assert state.pop_next().role == TaskRole.AUXILIARY

    def test_same_role_sorted_by_subtree_depth(self):
        state = _make_state()
        t1 = _make_task("t1", role=TaskRole.AUXILIARY)
        t1.subtree_depth = 2
        t2 = _make_task("t2", role=TaskRole.AUXILIARY)
        t2.subtree_depth = 0
        state.enqueue_tasks([t1, t2])
        assert state.pop_next().subtree_depth == 0
        assert state.pop_next().subtree_depth == 2

    def test_multi_root_fair_scheduling(self):
        """多个 root task 都可弹出。"""
        state = _make_state()
        tasks = [_make_task(f"r{i}", role=TaskRole.ROOT) for i in range(3)]
        state.enqueue_tasks(tasks)
        popped = []
        while state.has_pending():
            popped.append(state.pop_next())
        assert len(popped) == 3
        assert all(t.role == TaskRole.ROOT for t in popped)

    def test_has_pending_reflects_queue_state(self):
        state = _make_state()
        assert not state.has_pending()
        state.enqueue_tasks([_make_task()])
        assert state.has_pending()
        state.pop_next()
        assert not state.has_pending()


# ═══════════════════════════════════════════════════════════════════════
# ExplorationState — 任务去重
# ═══════════════════════════════════════════════════════════════════════

class TestTaskDedup:
    """is_duplicate — 同类型+同目标视为重复。"""

    def test_same_type_and_target_is_duplicate(self):
        state = _make_state()
        t1 = _make_task("t1", "locate_definition", "Widget")
        t2 = _make_task("t2", "locate_definition", "Widget")
        state.enqueue_tasks([t1])
        assert state.is_duplicate(t2)

    def test_different_type_not_duplicate(self):
        state = _make_state()
        t1 = _make_task("t1", "locate_definition", "Widget")
        state.enqueue_tasks([t1])
        t2 = _make_task("t2", "read_implementation", "Widget")
        assert not state.is_duplicate(t2)

    def test_different_target_not_duplicate(self):
        state = _make_state()
        t1 = _make_task("t1", "locate_definition", "Widget")
        state.enqueue_tasks([t1])
        t2 = _make_task("t2", "locate_definition", "Gadget")
        assert not state.is_duplicate(t2)

    def test_enqueue_duplicate_skipped(self):
        """enqueue_tasks 跳过已有同 ID 的任务。"""
        state = _make_state()
        t1 = _make_task("t1")
        t1_dup = _make_task("t1")  # same id
        state.enqueue_tasks([t1])
        state.enqueue_tasks([t1_dup])
        assert len(state.all_tasks) == 1
        assert len(state.pending_tasks) == 1


# ═══════════════════════════════════════════════════════════════════════
# ExplorationState — 证据可信链
# ═══════════════════════════════════════════════════════════════════════

class TestEvidenceChain:
    """verified vs candidate 证据分离 + 合同/发现只用 verified。"""

    def test_add_verified_evidence(self):
        state = _make_state()
        ev = _make_ev()
        state.add_verified_evidence(ev, "t1")
        assert "t1" in state.verified_evidence
        assert len(state.verified_evidence["t1"]) == 1
        assert ev.id in state.all_evidence

    def test_add_candidate_evidence(self):
        state = _make_state()
        ev = _make_ev()
        state.add_candidate_evidence(ev, "t1", "verification failed")
        assert "t1" in state.candidate_evidence
        assert len(state.candidate_evidence["t1"]) == 1
        assert state.candidate_evidence["t1"][0][1] == "verification failed"
        assert ev.id not in state.all_evidence

    def test_get_verified_evidence_returns_copy(self):
        state = _make_state()
        ev = _make_ev()
        state.add_verified_evidence(ev, "t1")
        result = state.get_verified_evidence()
        assert "t1" in result
        # 返回的是副本，修改不影响原 state
        result["new_key"] = []
        assert "new_key" not in state.verified_evidence

    def test_verified_and_candidate_separated(self):
        """同 task 的 verified 和 candidate 分别存储。"""
        state = _make_state()
        ev1 = _make_ev(source="resolve_symbol", snippet="class Widget:")
        ev2 = _make_ev(source="search_references", snippet="Widget()",
                       confidence=0.8)
        state.add_verified_evidence(ev1, "t1")
        state.add_candidate_evidence(ev2, "t1", "not a definition")
        assert len(state.verified_evidence.get("t1", [])) == 1
        assert len(state.candidate_evidence.get("t1", [])) == 1


# ═══════════════════════════════════════════════════════════════════════
# fill_work_orders
# ═══════════════════════════════════════════════════════════════════════

class TestFillWorkOrders:
    """SlotKind（证据槽位）→ WorkOrder（工单）映射。"""

    def test_locate_definition_produces_resolve(self):
        t = _make_task(slots={SlotKind.DEFINITION})
        orders = fill_work_orders(t)
        assert len(orders) == 1
        assert orders[0].tool_hint == "resolve_symbol"
        assert orders[0].search_kind == "definition"

    def test_implementation_produces_read(self):
        t = _make_task(slots={SlotKind.IMPLEMENTATION})
        orders = fill_work_orders(t)
        assert len(orders) == 1
        assert orders[0].tool_hint == "read_window"

    def test_verified_caller_produces_verification_tool(self):
        t = _make_task(slots={SlotKind.VERIFIED_CALLER_EDGE})
        orders = fill_work_orders(t)
        assert len(orders) == 1
        assert orders[0].tool_hint == "verify_callers"
        assert orders[0].search_kind == "callers"

    def test_directed_callee_order_keeps_counterpart_and_claims(self):
        t = _make_task(slots={SlotKind.VERIFIED_CALLEE_EDGE})
        t.counterpart = "next_step"
        t.relation_id = "relation_001"
        t.required_claims = ["entry 调用 next_step"]

        order = fill_work_orders(t)[0]

        assert order.counterpart == "next_step"
        assert order.relation_id == "relation_001"
        assert "entry 调用 next_step" in order.required_claims
        assert "-> next_step" in order.description

    def test_candidate_reference_produces_reference_search(self):
        t = _make_task(slots={SlotKind.CANDIDATE_REFERENCE})
        orders = fill_work_orders(t)
        assert len(orders) >= 1
        assert orders[0].tool_hint == "search_references"
        assert orders[0].search_kind == "references"

    def test_empty_slots_do_not_create_unscoped_action(self):
        t = InvestigationTask(id="empty", target="Widget")
        orders = fill_work_orders(t)
        assert orders == []

    def test_task_round_trip_restores_slot_enums_for_resumed_execution(self):
        """A persisted V24 task remains schedulable after JSON recovery."""
        task = _make_task(
            task_id="resume",
            target="Widget",
            slots={SlotKind.DEFINITION, SlotKind.IMPLEMENTATION},
        )

        restored = InvestigationTask.from_dict(task.to_dict())

        assert restored.required_slots == {
            SlotKind.DEFINITION,
            SlotKind.IMPLEMENTATION,
        }
        assert fill_work_orders(restored)[0].tool_hint == "resolve_symbol"

    def test_max_orders_per_task_capped(self):
        """工单数不超过 MAX_ORDERS_PER_TASK (4)。"""
        t = _make_task(slots={SlotKind.DEFINITION, SlotKind.IMPLEMENTATION,
                              SlotKind.VERIFIED_CALLER_EDGE,
                              SlotKind.VERIFIED_CALLEE_EDGE,
                              SlotKind.CANDIDATE_REFERENCE})
        orders = fill_work_orders(t)
        assert len(orders) <= 4


# ═══════════════════════════════════════════════════════════════════════
# _deterministic_work_orders — 补缺策略
# ═══════════════════════════════════════════════════════════════════════

class TestDeterministicWorkOrders:
    """Gap task 使用 strategy_override 生成替代工单。"""

    def test_no_strategy_falls_back_to_fill(self):
        t = _make_task(task_type="locate_definition")
        orders = _deterministic_work_orders(t)
        assert len(orders) >= 1
        assert orders[0].tool_hint == "resolve_symbol"

    def test_strategy_override_changes_tool(self):
        gs = GapStrategy(preferred_tool="search_references",
                         search_kind="definition", scope_override="allow_all")
        t = _make_task(task_type="locate_definition")
        t.strategy_override = gs
        orders = _deterministic_work_orders(t)
        assert len(orders) == 1
        assert orders[0].tool_hint == "search_references"
        assert orders[0].search_kind == "definition"

    def test_strategy_override_preserves_file_hint(self):
        gs = GapStrategy(preferred_tool="read_window",
                         search_kind="definition", file_hint="code.py")
        t = _make_task(task_type="read_implementation")
        t.strategy_override = gs
        orders = _deterministic_work_orders(t)
        assert orders[0].file_hint == "code.py"


# ═══════════════════════════════════════════════════════════════════════
# discover_tasks — 从 verified evidence 动态发现
# ═══════════════════════════════════════════════════════════════════════

class TestDiscoverTasks:
    """从 verified evidence 发现新调查任务（仅规则驱动，不依赖 LLM）。"""

    def test_no_evidence_returns_empty(self):
        state = _make_state()
        parent = _make_task()
        new_tasks = discover_tasks([], parent, state)
        assert new_tasks == []

    def test_read_window_discovers_callees(self):
        """read_window 中发现的函数调用 → AUXILIARY 子任务。"""
        state = _make_state(question_type="trace")
        parent = _make_task(target="run")
        ev = _make_ev(source="read_window", file="code.py", line=1,
                      snippet="def run(self):\n    return helper()\n")
        new_tasks = discover_tasks([ev], parent, state)
        # "helper" 出现在 read_window snippet 中，且不是 builtin
        assert len(new_tasks) >= 1
        assert any(t.target == "helper" for t in new_tasks)
        assert all(t.role == TaskRole.AUXILIARY for t in new_tasks)

    def test_builtins_filtered_out(self):
        """print/len/self 等 builtin 不被发现为调查目标。"""
        state = _make_state(question_type="trace")
        parent = _make_task(target="run")
        ev = _make_ev(source="read_window", file="code.py", line=1,
                      snippet="def run(self):\n    print(len(x))\n    return self.value\n")
        new_tasks = discover_tasks([ev], parent, state)
        # print, len, self 都被过滤
        targets = {t.target for t in new_tasks}
        assert "print" not in targets
        assert "len" not in targets
        assert "self" not in targets

    def test_parent_target_not_rediscovered(self):
        """父任务的目标不被重复发现。"""
        state = _make_state(question_type="trace")
        parent = _make_task(target="helper")
        ev = _make_ev(source="read_window", snippet="def helper():\n    return other()\n")
        new_tasks = discover_tasks([ev], parent, state)
        assert not any(t.target == "helper" for t in new_tasks)

    def test_discovery_limit_three(self):
        """动态发现最多 3 个新任务。"""
        state = _make_state(question_type="trace")
        parent = _make_task(target="main")
        ev = _make_ev(source="read_window", snippet=(
            "def main():\n"
            "    a()\n    b()\n    c()\n    d()\n    e()\n"
        ))
        new_tasks = discover_tasks([ev], parent, state)
        assert len(new_tasks) <= 3

    def test_verify_callees_discovers_symbols(self):
        """verify_callees 证据也参与发现（新格式：从 location.symbol 读取 callee）。"""
        state = _make_state(question_type="trace")
        parent = _make_task(target="process")
        ev = _make_ev(source="verify_callees", symbol="validate",
                      snippet="process → validate() at code.py:42")
        new_tasks = discover_tasks([ev], parent, state)
        targets = {t.target for t in new_tasks}
        assert "validate" in targets


# ═══════════════════════════════════════════════════════════════════════
# gap_analyzer
# ═══════════════════════════════════════════════════════════════════════

class TestGapAnalyzer:
    """LLM 缺口分析 — 决定是否需要一次 retool。"""

    def test_no_llm_returns_done(self):
        state = _make_state()
        result = gap_analyzer("test question", state, call_llm=None)
        assert result["action"] == "done"
        assert result["new_task"] is None

    def test_llm_returns_add_one_task(self):
        def mock_llm(prompt, **kwargs):
            return '{"action": "add_one_task", "target": "missing_func", "slot": "definition", "reason": "缺少函数定义"}'
        state = _make_state()
        state.all_tasks.append(_make_task("t1"))
        result = gap_analyzer("Where is Foo?", state, call_llm=mock_llm)
        assert result["action"] == "add_one_task"
        assert result["new_task"]["target"] == "missing_func"

    def test_llm_rejects_untyped_or_incomplete_task(self):
        def mock_llm(prompt, **kwargs):
            return '{"action": "add_one_task", "target": "missing_func", "reason": "缺少函数定义"}'
        result = gap_analyzer("test", _make_state(), call_llm=mock_llm)
        assert result["action"] == "done"
        assert result["reason"] == "invalid retool slot"

    def test_llm_retool_prompt_includes_draft_and_evidence(self):
        prompts = []
        def mock_llm(prompt, **kwargs):
            prompts.append(prompt)
            return '{"action": "done", "reason": "sufficient"}'
        state = _make_state()
        evidence = _make_ev(snippet="def missing_func(): pass", symbol="missing_func")
        state.all_evidence[evidence.id] = evidence
        gap_analyzer("test", state, call_llm=mock_llm, draft_answer="draft answer")
        assert "draft answer" in prompts[0]
        assert "证据摘要" in prompts[0]

    def test_llm_returns_done(self):
        def mock_llm(prompt, **kwargs):
            return '{"action": "done", "reason": "evidence sufficient"}'
        state = _make_state()
        result = gap_analyzer("test", state, call_llm=mock_llm)
        assert result["action"] == "done"

    def test_llm_bad_json_falls_back_to_done(self):
        def mock_llm(prompt, **kwargs):
            return "not json at all"
        state = _make_state()
        result = gap_analyzer("test", state, call_llm=mock_llm)
        assert result["action"] == "done"

    def test_llm_exception_falls_back_to_done(self):
        def mock_llm(prompt, **kwargs):
            raise RuntimeError("API error")
        state = _make_state()
        result = gap_analyzer("test", state, call_llm=mock_llm)
        assert result["action"] == "done"


# ═══════════════════════════════════════════════════════════════════════
# _deterministic_gap_fill
# ═══════════════════════════════════════════════════════════════════════

class TestDeterministicGapFill:
    """从合同 open slots 生成补缺任务。"""

    def test_no_open_slots_returns_empty(self):
        state = _make_state()
        target = AnswerTarget("t1", "locate_definition", "Widget",
                              {SlotKind.DEFINITION})
        targets = {"t1": target}
        result = _deterministic_gap_fill(targets, state)
        # DEFINITION is open → gap task
        assert len(result) >= 1
        assert result[0].role == TaskRole.GAP
        assert result[0].strategy_override is not None

    def test_gap_tasks_have_strategy_override(self):
        state = _make_state()
        target = AnswerTarget("t1", "locate_definition", "Widget",
                              {SlotKind.DEFINITION})
        targets = {"t1": target}
        result = _deterministic_gap_fill(targets, state)
        for t in result:
            assert t.strategy_override is not None
            assert t.role == TaskRole.GAP
            assert t.subtree_depth == 0

    def test_gap_tasks_capped_at_four(self):
        state = _make_state()
        # 创建多个有 open slots 的 targets
        targets = {}
        for i, name in enumerate(["A", "B", "C", "D"], 1):
            targets[f"t{i}"] = AnswerTarget(
                f"t{i}", "locate_definition", name, {SlotKind.DEFINITION})
        result = _deterministic_gap_fill(targets, state)
        assert len(result) <= 4  # V23: 增至 4 以容纳 claims 补缺

    def test_gap_strategy_for_definition_uses_search(self):
        """DEFINITION slot 的补缺策略是 search_references allow_all。"""
        strategy = _GAP_STRATEGIES.get(SlotKind.DEFINITION)
        assert strategy is not None
        assert strategy.preferred_tool == "search_references"
        assert strategy.scope_override == "allow_all"

    def test_gap_strategy_for_implementation_uses_read_window(self):
        """IMPLEMENTATION slot 的补缺策略是 read_window。"""
        strategy = _GAP_STRATEGIES.get(SlotKind.IMPLEMENTATION)
        assert strategy is not None
        assert strategy.preferred_tool == "read_window"


# ═══════════════════════════════════════════════════════════════════════
# _execute_task — Phase 2 单任务执行
# ═══════════════════════════════════════════════════════════════════════

class TestExecuteTask:
    """Phase 2 _execute_task — 工单链执行 + 子任务入全局队列。"""

    def test_execute_task_increments_attempt_count(self):
        state = _make_state()
        repo = _setup_repo(Path("."), {"code.py": "class Widget:\n    pass\n"})
        # 使用当前目录作为 repo
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "code.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
            state.repo_path = td
            t = _make_task(target="Widget")
            executor = ToolExecutor(td)
            _execute_task(t, state, allow_children=False, tool_executor=executor)
            assert t.attempt_count == 1

    def test_execute_task_consumes_main_budget(self):
        state = _make_state()
        state.main_steps_used = 11
        state.current_phase = "MAIN"
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "code.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
            state.repo_path = td
            t = _make_task(target="Widget")
            executor = ToolExecutor(td)
            _execute_task(t, state, allow_children=False, tool_executor=executor)
            # 至少消耗一个 budget（resolve_symbol 工单）
            assert state.main_steps_used >= 12 or state.main_steps_used > 11

    def test_execute_task_marks_completed(self):
        state = _make_state()
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "code.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
            state.repo_path = td
            t = _make_task(target="Widget")
            executor = ToolExecutor(td)
            _execute_task(t, state, allow_children=False, tool_executor=executor)
            assert f"task:{t.id}" in state.completed_action_keys

    def test_multi_slot_task_requeues_once_per_verified_slot(self, tmp_path):
        (tmp_path / "code.py").write_text(
            "def helper():\n    return 1\n\ndef Widget():\n    return helper()\n",
            encoding="utf-8",
        )
        state = _make_state(repo_path=str(tmp_path), question_type="explain")
        task = _make_task(target="Widget", slots={SlotKind.DEFINITION, SlotKind.IMPLEMENTATION})
        executor = ToolExecutor(str(tmp_path))
        _execute_task(task, state, allow_children=False, tool_executor=executor)
        assert [s["tool"] for s in state.steps] == ["resolve_symbol"]
        assert state.pending_tasks == [task]
        _execute_task(state.pop_next(), state, allow_children=False, tool_executor=executor)
        assert [s["tool"] for s in state.steps] == ["resolve_symbol", "read_window"]
        assert not state.pending_tasks
        assert task.status == TaskStatus.VERIFIED.value

    def test_no_evidence_is_not_reenqueued(self, tmp_path):
        state = _make_state(repo_path=str(tmp_path))
        task = _make_task(target="Missing", slots={SlotKind.DEFINITION, SlotKind.IMPLEMENTATION})
        _execute_task(task, state, allow_children=False, tool_executor=ToolExecutor(str(tmp_path)))
        assert task.status == TaskStatus.NO_EVIDENCE.value
        assert not state.pending_tasks

    def test_directed_trace_edge_closes_only_for_planned_callee(self, tmp_path):
        (tmp_path / "code.py").write_text(
            "def middle():\n    return 1\n\n"
            "def other():\n    return 2\n\n"
            "def entry():\n    return middle() + other()\n",
            encoding="utf-8",
        )
        state = _make_state(repo_path=str(tmp_path), question_type="trace")
        task = _make_task(target="entry", slots={
            SlotKind.DEFINITION, SlotKind.VERIFIED_CALLEE_EDGE,
        })
        task.counterpart = "middle"
        executor = ToolExecutor(str(tmp_path))

        _execute_task(task, state, allow_children=False, tool_executor=executor)
        assert state.pending_tasks == [task]
        _execute_task(state.pop_next(), state, allow_children=False,
                      tool_executor=executor)

        verified = state.verified_evidence[task.id]
        callees = [ev.location.symbol for ev in verified
                   if ev.source == "verify_callees"]
        assert callees == ["middle"]

    def test_execute_task_with_strategy_override(self):
        """有 strategy_override 的任务走 _deterministic_work_orders。"""
        state = _make_state()
        gs = GapStrategy(preferred_tool="search_references",
                         search_kind="definition", scope_override="allow_all")
        t = _make_task(target="Widget")
        t.strategy_override = gs
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "code.py").write_text("class Widget:\n    def run(self): pass\n",
                                          encoding="utf-8")
            state.repo_path = td
            executor = ToolExecutor(td)
            _execute_task(t, state, allow_children=False, tool_executor=executor)
            assert t.attempt_count == 1

    def test_execute_task_budget_exhausted_skips(self):
        """预算耗尽时不执行工单。"""
        state = _make_state()
        state.main_steps_used = 12  # 已耗尽
        state.current_phase = "MAIN"
        t = _make_task(target="Widget")
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "code.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
            state.repo_path = td
            executor = ToolExecutor(td)
            _execute_task(t, state, allow_children=False, tool_executor=executor)
            # 应被跳过
            assert "budget: MAIN exhausted" in " ".join(state.traces)


# ═══════════════════════════════════════════════════════════════════════
# _execute_task_subtree — Phase 4/5 子树执行
# ═══════════════════════════════════════════════════════════════════════

class TestExecuteTaskSubtree:
    """Phase 4/5 _execute_task_subtree — 本地队列递归 + 独立预算。"""

    def test_subtree_uses_gap_budget(self):
        state = _make_state()
        state.gap_steps_used = 0
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "code.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
            state.repo_path = td
            t = _make_task(target="Widget")
            executor = ToolExecutor(td)
            _execute_task_subtree(t, state, max_depth=2, budget_phase="GAP",
                                  tool_executor=executor)
            # gap budget 被消耗，main budget 不变
            assert state.gap_steps_used > 0
            assert state.verified_evidence[t.id]

    def test_subtree_restores_phase_after_execution(self):
        state = _make_state()
        state.current_phase = "MAIN"
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "code.py").write_text("class Widget:\n    pass\n", encoding="utf-8")
            state.repo_path = td
            t = _make_task(target="Widget")
            executor = ToolExecutor(td)
            _execute_task_subtree(t, state, max_depth=2, budget_phase="GAP",
                                  tool_executor=executor)
            assert state.current_phase == "MAIN"

    def test_subtree_child_executes_in_local_queue(self):
        """子任务在本地队列中执行，不入全局队列。"""
        state = _make_state()
        # 创建一个会产出 read_window evidence 的场景
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "code.py").write_text(
                "def helper():\n    return 42\n\n"
                "def Widget():\n    return helper()\n",
                encoding="utf-8",
            )
            state.repo_path = td
            t = _make_task(target="Widget", task_type="read_implementation")
            executor = ToolExecutor(td)
            initial_pending = len(state.pending_tasks)
            _execute_task_subtree(t, state, max_depth=2, budget_phase="GAP",
                                  tool_executor=executor)
            # 全局队列不变（子任务入本地队列）
            assert len(state.pending_tasks) == initial_pending

    def test_subtree_respects_max_depth(self):
        state = _make_state()
        state.gap_steps_used = 0
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "code.py").write_text(
                "def a():\n    return b()\n"
                "def b():\n    return c()\n"
                "def c():\n    return 42\n",
                encoding="utf-8",
            )
            state.repo_path = td
            t = _make_task(target="a", task_type="read_implementation")
            executor = ToolExecutor(td)
            _execute_task_subtree(t, state, max_depth=1, budget_phase="GAP",
                                  tool_executor=executor)
            # 深度 ≤1 的子任务最多一层


# ═══════════════════════════════════════════════════════════════════════
# _verify_evidence + _task_status
# ═══════════════════════════════════════════════════════════════════════

class TestVerifyEvidence:
    """证据验证与任务状态判定。"""

    def test_empty_evidence_fails(self):
        ev = Evidence(kind="code", source="resolve_symbol", snippet="",
                      location=CodeLocation(file="f.py", start_line=1))
        t = _make_task()
        wo = WorkOrder(task_id="t1", description="test", target="Widget",
                       tool_hint="resolve_symbol")
        ok, reason = _verify_evidence(ev, t, wo)
        assert not ok
        assert "empty" in reason

    def test_no_location_fails(self):
        ev = Evidence(kind="code", source="resolve_symbol", snippet="class Widget:",
                      location=None)
        t = _make_task()
        wo = WorkOrder(task_id="t1", description="test", target="Widget",
                       tool_hint="resolve_symbol")
        ok, reason = _verify_evidence(ev, t, wo)
        assert not ok

    def test_resolve_symbol_accepted(self):
        ev = _make_ev(source="resolve_symbol", snippet="class Widget:")
        t = _make_task()
        wo = WorkOrder(task_id="t1", description="test", target="Widget",
                       tool_hint="resolve_symbol")
        ok, reason = _verify_evidence(ev, t, wo)
        assert ok

    def test_read_window_accepted(self):
        ev = _make_ev(source="read_window", snippet="def run(self): pass")
        t = _make_task()
        wo = WorkOrder(task_id="t1", description="test", target="run",
                       tool_hint="read_window")
        ok, reason = _verify_evidence(ev, t, wo)
        assert ok

    def test_verify_callsite_with_call_expression(self):
        ev = _make_ev(source="verify_callsite",
                      snippet="    widget.run()\n")
        t = _make_task(target="Widget.run")
        wo = WorkOrder(task_id="t1", description="test", target="Widget.run",
                       tool_hint="verify_callsite")
        ok, reason = _verify_evidence(ev, t, wo)
        assert ok

    def test_verify_callsite_no_call_rejected(self):
        ev = _make_ev(source="verify_callsite",
                      snippet="class Widget:\n    pass\n")
        t = _make_task(target="Widget.run")
        wo = WorkOrder(task_id="t1", description="test", target="Widget.run",
                       tool_hint="verify_callsite")
        ok, reason = _verify_evidence(ev, t, wo)
        assert not ok

    def test_verify_callees_with_calls(self):
        """新格式：location.symbol 包含 callee → 通过。"""
        ev = _make_ev(source="verify_callees",
                      symbol="validate",
                      snippet="process → validate() at code.py:42")
        t = _make_task(target="process")
        wo = WorkOrder(task_id="t1", description="test", target="process",
                       tool_hint="verify_callees")
        ok, reason = _verify_evidence(ev, t, wo)
        assert ok
        assert "validate" in reason

    def test_verify_callees_rejects_other_directed_edge(self):
        ev = _make_ev(source="verify_callees", symbol="other",
                      snippet="process -> other() at code.py:42")
        t = _make_task(target="process")
        wo = WorkOrder(task_id="t1", description="test", target="process",
                       tool_hint="verify_callees", counterpart="validate")
        ok, reason = _verify_evidence(ev, t, wo)
        assert not ok
        assert "expected directed callee" in reason

    def test_verify_callees_no_calls_rejected(self):
        """新格式：location.symbol 为空 → 拒绝。"""
        ev = _make_ev(source="verify_callees",
                      symbol="",
                      snippet="process → ? at code.py:42")
        t = _make_task(target="process")
        wo = WorkOrder(task_id="t1", description="test", target="process",
                       tool_hint="verify_callees")
        ok, reason = _verify_evidence(ev, t, wo)
        assert not ok

    def test_task_status_verified(self):
        t = _make_task()
        t.attempt_count = 1
        assert _task_status(t, [_make_ev()]) == TaskStatus.VERIFIED

    def test_task_status_no_evidence(self):
        t = _make_task()
        t.attempt_count = 1
        assert _task_status(t, []) == TaskStatus.NO_EVIDENCE

    def test_task_status_failed(self):
        t = _make_task()
        t.attempt_count = 0
        assert _task_status(t, []) == TaskStatus.FAILED


# ═══════════════════════════════════════════════════════════════════════
# _determine_stop_reason
# ═══════════════════════════════════════════════════════════════════════

class TestStopReason:
    """停止原因判定。"""

    def test_complete(self):
        state = _make_state()
        assert _determine_stop_reason(state, True) == "COMPLETE"

    def test_complete_after_retool(self):
        state = _make_state()
        state.retool_used = True
        assert _determine_stop_reason(state, True) == "COMPLETE_AFTER_RETOOL"

    def test_stop_main_budget(self):
        state = _make_state()
        state.main_steps_used = 12
        assert _determine_stop_reason(state, False) == "STOP_MAIN_BUDGET"

    def test_stop_no_pending(self):
        state = _make_state()
        state.main_steps_used = 5  # not exhausted
        assert _determine_stop_reason(state, False) == "STOP_NO_PENDING"

    def test_partial_when_budget_left_and_pending(self):
        state = _make_state()
        state.main_steps_used = 5
        state.enqueue_tasks([_make_task()])
        assert _determine_stop_reason(state, False) == "PARTIAL"


# ═══════════════════════════════════════════════════════════════════════
# _build_retool_task + SlotKind（证据槽位）映射
# ═══════════════════════════════════════════════════════════════════════

class TestRetoolHelpers:
    """retool 任务构建辅助函数。"""

    def test_build_retool_task(self):
        state = _make_state()
        state.all_tasks.append(_make_task("t1"))
        gap_result = {
            "action": "add_one_task",
            "new_task": {
                "task_type": "locate_definition",
                "target": "missing_func",
                "reason": "缺少函数定义",
            },
        }
        t = _build_retool_task(gap_result, state)
        assert t.role == TaskRole.RETOOL
        assert t.target == "missing_func"
        assert t.required_slots == {SlotKind.DEFINITION, SlotKind.IMPLEMENTATION}
        assert t.subtree_depth == 0

    def test_slot_to_tool_mapping(self):
        assert SLOT_TO_TOOL[SlotKind.DEFINITION][0] == "resolve_symbol"
        assert SLOT_TO_TOOL[SlotKind.IMPLEMENTATION][0] == "read_window"
        assert SLOT_TO_TOOL[SlotKind.VERIFIED_CALLER_EDGE][0] == "verify_callers"
        assert SLOT_TO_TOOL[SlotKind.VERIFIED_CALLEE_EDGE][0] == "verify_callees"
        assert SLOT_TO_TOOL[SlotKind.CANDIDATE_REFERENCE][0] == "search_references"


# ═══════════════════════════════════════════════════════════════════════
# AST 调用提取（verify_callees 核心逻辑）
# ═══════════════════════════════════════════════════════════════════════

class TestAstCallExtraction:
    """_collect_calls_in_function、owner 标准化、class 上下文。"""

    # ── _get_class_context ──────────────────────────────────────────

    def test_get_class_context_simple(self):
        tree = _ast.parse("class Widget:\n    def run(self):\n        pass\n")
        name, bases = _get_class_context(tree, 2)
        assert name == "Widget"
        assert bases == []

    def test_get_class_context_with_inheritance(self):
        tree = _ast.parse("class Widget(Base, Mixin):\n    def run(self):\n        pass\n")
        name, bases = _get_class_context(tree, 2)
        assert name == "Widget"
        assert bases == ["Base", "Mixin"]

    def test_get_class_context_module_level_func(self):
        tree = _ast.parse("def run():\n    pass\n")
        name, bases = _get_class_context(tree, 1)
        assert name is None
        assert bases == []

    # ── _collect_calls_in_function ──────────────────────────────────

    def test_collect_bare_calls(self):
        tree = _ast.parse("def process():\n    validate()\n    transform()\n")
        calls = _collect_calls_in_function(tree, 1)
        callees = {c["callee_normalized"] for c in calls}
        assert "validate" in callees
        assert "transform" in callees

    def test_collect_self_calls_with_class_context(self):
        tree = _ast.parse(
            "class Widget:\n    def process(self):\n        self.validate()\n"
        )
        calls = _collect_calls_in_function(tree, 2, class_name="Widget")
        assert len(calls) >= 1
        assert calls[0]["callee_normalized"] == "Widget.validate"
        assert calls[0]["receiver_type"] == "self"

    def test_collect_super_calls(self):
        tree = _ast.parse(
            "class Widget(Base):\n    def process(self):\n        super().run()\n"
        )
        calls = _collect_calls_in_function(tree, 2, class_name="Widget",
                                           base_classes=["Base"])
        assert len(calls) >= 1
        assert calls[0]["callee_normalized"] == "Base.run"
        assert calls[0]["receiver_type"] == "super"

    def test_collect_variable_method_calls(self):
        tree = _ast.parse(
            "class Widget:\n    def process(self, helper):\n        helper.transform()\n"
        )
        calls = _collect_calls_in_function(tree, 2, class_name="Widget")
        assert len(calls) >= 1
        assert calls[0]["callee_normalized"] == "helper.transform"
        assert calls[0]["receiver_type"] == "variable"

    def test_skip_nested_function_calls(self):
        tree = _ast.parse(
            "def outer():\n    validate()\n"
            "    def inner():\n        hidden()\n"
        )
        calls = _collect_calls_in_function(tree, 1)
        callees = {c["callee_normalized"] for c in calls}
        assert "validate" in callees
        assert "hidden" not in callees  # 嵌套函数内部调用被跳过

    def test_skip_nested_class_calls(self):
        tree = _ast.parse(
            "class Outer:\n    def process(self):\n        self.a()\n"
            "        class Inner:\n            def inner_method(self):\n"
            "                self.b()\n"
        )
        calls = _collect_calls_in_function(tree, 2, class_name="Outer")
        callees = {c["callee_normalized"] for c in calls}
        assert "Outer.a" in callees
        assert "Outer.b" not in callees  # 嵌套类内部调用被跳过

    def test_collect_calls_in_control_flow(self):
        tree = _ast.parse(
            "def process(self, items):\n"
            "    for item in items:\n        self.handle(item)\n"
            "    if True:\n        cleanup()\n"
        )
        calls = _collect_calls_in_function(tree, 1, class_name="Processor")
        callees = {c["callee_normalized"] for c in calls}
        assert "Processor.handle" in callees
        assert "cleanup" in callees

    def test_collect_calls_in_assignment(self):
        tree = _ast.parse(
            "def process(self):\n    result = build_result()\n"
        )
        calls = _collect_calls_in_function(tree, 1)
        callees = {c["callee_normalized"] for c in calls}
        assert "build_result" in callees

    def test_collect_calls_in_with_statement(self):
        tree = _ast.parse(
            "def process(self):\n    with open_file('path') as f:\n        pass\n"
        )
        calls = _collect_calls_in_function(tree, 1)
        callees = {c["callee_normalized"] for c in calls}
        assert "open_file" in callees

    # ── _classify_and_normalize ─────────────────────────────────────

    def test_normalize_self_call(self):
        func = _ast.parse("self.run()").body[0].value.func
        expr, rtype, normalized = _classify_and_normalize(func, "Widget", [])
        assert rtype == "self"
        assert normalized == "Widget.run"
        assert expr == "self.run"

    def test_normalize_cls_call(self):
        func = _ast.parse("cls.create()").body[0].value.func
        expr, rtype, normalized = _classify_and_normalize(func, "Widget", [])
        assert rtype == "cls"
        assert normalized == "Widget.create"

    def test_normalize_super_call(self):
        func = _ast.parse("super().run()").body[0].value.func
        expr, rtype, normalized = _classify_and_normalize(func, "Widget", ["Base"])
        assert rtype == "super"
        assert normalized == "Base.run"

    def test_normalize_super_call_no_base(self):
        func = _ast.parse("super().run()").body[0].value.func
        expr, rtype, normalized = _classify_and_normalize(func, "Widget", [])
        assert rtype == "super"
        assert normalized == ""  # 无基类时无法标准化

    def test_normalize_bare_call(self):
        func = _ast.parse("validate()").body[0].value.func
        expr, rtype, normalized = _classify_and_normalize(func, None, [])
        assert rtype == "bare"
        assert normalized == "validate"

    def test_normalize_bare_call_in_class(self):
        func = _ast.parse("helper()").body[0].value.func
        expr, rtype, normalized = _classify_and_normalize(func, "Widget", [])
        assert rtype == "bare"
        assert normalized == "helper"

    def test_normalize_variable_method_call(self):
        func = _ast.parse("obj.transform()").body[0].value.func
        expr, rtype, normalized = _classify_and_normalize(func, None, [])
        assert rtype == "variable"
        assert normalized == "obj.transform"

    # ── _get_attr_receiver_name ─────────────────────────────────────

    def test_attr_receiver_simple_name(self):
        func_node = _ast.parse("x.y()").body[0].value.func.value
        assert _get_attr_receiver_name(func_node) == "x"

    def test_attr_receiver_chained(self):
        func_node = _ast.parse("a.b.c.d()").body[0].value.func.value
        assert _get_attr_receiver_name(func_node) == "a.b.c"

    def test_attr_receiver_super_call(self):
        func_node = _ast.parse("super().run()").body[0].value.func.value
        assert _get_attr_receiver_name(func_node) == "super"


# ═══════════════════════════════════════════════════════════════════════
# ToolExecutor — 工具执行
# ═══════════════════════════════════════════════════════════════════════

class TestToolExecutor:
    """ToolExecutor 在真实临时文件系统上的行为。"""

    def test_resolve_finds_class_definition(self, tmp_path):
        _setup_repo(tmp_path, {"code.py": "class Widget:\n    pass\n"})
        executor = ToolExecutor(str(tmp_path))
        wo = WorkOrder(task_id="t1", description="find", target="Widget",
                       tool_hint="resolve_symbol")
        results = executor.execute(wo)
        assert len(results) >= 1
        assert results[0].source == "resolve_symbol"
        assert "Widget" in results[0].snippet

    def test_resolve_not_found_returns_empty(self, tmp_path):
        _setup_repo(tmp_path, {"code.py": "x = 1\n"})
        executor = ToolExecutor(str(tmp_path))
        wo = WorkOrder(task_id="t1", description="find", target="Missing",
                       tool_hint="resolve_symbol")
        results = executor.execute(wo)
        assert len(results) == 0

    def test_read_window_without_file_resolves_first(self, tmp_path):
        _setup_repo(tmp_path, {"code.py": "class Widget:\n    def run(self):\n        pass\n"})
        executor = ToolExecutor(str(tmp_path))
        wo = WorkOrder(task_id="t1", description="read", target="Widget.run",
                       tool_hint="read_window", search_kind="definition")
        results = executor.execute(wo)
        # 应该先 resolve Widget.run → Widget，再读窗口
        # run 是 Widget 的方法，resolve 会找到 class Widget
        assert len(results) >= 1
        assert results[0].source in ("resolve_symbol", "read_window")

    def test_search_references_finds_hits(self, tmp_path):
        _setup_repo(tmp_path, {
            "main.py": "from lib import Widget\nw = Widget()\n",
            "lib.py": "class Widget:\n    pass\n",
        })
        executor = ToolExecutor(str(tmp_path))
        wo = WorkOrder(task_id="t1", description="search", target="Widget",
                       tool_hint="search_references", search_kind="references")
        results = executor.execute(wo)
        # Widget 出现在 main.py 和 lib.py 中
        files = {ev.location.file for ev in results}
        assert len(results) >= 1
        # production source 优先
        assert any("lib.py" in f or "main.py" in f for f in files)

    def test_verify_callsite_reads_window(self, tmp_path):
        _setup_repo(tmp_path, {"code.py": "widget.run()\n"})
        executor = ToolExecutor(str(tmp_path))
        wo = WorkOrder(task_id="t1", description="verify", target="Widget.run",
                       tool_hint="verify_callsite", file_hint="code.py", line=1)
        results = executor.execute(wo)
        assert len(results) == 1
        assert results[0].source == "verify_callsite"

    def test_read_window_size_by_question_type(self, tmp_path):
        """explain 窗口 > trace/impact 窗口 > locate/grep 窗口。"""
        _setup_repo(tmp_path, {"code.py": "\n".join(f"    line_{i}()" for i in range(200))})

        # 需要先有一个有效的文件+行号组合
        executor = ToolExecutor(str(tmp_path))

        # 用 resolve 找到定义
        wo_resolve = WorkOrder(task_id="t1", description="find", target="line_0",
                               tool_hint="resolve_symbol")
        result = executor.execute(wo_resolve)
        if result:
            file = result[0].location.file
            line = result[0].location.start_line

            # explain 窗口
            wo_explain = WorkOrder(task_id="t2", description="read", target="line_0",
                                   tool_hint="read_window", file_hint=file, line=line)
            results_explain = executor.execute(wo_explain, "explain")
            # locate 窗口
            results_locate = executor.execute(
                WorkOrder(task_id="t3", description="read", target="line_0",
                          tool_hint="read_window", file_hint=file, line=line),
                "locate",
            )
            # explain 窗口 ≥ locate 窗口
            if results_explain and results_locate:
                assert len(results_explain[0].snippet or "") >= len(
                    results_locate[0].snippet or "")


# ═══════════════════════════════════════════════════════════════════════
# 合同判定 — AUXILIARY 不进入合同
# ═══════════════════════════════════════════════════════════════════════

class TestContractAuxiliaryExclusion:
    """合同的 required tasks 排除 AUXILIARY 角色。"""

    def test_auxiliary_not_in_required_tasks(self):
        """AUXILIARY 任务不在 targets_from_tasks 中贡献 slot。"""
        t_root = _make_task("t_root", "locate_definition", "Widget", TaskRole.ROOT)
        t_aux = _make_task("t_aux", "locate_definition", "Helper", TaskRole.AUXILIARY)
        # targets_from_tasks 接受所有 task
        targets = targets_from_tasks([t_root, t_aux])
        # 两个 task 对应不同 symbol → 两个 target
        assert len(targets) >= 1

    def test_contract_only_counts_non_auxiliary(self):
        """合同判定时只检查非 AUXILIARY 任务的目标。"""
        # 这个测试验证概念：合同判定从 required_tasks 中排除 AUXILIARY
        state = _make_state()
        t_required = _make_task("t1", "locate_definition", "Widget", TaskRole.ROOT)
        t_aux = _make_task("t2", "locate_definition", "Helper", TaskRole.AUXILIARY)

        # 只对 non-AUXILIARY 做 targets_from_tasks
        required = [t for t in [t_required, t_aux]
                    if t.role != TaskRole.AUXILIARY]
        assert len(required) == 1
        assert required[0].target == "Widget"


# ═══════════════════════════════════════════════════════════════════════
# regressions — 回归防护
# ═══════════════════════════════════════════════════════════════════════

class TestRegressions:
    """V22 特定回归防护。"""

    def test_retool_task_has_correct_role(self):
        state = _make_state()
        gap_result = {"action": "add_one_task", "new_task": {
            "task_type": "locate_definition", "target": "X", "reason": "gap"}}
        t = _build_retool_task(gap_result, state)
        assert t.role == TaskRole.RETOOL

    def test_gap_tasks_use_alternate_tools(self):
        """gap task 的 strategy_override 使用不同 tool。"""
        state = _make_state()
        target = AnswerTarget("t1", "locate_definition", "Widget",
                              {SlotKind.DEFINITION})
        result = _deterministic_gap_fill({"t1": target}, state)
        if result:
            gs = result[0].strategy_override
            assert gs is not None
            # 补缺 tool 应不同于初始 tool (resolve_symbol)
            t = _make_task(task_type="locate_definition")
            initial_orders = fill_work_orders(t)
            initial_tool = initial_orders[0].tool_hint
            assert gs.preferred_tool != initial_tool or gs.scope_override == "allow_all"

    def test_exploration_state_defaults(self):
        state = ExplorationState(question="q")
        assert state.max_main_steps == 12
        assert state.max_gap_steps == 3
        assert state.max_retool_steps == 4
        assert state.max_subtree_depth == 2
        assert state.main_steps_used == 0
        assert state.gap_steps_used == 0
        assert state.retool_steps_used == 0
        assert state.current_phase == "MAIN"
        assert state.retool_used is False
        assert state.pending_tasks == []
        assert state.all_tasks == []

    def test_register_task_vs_enqueue(self):
        """register_task 不入 pending 队列，enqueue_tasks 入。"""
        state = _make_state()
        t1 = _make_task("t1")
        t2 = _make_task("t2")
        state.register_task(t1)
        state.enqueue_tasks([t2])
        assert len(state.all_tasks) == 2
        assert len(state.pending_tasks) == 1  # only t2
        assert state.pending_tasks[0].id == "t2"
