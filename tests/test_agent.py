"""V1.1 Investigation Agent 测试 — mock LLM，不上网。"""

import os

from app.agent.investigator import (
    InvestigationAgent, InvestigationResult, _classify,
)


class TestClassify:
    """问题类型识别。"""

    def test_locate_chinese(self):
        assert _classify("login 函数在哪里定义的？") == "locate"

    def test_locate_english(self):
        assert _classify("where is the login function defined?") == "locate"

    def test_explain(self):
        assert _classify("这个函数做什么用的？") == "explain"

    def test_trace(self):
        assert _classify("谁调用了 handle_request？") == "trace"

    def test_grep(self):
        assert _classify("列出所有使用 subprocess 的地方") == "grep"

    def test_default_locate(self):
        assert _classify("随便什么看不懂的问题") == "locate"


class TestExtractKeywords:
    """关键词提取。"""

    def test_quoted(self):
        assert InvestigationAgent._extract_keywords('where is "login_handler" defined?') == ["login_handler"]

    def test_camelcase(self):
        keywords = InvestigationAgent._extract_keywords("where is UserService defined?")
        assert "UserService" in keywords

    def test_snake_case(self):
        keywords = InvestigationAgent._extract_keywords("where is handle_request defined?")
        assert "handle_request" in keywords

    def test_fallback_words(self):
        keywords = InvestigationAgent._extract_keywords("where is the login?")
        assert len(keywords) > 0
        assert "where" not in keywords  # 停用词被过滤

    def test_no_keywords(self):
        keywords = InvestigationAgent._extract_keywords("在哪里？干什么？")
        assert keywords == []


class TestInvestigateWithMockLLM:
    """用 mock LLM 测试完整调查流程。"""

    def test_investigate_finds_results(self):
        """在自身仓库中搜索已知存在的符号，验证能返回答案。"""
        mock_responses = ["找到了，在 app/cli.py 中定义了 main 函数。"]

        def mock_chat(prompt, system="", temperature=0.3, max_tokens=2000):
            return mock_responses.pop(0)

        agent = InvestigationAgent(call_llm=mock_chat)
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        result = agent.investigate(repo, "main 函数在哪里？")

        assert isinstance(result, InvestigationResult)
        assert len(result.answer) > 0
        assert "main" in result.answer.lower() or "cli" in result.answer.lower()
        assert len(result.trace) > 0
        assert result.duration_ms > 0

    def test_investigate_no_results(self):
        """搜索不存在的符号时返回无结果提示。"""
        import uuid
        agent = InvestigationAgent(call_llm=lambda *a, **kw: "mock")
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        keyword = uuid.uuid4().hex
        result = agent.investigate(repo, f'"{keyword}"')

        assert result.answer == "" or "未找到" in result.answer or keyword in result.answer
        assert result.duration_ms > 0

    def test_llm_fallback_on_error(self):
        """LLM 调用失败时用 grep 原始结果兜底。"""
        def crashing_llm(*a, **kw):
            raise RuntimeError("LLM 服务不可用")

        agent = InvestigationAgent(call_llm=crashing_llm)
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        result = agent.investigate(repo, "InvestigationAgent")

        assert "LLM 不可用" in result.answer or "InvestigationAgent" in result.answer
        assert any("llm_fallback" in t for t in result.trace)

    def test_empty_keywords(self):
        """无关键词时不做 grep，直接返回提示。"""
        agent = InvestigationAgent(call_llm=lambda *a, **kw: "unused")
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        # 用纯中文无标识符的问题触发关键词空
        result = agent.investigate(repo, "在哪里？")

        assert "关键词" in result.answer or len(result.files_visited) == 0
        assert result.duration_ms > 0

    def test_result_to_dict(self):
        """InvestigationResult.to_dict() 序列化正确。"""
        result = InvestigationResult(
            question="测试问题",
            answer="测试答案",
            files_visited=["a.py", "b.py"],
            findings=["发现1"],
            trace=["question_type=locate"],
            duration_ms=123.4,
        )
        d = result.to_dict()
        assert d["question"] == "测试问题"
        assert d["answer"] == "测试答案"
        assert d["files_visited"] == ["a.py", "b.py"]
        assert d["findings"] == ["发现1"]
        assert d["trace"] == ["question_type=locate"]
        assert d["duration_ms"] == 123.4

    def test_evidence_collected(self):
        """调查成功时收集 Evidence 并附带文件位置。"""
        def mock_chat(prompt, system="", temperature=0.3, max_tokens=2000):
            return "在 app/agent/investigator.py 中定义了 InvestigationAgent 类。"

        agent = InvestigationAgent(call_llm=mock_chat)
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        result = agent.investigate(repo, "InvestigationAgent")

        assert len(result.files_visited) > 0
        assert len(result.evidence) > 0
        # 检查 evidence 结构
        ev = result.evidence[0]
        assert ev.kind == "code"
        assert ev.source == "git_grep"
        assert ev.location is not None
        # 至少有一条 evidence 的文件路径包含 investigator 或 agent
        ev_files = [e.location.file for e in result.evidence if e.location]
        assert any("investigator" in f or "agent" in f for f in ev_files)

    def test_files_visited_capped(self):
        """文件列表不超过 20 个。"""
        def mock_chat(prompt, system="", temperature=0.3, max_tokens=2000):
            return "找到了。"

        agent = InvestigationAgent(call_llm=mock_chat)
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        result = agent.investigate(repo, "def")

        assert len(result.files_visited) <= 20
