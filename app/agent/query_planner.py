"""V24 Query Planner — Relation 驱动的任务分解 + 确定性兜底

输入用户问题，返回 PlannerOutput（relations + targets + claims）。
LLM 负责分析语义关系并输出结构化事实需求；确定性策略负责展开为 slot。

V24: 不再输出 TaskType / tool 序列。Planner 只输出"需要确认的事实"。
"""

import json
import re

from app.models.target import (
    InvestigationTask,
    PlannerOutput, PlannerTarget, RelationDef, RelationType,
    Requirement, TargetSpec,
    validate_planner_output,
)

# GLM 思考内容与正文共用 max_tokens——合成大 prompt 时思考吃光预算
_LLM_CALL_KWARGS = {"timeout": 60, "extra_body": {"thinking": {"type": "disabled"}}}

# ── Query Planner 系统提示（V24: relation 格式）───────────────────

_QUERY_PLANNER_SYSTEM = """你是一个代码调查分析器。分析用户的问题，确定它属于什么类型的调查，需要调查哪些符号，以及它们之间存在什么关系。

重要：你只输出"需要确认的事实"，不设计工具流程。工具选择由后续确定性策略完成。

## 输出格式

严格 JSON（不要包含 markdown 代码块包裹）：

{
  "question_type": "<类型>",
  "relations": [
    {
      "type": "<RelationType>",
      "subjects": ["<符号1>", "<符号2>"],
      "required_claims": ["<该关系需要回答的内容点>", ...]
    }
  ],
  "standalone_targets": [
    {"symbol": "<符号>", "required_claims": ["<该符号需要回答的内容点>", ...]}
  ],
  "required_claims": ["<全局需要回答的内容点>", ...]
}

## question_type 取值

- locate: 定位某个符号的定义位置
- explain: 解释某个符号的实际行为/机制
- trace: 追踪调用链
- compare: 比较两个符号的行为差异
- impact: 分析修改影响
- grep: 搜索字面量/字符串出现位置

## RelationType 取值

- definition_location: 定位定义
- explain_behavior: 解释行为（需要定义+实现+被调用者）
- trace_call_chain: 追踪调用链（需要定义+调用者+被调用者+实现）
- compare_behavior: 比较两个符号的行为差异（每个符号需要定义+实现）
- impact_change: 分析修改影响（需要定义+调用者+引用）
- enumerate_usages: 枚举使用位置（需要定义+引用）

## 规则

1. 优先使用 relations 来表达符号间的关系（如 compare_behavior 涉及两个符号的对比）
2. 对于没有明确关系对的独立符号，使用 standalone_targets
3. subjects 中的符号必须是代码标识符（如 Context.invoke），不能是自然语言句子
4. required_claims 是简洁的中文短句，描述答案必须覆盖的知识点
5. 每条 relation 至少 1 条 required_claim；全局至少 2 条，最多 6 条
6. 至少输出一个 relation 或一个 standalone_target
7. 不要输出工具名称（resolve/read/search/verify）——这是 Planner 的职责边界

## 示例

问题："Typer 如何支持 List[str]、Tuple[str, int] 这类复合类型注解？"
输出：
{
  "question_type": "explain",
  "relations": [
    {
      "type": "explain_behavior",
      "subjects": ["get_click_type", "get_click_param", "determine_type_convertor"],
      "required_claims": ["复合类型的推断逻辑", "List 和 Tuple 的 convertor 选择", "如何映射到 Click 参数类型"]
    }
  ],
  "standalone_targets": [],
  "required_claims": ["get_click_type 的类型推断规则", "get_click_param 如何确定参数类型", "determine_type_convertor 的映射表"]
}

问题："Typer 启动到回调执行的完整调用链"
输出：
{
  "question_type": "trace",
  "relations": [
    {
      "type": "trace_call_chain",
      "subjects": ["Typer.__call__", "get_command", "click.Command.main"],
      "required_claims": ["Typer.__call__ 如何进入 Click", "get_command/get_group 的转换逻辑", "click.Command.main 到用户回调的路径"]
    }
  ],
  "standalone_targets": [],
  "required_claims": ["Typer.__call__ 的入口职责", "get_command 如何转换为 Click Command", "Click main 到回调执行的完整流程"]
}

问题："click.option 和 typer.Option 的 default 参数行为有什么区别？"
输出：
{
  "question_type": "compare",
  "relations": [
    {
      "type": "compare_behavior",
      "subjects": ["click.option", "typer.Option"],
      "required_claims": ["default 参数的默认值差异", "default 的类型推断差异", "default 的 help 文本生成差异"]
    }
  ],
  "standalone_targets": [],
  "required_claims": ["click.option 的 default 处理逻辑", "typer.Option 的 default 处理逻辑", "两者的关键行为差异"]
}"""


