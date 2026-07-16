"""受控工作区 — 本地仓库的隔离分析副本

M1 确定性事实层的基础设施。为 GitTool 等静态工具提供只读、受限的文件访问。
"""

import os
import shutil
import subprocess
import tempfile
import io
import tarfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkspaceConfig:
    """工作区约束配置"""

    # 允许分析的文件扩展名（小写，含点号）
    allowed_extensions: tuple[str, ...] = (".py", ".pyi", ".pyx")
    # 单文件最大字节数
    max_file_bytes: int = 2 * 1024 * 1024  # 2 MB
    # 最多分析文件数
    max_files: int = 500
    # 快照允许的总字节数
    max_total_bytes: int = 50 * 1024 * 1024  # 50 MB
    # git archive 导出超时
    export_timeout_s: float = 60.0
    # 临时目录前缀
    tmp_prefix: str = "review_ws_"


@dataclass
class Workspace:
    """受控工作区：持有仓库在目标 commit 的隔离文件快照。"""

    repo_path: str                     # 原始仓库路径
    work_dir: str                      # 实际分析用目录
    head_ref: str = ""                 # 当前检出的 ref/commit
    config: WorkspaceConfig = field(default_factory=WorkspaceConfig)

    # ---- 核心操作 ----------------------------------------------------

    def list_files(self) -> list[str]:
        """列出工作区内被允许分析的文件（相对路径）。"""
        files: list[str] = []
        total_bytes = 0
        for root, _dirs, filenames in os.walk(self.work_dir):
            if len(files) >= self.config.max_files:
                break
            for name in filenames:
                ext = os.path.splitext(name)[1].lower()
                if ext not in self.config.allowed_extensions:
                    continue
                full = os.path.join(root, name)
                size = os.path.getsize(full)
                if size > self.config.max_file_bytes or total_bytes + size > self.config.max_total_bytes:
                    continue
                if len(files) >= self.config.max_files:
                    break
                rel = os.path.relpath(full, self.work_dir).replace("\\", "/")
                files.append(rel)
                total_bytes += size
        return sorted(files)

    def read_file(self, relative_path: str) -> str:
        """只读方式读取文件内容，编码自动检测。"""
        if os.path.isabs(relative_path):
            raise ValueError(f"不允许绝对路径: {relative_path}")
        full = os.path.join(self.work_dir, relative_path)
        # 安全检查：不允许 .. 跳出工作区
        real = os.path.realpath(full)
        real_root = os.path.realpath(self.work_dir)
        if os.path.commonpath([real, real_root]) != real_root:
            raise ValueError(f"路径越界: {relative_path}")
        if not os.path.isfile(real):
            raise ValueError(f"不是可读取文件: {relative_path}")
        if os.path.splitext(real)[1].lower() not in self.config.allowed_extensions:
            raise ValueError(f"不允许的文件类型: {relative_path}")
        if os.path.getsize(real) > self.config.max_file_bytes:
            raise ValueError(f"文件过大: {relative_path}")
        return Path(real).read_text(encoding="utf-8", errors="replace")

    def cleanup(self) -> None:
        """删除临时工作目录。"""
        if os.path.isdir(self.work_dir):
            shutil.rmtree(self.work_dir, ignore_errors=True)


class WorkspaceManager:
    """受控工作区管理器。

    职责：为一次审查创建隔离的文件快照，并施加安全约束。
    不执行目标仓库代码、不修改原仓库。
    """

    def __init__(self, config: WorkspaceConfig | None = None):
        self.config = config or WorkspaceConfig()

    # ---- 公开接口 ----------------------------------------------------

    def prepare(self, repo_path: str, head_ref: str | None = None) -> Workspace:
        """为本地仓库的目标 ref 创建工作区。

        Args:
            repo_path: 本地 git 仓库路径
            head_ref: 目标 branch/commit/tag；None 表示使用当前 HEAD

        Returns:
            Workspace: 就绪的隔离工作区
        """
        repo_path = os.path.abspath(repo_path)
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            raise ValueError(f"不是有效的 git 仓库: {repo_path}")

        head_ref = head_ref or "HEAD"
        work_dir = tempfile.mkdtemp(prefix=self.config.tmp_prefix)

        try:
            self._export_snapshot(repo_path, head_ref, work_dir)
        except Exception:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise

        ws = Workspace(
            repo_path=repo_path,
            work_dir=work_dir,
            head_ref=head_ref,
            config=self.config,
        )
        return ws

    # ---- 内部 --------------------------------------------------------

    def _export_snapshot(self, repo_path: str, ref: str, target_dir: str) -> None:
        """用 git archive 或 checkout-index 导出 ref 的文件快照。"""
        # 优先用 git archive（干净、只导出跟踪文件）
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "archive", "--format=tar", ref],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=self.config.export_timeout_s,
            )
            if len(result.stdout) > self.config.max_total_bytes:
                raise ValueError("仓库快照超过总大小上限")
            with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
                members = tar.getmembers()
                if len(members) > self.config.max_files:
                    raise ValueError("仓库快照文件数超过上限")
                total_size = sum(member.size for member in members if member.isfile())
                if total_size > self.config.max_total_bytes:
                    raise ValueError("仓库快照解压后超过总大小上限")
                root = os.path.realpath(target_dir)
                for member in members:
                    destination = os.path.realpath(os.path.join(target_dir, member.name))
                    if os.path.commonpath([root, destination]) != root or member.issym() or member.islnk():
                        raise ValueError("仓库快照包含不安全路径或链接")
                tar.extractall(target_dir, members=members)
        except Exception:
            # archive 失败不回退到未受限复制，避免绕开资源与路径约束。
            raise

    def _copy_snapshot(self, repo_path: str, target_dir: str) -> None:
        """回退方案：将工作树文件复制到临时目录（排除 .git）。"""
        total_bytes = 0
        copied_files = 0
        for root, dirs, files in os.walk(repo_path):
            dirs[:] = [d for d in dirs if d != ".git"]
            for name in files:
                src = os.path.join(root, name)
                if os.path.islink(src):
                    continue
                size = os.path.getsize(src)
                copied_files += 1
                total_bytes += size
                if copied_files > self.config.max_files or total_bytes > self.config.max_total_bytes:
                    raise ValueError("复制快照超过资源上限")
                rel = os.path.relpath(src, repo_path)
                dst = os.path.join(target_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
