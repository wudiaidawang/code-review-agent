"""变更集 — 一次审查的输入事实（阶段二 GitTool 产出）

ChangeSet 描述"这次改了哪些文件、每个文件怎么改的"。阶段一只定义结构，
让下游（ReviewPlan 选工具、LLM 拿最小 diff）有稳定契约可依赖。
"""

from dataclasses import dataclass, field, asdict

# 文件变更类型
CHANGE_TYPES = ("added", "modified", "deleted", "renamed")


@dataclass
class Hunk:
    """一个 diff 块的行号映射，供把工具发现对齐回变更行。"""

    old_start: int = 0
    old_lines: int = 0
    new_start: int = 0
    new_lines: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Hunk":
        return cls(**d)


@dataclass
class FileChange:
    """单个文件的变更。renamed 时 old_path 记原路径。"""

    path: str
    change_type: str                                 # 见 CHANGE_TYPES
    old_path: str = ""                               # 仅 renamed 有意义
    added_lines: int = 0
    deleted_lines: int = 0
    hunks: list[Hunk] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FileChange":
        data = dict(d)
        data["hunks"] = [Hunk.from_dict(h) for h in data.get("hunks", [])]
        return cls(**data)


@dataclass
class ChangeSet:
    """一次审查涉及的全部文件变更（base → head）。空 diff 时 files 为空列表。"""

    base: str = ""                                   # base ref/commit
    head: str = ""                                   # head ref/commit
    files: list[FileChange] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChangeSet":
        data = dict(d)
        data["files"] = [FileChange.from_dict(f) for f in data.get("files", [])]
        return cls(**data)
