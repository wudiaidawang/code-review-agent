"""M5.1 容错 — 单工具崩溃/失败不影响 Pipeline 其余步骤。

通过替换 _TOOL_REGISTRY 中的工具实例注入故障，验证：
1. Pipeline 仍然完成（不抛异常）
2. 未崩溃的工具正常产出
3. trace 正确记录失败步骤
4. report 正常生成
"""

import app.pipeline.executor as _ex
from app.pipeline.review_pipeline import ReviewPipeline
from app.tools.contract import Tool, ToolRequest, ToolResult
from app.models.diagnostic import Diagnostic


def _crash_tool(name):
    """构造一个 execute 就抛异常的工具。"""
    def crash_execute(self, request):
        raise RuntimeError(f"{name} 模拟崩溃")
    return type("CrashTool", (), {"name": name, "execute": crash_execute})()


def _fail_tool(name, code, msg):
    """构造一个 execute 返回 failed 的工具。"""
    def fail_execute(self, request):
        return ToolResult(
            tool=name, status="failed",
            diagnostics=[Diagnostic(code=code, message=msg, tool=name)],
        )
    return type("FailTool", (), {"name": name, "execute": fail_execute})()


def _patch_registry(monkeypatch, overrides: dict):
    """替换 _TOOL_REGISTRY 中指定工具的实例。"""
    registry = dict(_ex._TOOL_REGISTRY)  # 拷贝
    registry.update(overrides)
    monkeypatch.setattr(_ex, "_TOOL_REGISTRY", registry)


class TestPipelineRecovery:
    """验证单个工具崩溃时 Pipeline 仍能完成。"""

    def test_bandit_raises_exception_pipeline_finishes(self, monkeypatch):
        """Bandit 抛 RuntimeError → Pipeline 不崩，其余产出正常。"""
        _patch_registry(monkeypatch, {"bandit": _crash_tool("bandit")})

        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        assert output.duration_ms > 0
        assert len(output.markdown) > 0
        assert len(output.change_set.get("files", [])) > 0
        assert len(output.evidence) > 0
        bandit_trace = [t for t in output.trace if "bandit" in t.step]
        if "bandit" in output.plan.get("analyzers", []):
            assert any(t.status == "failed" for t in bandit_trace)
        else:
            assert len(bandit_trace) == 0

    def test_ruff_raises_exception_pipeline_finishes(self, monkeypatch):
        """Ruff 抛异常 → Pipeline 完成，git/bandit 产出保留。"""
        _patch_registry(monkeypatch, {"ruff": _crash_tool("ruff")})

        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        assert output.duration_ms > 0
        assert len(output.markdown) > 0
        assert len(output.evidence) > 0
        ruff_trace = [t for t in output.trace if "ruff" in t.step]
        if "ruff" in output.plan.get("analyzers", []):
            assert any(t.status == "failed" for t in ruff_trace)
        else:
            assert len(ruff_trace) == 0

    def test_ast_crashes_pipeline_finishes(self, monkeypatch):
        """AST 工具抛异常 → Pipeline 完成。"""
        _patch_registry(monkeypatch, {"python_ast": _crash_tool("python_ast")})

        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        assert output.duration_ms > 0
        ast_trace = [t for t in output.trace if "python_ast" in t.step]
        if "python_ast" in output.plan.get("analyzers", []):
            assert any(t.status == "failed" for t in ast_trace)
        else:
            assert len(ast_trace) == 0

    def test_dependency_crashes_pipeline_finishes(self, monkeypatch):
        """Dependency 工具抛异常 → Pipeline 完成。"""
        _patch_registry(monkeypatch, {"dependency": _crash_tool("dependency")})

        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        assert output.duration_ms > 0
        dep_trace = [t for t in output.trace if "dependency" in t.step]
        if "dependency" in output.plan.get("analyzers", []):
            assert any(t.status == "failed" for t in dep_trace)
        else:
            # dependency 未被计划时，不应有相关 trace
            assert len(dep_trace) == 0

    def test_all_static_tools_crash_pipeline_finishes(self, monkeypatch):
        """全部静态工具崩溃 → Pipeline 仍然完成，git 产出不变。"""
        _patch_registry(monkeypatch, {
            "python_ast": _crash_tool("python_ast"),
            "ruff": _crash_tool("ruff"),
            "bandit": _crash_tool("bandit"),
            "dependency": _crash_tool("dependency"),
        })

        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        assert len(output.change_set.get("files", [])) > 0
        assert len(output.markdown) > 0
        planned_crash = [t for t in ["python_ast", "ruff", "bandit", "dependency"]
                        if t in output.plan.get("analyzers", [])]
        if planned_crash:
            failed_steps = [t for t in output.trace if t.status == "failed"]
            assert len(failed_steps) >= 1

    def test_tool_returns_failure_pipeline_continues(self, monkeypatch):
        """工具返回 status=failed（不抛异常）→ Pipeline 继续。"""
        _patch_registry(monkeypatch, {
            "bandit": _fail_tool("bandit", "NO_BANDIT", "bandit 未安装"),
        })

        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        assert output.duration_ms > 0
        bandit_trace = [t for t in output.trace if "bandit" in t.step]
        if "bandit" in output.plan.get("analyzers", []):
            assert any(t.status == "failed" for t in bandit_trace)
        ruff_trace = [t for t in output.trace if "ruff" in t.step]
        if "ruff" in output.plan.get("analyzers", []):
            assert any(t.status in ("success", "no_issues") for t in ruff_trace)

    def test_report_includes_failure_trace(self, monkeypatch):
        """崩溃后 report trace 中包含失败步骤信息。"""
        _patch_registry(monkeypatch, {"bandit": _crash_tool("bandit")})

        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        bandit_trace = [t for t in output.trace if "bandit" in t.step]
        if "bandit" in output.plan.get("analyzers", []):
            assert any(t.status == "failed" for t in bandit_trace)
            assert "bandit" in output.markdown.lower()


class TestPipelineRecoveryEdgeCases:
    """边界场景：git 失败、未知工具等。"""

    def test_git_tool_fails_still_returns_output(self, monkeypatch):
        """Git 工具失败 → Pipeline 仍返回输出（ChangeSet 为空）。"""
        _patch_registry(monkeypatch, {
            "git": _fail_tool("git", "GIT_ERROR", "git 不可用"),
        })

        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        assert output.duration_ms >= 0
        assert output.markdown is not None

    def test_unknown_tool_in_plan_no_crash(self):
        """Plan 中包含未注册工具 → 不影响已知工具执行。"""
        executor = _ex.ReviewExecutor()
        result = executor.execute(".", "HEAD~1", "HEAD", {
            "analyzers": ["git", "unknown_future_tool", "ruff"],
        })
        assert "git" in result.tool_results
        assert result.tool_results["git"].ok()
        if "ruff" in result.tool_results:
            assert result.tool_results["ruff"].ok()

    def test_recovery_time_is_recorded(self, monkeypatch):
        """失败工具的耗时仍记录在 trace 中。"""
        import time

        def slow_crash(self, request):
            time.sleep(0.05)
            raise RuntimeError("慢速崩溃")

        mock = type("SlowCrash", (), {"name": "bandit", "execute": slow_crash})()
        _patch_registry(monkeypatch, {"bandit": mock})

        pipeline = ReviewPipeline()
        output = pipeline.run(".", "HEAD~2", "HEAD")

        bandit_entries = [t for t in output.trace if "bandit" in t.step]
        if bandit_entries:
            assert bandit_entries[0].duration_ms > 0
