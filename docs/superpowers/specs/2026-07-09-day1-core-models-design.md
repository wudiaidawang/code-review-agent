# Day 1 设计文档 — 核心数据模型与 Pipeline 骨架

> 对应计划书 Phase 1 / Day 1。目标：把三个"永不推倒重来"的核心对象定义好，
> 为后续所有 Analyzer（Git/AST/Ruff/Bandit/LLM）提供统一的数据契约与编排骨架。
>
> 日期：2026-07-09 ｜ 状态：设计 → 待实现

---

## 一、为什么是这几个对象（设计动机）

计划书的核心原则是：**底层稳定，以后任何新功能只是"加一个 Analyzer"。** 要做到这点，需要三层地基：

| 对象 | 角色比喻 | 解决的问题 |
|---|---|---|
| `Issue` | 系统的"通用货币" | 各工具输出格式不一 → 统一成一种结构，聚合/去重/排序/展示只写一套 |
| `ReviewContext` | 流水线里传的"公文包" | 各步骤不互相调用，只跟 Context 读写 → 步骤间解耦，可独立测试 |
| `PipelineStep` + `Pipeline` | 编排骨架 | 固定顺序执行分析步骤；预留 `should_run` 扩展点，未来加策略不必重构 |

**关键学习点：`should_run` 的前瞻设计。**
Day 1 就给每个步骤留一个 `should_run(context) -> bool` 接口，Phase 1 全部默认返回 `True`（等于顺序执行）。等 Phase 3 加"智能策略"（如改 README 就跳过 LLM）时，只需在子类覆盖 `should_run`，**Pipeline 本身一行不改**。这是"为已知的未来需求预留扩展点"，避免后期重构。

---

## 二、文件产出清单

```
app/
├── models/
│   ├── issue.py          # Issue 统一问题模型
│   └── context.py        # ReviewContext + InvestigationContext
└── core/
    ├── pipeline_step.py  # PipelineStep 抽象基类（含 should_run）
    └── pipeline.py       # Pipeline 执行器
tests/
└── test_pipeline.py      # 第一个单测
```

Phase 1 追求"能跑通、结构对"，**不追求字段完备**。复杂子类型（DiffData / ASTData 等）本 Day 不定义，Context 里先用宽松类型占位，等对应 Analyzer 那天再细化。

---

## 三、数据模型设计

### 3.1 `Issue`（app/models/issue.py）

统一问题模型。用 `dataclass`（轻量、无需校验框架；Phase 1 不引入 pydantic 依赖到模型层，API 层再用）。

**字段：**

| 字段 | 类型 | 含义 |
|---|---|---|
| `type` | str | bug / security / performance / style / architecture |
| `severity` | str | critical / high / medium / low / info |
| `file` | str | 文件路径 |
| `line` | int | 行号（0 表示非行级/整文件问题） |
| `title` | str | 简短描述 |
| `reason` | str | 为什么这是问题 |
| `fix` | str | 建议怎么改 |
| `source` | list[str] | 来源，如 ["ruff"] / ["llm"] / ["ruff","llm"]（去重合并后可多来源） |
| `references` | list[str] | RAG 检索到的相关规范（默认空） |

**设计说明：**
- `type` / `severity` 用字符串而非 Enum：Phase 1 保持简单，值域用模块级常量（`ISSUE_TYPES` / `SEVERITIES`）约束 + 可选校验，避免 Enum 在 JSON 序列化时的额外转换成本。
- `source` 用 list：为 Day 9 的"跨工具去重"预留——同一问题被 Ruff 和 LLM 同时报出时，合并为一条、source 记 `["ruff","llm"]`。
- `severity_rank()` 辅助方法：返回排序权重（critical 最大），供报告按严重度排序。

### 3.2 `ReviewContext`（app/models/context.py）

Review Mode 的"公文包"，在 Pipeline 中逐步被各 step 填充。

| 字段 | 类型 | 由哪天填充 |
|---|---|---|
| `repo_url` | str | 输入 |
| `commit` | str | 输入 |
| `diff` | Any（占位） | Day 2 GitDiffAnalyzer |
| `ast_data` | Any（占位） | Day 3 ASTAnalyzer |
| `function_info` | dict | Day 8 ContextAnalyzer |
| `knowledge_docs` | list[str] | Day 12 RAG |
| `issues` | list[Issue] | 各 Analyzer 追加 |
| `strategy_log` | list[str] | Pipeline 记录每步执行/跳过 |
| `stats` | dict | Day 6 统计 |

