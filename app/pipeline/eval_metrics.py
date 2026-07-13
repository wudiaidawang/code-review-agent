"""M4 评测指标 — precision/recall/F1 for analyzer selection, risk_level, reason_codes."""

from dataclasses import dataclass, field


@dataclass
class EvalMetrics:
    """一次评测运行的聚合指标。"""
    total_samples: int = 0

    # Analyzer 选择指标（按样本 set 计算，取平均）
    analyzer_precision: float = 0.0
    analyzer_recall: float = 0.0
    analyzer_f1: float = 0.0

    # 风险等级准确率
    risk_level_accuracy: float = 0.0

    # 高风险工具召回率（需 bandit 的样本中 bandit 被选中的比例）
    high_risk_recall: float = 0.0

    # Reason codes 指标
    reason_precision: float = 0.0
    reason_recall: float = 0.0
    reason_f1: float = 0.0

    # 逐样本明细
    per_sample: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_samples": self.total_samples,
            "analyzer_precision": round(self.analyzer_precision, 4),
            "analyzer_recall": round(self.analyzer_recall, 4),
            "analyzer_f1": round(self.analyzer_f1, 4),
            "risk_level_accuracy": round(self.risk_level_accuracy, 4),
            "high_risk_recall": round(self.high_risk_recall, 4),
            "reason_precision": round(self.reason_precision, 4),
            "reason_recall": round(self.reason_recall, 4),
            "reason_f1": round(self.reason_f1, 4),
            "per_sample": self.per_sample,
        }


# 高风险 reason_codes — 这些场景下 bandit 不应被跳过
_HIGH_RISK_CODES = {"auth_change", "command_injection", "sql_risk", "deserialization"}


def _set_precision(predicted: set, ground: set) -> float:
    if not predicted:
        return 0.0
    return len(predicted & ground) / len(predicted)


def _set_recall(predicted: set, ground: set) -> float:
    if not ground:
        return 1.0
    return len(predicted & ground) / len(ground)


def _set_f1(p: float, r: float) -> float:
    if p + r == 0:
        return 0.0
    return 2 * p * r / (p + r)


def compute(predictions: list[dict], ground_truths: list[dict]) -> EvalMetrics:
    """计算评测指标。

    Args:
        predictions: 预测的 ReviewPlan dict 列表（含 analyzers/risk_level/reason_codes）
        ground_truths: 标注的 ground_truth dict 列表
    """
    n = len(predictions)
    if n == 0:
        return EvalMetrics()

    ap_sum = ar_sum = af_sum = 0.0
    rl_correct = 0
    hr_hits = 0
    hr_total = 0
    rp_sum = rr_sum = rf_sum = 0.0
    per_sample: list[dict] = []

    for i, (pred, gt) in enumerate(zip(predictions, ground_truths)):
        p_analyzers = set(pred.get("analyzers", []))
        g_analyzers = set(gt.get("analyzers", []))

        p_reasons = set(pred.get("reason_codes", []))
        g_reasons = set(gt.get("reason_codes", []))

        # Analyzer 指标
        ap = _set_precision(p_analyzers, g_analyzers)
        ar = _set_recall(p_analyzers, g_analyzers)
        af = _set_f1(ap, ar)
        ap_sum += ap
        ar_sum += ar
        af_sum += af

        # 风险等级
        if pred.get("risk_level") == gt.get("risk_level"):
            rl_correct += 1

        # 高风险召回：ground truth 含高风险 reason_code 时，bandit 必须在
        if g_reasons & _HIGH_RISK_CODES or gt.get("risk_level") == "high":
            hr_total += 1
            if "bandit" in p_analyzers:
                hr_hits += 1

        # Reason codes 指标
        rp = _set_precision(p_reasons, g_reasons)
        rr = _set_recall(p_reasons, g_reasons)
        rf = _set_f1(rp, rr)
        rp_sum += rp
        rr_sum += rr
        rf_sum += rf

        per_sample.append({
            "sample_id": pred.get("id", f"s{i:03d}"),
            "predicted_analyzers": sorted(p_analyzers),
            "ground_analyzers": sorted(g_analyzers),
            "analyzer_precision": round(ap, 4),
            "analyzer_recall": round(ar, 4),
            "analyzer_f1": round(af, 4),
            "predicted_risk": pred.get("risk_level"),
            "ground_risk": gt.get("risk_level"),
            "predicted_reasons": sorted(p_reasons),
            "ground_reasons": sorted(g_reasons),
        })

    return EvalMetrics(
        total_samples=n,
        analyzer_precision=ap_sum / n,
        analyzer_recall=ar_sum / n,
        analyzer_f1=af_sum / n,
        risk_level_accuracy=rl_correct / n,
        high_risk_recall=hr_hits / hr_total if hr_total > 0 else 1.0,
        reason_precision=rp_sum / n,
        reason_recall=rr_sum / n,
        reason_f1=rf_sum / n,
        per_sample=per_sample,
    )
