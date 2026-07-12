# 文件索引 INDEX

全项目文件索引，逐文件概括结构与作用。新建/修改/删除文件时须同步维护本文件（见 `CLAUDE.md` 规范一）。

> 架构说明：项目正按 `_PLAN/AI Code Review Platform — V1 完整任务计划书.md` 迁移到全新 `app/` 结构
> （FastAPI 后端 + 自研 Pipeline + React 前端，双模式：Review / Investigation）。
> 当前处于**结构迁移阶段**：已把旧单文件 CLI 的可复用部分搬入 `app/`，其余骨架目录待后续按计划补齐。

## app/ — 应用包

### `app/tools/contract.py`
Tool 统一契约（阶段一 M0）——工具层与编排层之间的唯一接口，工具不依赖 Pipeline/Agent。
- `TOOL_STATUS` — 工具执行状态常量。
- `class ToolRequest` — 调用请求（tool/params/timeout_s）。
- `class ToolResult` — 统一产出（artifacts/evidence/findings/diagnostics/duration_ms）；`ok()`、`failure()` 便捷构造、`to_dict/from_dict`。
- `class Tool(Protocol)` — 工具协议：`name` + `execute(request) -> ToolResult`；失败以 `ToolResult.failure` 表达，不抛业务异常。
- `Diagnostic` 从 `app/models/diagnostic.py` 导入（诊断属领域模型层，工具层只消费，避免模型反向依赖工具层）。

### `app/tools/llm_tool.py`
**⚠️ 旧能力，尚未接入新 Pipeline，非阶段一交付。** 计划书§七拟适配为统一 Tool 契约后复用。
LLM 工具（OpenAI 兼容接口；改 `.env` 即可在智谱 GLM / DeepSeek / Qwen 间切换）。
- `get_client()` — 构造 `OpenAI` 客户端（读 `ZHIPU_API_KEY` / `ZHIPU_API_URL`）。
- `get_model()` — 返回模型名（默认 `glm-4.5-air`）。
- `chat_completion(messages, ...)` — 底层调用，tenacity 指数退避重试（最多 3 次）。
- `chat(prompt, system, ...)` — 单轮对话便捷封装。

### `app/retriever/knowledge_base.py`
**⚠️ 旧能力，尚未接入新 Pipeline，非阶段一交付。** 计划书§七拟适配为可插拔知识检索后复用；现有 Chroma 种子当演示数据。
ChromaDB 知识库管理层。启动设置 HF 镜像（`hf-mirror.com`）；embedding 用 `BAAI/bge-m3`。
- `class KnowledgeBase` — 管理 3 类 collection：`code_standards` / `vuln_patterns` / `review_history`。
  - `__init__(persist_dir)` / `_ensure_collections()` — 初始化并幂等创建/获取 collection。
  - `add_code_standards` / `add_vuln_patterns` / `add_review_record` — 写入。
  - `query(query_text, n_results)` — 跨 collection 检索，返回相关文本列表。

### `app/retriever/kb_seed.py`
**⚠️ 旧能力，尚未接入新 Pipeline，非阶段一交付。** 演示用种子数据。
- `CODE_STANDARDS` — 20 条代码规范（Google Python Style Guide + 通用最佳实践）。
- `VULN_PATTERNS` — 10 条漏洞模式（OWASP Top 10 / CWE）。
- `seed_kb(kb)` — 幂等灌入（通过 `count()==0` 判断）。

### `app/models/diagnostic.py`
结构化诊断（领域模型层）——`ReviewRun` 与 `ToolResult` 共同引用，故置于 models 层避免分层污染。
- `ERROR_CODES` — 统一错误码常量。
- `class Diagnostic` — code/message/severity/tool；`to_dict/from_dict`。

