# 文件索引 INDEX

全项目文件索引，逐文件概括结构与作用。新建/修改/删除文件时须同步维护本文件（见 `CLAUDE.md` 规范一）。

> 架构说明：项目正按 `_PLAN/AI Code Review Platform — V1 完整任务计划书.md` 迁移到全新 `app/` 结构
> （FastAPI 后端 + 自研 Pipeline + React 前端，双模式：Review / Investigation）。
> 当前处于**结构迁移阶段**：已把旧单文件 CLI 的可复用部分搬入 `app/`，其余骨架目录待后续按计划补齐。

## app/ — 应用包

### `app/tools/git_tool.py`
阶段二 M1 —— GitTool，实现 `Tool` 协议，将 git diff 输出为结构化 `ChangeSet` + Evidence。
- `class GitTool` — parse `git diff --name-status`/`--numstat`/`--unified`，产出含 Hunks 的 ChangeSet 与逐文件 Evidence。
- `_parse_hunks()` — 从 unified diff 提取 `@@` hunk 行号映射。

### `app/tools/ast_tool.py`
阶段二 M1 —— PythonParserTool，基于内置 `ast` 模块提取符号。
- `class ASTTool` — 输入 (path, source) 列表，walk AST 提取函数/类/导入及调用边。
- `class _SymbolVisitor` — ast.NodeVisitor，产出 Symbol 列表。

### `app/tools/ruff_tool.py`
阶段二 M1 —— RuffTool，对指定文件跑 `ruff check --output-format json`，输出标准化 Finding + Evidence。每个 Finding 带 rule_id、位置与 evidence_ids。

### `app/tools/bandit_tool.py`
阶段二 M1 —— BanditTool，对指定文件跑 `bandit -f json`，输出安全扫描 Finding + Evidence。严重度映射 LOW/MEDIUM/HIGH → low/medium/high。

### `app/tools/dependency_tool.py`
阶段六 M5 补完 —— DependencyTool，分析 Python import 变更与依赖清单文件变更。
- `class DependencyTool` — 实现 Tool 协议，输入 (files, changed_files)，输出 EXTERNAL_IMPORT + DEP_FILE_CHANGED Finding + Evidence。
- `_extract_imports(source)` — 从 Python 源码提取 import/from/from_relative 语句，过滤标准库。
- `_STDLIB` — Python 3.10+ 标准库模块名集合。
- `_DEP_FILES` — 依赖清单文件名集合（requirements.txt/setup.py/pyproject.toml 等）。

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

### `app/core/workspace.py`
阶段二 M1 —— 受控工作区管理器。为一次审查创建隔离的 git archive 快照，施加安全约束。
- `class WorkspaceConfig` — 工作区约束：allowed_extensions/max_file_bytes/max_files。
- `class Workspace` — 就绪的工作区实例：list_files()/read_file()/cleanup()；read_file 内含路径越界检查。
- `class WorkspaceManager` — prepare(repo_path, head_ref) 创建 Workspace；_export_snapshot 用 git archive 导出快照。

### `app/pipeline/plan_builder.py`
阶段三 M2 — 规则式计划生成器。基于变更特征（语言/文件类型/diff规模/风险信号）确定性选择工具。
- `class RuleBasedPlanBuilder` — `build(change_set, file_contents?)` 输出 `ReviewPlan`。
- `_RISK_PATTERNS` — 风险关键词→reason_code 映射（auth/sql/command/deserialization/dependency）。
- 内置最低安全策略：高风险信号时 bandit 不可跳过；依赖清单文件变更时自动加 dependency 分析。

### `app/pipeline/executor.py`
阶段三 M2 — 按 ReviewPlan 执行工具链，记录 trace，失败降级。
- `class ReviewExecutor` — `execute(repo_path, base_ref, head_ref, plan)` 返回 `ExecutionResult`。
- `_TOOL_REGISTRY` — 工具名→实例映射；`_run_step()` 上下文中记录 TraceEntry。

### `app/pipeline/aggregator.py`
阶段三 M2 — 确定性聚合器，按 (file, rule_id) 分组去重 Findings → Issues。
- `class Aggregator` — `aggregate(findings, evidence)` 产出去重 Issue 列表；同组合并 message 与 evidence_ids。

### `app/pipeline/report.py`
阶段三 M2 — 确定性报告生成器（Markdown + JSON）。
- `class ReportGenerator` — `markdown(...)` 生成结构化 Markdown 报告；`json_report(...)` 生成 JSON。

### `app/pipeline/review_pipeline.py`
阶段三 M2 — 完整审查管道：PlanBuilder → Executor → Aggregator → Report。
阶段四 M3 扩展：可选 `llm_reviewer` 注入，在静态工具之后运行 LLM 语义审查。
- `class ReviewPipeline` — `run(repo_path, base_ref, head_ref)` 一行调用完成端到端审查。
- `class ReviewOutput` — plan/change_set/symbol_index/issues/evidence/trace/markdown/json。

