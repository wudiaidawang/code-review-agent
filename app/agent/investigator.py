"""V1.1 Investigation Agent — 代码库探索模式

受限工具选择 + 证据累积 + 引用式回答。
LLM 只用于最终合成，工具选择走确定性规则。
"""

import os
import re
import time
from dataclasses import dataclass, field

from app.models.evidence import Evidence
from app.models.location import CodeLocation
from app.tools.llm_tool import chat
from app.tools.search_tool import SearchTool
from app.tools.contract import ToolRequest


@dataclass
class InvestigationResult:
    """一次调查的完整产出。"""
    question: str
    answer: str = ""
    evidence: list[Evidence] = field(default_factory=list)
    files_visited: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "evidence": [e.to_dict() for e in self.evidence],
            "files_visited": self.files_visited,
            "findings": self.findings,
            "trace": self.trace,
            "duration_ms": round(self.duration_ms, 1),
        }


# ---- 问题类型识别 ----

_QUESTION_PATTERNS = [
    (r"在哪|在哪里|where|定义|defined|位置|location|哪个文件|which file", "locate"),
    (r"做什么|干什么|what.*do|作用|功能|purpose|负责", "explain"),
    (r"调用|calls?|invoke|依赖|depends|import|连接|connect", "trace"),
    (r"所有|全部|all|every|list|列举|find.*all", "grep"),
]


def _classify(question: str) -> str:
    """根据问题文本判断调查类型。"""
    lower = question.lower()
    for pattern, qtype in _QUESTION_PATTERNS:
        if re.search(pattern, lower):
            return qtype
    return "locate"  # 默认按定位处理


# ---- 调查核心 ----


class InvestigationAgent:
    """受限代码库调查 Agent。"""

    def __init__(self, call_llm=None):
        self.call_llm = call_llm or chat

    def investigate(self, repo_path: str, question: str) -> InvestigationResult:
        """调查一个问题并返回带证据的答案。"""
        t0 = time.perf_counter()
        result = InvestigationResult(question=question)
        abs_path = os.path.abspath(repo_path)
        search = SearchTool()

        qtype = _classify(question)
        result.trace.append(f"question_type={qtype}")

        # 提取问题中的关键词（取引号内容或大写/驼峰词）
        keywords = self._extract_keywords(question)
        if not keywords:
            result.answer = "无法从问题中提取关键词，请提供具体的函数名、类名或文件名。"
            result.duration_ms = (time.perf_counter() - t0) * 1000
            return result

        result.trace.append(f"keywords={keywords}")

        # Step 1: 通过 SearchTool 定位相关文件
        search_result = search.execute(ToolRequest(
            tool="search", params={
                "repo_path": abs_path, "query": keywords,
                "search_type": "grep", "max_results": 50,
            },
        ))

        grep_hits = search_result.artifacts.get("matches", [])
        grep_text = search_result.artifacts.get("files", [])

        if not grep_hits:
            # 尝试搜索文件名
            file_result = search.execute(ToolRequest(
                tool="search", params={
                    "repo_path": abs_path, "query": keywords[0],
                    "search_type": "filename", "max_results": 20,
                },
            ))
            grep_hits = file_result.artifacts.get("matches", [])
            grep_text = file_result.artifacts.get("files", [])
            result.trace.append("fallback: filename search")

        if not grep_hits:
            result.answer = f"在仓库中未找到与 '{' '.join(keywords)}' 相关的结果。"
            result.duration_ms = (time.perf_counter() - t0) * 1000
            return result

        result.trace.append(f"grep_hits={len(grep_hits)}")

        # Step 2: 解析搜索结果，收集文件列表
        files_seen: set[str] = set()
        evidence_snippets: list[tuple[str, int, str]] = []  # (file, line, snippet)

        for match in grep_hits[:50]:
            fpath = match.get("file", "")
            lineno = match.get("line", 1)
            snippet = match.get("snippet", "")
            if not fpath:
                continue
            files_seen.add(fpath)
            evidence_snippets.append((fpath, lineno, snippet[:200]))

        result.files_visited = sorted(files_seen)[:20]

        # Step 3: 为每个发现创建 Evidence
        for fpath, lineno, snippet in evidence_snippets[:30]:
            result.evidence.append(Evidence(
                kind="code", source="git_grep",
                location=CodeLocation(file=fpath, start_line=lineno),
                snippet=snippet,
                confidence=0.95,
            ))

        # Step 4: 读取关键文件的前 100 行（用于 LLM 上下文）
        context_chunks: list[str] = []
        for fpath in list(files_seen)[:5]:
            full_path = os.path.join(abs_path, fpath)
            try:
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()[:3000]  # 每个文件最多 3000 字符
                context_chunks.append(f"### {fpath}\n```python\n{content}\n```")
            except Exception:
                continue

        # Step 5: LLM 合成答案
        sys_prompt = (
            "你是一个代码库调查助手。根据提供的 grep 结果和文件内容，"
            "回答用户关于代码库的问题。回答必须引用具体的文件路径和行号。"
            "如果信息不足以回答，请明确说明。用中文回答。"
        )

        evidence_text = "\n".join(
            f"{f}:{l}: {s}" for f, l, s in evidence_snippets[:20]
        )
        context_text = "\n\n".join(context_chunks) if context_chunks else "(文件内容不可用)"

        user_prompt = f"""## 问题
{question}

## grep 搜索结果
{evidence_text[:3000]}

## 相关文件内容
{context_text[:4000]}

请根据以上信息回答问题，引用文件路径和行号。"""

        try:
            answer = self.call_llm(user_prompt, system=sys_prompt, temperature=0.3, max_tokens=1500)
            result.answer = answer.strip()
        except Exception as exc:
            # LLM 不可用时用 grep 结果直接回答
            result.answer = (
                f"（LLM 不可用，以下是原始搜索结果）\n\n"
                f"关键词 '{' '.join(keywords)}' 的匹配结果：\n\n"
                + "\n".join(f"- {f}:{l}: {s}" for f, l, s in evidence_snippets[:15])
            )
            result.trace.append(f"llm_fallback: {exc}")

        result.duration_ms = (time.perf_counter() - t0) * 1000
        return result

    @staticmethod
    def _extract_keywords(question: str) -> list[str]:
        """从问题中提取关键词（引号内容、驼峰词、大写词）。"""
        # 引号内容
        quoted = re.findall(r'["\']([^"\']+)["\']', question)
        if quoted:
            return quoted

        # 大写/驼峰标识符
        identifiers = re.findall(r'\b([A-Z][a-zA-Z0-9_]*|[a-z]+_[a-z_]+)\b', question)
        if identifiers:
            # 过滤常见的英文停用词
            stop = {"the", "is", "are", "where", "what", "how", "does", "do", "in", "of", "and", "or"}
            identifiers = [w for w in identifiers if w.lower() not in stop]
            if identifiers:
                return identifiers[:3]

        # 回退：提取英文单词
        words = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{2,}', question)
        stop = {"the", "is", "are", "where", "what", "how", "does", "do", "and", "or", "not"}
        return [w for w in words if w.lower() not in stop][:3]
