"""BanditTool — 基于 bandit 的 Python 安全扫描

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

# bandit 严重度映射
_SEV = {"LOW": "low", "MEDIUM": "medium", "HIGH": "high"}


@dataclass
class BanditTool:
    name: str = "bandit"

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
            return ToolResult.failure(self.name, "BANDIT_ERROR", str(e))

    def _run(self, paths: list[str]) -> tuple[list[Finding], list[Evidence]]:
        result = subprocess.run(
            ["python", "-m", "bandit", "-f", "json", "-q"] + paths,
            capture_output=True, timeout=120,
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        if result.returncode not in (0, 1):
            raise RuntimeError(f"bandit exit {result.returncode}")

        raw = json.loads(stdout) if stdout.strip() else {}
        findings: list[Finding] = []
        evidence: list[Evidence] = []

        for item in raw.get("results", []):
            loc = CodeLocation(
                file=item.get("filename", ""),
                start_line=item.get("line_number", 0),
            )
            rule = f"B{item.get('test_id', '???')}"
            severity = _SEV.get(item.get("issue_severity", ""), "info")
            msg = item.get("issue_text", "")
            evidence.append(Evidence(
                kind="tool_finding", source="bandit",
                location=loc,
                snippet=f"{rule}: {msg}",
                reference=f"https://bandit.readthedocs.io/en/latest/plugins/{rule.lower()}.html",
                confidence=1.0,
            ))
            findings.append(Finding(
                tool="bandit", rule_id=rule, severity=severity,
                location=loc, message=msg, evidence_ids=[evidence[-1].id],
            ))
        return findings, evidence
