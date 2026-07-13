"""M4 评测基准 — LLM Planner vs RuleBaseline 对比评测

用法：
    python -m app.pipeline.eval_benchmark          # 跑全部 10 条样本
    python -m app.pipeline.eval_benchmark --top 3  # 只跑前 3 条（快速验证）
"""

import json
import time
from app.pipeline.eval_dataset import load_samples
from app.pipeline.eval_metrics import compute, EvalMetrics
from app.pipeline.plan_builder import RuleBasedPlanBuilder
from app.tools.llm_tool import chat


# ---- LLM Planner ----

_SYSTEM_PROMPT = """\
你是一个代码审查策略生成器。根据变更特征，输出一个 JSON 格式的 ReviewPlan。

可选 analyzers: git, python_ast, ruff, bandit
风险等级 risk_level: low, medium, high
reason_codes: auth_change, sql_risk, command_injection, deserialization, dependency_change, no_python_changes, python_ast_skipped_large_diff, bandit_skipped_low_risk

规则：
1. 有 .py 文件 → 必选 git + ruff；文件数 ≤50 → 加 python_ast，否则 reason_codes 加 python_ast_skipped_large_diff
2. 有安全风险信号 → 加 bandit；无风险信号且变更量 ≤100 行 → 跳过 bandit（reason 写 bandit_skipped_low_risk）
3. 无 .py 文件 → 只选 git，reason 写 no_python_changes
4. risk_level: ≥3 个风险信号 → high；≥1 个 → medium；0 个 → low
5. reason_codes 按风险信号对应填写

只输出 JSON，不要解释。"""


def _build_user_prompt(sample: dict) -> str:
    inp = sample["input"]
    return f"""\
变更摘要: {inp["change_summary"]}
文件类型: {json.dumps(inp["file_types"])}
diff 规模: {json.dumps(inp["diff_size"])}
风险信号: {json.dumps(inp["risk_signals"])}
AST 摘要: {inp["ast_summary"]}
静态发现数: {inp["static_findings_count"]}"""


def _parse_llm_output(raw: str, sample_id: str) -> dict:
    """从 LLM 原始输出中提取 ReviewPlan dict。"""
    # 去掉可能的 markdown 包裹
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    try:
        plan = json.loads(text)
    except json.JSONDecodeError:
        return {"id": sample_id, "analyzers": [], "risk_level": "low", "reason_codes": [],
                "_error": f"JSON parse failed: {raw[:100]}"}
    return {
        "id": sample_id,
        "analyzers": plan.get("analyzers", []),
        "risk_level": plan.get("risk_level", "low"),
        "reason_codes": plan.get("reason_codes", []),
    }


def run_llm_planner(samples: list, verbose: bool = True) -> list[dict]:
    """用 LLM API 为每条样本生成 ReviewPlan 预测。"""
    predictions: list[dict] = []
    for i, s in enumerate(samples):
        sample_id = s.id
        if verbose:
            print(f"  [{i+1}/{len(samples)}] {sample_id} ...", end=" ", flush=True)

        try:
            t0 = time.perf_counter()
            raw = chat(_build_user_prompt({"id": s.id, "input": s.input}),
                       system=_SYSTEM_PROMPT, temperature=0.1, max_tokens=500)
            elapsed = time.perf_counter() - t0
            pred = _parse_llm_output(raw, sample_id)
            pred["_latency_ms"] = round(elapsed * 1000)
            if verbose:
                status = "OK" if "_error" not in pred else f"ERR: {pred['_error']}"
                print(f"{status} ({pred['_latency_ms']}ms)")
        except Exception as exc:
            pred = {"id": sample_id, "analyzers": [], "risk_level": "low",
                    "reason_codes": [], "_error": str(exc)}
            if verbose:
                print(f"FAIL: {exc}")

        predictions.append(pred)
    return predictions


# ---- Rule Baseline ----

def run_rule_baseline(samples: list, verbose: bool = True) -> list[dict]:
    """用 RuleBasedPlanBuilder 为每条样本生成 ReviewPlan 预测。"""
    builder = RuleBasedPlanBuilder()
    predictions: list[dict] = []
    for s in samples:
        # 构造最小 ChangeSet dict 供 build() 消费
        inp = s.input
        files = []
        for ft in inp["file_types"]:
            ext = ft.lstrip(".")
            files.append({
                "path": f"changed.{ext}" if ext else "changed.py",
                "change_type": "modified",
                "added_lines": inp["diff_size"]["added_lines"],
                "deleted_lines": inp["diff_size"]["deleted_lines"],
            })
        change_set = {"files": files}
        plan = builder.build(change_set)
        predictions.append({
            "id": s.id,
            "analyzers": plan.analyzers,
            "risk_level": plan.risk_level,
            "reason_codes": plan.reason_codes,
        })
    if verbose:
        print(f"  规则基线: {len(predictions)} 条样本完成")
    return predictions


# ---- Benchmark ----

