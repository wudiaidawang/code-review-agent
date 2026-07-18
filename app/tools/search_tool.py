"""SearchTool — 只读代码/符号搜索

提供 git grep 内容搜索和 git ls-files 文件名搜索，输出结构化 ToolResult，
供 Review Pipeline 和 Investigation Agent 复用。

结果按四维评分模型排序：
  1. file_type_score  — 文件类型（正式源码 > 测试 > 示例 > 文档 > 其他）
  2. hit_type_score   — 命中类型（精确定义 > 导入 > 调用 > 引用 > 注释/文本）
  3. match_precision  — 匹配精确度（大小写精确 > 忽略大小写单词 > 子串）
  4. query_coverage   — 多关键词覆盖（命中关键词越多得分越高）

使用流式 Top-K 处理：git grep 逐行输出 → 即时分类/评分 → 小根堆维护候选 →
扫描结束后确定性排序 → 返回 max_results。不再按字母序预截断。
"""

import ast
import heapq
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

from app.models.evidence import Evidence
from app.models.location import CodeLocation
from app.tools.contract import Tool, ToolRequest, ToolResult

# ---- 文件分类 ------------------------------------------------------------

# 源码后缀（仅当路径非文档/示例/测试目录时才算正式源码）
SOURCE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".rb", ".c", ".h", ".cpp", ".hpp"}

# 按目录段分类，不是简单字符串包含
DOC_DIRS = {"docs", "doc", "documentation", "docs_src", "doc_src"}
EXAMPLE_DIRS = {"examples", "example", "samples", "sample", "tutorials", "tutorial", "demo"}
TEST_DIRS = {"tests", "test", "testing", "__tests__", "spec"}
TEST_FILE_PREFIXES = ("test_", "spec_")
DOC_FILE_NAMES = {"readme", "changelog", "contributing", "license", "authors", "index", "security"}
BUILD_FILE_NAMES = {"setup.py", "setup.cfg", "pyproject.toml", "makefile", "dockerfile", "package.json", ".gitignore"}

# 文件类型评分（越大越优先）
_FILE_TYPE_SCORES = {
    "source": 500,
    "test": 300,
    "example": 200,
    "documentation": 100,
    "config": 50,
    "build": 30,
    "other": 10,
}

# 命中类型评分
_HIT_TYPE_SCORES = {
    "definition": 500,
    "import": 400,
    "call": 300,
    "reference": 200,
    "comment": 100,
}

# 匹配精确度分值
_MATCH_PRECISION = {
    "exact_case": 300,       # 大小写精确的完整单词匹配
    "case_insensitive": 250, # 忽略大小写的完整单词匹配
    "substring": 50,         # 部分子串包含
}

# 每个命中关键词贡献分值
_COVERAGE_PER_KEYWORD = 50

# 命中类型 → Evidence 置信度
HIT_CONFIDENCE = {
    "definition": 0.98, "call": 0.95, "import": 0.90,
    "reference": 0.80, "comment": 0.60,
}

# 资源保护上限
MAX_LINES_SCANNED = 20_000
MAX_OUTPUT_BYTES = 10 * 1024 * 1024  # 10 MB

# 定义名提取正则（多语言）：提取 class/def/fn/func 后紧跟的符号名
_RE_DEFINED_NAME = re.compile(
    r'(?:class|def|async\s+def)\s+(\w+)'                    # Python
    r'|\bfn\s+(\w+)'                                          # Rust
    r'|\bfunc\s+(?:\s*\([^)]*\)\s+)?(\w+)'                   # Go
    r'|\bfunction\s+(\w+)'                                    # JS/TS
    r'|\b(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|enum)\s+(\w+)'  # Java/TS
)


def _normalize_query(query) -> str:
    """将 query（str 或 list[str]）规整为空格分隔的单一字符串。"""
    if isinstance(query, list):
        return " ".join(str(q) for q in query)
    return str(query) if query else ""


