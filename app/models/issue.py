"""Issue — 统一问题模型

全系统的"通用货币"：无论来自 Ruff / Bandit / LLM / RAG，问题最终都表达为 Issue。
统一结构让聚合、去重、排序、展示只写一套逻辑。
"""

from dataclasses import dataclass, field, asdict

from app.models.ids import new_id

# 允许的问题类型与严重程度（Phase 1 用字符串 + 常量约束，不引入 Enum）
ISSUE_TYPES = ("bug", "security", "performance", "style", "architecture")
SEVERITIES = ("critical", "high", "medium", "low", "info")

# 严重程度排序权重：数值越大越严重
_SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}


@dataclass
class Issue:
    type: str                                    # bug / security / performance / style / architecture
    severity: str                                # critical / high / medium / low / info
    file: str                                    # 文件路径
    line: int                                    # 行号（0 表示非行级/整文件问题）
    title: str                                   # 简短描述
    reason: str = ""                             # 为什么这是问题
    fix: str = ""                                # 建议怎么改
    source: list[str] = field(default_factory=list)      # 来源，如 ["ruff", "llm"]
    references: list[str] = field(default_factory=list)  # RAG 检索到的相关规范
    id: str = field(default_factory=lambda: new_id("iss"))  # 稳定身份，供 trace/报告引用
    evidence_ids: list[str] = field(default_factory=list)   # 支撑本 Issue 的 Evidence id（可反查）

    def severity_rank(self) -> int:
        """返回严重程度排序权重（critical 最大），供报告按严重度降序排序。"""
        return _SEVERITY_ORDER.get(self.severity, -1)

    def to_dict(self) -> dict:
        """序列化为 dict（供 JSON 报告 / API / 存储使用）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Issue":
        return cls(**d)
