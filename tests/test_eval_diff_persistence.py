"""评测证据链测试 — unified_diff 必须随 Pipeline 结果持久化，Judge 直接使用该证据。

背景：Judge 曾在评判时到样本目录执行 `git diff HEAD~1..HEAD` 取证。
样本放在可被清理的临时目录，目录一旦失效（非 git 仓库），整批 Judge 结果全部无效。
正确设计：Pipeline 运行当时就把 unified_diff 固化进结果 JSON，Judge 只消费持久化证据。
"""

import json

from tests.helpers import FIXED_UNIFIED_DIFF


class TestPipelineCarriesDiff:
    """ReviewOutput 与 run_pipeline 结果必须携带 unified_diff。"""

    def test_review_output_carries_unified_diff(self, fixed_git_diff):
        from app.pipeline.review_pipeline import ReviewPipeline

        output = ReviewPipeline().run(".", "HEAD~1", "HEAD")
        assert output.unified_diff == FIXED_UNIFIED_DIFF

    def test_run_one_persists_unified_diff(self, fixed_git_diff):
        from eval_report.run_pipeline import run_one

        result = run_one(".", "test_sample", mode="static")
        assert result.get("error") is None
        assert result["unified_diff"] == FIXED_UNIFIED_DIFF
        # 结果必须可 JSON 序列化（持久化到磁盘的前提）
        json.dumps(result, ensure_ascii=False)


class TestJudgeUsesPersistedDiff:
    """Judge 不得依赖样本目录的 git 仓库。"""

    def _fake_chat(self, captured: dict):
        def fake_chat(prompt, system=None, **kwargs):
            captured["prompt"] = prompt
            return json.dumps({
                "per_issue": [{"issue_index": 0, "verdict": "correct", "reason": "ok"}],
                "missed": [],
                "overall_assessment": "ok",
            })
        return fake_chat

    def test_judge_works_without_sample_repo(self, monkeypatch, tmp_path):
        """样本目录已被清理时，Judge 仍能基于持久化 diff 正常评判。"""
        import eval_report.judge as judge_mod

        captured = {}
        monkeypatch.setattr(judge_mod, "chat", self._fake_chat(captured))

        gone_dir = str(tmp_path / "cleaned_up_sample")  # 不存在，更不是 git 仓库
        pipeline_output = {
            "sample_id": "s01_test",
            "sample_dir": gone_dir,
            "unified_diff": FIXED_UNIFIED_DIFF,
            "issues": [{"title": "SQL 注入", "severity": "high",
                        "file": "auth.py", "line": 11, "source": ["bandit"]}],
        }

        result = judge_mod.judge_one("s01_test", pipeline_output)

        assert not result.get("error")
        assert result["per_issue"][0]["verdict"] == "correct"
        # 喂给 Judge 的就是持久化的 diff
        assert "diff --git a/auth.py b/auth.py" in captured["prompt"]

    def test_judge_errors_clearly_when_diff_missing(self, monkeypatch, tmp_path):
        """旧版结果没有 unified_diff 字段：必须报明确错误，而不是静默回退到 git。"""
        import eval_report.judge as judge_mod

        monkeypatch.setattr(judge_mod, "chat", self._fake_chat({}))

        pipeline_output = {
            "sample_id": "s02_test",
            "sample_dir": str(tmp_path / "gone"),
            "issues": [],
        }

        result = judge_mod.judge_one("s02_test", pipeline_output)

        assert result.get("error")
        assert "unified_diff" in result["error"]


class TestMetricsResultsDir:
    """metrics 必须能从指定目录（results/static、results/llm）加载配对数据。"""

    def test_load_data_from_custom_dir(self, tmp_path):
        from eval_report.metrics import _load_data

        (tmp_path / "s01_pipeline_output.json").write_text(
            json.dumps({"sample_id": "s01", "issues": []}), encoding="utf-8")
        (tmp_path / "s01_judgment.json").write_text(
            json.dumps({"sample_id": "s01", "per_issue": []}), encoding="utf-8")

        paired = _load_data(tmp_path)

        assert len(paired) == 1
        assert paired[0]["sample_id"] == "s01"
        assert paired[0]["judgment"] is not None
