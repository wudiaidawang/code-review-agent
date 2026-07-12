"""代码位置与符号 — 一切 Evidence/Finding 定位的基础

CodeLocation 回答"问题在哪一行"，Symbol 回答"这是哪个函数/类"。
所有工具产出的事实都通过 CodeLocation 绑定到具体代码，杜绝"无位置的结论"。
"""

from dataclasses import dataclass, field, asdict

# 合法的符号种类（阶段二 AST 工具会产出这些）
SYMBOL_KINDS = ("function", "method", "class", "import", "variable", "module")


@dataclass
class CodeLocation:
    """代码中的一个位置区间。start_line/end_line 为 0 表示非行级（整文件）问题。"""

    file: str
    start_line: int = 0
    end_line: int = 0          # 0 表示与 start_line 相同或不适用单行
    symbol: str = ""           # 可选：所属符号限定名，如 "UserService.login"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CodeLocation":
        return cls(**d)


@dataclass
class Symbol:
    """源码符号（函数/类/导入等）。阶段二 PythonParserTool 提取后进 SymbolIndex。"""

    name: str
    kind: str                                      # 见 SYMBOL_KINDS
    location: CodeLocation
    parent: str = ""                               # 所属类/模块限定名
    calls: list[str] = field(default_factory=list) # 调用的其他符号名（调用边）

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Symbol":
        data = dict(d)
        data["location"] = CodeLocation.from_dict(data["location"])
        return cls(**data)
