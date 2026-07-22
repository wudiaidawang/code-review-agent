"""Agent 评测集测试 — mock LLM，不上网。

覆盖：数据集加载、单样本执行、续问链、5 项指标计算、辅助判定函数。
"""

import json
import os

import pytest

from app.pipeline.eval_dataset import load_samples, RealInvestigationSample
from app.pipeline.agent_eval_metrics import (
    AgentEvalMetrics, _judge_completion, _has_evidence_citations, detect_budget_exceeded,
)
from app.pipeline.agent_eval_runner import AgentEvalRunner
from app.pipeline.agent_eval_judge import (
    judge_record, summarize_judgments, judge_baseline,
    _parse_judge_json, _validate_schema, _extract_json_object, _strip_fences,
    _truncate_evidence, _check_evidence_grounding,
    JUDGE_OUTPUT_SCHEMA, JUDGE_SYSTEM_PROMPT,
)


class TestRealDataset:
    """真实评测数据集加载。"""

    def test_load_agent_real(self):
        """load_samples('agent_real') 返回 RealInvestigationSample 列表。"""
        samples = load_samples("agent_real")
        assert len(samples) >= 40, f"期望 >=40 条样本，实际 {len(samples)} 条"
        for s in samples:
            assert isinstance(s, RealInvestigationSample)
            assert s.id
            assert s.question
            assert isinstance(s.ground_truth, dict)
            assert "question_type" in s.ground_truth

    def test_all_question_types_present(self):
        """五种问题类型都出现。"""
        samples = load_samples("agent_real")
        types = {s.ground_truth["question_type"] for s in samples if s.ground_truth.get("question_type")}
        for qt in ["locate", "explain", "trace", "impact", "grep"]:
            assert qt in types, f"缺少 question_type={qt}"

    def test_follow_up_groups_present(self):
        """至少有一个续问链组。"""
        samples = load_samples("agent_real")
        fu_samples = [s for s in samples if s.follow_up_group]
        assert len(fu_samples) >= 4, f"期望 >=4 条续问样本，实际 {len(fu_samples)} 条"
        groups = {s.follow_up_group for s in fu_samples}
        assert len(groups) >= 2, f"期望 >=2 组续问，实际 {len(groups)} 组"

    def test_load_external_dataset_per_project(self):
        samples = load_samples("agent_external", project="click")
        assert len(samples) == 21
        assert {s.project for s in samples} == {"click"}
        assert all(len(s.commit_sha) == 40 for s in samples)


