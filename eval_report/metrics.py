"""Issue 级指标计算与报告生成 — 综合 Pipeline 输出 + Judge 评判结果.

用法:
    python -m eval_report.metrics                         # 生成 Markdown + JSON 报告
    python -m eval_report.metrics --json-only             # 只生成 JSON
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"
REPORTS_DIR = ROOT / "reports"


def _load_data(results_dir: Path | None = None) -> list[dict]:
    """加载所有样本的 pipeline_output + judgment 配对数据。"""
    rdir = Path(results_dir) if results_dir else RESULTS_DIR
    if not rdir.exists():
        return []

    paired = []
    pipeline_files = sorted(rdir.glob("*_pipeline_output.json"))
    for pf in pipeline_files:
        sid = pf.stem.replace("_pipeline_output", "")
        jf = rdir / f"{sid}_judgment.json"

        pipeline = json.loads(pf.read_text(encoding="utf-8"))
        judgment = None
        if jf.exists():
            judgment = json.loads(jf.read_text(encoding="utf-8"))
        paired.append({"sample_id": sid, "pipeline": pipeline, "judgment": judgment})
    return paired


def compute_metrics(paired: list[dict]) -> dict:
    """计算 Issue 级别各项指标。"""
    total_issues = 0
    correct = 0
    false_positive = 0
    uncertain = 0
    total_missed = 0
    missed_high = 0

    # 按工具分组
    by_tool: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0, "fp": 0, "uncertain": 0})

    # 按严重度分组
    by_severity: dict[str, dict] = defaultdict(lambda: {"total": 0, "correct": 0, "fp": 0, "uncertain": 0})

    # 按样本分组
    per_sample: list[dict] = []

    # 高风险样本（ground truth risk_level=high）的相关指标
    high_risk_samples = 0
    high_risk_issues_detected = 0
    high_risk_missed = 0

    judged_count = 0
    error_count = 0
    pipeline_failures = 0

    for p in paired:
        sample_id = p["sample_id"]
        pipeline = p["pipeline"]
        judgment = p.get("judgment")

        # 统计 Pipeline 失败
        if pipeline.get("error"):
            pipeline_failures += 1
            continue

        issues = pipeline.get("issues", [])

        if not judgment:
            if issues:
                per_sample.append({
                    "sample_id": sample_id,
                    "issues_count": len(issues),
                    "judged": False,
                    "note": "未评判",
                })
            continue

        if judgment.get("error") or judgment.get("_parse_error"):
            error_count += 1
            per_sample.append({
                "sample_id": sample_id,
                "issues_count": len(issues),
                "judged": False,
                "note": judgment.get("error", "Judge 解析错误"),
            })
            continue

        judged_count += 1
        per_issue_verdicts = judgment.get("per_issue", [])
        missed_list = judgment.get("missed", [])

        sample_correct = 0
        sample_fp = 0
        sample_unc = 0

        for pi in per_issue_verdicts:
            idx = pi.get("issue_index", 0)
            verdict = pi["verdict"]
            reason = pi.get("reason", "")

            total_issues += 1
            if verdict == "correct":
                correct += 1
                sample_correct += 1
            elif verdict == "false_positive":
                false_positive += 1
                sample_fp += 1
            else:
                uncertain += 1
                sample_unc += 1

            # 按工具统计
            if idx < len(issues):
                issue = issues[idx]
                raw_sources = issue.get("source", [])
                # 兼容字符串和列表两种格式
                if isinstance(raw_sources, str):
                    sources = [raw_sources]
                else:
                    sources = raw_sources
                sev = issue.get("severity", "info")
                for src in sources:
                    by_tool[src]["total"] += 1
                    if verdict == "correct":
                        by_tool[src]["correct"] += 1
                    elif verdict == "false_positive":
                        by_tool[src]["fp"] += 1
                    else:
                        by_tool[src]["uncertain"] += 1

                by_severity[sev]["total"] += 1
                if verdict == "correct":
                    by_severity[sev]["correct"] += 1
                elif verdict == "false_positive":
                    by_severity[sev]["fp"] += 1
                else:
                    by_severity[sev]["uncertain"] += 1

        # 遗漏统计
        total_missed += len(missed_list)
        for m in missed_list:
            if m.get("severity") in ("high", "critical"):
                missed_high += 1

        # 高风险样本（通过 plan 中的 risk_level 判断）
        plan = pipeline.get("plan", {}) or {}
        if plan.get("risk_level") == "high":
            high_risk_samples += 1
            high_risk_issues_detected += sample_correct
            high_risk_missed += len(missed_list)

        per_sample.append({
            "sample_id": sample_id,
            "plan_risk_level": plan.get("risk_level"),
            "issues_count": len(issues),
            "correct": sample_correct,
            "false_positive": sample_fp,
            "uncertain": sample_unc,
            "missed": len(missed_list),
            "judged": True,
        })

    # 计算聚合指标
    rated_total = correct + false_positive  # 排除 uncertain

    # Precision = correct / (correct + false_positive)
    precision = round(correct / rated_total, 4) if rated_total > 0 else 0.0

    # Recall = correct / (correct + missed) (近似)
    recall = round(correct / (correct + total_missed), 4) if (correct + total_missed) > 0 else 0.0

    f1 = round(2 * precision * recall / (precision + recall), 4) if (precision + recall) > 0 else 0.0

    # 按工具的 precision
    tool_precision = {}
    for tool, stats in sorted(by_tool.items()):
        rated = stats["correct"] + stats["fp"]
        tool_precision[tool] = {
            "total": stats["total"],
            "correct": stats["correct"],
            "false_positive": stats["fp"],
            "uncertain": stats["uncertain"],
            "precision": round(stats["correct"] / rated, 4) if rated > 0 else 0.0,
        }

    # 按严重度的 precision
    severity_precision = {}
    for sev in ["critical", "high", "medium", "low", "info"]:
        stats = by_severity.get(sev)
        if stats and stats["total"] > 0:
            rated = stats["correct"] + stats["fp"]
            severity_precision[sev] = {
                "total": stats["total"],
                "correct": stats["correct"],
                "false_positive": stats["fp"],
                "uncertain": stats["uncertain"],
                "precision": round(stats["correct"] / rated, 4) if rated > 0 else 0.0,
            }

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sample_count": len(paired),
        "judged_samples": judged_count,
        "pipeline_failures": pipeline_failures,
        "judge_errors": error_count,
        "total_issues_produced": total_issues,
        "correct": correct,
        "false_positive": false_positive,
        "uncertain": uncertain,
        "total_missed": total_missed,
        "missed_high_severity": missed_high,
        "issue_precision": precision,
        "issue_recall": recall,
        "issue_f1": f1,
        "false_positive_rate": round(false_positive / total_issues, 4) if total_issues > 0 else 0.0,
        "high_risk": {
            "samples": high_risk_samples,
            "issues_detected": high_risk_issues_detected,
            "missed": high_risk_missed,
        },
        "by_tool": tool_precision,
        "by_severity": severity_precision,
        "per_sample": per_sample,
    }


def generate_report(metrics: dict) -> str:
    """生成 Markdown 格式的评测报告。"""
    lines = [
        "# Report 级评测报告",
        "",
        f"**生成时间**: {metrics.get('generated_at', '?')}",
        "",
        "## 概览",
        "",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| 样本总数 | {metrics['sample_count']} |",
        f"| 已评判样本 | {metrics['judged_samples']} |",
        f"| Pipeline 失败 | {metrics['pipeline_failures']} |",
        f"| Judge 错误 | {metrics['judge_errors']} |",
        f"| 总 Issue 产出 | {metrics['total_issues_produced']} |",
        f"| Judge 认定正确 | {metrics['correct']} |",
        f"| Judge 认定误报 | {metrics['false_positive']} |",
        f"| Judge 无法确定 | {metrics['uncertain']} |",
        f"| 遗漏问题数 | {metrics['total_missed']} |",
        "",
        "## 核心指标",
        "",
        f"| 指标 | 值 |",
        f"|------|-----|",
        f"| **Issue Precision** | **{metrics['issue_precision']:.2%}** |",
        f"| **Issue Recall** (估算) | **{metrics['issue_recall']:.2%}** |",
        f"| **Issue F1** | **{metrics['issue_f1']:.2%}** |",
        f"| 误报率 (FPR) | {metrics['false_positive_rate']:.2%} |",
        f"| 遗漏高严重度问题 | {metrics['missed_high_severity']} |",
        "",
    ]

    # 高风险样本
    hr = metrics.get("high_risk", {})
    if hr.get("samples", 0) > 0:
        lines.extend([
            "## 高风险样本",
            "",
            f"| 指标 | 值 |",
            f"|------|-----|",
            f"| 高风险样本数 | {hr['samples']} |",
            f"| 检出 Issue 数 | {hr['issues_detected']} |",
            f"| 遗漏问题数 | {hr['missed']} |",
            "",
        ])

    # 按工具
    by_tool = metrics.get("by_tool", {})
    if by_tool:
        lines.extend([
            "## 按工具 Precision",
            "",
            "| 工具 | Issue 数 | Correct | FP | Uncertain | Precision |",
            "|------|---------|---------|-----|-----------|-----------|",
        ])
        for tool, stats in sorted(by_tool.items()):
            lines.append(
                f"| {tool} | {stats['total']} | {stats['correct']} | "
                f"{stats['false_positive']} | {stats['uncertain']} | "
                f"{stats['precision']:.2%} |"
            )
        lines.append("")

    # 按严重度
    by_sev = metrics.get("by_severity", {})
    if by_sev:
        lines.extend([
            "## 按严重度 Precision",
            "",
            "| 严重度 | Issue 数 | Correct | FP | Uncertain | Precision |",
            "|--------|---------|---------|-----|-----------|-----------|",
        ])
        for sev, stats in sorted(by_sev.items()):
            lines.append(
                f"| {sev} | {stats['total']} | {stats['correct']} | "
                f"{stats['false_positive']} | {stats['uncertain']} | "
                f"{stats['precision']:.2%} |"
            )
        lines.append("")

    # 逐样本
    lines.extend([
        "## 逐样本明细",
        "",
        "| 样本 ID | 风险等级 | Issues | Correct | FP | Unc | Missed |",
        "|---------|---------|--------|---------|-----|-----|--------|",
    ])
    for ps in metrics.get("per_sample", []):
        if ps.get("judged"):
            lines.append(
                f"| {ps['sample_id']} | {ps.get('plan_risk_level', '?')} | "
                f"{ps['issues_count']} | {ps['correct']} | {ps['false_positive']} | "
                f"{ps['uncertain']} | {ps['missed']} |"
            )
        else:
            lines.append(
                f"| {ps['sample_id']} | - | {ps['issues_count']} | - | - | - | "
                f"{ps.get('note', '未评判')} |"
            )
    lines.append("")

    # 说明
    lines.extend([
        "## 指标说明",
        "",
        "- **Issue Precision**: `correct / (correct + false_positive)`，排除 uncertain。衡量产出的 Issue 中有多少是真正有问题的。",
        "- **Issue Recall**: `correct / (correct + missed)`，粗略估算。missed 来自 Judge 的补充判断，不代表 100% 覆盖。",
        "- **FPR (False Positive Rate)**: `false_positive / total_issues`。",
        "- **Uncertain**: Judge 无法确定的边界情况，通常需要人工判断。",
        "",
        "> 注意: 所有指标依赖 LLM-as-Judge 的判断，Judge 自身可能有偏差。建议定期人工抽检 Judge 结果以校准。",
        "",
    ])

    return "\n".join(lines)


def run(json_only: bool = False, results_dir: str | None = None):
    """主入口：加载数据 → 计算指标 → 生成报告。"""
    paired = _load_data(Path(results_dir) if results_dir else None)
    if not paired:
        print("没有找到数据。请先运行 run_pipeline.py 和 judge.py。")
        return

    metrics = compute_metrics(paired)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 保存 JSON
    json_path = REPORTS_DIR / f"report_{ts}.json"
    json_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 报告: {json_path}")

    if not json_only:
        report = generate_report(metrics)
        md_path = REPORTS_DIR / f"report_{ts}.md"
        md_path.write_text(report, encoding="utf-8")
        print(f"Markdown 报告: {md_path}")
        print()
        print(report)

    # 打印核心数据
    print(f"\n核心结果: Precision={metrics['issue_precision']:.2%}, "
          f"Recall={metrics['issue_recall']:.2%}, F1={metrics['issue_f1']:.2%}")


# ---- CLI ----

if __name__ == "__main__":
    import argparse

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Report 级评测 — 指标计算与报告")
    parser.add_argument("--json-only", action="store_true", help="只输出 JSON 报告")
    parser.add_argument("--results-dir", type=str, default=None,
                        help="结果目录（如 eval_report/results/static）")
    args = parser.parse_args()

    run(json_only=args.json_only, results_dir=args.results_dir)