def _classify_file(file_path: str) -> str:
    """根据路径目录段将文件归类为 source / test / example / documentation / config / build / other。

    按目录段精确匹配，不以简单字符串包含判断。优先级：文档 > 测试 > 示例 > 构建 > 配置 > 源码。

    注意：docs_src / doc_src 归类为 documentation，不因 .py 后缀自动归为 source。
    """
    path = file_path.replace("\\", "/")
    lower = path.lower()
    parts = path.split("/")

    # 1. 文档目录（含 docs_src / doc_src）
    for part in parts:
        if part.lower() in DOC_DIRS:
            return "documentation"

    # 2. 文件名命中文档特征（CHANGELOG, README 等）
    fname = os.path.basename(path)
    stem = os.path.splitext(fname)[0].lower()
    if any(d in stem for d in DOC_FILE_NAMES):
        return "documentation"
    if fname.lower().endswith((".md", ".rst", ".txt", ".adoc")):
        return "documentation"

    # 3. 测试目录
    for part in parts:
        if part.lower() in TEST_DIRS:
            return "test"

    # 4. 测试文件名
    if stem.startswith(TEST_FILE_PREFIXES) or stem.endswith(("_test", "_spec")):
        return "test"
    if stem == "conftest":
        return "test"

    # 5. 示例/教程目录
    for part in parts:
        if part.lower() in EXAMPLE_DIRS:
            return "example"

    # 6. 构建/配置文件（按文件名精确匹配）
    if stem in BUILD_FILE_NAMES:
        return "build"
    if fname.lower().endswith((".cfg", ".ini", ".toml", ".yml", ".yaml", ".json")):
        return "config"

    # 7. 源码（仅当不在上述任何目录时）
    ext = os.path.splitext(path)[1].lower()
    if ext in SOURCE_EXTS:
        return "source"

    return "other"


def _parse_defined_name(snippet: str) -> str | None:
    """从代码片段中提取 class/def/fn/func 定义的符号名（多语言支持）。"""
    m = _RE_DEFINED_NAME.search(snippet.strip())
    if m:
        return next((g for g in m.groups() if g is not None), None)
    return None


def _name_matches_keyword(name: str, keywords: list[str]) -> bool:
    """检查定义的符号名是否精确匹配查询关键词（大小写敏感，用于 Python；
    大小写不敏感，用于标识符回退判断）。"""
    if not name:
        return False
    for kw in keywords:
        if kw == name:
            return True
    # 回退：忽略大小写
    name_lower = name.lower()
    for kw in keywords:
        if kw.lower() == name_lower:
            return True
    return False


def _compute_match_precision(snippet: str, keywords: list[str]) -> int:
    """计算 snippet 对关键词的匹配精确度分值。

    取所有关键词中最佳匹配级别：
      - exact_case: 大小写精确的 \b 单词匹配 → 300
      - case_insensitive: 忽略大小写的 \b 单词匹配 → 250
      - substring: 子串包含 → 50
    """
    best = 0
    for kw in keywords:
        if not kw:
            continue
        # 大小写精确的单词匹配
        if re.search(r'\b' + re.escape(kw) + r'\b', snippet):
            best = max(best, _MATCH_PRECISION["exact_case"])
        elif re.search(r'\b' + re.escape(kw) + r'\b', snippet, re.IGNORECASE):
            best = max(best, _MATCH_PRECISION["case_insensitive"])
        elif kw.lower() in snippet.lower():
            best = max(best, _MATCH_PRECISION["substring"])
    return best


def _compute_query_coverage(snippet: str, keywords: list[str]) -> int:
    """计算 snippet 覆盖了多少个关键词，每个命中关键词贡献 _COVERAGE_PER_KEYWORD 分。"""
    count = 0
    for kw in keywords:
        if kw and kw.lower() in snippet.lower():
            count += 1
    return count * _COVERAGE_PER_KEYWORD


def _find_matched_terms(snippet: str, keywords: list[str]) -> list[str]:
    """返回 snippet 中实际命中的关键词列表。"""
    return [kw for kw in keywords if kw and kw.lower() in snippet.lower()]


# ---- AST 分析（流式复用）----------------------------------------------