class TestMetricsComputation:
    """AgentEvalMetrics 计算测试。"""

    def _make_record(self, answer, evidence_files=None, expected_kw=None,
                     step_count=2, is_follow_up=False, budget_exhausted=False,
                     follow_up_group="", trace=None, expected_evidence_files=None):
        """构造单条评测记录。"""
        evidence = []
        for f in (evidence_files or []):
            evidence.append({"location": {"file": f, "start_line": 10}, "snippet": "test"})
        return {
            "sample_id": "test_01",
            "question": "测试问题",
            "question_type": "locate",
            "is_follow_up": is_follow_up,
            "follow_up_group": follow_up_group,
            "answer": answer,
            "evidence": evidence,
            "files_visited": evidence_files or [],
            "steps": [{"tool": "search", "decision": "STOP"}],
            "step_count": step_count,
            "trace": trace or [],
            "duration_ms": 100,
            "investigation_id": "inv_test",
            "reused_evidence_refs": [],
            "budget_exhausted": budget_exhausted,
            "expected_answer_keywords": expected_kw or [],
            "expected_evidence_files": expected_evidence_files or [],
            "expected_answer_summary": "",
        }

    def test_task_completion_rate_perfect(self):
        """全部匹配 → 任务完成率 1.0。"""
        records = [
            self._make_record(
                answer="在 investigator.py:175 定义",
                evidence_files=["app/agent/investigator.py"],
                expected_kw=["investigator"],
                expected_evidence_files=["app/agent/investigator.py"],
            ),
            self._make_record(
                answer="在 evidence.py:20 定义",
                evidence_files=["app/models/evidence.py"],
                expected_kw=["evidence"],
                expected_evidence_files=["app/models/evidence.py"],
            ),
        ]
        metrics = AgentEvalMetrics.compute(records)
        assert metrics.task_completion_rate == 1.0

    def test_task_completion_rate_zero(self):
        """完全不匹配 → 任务完成率 0.0。"""
        records = [
            self._make_record(
                answer="不知道",
                evidence_files=[],
                expected_kw=["investigator"],
                expected_evidence_files=["app/agent/investigator.py"],
            ),
        ]
        metrics = AgentEvalMetrics.compute(records)
        assert metrics.task_completion_rate == 0.0

    def test_evidence_traceability_rate(self):
        """答案含 file:line 引用 → 证据可追溯。"""
        records = [
            self._make_record(answer="在 app/agent/investigator.py:175 定义"),
            self._make_record(answer="不知道"),
        ]
        metrics = AgentEvalMetrics.compute(records)
        assert metrics.evidence_traceability_rate == 0.5

    def test_avg_tool_steps_by_type(self):
        """按问题类型分组计算平均步数。"""
        r1 = self._make_record(answer="test", expected_evidence_files=[],
                               expected_kw=[])
        r1["question_type"] = "locate"
        r1["step_count"] = 2
        r2 = self._make_record(answer="test", expected_evidence_files=[],
                               expected_kw=[])
        r2["question_type"] = "locate"
        r2["step_count"] = 4
        r3 = self._make_record(answer="test", expected_evidence_files=[],
                               expected_kw=[])
        r3["question_type"] = "trace"
        r3["step_count"] = 3
        records = [r1, r2, r3]
        metrics = AgentEvalMetrics.compute(records)
        assert metrics.avg_tool_steps["locate"] == 3.0
        assert metrics.avg_tool_steps["trace"] == 3.0
        assert metrics.overall_avg_tool_steps == 3.0

    def test_budget_overrun_rate(self):
        """含 budget_exhausted 的样本被计为超限。"""
        records = [
            self._make_record(
                answer="test",
                budget_exhausted=True,
                trace=["step_1: ...", "budget_exhausted: steps"],
            ),
            self._make_record(
                answer="test",
                budget_exhausted=False,
                trace=["step_1: ... decision=STOP"],
            ),
        ]
        metrics = AgentEvalMetrics.compute(records)
        assert metrics.budget_overrun_rate == 0.5
        assert metrics.budget_overrun_by_type.get("steps") == 1

    def test_budget_detects_step_record_without_trace_marker(self):
        record = self._make_record(answer="test", trace=[])
        record["steps"] = [{"tool": "python_ast", "decision": "BUDGET", "budget_reason": "tokens"}]
        assert detect_budget_exceeded(record) == (True, "tokens")
        metrics = AgentEvalMetrics.compute([record])
        assert metrics.budget_overrun_rate == 1.0
        assert metrics.budget_overrun_by_type == {"tokens": 1}

    def test_budget_detects_new_terminal_limit_code(self):
        record = self._make_record(answer="test", trace=[])
        record["steps"] = [{"tool": "search", "decision": "STOP_STEP_LIMIT"}]
        assert detect_budget_exceeded(record) == (True, "steps")

    def test_budget_detects_final_state_status(self):
        record = self._make_record(answer="test", trace=[])
        record["final_state"] = {"status": "BUDGET", "budget_type": "files"}
        assert detect_budget_exceeded(record) == (True, "files")

    def test_follow_up_relative_cost(self):
        """续问指标报告相对成本。"""
        records = [
            self._make_record(answer="test", step_count=4, is_follow_up=False,
                              follow_up_group="g1"),
            self._make_record(answer="test", step_count=1, is_follow_up=True,
                              follow_up_group="g1"),
        ]
        metrics = AgentEvalMetrics.compute(records)
        assert metrics.follow_up_relative_cost == 0.25
        assert len(metrics.follow_up_per_sample) == 1

    def test_follow_up_relative_cost_varied_groups(self):
        """续问相对成本按组分别计算。"""
        records = [
            self._make_record(answer="test", step_count=2, follow_up_group="g1"),
            self._make_record(answer="test", step_count=0, is_follow_up=True,
                              follow_up_group="g1"),
            self._make_record(answer="test", step_count=1, follow_up_group="g2"),
            self._make_record(answer="test", step_count=2, is_follow_up=True,
                              follow_up_group="g2"),
        ]
        metrics = AgentEvalMetrics.compute(records)
        assert metrics.follow_up_relative_cost == 1.0

    def test_empty_records(self):
        """空记录 → 所有指标为 0 或空。"""
        metrics = AgentEvalMetrics.compute([])
        assert metrics.total_samples == 0
        assert metrics.task_completion_rate == 0.0

    def test_to_dict(self):
        """to_dict() 包含所有 5 项指标。"""
        records = [self._make_record(answer="test")]
        metrics = AgentEvalMetrics.compute(records)
        d = metrics.to_dict()
        for key in ["task_completion_rate", "evidence_traceability_rate",
                     "avg_tool_steps", "overall_avg_tool_steps",
                     "budget_overrun_rate",
                     "follow_up_relative_cost",
                     "strict_completion_rate", "evidence_retrieval_rate", "citation_grounded_rate"]:
            assert key in d, f"缺少字段 {key}"

    def test_summary_output(self):
        """summary() 返回 Markdown 字符串。"""
        records = [self._make_record(answer="test")]
        metrics = AgentEvalMetrics.compute(records)
        s = metrics.summary()
        assert "# Agent 评测报告" in s
        assert "严格完成率" in s


