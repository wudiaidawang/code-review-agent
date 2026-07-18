"""V1.1 Investigation Agent — 代码库探索模式

M3: 假设驱动的有限状态调查循环 + 预算管理 + 跨工具关联 + 续问上下文。
支持 investigation_id 持久化、续问复用已有证据、跨轮证据引用。
"""

import hashlib
import os
import re
import time
import uuid
from dataclasses import dataclass, field

from app.models.evidence import Evidence
from app.models.location import CodeLocation
from app.core.workspace import WorkspaceManager
from app.tools.llm_tool import chat
from app.tools.search_tool import SearchTool
from app.tools.ast_tool import ASTTool
from app.tools.dependency_tool import DependencyTool
from app.tools.git_tool import GitTool
from app.tools.contract import ToolRequest
from app.pipeline.knowledge_retriever import StaticKnowledge


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


def _normalize_search_keyword(value: str) -> str | None:
    """Return a searchable terminal symbol; reject broad natural-language terms."""
    candidate = value.strip().strip("`'\"()[]{} ")
    if not candidate:
        return None
    candidate = re.split(r"(?:\.|::)", candidate)[-1].strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", candidate):
        return None
    if candidate.lower() in _KEYWORD_STOP_WORDS | _GENERIC_SEARCH_TERMS:
        return None
    return candidate


# ---- 数据结构 ------------------------------------------------------------

@dataclass
class StepRecord:
    """单步调查记录 — 保证完整可重放。"""
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
            "step": self.step,
            "tool": self.tool,
            "params": self.params,
            "status": self.status,
            "evidence_count": self.evidence_count,
            "hypothesis_before": self.hypothesis_before,
            "hypothesis_after": self.hypothesis_after,
            "decision": self.decision,
            "budget_reason": self.budget_reason,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class InvestigationResult:
    """一次调查的完整产出。"""
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
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "evidence": [e.to_dict() for e in self.evidence],
            "files_visited": self.files_visited,
            "findings": self.findings,
            "plan": self.plan,
            "trace": self.trace,
            "steps": self.steps,
            "investigation_id": self.investigation_id,
            "is_follow_up": self.is_follow_up,
            "reused_evidence_refs": self.reused_evidence_refs,
            "duration_ms": round(self.duration_ms, 1),
        }


# ---- 调查会话持久化 -------------------------------------------------------

class InvestigationStore:
    """调查会话持久化存储 — 支持续问上下文复用。"""

    def __init__(self):
        self._sessions: dict[str, dict] = {}

    def save(self, investigation_id: str, state: "InvestigationState") -> None:
        self._sessions[investigation_id] = {
            "question": state.question,
            "goal": state.goal,
            "keywords": list(state.keywords),
            "hypotheses": list(state.hypotheses),
            "confirmed": list(state.confirmed),
            "evidence": [e.to_dict() for e in state.evidence],
            "steps": [s.to_dict() for s in state.steps],
            "files_visited": sorted(state.files_visited),
            "trace": list(state.trace),
            "files_read": state.files_read,
            "tokens_used": state.tokens_used,
        }

    def load(self, investigation_id: str) -> dict | None:
        return self._sessions.get(investigation_id)

    def delete(self, investigation_id: str) -> None:
        self._sessions.pop(investigation_id, None)

    @property
    def session_count(self) -> int:
        return len(self._sessions)


# ---- 问题类型识别 --------------------------------------------------------

_QUESTION_PATTERNS = [
    (r"在哪|在哪里|where|定义|defined|位置|location|哪个文件|which file", "locate"),
    (r"做什么|干什么|what.*do|作用|功能|purpose|负责", "explain"),
    (r"影响|affect|impact|后果|导致|break|破坏", "impact"),
    (r"调用|calls?|invoke|依赖|depends|import|连接|connect", "trace"),
    (r"所有|全部|all|every|list|列举|find.*all", "grep"),
]


def _classify(question: str) -> str:
    lower = question.lower()
    for pattern, qtype in _QUESTION_PATTERNS:
        if re.search(pattern, lower):
            return qtype
    return "locate"


# ---- 工具优先级表 --------------------------------------------------------

