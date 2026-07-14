"""SearchTool — 只读代码/符号搜索

提供 git grep 内容搜索和 git ls-files 文件名搜索，输出结构化 ToolResult，
供 Review Pipeline 和 Investigation Agent 复用。
"""

import subprocess
import time
from dataclasses import dataclass

from app.models.evidence import Evidence
from app.models.location import CodeLocation
from app.tools.contract import Tool, ToolRequest, ToolResult


@dataclass
class SearchTool:
    name: str = "search"

    def execute(self, request: ToolRequest) -> ToolResult:
        t0 = time.perf_counter()
        repo = request.params.get("repo_path", ".")
        query = request.params.get("query", "")
        search_type = request.params.get("search_type", "grep")
        max_results = request.params.get("max_results", 50)
        file_patterns = request.params.get("file_patterns", [])  # e.g. ["*.py", "*.js"]

        if not query:
            return ToolResult.failure(self.name, "SEARCH_EMPTY_QUERY", "搜索关键词为空")

        try:
            if search_type == "filename":
                lines = self._search_files(repo, query, file_patterns, max_results)
            else:
                lines = self._search_grep(repo, query, file_patterns, max_results)

            artifacts, evidence = self._build_results(lines, search_type)
            return ToolResult(
                tool=self.name,
                status="success",
                artifacts=artifacts,
                evidence=evidence,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return ToolResult.failure(self.name, "SEARCH_ERROR", str(e))

    # ---- 搜索实现 --------------------------------------------------

    def _search_grep(self, repo: str, query: str, patterns: list[str], limit: int) -> list[str]:
        """git grep 内容搜索。"""
        args = ["git", "-C", repo, "grep", "-n", "-i"]
        if patterns:
            for p in patterns[:5]:
                args.extend(["--", p])
        args.append("--")
        for keyword in (query if isinstance(query, list) else [query]):
            args.append(keyword)
        return self._run(args).split("\n")[:limit]

    def _search_files(self, repo: str, query: str, patterns: list[str], limit: int) -> list[str]:
        """git ls-files 文件名搜索。"""
        results: list[str] = []
        if patterns:
            for pattern in patterns[:5]:
                out = self._run(["git", "-C", repo, "ls-files", pattern])
                results.extend(out.split("\n"))
        else:
            out = self._run(["git", "-C", repo, "ls-files", f"*{query}*"])
            results = out.split("\n")
        return [r for r in results if r.strip()][:limit]

    # ---- 结果解析 --------------------------------------------------

    def _build_results(self, lines: list[str], search_type: str) -> tuple[dict, list[Evidence]]:
        """将搜索原始行解析为结构化 artifacts 和 Evidence。"""
        matched: list[dict] = []
        evidence: list[Evidence] = []
        files_seen: set[str] = set()

        for line in lines:
            if not line.strip() or line.startswith("\x1b"):
                continue
            # 格式: "file:lineno:content" (grep) 或 "file" (filename)
            if ":" in line:
                parts = line.split(":", 2)
                fpath = parts[0]
                try:
                    lineno = int(parts[1]) if len(parts) > 1 else 1
                    snippet = parts[2].strip()[:200] if len(parts) > 2 else ""
                except ValueError:
                    # 可能是文件名中有冒号，回退处理
                    fpath = line
                    lineno = 1
                    snippet = ""
            else:
                fpath = line.strip()
                lineno = 1
                snippet = ""

            if fpath not in files_seen:
                files_seen.add(fpath)
            matched.append({"file": fpath, "line": lineno, "snippet": snippet})

            if snippet:
                evidence.append(Evidence(
                    kind="code", source="search",
                    location=CodeLocation(file=fpath, start_line=lineno),
                    snippet=snippet,
                    confidence=0.95 if search_type == "grep" else 0.7,
                ))

        return {
            "matches": matched,
            "files": sorted(files_seen),
            "total_count": len(matched),
            "search_type": search_type,
        }, evidence

    # ---- 底层调用 --------------------------------------------------

    @staticmethod
    def _run(args: list[str], timeout: int = 30) -> str:
        result = subprocess.run(args, capture_output=True, timeout=timeout)
        return result.stdout.decode("utf-8", errors="replace")
