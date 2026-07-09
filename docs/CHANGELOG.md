# 修改日志 CHANGELOG

本项目所有代码改动的留痕记录，用于回溯与查证。维护规则见 `CLAUDE.md` 规范二。

## 记录规则

- **单位**：以「一段工作时长」为一条记录（非每次文件保存，也非每个 git commit）。
- **时机**：在 `git push` 前补写本段工作的记录。
- **每条必含**：日期（绝对日期）、改了什么（涉及文件与改动点，逐一列全）、为什么改、以及必要的「A → B」因果链。
- **铁律**：任何改动都要记录，禁止「为了 A 改了 B 却隐瞒 B」。

---

## 2026-07-09 — 建立文档规范体系（INDEX + CHANGELOG）

**改了什么：**
- 新增 `CLAUDE.md` — 写入两条长期编码规范：规范一（文件索引管理，维护 `docs/INDEX.md`）、规范二（修改留痕，维护本 CHANGELOG）。
- 新增 `docs/INDEX.md` — 全项目文件索引，逐文件概括结构与作用。
- 新增 `docs/CHANGELOG.md` — 本文件，含记录规则与首条记录。

**为什么改：**
- 用户提出两条长期编码要求：(1) 每个文件在创建/修改时有索引管理，概括代码结构与作用，避免日后到处查找；(2) 任何修改都记录在案，杜绝「为 A 改 B 却隐瞒 B」，并将记录持久化为文档集以便回溯查证。

**待记录的既有未提交改动（本段工作开始前已存在，留待相应改动提交时补全归属）：**
- `kb_manager.py`（未 staged）：embedding function 由 ChromaDB 默认切换为 `BAAI/bge-m3`；新增 `HF_ENDPOINT=https://hf-mirror.com` 镜像设置以规避 huggingface.co 连接超时；3 个 collection 的 create/get 均显式传入该 embedding_function。
- `plan.md → _PLAN/plan.md`（已 staged 的重命名，含内容改动）：规划文档被移入 `_PLAN/` 目录。

---

## 2026-07-09 — 行号修复 + Strategy 重构 + 错误处理 + 知识库扩充 + 全面架构迁移

本条一次性归档多段工作，并把上一条「待记录的既有改动」正式纳入本次提交。

**1. 审查行号定位修复（原 `review_graph.py` reviewer 节点）：**
- 新增 `_add_line_numbers()`，审查前给代码注入 `行号 |` 前缀再喂给 LLM，并在 prompt 中要求直接引用该行号。
- 原因：改前 LLM 自行数行导致行号偏移（SQL 注入误报第 9 行、密钥误报第 16 行）。改后 9 类问题行号全部命中实际行号。
- 采纳最小内联注入方案，未实现用户所贴方案中的 map_back/extract/strip（本项目整文件、从第 1 行、输出 JSON，显示行号=原始行号，那些是冗余）。

