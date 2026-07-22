# V1 计划任务跟踪表

对照 `_PLAN/AI Code Review Platform — V1 完整任务计划书.md` 逐项跟踪完成状态。更新日期：2026-07-21。

**当前状态定义：V22 Task 驱动探查架构已实现；待编写新测试 + 集成 + 外部评测回归。**

## V22 Task 驱动探查（2026-07-21）

| 改动 | 状态 | 说明 |
|------|------|------|
| InvestigationTask 增加 role/subtree_depth/strategy_override 等 8 字段 | ✅ | `app/models/target.py` |
| ExplorationState（三阶段独立预算 + 任务树 + 证据可信链） | ✅ | `app/agent/task_explorer.py` |
| ToolExecutor（从 EvidenceClosureEngine 提取工具执行） | ✅ | `app/agent/task_explorer.py` |
| fill_work_orders / _deterministic_work_orders | ✅ | `app/agent/task_explorer.py` |
| discover_tasks（从 verified evidence 动态发现） | ✅ | `app/agent/task_explorer.py` |
| gap_analyzer / _deterministic_gap_fill / _GAP_STRATEGIES | ✅ | `app/agent/task_explorer.py` |
| _execute_task / _execute_task_subtree | ✅ | `app/agent/task_explorer.py` |
| EvidenceClosureEngine 删除（~725 行），保留核心类型 | ✅ | `app/agent/evidence_closure.py` |
| Investigator 6 阶段主流程重写（~3200→~700 行） | ✅ | `app/agent/investigator.py` |
| InvestigationState 删除 + ~25 legacy 方法清理 | ✅ | `app/agent/investigator.py` |
| __init__.py 适配 V22 导出 | ✅ | `app/agent/__init__.py` |
| test_agent.py 适配（69 passed） | ✅ | `tests/test_agent.py` |
| test_evidence_closure.py 跳过标志（85 skipped） | ✅ | `tests/test_evidence_closure.py` |
| **task_explorer.py 单元测试（88 条）** | ✅ | 预算(9)+调度(7)+去重(4)+证据链(4)+fill_work_orders(6)+deterministic_work_orders(3)+discover_tasks(6)+gap_analyzer(5)+deterministic_gap_fill(6)+_execute_task(5)+_execute_task_subtree(4)+_verify_evidence(11)+stop_reason(5)+retool(3)+ToolExecutor(6)+contract(2)+regression(4) |
| **test_evidence_closure.py 迁移到 V22** | ✅ | 45 条活跃测试，保留函数 + ToolExecutor 集成 + V22 删除确认 |
| **V22 Q&A 评测（4 Fix 后 46 样本 Semantic Judge）** | ✅ | 2026-07-21：any-correct 87.0%(40/46)、correct 34.8%(16/46)、partially_correct 52.2%(24/46)、incorrect 8.7%(4/46)、unjudgeable 4.3%(2/46)、budget_exceeded 0%、evidence_retrieval 91.3%、Judge 100% 有效 |
| **外部评测回归（63 样本 V21 vs V22）** | ⬜ | |
| **docs/INDEX.md 更新** | ✅ | 2026-07-21 |
| **docs/CHANGELOG.md 更新** | ✅ | 2026-07-21 |
| **_PLAN/plan_status.md 更新** | ✅ | 2026-07-21 |

## V21 Agent 评测增强（2026-07-21）

| 改动 | 状态 | 说明 |
|------|------|------|
| Task 关联问题类型 + ClosureState.question_type | ✅ | locate/grep/explain/trace/impact 贯穿全流程 |
| 按问题类型差异化搜索路线 | ✅ | explain 读 helper 链；impact 扩依赖搜索；窗口大小按类型调整 |
| Evidence 按 Target 分组合成 | ✅ | 每个 target 独立展示已确认/仍缺少/相关代码 |
| 按问题类型最低 COMPLETE 条件 | ✅ | `check_minimum_evidence_contract()` 控制终止判定 |
| 分类修复 `calls?` 误匹配 `__call__` | ✅ | explain 增加「如何/怎么/怎样」模式 |

## M0 领域模型与工具契约

| 子任务 | 状态 | 备注 |
|--------|------|------|
| ChangeSet / FileChange / CodeLocation / Symbol 模型 | ✅ |
| Evidence / Finding / Issue / ReviewPlan / ReviewRun 模型 | ✅ |
| ToolRequest / ToolResult / Diagnostic 统一契约 | ✅ |
| Tool(Protocol) 协议定义 | ✅ |
| ReviewContext → ReviewRun 迁移（保留兼容路径） | ✅ |
| Issue 至少关联一条 Evidence（可追溯性校验） | ✅ |
| 工具失败返回结构化诊断（不抛业务异常） | ✅ |

## M1 确定性事实层

