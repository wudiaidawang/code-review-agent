"""Pipeline 批量执行器 — 对每个样本 repo 运行 ReviewPipeline，收集输出.

用法:
    python -m eval_report.run_pipeline                         # 跑全部样本
    python -m eval_report.run_pipeline --sample s001_simple    # 只跑指定样本
    python -m eval_report.run_pipeline --limit 5               # 只跑前 5 个
"""

import json
import os
import sys
import time
from pathlib import Path

from app.pipeline.review_pipeline import ReviewPipeline
from app.pipeline.llm_reviewer import LLMReviewer
from app.tools.llm_tool import chat

ROOT = Path(__file__).resolve().parent
_DEFAULT_SAMPLES_DIR = Path(os.environ.get("EVAL_SAMPLES_DIR",
    Path(os.environ.get("TEMP", "/tmp")) / "eval_report_samples"))
RESULTS_DIR = ROOT / "results"


def _load_index(samples_dir: Path) -> list[dict]:
    if not samples_dir.exists():
        return []  # 目录不存在时静默返回空，由调用方提示用户先运行 generate_samples
    idx_path = samples_dir / "_index.json"
    if idx_path.exists():
        return json.loads(idx_path.read_text(encoding="utf-8"))
    # 回退：扫描目录
    samples = []
    for d in sorted(samples_dir.iterdir()):
        if d.is_dir() and (d / "_meta.json").exists():
            samples.append({"sample_id": d.name, "dir": str(d)})
    return samples


def _glm_call(system_prompt: str, user_prompt: str) -> str:
    # 评测只接受短 JSON；关闭推理模型 thinking（思考与正文共用输出预算，
    # 预算被吃光会导致 findings JSON 截断/为空），并给足列出多处问题的空间。
    return chat(user_prompt, system=system_prompt, temperature=0.1, max_tokens=1500,
                extra_body={"thinking": {"type": "disabled"}})


def run_one(sample_dir: str, sample_id: str, mode: str = "static") -> dict | None:
    """对单个样本运行 Pipeline，返回序列化后的 ReviewOutput。"""
    # 基准要求每个样本都有确定的时间边界；生产重试策略不在此处叠加。
    reviewer = LLMReviewer(call_llm=_glm_call, max_retries=0) if mode == "llm" else None
    pipeline = ReviewPipeline(llm_reviewer=reviewer)
    try:
        t0 = time.perf_counter()
        output = pipeline.run(sample_dir, "HEAD~1", "HEAD")
        elapsed = time.perf_counter() - t0

        result = {
            "sample_id": sample_id,
            "mode": mode,
            "sample_dir": sample_dir,
            "duration_ms": round(elapsed * 1000),
            "plan": output.plan,
            "change_set": output.change_set,
            # Judge 的评判证据：样本临时目录可能被清理，diff 必须随结果持久化
            "unified_diff": output.unified_diff,
            "issues": [i.to_dict() for i in output.issues],
            "evidence_count": len(output.evidence),
            "trace": [{"step": t.step, "status": t.status, "duration_ms": t.duration_ms}
                      for t in output.trace],
            "timeline": output.timeline.to_dict() if output.timeline else None,
        }
        return result
    except Exception as e:
        return {
            "sample_id": sample_id,
            "sample_dir": sample_dir,
            "error": str(e),
            "duration_ms": 0,
            "plan": None,
            "change_set": None,
            "issues": [],
            "evidence_count": 0,
            "trace": [],
        }


def run(limit: int | None = None, sample_id: str | None = None, samples_dir: str | None = None,
        results_dir: str | None = None, mode: str = "static"):
    """批量运行 Pipeline 并保存结果。"""
    sdir = Path(samples_dir) if samples_dir else _DEFAULT_SAMPLES_DIR
    index = _load_index(sdir)
    if not index:
        print(f"没有找到样本（目录: {sdir}）。请先运行 generate_samples.py")
        return

    if sample_id:
        index = [s for s in index if s["sample_id"] == sample_id]
        if not index:
            print(f"未找到样本: {sample_id}")
            return

    if limit:
        index = index[:limit]

    output_dir = Path(results_dir) if results_dir else RESULTS_DIR / mode
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"运行 {len(index)} 个样本的 Pipeline ...\n")

    success = 0
    failures = 0
    total_issues = 0

    for i, entry in enumerate(index):
        sid = entry["sample_id"]
        sdir = entry["dir"]

        print(f"[{i+1}/{len(index)}] {sid} ...", end=" ", flush=True)
        result = run_one(sdir, sid, mode=mode)

        # 保存结果
        out_path = output_dir / f"{sid}_pipeline_output.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        if result.get("error"):
            print(f"ERROR: {result['error']}")
            failures += 1
        else:
            n = len(result["issues"])
            total_issues += n
            dur = result["duration_ms"]
            print(f"OK ({n} issues, {dur}ms)")
            success += 1

    # 保存汇总
    summary = {
        "total": len(index),
        "success": success,
        "failures": failures,
        "total_issues_found": total_issues,
        "avg_issues_per_sample": round(total_issues / success, 1) if success else 0,
    }
    summary["mode"] = mode
    (output_dir / "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n完成: {success}/{len(index)} 成功, 共 {total_issues} 个 Issues")
    print(f"结果目录: {output_dir}")


# ---- CLI ----

if __name__ == "__main__":
    import argparse

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Report 级评测 — 批量 Pipeline 执行")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个样本")
    parser.add_argument("--sample", type=str, default=None, help="只跑指定样本 ID")
    parser.add_argument("--samples-dir", type=str, default=None, help="样本目录（默认 ~/.eval_report_samples）")
    parser.add_argument("--results-dir", type=str, default=None)
    parser.add_argument("--mode", choices=("static", "llm"), default="static")
    args = parser.parse_args()

    run(limit=args.limit, sample_id=args.sample, samples_dir=args.samples_dir,
        results_dir=args.results_dir, mode=args.mode)