def _analyze_python_ast_cached(file_path: str, query_keywords: list[str],
                                ast_cache: dict) -> dict[int, str]:
    """解析 Python 文件 AST，返回 {行号: hit_kind} — 带缓存。

    只有函数/类名匹配关键词时才标记为 definition。
    """
    if file_path in ast_cache:
        return ast_cache[file_path]

    if not file_path.endswith(".py"):
        ast_cache[file_path] = {}
        return {}

    kw_lower = [k.lower() for k in query_keywords if k]
    if not kw_lower:
        ast_cache[file_path] = {}
        return {}

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            source = f.read()
        if len(source) > 1_000_000:
            ast_cache[file_path] = {}
            return {}
        tree = ast.parse(source)

        line_kinds: dict[int, str] = {}

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if any(kw in node.name.lower() for kw in kw_lower):
                    line_kinds[node.lineno] = "definition"

            elif isinstance(node, ast.Call):
                target = ""
                if isinstance(node.func, ast.Name):
                    target = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    target = node.func.attr
                if target and any(kw in target.lower() for kw in kw_lower):
                    line_kinds.setdefault(node.lineno, "call")

            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                line_kinds.setdefault(node.lineno, "import")

        ast_cache[file_path] = line_kinds
        return line_kinds

    except (SyntaxError, MemoryError, RecursionError, OSError):
        ast_cache[file_path] = {}
        return {}


# ---- 流式命中分类 --------------------------------------------------------

def _classify_hit_streaming(
    fpath: str,
    lineno: int,
    snippet: str,
    keywords: list[str],
    repo: str,
    ast_cache: dict,
) -> str:
    """判断单条命中属于 definition / import / call / comment / reference。

    定义名必须与查询关键词精确匹配，不因 def 关键词出现而误判。
    优先使用 AST（按文件缓存），正则仅作为无 AST 时的回退。
    """
    stripped = snippet.strip()

    # 注释行快速排除
    if stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("--"):
        return "comment"

    # Python 文件：优先 AST（带缓存）
    if fpath.endswith(".py"):
        full_path = os.path.join(repo, fpath) if not os.path.isabs(fpath) else fpath
        ast_hits = _analyze_python_ast_cached(full_path, keywords, ast_cache)
        if lineno in ast_hits:
            return ast_hits[lineno]

    # 正则回退：只有定义名匹配才判为 definition
    defined_name = _parse_defined_name(stripped)
    if defined_name and _name_matches_keyword(defined_name, keywords):
        return "definition"

    # 导入
    if stripped.startswith(("import ", "from ")):
        return "import"

    # 调用
    if "(" in stripped:
        return "call"

    return "reference"


# ---- SearchTool ----------------------------------------------------------

