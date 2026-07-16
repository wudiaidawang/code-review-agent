# AI Code Review Platform

确定性多层代码审查平台 — 规则引擎 + LLM 语义审查 + Investigation Agent + 完整评测体系。

## 项目进度

| 里程碑 | 状态 |
|--------|------|
| M0 数据契约 | ✅ |
| M1 工具层 (Git/AST/Ruff/Bandit/Dependency) | ✅ |
| M2 固定审查管道 (Plan→Execute→Aggregate→Report) | ✅ |
| M3 LLM 语义审查器 + 知识检索 | ✅ |
| M4 评测体系 + 700 条版控数据集 | ✅ |
| M5 服务化 (FastAPI + CLI + Docker) | ✅ |
| M5.1 质量工程 (容错/黄金/回归/性能) | ✅ |
| V1.1 Investigation Agent + CI/CD | ✅ |

**测试**: 173 条 | **评测数据集**: 700 条 (550 Review + 150 Agent)

## 项目文件树

```
Agent_project/
│
├── CLAUDE.md                          # 项目编码规范（规范零/一/二）
├── README.md                          # 本文件 — 项目总览与文件树
├── requirements.txt                   # Python 依赖清单
├── pytest.ini                         # pytest 配置（testpaths/markers）
├── Dockerfile                         # Docker 镜像（python:3.11-slim）
├── docker-compose.yml                 # 一键启动 API 服务
├── litellm_config.yaml                # LiteLLM 代理配置
├── proxy_anthropic_openai.py          # Anthropic→OpenAI 协议代理
├── sample_bad.py                      # 演示用问题代码（多类漏洞植入）
├── sample_bad_review_report.md        # 演示审查报告
│
├── .github/workflows/
│   └── test.yml                       # CI/CD：test/golden/recovery 3 job
│
├── _PLAN/
│   └── AI Code Review Platform — V1 完整任务计划书.md  # 主计划书（M0—M5 + 附录A 流程图）
│
├── app/                               # 应用包
│   ├── __init__.py
│   ├── cli.py                         # CLI 入口（review/investigate/serve）
│   │
│   ├── agent/                         # V1.1 Investigation Agent
│   │   ├── __init__.py                # 导出 InvestigationAgent / InvestigationResult
│   │   └── investigator.py            # 核心调查 Agent（grep→解析→读文件→LLM 合成）
│   │
│   ├── analyzers/                     # Analyzer 占位目录（待扩展）
│   │   └── __init__.py
│   │
│   ├── api/                           # FastAPI 服务层
│   │   ├── __init__.py                # create_app() 应用工厂
│   │   ├── routes.py                  # /review /investigate /runs /health 端点
│   │   └── schemas.py                 # Pydantic 请求/响应模型
│   │
│   ├── core/                          # 核心基础设施
│   │   ├── __init__.py
│   │   ├── pipeline.py                # Pipeline 编排器（顺序执行 steps）
│   │   ├── pipeline_step.py           # PipelineStep(ABC) 基类
│   │   └── workspace.py               # 受控工作区管理器（git archive 隔离）
│   │
│   ├── models/                        # 领域模型（数据契约）
│   │   ├── __init__.py
│   │   ├── change.py                  # ChangeSet / FileChange / Hunk
│   │   ├── context.py                 # ReviewContext / InvestigationContext（旧结构，逐步被取代）
│   │   ├── diagnostic.py              # Diagnostic + ERROR_CODES
│   │   ├── evidence.py                # Evidence（可引用事实原子）
│   │   ├── finding.py                 # Finding（工具候选发现）
│   │   ├── ids.py                     # new_id() 稳定短 id 生成
│   │   ├── issue.py                   # Issue（统一问题模型，全系统"通用货币"）
│   │   ├── location.py                # CodeLocation / Symbol
│   │   ├── plan.py                    # ReviewPlan（审查执行计划）
│   │   └── run.py                     # ReviewRun（运行级容器，可追溯性校验）
│   │
│   ├── persistence/
│   │   └── store.py                   # RunStore — JSON 文件持久化（runs/ 目录）
│   │
│   ├── pipeline/                      # 审查管道核心
│   │   ├── __init__.py
│   │   ├── aggregator.py              # 聚合器（按 file+rule_id 去重 Findings→Issues）
│   │   ├── eval_benchmark.py          # 评测基准脚本（LLM vs 规则基线 + Agent 评测）
│   │   ├── eval_dataset.py            # 评测数据集加载（10 条手工 + 700 条 v2）
│   │   ├── eval_generator.py          # 评测数据集生成器（覆盖矩阵 + LLM 批量生成）
│   │   ├── eval_metrics.py            # 评测指标计算（F1/召回率/准确率）
│   │   ├── executor.py                # 执行器（按 Plan 运行工具链，容错降级）
│   │   ├── fact_collector.py          # 事实收集器（M1 全链编排）
│   │   ├── knowledge_retriever.py     # 知识检索器（NullRetriever/StaticKnowledge）
│   │   ├── llm_reviewer.py            # LLM 语义审查器（结构化 prompt + schema 校验）
│   │   ├── observability.py           # 可观测性（PipelineTimeline + 性能分解）
│   │   ├── plan_builder.py            # 规则式计划生成器（风险信号检测 + 工具选择）
│   │   ├── report.py                  # 报告生成器（Markdown/JSON）
│   │   └── review_pipeline.py         # 完整审查管道（Plan→Execute→Aggregate→Report）
│   │
│   ├── report/
│   │   └── __init__.py
│   │
│   ├── retriever/                     # 知识库（旧能力，待适配新 Pipeline）
│   │   ├── __init__.py
│   │   ├── kb_seed.py                 # 种子数据（20 条规范 + 10 条漏洞模式）
│   │   └── knowledge_base.py          # ChromaDB 知识库（BAAI/bge-m3 embedding）
│   │
│   ├── tools/                         # 确定性工具集
│   │   ├── __init__.py
│   │   ├── ast_tool.py                # AST 符号提取（函数/类/导入/调用边）
│   │   ├── bandit_tool.py             # Bandit 安全扫描
│   │   ├── contract.py                # Tool 统一契约（ToolResult/Tool 协议）
│   │   ├── dependency_tool.py         # 依赖分析（import 变更 + 清单文件）
│   │   ├── git_tool.py                # Git diff 解析（ChangeSet + Evidence）
│   │   ├── llm_tool.py                # LLM 工具（OpenAI 兼容接口，带重试）
│   │   └── ruff_tool.py               # Ruff 代码风格检查
│   │
│   └── utils/
│       └── __init__.py
│
├── tests/                             # 测试（共 173 条）
│   ├── test_agent.py                  # Investigation Agent 测试（18 条，mock LLM）
│   ├── test_ast_tool.py               # AST 工具测试（4 条）
│   ├── test_data_contracts.py         # 数据契约序列化测试（10 条）
│   ├── test_fact_collector.py         # 端到端集成测试（4 条）
│   ├── test_git_tool.py               # Git 工具测试（4 条）
│   ├── test_golden.py                 # 黄金基线 + 回归快照（6 条）
│   ├── test_m2_pipeline.py            # M2 管道闭环测试（12 条）
│   ├── test_m3_llm.py                 # M3 LLM 审查测试（9 条，mock LLM）
│   ├── test_m4_eval.py                # M4 评测体系测试（31 条，mock LLM）
│   ├── test_m5_api.py                 # M5 API 服务化测试（18 条）
│   ├── test_performance.py            # 性能基准 + Timeline 测试（9 条）
│   ├── test_pipeline.py               # Pipeline 骨架测试（5 条）
│   ├── test_pipeline_recovery.py      # 容错恢复测试（10 条）
│   ├── test_review_run.py             # ReviewRun 可追溯性测试（6 条）
│   ├── test_static_tools.py           # Ruff/Bandit 工具测试（17 条）
│   ├── test_tool_contract.py          # Tool 契约测试（5 条）
│   ├── test_workspace.py              # 工作区管理测试（5 条）
│   └── __snapshots__/
│       ├── eval_dataset_v2.json       # 700 条评测数据集
│       ├── eval_dataset_v2_meta.json   # v2 数据集元数据
│       └── pipeline_head_snapshot.json # Pipeline 回归快照
│
├── docs/
│   ├── CHANGELOG.md                   # 修改留痕日志
│   ├── INDEX.md                       # 全项目文件索引
│   └── stages/
│       ├── README.md                  # 阶段进度总览
│       └── stage-1-data-contract.md   # 阶段一交付报告
│
└── runs/                              # 审查运行持久化目录（已 gitignore）
    └── run_*.json                     # 各次审查运行记录
```

