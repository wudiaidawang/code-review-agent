"""M2 固定审查闭环 — 单元测试 + 集成测试。"""
from app.pipeline.plan_builder import RuleBasedPlanBuilder
from app.pipeline.aggregator import Aggregator
from app.pipeline.report import ReportGenerator
from app.pipeline.review_pipeline import ReviewPipeline
from app.models.finding import Finding
from app.models.evidence import Evidence
from app.models.location import CodeLocation
from app.models.run import TraceEntry


class TestPlanBuilder:
    def test_no_python_files(self):
        pb = RuleBasedPlanBuilder()
        cs = {"files": [{"path": "README.md", "change_type": "modified", "added_lines": 5, "deleted_lines": 0}]}
        plan = pb.build(cs)
        assert plan.analyzers == ["git"]
        assert "no_python_changes" in plan.reason_codes

    def test_python_files_triggers_python_tools(self):
        pb = RuleBasedPlanBuilder()
        cs = {"files": [{"path": "src/main.py", "change_type": "modified", "added_lines": 10, "deleted_lines": 5}]}
        plan = pb.build(cs)
        assert "git" in plan.analyzers
        assert "ruff" in plan.analyzers

    def test_high_risk_signals(self):
        pb = RuleBasedPlanBuilder()
        cs = {"files": [
            {"path": "auth.py", "change_type": "modified", "added_lines": 30, "deleted_lines": 20},
        ]}
        contents = {"auth.py": "def login(): password = 'secret'; eval(user_input)"}
        plan = pb.build(cs, file_contents=contents)
        assert plan.risk_level in ("medium", "high")
        assert "bandit" in plan.analyzers

    def test_deterministic_same_input(self):
        """M2 验收：相同输入 → 相同计划。"""
        pb = RuleBasedPlanBuilder()
        cs = {"files": [{"path": "x.py", "change_type": "modified", "added_lines": 10, "deleted_lines": 0}]}
        p1 = pb.build(cs)
        p2 = pb.build(cs)
        assert p1.analyzers == p2.analyzers
        assert p1.risk_level == p2.risk_level
        assert p1.reason_codes == p2.reason_codes


class TestAggregator:
    def test_dedup_by_file_and_rule(self):
        agg = Aggregator()
        loc = CodeLocation(file="a.py", start_line=1)
        ev = Evidence(kind="tool_finding", source="ruff", location=loc, snippet="test")
        f1 = Finding(tool="ruff", rule_id="F401", message="unused", location=loc, evidence_ids=[ev.id])
        f2 = Finding(tool="ruff", rule_id="F401", message="unused", location=loc, evidence_ids=[ev.id])
        issues = agg.aggregate([f1, f2], [ev])
        assert len(issues) == 1

    def test_different_rules_not_merged(self):
        agg = Aggregator()
        loc = CodeLocation(file="a.py", start_line=1)
        ev = Evidence(kind="tool_finding", source="ruff", location=loc, snippet="test")
        f1 = Finding(tool="ruff", rule_id="F401", message="a", location=loc, evidence_ids=[ev.id])
        f2 = Finding(tool="ruff", rule_id="E501", message="b", location=loc, evidence_ids=[ev.id])
        issues = agg.aggregate([f1, f2], [ev])
        assert len(issues) == 2

    def test_empty(self):
        agg = Aggregator()
        assert agg.aggregate([], []) == []


class TestReportGenerator:
    def test_markdown_contains_sections(self):
        rg = ReportGenerator()
        md = rg.markdown(
            change_set={"base": "HEAD~1", "head": "HEAD", "files": []},
            plan={"analyzers": ["git"], "risk_level": "low", "reason_codes": []},
            trace=[TraceEntry(step="git", status="success", duration_ms=100)],
            issues=[],
            evidence=[],
            duration_ms=500,
        )
        assert "Code Review Report" in md
        assert "Change Summary" in md
        assert "Execution Trace" in md
        assert "Issues" in md
        assert "Plan Details" in md

    def test_json_is_valid(self):
        import json
        rg = ReportGenerator()
        j = rg.json_report(
            change_set={"base": "HEAD~1", "head": "HEAD", "files": []},
            plan={"analyzers": ["git"], "risk_level": "low", "reason_codes": []},
            trace=[],
            issues=[],
            evidence=[],
            duration_ms=100,
        )
        data = json.loads(j)
        assert "change_set" in data
        assert "plan" in data
        assert "issues" in data


class TestReviewPipeline:
    """端到端集成测试 — 用本仓库验证。"""

    def test_full_pipeline(self):
        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")
        assert len(output.change_set.get("files", [])) > 0
        assert len(output.trace) >= 1
        assert len(output.markdown) > 0
        assert len(output.json) > 0

    def test_idempotent(self):
        """M2 验收：同一输入 → 同一计划 + 同一 trace 步骤。"""
        pipeline = ReviewPipeline()
        o1 = pipeline.run(".", "HEAD", "HEAD")
        o2 = pipeline.run(".", "HEAD", "HEAD")
        assert o1.plan["analyzers"] == o2.plan["analyzers"]
        assert [t.step for t in o1.trace] == [t.step for t in o2.trace]

    def test_empty_diff_returns_no_issues(self):
        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD", "HEAD")
        files = output.change_set.get("files", [])
        # 空 diff 可能没有文件变更
        if not files:
            assert output.issues == []
