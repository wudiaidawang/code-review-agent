"""PythonParserTool — 基于内置 ast 模块提取 Python 符号

产出 artifacts 中的 "symbol_index"（Symbol 列表），供 LLM/Planner 了解代码结构。
"""

import ast
import time
from dataclasses import dataclass, field

from app.models.evidence import Evidence
from app.models.location import CodeLocation, Symbol
from app.tools.contract import Tool, ToolRequest, ToolResult


@dataclass
class ASTTool:
    name: str = "python_ast"

    def execute(self, request: ToolRequest) -> ToolResult:
        t0 = time.perf_counter()
        files = request.params.get("files", [])  # list of (path, source_text)
        symbols: list[Symbol] = []
        evidence: list[Evidence] = []

        for path, source in files:
            try:
                tree = ast.parse(source)
                visitor = _SymbolVisitor(path)
                visitor.visit(tree)
                symbols.extend(visitor.symbols)
                evidence.append(Evidence(
                    kind="code", source="python_ast",
                    location=CodeLocation(file=path),
                    snippet=f"parsed {len(visitor.symbols)} symbols",
                    confidence=1.0,
                ))
            except SyntaxError:
                evidence.append(Evidence(
                    kind="code", source="python_ast",
                    location=CodeLocation(file=path),
                    snippet="parse error — skipped",
                    confidence=1.0,
                ))

        return ToolResult(
            tool=self.name,
            status="success",
            artifacts={"symbol_index": [s.to_dict() for s in symbols]},
            evidence=evidence,
            duration_ms=(time.perf_counter() - t0) * 1000,
        )


class _SymbolVisitor(ast.NodeVisitor):
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.symbols: list[Symbol] = []

    def visit_FunctionDef(self, node):
        self.symbols.append(Symbol(
            name=node.name, kind="function",
            location=CodeLocation(file=self.filepath, start_line=node.lineno),
            calls=self._calls(node),
        ))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.symbols.append(Symbol(
            name=node.name, kind="function",
            location=CodeLocation(file=self.filepath, start_line=node.lineno),
            calls=self._calls(node),
        ))
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        self.symbols.append(Symbol(
            name=node.name, kind="class",
            location=CodeLocation(file=self.filepath, start_line=node.lineno),
        ))
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.symbols.append(Symbol(
                name=alias.name, kind="import",
                location=CodeLocation(file=self.filepath, start_line=node.lineno),
            ))

    def visit_ImportFrom(self, node):
        module = node.module or ""
        for alias in node.names:
            self.symbols.append(Symbol(
                name=f"{module}.{alias.name}", kind="import",
                location=CodeLocation(file=self.filepath, start_line=node.lineno),
            ))

    @staticmethod
    def _calls(node) -> list[str]:
        callee_names: list[str] = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                callee_names.append(child.func.id)
        return callee_names
