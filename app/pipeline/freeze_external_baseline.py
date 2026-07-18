"""冻结外部真实 LLM 基线，保留可复现的逐样本诊断证据。"""

import json
import os
from datetime import datetime, timezone

from app.pipeline.agent_eval_metrics import _judge_completion, detect_budget_exceeded
from app.pipeline.eval_dataset import load_samples
from app.tools.llm_tool import get_model


DEFAULT_BUDGET = {"steps_max": 6, "files_max": 50, "token_budget": 16000}


def _labels(record: dict) -> list[str]:
    answer = (record.get("answer") or "").lower()
    expected_kw = record.get("expected_answer_keywords", [])
    keyword_ok = all(k.lower() in answer for k in expected_kw) if expected_kw else False
    expected_files = set(record.get("expected_evidence_files", []))
    evidence_files = {
        (e.get("location") or {}).get("file", "") for e in record.get("evidence", [])
    }
    file_ok = bool(expected_files & evidence_files) if expected_files else True
    labels = []
    if not keyword_ok:
        labels.append("keyword_miss")
    if not file_ok:
        labels.append("expected_file_miss")
    if "llm 不可用" in answer:
        labels.append("llm_fallback")
    if detect_budget_exceeded(record)[0]:
        labels.append("budget_exhausted")
    if any(s.get("status") == "failed" for s in record.get("steps", [])) or any(
        t.startswith("tool_error:") or t.startswith("error:") for t in record.get("trace", [])
    ):
        labels.append("tool_error")
    return labels


def freeze(input_dir: str, output_dir: str, baseline_id: str = "external_glm_v0") -> list[str]:
    """Freeze external results under a new immutable baseline identifier."""
    if not baseline_id.startswith("external_glm_v"):
        raise ValueError("baseline_id must look like external_glm_vN")
    version = baseline_id.removeprefix("external_glm_")
    samples = {
        sample.id: sample
        for project in ("click", "httpx", "typer")
        for sample in load_samples("agent_external", project=project)
    }
    os.makedirs(output_dir, exist_ok=True)
    written = []
    for project in ("click", "httpx", "typer"):
        source = os.path.join(input_dir, f"external_{project}_glm.json")
        target = os.path.join(output_dir, f"external_{project}_glm_{version}.json")
        if os.path.exists(target):
            raise FileExistsError(f"拒绝覆盖已冻结基线: {target}")
        with open(source, "r", encoding="utf-8") as f:
            result = json.load(f)
        records = []
        for record in result["per_sample"]:
            sample = samples[record["sample_id"]]
            answer = record.get("answer", "")
            labels = _labels(record)
            records.append({
                "sample_id": record["sample_id"],
                "repository": {"url": sample.repo_url, "commit_sha": sample.commit_sha, "project": sample.project},
                "question": record["question"], "question_type": record["question_type"],
                "model_config": {"model": get_model(), "thinking": "default_at_run_time", "synthesis_max_tokens": 2000},
                "raw_llm_response": None if "llm 不可用" in answer else answer,
                "final_answer": answer,
                "fallback_reason": "llm_unavailable_or_request_failed" if "llm 不可用" in answer else None,
                "evidence": record.get("evidence", []), "steps": record.get("steps", []), "trace": record.get("trace", []),
                "budget": {**DEFAULT_BUDGET, "steps_used": record.get("step_count", 0),
                           "files_visited": len(record.get("files_visited", [])),
                           "tokens_used": None, "tokens_used_note": "not persisted by v0 runner"},
                "score": {"completed": _judge_completion(record), "expected_answer_keywords": record.get("expected_answer_keywords", []),
                          "expected_evidence_files": record.get("expected_evidence_files", [])},
                "failure_reason_labels": labels,
                "ground_truth_ambiguous": False,
                "ground_truth_ambiguity_note": "reserved for manual annotation",
            })
        payload = {"baseline_id": baseline_id, "frozen_at": datetime.now(timezone.utc).isoformat(),
                   "source_result": os.path.basename(source), "metrics": result.get("metrics", {}), "samples": records}
        with open(target, "x", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        written.append(target)
    return written


def enrich_frozen_with_ground_truth(frozen_path: str) -> dict:
    """向冻结快照中补充 expected_answer_summary 与 expected_answer_keywords。

    从原始评测数据集中按 sample_id 查找 ground truth，写入冻结记录的顶层字段。
    这是元数据补充，不改动 Agent 运行结果。
    """
    with open(frozen_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)

    # 从第一个 sample 的 repository.project 推断项目名
    if not baseline.get("samples"):
        return {"status": "empty", "enriched": 0}
    project = baseline["samples"][0]["repository"]["project"]
    samples = {s.id: s for s in load_samples("agent_external", project=project)}

    enriched = 0
    for record in baseline["samples"]:
        sid = record["sample_id"]
        if sid in samples:
            gt = samples[sid].ground_truth
            record["expected_answer_summary"] = gt.get("expected_answer_summary", "")
            record["expected_answer_keywords"] = gt.get("expected_answer_keywords", [])
            enriched += 1

    with open(frozen_path, "w", encoding="utf-8") as f:
        json.dump(baseline, f, ensure_ascii=False, indent=2)
    return {"status": "ok", "enriched": enriched, "project": project}


if __name__ == "__main__":
    import argparse

    root = os.path.join(os.path.dirname(__file__), "..", "..", "eval_report", "results_agent")
    parser = argparse.ArgumentParser(description="Freeze an external Agent evaluation baseline")
    parser.add_argument("--input-dir", default=root)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--baseline-id", default="external_glm_v0")
    parser.add_argument("--enrich", default=None, metavar="FROZEN_PATH",
                        help="Enrich an existing frozen baseline with expected_answer_summary/keywords from dataset")
    args = parser.parse_args()
    if args.enrich:
        result = enrich_frozen_with_ground_truth(args.enrich)
        print(json.dumps(result, ensure_ascii=False))
    else:
        output_dir = args.output_dir or os.path.join(root, args.baseline_id)
        for path in freeze(args.input_dir, output_dir, args.baseline_id):
            print(f"已冻结: {path}")
