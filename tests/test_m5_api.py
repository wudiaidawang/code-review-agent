"""M5 服务化 — API + CLI + 持久化测试。"""
import json
import os
import tempfile
from fastapi.testclient import TestClient
from app.api import create_app
from app.api.schemas import ReviewRequest
from app.persistence.store import RunStore, RunRecord


# ---- FastAPI TestClient ----

app = create_app()
client = TestClient(app)


class TestHealthCheck:
    def test_health_returns_ok(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestCreateReview:
    def test_review_success(self):
        """对当前仓库发起审查，验证返回结构。"""
        resp = client.post("/review", json={
            "repo_path": ".",
            "base_ref": "HEAD~2",
            "head_ref": "HEAD",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "run_id" in data
        assert data["run_id"].startswith("run_")
        assert "plan" in data
        assert "change_set" in data
        assert "issues" in data
        assert "evidence" in data
        assert "trace" in data
        assert "markdown" in data
        assert "json_report" in data
        assert data["duration_ms"] > 0

    def test_review_with_default_refs(self):
        """使用默认 base/head refs 发起审查。"""
        resp = client.post("/review", json={
            "repo_path": ".",
            "base_ref": "HEAD~1",
            "head_ref": "HEAD",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["base_ref"] == "HEAD~1"
        assert data["head_ref"] == "HEAD"

    def test_review_bad_repo_returns_500(self):
        """不存在的仓库路径返回 500 错误。"""
        resp = client.post("/review", json={
            "repo_path": "/nonexistent/path/xyz",
            "base_ref": "HEAD~1",
            "head_ref": "HEAD",
        })
        assert resp.status_code == 500
        err = resp.json()
        assert "error" in err


class TestGetReview:
    def test_get_review_after_create(self):
        """创建后可通过 run_id 查询。"""
        # 先创建
        resp = client.post("/review", json={
            "repo_path": ".",
            "base_ref": "HEAD~1",
            "head_ref": "HEAD",
        })
        run_id = resp.json()["run_id"]

        # 查询
        resp2 = client.get(f"/review/{run_id}")
        assert resp2.status_code == 200
        assert resp2.json()["run_id"] == run_id

    def test_get_review_not_found(self):
        """不存在的 run_id 返回 404。"""
        resp = client.get("/review/run_nonexistent_12345")
        assert resp.status_code == 404
        err = resp.json()
        assert "error" in err

    def test_list_runs(self):
        """列出所有历史运行。"""
        resp = client.get("/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert "runs" in data
        assert "total" in data
        assert isinstance(data["runs"], list)

    def test_review_end_to_end_has_issues_in_own_repo(self):
        """端到端：审查本项目自身应检出 ruff/bandit 发现。"""
        resp = client.post("/review", json={
            "repo_path": ".",
            "base_ref": "HEAD~5",
            "head_ref": "HEAD",
        })
        assert resp.status_code == 200
        data = resp.json()
        # 对本项目来说，至少应有 git 变更和 trace 记录
        assert len(data["trace"]) > 0
        assert len(data["evidence"]) > 0
        # markdown 报告应包含基本章节
        assert "Change Summary" in data["markdown"]
        assert "Issue" in data["markdown"]


# ---- Persistence ----

class TestRunStore:
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RunStore(runs_dir=tmpdir)
            data = {"run_id": "test_001", "plan": {"risk_level": "high"}, "issues": []}
            path = store.save("test_001", data)
            assert os.path.isfile(path)

            loaded = store.load("test_001")
            assert loaded is not None
            assert loaded["run_id"] == "test_001"
            assert loaded["plan"]["risk_level"] == "high"

    def test_load_nonexistent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RunStore(runs_dir=tmpdir)
            assert store.load("nonexistent") is None

    def test_list_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RunStore(runs_dir=tmpdir)
            store.save("run_1", {"run_id": "run_1", "plan": {"risk_level": "low"}, "issues": [],
                                  "repo_url": ".", "base_ref": "HEAD~1", "head_ref": "HEAD",
                                  "created_at": "2026-01-01", "duration_ms": 100})
            store.save("run_2", {"run_id": "run_2", "plan": {"risk_level": "high"}, "issues": [{}, {}],
                                  "repo_url": "/x", "base_ref": "HEAD~3", "head_ref": "HEAD",
                                  "created_at": "2026-01-02", "duration_ms": 200})

            records = store.list_runs()
            assert len(records) == 2
            # 最新在前
            assert records[0].run_id == "run_2"
            assert records[0].risk_level == "high"
            assert records[0].issue_count == 2

    def test_delete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RunStore(runs_dir=tmpdir)
            store.save("run_x", {"run_id": "run_x"})
            assert store.load("run_x") is not None
            assert store.delete("run_x") is True
            assert store.load("run_x") is None

    def test_delete_nonexistent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RunStore(runs_dir=tmpdir)
            assert store.delete("no_such") is False

    def test_list_skips_corrupt_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RunStore(runs_dir=tmpdir)
            # 写入损坏的 JSON
            with open(os.path.join(tmpdir, "corrupt.json"), "w") as f:
                f.write("not json {{{")
            # 写入正常文件
            store.save("ok", {"run_id": "ok", "plan": {}, "issues": [],
                                "repo_url": "", "base_ref": "", "head_ref": "",
                                "created_at": "", "duration_ms": 0})
            records = store.list_runs()
            assert len(records) == 1
            assert records[0].run_id == "ok"


# ---- Schemas ----

class TestSchemas:
    def test_review_request_defaults(self):
        req = ReviewRequest(repo_path=".")
        assert req.base_ref == "HEAD~1"
        assert req.head_ref == "HEAD"

    def test_review_request_explicit(self):
        req = ReviewRequest(repo_path="/tmp/repo", base_ref="main~3", head_ref="main")
        assert req.base_ref == "main~3"


# ---- CLI smoke ----

class TestCLI:
    def test_cli_module_imports(self):
        """CLI 模块可导入且包含 main 函数。"""
        from app.cli import main
        assert callable(main)

    def test_cli_review_integration(self, capsys):
        """CLI review 命令可运行并输出报告。"""
        from app.cli import cmd_review
        import argparse

        ns = argparse.Namespace(
            repo=".",
            base="HEAD~1",
            head="HEAD",
            output=None,
            json=None,
        )
        output = cmd_review(ns)
        assert output is not None
        assert len(output.markdown) > 0
        assert output.duration_ms > 0