| 子任务 | 状态 | 备注 |
|--------|------|------|
| 受控工作区（WorkspaceManager） | ✅ |
| GitTool（ChangeSet + Hunk 行号映射） | ✅ |
| PythonParserTool / ASTTool（符号提取） | ✅ |
| RuffTool（代码风格） | ✅ |
| BanditTool（安全扫描） | ✅ |
| DependencyTool（import 变更 + 依赖清单） | ✅ |
| **SearchTool（只读代码/符号搜索）** | ✅ | 2026-07-14 完成，实现 Tool 协议，支持 grep + 文件名搜索 |
| fixture 覆盖新增/修改/删除/空 diff | ✅ |
| 静态 Finding 100% 带路径/行号/规则 ID | ✅ |
| 不执行目标仓库代码（受控工作区保证） | ✅ |

## M2 固定审查闭环

| 子任务 | 状态 | 备注 |
|--------|------|------|
| RuleBasedPlanBuilder（规则式计划生成） | ✅ |
| 最低安全策略（高风险信号强制 bandit） | ✅ |
| ReviewExecutor（执行计划 + trace + 降级） | ✅ |
| Aggregator（按 file+rule_id+line 去重） | ✅ | 2026-07-16 粒度细化：同规则不同行各自成 Issue，对齐主流工具报告粒度 |
| ReportGenerator（Markdown + JSON） | ✅ |
| 同输入→同结果（确定性幂等） | ✅ |
| 规则问题不因 LLM/RAG 不可用而丢失 | ✅ |

## M3 LLM 语义审查

| 子任务 | 状态 | 备注 |
|--------|------|------|
| 结构化 LLM 请求（最小 diff + 符号 + 静态发现） | ✅ |
| LLM 输出含 location/reason/suggestion/evidence_ids | ✅ |
| Schema 异常降级（缺字段/非法 JSON → 低置信度） | ✅ |
| 低置信度强制降级为 info | ✅ |
| 知识库改为可插拔 KnowledgeRetriever(Protocol) | ✅ |
| NullRetriever（默认降级策略） | ✅ |
| StaticKnowledge（内置 7 条 OWASP/PEP8） | ✅ |
| RAG 检索失败不阻塞 Pipeline | ✅ |
| LLM 结论可回链证据 | ✅ |
| LLM 失败时静态结果保留 | ✅ |
| **旧 Chroma 知识库适配为统一 Tool 契约** | ❌ | 当前仍是旧能力，未接入新 Pipeline |
| **真实模型跑 benchmark（LLM 增量检出/误报/耗时/token）** | ✅ | 2026-07-16 完成：GLM-4.5-Air 真实调用，20 固定样本 static vs llm 两组 + 独立 LLM-as-Judge。llm 相对 static F1 +7.8pp（85.62% vs 77.78%），验证 LLM 补足静态盲区 |

## M4 评测体系与微调 Planner

| 子任务 | 状态 | 备注 |
|--------|------|------|
| 版本化评测数据集（eval_dataset.py） | ✅ | v2: 700 条 |
| 覆盖矩阵生成器（eval_generator.py） | ✅ |
| 评测指标计算（eval_metrics.py） | ✅ |
| 评测基准脚本（LLM vs 规则基线） | ✅ |
| Agent 评测（Question Type + Keyword F1） | ✅ |
| **人工校验 ground truth（50 条）** | ⬜ | V1 收尾三缺口之一：真值仍来自合成样本 + LLM-as-Judge，缺独立人工/外部真实项目校准。当前指标只能证明"系统内对比提升"，不得表述为真实生产准确率 |
| **人工校验 ground truth（全量 700 条双层校验）** | ⬜ | 待定 |
| **LLM 生成器 JSON 解析加固** | ✅ | 三层兜底：整体解析→正则逐对象→空占位 |
| **非 Python 语言覆盖扩展** | ✅ | 2026-07-14 完成，Python 50% / JS 14% / TS 10% / Java 6% / Go 6% |
| **LLM 生成失败批补生成（~100 条 change_summary 为空）** | ✅ | 2026-07-14 完成，添加 --regenerate 命令，确定性匹配空样本并补生成 |
| **Report 级评测（Issue/Evidence/Suggestion 质量）** | ✅ | 2026-07-16 完成：eval_report/ 全链路（20 样本 → Pipeline → LLM-as-Judge → P/R/F1），证据链持久化（unified_diff 随结果固化，Judge 不回仓库取证） |
| **微调训练数据构建（从 trace 生成偏好数据）** | ⬜ | 刻意放最后：规则 Planner 已有效，先做 Agent 增强更划算 |
| **微调 Planner 模型训练** | ⬜ | 同上 |
| **微调 vs 规则基线对比实验** | ⬜ | 同上 |
| **shadow mode 上线策略实现** | ⬜ | |
| **下游 Issue precision/recall/严重漏报率评测** | ✅ | 2026-07-16 完成（LLM-as-Judge 口径）：static P 97.47% / R 64.71%；llm P 95.62% / R 77.51%；含 missed_high_severity 统计 |

