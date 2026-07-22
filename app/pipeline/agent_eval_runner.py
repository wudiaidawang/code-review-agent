"""Agent 评测执行器 — 实际运行 InvestigationAgent 并计算 5 项指标。

用法：
    python -m app.pipeline.agent_eval_runner                   # 真实 LLM 模式（全量 46 条）
    python -m app.pipeline.agent_eval_runner --mock            # Mock LLM 模式（CI/可复现）
    python -m app.pipeline.agent_eval_runner --top 10 --json   # 前 10 条 + JSON 输出
    python -m app.pipeline.agent_eval_runner --mock --json --output results/agent_eval.json
"""

import argparse
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from collections import defaultdict

from app.agent.investigator import InvestigationAgent, InvestigationStore
from app.pipeline.eval_dataset import load_samples, RealInvestigationSample
from app.pipeline.agent_eval_metrics import AgentEvalMetrics, _judge_completion, _has_evidence_citations, detect_budget_exceeded
from app.pipeline.agent_eval_judge import judge_record


def _make_mock_answer(sample: RealInvestigationSample) -> str:
    """为样本生成确定性 mock 答案（含文件路径和行号引用）。"""
    gt = sample.ground_truth
    summary = gt.get("expected_answer_summary", "mock answer")
    files = gt.get("expected_evidence_files", [])
    if files:
        loc_strs = ", ".join(f"{f}:{l}" for f, l in zip(files, [10] * len(files)))
        return f"{summary}\n\n参考位置: {loc_strs}"
    return summary


@dataclass
class AgentEvalResult:
    """一次 Agent 评测的完整结果。"""
    metrics: AgentEvalMetrics = field(default_factory=AgentEvalMetrics)
    per_sample: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return self.metrics.summary()

    def to_dict(self) -> dict:
        record = {
            "metrics": self.metrics.to_dict(),
            "per_sample": self.per_sample,
        }
        return record

    def save_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)


