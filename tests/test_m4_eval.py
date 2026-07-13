"""M4 评测体系 — 单元测试（mock LLM，不上网）。"""
import json
from app.pipeline.eval_dataset import load_samples, EvalSample, to_json
from app.pipeline.eval_metrics import compute, EvalMetrics, _set_precision, _set_recall, _set_f1
from app.pipeline.eval_benchmark import (
    run_rule_baseline, run_llm_planner, run_benchmark,
    _build_user_prompt, _parse_llm_output, BenchmarkResult,
)


class TestEvalDataset:
    def test_load_v1_returns_10_samples(self):
        samples = load_samples(dataset_version="v1")
        assert len(samples) == 10
        for s in samples:
            assert isinstance(s, EvalSample)
            assert s.id
            assert s.scenario
            assert "analyzers" in s.ground_truth
            assert "risk_level" in s.ground_truth

    def test_v1_samples_have_required_input_fields(self):
        for s in load_samples(dataset_version="v1"):
            inp = s.input
            assert "change_summary" in inp
            assert "file_types" in inp
            assert "diff_size" in inp
            assert "risk_signals" in inp
            assert "ast_summary" in inp

    def test_v2_samples_have_required_fields(self):
        """v2 数据集 700 条样本必须都有合法字段。"""
        samples = load_samples(dataset_version="latest")
        assert len(samples) >= 500  # 至少有大量样本
        for s in samples[:20]:  # 抽查前 20 条
            assert s.id
            assert s.ground_truth
            assert "analyzers" in s.ground_truth
            assert "risk_level" in s.ground_truth

    def test_to_json_roundtrip(self):
        samples = load_samples(dataset_version="v1")
        js = to_json(samples)
        data = json.loads(js)
        assert len(data) == 10
        assert data[0]["id"] == "s001_simple_python"


class TestSetMetrics:
    def test_precision_full_match(self):
        assert _set_precision({"a", "b"}, {"a", "b"}) == 1.0

    def test_precision_half(self):
        assert _set_precision({"a", "b", "c"}, {"a", "b"}) == 2 / 3

    def test_precision_empty_pred(self):
        assert _set_precision(set(), {"a"}) == 0.0

    def test_recall_full_match(self):
        assert _set_recall({"a", "b"}, {"a", "b"}) == 1.0

    def test_recall_missing(self):
        assert _set_recall({"a"}, {"a", "b"}) == 0.5

    def test_recall_empty_ground(self):
        assert _set_recall({"a"}, set()) == 1.0

    def test_f1_perfect(self):
        assert _set_f1(1.0, 1.0) == 1.0

    def test_f1_zero(self):
        assert _set_f1(0.0, 0.0) == 0.0


class TestEvalMetrics:
    def test_compute_perfect_match(self):
        preds = [{"id": "s1", "analyzers": ["git", "ruff"], "risk_level": "low", "reason_codes": []}]
        gts = [{"id": "s1", "analyzers": ["git", "ruff"], "risk_level": "low", "reason_codes": []}]
        m = compute(preds, gts)
        assert m.analyzer_precision == 1.0
        assert m.analyzer_recall == 1.0
        assert m.analyzer_f1 == 1.0
        assert m.risk_level_accuracy == 1.0

    def test_compute_empty_input(self):
        m = compute([], [])
        assert m.total_samples == 0

    def test_compute_wrong_analyzers(self):
        preds = [{"id": "s1", "analyzers": ["git"], "risk_level": "low", "reason_codes": []}]
        gts = [{"id": "s1", "analyzers": ["git", "ruff", "bandit"], "risk_level": "low", "reason_codes": []}]
        m = compute(preds, gts)
        assert m.analyzer_precision == 1.0  # git 选对了
        assert m.analyzer_recall == 1 / 3   # 只召回了 git
        assert m.analyzer_f1 < 1.0

    def test_risk_level_accuracy(self):
        preds = [
            {"id": "s1", "analyzers": ["git"], "risk_level": "low", "reason_codes": []},
            {"id": "s2", "analyzers": ["git"], "risk_level": "medium", "reason_codes": []},
            {"id": "s3", "analyzers": ["git"], "risk_level": "high", "reason_codes": []},
        ]
        gts = [
            {"id": "s1", "analyzers": ["git"], "risk_level": "low", "reason_codes": []},
            {"id": "s2", "analyzers": ["git"], "risk_level": "low", "reason_codes": []},
            {"id": "s3", "analyzers": ["git"], "risk_level": "high", "reason_codes": []},
        ]
        m = compute(preds, gts)
        assert m.risk_level_accuracy == 2 / 3

    def test_high_risk_recall_bandit_missed(self):
        preds = [{"id": "s1", "analyzers": ["git", "ruff"], "risk_level": "medium", "reason_codes": ["sql_risk"]}]
        gts = [{"id": "s1", "analyzers": ["git", "ruff", "bandit"], "risk_level": "high", "reason_codes": ["sql_risk"]}]
        m = compute(preds, gts)
        assert m.high_risk_recall == 0.0  # bandit 没被选

    def test_high_risk_recall_bandit_present(self):
        preds = [{"id": "s1", "analyzers": ["git", "ruff", "bandit"], "risk_level": "medium", "reason_codes": ["auth_change"]}]
        gts = [{"id": "s1", "analyzers": ["git", "ruff", "bandit"], "risk_level": "medium", "reason_codes": ["auth_change"]}]
        m = compute(preds, gts)
        assert m.high_risk_recall == 1.0

    def test_per_sample_detail(self):
        preds = [{"id": "s1", "analyzers": ["git"], "risk_level": "low", "reason_codes": []}]
        gts = [{"id": "s1", "analyzers": ["git", "ruff"], "risk_level": "low", "reason_codes": []}]
        m = compute(preds, gts)
        assert len(m.per_sample) == 1
        assert m.per_sample[0]["sample_id"] == "s1"

    def test_to_dict(self):
        m = compute([], [])
        d = m.to_dict()
        assert "analyzer_f1" in d
        assert "high_risk_recall" in d


