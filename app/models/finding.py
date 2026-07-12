"""Finding — 工具/规则产生的"候选发现"

介于原始工具输出与最终 Issue 之间：Ruff/Bandit 每条诊断先变成 Finding，
带规则来源与位置，并挂上支撑它的 Evidence id。阶段三 Aggregator 再把
Finding 去重/合并为对用户展示的 Issue。
"""

from dataclasses import dataclass, field, asdict

from app.models.ids import new_id
from app.models.location import CodeLocation


@dataclass
class Finding:
    """一条候选发现。rule_id 记规则来源（如 "E501" / "B608"），保证可追溯。"""

    tool: str                                          # ruff / bandit / dependency ...
    rule_id: str                                       # 规则标识
    message: str
    severity: str = "info"                             # 取值同 issue.SEVERITIES
    id: str = field(default_factory=lambda: new_id("fnd"))
    location: CodeLocation | None = None
    evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Finding":
        data = dict(d)
        loc = data.get("location")
        data["location"] = CodeLocation.from_dict(loc) if loc else None
        return cls(**data)
