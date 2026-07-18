# 修改日志 CHANGELOG

本项目所有代码改动的留痕记录，用于回溯与查证。维护规则见 `CLAUDE.md` 规范二。

## 记录规则

- **单位**：以「一段工作时长」为一条记录（非每次文件保存，也非每个 git commit）。
- **时机**：在 `git push` 前补写本段工作的记录。
- **每条必含**：日期（绝对日期）、改了什么（涉及文件与改动点，逐一列全）、为什么改、以及必要的「A → B」因果链。
- **铁律**：任何改动都要记录，禁止「为了 A 改了 B 却隐瞒 B」。

---

## 2026-07-19 — Judge V2：评判标准+JSON Schema+Evidence 截断+冻结数据补全

- `app/pipeline/agent_eval_judge.py` — V2 重写：
  - System prompt 从 80 字符扩展为含完整评判标准（verdict/score/字段定义）、边界规则（Agent 声明"无法确定"时判定规则）和类型约束的提示词
  - `_validate_schema()` 替代 `_normalize_judge_schema()`：JSON Schema 严格校验类型（boolean 必须是 true/false，integer 必须是整数），不合法时触发重试而非静默转义
  - `_truncate_evidence()` 新增：保留 Agent 引用的证据 + 预期文件证据 + 高置信度补充，控制在 ≤18 条，确保多样性（≥5 条非预期文件）
  - `JUDGE_OUTPUT_SCHEMA` 定义完整 JSON Schema（required/type/enum/minimum/maximum/additionalProperties）
  - 调用参数：max_tokens 1200→1800，timeout 20s→60s
  - Schema 不合法与解析失败均触发一次修复重试（含具体错误说明）
  - `judge_record()` 的输入新增 `expected_answer_keywords`（辅助参考，不要求逐词复述）
- `app/pipeline/freeze_external_baseline.py` — 新增 `enrich_frozen_with_ground_truth()`：从原始评测数据集按 sample_id 查找 ground truth，补全 `expected_answer_summary` 与 `expected_answer_keywords` 到冻结快照顶层字段；`--enrich` CLI 参数
- `eval_report/results_agent/external_glm_v1/frozen/external_{click,httpx,typer}_glm_v1.json` — 三个冻结快照各 21 条已补全 ground truth 摘要与关键词
- `eval_report/results_agent/external_glm_v1/judgments_final_{click,httpx,typer}.json` — 基于 V2 Judge 重判（--no-resume）
- `eval_report/results_agent/external_glm_v1/V1_JUDGE_REPORT.md` — 更新为 V2 数据 + 人工验证对比
- `tests/test_agent_eval.py` — 新增 `TestJudgeSchemaValidation`（11 条，JSON Schema 校验）+ `TestEvidenceTruncation`（5 条，截断策略）+ 更新 `TestSemanticJudge`（Schema 错误触发重试）
- `docs/CHANGELOG.md` — 本条目
- `docs/INDEX.md` — 同步更新
- `_PLAN/plan_status.md` — 同步更新

**为什么改（V1→V2）**：V1 Judge 的三项核心问题——(1) system prompt 太弱（80 字符无评判标准），导致 GLM 把问题文本填入 answered_question 字段而非 bool；(2) expected_answer_summary 全部为空（冻结时漏写），Judge 在不知"正确答案"的情况下盲判；(3) 每条样本塞入 60 条 evidence，噪音过大。结果：invalid_schema 率 5-24%，effective 率 76-95%。

**V2 改进效果**：
- judge_invalid_schema_rate：Click 23.8%→0%，HTTPX 4.8%→0%，Typer 14.3%→0%
- judge_effective_rate：三仓库均 100%
- 人工验证 12 条样本，Judge vs 人工一致率 91.7%（11/12）
- 语义完成率（5-10%）反映的是 v1 Agent 答案质量问题（LLM 合成因 token 预算耗尽而降级），非 Judge 问题

**已有判决保护**：旧 judgments_*.json 和 judgments_retry*.json 原样保留；frozen/ 下仅新增 expected_answer_summary/keywords 字段，原始运行结果未修改。

## 2026-07-18 — Agent 外部评测 LLM-as-Judge 可靠性修复与重判

- `app/pipeline/agent_eval_judge.py` — 全面重写 Judge 模块：
  - `_parse_judge_json()` 支持裸 JSON、Markdown fenced JSON、前后带解释文本、嵌套括号匹配；禁止 eval，只用 json.loads
  - 新增 `_extract_json_object()` 从文本中提取 JSON 对象（括号计数匹配）
  - 新增 `_strip_fences()` 去掉 Markdown 围栏
  - 新增 `_normalize_judge_schema()` 字段归一化：verdict（4 值白名单）、answered_question→bool、uses_supported_evidence→bool、score→0..2、expected_file_coverage（3 值白名单）；逐字段记录归一化错误
  - `judge_record()` 明确区分 `judge_unavailable`（API 失败/空输出/不可解析）与 `judge_invalid_schema`（可解析但字段不合法）；新增 `judge_error_type`、`schema_errors`、`retry_raw_response`、`retry_error` 字段
  - 空响应不再直接标为 unavailable，而是触发一次 JSON 修复重试；重试提示包含原始输出与错误原因，要求"只返回合法 JSON"
  - `summarize_judgments()` 新增 `judge_invalid_schema_rate`、`judge_effective_rate`、`retry_success_count`
  - `judge_baseline()` 新增 `call_llm` 参数支持测试依赖注入；`--no-resume` 参数强制忽略已有输出全量重判
  - Judge 输入只含 question/expected_answer_summary/expected_evidence_files/agent_answer/agent_evidence/fallback_reason，不读仓库
- `tests/test_agent_eval.py` — 从 30 条扩充至 71 条：新增 `TestJudgeJsonParsing`（16 条，裸 JSON/fenced/前后文本/空/None/嵌套/围栏剥离/JSON 对象提取）和 `TestJudgeSchemaNormalization`（13 条，合法 schema/无效 verdict/bool 归一/score 四舍五入与限幅/coverage 白名单/missing_points 非列表/reason 非字符串/GLM 典型误解），扩展 `TestSemanticJudge`（12 条新增，含重试成功/重试失败/empty→retry/None 持久化/invalid_schema 不重试/API 异常摘要/重试计数/原始响应审计）
- `eval_report/results_agent/external_glm_v1/judgments_final_{click,httpx,typer}.json` — 三份最终判决（每份 21 条），基于冻结快照 `frozen/external_{project}_glm_v1.json`
- `eval_report/results_agent/external_glm_v1/judge_final_run.log` — 实时运行日志
- `eval_report/results_agent/external_glm_v1/V1_JUDGE_REPORT.md` — 四层指标 Markdown 报告
- `docs/CHANGELOG.md` — 本条目（补写）
- `docs/INDEX.md` — 同步更新 Judge 模块与新评测产物条目
- `_PLAN/plan_status.md` — 同步更新评测跟踪状态