### `app/models/issue.py`
统一问题模型（全系统"通用货币"）。
- `ISSUE_TYPES` / `SEVERITIES` — 合法的类型与严重程度常量。
- `class Issue`（dataclass）— 字段：type/severity/file/line/title/reason/fix/source/references/**id/evidence_ids**（阶段一新增后两项，向后兼容）。
  - `severity_rank()` — 严重度排序权重（critical 最大）。
  - `to_dict()` / `from_dict()` — 序列化往返。

### `app/models/ids.py`
- `new_id(prefix)` — 生成形如 `ev_1a2b3c4d` 的稳定短 id（uuid4 前 8 位），供各可引用对象取身份。

### `app/models/location.py`
代码位置与符号 —— 一切事实的定位基础。
- `SYMBOL_KINDS` — 合法符号种类常量。
- `class CodeLocation` — file/start_line/end_line/symbol；`to_dict/from_dict`。
- `class Symbol` — name/kind/location/parent/calls；`to_dict/from_dict`（重建嵌套 CodeLocation）。

### `app/models/change.py`
变更集模型（阶段二 GitTool 产出，阶段一先定契约）。
- `CHANGE_TYPES` — added/modified/deleted/renamed。
- `class Hunk` — diff 块行号映射。
- `class FileChange` — path/change_type/old_path/added_lines/deleted_lines/hunks。
- `class ChangeSet` — base/head/files；均含 `to_dict/from_dict`（递归重建嵌套）。

### `app/models/evidence.py`
可引用的事实片段 —— 可追溯性的原子。
- `EVIDENCE_KINDS` — code/tool_finding/knowledge/dependency/change。
- `class Evidence` — id(默认 `ev_*`)/kind/source/location/snippet/confidence/reference；`to_dict/from_dict`。

### `app/models/finding.py`
工具/规则的候选发现（介于原始输出与最终 Issue 之间）。
- `class Finding` — id(默认 `fnd_*`)/tool/rule_id/message/severity/location/evidence_ids；复用 `issue.SEVERITIES`；`to_dict/from_dict`。

### `app/models/plan.py`
审查执行计划（字段对齐计划书 M4 微调 Planner 输出 schema）。
- `RISK_LEVELS` — low/medium/high。
- `class ReviewPlan` — analyzers/enable_rag/enable_llm_semantic_review/risk_level/reason_codes/budget_tokens；`to_dict/from_dict`。

### `app/models/run.py`
ReviewRun —— 一次审查运行的完整记录（替代旧 ReviewContext 作运行级容器）。
- `class TraceEntry` — 执行 trace 一步：step/status/duration_ms/detail。
- `class ReviewRun` — id/repo_url/base/head/plan/change_set + 按 id 索引的 evidence/findings + issues + trace/diagnostics/tool_versions/stats。
  - `add_evidence/add_finding/add_issue/record` — 写入。
  - `resolve_evidence(ids)` — 按 id 反查 Evidence（忽略悬空 id）。
  - `validate_traceability()` — 校验每个 Issue 与每个 Finding 都至少关联一条 Evidence 且无悬空引用，返回问题列表。
  - `to_dict/from_dict` — 完整序列化往返。

### `app/models/context.py`
Pipeline / Agent 传递的上下文"公文包"（旧结构，阶段一起逐步被 `ReviewRun` 取代，暂保留兼容迁移路径）。
- `class ReviewContext` — repo_url/commit/diff/ast_data/function_info/knowledge_docs/issues/strategy_log/stats；复杂子类型 Phase 1 用占位类型，各 Analyzer 那天再填。
- `class InvestigationContext` — question/repo_path/collected_info/files_visited/findings/current_hypothesis/step_count/answer（Agent 逻辑 Day 15 实现）。

### `app/core/pipeline_step.py`
- `class PipelineStep(ABC)` — 分析步骤基类。`should_run(context)` 默认 True（前瞻扩展点）；`analyze(context)` 抽象方法，就地修改 context。

### `app/core/pipeline.py`
- `class Pipeline` — 按序执行 steps，`should_run` 为 False 时跳过；每步记入 `strategy_log`。返回被填充的 context。

### 骨架目录（待按计划书补齐）
`app/analyzers/`（Git/AST/Ruff/Bandit）、`app/pipeline/`（Aggregator/Strategy/Storage）、`app/report/`、`app/agent/`（双模式 + Investigation）、`app/api/`（FastAPI 路由）——目前仅含 `__init__.py`，尚未实现。

## 数据 / 示例

### `sample_bad.py`
故意植入多类问题的演示文件（可变默认参数、SQL 注入、硬编码密钥、裸 except、命令注入、除零、全局变量等），用作审查系统的测试夹具。

## 测试

### `tests/test_pipeline.py`
Pipeline 骨架单测（纯确定性，不碰网络）：空 Pipeline、顺序执行、should_run 跳过、step 写入 issues、Issue 严重度排序。

### `tests/test_data_contracts.py`
阶段一数据契约单测：CodeLocation/Symbol/ChangeSet/FileChange/Hunk/Evidence/Finding/ReviewPlan/Issue 的 `to_dict→from_dict` 序列化往返，含空 diff、默认 id、向后兼容构造。

### `tests/test_tool_contract.py`
Tool 统一契约单测：假工具满足 `Tool` 协议、失败返回结构化 Diagnostic 不抛异常、成功结果 `ok()`、ToolResult/ToolRequest 序列化往返。

### `tests/test_review_run.py`
ReviewRun 单测（阶段一验收核心）：完整 run 可追溯、无证据 Issue 被标记、无证据 Finding 被标记、悬空 evidence 引用被标记、`resolve_evidence` 反查、ReviewRun 序列化往返。

## 文档 / 规划

### `CLAUDE.md`
项目编码规范（长期生效）：规范零（输出语言：一律中文）、规范一（文件索引管理）、规范二（修改留痕 CHANGELOG）。

### `docs/INDEX.md`
本文件 — 全项目文件索引。

### `docs/CHANGELOG.md`
修改留痕日志，以工作段为单位，push 时补写。

### `docs/superpowers/specs/`
各阶段设计文档（spec）。当前含 `2026-07-09-day1-core-models-design.md` — Day 1 核心模型与 Pipeline 骨架的设计动机、字段、接口、测试策略与验收标准。

### `docs/stages/`
阶段进度报告目录，逐阶段记录完成能力与新增内容，供最终成品回看每阶段交付。
- `README.md` — 阶段进度总览（阶段/里程碑/状态/报告链接表）。
- `stage-1-data-contract.md` — 阶段一（M0 可追溯数据契约）报告：目标、完成能力、文件清单、设计决策、验收对照、测试结果、已知限制、下一阶段输入。

### `_PLAN/AI Code Review Platform — V1 完整任务计划书.md`
现行主计划书：以确定性事实层、Artifact/Evidence 契约、固定 `ReviewPlan`、可追溯 Review Pipeline 与可评测微调 Planner 为主线；包含 M0—M5 跨步实施顺序、阶段交付/验收、简历指标口径、工具边界、安全约束、评测与后续 Investigation Agent 的演进条件；附录 A 为项目最终流程图（逐层标注用处）。

## 配置 / 其他

- `pytest.ini` — pytest 配置：`testpaths=tests` 把收集范围钉在 `tests/`，避免向上走到 E:\ 根目录导致收集失败；`addopts=-q`。
- `.env` — 环境变量（密钥、API URL、模型名、GITHUB_TOKEN）；已 gitignore。
- `.gitignore` — 忽略 `chroma_db/`、`.env`、`.claude/`、`__pycache__/`、`.idea/`、`*_review_report.md` 等。
- `chroma_db/` — ChromaDB 持久化目录（运行时生成）；已 gitignore。
- `chroma_db.bak.*` — 迁移期间对旧知识库的临时备份（运行时产物，可删）。