## M5 服务化与演示

| 子任务 | 状态 | 备注 |
|--------|------|------|
| FastAPI 应用工厂 + 4 端点 | ✅ | /review /runs /health /investigate |
| 统一错误 schema（ErrorResponse） | ✅ |
| CLI（review / investigate / serve） | ✅ |
| JSON 文件持久化（RunStore） | ✅ |
| Dockerfile + docker-compose.yml | ✅ |
| 本地持久化（runs/ 目录） | ✅ |
| **最小前端 / 可视化界面** | ⬜ | 计划书列为 M5，定为 V2 |

## M5.1 质量工程（计划书外追加）

| 子任务 | 状态 | 备注 |
|--------|------|------|
| 容错隔离（单工具崩溃不中断 Pipeline） | ✅ |
| 黄金基线测试（analyzer F1≥0.5，平均≥0.75） | ✅ |
| 回归快照测试（Issue 数不骤降，幂等一致） | ✅ |
| PipelineTimeline 可观测性 | ✅ |
| 性能基准（Git≤5s，Pipeline≤30s） | ✅ |
| **测试稳定性重构（固定 Git Diff 输入）** | ✅ | 2026-07-14 完成，使用 tests/helpers.py + conftest.py fixture 替代 HEAD~N 相对引用 |

## V1.1 Investigation Agent + CI/CD（计划书外追加）

| 子任务 | 状态 | 备注 |
|--------|------|------|
| InvestigationAgent（grep→解析→读文件→LLM 合成） | ✅ |
| 问题分类（locate/explain/trace/grep） | ✅ |
| CLI + API 端点 | ✅ |
| GitHub Actions CI（test/golden/recovery） | ✅ |
| **多轮探索 UX** | ✅ | 2026-07-17 M3 完成：假设驱动有限状态调查循环 + 三维预算 + 跨工具关联链 + LLM 辅助排序 + 去重 + investigation_id/续问复用；grep 无命中会受限回退文件名搜索后才 NO_EVIDENCE。Agent 评测已补相对成本/加权节省率及三类预算临界测试。 |
| **SearchTool 独立实现** | ✅ | 2026-07-18 完成外部仓库检索修复：流式 Top-K、每文件预入堆去重、源码优先排序、docs_src 正确降权、定义名精确匹配；Agent 端过滤通用检索词并归一限定名。Typer 证据检索重放 5/19 → 17/19；该结果是工具层回放，尚非 GLM v1 端到端结果。 |
| **合成健壮性修复 + 外部端到端 v2** | ✅ | 2026-07-19 完成：三处 LLM 调用统一 timeout=60 + thinking disabled（v1 端到端 47/63 条合成降级的根因），合成证据优先级选取（置信度+单文件上限），上下文文件按证据命中排序。v2 端到端 63/63 真实回答、0 降级；grounded_answer_rate 4.8-14.3% → 52.4-66.7%。 |
| **合成上下文窗口化（v3/v4/v5 三轮）** | ✅ | 2026-07-19 完成：证据命中行 ±30 行窗口（v3）→ 上下文文件排序 源码目录>定义命中>命中数（v4）→ 关键词定义行权重 4 优先窗口（v5，补 SearchTool 每文件单命中的方法级盲区）。定性改善真实（explain 开始产出结构化正确回答、incorrect 保持低位），但总量指标平台期（grounded 52-71% 徘徊）。轮间对比受合成 temperature=0.3 噪声影响（±5pp 量级）。 |
| **探索预算计账重构（v6）** | ✅ | 2026-07-19 完成（代码改动由 Codex 侧完成，本侧评测验收）：证据保留 [:8] + min(300) 字符计账、AST/依赖读源码不计账、合成保底 5000 tokens、合成上下文字符预算。v6 评测：budget_exhausted 36→0、平均步数 1.87→2.11、首现 4 步调查、63/63 回答保持；守卫命中 typer evidence_retrieval 100→90.5%（[:8] 截断误作用于事实层证据库，同日已修复：证据库全量保留、[:8] 只作用于计账口径，待重跑验证恢复）。语义层平台期；停止原因 STOP 37/NO_EVIDENCE 10/CONTINUE 9/BUDGET 0 → 下一瓶颈为假设静态模板。 |
| **证据缺口动态行动（V9 待端到端验收）** | 🟡 | V8 固定 63 条真实 GLM + Judge：三仓库 `read_window` 基础设施失败 0（V7 Typer 9→0）、同调查重复 `(gap,target)` 0、Typer evidence_retrieval 100%。2026-07-19 已重构为 Evidence → answer_sufficient → missing_evidence → 动态行动的闭环：仅缺口行动可继续，零增量立即停，覆盖/无关行动清理，深度≤3、同类/同目标去重；通用工具队列不再续跑。待 V9 63 条验证 steps 超限 4/63 是否消除且质量守卫不退化。 |

