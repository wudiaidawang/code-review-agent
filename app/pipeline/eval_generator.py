"""最终评测集生成器 -- 覆盖 Review Pipeline + Investigation Agent 全能力

用法：
    python -m app.pipeline.eval_generator                     # 默认 550 review + 150 agent
    python -m app.pipeline.eval_generator --review 100 --agent 50  # 自定义数量
    python -m app.pipeline.eval_generator --batch 15              # 调整 LLM 批量大小

设计原则：
    1. LLM 只生成自然语言文本，不生成 ground truth
    2. Ground truth 由 RuleBasedPlanBuilder 确定性计算
    3. 覆盖率报告自动生成，验证各维度分布
    4. 数据集版本化保存，含元数据
"""

import itertools
import json
import os
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.pipeline.plan_builder import RuleBasedPlanBuilder
from app.tools.llm_tool import chat

# ---- 覆盖矩阵定义 ----

LANGUAGES = ["Python", "JavaScript", "TypeScript", "Java", "Go"]
CHANGE_TYPES = ["bug_fix", "feature", "refactor", "security_patch", "config_change", "dependency_upgrade"]
FILE_COUNTS = [1, 3, 8, 25, 65]
LINE_COUNTS = [5, 30, 150, 500, 2000]
RISK_SIGNALS_POOL = ["auth_change", "sql_risk", "command_injection", "deserialization", "dependency_change"]
PY_EXT = ".py"
JS_EXT = ".js"
TS_EXT = ".ts"
JAVA_EXT = ".java"
GO_EXT = ".go"
NON_CODE_EXTS = [".md", ".json", ".yml", ".toml", ".txt", ".cfg", ".ini"]

SEED = 42


def _risk_combos():
    """生成所有有意义的风险信号组合（0~5 个信号）。"""
    combos: list[list[str]] = [[]]  # 无风险
    pool = list(RISK_SIGNALS_POOL)
    for r in range(1, len(pool) + 1):
        for c in itertools.combinations(pool, r):
            combos.append(list(c))
    return combos  # 32 种


def _pick(values, count):
    """从列表中随机采 count 个（最多 len(values)）。"""
    rng = random.Random(SEED)
    return rng.sample(values, min(count, len(values)))


