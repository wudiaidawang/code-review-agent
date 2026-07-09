# AI Code Review Platform — V2 修订版任务计划书

> **目标：21天内（7/9 - 7/29）完成一个可演示、可部署、可写进简历的AI代码审查平台，支持两种模式：Review Mode（Pipeline审查）和 Investigation Mode（Agent探索）。**
>
> **最后2天（7/30 - 7/31）用于简历撰写和面试准备。**

---

## 一、项目定位

**一句话定位：**

> 企业级 AI Code Review 平台，支持 Pipeline 审查与 Agent 自主探索双模式。

**核心设计原则：**

| 原则 | 说明 |
|---|---|
| 底层必须稳定 | Pipeline + ReviewContext + Issue 三个核心对象定义好后不推倒重来，以后任何新功能只是"加一个Analyzer" |
| 确定性交给程序 | Git Diff、AST、Ruff、Bandit、依赖分析——有确定答案的不走LLM |
| 语义问题交给LLM | "为什么这里设计不好""有没有隐藏Bug"——这些才调LLM |
| 所有输出统一 | 任何模块最后都输出 `Issue` 对象，统一 schema，聚合零成本 |
| 两种模式，一套工具 | Review Mode 用 Pipeline，Investigation Mode 用 Agent，工具层完全共享 |

---

## 二、整体架构

```
                    用户请求
                         │
          ┌──────────────┴──────────────┐
          │                             │
          ▼                             ▼
 Review Mode（标准审查）      Investigation Mode（探索分析）
 （Pipeline固定流水线）        （Agent自主探索）

          │                             │
          └──────────────┬──────────────┘
                         ▼
               Tool Layer（共享工具层）
      Git / AST / Ruff / Bandit / RAG / LLM / Search

                         │
                         ▼
              Knowledge Layer（知识层）

                         │
                         ▼
             Report / Answer（输出结果）
```

**Review Mode：** 输入PR链接，固定Pipeline依次执行，输出结构化报告。

**Investigation Mode：** 输入自然语言问题（如"认证流程怎么实现的"），Agent自主决定每一步做什么，输出带代码引用的回答。

**架构演进路线：**

```
Phase 1（Day 1-7）  ── 底层 + 端到端可跑（Pipeline）
Phase 2（Day 8-14） ── 分析能力增强 + 前端完善
Phase 3（Day 15-21）── Agent编排 + 亮点功能 + 部署
收尾（Day 22-23）   ── 简历 + 面试准备
```

---

## 三、技术栈

| 模块 | 技术 |
|---|---|
| 后端 | FastAPI |
| LLM | DeepSeek API / Qwen API（OpenAI兼容接口） |
| AST | Tree-sitter + tree-sitter-python |
| 静态分析 | Ruff（规范） + Bandit（安全） |
| Git操作 | GitPython |
| 向量库 | Chroma |
| Embedding | BAAI BGE-M3（sentence-transformers） |
| 数据库 | SQLite（轻量，V1够用） |
| 前端 | React + TypeScript + Vite + Tailwind CSS |
| 报告 | Markdown + Jinja2 HTML |
| 部署 | Docker Compose |

---

## 四、关键数据模型（全程不变）

### Issue（统一问题模型）

```python
@dataclass
class Issue:
    type: str          # bug / security / performance / style / architecture
    severity: str      # critical / high / medium / low / info
    file: str          # 文件路径
    line: int          # 行号
    title: str         # 简短描述
    reason: str        # 为什么这是问题
    fix: str           # 建议怎么改
    source: list[str]  # ["ruff", "llm", "bandit", "rag"]
    references: list   # RAG检索到的相关规范
```

### ReviewContext（上下文对象）

```python
@dataclass
class ReviewContext:
    repo_url: str
    commit: str
    diff: DiffData                    # Git Diff结果
    ast_data: ASTData                 # Tree-sitter解析结果
    function_info: dict               # 函数/类/调用关系
    knowledge_docs: list              # RAG检索到的规范
    issues: list[Issue]               # 所有Issue（各模块追加）
    strategy_log: list[str]           # 策略执行日志
    stats: ReviewStats                # 统计数据
```

### InvestigationContext（探索上下文）

```python
@dataclass
class InvestigationContext:
    question: str                     # 用户的问题
    repo_path: str                    # 代码仓库本地路径
    collected_info: list[str]         # 已收集的信息（逐步累积）
    files_visited: list[str]          # 已访问的文件
    findings: list[dict]              # 发现的线索
    current_hypothesis: str | None    # 当前假设
    step_count: int                   # 已执行步骤数
    answer: str | None                # 最终答案
```

### Analyzer（基类）

```python
class Analyzer(ABC):
    @abstractmethod
    def analyze(self, context: ReviewContext) -> None:
        """读取context中的数据，处理后将结果写回context"""
        pass
```

### Pipeline（流水线，带可插拔条件执行）

```python
class PipelineStep(ABC):
    @abstractmethod
    def should_run(self, context: ReviewContext) -> bool:
        """默认返回True，子类可覆盖实现条件执行"""
        return True

    @abstractmethod
    def analyze(self, context: ReviewContext) -> None:
        pass

class Pipeline:
    def __init__(self, steps: list[PipelineStep]):
        self.steps = steps

    def run(self, context: ReviewContext):
        for step in self.steps:
            if step.should_run(context):
                step.analyze(context)
                context.strategy_log.append(f"✅ {step.name}")
            else:
                context.strategy_log.append(f"⏭️ {step.name} (跳过)")
```

### Plan / PlanStep（Agent规划结构）

```python
class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"

@dataclass
class PlanStep:
    step_id: str
    name: str
    tool: str                        # "git", "ast", "ruff", "llm", "rag", "search", "dependency"
    args: dict
    depends_on: list[str] = field(default_factory=list)
    status: StepStatus = StepStatus.PENDING
    result: any = None

@dataclass
class Plan:
    steps: list[PlanStep]

    def get_ready_steps(self) -> list[PlanStep]:
        """返回所有依赖已满足、可以执行的步骤"""
        done_ids = {s.step_id for s in self.steps if s.status == StepStatus.DONE}
        return [
            s for s in self.steps
            if s.status == StepStatus.PENDING
            and all(dep in done_ids for dep in s.depends_on)
        ]
```

---

## 五、三周详细任务计划

---

### Phase 1：底层架构 + 端到端可跑（Day 1-7）

> **核心目标：第一周结束时，有一个从输入Git仓库到生成Markdown报告的完整链路能跑通。**
>
> 如果第二周第三周出问题，至少你有了一个能写简历的项目。

---

#### Day 1：项目脚手架 + 核心数据模型

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 1.1 | 初始化项目结构（按目录规范） | 完整目录骨架 | 0.5h |
| 1.2 | 创建FastAPI项目脚手架 | `main.py` + health check接口 | 0.5h |
| 1.3 | 定义 `ReviewContext` 模型 | `app/models/context.py` | 1.5h |
| 1.4 | 定义 `InvestigationContext` 模型 | `app/models/context.py` 新增 | 0.5h |
| 1.5 | 定义 `Issue` 统一问题模型 | `app/models/issue.py` | 1h |
| 1.6 | 定义 `ReviewRequest` / `ReviewResponse` API模型 | `app/models/api.py` | 1h |
| 1.7 | 定义 `PipelineStep` 基类（带 `should_run` 接口） | `app/core/pipeline_step.py` | 0.5h |
| 1.8 | 定义 `Pipeline` 类（步骤列表 → 条件执行） | `app/pipeline/pipeline.py` | 1h |
| 1.9 | 写第一个单测：Pipeline空跑不报错 | `tests/test_pipeline.py` | 0.5h |

**验收标准：**

```
✅ python -m pytest tests/ 通过
✅ FastAPI /health 返回200
✅ ReviewContext 能实例化，包含 diff/issues/ast/stats 字段
✅ Issue 有统一字段：type, severity, file, line, title, reason, fix, source
✅ Pipeline 的 should_run 返回 True 时执行，返回 False 时跳过
```

**产出目录结构：**