# ── 主函数 ──────────────────────────────────────────────────────

def query_planner(question: str, call_llm=None) -> PlannerOutput:
    """将用户问题分解为事实调查需求。

    LLM 路径：分析问题语义 → 输出关系+目标+claims。
    确定性兜底路径：规则推断 relation type + 关键词提取。

    返回 PlannerOutput（不再返回 InvestigationTask 列表——展开由 expand_relations 完成）。
    """
    return _llm_query_planner(question, call_llm)


def _llm_query_planner(question: str, call_llm) -> PlannerOutput:
    """LLM 驱动的任务分解。失败时回退到确定性规则。"""
    if call_llm is None:
        return _fallback_query_planner(question)

    prompt = f"用户问题：{question}"

    try:
        raw = call_llm(
            prompt,
            system=_QUERY_PLANNER_SYSTEM,
            temperature=0,
            max_tokens=800,
            **_LLM_CALL_KWARGS,
        )
    except Exception:
        return _fallback_query_planner(question)

    if not raw or not isinstance(raw, str):
        return _fallback_query_planner(question)

    data = _extract_json(raw)
    if data is None:
        return _fallback_query_planner(question)

    errors = validate_planner_output(data)
    if errors:
        return _fallback_query_planner(question)

    return _build_planner_output(data)


def _extract_json(raw: str) -> dict | None:
    """从 LLM 返回文本中提取 JSON，容忍 markdown 代码块包裹。"""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _build_planner_output(data: dict) -> PlannerOutput:
    """将 LLM 输出的 JSON 转为 PlannerOutput 对象。"""
    relations: list[RelationDef] = []
    for i, rel in enumerate(data.get("relations", [])):
        try:
            rt = RelationType(rel.get("type", ""))
        except ValueError:
            continue
        subjects = rel.get("subjects", [])
        if not isinstance(subjects, list) or not subjects:
            continue
        claims = rel.get("required_claims", [])
        if not isinstance(claims, list):
            claims = []
        relations.append(RelationDef(
            type=rt,
            subjects=[s for s in subjects if isinstance(s, str) and s.strip()],
            required_claims=[c.strip() for c in claims if isinstance(c, str) and c.strip()],
            index=i,
        ))

    targets: list[PlannerTarget] = []
    for t in data.get("standalone_targets", []):
        if not isinstance(t, dict):
            continue
        sym = t.get("symbol", "")
        if not sym or not isinstance(sym, str):
            continue
        claims = t.get("required_claims", [])
        if not isinstance(claims, list):
            claims = []
        targets.append(PlannerTarget(
            symbol=sym.strip(),
            required_claims=[c.strip() for c in claims if isinstance(c, str) and c.strip()],
        ))

    global_claims = data.get("required_claims", [])
    if not isinstance(global_claims, list):
        global_claims = []

    return PlannerOutput(
        question_type=data.get("question_type", "locate"),
        relations=relations,
        standalone_targets=targets,
        required_claims=[c.strip() for c in global_claims
                         if isinstance(c, str) and c.strip()],
    )


# ── 确定性兜底 ──────────────────────────────────────────────────

# Requirement → RelationType 映射
_REQUIREMENT_TO_RELATION: dict = {
    "LOCATE_SYMBOL": RelationType.DEFINITION_LOCATION,
    "READ_IMPLEMENTATION": RelationType.EXPLAIN_BEHAVIOR,
    "EXPLAIN_BEHAVIOR": RelationType.EXPLAIN_BEHAVIOR,
    "TRACE_CALLER": RelationType.TRACE_CALL_CHAIN,
    "TRACE_CALLEE": RelationType.TRACE_CALL_CHAIN,
    "ENUMERATE_SYMBOLS": RelationType.ENUMERATE_USAGES,
    "ANALYZE_IMPACT": RelationType.IMPACT_CHANGE,
    "COMPARE_SYMBOLS": RelationType.COMPARE_BEHAVIOR,
    "FIND_LITERAL_USAGE": RelationType.ENUMERATE_USAGES,
}


# ── 确定性分类（从 investigator 移入，仅 fallback 使用）─────────

_QUESTION_PATTERNS = [
    (r"在哪|在哪里|where|定义|defined|位置|location|哪个文件|which file", "locate"),
    (r"所有|全部|列举|find\\s+all|列出|搜索.*出现|哪些地方|everywhere|所有.*引用", "grep"),
    (r"做什么|干什么|what.*do|作用|功能|purpose|负责|如何|怎么|怎样", "explain"),
    (r"影响|affect|impact|后果|导致|break|破坏", "impact"),
    (r"调用链|callee|依赖链|谁调|who.*call|caller|invoke\\b|调用者|被.*调用|依赖|depends|import|连接|connect|调用", "trace"),
    (r"比较|compare|vs|versus|区别|difference|不同|差异|diff", "compare"),
]


