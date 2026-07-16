"""M3 LLM 语义审查 — 单元测试（mock LLM，不上网）。"""
import json
from app.pipeline.llm_reviewer import LLMReviewer
from app.pipeline.review_pipeline import ReviewPipeline
from app.pipeline.knowledge_retriever import StaticKnowledge, NullRetriever
from app.models.evidence import Evidence
from app.models.finding import Finding
from app.models.location import CodeLocation


# ---- Mock LLM 函数 ----


def _mock_valid(sys_prompt: str, user_prompt: str) -> str:
    return json.dumps({
        "findings": [{
            "location": {"file": "test.py", "start_line": 10},
            "severity": "medium",
            "reason": "login() 缺少输入校验",
            "suggestion": "添加参数校验",
            "confidence": 0.85,
            "evidence_ids": [],
        }],
    })


def _mock_empty(sys_prompt: str, user_prompt: str) -> str:
    return json.dumps({"findings": []})


def _mock_invalid_json(sys_prompt: str, user_prompt: str) -> str:
    return "这不是 JSON"


def _mock_missing_fields(sys_prompt: str, user_prompt: str) -> str:
    return json.dumps({"findings": [{"severity": "high"}]})


def _mock_no_findings_key(sys_prompt: str, user_prompt: str) -> str:
    return json.dumps({"something": "else"})


def _mock_fails_then_ok(sys_prompt: str, user_prompt: str) -> str:
    return json.dumps({
        "findings": [{
            "location": {"file": "x.py", "start_line": 1},
            "severity": "low",
            "reason": "something",
            "suggestion": "fix it",
            "confidence": 0.7,
            "evidence_ids": [],
        }],
    })


class TestLLMToolExtraBody:
    """llm_tool.chat 必须支持 extra_body 透传（如关闭推理模型 thinking）。"""

    def test_chat_passes_extra_body(self, monkeypatch):
        from app.tools import llm_tool

        captured = {}

        class FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                msg = type("Msg", (), {"content": "ok"})
                choice = type("Choice", (), {"message": msg})
                return type("Resp", (), {"choices": [choice]})

        fake_client = type("Client", (), {
            "chat": type("Chat", (), {"completions": FakeCompletions()}),
        })
        monkeypatch.setattr(llm_tool, "get_client", lambda timeout=None: fake_client)

        out = llm_tool.chat("hi", extra_body={"thinking": {"type": "disabled"}})

        assert out == "ok"
        assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


class TestLLMReviewer:
    def test_valid_output(self):
        reviewer = LLMReviewer(call_llm=_mock_valid)
        findings, evidence = reviewer.review(
            "test.py", "+def login(): pass", [], [], [],
        )
        assert len(findings) == 1
        assert findings[0].severity == "medium"
        assert findings[0].rule_id == "LLM_SEMANTIC"

    def test_empty_findings(self):
        reviewer = LLMReviewer(call_llm=_mock_empty)
        findings, evidence = reviewer.review("test.py", "+x=1", [], [], [])
        assert findings == []

    def test_invalid_json_produces_evidence(self):
        reviewer = LLMReviewer(call_llm=_mock_invalid_json)
        findings, evidence = reviewer.review("test.py", "+x=1", [], [], [])
        assert findings == []
        assert len(evidence) == 1
        assert "parse failure" in evidence[0].snippet

    def test_missing_fields_rejected(self):
        reviewer = LLMReviewer(call_llm=_mock_missing_fields)
        findings, evidence = reviewer.review("test.py", "+x=1", [], [], [])
        assert findings == []
        assert any("rejected" in e.snippet for e in evidence)

    def test_low_confidence_downgraded(self):
        def low_conf(sys, usr):
            return json.dumps({"findings": [{
                "location": {"file": "x.py", "start_line": 1},
                "severity": "high", "reason": "?",
                "suggestion": "?", "confidence": 0.3,
                "evidence_ids": [],
            }]})
        reviewer = LLMReviewer(call_llm=low_conf)
        findings, _ = reviewer.review("x.py", "+x=1", [], [], [])
        assert len(findings) == 1
        assert findings[0].severity == "info"  # 降级

    def test_NullRetriever_returns_empty(self):
        nr = NullRetriever()
        assert nr.retrieve("anything") == []

    def test_StaticKnowledge_matches_security(self):
        sk = StaticKnowledge()
        results = sk.retrieve("eval sql injection", top_k=3)
        assert len(results) > 0
        for r in results:
            assert "content" in r
            assert "source" in r
            assert "license" in r


