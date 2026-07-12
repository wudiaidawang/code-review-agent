"""Tool 统一契约单测 — 失败走结构化诊断，不吞异常也不抛异常

验收点：工具失败时返回 status="failed" 且带 Diagnostic 的 ToolResult，
编排层据此可继续；ToolResult 可序列化往返。
"""

from app.models.diagnostic import ERROR_CODES, Diagnostic
from app.models.evidence import Evidence
from app.models.finding import Finding
from app.tools.contract import (
    Tool,
    ToolRequest,
    ToolResult,
)


class _FailingTool:
    """模拟一个执行必失败的工具：不抛异常，返回结构化失败结果。"""

    name = "failing"

    def execute(self, request: ToolRequest) -> ToolResult:
        return ToolResult.failure(self.name, "tool_error", "boom")


class _OkTool:
    name = "ok"

    def execute(self, request: ToolRequest) -> ToolResult:
        return ToolResult(
            tool=self.name,
            evidence=[Evidence(kind="code", source="ok")],
            findings=[Finding(tool="ok", rule_id="R1", message="m")],
            duration_ms=1.5,
        )


def test_tools_satisfy_protocol():
    assert isinstance(_FailingTool(), Tool)
    assert isinstance(_OkTool(), Tool)


def test_failure_returns_structured_diagnostic_without_raising():
    result = _FailingTool().execute(ToolRequest(tool="failing"))
    assert result.status == "failed"
    assert result.ok() is False
    assert len(result.diagnostics) == 1
    diag = result.diagnostics[0]
    assert diag.code in ERROR_CODES
    assert diag.tool == "failing"


def test_success_result_is_ok():
    result = _OkTool().execute(ToolRequest(tool="ok"))
    assert result.ok() is True
    assert result.status == "success"
    assert len(result.evidence) == 1
    assert len(result.findings) == 1


def test_tool_result_roundtrip():
    result = _OkTool().execute(ToolRequest(tool="ok"))
    result.diagnostics.append(Diagnostic(code="ok", message="fine", severity="info"))
    assert ToolResult.from_dict(result.to_dict()) == result


def test_tool_request_roundtrip():
    req = ToolRequest(tool="git", params={"base": "HEAD~1", "head": "HEAD"}, timeout_s=10.0)
    assert ToolRequest.from_dict(req.to_dict()) == req