class AgentEvalRunner:
    """运行 InvestigationAgent 实际调查，计算 5 项评测指标。"""

    def __init__(self, repo_path: str = ".", mock: bool = False,
                 store: InvestigationStore | None = None, call_llm=None):
        self.repo_path = os.path.abspath(repo_path)
        self.mock = mock
        self.call_llm = call_llm
        self.store = store or InvestigationStore()
        if mock:
            self.call_llm = lambda *a, **kw: "mock answer"
        self.agent = InvestigationAgent(call_llm=self.call_llm, store=self.store)

    _EXTERNAL_REPO_BASE = os.path.join(
        os.environ.get("EVAL_REPO_BASE", os.path.join(tempfile.gettempdir(), "eval_repos"))
    )

    def run_all(self, top_n: int | None = None, verbose: bool = True,
                dataset_mode: str = "agent_real", project: str | None = None,
                checkpoint_path: str | None = None, run_judge: bool = True) -> AgentEvalResult:
        """加载真实样本，逐个运行 investigate/follow_up，计算指标。"""
        if dataset_mode == "agent_external" and project:
            self.repo_path = os.path.join(self._EXTERNAL_REPO_BASE, project)
        samples = load_samples(dataset_mode, project=project)
        if not samples:
            print("错误: 未找到 agent_eval_real.json，请先创建评测数据集。")
            return AgentEvalResult()

        if top_n:
            samples = samples[:top_n]

        if verbose:
            print(f"加载 {len(samples)} 条真实调查样本\n")

        per_sample = self._load_checkpoint(checkpoint_path)
        completed_ids = {r.get("sample_id") for r in per_sample}
        # 分阶段处理：先独立问题，再 follow_up 链
        standalone = [s for s in samples if not s.follow_up_group and s.id not in completed_ids]
        follow_up_samples = [s for s in samples if s.follow_up_group]

        if verbose and standalone:
            print(f"--- 独立问题 ({len(standalone)} 条) ---")
        for i, s in enumerate(standalone):
            if verbose:
                print(f"[{i+1}/{len(standalone)}] {s.id}: {s.question[:60]}...", end=" ", flush=True)
            record = self._run_single(s)
            per_sample.append(record)
            self._save_checkpoint(checkpoint_path, per_sample)
            if verbose:
                status = "OK" if _judge_completion(record) else "PARTIAL"
                print(f"{status} ({record['step_count']} 步, {record['duration_ms']:.0f}ms)")

        # 按 follow_up_group 分组
        groups: dict[str, list] = defaultdict(list)
        for s in follow_up_samples:
            groups[s.follow_up_group].append(s)

        if verbose and groups:
            print(f"\n--- 续问链 ({len(groups)} 组, {len(follow_up_samples)} 条) ---")
        for gid, chain in sorted(groups.items()):
            chain.sort(key=lambda x: x.follow_up_order)
            # 续问依赖首次调查内存会话：只有整组均完成时才可跳过。
            if all(s.id in completed_ids for s in chain):
                continue
            per_sample = [r for r in per_sample if r.get("follow_up_group") != gid]
            if verbose:
                print(f"  [{gid}]:", end=" ", flush=True)
            results = self._run_follow_up_chain(chain)
            per_sample.extend(results)
            self._save_checkpoint(checkpoint_path, per_sample)
            if verbose:
                initial = results[0]["step_count"] if results else 0
                fu_steps = [r["step_count"] for r in results[1:]]
                fu_str = ", ".join(str(s) for s in fu_steps)
                print(f"初始 {initial} 步 → 续问 [{fu_str}] 步")

        # LLM Judge 语义评判阶段
        if run_judge and not self.mock:
            if verbose:
                print(f"\n--- LLM Judge 语义评判 ({len(per_sample)} 条) ---")
            for i, record in enumerate(per_sample):
                if record.get("llm_judge"):
                    continue  # 从 checkpoint 恢复，已有评判结果
                if verbose:
                    print(f"  Judge [{i+1}/{len(per_sample)}] {record.get('sample_id', '?')}...", end=" ", flush=True)
                try:
                    judgment = judge_record(record)
                    record["llm_judge"] = judgment
                    if verbose:
                        print(judgment.get("verdict", "?"))
                except Exception as exc:
                    record["llm_judge"] = {"verdict": "unjudgeable", "judge_error": str(exc), "judge_error_type": "judge_unavailable"}
                    if verbose:
                        print(f"ERROR: {exc}")
                # Judge calls are the slowest and least predictable phase.
                # Persist every verdict so an interruption resumes from the
                # next sample instead of repeating the whole 21-sample batch.
                self._save_checkpoint(checkpoint_path, per_sample)
            self._save_checkpoint(checkpoint_path, per_sample)

        metrics = AgentEvalMetrics.compute(per_sample)
        if verbose:
            print(f"\n{'='*50}")
            print(metrics.summary())

        return AgentEvalResult(metrics=metrics, per_sample=per_sample)

    @staticmethod
    def _load_checkpoint(path: str | None) -> list[dict]:
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f).get("per_sample", [])
        except (OSError, json.JSONDecodeError):
            return []

    @staticmethod
    def _save_checkpoint(path: str | None, records: list[dict]) -> None:
        if not path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"per_sample": records}, f, ensure_ascii=False, indent=2)

    def _run_single(self, sample: RealInvestigationSample) -> dict:
        """对单个样本执行 investigate() 并收集指标数据。"""
        if self.mock:
            mock_answer = _make_mock_answer(sample)
            self.agent.call_llm = lambda *a, **kw: mock_answer

        t0 = time.perf_counter()
        try:
            result = self.agent.investigate(self.repo_path, sample.question)
            duration_ms = (time.perf_counter() - t0) * 1000
        except Exception as exc:
            return self._error_record(sample, str(exc))

        return self._record_to_dict(sample, result)

    def _run_follow_up_chain(self, chain: list[RealInvestigationSample]) -> list[dict]:
        """按 follow_up_order 依次执行 investigate() + follow_up()。"""
        records = []
        for i, sample in enumerate(chain):
            if self.mock:
                mock_answer = _make_mock_answer(sample)
                self.agent.call_llm = lambda *a, **kw: mock_answer

            t0 = time.perf_counter()
            try:
                if i == 0:
                    result = self.agent.investigate(self.repo_path, sample.question)
                else:
                    inv_id = records[0]["investigation_id"]
                    result = self.agent.follow_up(self.repo_path, inv_id, sample.question)
                duration_ms = (time.perf_counter() - t0) * 1000
            except Exception as exc:
                records.append(self._error_record(sample, str(exc)))
                continue

            records.append(self._record_to_dict(sample, result, is_follow_up=(i > 0)))
        return records

    def _record_to_dict(self, sample: RealInvestigationSample,
                        result, is_follow_up: bool = False) -> dict:
        gt = sample.ground_truth
        step_count = len([s for s in result.steps if s.get("tool") != "(blocked)"])
        record = {
            "sample_id": sample.id,
            "question": sample.question,
            "question_type": gt.get("question_type", ""),
            "project": sample.project,
            "repo_url": sample.repo_url,
            "repo_commit": sample.commit_sha,
            "is_follow_up": is_follow_up,
            "follow_up_group": sample.follow_up_group,
            "answer": result.answer,
            "evidence": [e.to_dict() for e in result.evidence],
            "files_visited": result.files_visited,
            "steps": result.steps,
            "step_count": step_count,
            "trace": result.trace,
            "duration_ms": result.duration_ms,
            "investigation_id": result.investigation_id,
            "reused_evidence_refs": result.reused_evidence_refs,
            "expected_answer_keywords": gt.get("expected_answer_keywords", []),
            "expected_evidence_files": gt.get("expected_evidence_files", []),
            "expected_answer_summary": gt.get("expected_answer_summary", ""),
            "expected_status": gt.get("expected_status", "active"),
            "expected_replacement": gt.get("expected_replacement", ""),
            # V22 诊断字段
            "planned_tasks": result.planned_tasks,
            "all_tasks": result.all_tasks,
            "work_orders": result.work_orders,
            "verified_evidence_summary": result.verified_evidence_summary,
            "candidate_evidence_count": result.candidate_evidence_count,
            "required_slots": result.required_slots,
            "closed_slots": result.closed_slots,
            "open_slots": result.open_slots,
            "contract_met_before": result.contract_met_before,
            "contract_met_after": result.contract_met_after,
            "stop_reason": result.stop_reason,
            "retool_triggered": result.retool_triggered,
            # V23 Claims 诊断
            "required_claims": result.required_claims,
            "covered_claims": result.covered_claims,
            "uncovered_claims": result.uncovered_claims,
            "claim_coverage_rate": result.claim_coverage_rate,
        }
        exceeded, budget_type = detect_budget_exceeded(record)
        record["budget_exhausted"] = exceeded
        record["budget_type"] = budget_type
        return record

    @staticmethod
    def _error_record(sample: RealInvestigationSample, error_msg: str) -> dict:
        return {
            "sample_id": sample.id,
            "question": sample.question,
            "question_type": sample.ground_truth.get("question_type", ""),
            "project": sample.project,
            "repo_url": sample.repo_url,
            "repo_commit": sample.commit_sha,
            "is_follow_up": False,
            "follow_up_group": sample.follow_up_group,
            "answer": f"(执行错误: {error_msg})",
            "evidence": [],
            "files_visited": [],
            "steps": [],
            "step_count": 0,
            "trace": [f"error: {error_msg}"],
            "duration_ms": 0,
            "investigation_id": "",
            "reused_evidence_refs": [],
            "budget_exhausted": False,
            "expected_answer_keywords": sample.ground_truth.get("expected_answer_keywords", []),
            "expected_evidence_files": sample.ground_truth.get("expected_evidence_files", []),
            "expected_answer_summary": sample.ground_truth.get("expected_answer_summary", ""),
            "expected_status": sample.ground_truth.get("expected_status", "active"),
            "expected_replacement": sample.ground_truth.get("expected_replacement", ""),
        }