```
app/
    __init__.py
    main.py
    api/
        __init__.py
        routes_review.py          # Day 5 写
    core/
        __init__.py
        pipeline_step.py          # PipelineStep基类（带should_run）
        pipeline.py               # Pipeline执行器
    models/
        __init__.py
        context.py                # ReviewContext + InvestigationContext
        issue.py
        api.py
    tools/                        # Day 2-4 填充
    retriever/                    # Day 12 填充
    parser/                       # Day 3 填充
    pipeline/
    reviewers/                    # Day 5/11 填充
    report/                       # Day 6 填充
    agent/                        # Day 15 填充（Agent编排层）
    memory/                       # V2
configs/
    settings.py
tests/
    test_pipeline.py
requirements.txt
```

**设计说明：**

```
Day 1 就把 PipelineStep 基类设计为带 should_run 接口。
Phase 1 所有步骤的 should_run 默认返回 True（等同于顺序执行）。
Phase 3 加策略时，子类只需覆盖 should_run，不用改 Pipeline 本身。
这样 Phase 3 不需要重构 Phase 1 的代码。
```

---

#### Day 2：Git Diff Analyzer

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 2.1 | 安装GitPython，封装Git工具类 | `app/tools/git_tool.py` | 1h |
| 2.2 | 实现 `clone_repo(repo_url)` | 克隆远程仓库到本地临时目录 | 1h |
| 2.3 | 实现 `get_diff(commit_a, commit_b)` | 获取两次commit之间的diff | 1h |
| 2.4 | 实现 `get_diff_by_branch(branch)` | 获取分支相对于main的diff | 0.5h |
| 2.5 | 实现 `parse_diff_to_files(raw_diff)` | 解析diff为文件级变更列表 | 1.5h |
| 2.6 | 实现 `GitDiffAnalyzer(PipelineStep)` | `app/analyzers/git_diff_analyzer.py` | 1h |
| 2.7 | 写单测：用一个真实repo测试 | `tests/test_git_diff.py` | 1h |

**验收标准：**

```
✅ 输入GitHub仓库URL + commit hash → 能clone并获取diff
✅ diff解析为结构化数据：每个文件的变更类型(added/modified/deleted/renamed)
✅ 每个文件的变更包含：old_content, new_content, hunks（带行号）
✅ GitDiffAnalyzer 将结果写入 ReviewContext.diff
✅ 单测覆盖：新增文件、修改文件、删除文件三种场景
```

**关键数据结构：**

```python
@dataclass
class FileDiff:
    path: str
    change_type: str              # added / modified / deleted / renamed
    old_content: str | None
    new_content: str | None
    hunks: list[Hunk]

@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    added_lines: list[str]
    removed_lines: list[str]
    context_lines: list[str]
```

**今日必做：准备一个测试用仓库。**

```
建议用你自己以前的项目，或者fork一个中小型Python项目（如fastapi官方examples），
手动提交几个包含bug的commit，作为后续所有测试的数据源。
今天就准备好，后面每天都用它。
```

---

#### Day 3：AST Parser（Tree-sitter）

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 3.1 | 安装 `tree-sitter` + `tree-sitter-python` | 依赖就绪 | 0.5h |
| 3.2 | 封装Python AST Parser：提取函数定义 | `app/parser/python_parser.py` | 2h |
| 3.3 | 扩展：提取类定义、方法、继承关系 | 同上 | 1.5h |
| 3.4 | 扩展：提取import语句 | 同上 | 0.5h |
| 3.5 | 扩展：提取函数调用关系 | 同上 | 1.5h |
| 3.6 | 实现 `ASTAnalyzer(PipelineStep)` | `app/analyzers/ast_analyzer.py` | 1h |
| 3.7 | 写单测：解析一个真实Python文件 | `tests/test_ast_parser.py` | 1h |

**验收标准：**

```
✅ 输入Python源码 → 输出函数列表、类列表、import列表、调用关系
✅ 每个函数包含：name, params, start_line, end_line, docstring, decorators
✅ 每个类包含：name, bases, methods列表
✅ 调用关系：函数A内调用了函数B → 输出 (caller, callee, line)
✅ ASTAnalyzer 仅对 .py 文件执行，其他文件跳过
✅ 结果写入 ReviewContext.ast_data
```

**关键数据结构：**

```python
@dataclass
class FunctionInfo:
    name: str
    params: list[str]
    start_line: int
    end_line: int
    docstring: str | None
    decorators: list[str]
    parent_class: str | None

@dataclass
class ClassInfo:
    name: str
    bases: list[str]
    methods: list[FunctionInfo]
    start_line: int
    end_line: int

@dataclass
class CallRelation:
    caller: str
    callee: str
    line: int
```

**Tree-sitter真实难点提醒：**

```
Tree-sitter的API基于S-expression查询语法，学习成本不低。
每种语言的节点名不同。
今天只做Python一门语言。先做函数+类提取，调用关系放在最后，能做就做，做不完明天补。
面试时说"架构上设计了语言适配器，扩展新语言只需实现parser接口"就够了。
```

---

#### Day 4：静态分析集成（Ruff + Bandit）

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 4.1 | 安装Ruff，确认CLI调用方式 | 命令行验证通过 | 0.5h |
| 4.2 | 实现Ruff执行器：subprocess调用 + JSON解析 | `app/tools/linter_tool.py` | 1.5h |
| 4.3 | 将Ruff输出适配为Issue模型 | `app/analyzers/rule_analyzer.py` | 1h |
| 4.4 | 安装Bandit，确认CLI调用方式 | 命令行验证通过 | 0.5h |
| 4.5 | 实现Bandit执行器 + 输出适配为Issue | 同上文件 | 1.5h |
| 4.6 | 实现 `RuleAnalyzer(PipelineStep)`，内部调度Ruff和Bandit | 整合完成 | 1h |
| 4.7 | 写单测：故意写一段有问题的代码，验证检测 | `tests/test_rule_analyzer.py` | 1h |
| 4.8 | 关键：只对diff中修改的文件运行Ruff/Bandit | 性能优化 | 1h |

**验收标准：**

```
✅ 修改过的.py文件 → Ruff自动检查 → 输出Issue列表
✅ 修改过的.py文件 → Bandit自动检查 → 输出Issue列表
✅ 未修改的文件不检查（节省时间）
✅ Issue包含正确的file、line、title、severity映射
✅ Ruff的severity映射：E→warning, F→warning, W→info
✅ Bandit的severity映射：HIGH→critical, MEDIUM→warning, LOW→info
```

**Ruff输出格式参考（需要解析这个JSON）：**

```json
[
  {
    "code": "F841",
    "message": "Local variable `x` is assigned to but never used",
    "filename": "src/app.py",
    "location": {"row": 10, "column": 5},
    "fix": {"applicability": "safe"}
  }
]
```

---

#### Day 5：LLM Reviewer + API接口

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 5.1 | 封装LLM调用（DeepSeek API / OpenAI兼容接口） | `app/tools/llm_tool.py` | 1.5h |
| 5.2 | 设计System Prompt（代码审查专家角色） | `configs/prompts/system_review.md` | 1h |
| 5.3 | 设计Diff Review Prompt模板 | `configs/prompts/diff_review.md` | 1h |
| 5.4 | 实现 `LLMReviewer(PipelineStep)`：拼装prompt → 调LLM → 解析为Issue | `app/reviewers/llm_reviewer.py` | 2h |
| 5.5 | Prompt输出格式约束：要求LLM输出JSON格式的Issue列表 | prompt中加JSON schema约束 | 1h |
| 5.6 | 实现 `/api/v1/review` 接口（接收repo_url + commit） | `app/api/routes_review.py` | 1.5h |
| 5.7 | 手动测试：curl调用 → 拿到完整报告 | 端到端验证 | 0.5h |

**验收标准：**

```
✅ POST /api/v1/review {"repo_url": "...", "commit": "..."} → 返回200
✅ 响应包含：issues列表、summary文本、score数字
✅ LLM输出被解析为结构化Issue（如果LLM输出格式不对，有兜底处理）
✅ 单个文件的review prompt不超过4096 token（控制输入长度）
```

**LLM Prompt设计要点：**

```
你是一个高级代码审查专家。

## 输入
- 文件路径：{file_path}
- 变更类型：{change_type}
- Diff内容：
{diff_content}

## 上下文
- AST信息：函数{func_name}，参数{params}，属于类{class_name}
- 已发现的静态问题：{ruff_issues}

## 要求
请分析以上代码变更，输出JSON数组，每个元素格式如下：
{
  "type": "bug|security|performance|style|architecture",
  "severity": "critical|high|medium|low|info",
  "title": "简短描述",
  "reason": "为什么这是问题",
  "fix": "建议怎么改"
}

## 限制
- 只分析diff中变更的代码，不要评价未修改的代码
- 如果代码没有问题，返回空数组 []
- 不要输出与静态分析工具重复的问题
```

