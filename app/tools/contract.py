"""Tool 统一契约 — 工具层与编排层之间的唯一接口

计划书铁律："工具与编排解耦"。每个工具只认 ToolRequest/ToolResult，不知道
自己被 Review Pipeline 还是 Investigation Agent 调用，因此两种编排能复用同一批工具。

关键约束："工具失败不吞异常"——execute 不应向上抛业务异常，而是返回
status="failed" 且带结构化 Diagnostic 的 ToolResult，让编排层可继续跑其余步骤。
"""

from dataclasses import dataclass, field, asdict
from typing import Any, Protocol, runtime_checkable

from app.models.evidence import Evidence
from app.models.finding import Finding

# 统一错误码：编排层据此决定降级/重试/跳过
ERROR_CODES = (
    "ok",
    "invalid_request",   # 入参不合法
    "timeout",           # 超时
    "tool_error",        # 工具内部执行失败
    "not_found",         # 目标（文件/符号）不存在
    "unsupported",       # 不支持的输入（如非 Python 文件）
)

# 工具执行状态
TOOL_STATUS = ("success", "partial", "failed")


@dataclass
class Diagnostic:
    """结构化诊断，取代裸抛异常。severity: error/warning/info。"""

    code: str                      # 见 ERROR_CODES
    message: str
    severity: str = "error"
    tool: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Diagnostic":
        return cls(**d)


@dataclass
class ToolRequest:
    """对工具的一次调用请求。params 由各工具自解释；timeout_s 供编排层设界。"""

    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 30.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ToolRequest":
        return cls(**d)


@dataclass
class ToolResult:
    """工具的统一产出。工具不直接产 Issue：只产 evidence/findings/artifacts，
    聚合阶段才统一成 Issue。失败时 status="failed" 且 diagnostics 非空。"""

    tool: str
    status: str = "success"                              # 见 TOOL_STATUS
    artifacts: dict[str, Any] = field(default_factory=dict)   # 事实产物（ChangeSet/SymbolIndex 等）
    evidence: list[Evidence] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    duration_ms: float = 0.0

    def ok(self) -> bool:
        """是否产出可用结果（failed 之外都算可用；partial 表示部分成功）。"""
        return self.status != "failed"

    @classmethod
    def failure(cls, tool: str, code: str, message: str) -> "ToolResult":
        """构造一个失败结果的便捷方法——保证失败也走结构化诊断而非异常。"""
        return cls(
            tool=tool,
            status="failed",
            diagnostics=[Diagnostic(code=code, message=message, tool=tool)],
        )

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "status": self.status,
            "artifacts": self.artifacts,
            "evidence": [e.to_dict() for e in self.evidence],
            "findings": [f.to_dict() for f in self.findings],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ToolResult":
        data = dict(d)
        data["evidence"] = [Evidence.from_dict(e) for e in data.get("evidence", [])]
        data["findings"] = [Finding.from_dict(f) for f in data.get("findings", [])]
        data["diagnostics"] = [Diagnostic.from_dict(x) for x in data.get("diagnostics", [])]
        return cls(**data)


@runtime_checkable
class Tool(Protocol):
    """所有工具实现的协议。name 唯一；execute 只做一件事，不决定调用顺序。"""

    name: str

    def execute(self, request: ToolRequest) -> ToolResult:
        """执行能力并返回结构化结果；失败以 ToolResult.failure 表达，不抛业务异常。"""
        ...
