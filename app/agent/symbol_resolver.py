"""SymbolResolverV2 — 统一 Python 符号解析器。

替代 ToolExecutor._resolve() 的纯 grep 方式，用 AST + 路径遍历
支持 7 种解析场景：
  1. 包导出（httpx.Client → __init__.py re-export 追踪）
  2. 类成员（typer.Typer.command → AST class body 搜索）
  3. 模块函数（typer._main → _main.py）
  4. Owner 约束验证（Evidence.to_dict）
  5. Import alias 追踪
  6. 相对导入解析
  7. 前导下划线文件支持

核心算法：最长可导入模块前缀 + 剩余 class/member 链解析。
"""

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.models.evidence import Evidence
from app.models.location import CodeLocation

# ── 排除目录（全仓库扫描时跳过）───────────────────────────────────

_EXCLUDE_DIRS = frozenset({
    ".venv", "venv", "build", "dist", "site-packages", ".git",
    "__pycache__", ".tox", ".eggs", "node_modules", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "htmlcov",
})

_EXCLUDE_DIR_NAMES = frozenset({
    "tests", "test", "docs", "doc", "examples", "example",
    "benchmarks", "benchmark",
})

# ── ResolvedSymbol ──────────────────────────────────────────────────


@dataclass
class ResolvedSymbol:
    """符号解析结果（中间表示，由 ToolExecutor 转为 Evidence）。"""
    requested_name: str           # 原始查询符号（如 "httpx.Client"）
    canonical_name: str           # 规范化名称（如 "httpx._client.Client"）
    file: str                     # repo_path 相对路径
    line: int                     # 定义行号
    end_line: int = 0             # 定义结束行号（AST end_lineno）
    kind: str = ""                # "class" | "function" | "async_function"
    owner: str = ""               # 直接所属类名
    resolution_path: list[str] = field(default_factory=list)  # 解析链路
    confidence: float = 1.0       # 0~1

    def is_valid(self) -> bool:
        return bool(self.file and self.line > 0 and self.kind)


# ── SymbolResolverV2 ────────────────────────────────────────────────


