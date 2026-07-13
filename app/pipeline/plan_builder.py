"""RuleBasedPlanBuilder — 基于变更特征的确定性工具选择

M2 核心：同输入 → 同计划。不调 LLM，规则透明可解释。
"""

from app.models.plan import ReviewPlan

# 风险信号 → reason_code 映射
_RISK_PATTERNS: dict[str, list[str]] = {
    "auth_change": ["auth", "password", "token", "secret", "credential", "login", "session", "jwt", "oauth"],
    "sql_risk": ["sql", "query", "execute", "cursor", "psycopg", "sqlite", "mysql"],
    "command_injection": ["eval(", "exec(", "subprocess", "os.system", "popen", "__import__", "compile("],
    "deserialization": ["pickle", "yaml.load", "xml.etree", "deserialize", "marshal"],
    "dependency_change": ["requirements.txt", "setup.py", "setup.cfg", "pyproject.toml", "Pipfile", "poetry.lock"],
}


class RuleBasedPlanBuilder:
    """规则式计划生成器。输入 ChangeSet → 输出 ReviewPlan。"""

    def build(self, change_set: dict, file_contents: dict[str, str] | None = None) -> ReviewPlan:
        """根据变更集生成审查计划。

        Args:
            change_set: GitTool 产出的 ChangeSet dict
            file_contents: 可选，变更文件的完整内容（用于风险扫描）；不给则只按文件名和 hunk 统计判断
        """
        files = change_set.get("files", [])
        py_files = [f for f in files if f["path"].endswith(".py") and f.get("change_type") != "deleted"]

        analyzers = ["git"]
        reason_codes: list[str] = []
        risk_signals: set[str] = set()

        # 规则 1：Python 文件 → Python 相关工具
        if py_files:
            total_added = sum(f.get("added_lines", 0) for f in py_files)
            total_deleted = sum(f.get("deleted_lines", 0) for f in py_files)

            # AST：文件数不多时开启（太多时跳过以节省时间）
            if len(py_files) <= 50:
                analyzers.append("python_ast")
            else:
                reason_codes.append("python_ast_skipped_large_diff")

            analyzers.append("ruff")

            # 风险信号扫描
            if file_contents:
                risk_signals = self._scan_risks(file_contents, py_files)
            else:
                # 降级：仅按文件名与变更行数判断
                risk_signals = self._scan_risks_fast(files, total_added + total_deleted)

            # 安全硬规则：高风险信号时 bandit 不可跳过
            if risk_signals:
                analyzers.append("bandit")
                reason_codes.extend(sorted(risk_signals))
                if risk_signals & {"auth_change", "command_injection", "deserialization"}:
                    analyzers.append("bandit")  # 去重由 set 保证
            elif total_added + total_deleted > 100:
                # 变更量大也跑 bandit
                analyzers.append("bandit")
            else:
                reason_codes.append("bandit_skipped_low_risk")

            # 规则 1b：依赖文件变更 → 加 dependency 分析
            dep_files = {"requirements.txt", "setup.py", "setup.cfg", "pyproject.toml", "Pipfile", "poetry.lock"}
            if any(f["path"].split("/")[-1] in dep_files for f in files):
                analyzers.append("dependency")
                reason_codes.append("dependency_change")

        # 规则 2：非 Python 文件 → 仅 git
        if not py_files and len(files) > 0:
            reason_codes.append("no_python_changes")

        # 风险等级
        risk_level = self._calc_risk_level(risk_signals)

        return ReviewPlan(
            analyzers=list(dict.fromkeys(analyzers)),  # 保序去重
            enable_rag=False,
            enable_llm_semantic_review=False,
            risk_level=risk_level,
            reason_codes=reason_codes,
        )

    # ---- 风险扫描 --------------------------------------------------

    def _scan_risks(self, file_contents: dict[str, str], py_files: list[dict]) -> set[str]:
        """全内容扫描：对变更的 Python 文件检查风险关键词。"""
        signals: set[str] = set()
        for f in py_files:
            content = file_contents.get(f["path"], "")
            if not content:
                continue
            lower = content.lower()
            for code, keywords in _RISK_PATTERNS.items():
                if any(kw in lower for kw in keywords):
                    signals.add(code)
        # dependency_change 也检查文件名
        for f in py_files:
            for kw in _RISK_PATTERNS["dependency_change"]:
                if kw in f["path"].lower():
                    signals.add("dependency_change")
        return signals

    def _scan_risks_fast(self, files: list[dict], total_lines: int) -> set[str]:
        """快速风险扫描：仅按文件名判断（无文件内容时使用）。"""
        signals: set[str] = set()
        all_paths = " ".join(f["path"].lower() for f in files)
        for code, keywords in _RISK_PATTERNS.items():
            # 仅检查文件名中含的关键词（如 auth.py、requirements.txt）
            if code == "dependency_change":
                if any(kw in all_paths for kw in keywords):
                    signals.add(code)
        if total_lines > 500:
            # 大变更默认视为有一定风险
            pass
        return signals

    @staticmethod
    def _calc_risk_level(signals: set[str]) -> str:
        if len(signals) >= 3:
            return "high"
        if len(signals) >= 1:
            return "medium"
        return "low"
