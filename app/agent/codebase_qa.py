"""受控代码库问答：为结构类问题建立小型、可引用的代码地图。"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.models.evidence import Evidence
from app.models.location import CodeLocation


_STRUCTURE_WORDS = ("接口", "路由", "api", "endpoint", "前端", "项目结构", "目录", "文件职责", "pipeline", "管道", "入口", "架构", "模块")
_MAX_FILES = 500
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_CONTEXT_FILES = 6


@dataclass
class CodebaseAnswer:
    answer: str
    evidence: list[Evidence]
    files_visited: list[str]
    plan: list[str]
    trace: list[str]


def can_answer(question: str) -> bool:
    return any(word in question.lower() for word in _STRUCTURE_WORDS)


def answer(question: str, repo_path: str, repo_commit: str, call_llm) -> CodebaseAnswer | None:
    """Answer a bounded structural question from scanned source facts only."""
    if not can_answer(question):
        return None
    route_question = any(word in question.lower() for word in ("接口", "路由", "api", "endpoint"))
    records = _scan_python_files(repo_path)
    if not records:
        return None
    selected = _select_records(question, records, route_question)
    evidence = [item for record in selected if (item := _evidence_from_record(record, repo_commit))]
    if not evidence:
        return None
    response = _route_answer(evidence) if route_question else (_llm_answer(question, evidence, call_llm) or _fallback_answer(evidence))
    return CodebaseAnswer(response, evidence,
                          sorted({item.location.file for item in evidence if item.location}),
                          ["codebase_map", "read_key_windows", "bounded_llm_synthesis"],
                          [f"codebase_qa: selected={len(evidence)} evidence", "codebase_qa: evidence_only_answer"])


def _scan_python_files(repo_path: str) -> list[dict]:
    root = Path(repo_path)
    records: list[dict] = []
    for current, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in {".git", ".venv", "venv", "__pycache__", ".deps"}]
        for name in names:
            if len(records) >= _MAX_FILES or not name.endswith(".py"):
                continue
            full = Path(current) / name
            try:
                if full.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = full.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(text)
            except (OSError, SyntaxError):
                continue
            lines = text.splitlines()
            routes, symbols = [], []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    symbols.append((node.name, node.lineno))
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for decorator in node.decorator_list:
                        route = _parse_route(decorator)
                        if route:
                            routes.append((*route, node.name, node.lineno))
            records.append({"file": full.relative_to(root).as_posix(), "lines": lines, "routes": routes, "symbols": symbols})
    return records


def _parse_route(node: ast.AST) -> tuple[str, str] | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not node.args:
        return None
    method = node.func.attr.upper()
    path = node.args[0]
    if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"} and isinstance(path, ast.Constant) and isinstance(path.value, str):
        return method, path.value
    return None


def _select_records(question: str, records: list[dict], route_question: bool) -> list[dict]:
    if route_question:
        return [record for record in records if record["routes"]][:_MAX_CONTEXT_FILES]
    terms = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", question.lower()))
    scored = []
    for record in records:
        text = (record["file"] + " " + " ".join(name for name, _ in record["symbols"])).lower()
        score = sum(term in text for term in terms)
        if "pipeline" in question.lower() and "pipeline" in record["file"].lower():
            score += 4
        if "前端" in question and ("api/" in record["file"] or "web" in record["file"]):
            score += 3
        if score:
            scored.append((score, record))
    return [record for _, record in sorted(scored, key=lambda item: (-item[0], item[1]["file"]))][:_MAX_CONTEXT_FILES] or records[:3]


def _evidence_from_record(record: dict, repo_commit: str) -> Evidence | None:
    lines = record["lines"]
    if record["routes"]:
        start = min(line for _, _, _, line in record["routes"])
        end = max(line for _, _, _, line in record["routes"])
        snippet = "\n".join(f"{method} {path} -> {handler}" for method, path, handler, _ in record["routes"])
        symbol = "FastAPI routes"
    elif record["symbols"]:
        symbol, start = next(
            ((name, line) for name, line in record["symbols"] if "pipeline" in name.lower()),
            record["symbols"][0],
        )
        end = min(len(lines), start + 35)
        snippet = "\n".join(lines[start - 1:end])
    else:
        return None
    evidence = Evidence(kind="code", source="codebase_map", location=CodeLocation(record["file"], start, end, symbol), snippet=snippet, confidence=1.0)
    evidence.set_deterministic_id(repo_commit, record["file"], start, end, snippet)
    return evidence


def _route_answer(evidence: list[Evidence]) -> str:
    routes = [line for item in evidence for line in item.snippet.splitlines() if re.match(r"^(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD) /", line)]
    listing = "\n".join(f"- {route}" for route in routes)
    return f"在已扫描到的 FastAPI 路由中，共发现 {len(routes)} 个接口：\n{listing}\n\n证据：" + ", ".join(f"[{item.id}]" for item in evidence) + "。"


def _llm_answer(question: str, evidence: list[Evidence], call_llm) -> str | None:
    context = "\n\n".join(f"[{item.id}] {item.location.file}:{item.location.start_line}-{item.location.end_line}\n{item.snippet[:3500]}" for item in evidence)
    try:
        result = call_llm(f"问题：{question}\n\n可用证据：\n{context}", system="你是代码库问答助手。只能陈述给定证据中直接可见的事实；每个要点必须带一个 [ev_xxx] 引用。证据不足时明确说不知道。", temperature=0, max_tokens=700, timeout=60, extra_body={"thinking": {"type": "disabled"}})
    except Exception:
        return None
    allowed = {item.id for item in evidence}
    cited = set(re.findall(r"\[(ev_[a-f0-9]+)\]", result or ""))
    return result.strip() if isinstance(result, str) and cited and cited <= allowed else None


def _fallback_answer(evidence: list[Evidence]) -> str:
    return "根据受控代码地图，相关入口如下：\n" + "\n".join(f"- `{item.location.file}:{item.location.start_line}` 包含 `{item.location.symbol}`。[{item.id}]" for item in evidence)
