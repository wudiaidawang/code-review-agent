"""上下文对象 — 在 Pipeline / Agent 中传递的"公文包"

各分析步骤只跟 Context 读写，不互相直接调用 → 步骤间解耦、可独立测试。
Phase 1 只定义结构；复杂子类型（DiffData / ASTData 等）用宽松类型占位，
待对应 Analyzer 那天再细化。
"""

from dataclasses import dataclass, field
from typing import Any

from app.models.issue import Issue


@dataclass
class ReviewContext:
    """Review Mode 的上下文，在 Pipeline 中逐步被各 step 填充。"""

    repo_url: str = ""
    commit: str = ""
    diff: Any = None                                    # Day 2 GitDiffAnalyzer 填充
    ast_data: Any = None                                # Day 3 ASTAnalyzer 填充
    function_info: dict = field(default_factory=dict)   # Day 8 ContextAnalyzer 填充
    knowledge_docs: list[str] = field(default_factory=list)  # Day 12 RAG 填充
    issues: list[Issue] = field(default_factory=list)   # 各 Analyzer 追加
    strategy_log: list[str] = field(default_factory=list)    # Pipeline 记录每步执行/跳过
    stats: dict = field(default_factory=dict)           # Day 6 统计


@dataclass
class InvestigationContext:
    """Investigation Mode（Agent 探索）的上下文。Phase 1 仅定义结构，Agent 逻辑 Day 15 实现。"""

    question: str = ""
    repo_path: str = ""
    collected_info: list[str] = field(default_factory=list)  # 已收集的信息（逐步累积）
    files_visited: list[str] = field(default_factory=list)   # 已访问的文件
    findings: list[dict] = field(default_factory=list)       # 发现的线索
    current_hypothesis: str | None = None                    # 当前假设
    step_count: int = 0                                      # 已执行步骤数
    answer: str | None = None                                # 最终答案