### `app/pipeline/knowledge_retriever.py`
阶段四 M3 — 可插拔知识检索器。
- `class KnowledgeRetriever(Protocol)` — 可插拔协议：`retrieve(query, top_k)` 返回知识条目列表（content/source/version/license）。
- `class NullRetriever` — 空检索器（默认降级策略）。
- `class StaticKnowledge` — 内置静态知识条目（7 条，OWASP/PEP8/Google Style Guide），关键词匹配检索。

### `app/pipeline/llm_reviewer.py`
阶段四 M3 — LLM 驱动的语义审查器，只把静态工具无法判定的问题交给 LLM。
- `class LLMReviewer` — `review(file, diff, symbols, static_findings, evidence)` 返回 (findings, evidence)。
- 结构化 prompt + JSON schema 校验 + 低置信度降级 + 重试 + 失败证据记录。
- `call_llm` 通过依赖注入，不绑定特定 LLM SDK。

### `app/pipeline/eval_dataset.py`
阶段五 M4 — 评测数据集，10 条手工标注样本（s001—s010）。
- `class EvalSample` — id/scenario/input(change_summary/file_types/diff_size/risk_signals/ast_summary/static_findings_count)/ground_truth(analyzers/risk_level/reason_codes)。
- `load_samples()` — 返回 `list[EvalSample]`。
- `to_json(samples)` — 序列化为 JSON。

### `app/pipeline/eval_metrics.py`
阶段五 M4 — 评测指标计算。
- `class EvalMetrics` — 聚合指标：analyzer precision/recall/F1、risk_level_accuracy、high_risk_recall、reason precision/recall/F1。
- `compute(predictions, ground_truths)` — 计算指标，含逐样本明细。
- `_set_precision/_set_recall/_set_f1` — 集合级指标工具函数。

### `app/pipeline/eval_benchmark.py`
阶段五 M4 — 评测基准脚本（LLM Planner vs 规则基线对比）。
- `run_llm_planner(samples)` — 调用 `llm_tool.chat()` 为每条样本生成 ReviewPlan 预测。
- `run_rule_baseline(samples)` — 使用 `RuleBasedPlanBuilder` 生成基线预测。
- `run_benchmark(top_n)` — 完整评测流程，输出对比报告。
- `class BenchmarkResult` — 评测结果容器，`summary()` 生成 Markdown 对比报告。
- `__main__` CLI — `python -m app.pipeline.eval_benchmark --top 3 --json`。

### 骨架目录（待按计划书补齐）
`app/analyzers/`（更多 Analyzer，目前仅含 `__init__.py`）、`app/agent/`（双模式 + Investigation）——尚未实现。

## 服务化

### `app/api/__init__.py`
阶段六 M5 — FastAPI 应用工厂。`create_app()` 创建 FastAPI 实例、注册路由。

### `app/api/schemas.py`
阶段六 M5 — API 请求/响应 Pydantic schema。
- `ReviewRequest` — 提交审查请求（repo_path/base_ref/head_ref）。
- `ReviewResponse` — 审查完整响应（plan/change_set/issues/evidence/trace/markdown/json_report）。
- `RunSummary` / `RunListResponse` — 历史运行列表。
- `ErrorDetail` / `ErrorResponse` — 统一错误格式。

### `app/api/routes.py`
阶段六 M5 — API 路由。
- `POST /review` — 提交代码审查，运行 Pipeline，持久化结果。
- `GET /review/{run_id}` — 查询审查结果（404 当不存在）。
- `GET /runs` — 列出所有历史运行。
- `GET /health` — 健康检查。
- 统一异常处理：HTTPException → JSON `{"error": {"code": "...", "message": "..."}}`。

### `app/pipeline/observability.py`
阶段六 M5.1 — Pipeline 可观测性：性能分解与结构化 Timeline。
- `class PipelineTimeline` — 逐阶段耗时/状态/产出计数；`success_count`/`failure_count`/`bottleneck`。
- `class StageMetric` — 单阶段度量（stage/duration_ms/status/finding_count/evidence_count）。
- `build_timeline(run_id, plan, trace, tool_results, total_duration_ms)` — 从原始数据构建 Timeline。
- `PipelineTimeline.ascii_bar(width)` — ASCII 柱状图（面试展示用）。
- `PipelineTimeline.to_dict()` — 序列化。

### `app/persistence/store.py`
阶段六 M5 — JSON 文件持久化存储。
- `class RunStore` — `save(run_id, data)` / `load(run_id)` / `list_runs()` / `delete(run_id)`。
- `class RunRecord` — 运行摘要 dataclass。
- 数据目录：`runs/`（已 gitignore）。

### `app/cli.py`
阶段六 M5 — 命令行入口。
- `python -m app.cli review <repo> --base --head --output --json` — 执行审查并输出报告。
- `python -m app.cli serve --host --port` — 启动 FastAPI 服务 (uvicorn)。

### `Dockerfile`
阶段六 M5 — Docker 镜像（python:3.11-slim + git/ruff/bandit + API 服务）。

### `docker-compose.yml`
阶段六 M5 — 一键启动 API 服务，挂载 `runs/` 数据卷和 `.env` 密钥文件。