class TestJudgeHelpers:
    """辅助判定函数测试。"""

    def test_has_evidence_citations_file_colon_line(self):
        """识别 file.py:123 格式。"""
        record = {"answer": "在 app/agent/investigator.py:175 定义"}
        assert _has_evidence_citations(record)

    def test_has_evidence_citations_file_at_line(self):
        """识别 file.py at line 123 格式。"""
        record = {"answer": "在 app/agent/investigator.py at line 175"}
        assert _has_evidence_citations(record)

    def test_has_evidence_citations_none(self):
        """无引用 → False。"""
        record = {"answer": "不知道在哪里"}
        assert not _has_evidence_citations(record)

    def test_judge_completion_kw_and_file_match(self):
        """关键词+文件都匹配 → True。"""
        record = {
            "answer": "InvestigationAgent 在 app/agent/investigator.py 中定义",
            "evidence": [{"location": {"file": "app/agent/investigator.py"}}],
            "expected_answer_keywords": ["investigator.py"],
            "expected_evidence_files": ["app/agent/investigator.py"],
        }
        assert _judge_completion(record)

    def test_judge_completion_kw_mismatch(self):
        """关键词不匹配 → False。"""
        record = {
            "answer": "不知道",
            "evidence": [{"location": {"file": "app/agent/investigator.py"}}],
            "expected_answer_keywords": ["some_missing_kw_xyz"],
            "expected_evidence_files": ["app/agent/investigator.py"],
        }
        assert not _judge_completion(record)


class TestJudgeJsonParsing:
    """_parse_judge_json 与 JSON 提取函数测试。"""

    def test_parse_bare_json(self):
        assert _parse_judge_json('{"verdict":"correct"}')["verdict"] == "correct"

    def test_parse_fenced_json(self):
        assert _parse_judge_json('```json\n{"verdict":"correct"}\n```')["verdict"] == "correct"

    def test_parse_fenced_no_lang(self):
        assert _parse_judge_json('```\n{"verdict":"correct"}\n```')["verdict"] == "correct"

    def test_parse_json_with_prefix_text(self):
        raw = '这是评测结果：\n{"verdict": "correct", "score": 2}'
        assert _parse_judge_json(raw)["verdict"] == "correct"

    def test_parse_json_with_suffix_text(self):
        raw = '{"verdict": "correct", "score": 2}\n以上就是评判结果。'
        assert _parse_judge_json(raw)["verdict"] == "correct"

    def test_parse_json_with_text_both_sides(self):
        raw = '开始分析...\n{"verdict": "incorrect", "score": 0}\n分析完成。'
        assert _parse_judge_json(raw)["verdict"] == "incorrect"

    def test_parse_empty_string_raises(self):
        with pytest.raises(ValueError):
            _parse_judge_json("")

    def test_parse_none_raises(self):
        with pytest.raises(ValueError):
            _parse_judge_json(None)

    def test_parse_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            _parse_judge_json("   \n  ")

    def test_parse_unparseable_text_raises(self):
        with pytest.raises(ValueError):
            _parse_judge_json("这不是 JSON 内容")

    def test_parse_nested_json_object(self):
        raw = '{"verdict":"correct","missing_points":[{"point":"x","detail":"y"}]}'
        assert _parse_judge_json(raw)["missing_points"] == [{"point": "x", "detail": "y"}]

    def test_parse_fenced_then_prefix(self):
        raw = '```json\n一些说明文字\n{"verdict":"correct"}\n```'
        result = _parse_judge_json(raw)
        assert result["verdict"] == "correct"

    def test_extract_json_object_basic(self):
        assert "correct" in _extract_json_object('xxx{"verdict":"correct"}yyy')

    def test_extract_json_object_nested(self):
        text = 'prefix {"outer": {"inner": [1,2,3]}, "x": "y"} suffix'
        extracted = _extract_json_object(text)
        assert json.loads(extracted)["outer"] == {"inner": [1, 2, 3]}

    def test_strip_fences_json_tag(self):
        assert _strip_fences("```json\n{}\n```") == "{}"

    def test_strip_fences_no_tag(self):
        assert _strip_fences("```\n{}\n```") == "{}"


