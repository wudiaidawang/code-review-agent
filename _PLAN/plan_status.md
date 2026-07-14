# V1 计划任务跟踪表

对照 `_PLAN/AI Code Review Platform — V1 完整任务计划书.md` 逐项跟踪完成状态。更新日期：2026-07-14。

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
| Aggregator（按 file+rule_id 去重） | ✅ |
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
| **真实模型跑 benchmark（LLM 增量检出/误报/耗时/token）** | ❌ | 当前 LLM 测试全是 mock |

## M4 评测体系与微调 Planner

| 子任务 | 状态 | 备注 |
|--------|------|------|
| 版本化评测数据集（eval_dataset.py） | ✅ | v2: 700 条 |
| 覆盖矩阵生成器（eval_generator.py） | ✅ |
| 评测指标计算（eval_metrics.py） | ✅ |
| 评测基准脚本（LLM vs 规则基线） | ✅ |
| Agent 评测（Question Type + Keyword F1） | ✅ |
| **人工校验 ground truth（50 条）** | ✅ | 2026-07-14 完成，无问题 |
| **人工校验 ground truth（全量 700 条双层校验）** | ⬜ | 待定 |
| **LLM 生成器 JSON 解析加固** | ✅ | 三层兜底：整体解析→正则逐对象→空占位 |
| **非 Python 语言覆盖扩展** | ✅ | 2026-07-14 完成，Python 50% / JS 14% / TS 10% / Java 6% / Go 6% |
| **LLM 生成失败批补生成（~100 条 change_summary 为空）** | ✅ | 2026-07-14 完成，添加 --regenerate 命令，确定性匹配空样本并补生成 |
| **Report 级评测（Issue/Evidence/Suggestion 质量）** | ⬜ | 需先扩展 ground truth schema |
| **微调训练数据构建（从 trace 生成偏好数据）** | ⬜ | |
| **微调 Planner 模型训练** | ⬜ | |
| **微调 vs 规则基线对比实验** | ⬜ | |
| **shadow mode 上线策略实现** | ⬜ | |
| **下游 Issue precision/recall/严重漏报率评测** | ⬜ | |

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

## V1.1 Investigation Agent + CI/CD（计划书外追加）

| 子任务 | 状态 | 备注 |
|--------|------|------|
| InvestigationAgent（grep→解析→读文件→LLM 合成） | ✅ |
| 问题分类（locate/explain/trace/grep） | ✅ |
| CLI + API 端点 | ✅ |
| GitHub Actions CI（test/golden/recovery） | ✅ |
| **多轮探索 UX** | ⬜ | V2 |
| **SearchTool 独立实现** | ✅ | 2026-07-14 完成，app/tools/search_tool.py，InvestigationAgent 已重构使用 |

## 明确不纳入 V1（计划书 §八）

| 项目 | 说明 |
|------|------|
| GitHub PR inline 评论 | V2 |
| 多语言 Parser/Analyzer 插件 | V2 |
| 高级前端大盘 | V2 |
| 团队知识/历史反馈闭环 | V2 |
| 多租户存储 | V2 |

## 当前优先级

更新日期：2026-07-14（任务 3/4/5 已完成）

1. **Report 级评测** — 扩展 ground truth schema，跑完整 Pipeline 评测 Issue/Evidence 质量
2. **微调 Planner** — 训练数据构建 → 模型训练 → 对比实验
3. ~~补生成空样本~~ ✅
4. ~~非 Python 语言扩展~~ ✅
5. ~~SearchTool~~ ✅
