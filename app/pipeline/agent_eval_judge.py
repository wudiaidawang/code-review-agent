"""LLM-as-Judge：只基于 Agent 答案与其 Evidence 评判语义完成度。"""

import argparse
import json
import os
import re
from app.tools.llm_tool import chat, get_model

VALID_VERDICTS = {"correct", "partially_correct", "incorrect", "unjudgeable"}
VALID_COVERAGE = {"full", "partial", "none"}

# ── JSON Schema（Judge 输出规范） ──────────────────────────────────────────
JUDGE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "score", "answered_question", "uses_supported_evidence",
                 "expected_file_coverage", "reason", "missing_points"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["correct", "partially_correct", "incorrect", "unjudgeable"],
        },
        "score": {"type": "integer", "minimum": 0, "maximum": 2},
        "answered_question": {"type": "boolean"},
        "uses_supported_evidence": {"type": "boolean"},
        "expected_file_coverage": {
            "type": "string",
            "enum": ["full", "partial", "none"],
        },
        "reason": {"type": "string"},
        "missing_points": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}

# ── System Prompt ───────────────────────────────────────────────────────────
JUDGE_SYSTEM_PROMPT = (
    "你是严格的代码调查评测员。只能依据提供的 agent_evidence 评判 Agent 的回答质量，"
    "不能自行补充仓库知识或臆测代码内容。\n\n"
    "## 评判规则\n\n"
    "你需要对照 expected_answer_summary（期望答案摘要），判断 Agent 的回答是否正确。\n\n"
    "### verdict 判定标准\n"
    "- correct: Agent 回答覆盖了 expected_answer_summary 的核心结论，关键事实与预期一致，且能从 agent_evidence 中找到支撑\n"
    "- partially_correct: Agent 回答方向正确但遗漏了重要信息，或部分陈述缺乏 agent_evidence 支撑\n"
    "- incorrect: Agent 回答与 expected_answer_summary 明显矛盾，或核心结论错误\n"
    "- unjudgeable: agent_evidence 中确实缺乏足够信息来判断正误；或 Agent 的回答是\"LLM 不可用\"等降级文本；"
    "或 Agent 明确声明\"无法确定/无法回答\"并给出了合理理由（如证据不足、文件内容不可用）——此时即使 expected_answer_summary 有答案，"
    "也应尊重 Agent 的判断，评为 unjudgeable 而非 incorrect\n\n"
    "### score 评分标准（与 verdict 对应）\n"
    "- 2: 回答准确、关键信息齐全\n"
    "- 1: 回答部分正确，有遗漏或次要错误\n"
    "- 0: 回答错误，或无法评判（verdict=incorrect 或 unjudgeable）\n\n"
    "### 字段说明\n"
    "- answered_question: 布尔值。Agent 是否给出了针对问题的有效回答（注意：不是一个字符串，是 true 或 false）。"
    "如果回答是\"LLM 不可用\"/\"无法确定\"/纯降级信息则填 false\n"
    "- uses_supported_evidence: 布尔值。Agent 的结论是否引用了 agent_evidence 中的具体证据（文件+行号）\n"
    "- expected_file_coverage: Agent 的证据覆盖了多少预期关键文件。full=全部覆盖，partial=部分覆盖，none=未覆盖\n"
    "- reason: 字符串，用中文简述判决理由\n"
    "- missing_points: 字符串数组，列出 Agent 遗漏的关键信息点。如果回答已完整可为空数组 []\n\n"
    "### 重要提示\n"
    "- expected_answer_keywords 是辅助参考，关注语义覆盖而非逐词复述——Agent 用不同措辞表达相同含义应视为覆盖\n"
    "- 若 expected_answer_summary 为空字符串，则只基于 agent_evidence 与问题本身判断 Agent 是否给出了有证据支撑的合理回答\n"
    "- 只返回 JSON 对象，不要添加任何解释文字或 Markdown 围栏\n"
    "- 严格遵守字段类型：answered_question 和 uses_supported_evidence 必须是布尔值 true/false，"
    "score 必须是整数 0/1/2，missing_points 必须是字符串数组"
)


