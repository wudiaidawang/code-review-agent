"""代码样本生成器 — LLM 批量生成含已知漏洞模式的代码文件，并初始化为独立 git repo.

用法:
    python -m eval_report.generate_samples --count 30     # 生成 30 条样本
    python -m eval_report.generate_samples --count 5 --dry-run  # 只打印参数，不调 LLM
"""

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.pipeline.eval_dataset import load_samples
from app.tools.llm_tool import chat

ROOT = Path(__file__).resolve().parent
_DEFAULT_SAMPLES_DIR = Path(os.environ.get("EVAL_SAMPLES_DIR",
    Path(os.environ.get("TEMP", "/tmp")) / "eval_report_samples"))

# ---- 风险信号 → 漏洞模式描述 ----

_RISK_PATTERN_DESC: dict[str, str] = {
    "sql_risk": "SQL 注入漏洞：使用字符串拼接或 f-string 构造 SQL 查询，用户输入未经参数化处理。"
                "例如 cursor.execute(f\"SELECT * FROM users WHERE name='{user_input}'\") 或 "
                "query = \"SELECT * FROM users WHERE name='\" + user_input + \"'\"",

    "command_injection": "命令注入漏洞：使用 subprocess.run/call/os.system 执行包含用户输入的命令，"
                         "且 shell=True 或未对输入做校验。"
                         "例如 subprocess.run(f\"ping {user_host}\", shell=True) 或 os.system(\"rm -rf \" + user_path)",

    "deserialization": "不安全反序列化：使用 pickle.load()/yaml.load()/marshal.load() 加载未验证的外部数据。"
                       "例如 pickle.loads(user_data)、yaml.load(user_input, Loader=yaml.Loader)",

    "auth_change": "认证/鉴权缺陷：在 auth.py 或 login 相关文件中存在硬编码凭据、弱密码校验、"
                   "缺少权限检查等问题。例如 if password == 'admin123': 或 hardcoded API_KEY",

    "dependency_change": "依赖变更：在 requirements.txt/pyproject.toml/package.json 中添加了"
                         "已知有漏洞的旧版本依赖，或在未锁定版本号的情况下升级了包",
}

# ---- System Prompt ----

_GENERATE_SYSTEM = """\
你是一个安全测试代码生成专家。你需要生成真实的、可运行的代码文件，其中包含特定的安全漏洞模式。

要求:
1. 生成的代码必须是语法正确的（Python/JavaScript/TypeScript/Java/Go）
2. 代码结构要像真实项目：有适当的 import、类/函数定义、主逻辑
3. 漏洞代码必须混合在正常业务逻辑中，不要单独写一个"这是漏洞"的函数
4. 文件数量、新增行数要符合指定参数
5. 漏洞要真实可被静态分析工具（如 bandit、ruff）检测到

输出格式: 严格 JSON
{
  "files": [
    {
      "path": "src/auth.py",
      "content": "完整的文件内容..."
    }
  ],
  "expected_issues": [
    {
      "file": "src/auth.py",
      "line": 12,
      "rule_id": "B608",
      "severity": "medium",
      "description": "SQL 注入：用户输入直接拼接到 SQL 查询中"
    }
  ]
}

注意:
- files[].content 中必须包含完整代码，不要省略或使用占位符
- expected_issues 列出所有你刻意植入的漏洞，含文件、大致行号、对应规则ID、严重度
"""

_GENERATE_USER = """\
语言: {language}
文件扩展名: {file_exts}
文件数量: {file_count} 个（其中 Python: {python_count} 个）
变更类型: {change_type}
新增行数: {added_lines}、删除行数: {deleted_lines}
需包含的漏洞模式: {risk_signals_desc}
风险等级: {risk_level}
场景描述: {scenario}

请生成符合以上参数的代码变更。对于每个漏洞模式，必须在代码中植入对应的漏洞。
输出严格 JSON。"""