---

#### Day 6：Report Generator + Aggregator基础版

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 6.1 | 实现Markdown报告生成器 | `app/report/markdown_report.py` | 1.5h |
| 6.2 | 实现JSON报告生成器 | `app/report/json_report.py` | 0.5h |
| 6.3 | 实现HTML报告生成器（Jinja2模板） | `app/report/html_report.py` + template | 2h |
| 6.4 | 实现基础Aggregator：按severity排序 + 统计 | `app/pipeline/aggregator.py` | 1.5h |
| 6.5 | 实现基础去重：同文件同行号同type的Issue合并 | 同上 | 1h |
| 6.6 | 实现评分算法：根据Issue数量和severity计算0-100分 | 同上 | 1h |
| 6.7 | 串联完整Pipeline测试 | 端到端验证 | 0.5h |

**验收标准：**

```
✅ Markdown报告格式：标题、评分、Issue列表、统计
✅ HTML报告有基本样式，浏览器打开可读
✅ Issue按severity排序：critical > high > medium > low > info
✅ 相同file+line+type的Issue只保留一条（取severity更高的）
✅ 评分规则：100 - critical*15 - high*8 - medium*3 - low*1，最低0分
```

**报告模板示例：**

```markdown
# Code Review Report

**Score: 84/100**
**Issues: 12 (2 critical, 3 high, 4 medium, 3 low)**
**Files Changed: 8**
**Review Time: 12.3s**

---

## Critical Issues

### [C-001] SQL Injection Risk
- **File:** `app/api/users.py:54`
- **Source:** Bandit + LLM
- **Reason:** 字符串拼接构造SQL语句，存在注入风险
- **Suggestion:** 使用参数化查询

---

## High Issues
...
```

---

#### Day 7：前端MVP + 整体联调

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 7.1 | 初始化React + TypeScript + Vite项目 | `frontend/` 目录 | 0.5h |
| 7.2 | 实现首页：Git URL输入框 + Commit输入框 + Review按钮 | `HomePage.tsx` | 1.5h |
| 7.3 | 实现Review结果页：Markdown报告渲染 | `ReviewPage.tsx` | 2h |
| 7.4 | 实现Issue列表组件：按severity分组展示 | `IssueList.tsx` | 2h |
| 7.5 | 对接后端API（axios/fetch） | `api/review.ts` | 1h |
| 7.6 | 整体联调：输入URL → 点击 → 看到报告 | 端到端验证 | 1h |
| 7.7 | 修bug + 处理异常情况（仓库不存在、大仓库超时等） | 错误处理 | 1h |

**验收标准：**

```
✅ 浏览器输入URL，点击Review，30秒内看到结果
✅ Issue按severity颜色标记（critical红、high橙、medium黄、low灰）
✅ 页面不崩溃，loading状态有提示
✅ 错误有友好的提示信息（不是白屏）
```

**前端技术栈：** Vite + React + TypeScript + react-markdown + Tailwind CSS

---

#### Phase 1 完成后的能力

```
输入：Git仓库URL + Commit Hash
         ↓
    clone仓库
         ↓
    获取Diff
         ↓
    Tree-sitter提取AST
         ↓
    Ruff + Bandit静态检查
         ↓
    LLM语义审查
         ↓
    聚合 + 去重 + 评分
         ↓
    生成Markdown/HTML报告
         ↓
    前端展示

输出：一份结构化的代码审查报告，包含评分、Issue列表、建议
```

**这个版本已经可演示。如果后面两周出问题，至少你有了一个能写简历的项目。**

---

### Phase 2：分析能力增强 + 前端完善（Day 8-14）

> **核心目标：系统从"能跑"变成"真正有价值"，前端从"能看"变成"好用"。**

---

#### Day 8：Context Analyzer（上下文分析器）

> 让系统知道"改的这段代码在更大的结构中处于什么位置"。

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 8.1 | 扩展AST Parser：支持提取类信息（类定义、方法列表、继承关系） | `python_parser.py` 新增 `extract_classes()` | 1.5h |
| 8.2 | 扩展AST Parser：支持提取import关系 | `extract_imports()` | 0.5h |
| 8.3 | 实现函数定位：给定diff中的行号，找到所属函数和类 | `locate_function(line_number)` | 1.5h |
| 8.4 | 实现调用关系分析 | 基于call_expression AST节点，建立caller→callee映射 | 2h |
| 8.5 | 实现影响范围计算 | 给定被修改的函数，找出所有直接调用它的地方 | 1h |
| 8.6 | 封装为 `ContextAnalyzer(PipelineStep)` | `analyzers/context_analyzer.py` | 1h |
| 8.7 | 更新LLM Reviewer的Prompt：注入上下文信息 | `prompts/diff_review.md` 更新 | 0.5h |
| 8.8 | 测试 | 修改一个基础工具函数，验证影响范围是否正确 | 1h |

**写入ReviewContext的数据：**

```python
context.function_info = {
    "modified_functions": [
        {
            "name": "calculate_total",
            "class": "OrderService",
            "params": ["self", "items: list[Item]", "tax_rate: float"],
            "docstring": "计算订单总价，包含税费",
            "start_line": 45,
            "end_line": 62,
            "callers": ["process_order", "preview_order"],
            "callees": ["apply_discount", "round_decimal"],
        }
    ],
    "impact_summary": "本次修改的 calculate_total 被 2 个函数调用，影响范围中等"
}
```

**验收标准：**

```
✅ 输入一个Python文件 + diff行号 → 输出该行所属的函数名和类名
✅ 输入一个函数名 → 输出所有调用它的函数列表
✅ 调用关系图支持跨文件分析（同仓库内import的文件）
✅ LLM prompt中包含："此函数属于 OrderService 类，被 process_order 和 preview_order 调用"
✅ 如果修改的是公共API函数（被3个以上函数调用），LLM prompt中提示"影响范围较大"
```

---

#### Day 9：Aggregator增强 + 评分优化

> 解决"同一个问题被Ruff和LLM同时报出来"的噪音问题。

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 9.1 | 实现基于文件+行号+语义相似度的去重逻辑 | `pipeline/aggregator.py` 增强 | 2h |
| 9.2 | 实现语义相似度判断（关键词匹配，不引入模型） | `utils/text_similarity.py` | 1.5h |
| 9.3 | 合并时保留来源信息：source字段标注 ["ruff", "llm"] | Issue模型增强 | 0.5h |
| 9.4 | 实现Issue分组：同一文件的Issue归为一组 | Aggregator新增 `group_by_file()` | 1h |
| 9.5 | 实现评分算法：根据Issue数量和severity计算0-100分 | Aggregator增强 | 1.5h |
| 9.6 | 实现Review Summary自动生成（LLM总结） | `report/summary_generator.py` | 1.5h |
| 9.7 | 测试 | 用两个已知有重复Issue的PR测试去重效果 | 1h |

**评分算法设计：**

```python
def calculate_score(issues: list[Issue], context: ReviewContext) -> int:
    """
    基础分100
    每个critical: -15, high: -8, medium: -3, low: -1, info: 0
    影响范围加权：被3个以上函数调用的Issue扣分x1.5，公共API扣分x1.3
    最低0分，最高100分
    """
```

**验收标准：**

```
✅ Ruff的F841 + LLM的"未使用变量" → 只保留一条，source标注为 ["ruff", "llm"]
✅ 同文件的Issue在报告中归为一组
✅ 评分算法输出合理：无Issue时100分，10个critical时0分
✅ Review Summary不超过200字，包含最关键问题和整体风险评估
```

---

#### Day 10：前端增强——代码Diff视图

> 前端升级为"类GitHub PR Review界面"。

**目标布局：**