def _classify(question: str) -> tuple[str, list[Requirement]]:
    lower = question.lower()
    requirements: list[Requirement] = []
    if re.search(r"在哪|在哪里|where|定义|defined|位置|location|哪个文件|which file", lower):
        requirements.append(Requirement.LOCATE_SYMBOL)
    if re.search(r"做什么|干什么|what.*do|作用|功能|purpose|负责|用来|它是", lower):
        requirements.append(Requirement.READ_IMPLEMENTATION)
        requirements.append(Requirement.EXPLAIN_BEHAVIOR)
    if re.search(r"所有|全部|all|every|list|列举|find.*all|enumerate", lower):
        requirements.append(Requirement.ENUMERATE_SYMBOLS)
    if re.search(r"调用|谁调|who.*call|caller|callee|调用链", lower):
        requirements.append(Requirement.TRACE_CALLER)
    if re.search(r"依赖|depends|import|调用了|calling|被谁|依赖链", lower):
        requirements.append(Requirement.TRACE_CALLEE)
    if re.search(r"影响|affect|impact|后果|导致|break|破坏", lower):
        requirements.append(Requirement.ANALYZE_IMPACT)
    if re.search(r"比较|compare|vs|versus|区别|difference|不同|差异|diff", lower):
        requirements.append(Requirement.COMPARE_SYMBOLS)
    if re.search(r"字面|literal|字符串.*出现|string.*occur|使用.*模式", lower):
        requirements.append(Requirement.FIND_LITERAL_USAGE)
    if not requirements:
        requirements.append(Requirement.LOCATE_SYMBOL)
    for pattern, qtype in _QUESTION_PATTERNS:
        if re.search(pattern, lower):
            return qtype, requirements
    return "locate", requirements


def _fallback_query_planner(question: str) -> PlannerOutput:
    """确定性兜底：规则推断 relation type + 关键词提取 → PlannerOutput。"""
    # 延迟导入避免循环依赖
    from app.agent.investigator import InvestigationAgent  # noqa: E402
    _re = __import__("re")

    goal, requirements = _classify(question)
    keywords = InvestigationAgent._extract_keywords(question)

    # 确定主导 relation type
    relation_type = RelationType.DEFINITION_LOCATION
    if requirements:
        req_name = list(requirements)[0].name if hasattr(list(requirements)[0], "name") else ""
        relation_type = _REQUIREMENT_TO_RELATION.get(req_name, RelationType.DEFINITION_LOCATION)

    # 确定 question_type
    _REQ_TO_QTYPE = {
        "COMPARE_SYMBOLS": "compare",
        "TRACE_CALLER": "trace", "TRACE_CALLEE": "trace",
        "EXPLAIN_BEHAVIOR": "explain",
        "ANALYZE_IMPACT": "impact",
        "ENUMERATE_SYMBOLS": "grep",
        "FIND_LITERAL_USAGE": "grep",
    }
    question_type = "locate"
    if requirements:
        req_name = list(requirements)[0].name if hasattr(list(requirements)[0], "name") else ""
        question_type = _REQ_TO_QTYPE.get(req_name, "locate")

    # 提取目标符号
    symbols: list[str] = []
    if keywords:
        for kw in keywords[:4]:
            t = kw.qualified_symbol if isinstance(kw, TargetSpec) else str(kw)
            if t not in symbols:
                symbols.append(t)

    if not symbols:
        symbols = [question.strip()[:60]]

    # 构建全局 claims
    claims: list[str] = [goal] if goal else []
    for s in symbols[:3]:
        claim = f"{s} 的定义和作用"
        if claim not in claims:
            claims.append(claim)

    # compare 类型：尝试提取两个对比目标
    if relation_type == RelationType.COMPARE_BEHAVIOR and len(symbols) >= 2:
        return PlannerOutput(
            question_type=question_type,
            relations=[RelationDef(
                type=RelationType.COMPARE_BEHAVIOR,
                subjects=symbols[:2],
                required_claims=claims,
            )],
            standalone_targets=[PlannerTarget(symbol=s, required_claims=claims)
                               for s in symbols[2:3]],
            required_claims=claims,
        )

    # 其他类型：symbols 全部放入 relation subjects
    return PlannerOutput(
        question_type=question_type,
        relations=[RelationDef(
            type=relation_type,
            subjects=symbols[:3],
            required_claims=claims,
        )],
        standalone_targets=[],
        required_claims=claims,
    )
