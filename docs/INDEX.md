# 文件索引 INDEX

全项目文件索引，逐文件概括结构与作用。新建/修改/删除文件时须同步维护本文件（见 `CLAUDE.md` 规范一）。

> 架构说明：项目正按 `_PLAN/AI Code Review Platform — V1 完整任务计划书.md` 迁移到全新 `app/` 结构
> （FastAPI 后端 + 自研 Pipeline + React 前端，双模式：Review / Investigation）。
> 当前处于**结构迁移阶段**：已把旧单文件 CLI 的可复用部分搬入 `app/`，其余骨架目录待后续按计划补齐。

## app/ — 应用包

### `app/tools/llm_tool.py`
LLM 工具（OpenAI 兼容接口；改 `.env` 即可在智谱 GLM / DeepSeek / Qwen 间切换）。
- `get_client()` — 构造 `OpenAI` 客户端（读 `ZHIPU_API_KEY` / `ZHIPU_API_URL`）。
- `get_model()` — 返回模型名（默认 `glm-4.5-air`）。
- `chat_completion(messages, ...)` — 底层调用，tenacity 指数退避重试（最多 3 次）。
- `chat(prompt, system, ...)` — 单轮对话便捷封装。

### `app/retriever/knowledge_base.py`
ChromaDB 知识库管理层。启动设置 HF 镜像（`hf-mirror.com`）；embedding 用 `BAAI/bge-m3`。
- `class KnowledgeBase` — 管理 3 类 collection：`code_standards` / `vuln_patterns` / `review_history`。
  - `__init__(persist_dir)` / `_ensure_collections()` — 初始化并幂等创建/获取 collection。
  - `add_code_standards` / `add_vuln_patterns` / `add_review_record` — 写入。
  - `query(query_text, n_results)` — 跨 collection 检索，返回相关文本列表。

### `app/retriever/kb_seed.py`
知识库种子数据与灌入逻辑。
- `CODE_STANDARDS` — 20 条代码规范（Google Python Style Guide + 通用最佳实践）。
- `VULN_PATTERNS` — 10 条漏洞模式（OWASP Top 10 / CWE）。
- `seed_kb(kb)` — 幂等灌入（通过 `count()==0` 判断）。

### `app/reviewers/multi_reviewer.py`
多角色审查 — Strategy 模式，按维度拆分审查器。
- `ReviewStrategy(ABC)` — 策略基类：`get_system_prompt()` + `review(numbered_code, file_path, kb_text)`；LLM/JSON 失败时优雅降级为空类别。
- `SecurityReviewer` — 安全维度（OWASP/CWE），产出 `security`。
- `QualityReviewer` — 质量维度（Bug + 代码异味），产出 `bugs` / `code_smells`。
- `_extract_json(raw)` — 稳健提取 JSON（容忍 ```json 包裹或前后杂文本）。
- `build_user_prompt(...)` — 构造带行号代码的用户消息。
- `get_default_strategies()` — 返回默认策略集合（Security + Quality）。

### `app/utils/code_lines.py`
- `add_line_numbers(code)` — 给代码每行加 `行号 |` 前缀，让 LLM 引用准确行号。

### `app/models/issue.py`
统一问题模型（全系统"通用货币"）。
- `ISSUE_TYPES` / `SEVERITIES` — 合法的类型与严重程度常量。
- `class Issue`（dataclass）— 字段：type/severity/file/line/title/reason/fix/source/references。
  - `severity_rank()` — 严重度排序权重（critical 最大）。
  - `to_dict()` — 序列化为 dict。

### `app/models/context.py`
Pipeline / Agent 传递的上下文"公文包"。
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

## 文档 / 规划

### `CLAUDE.md`
项目编码规范（长期生效）：规范一（文件索引管理）、规范二（修改留痕 CHANGELOG）。

### `docs/INDEX.md`
本文件 — 全项目文件索引。

### `docs/CHANGELOG.md`
修改留痕日志，以工作段为单位，push 时补写。

### `docs/superpowers/specs/`
各阶段设计文档（spec）。当前含 `2026-07-09-day1-core-models-design.md` — Day 1 核心模型与 Pipeline 骨架的设计动机、字段、接口、测试策略与验收标准。

### `_PLAN/AI Code Review Platform — V1 完整任务计划书.md`
现行主计划书（V2 修订版）：双模式 AI 代码审查平台的 23 天 / 3 Phase 详细任务、架构、数据模型与交付清单。

### `_PLAN/plan.md`
旧版规划与更新日志（Phase 1 已完成、旧 Phase 2 待办）；已被新计划书取代，保留作历史参考。

## 配置 / 其他

- `.env` — 环境变量（密钥、API URL、模型名、GITHUB_TOKEN）；已 gitignore。
- `.gitignore` — 忽略 `chroma_db/`、`.env`、`.claude/`、`__pycache__/`、`.idea/`、`*_review_report.md` 等。
- `chroma_db/` — ChromaDB 持久化目录（运行时生成）；已 gitignore。
- `chroma_db.bak.*` — 迁移期间对旧知识库的临时备份（运行时产物，可删）。
