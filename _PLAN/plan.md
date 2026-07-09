## 更新日志

### 2026-07-09 — Phase 2 部分完成

**已完成：**
- [x] embedding 模型 — 已从默认 `all-MiniLM-L6-v2` 切换为 `BAAI/bge-m3`（中文匹配更强），HF 镜像设为 `hf-mirror.com`
- [x] GitHub Token — 已配置 `GITHUB_TOKEN` 到 `.env`

**待完成 (Phase 2)：**
- [ ] github MCP — 获取 PR Diff
- [ ] Strategy 模式重构 — 按 plan 原设计拆分 UnifiedReviewer → SecurityReviewer + QualityReviewer
- [ ] 错误处理增强 — LLM 解析失败重试 + RAG 失败降级 + 文件读取异常
- [ ] 代码规范知识库扩充 — 接入 Google Python Style Guide 完整文本
- [ ] MCP filesystem 标准化 — 当前直接 open()，改为标准 MCP 协议

---

### 2026-07-07 — Phase 1 完成

**已完成：**
- [x] ChromaDB 知识库 (`kb_manager.py`) — 3 个 collection：code_standards / vuln_patterns / review_history
- [x] 种子数据 (`kb_seed.py`) — 10 条代码规范 + 10 条 OWASP/CWE 漏洞模式
- [x] LLM 连接 (`llm_client.py`) — 智谱 GLM-4.5-Air，OpenAI 兼容接口
- [x] LangGraph 工作流 (`review_graph.py`) — 4 节点线性流转
  - [x] context_analyzer — RAG 检索知识库上下文
  - [x] reviewer — 安全 + 质量合并审查（GLM 输出 JSON）
  - [x] report_generator — 格式化 Markdown 报告
  - [x] history_recorder — 审查结果自动写入 review_history collection
- [x] CLI 入口 (`review.py`) — `python review.py <文件路径>`
- [x] Git 仓库 + 推送 — https://github.com/wudiaidawang/code-review-agent
- [x] 验收通过：输入 `sample_bad.py`，检出 10 个问题（Bug / 安全 / 代码异味）

---

你的分析非常到位，4个补充点都抓到了Phase 1的**关键风险点**。下面逐一点评，并给出可以直接开始vibecoding的具体指导。

---

## 一、状态机定义：补全状态流转与字段

你的直觉是对的，"定义状态机"不能只停留在概念层面。Phase 1的流转必须**极简且线性**，不要提前引入条件分支。

### 推荐状态流转

```
START → analyze → review → report → END
     ↑___________________________|
              (失败时重试，最多2次)
```

**为什么这样设计**：Phase 1不要引入`human_review`或`fix`节点，那些是Phase 2的事。线性流程确保你第一天就能跑通端到端。

### 状态字段定义（可直接复制使用）

```python
from typing import TypedDict, List, Optional, Literal
from pydantic import BaseModel

class Issue(BaseModel):
    severity: Literal["critical", "high", "medium", "low"]
    category: Literal["bug", "security", "code_smell"]  # 对应你的3类问题
    line: Optional[int] = None
    message: str
    suggestion: str

class ReviewState(TypedDict):
    # 输入
    file_path: str
    code_content: str
    
    # RAG上下文
    context: str              # 检索到的规范/历史记录
    
    # 审查结果
    issues: List[Issue]
    review_status: Literal["pending", "success", "failed"] = "pending"
    error_message: Optional[str] = None
    
    # 输出
    report: Optional[str] = None
```

**关键设计**：`review_status`和`error_message`是你错误处理的基础设施，每个节点都必须更新这两个字段。

---

## 二、知识库优先级：MVP最小集合

你的担心很实际。Phase 1**只需要1类知识库即可验收**，其余两类用**硬编码prompt + mock数据**替代。

| 知识库 | Phase 1策略 | 原因 |
|--------|------------|------|
| **代码规范** | ✅ **必须接入** | 这是RAG复用的核心，没有它系统就不叫"RAG增强的Agent" |
| 常见漏洞模式 | ❌ 暂不接入 | 在reviewer的system prompt里硬编码Top 5漏洞规则（SQL注入、XSS、硬编码密钥、路径遍历、eval） |
| 项目历史审查记录 | ❌ 暂不接入 | 用空列表mock，Phase 2再加 |

**验收时你只需要证明**："系统能根据检索到的规范文档，指出代码违反了哪条规则"。

**快速启动方案**：先把Google Python Style Guide的PDF/网页转成文本，切分chunk，存入你现有的向量库。不需要完美，能检索到3-5条相关规范即可。

---

## 三、Reviewer拆分：预留扩展点

你的前瞻性很好。合并的reviewer必须**从设计上就支持后续拆分**，否则Phase 2要重构大量代码。

### 推荐方案：Prompt模板化 + 策略接口

