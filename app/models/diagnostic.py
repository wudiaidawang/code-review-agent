"""Diagnostic — 结构化诊断（领域模型层）

放在 models 层而非 tools 层：ReviewRun（领域模型）与 ToolResult（工具层）都要引用它，
若定义在 tools 层会造成"模型层反向依赖工具层"的分层污染。诊断本身是领域概念
（一次运行/一步工具的结构化错误信息），故归入 models。

关键约束：用它取代裸抛异常——工具/步骤失败时返回带 Diagnostic 的结果，让编排层可继续。
"""

from dataclasses import dataclass, asdict

# 统一错误码：编排层据此决定降级/重试/跳过
ERROR_CODES = (
    "ok",
    "invalid_request",   # 入参不合法
    "timeout",           # 超时
    "tool_error",        # 工具内部执行失败
    "not_found",         # 目标（文件/符号）不存在
    "unsupported",       # 不支持的输入（如非 Python 文件）
)


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
