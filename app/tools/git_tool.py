"""GitTool — 将 git 变更输出为结构化 ChangeSet + Evidence

实现 Tool 协议，产出 artifacts 中的 "change_set" 供下游所有工具消费。
"""

import re
import subprocess
import time
from dataclasses import dataclass

from app.models.change import ChangeSet, FileChange, Hunk
from app.models.evidence import Evidence
from app.models.location import CodeLocation
from app.tools.contract import Tool, ToolRequest, ToolResult


@dataclass
class GitTool:
    name: str = "git"

    def execute(self, request: ToolRequest) -> ToolResult:
        t0 = time.perf_counter()
        repo = request.params.get("repo_path", ".")
        base = request.params.get("base_ref", "HEAD~1")
        head = request.params.get("head_ref", "HEAD")

        try:
            changeset = self._diff(repo, base, head)
            evidence = self._build_evidence(changeset)
            return ToolResult(
                tool=self.name,
                status="success",
                artifacts={"change_set": changeset.to_dict()},
                evidence=evidence,
                duration_ms=(time.perf_counter() - t0) * 1000,
            )
        except Exception as e:
            return ToolResult.failure(self.name, "GIT_ERROR", str(e))

    # ---- diff 解析 --------------------------------------------------

    def _diff(self, repo: str, base: str, head: str) -> ChangeSet:
        files = self._changed_files(repo, base, head)
        cs = ChangeSet(base=base, head=head, files=files)
        return cs

    def _changed_files(self, repo: str, base: str, head: str) -> list[FileChange]:
        # --name-status 获取文件变更类型
        status_out = self._git(repo, ["diff", "--name-status", "--diff-filter=ACDMR", f"{base}...{head}"])
        # --numstat 获取行数增减
        numstat_out = self._git(repo, ["diff", "--numstat", f"{base}...{head}"])
        # 完整的 unified diff 用于提取 hunk 行号
        diff_out = self._git(repo, ["diff", "--unified=3", f"{base}...{head}"])

        status_lines = [l for l in status_out.split("\n") if l.strip()]
        numstat_lines = [l for l in numstat_out.split("\n") if l.strip()]

        files: list[FileChange] = []
        for i, line in enumerate(status_lines):
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            change_type = self._map_status(parts[0][0])
            path = parts[-1]
            old_path = parts[1] if change_type == "renamed" and len(parts) >= 3 else ""

            added, deleted = 0, 0
            if i < len(numstat_lines):
                ns = numstat_lines[i].split("\t")
                if len(ns) >= 2:
                    try:
                        added = int(ns[0]) if ns[0] != "-" else 0
                        deleted = int(ns[1]) if ns[1] != "-" else 0
                    except ValueError:
                        pass

            hunks = self._parse_hunks(diff_out, path)
            files.append(FileChange(
                path=path, change_type=change_type, old_path=old_path,
                added_lines=added, deleted_lines=deleted, hunks=hunks,
            ))
        return files

    def _parse_hunks(self, diff_text: str, target_path: str) -> list[Hunk]:
        """从 unified diff 中提取指定文件的 hunk 行号映射。"""
        hunks: list[Hunk] = []
        in_target = False
        for line in diff_text.split("\n"):
            if line.startswith("diff --git"):
                in_target = target_path in line
                continue
            if not in_target:
                continue
            m = re.match(r"^@@ -(\d+),?(\d*) \+(\d+),?(\d*) @@", line)
            if m:
                hunks.append(Hunk(
                    old_start=int(m.group(1)),
                    old_lines=int(m.group(2) or 1),
                    new_start=int(m.group(3)),
                    new_lines=int(m.group(4) or 1),
                ))
        return hunks

    def _map_status(self, code: str) -> str:
        return {"A": "added", "M": "modified", "D": "deleted", "R": "renamed", "C": "modified"}.get(code, "modified")

    # ---- 证据构建 ----------------------------------------------------

    def _build_evidence(self, cs: ChangeSet) -> list[Evidence]:
        return [
            Evidence(
                kind="change", source="git",
                location=CodeLocation(file=f.path, start_line=0),
                snippet=f"{f.change_type}: {f.path} (+{f.added_lines}/-{f.deleted_lines})",
                confidence=1.0,
            )
            for f in cs.files
        ]

    # ---- git 调用 ----------------------------------------------------

    @staticmethod
    def _git(repo: str, args: list[str]) -> str:
        result = subprocess.run(
            ["git", "-C", repo, "-c", "i18n.logOutputEncoding=UTF-8"] + args,
            capture_output=True, timeout=60,
        )
        # 强制 UTF-8 解码；replace 处理极端情况
        return result.stdout.decode("utf-8", errors="replace")
