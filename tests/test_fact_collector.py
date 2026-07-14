"""FactCollector 集成测试 — 端到端确定性工具链。"""
from app.pipeline.fact_collector import FactCollector


class TestFactCollector:
    def test_collect_on_self(self):
        """用本仓库自身做端到端验证。"""
        fc = FactCollector()
        result = fc.collect(".", "HEAD~2", "HEAD")
        # git 始终在，其他工具取决于变更文件类型
        assert "git" in result.tool_results
        assert result.tool_results["git"].ok(), "git failed"
        for name in ("python_ast", "ruff", "bandit"):
            if name in result.tool_results:
                assert result.tool_results[name].ok(), f"{name} failed"
        # 有变更文件和证据
        assert len(result.change_set.get("files", [])) > 0
        assert len(result.evidence) > 0

    def test_empty_range(self):
        """HEAD vs HEAD 无变更，所有工具应成功但不产出 findings。"""
        fc = FactCollector()
        result = fc.collect(".", "HEAD", "HEAD")
        files = result.change_set.get("files", [])
        assert files == []
        # git 仍然成功
        assert result.tool_results["git"].ok()

    def test_all_findings_have_evidence(self):
        """M1 验收：每个 Finding 必须有 evidence_ids。"""
        fc = FactCollector()
        result = fc.collect(".", "HEAD~2", "HEAD")
        for f in result.findings:
            assert f.evidence_ids, f"{f.rule_id} 缺少 evidence_ids"
            assert len(f.evidence_ids) >= 1

    def test_all_findings_have_location(self):
        """M1 验收：每个 Finding 必须有代码位置。"""
        fc = FactCollector()
        result = fc.collect(".", "HEAD~2", "HEAD")
        for f in result.findings:
            assert f.location is not None, f"{f.rule_id} 缺少 location"
            assert f.location.file, f"{f.rule_id} location 缺 file"