class BenchmarkResult:
    """一次完整评测运行的结果。"""
    def __init__(self, llm_metrics: EvalMetrics, baseline_metrics: EvalMetrics,
                 llm_predictions: list[dict], baseline_predictions: list[dict],
                 ground_truths: list[dict]):
        self.llm_metrics = llm_metrics
        self.baseline_metrics = baseline_metrics
        self.llm_predictions = llm_predictions
        self.baseline_predictions = baseline_predictions
        self.ground_truths = ground_truths

    def summary(self) -> str:
        """生成对比摘要报告。"""
        lm = self.llm_metrics
        bm = self.baseline_metrics

        def delta(llm_val, base_val) -> str:
            d = llm_val - base_val
            if d > 0:
                return f"+{d:.4f}"
            return f"{d:.4f}"

        lines = [
            "=" * 64,
            "  M4 评测报告 — LLM Planner vs 规则基线",
            "=" * 64,
            "",
            f"  样本数: {lm.total_samples}",
            "",
            "  | 指标              | LLM Planner | 规则基线   | Δ         |",
            "  |-------------------|-------------|------------|-----------|",
            f"  | Analyzer Precision | {lm.analyzer_precision:.4f}      | {bm.analyzer_precision:.4f}     | {delta(lm.analyzer_precision, bm.analyzer_precision):>9} |",
            f"  | Analyzer Recall    | {lm.analyzer_recall:.4f}      | {bm.analyzer_recall:.4f}     | {delta(lm.analyzer_recall, bm.analyzer_recall):>9} |",
            f"  | Analyzer F1        | {lm.analyzer_f1:.4f}      | {bm.analyzer_f1:.4f}     | {delta(lm.analyzer_f1, bm.analyzer_f1):>9} |",
            f"  | Risk Level Acc.    | {lm.risk_level_accuracy:.4f}      | {bm.risk_level_accuracy:.4f}     | {delta(lm.risk_level_accuracy, bm.risk_level_accuracy):>9} |",
            f"  | High-Risk Recall   | {lm.high_risk_recall:.4f}      | {bm.high_risk_recall:.4f}     | {delta(lm.high_risk_recall, bm.high_risk_recall):>9} |",
            f"  | Reason Precision   | {lm.reason_precision:.4f}      | {bm.reason_precision:.4f}     | {delta(lm.reason_precision, bm.reason_precision):>9} |",
            f"  | Reason Recall      | {lm.reason_recall:.4f}      | {bm.reason_recall:.4f}     | {delta(lm.reason_recall, bm.reason_recall):>9} |",
            f"  | Reason F1          | {lm.reason_f1:.4f}      | {bm.reason_f1:.4f}     | {delta(lm.reason_f1, bm.reason_f1):>9} |",
            "",
        ]

        # LLM 调用成本
        latencies = [p.get("_latency_ms", 0) for p in self.llm_predictions]
        errors = [p for p in self.llm_predictions if "_error" in p]
        if latencies:
            avg_lat = sum(latencies) / len(latencies)
            lines.append(f"  LLM 平均延迟: {avg_lat:.0f}ms")
            lines.append(f"  LLM 错误数: {len(errors)}/{len(self.llm_predictions)}")
            lines.append("")

        # 逐样本对比
        lines.append("  --- 逐样本 Analyzer F1 对比 ---")
        for i, (ls, bs) in enumerate(zip(lm.per_sample, bm.per_sample)):
            sid = ls["sample_id"]
            lf1 = ls["analyzer_f1"]
            bf1 = bs["analyzer_f1"]
            better = "LLM ✓" if lf1 > bf1 else ("基线 ✓" if bf1 > lf1 else "平")
            lines.append(f"  {sid}: LLM={lf1:.4f}  基线={bf1:.4f}  {better}")
        lines.append("")
        lines.append("=" * 64)

        return "\n".join(lines)


def run_benchmark(top_n: int | None = None, verbose: bool = True) -> BenchmarkResult:
    """运行完整评测基准。

    Args:
        top_n: 只跑前 N 条样本（None=全部）
        verbose: 是否打印进度
    """
    all_samples = load_samples()
    samples = all_samples[:top_n] if top_n else all_samples

    if verbose:
        print(f"\n{'='*48}")
        print(f"  M4 评测基准 — {len(samples)} 条样本")
        print(f"{'='*48}\n")

    # 1. 规则基线（快速，先跑）
    if verbose:
        print("[1/2] 规则基线 (RuleBasedPlanBuilder):")
    baseline_preds = run_rule_baseline(samples, verbose=verbose)

    # 2. LLM Planner
    if verbose:
        print("\n[2/2] LLM Planner (调用 API):")
    llm_preds = run_llm_planner(samples, verbose=verbose)

    # 3. 提取 ground truths
    ground_truths = [
        {"id": s.id, "analyzers": s.ground_truth["analyzers"],
         "risk_level": s.ground_truth["risk_level"],
         "reason_codes": s.ground_truth["reason_codes"]}
        for s in samples
    ]

    # 4. 计算指标
    llm_metrics = compute(llm_preds, ground_truths)
    baseline_metrics = compute(baseline_preds, ground_truths)

    result = BenchmarkResult(llm_metrics, baseline_metrics, llm_preds, baseline_preds, ground_truths)

    if verbose:
        print("\n" + result.summary())

    return result


# ---- CLI ----

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="M4 评测基准")
    parser.add_argument("--top", type=int, default=None, help="只跑前 N 条样本")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式结果")
    args = parser.parse_args()

    result = run_benchmark(top_n=args.top)

    if args.json:
        print(json.dumps({
            "llm": result.llm_metrics.to_dict(),
            "baseline": result.baseline_metrics.to_dict(),
        }, ensure_ascii=False, indent=2))
