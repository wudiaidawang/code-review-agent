"""FactCollector — M1 确定性事实收集器

编排 WorkspaceManager → GitTool → ASTTool → RuffTool → BanditTool，
输入本地 git 仓库 + 提交范围，输出结构化变更/符号/静态发现。
"""

import time
from dataclasses import dataclass, field

from app.core.workspace import WorkspaceManager, WorkspaceConfig, Workspace
from app.tools.git_tool import GitTool
from app.tools.ast_tool import ASTTool
from app.tools.ruff_tool import RuffTool
from app.tools.bandit_tool import BanditTool
from app.tools.contract import ToolRequest, ToolResult
from app.models.evidence import Evidence
from app.models.finding import Finding


@dataclass
class FactCollection:
    """一次事实收集的完整产出。"""
    change_set: dict = field(default_factory=dict)
    symbol_index: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    tool_results: dict[str, ToolResult] = field(default_factory=dict)
    duration_ms: float = 0.0


class FactCollector:
    """编排确定性工具链，收集所有不依赖 LLM 的事实。"""

    def __init__(self, config: WorkspaceConfig | None = None):
        self.ws_mgr = WorkspaceManager(config)

    def collect(self, repo_path: str, base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> FactCollection:
        """对指定仓库的提交范围运行全部确定性工具。"""
        t0 = time.perf_counter()
        fc = FactCollection()

        # 1. 工作区快照
        ws = self.ws_mgr.prepare(repo_path, head_ref)
        try:
            # 2. GitTool — 变更集
            git_result = GitTool().execute(ToolRequest(tool="git", params={
                "repo_path": repo_path, "base_ref": base_ref, "head_ref": head_ref,
            }))
            fc.tool_results["git"] = git_result
            if git_result.ok():
                fc.change_set = git_result.artifacts.get("change_set", {})
                fc.evidence.extend(git_result.evidence)

            # 3. 收集变更的 Python 文件
            changed_files = [f["path"] for f in fc.change_set.get("files", [])
                             if f["path"].endswith(".py") and f["change_type"] != "deleted"]
            file_sources = []
            for path in changed_files:
                try:
                    source = ws.read_file(path)
                    file_sources.append((path, source))
                except Exception:
                    continue

            # 4. ASTTool
            if file_sources:
                ast_result = ASTTool().execute(ToolRequest(tool="python_ast", params={"files": file_sources}))
                fc.tool_results["python_ast"] = ast_result
                if ast_result.ok():
                    fc.symbol_index = ast_result.artifacts.get("symbol_index", [])
                    fc.evidence.extend(ast_result.evidence)

            # 5. RuffTool — 直接用工作区路径列表
            ruff_targets = [ws.work_dir + "/" + f for f in changed_files]
            if ruff_targets:
                ruff_result = RuffTool().execute(ToolRequest(tool="ruff", params={"paths": ruff_targets}))
                fc.tool_results["ruff"] = ruff_result
                if ruff_result.ok():
                    fc.findings.extend(ruff_result.findings)
                    fc.evidence.extend(ruff_result.evidence)

            # 6. BanditTool
            if ruff_targets:
                bandit_result = BanditTool().execute(ToolRequest(tool="bandit", params={"paths": ruff_targets}))
                fc.tool_results["bandit"] = bandit_result
                if bandit_result.ok():
                    fc.findings.extend(bandit_result.findings)
                    fc.evidence.extend(bandit_result.evidence)

        finally:
            ws.cleanup()

        fc.duration_ms = (time.perf_counter() - t0) * 1000
        return fc
