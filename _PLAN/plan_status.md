# V1 计划任务跟踪表

对照 `_PLAN/AI Code Review Platform — V1 完整任务计划书.md` 逐项跟踪完成状态。更新日期：2026-07-18。

**当前状态定义：核心 Review 产品已完成且可展示；进入 V1 收尾与 V1.1 Agent 增强阶段。**

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

更新日期：2026-07-18（V1 收尾与 V1.1 Agent 增强阶段，M3 已落地，外部评测 Judge 已修复）

1. **外部 Agent v1 语义评测 V2 已完成** ✅（2026-07-19） — V2 改进：JSON Schema 校验 + 完整评判标准 + Evidence 截断 + 冻结数据补全 + 人工验证 12 条（一致率 91.7%）。judge_invalid_schema 三仓库均 0%（曾 5-24%），judge_effective 三仓库均 100%（曾 76-95%）。Judge 管道已可投入生产使用。
2. **评测真值校准** — 人工校验 ground truth（先 50 条）+ 外部真实项目样本（sample_cve.py 路线），把"系统内对比"升级为可外部背书的指标
3. **微调 Planner** — 刻意放最后：规则 Planner 已有效，先做 Agent 增强更划算
4. ~~Investigation Agent 增强 (M1/M2/M3)~~ ✅（2026-07-17）
5. ~~Report 级评测~~ ✅（2026-07-16）
6. ~~真实 GLM benchmark~~ ✅（2026-07-16）