## 明确不纳入 V1（计划书 §八）

| 项目 | 说明 |
|------|------|
| GitHub PR inline 评论 | V2 |
| 多语言 Parser/Analyzer 插件 | V2 |
| 高级前端大盘 | V2 |
| 团队知识/历史反馈闭环 | V2 |
| 多租户存储 | V2 |

## 本轮评测跟踪（2026-07-16，已完成两轮迭代）

**第一轮：评测基础设施修复**
- 根因修复：Judge 曾回样本临时仓库跑 `git diff HEAD~1..HEAD` 取证，目录被清理后整批 20 个 Judge 结果失效。改为 Pipeline 结果持久化 `unified_diff`，Judge 只消费固化证据（`ReviewOutput.unified_diff` → `run_pipeline.py` 序列化 → `judge.py::_load_diff`）。
- 附带修复：glm-4.5-air 推理内容与正文共用 max_tokens 预算，思考耗尽预算导致 JSON 截断/为空（曾 11/20 解析失败）。Judge 与评测 LLM 调用统一关闭 thinking（`extra_body={"thinking": {"type": "disabled"}}`）。
- 基线（有效）：static P 87.18% / R 40.48% / F1 55.29%；llm P 87.50% / R 41.18% / F1 56.00%。

**第二轮：针对漏报的四项补强（同基准验证）**
1. ruff 显式 `--select E,W,F,S,C90`（默认集不含 S101/C901/E501）
2. Aggregator 粒度 (file, rule) → (file, rule, line)，同规则多处命中不再被合并吞掉
3. LLM 语义审查从仅 `.py` 扩展到 js/ts/json/yaml/toml 等（排除 lock 文件）
4. LLMReviewer 系统提示改为 9 项显式检查清单

**验收结果（同 20 样本、同 Judge 配置、40/40 评判有效）：**

| 模式 | Precision | Recall | F1 |
|------|-----------|--------|-----|
| static | 87.18% → 97.47% | 40.48% → 64.71% | 55.29% → 77.78% |
| llm | 87.50% → 95.62% | 41.18% → 77.51% | 56.00% → 85.62% |

- llm 相对 static F1 +7.8pp——验证「确定性工具负责可靠扫描，LLM 负责语义补漏」架构方向成立。
- 基线结果备份：`eval_report/results_baseline_20260716/`；报告：`eval_report/reports/report_20260716_*.{md,json}`。
- 口径警告：真值来自合成样本 + LLM-as-Judge，是系统内对比指标，不是生产准确率。

## 当前优先级

更新日期：2026-07-19（V1 收尾与 V1.1 Agent 增强阶段；Judge 管道生产可用；端到端 v2-v8 已完成，V9 待验收）

1. **动态行动 V9 端到端验收** — 固定 63 条 + Judge，验证新的 Evidence→充分性→缺口行动闭环。验收：三仓库零 `read_window` 基础设施失败、同调查零重复 `(gap,target)`、无旧通用工具续跑、零增量行动立即停止、steps 超限不高于 V8 的 4/63，且 Typer evidence_retrieval 保持 100%。
2. **评测真值校准** — 人工校验 ground truth（先 50 条）+ 外部真实项目样本（sample_cve.py 路线），把"系统内对比"升级为可外部背书的指标
3. **微调 Planner** — 刻意放最后：规则 Planner 已有效，先做 Agent 增强更划算
4. ~~探索预算计账重构~~ ✅（2026-07-19）：budget_exhausted 36→0、平均步数 1.87→2.11、首现 4 步、63/63 回答保持；代价 typer 证据层 -9.5pp 被守卫指标捕获，详见 `external_glm_v6/V6_EVAL_REPORT.md`
5. ~~合成上下文窗口化（v3/v4/v5）~~ ✅（2026-07-19）：定性改善（explain 结构化回答、trace unjudgeable 13→10），总量平台期，详见 `external_glm_v5/V5_EVAL_REPORT.md`
6. ~~外部 Agent 端到端 v2（合成健壮性修复 + 63 条重跑 + Judge 重判）~~ ✅（2026-07-19）：63/63 零降级，grounded 52.4-66.7%，Judge 一次通过率 100%（输出模板修复后 retry=0）
7. ~~外部 Agent v1 语义评测 V2（Judge 管道修复）~~ ✅（2026-07-19）
8. ~~Investigation Agent 增强 (M1/M2/M3)~~ ✅（2026-07-17）