```
┌──────────────────────────────────────────────────────┐
│  AI Code Review Platform            Score: 84/100    │
├──────────────┬───────────────────────────────────────┤
│ File Tree    │  Code Diff View                       │
│              │                                       │
│ > src/       │  45 │     def calculate_total(         │
│   app.py ✕3  │  46 │         self,                    │
│   utils.py✕1 │  47 │         items: list[Item],      │
│ > tests/     │  48 │         tax_rate: float = 0.1   │
│   test_app   │  49 │     ) -> Decimal:               │
│              │  50 │ +       total = Decimal("0")    │
│              │  51 │ +       for item in items:      │
│              │  52 │ +           total += item.price  │
├──────────────┴───────────────────────────────────────┤
│ Issues (12)                                          │
│  🔴 [C-001] SQL Injection       app.py:54            │
│  🟠 [H-001] 未使用变量           app.py:61            │
│  🟡 [M-001] 函数过长             app.py:45-89         │
└──────────────────────────────────────────────────────┘
```

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 10.1 | 技术选型确认：CodeMirror 6（轻量，有diff扩展）或降级为 `<pre>` + 行号 | 决策 | 0.5h |
| 10.2 | 实现代码Diff视图组件：左侧修改前，右侧修改后，变更行高亮 | `CodeDiffView.tsx` | 2.5h |
| 10.3 | 实现文件树组件：显示修改了哪些文件，点击切换代码 | `FileTree.tsx` | 1.5h |
| 10.4 | 实现Issue列表组件增强版：severity颜色标记 | `IssueList.tsx` 增强 | 1.5h |
| 10.5 | 实现点击联动：点击Issue → 代码滚动到对应行 + 高亮 | 事件通信 | 2h |
| 10.6 | 实现三栏布局：File Tree (20%) + Code (40%) + Issues (40%) | 布局重构 | 1h |
| 10.7 | 测试 | 用一个真实PR测试所有交互 | 1h |

**验收标准：**

```
✅ 三栏布局：文件树 | 代码Diff | Issue列表
✅ 点击文件树 → 代码区显示该文件的diff
✅ 点击Issue → 代码区滚动到对应行 + 该行背景高亮
✅ 代码有语法高亮（至少Python）
✅ 新增行绿色背景，删除行红色背景
```

---

#### Day 11：多角色Reviewer

> 不是一个LLM笼统review，而是多个角色从不同角度审查。

**四个Reviewer角色：**

| 角色 | 关注点 | 典型Issue |
|---|---|---|
| Bug Hunter | 逻辑错误、边界条件、空指针、类型错误 | "当items为空列表时，未处理零除异常" |
| Security Auditor | 安全漏洞、注入风险、敏感信息泄露 | "第54行使用字符串拼接构造SQL，存在注入风险" |
| Style Coach | 代码风格、命名规范、可读性、注释 | "函数名建议改为calculate_order_total以明确语义" |
| Architecture Critic | 设计模式、职责单一、耦合度、可扩展性 | "OrderService同时承担了计算和持久化职责，建议拆分" |

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 11.1 | 编写4个Reviewer的System Prompt | `configs/prompts/reviewers/` 下4个文件 | 2h |
| 11.2 | 实现 `MultiReviewer` 类 | `reviewers/multi_reviewer.py` | 1.5h |
| 11.3 | 实现并行调用：`asyncio.gather` | 并发逻辑 | 1h |
| 11.4 | 实现Token成本控制策略 | diff<50行→只用1个，50-200行→2个，>200行→4个 | 1h |
| 11.5 | 更新Aggregator：多个Reviewer的Issue合并逻辑 | Aggregator更新 | 1h |
| 11.6 | 测试 | 验证4个Reviewer是否给出不同视角 | 1.5h |

**Bug Hunter Prompt 示例：**

```markdown
# 角色：Bug Hunter（缺陷猎人）

你是一位专注于发现代码缺陷的高级审查员。

## 你关注的问题
- 逻辑错误（条件判断错误、循环边界错误）
- 空指针/NoneType错误
- 类型错误
- 资源泄漏（文件未关闭、连接未释放）
- 异常处理不当（bare except、吞掉异常）
- 边界条件未处理（空列表、零值、负数）

## 你不关注的问题
- 代码风格（交给Style Coach）
- 安全漏洞（交给Security Auditor）
- 架构设计（交给Architecture Critic）

## 输出格式
JSON数组，每个元素：
{"type":"bug","severity":"...","title":"...","reason":"...","fix":"..."}
如果没有bug，返回 []
```

**验收标准：**

```
✅ 同一份diff，4个Reviewer返回不同类型的Issue
✅ Bug Hunter不会报风格问题，Security Auditor不会报命名问题
✅ diff < 50行时只调用1个Reviewer（日志中可见）
✅ 4个Reviewer的LLM调用是并行的（总耗时 ≈ 最慢的一个）
✅ 同一行被不同Reviewer报了不同问题 → 两条都保留
✅ 同一行被两个Reviewer报了同一个问题 → 只保留一条
```

---

#### Day 12：RAG知识库（规范检索）

> LLM不再凭空给建议，而是引用具体的编码规范和安全标准。

**核心区别：**

```
没有RAG的LLM：
  "建议使用参数化查询"        ← LLM自己知道的，可能对可能错

有RAG的LLM：
  "根据OWASP A03:2021注入防护规范，建议使用参数化查询。"
  ← 有出处，可验证，面试时能讲清楚
```

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 12.1 | 安装依赖：Chroma + sentence-transformers (BGE-M3) | 依赖就绪 | 0.5h |
| 12.2 | 准备知识库文档（15-20条，自己写） | `knowledge/` 目录 | 2h |
| 12.3 | 实现文档切片 + Embedding + 入库脚本 | `scripts/ingest_knowledge.py` | 1.5h |
| 12.4 | 实现 `KnowledgeRetriever`：输入Issue关键词，返回Top-3相关规范 | `retriever/knowledge_retriever.py` | 1.5h |
| 12.5 | 集成到LLM Reviewer：检索到的规范注入prompt | Reviewer更新 | 1h |
| 12.6 | 测试 | SQL注入场景 → 检索到SQL规范 → LLM引用规范给建议 | 1.5h |

**知识库文档清单（自己写，每条300-500字）：**

```
knowledge/
├── security/
│   ├── owasp_injection.md           # SQL/NoSQL注入防护
│   ├── owasp_broken_auth.md         # 认证漏洞
│   ├── owasp_sensitive_data.md      # 敏感数据泄露
│   ├── owasp_xss.md                # XSS防护
│   ├── command_injection.md         # OS命令注入
│   ├── eval_usage.md               # eval/exec危险用法
│   ├── pickle_security.md          # pickle反序列化风险
│   └── hardcoded_secrets.md        # 硬编码密码/密钥
├── style/
│   ├── pep8_naming.md              # PEP8命名规范
│   ├── pep8_imports.md             # Import规范
│   ├── type_annotations.md         # 类型注解要求
│   ├── docstring_guide.md          # Docstring规范
│   └── function_length.md          # 函数长度建议
└── patterns/
    ├── anti_god_class.md           # 上帝类反模式
    ├── anti_deep_nesting.md        # 深层嵌套反模式
    ├── error_handling.md           # 异常处理最佳实践
    ├── resource_management.md      # 资源管理（with语句）
    └── n_plus_one_query.md         # N+1查询问题
```

**验收标准：**

```
✅ 知识库至少15条文档，涵盖安全、风格、反模式三个类别
✅ 检索"SQL注入" → 返回 owasp_injection.md
✅ 检索"未使用变量" → 返回 pep8相关规范
✅ LLM的review结果中引用了知识库内容
✅ 相似度太低（< 0.5）时，不注入prompt（避免误导LLM）
```

---

#### Day 13：前端完善——统计面板 + 筛选

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 13.1 | 实现评分展示组件：环形进度条 | `ScoreRing.tsx` | 1h |
| 13.2 | 实现Issue统计组件：severity分组柱状图/饼图 | `IssueStats.tsx` | 1.5h |
| 13.3 | 实现Issue筛选：按severity、按type、按文件 | 筛选逻辑 | 1.5h |
| 13.4 | 实现搜索功能：Issue列表中搜索关键词 | 搜索框 | 0.5h |
| 13.5 | 实现Review详情头部：仓库名、Commit、文件数、耗时、Token消耗 | `ReviewHeader.tsx` | 1h |
| 13.6 | 整体样式统一 | CSS调整 | 1.5h |
| 13.7 | 响应式布局（移动端基本可看） | 媒体查询 | 0.5h |

**配色方案：**

```css
:root {
    --critical: #dc2626;   /* 红色 */
    --high: #ea580c;       /* 橙色 */
    --medium: #ca8a04;     /* 黄色 */
    --low: #6b7280;        /* 灰色 */
    --info: #3b82f6;       /* 蓝色 */
}
```

