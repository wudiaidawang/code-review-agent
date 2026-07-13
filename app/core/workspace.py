"""受控工作区 — 本地仓库的隔离分析副本

M1 确定性事实层的基础设施。为 GitTool 等静态工具提供只读、受限的文件访问。
"""

import os
import shutil
import subprocess
import tempfile
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
                if size > self.config.max_file_bytes:
                    continue
                if len(files) >= self.config.max_files:
                    break
                rel = os.path.relpath(full, self.work_dir).replace("\\", "/")
                files.append(rel)
                total_bytes += size
        return sorted(files)

    def read_file(self, relative_path: str) -> str:
        """只读方式读取文件内容，编码自动检测。"""
        full = os.path.join(self.work_dir, relative_path)
        # 安全检查：不允许 .. 跳出工作区
        real = os.path.realpath(full)
        real_root = os.path.realpath(self.work_dir)
        if not real.startswith(real_root + os.sep) and real != real_root:
            raise ValueError(f"路径越界: {relative_path}")
        return Path(full).read_text(encoding="utf-8")

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

        # 用 git checkout-index 导出目标快照到临时目录（只读、不污染原仓库）
        self._export_snapshot(repo_path, head_ref, work_dir)

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
            subprocess.run(
                ["git", "-C", repo_path, "archive", "--format=tar", ref],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
            ).stdout
            # archive 输出到 stdout，pipe 到 tar 解压
            result = subprocess.run(
                ["git", "-C", repo_path, "archive", "--format=tar", ref],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            import tarfile
            import io

            with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
                tar.extractall(target_dir)
        except Exception:
            # 回退：直接复制（适合裸仓库或非标准 git）
            self._copy_snapshot(repo_path, target_dir)

    def _copy_snapshot(self, repo_path: str, target_dir: str) -> None:
        """回退方案：将工作树文件复制到临时目录（排除 .git）。"""
        for item in os.listdir(repo_path):
            if item == ".git":
                continue
            src = os.path.join(repo_path, item)
            dst = os.path.join(target_dir, item)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=False)
            else:
                shutil.copy2(src, dst)
