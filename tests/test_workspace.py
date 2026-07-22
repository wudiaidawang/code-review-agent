"""WorkspaceManager 单元测试 — 完全离线，用本仓库自身为测试目标。"""
import os
from app.core.workspace import WorkspaceManager, WorkspaceConfig


class TestWorkspaceManager:
    """基础功能测试，用当前项目仓库作为测试目标（确保是 git repo）。"""

    def test_prepare_and_list_files(self):
        mgr = WorkspaceManager(WorkspaceConfig(tmp_prefix="tws_"))
        ws = mgr.prepare(".")
        try:
            files = ws.list_files()
            assert len(files) > 0
            assert all(f.endswith((".py", ".pyi", ".pyx")) for f in files)
            assert len(files) <= ws.config.max_files
        finally:
            ws.cleanup()

    def test_read_file(self):
        mgr = WorkspaceManager(WorkspaceConfig(tmp_prefix="tws_"))
        ws = mgr.prepare(".")
        try:
            files = ws.list_files()
            content = ws.read_file(files[0])
            assert isinstance(content, str)
        finally:
            ws.cleanup()

    def test_read_file_at_ref_does_not_require_full_snapshot(self):
        # A sparse read is valid even when a full snapshot would exceed the
        # repository file-count cap.
        mgr = WorkspaceManager(WorkspaceConfig(max_files=1, tmp_prefix="tws_"))
        content = mgr.read_file_at_ref(".", "HEAD", "app/core/workspace.py")
        assert "class WorkspaceManager" in content

    def test_read_file_at_ref_rejects_unsafe_or_unsupported_paths(self):
        mgr = WorkspaceManager(WorkspaceConfig(tmp_prefix="tws_"))
        for path in ("../../etc/passwd", "C:/Windows/system32.py", "README.md"):
            try:
                mgr.read_file_at_ref(".", "HEAD", path)
                assert False, f"should reject {path}"
            except ValueError:
                pass

    def test_read_file_at_ref_enforces_single_file_limit(self):
        mgr = WorkspaceManager(WorkspaceConfig(max_file_bytes=1, tmp_prefix="tws_"))
        try:
            mgr.read_file_at_ref(".", "HEAD", "app/core/workspace.py")
            assert False, "should reject an oversized file"
        except ValueError:
            pass

    def test_path_traversal_blocked(self):
        mgr = WorkspaceManager(WorkspaceConfig(tmp_prefix="tws_"))
        ws = mgr.prepare(".")
        try:
            try:
                ws.read_file("../../etc/passwd")
                assert False, "应该拒绝越界路径"
            except ValueError:
                pass
        finally:
            ws.cleanup()

    def test_non_git_repo_raises(self):
        mgr = WorkspaceManager(WorkspaceConfig(tmp_prefix="tws_"))
        import tempfile
        tmp = tempfile.mkdtemp()
        try:
            try:
                mgr.prepare(tmp)
                assert False, "非 git 仓库应该抛错"
            except ValueError:
                pass
        finally:
            import shutil
            shutil.rmtree(tmp)

    def test_cleanup_removes_work_dir(self):
        mgr = WorkspaceManager(WorkspaceConfig(tmp_prefix="tws_"))
        ws = mgr.prepare(".")
        work_dir = ws.work_dir
        assert os.path.isdir(work_dir)
        ws.cleanup()
        assert not os.path.isdir(work_dir)
