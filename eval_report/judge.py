"""LLM-as-Judge — 用强模型评判 Pipeline 产出的 Issue 质量.

对每个样本:
1. 读取样本的 git diff（HEAD~1..HEAD 的完整变更）
2. 读取 Pipeline 产出的 Issues
3. 喂给 LLM Judge，对每个 Issue 打标: correct / false_positive / uncertain
4. Judge 同时指出遗漏的问题 (missed)

用法:
    python -m eval_report.judge                         # 评判全部样本
    python -m eval_report.judge --limit 5               # 只评判前 5 个
    python -m eval_report.judge --sample s001_simple    # 只评判指定样本
"""

import json
import re
import sys
import time
from pathlib import Path

from app.tools.llm_tool import chat

ROOT = Path(__file__).resolve().parent
SAMPLES_DIR = ROOT / "samples"
RESULTS_DIR = ROOT / "results"

_JUDGE_SYSTEM = """\
你是一个代码审查质量评估专家。你的任务是评估自动化代码审查工具产出的 Issue 质量。

你会收到:
1. 一段 git diff（代码变更的完整内容，含文件名、行号、变更的代码行）
2. 自动化工具产出的 Issue 列表（每个 Issue 含: 标题、严重度、文件路径、行号、原因描述、修复建议、来源工具）

对每个 Issue，你必须在以下三个判定中选择一个:
- "correct": Issue 确实指出了真实的安全或质量问题
- "false_positive": Issue 的结论是错误的——代码实际没有问题，或者问题被误判
- "uncertain": 无法确定（例如需要更多上下文才能判断）

此外，你需要通读完整 diff，判断是否有明显的问题被自动化工具遗漏:
- missed: 列出遗漏的问题（如果能识别的话），每个含 description、severity、file、approximate_line

判断原则:
1. 安全漏洞（SQL注入、命令注入、反序列化、硬编码密钥等）即使在实际环境中利用难度高，也应标记为 correct
2. 代码风格问题（如行太长、变量命名）如果确实违反规范，标记为 correct
3. 如果 Issue 指向的位置和描述的问题确实匹配，标记为 correct
4. 如果 Issue 声称有 SQL 注入但实际代码已做参数化处理，标记为 false_positive
5. 不要因为"这个问题不重要"而判 false_positive——只因为"这个问题不存在"才判 false_positive

输出格式: 严格 JSON
{
  "per_issue": [
    {"issue_index": 0, "verdict": "correct", "reason": "一行简短理由"}
  ],
  "missed": [
    {"description": "遗漏问题的描述", "severity": "medium", "file": "src/auth.py", "approximate_line": 25}
  ],
  "overall_assessment": "一句话评估此次审查的整体质量"
}

注意:
- issue_index 对应输入中 Issue 的顺序（从 0 开始）
- 如果所有 Issue 都正确且没有遗漏，missed 可以为空数组
- reason 用中文写，简短一句话即可
"""

_JUDGE_USER = """\
## Git Diff

{diff}

## 自动化工具产出的 Issues

{issues_text}
"""


def _load_diff(pipeline_output: dict) -> str | None:
    """从 Pipeline 结果中读取持久化的 unified_diff，若缺失则返回 None。

    Judge 禁止回到样本仓库自行运行 `git diff`，因为样本仓库可能已被清理为非 git 目录。
    """
    diff = pipeline_output.get("unified_diff", "")
    if not diff:
        return None
    # 限制 6000 字符（避免超过 LLM 上下文）
    if len(diff) > 6000:
        diff = diff[:6000] + "\n... (diff truncated)"
    return diff


def _format_issues(issues: list[dict]) -> str:
    """将 Issue 列表格式化为 LLM 友好的文本。"""
    if not issues:
        return "（此次审查未发现任何 Issue）"

    lines = []
    for idx, issue in enumerate(issues):
        severity = issue.get("severity", "?")
        title = issue.get("title", "无标题")
        file = issue.get("file", "?")
        line = issue.get("line", "?")
        reason = issue.get("reason", "")
        fix = issue.get("fix", "")
        source = ", ".join(issue.get("source", []))

        lines.append(f"--- Issue #{idx} ---")
        lines.append(f"  严重度: {severity} | 文件: {file}:{line} | 来源: {source}")
        lines.append(f"  标题: {title}")
        if reason:
            lines.append(f"  原因: {reason}")
        if fix:
            lines.append(f"  修复建议: {fix}")
        lines.append("")
    return "\n".join(lines)


def _parse_judge_json(raw: str) -> dict:
    """解析 Judge 输出。"""
    text = raw.strip()
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "per_issue": [],
            "missed": [],
            "overall_assessment": f"JSON 解析失败: {raw[:200]}",
            "_parse_error": True,
        }