# ── JSON Schema 校验 ────────────────────────────────────────────────────────
def _validate_schema(data: dict) -> list[str]:
    """校验 data 是否符合 JUDGE_OUTPUT_SCHEMA。返回错误列表（空列表表示通过）。"""
    errors = []
    for field in JUDGE_OUTPUT_SCHEMA["required"]:
        if field not in data:
            errors.append(f"缺少必需字段: {field}")
    if errors:
        return errors
    for field, spec in JUDGE_OUTPUT_SCHEMA["properties"].items():
        if field not in data:
            continue
        value = data[field]
        expected_type = spec["type"]
        if expected_type == "string" and not isinstance(value, str):
            errors.append(f"{field}: 期望 string，实际 {type(value).__name__}")
        elif expected_type == "integer" and not isinstance(value, int):
            errors.append(f"{field}: 期望 integer，实际 {type(value).__name__}")
        elif expected_type == "boolean" and not isinstance(value, bool):
            errors.append(f"{field}: 期望 boolean，实际 {type(value).__name__}（当前值: {repr(value)[:80]}）")
        elif expected_type == "array" and not isinstance(value, list):
            errors.append(f"{field}: 期望 array，实际 {type(value).__name__}")
        if "enum" in spec and isinstance(value, str) and value not in spec["enum"]:
            errors.append(f"{field}: {value!r} 不在合法值 {spec['enum']} 中")
        if "minimum" in spec and isinstance(value, (int, float)) and not isinstance(value, bool):
            if value < spec["minimum"]:
                errors.append(f"{field}: {value} < 最小值 {spec['minimum']}")
        if "maximum" in spec and isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > spec["maximum"]:
                errors.append(f"{field}: {value} > 最大值 {spec['maximum']}")
    if "additionalProperties" in JUDGE_OUTPUT_SCHEMA and not JUDGE_OUTPUT_SCHEMA["additionalProperties"]:
        for key in data:
            if key not in JUDGE_OUTPUT_SCHEMA["properties"]:
                errors.append(f"不允许的额外字段: {key!r}")
    return errors


# ── Evidence 截断 ───────────────────────────────────────────────────────────
def _truncate_evidence(evidence: list, expected_files: list, agent_answer: str,
                       max_items: int = 18, diversity_min: int = 5) -> list:
    """保留 Agent 实际引用的证据 + 预期文件证据 + 少量高置信度补充。

    不只看预期文件（避免暗示 Judge）；确保至少 diversity_min 条非预期文件证据。
    """
    if len(evidence) <= max_items:
        return evidence

    # 解析 Agent 答案中引用的 file:line
    cited = set()
    for m in re.finditer(r"([\w./-]+\.[\w]+):(\d+)", agent_answer):
        cited.add(m.group(1))

    expected_set = set(expected_files or [])

    # 分组
    cited_ev = []       # Agent 答案引用的
    expected_ev = []    # 预期文件中的（非引用）
    high_conf_ev = []   # 高置信度 (>=0.9)
    others_ev = []      # 其余

    for e in evidence:
        loc = (e.get("location") or {})
        fname = loc.get("file", "")
        conf = e.get("confidence", 0)
        if fname in cited:
            cited_ev.append(e)
        elif fname in expected_set:
            expected_ev.append(e)
        elif isinstance(conf, (int, float)) and conf >= 0.9:
            high_conf_ev.append(e)
        else:
            others_ev.append(e)

    selected = []
    selected.extend(cited_ev)
    # 预期文件证据（控制数量，避免全是预期文件）
    expected_limit = max_items - len(selected) - diversity_min
    expected_limit = max(0, expected_limit)
    selected.extend(expected_ev[:expected_limit])
    # 高置信度补充
    remaining = max_items - len(selected)
    selected.extend(high_conf_ev[:remaining])
    # 多样性填充
    remaining = max_items - len(selected)
    if remaining > 0 and others_ev:
        selected.extend(others_ev[:remaining])

    # 确保最少多样性：如果非预期文件少于 diversity_min，用 others 替换部分 expected
    non_expected = [e for e in selected if (e.get("location") or {}).get("file", "") not in expected_set]
    if len(non_expected) < diversity_min and len(others_ev) > 0:
        # 去掉尾部 expected 条目，换成 others
        to_replace = diversity_min - len(non_expected)
        selected = [e for e in selected if (e.get("location") or {}).get("file", "") in expected_set][:-to_replace] if to_replace > 0 else selected
        # 重新计算——更简单的做法：从 expected_ev 尾部移除，加 others
        selected_non_expected = [e for e in selected if (e.get("location") or {}).get("file", "") not in expected_set]
        selected_expected = [e for e in selected if (e.get("location") or {}).get("file", "") in expected_set]
        needed = diversity_min - len(selected_non_expected)
        if needed > 0 and len(selected_expected) > 0:
            trim = min(needed, len(selected_expected))
            selected_expected = selected_expected[:-trim]
            for e in others_ev:
                if len(selected_non_expected) + len(selected_expected) >= max_items:
                    break
                if e not in selected_expected and e not in selected_non_expected:
                    selected_non_expected.append(e)
            selected = selected_expected + selected_non_expected

    return selected[:max_items]