class TestJudgeSchemaValidation:
    """_validate_schema JSON Schema 校验测试。"""

    def _valid(self):
        return {"verdict":"correct","score":2,"answered_question":True,
                "uses_supported_evidence":True,"expected_file_coverage":"full",
                "reason":"ok","missing_points":[]}

    def test_valid_schema_no_errors(self):
        assert _validate_schema(self._valid()) == []

    def test_missing_required_fields(self):
        errors = _validate_schema({"verdict": "correct"})
        assert len(errors) >= 1
        assert any("缺少必需字段" in e for e in errors)

    def test_wrong_verdict_rejected(self):
        data = self._valid(); data["verdict"] = "wrong_value"
        errors = _validate_schema(data)
        assert len(errors) >= 1
        assert any("wrong_value" in e for e in errors)

    def test_string_instead_of_bool(self):
        data = self._valid(); data["answered_question"] = "yes"
        errors = _validate_schema(data)
        assert len(errors) >= 1
        assert any("answered_question" in e for e in errors)

    def test_int_instead_of_bool(self):
        data = self._valid(); data["answered_question"] = 1
        errors = _validate_schema(data)
        assert len(errors) >= 1

    def test_float_score_rejected(self):
        data = self._valid(); data["score"] = 1.7
        errors = _validate_schema(data)
        assert len(errors) >= 1
        assert any("score" in e for e in errors)

    def test_out_of_range_score(self):
        data = self._valid(); data["score"] = 5
        errors = _validate_schema(data)
        assert len(errors) >= 1

    def test_wrong_coverage_value(self):
        data = self._valid(); data["expected_file_coverage"] = "mostly"
        errors = _validate_schema(data)
        assert len(errors) >= 1

    def test_missing_points_not_list(self):
        data = self._valid(); data["missing_points"] = "nothing"
        errors = _validate_schema(data)
        assert len(errors) >= 1

    def test_extra_property_rejected(self):
        data = self._valid(); data["extra_field"] = "should not be here"
        errors = _validate_schema(data)
        assert len(errors) >= 1

    def test_schema_defines_all_expected_properties(self):
        assert "verdict" in JUDGE_OUTPUT_SCHEMA["properties"]
        assert "score" in JUDGE_OUTPUT_SCHEMA["properties"]
        assert "answered_question" in JUDGE_OUTPUT_SCHEMA["properties"]
        assert "uses_supported_evidence" in JUDGE_OUTPUT_SCHEMA["properties"]
        assert not JUDGE_OUTPUT_SCHEMA.get("additionalProperties", True)


class TestEvidenceTruncation:
    """_truncate_evidence 证据截断测试。"""

    def _make_evidence(self, count=30):
        ev = []
        for i in range(count):
            ev.append({"location":{"file":f"src/module_{i}.py","start_line":i*10},
                       "snippet":f"code{i}","confidence":0.95 if i<20 else 0.5})
        return ev

    def test_truncation_reduces_count(self):
        ev = self._make_evidence(30)
        result = _truncate_evidence(ev, [], "", max_items=18)
        assert len(result) <= 18

    def test_no_truncation_when_under_limit(self):
        ev = self._make_evidence(10)
        result = _truncate_evidence(ev, [], "", max_items=18)
        assert len(result) == 10

    def test_cited_files_preserved(self):
        ev = self._make_evidence(60)
        result = _truncate_evidence(ev, [], "answer mentions src/module_3.py:30 and src/module_7.py:70", max_items=18)
        result_files = {e["location"]["file"] for e in result}
        assert "src/module_3.py" in result_files
        assert "src/module_7.py" in result_files

    def test_expected_files_included_but_not_exclusive(self):
        ev = self._make_evidence(60)
        expected = ["src/module_1.py", "src/module_2.py"]
        result = _truncate_evidence(ev, expected, "", max_items=18)
        result_files = {e["location"]["file"] for e in result}
        assert "src/module_1.py" in result_files
        assert "src/module_2.py" in result_files
        # 必须有多样性：不只含预期文件
        non_expected = [f for f in result_files if f not in expected]
        assert len(non_expected) >= 3, f"只有 {len(non_expected)} 条非预期文件证据，多样性不足"

    def test_high_confidence_prioritized(self):
        ev = self._make_evidence(30)
        result = _truncate_evidence(ev, [], "", max_items=10)
        confidences = [e["confidence"] for e in result]
        # 大部分应该是高置信度的
        high_conf = sum(1 for c in confidences if c >= 0.9)
        assert high_conf >= len(result) * 0.5, f"只有 {high_conf}/{len(result)} 高置信度"