用 `dataclass` + `field(default_factory=...)` 给列表/字典默认值，避免可变默认参数陷阱（正是知识库里 cs_006 那条规范）。

### 3.3 `InvestigationContext`（app/models/context.py）

Investigation Mode（Agent 探索）的上下文。Phase 1 仅定义结构，Agent 逻辑 Day 15 才做。

字段：`question` / `repo_path` / `collected_info: list[str]` / `files_visited: list[str]` / `findings: list[dict]` / `current_hypothesis: str|None` / `step_count: int` / `answer: str|None`。

---

## 四、Pipeline 骨架设计

### 4.1 `PipelineStep`（app/core/pipeline_step.py）

抽象基类，所有分析步骤的父类。

```python
class PipelineStep(ABC):
    name: str = "step"                      # 用于日志

    def should_run(self, context) -> bool:  # 默认执行，子类可覆盖实现条件执行
        return True

    @abstractmethod
    def analyze(self, context) -> None:     # 读 context、处理、写回 context（无返回值）
        ...
```

**为什么 `analyze` 无返回值**：约定"就地修改 context"，让数据流向单一（都进 Context），聚合零成本。这与旧 LangGraph 版每个节点 return dict 的风格不同——新架构统一走 Context。

### 4.2 `Pipeline`（app/core/pipeline.py）

```python
class Pipeline:
    def __init__(self, steps: list[PipelineStep]):
        self.steps = steps

    def run(self, context):
        for step in self.steps:
            if step.should_run(context):
                step.analyze(context)
                context.strategy_log.append(f"[OK] {step.name}")
            else:
                context.strategy_log.append(f"[SKIP] {step.name}")
        return context
```

**Phase 1 不做的**：并行、异常兜底、超时。保持最简，先跑通。异常处理留到 Day 20（计划书明确排期）。

---

## 五、测试策略（tests/test_pipeline.py）

Phase 1 第一个单测，纯确定性、不碰网络。用 pytest。

| 用例 | 验证点 |
|---|---|
| 空 Pipeline | `Pipeline([]).run(ctx)` 不报错，strategy_log 为空 |
| 顺序执行 | 两个 step 按加入顺序执行（用记录调用顺序的假 step 验证） |
| should_run 跳过 | `should_run` 返回 False 的 step 不执行 analyze，且 strategy_log 记 `[SKIP]` |
| 写入 issues | 一个 step 向 `context.issues` append 一个 Issue，运行后 context 里能拿到 |
| Issue 排序 | `severity_rank()` 让 critical 排在 low 前面 |

**为什么先测 Pipeline 而非 Analyzer**：Pipeline 是纯编排逻辑、无外部依赖，最适合确定性单测；它跑通意味着"骨架能承载后续所有 step"。

---

## 六、验收标准（对照计划书 Day 1）

```
✅ python -m pytest tests/ 通过
✅ ReviewContext 能实例化，包含 diff/issues/ast/stats 字段
✅ Issue 有统一字段：type, severity, file, line, title, reason, fix, source
✅ Pipeline 的 should_run 返回 True 时执行，返回 False 时跳过
◻ FastAPI /health 返回 200 —— 本 Day 暂缓（等 Day 5 建 API 层时一并做，避免过早引入 Web 层）
```

**与计划书的一处偏差（如实标注）**：计划书 Day 1 含"FastAPI 脚手架 + /health"。本设计**推迟 FastAPI 到 Day 5**，理由：Day 1 聚焦纯数据模型与编排骨架（可纯单测验证），Web 层在有实际接口要暴露时再引入更自然，避免空壳 API。此偏差已记录，供回溯。

---

## 七、文件管理约定（本项目规范）

- 实现后同步更新 `docs/INDEX.md`（新增 4 个源文件 + tests）。
- 本段工作在 push 时补写进 `docs/CHANGELOG.md`。
- 见 `CLAUDE.md`。