# ── JSON 解析 ───────────────────────────────────────────────────────────────
def _extract_json_object(text: str) -> str:
    """从文本中提取最外层的 JSON 对象（括号计数匹配）。"""
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in response")
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unmatched braces in response")


def _strip_fences(text: str) -> str:
    """去掉 Markdown ```json / ``` 围栏。"""
    t = text.strip()
    m = re.match(r"```(?:json)?\s*\n?", t)
    if m:
        t = t[m.end():]
    if t.rstrip().endswith("```"):
        t = t.rstrip()[: -3].rstrip()
    return t


def _parse_judge_json(raw: str | None) -> dict:
    """从原始 Judge 返回中解析 JSON。

    支持裸 JSON、```json 代码块、前后带解释文本。
    用 json.loads 解析，禁止 eval。
    """
    if raw is None:
        raise ValueError("judge response is None")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty judge response")

    text = raw.strip()
    # 策略1: 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 策略2: 去围栏
    unfenced = _strip_fences(text)
    if unfenced != text:
        try:
            return json.loads(unfenced)
        except json.JSONDecodeError:
            pass
    # 策略3: 提取 JSON 对象
    try:
        extracted = _extract_json_object(text)
        return json.loads(extracted)
    except (ValueError, json.JSONDecodeError):
        pass
    # 策略4: 去围栏 + 提取
    try:
        extracted = _extract_json_object(unfenced)
        return json.loads(extracted)
    except (ValueError, json.JSONDecodeError):
        pass
    raise ValueError(f"unparseable judge response: {raw[:200]!r}")