def build_coverage_matrix(target_count: int = 550) -> list[dict]:
    """构建 Review Planner 评测覆盖矩阵。

    分层采样策略：
      1. Python 低风险 (~80): 无风险信号，变化文件数/行数/依赖文件
      2. Python 单风险 (~120): 每种风险信号 25 条，变化其他维度
      3. Python 双风险 (~100): 10 种两两组合 × 10 条
      4. Python 多风险 (~60): ≥3 信号组合，确保 high risk
      5. JS/TS (~50): 非 Python 的正常变更
      6. Java/Go (~30): 多语言覆盖
      7. 非代码 (~40): 纯配置文件/文档变更
      8. 混合 (~30): 多语言+配置混合
      9. 边界/极端 (~40): 0 文件、超大 diff、纯删除
    """
    rng = random.Random(SEED)
    samples: list[dict] = []

    def _vary(params, key, values, count):
        """对给定参数集，用 values 中的不同值各生成 count 个变体。"""
        result = []
        base = dict(params)
        for v in _pick(values, count):
            p = dict(base)
            p[key] = v
            p["_id"] = _make_review_id(p)
            result.append(p)
            samples.append(p)
        return result

    def _vary_n(params, key, values, n):
        """对给定参数集，变化 key 为 values 中的不同值，每个值生成 n 条。"""
        result = []
        base = dict(params)
        for v in values:
            for _ in range(n):
                p = dict(base)
                p[key] = v
                p["_id"] = _make_review_id(p)
                result.append(p)
                samples.append(p)
        return result

    # ---- 1. Python 低风险 (~80) ----
    py_low_base = {
        "language": "Python",
        "file_exts": [".py"],
        "risk_signals": [],
        "change_type": "bug_fix",
        "has_dep_files": False,
        "static_findings_count": 0,
    }
    for fc in [1, 3, 8, 25]:
        for lc in [5, 30, 150, 500]:
            for ct in ["bug_fix", "feature", "refactor"]:
                p = dict(py_low_base)
                p.update(python_file_count=fc, total_lines=lc, change_type=ct)
                p["_id"] = _make_review_id(p)
                samples.append(p)
                if len([s for s in samples if not s.get("risk_signals")]) >= 80:
                    break
            if len([s for s in samples if not s.get("risk_signals")]) >= 80:
                break
        if len([s for s in samples if not s.get("risk_signals")]) >= 80:
            break

    # ---- 2. Python 单风险 (~120) ----
    for sig in RISK_SIGNALS_POOL:
        base = {
            "language": "Python",
            "file_exts": [".py"],
            "risk_signals": [sig],
            "has_dep_files": sig == "dependency_change",
            "static_findings_count": rng.choice([0, 2, 8]),
        }
        for _ in range(25):
            p = dict(base)
            p["python_file_count"] = rng.choice(FILE_COUNTS[:4])
            p["total_lines"] = rng.choice(LINE_COUNTS)
            p["change_type"] = rng.choice(CHANGE_TYPES[:3] + ["security_patch"])
            p["_id"] = _make_review_id(p)
            samples.append(p)

    # ---- 3. Python 双风险 (~100) ----
    two_combos = list(itertools.combinations(RISK_SIGNALS_POOL, 2))
    for combo in two_combos:
        base = {
            "language": "Python",
            "file_exts": [".py"],
            "risk_signals": list(combo),
            "has_dep_files": "dependency_change" in combo,
            "static_findings_count": rng.choice([2, 8]),
        }
        for _ in range(10):
            p = dict(base)
            p["python_file_count"] = rng.choice(FILE_COUNTS[:4])
            p["total_lines"] = rng.choice(LINE_COUNTS)
            p["change_type"] = rng.choice(["bug_fix", "feature", "security_patch"])
            p["_id"] = _make_review_id(p)
            samples.append(p)

    # ---- 4. Python 多风险 ≥3 (~60) ----
    multi_combos = list(itertools.combinations(RISK_SIGNALS_POOL, 3)) + \
                   list(itertools.combinations(RISK_SIGNALS_POOL, 4)) + \
                   [tuple(RISK_SIGNALS_POOL)]
    for combo in multi_combos:
        base = {
            "language": "Python",
            "file_exts": [".py"],
            "risk_signals": list(combo),
            "has_dep_files": "dependency_change" in combo,
            "static_findings_count": rng.choice([8, 20]),
            "change_type": "security_patch",
        }
        for _ in range(6 if len(combo) <= 3 else 4):
            p = dict(base)
            p["python_file_count"] = rng.choice([1, 3, 8])
            p["total_lines"] = rng.choice(LINE_COUNTS[1:])
            p["_id"] = _make_review_id(p)
            samples.append(p)

    # ---- 5. JS/TS (~50) ----
    for lang, ext, ct in [("JavaScript", ".js", "feature"), ("TypeScript", ".ts", "refactor")]:
        for _ in range(25):
            samples.append({
                "_id": _make_review_id({"language": lang}),
                "language": lang,
                "file_exts": [ext],
                "risk_signals": rng.choice([[], [rng.choice(RISK_SIGNALS_POOL[:3])]]),
                "python_file_count": 0,
                "total_lines": rng.choice(LINE_COUNTS[:3]),
                "change_type": rng.choice([ct, "bug_fix"]),
                "has_dep_files": False,
                "static_findings_count": 0,
            })

    # ---- 6. Java/Go (~30) ----
    for lang, ext in [("Java", ".java"), ("Go", ".go")]:
        for _ in range(15):
            samples.append({
                "_id": _make_review_id({"language": lang}),
                "language": lang,
                "file_exts": [ext],
                "risk_signals": rng.choice([[], [rng.choice(RISK_SIGNALS_POOL[:2])]]),
                "python_file_count": 0,
                "total_lines": rng.choice(LINE_COUNTS[:3]),
                "change_type": rng.choice(CHANGE_TYPES[:3]),
                "has_dep_files": False,
                "static_findings_count": 0,
            })

    # ---- 7. 非代码文件 (~40) ----
    for ext in NON_CODE_EXTS:
        lang = {"md": "Markdown", "json": "JSON", "yml": "YAML", "toml": "TOML",
                "txt": "Text", "cfg": "Config", "ini": "Config"}.get(ext.lstrip("."), "Config")
        for _ in range(6):
            dep = ext in [".toml", ".cfg", ".ini"]
            samples.append({
                "_id": _make_review_id({"language": lang}),
                "language": lang,
                "file_exts": [ext],
                "risk_signals": ["dependency_change"] if dep else [],
                "python_file_count": 0,
                "total_lines": rng.choice([5, 30, 150]),
                "change_type": "config_change" if dep else "bug_fix",
                "has_dep_files": dep,
                "static_findings_count": 0,
            })

    # ---- 8. 混合 (~30) ----
    for _ in range(30):
        exts = list(set(rng.choices([".py"] + NON_CODE_EXTS, k=rng.randint(2, 4))))
        has_py = ".py" in exts
        samples.append({
            "_id": _make_review_id({"language": "Mixed"}),
            "language": "Mixed",
            "file_exts": exts,
            "risk_signals": rng.choice(RISK_SIGNALS_POOL[:3]).split() if rng.random() > 0.5 and has_py else [],
            "python_file_count": rng.choice([1, 3]) if has_py else 0,
            "total_lines": rng.choice(LINE_COUNTS[:3]),
            "change_type": rng.choice(CHANGE_TYPES),
            "has_dep_files": any(e in [".toml", ".cfg", ".ini"] for e in exts),
            "static_findings_count": rng.choice([0, 2]) if has_py else 0,
        })

    # ---- 9. 边界/极端 (~40) ----
    edge_cases = [
        {"language": "Python", "file_exts": [], "risk_signals": [], "python_file_count": 0,
         "total_lines": 0, "change_type": "bug_fix", "has_dep_files": False, "static_findings_count": 0,
         "_scenario": "empty_change"},
        {"language": "Python", "file_exts": [".py"], "risk_signals": [], "python_file_count": 1,
         "total_lines": 10000, "change_type": "refactor", "has_dep_files": False, "static_findings_count": 50,
         "_scenario": "giant_diff"},
        {"language": "Python", "file_exts": [".py"], "risk_signals": RISK_SIGNALS_POOL,
         "python_file_count": 25, "total_lines": 2000, "change_type": "security_patch",
         "has_dep_files": True, "static_findings_count": 30, "_scenario": "all_risks"},
    ]
    for ec in edge_cases:
        ec["_id"] = _make_review_id(ec)
        samples.append(ec)

    # 补齐至目标数量
    while len(samples) < target_count:
        existing = rng.choice(samples)
        variant = dict(existing)
        variant["total_lines"] = rng.choice(LINE_COUNTS)
        variant["python_file_count"] = rng.choice(FILE_COUNTS[:4]) if variant.get("python_file_count", 0) > 0 else 0
        variant["_id"] = _make_review_id(variant)
        samples.append(variant)

    # 截断至目标数
    result = samples[:target_count]
    # 确保 ID 唯一
    seen_ids = set()
    unique = []
    for s in result:
        if s["_id"] not in seen_ids:
            seen_ids.add(s["_id"])
            unique.append(s)
    while len(unique) < target_count:
        base = rng.choice(unique)
        variant = dict(base)
        variant["total_lines"] = rng.choice(LINE_COUNTS)
        variant["_id"] = _make_review_id(variant)
        if variant["_id"] not in seen_ids:
            seen_ids.add(variant["_id"])
            unique.append(variant)

    return unique[:target_count]


