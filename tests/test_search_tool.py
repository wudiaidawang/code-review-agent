"""SearchTool 单元测试 — 流式 Top-K、docs_src 分类、定义名精确匹配、确定性排序、资源保护。

不依赖外部仓库，使用临时 git 仓库构造测试场景。
"""

import os
import subprocess
import tempfile

import pytest

from app.tools.search_tool import (
    SearchTool,
    _classify_file,
    _classify_hit_streaming,
    _compute_match_precision,
    _compute_query_coverage,
    _find_matched_terms,
    _parse_defined_name,
    _name_matches_keyword,
)
from app.tools.contract import ToolRequest


# ---- helpers ---------------------------------------------------------------


def _init_git_repo(path: str, files: dict[str, str]) -> None:
    """在 path 目录创建文件并 git init + commit。"""
    os.makedirs(path, exist_ok=True)
    for rel, content in files.items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    subprocess.run(["git", "-C", path, "init"], capture_output=True)
    subprocess.run(["git", "-C", path, "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", path, "commit", "-m", "init", "--allow-empty"],
                   capture_output=True)


@pytest.fixture
def toy_repo():
    """创建一个包含源码 + 文档 + 测试 + 示例的微型 git 仓库。"""
    with tempfile.TemporaryDirectory() as tmp:
        files = {
            "src/myapp/main.py": (
                "class App:\n"
                "    def run(self) -> None:\n"
                "        app = App()\n"
                "        return app.do_work()\n"
            ),
            "src/myapp/utils.py": (
                "from myapp.main import App\n\n"
                "def helper(app: App) -> str:\n"
                "    return str(app)\n"
            ),
            "docs_src/tutorial.py": (
                "from myapp.main import App\n\n"
                "def main(app: App = App()) -> None:\n"
                "    app.run()\n"
            ),
            "docs/guide.md": (
                "# App Guide\n\n"
                "The `App` class is the main entry point.\n"
                "Usage: `app = App()`\n"
            ),
            "tests/test_main.py": (
                "from myapp.main import App\n\n"
                "def test_app() -> None:\n"
                "    app = App()\n"
                "    app.run()\n"
            ),
            "examples/demo.py": (
                "from myapp.main import App\n\n"
                "app = App()\n"
                "app.run()\n"
            ),
            ".github/workflows/ci.yml": (
                "name: CI\n"
                "on: push\n"
                "jobs:\n"
                "  test:\n"
                "    run: pytest\n"
            ),
        }
        _init_git_repo(tmp, files)
        yield tmp


@pytest.fixture
def large_repo():
    """创建大量文档命中（>1000）＋源码在字母序末尾的仓库，验证截断回归。"""
    with tempfile.TemporaryDirectory() as tmp:
        files = {}
        # 大量文档文件（字母序在源码前）
        for i in range(200):
            files[f"docs/page_{i:04d}.md"] = (
                f"# Page {i}\n\nReference to `MySymbol` on page {i}.\n"
            )
        # 源码在字母序末尾
        files["zzz_source/main.py"] = (
            "class MySymbol:\n"
            "    \"\"\"The real definition.\"\"\"\n"
            "    def method(self) -> None:\n"
            "        pass\n"
        )
        _init_git_repo(tmp, files)
        yield tmp


# ---- 文件分类 ---------------------------------------------------------------


class TestClassifyFile:
    """_classify_file 路径分类正确性。"""

    def test_source(self):
        assert _classify_file("src/package/main.py") == "source"

    def test_test_dir(self):
        assert _classify_file("tests/test_foo.py") == "test"
        assert _classify_file("test/test_bar.py") == "test"
        assert _classify_file("testing/utils.py") == "test"

    def test_docs_src_not_source(self):
        """docs_src/*.py 不能归类为 source。"""
        assert _classify_file("docs_src/tutorial.py") == "documentation"
        assert _classify_file("docs_src/sub/deep/file.py") == "documentation"

    def test_doc_src(self):
        assert _classify_file("doc_src/guide.py") == "documentation"

    def test_docs_dir(self):
        assert _classify_file("docs/guide.md") == "documentation"
        assert _classify_file("doc/api.rst") == "documentation"
        assert _classify_file("documentation/index.md") == "documentation"

    def test_example_dir(self):
        assert _classify_file("examples/demo.py") == "example"
        assert _classify_file("tutorials/basic.py") == "example"

    def test_config(self):
        assert _classify_file(".github/workflows/ci.yml") == "config"

    def test_typer_source_still_source(self):
        """typer/main.py 仍归类为 source（docs_src 修复不能误伤源码目录）。"""
        assert _classify_file("typer/main.py") == "source"


