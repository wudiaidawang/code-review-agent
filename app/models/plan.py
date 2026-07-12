"""ReviewPlan — 本次审查"调哪些工具、按什么预算、为什么"

阶段一只定义结构（字段与计划书 M4 的微调输出 schema 完全对齐），
阶段三由规则式 ReviewPlanBuilder 生成，阶段五可换成微调小模型生成。
无论谁生成，Pipeline 都按同一份 ReviewPlan 执行，保证可解释、可复现。
"""

from dataclasses import dataclass, field, asdict

# 风险等级
RISK_LEVELS = ("low", "medium", "high")


@dataclass
class ReviewPlan:
    """一次审查的执行计划。字段对应计划书 M4 微调 Planner 的输出 JSON。"""

    analyzers: list[str] = field(default_factory=list)      # ["git","python_ast","ruff","bandit","dependency"]
    enable_rag: bool = False
    enable_llm_semantic_review: bool = False
    risk_level: str = "low"                                 # 见 RISK_LEVELS
    reason_codes: list[str] = field(default_factory=list)   # ["auth_change","tainted_input",...]
    budget_tokens: int = 0                                  # 0 表示不设限

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewPlan":
        return cls(**d)
