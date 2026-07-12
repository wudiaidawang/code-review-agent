"""ReviewRun — 一次审查运行的完整记录（可追溯性的落脚点）

它替代旧 ReviewContext 成为运行级容器：保存输入、计划、事实产物、证据、结论、
每步 trace 与失败诊断，并保证"每个 Issue 都能反查到 Evidence"。

设计要点：Evidence/Finding 存在按 id 索引的字典里，Issue 只持有 evidence_ids
（引用而非内嵌）——这样同一条证据可被多个结论共享，去重时也不必搬运证据本体。
validate_traceability() 是阶段一验收"引用关系有单测"的核心校验。
"""

from dataclasses import dataclass, field

from app.models.change import ChangeSet
from app.models.evidence import Evidence
from app.models.finding import Finding
from app.models.ids import new_id
from app.models.issue import Issue
from app.models.plan import ReviewPlan
from app.tools.contract import Diagnostic


@dataclass
class TraceEntry:
    """执行 trace 中的一步：谁、状态、耗时、备注。对应计划书 6.2 的"每步状态/耗时"。"""

    step: str
    status: str = "ok"                  # ok / skipped / failed
    duration_ms: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "step": self.step,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TraceEntry":
        return cls(**d)


@dataclass
class ReviewRun:
    """一次审查运行的全部状态与产物引用关系。"""

    id: str = field(default_factory=lambda: new_id("run"))
    repo_url: str = ""
    base: str = ""
    head: str = ""
    plan: ReviewPlan | None = None
    change_set: ChangeSet | None = None

    # 按 id 索引的可引用事实与结论
    evidence: dict[str, Evidence] = field(default_factory=dict)
    findings: dict[str, Finding] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)

    # 运行记录
    trace: list[TraceEntry] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    tool_versions: dict[str, str] = field(default_factory=dict)
    stats: dict = field(default_factory=dict)

    # ---- 写入 ----
    def add_evidence(self, ev: Evidence) -> str:
        """登记一条证据，返回其 id（供 Finding/Issue 引用）。"""
        self.evidence[ev.id] = ev
        return ev.id

    def add_finding(self, f: Finding) -> str:
        self.findings[f.id] = f
        return f.id

    def add_issue(self, issue: Issue) -> None:
        self.issues.append(issue)

    def record(self, entry: TraceEntry) -> None:
        self.trace.append(entry)

    # ---- 读取 / 反查 ----
    def resolve_evidence(self, ids: list[str]) -> list[Evidence]:
        """把一组 evidence_ids 解析成 Evidence 对象；忽略悬空 id（由校验单独报出）。"""
        return [self.evidence[i] for i in ids if i in self.evidence]

    def validate_traceability(self) -> list[str]:
        """校验可追溯性，返回问题描述列表（空列表=全部通过）。
        规则：① 每个 Issue 至少关联一条 Evidence；② Issue/Finding 的 evidence_ids
        不得悬空（必须能在 evidence 存储中找到）。"""
        problems: list[str] = []
        for issue in self.issues:
            if not issue.evidence_ids:
                problems.append(f"issue {issue.id} 没有关联任何 Evidence")
            for eid in issue.evidence_ids:
                if eid not in self.evidence:
                    problems.append(f"issue {issue.id} 引用了不存在的 Evidence {eid}")
        for f in self.findings.values():
            for eid in f.evidence_ids:
                if eid not in self.evidence:
                    problems.append(f"finding {f.id} 引用了不存在的 Evidence {eid}")
        return problems

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_url": self.repo_url,
            "base": self.base,
            "head": self.head,
            "plan": self.plan.to_dict() if self.plan else None,
            "change_set": self.change_set.to_dict() if self.change_set else None,
            "evidence": {k: v.to_dict() for k, v in self.evidence.items()},
            "findings": {k: v.to_dict() for k, v in self.findings.items()},
            "issues": [i.to_dict() for i in self.issues],
            "trace": [t.to_dict() for t in self.trace],
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "tool_versions": self.tool_versions,
            "stats": self.stats,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewRun":
        plan = d.get("plan")
        change_set = d.get("change_set")
        return cls(
            id=d["id"],
            repo_url=d.get("repo_url", ""),
            base=d.get("base", ""),
            head=d.get("head", ""),
            plan=ReviewPlan.from_dict(plan) if plan else None,
            change_set=ChangeSet.from_dict(change_set) if change_set else None,
            evidence={k: Evidence.from_dict(v) for k, v in d.get("evidence", {}).items()},
            findings={k: Finding.from_dict(v) for k, v in d.get("findings", {}).items()},
            issues=[Issue.from_dict(i) for i in d.get("issues", [])],
            trace=[TraceEntry.from_dict(t) for t in d.get("trace", [])],
            diagnostics=[Diagnostic.from_dict(x) for x in d.get("diagnostics", [])],
            tool_versions=d.get("tool_versions", {}),
            stats=d.get("stats", {}),
        )