def _make_review_id(params: dict) -> str:
    """根据参数生成唯一样本 ID。"""
    import hashlib
    key = json.dumps({k: v for k, v in sorted(params.items()) if not k.startswith("_")}, sort_keys=True)
    h = hashlib.md5(key.encode()).hexdigest()[:6]
    lang = str(params.get("language", "X"))[:4]
    return f"r{params.get('python_file_count', 0)}_{params.get('total_lines', 0)}_{lang}_{h}"


# ---- LLM 文本生成 ----

_TEXT_SYSTEM = """你是一个评测数据集生成器。根据给定的参数列表，为每条样本生成一段真实的变更摘要(change_summary)和AST摘要(ast_summary)。

change_summary: 用1-2句中/英文描述这次代码变更做了什么，必须与参数匹配。
ast_summary: 描述受影响的函数/类/结构（如 "2 functions modified, 1 class added"），必须与python_file_count匹配。

输出格式（JSON 数组，每个元素对应输入的一条）：
```json
[
  {"change_summary": "...", "ast_summary": "..."},
  ...
]
```

规则：
- change_summary 必须反映 language/change_type/risk_signals/file_exts
- ast_summary 必须与 python_file_count 一致（0 个 Python 文件 → "N/A"）
- 风格多样，不要重复相同的描述模板
- 只输出 JSON，不要解释"""