**验收标准：**

```
✅ 页面顶部有评分环形图 + Issue数量统计
✅ Issue列表支持按severity和type筛选
✅ 筛选后Issue数量实时更新
✅ 整体视觉整洁，配色统一
```

---

#### Day 14：Phase 2联调 + 测试 + 单元测试补充

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 14.1 | 准备3-5个真实开源项目的PR作为测试数据 | 测试数据 | 1h |
| 14.2 | 端到端测试：从输入URL到看到完整报告 | 测试记录 | 2h |
| 14.3 | 修复联调中发现的bug | bug fix | 2h |
| 14.4 | 性能测试：大diff（>500行）的处理 | 性能记录 | 1h |
| 14.5 | 边界处理：空diff、非Python文件、LLM返回异常 | 异常处理 | 1h |
| 14.6 | 编写核心模块单元测试（纯函数 + Pipeline编排 + LLM容错） | `tests/` 补充 | 3h |
| 14.7 | 记录Demo素材：截图效果好的review结果（面试用） | 截图 | 1h |

**单元测试范围（新增，不碰LLM网络调用）：**

```
tests/
├── test_pipeline.py          # Pipeline编排：顺序执行、should_run跳过、异常不阻断
├── test_context.py           # ReviewContext实例化、字段赋值
├── test_issue.py             # Issue模型：字段校验、序列化
├── test_git_diff.py          # Git Diff解析：新增/修改/删除文件
├── test_ast_parser.py        # AST解析：函数/类/调用关系提取
├── test_rule_analyzer.py     # Ruff/Bandit输出→Issue映射
├── test_aggregator.py        # 去重逻辑、评分算法、分组
├── test_llm_parser.py        # LLM输出解析容错（mock LLM，测JSON解析逻辑）
└── test_context_analyzer.py  # 函数定位、影响范围计算
```

**测试策略：**

```
纯函数（AST解析、评分算法、行号定位）→ 确定性单测
Pipeline编排 → mock LLM，测"输入context → 输出context"的流转
LLM输出解析 → mock LLM返回各种格式（正常JSON、带markdown包裹、纯文本），测兜底逻辑
```

**测试用PR推荐：**

```
1. tiangolo/fastapi 的任意PR → 展示Style/Architecture审查
2. pallets/flask 的某个修复bug的PR → 展示Bug Hunter
3. 你自己写的项目，手动提交包含以下问题的PR：
   - SQL字符串拼接（安全问题）
   - 未使用变量（Ruff能发现）
   - 函数过长（架构问题）
   - 缺少类型注解（风格问题）
   → 这个是最好的demo，你完全控制测试数据
```

**验收标准：**

```
✅ 至少3个不同项目的PR能成功review
✅ 没有白屏、没有未处理的异常
✅ 大diff不会导致超时或OOM
✅ 手里有3-4个效果好的demo案例（面试演示用）
✅ pytest 通过所有测试，关键路径有覆盖
```

---

#### Phase 2 完成后的能力

```
在Phase 1基础上新增：
✅ 上下文感知：知道修改的函数影响范围
✅ 多角色审查：4个Reviewer从不同角度审查
✅ 知识库增强：审查建议引用编码规范和安全标准
✅ 智能去重：跨工具Issue合并
✅ 专业前端：代码Diff视图 + Issue高亮 + 统计面板
✅ 单元测试覆盖核心路径
```

---

### Phase 3：Agent编排 + 亮点功能 + 部署（Day 15-21）

> **核心目标：将Pipeline升级为支持两种模式的系统，做出差异化亮点。**

---

#### Day 15：ReviewAgent + Investigation Mode（Agent编排核心）

> 这是整个项目最有面试价值的一天。将固定Pipeline升级为支持双模式的Agent系统。

**Review Mode vs Investigation Mode 对比：**

| | Review Mode | Investigation Mode |
|---|---|---|
| 输入 | PR链接 / Commit Hash | 自然语言问题 |
| 路径 | 固定Pipeline | Agent动态决定 |
| 输出 | 结构化Issue报告 | 带代码引用的自然语言回答 |
| 核心价值 | 确定性、可靠、可重复 | 智能、灵活、自主探索 |
| 适用场景 | CI/CD自动审查 | 开发者在代码库中找答案 |

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 15.1 | 实现 `SearchCodeTool`：在代码库中搜索关键词 | `app/tools/search_tool.py` | 1h |
| 15.2 | 实现 `ReviewAgent` 类：路由到两种模式 | `app/agent/review_agent.py` | 1h |
| 15.3 | 实现 Investigation Mode 的 `think()` 方法：LLM决定下一步行动 | `app/agent/investigator.py` | 2.5h |
| 15.4 | 实现 `act()` 方法：调用对应工具 | 同上 | 1h |
| 15.5 | 实现 `observe()` 方法：更新InvestigationContext | 同上 | 1h |
| 15.6 | 实现终止条件：信息足够 / 达到最大步数 | 同上 | 0.5h |
| 15.7 | 实现 `/api/v1/investigate` 接口 | `app/api/routes_review.py` 增强 | 1h |
| 15.8 | 测试 | 输入不同问题，观察Agent是否走不同路径 | 1.5h |

**总预计耗时：10h**

---

**ReviewAgent 核心代码：**

```python
class ReviewAgent:
    def __init__(self, tools: dict):
        self.tools = tools  # Git, AST, Ruff, Bandit, LLM, RAG, Search
        self.pipeline = self._build_pipeline()  # Review Mode用
        self.investigator = CodeInvestigator(tools)  # Investigation Mode用

    def run(self, request):
        if request.mode == "review":
            return self._review_mode(request)
        elif request.mode == "investigate":
            return self._investigate_mode(request)

    def _review_mode(self, request):
        """Pipeline：固定流程，快速可靠"""
        ctx = ReviewContext(request)
        self.pipeline.run(ctx)
        return ctx

    def _investigate_mode(self, request):
        """Agent：动态探索，自主决策"""
        ctx = InvestigationContext(request)
        return self.investigator.run(ctx)
```

**CodeInvestigator（Agent核心）：**

```python
class CodeInvestigator:
    def __init__(self, tools: dict, max_steps: int = 10):
        self.tools = tools
        self.max_steps = max_steps

    def run(self, ctx: InvestigationContext) -> InvestigationContext:
        for step in range(self.max_steps):
            # 1. 思考：LLM根据当前状态决定下一步
            action = self._think(ctx)

            # 2. 终止条件
            if action["tool"] == "finish":
                ctx.answer = action["answer"]
                break

            # 3. 执行：调用工具
            result = self._act(action, ctx)

            # 4. 观察：更新状态，影响下一轮决策
            self._observe(action, result, ctx)

        return ctx

    def _think(self, ctx: InvestigationContext) -> dict:
        """LLM决定下一步做什么"""
        prompt = f"""
你正在调查一个问题：{ctx.question}

已收集的信息：
{chr(10).join(ctx.collected_info[-5:])}  # 最近5条

已访问的文件：{', '.join(ctx.files_visited[-10:])}

可用工具：
- search_code(keyword): 搜索代码关键词
- read_file(path): 读取文件完整内容
- parse_ast(path): 解析代码结构（函数/类/调用关系）
- get_callers(func_name): 查找谁调用了这个函数
- get_callees(func_name): 查找这个函数调用了谁
- search_knowledge(query): 搜索知识库
- finish(answer): 信息足够，输出答案

下一步应该做什么？输出JSON：
{{"tool": "...", "args": {{...}}}}
如果信息足够：
{{"tool": "finish", "answer": "..."}}
"""
        response = self.tools["llm"].chat(prompt)
        return json.loads(response)

    def _act(self, action: dict, ctx: InvestigationContext) -> any:
        """调用工具"""
        tool = self.tools[action["tool"]]
        return tool.execute(**action["args"])

    def _observe(self, action: dict, result: any, ctx: InvestigationContext):
        """更新状态"""
        if action["tool"] == "search_code":
            ctx.collected_info.append(f"搜索 '{action['args']['keyword']}': 找到 {len(result)} 处")
        elif action["tool"] == "read_file":
            ctx.files_visited.append(action["args"]["path"])
            ctx.collected_info.append(f"读取 {action['args']['path']}: {len(result)} 行")
        elif action["tool"] == "parse_ast":
            ctx.collected_info.append(f"AST解析: 发现 {len(result['functions'])} 个函数, {len(result['classes'])} 个类")
        # ... 其他工具的observe逻辑
```