```python
from abc import ABC, abstractmethod
from typing import List

class ReviewStrategy(ABC):
    @abstractmethod
    def get_system_prompt(self, context: str) -> str:
        pass
    
    @abstractmethod
    def parse_issues(self, raw_response: str) -> List[Issue]:
        pass

class UnifiedReviewer(ReviewStrategy):
    """Phase 1：合并审查，但内部按category分类输出"""
    def get_system_prompt(self, context: str) -> str:
        return f"""你是一名严格的代码审查员。基于以下项目规范进行审查：
        {context}
        
        请检查以下3类问题，按JSON格式输出：
        1. bug - 逻辑错误、空指针、边界条件
        2. security - SQL注入、XSS、硬编码密钥、路径遍历
        3. code_smell - 命名不规范、函数过长、重复代码
        
        输出格式：[{{"severity": "...", "category": "...", "line": ..., "message": "...", "suggestion": "..."}}]
        """
    
    def parse_issues(self, raw_response: str) -> List[Issue]:
        # JSON解析逻辑
        pass

# LangGraph节点中这样调用：
def reviewer(state: ReviewState) -> ReviewState:
    strategy = UnifiedReviewer()  # Phase 1
    # Phase 2改成：strategies = [SecurityReviewer(), QualityReviewer(), ...]
    
    prompt = strategy.get_system_prompt(state["context"])
    response = llm.invoke(prompt + "\n\n代码：\n" + state["code_content"])
    issues = strategy.parse_issues(response.content)
    
    return {
        "issues": issues,
        "review_status": "success" if issues else "failed",
        "error_message": None
    }
```

**Phase 2的迁移成本**：只需要把`strategy = UnifiedReviewer()`改成`strategies = [SecurityReviewer(), QualityReviewer(), PerformanceReviewer()]`，然后并行调用。节点逻辑几乎不用改。

---

## 四、错误处理：异常路径设计

这是最容易被忽略但面试必问的点。Phase 1必须处理3类异常：

### 1. 文件读取失败（MCP工具层）

```python
def read_file_via_mcp(file_path: str) -> dict:
    try:
        # 调用filesystem MCP
        result = mcp_client.call_tool("read_file", {"path": file_path})
        if result.get("error"):
            return {"success": False, "error": result["error"], "content": ""}
        return {"success": True, "content": result["content"]}
    except Exception as e:
        return {"success": False, "error": str(e), "content": ""}
```

### 2. RAG检索失败（降级策略）

```python
def context_analyzer(state: ReviewState) -> ReviewState:
    try:
        context = rag_retrieve(state["code_content"])
        if not context:  # 检索为空
            context = "未检索到相关规范，使用通用Python最佳实践进行审查。"
    except Exception as e:
        context = f"RAG检索失败({str(e)})，使用通用规范进行审查。"
    
    return {"context": context, "review_status": "pending"}
```

**关键原则**：RAG失败不能阻断流程，必须**降级为通用prompt**继续审查。

### 3. LLM输出解析失败（重试+兜底）

```python
def reviewer(state: ReviewState) -> ReviewState:
    max_retries = 2
    for attempt in range(max_retries):
        try:
            response = llm.invoke(...)
            issues = parse_issues(response.content)
            return {"issues": issues, "review_status": "success"}
        except (json.JSONDecodeError, ValidationError) as e:
            if attempt == max_retries - 1:
                return {
                    "issues": [],
                    "review_status": "failed",
                    "error_message": f"LLM输出解析失败（重试{max_retries}次）：{str(e)}"
                }
            # 告诉LLM重新生成，强调格式要求
            continue
```

---

## 五、今天就能开始的「最小可行动作集」

按这个优先级执行，不要并行：

| 优先级 | 动作 | 预计时间 | 验收标准 |
|--------|------|----------|----------|
| **P0** | 把Google Python Style Guide文本化，存入现有向量库 | 1-2h | 能检索到"函数命名应该用snake_case"这类规范 |
| **P0** | 定义`ReviewState`和`Issue`模型 | 15min | 代码能运行，类型检查通过 |
| **P1** | 实现`context_analyzer`节点（RAG检索） | 1h | 输入一段代码，输出包含相关规范的字符串 |
| **P1** | 实现`reviewer`节点（合并Agent + 硬编码安全规则） | 1-2h | LLM能按JSON格式输出3类问题 |
| **P1** | 实现`report_generator`节点（Markdown格式化） | 30min | 输出美观的Markdown报告 |
| **P2** | 用LangGraph串联3个节点 | 1h | 输入文件路径 → 输出完整报告 |
| **P2** | 接入filesystem MCP读取本地文件 | 30min | 不用手动粘贴代码，直接读文件 |
| **P3** | 加错误处理（降级、重试） | 1h | 故意输入错误路径，系统 graceful 降级 |

**关键建议**：P0和P1完成后，你应该已经有一个**不依赖LangGraph、纯串行调用的原型**能跑通。这时候再套LangGraph的壳，而不是一开始就写状态机。

---

## 总结

你的4个补充点都击中了要害：

- **状态机**：Phase 1保持线性，Phase 2再加条件分支
- **知识库**：只接代码规范，其他两类mock掉
- **扩展性**：用Strategy模式封装reviewer，Phase 2无痛拆分
- **错误处理**：每个节点都要更新`review_status`，RAG失败降级，LLM失败重试

**下一步**：建议你先做P0（把规范入库），