def _build_text_prompt(params_batch: list[dict]) -> str:
    """构造批量文本生成的 user prompt。"""
    lines = ["为以下样本生成 change_summary 和 ast_summary：\n"]
    for i, p in enumerate(params_batch):
        risk_str = ", ".join(p.get("risk_signals", [])) or "无"
        exts = ", ".join(p.get("file_exts", []))
        lines.append(
            f"{i}. language={p['language']}, change_type={p['change_type']}, "
            f"file_exts=[{exts}], python_file_count={p.get('python_file_count', 0)}, "
            f"total_lines={p['total_lines']}, risk_signals=[{risk_str}], "
            f"has_dep_files={p.get('has_dep_files', False)}"
        )
    lines.append(f"\n请输出 {len(params_batch)} 条样本，JSON 数组格式。")
    return "\n".join(lines)


def _parse_text_response(raw: str, expected_count: int) -> list[dict]:
    """解析 LLM 返回的批量文本 JSON。"""
    text = raw.strip()
    # 去掉 markdown 包裹
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        items = json.loads(text)
        if not isinstance(items, list):
            return [{"change_summary": "", "ast_summary": ""} for _ in range(expected_count)]
        # 补齐不足
        while len(items) < expected_count:
            items.append({"change_summary": "", "ast_summary": ""})
        return items[:expected_count]
    except json.JSONDecodeError:
        # 尝试逐行解析或回退
        fallback = []
        for _ in range(expected_count):
            fallback.append({"change_summary": "", "ast_summary": ""})
        return fallback


def generate_texts_batch(params_batch: list[dict], verbose: bool = True) -> list[dict]:
    """调用 LLM API 为一组参数批量生成文本。失败时返回空占位符。"""
    try:
        prompt = _build_text_prompt(params_batch)
        raw = chat(prompt, system=_TEXT_SYSTEM, temperature=0.7, max_tokens=4000)
        return _parse_text_response(raw, len(params_batch))
    except Exception as exc:
        if verbose:
            print(f"  LLM 调用失败: {exc}")
        return [{"change_summary": "", "ast_summary": ""} for _ in range(params_batch)]


# ---- Agent 样本生成（确定性，不需要 LLM） ----