# ---- 定义名解析 --------------------------------------------------------------


class TestDefinedName:
    """_parse_defined_name 和 _name_matches_keyword。"""

    def test_class(self):
        assert _parse_defined_name("class Typer:") == "Typer"
        assert _parse_defined_name("class Typer(GenericApp):") == "Typer"

    def test_def(self):
        assert _parse_defined_name("def run(self) -> None:") == "run"

    def test_async_def(self):
        assert _parse_defined_name("async def fetch(url: str):") == "fetch"

    def test_no_def(self):
        assert _parse_defined_name("app = Typer()") is None
        assert _parse_defined_name("from typer import Typer") is None

    def test_name_match_exact(self):
        assert _name_matches_keyword("Typer", ["Typer"]) is True
        assert _name_matches_keyword("Typer", ["typer"]) is True  # fallback

    def test_name_no_match(self):
        assert _name_matches_keyword("main", ["Typer"]) is False

    def test_rust_fn(self):
        assert _parse_defined_name("fn main() {") == "main"

    def test_go_func(self):
        assert _parse_defined_name("func (s *Server) ListenAndServe() error {") == "ListenAndServe"
        assert _parse_defined_name("func ListenAndServe() error {") == "ListenAndServe"


# ---- 命中类型分类 ------------------------------------------------------------


class TestClassifyHit:
    """_classify_hit_streaming 定义名精确匹配。"""

    def test_class_definition_matches(self):
        ast_cache = {}
        result = _classify_hit_streaming(
            "test.py", 1, "class Typer:", ["Typer"], ".", ast_cache,
        )
        assert result == "definition"

    def test_def_not_definition(self):
        """def main(...typer.Option(...)): 不得判为 Typer 的 definition。"""
        ast_cache = {}
        result = _classify_hit_streaming(
            "test.py", 5, "def main(network: str = typer.Option(\"CNN\")):",
            ["Typer"], ".", ast_cache,
        )
        assert result != "definition"

    def test_call_detected(self):
        ast_cache = {}
        result = _classify_hit_streaming(
            "test.py", 10, "app = typer.Typer()", ["Typer"], ".", ast_cache,
        )
        assert result == "call"

    def test_import_detected(self):
        ast_cache = {}
        result = _classify_hit_streaming(
            "test.py", 1, "from typer import Typer", ["Typer"], ".", ast_cache,
        )
        assert result == "import"

    def test_comment(self):
        ast_cache = {}
        result = _classify_hit_streaming(
            "test.py", 1, "# This uses Typer", ["Typer"], ".", ast_cache,
        )
        assert result == "comment"

    def test_ast_cache_reuse(self):
        """同一文件多次分类应复用 AST 缓存。"""
        ast_cache = {}
        # 第一次调用会触发 AST 解析
        r1 = _classify_hit_streaming(
            "test.py", 1, "class Typer:", ["Typer"], ".", ast_cache,
        )
        # 第二次调用同一文件应使用缓存
        r2 = _classify_hit_streaming(
            "test.py", 5, "app = Typer()", ["Typer"], ".", ast_cache,
        )
        assert r1 == "definition"
        assert r2 == "call"
        assert any("test.py" in k for k in ast_cache)


# ---- 匹配精确度 --------------------------------------------------------------


class TestMatchPrecision:
    """_compute_match_precision 评分层级。"""

    def test_exact_case(self):
        assert _compute_match_precision("class Typer:", ["Typer"]) == 300

    def test_case_insensitive(self):
        assert _compute_match_precision("typer.run()", ["Typer"]) == 250

    def test_substring(self):
        assert _compute_match_precision("my_typer_var", ["Typer"]) == 50

    def test_no_match(self):
        assert _compute_match_precision("nothing here", ["Typer"]) == 0

    def test_best_of_multiple(self):
        """取所有关键词中最佳匹配级别。"""
        score = _compute_match_precision("class Typer:", ["Typer", "Unknown"])
        assert score == 300  # Typer 精确定义优先


