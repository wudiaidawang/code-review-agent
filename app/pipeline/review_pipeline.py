"""ReviewPipeline — M2 完整审查流程：Plan → Execute → Aggregate → Report

一次调用完成端到端的确定性审查（不依赖 LLM）。
"""

import time
from dataclasses import dataclass, field

from app.pipeline.plan_builder import RuleBasedPlanBuilder
from app.pipeline.executor import ReviewExecutor, ExecutionResult
from app.pipeline.aggregator import Aggregator
from app.pipeline.report import ReportGenerator
from app.core.workspace import WorkspaceConfig
from app.models.issue import Issue
from app.models.evidence import Evidence
from app.models.run import TraceEntry


@dataclass
class ReviewOutput:
    """一次完整审查的产出。"""
    plan: dict = field(default_factory=dict)
    change_set: dict = field(default_factory=dict)
    symbol_index: list[dict] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)
    markdown: str = ""
    json: str = ""
    duration_ms: float = 0.0


class ReviewPipeline:
    """M2/M3 审查管道（确定性 + 可选 LLM 语义审查）。

    Usage:
        pipeline = ReviewPipeline()                         # 仅确定性
        pipeline = ReviewPipeline(llm_reviewer=my_reviewer) # 含 LLM 语义审查
        output = pipeline.run(".", "HEAD~3", "HEAD")
        print(output.markdown)
    """

    def __init__(self, config: WorkspaceConfig | None = None, llm_reviewer=None):
        self.plan_builder = RuleBasedPlanBuilder()
        self.executor = ReviewExecutor(config)
        self.aggregator = Aggregator()
        self.reporter = ReportGenerator()
        self.llm_reviewer = llm_reviewer  # M3: 可选 LLMReviewer

    def run(self, repo_path: str, base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> ReviewOutput:
        """执行完整审查流程（M2 确定性 + M3 可选 LLM）。"""
        t0 = time.perf_counter()
        output = ReviewOutput()

        from app.tools.git_tool import GitTool
        from app.tools.contract import ToolRequest

        # Step 1: 快速获取 ChangeSet
        git_result = GitTool().execute(ToolRequest(tool="git", params={
            "repo_path": repo_path, "base_ref": base_ref, "head_ref": head_ref,
        }))
        change_set = git_result.artifacts.get("change_set", {}) if git_result.ok() else {}

        # Step 2: PlanBuilder 基于 ChangeSet 生成计划
        plan = self.plan_builder.build(change_set)
        plan_dict = plan.to_dict()
        # M3: 有 LLM reviewer 且计划允许时，开启 LLM 审查
        if self.llm_reviewer:
            plan_dict["enable_llm_semantic_review"] = True

        # Step 3: Executor 执行静态工具
        exec_result = self.executor.execute(repo_path, base_ref, head_ref, plan_dict)
        output.change_set = exec_result.change_set
        output.symbol_index = exec_result.symbol_index
        output.trace = exec_result.trace
        output.plan = plan_dict

        all_findings = list(exec_result.findings)
        all_evidence = list(exec_result.evidence)

        # Step 4 (M3): LLM 语义审查（可选）
        if self.llm_reviewer and plan_dict.get("enable_llm_semantic_review"):
            py_files = [f for f in change_set.get("files", [])
                        if f["path"].endswith(".py") and f.get("change_type") != "deleted"]
            for fc in py_files[:10]:  # 最多审查 10 个文件
                # 收集该文件的静态 findings
                file_findings = [f for f in all_findings
                                  if f.location and f.location.file == fc["path"]]
                # 收集该文件的符号
                file_symbols = [s for s in output.symbol_index
                                if s.get("location", {}).get("file") == fc["path"]]
                # 获取 diff snippet
                diff_snippet = "\n".join(
                    f"@@ -{h['old_start']},{h['old_lines']} +{h['new_start']},{h['new_lines']} @@"
                    for h in fc.get("hunks", [])
                )
                try:
                    llm_findings, llm_evidence = self.llm_reviewer.review(
                        file_path=fc["path"],
                        diff_snippet=diff_snippet,
                        symbols=file_symbols,
                        static_findings=file_findings,
                        existing_evidence=all_evidence,
                    )
                    all_findings.extend(llm_findings)
                    all_evidence.extend(llm_evidence)
                    output.trace.append(TraceEntry(
                        step=f"llm_review({fc['path']})",
                        status="success" if llm_findings else "no_issues",
                    ))
                except Exception:
                    output.trace.append(TraceEntry(
                        step=f"llm_review({fc['path']})",
                        status="failed",
                    ))

        # Step 5: Aggregator 去重合并
        issues = self.aggregator.aggregate(all_findings, all_evidence)
        output.issues = issues
        output.evidence = all_evidence

        # Step 6: 生成报告
        output.markdown = self.reporter.markdown(
            output.change_set, plan_dict, output.trace, issues, all_evidence, exec_result.duration_ms,
        )
        output.json = self.reporter.json_report(
            output.change_set, plan_dict, output.trace, issues, all_evidence, exec_result.duration_ms,
        )
        output.duration_ms = (time.perf_counter() - t0) * 1000

        return output