def generate_agent_samples(target_count: int = 150) -> list[dict]:
    """生成 Investigation Agent 评测样本（不调 LLM，确定性生成）。"""
    rng = random.Random(SEED)
    samples: list[dict] = []

    # 模板库：locate 类
    locate_templates_cn = [
        '"{symbol}" 在哪个文件里定义的？',
        '"{symbol}" 的定义在哪个文件中？',
        '代码库中哪里定义了 {symbol}？',
        '帮我找一下 {symbol} 的位置',
        '{symbol} 函数在哪里实现的？',
        '{symbol} 类的定义位置是？',
        '我想看 {symbol} 的源码，在哪个文件？',
        '搜索 {symbol} 的文件位置',
        '{symbol} 放在哪个目录下？',
        '请定位 {symbol} 这个符号',
    ]
    locate_templates_en = [
        'Where is "{symbol}" defined?',
        'Which file contains the definition of {symbol}?',
        'In which file is {symbol} located?',
        'Find the definition location of {symbol}',
        'Where can I find {symbol} in the codebase?',
    ]

    explain_templates_cn = [
        '"{symbol}" 是做什么用的？',
        '{symbol} 的主要功能是什么？',
        '这个 {symbol} 函数起什么作用？',
        '能否解释一下 {symbol} 的用途？',
        '{symbol} 负责处理什么逻辑？',
        '说明 {symbol} 的功能',
        '给我介绍下 {symbol} 这个类',
        '"{symbol}" 在整个系统中扮演什么角色？',
        '{symbol} 解决什么问题？',
        '描述 "{symbol}" 的作用和职责',
    ]
    explain_templates_en = [
        'What does "{symbol}" do?',
        'What is the purpose of {symbol}?',
        'Explain the functionality of {symbol}',
        'What role does {symbol} play?',
        'Describe what {symbol} is responsible for',
    ]

    trace_templates_cn = [
        '哪些地方调用了 {symbol}？',
        '谁在调用 {symbol}？',
        '帮我追踪 {symbol} 的调用链',
        '{symbol} 被哪些函数引用？',
        '查找所有调用 {symbol} 的位置',
        '追踪一下谁依赖了 {symbol}',
        '{symbol} 被 import 到哪些文件了？',
        '找一下 {symbol} 的所有调用者',
    ]
    trace_templates_en = [
        'What calls {symbol}?',
        'Who invokes {symbol}?',
        'Find all callers of {symbol}',
        'Which functions depend on {symbol}?',
    ]

    grep_templates_cn = [
        '列出所有用到 {keyword} 的地方',
        '搜索所有包含 {keyword} 的代码',
        '帮我找一下所有 "{keyword}" 相关的位置',
        '把所有 {keyword} 相关代码找出来',
        '查找所有 import {keyword} 的文件',
        '列出 "{keyword}" 的全部出现位置',
        '扫描所有包含字符串 {keyword} 的源文件',
    ]
    grep_templates_en = [
        'Find all occurrences of "{keyword}"',
        'List all files that use {keyword}',
        'Search for all references to {keyword}',
    ]

    # 符号列表（各种形态）
    camel_symbols = ["UserService", "AuthMiddleware", "ConfigParser", "DataLoader", "HttpClient",
                     "TokenValidator", "CacheManager", "LogHandler", "ApiGateway", "DbPool"]
    snake_symbols = ["handle_request", "parse_config", "load_data", "validate_token", "execute_query",
                     "cache_lookup", "format_response", "hash_password", "send_email", "read_file"]
    module_names = ["utils.helpers", "api.v1.auth", "core.database", "models.user", "services.email",
                    "middleware.cors", "lib.crypto", "handlers.http", "storage.cache", "plugins.exporter"]
    kw_list = ["subprocess", "pickle", "requests", "sqlalchemy", "asyncio", "threading",
               "tempfile", "logging", "argparse", "dataclasses"]

    # ---- Locate 样本 (~40) ----
    for i in range(40):
        is_cn = i < 35  # 大部分中文
        tmpl = rng.choice(locate_templates_cn if is_cn else locate_templates_en)
        symbol = rng.choice(camel_symbols + snake_symbols)
        question = tmpl.format(symbol=symbol)
        samples.append({
            "id": f"a{len(samples)+1:04d}_locate",
            "mode": "investigation",
            "question": question,
            "ground_truth": {
                "question_type": "locate",
                "expected_keywords": [symbol],
                "expected_tools": ["git_grep", "file_read", "llm_synthesize"],
            },
        })

    # ---- Explain 样本 (~40) ----
    for i in range(40):
        is_cn = i < 35
        tmpl = rng.choice(explain_templates_cn if is_cn else explain_templates_en)
        symbol = rng.choice(camel_symbols + snake_symbols + module_names)
        question = tmpl.format(symbol=symbol)
        samples.append({
            "id": f"a{len(samples)+1:04d}_explain",
            "mode": "investigation",
            "question": question,
            "ground_truth": {
                "question_type": "explain",
                "expected_keywords": [symbol],
                "expected_tools": ["git_grep", "file_read", "llm_synthesize"],
            },
        })

    # ---- Trace 样本 (~35) ----
    for i in range(35):
        is_cn = i < 30
        tmpl = rng.choice(trace_templates_cn if is_cn else trace_templates_en)
        symbol = rng.choice(camel_symbols + snake_symbols)
        question = tmpl.format(symbol=symbol)
        samples.append({
            "id": f"a{len(samples)+1:04d}_trace",
            "mode": "investigation",
            "question": question,
            "ground_truth": {
                "question_type": "trace",
                "expected_keywords": [symbol],
                "expected_tools": ["git_grep", "file_read", "llm_synthesize"],
            },
        })

    # ---- Grep 样本 (~35) ----
    for i in range(35):
        is_cn = i < 30
        tmpl = rng.choice(grep_templates_cn if is_cn else grep_templates_en)
        keyword = rng.choice(kw_list + module_names)
        question = tmpl.format(keyword=keyword)
        samples.append({
            "id": f"a{len(samples)+1:04d}_grep",
            "mode": "investigation",
            "question": question,
            "ground_truth": {
                "question_type": "grep",
                "expected_keywords": [keyword],
                "expected_tools": ["git_grep", "file_read", "llm_synthesize"],
            },
        })

    # ---- 边界样本 ----
    edge_questions = [
        ("在哪里？", "locate", []),
        ("这个是什么？", "explain", []),
        ("", "locate", []),
        ("帮我看看代码", "locate", []),
        ("find", "locate", []),
        ("what does do?", "explain", []),
        ("where is?", "locate", []),
        ("function 在哪里", "locate", []),
    ]
    for q, qtype, kw in edge_questions:
        samples.append({
            "id": f"a{len(samples)+1:04d}_edge",
            "mode": "investigation",
            "question": q,
            "ground_truth": {
                "question_type": qtype,
                "expected_keywords": kw,
                "expected_tools": [],
            },
        })

    rng.shuffle(samples)
    return samples[:target_count]


# ---- Ground Truth 计算 ----

