"""LLMReviewer — 结构化 LLM 语义审查

M3 核心：只把静态工具无法判定的语义问题交给 LLM。
输入最小必要上下文（diff + 符号 + 静态发现 + 知识），输出经过 schema 校验的 Findings。
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from app.models.evidence import Evidence
from app.models.finding import Finding
from app.models.location import CodeLocation
from app.pipeline.knowledge_retriever import StaticKnowledge, NullRetriever

# LLM 输出 JSON Schema（简化版）
_REQUIRED_FIELDS = ("location", "reason", "suggestion")

_SYSTEM_PROMPT = """你是一个代码审查助手。基于提供的 diff 上下文、符号信息和静态分析结果，
识别静态规则工具遗漏的问题。文件可能是任何语言的代码或配置（Python/JS/TS/JSON/YAML 等）。

重点检查清单（逐项过一遍 diff）:
1. 硬编码凭据与后门: 密码/API key/token 写死在代码或配置文件中、万能密码、写死的管理员账号判断
2. 注入类漏洞: SQL/命令/模板注入（任何语言，包括 JS 的字符串拼接 SQL）
3. 鉴权与授权缺失: 敏感操作没有权限校验、token 生成未验证角色、API 端点无认证
4. 不安全的密码学: MD5/SHA1 存密码、可预测随机数、base64 当加密用
5. 异常与错误处理缺失: 文件/网络/解析操作裸奔、失败被静默吞掉
6. 资源泄漏: 连接/文件句柄未关闭（缺 with/try-finally）
7. 框架安全配置缺失: Flask/FastAPI/Express 等缺 SECRET_KEY、CSRF、CORS、调试模式开启
8. 不安全的固定路径: 可预测的 /tmp 文件、世界可读的敏感文件
9. 逻辑与边界: 明显的逻辑错误、边界条件遗漏、危险的默认值

判断"是否重复"只看下方列出的静态发现列表: 列表中没有的问题都应报告，
即使它看起来像规则工具"本该"发现的。同一文件多处同类问题要逐处列出。