**Investigation Mode 接口设计：**

```python
# POST /api/v1/investigate
{
    "repo_url": "https://github.com/user/project",
    "question": "这个项目的认证流程是怎么实现的？"
}

# Response
{
    "answer": "这个项目的认证流程如下：...",
    "steps_taken": [
        {"step": 1, "action": "search_code('auth')", "result": "找到5个相关文件"},
        {"step": 2, "action": "read_file('auth/login.py')", "result": "读取45行"},
        {"step": 3, "action": "parse_ast('auth/login.py')", "result": "发现login, authenticate函数"},
        {"step": 4, "action": "get_callers('authenticate')", "result": "被login和middleware调用"},
        {"step": 5, "action": "finish(...)", "result": "生成答案"}
    ],
    "files_visited": ["auth/login.py", "auth/middleware.py", "auth/token.py"],
    "tokens_used": 3200
}
```

**验收标准：**

```
✅ 输入"这个项目的认证流程是怎么实现的" → Agent追踪auth相关文件并输出完整回答
✅ 输入"改了 base_model 会影响哪些模块" → Agent分析依赖关系并列出影响范围
✅ 输入"这个函数为什么慢" → Agent读代码、查数据库调用、分析瓶颈
✅ 不同问题走完全不同的路径（演示时可以展示）
✅ Agent不会无限循环，超过max_steps自动终止并输出已收集的信息
✅ 每一步的think-act-observe有日志输出（调试和演示用）
```

---

#### Day 16：Review Strategy（智能审查策略）

> 在Pipeline的每个步骤上实现 `should_run` 条件判断。

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 16.1 | 实现文件类型路由：覆盖各Step的 `should_run` | 各Analyzer增强 | 1.5h |
| 16.2 | 实现diff规模判断：小diff跳过多Reviewer | `should_run` 逻辑 | 1h |
| 16.3 | 实现分块审查：大diff按文件拆分 | Pipeline增强 | 2h |
| 16.4 | 实现策略日志 | 日志输出 | 0.5h |
| 16.5 | 实现策略可视化：报告底部展示执行了哪些Analyzer | Report增强 | 1h |
| 16.6 | 测试 | 修改README → 不调LLM；修改核心模块 → 全量 | 1h |

**策略设计：**

```
1. 文件类型判断
   ├── .py         → 完整Pipeline（AST + Rule + LLM + RAG）
   ├── .js/.ts     → 简化Pipeline（Rule + LLM，不做AST）
   ├── .md/.txt    → 不做Review
   ├── requirements.txt → 只做安全检查
   ├── Dockerfile  → 只做安全检查
   └── 其他        → 跳过

2. Diff规模判断
   ├── < 30行     → 单Reviewer（不调多角色）
   ├── 30-200行   → 标准Pipeline
   └── > 200行    → 分块审查（每文件独立，避免超token）

3. 文件重要度
   ├── __init__.py / config.py → 提升severity
   ├── test_*.py               → 降低severity
   └── 被大量import的模块       → 提升severity
```

**验收标准：**

```
✅ 修改README.md → 报告显示"跳过：非代码文件"
✅ 修改requirements.txt → 只运行安全检查，不调LLM
✅ 修改20行代码 → 只调1个Reviewer
✅ 修改200行代码 → 调4个Reviewer
✅ 报告底部显示策略日志："[AST: ✅, Rule: ✅, LLM: ✅, RAG: ✅, Multi-Reviewer: ✅]"
```

---

#### Day 17：GitHub PR集成

> Review结果直接评论到GitHub PR上，模拟企业CI流程。

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 17.1 | 封装GitHub API客户端（requests 或 PyGithub） | `tools/github_tool.py` | 1.5h |
| 17.2 | 实现获取PR信息：`GET /repos/{owner}/{repo}/pulls/{number}` | API封装 | 1h |
| 17.3 | 实现PR行级评论：`POST .../pulls/{number}/comments` | API封装 | 1.5h |
| 17.4 | 实现Review Summary评论：`POST .../issues/{number}/comments` | API封装 | 0.5h |
| 17.5 | 实现API路由：`POST /api/v1/review/pr` | `api/routes_review.py` 增强 | 1h |
| 17.6 | 实现行级精确定位：Issue行号 → GitHub diff position映射 | 位置映射 | 1.5h |
| 17.7 | 测试：自己的GitHub仓库创建PR → Review → 验证评论出现 | 测试 | 1h |

**验收标准：**

```
✅ POST /api/v1/review/pr → 自动review → 结果评论到GitHub PR
✅ Summary评论包含评分、Issue数量统计
✅ Critical/High Issue作为inline comment附在对应代码行
✅ Medium/Low Issue汇总在Summary中（避免评论太多打扰开发者）
✅ 需要GitHub Token（环境变量），不存在时返回友好错误
```

---

#### Day 18：Metrics统计 + SQLite存储

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 18.1 | 设计数据库表结构（review表、issue表） | `models/db.py` | 1h |
| 18.2 | 实现Review结果存储：每次Review完成后自动入库 | `pipeline/storage.py` | 1.5h |
| 18.3 | 实现历史Review查询接口：`GET /api/v1/reviews`（分页+筛选） | `api/routes_history.py` | 1h |
| 18.4 | 实现Review详情接口：`GET /api/v1/reviews/{id}` | 同上 | 0.5h |
| 18.5 | 实现Metrics统计接口：`GET /api/v1/metrics`（平均评分、常见Issue类型等） | 同上 | 1.5h |
| 18.6 | 前端对接：历史Review列表页面 | `HistoryPage.tsx` | 1.5h |
| 18.7 | 前端对接：统计图表页面 | `MetricsPage.tsx` | 1.5h |

**数据库表结构：**

```sql
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,               -- "review" or "investigate"
    repo_url TEXT NOT NULL,
    commit_hash TEXT,
    pr_number INTEGER,
    question TEXT,                     -- Investigation Mode的问题
    score INTEGER,
    issue_count INTEGER,
    critical_count INTEGER,
    high_count INTEGER,
    medium_count INTEGER,
    low_count INTEGER,
    strategy TEXT,                    -- JSON: 执行了哪些Analyzer
    token_usage INTEGER,
    duration_seconds REAL,
    summary TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE issues (
    id INTEGER PRIMARY KEY,
    review_id INTEGER REFERENCES reviews(id),
    type TEXT,
    severity TEXT,
    file_path TEXT,
    line_number INTEGER,
    title TEXT,
    reason TEXT,
    fix TEXT,
    source TEXT,                      -- JSON: ["ruff", "llm"]
    references TEXT                   -- JSON: RAG检索到的规范
);
```

**验收标准：**

```
✅ 每次Review完成后数据自动存入SQLite
✅ 历史Review列表支持分页查询
✅ 前端历史页面可查看记录
✅ 统计页面显示：平均评分趋势、最常见Issue类型、总Token消耗
```

---

#### Day 19：Docker部署

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 19.1 | 编写后端Dockerfile | `docker/Dockerfile.backend` | 1h |
| 19.2 | 编写前端Dockerfile（Node构建 + Nginx serve） | `docker/Dockerfile.frontend` | 1h |
| 19.3 | 编写docker-compose.yml（backend + frontend + chroma） | `docker-compose.yml` | 1.5h |
| 19.4 | 管理环境变量：`.env.example` 列出所有需要配置的变量 | `.env.example` | 0.5h |
| 19.5 | 编写README.md：项目介绍、架构图、安装步骤、功能截图、技术栈 | `README.md` | 2h |
| 19.6 | 本地测试：`docker-compose up` → 验证全流程 | 端到端验证 | 1h |

**docker-compose.yml 结构：**

```yaml
version: "3.8"

services:
  backend:
    build:
      context: .
      dockerfile: docker/Dockerfile.backend
    ports:
      - "8000:8000"
    environment:
      - LLM_API_KEY=${LLM_API_KEY}
      - GITHUB_TOKEN=${GITHUB_TOKEN}
    volumes:
      - ./data:/app/data
      - ./knowledge:/app/knowledge
    depends_on:
      - chroma

  frontend:
    build:
      context: ./frontend
      dockerfile: ../docker/Dockerfile.frontend
    ports:
      - "3000:80"
    depends_on:
      - backend

  chroma:
    image: chromadb/chroma:latest
    ports:
      - "8500:8000"
    volumes:
      - ./data/chroma:/chroma/chroma
```