class TestPipelineWithMockLLM:
    """验证 LLM 审查接入 Pipeline 后不破坏确定性结果。"""

    def test_pipeline_with_mock_llm_still_returns_static(self, fixed_git_diff):
        from app.pipeline.llm_reviewer import LLMReviewer

        reviewer = LLMReviewer(call_llm=_mock_valid)
        pipeline = ReviewPipeline(llm_reviewer=reviewer)
        output = pipeline.run(".", "HEAD~2", "HEAD")
        # 确定性结果不受影响
        assert len(output.change_set.get("files", [])) > 0
        assert len(output.markdown) > 0
        # 固定 change_set 含 Python 文件，trace 必须包含 LLM 审查步骤
        has_llm_trace = any("llm_review" in t.step for t in output.trace)
        assert has_llm_trace

    def test_pipeline_static_results_preserved_on_llm_failure(self, fixed_git_diff):
        """M3 验收：LLM 失败时静态结果不受影响。"""
        reviewer = LLMReviewer(call_llm=_mock_invalid_json)
        pipeline = ReviewPipeline(llm_reviewer=reviewer)
        output = pipeline.run(".", "HEAD~2", "HEAD")
        # 即使 LLM 失败（mock_invalid_json），git 证据依然产出
        assert len(output.evidence) > 0

    def test_pipeline_passes_actual_patch_to_llm(self, fixed_git_diff):
        prompts = []

        def capture_prompt(system_prompt, user_prompt):
            prompts.append(user_prompt)
            return json.dumps({"findings": []})

        output = ReviewPipeline(llm_reviewer=LLMReviewer(call_llm=capture_prompt)).run(
            ".", "HEAD~2", "HEAD"
        )
        assert output.change_set.get("files")
        assert prompts
        assert any("diff --git" in prompt for prompt in prompts)

    def test_llm_review_covers_non_python_code_and_config(self, monkeypatch):
        """JS/JSON 等代码/配置文件也必须进 LLM 语义审查（评测中 index.js 的 SQL 注入曾零检出）。"""
        from app.tools.git_tool import GitTool
        from app.tools.contract import ToolRequest, ToolResult

        changeset = {"files": [
            {"path": "index.js", "change_type": "added", "added_lines": 12, "deleted_lines": 0, "hunks": []},
            {"path": "settings.json", "change_type": "added", "added_lines": 5, "deleted_lines": 0, "hunks": []},
            {"path": "package-lock.json", "change_type": "added", "added_lines": 900, "deleted_lines": 0, "hunks": []},
        ]}
        diff = (
            "diff --git a/index.js b/index.js\n+const q = \"SELECT * FROM u WHERE id=\" + req.params.id;\n"
            "diff --git a/settings.json b/settings.json\n+{\"secret_key\": \"prod-secret\"}\n"
            "diff --git a/package-lock.json b/package-lock.json\n+{}\n"
        )

        def mock_git(self, request: ToolRequest) -> ToolResult:
            return ToolResult(tool="git", status="success",
                              artifacts={"change_set": changeset, "unified_diff": diff})

        monkeypatch.setattr(GitTool, "execute", mock_git)

        reviewed = []

        class CaptureReviewer:
            def review(self, file_path, diff_snippet, symbols, static_findings, existing_evidence):
                reviewed.append(file_path)
                return [], []

        ReviewPipeline(llm_reviewer=CaptureReviewer()).run(".", "HEAD~1", "HEAD")

        assert "index.js" in reviewed
        assert "settings.json" in reviewed
        # 锁文件是机器生成的海量 diff，不值得花 LLM 预算
        assert "package-lock.json" not in reviewed
