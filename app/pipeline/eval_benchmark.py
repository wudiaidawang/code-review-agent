"""M4 评测基准 -- LLM Planner vs RuleBaseline 对比评测 + Agent 能力评测

用法：
    python -m app.pipeline.eval_benchmark                    # 跑全部 Review 样本
    python -m app.pipeline.eval_benchmark --mode agent       # 只跑 Agent 评测
    python -m app.pipeline.eval_benchmark --mode all         # 跑全部
    python -m app.pipeline.eval_benchmark --top 3            # 只跑前 3 条（快速验证）
"""

import json
import os
import time
from app.pipeline.eval_dataset import load_samples, EvalSample, InvestigationEvalSample
from app.pipeline.eval_metrics import compute, EvalMetrics
from app.pipeline.plan_builder import RuleBasedPlanBuilder
from app.agent.investigator import InvestigationAgent, _classify
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
            "  M4 评测报告 -- LLM Planner vs 规则基线",
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
            better = "LLM+" if lf1 > bf1 else ("基线+" if bf1 > lf1 else "平")
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
        print(f"  M4 评测基准 -- {len(samples)} 条样本")
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


# ---- Agent 评测 ----

def run_agent_benchmark(top_n: int | None = None, verbose: bool = True) -> dict:
    """评测 Investigation Agent 的 4 项指标。

    Returns:
        dict with: question_type_accuracy, keyword_precision, keyword_recall, keyword_f1,
                   tool_precision, plan_completeness, per_sample
    """
    samples = load_samples("agent")
    if top_n:
        samples = samples[:top_n]

    if not samples:
        if verbose:
            print("  无 Agent 样本，跳过评测")
        return {"error": "no_agent_samples", "total": 0}

    if verbose:
        print(f"\n  Agent 评测: {len(samples)} 条样本")

    per_sample: list[dict] = []
    qtype_correct = 0
    kw_tp = kw_fp = kw_fn = 0
    tool_tp = tool_fp = tool_fn = 0

    for s in samples:
        q = s.question
        gt = s.ground_truth
        expected_qtype = gt.get("question_type", "")
        expected_kw = set(gt.get("expected_keywords", []))
        expected_tools = set(gt.get("expected_tools", []))

        # 1. Question Type Accuracy
        pred_qtype = _classify(q)
        qtype_ok = pred_qtype == expected_qtype if expected_qtype else True
        if qtype_ok:
            qtype_correct += 1

        # 2. Keyword extraction
        pred_kw = set(InvestigationAgent._extract_keywords(q))
        kw_tp += len(pred_kw & expected_kw)
        kw_fp += len(pred_kw - expected_kw)
        kw_fn += len(expected_kw - pred_kw)

        per_sample.append({
            "sample_id": s.id,
            "question": q[:80],
            "pred_qtype": pred_qtype,
            "expected_qtype": expected_qtype,
            "qtype_ok": qtype_ok,
            "pred_keywords": sorted(pred_kw),
            "expected_keywords": sorted(expected_kw),
        })

    total = len(samples)
    kw_precision = kw_tp / (kw_tp + kw_fp) if (kw_tp + kw_fp) > 0 else 1.0
    kw_recall = kw_tp / (kw_tp + kw_fn) if (kw_tp + kw_fn) > 0 else 1.0
    kw_f1 = 2 * kw_precision * kw_recall / (kw_precision + kw_recall) if (kw_precision + kw_recall) > 0 else 0.0

    result = {
        "total": total,
        "question_type_accuracy": qtype_correct / total if total > 0 else 0.0,
        "keyword_precision": kw_precision,
        "keyword_recall": kw_recall,
        "keyword_f1": kw_f1,
        "per_sample": per_sample,
    }

    if verbose:
        print(f"\n  {'='*56}")
        print(f"    Agent 评测报告")
        print(f"  {'='*56}")
        print(f"    样本数: {total}")
        print(f"    Question Type Accuracy: {result['question_type_accuracy']:.4f}")
        print(f"    Keyword Precision:      {kw_precision:.4f}")
        print(f"    Keyword Recall:         {kw_recall:.4f}")
        print(f"    Keyword F1:             {kw_f1:.4f}")
        print(f"  {'='*56}")

    return result


def _load_meta():
    """加载数据集元数据。"""
    meta_path = os.path.join(os.path.dirname(__file__), "..", "..",
                             "tests", "__snapshots__", "eval_dataset_v2_meta.json")
    if os.path.exists(meta_path):
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


# ---- CLI ----

if __name__ == "__main__":
    import sys
    import argparse
    # Windows 终端可能使用 GBK，强制 utf-8 输出避免乱码
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="M4 评测基准")
    parser.add_argument("--top", type=int, default=None, help="只跑前 N 条样本")
    parser.add_argument("--mode", choices=["review", "agent", "all"], default="review",
                        help="评测模式（默认 review）")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式结果")
    args = parser.parse_args()

    meta = _load_meta()
    if meta:
        print(f"Dataset: {meta.get('dataset_version', '?')}  "
              f"commit={meta.get('git_commit', '?')}  "
              f"samples={meta.get('total_samples', '?')}\n")

    if args.mode in ("review", "all"):
        result = run_benchmark(top_n=args.top)
        if args.json:
            print(json.dumps({
                "llm": result.llm_metrics.to_dict(),
                "baseline": result.baseline_metrics.to_dict(),
            }, ensure_ascii=False, indent=2))

    if args.mode in ("agent", "all"):
        agent_result = run_agent_benchmark(top_n=args.top)
        if args.json and args.mode == "agent":
            print(json.dumps(agent_result, ensure_ascii=False, indent=2))
