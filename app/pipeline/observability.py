"""M5.1 可观测性 — Pipeline 性能分解与结构化 Timeline。

每个 ReviewRun 自动生成逐阶段耗时分解和结构化指标，
方便面试展示、性能回归定位和运维排障。
"""

from dataclasses import dataclass, field


@dataclass
class StageMetric:
    """单个阶段的度量。"""
    stage: str
    duration_ms: float
    status: str                # success / failed / skipped
    finding_count: int = 0
    evidence_count: int = 0


@dataclass
class PipelineTimeline:
    """一次 Pipeline 运行的完整性能分解。"""
    run_id: str = ""
    total_duration_ms: float = 0.0
    stages: list[StageMetric] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for s in self.stages if s.status == "success")

    @property
    def failure_count(self) -> int:
        return sum(1 for s in self.stages if s.status == "failed")

    @property
    def bottleneck(self) -> StageMetric | None:
        """耗时最长的阶段。"""
        if not self.stages:
            return None
        return max(self.stages, key=lambda s: s.duration_ms)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "total_duration_ms": round(self.total_duration_ms, 2),
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "bottleneck": self.bottleneck.stage if self.bottleneck else None,
            "stages": [
                {
                    "stage": s.stage,
                    "duration_ms": round(s.duration_ms, 2),
                    "status": s.status,
                    "finding_count": s.finding_count,
                    "evidence_count": s.evidence_count,
                }
                for s in self.stages
            ],
        }

    def ascii_bar(self, width: int = 40) -> str:
        """生成 ASCII 柱状图，面试展示用。"""
        if not self.stages or self.total_duration_ms <= 0:
            return "(no data)"

        lines = [f"Pipeline Timeline  ({self.total_duration_ms:.0f}ms total)", "-" * 50]
        max_name = max(len(s.stage) for s in self.stages)
        for s in self.stages:
            bar_len = int(s.duration_ms / self.total_duration_ms * width)
            bar = "█" * bar_len + "░" * (width - bar_len)
            flag = "✓" if s.status == "success" else ("✗" if s.status == "failed" else "—")
            lines.append(
                f"  {s.stage:<{max_name}}  {bar}  {s.duration_ms:>6.0f}ms  {flag}  "
                f"({s.finding_count}f/{s.evidence_count}e)"
            )
        lines.append("-" * 50)
        return "\n".join(lines)


def build_timeline(run_id: str, plan: dict, trace: list, tool_results: dict,
                   total_duration_ms: float) -> PipelineTimeline:
    """从 ReviewRun 的原始数据构建 PipelineTimeline。

    Args:
        run_id: 运行 ID
        plan: ReviewPlan dict（含 analyzers 列表）
        trace: TraceEntry 列表（含 step/status/duration_ms）
        tool_results: tool_name → ToolResult 映射
        total_duration_ms: 总耗时
    """
    stages: list[StageMetric] = []
    analyzers = plan.get("analyzers", [])

    # 按 trace 顺序构建
    trace_by_step = {t.step: t for t in trace}

    for tool_name in analyzers:
        t = trace_by_step.get(tool_name)
        if t:
            tr = tool_results.get(tool_name)
            stages.append(StageMetric(
                stage=tool_name,
                duration_ms=t.duration_ms or 0,
                status=t.status,
                finding_count=len(tr.findings) if tr else 0,
                evidence_count=len(tr.evidence) if tr else 0,
            ))

    # 聚合阶段（不在 analyzers 里）
    for step_name in ("aggregator", "report"):
        t = trace_by_step.get(step_name)
        if t:
            stages.append(StageMetric(
                stage=step_name,
                duration_ms=t.duration_ms or 0,
                status=t.status,
            ))

    # LLM 阶段
    for step_name, t in trace_by_step.items():
        if step_name.startswith("llm_review"):
            stages.append(StageMetric(
                stage=step_name,
                duration_ms=t.duration_ms or 0,
                status=t.status,
            ))

    return PipelineTimeline(
        run_id=run_id,
        total_duration_ms=total_duration_ms,
        stages=stages,
    )
