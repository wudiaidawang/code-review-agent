"""M4 评测数据集 — 版本化样本集，从审查 trace 构建 ground truth

格式对齐计划书 M4 微调任务定义：
输入：变更摘要/文件类型/diff规模/风险特征/AST摘要/已有发现
输出：analyzers/enable_rag/enable_llm/risk_level/reason_codes
"""

import json
from dataclasses import dataclass, field


@dataclass
class EvalSample:
    """一条评测样本。"""
    id: str
    scenario: str
    input: dict       # change_summary/file_types/diff_size/risk_signals/ast_summary/static_findings
    ground_truth: dict  # analyzers/risk_level/reason_codes


# ---- 样本数据集（手工标注，基于真实审查场景） ----

_SAMPLES: list[dict] = [
    {
        "id": "s001_simple_python",
        "scenario": "简单 Python 脚本变更（小 diff，无风险信号）",
        "input": {
            "change_summary": "修改 1 个 Python 文件，添加 5 行代码",
            "file_types": [".py"],
            "diff_size": {"files": 1, "added_lines": 5, "deleted_lines": 2},
            "risk_signals": [],
            "ast_summary": "0 functions modified",
            "static_findings_count": 0,
        },
        "ground_truth": {
            "analyzers": ["git", "python_ast", "ruff"],
            "risk_level": "low",
            "reason_codes": [],
        },
    },
    {
        "id": "s002_auth_change",
        "scenario": "认证模块变更（auth/password/token 关键词）",
        "input": {
            "change_summary": "修改 auth.py，修改 login() 函数，涉及 token 验证逻辑",
            "file_types": [".py"],
            "diff_size": {"files": 2, "added_lines": 80, "deleted_lines": 30},
            "risk_signals": ["auth_change"],
            "ast_summary": "1 function modified (login), 1 class modified (AuthService)",
            "static_findings_count": 2,
        },
        "ground_truth": {
            "analyzers": ["git", "python_ast", "ruff", "bandit"],
            "risk_level": "medium",
            "reason_codes": ["auth_change"],
        },
    },
    {
        "id": "s003_sql_injection",
        "scenario": "SQL 查询变更（sql/query/cursor 关键词）",
        "input": {
            "change_summary": "修改 db.py，新增 execute_query() 函数，拼接 SQL 字符串",
            "file_types": [".py"],
            "diff_size": {"files": 1, "added_lines": 45, "deleted_lines": 10},
            "risk_signals": ["sql_risk"],
            "ast_summary": "1 function added (execute_query)",
            "static_findings_count": 1,
        },
        "ground_truth": {
            "analyzers": ["git", "python_ast", "ruff", "bandit"],
            "risk_level": "medium",
            "reason_codes": ["sql_risk"],
        },
    },
    {
        "id": "s004_command_injection",
        "scenario": "命令执行变更（eval/subprocess/os.system 关键词）",
        "input": {
            "change_summary": "修改 worker.py，新增 subprocess.run 调用处理用户输入",
            "file_types": [".py"],
            "diff_size": {"files": 1, "added_lines": 30, "deleted_lines": 5},
            "risk_signals": ["command_injection"],
            "ast_summary": "1 function modified (process_job)",
            "static_findings_count": 1,
        },
        "ground_truth": {
            "analyzers": ["git", "python_ast", "ruff", "bandit"],
            "risk_level": "medium",
            "reason_codes": ["command_injection"],
        },
    },
    {
        "id": "s005_multi_risk",
        "scenario": "多风险叠加（认证 + SQL + 命令执行）",
        "input": {
            "change_summary": "修改 admin.py，新增认证中间件、SQL 查询、subprocess 调用",
            "file_types": [".py"],
            "diff_size": {"files": 3, "added_lines": 200, "deleted_lines": 80},
            "risk_signals": ["auth_change", "sql_risk", "command_injection"],
            "ast_summary": "2 functions added, 1 class added",
            "static_findings_count": 5,
        },
        "ground_truth": {
            "analyzers": ["git", "python_ast", "ruff", "bandit"],
            "risk_level": "high",
            "reason_codes": ["auth_change", "command_injection", "sql_risk"],
        },
    },
    {
        "id": "s006_non_python",
        "scenario": "非 Python 文件变更（只有 Markdown）",
        "input": {
            "change_summary": "修改 README.md 和 CHANGELOG.md",
            "file_types": [".md"],
            "diff_size": {"files": 2, "added_lines": 50, "deleted_lines": 20},
            "risk_signals": [],
            "ast_summary": "",
            "static_findings_count": 0,
        },
        "ground_truth": {
            "analyzers": ["git"],
            "risk_level": "low",
            "reason_codes": ["no_python_changes"],
        },
    },
    {
        "id": "s007_large_diff",
        "scenario": "大规模 Python 重构（>50 个文件）",
        "input": {
            "change_summary": "大规模重构，重命名 60+ 文件中的 import 路径",
            "file_types": [".py"],
            "diff_size": {"files": 65, "added_lines": 500, "deleted_lines": 480},
            "risk_signals": [],
            "ast_summary": "大量 import 变更",
            "static_findings_count": 20,
        },
        "ground_truth": {
            "analyzers": ["git", "ruff", "bandit"],
            "risk_level": "medium",
            "reason_codes": ["python_ast_skipped_large_diff"],
        },
    },
    {
        "id": "s008_deserialization",
        "scenario": "反序列化风险（pickle/yaml.load 关键词）",
        "input": {
            "change_summary": "新增 data_loader.py，使用 pickle.load 加载外部数据",
            "file_types": [".py"],
            "diff_size": {"files": 1, "added_lines": 25, "deleted_lines": 0},
            "risk_signals": ["deserialization"],
            "ast_summary": "1 function added (load_data)",
            "static_findings_count": 1,
        },
        "ground_truth": {
            "analyzers": ["git", "python_ast", "ruff", "bandit"],
            "risk_level": "medium",
            "reason_codes": ["deserialization"],
        },
    },
    {
        "id": "s009_dependency_change",
        "scenario": "依赖文件变更（requirements.txt/pyproject.toml）",
        "input": {
            "change_summary": "更新 pyproject.toml，添加 3 个新依赖",
            "file_types": [".toml", ".py"],
            "diff_size": {"files": 2, "added_lines": 15, "deleted_lines": 5},
            "risk_signals": ["dependency_change"],
            "ast_summary": "0 Python changes",
            "static_findings_count": 0,
        },
        "ground_truth": {
            "analyzers": ["git", "ruff", "dependency"],
            "risk_level": "low",
            "reason_codes": ["dependency_change"],
        },
    },
    {
        "id": "s010_empty_change",
        "scenario": "空变更（HEAD vs HEAD）",
        "input": {
            "change_summary": "无变更",
            "file_types": [],
            "diff_size": {"files": 0, "added_lines": 0, "deleted_lines": 0},
            "risk_signals": [],
            "ast_summary": "",
            "static_findings_count": 0,
        },
        "ground_truth": {
            "analyzers": ["git"],
            "risk_level": "low",
            "reason_codes": [],
        },
    },
]


def load_samples() -> list[EvalSample]:
    """加载评测数据集。"""
    return [
        EvalSample(
            id=s["id"],
            scenario=s["scenario"],
            input=s["input"],
            ground_truth=s["ground_truth"],
        )
        for s in _SAMPLES
    ]


def to_json(samples: list[EvalSample]) -> str:
    """序列化为 JSON（方便版本化管理）。"""
    return json.dumps([
        {"id": s.id, "scenario": s.scenario, "input": s.input, "ground_truth": s.ground_truth}
        for s in samples
    ], ensure_ascii=False, indent=2)
