"""受控的前端仓库导入：浏览器文件夹和 GitHub HTTPS 地址。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4


class RepositoryImportManager:
    """只管理本服务创建的临时仓库，不接收客户端给出的本地路径。"""

    max_files = 500
    max_bytes = 50 * 1024 * 1024

    def __init__(self) -> None:
        self.root = Path(tempfile.gettempdir()) / "code_review_agent_imports"
        self.root.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, dict] = {}

    def import_files(self, files, paths_json: str, owner_id: str) -> dict:
        try:
            paths = json.loads(paths_json)
        except json.JSONDecodeError as exc:
            raise ValueError("本地文件路径格式无效") from exc
        if not isinstance(paths, list) or len(paths) != len(files):
            raise ValueError("上传文件和路径数量不匹配")
        if not files:
            raise ValueError("请选择一个包含代码文件的文件夹")

        repo_dir = self._new_dir()
        total = 0
        try:
            for upload, relative_path in zip(files, paths):
                relative = self._safe_relative_path(relative_path)
                data = upload.file.read()
                total += len(data)
                if total > self.max_bytes:
                    raise ValueError("导入内容超过 50 MB 限制")
                target = repo_dir / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            self._initialize_git(repo_dir)
        except Exception:
            shutil.rmtree(repo_dir, ignore_errors=True)
            raise
        return self._register(repo_dir, "local", f"本地文件夹（{len(files)} 个文件）", owner_id)

    def import_github(self, url: str, owner_id: str) -> dict:
        normalized = self._github_url(url)
        repo_dir = self._new_dir()
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", normalized, str(repo_dir)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                timeout=90, check=True,
            )
            count, total = self._directory_size(repo_dir)
            if count > self.max_files or total > self.max_bytes:
                raise ValueError("GitHub 仓库超过导入限制（500 个文件或 50 MB）")
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(repo_dir, ignore_errors=True)
            raise ValueError("GitHub 克隆超时") from exc
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(repo_dir, ignore_errors=True)
            raise ValueError(f"无法克隆 GitHub 仓库：{exc.stderr.strip()}") from exc
        except Exception:
            shutil.rmtree(repo_dir, ignore_errors=True)
            raise
        return self._register(repo_dir, "github", normalized, owner_id)

    def _new_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="repo_", dir=self.root))

    def _register(self, repo_dir: Path, source: str, label: str, owner_id: str) -> dict:
        repo_id = uuid4().hex
        record = {"repo_id": repo_id, "repo_path": str(repo_dir), "owner_id": owner_id, "source": source,
                  "label": label, "created_at": int(time.time())}
        self._records[repo_id] = record
        return {key: value for key, value in record.items() if key not in {"repo_path", "owner_id"}}

    def path_for_owner(self, repo_id: str, owner_id: str) -> str | None:
        record = self._records.get(repo_id)
        return record["repo_path"] if record and record["owner_id"] == owner_id else None

    @staticmethod
    def _safe_relative_path(value: object) -> Path:
        if not isinstance(value, str) or not value:
            raise ValueError("文件路径无效")
        path = PurePosixPath(value.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or any(part in ("", ".") for part in path.parts):
            raise ValueError(f"不安全的文件路径：{value}")
        return Path(*path.parts)

    @staticmethod
    def _github_url(url: str) -> str:
        value = (url or "").strip()
        if not value.startswith("https://github.com/"):
            raise ValueError("仅支持 https://github.com/ 的公开仓库地址")
        if any(ch.isspace() for ch in value) or value.count("/") < 4:
            raise ValueError("GitHub 仓库地址无效")
        return value.removesuffix("/")

    @staticmethod
    def _initialize_git(repo_dir: Path) -> None:
        for command in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "import@local"],
            ["git", "config", "user.name", "Code Review Import"],
            ["git", "add", "--", "."],
            ["git", "commit", "-q", "--allow-empty", "-m", "Imported local files"],
        ):
            subprocess.run(command, cwd=repo_dir, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, check=True, timeout=30)

    @staticmethod
    def _directory_size(root: Path) -> tuple[int, int]:
        count = total = 0
        for current, dirs, names in os.walk(root):
            dirs[:] = [name for name in dirs if name != ".git"]
            for name in names:
                item = Path(current) / name
                if item.is_symlink():
                    continue
                count += 1
                total += item.stat().st_size
        return count, total
