"""SymbolResolverV2 单元测试。"""

import os
import tempfile
from pathlib import Path

import pytest

from app.agent.symbol_resolver import (
    SymbolResolverV2,
    ResolvedSymbol,
    resolved_to_evidence,
)


def _write(repo: Path, rel: str, content: str) -> Path:
    """在 repo 中写文件，自动创建父目录。"""
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ═══════════════════════════════════════════════════════════════════
# 简单符号
# ═══════════════════════════════════════════════════════════════════


class TestDirectSearch:
    def test_simple_class(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "app.py", "class Widget:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("Widget")
            assert result is not None
            assert result.kind == "class"
            assert "Widget" in result.canonical_name

    def test_simple_function(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "util.py", "def run():\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("run")
            assert result is not None
            assert result.kind == "function"

    def test_simple_async_function(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "async_util.py", "async def fetch():\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("fetch")
            assert result is not None
            assert result.kind == "async_function"

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "app.py", "class Widget:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            assert r.resolve("NonExistent") is None


# ═══════════════════════════════════════════════════════════════════
# 包导出
# ═══════════════════════════════════════════════════════════════════


class TestPackageExport:
    def test_basic_init_re_export(self):
        """httpx.Client → __init__.py re-export → _client.py::class Client"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "httpx/__init__.py", "from ._client import Client\n")
            _write(repo, "httpx/_client.py",
                   "class BaseClient:\n    pass\n\n\nclass Client(BaseClient):\n    def __init__(self):\n        pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("httpx.Client")
            assert result is not None
            assert result.kind == "class"
            assert "_client" in result.file
            assert "Client" in result.canonical_name

    def test_direct_init_definition(self):
        """符号直接定义在 __init__.py 中。"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "mylib/__init__.py", "class MyClass:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("mylib.MyClass")
            assert result is not None
            assert result.kind == "class"
            assert "__init__" in result.file

    def test_two_level_package(self):
        """httpx._client.Client → 模块 httpx._client 直接包含 class Client"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "httpx/_client.py",
                   "class Client:\n    def send(self):\n        pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("httpx._client.Client")
            assert result is not None
            assert result.kind == "class"
            assert "_client.py" in result.file

    def test_export_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "httpx/__init__.py", "from ._client import Client\n")
            _write(repo, "httpx/_client.py", "class BaseClient:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            # Client 不在 _client.py（只有 BaseClient）
            result = r.resolve("httpx.Client")
            # import 链追踪不到定义 → 应返回 None
            assert result is None


# ═══════════════════════════════════════════════════════════════════
# 类成员
# ═══════════════════════════════════════════════════════════════════


class TestClassMember:
    def test_typer_typer_command(self):
        """typer.Typer.command → class Typer 体内的 def command"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "typer/__init__.py", "from .main import Typer\n")
            _write(repo, "typer/main.py",
                   "class Typer:\n    def __init__(self):\n        pass\n\n    def command(self, func):\n        return func\n\n    def add_typer(self, sub):\n        pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("typer.Typer.command")
            assert result is not None
            assert result.kind in ("function", "async_function")
            assert result.owner == "Typer"

    def test_class_member_in_non_init_file(self):
        """类成员在包的非 __init__ 文件中"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "mylib/__init__.py", "")
            _write(repo, "mylib/models.py",
                   "class User:\n    def get_name(self):\n        return 'name'\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("mylib.models.User.get_name")
            assert result is not None
            assert result.kind == "function"

    def test_four_segment_symbol(self):
        """package.module.Class.method 四段符号"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "pkg/submod/__init__.py", "")
            _write(repo, "pkg/submod/data.py",
                   "class DataStore:\n    def query(self, sql):\n        pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("pkg.submod.data.DataStore.query")
            assert result is not None
            assert result.kind == "function"
            assert result.owner == "DataStore"

    def test_class_member_not_found(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "typer/main.py",
                   "class Typer:\n    def __init__(self):\n        pass\n")
            r = SymbolResolverV2(str(repo))
            # command 不在 Typer 中
            assert r.resolve("typer.Typer.command") is None


# ═══════════════════════════════════════════════════════════════════
# Owner 约束验证
# ═══════════════════════════════════════════════════════════════════


class TestOwnerQualified:
    def test_evidence_to_dict(self):
        """Evidence.to_dict → 验证 to_dict 在 class Evidence 内部"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "models.py",
                   "class Other:\n    def to_dict(self):\n        pass\n\n\nclass Evidence:\n    def to_dict(self):\n        return {}\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("Evidence.to_dict")
            assert result is not None
            assert result.kind == "function"
            assert result.owner == "Evidence"

    def test_package_not_confused_with_class(self):
        """httpx 被当作包而非 class，包名不会去 greb 搜索 class httpx"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "httpx/__init__.py", "from ._client import Client\n")
            _write(repo, "httpx/_client.py",
                   "class Client:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("httpx.Client")
            assert result is not None
            # 不应出现 "class httpx" 导致的误判
            assert "class" in result.kind

    def test_same_method_different_class(self):
        """同名方法在不同类中 → 只匹配到指定 owner 的类"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "app.py",
                   "class Foo:\n    def run(self):\n        pass\n\n\nclass Bar:\n    def run(self):\n        return True\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("Bar.run")
            assert result is not None
            assert result.owner == "Bar"


# ═══════════════════════════════════════════════════════════════════
# Import 追踪
# ═══════════════════════════════════════════════════════════════════


class TestImportTracing:
    def test_multi_level_re_export(self):
        """__init__ → api.py → _impl.py 多级 re-export"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "mylib/__init__.py", "from .api import get_client\n")
            _write(repo, "mylib/api.py",
                   "from ._impl import get_client\n")
            _write(repo, "mylib/_impl.py",
                   "def get_client():\n    return 'client'\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("mylib.get_client")
            # 应该追踪到 _impl.py 中的定义
            assert result is not None
            assert result.kind == "function"
            # 解析链路应该包含 import 步骤
            assert len(result.resolution_path) >= 2

    def test_import_alias(self):
        """from .x import Client as SyncClient"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "mylib/__init__.py",
                   "from ._client import Client as SyncClient\n")
            _write(repo, "mylib/_client.py",
                   "class Client:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            # 请求 SyncClient，import 后指向 Client
            result = r.resolve("mylib.SyncClient")
            # import alias 追踪：from ._client import Client as SyncClient
            # AST ImportFrom 节点中 name="Client", asname="SyncClient"
            assert result is not None
            assert result.kind == "class"

    def test_import_chain_stops_at_definition(self):
        """import 不能作为 DEFINITION 终点"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "mylib/__init__.py",
                   "from ._client import Client\n")
            _write(repo, "mylib/_client.py",
                   "from ._base import Client\n")
            _write(repo, "mylib/_base.py",
                   "class Client:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("mylib.Client")
            assert result is not None
            assert result.kind == "class"
            # 应该在 _base.py 中找到 class Client
            assert "_base" in result.file

    def test_cannot_trace_to_definition_returns_none(self):
        """import 链无法追踪到最终定义 → 返回 None"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "mylib/__init__.py",
                   "from ._missing import Client\n")
            # _missing.py 不存在
            r = SymbolResolverV2(str(repo))
            result = r.resolve("mylib.Client")
            assert result is None

    def test_import_cycle_no_infinite_loop(self):
        """import 循环不会导致死循环"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "mylib/__init__.py", "from .a import X\n")
            _write(repo, "mylib/a.py", "from .b import X\n")
            _write(repo, "mylib/b.py", "from .a import X\n")
            r = SymbolResolverV2(str(repo))
            # 应该快速返回 None（循环中找不到定义），不会死循环
            result = r.resolve("mylib.X")
            assert result is None  # 循环中没有真正的定义


# ═══════════════════════════════════════════════════════════════════
# 候选排序
# ═══════════════════════════════════════════════════════════════════


class TestCandidateRanking:
    def test_prefer_src_over_tests(self):
        """同名类在 src 和 tests → 选 src"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "app.py", "class Thing:\n    pass\n")
            # tests 目录
            (repo / "tests").mkdir(exist_ok=True)
            _write(repo, "tests/fakes.py", "class Thing:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("Thing")
            assert result is not None
            # 应该优先选非 tests 目录的结果
            assert "tests" not in result.file.lower() or "test" not in result.file.lower().split("/")[0]

    def test_prefer_src_over_examples(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "app.py", "class Thing:\n    pass\n")
            (repo / "examples").mkdir(exist_ok=True)
            _write(repo, "examples/demo.py", "class Thing:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("Thing")
            assert result is not None
            assert "examples" not in str(result.file).lower().split("/")


# ═══════════════════════════════════════════════════════════════════
# 边界条件
# ═══════════════════════════════════════════════════════════════════


class TestEdgeCases:
    def test_syntax_error_file_graceful(self):
        """语法错误的文件优雅跳过"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "broken.py", "class Bad {{{{{\n")
            _write(repo, "good.py", "class Good:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("Good")
            assert result is not None
            assert result.kind == "class"

    def test_underscore_file(self):
        """前导下划线文件正常搜索"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "_internal.py", "def helper():\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("helper")
            assert result is not None
            assert result.kind == "function"
            assert "_internal" in result.file

    def test_empty_symbol_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            r = SymbolResolverV2(str(d))
            assert r.resolve("") is None
            assert r.resolve("   ") is None

    def test_dotted_name_no_package(self):
        """点号名称但包不存在 → 回退到 owner-qualified 搜索"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "app.py",
                   "class MyService:\n    def start(self):\n        pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("MyService.start")
            assert result is not None
            assert result.owner == "MyService"
            assert result.kind == "function"

    def test_source_root_src_dir(self):
        """src/ 目录下的包也能找到"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "src/mylib/__init__.py",
                   "from .core import Engine\n")
            _write(repo, "src/mylib/core.py",
                   "class Engine:\n    def run(self):\n        pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("mylib.Engine")
            assert result is not None
            assert result.kind == "class"

    def test_excluded_dirs_skipped(self):
        """venv 等目录被跳过"""
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "app.py", "class Widget:\n    pass\n")
            _write(repo, ".venv/lib/site.py",
                   "class Widget:\n    ...\n")  # 不应匹配这个
            r = SymbolResolverV2(str(repo))
            result = r.resolve("Widget")
            assert result is not None
            assert ".venv" not in result.file

    def test_multiple_candidates_returns_first_ranked(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "a.py", "class Tool:\n    pass\n")
            _write(repo, "b.py", "class Tool:\n    pass\n")
            r = SymbolResolverV2(str(repo))
            result = r.resolve("Tool")
            assert result is not None
            assert result.kind == "class"


# ═══════════════════════════════════════════════════════════════════
# resolved_to_evidence
# ═══════════════════════════════════════════════════════════════════


class TestResolvedToEvidence:
    def test_valid_resolved_produces_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            _write(repo, "app.py", "class Widget:\n    def run(self):\n        pass\n")
            r = SymbolResolverV2(str(repo))
            resolved = r.resolve("Widget.run")
            assert resolved is not None
            ev = resolved_to_evidence(resolved, str(repo))
            assert ev is not None
            assert ev.source == "resolve_symbol"
            assert ev.kind == "code"
            assert ev.location is not None
            assert ev.location.file
            assert ev.location.start_line > 0
            assert ev.snippet != ""
            assert ev.confidence > 0

    def test_invalid_resolved_returns_none(self):
        bad = ResolvedSymbol(
            requested_name="x", canonical_name="x",
            file="", line=0, kind="", confidence=0,
        )
        assert resolved_to_evidence(bad, ".") is None