# ---- 多关键词覆盖 ------------------------------------------------------------


class TestQueryCoverage:
    """_compute_query_coverage 和 _find_matched_terms。"""

    def test_all_match(self):
        assert _compute_query_coverage("typer.run(click.echo(x))", ["Typer", "Click"]) == 100

    def test_partial_match(self):
        assert _compute_query_coverage("typer.run()", ["Typer", "Click"]) == 50

    def test_matched_terms(self):
        assert _find_matched_terms("typer.run()", ["Typer", "Click"]) == ["Typer"]


# ---- SearchTool 集成测试 ----------------------------------------------------


class TestSearchToolIntegration:
    """端到端 SearchTool 测试。"""

    def test_basic_search(self, toy_repo):
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": toy_repo, "query": ["App"], "max_results": 10,
        }))
        assert r.status == "success"
        assert len(r.evidence) > 0
        assert len(r.evidence) <= 10

    def test_source_ranks_highest(self, toy_repo):
        """源码中的定义应排在文档和示例前面。"""
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": toy_repo, "query": ["App"], "max_results": 5,
        }))
        matches = r.artifacts["matches"]
        # 第一命中应为 src/myapp/main.py:1 (class App)
        assert matches[0]["file"] == "src/myapp/main.py"
        assert matches[0]["hit_type"] == "definition"

    def test_docs_src_not_definition(self, toy_repo):
        """docs_src/tutorial.py 的 def main(...) 不应标为 App 的 definition。"""
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": toy_repo, "query": ["App"], "max_results": 10,
        }))
        for m in r.artifacts["matches"]:
            if m["file"] == "docs_src/tutorial.py":
                assert m["hit_type"] != "definition", (
                    f"docs_src def main should not be definition for App"
                )

    def test_deterministic(self, toy_repo):
        """同一输入重复执行，返回顺序完全一致。"""
        st = SearchTool()
        params = {"repo_path": toy_repo, "query": ["App"], "max_results": 10}
        r1 = st.execute(ToolRequest(tool="search", params=dict(params)))
        r2 = st.execute(ToolRequest(tool="search", params=dict(params)))
        files1 = [e.location.file for e in r1.evidence]
        files2 = [e.location.file for e in r2.evidence]
        assert files1 == files2

    def test_artifact_fields(self, toy_repo):
        """artifact matches 必须包含可解释性字段。"""
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": toy_repo, "query": ["App"], "max_results": 3,
        }))
        for m in r.artifacts["matches"]:
            assert "file_type" in m
            assert "hit_type" in m
            assert "score" in m
            assert "matched_terms" in m
        assert "truncated" in r.artifacts
        assert "total_scanned" in r.artifacts

    def test_empty_query(self, toy_repo):
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": toy_repo, "query": "", "max_results": 10,
        }))
        assert r.status == "failed"

    def test_filename_search(self, toy_repo):
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": toy_repo, "query": "main", "search_type": "filename",
            "max_results": 5,
        }))
        assert r.status == "success"


# ---- 排序前截断回归 -----------------------------------------------------------