# 非 Python 语言的提示
_NON_PYTHON_SYSTEM = """\
你是一个安全测试代码生成专家。你需要生成真实的、可运行的代码文件，其中包含特定的安全漏洞模式。

要求:
1. 生成的代码必须是语法正确的{language}
2. 代码结构要像真实项目：有适当的 import/require、类/函数定义、主逻辑
3. 漏洞代码必须混合在正常业务逻辑中
4. 输出格式: 严格 JSON
{{
  "files": [
    {{
      "path": "src/component.{ext}",
      "content": "完整的文件内容..."
    }}
  ],
  "expected_issues": [
    {{
      "file": "src/component.{ext}",
      "line": 15,
      "severity": "medium",
      "description": "问题描述"
    }}
  ]
}}
"""


def _select_params(count: int = 40) -> list[dict]:
    """从 v2 数据集中选取代表性样本参数。"""
    all_samples = load_samples("review", dataset_version="latest")
    selected: list[dict] = []
    buckets: dict[str, list[dict]] = {
        "high": [], "medium": [], "low": [],
        "non_python": [], "mixed": [], "edge": [],
    }

    for s in all_samples:
        gt = s.ground_truth
        inp = s.input
        risk = gt.get("risk_level", "low")
        file_types = inp.get("file_types", [])
        has_py = any(ft == ".py" for ft in file_types)
        has_other = any(ft != ".py" for ft in file_types)

        if risk == "high":
            buckets["high"].append(s)
        elif risk == "medium":
            buckets["medium"].append(s)
        elif not has_py:
            buckets["non_python"].append(s)
        elif has_py and has_other:
            buckets["mixed"].append(s)
        else:
            buckets["low"].append(s)

    # 边缘 case：空文件、超大 diff
    for s in all_samples:
        inp = s.input
        if inp["diff_size"]["files"] == 0 or inp["diff_size"]["files"] > 50:
            buckets["edge"].append(s)

    # 按比例分配 count
    plan = [
        ("high", max(count // 5, 5)),
        ("medium", max(count // 4, 8)),
        ("low", max(count // 5, 5)),
        ("non_python", max(count // 10, 3)),
        ("mixed", max(count // 10, 3)),
        ("edge", max(count // 20, 2)),
    ]

    for bucket, n in plan:
        pool = buckets.get(bucket, [])
        if pool:
            selected.extend(pool[:n])

    # 去重
    seen = set()
    unique = []
    for s in selected:
        if s.id not in seen:
            seen.add(s.id)
            # 跳过没有风险信号的（留到 low bucket）
            unique.append(s)

    return unique[:count]


def _make_sample_dir(sample_id: str, samples_dir: Path) -> Path:
    d = samples_dir / sample_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _init_git_repo(sample_dir: Path, files: list[dict], expected_issues: list[dict]) -> bool:
    """在 sample_dir 中初始化 git 仓库:
    1. git init
    2. 写入文件的"安全版本"（无漏洞）-> git add + commit (base)
    3. 写入文件的"漏洞版本"（含漏洞）-> git add + commit (head)
    返回 True 表示成功。
    """
    try:
        subprocess.run(["git", "init", str(sample_dir)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sample_dir), "config", "user.email", "eval@test.local"], check=True)
        subprocess.run(["git", "-C", str(sample_dir), "config", "user.name", "Eval Generator"], check=True)

        # 安全版本：去除漏洞行的代码（简化为空函数体）
        safe_files = _make_safe_versions(files, expected_issues)
        for f in safe_files:
            fpath = sample_dir / f["path"]
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(f["content"], encoding="utf-8")

        subprocess.run(["git", "-C", str(sample_dir), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sample_dir), "commit", "-m", "initial: safe version"], check=True, capture_output=True)

        # 漏洞版本：写入含漏洞的代码
        for f in files:
            fpath = sample_dir / f["path"]
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(f["content"], encoding="utf-8")

        subprocess.run(["git", "-C", str(sample_dir), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(sample_dir), "commit", "-m", "feat: add feature with vulnerabilities"], check=True, capture_output=True)

        # 保存元数据
        meta = {
            "sample_id": sample_id,
            "files": [{"path": f["path"], "lines": len(f["content"].splitlines())} for f in files],
            "expected_issues": expected_issues,
        }
        (sample_dir / "_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        return True
    except subprocess.CalledProcessError as e:
        print(f"  [ERROR] git 操作失败: {e}")
        return False


def _make_safe_versions(files: list[dict], expected_issues: list[dict]) -> list[dict]:
    """生成安全版本：去掉漏洞代码行，用安全的替代实现填充。"""
    safe_files = []
    for f in files:
        content = f["content"]
        lines = content.split("\n")
        # 把漏洞相关的行注释掉或替换为空实现
        safe_lines = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # 跳过明显的漏洞行
            is_vuln = any([
                "shell=True" in stripped,
                "pickle.load" in stripped,
                "yaml.load(" in stripped and "SafeLoader" not in stripped,
                "marshal.load" in stripped,
                "execute(" in stripped and "f\"" in stripped,
                "os.system(" in stripped,
                "hardcoded" in stripped.lower() and "password" in stripped.lower(),
                stripped.startswith("API_KEY = ") or stripped.startswith("SECRET = "),
                stripped.startswith("PASSWORD = "),
            ])
            if is_vuln:
                safe_lines.append(f"    pass  # TODO: implement safely")
            else:
                safe_lines.append(line)
        safe_files.append({"path": f["path"], "content": "\n".join(safe_lines)})
    return safe_files


def _parse_llm_json(raw: str) -> dict:
    """从 LLM 输出中提取 JSON。"""
    text = raw.strip()
    # 去掉 markdown 包裹
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 尝试修复常见问题
        text = text.replace("```json", "").replace("```", "").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            print(f"  [WARN] JSON 解析失败，原始输出: {raw[:200]}")
            return {"files": [], "expected_issues": []}


def _generate_python(sample, retries=2) -> tuple[list[dict], list[dict]]:
    """生成 Python 代码。"""
    inp = sample.input
    gt = sample.ground_truth
    risk_signals = inp.get("risk_signals", [])
    risk_desc = "\n".join(f"- {s}: {_RISK_PATTERN_DESC.get(s, s)}" for s in risk_signals) or "无特定漏洞需求"
    risk_level = gt.get("risk_level", "low")

    user_prompt = _GENERATE_USER.format(
        language="Python",
        file_exts=", ".join(inp.get("file_types", [".py"])),
        file_count=inp["diff_size"]["files"],
        python_count=inp["diff_size"]["files"],
        change_type=inp.get("change_summary", "bug_fix"),
        added_lines=inp["diff_size"]["added_lines"],
        deleted_lines=inp["diff_size"]["deleted_lines"],
        risk_signals_desc=risk_desc,
        risk_level=risk_level,
        scenario=sample.scenario,
    )

    for attempt in range(retries + 1):
        try:
            raw = chat(user_prompt, system=_GENERATE_SYSTEM, temperature=0.7, max_tokens=4000)
            result = _parse_llm_json(raw)
            if result.get("files"):
                return result["files"], result.get("expected_issues", [])
        except Exception as e:
            print(f"  [WARN] LLM 调用失败 (attempt {attempt+1}): {e}")
            time.sleep(2)
    return [], []


def _generate_non_python(sample, language: str, ext: str, retries=2) -> tuple[list[dict], list[dict]]:
    """生成非 Python 代码。"""
    inp = sample.input
    risk_signals = inp.get("risk_signals", [])
    risk_desc = "\n".join(f"- {s}: {_RISK_PATTERN_DESC.get(s, s)}" for s in risk_signals) or "无特定漏洞需求"

    system = _NON_PYTHON_SYSTEM.format(language=language, ext=ext)
    user = _GENERATE_USER.format(
        language=language,
        file_exts=ext,
        file_count=inp["diff_size"]["files"],
        python_count=0,
        change_type=inp.get("change_summary", "feature"),
        added_lines=inp["diff_size"]["added_lines"],
        deleted_lines=inp["diff_size"]["deleted_lines"],
        risk_signals_desc=risk_desc,
        risk_level="low",
        scenario=sample.scenario,
    )

    for attempt in range(retries + 1):
        try:
            raw = chat(user, system=system, temperature=0.7, max_tokens=4000)
            result = _parse_llm_json(raw)
            if result.get("files"):
                return result["files"], result.get("expected_issues", [])
        except Exception as e:
            print(f"  [WARN] LLM 调用失败 (attempt {attempt+1}): {e}")
            time.sleep(2)
    return [], []


def _lang_from_file_types(file_types: list[str]) -> tuple[str, str]:
    """从 file_types 推断语言和主扩展名。"""
    ext_map = {".py": ("Python", ".py"), ".js": ("JavaScript", ".js"),
               ".ts": ("TypeScript", ".ts"), ".java": ("Java", ".java"),
               ".go": ("Go", ".go")}
    for ft in file_types:
        if ft in ext_map:
            return ext_map[ft]
    return ("Text", file_types[0] if file_types else ".txt")


def generate(count: int = 40, dry_run: bool = False, output_dir: str | None = None) -> list[dict]:
    """主入口：选取样本参数，生成代码，初始化 git repo。

    Args:
        count: 目标样本数量
        dry_run: 只打印参数不调 LLM
        output_dir: 样本输出目录，默认 ~/.eval_report_samples
    """
    samples_dir = Path(output_dir) if output_dir else _DEFAULT_SAMPLES_DIR
    samples_dir.mkdir(parents=True, exist_ok=True)

    params = _select_params(count)
    print(f"选取 {len(params)} 条样本参数（目标 {count} 条）")
    print(f"输出目录: {samples_dir}\n")

    if dry_run:
        for i, s in enumerate(params):
            inp = s.input
            gt = s.ground_truth
            print(f"[{i+1}] {s.id}")
            print(f"    语言: {inp.get('file_types')}  风险: {gt.get('risk_level')}  信号: {gt.get('reason_codes')}")
            print(f"    文件数: {inp['diff_size']['files']}  行数: +{inp['diff_size']['added_lines']}/-{inp['diff_size']['deleted_lines']}")
        return []

    generated = []
    for i, s in enumerate(params):
        sample_id = s.id
        print(f"[{i+1}/{len(params)}] {sample_id} ...", end=" ", flush=True)

        inp = s.input
        file_types = inp.get("file_types", [])
        has_py = any(ft == ".py" for ft in file_types)

        if has_py:
            files, expected = _generate_python(s)
        else:
            lang, ext = _lang_from_file_types(file_types)
            files, expected = _generate_non_python(s, lang, ext)

        if not files:
            print("SKIP (无文件生成)")
            continue

        sample_dir = _make_sample_dir(sample_id, samples_dir)
        ok = _init_git_repo(sample_dir, files, expected)

        if ok:
            n_issues = len(expected)
            print(f"OK ({len(files)} files, {n_issues} expected issues)")
            generated.append({
                "sample_id": sample_id,
                "dir": str(sample_dir),
                "expected_issues_count": n_issues,
                "file_count": len(files),
            })
        else:
            print("FAIL (git init 失败)")

        time.sleep(0.5)  # 避免 API 限流

    # 保存索引
    index_path = samples_dir / "_index.json"
    index_path.write_text(json.dumps(generated, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n完成: {len(generated)}/{len(params)} 条样本生成成功")
    print(f"样本目录: {samples_dir}")
    print(f"索引文件: {index_path}")
    return generated


# ---- CLI ----

if __name__ == "__main__":
    import argparse

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Report 级评测 — 代码样本生成")
    parser.add_argument("--count", type=int, default=40, help="生成样本数量（默认 40）")
    parser.add_argument("--dry-run", action="store_true", help="只打印参数，不调 LLM")
    parser.add_argument("--output-dir", type=str, default=None, help="样本输出目录（默认 ~/.eval_report_samples）")
    args = parser.parse_args()

    generate(count=args.count, dry_run=args.dry_run, output_dir=args.output_dir)