def main():
    # Windows 控制台可能默认 GBK；评测问题、源码片段和 JSON 都要求 UTF-8。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(
        description="Agent 评测执行器 — 运行 InvestigationAgent 计算 5 项指标",
        prog="python -m app.pipeline.agent_eval_runner",
    )
    parser.add_argument("--mock", action="store_true",
                        help="使用 mock LLM（确定性，不上网）")
    parser.add_argument("--top", type=int, default=None,
                        help="只评测前 N 条样本")
    parser.add_argument("--json", action="store_true",
                        help="输出 JSON 而非 Markdown")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="JSON 输出路径（默认输出到 stdout）")
    parser.add_argument("--repo", type=str, default=".",
                        help="仓库路径（默认当前目录）")
    parser.add_argument("--dataset", choices=["agent_real", "agent_external"],
                        default="agent_real", help="评测集类型")
    parser.add_argument("--project", choices=["click", "httpx", "typer"],
                        default=None, help="外部评测集项目过滤（agent_external 必填）")
    parser.add_argument("--checkpoint", type=str, default=None,
                        help="逐样本 checkpoint JSON；重复运行会恢复已完成样本")
    parser.add_argument("--no-judge", action="store_true",
                        help="跳过 LLM Judge 语义评判阶段")

    args = parser.parse_args()

    runner = AgentEvalRunner(repo_path=args.repo, mock=args.mock)
    if args.dataset == "agent_external" and not args.project:
        parser.error("--dataset agent_external 时必须提供 --project")
    result = runner.run_all(top_n=args.top, dataset_mode=args.dataset,
                            project=args.project, checkpoint_path=args.checkpoint,
                            run_judge=not args.no_judge)

    if args.json:
        output = json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
        if args.output:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(output)
            print(f"JSON 已写入: {args.output}")
        else:
            print(output)
    elif args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        result.save_json(args.output)
        print(f"JSON 已写入: {args.output}")
        print(result.summary())


if __name__ == "__main__":
    main()