@dataclass
class SearchTool:
    name: str = "search"

    def execute(self, request: ToolRequest) -> ToolResult:
        t0 = time.perf_counter()
        repo = request.params.get("repo_path", ".")
        query = request.params.get("query", "")
        search_type = request.params.get("search_type", "grep")
        max_results = request.params.get("max_results", 50)
        file_patterns = request.params.get("file_patterns", [])

        if not query:
            return ToolResult.failure(self.name, "SEARCH_EMPTY_QUERY", "搜索关键词为空")

        try:
            if search_type == "filename":
                result_data = self._search_files(repo, query, file_patterns, max_results)
            else:
                result_data = self._search_grep_stream(repo, query, file_patterns, max_results)

            artifacts, evidence = self._build_results_from_scored(
                result_data, search_type, max_results,
            )
            return ToolResult(
                tool=self.name,
                status="success",
                artifacts=artifacts,
                evidence=evidence,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return ToolResult.failure(self.name, "SEARCH_ERROR", str(e))

    # ---- 流式 git grep + Top-K 小根堆 -----------------------------------

    def _search_grep_stream(
        self, repo: str, query, file_patterns: list[str], max_results: int,
    ) -> dict:
        """用 git grep 流式扫描仓库，每条命中即时分类/评分，小根堆维护 Top-K。

        返回包含 scored_items 列表和统计信息的字典。
        """
        keywords = query if isinstance(query, list) else [query]
        keywords = list(dict.fromkeys(str(k) for k in keywords if k))
        if not keywords:
            return {"scored_items": [], "total_scanned": 0, "total_kept": 0,
                    "truncated": False, "truncated_reason": ""}

        args = ["git", "-C", repo, "grep", "-n", "-i"]
        for kw in keywords:
            args.extend(["-e", kw])
        args.append("--")
        if file_patterns:
            for p in file_patterns[:5]:
                args.append(p)

        candidate_limit = max(max_results * 4, 100)
        # Keep a single best line per file *before* the global Top-K. A lazy
        # heap gives bounded memory while allowing a later better hit in a
        # retained file to replace its earlier hit.
        heap: list[tuple[int, int, str]] = []  # (score, version, file)
        best_by_file: dict[str, tuple[int, int, dict]] = {}
        version_counter = 0

        total_bytes = 0
        lines_scanned = 0
        truncated = False
        truncated_reason = ""

        file_type_cache: dict[str, str] = {}
        ast_cache: dict[str, dict[int, str]] = {}

        try:
            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            return {"scored_items": [], "total_scanned": 0, "total_kept": 0,
                    "truncated": False, "truncated_reason": f"Popen failed: {e}"}

        try:
            for line_bytes in proc.stdout:
                total_bytes += len(line_bytes)
                if total_bytes > MAX_OUTPUT_BYTES:
                    truncated = True
                    truncated_reason = "max_output_bytes"
                    proc.kill()
                    break

                lines_scanned += 1
                if lines_scanned > MAX_LINES_SCANNED:
                    truncated = True
                    truncated_reason = "max_lines_scanned"
                    proc.kill()
                    break

                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                if not line.strip() or line.startswith("\x1b"):
                    continue

                # 解析 file:line:snippet
                parts = line.split(":", 2)
                if len(parts) < 2:
                    continue
                fpath = parts[0]
                try:
                    lineno = int(parts[1])
                    snippet = parts[2].strip()[:200] if len(parts) > 2 else ""
                except ValueError:
                    continue

                # 文件分类（按文件路径缓存）
                if fpath not in file_type_cache:
                    file_type_cache[fpath] = _classify_file(fpath)
                file_type = file_type_cache[fpath]

                # 命中类型分类
                hit_type = _classify_hit_streaming(
                    fpath, lineno, snippet, keywords, repo, ast_cache,
                )

                # 四维评分
                file_type_score = _FILE_TYPE_SCORES.get(file_type, 10)
                hit_type_score = _HIT_TYPE_SCORES.get(hit_type, 100)
                match_precision = _compute_match_precision(snippet, keywords)
                query_coverage = _compute_query_coverage(snippet, keywords)

                score = file_type_score + hit_type_score + match_precision + query_coverage
                matched_terms = _find_matched_terms(snippet, keywords)

                item = {
                    "file": fpath,
                    "line": lineno,
                    "snippet": snippet,
                    "file_type": file_type,
                    "hit_type": hit_type,
                    "score": score,
                    "matched_terms": matched_terms,
                }

                current = best_by_file.get(fpath)
                item_key = (-score, fpath, lineno, snippet)
                if current is not None:
                    current_item = current[2]
                    current_key = (-current[0], fpath, current_item["line"], current_item["snippet"])
                    if item_key >= current_key:
                        continue

                # Discard obsolete lazy entries before using the heap root as
                # the current global worst candidate.
                while heap:
                    heap_score, heap_version, heap_file = heap[0]
                    active = best_by_file.get(heap_file)
                    if active is not None and active[0] == heap_score and active[1] == heap_version:
                        break
                    heapq.heappop(heap)

                if current is None and len(best_by_file) >= candidate_limit:
                    worst_score, _, worst_file = heap[0]
                    worst_item = best_by_file[worst_file][2]
                    worst_key = (-worst_score, worst_file, worst_item["line"], worst_item["snippet"])
                    if item_key >= worst_key:
                        continue
                    del best_by_file[worst_file]
                    heapq.heappop(heap)

                version_counter += 1
                best_by_file[fpath] = (score, version_counter, item)
                heapq.heappush(heap, (score, version_counter, fpath))

                # Lazy replacement can leave stale heap entries. Compact them
                # periodically so a noisy file cannot grow memory unbounded.
                if len(heap) > max(candidate_limit * 3, 300):
                    heap = [
                        (saved_score, saved_version, file_path)
                        for file_path, (saved_score, saved_version, _) in best_by_file.items()
                    ]
                    heapq.heapify(heap)

        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

        # 确定性排序：score DESC, file ASC, line ASC, snippet ASC
        scored_items = [item for _, _, item in best_by_file.values()]
        scored_items.sort(key=lambda x: (-x["score"], x["file"], x["line"], x["snippet"]))

        return {
            "scored_items": scored_items,
            "total_scanned": lines_scanned,
            "total_kept": len(best_by_file),
            "truncated": truncated,
            "truncated_reason": truncated_reason,
        }

    # ---- 文件名搜索 -------------------------------------------------------

    def _search_files(self, repo: str, query, file_patterns: list[str],
                      max_results: int) -> dict:
        """git ls-files 文件名搜索。结果量通常不大，按 score 排序后截断。"""
        keywords = [query] if isinstance(query, str) else query
        keyword = keywords[0] if keywords else ""
        results: list[str] = []

        if file_patterns:
            for pattern in file_patterns[:5]:
                out = self._run(["git", "-C", repo, "ls-files", pattern])
                results.extend(out.split("\n"))
        else:
            out = self._run(["git", "-C", repo, "ls-files", f"*{keyword}*"])
            results = out.split("\n")

        # 构建简单 scored items（文件名搜索无 snippet/行号）
        items = []
        for r in results:
            r = r.strip()
            if not r:
                continue
            file_type = _classify_file(r)
            score = _FILE_TYPE_SCORES.get(file_type, 10)
            # 文件名包含关键词额外加分
            if keyword.lower() in os.path.basename(r).lower():
                score += 200
            items.append({
                "file": r, "line": 1, "snippet": r,
                "file_type": file_type, "hit_type": "reference",
                "score": score, "matched_terms": [keyword] if keyword else [],
            })

        items.sort(key=lambda x: (-x["score"], x["file"], x["line"]))
        return {
            "scored_items": items[:max_results * 4],
            "total_scanned": len(results),
            "total_kept": len(items),
            "truncated": False,
            "truncated_reason": "",
        }

    # ---- 结果构建 --------------------------------------------------------

    def _build_results_from_scored(
        self, result_data: dict, search_type: str, max_results: int,
    ) -> tuple[dict, list[Evidence]]:
        """从已评分的候选列表中按文件去重后截取 top max_results，构建 artifacts 和 Evidence。

        先去重（每文件保留最高分命中），再用剩余命中补足 max_results，
        避免单个文件的大量高分命中挤占其他文件的槽位。
        """
        scored_items = result_data.get("scored_items", [])

        # 按文件去重：优先每文件一条最高分命中，剩余补足
        seen_files: dict[str, dict] = {}  # file_path -> best item
        overflow: list[dict] = []
        for item in scored_items:
            f = item["file"]
            if f not in seen_files:
                seen_files[f] = item
            else:
                overflow.append(item)

        deduped = sorted(
            seen_files.values(),
            key=lambda x: (-x["score"], x["file"], x["line"], x["snippet"]),
        )
        top_items = deduped[:max_results]
        if len(top_items) < max_results:
            top_items.extend(overflow[:max_results - len(top_items)])

        files_seen: set[str] = set()
        evidence: list[Evidence] = []
        matches: list[dict] = []

        for item in top_items:
            fpath = item["file"]
            files_seen.add(fpath)

            hit_type = item.get("hit_type", "reference")
            confidence = HIT_CONFIDENCE.get(hit_type, 0.80)

            evidence.append(Evidence(
                kind="code",
                source="search",
                location=CodeLocation(file=fpath, start_line=item["line"]),
                snippet=item.get("snippet", ""),
                confidence=confidence,
            ))

            matches.append({
                "file": fpath,
                "line": item["line"],
                "snippet": item.get("snippet", ""),
                "file_type": item.get("file_type", ""),
                "hit_type": hit_type,
                "score": item.get("score", 0),
                "matched_terms": item.get("matched_terms", []),
            })

        return {
            "matches": matches,
            "files": sorted(files_seen),
            "total_count": len(matches),
            "total_kept": result_data.get("total_kept", len(matches)),
            "total_scanned": result_data.get("total_scanned", 0),
            "search_type": search_type,
            "ranked": True,
            "truncated": result_data.get("truncated", False),
            "truncated_reason": result_data.get("truncated_reason", ""),
        }, evidence

    # ---- 底层调用 --------------------------------------------------------

    @staticmethod
    def _run(args: list[str], timeout: int = 30) -> str:
        result = subprocess.run(args, capture_output=True, timeout=timeout)
        return result.stdout.decode("utf-8", errors="replace")