_TOOL_PRIORITY = {
    "locate":  ["search", "search_filename", "python_ast"],
    "explain": ["search", "search_filename", "python_ast", "dependency", "knowledge"],
    "trace":   ["search", "search_filename", "python_ast", "dependency", "git"],
    "impact":  ["search", "search_filename", "python_ast", "dependency", "git"],
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


# ---- 假设模板 ------------------------------------------------------------

_HYPOTHESIS_TEMPLATES = {
    "locate":  "符号 {kw} 定义在某个 .py 文件中",
    "explain": "符号 {kw} 是一个函数/类，其作用可通过代码和 AST 推断",
    "trace":   "符号 {kw} 被其他函数调用，也调用了其他函数",
    "impact":  "修改符号 {kw} 会影响其调用者和被调用者",
    "grep":    "仓库中有若干处使用了模式 {kw}",
}


# ---- InvestigationAgent -------------------------------------------------

class InvestigationAgent:
    """M3: 假设驱动的有限状态调查循环 + 续问上下文。

    支持 investigation_id 持久化，续问复用已有证据，跨轮证据引用。
    """

    def __init__(self, call_llm=None, store: InvestigationStore | None = None):
        self.call_llm = call_llm or chat
        self.store = store or InvestigationStore()

    # ---- 主入口 ----------------------------------------------------------

    def investigate(self, repo_path: str, question: str) -> InvestigationResult:
        t0 = time.perf_counter()
        abs_path = os.path.abspath(repo_path)

        goal = _classify(question)
        keywords = self._extract_keywords(question)
        if not keywords:
            inv_id = self._new_investigation_id(question)
            result = InvestigationResult(question=question, investigation_id=inv_id)
            result.answer = "无法从问题中提取关键词，请提供具体的函数名、类名或文件名。"
            result.duration_ms = (time.perf_counter() - t0) * 1000
            result.trace.append("error=empty_keywords")
            return result

        state = InvestigationState(question=question, goal=goal, keywords=keywords)
        self._seed_hypotheses(state)

        # 主循环
        while state.steps_remaining > 0:
            budget_block = self._check_budget(state)
            if budget_block:
                dummy = StepRecord(
                    step=len(state.steps) + 1, tool="(blocked)",
                    decision="BUDGET", budget_reason=budget_block,
                )
                state.steps.append(dummy)
                state.trace.append(f"budget_exhausted: {budget_block}")
                break

            tool_name = self._select_next_tool(state)
            if tool_name is None:
                state.trace.append("decision=no_applicable_tool")
                break

            step = self._execute_step(abs_path, state, tool_name)
            state.steps.append(step)
            self._update_hypotheses(state, step)

            decision, budget_reason = self._evaluate(state, step)
            step.decision = decision
            step.budget_reason = budget_reason
            state.trace.append(
                f"step_{step.step}: tool={step.tool} status={step.status} "
                f"evidence={step.evidence_count} decision={decision}"
                + (f" budget={budget_reason}" if budget_reason else "")
            )

            if decision != "CONTINUE":
                break

        # 生成 investigation_id 并持久化
        inv_id = self._new_investigation_id(question)
        self.store.save(inv_id, state)

        result = self._synthesize(state, abs_path, t0)
        result.investigation_id = inv_id
        result.plan = [s.tool for s in state.steps]
        result.trace = state.trace
        result.steps = [s.to_dict() for s in state.steps]
        return result

    # ---- 续问入口 ----------------------------------------------------------

    def follow_up(self, repo_path: str, investigation_id: str,
                  question: str) -> InvestigationResult:
        """接续已有调查，复用证据回答新问题。

        若已有证据能覆盖新问题 → 直接合成（0 次新工具调用）。
        若部分覆盖 → 加载状态、追加工具步骤、合并证据。
        """
        t0 = time.perf_counter()
        abs_path = os.path.abspath(repo_path)

        session = self.store.load(investigation_id)
        if session is None:
            result = InvestigationResult(
                question=question, investigation_id=investigation_id,
                is_follow_up=True,
            )
            result.answer = f"未找到调查会话 {investigation_id}，请先执行首次调查。"
            result.duration_ms = (time.perf_counter() - t0) * 1000
            return result

        keywords = self._extract_keywords(question)
        if not keywords:
            result = InvestigationResult(
                question=question, investigation_id=investigation_id,
                is_follow_up=True,
            )
            result.answer = "无法从续问中提取关键词。"
            result.duration_ms = (time.perf_counter() - t0) * 1000
            return result

        # 检查已有证据是否覆盖新问题
        matched_refs = self._match_existing_evidence(session, question, keywords)
        sufficient = len(matched_refs) >= 3

        if sufficient:
            # 纯复用：已有证据足够回答
            result = InvestigationResult(
                question=question,
                investigation_id=investigation_id,
                is_follow_up=True,
                reused_evidence_refs=matched_refs[:10],
                files_visited=session["files_visited"],
            )
            result = self._synthesize_follow_up(result, session, question,
                                                matched_refs, abs_path, t0)
            return result

        # 部分覆盖：恢复状态，追加工具步骤
        state = self._restore_state(session, question)
        state.keywords = keywords

        while state.steps_remaining > 0:
            budget_block = self._check_budget(state)
            if budget_block:
                dummy = StepRecord(
                    step=len(state.steps) + 1, tool="(blocked)",
                    decision="BUDGET", budget_reason=budget_block,
                )
                state.steps.append(dummy)
                state.trace.append(f"budget_exhausted: {budget_block}")
                break

            tool_name = self._select_next_tool(state)
            if tool_name is None:
                state.trace.append("decision=no_applicable_tool")
                break

            step = self._execute_step(abs_path, state, tool_name)
            state.steps.append(step)
            self._update_hypotheses(state, step)

            decision, budget_reason = self._evaluate(state, step)
            step.decision = decision
            step.budget_reason = budget_reason
            state.trace.append(
                f"step_{step.step}: tool={step.tool} status={step.status} "
                f"evidence={step.evidence_count} decision={decision}"
                + (f" budget={budget_reason}" if budget_reason else "")
            )

            if decision != "CONTINUE":
                break

        self.store.save(investigation_id, state)

        result = self._synthesize(state, abs_path, t0)
        result.investigation_id = investigation_id
        result.is_follow_up = True
        result.reused_evidence_refs = matched_refs[:10]
        result.plan = [s.tool for s in state.steps]
        result.trace = state.trace
        result.steps = [s.to_dict() for s in state.steps]
        return result

    # ---- 证据匹配 ---------------------------------------------------------

    @staticmethod
    def _match_existing_evidence(session: dict, question: str,
                                 keywords: list[str]) -> list[str]:
        """在已有证据中搜索与新问题关键词匹配的条目，返回引用列表。"""
        refs: list[str] = []
        kw_lower = {k.lower() for k in keywords}
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

    # ---- 状态恢复 ---------------------------------------------------------

    @staticmethod
    def _restore_state(session: dict, new_question: str) -> "InvestigationState":
        """从持久化会话恢复 InvestigationState，保留已有证据。"""
        evidence = [Evidence.from_dict(e) for e in session.get("evidence", [])]
        state = InvestigationState(
            question=new_question,
            goal=_classify(new_question),
            keywords=list(session.get("keywords", [])),
            hypotheses=list(session.get("hypotheses", [])),
            confirmed=list(session.get("confirmed", [])),
            evidence=evidence,
            files_visited=set(session.get("files_visited", [])),
            trace=list(session.get("trace", [])),
            files_read=session.get("files_read", 0),
            tokens_used=session.get("tokens_used", 0),
        )
        # 恢复之前的步骤记录
        for s in session.get("steps", []):
            state.steps.append(StepRecord(
                step=s.get("step", 0),
                tool=s.get("tool", ""),
                params=s.get("params", {}),
                status=s.get("status", "success"),
                evidence_count=s.get("evidence_count", 0),
                hypothesis_after=s.get("hypothesis_after", ""),
                decision=s.get("decision", ""),
                budget_reason=s.get("budget_reason", ""),
                duration_ms=s.get("duration_ms", 0),
            ))
        return state

    # ---- 答案合成（续问专用）-----------------------------------------------

    def _synthesize_follow_up(self, result: InvestigationResult,
                              session: dict, question: str,
                              matched_refs: list[str],
                              repo_path: str, t0: float) -> InvestigationResult:
        """基于已有证据直接合成续问答案，带跨轮引用。"""
        sys_prompt = (
            "你是一个代码库调查助手。用户正在对之前的调查进行追问。"
            "以下证据来自上一轮调查，请直接基于这些证据回答用户的新问题。"
            "回答必须引用具体文件路径和行号。如果已有证据不足以回答，请明确说明。"
            "在回答末尾标注引用了上一轮的哪几条证据。用中文回答。"
        )

        evidence_text = "\n".join(
            f"[ref{i+1}] {ref}" for i, ref in enumerate(matched_refs[:10])
        )

        user_prompt = f"""## 新问题（续问）
{question}

## 上一轮问题
{session.get('question', '')}

## 已有证据（{len(matched_refs)} 条匹配）
{evidence_text}

请基于以上已有证据回答新问题。回答末尾列出引用的证据引用编号。"""

        try:
            answer = self.call_llm(user_prompt, system=sys_prompt, temperature=0.3, max_tokens=1200)
            result.answer = answer.strip()
        except Exception:
            result.answer = (
                f"（LLM 不可用）基于已有 {len(matched_refs)} 条证据回答：\n\n"
                + "\n".join(f"- {ref}" for ref in matched_refs[:10])
            )

        result.evidence = [Evidence.from_dict(e) for e in session.get("evidence", [])]
        result.trace = [f"follow_up: reused {len(matched_refs)} evidence refs, 0 new tool calls"]
        result.steps = []
        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    # ---- 预算检查 ----------------------------------------------------------

    @staticmethod
    def _check_budget(state: "InvestigationState") -> str:
        if state.steps_remaining <= 0:
            return "steps"
        if state.is_files_exhausted:
            return "files"
        if state.is_token_exhausted:
            return "tokens"
        return ""

    # ---- 状态初始化 -------------------------------------------------------

    @staticmethod
    def _seed_hypotheses(state: "InvestigationState") -> None:
        kw_str = "、".join(state.keywords)
        template = _HYPOTHESIS_TEMPLATES.get(state.goal, _HYPOTHESIS_TEMPLATES["locate"])
        state.hypotheses.append(template.format(kw=kw_str))

    # ---- 工具选择 ----------------------------------------------------------

    def _select_next_tool(self, state: "InvestigationState") -> str | None:
        used = {s.tool for s in state.steps}
        candidates = list(_TOOL_PRIORITY.get(state.goal, ["search"]))
        candidates = [t for t in candidates if t not in used]

        if not state.files_visited and "search" not in used:
            return "search"

        # grep 未命中不等于“仓库无证据”。先做确定性的文件名恢复，
        # 再允许 NO_EVIDENCE 终止，避免低质量首次调查拖累续问复用。
        if (state.steps and state.steps[-1].tool == "search"
                and state.steps[-1].evidence_count == 0
                and "search_filename" in candidates):
            return "search_filename"

        # 文件名搜索仅是 grep 无命中的恢复路径，不应在已有内容证据后
        # 额外消耗一步或干扰后续 AST/依赖分析。
        candidates = [t for t in candidates if t != "search_filename"]

        candidates = [t for t in candidates if not self._is_duplicate(state, t)]

        if state.steps:
            candidates = self._correlate_candidates(state, candidates)

        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        return self._llm_rank_tools(state, candidates)

    # ---- 跨工具关联 -------------------------------------------------------

    @staticmethod
    def _correlate_candidates(state: "InvestigationState",
                              candidates: list[str]) -> list[str]:
        last = state.steps[-1]
        if last.status == "failed" or last.evidence_count == 0:
            return candidates

        if last.tool == "search":
            py_count = sum(1 for f in state.files_visited if f.endswith(".py"))
            if py_count > 0 and "python_ast" in candidates:
                candidates.remove("python_ast")
                candidates.insert(0, "python_ast")
        elif last.tool == "python_ast":
            has_hits = any(
                kw.lower() in f for kw in state.keywords
                for f in state.files_visited
            )
            if has_hits and "dependency" in candidates:
                candidates.remove("dependency")
                candidates.insert(0, "dependency")
        elif last.tool == "dependency":
            if last.evidence_count > 0 and "knowledge" in candidates:
                candidates.remove("knowledge")
                candidates.insert(0, "knowledge")

        return candidates

    # ---- LLM 辅助排序 -----------------------------------------------------

    def _llm_rank_tools(self, state: "InvestigationState",
                        candidates: list[str]) -> str:
        if len(candidates) <= 1:
            return candidates[0]

        tool_descriptions = {
            "search": "搜索代码库中的关键词/模式",
            "python_ast": "分析 Python 文件的结构（函数、类、调用关系）",
            "dependency": "分析 import 和依赖清单变更",
            "git": "查看最近的 git 变更历史",
            "knowledge": "检索安全规范/最佳实践知识库",
        }
        candidate_desc = "\n".join(
            f"- {t}: {tool_descriptions.get(t, t)}" for t in candidates
        )

        prompt = (
            f"问题：{state.question}\n"
            f"当前已确认：{', '.join(state.confirmed) if state.confirmed else '无'}\n"
            f"待验证假设：{state.hypotheses[0] if state.hypotheses else '无'}\n"
            f"已访问文件：{len(state.files_visited)} 个\n"
            f"可用工具：\n{candidate_desc}\n\n"
            f"请选择下一步最应该使用哪个工具。只输出工具名（不含引号）。"
        )

        try:
            choice = self.call_llm(prompt, system="你是一个代码调查策略助手。", temperature=0, max_tokens=20)
            choice = choice.strip().strip('"').strip("'")
            if choice in candidates:
                return choice
        except Exception:
            pass

        return candidates[0]

    # ---- 去重辅助 ---------------------------------------------------------

    @staticmethod
    def _is_duplicate(state: "InvestigationState", tool_name: str) -> bool:
        if tool_name not in _DEDUP_KEYS:
            return False
        key_fields = _DEDUP_KEYS[tool_name]({})
        current_fp = _hash_params(tool_name, state, key_fields)
        for prev in state.steps:
            if prev.tool != tool_name:
                continue
            prev_fp = _hash_params(tool_name, state, key_fields)
            if prev_fp == current_fp:
                return True
        return False

    # ---- 工具执行 ---------------------------------------------------------

    def _execute_step(self, repo_path: str, state: "InvestigationState",
                      tool_name: str) -> StepRecord:
        t0 = time.perf_counter()
        hypothesis_before = state.hypotheses[0] if state.hypotheses else ""
        step = StepRecord(
            step=len(state.steps) + 1,
            tool=tool_name,
            hypothesis_before=hypothesis_before,
        )

        try:
            if tool_name in ("search", "search_filename"):
                search_type = "filename" if tool_name == "search_filename" else "grep"
                step.params = {"repo_path": repo_path, "query": state.keywords,
                               "search_type": search_type, "max_results": 50}
                result = SearchTool().execute(ToolRequest(tool="search", params=step.params))
                self._ingest_search_result(state, result)

            elif tool_name == "python_ast":
                py_files = self._read_python_files(repo_path, state)
                step.params = {"file_count": len(py_files)}
                if not py_files:
                    step.status = "failed"
                    step.decision = "NO_EVIDENCE"
                    step.duration_ms = (time.perf_counter() - t0) * 1000
                    return step
                total_chars = sum(len(src) for _, src in py_files)
                state.files_read += len(py_files)
                state.tokens_used += self._estimate_tokens(total_chars)
                result = ASTTool().execute(ToolRequest(
                    tool="python_ast", params={"files": py_files},
                ))
                self._ingest_ast_result(state, result)

            elif tool_name == "dependency":
                py_files = self._read_python_files(repo_path, state)
                step.params = {"file_count": len(py_files),
                               "changed_files": len(state.files_visited)}
                total_chars = sum(len(src) for _, src in py_files)
                state.files_read += len(py_files)
                state.tokens_used += self._estimate_tokens(total_chars)
                result = DependencyTool().execute(ToolRequest(
                    tool="dependency", params={
                        "files": py_files,
                        "changed_files": list(state.files_visited),
                    },
                ))

            elif tool_name == "git":
                step.params = {"repo_path": repo_path, "base_ref": "HEAD~5", "head_ref": "HEAD"}
                result = GitTool().execute(ToolRequest(tool="git", params=step.params))

            elif tool_name == "knowledge":
                kw_str = " ".join(state.keywords)
                entries = StaticKnowledge().retrieve(kw_str, top_k=5)
                step.params = {"query": kw_str, "top_k": 5}
                result = None
                for entry in entries:
                    state.evidence.append(Evidence(
                        kind="knowledge", source=entry.get("source", "static_knowledge"),
                        location=CodeLocation(file="(knowledge)", start_line=0),
                        snippet=entry.get("content", ""),
                        confidence=0.7,
                    ))

            else:
                step.status = "failed"
                step.duration_ms = (time.perf_counter() - t0) * 1000
                return step

            if tool_name != "knowledge":
                step.status = result.status if result else "success"
                step.evidence_count = len(result.evidence) if result else len(entries)
                if result and result.evidence:
                    state.evidence.extend(result.evidence)
                    ev_chars = sum(len(ev.snippet or "") for ev in result.evidence)
                    state.tokens_used += self._estimate_tokens(ev_chars)

        except Exception as exc:
            step.status = "failed"
            state.trace.append(f"tool_error: {tool_name}: {exc}")

        step.duration_ms = (time.perf_counter() - t0) * 1000
        return step

    # ---- 证据摄取 ---------------------------------------------------------

    @staticmethod
    def _ingest_search_result(state: "InvestigationState", result) -> None:
        matches = result.artifacts.get("matches", [])
        for m in matches[:50]:
            fpath = m.get("file", "")
            if fpath:
                state.files_visited.add(fpath)

    @staticmethod
    def _ingest_ast_result(state: "InvestigationState", result) -> None:
        symbols = result.artifacts.get("symbol_index", [])
        keywords_lower = {k.lower() for k in state.keywords}
        for sym in symbols:
            name = sym.get("name", "")
            calls = sym.get("calls", [])
            if any(kw in name.lower() or any(kw in call.lower() for call in calls)
                   for kw in keywords_lower):
                loc = sym.get("location", {})
                fpath = loc.get("file", "")
                if fpath:
                    state.files_visited.add(fpath)

    # ---- 假设更新 ---------------------------------------------------------

    @staticmethod
    def _update_hypotheses(state: "InvestigationState", step: StepRecord) -> None:
        if step.evidence_count > 0 and state.hypotheses:
            confirmed = state.hypotheses.pop(0)
            state.confirmed.append(confirmed)

            kw_str = "、".join(state.keywords)
            if step.tool == "search" and state.files_visited:
                py_files = [f for f in state.files_visited if f.endswith(".py")]
                if py_files and state.goal in ("locate", "explain", "trace", "impact"):
                    state.hypotheses.append(
                        f"文件 {', '.join(sorted(py_files)[:3])} 中包含相关符号，"
                        f"需通过 AST 分析代码结构"
                    )
                elif state.goal == "trace":
                    state.hypotheses.append(f"需追踪 {kw_str} 的调用者和被调用者")
            elif step.tool == "python_ast" and state.goal in ("trace", "impact"):
                state.hypotheses.append(
                    f"已分析 {kw_str} 的结构，需追踪其依赖链和影响范围"
                )
            elif step.tool == "dependency" and state.goal in ("trace", "impact"):
                state.hypotheses.append(
                    f"已分析 {kw_str} 的依赖关系，汇总影响范围"
                )
        elif step.evidence_count == 0 and state.hypotheses:
            pass

        step.hypothesis_after = state.hypotheses[0] if state.hypotheses else "(无待验证假设)"

    # ---- 决策评估 ---------------------------------------------------------

    @staticmethod
    def _evaluate(state: "InvestigationState", step: StepRecord) -> tuple[str, str]:
        if (step.tool == "search" and step.evidence_count == 0
                and not any(s.tool == "search_filename" for s in state.steps)):
            return "CONTINUE", ""
        if step.evidence_count == 0 and step.status != "failed":
            return "NO_EVIDENCE", ""
        if state.steps_remaining <= 0:
            return "BUDGET", "steps"
        if state.is_files_exhausted:
            return "BUDGET", "files"
        if state.is_token_exhausted:
            return "BUDGET", "tokens"
        if not state.hypotheses:
            return "STOP", ""
        return "CONTINUE", ""

    # ---- 答案合成 ---------------------------------------------------------

    def _synthesize(self, state: "InvestigationState", repo_path: str,
                    t0: float) -> InvestigationResult:
        result = InvestigationResult(
            question=state.question,
            evidence=state.evidence,
            files_visited=sorted(state.files_visited)[:20],
        )

        context_chunks: list[str] = []
        workspace = None
        try:
            workspace = WorkspaceManager().prepare(repo_path, "HEAD")
            read_limit = min(5, state.files_max - state.files_read)
            for fpath in list(state.files_visited)[:read_limit]:
                try:
                    content = workspace.read_file(fpath)[:3000]
                    context_chunks.append(f"### {fpath}\n```\n{content}\n```")
                    state.files_read += 1
                    state.tokens_used += self._estimate_tokens(len(content))
                except ValueError:
                    continue
        except Exception:
            pass
        finally:
            if workspace:
                workspace.cleanup()

        evidence_lines: list[str] = []
        for ev in state.evidence[:30]:
            loc = ev.location
            loc_str = f"{loc.file}:{loc.start_line}" if loc else "(无位置)"
            evidence_lines.append(f"[{ev.source}] {loc_str}: {ev.snippet[:200]}")

        last_step = state.steps[-1] if state.steps else None
        no_conclusion = (
            last_step and last_step.decision in ("NO_EVIDENCE", "BUDGET")
            and len(state.confirmed) == 0
        )

        sys_prompt = (
            "你是一个代码库调查助手。根据提供的调查步骤、证据和文件内容，"
            "回答用户关于代码库的问题。回答必须引用具体的文件路径和行号。"
            "如果信息不足以得出确定性结论，请明确说明'无法确认'并解释原因。"
            "用中文回答。"
        )

        if no_conclusion:
            reason_map = {
                "NO_EVIDENCE": "未找到足够证据",
                "BUDGET": f"调查预算耗尽（{last_step.budget_reason or 'steps'}）",
            }
            reason = reason_map.get(last_step.decision, last_step.decision)
            result.answer = (
                f"无法确认：{reason}。\n\n"
                f"已执行步骤：{' → '.join(s.tool for s in state.steps if s.tool != '(blocked)')}\n"
                f"已确认假设：{len(state.confirmed)} 条\n"
                f"涉及文件：{len(state.files_visited)} 个\n"
                f"收集证据：{len(state.evidence)} 条\n"
                f"资源消耗：{len(state.steps)} 步 / {state.files_read} 文件 / ~{state.tokens_used} tokens"
            )
            result.duration_ms = (time.perf_counter() - t0) * 1000
            return result

        budget_note = ""
        if state.files_read >= state.files_max * 0.8:
            budget_note = f"\n（文件预算已用 {state.files_read}/{state.files_max}，结果可能不完整）"

        user_prompt = f"""## 问题
{state.question}

## 调查步骤
{chr(10).join(state.trace)}

## 证据（{len(evidence_lines)} 条）
{chr(10).join(evidence_lines[:20])}

## 相关文件内容
{chr(10).join(context_chunks) if context_chunks else "(文件内容不可用)"}
{budget_note}

请根据以上信息回答问题，引用文件路径和行号。"""

        try:
            answer = self.call_llm(user_prompt, system=sys_prompt, temperature=0.3, max_tokens=1500)
            result.answer = answer.strip()
        except Exception:
            parts = ["（LLM 不可用，以下为调查结果摘要）\n"]
            parts.append(f"调查目标: {state.goal}")
            parts.append(f"已确认假设: {len(state.confirmed)} 条")
            parts.append(f"涉及文件: {len(state.files_visited)} 个")
            parts.append(f"资源消耗: {len(state.steps)} 步 / {state.files_read} 文件 / ~{state.tokens_used} tokens")
            if evidence_lines:
                parts.append("\n关键证据:")
                parts.extend(f"- {line}" for line in evidence_lines[:10])
            result.answer = "\n".join(parts)

        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    # ---- ID 生成 ----------------------------------------------------------

    @staticmethod
    def _new_investigation_id(question: str) -> str:
        raw = f"{question}|{time.time()}|{uuid.uuid4().hex[:6]}"
        return "inv_" + hashlib.md5(raw.encode()).hexdigest()[:12]

    # ---- 辅助方法 ---------------------------------------------------------

    @staticmethod
    def _read_python_files(repo_path: str, state: "InvestigationState") -> list[tuple[str, str]]:
        remaining = state.files_max - state.files_read
        py_files = [f for f in state.files_visited if f.endswith(".py")][:min(10, remaining)]
        if not py_files:
            return []

        files: list[tuple[str, str]] = []
        workspace = None
        try:
            workspace = WorkspaceManager().prepare(repo_path, "HEAD")
            for fpath in py_files:
                try:
                    content = workspace.read_file(fpath)
                    files.append((fpath, content))
                except ValueError:
                    continue
        except Exception:
            pass
        finally:
            if workspace:
                workspace.cleanup()
        return files

    @staticmethod
    def _estimate_tokens(char_count: int) -> int:
        return max(1, char_count // 4)

    @staticmethod
    def _extract_keywords(question: str) -> list[str]:
        quoted = re.findall(r'["\']([^"\']+)["\']', question)
        if quoted:
            normalized = [_normalize_search_keyword(value) for value in quoted]
            result = list(dict.fromkeys(value for value in normalized if value))
            if result:
                return result[:3]

        identifiers = re.findall(r'\b([A-Z][a-zA-Z0-9_]*|[a-z]+_[a-z_]+)\b', question)
        if identifiers:
            identifiers = [_normalize_search_keyword(value) for value in identifiers]
            identifiers = [value for value in identifiers if value]
            if identifiers:
                return list(dict.fromkeys(identifiers))[:3]

        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{2,}', question)
        normalized = [_normalize_search_keyword(value) for value in words]
        return list(dict.fromkeys(value for value in normalized if value))[:3]


# ---- 调查状态机 ----------------------------------------------------------

@dataclass
class InvestigationState:
    """调查状态机 — 贯穿整个调查生命周期（M3: 三维预算 + 持久化）。"""
    question: str
    goal: str = "locate"
    keywords: list[str] = field(default_factory=list)
    hypotheses: list[str] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    steps: list[StepRecord] = field(default_factory=list)
    files_visited: set[str] = field(default_factory=set)
    trace: list[str] = field(default_factory=list)

    steps_max: int = 6
    files_max: int = 50
    token_budget: int = 16000
    files_read: int = 0
    tokens_used: int = 0

    @property
    def steps_remaining(self) -> int:
        return self.steps_max - len(self.steps)

    @property
    def is_files_exhausted(self) -> bool:
        return self.files_read >= self.files_max

    @property
    def is_token_exhausted(self) -> bool:
        return self.tokens_used >= self.token_budget

    @property
    def is_budget_exhausted(self) -> bool:
        return (len(self.steps) >= self.steps_max
                or self.is_files_exhausted
                or self.is_token_exhausted)


# ---- 参数哈希 ------------------------------------------------------------

def _hash_params(tool_name: str, state: "InvestigationState", key_fields: tuple) -> str:
    raw = tool_name
    for field in key_fields:
        if field == "query":
            raw += "|" + ",".join(sorted(state.keywords))
        elif field == "search_type":
            raw += "|grep"
        elif field == "files":
            py_files = sorted(f for f in state.files_visited if f.endswith(".py"))
            raw += "|" + ",".join(py_files[:10])
        elif field == "base_ref":
            raw += "|HEAD~5"
        elif field == "head_ref":
            raw += "|HEAD"
    return hashlib.md5(raw.encode()).hexdigest()[:12]