class TestRuleBaseline:
    def test_baseline_returns_10_predictions(self):
        samples = load_samples(dataset_version="v1")
        preds = run_rule_baseline(samples, verbose=False)
        assert len(preds) == 10
        for p in preds:
            assert "analyzers" in p
            assert "risk_level" in p
            assert "reason_codes" in p
            assert "id" in p

    def test_baseline_non_python_only_git(self):
        """s006 (markdown only) → 只选 git"""
        from app.pipeline.eval_dataset import _SAMPLES
        s006 = [s for s in _SAMPLES if s["id"] == "s006_non_python"][0]
        from app.pipeline.plan_builder import RuleBasedPlanBuilder
        builder = RuleBasedPlanBuilder()
        plan = builder.build({"files": [{"path": "README.md", "change_type": "modified",
                                          "added_lines": 50, "deleted_lines": 20}]})
        assert "ruff" not in plan.analyzers
        assert "python_ast" not in plan.analyzers
        assert "git" in plan.analyzers

    def test_baseline_empty_change(self):
        """s010 (空变更) → 只选 git"""
        from app.pipeline.plan_builder import RuleBasedPlanBuilder
        builder = RuleBasedPlanBuilder()
        plan = builder.build({"files": []})
        assert plan.analyzers == ["git"]
        assert plan.risk_level == "low"


class TestLLMParser:
    def test_parse_valid_json(self):
        raw = '{"analyzers": ["git", "ruff"], "risk_level": "low", "reason_codes": []}'
        pred = _parse_llm_output(raw, "s001")
        assert pred["analyzers"] == ["git", "ruff"]
        assert pred["risk_level"] == "low"

    def test_parse_markdown_wrapped(self):
        raw = '```json\n{"analyzers": ["git"], "risk_level": "medium", "reason_codes": ["auth_change"]}\n```'
        pred = _parse_llm_output(raw, "s002")
        assert pred["analyzers"] == ["git"]
        assert pred["risk_level"] == "medium"

    def test_parse_invalid_json(self):
        raw = "not json at all"
        pred = _parse_llm_output(raw, "sX")
        assert "_error" in pred
        assert pred["analyzers"] == []


class TestBenchmarkWithMockLLM:
    """用 mock LLM 验证评测流程（不上网）。"""

    def test_run_llm_planner_with_mock(self, monkeypatch):
        def mock_chat(prompt, system="", temperature=0.1, max_tokens=500):
            return json.dumps({"analyzers": ["git", "ruff", "bandit"],
                               "risk_level": "medium", "reason_codes": ["sql_risk"]})

        monkeypatch.setattr("app.pipeline.eval_benchmark.chat", mock_chat)

        samples = load_samples()[:3]
        preds = run_llm_planner(samples, verbose=False)
        assert len(preds) == 3
        for p in preds:
            assert "bandit" in p["analyzers"]
            assert p["risk_level"] == "medium"

    def test_llm_planner_json_parse_error(self, monkeypatch):
        def mock_chat_bad(prompt, system="", temperature=0.1, max_tokens=500):
            return "not valid json ###"

        monkeypatch.setattr("app.pipeline.eval_benchmark.chat", mock_chat_bad)

        samples = load_samples()[:1]
        preds = run_llm_planner(samples, verbose=False)
        assert len(preds) == 1
        assert "_error" in preds[0]

    def test_llm_planner_exception(self, monkeypatch):
        def mock_chat_fail(prompt, system="", temperature=0.1, max_tokens=500):
            raise RuntimeError("API 不可用")

        monkeypatch.setattr("app.pipeline.eval_benchmark.chat", mock_chat_fail)

        samples = load_samples()[:1]
        preds = run_llm_planner(samples, verbose=False)
        assert len(preds) == 1
        assert "API 不可用" in preds[0]["_error"]

    def test_full_benchmark_with_mock(self, monkeypatch):
        def mock_chat(prompt, system="", temperature=0.1, max_tokens=500):
            return json.dumps({"analyzers": ["git", "ruff"], "risk_level": "low", "reason_codes": []})

        monkeypatch.setattr("app.pipeline.eval_benchmark.chat", mock_chat)

        result = run_benchmark(top_n=3, verbose=False)
        assert isinstance(result, BenchmarkResult)
        assert result.llm_metrics.total_samples == 3
        assert result.baseline_metrics.total_samples == 3
        assert len(result.llm_predictions) == 3
        assert len(result.baseline_predictions) == 3
        # 验证 summary 可生成
        s = result.summary()
        assert "M4 评测报告" in s


class TestUserPrompt:
    def test_build_prompt_contains_key_info(self):
        sample = {"id": "s002", "input": {
            "change_summary": "修改 auth.py",
            "file_types": [".py"],
            "diff_size": {"files": 2, "added_lines": 80, "deleted_lines": 30},
            "risk_signals": ["auth_change"],
            "ast_summary": "1 function modified",
            "static_findings_count": 2,
        }}
        prompt = _build_user_prompt(sample)
        assert "auth.py" in prompt
        assert "auth_change" in prompt
        assert "80" in prompt