**为什么改**：Judge 初版 retry2 的 Click 不可用率高达 71.4%（15/21），原因为 GLM 空响应/Markdown ```json 包装 + 解析函数只处理 fenced JSON 导致大量 JSONDecodeError。同时旧代码混用空响应/解析失败/字段不合法三类情况，无法区分"Judge 挂了"和"Judge 判断不可评"。

**核心改进**：
- Judge 不可用率：Click 71.4%→0%，HTTPX 0%→0%，Typer 0%→0%
- Judge 有效评判率：Click 76.2%，HTTPX 95.2%，Typer 85.7%
- 语义完成率偏低（5-13%）反映的是 v1 Agent 答案质量（LLM 合成因 token 预算耗尽而降级），非 Judge 问题

**已有判决保护**：旧 judgments_*.json 和 judgments_retry*.json 原样保留作故障审计；新输出写入 judgments_final_*.json，不覆盖任何已有文件；frozen/ 下快照未修改。

## 2026-07-18 — 收紧 Investigation 关键词与 SearchTool 候选公平性边界

- `app/agent/investigator.py` — 关键词提取新增轻量停用词/通用词过滤，避免 `python`、`app`、`True`、`False`、`configuration` 等自然语言词触发无目标的大范围搜索；限定名如 `typer.main.Typer` 统一归一为可在源码定义处命中的末段符号 `Typer`。这是确定性启发式，不替代后续 LLM Planner。
- `app/tools/search_tool.py` — 将“每文件保留最佳命中”从最终输出阶段前移到流式 Top-K 候选维护阶段，并使用版本化惰性堆清理与压缩。单个文件的高分重复命中不再占满候选堆、阻断其他源码文件参与排序；保留 20,000 行扫描和有界内存保护。
- `tests/test_agent.py`、`tests/test_search_tool.py` — 覆盖限定名归一、通用词不触发搜索，以及 120 条噪声文件命中无法挤占 100 个候选槽位的回归场景。

**验证：** `tests/test_search_tool.py tests/test_agent.py` 定向回归通过，`git diff --check` 无内容错误。完整全量测试由提交者执行；本轮未改动冻结的 `external_glm_v0`。

## 2026-07-18 — SearchTool 流式 Top-K 修复外部仓库源码饥饿

- `app/tools/search_tool.py` — 将 `git grep` 的“先取前 1000 行、再排序”改为流式扫描与有界 Top-K 小根堆；扫描期间即时完成文件分类、命中分类与评分，最多扫描 20,000 行 / 10 MB / 30 秒，并明确暴露截断状态。这样 `git grep` 按路径字母序输出时，`docs/` 不会再耗尽候选池、使后续 `typer/` 源码不可达。
- `app/tools/search_tool.py` — `docs_src`、`doc_src` 按目录段归为 documentation；定义命中必须解析出真实定义名且与查询词匹配；最终结果每个文件优先保留其最高分命中，避免同一文件占满返回槽位。评分统一为：文件类型 + 命中类型 + 匹配精度 + 查询词覆盖度。
- `tests/test_search_tool.py` — 增加文件分类、定义名解析、命中分类、精度/多关键词评分、源代码优先、字母序末尾源码仍可入选、资源上限与回归场景的覆盖。

**确定性重放验收：** Typer 预期文件命中从 5/19（26.3%）提升至 17/19（89.5%），14 条原失败中修复 12 条；Click 77.8% → 88.9%，HTTPX 66.7% → 94.4%。这些是 SearchTool 的证据检索重放结果，证明工具层召回改善，**不是**新的 GLM 端到端成绩；`external_glm_v0` 仍冻结不变，完整重跑须另存为 `external_glm_v1` 并使用三层评分口径。

**已知边界：** 仍有 2 条 Typer 样本受通用词关键词提取影响；非 Python 文件继续使用正则回退。另，当前“按文件去重”发生在候选堆出堆后，能保证返回集去重；若后续出现单一文件在候选堆内挤占大量槽位，应将去重前移为每文件保留最佳候选。

## 2026-07-18 — Agent 评测拆分严格、证据与语义三层

- `app/pipeline/agent_eval_metrics.py` — 保留原关键词+文件规则并正式命名 `strict_completion_rate`；新增 `evidence_retrieval_rate`（预期文件是否进入 Evidence）和 `citation_grounded_rate`（答案 file:line 是否逐条回链 Agent Evidence）。
- `app/pipeline/agent_eval_judge.py` — 新增独立、可恢复的 LLM-as-Judge：仅消费问题、期望摘要、Agent 答案和 Agent Evidence；强制关闭 thinking，持久化原始 Judge JSON、判决、理由和不可判定状态。语义正确率、部分正确率、扎根答案率、Judge 不可用率分开统计。
- `tests/test_agent_eval.py` — 覆盖严格指标保留、结构化 Judge 输出、thinking 禁用、不可判定率及既有判决恢复；共 30 条评测测试通过。

**口径：** strict/evidence/semantic/citation 是互补层，严禁用其中任一指标替代其余层；Judge 不浏览仓库，不能为 Agent 补找证据。

## 2026-07-18 — 修复 Agent 评测预算耗尽统计

- `app/pipeline/agent_eval_metrics.py` — 新增唯一预算判定函数 `detect_budget_exceeded()`：trace 含 `budget_exhausted`、任一 StepRecord 的 `decision=BUDGET`、或 `final_state/state.status=BUDGET` 任一成立即计超限；同时提取 `steps/files/tokens` 类型。
- `app/pipeline/agent_eval_runner.py` — 每条结果写入统一计算的 `budget_exhausted` 与 `budget_type`，不再仅依赖 trace 文本。
- `app/pipeline/freeze_external_baseline.py` — 冻结标签复用同一判定函数，避免报告与失败标签口径漂移。
- `tests/test_agent_eval.py` — 覆盖 trace、StepRecord、最终 state 三个判定来源；验证 StepRecord 单独存在时也会进入超限率和类型统计。

**原始结果审计：** 对 external_glm_v0 来源的 63 条逐条对照，旧字段与真实状态有 30 条不一致；新逻辑识别 30 条 `BUDGET`，逐条一致。v0 快照保持不变，后续报告基于修复后的计算逻辑重算。

## 2026-07-18 — 冻结首次外部真实 LLM 基线 external_glm_v0

- `app/pipeline/freeze_external_baseline.py` — 新增不可覆盖的基线冻结器：从三份外部 GLM 结果生成 `external_glm_v0`；目标文件用创建模式写入，存在即拒绝覆盖。
- `eval_report/results_agent/external_glm_v0/external_{click,httpx,typer}_glm_v0.json` — 固化每条样本的仓库/commit、问题类型、模型配置、原始回答/降级原因、Evidence、StepRecord、预算配置与观测值、规则评分、可重叠失败标签。`tokens_used` 在 v0 运行器未持久化，明确记为 `null`，不伪造数据。

**失败标签：** `keyword_miss`、`expected_file_miss`、`llm_fallback`、`budget_exhausted`、`ground_truth_ambiguous`（留待人工标注）、`tool_error`；标签可重叠，禁止按列相加当作失败总数。

**冻结校验：** 三项目共 63 条，快照各 21 条。发现 Click/HTTPX 各有 15 条 StepRecord 标记 `BUDGET`，而旧汇总的 budget_overrun_rate 为 0，确认这是 runner 统计缺陷，后续修复不得回写或覆盖 v0。

## 2026-07-18 — 外部三项目真实 GLM Agent 基线完成

- `app/pipeline/agent_eval_runner.py` — 增加逐样本 checkpoint 与恢复：独立题每条立即持久化；续问链整组完成后持久化，恢复时不错误复用失效内存会话。
- `eval_report/results_agent/external_{click,httpx,typer}_glm.{checkpoint,}json` — 固化 Click 8.4.2、HTTPX 0.28.1、Typer 0.27.0 各 21 条（五类问题 + 三组续问）的真实 GLM-4.5-Air 运行结果。

**首轮外部基线：** Click 完成率 47.62% / 可追溯率 76.19% / 平均 1.89 步；HTTPX 19.05% / 80.95% / 2.06 步；Typer 4.76% / 57.14% / 1.94 步。三项目共 63 条，完成率为关键词+预期文件的规则代理指标，不能表述为真实回答正确率；项目间落差证明当前 Search/AST 证据路径仍有明显外部泛化缺口。

## 2026-07-17 — 外部 Agent 冒烟评测：SearchTool 调用链修复

- `app/tools/search_tool.py` — 将结果截断从 `git grep` 前移至相关性排序后，避免前 50 条文档命中挤掉后续源码定义；多关键词改用 Git 正确的 `-e pattern ... -- pathspec` 语法，修复关键词被误作 pathspec 导致的零命中。
- `app/pipeline/eval_dataset.py` — 外部候选缺少显式答案关键词时，仅从已核验摘要中派生必答 target symbols，不再要求简明答案逐字枚举全部调查目标。
- `app/pipeline/agent_eval_runner.py` — Windows stdout/stderr 强制 UTF-8，评测 JSON 输出不再因 GBK 编码崩溃。

**验收：** Click 固定 commit 的前 5 条 mock 端到端冒烟集：任务完成率 100%、证据可追溯率 100%、平均 2.0 工具步、0% 预算超限；该结果验证检索/评测链路，不代表真实 LLM 质量。

---

## 2026-07-17 — 外部开源仓库 Investigation 评测集导入

- `tests/__snapshots__/agent_eval_external_v1.json` — 导入 Click 8.4.2、HTTPX 0.28.1、Typer 0.27.0 的 63 条外部候选（45 条独立题 + 9 组续问链）；每条保留 `repo_url`、固定 `commit_sha`、证据文件/位置与可复验命令。
- `app/pipeline/eval_dataset.py` — 增加独立 `agent_external` 加载模式，支持按 `project` 过滤，不与本项目 46 条 `agent_real` 混合；样本保留项目和固定提交元数据。
- `app/pipeline/agent_eval_runner.py` — CLI 增加 `--dataset agent_external --project click|httpx|typer`，使 Agent 在相应 checkout 工作树上执行端到端评测。
- `tests/test_agent_eval.py` — 增加外部数据集按项目加载、固定 SHA 完整性测试。

**独立校验：** 对 63 条逐项核验本地 checkout HEAD、所有 `expected_evidence_files` 与 `verification_method` 必填字段，结果 `63 samples / 0 errors`。这证明候选数据与固定源码快照一致；语义答案质量仍需后续端到端运行与人工抽样复核。

**验证：** `tests/test_agent_eval.py` → 25 passed；`git diff --check` 通过。

**冒烟发现（尚未宣称为模型指标）：** Click 前 5 条 mock 端到端链路可以运行，但暴露外部 schema 的 `expected_keywords` → 内部 `expected_answer_keywords` 映射缺失，已兼容修复；同时多关键词 SearchTool 优先返回文档/变更记录而非源码，作为下一项外部泛化缺陷处理，不能将本次 0% mock 完成率解释为 Agent 能力。

---

## 2026-07-17 — Agent 评测口径校正与首次调查无证据恢复

- `app/pipeline/agent_eval_metrics.py` — 将原误称为“续问节省率”的 `follow_up_steps / initial_steps` 明确为 `follow_up_relative_cost`（越低越好）；`follow_up_savings_rate` 改为非加权节省率 `1 - cost`，并新增按总工具步数计算的 `follow_up_weighted_savings_rate`。逐组明细同时输出相对成本与节省率，避免短首次调查造成等权误导。
- `app/agent/investigator.py` — 内容搜索零证据时，状态机先执行一次受限的 `search_filename` 恢复；仅文件名恢复仍无证据时才 `NO_EVIDENCE`。该路径不会在内容搜索已命中时额外执行，保持常规搜索→AST/依赖链的效率与确定性。
- `tests/test_agent.py` — 增加无证据恢复选择测试，以及 steps/files/tokens 三类预算在“上限前可继续、达到上限即阻塞”的临界测试。
- `tests/test_agent_eval.py` — 更新节省率口径测试，新增加权指标防止短首次调查的等权偏差。

**原因：** 首批续问结果中的 75% 实为“相对成本”而非节省；`NO_EVIDENCE` 的一步退出还会使后续追问从零开始。修正后，评测指标与产品行为均能如实反映调查效率。

**验证：** 使用 `E:\Anaconda\envs\bid_rag\python.exe` 配合临时 pytest runner 执行 `tests/test_agent.py tests/test_agent_eval.py -q --tb=short` → 99 passed；`git diff --check` 通过。

---

## 2026-07-17 — Agent 真实评测集：46 条调查问题 + 5 项指标评测框架

继 M1/M2/M3 落地后，按用户规划建立 Agent 评测集，用**真实代码库问题**（非模板生成）测量 InvestigationAgent 的实际表现。

**新增文件：**
- `tests/__snapshots__/agent_eval_real.json` — 46 条真实调查问题，覆盖全部 5 种问题类型（locate 8 + explain 8 + trace 8 + impact 6 + grep 8）+ 4 组续问对（8 条）。每条含扩展 ground truth（expected_answer_keywords/expected_evidence_files/expected_answer_summary）。
- `app/pipeline/agent_eval_metrics.py` — `AgentEvalMetrics` 类，5 项指标：任务完成率（规则判定：关键词+文件匹配）、证据可追溯率（正则 file:line 引用）、按类型平均工具步数（排除 blocked 步骤）、预算超限率（细分 steps/files/tokens）、续问节省率（follow_up_steps/initial_steps）。`compute()` 聚合 + `to_dict()` + `summary()` Markdown 报告。
- `app/pipeline/agent_eval_runner.py` — `AgentEvalRunner` 类：`run_all()` 按独立问题→续问链顺序执行 `investigate()`/`follow_up()`；支持 `--mock` 确定性模式（不上网）、`--top N`、`--json`、`--output`。CLI 入口：`python -m app.pipeline.agent_eval_runner`。
- `tests/test_agent_eval.py` — 23 条测试（mock LLM）：TestRealDataset(3)+TestMetricsComputation(9)+TestJudgeHelpers(5)+TestRunnerMock(3)+TestAgentRealJsonIntegrity(3)。

**修改文件：**
- `app/pipeline/eval_dataset.py` — 新增 `RealInvestigationSample` dataclass（含 follow_up_group/follow_up_order）；`load_samples()` 新增 `mode="agent_real"` 分支，从 `agent_eval_real.json` 加载。

**文档同步：**
- `docs/INDEX.md` — 追加 4 个新文件条目；测试总数 236→259。
- `_PLAN/plan_status.md` — 标记 Agent 评测集进度（🟡 进行中）。
- `docs/CHANGELOG.md` — 本条目。

**验证：** `python -m pytest tests/test_agent.py tests/test_agent_eval.py -v` → 94 passed。

---

## 2026-07-17 — V1.1 Investigation Agent 增强三增量（M1/M2/M3）全段落地

本段工作按用户规划的 M1→M2→M3 三增量逐级实施，将 InvestigationAgent 从简单的”问题分类→固定计划→LLM 合成”升级为假设驱动的有限状态调查循环 + 三维预算 + 跨工具关联 + 续问上下文。**71 条 Agent 测试全绿（mock LLM，不上网）。**

**A → B 因果链**：因为用户要求”先做 M1，然后建立 Agent 评测集”→ M1/M2/M3 均已落地；Agent 评测集（30–50 真实问题）为下一段工作。

### M1：假设驱动的有限状态调查循环

**核心改动 — `app/agent/investigator.py`：**
- 新增 `StepRecord` — 单步调查记录：step/tool/params/status/evidence_count/hypothesis_before/hypothesis_after/decision/duration_ms；全局唯一可重放。
- 新增 `InvestigationState` — 调查状态机：goal/keywords/hypotheses/confirmed/evidence/steps/files_visited + 派生属性 `steps_remaining`/`is_budget_exhausted`。
- 重写 `investigate()` — 主循环：`_seed_hypotheses` → while steps_remaining > 0: `_select_next_tool` → `_execute_step` → `_update_hypotheses` → `_evaluate` → break if != CONTINUE。
- 确定性工具选择：按 goal（locate/explain/trace/grep）定义优先级表 `_TOOL_PRIORITY`，`_select_next_tool()` 从表中选择第一个未使用且适用当前假设的工具。
- 安全退出三层保护：步数上限(6)、证据门禁(某步 0 Evidence → NO_EVIDENCE→STOP)、无工具可选用(return None)。
- 保留 `_classify()`、`_extract_keywords()` 不变。

**支撑文件更新：**
- `app/agent/__init__.py` — 导出 `InvestigationState`、`StepRecord`。
- `app/api/schemas.py` — `InvestigateResponse` 新增 `steps: list[dict]` 字段。
- `app/cli.py` — 调查输出增加步骤详情（工具/状态/证据数/决策标记）；stdout 强制 UTF-8 解决 Windows GBK 终端编码错误。
- `tests/test_agent.py` — 35 条测试（重写全部现有 18 条 + 新增 17 条 M1 专项）：TestClassify(6)+TestExtractKeywords(5)+TestToolSelection(5)+TestInvestigateWithMockLLM(8)+TestStateMachine(5)+TestHypothesisFlow(6)。

### M2：三维预算 + 跨工具关联 + LLM 辅助排序

**核心改动 — `app/agent/investigator.py`：**
- `InvestigationState` 扩展三维预算：`steps_max=6`/`files_max=50`/`token_budget=16000` + `files_read`/`tokens_used`；新增 `is_files_exhausted`/`is_token_exhausted` 派生属性。
- `_check_budget()` — 三维预算前置检查，返回阻塞原因字符串。
- `StepRecord` 新增 `budget_reason` 字段；`_evaluate()` 返回 `(decision, budget_reason)` 元组。
- `_select_next_tool()`：从 static 改为 instance 方法；新增 `_correlate_candidates()` 跨工具关联链（Search→AST→Dependency→Knowledge）；新增 `_llm_rank_tools()` LLM 排序（白名单校验 + 失败回退确定性优先级）；新增 `_is_duplicate()` 等效工具去重（参数哈希）。
- 问题类型新增 “impact”（修改 X 会影响什么？）。
- `_estimate_tokens(char_count)` — token 估算（chars/4，代码比自然语言密集）。
- `_read_python_files()` — 经 WorkspaceManager 受控快照读取 .py 文件。
- `_ingest_search_result()`/`_ingest_ast_result()` — 从工具结果提取 files_visited。

**测试扩展 — `tests/test_agent.py`（35→52）：**
- `TestBudget3D`（4）— 文件/token 耗尽/初始未耗尽/极小文件预算。
- `TestCrossToolCorrelation`（3）— 搜索→AST 优先/AST→dependency 优先/单候选不变。
- `TestLLMRanking`（3）— 有效返回/无效回退/单候选跳过。
- `TestDedup`（2）— 等效工具去重。
- `TestM2Integration`（5）— 多工具链/budget_reason 字段/跨工具关联验证。
- `TestHypothesisFlow` 从 3→5（impact 类型 + AST→dependency 假设链）。
- `TestClassify` 从 6→7（impact 类型）。

### M3：续问上下文 + 会话持久化

**核心改动 — `app/agent/investigator.py`：**
- 新增 `InvestigationStore` 类 — `save(id, state)`/`load(id)`/`delete(id)`/`session_count`；内存 dict 存储，支持跨轮证据复用。
- `InvestigationResult` 新增字段：`investigation_id`（`inv_` + 12 位 hex，基于 question+time+uuid 的 MD5）、`is_follow_up`、`reused_evidence_refs`。
- 新增 `follow_up(repo_path, investigation_id, question)` — 续问入口：加载会话→匹配已有证据→充足时零工具调用合成答案→不足时恢复状态追加步骤。
- 新增 `_match_existing_evidence(session, question, keywords)` — 关键词匹配已有证据，返回引用列表。
- 新增 `_restore_state(session, new_question)` — 从持久化 session 重建 InvestigationState（含步数/文件数/token 消耗）。
- 新增 `_synthesize_follow_up(result, session, question, matched_refs, ...)` — 基于已有证据合成续问答案，带跨轮引用编号 [ref1][ref2]。
- 新增 `_new_investigation_id(question)` — 生成唯一会话 ID。
- `investigate()` 结束时自动 `store.save()` 持久化状态。

**支撑文件更新：**
- `app/agent/__init__.py` — 导出 `InvestigationStore`。
- `app/api/schemas.py` — `InvestigateResponse` 新增 `investigation_id`、`is_follow_up`、`reused_evidence_refs`。
- `app/cli.py` — `investigate` 子命令新增 `--follow-up <inv_id>` 参数；输出中显示 investigation_id、续问状态、复用证据引用、预算原因。
- `tests/test_agent.py`（52→71）：新增 `TestInvestigationId`(4)+`TestInvestigationStore`(5)+`TestFollowUp`(10)；导入 `Evidence`/`CodeLocation`。

**文档同步：**
- `docs/INDEX.md` — 更新 agent/__init__.py、investigator.py、schemas.py、cli.py、test_agent.py 条目；测试总数 217→236。
- `_PLAN/plan_status.md` — V1.1「多轮探索 UX」翻转为 ✅；优先级首位调整为「Agent 评测集构建」。
- `docs/CHANGELOG.md` — 本条目。

**验证：** `python -m pytest tests/test_agent.py -v` → 71 passed。

---

## 2026-07-17 — 修复 GitHub Actions 缺失 pytest 依赖

- `requirements.txt` — 增加 `pytest>=7.4.0`。CI 的 `test` job 只安装此文件中的依赖，但此前未声明 pytest，导致 Python 3.11 报 `No module named pytest`，Python 3.10 与后续 golden/recovery job 被连带取消。

**验证：** GitHub Actions 下次 push 将先安装 pytest，再执行既有测试矩阵。

---

## 2026-07-16 ~ 2026-07-17 — Report 级评测落地、证据链修复、漏报四项补强与真实 GLM 基准

本段工作跨三个主题，最终产出首个有效的真实模型基准：static P 97.47% / R 64.71% / F1 77.78%；llm P 95.62% / R 77.51% / F1 85.62%（llm 相对 static F1 +7.8pp，验证「确定性工具可靠扫描 + LLM 语义补漏」架构方向）。口径警告：真值来自合成样本 + LLM-as-Judge，属系统内对比指标，不得表述为生产准确率。

### 主题一：Report 级评测框架落地（eval_report/）

**动机**：M4 只有 Plan 级评测（预测该跑哪些工具），缺 Issue 级质量评测（报出来的问题对不对、漏了什么）。

- `eval_report/__init__.py` — 新包：真实代码 → Pipeline → LLM-as-Judge → Issue 质量指标。
- `eval_report/generate_samples.py` — LLM 样本生成器：按风险信号批量生成含已知漏洞的代码，初始化为 safe→vuln 双 commit git repo。后续改造：样本目录从仓库内 `samples/` 移到 `%TEMP%/eval_report_samples`（`EVAL_SAMPLES_DIR` 可覆盖），新增 `--output-dir` 参数。
- `eval_report/_gen_20_samples.py` — 手工构造 20 个确定性样本（s01–s20），覆盖 bandit/ruff/dependency/python_ast/混合/边界 6 类场景，内嵌 expected_issues 标注。
- `eval_report/run_pipeline.py` — 批量执行器，`--mode static|llm` 输出到 `results/<mode>/`。
- `eval_report/judge.py` — LLM-as-Judge：逐 Issue 判 correct/false_positive/uncertain + 列 missed。
- `eval_report/metrics.py` — 配对 pipeline_output + judgment 计算 P/R/F1，输出 Markdown/JSON 报告。
- `eval_report/sample_cve.py` — GitHub 真实 CVE 案例采样脚本（gh CLI 搜索 fix commit，checkout 漏洞版本跑 Pipeline）。
- `eval_report/_zhipu_review.py` — 临时脚本：把当前项目 diff 发给智谱做一次性审查。
- `eval_report/samples/test_manual_001/auth.py` — 手工样本实验产物，添加后即删除，不保留（样本一律放临时目录）。
- `.gitignore` — 忽略 `eval_report/samples|results|results_baseline_*|reports/` 与 `review_output.json`（运行产物不入库）。
- `README.md`、`tests/test_fact_collector.py`、`tests/test_m2_pipeline.py`、`tests/test_m5_api.py`、`tests/test_performance.py`、`.github/workflows/test.yml` — 属 2026-07-15 已记录条目的实际落盘（fixed_git_diff 覆盖补全 + actions 升级），随本段一并提交。

### 主题二：评测证据链缺陷修复（Judge 结果曾 20/20 全部无效）

**根因**：Judge 在评判时到样本目录执行 `git diff HEAD~1..HEAD` 取证；样本放在可被清理的临时目录，目录失效后整批 Judge 结果作废。**正确设计：Pipeline 运行当时把 diff 固化进结果，Judge 只消费持久化证据。**

- `app/tools/git_tool.py` — `_diff()` 返回 `(ChangeSet, unified_diff)`，artifacts 增加 `unified_diff`（`--unified=8`）。
- `app/pipeline/review_pipeline.py` — `ReviewOutput` 新增 `unified_diff` 字段，run() 固化本次 diff 证据。
- `eval_report/run_pipeline.py` — 结果 JSON 持久化 `unified_diff`。
- `eval_report/judge.py` — 删除 `_get_diff()`（git 取证）改为 `_load_diff()`（只读持久化字段）；缺字段报明确错误，不回退 git；修正结束时打印的目录为实际输出目录。
- `eval_report/metrics.py` — `_load_data()`/`run()` 支持 `--results-dir`（结果已按 mode 分目录）。
- `tests/helpers.py` — 新增 `FIXED_UNIFIED_DIFF`，mock GitTool 同步返回 unified_diff artifact。
- `tests/__init__.py`、`tests/test_eval_diff_persistence.py` — 新增 5 条证据链回归测试（TDD，先红后绿）。

**连带修复（A→B 因果）**：重跑 Judge 时 11/20 样本 JSON 解析失败 → 排查发现 glm-4.5-air 是推理模型，思考内容与正文共用 max_tokens 预算，预算被思考耗尽导致正文为空 → 为此改了 `app/tools/llm_tool.py`：`chat()/chat_completion()` 增加 `extra_body` 透传（如 `{"thinking": {"type": "disabled"}}`）、`timeout` 参数、惰性导入 OpenAI SDK（mock/offline 测试无需依赖）、SDK 层 `max_retries=0`（重试统一由 tenacity 管理，避免叠加重试导致长时间无观测阻塞）→ `judge.py` 与 `run_pipeline.py::_glm_call` 关闭 thinking 并调整输出预算（900→1500）。`tests/test_m3_llm.py` 新增 extra_body 透传测试。

### 主题三：针对漏报的四项补强（同基准验证 Recall 真实提升）

**动机**：首轮有效基准 Recall 仅 ~41%。聚合 40 条 Judge missed 清单定位四类根因，逐项补强后同基准复测。

- `app/tools/ruff_tool.py` — 显式 `--select E,W,F,S,C90`（ruff 默认只启用 E4/E7/E9/F，S101 assert、C901 复杂度、E501 行长全部漏检；s11/s12 类样本曾零检出）。E2xx/E3xx 空格空行类需 `--preview` 才生效，经权衡不开启（preview 规则跨版本行为漂移，与确定性原则和不锁版本的 `ruff>=0.1.0` 冲突；仅影响 E302/E231 两条最轻微格式项）。
- `app/pipeline/aggregator.py` — 分组键 `(file, rule_id)` → `(file, rule_id, start_line)`：同规则多处命中不再被合并吞掉（s01 第二处 SQL 注入、s08 六个未使用导入曾在报告中不可见），对齐主流工具报告粒度。
- `app/pipeline/review_pipeline.py` — LLM 语义审查从仅 `.py` 扩展到 `_llm_reviewable()` 白名单（js/ts/java/go/json/yaml/toml 等，排除 lock 文件）；s20 index.js 的 SQL 注入、s17 settings.json 硬编码密钥曾完全无覆盖。
- `app/pipeline/llm_reviewer.py` — 系统提示改为 9 项显式检查清单（硬编码凭据/注入/鉴权缺失/不安全密码学/异常处理/资源泄漏/框架安全配置/固定路径/逻辑边界）；「是否重复」只对照静态发现列表判断（原「不要重复静态工具的问题」表述导致 LLM 不报它以为 bandit 会报的问题）。
- `tests/test_static_tools.py`、`tests/test_m2_pipeline.py`、`tests/test_m3_llm.py` — 各新增 1 条针对性测试（TDD）。
- 验收（同 20 样本、同 Judge 配置）：static F1 55.29%→77.78%，llm F1 56.00%→85.62%；预设缺陷命中率（expected_issues 口径）static 32/38、llm 36/38。基线备份 `eval_report/results_baseline_20260716/`（不入库）。

### 主题四：受控工作区安全加固与 Agent 接入（随本段一并提交的前期改动）

- `app/core/workspace.py` — tar 快照解压增加安全校验（路径逃逸/符号链接拒绝、文件数与总大小上限、导出超时）；`read_file()` 拒绝绝对路径、校验扩展名与文件大小；archive 失败不再回退到无约束复制；导出失败清理临时目录；`_copy_snapshot` 补文件数/大小上限。
- `app/agent/investigator.py` — 读取上下文文件从直接读仓库改为经 `WorkspaceManager` 受控快照（与 Pipeline 同一安全边界）。
- `app/pipeline/plan_builder.py` — 修复文件名快速风险扫描 bug：原只对 `dependency_change` 一类生效，现对全部风险模式逐一匹配（auth.py→auth_change 等）；大变更显式加 `large_diff` 信号。
- `pytest.ini` — markers 描述改为纯英文（延续该文件纯 ASCII 约定，规避本地编码读取问题）。

### 文档与状态同步

- `docs/INDEX.md` — 补齐整个 `eval_report/` 章节（此前从未索引）；同步 ruff_tool/aggregator/llm_reviewer/review_pipeline/llm_tool 及测试条目；测试计数更新为 183。
- `_PLAN/plan_status.md` — 同步真实进度：M3「真实模型 benchmark」、M4「Report 级评测」「下游 Issue P/R 评测」翻转为 ✅；重写「本轮评测跟踪」（原文还写着"无 ZHIPU_API_KEY、GLM 基准未完成"，与事实脱节）；优先级更新为 ① Agent 增强 ② 真值校准 ③ 微调 Planner；页眉加当前阶段定义。
- `docs/CHANGELOG.md` — 本条目。

**验证**：`python -m pytest tests/` → 183 passed（含 8 条本段新增，全部 TDD 先红后绿）。

---

## 2026-07-15 — 补全固定 Git Diff 覆盖范围 + Node.js 20 弃用修复

**动机**：上次修复（2026-07-14）只把 `fixed_git_diff` fixture 应用到了 `test_pipeline_recovery.py` 和 `test_m3_llm.py`，但仍有 4 个测试文件在 CI 中直接调用真实 GitTool，当 push 只含 .md 文件时可能失败。GitHub 邮件再次报 exit code 1，同时附带了 Node.js 20 弃用告警。

**改了什么**：

- `tests/test_m2_pipeline.py` — `TestReviewPipeline.test_full_pipeline` 新增 `fixed_git_diff` fixture 参数，确保集成测试不依赖仓库当前 diff。
- `tests/test_fact_collector.py` — `test_collect_on_self`、`test_all_findings_have_evidence`、`test_all_findings_have_location` 新增 `fixed_git_diff` fixture 参数。注意：`FactCollector.collect()` 内直接 `GitTool()` new 实例（不走 `_TOOL_REGISTRY`），但 `monkeypatch.setattr` 作用于类方法，所有实例均受影响。
- `tests/test_m5_api.py` — 4 个 Review 创建测试（`test_review_success`、`test_review_with_default_refs`、`test_get_review_after_create`、`test_review_end_to_end_has_issues_in_own_repo`）新增 `fixed_git_diff` fixture 参数；`test_cli_review_integration` 同样新增 fixture。`test_review_end_to_end_has_issues_in_own_repo` 的 `HEAD~5` 改为 `HEAD~3`（修复前 `HEAD~5` 超出 CI 浅克隆深度 5，git diff 返回空，`len(evidence) > 0` 断言失败）。
- `tests/test_performance.py` — `test_timeline_produced` 和 `test_timeline_ascii_output`（均标记 `@pytest.mark.perf`，在 CI 中执行）新增 `fixed_git_diff` fixture 参数。
- `.github/workflows/test.yml` — 全部 3 个 job（test/golden/recovery）中 `actions/checkout` 由 v4 升级为 v5、`actions/setup-python` 由 v5 升级为 v6，消除 Node.js 20 弃用告警。
- `docs/INDEX.md` — 同步更新上述测试文件 + workflow 条目的描述。
- `docs/CHANGELOG.md` — 本条目。

**A → B 因果链**：
- 因为上次 `fixed_git_diff` 只覆盖了 recovery 和 m3_llm 两个测试文件 → 其余测试在 md-only push 场景下仍会因 diff 无 .py 文件而计划只含 git → 部分断言（如 `len(evidence) > 0`、`"bandit" in analyzers`）失败 → 本次补全所有仍使用真实 git diff 的测试。
- 因为 CI `fetch-depth: 5` 只能拉到 `HEAD~4` → `test_review_end_to_end_has_issues_in_own_repo` 的 `HEAD~5` 在浅克隆中不存在 → git diff 静默返回空（`_git()` 不检查 returncode）→ 无 evidence → 断言失败 → 改为 `HEAD~3`。
- 因为 GitHub 于 2026-06-02 强制所有 actions 运行在 Node.js 24 → `actions/checkout@v4`（Node 20）和 `actions/setup-python@v5`（Node 20）触发弃用告警 → 升级到 `@v5`/`@v6`（均为 Node 24）。

**验证**：`python -m pytest tests/ -q -m "not slow and not golden"` → 全部通过；golden（4 条）→ 通过；recovery（10 条）→ 通过。

---

## 2026-07-14 — 测试稳定性重构：固定 Git Diff 输入，消除对仓库提交历史的依赖

**动机**：前几轮 CI 修复反复在 `test_pipeline_recovery.py` 和 `test_m3_llm.py` 中加入条件守卫
（`if "X" in output.plan.get("analyzers", [])`），本质是治标不治本的创可贴。用户明确指出：
"测试不应该依赖仓库'当前最近几次提交'这种会变化的状态，而应该使用固定的测试输入"。

**改了什么**：
- `tests/helpers.py`（新文件）— 共享测试工具：定义 `FIXED_CHANGESET`（含 auth.py/utils.py/requirements.txt，
  确保 plan builder 始终选中 git + python_ast + ruff + bandit + dependency 全部五个工具）、
  `mock_git_execute`（返回固定 change_set 的 GitTool.execute 替代）、`patch_git_tool(monkeypatch)`。
- `tests/conftest.py`（新文件）— `fixed_git_diff` fixture，自动 monkeypatch `GitTool.execute` 为固定返回。
- `tests/test_pipeline_recovery.py` — 重写全部 10 个测试：
  - 所有调用 `pipeline.run(".", "HEAD~2", "HEAD")` 的测试改为使用 `fixed_git_diff` fixture
  - 移除全部条件守卫（`if "X" in output.plan.get("analyzers", [])`）
  - 恢复确定性断言：直接验证 trace 中含失败步骤，不再需要 else 分支
  - 新增 `_success_tool` 辅助构造器，用于 `test_tool_returns_failure_pipeline_continues` 中
    隔离 ruff 不受 bandit 失败影响
- `tests/test_m3_llm.py` — `test_pipeline_with_mock_llm_still_returns_static` 和
  `test_pipeline_static_results_preserved_on_llm_failure` 使用 `fixed_git_diff` fixture，
  移除 `has_py` 条件判断，LLM trace 断言始终生效

**为什么改**：`HEAD~2..HEAD` 的相对含义随每次 commit 改变。之前 3 轮 CI 修复都是
在测试里加 if/else 分支来应对"这次提交有没有 .py 文件"的不确定性，但这让测试逻辑
越来越复杂，且不能保证覆盖目标场景。固定 change_set 让测试输入从"仓库当前状态"变成
"预先设计好的场景"，无论何时、在哪个环境运行，plan 都包含全部 5 个工具，断言永远一致。

**A → B 因果链**：
- 因为 `pytest.monkeypatch` 在 fixture 中替换的是 `GitTool.execute` 类方法 → 所有
  GitTool 实例（包括 `_TOOL_REGISTRY` 中预创建的）都受影响，无需额外协调
- 因为固定 change_set 中的文件（auth.py/utils.py）在实际仓库中不存在 → ruff/bandit
  在恢复测试中会被调用（ws_targets 从 change_set 构建，非空）但找不到文件而失败 →
  恢复测试中需同时 mock 掉不关心的工具（如 `_success_tool("ruff")`），避免噪音失败
  干扰被测断言
- 测试总数保持 173 条全绿，无回归

---

## 2026-07-14 — 任务 3/4/5：SearchTool + 语言覆盖扩展 + 空样本补生成 + CI 修复

本段工作按 `_PLAN/plan_status.md` 当前优先级执行，完成计划跟踪表中排位 3/4/5 的三项任务，同时修复 CI 上 3 条测试失败。

**1. CI 测试修复（3 errors → 0）：**
- `tests/test_agent.py` — `test_investigate_no_results`：硬编码关键词 `zzz_not_exist_xyz_12345` 出现在测试文件自身中，git grep 必然命中（自己搜到自己），导致走到了 LLM 合成路径返回 "mock" 而无"未找到"提示 → 改用 `uuid.uuid4().hex` 动态生成关键词并用引号包裹，确保仓库内无匹配。
- `tests/test_pipeline_recovery.py` — `test_dependency_crashes_pipeline_finishes`：`HEAD~2..HEAD` 全部是 `.py` 文件变更，无依赖文件（requirements.txt 等），`RuleBasedPlanBuilder` 不会把 `dependency` 加入 analyzer 列表，工具从未被调用，`dep_trace` 为空导致 `any()` 返回 `False` → 先检查 `output.plan` 是否包含 dependency，不在计划中则跳过断言。
- `tests/__snapshots__/pipeline_head_snapshot.json` — `test_snapshot_issue_count_not_decreased`：快照文件使用相对引用 `HEAD~3..HEAD` 生成，commit 不断新增后旧基线（84 条）不适用于当前 diff（27 条），ratio=0.32<0.5 → 删除过时快照，下次运行以当前基线重建。

**2. SearchTool 独立实现（M1 补完，任务 5）：**
- `app/tools/search_tool.py`（新文件）— 遵循 Tool 协议（`name` + `execute(ToolRequest) -> ToolResult`），提供两种搜索模式：`search_type="grep"` 调用 git grep 做内容搜索，`search_type="filename"` 调用 git ls-files 做文件名搜索。返回结构化 ToolResult（artifacts 含 matches/files + Evidence 列表）。

**3. InvestigationAgent 重构（任务 5 连带）：**
- `app/agent/investigator.py` — `investigate()` 方法中原先用 `subprocess.run` 裸调 git grep/git ls-files，现改为 `SearchTool.execute(ToolRequest(...))` 并通过 `ToolResult.artifacts` 解析结构化结果。导入新增 `SearchTool` 和 `ToolRequest`，移除 `subprocess` 导入。

**4. 非 Python 语言覆盖扩展（M4，任务 4）：**
- `app/pipeline/eval_generator.py` — `build_coverage_matrix()` 从 9 段分层扩展为 11 段：原 JS/TS(~50) 拆分为 JS(~70)+TS(~50)，原 Java/Go(~30) 拆分为 Java(~30)+Go(~30)，Python 占比从 93%(512/550) 降至 50%(275/550)。覆盖分布：Python 50%、JavaScript 14%、TypeScript 10%、Java 6%、Go 6%、Mixed/配置 14%。
- **因果链**：增加非 Python 样本数量后，发现分布并未改善 → 定位到 `_id` 生成 bug：所有同语言样本使用 `_make_review_id({"language":"JavaScript"})` 产生完全相同的 ID，70 条 JS 去重后只剩 1 条，补齐逻辑 98% 概率填回 Python → 在每个样本的 `_id` 中加入 `idx` 序号确保唯一性；同时为 section 9（非代码）加入 ext+idx，section 10（混合）加入 idx。修复后去重丢弃量从 260 条降至 0 条。

**5. 空样本补生成（M4，任务 3）：**
- `app/pipeline/eval_generator.py` — 新增 `regenerate_empty(dataset_path)` 函数：加载已有数据集 → 找到 change_summary 为空的 review 样本 → 利用 `build_coverage_matrix` 确定性按位置匹配原始参数 → 批量 LLM 补生成 → 更新保存。
- CLI 新增 `--regenerate` 开关：`python -m app.pipeline.eval_generator --regenerate`。

**6. 文档同步：**
- `docs/INDEX.md` — 新增 `app/tools/search_tool.py` 条目；更新 `eval_generator.py` 条目（V1.2 11 段 + regenerate_empty）；更新 `investigator.py` 条目（SearchTool 重构）。
- `_PLAN/plan_status.md` — 同步更新 M1/SearchTool、M4/语言扩展、M4/空样本补生成、V1.1/SearchTool 的状态为 ✅，刷新"当前优先级"列表（3/4/5 标记完成）。

**A → B 因果链：**
- 因为 CI 3 条测试失败阻塞了 master 分支 → 必须先修复才能继续推进任务 3/4/5。
- 因为新非 Python 样本 _id 全部相同导致去重丢弃 260 条 → 补齐逻辑从 98% Python 的池子中采样，把非 Python 又填回了 Python → 语言分布未改善 → 需在 _id 中加入唯一索引。
- 因为 `build_coverage_matrix` 使用固定 `random.Random(SEED)` 确定性生成 → `regenerate_empty` 可以按位置精确匹配原始参数，无需在数据集中存储参数副本。
- **追加修复：跨环境快照基线污染问题 (ef1ab64)**：
  首次 push (fc502d2) 后 CI 仍然 3 errors，邮件告警未消除。排查过程：
  1. 本地修复时删除了过时的快照文件，测试重新运行后自动生成了新快照（issue_count=27）
  2. 这个新快照在 `git add` 时被一并提交到了仓库（fc502d2）
  3. CI 环境 checkout 代码后，快照文件内容为 `issue_count=27`，但 CI 的 `HEAD~3` 指向的 commit 与本地不同（浅克隆 + 不同 clone 时间点）
  4. CI 上实际跑出来的 issue 数量 ≠ 27 → ratio < 0.5 → 测试再次失败
  根本原因：快照文件依赖 `HEAD~N` 相对引用，本质上是环境相关的产物，不应进入版本控制。提交它就等于把"我机器的检测结果"当成"所有机器的正确答案"。
  修复：`pipeline_head_snapshot.json` 加入 `.gitignore` 并从 git 追踪中移除（`git rm --cached`），各环境首次运行时自行生成基线。本地多次运行之间仍保留回归检测能力，CI 则退化为"只生成不比对"的安全模式。
- 因为 `build_coverage_matrix` 使用固定 `random.Random(SEED)` 确定性生成 → `regenerate_empty` 可以按位置精确匹配原始参数，无需在数据集中存储参数副本。

---

## 2026-07-13 — M4 最终评测集（700 条版控数据集 + Agent 评测）

在 V1.1 Investigation Agent 和 CI/CD 完成后，将评测数据集从 10 条手工样本扩充到 700 条，覆盖 Review Pipeline 和 Investigation Agent 的全能力评估。

**1. 评测数据集生成器（`app/pipeline/eval_generator.py`）：**
- `build_coverage_matrix()` — 分层采样：Python 低风险/单风险/双风险/多风险/JS+TS/Java+Go/非代码/混合/边界共 9 类场景。
- `generate_texts_batch()` — 批量调 LLM API（20 条/次）生成 change_summary + ast_summary，28 批次完成 550 条。
- `compute_ground_truth(params)` — **关键设计改进：ground truth 直接从覆盖矩阵参数计算，不依赖文件内容扫描**。避免"参数→假数据→扫描假数据→标准答案"链路的 Bug。参数同时驱动样本生成和标准答案生成。
- `generate_agent_samples()` — 确定性生成 150 条 Agent 样本（locate/explain/trace/grep 各约 35-42 条）。
- `compute_coverage_report()` — 自动统计各维度分布和组合覆盖。
- `save_dataset()` — 版本化保存 JSON + 元数据（git_commit/coverage/prompt_version）。

**2. 数据集更新（`app/pipeline/eval_dataset.py`）：**
- `load_samples(dataset_version="latest")` — 新增 `dataset_version` 参数，"v1" 强制使用 10 条手工样本，"latest" 优先加载 v2 JSON。
- 新增 `InvestigationEvalSample` — Agent 模式样本（question/ground_truth）。
- 向后兼容：所有旧测试通过 `dataset_version="v1"` 保持原有行为。

**3. 评测基准扩展（`app/pipeline/eval_benchmark.py`）：**
- `--mode review|agent|all` — 支持分别评测 Review 和 Agent。
- `run_agent_benchmark()` — Agent 评测：Question Type Accuracy + Keyword Precision/Recall/F1。
- 输出中显示 dataset_version 和 git_commit。

**4. 数据集 JSON（`tests/__snapshots__/`）：**
- `eval_dataset_v2.json` — 700 条样本（550 Review + 150 Agent）。
- `eval_dataset_v2_meta.json` — 元数据（版本/commit/覆盖率报告）。
- Review 分布：low=81 / medium=344 / high=125；覆盖 5 种 Analyzer 组合。
- Agent 分布：locate=42 / explain=39 / trace=35 / grep=34。

**5. 测试更新（+2 条，172→174）：**
- `test_m4_eval.py` — `TestEvalDataset` 新增 `test_v2_samples_have_required_fields`（v2 数据集结构校验）。
- `test_golden.py`、`test_m4_eval.py` — 旧测试改用 `dataset_version="v1"` 保持原断言。

**A → B 因果链：**
- 因为 `_make_fake_changeset` 只生成文件名不生成内容，`_scan_risks_fast` 只能检测 dependency_change，导致 550 条样本全部退化为 low risk + git only → 改为从参数直接计算 ground truth，参数同时驱动样本文本生成和标准答案。
- 因为 v2 JSON 文件存在后 `load_samples()` 自动加载 700 条，旧测试中硬编码 10 条的断言失效 → 新增 `dataset_version` 参数，旧测试显式指定 "v1"。

## 2026-07-13 — V1.1 Investigation Agent + CI/CD

在 M5.1 质量工程层完成之后，按计划书实施 V1.1 代码库探索模式（Investigation Agent）和 GitHub Actions 自动测试。

**1. Investigation Agent（`app/agent/`）：**
- `investigator.py` — 核心调查 Agent：`InvestigationAgent.investigate(repo_path, question)` 执行 git grep → 解析结果 → 收集 Evidence → 读取关键文件 → LLM 合成答案。LLM 不可用时用 grep 原始结果兜底。
  - `InvestigationResult` — 调查产出 dataclass（question/answer/evidence/files_visited/findings/trace/duration_ms + to_dict）。
  - `_classify(question)` — 问题类型识别（locate/explain/trace/grep），正则匹配。
  - `_extract_keywords(question)` — 关键词提取（引号 > 驼峰/下划线标识符 > 英文单词回退）。
- `__init__.py` — 导出 `InvestigationAgent` 和 `InvestigationResult`。
- agent 设计原则：受限工具选择 + 证据累积 + 引用式回答，LLM 只用于最终合成。

**2. CLI 扩展（`app/cli.py`）：**
- 新增 `python -m app.cli investigate <repo> "<question>"` 子命令，输出带文件路径和行号的答案。
- 更新帮助文档。

**3. API 扩展（`app/api/`）：**
- `schemas.py` — 新增 `InvestigateRequest`（repo_path/question）和 `InvestigateResponse`。
- `routes.py` — 新增 `POST /investigate` 端点，运行 InvestigationAgent 返回带证据的答案。

**4. CI/CD（`.github/workflows/test.yml`）：**
- 3 个 job：`test`（单元+集成，Python 3.10/3.11 矩阵）、`golden`（黄金基线测试）、`recovery`（容错验证）。
- `fetch-depth: 5` 确保 git history 可用于测试。

**5. 测试（`tests/test_agent.py`，18 条）：**
- `TestClassify`（6 条）— 中英文问题类型识别。
- `TestExtractKeywords`（5 条）— 引号/驼峰/下划线/回退/空关键词。
- `TestInvestigateWithMockLLM`（7 条）— 找得到结果/找不到结果/LLM 崩溃兜底/空关键词提示/结果序列化/Evidence 结构/文件数上限。全部使用 mock LLM，不上网。

**A → B 因果链：**
- 为了支持 `POST /investigate` 端点，需要在 `routes.py` 模块顶层实例化 `InvestigationAgent()`，因此将其放在 `store`/`pipeline` 同级的模块全局作用域。
- 为了在 Windows 上正确解析 grep 输出，去掉了 `git grep --heading` 标志，改用标准 `file:line:content` 格式解析（`--heading` 在 Windows 上的输出格式不一致）。

## 2026-07-13 — 阶段六(M5) 服务化与演示

在 M4 评测体系之上完成最小服务化：FastAPI 接口、CLI 命令行、JSON 文件持久化、Docker 容器化。

**1. API 层（`app/api/`）：**
- `__init__.py` — `create_app()` FastAPI 应用工厂。
- `schemas.py` — Pydantic 请求/响应模型：`ReviewRequest`、`ReviewResponse`、`RunSummary`、`RunListResponse`、`ErrorResponse`（统一 `{"error": {"code": "...", "message": "..."}}` 格式）。
- `routes.py` — 4 个端点：`POST /review`（提交审查→运行 Pipeline→持久化）、`GET /review/{run_id}`（查询，404 当不存在）、`GET /runs`（历史列表）、`GET /health`（健康检查）。统一 HTTPException 异常处理。

**2. CLI（`app/cli.py`）：**
- `python -m app.cli review <repo> --base --head --output --json` — 命令行审查，输出 Markdown + 可选 JSON。
- `python -m app.cli serve --host --port` — 启动 uvicorn API 服务。

**3. 持久化（`app/persistence/store.py`）：**
- `RunStore` — JSON 文件存储到 `runs/` 目录（save/load/list/delete），按 run_id 索引。
- `RunRecord` — 运行摘要 dataclass；list 时跳过损坏文件。

**4. Docker 支持：**
- `Dockerfile` — python:3.11-slim + git/ruff/bandit + API 服务。
- `docker-compose.yml` — 一键启动，挂载 `runs/` 数据卷和 `.env` 密钥。
- `requirements.txt` — 完整依赖清单（fastapi/uvicorn/pydantic/openai/tenacity/python-dotenv/ruff/bandit/chromadb）。

**5. 配置更新：**
- `.gitignore` — 新增 `runs/` 忽略规则。

**6. 测试（新增 18 条，总计 118 条全绿）：**
- `tests/test_m5_api.py`（18）— Health check、创建 Review/默认 refs/坏仓库 500、按 run_id 查询/404、历史列表、端到端审查本项目、RunStore 存取往返/不存在/列表/删除/损坏文件跳过、Schema 默认值、CLI 集成。

**7. `docs/INDEX.md` / `.gitignore` 同步（规范一）：**
- 追加 api/、persistence/、cli.py、Dockerfile、docker-compose.yml、requirements.txt、test_m5_api.py 条目。
- 测试总数更新为 118 条。

**8. 依赖分析工具补完（`app/tools/dependency_tool.py`）：**
- `DependencyTool` — 实现 Tool 协议，分析 Python import 变更与依赖清单文件变更。
- `_extract_imports()` — AST 提取 import/from/from_relative 语句，过滤 Python 标准库。
- 产出两类 Finding：`EXTERNAL_IMPORT`（外部依赖引用）+ `DEP_FILE_CHANGED`（依赖清单文件变更）。
- `app/pipeline/executor.py` — 注册 dependency 到 `_TOOL_REGISTRY`，在 ruff/bandit 之后执行。
- `app/pipeline/plan_builder.py` — 规则 1b：依赖清单文件变更时自动加 dependency analyzer。
- `app/pipeline/eval_dataset.py` — s009 ground truth 更新为含 dependency。
- 测试新增 11 条，总计 129 条全绿。

---

## 2026-07-13 — M5.1 可观测性 + 容错增强 + 黄金回归测试

在 M5 服务化之上补齐企业级质量保障：容错隔离、黄金基线、回归快照、性能分解。

**1. 容错增强（`app/pipeline/executor.py`）：**
- `_safe_call()` — 每个工具调用统一通过此方法，try/except 隔离，单工具崩溃不中断 Pipeline。
- 崩溃自动转 failed ToolResult + Diagnostic（含 traceback snippet）。
- `_collect()` — 仅成功时合并 findings/evidence。
- **验收：注入任意工具崩溃，Pipeline 仍完成，其余工具产出不变。**

**2. 黄金结果测试（`tests/test_golden.py`，6 条）：**
- `pytest -m golden` — 评测集 PlanBuilder 准确率基线。
- 每条样本 analyzer F1 ≥ 0.5，平均 F1 ≥ 0.75。
- 高风险样本（安全 reason_code）强制包含 bandit，不可遗漏。
- 提供英文关键词内容时可正确识别风险等级。
- **验收：AI 项目升级模型/Prompt 后立即发现退化。**

**3. 回归快照测试（`tests/test_golden.py`，含 regression marker）：**
- `pytest -m regression` — 固定 commit 范围 Pipeline 快照对比。
- Issue 数不骤降（ratio ≥ 0.5）、Analyzer 集合不缩小、同一输入幂等一致。
- 快照存储于 `tests/__snapshots__/`。
- **验收：改代码后自动对比，防止引入无声退化。**

**4. 容错测试（`tests/test_pipeline_recovery.py`，10 条）：**
- 单工具崩溃（bandit/ruff/ast/dependency）→ Pipeline 不崩。
- 全部静态工具崩溃 → git 产出不变、report 仍生成。
- 工具返回 failed 状态 → trace 记录失败、未崩溃工具正常。
- git 失败 → 返回输出（ChangeSet 为空）。
- 未知工具 → 不阻塞已知工具。
- 失败工具耗时仍记录。
- **验收：Pipeline 永不因单点故障中断。**

**5. 性能基准 + 可观测性（`tests/test_performance.py`，9 条 + `app/pipeline/observability.py`）：**
- `PipelineTimeline` — 逐阶段耗时/状态/产出计数（success_count/failure_count/bottleneck）。
- `StageMetric` — 单阶段度量。
- `build_timeline()` — 从 trace + tool_results 构建结构化性能分解。
- `ascii_bar()` — ASCII 柱状图（面试展示用）。
- `ReviewOutput.timeline` — 每次审查自动生成。
- `pytest -m perf` — 快速性能验证（Timeline 生成/阶段覆盖）。
- `pytest -m slow` — 性能基准断言（Git ≤5s / Pipeline ≤30s）。
- **验收：每次审查可回答"哪个阶段最慢、哪个工具崩了、产出多少 Finding"。**

**6. `docs/INDEX.md` / CHANGELOG / pytest.ini 同步：**
- 追加 observability.py / test_pipeline_recovery.py / test_golden.py / test_performance.py 条目。
- 注册 pytest markers: golden, regression, slow, perf。
- 测试总数更新为 154 条。

**M5.1 验收对照：**
- Pipeline Timeline 可在面试中展示各阶段耗时分布。
- 容错：单工具崩溃 → Pipeline 完成率 100%（10/10 条 recovery 测试全绿）。
- 黄金：10 条样本 PlanBuilder 平均 F1 ≥ 0.75，高风险 bandit 召回率 100%。
- 回归：Pipeline 幂等一致性 + 快照防退化。
- 性能：Git ≤5s，Pipeline ≤30s（对 HEAD~2 范围）。

---

**M5 验收对照：**
- API 错误使用统一 schema（ErrorResponse `{"error": {"code": "...", "message": "..."}}`）。
- 审查运行可查询（GET /review/{run_id}、GET /runs）。
- 敏感配置不入库（.env 已 gitignore，docker-compose 只读挂载）。
- 命令行可发起审查并查看结果（`python -m app.cli review .`）。
- 一条命令启动服务（`docker-compose up` 或 `python -m app.cli serve`）。
- 评测基准可复现（M4 的 `python -m app.pipeline.eval_benchmark`）。

---

## 2026-07-13 — 阶段五(M4) 评测体系与微调 Planner 基线

在 M3 闭环之上建立离线评测能力：手工标注数据集 + 指标计算 + LLM Planner vs 规则基线对比基准。

**1. 评测数据集（`app/pipeline/eval_dataset.py`）：**
- `EvalSample` dataclass — id/scenario/input/ground_truth，字段对齐计划书 M4 微调任务定义。
- 10 条手工标注样本（s001—s010），覆盖：简单 Python、认证变更、SQL 注入、命令注入、多风险叠加、非 Python 文件、大规模 diff、反序列化、依赖变更、空变更。
- `load_samples()` / `to_json()` — 加载与序列化。

**2. 评测指标（`app/pipeline/eval_metrics.py`）：**
- `EvalMetrics` dataclass — analyzer precision/recall/F1、risk_level_accuracy、high_risk_recall（bandit 在高风险样本中的召回）、reason_codes precision/recall/F1。
- `compute(predictions, ground_truths)` — 聚合计算，含逐样本 per_sample 明细。
- 高风险定义为 ground truth reason_codes 含 {auth_change, command_injection, sql_risk, deserialization} 或 risk_level=="high"。

**3. 评测基准（`app/pipeline/eval_benchmark.py`）：**
- `run_llm_planner()` — 调用 `llm_tool.chat()` 为每条样本生成 ReviewPlan 预测（结构化 prompt + JSON 解析 + markdown 包裹容错）。
- `run_rule_baseline()` — 使用 `RuleBasedPlanBuilder` 生成基线预测。
- `run_benchmark(top_n)` — 完整双轨评测流程 → `BenchmarkResult`，含 `summary()` Markdown 对比报告。
- CLI 支持：`python -m app.pipeline.eval_benchmark --top 3 --json`。

**4. 测试（新增 30 条，总计 100 条全绿）：**
- `tests/test_m4_eval.py`（30）— 全部 mock LLM，不上网：数据集加载/字段校验/序列化往返、集合指标函数、EvalMetrics 完美匹配/空输入/错配/风险等级/高风险召回/逐样本明细/to_dict、规则基线 10 条/非 Python 只有 git/空变更、LLM 解析器 JSON/markdown/非法 JSON、Benchmark mock 集成/JSON 解析失败降级/异常处理/全量流程/summary 生成、Prompt 构造。

**5. `docs/INDEX.md` 同步（规范一）：**
- 追加 eval_dataset.py / eval_metrics.py / eval_benchmark.py / test_m4_eval.py 条目。
- 测试总数更新为 100 条。

**M4 验收对照：**
- 有版本化数据集：eval_dataset.py 含 10 条手工标注样本。
- 有基线：RuleBasedPlanBuilder 作为确定性基线。
- 有评测脚本与报告：eval_benchmark.py 产出 LLM vs 基线对比报告。
- 有指标：Analyzer F1、高风险工具召回、风险等级准确率均已计算。
- 全部 mock LLM 测试，不依赖网络。

---

## 2026-07-13 — 阶段四(M3) LLM 增量语义审查

在 M2 确定性管道上叠加可选的 LLM 语义审查层，只把静态工具无法判定的问题交给 LLM。

**1. KnowledgeRetriever（`app/pipeline/knowledge_retriever.py`）：**
- `KnowledgeRetriever(Protocol)` — 可插拔知识检索器协议：`retrieve(query, top_k)`。
- `NullRetriever` — 空检索器（默认降级策略，RAG 不可用时不影响 Pipeline）。
- `StaticKnowledge` — 内置 7 条静态知识（OWASP/PEP8/Google Style Guide），关键词匹配检索，条目带 source/version/license。

**2. LLMReviewer（`app/pipeline/llm_reviewer.py`）：**
- `call_llm` 通过依赖注入，不绑定特定 LLM SDK（方便测试与切换模型）。
- 结构化 prompt：系统级指令 + 最小必要上下文（diff/symbols/static_findings）。
- JSON schema 校验：必填字段 location/reason/suggestion；evidence_ids 若给则必须关联已知 evidence。
- 低置信度降级：confidence < 0.6 → severity 强制降为 "info"。
- 重试：最多 2 次重试；全部失败后产出失败 Evidence 而非抛异常。
- `_extract_json()` — 容忍 markdown 代码块包裹/前后杂文本。

**3. ReviewPipeline 集成（`app/pipeline/review_pipeline.py`）：**
- 构造时可选注入 `llm_reviewer`；为每个变更的 Python 文件（最多 10 个）调用 LLM 审查。
- LLM 产出 Finding + Evidence 合并到静态结果；LLM 失败不丢失静态结果（M3 验收）。
- trace 记录每文件 LLM 审查步骤。

**4. 测试（新增 9 条，总计 70 条全绿）：**
- `tests/test_m3_llm.py`（9）— mock LLM 覆盖：合法输出/空输出/非法 JSON/缺字段被拒/低置信度降级/NullRetriever/StaticKnowledge/Pipeline 集成/LLM 失败保留静态结果。
- 全部 mock，不依赖网络或真实 LLM。

**5. `docs/INDEX.md` 同步（规范一）：**
- 追加 knowledge_retriever.py/llm_reviewer.py/test_m3_llm.py 条目。
- 测试总数更新为 70 条。

**M3 验收对照：**
- LLM 结论可回链证据：每条 LLM Finding 带 evidence_ids（含自产 Evidence id）。
- schema 异常有可观测降级：缺字段/非法 JSON → 产出失败 Evidence，不生成高严重度 Issue。
- LLM 失败时静态结果保留：test_pipeline_static_results_preserved_on_llm_failure。

---

## 2026-07-13 — 阶段三(M2) 固定 ReviewPlan 与高可信审查闭环

紧接阶段二(M1)，实现规则式计划生成、确定性执行、去重聚合与结构化报告。

**1. RuleBasedPlanBuilder（`app/pipeline/plan_builder.py`）：**
- 基于变更特征确定性选择工具：Python 文件 → ruff+AST（≤50 文件时）；风险信号 → bandit 强制启用。
- `_RISK_PATTERNS` — 5 类风险信号（auth/sql/command_injection/deserialization/dependency_change）→ reason_code。
- 最低安全策略：高风险信号时 bandit 不可跳过。
- 同输入 → 同计划（test_deterministic_same_input）。

**2. ReviewExecutor（`app/pipeline/executor.py`）：**
- 按 ReviewPlan 执行工具链，`_run_step()` 上下文记录 TraceEntry。
- 任何工具失败不中断其余工具（降级）。
- 复用 `_TOOL_REGISTRY` 映射工具名→实例。

**3. Aggregator（`app/pipeline/aggregator.py`）：**
- 按 (file, rule_id) 分组去重 Finding → Issue。
- 同组保留全部 evidence_ids，message 去重拼接。
- 严重度取组内最高。

**4. ReportGenerator（`app/pipeline/report.py`）：**
- `markdown()` — 4 章结构：Change Summary / Execution Trace / Issues（带证据引用）/ Plan Details。
- `json_report()` — 完整 JSON（change_set/plan/trace/issues/evidence）。

**5. ReviewPipeline（`app/pipeline/review_pipeline.py`）：**
- `run(repo_path, base_ref, head_ref)` 一行完成 Plan→Execute→Aggregate→Report。
- 端到端验证：HEAD~2..HEAD 在 2.4s 内产出 3 Issues + 72 Evidence + 完整 Markdown/JSON 报告。

**6. 测试（新增 12 条，总计 61 条全绿）：**
- `tests/test_m2_pipeline.py`（12）— PlanBuilder/Aggregator/ReportGenerator 单测 + ReviewPipeline 集成测试 + 幂等性 + 空 diff。
- 新增覆盖：Plan 确定性、风险信号检测、去重逻辑、报告完整性、端到端幂等。

**7. `docs/INDEX.md` 同步（规范一）：**
- 追加 plan_builder.py/executor.py/aggregator.py/report.py/review_pipeline.py/test_m2_pipeline.py 条目。
- 测试总数更新为 61 条；`app/pipeline/` 移出骨架待补齐。

**M2 验收对照：**
- 同一输入得到相同计划与结果：test_deterministic_same_input + test_idempotent 覆盖。
- 报告展示变更摘要、执行 trace、Issue 证据与来源：test_markdown_contains_sections 覆盖全部章节。
- 规则问题不因 LLM/RAG 不可用而丢失：PlanBuilder 纯规则、Executor 纯确定性工具，零 LLM 调用。

---

## 2026-07-13 — 阶段二(M1) 确定性事实层：WorkspaceManager + GitTool + ASTTool + RuffTool + BanditTool + FactCollector

依据计划书 M1 规格，实现第一批确定性工具及编排器，让系统在不调 LLM 的情况下收集可信的变更、符号和静态事实。

**1. WorkspaceManager（`app/core/workspace.py`）：**
- 新增 `WorkspaceConfig` — 约束配置：allowed_extensions（.py/.pyi/.pyx）、max_file_bytes（2MB）、max_files（500）。
- 新增 `Workspace` — 隔离工作区实例：`list_files()` 列出合规文件、`read_file()` 只读（含路径越界检查）、`cleanup()` 清理临时目录。
- 新增 `WorkspaceManager` — `prepare(repo_path, head_ref)` 用 `git archive` 导出目标 ref 的快照到临时目录；对非 git 仓库报错。

**2. GitTool（`app/tools/git_tool.py`）：**
- 实现 `Tool` 协议，解析 `git diff --name-status`/`--numstat`/`--unified`。
- 产出 `ChangeSet`（含 FileChange + Hunk 行号映射）+ 逐文件 Evidence。

**3. ASTTool（`app/tools/ast_tool.py`）：**
- 实现 `Tool` 协议，基于内置 `ast` 模块提取函数/类/导入及调用边。
- `_SymbolVisitor` walk AST 产出 Symbol 列表；SyntaxError 不崩溃，记录 parse error evidence。

**4. RuffTool（`app/tools/ruff_tool.py`）：**
- 实现 `Tool` 协议，对指定文件跑 `ruff check --output-format json`。
- 每条诊断转为 Finding（带 rule_id/位置/evidence_ids）+ Evidence。

**5. BanditTool（`app/tools/bandit_tool.py`）：**
- 实现 `Tool` 协议，对指定文件跑 `bandit -f json`。
- 每条安全报警转为 Finding（severity 做 LOW/MEDIUM/HIGH 映射）+ Evidence。

**6. FactCollector（`app/pipeline/fact_collector.py`）：**
- 编排 WorkspaceManager → GitTool → ASTTool → RuffTool → BanditTool。
- `FactCollection` 汇总 change_set/symbol_index/findings/evidence/tool_results。
- `collect(repo_path, base_ref, head_ref)` 一行调用完成全链收集。

**7. 安装 ruff + bandit（pip install）：**
- 新增依赖 `ruff` 和 `bandit`（Python 静态分析工具）。

**8. 测试（新增 23 条，总计 49 条全绿）：**
- `tests/test_workspace.py`（5）— WorkspaceManager 单测。
- `tests/test_git_tool.py`（4）— GitTool 单测。
- `tests/test_ast_tool.py`（4）— ASTTool 单测。
- `tests/test_static_tools.py`（6）— RuffTool + BanditTool 单测。
- `tests/test_fact_collector.py`（4）— 端到端集成测试。

**9. `docs/INDEX.md` 同步（规范一）：**
- 追加 workspace.py、git_tool.py、ast_tool.py、ruff_tool.py、bandit_tool.py、fact_collector.py 及其测试条目。
- 测试总数更新为 49 条；`app/pipeline/` 移出骨架待补齐。

**为什么改：**
- 阶段二(M1) 要求系统不依赖 LLM 即可产出变更、符号和静态发现。本段工作按计划书顺序从 WorkspaceManager 开始，逐工具实现，最后用 FactCollector 串联。端到端验证通过：HEAD~2..HEAD 在 2 秒内收集 24 变更文件、149 符号、35 静态发现、72 条证据。

**M1 验收对照：**
- fixture 覆盖新增/修改/删除文件 + 空 diff：test_git_tool 覆盖。
- Finding 100% 带路径、行号与规则 ID：test_fact_collector 验证。
- 不执行目标仓库代码：WorkspaceManager 用 git archive 只导出文件快照，不 run 任何目标代码。
- 纯确定性测试可离线运行：49 条测试均无网络依赖。

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

---

## 2026-07-12 — 规范零(中文输出) + 计划书最终流程图 + 阶段一(M0) 可追溯数据契约

本段工作含三部分：一条新规范、一份计划书附录、以及计划书「阶段一」的完整实现。

**1. 新增规范零（`CLAUDE.md`）：**
- 在 `CLAUDE.md` 顶部新增「规范零：输出语言」——面向用户的所有输出一律用中文（技术符号除外），优先级最高。
- 原因：用户为中文母语者，要求以后所有回复用中文，并写入 CLAUDE.md 长期生效。
- 因 A（加规范零）改 B：`CLAUDE.md` 开头「以下两条规范」改为「以下规范」；并同步 `docs/INDEX.md` 中 CLAUDE.md 条目为「规范零/规范一/规范二」。

**2. 计划书附录 A：项目最终流程图（`_PLAN/AI Code Review Platform — V1 完整任务计划书.md`）：**
- 在计划书末尾**追加**「附录 A：项目最终流程图（含各层用处）」——九层端到端流程图 + 各层用处速查表；应用户「只准添加、不许修改原文」的要求，未改动任何既有内容。
- 同一文件中另有用户本人新增的「M0—M5 的实施顺序、阶段交付与验收指标」表与「指标口径与简历表达」小节（非本助手所写，但随本次提交一并纳入，如实标注归属）。
- 同步 `docs/INDEX.md` 中该计划书条目（补「附录 A」「M0—M5 实施顺序/指标口径」描述）。

**3. 阶段一实现（M0 可追溯数据契约）——依据计划书「阶段 1」行：**
- 新增 `app/models/ids.py` — `new_id(prefix)` 生成稳定短 id（`ev_`/`fnd_`/`iss_`/`run_`）。
- 新增 `app/models/location.py` — `CodeLocation` / `Symbol`（含 `SYMBOL_KINDS`），一切事实的定位基础；`to_dict/from_dict`（Symbol 重建嵌套 location）。
- 新增 `app/models/change.py` — `ChangeSet` / `FileChange` / `Hunk`（含 `CHANGE_TYPES`），变更集契约；递归序列化往返。
- 新增 `app/models/evidence.py` — `Evidence`（含 `EVIDENCE_KINDS`），可引用事实原子，带 source/location/confidence/reference，默认 id `ev_*`。
- 新增 `app/models/finding.py` — `Finding`，工具候选发现，带 rule_id 与 `evidence_ids`，默认 id `fnd_*`。
- 新增 `app/models/plan.py` — `ReviewPlan`（含 `RISK_LEVELS`），字段与计划书 M4 微调 Planner 输出 JSON 对齐。
- 新增 `app/models/run.py` — `ReviewRun` + `TraceEntry`：按 id 索引的 evidence/findings 存储、`add_*`/`record`/`resolve_evidence`/`validate_traceability`、完整 `to_dict/from_dict`。作为运行级容器，逐步取代旧 `ReviewContext`（本次仅并行引入，未删除旧结构，保留兼容迁移路径）。
- 新增 `app/tools/contract.py` — Tool 统一契约：`ERROR_CODES`/`TOOL_STATUS`、`Diagnostic`、`ToolRequest`、`ToolResult`（`ok()`/`failure()`/序列化）、`Tool(Protocol)`。约束「工具失败返回结构化诊断、不抛业务异常」。
- 改动 `app/models/issue.py`：向后兼容追加 `id`（默认 `iss_*`）与 `evidence_ids` 两字段、新增 `from_dict`；顶部 `import app.models.ids.new_id`。既有位置参数构造与 `test_pipeline.py` 不受影响。
- 新增测试：`tests/test_data_contracts.py`(10) / `tests/test_tool_contract.py`(5) / `tests/test_review_run.py`(5)。`python -m pytest tests/ -q` → **25 passed**（原 5 + 新增 20）。
- 因 A（Finding 复用严重度取值）改 B 又回退：`finding.py` 起初 `from app.models.issue import SEVERITIES`，发现是未使用导入（将被 Ruff 判 F401），改为删除该 import、仅在注释保留「取值同 issue.SEVERITIES」。

**4. 阶段报告目录（新增 `docs/stages/`）：**
- 新增 `docs/stages/README.md` — 阶段进度总览表（阶段/里程碑/状态/报告链接），供最终成品回看每阶段交付。
- 新增 `docs/stages/stage-1-data-contract.md` — 阶段一报告：目标、完成能力、文件清单、设计决策、验收对照、测试结果、已知限制、下一阶段输入。
- 原因：用户要求「docs 保存每阶段 readme，最终成品能看见每阶段的完成与新增」。

**5. `docs/INDEX.md` 同步（规范一）：**
- 追加 `app/tools/contract.py`、7 个新模型文件、3 个新测试文件、`docs/stages/` 的条目；更新 `app/models/issue.py`（新增字段）与 `app/models/context.py`（标注为旧结构、逐步被 ReviewRun 取代）条目。
- 移除 `_PLAN/plan.md` 条目（见第 6 点）。

**6. 删除旧规划 `_PLAN/plan.md`：**
- 用户已将旧版 `_PLAN/plan.md`（Phase 1/2 更新日志，已被 V1 计划书取代）删除并暂存；随本次提交一并移除，并按规范一同步删掉其 INDEX 条目。

**未纳入本次提交（如实说明）：**
- 工作区出现未跟踪文件 `AGENTS.md`，内容为 `CLAUDE.md` 的旧副本（尚无规范零），疑为工具自动生成的跨 agent 指令镜像。因来源与用途待确认，本次**未提交**，留待与用户确认后处理（更新为镜像 / 纳入 gitignore / 删除）。

---

## 2026-07-12 — 清理旧原型 + 阶段一代码审查修复（分层/校验/文档一致性）

紧接上一条提交（c7326cc）之后的一段工作：先按"计划书用到的留、没用到的删"清理旧原型，再落实一轮对阶段一的代码审查意见。

**1. 删除旧原型（计划书不再使用的路径）：**
- 删除 `app/reviewers/multi_reviewer.py` 及整个 `app/reviewers/` 目录（含 `__init__.py`）——旧"多 Reviewer Prompt"审查路径，计划书§七明确不再扩大。
- 删除 `app/utils/code_lines.py`（`add_line_numbers`）——仅服务旧路径的行号工具，已成孤儿。
- 保留 `app/tools/llm_tool.py`、`app/retriever/knowledge_base.py`、`app/retriever/kb_seed.py`：计划书§七要求"适配为统一 Tool 契约后复用"，属计划书用到的能力，故不删。
- 已确认这些旧模块无人 import、删除后 `pytest` 不受影响。
- 关于 `AGENTS.md`：上一条遗留的未跟踪 `AGENTS.md` 本段已按用户指示删除（用户另有本地 Codex 接入，但以 CLAUDE.md 为唯一权威源，不保留镜像）。该文件从未纳入 git，无版本痕迹。

**2. `Diagnostic` 下沉到领域模型层（修复分层反向依赖）：**
- 新增 `app/models/diagnostic.py`，将 `ERROR_CODES` 与 `class Diagnostic` 从 `app/tools/contract.py` 迁入。
- 因 A（下沉 Diagnostic）改 B：`app/tools/contract.py` 删除本地 `ERROR_CODES`/`Diagnostic` 定义，改为 `from app.models.diagnostic import Diagnostic`；`app/models/run.py` 的 `Diagnostic` 导入也由 `app.tools.contract` 改为 `app.models.diagnostic`。
- 动机：修复前 `app/models/run.py` → `app/tools/contract.py` → `app/models/*`，构成"模型层反向依赖工具层"的分层污染（尚未成 Python 导入环，但会增加耦合）。改后依赖单向 `tools → models`；已 grep 确认 `app/models/` 下无任何 `app.tools` 引用。
- 因 A 改 B：`tests/test_tool_contract.py` 的 `ERROR_CODES`/`Diagnostic` 导入相应改为从 `app.models.diagnostic` 引入。

**3. `validate_traceability()` 增加 Finding 空证据判错：**
- `app/models/run.py`：原先只检查 Finding 的 evidence_ids 是否悬空，现补充"Finding 至少关联一条 Evidence"，与 Issue 一致。
- 动机：计划书 M1 验收明确"每一个静态 Finding 带…对应 Evidence"，空证据的 Finding 应判错。
- 新增 `tests/test_review_run.py::test_finding_without_evidence_is_flagged`（本阶段测试数 25 → 26）。

**4. 新增 `pytest.ini`：**
- 内容 `[pytest] testpaths = tests / addopts = -q`。将收集范围钉在 `tests/`，避免 pytest 向上走到 `E:\` 根目录、在部分沙箱因根目录权限被拦而无法收集。
- 应用户要求删除了其中的中文注释，保持纯 ASCII（规避 configparser 按本地编码读 .ini 时的非 ASCII 报错）。

**5. 计划书文档同步（`_PLAN/AI Code Review Platform — V1 完整任务计划书.md`）：**
- M0 标题由"领域模型与受控工作区"改为"领域模型与工具契约"；删去 workspace 交付项与"不可信仓库不会被导入或执行"验收项。
- M1 增加"受控工作区（随 GitTool 落地）"交付项，并把"不可信仓库不会被导入或执行（受控工作区保证）"并入 M1 验收。
- "M0—M5 实施顺序"表：阶段 2 的新增交付补上"受控工作区"。
- 动机：文档与已确认方案（workspace 推迟到阶段二）保持一致。

**6. `docs/INDEX.md` 同步（规范一）：**
- 移除已删的 `multi_reviewer.py`、`code_lines.py` 条目。
- 新增 `app/models/diagnostic.py`、`pytest.ini` 条目；更新 `app/tools/contract.py`（Diagnostic 改由 models 层导入）、`app/models/run.py`（validate 覆盖 Finding）、`tests/test_review_run.py` 条目。
- 给 `llm_tool.py`/`knowledge_base.py`/`kb_seed.py` 标注"⚠️ 旧能力，尚未接入新 Pipeline，非阶段一交付"。

**7. 阶段报告同步（`docs/stages/stage-1-data-contract.md`）：**
- 文件清单加入 diagnostic.py、pytest.ini；新增"旧能力非本阶段交付"说明；设计决策补第 5 条（模型层不依赖工具层）；验收表加 Finding 证据项；测试结果更新为 26 passed + compileall。
- 如实记录验证状态差异：本会话沙箱 26 passed + compileall 通过；Codex 沙箱因 E:\ 根权限只跑通 compileall、未能独立复现 pytest 绿色，建议本机/CI 补一次正式测试记录。

**测试：** `python -m pytest` → 26 passed；`python -m compileall app tests` 通过。

---

## 当前节点（2026-07-17）

### 项目整体完成度

| 里程碑 | 状态 |
|--------|------|
| M0 数据契约 | ✅ |
| M1 工具层 (Git/AST/Ruff/Bandit/Dependency/Search) | ✅ |
| M2 固定审查管道 (PlanBuilder→Executor→Aggregator→Report) | ✅ |
| M3 LLM 语义审查器 + 知识检索 | ✅ |
| M4 评测体系 + 700 条版控数据集 | ✅ |
| M5 服务化 (FastAPI + CLI + Docker) | ✅ |
| M5.1 质量工程 (容错/黄金/回归/性能) | ✅ |
| V1.1 Investigation Agent M1/M2/M3 (假设驱动状态机 + 预算 + 续问) | ✅ |
| V1.1 Agent 真实评测集 (46 问题 + 5 指标框架) | ✅ |

**测试**: 259 条全绿 (`python -m pytest tests/ -q -m "not slow and not golden"`)；Agent 专项 71 + Agent 评测 23 = 94 条。

### 下次继续入口

1. **真实 LLM 跑 Agent 评测基线** — `python -m app.pipeline.agent_eval_runner --top 10`（有 API key 时），产出首批 5 项指标基线数据
2. **评测真值校准** — 人工校验 ground truth（先 50 条）+ 外部真实项目样本
3. **微调 Planner** — 刻意放最后：规则 Planner 已有效，先做 Agent 增强更划算

### 当前仓库状态

- **分支**: master
- **已 push**: 否（本段 M3 CHANGELOG 待 push）
- **数据集**: `tests/__snapshots__/eval_dataset_v2.json` (700 条)