def judge_one(sample_id: str, pipeline_output: dict, retries=0) -> dict:
    """对单个样本的 Pipeline 输出进行评判。"""
    issues = pipeline_output.get("issues", [])

    diff = _load_diff(pipeline_output)
    if diff is None:
        return {
            "sample_id": sample_id,
            "error": "Pipeline 结果缺少 unified_diff — 请用修正后的 run_pipeline.py 重新运行",
            "per_issue": [], "missed": [], "overall_assessment": "",
        }

    issues_text = _format_issues(issues)

    user_prompt = _JUDGE_USER.format(diff=diff, issues_text=issues_text)

    for attempt in range(retries + 1):
        try:
            # glm-4.5-air 是推理模型：思考内容与正文共用 max_tokens 预算，个别样本
            # 思考会耗尽全部预算导致正文为空（曾造成批量 JSON 解析失败）。
            # Judge 只需要确定性的短 JSON 标注，直接关闭 thinking。
            raw = chat(user_prompt, system=_JUDGE_SYSTEM, temperature=0.2,
                       max_tokens=1500, timeout=120.0,
                       extra_body={"thinking": {"type": "disabled"}})
            result = _parse_judge_json(raw)
            result["sample_id"] = sample_id
            result["_raw"] = raw[:500]
            result["_issues_count"] = len(issues)
            return result
        except Exception as e:
            print(f"  [WARN] Judge 调用失败 (attempt {attempt+1}): {e}")
            time.sleep(2)

    return {
        "sample_id": sample_id,
        "error": "Judge 调用全部失败",
        "per_issue": [], "missed": [], "overall_assessment": "",
    }


def _load_pipeline_results(results_dir: Path = RESULTS_DIR) -> list[dict]:
    """加载所有 Pipeline 运行结果。"""
    if not results_dir.exists():
        return []
    results = []
    for f in sorted(results_dir.glob("*_pipeline_output.json")):
        results.append(json.loads(f.read_text(encoding="utf-8")))
    return results


def judge(limit: int | None = None, sample_id: str | None = None,
          results_dir: str | None = None):
    """批量评判 Pipeline 输出。"""
    output_dir = Path(results_dir) if results_dir else RESULTS_DIR
    results = _load_pipeline_results(output_dir)
    if not results:
        print("没有找到 Pipeline 运行结果。请先运行 run_pipeline.py")
        return

    if sample_id:
        results = [r for r in results if r.get("sample_id") == sample_id]

    if limit:
        results = results[:limit]

    print(f"评判 {len(results)} 个样本的 Issue 质量 ...\n")

    judged = []
    for i, r in enumerate(results):
        sid = r.get("sample_id", f"unknown_{i}")
        n_issues = len(r.get("issues", []))
        print(f"[{i+1}/{len(results)}] {sid} ({n_issues} issues) ...", end=" ", flush=True)

        judgment = judge_one(sid, r)

        # 保存评判结果
        out_path = output_dir / f"{sid}_judgment.json"
        out_path.write_text(json.dumps(judgment, ensure_ascii=False, indent=2), encoding="utf-8")

        if judgment.get("error"):
            print(f"ERROR: {judgment['error']}")
        else:
            correct = sum(1 for pi in judgment.get("per_issue", []) if pi["verdict"] == "correct")
            fp = sum(1 for pi in judgment.get("per_issue", []) if pi["verdict"] == "false_positive")
            unc = sum(1 for pi in judgment.get("per_issue", []) if pi["verdict"] == "uncertain")
            missed = len(judgment.get("missed", []))
            print(f"OK (correct={correct}, fp={fp}, uncertain={unc}, missed={missed})")

        judged.append(judgment)
        time.sleep(0.5)

    # 保存汇总
    summary = _build_judge_summary(judged)
    (output_dir / "_judge_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n评判完成: {len(judged)} 个样本")
    print(f"结果目录: {output_dir}")


def _build_judge_summary(judged: list[dict]) -> dict:
    """生成 Judge 评判汇总。"""
    total_issues = 0
    correct = 0
    false_positive = 0
    uncertain = 0
    total_missed = 0
    errors = 0

    for j in judged:
        if j.get("error") or j.get("_parse_error"):
            errors += 1
            continue
        for pi in j.get("per_issue", []):
            total_issues += 1
            v = pi["verdict"]
            if v == "correct":
                correct += 1
            elif v == "false_positive":
                false_positive += 1
            else:
                uncertain += 1
        total_missed += len(j.get("missed", []))

    return {
        "samples_judged": len(judged),
        "samples_with_errors": errors,
        "total_issues_evaluated": total_issues,
        "correct": correct,
        "false_positive": false_positive,
        "uncertain": uncertain,
        "precision": round(correct / (correct + false_positive), 4) if (correct + false_positive) > 0 else 0,
        "total_missed": total_missed,
    }


# ---- CLI ----

if __name__ == "__main__":
    import argparse

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Report 级评测 — LLM-as-Judge")
    parser.add_argument("--limit", type=int, default=None, help="只评判前 N 个样本")
    parser.add_argument("--sample", type=str, default=None, help="只评判指定样本 ID")
    parser.add_argument("--results-dir", type=str, default=None)
    args = parser.parse_args()

    judge(limit=args.limit, sample_id=args.sample, results_dir=args.results_dir)
