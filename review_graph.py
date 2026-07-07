"""LangGraph 审查工作流 — 状态机 + 4 核心节点"""

import json
import uuid
from datetime import datetime
from typing import TypedDict, Annotated
import operator

from langgraph.graph import StateGraph, END

from llm_client import get_zhipu_client, get_zhipu_model
from kb_manager import KnowledgeBase

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
class ReviewState(TypedDict):
    code: str
    file_path: str
    kb_context: Annotated[list[str], operator.add]
    findings: str
    report: str


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------
def context_analyzer(state: ReviewState, kb: KnowledgeBase) -> dict:
    """RAG 检索：用代码片段搜索相关知识库上下文"""
    code = state["code"]
    queries = [
        code[:500],                              # 代码开头
        code[len(code)//2: len(code)//2 + 500],  # 代码中部
        code[-500:],                             # 代码尾部
    ]
    context = []
    seen = set()
    for q in queries:
        for doc in kb.query(q, n_results=4):
            if doc not in seen:
                context.append(doc)
                seen.add(doc)
    return {"kb_context": context}


def reviewer(state: ReviewState) -> dict:
    """核心审查 Agent：安全 + 质量合并审查"""
    client = get_zhipu_client()
    kb_text = "\n---\n".join(state["kb_context"]) if state["kb_context"] else "（无相关知识库匹配）"

    system_prompt = """你是一位资深 Python 代码审查专家，精通安全审计和工程质量。

请逐行仔细审查代码，重点检查以下模式：

**Bug 类：**
- 可变默认参数 (def f(x=[]))
- 除以零、索引越界、空值解引用
- 逻辑错误、错误返回值
- 未关闭的文件/连接/资源

**安全漏洞类：**
- SQL/命令注入（字符串拼接构造查询、os.system + 用户输入、shell=True）
- 硬编码密钥/密码/Token
- 不安全的反序列化 (pickle.loads)
- 路径遍历
- 弱加密算法 (MD5/SHA1)
- 不安全的随机数 (random.random 用于安全场景)

**代码异味类：**
- 裸 except:（未指定异常类型）
- 全局变量
- 过多嵌套、过长的函数
- 缺少类型注解
- 未使用的变量/导入
- 使用 assert 而非 raise

请严格按照以下格式输出纯 JSON（不要 Markdown 代码块包裹）：

{
  "bugs": [
    {"severity": "high|medium|low", "line": "行号", "title": "简短标题", "description": "详细描述", "suggestion": "修复建议"}
  ],
  "security": [
    {"severity": "critical|high|medium|low", "line": "行号", "title": "简短标题", "cwe": "CWE编号", "description": "详细描述", "suggestion": "修复建议"}
  ],
  "code_smells": [
    {"severity": "high|medium|low", "line": "行号", "title": "简短标题", "description": "详细描述", "suggestion": "重构建议"}
  ],
  "summary": "整体评价"
}

如果某类没有问题，返回空数组 []。line 必须是具体行号数字。"""

    user_prompt = f"""## 知识库参考（以下规范/漏洞模式供审查时参考）

{kb_text}

## 待审查代码

文件: {state["file_path"]}

```python
{state["code"]}
```

请对以上代码进行安全 + 质量审查，识别 bugs、安全漏洞、代码异味。按 JSON 格式输出。"""

    response = client.chat.completions.create(
        model=get_zhipu_model(),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.2,
        max_tokens=3000,
    )
    return {"findings": response.choices[0].message.content}


def report_generator(state: ReviewState) -> dict:
    """将审查结果格式化为 Markdown 报告"""
    client = get_zhipu_client()

    prompt = f"""请将以下审查 JSON 结果格式化为一份美观的 Markdown 审查报告。
要求：
- 直接输出 Markdown 正文，不要用 ```markdown 代码块包裹
- 使用中文
- 包含标题、摘要、分节（Bug / 安全漏洞 / 代码异味）
- 每条问题用表格展示（严重程度、位置、标题、描述、建议）
- 结尾给出整体评分（1-10分），并说明扣分原因

审查结果：
{state["findings"]}

文件：{state["file_path"]}"""

    response = client.chat.completions.create(
        model=get_zhipu_model(),
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=3000,
    )
    return {"report": response.choices[0].message.content}


def history_recorder(state: ReviewState, kb: KnowledgeBase) -> dict:
    """将审查发现的问题写入 history collection，供后续审查参考"""
    try:
        findings = json.loads(state["findings"])
    except json.JSONDecodeError:
        return {}

    file_path = state["file_path"]
    timestamp = datetime.now().isoformat()

    for category, cat_label in [("bugs", "bug"), ("security", "security"), ("code_smells", "code_smell")]:
        for item in findings.get(category, []):
            record_id = f"review_{uuid.uuid4().hex[:12]}"
            text = (
                f"[{cat_label.upper()}] {item.get('title', '')} "
                f"| 严重程度: {item.get('severity', '?')} "
                f"| 行号: {item.get('line', '?')} "
                f"| {item.get('description', '')} "
                f"| 建议: {item.get('suggestion', '')}"
            )
            kb.add_review_record({
                "id": record_id,
                "text": text,
                "meta": {
                    "file_path": file_path,
                    "category": cat_label,
                    "severity": item.get("severity", ""),
                    "line": str(item.get("line", "")),
                    "timestamp": timestamp,
                },
            })
    return {}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
_kb: KnowledgeBase | None = None


def build_graph(kb: KnowledgeBase | None = None) -> StateGraph:
    global _kb
    if kb is not None:
        _kb = kb

    graph = StateGraph(ReviewState)

    graph.add_node("context_analyzer", lambda s: context_analyzer(s, _kb))
    graph.add_node("reviewer", reviewer)
    graph.add_node("report_generator", report_generator)
    graph.add_node("history_recorder", lambda s: history_recorder(s, _kb))

    graph.set_entry_point("context_analyzer")
    graph.add_edge("context_analyzer", "reviewer")
    graph.add_edge("reviewer", "report_generator")
    graph.add_edge("report_generator", "history_recorder")
    graph.add_edge("history_recorder", END)

    return graph.compile()


def review_file(file_path: str) -> str:
    """审查单个文件，返回 Markdown 报告"""
    with open(file_path, "r", encoding="utf-8") as f:
        code = f.read()

    graph = build_graph()
    result = graph.invoke({"code": code, "file_path": file_path})
    return result["report"]
