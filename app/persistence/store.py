"""本地 JSON 文件持久化 — 保存/加载 ReviewRun 到 runs/ 目录。"""

import json
import os
from datetime import datetime
from dataclasses import dataclass, field


RUNS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "runs")


@dataclass
class RunRecord:
    """一条已保存的审查运行摘要。"""
    run_id: str
    repo_url_or_path: str
    base_ref: str
    head_ref: str
    created_at: str
    risk_level: str
    issue_count: int
    duration_ms: float


class RunStore:
    """JSON 文件持久化存储。"""

    def __init__(self, runs_dir: str = RUNS_DIR):
        self.runs_dir = runs_dir
        os.makedirs(self.runs_dir, exist_ok=True)

    def save(self, run_id: str, output: dict) -> str:
        """保存一次审查运行的完整结果。返回文件路径。"""
        filepath = os.path.join(self.runs_dir, f"{run_id}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2, default=str)
        return filepath

    def load(self, run_id: str) -> dict | None:
        """按 run_id 加载审查结果（不存在时返回 None）。"""
        filepath = os.path.join(self.runs_dir, f"{run_id}.json")
        if not os.path.isfile(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_runs(self) -> list[RunRecord]:
        """列出所有已保存的运行摘要。"""
        records: list[RunRecord] = []
        if not os.path.isdir(self.runs_dir):
            return records
        for fname in sorted(os.listdir(self.runs_dir), reverse=True):
            if not fname.endswith(".json"):
                continue
            filepath = os.path.join(self.runs_dir, fname)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            run_id = data.get("run_id", fname.replace(".json", ""))
            records.append(RunRecord(
                run_id=run_id,
                repo_url_or_path=data.get("repo_url", ""),
                base_ref=data.get("base_ref", ""),
                head_ref=data.get("head_ref", ""),
                created_at=data.get("created_at", ""),
                risk_level=data.get("plan", {}).get("risk_level", "unknown"),
                issue_count=len(data.get("issues", [])),
                duration_ms=data.get("duration_ms", 0),
            ))
        return records

    def delete(self, run_id: str) -> bool:
        """删除一次运行记录。"""
        filepath = os.path.join(self.runs_dir, f"{run_id}.json")
        if os.path.isfile(filepath):
            os.remove(filepath)
            return True
        return False