class SymbolResolverV2:
    """统一符号解析器。"""

    def __init__(self, repo_path: str):
        self._repo_path = Path(repo_path).resolve()
        self._source_roots = self._discover_source_roots()
        self._name_index: dict[str, list[Path]] = {}  # 名称 → [文件绝对路径]
        self._ast_cache: dict[Path, Optional[ast.AST]] = {}  # 已解析 AST 缓存
        self._source_cache: dict[Path, Optional[tuple[str, list[str]]]] = {}  # (源码, 行列表)
        self._package_dir_cache: dict[str, Optional[Path]] = {}  # 包名 → 目录
        self._module_file_cache: dict[str, Optional[Path]] = {}  # 模块点号名 → .py 或 __init__.py
        self._build_index()

    # ── 公共入口 ──────────────────────────────────────────────────

    def resolve(self, symbol: str) -> Optional[ResolvedSymbol]:
        """解析点号限定符号，返回 ResolvedSymbol 或 None。"""
        if not symbol or not symbol.strip():
            return None

        parts = symbol.split(".")
        return self._resolve_impl(parts, symbol)

    def get_ast(self, rel_path: str) -> Optional[ast.AST]:
        """获取指定文件的已解析 AST（带缓存）。rel_path 是相对于 repo 的路径。"""
        abs_path = self._repo_path / rel_path
        if not abs_path.is_file():
            # 也尝试 source roots
            for root in self._source_roots:
                candidate = root / rel_path
                if candidate.is_file():
                    abs_path = candidate
                    break
            else:
                return None
        return self._parse_file(abs_path)

    @staticmethod
    def find_node_at_line(tree: ast.AST, lineno: int) -> Optional[ast.AST]:
        """在 AST 树中定位包含指定行号的最深层节点。"""
        best: Optional[ast.AST] = None
        best_depth = -1

        def _walk(node, depth=0):
            nonlocal best, best_depth
            if not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
                # 容器节点（如 Module）没有位置信息，继续遍历子节点
                for child in ast.iter_child_nodes(node):
                    _walk(child, depth + 1)
                return
            if node.lineno <= lineno <= (node.end_lineno or node.lineno):
                if depth > best_depth:
                    best = node
                    best_depth = depth
                for child in ast.iter_child_nodes(node):
                    _walk(child, depth + 1)

        _walk(tree, 0)
        return best

    # ── 内部：主解析逻辑 ──────────────────────────────────────────

    def _resolve_impl(self, parts: list[str],
                      full_symbol: str) -> Optional[ResolvedSymbol]:
        """主解析：模块前缀 + 链解析。"""
        # 1. 找最长模块前缀
        module_dotted, remaining = self._resolve_module_prefix(parts)

        if module_dotted:
            module_path = self._locate_module(module_dotted)
            if module_path is not None:
                result = self._resolve_chain(module_path, remaining, full_symbol)
                if result and result.is_valid():
                    return result

        # 2. 无模块前缀或模块内解析失败：owner 约束 + 全仓库搜索
        if len(parts) >= 2:
            # Owner-qualified: Evidence.to_dict
            owner, member = parts[-2], parts[-1]
            result = self._resolve_owner_qualified(owner, member, full_symbol)
            if result and result.is_valid():
                return result

        # 3. 简单符号：全仓库索引搜索
        return self._resolve_direct(full_symbol.split(".")[-1], full_symbol)

    # ── 模块前缀解析 ──────────────────────────────────────────────

    def _resolve_module_prefix(self, parts: list[str]) -> tuple[str, list[str]]:
        """从左向右找最长可导入模块前缀。

        返回 (module_dotted, remaining_parts)。
        例如 ["httpx","_client","Client","send"] → ("httpx._client", ["Client","send"])
        """
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            if self._module_exists(candidate):
                return candidate, parts[i:]
        return "", list(parts)

    def _module_exists(self, dotted_name: str) -> bool:
        """检查点号模块名是否存在（.py 文件或包目录）。"""
        if dotted_name in self._module_file_cache:
            return self._module_file_cache[dotted_name] is not None

        parts = dotted_name.split(".")
        rel = Path(*parts)
        # 作为 .py 文件
        py_file = rel.with_suffix(".py")
        # 作为包目录
        pkg_init = rel / "__init__.py"

        for root in self._source_roots:
            if (root / py_file).is_file():
                self._module_file_cache[dotted_name] = root / py_file
                return True
            if (root / pkg_init).is_file():
                self._module_file_cache[dotted_name] = root / pkg_init
                return True

        self._module_file_cache[dotted_name] = None
        return False

    def _locate_module(self, dotted_name: str) -> Optional[Path]:
        """定位模块的文件路径。返回 .py 文件路径或包的 __init__.py 路径。"""
        if dotted_name in self._module_file_cache:
            return self._module_file_cache[dotted_name]

        parts = dotted_name.split(".")
        rel = Path(*parts)
        py_file = rel.with_suffix(".py")
        pkg_init = rel / "__init__.py"

        for root in self._source_roots:
            if (root / py_file).is_file():
                self._module_file_cache[dotted_name] = root / py_file
                return root / py_file
            if (root / pkg_init).is_file():
                self._module_file_cache[dotted_name] = root / pkg_init
                return root / pkg_init

        self._module_file_cache[dotted_name] = None
        return None

    # ── 链解析 ────────────────────────────────────────────────────

    def _resolve_chain(self, module_path: Path, remaining: list[str],
                       full_symbol: str) -> Optional[ResolvedSymbol]:
        """在模块内解析 remaining 链（class/member/member...）。

        module_path: .py 文件或 __init__.py
        remaining: ["Client", "send"] 表示在模块内找 class Client → def send
        """
        if not remaining:
            return None

        current_file = module_path
        current_ast = self._parse_file(current_file)
        if current_ast is None:
            return None

        visited: set[Path] = set()
        resolution_path: list[str] = []
        owner = ""
        resolved_node = None
        resolved_file = current_file

        for idx, name in enumerate(remaining):
            is_last = (idx == len(remaining) - 1)
            node = self._find_def_in_ast(current_ast, name)

            # 如果直接定义没找到，尝试找 import 语句
            if node is None:
                node = self._find_import_of(current_ast, name)

            # 如果"定义"是 import，追踪到源头
            import_depth = 0
            while node is not None and self._is_import_node(node):
                if import_depth >= 5:
                    break
                # 解析 import 的原始名称（处理 alias: from .x import Real as Alias）
                original_name = self._resolve_import_original_name(node, name)
                target_file = self._resolve_import_target(node, current_file.parent)
                if target_file is None or target_file in visited:
                    break
                visited.add(target_file)
                import_depth += 1
                resolution_path.append(
                    f"{self._rel_path(current_file)}:import {original_name}"
                    f"{' as ' + name if original_name != name else ''} (depth {import_depth})")
                current_file = target_file
                current_ast = self._parse_file(current_file)
                if current_ast is None:
                    break
                node = self._find_def_in_ast(current_ast, original_name)
                if node is None:
                    node = self._find_import_of(current_ast, original_name)

            if node is None:
                return None

            # 只有 ClassDef/FunctionDef/AsyncFunctionDef 才是真正的定义
            if self._is_import_node(node):
                # 追踪到底仍是 import（例如 from 外部包 import）
                return None

            if not self._is_definition_node(node):
                return None

            resolved_node = node
            resolved_file = current_file
            node_kind = self._node_kind(node)

            if is_last:
                # 最后一个：返回此节点
                resolution_path.append(
                    f"{self._rel_path(resolved_file)}:{node_kind} {name}")
                return ResolvedSymbol(
                    requested_name=full_symbol,
                    canonical_name=self._build_canonical_name(
                        module_path, remaining[:idx + 1], resolved_file),
                    file=self._rel_path(resolved_file),
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno),
                    kind=node_kind,
                    owner=owner,
                    resolution_path=resolution_path,
                    confidence=1.0,
                )
            else:
                # 中间层：必须是 class，继续在其 body 内查找下一个
                if not isinstance(node, ast.ClassDef):
                    return None
                owner = name
                resolution_path.append(
                    f"{self._rel_path(resolved_file)}:class {name}")
                # 直接在 class body 内找下一个成员
                member_node = self._find_member_in_class_body(node, remaining[idx + 1])
                if member_node is None:
                    return None

                # 对 member 也做 import 追踪
                m_import_depth = 0
                m_name = remaining[idx + 1]
                while member_node is not None and self._is_import_node(member_node):
                    if m_import_depth >= 5:
                        break
                    m_original = self._resolve_import_original_name(member_node, m_name)
                    target_f = self._resolve_import_target(member_node, resolved_file.parent)
                    if target_f is None or target_f in visited:
                        break
                    visited.add(target_f)
                    m_import_depth += 1
                    target_ast = self._parse_file(target_f)
                    if target_ast is None:
                        break
                    resolved_file = target_f
                    member_node = self._find_def_in_ast(target_ast, m_original)
                    if member_node is None:
                        member_node = self._find_import_of(target_ast, m_original)

                if member_node is None or not self._is_definition_node(member_node):
                    return None

                resolved_node = member_node
                m_kind = self._node_kind(member_node)
                resolution_path.append(
                    f"{self._rel_path(resolved_file)}:{m_kind} {remaining[idx + 1]}")
                return ResolvedSymbol(
                    requested_name=full_symbol,
                    canonical_name=self._build_canonical_name(
                        module_path, remaining, resolved_file),
                    file=self._rel_path(resolved_file),
                    line=member_node.lineno,
                    end_line=getattr(member_node, "end_lineno", member_node.lineno),
                    kind=m_kind,
                    owner=owner,
                    resolution_path=resolution_path,
                    confidence=1.0,
                )

        return None

    # ── Owner 约束解析 ────────────────────────────────────────────

    def _resolve_owner_qualified(self, owner: str, member: str,
                                  full_symbol: str) -> Optional[ResolvedSymbol]:
        """解析 owner-qualified 符号（如 Evidence.to_dict）。

        先定位 owner class，再验证 member 在其 AST 体内。
        """
        candidates = self._name_index.get(owner, [])
        if not candidates:
            return None

        ranked = self._rank_candidates(candidates, owner)
        for candidate_path in ranked:
            ast_node = self._parse_file(candidate_path)
            if ast_node is None:
                continue
            # 找 class <owner>
            owner_class = self._find_def_in_ast(ast_node, owner)
            if owner_class is None or not isinstance(owner_class, ast.ClassDef):
                continue
            # 在 class body 内找 member
            member_node = self._find_member_in_class_body(owner_class, member)
            if member_node is not None and self._is_definition_node(member_node):
                return ResolvedSymbol(
                    requested_name=full_symbol,
                    canonical_name=f"{owner}.{member}",
                    file=self._rel_path(candidate_path),
                    line=member_node.lineno,
                    end_line=getattr(member_node, "end_lineno", member_node.lineno),
                    kind=self._node_kind(member_node),
                    owner=owner,
                    resolution_path=[
                        f"{self._rel_path(candidate_path)}:class {owner}",
                        f"{self._rel_path(candidate_path)}:{self._node_kind(member_node)} {member}",
                    ],
                    confidence=1.0,
                )

        return None

    # ── 直接搜索（简单符号）───────────────────────────────────────

    def _resolve_direct(self, name: str,
                        full_symbol: str) -> Optional[ResolvedSymbol]:
        """通过名称索引直接搜索简单符号。"""
        candidates = self._name_index.get(name, [])
        if not candidates:
            return None

        ranked = self._rank_candidates(candidates, name)
        for candidate_path in ranked:
            ast_node = self._parse_file(candidate_path)
            if ast_node is None:
                continue
            def_node = self._find_def_in_ast(ast_node, name)
            if def_node is not None and self._is_definition_node(def_node):
                return ResolvedSymbol(
                    requested_name=full_symbol,
                    canonical_name=name,
                    file=self._rel_path(candidate_path),
                    line=def_node.lineno,
                    end_line=getattr(def_node, "end_lineno", def_node.lineno),
                    kind=self._node_kind(def_node),
                    resolution_path=[
                        f"{self._rel_path(candidate_path)}:{self._node_kind(def_node)} {name}"
                    ],
                    confidence=1.0,
                )

        return None

    # ── Import 追踪 ────────────────────────────────────────────────

    @staticmethod
    def _is_import_node(node: ast.AST) -> bool:
        """判断 AST 节点是否是 import 语句（不是真正的定义）。"""
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return True
        # 赋值形式: Client = _client.Client 不追踪
        return False

    @staticmethod
    def _is_definition_node(node: ast.AST) -> bool:
        """判断是否是真正的定义节点。"""
        return isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))

    def _resolve_import_target(self, node: ast.AST,
                                current_dir: Path) -> Optional[Path]:
        """解析 import 语句的目标文件路径。

        支持：
        - from ._client import Client  (相对导入)
        - from httpx._client import Client  (绝对导入)
        - import httpx._client  (绝对导入)
        """
        if isinstance(node, ast.ImportFrom):
            if node.module is None:
                return None
            module = node.module
            level = node.level if hasattr(node, 'level') else 0
            if level > 0:
                # 相对导入: from ._client import Client
                return self._resolve_relative_import(module, level, current_dir)
            else:
                # 绝对导入: from httpx._client import Client
                return self._locate_module(module)
        elif isinstance(node, ast.Import):
            # import httpx._client
            for alias in node.names:
                return self._locate_module(alias.name)
        return None

    @staticmethod
    def _resolve_import_original_name(node: ast.AST, alias_name: str) -> str:
        """获取 import 节点中 alias 对应的原始名称。

        from .x import Real as Alias → alias_name="Alias" → "Real"
        from .x import Real → alias_name="Real" → "Real"
        """
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == alias_name:
                    return alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name) == alias_name:
                    return alias.name
        return alias_name

    def _resolve_relative_import(self, module: str, level: int,
                                  current_dir: Path) -> Optional[Path]:
        """将相对导入解析为绝对文件路径。

        level=1: .   → current_dir
        level=2: ..  → current_dir.parent
        level=3: ... → current_dir.parent.parent
        """
        base = current_dir
        for _ in range(level - 1):
            base = base.parent

        parts = module.split(".") if module else []
        if parts:
            rel = Path(*parts)
            py_file = base / rel.with_suffix(".py")
            if py_file.is_file():
                return py_file
            pkg_init = base / rel / "__init__.py"
            if pkg_init.is_file():
                return pkg_init
        else:
            # from . import X → 当前目录的 __init__.py
            init = base / "__init__.py"
            if init.is_file():
                return init
        return None

    # ── AST 操作 ───────────────────────────────────────────────────

    def _parse_file(self, path: Path) -> Optional[ast.AST]:
        """解析 Python 文件为 AST，缓存结果。"""
        if path in self._ast_cache:
            return self._ast_cache[path]

        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
            self._ast_cache[path] = tree
            self._source_cache[path] = (source, source.splitlines())
            return tree
        except (SyntaxError, FileNotFoundError, OSError, UnicodeDecodeError):
            self._ast_cache[path] = None
            self._source_cache[path] = None
            return None

    @staticmethod
    def _find_def_in_ast(tree: ast.AST, name: str) -> Optional[ast.AST]:
        """在 AST 中搜索顶级 class/function/async_function 定义。"""
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == name:
                    return node
        return None

    @staticmethod
    def _find_import_of(tree: ast.AST, name: str) -> Optional[ast.AST]:
        """在 AST 中搜索导入指定名称的 import 语句。"""
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    target = alias.asname or alias.name
                    if target == name:
                        return node
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    target = alias.asname or alias.name
                    if target == name:
                        return node
        return None

    @staticmethod
    def _find_member_in_class_body(class_node: ast.ClassDef,
                                    member_name: str) -> Optional[ast.AST]:
        """在类体 AST 中搜索成员定义（方法、内部类）。"""
        for node in class_node.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == member_name:
                    return node
            # 处理装饰器包裹的方法
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
        # 递归搜索嵌套在 if/with/try 等块中的定义
        for node in ast.walk(class_node):
            if node is class_node:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == member_name:
                    return node
        return None

    @staticmethod
    def _node_kind(node: ast.AST) -> str:
        if isinstance(node, ast.ClassDef):
            return "class"
        if isinstance(node, ast.AsyncFunctionDef):
            return "async_function"
        if isinstance(node, ast.FunctionDef):
            return "function"
        return "unknown"

    # ── 候选排序 ───────────────────────────────────────────────────

    def _rank_candidates(self, candidates: list[Path],
                          _target_name: str) -> list[Path]:
        """按优先级排序候选项：生产源码 > tests/docs/examples。"""
        def _score(p: Path) -> int:
            rel_str = str(self._rel_path(p)).lower().replace("\\", "/")
            parts_set = set(rel_str.split("/"))
            # 排除目录给负分
            if parts_set & _EXCLUDE_DIR_NAMES:
                return 10
            # 生产源码优先
            return 0

        return sorted(candidates, key=_score)

    # ── Source Root 发现 ───────────────────────────────────────────

    def _discover_source_roots(self) -> list[Path]:
        """发现仓库中的 Python 源码根目录。"""
        roots = [self._repo_path]
        for candidate in ["src", "lib", "python"]:
            d = self._repo_path / candidate
            if d.is_dir() and d.name not in _EXCLUDE_DIRS:
                roots.append(d)
        return roots

    # ── 名称索引 ───────────────────────────────────────────────────

    def _build_index(self) -> None:
        """初始化轻量名称索引（文本扫描，非 AST）。"""
        for py_file in self._iter_python_files():
            try:
                lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except (FileNotFoundError, OSError):
                continue
            for lineno, line in enumerate(lines, 1):
                m = re.match(r'^\s*(?:async\s+def|def|class)\s+(\w+)', line)
                if m:
                    name = m.group(1)
                    if name not in self._name_index:
                        self._name_index[name] = []
                    if py_file not in self._name_index[name]:
                        self._name_index[name].append(py_file)

    def _iter_python_files(self):
        """遍历所有 Python 源文件（排除虚拟环境、构建产物等）。"""
        for root in self._source_roots:
            for py_file in root.rglob("*.py"):
                parts = py_file.relative_to(root).parts
                if any(p in _EXCLUDE_DIRS for p in parts):
                    continue
                yield py_file

    def _find_package_dir(self, name: str) -> Optional[Path]:
        """检查名称是否为包目录（有 __init__.py）。"""
        if name in self._package_dir_cache:
            return self._package_dir_cache[name]

        for root in self._source_roots:
            pkg = root / name
            if pkg.is_dir() and (pkg / "__init__.py").is_file():
                self._package_dir_cache[name] = pkg
                return pkg

        self._package_dir_cache[name] = None
        return None

    # ── 辅助 ───────────────────────────────────────────────────────

    def _rel_path(self, path: Path) -> str:
        """将绝对路径转为相对于 repo_path 的路径。"""
        try:
            return str(path.relative_to(self._repo_path)).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")

    def _build_canonical_name(self, module_path: Path, remaining: list[str],
                               resolved_file: Path) -> str:
        """构建规范化符号名。"""
        try:
            rel = resolved_file.relative_to(self._repo_path)
        except ValueError:
            rel = resolved_file
        mod_name = str(rel.with_suffix("")).replace("\\", "/").replace("/", ".")
        if mod_name.endswith(".__init__"):
            mod_name = mod_name[:-9]  # 去掉 .__init__
        return f"{mod_name}.{'.'.join(remaining)}"


