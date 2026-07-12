"""ReviewRun 单测 — 可追溯性校验与序列化（阶段一验收核心）

验收点：
- 能构造并序列化完整 ReviewRun，往返等价。
- 每个 Issue 至少关联一条 Evidence；validate_traceability 能报出无证据/悬空引用。
- evidence_ids 能反查回 Evidence 对象。
"""

from app.models.change import ChangeSet, FileChange
from app.models.evidence import Evidence
from app.models.finding import Finding
from app.models.issue import Issue
from app.models.location import CodeLocation
from app.models.plan import ReviewPlan
from app.models.run import ReviewRun, TraceEntry


def _run_with_one_traceable_issue() -> ReviewRun:
    run = ReviewRun(repo_url="repo", base="a", head="b")
    run.plan = ReviewPlan(analyzers=["git", "ruff"], risk_level="medium")
    run.change_set = ChangeSet(base="a", head="b", files=[FileChange(path="x.py", change_type="modified")])
    ev_id = run.add_evidence(
        Evidence(kind="tool_finding", source="ruff", location=CodeLocation(file="x.py", start_line=1))
    )
    run.add_finding(Finding(tool="ruff", rule_id="E501", message="line too long", evidence_ids=[ev_id]))
    run.add_issue(
        Issue(type="style", severity="low", file="x.py", line=1, title="line too long", evidence_ids=[ev_id])
    )
    run.record(TraceEntry(step="ruff", status="ok", duration_ms=2.0))
    return run


def test_full_run_is_traceable():
    run = _run_with_one_traceable_issue()
    assert run.validate_traceability() == []


def test_issue_without_evidence_is_flagged():
    run = ReviewRun()
    run.add_issue(Issue(type="bug", severity="high", file="x.py", line=1, title="no evidence"))
    problems = run.validate_traceability()
    assert len(problems) == 1
    assert "没有关联任何 Evidence" in problems[0]


def test_dangling_evidence_reference_is_flagged():
    run = ReviewRun()
    run.add_issue(
        Issue(type="bug", severity="high", file="x.py", line=1, title="dangling", evidence_ids=["ev_missing"])
    )
    problems = run.validate_traceability()
    assert any("不存在的 Evidence" in p for p in problems)


def test_resolve_evidence_returns_objects():
    run = ReviewRun()
    ev = Evidence(kind="code", source="git")
    ev_id = run.add_evidence(ev)
    resolved = run.resolve_evidence([ev_id, "ev_missing"])
    assert resolved == [ev]                       # 悬空 id 被忽略


def test_review_run_roundtrip():
    run = _run_with_one_traceable_issue()
    restored = ReviewRun.from_dict(run.to_dict())
    assert restored.to_dict() == run.to_dict()
    assert restored.validate_traceability() == []