class TestSemanticJudge:
    """judge_record / summarize_judgments / judge_baseline 集成测试。"""

    def _valid_json(self):
        return '{"verdict":"correct","score":2,"answered_question":true,"uses_supported_evidence":true,"expected_file_coverage":"full","reason":"ok","missing_points":[]}'

    def test_judge_uses_structured_json_and_disabled_thinking(self):
        seen = {}
        def fake(prompt, **kwargs):
            seen.update(kwargs)
            return self._valid_json()
        result = judge_record({"sample_id":"x","question":"q","final_answer":"a","evidence":[]}, fake)
        assert result["verdict"] == "correct"
        assert result["judge_error_type"] is None
        assert result["schema_errors"] == []
        assert seen["extra_body"] == {"thinking": {"type": "disabled"}}

    def test_judge_summary_distinguishes_error_types(self):
        summary = summarize_judgments([
            {"verdict":"correct", "uses_supported_evidence":True,
             "judge_error_type": None, "judge_error": None, "schema_errors": []},
            {"verdict":"unjudgeable", "uses_supported_evidence":False,
             "judge_error_type": "judge_unavailable", "judge_error": "empty", "schema_errors": []},
            {"verdict":"unjudgeable", "uses_supported_evidence":False,
             "judge_error_type": "judge_invalid_schema", "judge_error": "bad verdict", "schema_errors": ["bad verdict"]},
        ])
        assert summary["semantic_completion_rate"] == 1.0
        assert summary["judge_unavailable_rate"] == pytest.approx(1/3)
        assert summary["judge_invalid_schema_rate"] == pytest.approx(1/3)
        assert summary["judge_effective_rate"] == pytest.approx(1/3)

    def test_judge_preserves_empty_response_for_audit(self):
        result = judge_record({"sample_id":"x", "question":"q", "final_answer":"a", "evidence":[]}, lambda *a, **k: "")
        assert result["verdict"] == "unjudgeable"
        assert result["raw_judge_response"] == ""
        assert result["judge_error_type"] == "judge_unavailable"
        assert "empty" in result["judge_error"]

    def test_judge_preserves_none_response_for_audit(self):
        result = judge_record({"sample_id":"x", "question":"q", "final_answer":"a", "evidence":[]}, lambda *a, **k: None)
        assert result["verdict"] == "unjudgeable"
        assert result["raw_judge_response"] is None
        assert result["judge_error_type"] == "judge_unavailable"

    def test_judge_detects_invalid_schema(self):
        """Schema 不合法 → 重试仍不合法 → judge_invalid_schema。"""
        bad_json = '{"verdict":"wrong","score":5,"answered_question":"maybe","uses_supported_evidence":1,"expected_file_coverage":"all","reason":123,"missing_points":"x"}'
        fake = lambda *a, **k: bad_json
        result = judge_record({"sample_id":"x","question":"q","final_answer":"a","evidence":[]}, fake)
        assert result["judge_error_type"] == "judge_invalid_schema"
        assert len(result["schema_errors"]) > 0
        assert result["raw_judge_response"] is not None

    def test_judge_accepts_fenced_json(self):
        fake = lambda *a, **k: '```json\n' + self._valid_json() + '\n```'
        result = judge_record({"sample_id":"x","question":"q","final_answer":"a","evidence":[]}, fake)
        assert result["judge_error_type"] is None
        assert result["verdict"] == "correct"

    def test_judge_accepts_json_with_prefix_text(self):
        fake = lambda *a, **k: '分析结论如下：\n' + self._valid_json()
        result = judge_record({"sample_id":"x","question":"q","final_answer":"a","evidence":[]}, fake)
        assert result["judge_error_type"] is None
        assert result["verdict"] == "correct"

    def test_retry_succeeds_after_first_parse_failure(self):
        calls = []
        def fake(prompt, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                return "这不是 JSON"
            else:
                return self._valid_json()
        result = judge_record({"sample_id":"x","question":"q","final_answer":"a","evidence":[]}, fake)
        assert result["judge_error_type"] is None
        assert result["verdict"] == "correct"
        assert result["retry_error"] is not None
        assert "first attempt" in result["retry_error"]
        assert result["raw_judge_response"] == self._valid_json()

    def test_retry_fails_both_attempts_unavailable(self):
        calls = []
        def fake(prompt, **kwargs):
            calls.append(prompt)
            return "still not JSON"
        result = judge_record({"sample_id":"x","question":"q","final_answer":"a","evidence":[]}, fake)
        assert result["judge_error_type"] == "judge_unavailable"
        assert result["raw_judge_response"] == "still not JSON"
        assert result["retry_raw_response"] == "still not JSON"
        assert "retry also failed" in result["judge_error"]

    def test_retry_first_empty_then_success(self):
        calls = []
        def fake(prompt, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                return ""
            else:
                return self._valid_json()
        result = judge_record({"sample_id":"x","question":"q","final_answer":"a","evidence":[]}, fake)
        assert result["judge_error_type"] is None
        assert result["verdict"] == "correct"
        assert len(calls) == 2

    def test_schema_error_triggers_retry(self):
        """首次 Schema 不合法 → 触发修复重试 → 重试也失败 → judge_invalid_schema。"""
        bad_json = '{"verdict":"bad_verdict","score":2,"answered_question":true,"uses_supported_evidence":true,"expected_file_coverage":"full","reason":"x","missing_points":[]}'
        fake = lambda *a, **k: bad_json
        result = judge_record({"sample_id":"x","question":"q","final_answer":"a","evidence":[]}, fake)
        assert result["judge_error_type"] == "judge_invalid_schema"
        # 重试原响应应被保存
        assert result["retry_raw_response"] is not None

    def test_api_exception_becomes_unavailable(self):
        def fake(*a, **k):
            raise RuntimeError("API timeout")
        result = judge_record({"sample_id":"x","question":"q","final_answer":"a","evidence":[]}, fake)
        assert result["judge_error_type"] == "judge_unavailable"
        assert "API timeout" in result["judge_error"]

    def test_judge_baseline_resumes_existing_output(self, tmp_path):
        source = tmp_path / "v0.json"
        output = tmp_path / "judgments.json"
        source.write_text(json.dumps({"baseline_id": "v0", "samples": [
            {"sample_id": "x", "question": "q", "final_answer": "a", "evidence": []},
            {"sample_id": "y", "question": "q2", "final_answer": "a2", "evidence": []},
        ]}), encoding="utf-8")
        # 预填充一条已有判决，验证恢复机制不重判
        output.write_text(json.dumps({"judgments": [
            {"sample_id": "x", "verdict": "correct", "uses_supported_evidence": True, "judge_error_type": None,
             "raw_judge_response": "old", "judge_error": None, "schema_errors": []},
        ]}), encoding="utf-8")
        call_count = [0]
        def fake(*a, **k):
            call_count[0] += 1
            return '{"verdict":"correct","score":2,"answered_question":true,"uses_supported_evidence":true,"expected_file_coverage":"full","reason":"ok","missing_points":[]}'
        result = judge_baseline(str(source), str(output), call_llm=fake)
        # 只应调用一次（为 sample_id="y"），x 已有判决不重判
        assert call_count[0] == 1
        assert len(result["judgments"]) == 2
        assert result["summary"]["semantic_completion_rate"] == 1.0

    def test_judge_baseline_no_resume_rejudges_all(self, tmp_path):
        source = tmp_path / "v0.json"
        output = tmp_path / "judgments.json"
        source.write_text(json.dumps({"baseline_id": "v0", "samples": [
            {"sample_id": "x", "question": "q", "final_answer": "a", "evidence": []},
        ]}), encoding="utf-8")
        output.write_text(json.dumps({"judgments": [
            {"sample_id": "x", "verdict": "incorrect", "uses_supported_evidence": False, "judge_error_type": None,
             "raw_judge_response": "old", "judge_error": None, "schema_errors": []},
        ]}), encoding="utf-8")
        call_count = [0]
        def fake(*a, **k):
            call_count[0] += 1
            return '{"verdict":"correct","score":2,"answered_question":true,"uses_supported_evidence":true,"expected_file_coverage":"full","reason":"ok","missing_points":[]}'
        result = judge_baseline(str(source), str(output), resume=False, call_llm=fake)
        assert call_count[0] == 1
        assert result["judgments"][0]["verdict"] == "correct"

    def test_judge_summary_retry_count(self):
        summary = summarize_judgments([
            {"verdict":"correct", "uses_supported_evidence":True,
             "judge_error_type": None, "judge_error": None, "schema_errors": [],
             "retry_error": "first attempt: ..."},
            {"verdict":"unjudgeable", "uses_supported_evidence":False,
             "judge_error_type": "judge_unavailable", "judge_error": "fail", "schema_errors": [],
             "retry_error": None},
        ])
        assert summary["retry_success_count"] == 1

    def test_raw_response_always_persisted(self):
        """原始 LLM 返回必须始终持久化，任何情况都不能丢。"""
        test_cases = [
            ("", "empty"),
            ("not json", "unparseable"),
            ('{"verdict":"correct","score":2,"answered_question":true,"uses_supported_evidence":true,"expected_file_coverage":"full","reason":"x","missing_points":[]}', "valid"),
            ('```json\n{"verdict":"correct","score":2,"answered_question":true,"uses_supported_evidence":true,"expected_file_coverage":"full","reason":"x","missing_points":[]}\n```', "fenced"),
            ('结论：\n{"verdict":"correct","score":2,"answered_question":true,"uses_supported_evidence":true,"expected_file_coverage":"full","reason":"x","missing_points":[]}\n完成', "with text"),
        ]
        for raw, case_name in test_cases:
            def make_fake(response):
                return lambda *a, **k: response
            result = judge_record({"sample_id": case_name, "question":"q", "final_answer":"a", "evidence":[]}, make_fake(raw))
            assert result["raw_judge_response"] == raw, f"case {case_name}: raw_judge_response not preserved"


class TestRunnerMock:
    """AgentEvalRunner mock 模式集成测试。"""

    def test_run_single_sample(self):
        """mock 模式执行单个样本不报错。"""
        runner = AgentEvalRunner(mock=True)
        samples = load_samples("agent_real")
        assert len(samples) > 0
        record = runner._run_single(samples[0])
        assert record["sample_id"]
        assert record["answer"]
        assert "step_count" in record
        assert record["duration_ms"] >= 0

    def test_run_top_5(self):
        """mock 模式跑前 5 条样本。"""
        runner = AgentEvalRunner(mock=True)
        result = runner.run_all(top_n=5, verbose=False)
        assert len(result.per_sample) == 5
        assert result.metrics.total_samples == 5

    def test_result_serializes_metrics_and_samples(self):
        runner = AgentEvalRunner(mock=True)
        result = runner.run_all(top_n=1, verbose=False)
        payload = result.to_dict()
        assert payload["metrics"]["total_samples"] == 1
        assert len(payload["per_sample"]) == 1

    def test_run_follow_up_chain(self):
        """mock 模式执行一个续问链。"""
        samples = load_samples("agent_real")
        fu_samples = [s for s in samples if s.follow_up_group == "fu_group_01"]
        if len(fu_samples) >= 2:
            runner = AgentEvalRunner(mock=True)
            fu_samples.sort(key=lambda x: x.follow_up_order)
            results = runner._run_follow_up_chain(fu_samples)
            assert len(results) == 2
            assert results[0]["is_follow_up"] is False
            assert results[1]["is_follow_up"] is True
            assert results[1]["investigation_id"] == results[0]["investigation_id"]


class TestAgentRealJsonIntegrity:
    """验证 JSON 数据集完整性。"""

    def test_all_ids_unique(self):
        """所有样本 ID 唯一。"""
        samples = load_samples("agent_real")
        ids = [s.id for s in samples]
        assert len(ids) == len(set(ids)), f"有重复 ID"

    def test_follow_up_order_sequential(self):
        """同一 follow_up_group 内 order 从 0 递增。"""
        samples = load_samples("agent_real")
        from collections import defaultdict
        groups = defaultdict(list)
        for s in samples:
            if s.follow_up_group:
                groups[s.follow_up_group].append(s.follow_up_order)
        for gid, orders in groups.items():
            orders.sort()
            assert orders == list(range(len(orders))), \
                f"组 {gid} 的 order 不连续: {orders}"

    def test_ground_truth_has_required_fields(self):
        """每条样本的 ground_truth 包含必要字段。"""
        samples = load_samples("agent_real")
        for s in samples:
            gt = s.ground_truth
            assert "question_type" in gt, f"{s.id} 缺少 question_type"
            assert "expected_answer_keywords" in gt, f"{s.id} 缺少 expected_answer_keywords"
            assert "expected_answer_summary" in gt, f"{s.id} 缺少 expected_answer_summary"


class TestEvidenceGrounding:
    """_check_evidence_grounding() 增强：文件匹配 + 行号范围 + snippet 行号标注。"""

    def _make_evidence(self, file, start_line=0, end_line=0, snippet=""):
        return [{
            "location": {"file": file, "start_line": start_line, "end_line": end_line},
            "snippet": snippet,
        }]

    def test_file_exists_line_in_range_grounded(self):
        ev = self._make_evidence("app/agent/investigator.py", 296, 310,
                                  "296| class InvestigationAgent:\n297|     ...")
        result = _check_evidence_grounding("见 app/agent/investigator.py:296 定义", ev)
        assert result["grounded"] is True
        assert result["total_verified_lines"] == 1
        assert result["no_refs"] is False
        assert len(result["ungrounded_entries"]) == 0

    def test_file_exists_line_out_of_range_ungrounded(self):
        ev = self._make_evidence("app/agent/investigator.py", 296, 310)
        result = _check_evidence_grounding("见 app/agent/investigator.py:999 定义", ev)
        assert result["grounded"] is False
        assert result["total_verified_lines"] == 0
        assert len(result["ungrounded_entries"]) == 1
        assert result["ungrounded_entries"][0]["reason"] == "line out of evidence range"

    def test_file_not_found_ungrounded(self):
        ev = self._make_evidence("app/agent/investigator.py", 296, 310)
        result = _check_evidence_grounding("见 app/models/evidence.py:42 定义", ev)
        assert result["grounded"] is False
        assert result["ungrounded_entries"][0]["reason"] == "file not found in evidence"

    def test_no_refs_not_grounded(self):
        """答案无文件引用 → no_refs=True，不自动 grounded。"""
        ev = self._make_evidence("app/agent/investigator.py", 296, 310)
        result = _check_evidence_grounding("无法确定，证据不足", ev)
        assert result["grounded"] is False
        assert result["no_refs"] is True
        assert result["total_refs"] == 0

    def test_mixed_refs_partial_verified(self):
        """混合引用：部分文件匹配+行号通过，部分行号越界。"""
        ev = [
            {"location": {"file": "app/agent/investigator.py", "start_line": 296, "end_line": 310},
             "snippet": "296| class InvestigationAgent:"},
            {"location": {"file": "app/models/evidence.py", "start_line": 18, "end_line": 28},
             "snippet": "18| @dataclass\n19| class Evidence:"},
        ]
        result = _check_evidence_grounding(
            "见 app/agent/investigator.py:300 和 app/models/evidence.py:999",
            ev)
        assert result["grounded"] is False
        assert result["total_verified_lines"] == 1
        assert len(result["ungrounded_entries"]) == 1
        assert "999" in result["ungrounded_entries"][0]["ref"]

    def test_snippet_line_number_fallback(self):
        """当 start_line/end_line=0 时，应从 snippet 提取行号进行匹配。"""
        ev = [{
            "location": {"file": "app/agent/investigator.py", "start_line": 0, "end_line": 0},
            "snippet": "296| class InvestigationAgent:\n297|     \"\"\"...\"\"\"\n298|     def __init__(self):",
        }]
        result = _check_evidence_grounding("见 app/agent/investigator.py:297 定义", ev)
        assert result["grounded"] is True
        assert result["total_verified_lines"] == 1

    def test_whole_file_evidence_passes_any_line(self):
        """整文件级证据（start_line=end_line=0 且无 snippet）→ 行号检查通过。"""
        ev = self._make_evidence("app/agent/investigator.py", 0, 0, "")
        result = _check_evidence_grounding("见 app/agent/investigator.py:42 定义", ev)
        assert result["grounded"] is True
        assert result["total_verified_lines"] == 1

    def test_windows_path_normalization(self):
        """Windows 反斜杠路径在比较时统一为 /。"""
        ev = self._make_evidence("app\\agent\\investigator.py", 296, 310)
        result = _check_evidence_grounding("见 app/agent/investigator.py:296 定义", ev)
        assert result["grounded"] is True

    def test_multiple_references_all_verified(self):
        ev = [
            {"location": {"file": "app/agent/investigator.py", "start_line": 296, "end_line": 310},
             "snippet": "296| class InvestigationAgent:"},
            {"location": {"file": "app/agent/task_explorer.py", "start_line": 80, "end_line": 100},
             "snippet": "80| class ExplorationState:"},
        ]
        result = _check_evidence_grounding(
            "见 app/agent/investigator.py:300 和 app/agent/task_explorer.py:85",
            ev)
        assert result["grounded"] is True
        assert result["total_verified_lines"] == 2


class TestExpectedStatusRemoved:
    """expected_status=removed 时的 Judge 行为验证。"""

    def _valid_correct(self):
        return ('{"verdict":"correct","score":2,"answered_question":true,'
                '"uses_supported_evidence":true,"expected_file_coverage":"full",'
                '"reason":"正确报告符号已删除","missing_points":[]}')

    def test_expected_status_passed_in_payload(self):
        """验证 expected_status 和 expected_replacement 出现在 judge payload 中。"""
        seen_payload = []
        def fake(prompt, system=None, **kwargs):
            payload = json.loads(prompt)
            seen_payload.append(payload)
            return self._valid_correct()
        record = {
            "sample_id": "x", "question": "is_budget_exhausted 在哪定义？",
            "final_answer": "V22 中该符号已被删除，由 ExplorationState.consume_budget() 替代",
            "evidence": [],
            "expected_answer_summary": "V22 中已删除",
            "expected_answer_keywords": ["consume_budget"],
            "expected_evidence_files": [],
            "expected_status": "removed",
            "expected_replacement": "ExplorationState.consume_budget()",
        }
        result = judge_record(record, fake)
        assert result["verdict"] == "correct"
        assert seen_payload[0]["expected_status"] == "removed"
        assert seen_payload[0]["expected_replacement"] == "ExplorationState.consume_budget()"

    def test_expected_status_defaults_to_active(self):
        """未提供 expected_status 时默认值为 active。"""
        seen_payload = []
        def fake(prompt, system=None, **kwargs):
            payload = json.loads(prompt)
            seen_payload.append(payload)
            return self._valid_correct()
        record = {
            "sample_id": "x", "question": "q",
            "final_answer": "a", "evidence": [],
            "expected_answer_summary": "",
            "expected_answer_keywords": [],
            "expected_evidence_files": [],
        }
        result = judge_record(record, fake)
        assert seen_payload[0]["expected_status"] == "active"
        assert seen_payload[0]["expected_replacement"] == ""

    def test_agent_reports_removed_symbol_judge_correct(self):
        """Agent 正确报告符号已删除 + expected_status=removed → correct。"""
        fake = lambda *a, **k: self._valid_correct()
        record = {
            "sample_id": "real_explain_03", "question": "is_budget_exhausted 如何判断预算耗尽？",
            "final_answer": "无法回答，未找到相关证据。该符号在 V22 中已被删除。",
            "evidence": [],
            "expected_answer_summary": "V22 中该符号已被替代",
            "expected_answer_keywords": ["consume_budget"],
            "expected_evidence_files": ["app/agent/task_explorer.py"],
            "expected_status": "removed",
            "expected_replacement": "ExplorationState.consume_budget()",
        }
        result = judge_record(record, fake)
        assert result["judge_error_type"] is None

    def test_evidence_grounding_downgrades_correct_to_partial(self):
        """答案引用不在证据中的文件 → grounding 降级 correct → partially_correct。"""
        fake = lambda *a, **k: self._valid_correct()
        record = {
            "sample_id": "x", "question": "q",
            "final_answer": "该函数定义在 app/models/nonexistent.py:42",
            "evidence": [],
            "expected_answer_summary": "定义在 app/models/evidence.py",
            "expected_answer_keywords": [],
            "expected_evidence_files": [],
        }
        result = judge_record(record, fake)
        # 答案引用 nonexistent.py，应降级
        assert result["verdict"] == "partially_correct"
        assert "证据锚定降级" in result["reason"]
        assert result.get("evidence_grounding") is not None
        assert result["evidence_grounding"]["grounded"] is False