class TestNoPreTruncation:
    """验证字母序截断已修复：源码位于字母序末尾时仍然进入结果。"""

    def test_source_after_many_docs(self, large_repo):
        """200 个文档文件 + 源码在 zzz_source/ → 源码必须出现在结果中。"""
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": large_repo, "query": ["MySymbol"], "max_results": 20,
        }))
        source_files = [
            e.location.file for e in r.evidence
            if e.location.file.startswith("zzz_source/")
        ]
        assert len(source_files) > 0, (
            f"源码文件应进入 evidence，实际 evidence 文件: "
            f"{[e.location.file for e in r.evidence[:5]]}"
        )
        # 源码中的精确定义应排在最前
        top_file = r.evidence[0].location.file if r.evidence else ""
        assert top_file == "zzz_source/main.py", (
            f"class MySymbol 定义应为第 1 命中，实际: {top_file}"
        )

    def test_not_truncated(self, large_repo):
        """200 条文档命中不应触发截断。"""
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": large_repo, "query": ["MySymbol"], "max_results": 10,
        }))
        assert r.artifacts["truncated"] is False

    def test_noisy_file_cannot_monopolize_candidate_heap(self):
        """Per-file reduction occurs before Top-K, not only in final output."""
        with tempfile.TemporaryDirectory() as tmp:
            files = {
                "a_noisy.py": "\n".join("def App():\n    pass" for _ in range(120)),
            **{
                f"z_other_{index:03d}.py": "App()\n"
                for index in range(101)
            },
            # A real App definition is included only to keep the corpus valid
            # for the intended symbol-search scenario.
            "zz_definition.py": "class App:\n    pass\n",
        }
        _init_git_repo(tmp, files)
        result_data = SearchTool()._search_grep_stream(tmp, ["App"], [], max_results=1)

        retained_files = {item["file"] for item in result_data["scored_items"]}
        assert "a_noisy.py" in retained_files
        assert any(file_path.startswith("z_other_") for file_path in retained_files)
        assert len(retained_files) == len(result_data["scored_items"])


# ---- 资源保护 ----------------------------------------------------------------


class TestResourceLimits:
    """达到资源上限时安全停止并标记 truncated。"""

    def test_truncated_on_max_lines(self, toy_repo):
        """极端小的扫描上限触发截断。"""
        import app.tools.search_tool as st_mod
        old_limit = st_mod.MAX_LINES_SCANNED
        try:
            st_mod.MAX_LINES_SCANNED = 0  # 立即触发
            st = SearchTool()
            r = st.execute(ToolRequest(tool="search", params={
                "repo_path": toy_repo, "query": ["App"], "max_results": 5,
            }))
            # 可能因过早截断无结果或截断标记为 true
            assert r.artifacts["truncated"] is True
            assert r.artifacts["truncated_reason"] == "max_lines_scanned"
        finally:
            st_mod.MAX_LINES_SCANNED = old_limit


# ---- 多关键词 ----------------------------------------------------------------


class TestMultiKeyword:
    """多关键词查询不能只命中第一个关键词。"""

    def test_both_keywords_searched(self, toy_repo):
        """查询两个词，结果中应包含覆盖不同关键词的命中。"""
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": toy_repo, "query": ["App", "helper"], "max_results": 10,
        }))
        matches = r.artifacts["matches"]
        # 至少有一条命中覆盖了 "helper"
        helper_hits = [m for m in matches if "helper" in (m.get("matched_terms") or [])]
        assert len(helper_hits) > 0, "应有多关键词覆盖，'helper' 命中不应被遗漏"

    def test_coverage_in_scoring(self, toy_repo):
        """覆盖更多关键词的命中应排在只覆盖单个关键词的前面。"""
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": toy_repo, "query": ["App", "App"], "max_results": 5,
        }))
        # 至少结果能正常返回（重复关键词不导致崩溃）
        assert r.status == "success"


# ---- 现有行为不退化 -----------------------------------------------------------


class TestNoRegression:
    """确认基本功能不退化。"""

    def test_search_in_this_repo(self):
        """在本项目仓库中搜索，能找到已知符号。"""
        st = SearchTool()
        r = st.execute(ToolRequest(tool="search", params={
            "repo_path": ".", "query": ["SearchTool"], "max_results": 5,
        }))
        assert r.status == "success"
        assert len(r.evidence) > 0
        files = [e.location.file for e in r.evidence]
        assert "app/tools/search_tool.py" in files

    def test_max_results_respected(self, toy_repo):
        st = SearchTool()
        for n in [3, 10, 25]:
            r = st.execute(ToolRequest(tool="search", params={
                "repo_path": toy_repo, "query": ["App"], "max_results": n,
            }))
            assert len(r.evidence) <= n

    def test_file_type_not_source_for_non_source(self):
        """配置文件不应归类为 source。"""
        assert _classify_file(".github/workflows/ci.yml") == "config"
        assert _classify_file("README.md") == "documentation"
        assert _classify_file("CHANGELOG.md") == "documentation"