# ── Evidence 转换 ──────────────────────────────────────────────────


def resolved_to_evidence(resolved: ResolvedSymbol, repo_path: str) -> Optional[Evidence]:
    """将 ResolvedSymbol 转换为 Evidence（保持 source="resolve_symbol"）。

    供 ToolExecutor 使用，确保所有位置型 Tool 产出统一的 Evidence 格式。
    """
    if not resolved.is_valid():
        return None

    snippet = _make_snippet(Path(repo_path) / resolved.file,
                            resolved.line, resolved.end_line)

    ev = Evidence(
        kind="code",
        source="resolve_symbol",
        location=CodeLocation(
            file=resolved.file,
            start_line=resolved.line,
            end_line=resolved.end_line or resolved.line,
            symbol=resolved.requested_name,
        ),
        snippet=snippet,
        confidence=resolved.confidence,
    )
    ev.set_deterministic_id("HEAD", resolved.file, resolved.line,
                            resolved.end_line or resolved.line, snippet)
    return ev


def _make_snippet(file_path: Path, start_line: int, end_line: int,
                  before: int = 2, after: int = 5) -> str:
    """读取文件并生成带行号前缀的片段。"""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return ""
    lines = text.splitlines()
    s = max(1, start_line - before)
    e = min(len(lines), max(start_line, end_line) + after)
    return "\n".join(f"{i}| {lines[i - 1]}" for i in range(s, e + 1))