**README.md 框架：**

```markdown
# AI Code Review Platform

> 基于LLM的智能代码审查平台，支持两种模式：
> Review Mode（Pipeline自动审查）和 Investigation Mode（Agent自主探索）。

## 架构图
[插入架构图]

## 功能特性
### Review Mode
- Git Diff增量审查
- AST代码结构分析
- 静态规则检查（Ruff + Bandit）
- 多角色AI审查（Bug/Security/Style/Architecture）
- RAG知识库增强（OWASP/编码规范）
- 智能审查策略（按需审查）

### Investigation Mode
- 自然语言输入，Agent自主探索代码库
- 多步推理：搜索→读取→分析→追踪→回答
- 带代码引用的结构化回答

### 通用
- GitHub PR自动评论
- 审查统计与历史
- Docker一键部署

## 快速开始
git clone ... && cd ... && cp .env.example .env && docker-compose up

## 技术栈
[插入技术栈表格]

## 截图
[插入截图]
```

**验收标准：**

```
✅ docker-compose up → 3个服务全部启动无报错
✅ 浏览器访问localhost:3000 → 功能正常
✅ README包含：项目简介、架构图、功能列表、安装步骤、截图
```

---

#### Day 20：质量打磨 + 边界处理

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 20.1 | LLM输出解析容错：JSON解析失败时正则提取；LLM返回空时返回空列表 | 错误处理 | 1.5h |
| 20.2 | 大仓库处理：`git clone --depth 1` 浅克隆 + 超时控制 | 性能优化 | 1h |
| 20.3 | 大diff处理：超过500行按文件分批审查 | 性能优化 | 1h |
| 20.4 | Prompt精调：根据前两周测试反馈优化prompt | prompt迭代 | 2h |
| 20.5 | Investigation Mode的think prompt精调：减少Agent走弯路 | prompt迭代 | 1.5h |
| 20.6 | 错误信息友好化 | 错误处理 | 1h |
| 20.7 | 代码质量自查 | 代码整理 | 1h |

**关键容错示例：**

```python
# LLM返回非JSON时的兜底
def parse_llm_output(raw: str) -> list[dict]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []

# Investigation Mode的think返回非JSON时的兜底
def parse_think_output(raw: str) -> dict:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 尝试从文本中提取工具名和参数
        if "finish" in raw.lower():
            return {"tool": "finish", "answer": raw}
        if "search" in raw.lower():
            keyword = re.search(r'search[_\s]?(?:code)?[\(""\s]+(\w+)', raw)
            if keyword:
                return {"tool": "search_code", "args": {"keyword": keyword.group(1)}}
        # 降级：让Agent读取最相关的文件
        return {"tool": "search_code", "args": {"keyword": "main"}}
```

**验收标准：**

```
✅ LLM返回非标准JSON时不崩溃，有兜底处理
✅ Investigation Mode的think输出非JSON时，Agent不会卡死
✅ clone超过100MB的仓库时60秒内超时
✅ 所有API错误返回统一格式：{"error": "xxx", "detail": "xxx"}
```

---

#### Day 21：演示数据准备 + Demo录制 + 最终打磨

| # | 任务 | 产出物 | 预计耗时 |
|---|---|---|---|
| 21.1 | 精选3个Review Mode Demo PR | demo数据 | 1h |
| 21.2 | 精选3个Investigation Mode Demo问题 | demo数据 | 0.5h |
| 21.3 | 录制Demo视频：Review Mode（2分钟）+ Investigation Mode（2分钟） | demo视频 | 2h |
| 21.4 | 截图各页面 | 截图素材 | 1h |
| 21.5 | 最终全流程测试 | 最终验证 | 1.5h |
| 21.6 | 修复遗留bug + 代码整理 | bug fix | 1.5h |
| 21.7 | Git整理 + push到GitHub | 仓库整理 | 0.5h |

**Demo准备：**

```
Review Mode Demo（3个PR）：
  Demo 1：安全问题PR → 展示Security Auditor + RAG引用OWASP
  Demo 2：代码质量PR → 展示Bug Hunter + Style Coach + Rule Analyzer
  Demo 3：架构问题PR → 展示Architecture Critic + 影响范围分析

Investigation Mode Demo（3个问题）：
  Demo 1："这个项目的认证流程是怎么实现的？" → 展示多步探索
  Demo 2："改了 base_model.py 会影响哪些模块？" → 展示依赖追踪
  Demo 3："哪个函数最容易出安全问题？" → 展示代码库分析
```

---

### Phase 3 完成后的能力

```
在Phase 2基础上新增：
✅ Review Mode：Pipeline + 智能策略，按需审查
✅ Investigation Mode：Agent自主探索，多步推理
✅ GitHub PR自动评论
✅ 历史记录与统计
✅ Docker一键部署
✅ 单元测试覆盖
```

---

## 六、收尾：简历与面试准备（Day 22-23）

### Day 22：简历项目描述撰写

**项目名称：** AI Code Review Platform

**一句话：**

> 基于LLM的智能代码审查平台，支持Review Mode（Pipeline自动审查）和Investigation Mode（Agent自主探索）双模式。

**简历描述模板：**

```
AI Code Review Platform                          2025.07
个人项目

项目描述：
  基于LLM的智能代码审查平台，支持两种模式：
  Review Mode模拟企业Code Review全流程，
  Investigation Mode通过Agent自主探索代码库回答开发问题。

技术栈：
  FastAPI · Tree-sitter · Ruff · Bandit · DeepSeek API ·
  Chroma · BGE-M3 · React · TypeScript · Docker

核心工作：
  · 设计双模式架构：Review Mode用Pipeline做确定性审查，
    Investigation Mode用Agent做自主探索
  · 基于Tree-sitter实现Python AST解析，提取函数/类/调用关系，
    为LLM提供结构化上下文
  · 集成Ruff/Bandit做确定性检查，LLM做语义分析，
    实现"确定性优先、语义补充"的分层审查策略
  · 实现多角色Reviewer（Bug/Security/Style/Architecture），
    通过不同Prompt实现多视角审查
  · 基于RAG知识库（OWASP/编码规范）增强LLM审查准确性
  · Agent模式实现think-act-observe循环，
    自主决定每一步探索方向，支持多步推理
  · 集成GitHub PR API，实现自动Review并行级评论
  · 使用Docker Compose一键部署
```

---

### Day 23：面试准备

**面试核心问题及回答要点：**

| 面试官可能问 | 回答要点 |
|---|---|
| 为什么不用全量Review而用Diff？ | 效率 + 噪声控制 + 企业实践 |
| Tree-sitter相比ast模块有什么优势？ | 多语言 + 性能 + 错误容错 |
| LLM幻觉怎么控制？ | 确定性工具（Ruff/Bandit）先行，LLM只做语义补充；RAG提供规范约束 |
| 多Reviewer的Issue冲突怎么处理？ | Aggregator按file+line+type去重，source标注多个来源 |
| 两种模式的区别？ | Review Mode是Pipeline——固定流程、确定性、适合CI/CD。Investigation Mode是Agent——LLM决定每一步、动态路径、适合开发者探索 |
| Agent和Pipeline的本质区别？ | Pipeline的下一步是代码写死的，Agent的下一步是LLM根据当前状态决定的 |
| Agent不会走错路吗？ | 会。所以设了max_steps上限，超时自动终止并输出已收集的信息。同时think prompt中限制了可选工具范围，减少走弯路的概率 |
| RAG在这个项目中起什么作用？ | Review Mode中：规范增强，让LLM引用OWASP给出有出处的建议。Investigation Mode中：供Agent检索知识库辅助回答 |
| 怎么控制Token成本？ | Review Strategy根据diff规模决定Reviewer数量；Investigation Mode设max_steps上限；小diff跳过LLM |
| Pipeline架构的好处？ | 新增分析器只需实现PipelineStep接口，零成本扩展 |

**讲述框架：**

