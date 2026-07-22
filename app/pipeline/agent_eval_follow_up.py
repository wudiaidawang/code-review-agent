"""续问质量约束指标 — 在 Judge 判决之后计算。

续问节省率只有在回答质量达标时才算数。旧指标（follow_up_savings_rate）
奖励了"完全不做工具调用"的行为——V11 中 9 条 follow-up 全部 0 步，
Judge 全部 unjudgeable，但指标记为 100% 节省。

新指标：
- follow_up_reuse_success_rate: 复用证据后 answer 达到 correct/partial+grounded 的比例
- follow_up_tool_fallback_rate: 证据不足后恢复工具调用的比例
- quality_preserving_savings: 质量约束下的节省率（质量失败→0分）
"""

from collections import defaultdict


def compute_quality_preserving_savings(
    judgments: list[dict],
    records: list[dict],
) -> dict:
    """在 Judge 判决之后计算质量约束的续问指标。

    Args:
        judgments: Judge 输出列表，每项含 sample_id, verdict, uses_supported_evidence
        records: Agent 运行记录列表，每项含 sample_id, step_count, follow_up_group, is_follow_up

    Returns:
        dict with follow_up_reuse_success_rate, follow_up_tool_fallback_rate,
        quality_preserving_savings, per_group 明细
    """
    judgment_map = {j.get("sample_id", ""): j for j in judgments}

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        gid = r.get("follow_up_group", "")
        if gid:
            groups[gid].append(r)

    scores: list[dict] = []
    reuse_success_count = 0
    tool_fallback_count = 0

    for gid, chain in groups.items():
        chain.sort(key=lambda x: bool(x.get("is_follow_up", False)))
        if len(chain) < 2:
            continue

        initial_steps = chain[0].get("step_count", 0)

        for cr in chain[1:]:
            sample_id = cr.get("sample_id", "")
            j = judgment_map.get(sample_id, {})
            verdict = j.get("verdict", "")
            grounded = j.get("uses_supported_evidence", False)

            fu_steps = cr.get("step_count", 0)

            # 恢复工具调用
            if fu_steps > 0:
                tool_fallback_count += 1

            # 质量判定：correct 或 partially_correct 且 grounded
            quality_ok = (
                verdict == "correct"
                or (verdict == "partially_correct" and grounded)
            )

            if quality_ok:
                reuse_success_count += 1
                score = max(0.0, 1.0 - fu_steps / max(1, initial_steps))
            else:
                score = 0.0

            scores.append({
                "group_id": gid,
                "sample_id": sample_id,
                "initial_steps": initial_steps,
                "follow_up_steps": fu_steps,
                "score": round(score, 4),
                "verdict": verdict,
                "grounded": grounded,
                "quality_ok": quality_ok,
            })

    n = len(scores) or 1
    return {
        "follow_up_reuse_success_rate": round(reuse_success_count / n, 4) if scores else 0.0,
        "follow_up_tool_fallback_rate": round(tool_fallback_count / n, 4) if scores else 0.0,
        "quality_preserving_savings": round(
            sum(s["score"] for s in scores) / n, 4
        ) if scores else 0.0,
        "per_group": scores,
    }
