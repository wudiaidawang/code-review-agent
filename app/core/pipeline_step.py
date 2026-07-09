"""PipelineStep — 所有分析步骤的抽象基类

约定：每个 step 从 context 读取数据、处理、把结果写回 context（无返回值），
使数据流向单一（都进 Context），聚合零成本。

should_run 是前瞻设计：Phase 1 默认返回 True（等于顺序执行）；
Phase 3 加智能策略时，子类只需覆盖 should_run，Pipeline 本身无需改动。
"""

from abc import ABC, abstractmethod

from app.models.context import ReviewContext


class PipelineStep(ABC):
    name: str = "step"  # 用于 strategy_log 日志

    def should_run(self, context: ReviewContext) -> bool:
        """是否执行本步骤。默认执行；子类可覆盖以实现条件执行（如按文件类型/diff 规模跳过）。"""
        return True

    @abstractmethod
    def analyze(self, context: ReviewContext) -> None:
        """读取 context 中的数据，处理后将结果写回 context。不返回值。"""
        ...