```
第一层（30秒）：
"这是一个AI代码审查平台，支持两种模式：
 Review Mode自动审查PR，Investigation Mode用Agent探索代码库。"

第二层（2分钟）：
"Review Mode用Pipeline架构，Ruff/Bandit做确定性检查，
 LLM做语义分析，4个Reviewer从不同角度审查。
 Investigation Mode用Agent架构，LLM自主决定每一步做什么——
 搜索代码、读文件、解析AST、追踪调用链，
 最后给出带代码引用的回答。
 两种模式共享同一套工具层，区别只在编排逻辑。"

第三层（5分钟，面试官感兴趣时深入）：
"设计上有几个关键决策：
 1. 统一Issue模型——所有模块输出同一种数据结构，聚合零成本
 2. 确定性优先——能用工具解决的不走LLM，降低幻觉和成本
 3. Pipeline的should_run接口——Phase 1预留，Phase 3加策略时零重构
 4. Agent的think-act-observe循环——每步结果影响下步决策，
    不是固定的if-else，而是LLM动态规划
 5. 两种模式共享工具层——写一次工具，两种编排方式都能用"
```

---

## 七、风险管控

| 风险 | 概率 | 影响 | 应对 |
|---|---|---|---|
| Tree-sitter调试超时 | 高 | Day 3拖到Day 4 | Day 3只做函数+类提取，调用关系放Day 8 |
| LLM输出格式不稳定 | 中 | Day 5解析出错 | Prompt严格约束JSON schema + 正则兜底 |
| RAG模型下载慢 | 低 | Day 12卡住 | Day 10晚上提前下载BGE-M3 |
| CodeMirror集成困难 | 中 | Day 10卡住 | 降级为 `<pre>` + 行号 + CSS高亮 |
| 前端工作量超预期 | 高 | Day 7/13拖进度 | 前端只做核心功能，样式用Tailwind默认值 |
| Investigation Mode的LLM走弯路 | 中 | Agent输出质量差 | 设max_steps上限 + 限制可选工具 + prompt精调 |
| GitHub API限流 | 低 | Day 17测试失败 | 用个人Token，限流时等待重试 |
| 整体进度落后 | 中 | 收尾时间不够 | Day 17（GitHub PR）优先级最低，可砍掉 |

---

## 八、总体统计

| Phase | 时间 | 任务数 | 预计工时 | 核心交付 |
|---|---|---|---|---|
| Phase 1 | Day 1-7 | 42项 | ~57h | 端到端可跑的Review Pipeline + 前端MVP |
| Phase 2 | Day 8-14 | 40项 | ~56h | 上下文分析 + 多角色 + RAG + Diff视图 + 单元测试 |
| Phase 3 | Day 15-21 | 44项 | ~58h | Agent编排 + 策略 + GitHub PR + 存储 + Docker |
| 收尾 | Day 22-23 | 简历+面试 | ~12h | 简历写入 + 面试准备 |
| **总计** | **23天** | **~126项** | **~183h** | **双模式AI Code Review Platform** |

---

## 九、V1最终交付物清单

```
ai-code-review/
├── app/
│   ├── main.py
│   ├── api/
│   │   ├── routes_review.py            # Review + Investigate API
│   │   └── routes_history.py           # 历史查询 + Metrics API
│   ├── core/
│   │   ├── pipeline_step.py            # PipelineStep基类（带should_run）
│   │   └── pipeline.py                 # Pipeline执行器
│   ├── models/
│   │   ├── context.py                  # ReviewContext + InvestigationContext
│   │   ├── issue.py                    # Issue统一模型
│   │   ├── api.py                      # 请求/响应模型
│   │   └── db.py                       # 数据库表定义
│   ├── analyzers/
│   │   ├── git_diff_analyzer.py
│   │   ├── ast_analyzer.py
│   │   ├── rule_analyzer.py
│   │   └── context_analyzer.py
│   ├── reviewers/
│   │   ├── llm_reviewer.py
│   │   └── multi_reviewer.py
│   ├── agent/
│   │   ├── review_agent.py             # 双模式路由
│   │   └── investigator.py             # Investigation Mode Agent
│   ├── retriever/
│   │   ├── knowledge_retriever.py
│   │   └── chunk_loader.py
│   ├── tools/
│   │   ├── git_tool.py
│   │   ├── llm_tool.py
│   │   ├── github_tool.py
│   │   └── search_tool.py              # 代码搜索（Investigation Mode用）
│   ├── pipeline/
│   │   ├── aggregator.py
│   │   ├── strategy.py
│   │   └── storage.py
│   └── report/
│       ├── markdown_report.py
│       ├── html_report.py
│       ├── json_report.py
│       └── summary_generator.py
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── HomePage.tsx            # 首页（含模式选择）
│   │   │   ├── ReviewPage.tsx          # Review Mode结果页
│   │   │   ├── InvestigatePage.tsx     # Investigation Mode结果页
│   │   │   ├── HistoryPage.tsx
│   │   │   └── MetricsPage.tsx
│   │   ├── components/
│   │   │   ├── CodeDiffView.tsx
│   │   │   ├── FileTree.tsx
│   │   │   ├── IssueList.tsx
│   │   │   ├── ScoreRing.tsx
│   │   │   ├── IssueStats.tsx
│   │   │   └── StepTimeline.tsx        # Investigation Mode的步骤可视化
│   │   └── api/
│   │       └── review.ts
│   └── package.json
├── knowledge/
│   ├── security/
│   ├── style/
│   └── patterns/
├── configs/
│   ├── settings.py
│   └── prompts/
│       ├── system_review.md
│       ├── investigate.md              # Investigation Mode的think prompt
│       └── reviewers/
│           ├── bug_hunter.md
│           ├── security_auditor.md
│           ├── style_coach.md
│           └── architecture_critic.md
├── scripts/
│   └── ingest_knowledge.py
├── tests/
│   ├── test_pipeline.py
│   ├── test_context.py
│   ├── test_issue.py
│   ├── test_git_diff.py
│   ├── test_ast_parser.py
│   ├── test_rule_analyzer.py
│   ├── test_context_analyzer.py
│   ├── test_aggregator.py
│   ├── test_llm_parser.py
│   └── test_knowledge_retriever.py
├── docker/
│   ├── Dockerfile.backend
│   └── Dockerfile.frontend
├── docker-compose.yml
├── .env.example
├── requirements.txt
├── README.md
└── LICENSE
```

---

## 十、V1系统能力总览

### Review Mode（Pipeline）

| 能力 | 来自模块 | 确定性/LLM |
|---|---|---|
| Git Diff获取与解析 | GitDiffAnalyzer | 确定性 |
| 代码结构提取（函数/类/调用） | ASTAnalyzer (Tree-sitter) | 确定性 |
| PEP8/代码规范检查 | RuleAnalyzer (Ruff) | 确定性 |
| 安全漏洞扫描 | RuleAnalyzer (Bandit) | 确定性 |
| 影响范围分析 | ContextAnalyzer | 确定性 |
| 语义审查（Bug/逻辑/设计） | MultiReviewer (LLM) | LLM |
| 知识增强（引用规范） | KnowledgeRetriever (RAG) | 混合 |
| 智能策略（按需审查） | Strategy (should_run) | 确定性 |
| Issue聚合去重评分 | Aggregator | 确定性 |
| 报告生成 | ReportGenerator | 确定性 |
| GitHub PR自动评论 | GitHubTool | 确定性 |
| 历史记录与统计 | Storage + Metrics | 确定性 |

### Investigation Mode（Agent）

| 能力 | 来自模块 | 说明 |
|---|---|---|
| 自主探索 | CodeInvestigator | LLM决定每一步做什么 |
| 代码搜索 | SearchTool | 关键词搜索代码库 |
| 文件阅读 | ReadTool | 读取文件内容 |
| AST分析 | ASTTool | 解析代码结构 |
| 调用链追踪 | ASTTool (get_callers) | 追踪函数调用关系 |
| 知识库查询 | KnowledgeRetriever | 检索编码规范 |
| 多步推理 | think-act-observe | 每步结果影响下步决策 |
| 终止判断 | LLM / max_steps | 信息足够或超时自动停止 |

### 通用

| 能力 | 来自模块 |
|---|---|
| 可视化前端 | React + TypeScript |
| 一键部署 | Docker Compose |
| 单元测试 | pytest |

---

**如果完整做下来，这个项目的简历含金量会超过大多数候选人展示的AI项目。因为它同时展示了Pipeline设计能力和Agent设计能力，共享同一套工具层，体现了架构判断力。**

**先让它跑起来，再让它变漂亮。Day 14之前，确保Review Mode端到端能跑是底线。**