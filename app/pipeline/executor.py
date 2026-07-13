"""ReviewExecutor — 按 ReviewPlan 执行工具链，记录 trace，失败降级

M2 核心：执行透明，同计划 → 同结果。任何工具失败不中断其余工具。
"""

import time
from dataclasses import dataclass, field

from app.core.workspace import WorkspaceManager, WorkspaceConfig, Workspace
from app.tools.git_tool import GitTool
from app.tools.ast_tool import ASTTool
from app.tools.ruff_tool import RuffTool
from app.tools.bandit_tool import BanditTool
from app.tools.dependency_tool import DependencyTool
from app.tools.contract import ToolRequest, ToolResult
from app.models.evidence import Evidence
from app.models.finding import Finding
from app.models.run import TraceEntry


# 工具名 → 工具实例映射
_TOOL_REGISTRY = {
    "git": GitTool(),
    "python_ast": ASTTool(),
    "ruff": RuffTool(),
    "bandit": BanditTool(),
    "dependency": DependencyTool(),
}


@dataclass
class ExecutionResult:
    """一次计划执行的全部产出。"""
    plan: dict = field(default_factory=dict)
    change_set: dict = field(default_factory=dict)
    symbol_index: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    tool_results: dict[str, ToolResult] = field(default_factory=dict)
    trace: list[TraceEntry] = field(default_factory=list)
    duration_ms: float = 0.0


class ReviewExecutor:
    """按 ReviewPlan 执行工具链，记录每一步的 trace。"""

    def __init__(self, config: WorkspaceConfig | None = None):
        self.ws_mgr = WorkspaceManager(config)

    def execute(self, repo_path: str, base_ref: str, head_ref: str, plan: dict) -> ExecutionResult:
        t0 = time.perf_counter()
        result = ExecutionResult(plan=plan)

        analyzers = plan.get("analyzers", [])
        ws = self.ws_mgr.prepare(repo_path, head_ref)

        try:
            # 1. git — 始终第一个，提供 ChangeSet
            if "git" in analyzers:
                with self._run_step("git", result) as t:
                    tr = GitTool().execute(ToolRequest(tool="git", params={
                        "repo_path": repo_path, "base_ref": base_ref, "head_ref": head_ref,
                    }))
                    result.tool_results["git"] = tr
                    t.status = tr.status
                    if tr.ok():
                        result.change_set = tr.artifacts.get("change_set", {})
                        result.evidence.extend(tr.evidence)

            # 2. 收集变更的 Python 文件
            changed_py = [f["path"] for f in result.change_set.get("files", [])
                          if f["path"].endswith(".py") and f.get("change_type") != "deleted"]
            file_sources = []
            for path in changed_py:
                try:
                    source = ws.read_file(path)
                    file_sources.append((path, source))
                except Exception:
                    continue

            # 3. python_ast
            if "python_ast" in analyzers and file_sources:
                with self._run_step("python_ast", result) as t:
                    tr = ASTTool().execute(ToolRequest(tool="python_ast", params={"files": file_sources}))
                    result.tool_results["python_ast"] = tr
                    t.status = tr.status
                    if tr.ok():
                        result.symbol_index = tr.artifacts.get("symbol_index", [])
                        result.evidence.extend(tr.evidence)

            # 4. ruff + bandit
            ws_targets = [ws.work_dir + "/" + f for f in changed_py]
            for tool_name in ("ruff", "bandit"):
                if tool_name in analyzers and ws_targets:
                    tool = _TOOL_REGISTRY[tool_name]
                    with self._run_step(tool_name, result) as t:
                        tr = tool.execute(ToolRequest(tool=tool_name, params={"paths": ws_targets}))
                        result.tool_results[tool_name] = tr
                        t.status = tr.status
                        if tr.ok():
                            result.findings.extend(tr.findings)
                            result.evidence.extend(tr.evidence)

            # 5. dependency — 分析依赖变更
            if "dependency" in analyzers:
                with self._run_step("dependency", result) as t:
                    tr = DependencyTool().execute(ToolRequest(tool="dependency", params={
                        "files": file_sources,
                        "changed_files": [f["path"] for f in result.change_set.get("files", [])],
                    }))
                    result.tool_results["dependency"] = tr
                    t.status = tr.status
                    if tr.ok():
                        result.findings.extend(tr.findings)
                        result.evidence.extend(tr.evidence)

        finally:
            ws.cleanup()

        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    def _run_step(self, step_name: str, result: ExecutionResult):
        """返回一个上下文管理器，自动把 trace entry 追加到 result.trace。"""
        start = time.perf_counter()
        entry = TraceEntry(step=step_name, status="running")

        class _Ctx:
            status: str = "running"
            entry_ref = entry

            def __enter__(self_):
                return self_

            def __exit__(self_, *args):
                self_.entry_ref.duration_ms = (time.perf_counter() - start) * 1000
                self_.entry_ref.status = self_.status
                result.trace.append(self_.entry_ref)

        return _Ctx()
