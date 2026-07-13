"""DependencyTool — 依赖变更分析工具

分析 Python import 变更与依赖清单文件变更，产出 Finding + Evidence。
"""

import ast
import time
from dataclasses import dataclass

from app.models.evidence import Evidence
from app.models.finding import Finding
from app.models.location import CodeLocation
from app.tools.contract import Tool, ToolRequest, ToolResult

# 依赖清单文件名
_DEP_FILES = {"requirements.txt", "setup.py", "setup.cfg", "pyproject.toml", "Pipfile", "poetry.lock", "Pipfile.lock"}

# 标准库模块（Python 3.10+）
_STDLIB = {
    "abc", "aifc", "argparse", "array", "ast", "asynchat", "asyncio", "asyncore",
    "atexit", "audioop", "base64", "bdb", "binascii", "binhex", "bisect", "builtins",
    "bz2", "calendar", "cgi", "cgitb", "chunk", "cmath", "cmd", "code", "codecs",
    "codeop", "collections", "colorsys", "compileall", "concurrent", "configparser",
    "contextlib", "contextvars", "copy", "copyreg", "cProfile", "crypt", "csv",
    "ctypes", "curses", "dataclasses", "datetime", "dbm", "decimal", "difflib",
    "dis", "distutils", "doctest", "email", "encodings", "enum", "errno", "faulthandler",
    "fcntl", "filecmp", "fileinput", "fnmatch", "fractions", "ftplib", "functools",
    "gc", "getopt", "getpass", "gettext", "glob", "graphlib", "grp", "gzip", "hashlib",
    "heapq", "hmac", "html", "http", "idlelib", "imaplib", "imghdr", "imp", "importlib",
    "inspect", "io", "ipaddress", "itertools", "json", "keyword", "lib2to3", "linecache",
    "locale", "logging", "lzma", "mailbox", "mailcap", "marshal", "math", "mimetypes",
    "mmap", "modulefinder", "multiprocessing", "netrc", "nis", "nntplib", "numbers",
    "operator", "optparse", "os", "ossaudiodev", "pathlib", "pdb", "pickle", "pickletools",
    "pipes", "pkgutil", "platform", "plistlib", "poplib", "posix", "posixpath", "pprint",
    "profile", "pstats", "pty", "pwd", "py_compile", "pyclbr", "pydoc", "queue",
    "quopri", "random", "re", "readline", "reprlib", "resource", "rlcompleter",
    "runpy", "sched", "secrets", "select", "selectors", "shelve", "shlex", "shutil",
    "signal", "site", "smtpd", "smtplib", "sndhdr", "socket", "socketserver", "sqlite3",
    "ssl", "stat", "statistics", "string", "stringprep", "struct", "subprocess",
    "sunau", "symtable", "sys", "sysconfig", "syslog", "tabnanny", "tarfile", "telnetlib",
    "tempfile", "termios", "test", "textwrap", "threading", "time", "timeit", "tkinter",
    "token", "tokenize", "trace", "traceback", "tracemalloc", "tty", "turtle", "turtledemo",
    "types", "typing", "unicodedata", "unittest", "urllib", "uu", "uuid", "venv",
    "warnings", "wave", "weakref", "webbrowser", "winreg", "winsound", "wsgiref",
    "xdrlib", "xml", "xmlrpc", "zipapp", "zipfile", "zipimport", "zlib", "zoneinfo",
}


def _extract_imports(source: str) -> list[tuple[str, int, str]]:
    """从 Python 源码提取所有 import 语句。返回 [(module, line, kind), ...]
    kind 为 'import' / 'from' / 'from_relative'。"""
    results: list[tuple[str, int, str]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return results

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                results.append((top, node.lineno, "import"))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.level and node.level > 0:
                results.append((node.module, node.lineno, "from_relative"))
            else:
                top = node.module.split(".")[0]
                results.append((top, node.lineno, "from"))
    return results


@dataclass
class DependencyTool:
    name: str = "dependency"

    def execute(self, request: ToolRequest) -> ToolResult:
        t0 = time.perf_counter()
        files = request.params.get("files", [])  # list of (path, source)
        changed_files = request.params.get("changed_files", [])  # list of path strings

        findings: list[Finding] = []
        evidence: list[Evidence] = []

        try:
            # 1. 检查依赖清单文件
            for fpath in changed_files or []:
                basename = fpath.split("/")[-1]
                if basename in _DEP_FILES:
                    ev = Evidence(
                        kind="dependency", source="dependency",
                        location=CodeLocation(file=fpath, start_line=1),
                        snippet=f"依赖清单文件变更: {fpath}",
                        confidence=0.9,
                    )
                    evidence.append(ev)
                    findings.append(Finding(
                        tool="dependency", rule_id="DEP_FILE_CHANGED",
                        location=CodeLocation(file=fpath, start_line=1),
                        message=f"依赖清单文件变更: {basename}",
                        evidence_ids=[ev.id],
                    ))

            # 2. 分析 Python 文件的 import 变更
            for fpath, source in (files or []):
                imports = _extract_imports(source)
                external = [(mod, line, kind) for mod, line, kind in imports
                            if mod not in _STDLIB]

                for mod, line, kind in external:
                    ev = Evidence(
                        kind="dependency", source="dependency",
                        location=CodeLocation(file=fpath, start_line=line),
                        snippet=f"{kind} {mod} (line {line})",
                        confidence=0.8,
                    )
                    evidence.append(ev)

                if external:
                    mod_list = ", ".join(sorted(set(m for m, _, _ in external)))
                    findings.append(Finding(
                        tool="dependency", rule_id="EXTERNAL_IMPORT",
                        location=CodeLocation(file=fpath, start_line=external[0][1]),
                        message=f"外部依赖引用: {mod_list}",
                        severity="low",
                        evidence_ids=[e.id for e in evidence
                                      if e.location and e.location.file == fpath],
                    ))

            return ToolResult(
                tool=self.name, status="success",
                findings=findings, evidence=evidence,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return ToolResult.failure(self.name, "DEPENDENCY_ERROR", str(e))
