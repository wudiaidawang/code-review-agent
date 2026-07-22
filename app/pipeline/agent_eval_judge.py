"""LLM-as-Judge：只基于 Agent 答案与其 Evidence 评判语义完成度。"""

import argparse
import json
import os
import re
from app.tools.llm_tool import chat, get_model


def _norm_path(p: str) -> str:
    """归一化文件路径，统一使用正斜杠，用于跨平台比较。"""
    return p.replace("\\", "/")

VALID_VERDICTS = {"correct", "partially_correct", "incorrect", "unjudgeable"}
VALID_COVERAGE = {"full", "partial", "none"}

# ── Evidence grounding 检查 ─────────────────────────────────────────────────
def _check_evidence_grounding(agent_answer: str, agent_evidence: list[dict]) -> dict:
    """检查 Agent 答案中的事实性声明是否有证据支撑。

    从答案中提取 file:line 引用，逐一核对：
    1. 文件是否在 agent_evidence 中出现；
    2. 行号是否落在证据的 [start_line, end_line] 或 snippet 行号标注范围内。

    返回 {"grounded": bool, "no_refs": bool, "total_refs": int,
           "total_verified_lines": int, "ungrounded_entries": [...], "evidence_files": [...]}
    """
    # 构建证据索引：file → [(start_line, end_line, snippet_line_numbers)]
    evidence_index: dict[str, list[dict]] = {}
    for ev in agent_evidence:
        loc = ev.get("location") or {}
        f = _norm_path(loc.get("file", ""))
        if not f:
            continue
        sl = loc.get("start_line", 0)
        el = loc.get("end_line", 0)
        if el == 0:
            el = sl
        # 从 snippet 提取带行号标注的行号
        snippet_lines: set[int] = set()
        snip = ev.get("snippet", "")
        if snip:
            for line_m in re.finditer(r"^\s*(\d+)\|", snip, re.MULTILINE):
                snippet_lines.add(int(line_m.group(1)))
        if f not in evidence_index:
            evidence_index[f] = []
        evidence_index[f].append({
            "start_line": sl, "end_line": el, "snippet_lines": snippet_lines,
        })

    # 提取答案中的 file:line 引用
    answer_refs: list[tuple[str, int]] = []  # [(file, line), ...]
    for m in re.finditer(r"([\w./-]+\.[\w]+):(\d+)", _norm_path(agent_answer), re.ASCII):
        f = _norm_path(m.group(1))
        try:
            line = int(m.group(2))
        except ValueError:
            line = 0
        answer_refs.append((f, line))

    ungrounded_entries: list[dict] = []
    verified_lines = 0
    for f, line in answer_refs:
        if f not in evidence_index:
            ungrounded_entries.append({"ref": f"{f}:{line}", "reason": "file not found in evidence"})
            continue
        # 检查行号是否在任一证据的范围内
        line_match = False
        for entry in evidence_index[f]:
            if entry["start_line"] == 0 and entry["end_line"] == 0:
                # 整文件级证据，行号无法验证，视作通过
                line_match = True
                break
            if entry["start_line"] <= line <= entry["end_line"]:
                line_match = True
                break
            if line in entry["snippet_lines"]:
                line_match = True
                break
        if line_match:
            verified_lines += 1
        else:
            ungrounded_entries.append({"ref": f"{f}:{line}", "reason": "line out of evidence range"})

    no_refs = len(answer_refs) == 0
    grounded = not no_refs and len(ungrounded_entries) == 0

    return {
        "grounded": grounded,
        "no_refs": no_refs,
        "total_refs": len(answer_refs),
        "total_verified_lines": verified_lines,
        "ungrounded_entries": ungrounded_entries,
        "evidence_files": sorted(evidence_index.keys()),
    }

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
    "### 核心原则：证据锚定\n"
    "Agent 答案中的每个事实性声明（文件路径、行号、函数名、类名）都必须能在 agent_evidence "
    "中找到对应证据。如果 Agent 声称了某文件/行号的代码内容，但该文件不在 agent_evidence 中，"
    "则该声明视为无证据支撑——不能判 correct。\n\n"
    "### verdict 判定标准\n"
    "- correct: Agent 回答覆盖了 expected_answer_summary 的核心结论，关键事实与预期一致，"
    "且每个事实性声明都能从 agent_evidence 中找到支撑\n"
    "- partially_correct: Agent 回答方向正确但遗漏了重要信息，或部分陈述缺乏 agent_evidence 支撑，"
    "或引用了不在证据中的文件/行号\n"
    "- incorrect: Agent 回答与 expected_answer_summary 明显矛盾，或核心结论错误\n"
    "- unjudgeable: agent_evidence 中确实缺乏足够信息来判断正误；或 Agent 的回答是\"LLM 不可用\"等降级文本；"
    "或 Agent 明确声明\"无法确定/无法回答\"并给出了合理理由（如证据不足、文件内容不可用）——此时即使 expected_answer_summary 有答案，"
    "也应尊重 Agent 的判断，评为 unjudgeable 而非 incorrect\n\n"
    "### 特殊规则：expected_status = \"removed\"（符号已删除/重构）\n"
    "当 expected_status 字段为 \"removed\" 时，表示问题所问的符号在当前代码版本中已被删除或重命名。\n"
    "此时期望答案的核心结论是「该符号已不存在」，expected_replacement 说明了替代方案。\n"
    "- Agent 正确指出该符号已被删除/重命名/不存在，并给出合理理由 → **correct**（即使 agent_evidence 为空或无法定位）\n"
    "- Agent 正确指出符号不存在，还指明了替代方案（expected_replacement）→ **correct**\n"
    "- Agent 声称找到了该符号并给出了基于旧代码的错误信息 → **incorrect**\n"
    "- 此规则优先级高于「Agent 声明无法回答 → unjudgeable」——当 expected_status=removed 时，"
    "Agent 的「找不到/不存在」就是正确答案\n\n"
    "### score 评分标准（与 verdict 对应）\n"
    "- 2: 回答准确、关键信息齐全\n"
    "- 1: 回答部分正确，有遗漏或次要错误\n"
    "- 0: 回答错误，或无法评判（verdict=incorrect 或 unjudgeable）\n\n"
    "### 字段说明\n"
    "- answered_question: 布尔值。Agent 是否给出了针对问题的有效回答（注意：不是一个字符串，是 true 或 false）。"
    "如果回答是\"LLM 不可用\"/\"无法确定\"/纯降级信息则填 false\n"
    "- uses_supported_evidence: 布尔值。Agent 的结论是否引用了 agent_evidence 中的具体证据（文件+行号）。"
    "注意：如果 Agent 引用的文件/行号不在 agent_evidence 中，此字段应为 false\n"
    "- expected_file_coverage: Agent 的证据覆盖了多少预期关键文件。full=全部覆盖，partial=部分覆盖，none=未覆盖\n"
    "- reason: 字符串，用中文简述判决理由\n"
    "- missing_points: 字符串数组，列出 Agent 遗漏的关键信息点。如果回答已完整可为空数组 []\n\n"
    "### 重要提示\n"
    "- expected_answer_keywords 是辅助参考，关注语义覆盖而非逐词复述——Agent 用不同措辞表达相同含义应视为覆盖\n"
    "- 若 expected_answer_summary 为空字符串，则只基于 agent_evidence 与问题本身判断 Agent 是否给出了有证据支撑的合理回答\n"
    "- 只返回 JSON 对象，不要添加任何解释文字或 Markdown 围栏\n"
    "- 严格遵守字段类型：answered_question 和 uses_supported_evidence 必须是布尔值 true/false，"
    "score 必须是整数 0/1/2，missing_points 必须是字符串数组\n\n"
    "### 输出格式（必须包含全部 7 个字段，verdict 放在第一位）\n"
    '{"verdict": "correct|partially_correct|incorrect|unjudgeable", "score": 0, '
    '"answered_question": true, "uses_supported_evidence": true, '
    '"expected_file_coverage": "full|partial|none", "reason": "判决理由", "missing_points": []}'
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
    for m in re.finditer(r"([\w./-]+\.[\w]+):(\d+)", _norm_path(agent_answer), re.ASCII):
        cited.add(_norm_path(m.group(1)))

    expected_set = {_norm_path(f) for f in (expected_files or [])}

    # 分组
    cited_ev = []       # Agent 答案引用的
    expected_ev = []    # 预期文件中的（非引用）
    high_conf_ev = []   # 高置信度 (>=0.9)
    others_ev = []      # 其余

    for e in evidence:
        loc = (e.get("location") or {})
        fname = _norm_path(loc.get("file", ""))
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
    non_expected = [e for e in selected if _norm_path((e.get("location") or {}).get("file", "")) not in expected_set]
    if len(non_expected) < diversity_min and len(others_ev) > 0:
        # 去掉尾部 expected 条目，换成 others
        to_replace = diversity_min - len(non_expected)
        selected = [e for e in selected if _norm_path((e.get("location") or {}).get("file", "")) in expected_set][:-to_replace] if to_replace > 0 else selected
        # 重新计算——更简单的做法：从 expected_ev 尾部移除，加 others
        selected_non_expected = [e for e in selected if _norm_path((e.get("location") or {}).get("file", "")) not in expected_set]
        selected_expected = [e for e in selected if _norm_path((e.get("location") or {}).get("file", "")) in expected_set]
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
        "expected_status": record.get("expected_status", "active"),
        "expected_replacement": record.get("expected_replacement", ""),
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
        # ── 证据锚定检查：若 Judge 判 correct，验证答案引用是否在证据中 ──
        verdict = data["verdict"]
        grounding_info = None
        if verdict in ("correct", "partially_correct"):
            grounding_info = _check_evidence_grounding(agent_answer, truncated_evidence)
            # 若答案有引用但不在证据中（正向幻觉），且 Judge 判 correct，降级为 partially_correct
            # no_refs（答案无引用）不触发降级——无法验证但不等于幻觉
            if verdict == "correct" and not grounding_info["grounded"] and not grounding_info["no_refs"]:
                verdict = "partially_correct"
                data = dict(data)
                data["verdict"] = "partially_correct"
                data["score"] = min(data.get("score", 2), 1)
                ungrounded_refs = [e["ref"] for e in grounding_info.get("ungrounded_entries", [])]
                data["reason"] = (data.get("reason", "") +
                    f" [证据锚定降级: 答案引用 {ungrounded_refs} 无证据支撑]")

        return {
            "sample_id": record["sample_id"], "model": get_model(),
            "raw_judge_response": raw_resp, "retry_raw_response": retry_raw,
            "retry_error": retry_err, "judge_error": None,
            "judge_error_type": None, "schema_errors": [],
            "verdict": verdict, "score": data["score"],
            "answered_question": data["answered_question"],
            "uses_supported_evidence": data["uses_supported_evidence"],
            "expected_file_coverage": data["expected_file_coverage"],
            "reason": data.get("reason", ""),
            "missing_points": data.get("missing_points", []),
            "evidence_grounding": grounding_info,
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
