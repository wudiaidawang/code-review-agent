"""ASTTool 单元测试 — 纯离线，用项目自身文件。"""
from app.tools.ast_tool import ASTTool
from app.tools.contract import ToolRequest


SAMPLE = [
    ("sample.py", "import os\n\ndef hello():\n    print(os.getcwd())\n"),
    ("broken.py", "def foo(:\n"),  # 语法错误
]


class TestASTTool:
    def test_extracts_symbols(self):
        at = ASTTool()
        result = at.execute(ToolRequest(tool="python_ast", params={"files": SAMPLE}))
        assert result.ok()
        symbols = {s["name"] for s in result.artifacts["symbol_index"]}
        assert symbols >= {"os", "hello"}

    def test_syntax_error_does_not_crash(self):
        at = ASTTool()
        result = at.execute(ToolRequest(tool="python_ast", params={"files": SAMPLE}))
        assert result.ok()
        # broken.py 应有 parse error evidence
        parse_errors = [e for e in result.evidence if "parse error" in e.snippet]
        assert len(parse_errors) == 1

    def test_evidence_count_matches_files(self):
        at = ASTTool()
        result = at.execute(ToolRequest(tool="python_ast", params={"files": SAMPLE}))
        assert len(result.evidence) == 2

    def test_empty_files(self):
        at = ASTTool()
        result = at.execute(ToolRequest(tool="python_ast", params={"files": []}))
        assert result.ok()
        assert result.artifacts["symbol_index"] == []
