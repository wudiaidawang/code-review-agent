"""M5.1 性能基准 — 各阶段耗时上限 + Timeline 生成。

pytest -m perf        仅跑快速性能断言 (<1s)
pytest -m slow        跑完整 Pipeline 性能基准
"""

import pytest
from app.pipeline.review_pipeline import ReviewPipeline
from app.pipeline.observability import build_timeline, PipelineTimeline, StageMetric


class TestTimeline:
    """可观测性 Timeline 构建。"""

    def test_timeline_from_trace(self):
        """从 trace 和 tool_results 构建 PipelineTimeline。"""
        trace = [
            type("T", (), {"step": "git", "status": "success", "duration_ms": 120.0})(),
            type("T", (), {"step": "ruff", "status": "success", "duration_ms": 300.0})(),
            type("T", (), {"step": "bandit", "status": "failed", "duration_ms": 50.0})(),
        ]
        tool_results = {
            "git": type("R", (), {"findings": [], "evidence": [1, 2]})(),
            "ruff": type("R", (), {"findings": [1], "evidence": [1]})(),
            "bandit": type("R", (), {"findings": [], "evidence": []})(),
        }
        plan = {"analyzers": ["git", "ruff", "bandit"]}

        timeline = build_timeline("test-1", plan, trace, tool_results, 500.0)

        assert timeline.total_duration_ms == 500.0
        assert timeline.success_count == 2
        assert timeline.failure_count == 1
        assert timeline.bottleneck.stage == "ruff"
        assert timeline.bottleneck.duration_ms == 300.0

    def test_timeline_to_dict(self):
        """Timeline 可序列化。"""
        stages = [
            StageMetric("git", 100.0, "success", 0, 5),
            StageMetric("ruff", 200.0, "success", 3, 3),
        ]
        tl = PipelineTimeline("r1", 300.0, stages)
        d = tl.to_dict()
        assert d["total_duration_ms"] == 300.0
        assert d["success_count"] == 2
        assert len(d["stages"]) == 2

    def test_timeline_ascii_bar(self):
        """ASCII 柱状图可生成。"""
        stages = [
            StageMetric("git", 100.0, "success", 0, 2),
            StageMetric("ruff", 400.0, "success", 5, 5),
        ]
        tl = PipelineTimeline("r1", 500.0, stages)
        bar = tl.ascii_bar()
        assert "Pipeline Timeline" in bar
        assert "git" in bar
        assert "ruff" in bar
        assert "500ms" in bar

    def test_empty_timeline(self):
        """空 Timeline 不会崩溃。"""
        tl = PipelineTimeline("empty")
        assert tl.success_count == 0
        assert tl.bottleneck is None
        assert tl.to_dict()["stages"] == []


class TestPipelinePerf:
    """Pipeline 性能基准 — 各阶段耗时不能超过上限。"""

    @pytest.mark.slow
    def test_git_stage_under_limit(self):
        """Git 阶段应在合理时间内完成。"""
        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        assert output.timeline is not None
        git_stage = next((s for s in output.timeline.stages if s.stage == "git"), None)
        assert git_stage is not None
        # git diff 应在 5s 内完成（宽松上限，覆盖大 diff）
        assert git_stage.duration_ms < 5000, f"Git 耗时 {git_stage.duration_ms:.0f}ms > 5000ms"

    @pytest.mark.slow
    def test_total_pipeline_under_limit(self):
        """完整 Pipeline 总耗时应在合理范围内。"""
        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        # 小范围 diff 应在 30s 内完成
        assert output.duration_ms < 30000, f"Pipeline 总耗时 {output.duration_ms:.0f}ms > 30000ms"

    @pytest.mark.perf
    def test_timeline_produced(self, fixed_git_diff):
        """每次运行均应生成 Timeline。"""
        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~1", "HEAD")

        assert output.timeline is not None
        assert len(output.timeline.stages) > 0

    @pytest.mark.slow
    def test_timeline_stages_match_plan(self):
        """Timeline 的 stages 应覆盖 plan 中的所有 analyzer。"""
        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        planned = set(output.plan.get("analyzers", []))
        staged = {s.stage for s in output.timeline.stages}
        # 每个 analyzers 中的工具都应在 timeline 中有记录
        for tool in planned:
            assert tool in staged, f"Plan 中的 {tool} 未出现在 timeline stages 中"

    @pytest.mark.perf
    def test_timeline_ascii_output(self, fixed_git_diff):
        """快速验证 timeline ASCII 柱状图可生成。"""
        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~1", "HEAD")

        bar = output.timeline.ascii_bar()
        assert len(bar) > 0
        assert "Pipeline Timeline" in bar