# ── 判决主逻辑 ──────────────────────────────────────────────────────────────
def judge_record(record: dict, call_llm=chat) -> dict:
    """返回包含审计信息的判决结果。

    区分 judge_unavailable（API/空/不可解析）、judge_invalid_schema（可解析但不合 Schema）、
    有效判决。首次失败触发一次修复重试。
    """
    # ── 构建输入 ──
    evidence = record.get("evidence", [])
    expected_files = record.get("expected_evidence_files", [])
    agent_answer = record.get("final_answer", record.get("answer", ""))

    truncated_evidence = _truncate_evidence(evidence, expected_files, agent_answer)

    payload = {
        "question": record["question"],
        "expected_answer_summary": record.get("expected_answer_summary", ""),
        "expected_answer_keywords": record.get("expected_answer_keywords", []),
        "expected_evidence_files": expected_files,
        "agent_answer": agent_answer,
        "agent_evidence": truncated_evidence,
        "fallback_reason": record.get("fallback_reason"),
    }
    prompt = json.dumps(payload, ensure_ascii=False)
    call_kwargs = dict(temperature=0, max_tokens=1800, timeout=60,
                       extra_body={"thinking": {"type": "disabled"}})

    # ── 辅助构建函数 ──
    def _make_unavailable(error: str, raw_resp, retry_raw=None, retry_err=None):
        return {
            "sample_id": record["sample_id"], "model": get_model(),
            "raw_judge_response": raw_resp, "retry_raw_response": retry_raw,
            "retry_error": retry_err, "judge_error": error,
            "judge_error_type": "judge_unavailable", "schema_errors": [],
            "verdict": "unjudgeable", "score": 0,
            "answered_question": False, "uses_supported_evidence": False,
            "expected_file_coverage": "none",
            "reason": "judge unavailable", "missing_points": [],
        }

    def _make_invalid_schema(raw_resp, data, errors, retry_raw=None, retry_err=None):
        return {
            "sample_id": record["sample_id"], "model": get_model(),
            "raw_judge_response": raw_resp, "retry_raw_response": retry_raw,
            "retry_error": retry_err, "judge_error": "; ".join(errors),
            "judge_error_type": "judge_invalid_schema", "schema_errors": errors,
            "verdict": data.get("verdict", "unjudgeable"),
            "score": data.get("score", 0) if isinstance(data.get("score"), int) else 0,
            "answered_question": data.get("answered_question", False) if isinstance(data.get("answered_question"), bool) else False,
            "uses_supported_evidence": data.get("uses_supported_evidence", False) if isinstance(data.get("uses_supported_evidence"), bool) else False,
            "expected_file_coverage": data.get("expected_file_coverage", "none") if isinstance(data.get("expected_file_coverage"), str) else "none",
            "reason": str(data.get("reason", "")) if data.get("reason") is not None else "",
            "missing_points": data.get("missing_points", []) if isinstance(data.get("missing_points"), list) else [],
        }

    def _make_valid(raw_resp, data, retry_raw=None, retry_err=None):
        return {
            "sample_id": record["sample_id"], "model": get_model(),
            "raw_judge_response": raw_resp, "retry_raw_response": retry_raw,
            "retry_error": retry_err, "judge_error": None,
            "judge_error_type": None, "schema_errors": [],
            "verdict": data["verdict"], "score": data["score"],
            "answered_question": data["answered_question"],
            "uses_supported_evidence": data["uses_supported_evidence"],
            "expected_file_coverage": data["expected_file_coverage"],
            "reason": data.get("reason", ""),
            "missing_points": data.get("missing_points", []),
        }

    # ── 首次调用 ──
    raw = None
    try:
        raw = call_llm(prompt, system=JUDGE_SYSTEM_PROMPT, **call_kwargs)
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("empty judge response")

        data = _parse_judge_json(raw)
        errors = _validate_schema(data)
        if errors:
            # Schema 不合法 → 触发一次修复重试
            raise ValueError("schema validation failed:" + "; ".join(errors))

        return _make_valid(raw, data)
    except ValueError as exc:
        # 不可解析或 Schema 不合法 → 修复重试
        error_msg = str(exc)
        is_schema_error = error_msg.startswith("schema validation failed:")
        retry_raw = None
        try:
            if is_schema_error:
                retry_prompt = (
                    f"你上一次返回的 JSON 格式不符合要求。\n\n"
                    f"Schema 错误：\n{error_msg}\n\n"
                    f"你的原始输出：\n{(raw or '(empty)')[:600]}\n\n"
                    f"请修正后只返回合法 JSON。注意：\n"
                    f"- answered_question 和 uses_supported_evidence 必须是布尔值 true/false\n"
                    f"- score 必须是整数 0/1/2\n"
                    f"- missing_points 必须是字符串数组\n"
                    f"- 不要添加 Markdown 围栏或解释文字"
                )
            else:
                retry_prompt = (
                    f"你之前的输出无法解析为 JSON：\n\n"
                    f"--- 原始输出 ---\n{(raw or '(empty)')[:800]}\n--- 结束 ---\n\n"
                    f"解析错误：{exc}\n\n"
                    f"请只返回合法 JSON 对象，不要添加任何解释文字或 Markdown 围栏。"
                    f"字段必须严格遵守类型要求。"
                )
            retry_raw = call_llm(retry_prompt, system=JUDGE_SYSTEM_PROMPT, **call_kwargs)
            if not isinstance(retry_raw, str) or not retry_raw.strip():
                return _make_unavailable(f"{'schema' if is_schema_error else 'parse'} error: {exc}; retry returned empty",
                                         raw, retry_raw, "empty retry response")

            data = _parse_judge_json(retry_raw)
            errors = _validate_schema(data)
            if errors:
                return _make_invalid_schema(retry_raw, data, errors,
                                            retry_raw=retry_raw,
                                            retry_err=f"first attempt: {exc}")

            return _make_valid(retry_raw, data,
                               retry_raw=retry_raw,
                               retry_err=f"first attempt: {exc}; raw[first]: {(raw or '(empty)')[:200]}")
        except Exception as retry_exc:
            return _make_unavailable(
                f"{'schema' if is_schema_error else 'parse'} error: {exc}; retry also failed: {retry_exc}",
                raw, retry_raw, str(retry_exc))
    except Exception as exc:
        return _make_unavailable(str(exc), raw if isinstance(raw, str) else None)