**2. Strategy 模式重构（新增 `review_strategies.py`）：**
- 将单一 reviewer 拆为 `ReviewStrategy` 基类 + `SecurityReviewer` + `QualityReviewer`，`get_default_strategies()` 提供默认集合；reviewer 节点改为调度多策略并合并为统一 findings JSON。
- 新增 `_extract_json()` 稳健解析（容忍 ```json 包裹/前后杂文本）。
- 因 A（拆分策略）改 B：reviewer 节点签名与内部逻辑随之改写为合并式；下游 `report_generator`/`history_recorder` 消费的 JSON 结构保持不变。
- ⚠️ 已知回归（未修）：QualityReviewer 偶发因 LLM 返回空字符串而降级为空类别，导致 Bug/代码异味漏报；代码原样迁移，留待接入新 Pipeline 时处理。

**3. 错误处理增强：**
- `llm_client.py`：新增带 tenacity 指数退避重试（最多 3 次）的 `chat_completion()`，`chat()` 改为走它。
- `review_strategies.py`：策略 LLM 调用失败/解析失败时降级为空类别，不中断流程。
- 原 `review_graph.py`：`context_analyzer` 检索失败降级为空上下文；`report_generator` 改用 `chat_completion` 并在失败时降级为直接展示原始 findings。
- 原 `review.py`：文件读取加 `OSError/UnicodeDecodeError` 兜底。

**4. 代码规范知识库扩充（`kb_seed.py`）：**
- `CODE_STANDARDS` 由 10 条扩至 20 条（新增 cs_011~cs_020，覆盖字符串/条件/属性/函数长度/注释/导入/缩进/迭代等 Google Style Guide 要点）。
- 注意：`seed_kb` 幂等（`count()==0`），旧 `chroma_db/` 已有数据不会自动纳入新条目；本次验证时将旧库备份为 `chroma_db.bak.*` 后重灌。

**5. 全面架构迁移到 `app/`（依据 `_PLAN/AI Code Review Platform — V1 完整任务计划书.md`）：**
- 用户改用全新计划书（FastAPI + 自研 Pipeline + React 双模式平台），决定「按新架构把可复用部分搬入 app/，先不补齐，无法复用部分删掉」。
- 新建 `app/` 骨架目录（core/models/tools/retriever/reviewers/analyzers/pipeline/report/agent/api/utils）+ `__init__.py`。
- 迁移（含改 import 为 `app.` 绝对导入）：
  - `llm_client.py` → `app/tools/llm_tool.py`（泛化命名，保留重试）
  - `kb_manager.py` → `app/retriever/knowledge_base.py`
  - `kb_seed.py` → `app/retriever/kb_seed.py`
  - `review_strategies.py` → `app/reviewers/multi_reviewer.py`
  - `_add_line_numbers` 抽出为 `app/utils/code_lines.py::add_line_numbers`
- 删除不可复用文件：`main.py`（模板）、`review.py`（CLI 入口）、`review_graph.py`（LangGraph 编排 + ReviewState）、`llm_client.py`（已迁移）。
- 验证：app 包全部可导入，纯逻辑复验通过（行号格式、JSON 容错、种子 20 条、双策略），knowledge_base 重量级导入 OK。

**6. 文档：**
- `_PLAN/AI Code Review Platform — V1 完整任务计划书.md`：用户新增并修订的主计划书（已取代旧 `plan.md`）。
- `docs/INDEX.md`：随迁移整体重写，反映新 `app/` 结构，标注已迁移与待补齐骨架。

**骨架待补齐（本次仅建目录，未实现）：** `app/core`(Pipeline)、`app/models`(Issue/ReviewContext)、`app/analyzers`、`app/pipeline`、`app/report`、`app/agent`、`app/api`。

---

## 2026-07-09 — Phase 1 Day 1：核心数据模型 + Pipeline 骨架

依据 `_PLAN/AI Code Review Platform — V1 完整任务计划书.md` 开始 Phase 1 Day 1，落地上一条「骨架待补齐」中的 `app/models` 与 `app/core`。采用「先写设计文档 → 再实现」的工作方式（用户要求直接看文档）。

**改了什么：**
- 新增 `docs/superpowers/specs/2026-07-09-day1-core-models-design.md` — Day 1 设计文档：说明 Issue / ReviewContext / Pipeline 的设计动机（为什么这样设计）、字段、接口、测试策略、验收标准，以及本次的偏差说明。
- 新增 `app/models/issue.py` — 统一问题模型 `Issue`（dataclass）+ `ISSUE_TYPES` / `SEVERITIES` 常量；`severity_rank()` 排序权重、`to_dict()` 序列化。作为全系统「通用货币」，所有工具输出统一结构。
- 新增 `app/models/context.py` — `ReviewContext`（Review Mode 公文包）与 `InvestigationContext`（Investigation Mode 公文包）；复杂子类型用宽松类型占位，待对应 Analyzer 当天再细化；均用 `field(default_factory=...)` 规避可变默认参数陷阱。
- 新增 `app/core/pipeline_step.py` — `PipelineStep(ABC)` 基类；`should_run()` 默认 True（前瞻扩展点），`analyze(context)` 抽象方法（就地改 context，无返回值）。
- 新增 `app/core/pipeline.py` — `Pipeline` 编排器，按序执行 steps，`should_run` 为 False 时跳过并记入 `strategy_log`。
- 新增 `tests/test_pipeline.py` — 5 条纯确定性单测（不碰网络）：空 Pipeline、顺序执行、should_run 跳过、step 写入 issues、Issue 严重度排序。`python -m pytest tests/ -v` → 5 passed。
- `docs/INDEX.md` — 追加上述新文件条目，并把 `app/core`、`app/models` 从「骨架待补齐」移出。

**为什么改：**
- 新计划书要求 Phase 1 先立地基：统一的 Issue 模型与 Context 让各分析步骤只跟 Context 读写、互不直接调用（解耦、可独立测试），Pipeline 负责编排。这是后续所有 Analyzer（Git/AST/Ruff/Bandit/LLM）接入的前提。

**偏差说明（规范二铁律，如实上报）：**
- 计划书 Day 1 含「FastAPI `/health` 骨架」一项，本次**未实现**，推迟到 Day 5 与其余 API 路由一并搭建。原因：当前无其他 API 端点，单独起 FastAPI 应用价值低且会引入未使用依赖；设计文档已记录此推迟。
