"""Evidence — 可引用的事实片段（系统"可追溯性"的原子）

任何结论（Finding / Issue）都必须挂在一条或多条 Evidence 上，Evidence 自身
只陈述事实（哪段代码、哪条规则、哪条知识），不做推理。确定性工具产出的
Evidence confidence=1.0；LLM/RAG 产出的可低于 1.0。
"""

import hashlib
from dataclasses import dataclass, field, asdict

from app.models.ids import new_id
from app.models.location import CodeLocation

# 证据种类：来自代码本身 / 工具规则发现 / 外部知识 / 依赖关系 / 变更
EVIDENCE_KINDS = ("code", "tool_finding", "knowledge", "dependency", "change")


@dataclass
class Evidence:
    """一条可被 id 引用的事实。source 记产生者（git/ruff/bandit/rag/llm...）。"""

    kind: str                                          # 见 EVIDENCE_KINDS
    source: str                                        # 产生者标识
    id: str = field(default_factory=lambda: new_id("ev"))
    location: CodeLocation | None = None               # 事实对应的代码位置（可选）
    snippet: str = ""                                  # 事实片段：代码/规则文本/知识摘录
    confidence: float = 1.0                            # 0..1，确定性事实=1.0
    reference: str = ""                                # 外部来源：规则 URL / 规范 ID / 知识条目

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Evidence":
        data = dict(d)
        loc = data.get("location")
        data["location"] = CodeLocation.from_dict(loc) if loc else None
        return cls(**data)

    @staticmethod
    def compute_deterministic_id(repo_commit: str, file: str,
                                 start_line: int, end_line: int,
                                 snippet: str) -> str:
        """基于内容 hash 生成确定性 Evidence ID，保证同内容同 ID。

        用于评测可复现性——同一仓库同一位置的同一代码片段，
        每次运行都得到相同的 ev_xxxxxxxx。
        """
        raw = f"{repo_commit}|{file}|{start_line}|{end_line}|{snippet}"
        return "ev_" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]

    def set_deterministic_id(self, repo_commit: str, file: str,
                             start_line: int, end_line: int,
                             snippet: str) -> None:
        """用内容 hash 覆盖当前 Evidence 的 id 字段。"""
        self.id = self.compute_deterministic_id(
            repo_commit, file, start_line, end_line, snippet)
