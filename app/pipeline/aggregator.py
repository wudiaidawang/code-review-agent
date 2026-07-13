"""Aggregator — 基于位置与规则类型去重，合并 Findings → Issues

M2 核心：同输入 → 同输出。规则透明，不依赖 LLM。
"""

from collections import defaultdict

from app.models.evidence import Evidence
from app.models.finding import Finding
from app.models.issue import Issue


class Aggregator:
    """确定性聚合器：按 (file, rule_id) 分组去重，产出 Issue 列表。"""

    def aggregate(self, findings: list[Finding], evidence: list[Evidence]) -> list[Issue]:
        """将 Findings 去重合并为 Issues。

        分组键：location.file + rule_id。同组内合并 message，保留全部 evidence_ids。
        """
        # 按 (file, rule_id) 分组
        groups: dict[tuple[str, str], list[Finding]] = defaultdict(list)
        for f in findings:
            key = (f.location.file if f.location else "", f.rule_id)
            groups[key].append(f)

        issues: list[Issue] = []
        for (filepath, rule_id), group in sorted(groups.items()):
            first = group[0]
            # 合并 message：去重后拼接
            messages = list(dict.fromkeys(f.message for f in group))
            # 收集全部 evidence_ids
            ev_ids = []
            for f in group:
                ev_ids.extend(f.evidence_ids)
            ev_ids = list(dict.fromkeys(ev_ids))

            severity = self._pick_severity(group)
            line = first.location.start_line if first.location else 0

            issues.append(Issue(
                type="static",
                severity=severity,
                file=filepath,
                line=line,
                title=f"[{rule_id}] {messages[0][:80]}",
                reason="; ".join(messages),
                source=first.tool,
                evidence_ids=ev_ids,
            ))

        return sorted(issues, key=Issue.severity_rank, reverse=True)

    @staticmethod
    def _pick_severity(findings: list[Finding]) -> str:
        """取组内最高严重度。"""
        ranks = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
        best = "info"
        for f in findings:
            if ranks.get(f.severity, 0) > ranks.get(best, 0):
                best = f.severity
        return best
