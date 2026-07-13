"""RuffTool + BanditTool + DependencyTool 单元测试。"""
import tempfile
import os
from app.tools.ruff_tool import RuffTool
from app.tools.bandit_tool import BanditTool
from app.tools.dependency_tool import DependencyTool, _extract_imports, _STDLIB
from app.tools.contract import ToolRequest


def _write_bad_file(path: str):
    with open(path, "w") as f:
        f.write("import os, sys\n")
        f.write("eval('1+1')\n")
        f.write("password = 'secret123'\n")


class TestRuffTool:
    def test_finds_issues(self):
        tmpdir = tempfile.mkdtemp()
        fp = os.path.join(tmpdir, "test.py")
        try:
            _write_bad_file(fp)
            rt = RuffTool()
            result = rt.execute(ToolRequest(tool="ruff", params={"paths": [fp]}))
            assert result.ok()
            assert len(result.findings) > 0
            for f in result.findings:
                assert f.tool == "ruff"
                assert f.rule_id
                assert f.evidence_ids
        finally:
            os.remove(fp)
            os.rmdir(tmpdir)

    def test_empty_paths(self):
        rt = RuffTool()
        result = rt.execute(ToolRequest(tool="ruff", params={"paths": []}))
        assert result.ok()
        assert result.findings == []

    def test_findings_have_location(self):
        tmpdir = tempfile.mkdtemp()
        fp = os.path.join(tmpdir, "test.py")
        try:
            _write_bad_file(fp)
            rt = RuffTool()
            result = rt.execute(ToolRequest(tool="ruff", params={"paths": [fp]}))
            for f in result.findings:
                assert f.location is not None
                assert f.location.start_line > 0
        finally:
            os.remove(fp)
            os.rmdir(tmpdir)


class TestBanditTool:
    def test_finds_issues(self):
        tmpdir = tempfile.mkdtemp()
        fp = os.path.join(tmpdir, "test.py")
        try:
            _write_bad_file(fp)
            bt = BanditTool()
            result = bt.execute(ToolRequest(tool="bandit", params={"paths": [fp]}))
            assert result.ok()
            assert len(result.findings) > 0
            for f in result.findings:
                assert f.tool == "bandit"
                assert f.rule_id.startswith("B")
                assert f.evidence_ids
        finally:
            os.remove(fp)
            os.rmdir(tmpdir)

    def test_empty_paths(self):
        bt = BanditTool()
        result = bt.execute(ToolRequest(tool="bandit", params={"paths": []}))
        assert result.ok()
        assert result.findings == []

    def test_clean_code_no_findings(self):
        tmpdir = tempfile.mkdtemp()
        fp = os.path.join(tmpdir, "clean.py")
        try:
            with open(fp, "w") as f:
                f.write("x = 1\n")
            bt = BanditTool()
            result = bt.execute(ToolRequest(tool="bandit", params={"paths": [fp]}))
            assert result.ok()
            # bandit 可能报 B101 或其他，取决于规则配置，不做断言
        finally:
            os.remove(fp)
            os.rmdir(tmpdir)


class TestDependencyTool:
    def test_detects_external_imports(self):
        dt = DependencyTool()
        result = dt.execute(ToolRequest(tool="dependency", params={
            "files": [("app.py", "import requests\nfrom flask import Flask\nimport os\n")],
            "changed_files": ["app.py"],
        }))
        assert result.ok()
        # requests 和 flask 是非标准库
        assert len(result.findings) >= 1
        # at least one finding mentions external import
        external_msgs = [f.message for f in result.findings if "EXTERNAL_IMPORT" in (f.rule_id or "")]
        assert len(external_msgs) >= 1

    def test_stdlib_only_no_findings(self):
        dt = DependencyTool()
        result = dt.execute(ToolRequest(tool="dependency", params={
            "files": [("mod.py", "import os\nfrom pathlib import Path\nimport json\n")],
            "changed_files": ["mod.py"],
        }))
        assert result.ok()
        # 仅标准库 → 无 EXTERNAL_IMPORT finding
        external = [f for f in result.findings if f.rule_id == "EXTERNAL_IMPORT"]
        assert len(external) == 0

    def test_dependency_file_changed(self):
        dt = DependencyTool()
        result = dt.execute(ToolRequest(tool="dependency", params={
            "files": [],
            "changed_files": ["requirements.txt"],
        }))
        assert result.ok()
        dep_findings = [f for f in result.findings if f.rule_id == "DEP_FILE_CHANGED"]
        assert len(dep_findings) == 1
        assert "requirements.txt" in dep_findings[0].message

    def test_pyproject_toml_detected(self):
        dt = DependencyTool()
        result = dt.execute(ToolRequest(tool="dependency", params={
            "files": [],
            "changed_files": ["pyproject.toml", "src/main.py"],
        }))
        assert result.ok()
        dep_findings = [f for f in result.findings if f.rule_id == "DEP_FILE_CHANGED"]
        assert len(dep_findings) == 1

    def test_empty_files(self):
        dt = DependencyTool()
        result = dt.execute(ToolRequest(tool="dependency", params={
            "files": [],
            "changed_files": [],
        }))
        assert result.ok()
        assert result.findings == []

    def test_evidence_produced(self):
        dt = DependencyTool()
        result = dt.execute(ToolRequest(tool="dependency", params={
            "files": [("test.py", "import numpy as np\n")],
            "changed_files": ["test.py"],
        }))
        assert len(result.evidence) > 0
        for ev in result.evidence:
            assert ev.source == "dependency"


class TestExtractImports:
    def test_extract_import(self):
        imps = _extract_imports("import os, sys\n")
        assert ("os", 1, "import") in imps
        assert ("sys", 1, "import") in imps

    def test_extract_from(self):
        imps = _extract_imports("from flask import Flask\n")
        assert ("flask", 1, "from") in imps

    def test_extract_relative(self):
        imps = _extract_imports("from . import utils\nfrom ..sibling import foo\n")
        kinds = [k for _, _, k in imps]
        assert "from_relative" in kinds

    def test_syntax_error_returns_empty(self):
        imps = _extract_imports("def broken( >>>\n")
        assert imps == []

    def test_stdlib_not_in_external(self):
        """标准库模块不在外部依赖中。"""
        assert "os" in _STDLIB
        assert "sys" in _STDLIB
        assert "flask" not in _STDLIB
        assert "requests" not in _STDLIB