只输出 JSON，格式：
```json
{
  "findings": [
    {
      "location": {"file": "path/to/file.py", "start_line": 10, "end_line": 10},
      "severity": "medium",
      "reason": "具体原因（引用代码中的行/变量）",
      "suggestion": "修复建议",
      "confidence": 0.8,
      "evidence_ids": ["fnd_xxx", "ev_xxx"]
    }
  ]
}
```
- severity: low/medium/high（谨慎标 high，仅确认为安全/数据风险时标）
- confidence: 0.0-1.0（低于 0.6 会被降级为 info）
- evidence_ids: 可选，引用已有 evidence/finding 的 id；无法关联时留空数组
- 只报告 diff 中真实存在的问题，不要臆测看不到的代码；如果没有问题，返回 {"findings": []}
"""


@dataclass
class LLMReviewer:
    """LLM 驱动的语义审查器。

    不直接依赖特定的 LLM SDK——通过 call_llm 注入，方便测试与切换模型。
    """

    call_llm: Callable[[str, str], str] | None = None  # (system_prompt, user_prompt) -> text
    retriever: object = field(default_factory=StaticKnowledge)
    max_retries: int = 2

    def review(
        self,
        file_path: str,
        diff_snippet: str,
        symbols: list[dict],
        static_findings: list[Finding],
        existing_evidence: list[Evidence],
    ) -> tuple[list[Finding], list[Evidence]]:
        """对单个文件执行 LLM 语义审查。

        Returns:
            (findings, evidence): 新产生的 Findings 与 Evidence。
        """
        if not self.call_llm:
            return [], []

        # 1. 构造上下文
        user_prompt = self._build_prompt(file_path, diff_snippet, symbols, static_findings)

        # 2. 检索知识
        knowledge = self.retriever.retrieve(
            query=f"{file_path} {diff_snippet[:500]}",
            top_k=3,
        )

        # 3. 调用 LLM（带重试）
        raw_output = ""
        for attempt in range(self.max_retries + 1):
            try:
                raw_output = self.call_llm(_SYSTEM_PROMPT, user_prompt)
                if raw_output:
                    break
            except Exception:
                if attempt == self.max_retries:
                    return [], [Evidence(
                        kind="tool_finding", source="llm_reviewer",
                        location=CodeLocation(file=file_path),
                        snippet="LLM call failed after retries",
                        confidence=0.0,
                    )]

        # 4. 解析 JSON
        parsed = self._extract_json(raw_output)
        if parsed is None:
            return [], [Evidence(
                kind="tool_finding", source="llm_reviewer",
                location=CodeLocation(file=file_path),
                snippet=f"LLM output parse failure: {raw_output[:200]}",
                confidence=0.0,
            )]

        # 5. 校验每条 finding
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        ev_ids_pool = {e.id for e in existing_evidence}
        # 加静态 findings 的 id
        for sf in static_findings:
            ev_ids_pool.add(sf.id)

        for item in parsed.get("findings", []):
            valid, reason = self._validate(item, ev_ids_pool)
            if not valid:
                evidence.append(Evidence(
                    kind="tool_finding", source="llm_reviewer",
                    location=CodeLocation(file=file_path),
                    snippet=f"rejected finding: {reason} — {str(item)[:200]}",
                    confidence=0.0,
                ))
                continue

            loc_data = item.get("location", {})
            loc = CodeLocation(
                file=loc_data.get("file", file_path),
                start_line=loc_data.get("start_line", 0),
                end_line=loc_data.get("end_line", 0),
            )

            # 低置信度 → 降级
            confidence = float(item.get("confidence", 0.5))
            severity = item.get("severity", "info")
            if confidence < 0.6:
                severity = "info"

            ev = Evidence(
                kind="tool_finding", source="llm_reviewer",
                location=loc,
                snippet=item.get("reason", "")[:200],
                confidence=confidence,
            )
            evidence.append(ev)
            findings.append(Finding(
                tool="llm_reviewer", rule_id="LLM_SEMANTIC",
                severity=severity,
                location=loc,
                message=item.get("reason", ""),
                evidence_ids=item.get("evidence_ids", []) + [ev.id],
            ))

        return findings, evidence

    # ---- 内部 --------------------------------------------------------

    def _build_prompt(
        self, file_path: str, diff: str, symbols: list[dict], static_findings: list[Finding],
    ) -> str:
        lines = [
            f"## 文件: `{file_path}`\n",
            f"### Diff:\n```diff\n{diff[:3000]}\n```\n",
        ]
        if symbols:
            lines.append(f"### 符号 ({len(symbols)} 个):\n")
            for s in symbols[:20]:
                lines.append(f"- `{s.get('kind', '?')}` `{s.get('name', '?')}` @ L{s.get('location', {}).get('start_line', '?')}")
            lines.append("")
        if static_findings:
            lines.append(f"### 已有静态发现 ({len(static_findings)} 条):\n")
            for f in static_findings[:10]:
                lines.append(f"- [{f.rule_id}] L{f.location.start_line if f.location else '?'}: {f.message[:100]}")
            lines.append("")
        lines.append("请输出 JSON。")
        return "\n".join(lines)

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """从 LLM 输出中提取 JSON 对象（容忍 markdown 代码块包裹）。"""
        # 尝试直接解析
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        # 尝试提取 ```json ... ``` 块
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # 尝试提取第一个 {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def _validate(item: dict, ev_ids_pool: set[str]) -> tuple[bool, str]:
        """校验 finding 的必填字段与 evidence 引用。"""
        if not isinstance(item, dict):
            return False, "not a dict"
        for field in _REQUIRED_FIELDS:
            val = item.get(field)
            if not val:
                return False, f"missing field '{field}'"
        # evidence_ids 如果给了，必须能关联到已知 evidence
        for eid in item.get("evidence_ids", []):
            if eid not in ev_ids_pool:
                return False, f"evidence_id '{eid}' not found in pool"
        return True, ""
