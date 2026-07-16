"""共享测试工具 — 固定 change_set、mock GitTool。

确保无论仓库实际提交历史如何，测试输入永远一致。
"""

from app.tools.contract import ToolRequest, ToolResult
from app.models.evidence import Evidence
from app.models.location import CodeLocation

# 固定的变更集，触发全部静态分析工具：
#   git + python_ast + ruff + bandit + dependency
# 原因：2 个 Python 文件（≤50 → AST），requirements.txt（→ dependency），
#       "auth" 路径触发 auth_change 风险信号（→ bandit）
FIXED_CHANGESET = {
    "files": [
        {
            "path": "auth.py",
            "change_type": "modified",
            "added_lines": 60,
            "deleted_lines": 15,
            "hunks": [
                {"old_start": 10, "old_lines": 5, "new_start": 10, "new_lines": 20}
            ],
        },
        {
            "path": "utils.py",
            "change_type": "modified",
            "added_lines": 30,
            "deleted_lines": 5,
            "hunks": [
                {"old_start": 20, "old_lines": 3, "new_start": 20, "new_lines": 10}
            ],
        },
        {
            "path": "requirements.txt",
            "change_type": "modified",
            "added_lines": 3,
            "deleted_lines": 1,
            "hunks": [
                {"old_start": 5, "old_lines": 1, "new_start": 5, "new_lines": 2}
            ],
        },
    ]
}

FIXED_UNIFIED_DIFF = """diff --git a/auth.py b/auth.py
index 1111111..2222222 100644
--- a/auth.py
+++ b/auth.py
@@ -10,5 +10,6 @@
 def login(user):
+    return user
diff --git a/utils.py b/utils.py
index 1111111..2222222 100644
--- a/utils.py
+++ b/utils.py
@@ -20,3 +20,4 @@
 def helper():
+    return True
"""

# 固定 change_set 触发的预期工具列表
EXPECTED_ANALYZERS = ["git", "python_ast", "ruff", "bandit", "dependency"]


def mock_git_execute(self, request: ToolRequest) -> ToolResult:
    """返回固定 change_set 的 GitTool.execute 替代。"""
    return ToolResult(
        tool="git",
        status="success",
        artifacts={"change_set": FIXED_CHANGESET, "unified_diff": FIXED_UNIFIED_DIFF},
        evidence=[
            Evidence(
                kind="change",
                source="git",
                location=CodeLocation(file=f["path"], start_line=0),
                snippet=f"{f['change_type']}: {f['path']}",
                confidence=1.0,
            )
            for f in FIXED_CHANGESET["files"]
        ],
    )


def patch_git_tool(monkeypatch):
    """替换 GitTool.execute 为固定返回的 mock。"""
    from app.tools.git_tool import GitTool

    monkeypatch.setattr(GitTool, "execute", mock_git_execute)