# ── 汇总 ─────────────────────────────────────────────────────────────────────
def summarize_judgments(judgments: list[dict]) -> dict:
    total = len(judgments)
    evaluable = [j for j in judgments if j.get("judge_error_type") is None]
    unavailable = [j for j in judgments if j.get("judge_error_type") == "judge_unavailable"]
    invalid_schema = [j for j in judgments if j.get("judge_error_type") == "judge_invalid_schema"]
    return {
        "total": total,
        "semantic_completion_rate": sum(j["verdict"] == "correct" for j in evaluable) / len(evaluable) if evaluable else 0.0,
        "semantic_partial_rate": sum(j["verdict"] == "partially_correct" for j in evaluable) / len(evaluable) if evaluable else 0.0,
        "semantic_incorrect_rate": sum(j["verdict"] == "incorrect" for j in evaluable) / len(evaluable) if evaluable else 0.0,
        "judge_unjudgeable_rate": sum(j["verdict"] == "unjudgeable" for j in evaluable) / len(evaluable) if evaluable else 0.0,
        "grounded_answer_rate": sum(j.get("uses_supported_evidence", False) for j in judgments) / total if total else 0.0,
        "judge_unavailable_rate": len(unavailable) / total if total else 0.0,
        "judge_invalid_schema_rate": len(invalid_schema) / total if total else 0.0,
        "judge_effective_rate": len(evaluable) / total if total else 0.0,
        "retry_success_count": sum(1 for j in judgments if j.get("retry_error") and not j.get("judge_error")),
    }


# ── 批量评判 ─────────────────────────────────────────────────────────────────
def judge_baseline(input_path: str, output_path: str, resume: bool = True, call_llm=chat) -> dict:
    """可恢复地评判冻结快照；输出文件保存逐条原始判决、重试信息与汇总。

    resume=False 时忽略已有输出，全量重判。
    """
    with open(input_path, "r", encoding="utf-8") as f:
        baseline = json.load(f)
    existing = {}
    if resume and os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as f:
            existing = {j["sample_id"]: j for j in json.load(f).get("judgments", [])}
    for record in baseline["samples"]:
        if record["sample_id"] not in existing:
            existing[record["sample_id"]] = judge_record(record, call_llm=call_llm)
            payload = {
                "baseline_id": baseline["baseline_id"],
                "source": os.path.basename(input_path),
                "judgments": list(existing.values()),
                "summary": summarize_judgments(list(existing.values())),
            }
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
    return {
        "judgments": list(existing.values()),
        "summary": summarize_judgments(list(existing.values())),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Judge frozen external Agent baseline")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--no-resume", action="store_true")
    args = parser.parse_args()
    result = judge_baseline(args.input, args.output, resume=not args.no_resume)
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