### `requirements.txt`
项目依赖清单（fastapi/uvicorn/pydantic/openai/tenacity/python-dotenv/ruff/bandit/chromadb）。

## 数据 / 示例

### `sample_bad.py`
故意植入多类问题的演示文件（可变默认参数、SQL 注入、硬编码密钥、裸 except、命令注入、除零、全局变量等），用作审查系统的测试夹具。

## 测试（共 154 条）

### `tests/test_pipeline.py`（5 条）
Pipeline 骨架单测（纯确定性）：空 Pipeline、顺序执行、should_run 跳过、step 写入 issues、Issue 严重度排序。

### `tests/test_data_contracts.py`（10 条）
阶段一数据契约单测：CodeLocation/Symbol/ChangeSet/FileChange/Hunk/Evidence/Finding/ReviewPlan/Issue 序列化往返。

### `tests/test_tool_contract.py`（5 条）
Tool 统一契约单测：假工具满足 `Tool` 协议、失败返回结构化 Diagnostic 不抛异常、成功结果 `ok()`、序列化往返。

### `tests/test_review_run.py`（6 条）
ReviewRun 单测：完整 run 可追溯、无证据 Issue/Finding 被标记、悬空引用被标记、`resolve_evidence`、序列化往返。

### `tests/test_workspace.py`（5 条，阶段二新增）
WorkspaceManager 单测：prepare/list_files/read_file、路径越界拦截、非 git 仓库报错、cleanup 删除工作目录。

### `tests/test_git_tool.py`（4 条，阶段二新增）
GitTool 单测：diff 两 commit、空 diff（HEAD vs HEAD）、change_type 合法、坏 ref 不抛异常。

### `tests/test_ast_tool.py`（4 条，阶段二新增）
ASTTool 单测：符号提取、语法错误不崩溃、evidence 数匹配文件数、空文件列表。

### `tests/test_static_tools.py`（6 条，阶段二新增）
RuffTool + BanditTool 单测：检测到问题（Ruff：F401/E401；Bandit：B307/B105）、空路径返回空、Finding 带 location/rule_id/evidence_ids。

### `tests/test_fact_collector.py`（4 条，阶段二新增）
端到端集成测试：全工具链成功、空范围无变更、M1 验收（Finding 带 evidence_ids + location）。

### `tests/test_m2_pipeline.py`（12 条，阶段三新增）
M2 固定审查闭环测试：PlanBuilder/Aggregator/ReportGenerator 单测 + ReviewPipeline 集成 + 幂等性 + 空 diff。

### `tests/test_m3_llm.py`（9 条，阶段四新增）
M3 LLM 语义审查测试（mock LLM，不上网）：LLMReviewer 解析/空输出/非法 JSON/缺字段/低置信度降级；NullRetriever/StaticKnowledge；Pipeline 集成（mock LLM 不破坏静态结果、LLM 失败静态结果保留）。

### `tests/test_m4_eval.py`（30 条，阶段五新增）
M4 评测体系测试（mock LLM，不上网）：数据集加载与字段校验、集合指标函数、EvalMetrics 计算（完美匹配/空输入/错配/风险等级准确率/高风险召回）、规则基线（非 Python 只有 git/空变更）、LLM 解析器（合法 JSON/markdown 包裹/非法 JSON）、Benchmark 集成（mock LLM 端到端/JSON 解析失败/异常处理/全量流程与 summary 生成）、Prompt 构造。

### `tests/test_m5_api.py`（18 条，阶段六新增）
M5 服务化测试：Health check、创建 Review 验证返回结构/默认 refs/坏仓库 500、按 run_id 查询/不存在 404、列出历史运行、端到端审查本项目自检测、RunStore 存取往返/不存在返回 None/列出运行/删除/损坏文件跳过、Schema 默认值/显式构造、CLI 模块导入/集成运行。

### `tests/test_pipeline_recovery.py`（10 条，M5.1 容错）
Pipeline 容错测试：Bandit/Ruff/AST/Dependency 单个崩溃→Pipeline 完成、全部静态工具崩溃→git 产出保留、工具返回 failed 状态→Pipeline 继续、report 含失败 trace、git 失败→仍返回输出、未知工具不阻塞已知工具、失败耗时记录。通过替换 `_TOOL_REGISTRY` 注入故障。

### `tests/test_golden.py`（6 条，M5.1 黄金+回归）
黄金结果 + 回归快照：`pytest.mark.golden` — 每条样本 analyzer F1≥0.5、平均 F1≥0.75、高风险样本强制含 bandit、英文关键词风险等级不低估；`pytest.mark.regression` — Pipeline 快照 Issue 数不骤降/Analyzer 不缩小、同一 commit 幂等一致性。

### `tests/test_performance.py`（9 条，M5.1 性能）
性能基准 + Timeline：`pytest.mark.perf` — Timeline 生成/ASCII 柱状图/阶段覆盖 Plan 全部工具；`pytest.mark.slow` — Git ≤5s/Pipeline ≤30s/阶段匹配 Plan；Timeline 序列化/空 Timeline 不崩溃/单测覆盖。

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
