"""Pipeline 骨架单测 — 纯确定性，不碰网络（对应 Day 1 设计文档第五节）"""

from app.core.pipeline import Pipeline
from app.core.pipeline_step import PipelineStep
from app.models.context import ReviewContext
from app.models.issue import Issue


class RecordingStep(PipelineStep):
    """记录自身是否被执行的假 step，用于验证执行顺序与跳过逻辑。"""

    def __init__(self, name: str, log: list[str], run: bool = True):
        self.name = name
        self._log = log
        self._run = run

    def should_run(self, context: ReviewContext) -> bool:
        return self._run

    def analyze(self, context: ReviewContext) -> None:
        self._log.append(self.name)


class AddIssueStep(PipelineStep):
    name = "add_issue"

    def analyze(self, context: ReviewContext) -> None:
        context.issues.append(
            Issue(type="bug", severity="high", file="x.py", line=1, title="demo")
        )


def test_empty_pipeline_does_not_raise():
    ctx = ReviewContext()
    result = Pipeline([]).run(ctx)
    assert result is ctx
    assert result.strategy_log == []


def test_steps_run_in_order():
    order: list[str] = []
    ctx = ReviewContext()
    Pipeline([RecordingStep("a", order), RecordingStep("b", order)]).run(ctx)
    assert order == ["a", "b"]
    assert ctx.strategy_log == ["[OK] a", "[OK] b"]


def test_should_run_false_skips_step():
    order: list[str] = []
    ctx = ReviewContext()
    Pipeline([
        RecordingStep("run_me", order, run=True),
        RecordingStep("skip_me", order, run=False),
    ]).run(ctx)
    assert order == ["run_me"]                       # skip_me 未执行
    assert ctx.strategy_log == ["[OK] run_me", "[SKIP] skip_me"]


def test_step_can_append_issue_to_context():
    ctx = ReviewContext()
    Pipeline([AddIssueStep()]).run(ctx)
    assert len(ctx.issues) == 1
    assert ctx.issues[0].title == "demo"


def test_issue_severity_rank_orders_correctly():
    crit = Issue(type="bug", severity="critical", file="x", line=1, title="c")
    low = Issue(type="bug", severity="low", file="x", line=2, title="l")
    assert crit.severity_rank() > low.severity_rank()
    ordered = sorted([low, crit], key=lambda i: i.severity_rank(), reverse=True)
    assert ordered[0] is crit
