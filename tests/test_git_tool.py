"""GitTool 单元测试 — 用本仓库 git 历史作为测试数据。"""
from app.tools.git_tool import GitTool
from app.tools.contract import ToolRequest


class TestGitTool:
    """用本仓库自身验证 GitTool（无网络依赖）。"""

    def test_diff_two_commits(self):
        gt = GitTool()
        result = gt.execute(ToolRequest(tool="git", params={
            "repo_path": ".", "base_ref": "HEAD~2", "head_ref": "HEAD",
        }))
        assert result.ok()
        cs = result.artifacts["change_set"]
        assert cs["base"] == "HEAD~2"
        assert cs["head"] == "HEAD"
        assert len(cs["files"]) > 0
        # 每条 Evidence 的 file 必须与 ChangeSet 一致
        ev_paths = {e.location.file for e in result.evidence}
        cs_paths = {f["path"] for f in cs["files"]}
        assert ev_paths == cs_paths

    def test_empty_diff(self):
        """HEAD vs HEAD 无变更，应返回空 ChangeSet 但不失败。"""
        gt = GitTool()
        result = gt.execute(ToolRequest(tool="git", params={
            "repo_path": ".", "base_ref": "HEAD", "head_ref": "HEAD",
        }))
        assert result.ok()
        cs = result.artifacts["change_set"]
        assert cs["files"] == []

    def test_file_change_types(self):
        """验证 change_type 在合法取值内。"""
        gt = GitTool()
        result = gt.execute(ToolRequest(tool="git", params={
            "repo_path": ".", "base_ref": "HEAD~2", "head_ref": "HEAD",
        }))
        from app.models.change import CHANGE_TYPES
        for f in result.artifacts["change_set"]["files"]:
            assert f["change_type"] in CHANGE_TYPES

    def test_failure_on_bad_ref(self):
        gt = GitTool()
        result = gt.execute(ToolRequest(tool="git", params={
            "repo_path": ".", "base_ref": "deadbeef", "head_ref": "HEAD",
        }))
        # 坏 ref 会导致 git diff 返回空或失败，至少不应抛异常
        assert isinstance(result.status, str)
