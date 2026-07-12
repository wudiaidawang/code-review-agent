"""阶段一数据契约单测 — 序列化往返（纯确定性，不碰网络）

验收点：模型 to_dict → from_dict 后应还原等价对象（含嵌套结构）。
"""

from app.models.change import ChangeSet, FileChange, Hunk
from app.models.evidence import Evidence
from app.models.finding import Finding
from app.models.issue import Issue
from app.models.location import CodeLocation, Symbol
from app.models.plan import ReviewPlan


def test_code_location_roundtrip():
    loc = CodeLocation(file="a.py", start_line=3, end_line=5, symbol="f")
    assert CodeLocation.from_dict(loc.to_dict()) == loc


def test_symbol_roundtrip_with_nested_location():
    sym = Symbol(
        name="login",
        kind="method",
        location=CodeLocation(file="auth.py", start_line=10),
        parent="UserService",
        calls=["hash_pw"],
    )
    assert Symbol.from_dict(sym.to_dict()) == sym


def test_changeset_roundtrip_with_nested_files_and_hunks():
    cs = ChangeSet(
        base="aaa",
        head="bbb",
        files=[
            FileChange(
                path="m.py",
                change_type="modified",
                added_lines=4,
                deleted_lines=1,
                hunks=[Hunk(1, 2, 1, 5)],
            ),
            FileChange(path="new.py", change_type="added"),
        ],
    )
    assert ChangeSet.from_dict(cs.to_dict()) == cs


def test_empty_changeset_roundtrip():
    cs = ChangeSet(base="x", head="y")            # 空 diff
    restored = ChangeSet.from_dict(cs.to_dict())
    assert restored == cs
    assert restored.files == []


def test_evidence_roundtrip_and_default_id():
    ev = Evidence(kind="tool_finding", source="ruff", snippet="line too long")
    assert ev.id.startswith("ev_")
    assert Evidence.from_dict(ev.to_dict()) == ev


def test_evidence_with_location_roundtrip():
    ev = Evidence(
        kind="code",
        source="git",
        location=CodeLocation(file="x.py", start_line=1),
        confidence=1.0,
    )
    assert Evidence.from_dict(ev.to_dict()) == ev


def test_finding_roundtrip():
    f = Finding(
        tool="bandit",
        rule_id="B608",
        message="possible SQL injection",
        severity="high",
        location=CodeLocation(file="db.py", start_line=42),
        evidence_ids=["ev_1", "ev_2"],
    )
    assert f.id.startswith("fnd_")
    assert Finding.from_dict(f.to_dict()) == f


def test_review_plan_roundtrip_matches_schema():
    plan = ReviewPlan(
        analyzers=["git", "python_ast", "ruff", "bandit"],
        enable_rag=False,
        enable_llm_semantic_review=True,
        risk_level="high",
        reason_codes=["auth_change"],
    )
    assert ReviewPlan.from_dict(plan.to_dict()) == plan


def test_issue_backward_compatible_construction():
    # 旧式位置参数构造仍可用（不传 id/evidence_ids）
    iss = Issue(type="bug", severity="high", file="x.py", line=1, title="demo")
    assert iss.id.startswith("iss_")
    assert iss.evidence_ids == []


def test_issue_roundtrip_with_evidence_ids():
    iss = Issue(
        type="security",
        severity="critical",
        file="x.py",
        line=10,
        title="hardcoded secret",
        evidence_ids=["ev_a"],
    )
    assert Issue.from_dict(iss.to_dict()) == iss