def _make_fake_changeset(params: dict) -> dict:
    """将覆盖矩阵参数转换为 RuleBasedPlanBuilder 可消费的 ChangeSet dict。"""
    exts = params.get("file_exts", [])
    python_file_count = params.get("python_file_count", 0)
    total_lines = params.get("total_lines", 0)
    has_dep = params.get("has_dep_files", False)
    risk_signals = params.get("risk_signals", [])

    files = []
    # 生成 Python 文件
    for i in range(min(python_file_count, 200)):
        fname = f"changed_{i}.py" if i > 0 else "main.py"
        # 在文件名中嵌入风险信号关键词
        if risk_signals:
            if "auth_change" in risk_signals and i == 0:
                fname = "auth.py"
            elif "dependency_change" in risk_signals and i == 0:
                fname = "pyproject.toml"
        added = max(1, total_lines // max(python_file_count, 1))
        files.append({
            "path": fname,
            "change_type": "modified",
            "added_lines": added,
            "deleted_lines": max(0, added // 3),
        })

    # 非 Python 文件
    for ext in exts:
        if ext == ".py":
            continue
        fname = f"config{ext}" if ext not in [".md"] else "README.md"
        files.append({
            "path": fname,
            "change_type": "modified",
            "added_lines": max(1, total_lines // max(len(exts), 1)),
            "deleted_lines": 0,
        })

    # 依赖文件
    if has_dep:
        if not any(f["path"] in ["requirements.txt", "pyproject.toml", "setup.py"] for f in files):
            files.append({
                "path": "requirements.txt",
                "change_type": "modified",
                "added_lines": 5,
                "deleted_lines": 2,
            })

    if not files:
        files = []

    return {"files": files}


def compute_ground_truth(params: dict) -> dict:
    """直接根据参数计算 ground truth，不依赖文件内容扫描。

    与 RuleBasedPlanBuilder 的规则逻辑一致，但使用参数中的显式风险信号。
    """
    risk_signals = params.get("risk_signals", [])
    python_file_count = params.get("python_file_count", 0)
    total_lines = params.get("total_lines", 0)
    has_dep = params.get("has_dep_files", False)
    file_exts = params.get("file_exts", [])
    has_py = ".py" in file_exts or python_file_count > 0

    analyzers = ["git"]
    reason_codes: list[str] = []

    if has_py:
        # Python 文件 → ruff 固定
        analyzers.append("ruff")

        # AST：文件数 ≤50 时开启
        if python_file_count <= 50 and python_file_count > 0:
            analyzers.append("python_ast")
        elif python_file_count > 50:
            reason_codes.append("python_ast_skipped_large_diff")

        # Bandit：有风险信号 或 变更量大
        if risk_signals:
            analyzers.append("bandit")
            reason_codes.extend(sorted(risk_signals))
        elif total_lines > 100:
            analyzers.append("bandit")
        else:
            reason_codes.append("bandit_skipped_low_risk")

        # Dependency：依赖文件变更
        if has_dep or "dependency_change" in risk_signals:
            if "dependency" not in analyzers:
                analyzers.append("dependency")
            if "dependency_change" not in reason_codes:
                reason_codes.append("dependency_change")

    else:
        # 非 Python
        if file_exts:
            reason_codes.append("no_python_changes")
        # 如果有依赖文件但无 Python
        if has_dep:
            if "dependency" not in analyzers:
                analyzers.append("dependency")
            if "dependency_change" not in reason_codes:
                reason_codes.append("dependency_change")

    # 风险等级
    risk_level = _calc_risk_level(risk_signals)

    # 去重保序
    seen_a = set()
    unique_analyzers = []
    for a in analyzers:
        if a not in seen_a:
            seen_a.add(a)
            unique_analyzers.append(a)

    return {
        "analyzers": unique_analyzers,
        "risk_level": risk_level,
        "reason_codes": reason_codes,
    }


def _calc_risk_level(signals: list[str]) -> str:
    if len(signals) >= 3:
        return "high"
    if len(signals) >= 1:
        return "medium"
    return "low"


# ---- 覆盖率报告 ----

def compute_coverage_report(review_samples: list[dict], agent_samples: list[dict]) -> dict:
    """生成覆盖率统计报告。"""
    report: dict = {
        "total_review": len(review_samples),
        "total_agent": len(agent_samples),
        "review": {},
        "agent": {},
    }

    # Review 覆盖率
    lang_count = Counter(s.get("_meta", {}).get("language", "Unknown") for s in review_samples)
    risk_count = Counter()
    analyzer_count = Counter()
    change_count = Counter(s.get("_meta", {}).get("change_type", "Unknown") for s in review_samples)
    file_count_dist = Counter()
    line_count_dist = Counter()
    combo_checks = defaultdict(int)

    for s in review_samples:
        # 计算 ground truth 以获取 analyzer 组合
        gt = compute_ground_truth(s)
        risk_level = gt["risk_level"]
        risk_count[risk_level] += 1
        analyzer_key = "+".join(gt["analyzers"])
        analyzer_count[analyzer_key] += 1
        file_count_dist[f"files_{s.get('python_file_count', 0)}"] += 1
        line_count_dist[f"lines_{s.get('total_lines', 0)}"] += 1

        # 组合覆盖检查
        language = s.get("_meta", {}).get("language", "Unknown")
        signals = s.get("input", {}).get("risk_signals", [])
        sig_key = "+".join(sorted(signals)) if signals else "none"
        combo_checks[f"{language}+{risk_level}"] += 1
        combo_checks[f"{language}+{analyzer_key}"] += 1
        if signals:
            combo_checks[f"risk={sig_key}"] += 1

    report["review"] = {
        "by_language": dict(lang_count.most_common()),
        "by_risk_level": dict(risk_count.most_common()),
        "by_analyzer_combo": dict(analyzer_count.most_common()),
        "by_change_type": dict(change_count.most_common()),
        "by_file_count": dict(file_count_dist.most_common()),
        "by_line_count": dict(line_count_dist.most_common()),
        "combo_checks": dict(combo_checks),
    }

    # Agent 覆盖率
    qtype_count = Counter(s["ground_truth"]["question_type"] for s in agent_samples)
    report["agent"] = {
        "by_question_type": dict(qtype_count.most_common()),
    }

    return report


def _print_coverage(report: dict):
    """打印覆盖率报告（ASCII 格式）。"""
    rev = report["review"]
    agt = report["agent"]

    print("\n" + "=" * 56)
    print("  覆盖率报告 (Coverage Report)")
    print("=" * 56)
    print(f"\n  Review 样本: {report['total_review']}  |  Agent 样本: {report['total_agent']}")

    print("\n  --- 语言分布 ---")
    for lang, cnt in sorted(rev["by_language"].items(), key=lambda x: -x[1]):
        bar = "#" * max(1, cnt // 3)
        print(f"  {lang:<18} {cnt:>4}  {bar}")

    print("\n  --- 风险等级 ---")
    for level in ["high", "medium", "low"]:
        cnt = rev["by_risk_level"].get(level, 0)
        bar = "#" * max(1, cnt // 3)
        print(f"  {level:<18} {cnt:>4}  {bar}")

    print("\n  --- Analyzer 组合 ---")
    for combo, cnt in sorted(rev["by_analyzer_combo"].items(), key=lambda x: -x[1])[:10]:
        print(f"  {combo:<40} {cnt:>4}")

    print("\n  --- 变更类型 ---")
    for ct, cnt in sorted(rev["by_change_type"].items(), key=lambda x: -x[1]):
        print(f"  {ct:<20} {cnt:>4}")

    print("\n  --- 关键组合覆盖检查 ---")
    for combo, cnt in sorted(rev["combo_checks"].items()):
        status = "+" if cnt > 0 else "MISSING"
        print(f"  [{status}] {combo:<45} {cnt:>3}")

    print("\n  --- Agent 问题类型 ---")
    for qtype, cnt in sorted(agt["by_question_type"].items(), key=lambda x: -x[1]):
        print(f"  {qtype:<15} {cnt:>4}")

    print("\n" + "=" * 56 + "\n")


# ---- 数据集保存 ----

def save_dataset(review_samples: list[dict], agent_samples: list[dict],
                 report: dict, version: str = "v2"):
    """保存数据集和元数据到 JSON 文件。"""
    snap_dir = os.path.join(os.path.dirname(__file__), "..", "..", "tests", "__snapshots__")
    os.makedirs(snap_dir, exist_ok=True)

    # 获取 git commit
    import subprocess
    try:
        git_commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        git_commit = "unknown"

    # 数据集文件
    dataset_path = os.path.join(snap_dir, f"eval_dataset_{version}.json")
    all_samples = review_samples + agent_samples
    with open(dataset_path, "w", encoding="utf-8") as f:
        json.dump(all_samples, f, ensure_ascii=False, indent=2)

    # 元数据文件
    meta_path = os.path.join(snap_dir, f"eval_dataset_{version}_meta.json")
    meta = {
        "dataset_version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "prompt_version": "p1",
        "planner_version": "RuleBasedPlanBuilder v1",
        "total_samples": len(all_samples),
        "review_samples": len(review_samples),
        "agent_samples": len(agent_samples),
        "human_reviewed": 0,
        "coverage": report,
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print(f"数据集已保存: {dataset_path}")
    print(f"元数据已保存: {meta_path}")
    return dataset_path, meta_path


# ---- 主流程 ----

def generate_dataset(review_count: int = 550, agent_count: int = 150,
                     batch_size: int = 20, verbose: bool = True) -> str:
    """生成完整评测数据集。

    Returns:
        dataset_path: 生成的数据集 JSON 文件路径
    """
    if verbose:
        print("=" * 56)
        print("  评测数据集生成器")
        print(f"  Review: {review_count} 条  |  Agent: {agent_count} 条")
        print("=" * 56)

    # Step 1: 构建覆盖矩阵
    if verbose:
        print("\n[1/6] 构建覆盖矩阵...")
    review_params = build_coverage_matrix(review_count)
    if verbose:
        print(f"  生成 {len(review_params)} 条 Review 参数")

    # Step 2: LLM 批量生成文本
    if verbose:
        print(f"\n[2/6] LLM 批量生成文本 ({len(review_params)//batch_size + 1} 批，batch_size={batch_size})...")
    all_texts: list[dict] = []
    for bi in range(0, len(review_params), batch_size):
        batch = review_params[bi:bi + batch_size]
        if verbose:
            print(f"  批次 {bi//batch_size + 1}/{len(review_params)//batch_size + 1} "
                  f"({len(batch)} 条)...", end=" ", flush=True)
        texts = generate_texts_batch(batch, verbose=verbose)
        all_texts.extend(texts)
        if verbose:
            ok = sum(1 for t in texts if t.get("change_summary"))
            print(f"OK={ok}/{len(batch)}")
        # 轻量节流
        if bi + batch_size < len(review_params):
            time.sleep(0.3)

    # Step 3: 组装 Review 样本 + 计算 ground truth
    if verbose:
        print("\n[3/6] 组装 Review 样本 + 计算 Ground Truth...")
    review_samples: list[dict] = []
    for i, (params, text) in enumerate(zip(review_params, all_texts)):
        gt = compute_ground_truth(params)
        sample = {
            "id": params.get("_id", f"r{i:04d}"),
            "mode": "review",
            "scenario": text.get("change_summary", params.get("_scenario", "")),
            "_meta": {
                "language": params.get("language", "Unknown"),
                "change_type": params.get("change_type", "Unknown"),
            },
            "input": {
                "change_summary": text.get("change_summary", ""),
                "file_types": params.get("file_exts", [".py"]),
                "diff_size": {
                    "files": params.get("python_file_count", 0) + len(params.get("file_exts", [])),
                    "added_lines": params.get("total_lines", 0),
                    "deleted_lines": max(0, params.get("total_lines", 0) // 3),
                },
                "risk_signals": params.get("risk_signals", []),
                "ast_summary": text.get("ast_summary", ""),
                "static_findings_count": params.get("static_findings_count", 0),
            },
            "ground_truth": gt,
        }
        review_samples.append(sample)
    if verbose:
        print(f"  组装 {len(review_samples)} 条 Review 样本")

    # Step 4: 生成 Agent 样本
    if verbose:
        print("\n[4/6] 生成 Agent 样本...")
    agent_samples = generate_agent_samples(agent_count)
    if verbose:
        print(f"  生成 {len(agent_samples)} 条 Agent 样本")

    # Step 5: 覆盖率报告
    if verbose:
        print("\n[5/6] 计算覆盖率报告...")
    report = compute_coverage_report(review_samples, agent_samples)
    if verbose:
        _print_coverage(report)

    # Step 6: 保存
    if verbose:
        print("[6/6] 保存数据集...")
    dataset_path, meta_path = save_dataset(review_samples, agent_samples, report)

    if verbose:
        print("\n完成!")

    return dataset_path


# ---- CLI ----

if __name__ == "__main__":
    import argparse

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="评测数据集生成器")
    parser.add_argument("--review", type=int, default=550, help="Review 样本数（默认 550）")
    parser.add_argument("--agent", type=int, default=150, help="Agent 样本数（默认 150）")
    parser.add_argument("--batch", type=int, default=20, help="LLM 批量大小（默认 20）")
    args = parser.parse_args()

    generate_dataset(review_count=args.review, agent_count=args.agent, batch_size=args.batch)
