"""Pipeline — 顺序执行一串 PipelineStep 的编排器

Phase 1 保持最简：按序执行、should_run 为 False 时跳过并记录日志。
不做并行/异常兜底/超时（异常处理排期在 Day 20）。
"""

from app.core.pipeline_step import PipelineStep
from app.models.context import ReviewContext


class Pipeline:
    def __init__(self, steps: list[PipelineStep]):
        self.steps = steps

    def run(self, context: ReviewContext) -> ReviewContext:
        """依次执行各 step；被跳过的 step 也记入 strategy_log。返回同一个（已被填充的）context。"""
        for step in self.steps:
            if step.should_run(context):
                step.analyze(context)
                context.strategy_log.append(f"[OK] {step.name}")
            else:
                context.strategy_log.append(f"[SKIP] {step.name}")
        return context