## 评测数据集内容 (700 条)

### Review 样本 — 550 条

| 维度 | 分布 |
|------|------|
| **risk_level** | low=81 / medium=344 / high=125 |
| **analyzer 组合** | bandit+git+python_ast+ruff: 304 |
| | bandit+dependency+git+python_ast+ruff: 179 |
| | git: 34 |
| | git+python_ast+ruff: 24 |
| | dependency+git: 9 |
| **reason_code** | auth_change: 199 / dependency_change: 188 / sql_risk: 179 / deserialization: 179 / command_injection: 178 / no_python_changes: 37 / bandit_skipped_low_risk: 24 |
| **语言覆盖** | Python(.py): 507 / JS(.js): 8 / TS(.ts): 5 / JSON: 5 / TOML: 5 / Config(.cfg): 4 / Go(.go): 3 / Java(.java): 2 / YAML(.yml): 2 / Text(.txt): 2 / Markdown(.md): 1 / 其他: 6 |
| **样本字段** | id / mode / scenario / input(change_summary, file_types, diff_size, risk_signals, ast_summary, static_findings_count) / ground_truth(analyzers, risk_level, reason_codes) |

### Agent 样本 — 150 条

| 维度 | 分布 |
|------|------|
| **question_type** | locate: 42 / explain: 39 / trace: 35 / grep: 34 |
| **样本字段** | id / mode / question / ground_truth(question_type, expected_keywords, expected_tools) |

### 数据集特性

- **生成方式**: `eval_generator.py` 确定性覆盖矩阵（9 类场景 × 5 维度） + LLM 批量生成文本 + 参数直接计算 ground truth
- **版本**: v2，绑定 git commit `c612a7f`
- **人工校验**: 0 条（待抽 50 条确认）；当前 ground truth 由规则生成，仅用于回归与覆盖测试，不能作为独立效果证明。
