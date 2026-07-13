"""RuffTool — 基于 ruff 的 Python 代码风格/质量检查

遵循 Tool 协议，输出标准化 Finding + Evidence。
"""

import json
import subprocess
import time
from dataclasses import dataclass

from app.models.evidence import Evidence
from app.models.finding import Finding
from app.models.location import CodeLocation
from app.tools.contract import Tool, ToolRequest, ToolResult


@dataclass
class RuffTool:
    name: str = "ruff"

    def execute(self, request: ToolRequest) -> ToolResult:
        t0 = time.perf_counter()
        paths = request.params.get("paths", [])
        if not paths:
            return ToolResult(tool=self.name, status="success")

        try:
            findings, evidence = self._run(paths)
            return ToolResult(
                tool=self.name, status="success",
                findings=findings, evidence=evidence,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return ToolResult.failure(self.name, "RUFF_ERROR", str(e))

    def _run(self, paths: list[str]) -> tuple[list[Finding], list[Evidence]]:
        result = subprocess.run(
            ["python", "-m", "ruff", "check", "--output-format", "json"] + paths,
            capture_output=True, timeout=120,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        if result.returncode not in (0, 1):
            raise RuntimeError(f"ruff exit {result.returncode}: {stdout}")

        raw = json.loads(stdout) if stdout.strip() else []
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        for item in raw:
            loc = CodeLocation(
                file=item.get("filename", ""),
                start_line=item.get("location", {}).get("row", 0),
                end_line=item.get("end_location", {}).get("row", 0),
            )
            rule = item.get("code", "")
            msg = item.get("message", "")
            evidence.append(Evidence(
                kind="tool_finding", source="ruff",
                location=loc,
                snippet=f"{rule}: {msg}",
                reference=f"https://docs.astral.sh/ruff/rules/{rule.lower()}/",
                confidence=1.0,
            ))
            findings.append(Finding(
                tool="ruff", rule_id=rule,
                location=loc, message=msg, evidence_ids=[evidence[-1].id],
            ))
        return findings, evidence
