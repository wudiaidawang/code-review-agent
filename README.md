# AI Code Review Platform

确定性多层代码审查平台 —— 规则引擎 + LLM 语义审查 + Investigation Agent + 完整评测体系。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-408%20passed-brightgreen)]()
[![Dataset](https://img.shields.io/badge/dataset-700%20samples-orange)]()
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker)]()

## 两种模式

| 模式 | 入口 | 说明 |
|------|------|------|
| **Code Review** | `review` CLI / `POST /review` API | 对任意 Git 仓库的 diff 运行确定性+语义多层审查，输出结构化 Issue 与可追溯证据链 |
| **Investigation** | `investigate` CLI / `POST /investigate` API | 对代码库提问（定位/解释/追踪调用链/影响分析/对比/枚举），Agent 按 Relation 规划 → Slot 驱动探查 → 合成带证据引用的答案 |

## 快速开始

面向前端使用者的功能说明、可提问范围与限制见 [使用指南](docs/USER_GUIDE.md)。

前端首次打开需要注册本地账号；账号、会话令牌和对话历史保存在服务端 SQLite（轻量数据库）中，并按用户隔离。

准备项目介绍或技术面试时，可使用 [项目面试题库](docs/INTERVIEW_QUESTION_BANK.md)。

### 环境要求

- Python 3.10+
- Git
- Ruff + Bandit（`pip install ruff bandit`）

### 安装

```bash
git clone https://github.com/wudiaidawang/code-review-agent.git
cd code-review-agent
pip install -r requirements.txt
```

### CLI 用法

```bash
# 代码审查
python -m app.cli review /path/to/repo --base main --head feature-branch

# 代码库调查
python -m app.cli investigate /path/to/repo "get_user 函数的鉴权逻辑在哪里？"

# 启动 API 服务
python -m app.cli serve --host 0.0.0.0 --port 8000
```

### Docker 一键启动

```bash
docker compose up -d
```

### API 端点

```bash
POST /review           # 提交代码审查
GET  /review/{run_id}  # 查询审查结果
POST /investigate      # 提交代码库调查
GET  /runs             # 列出历史运行
GET  /health           # 健康检查
POST /jobs/review      # 异步提交审查，返回 job_id
POST /jobs/investigate # 异步提交调查，返回 job_id
GET  /jobs/{job_id}    # 查询异步任务状态/结果
GET  /jobs/{job_id}/events # SSE 流式输出 queued/plan/result 事件
GET  /                  # 浅色 Web 对话界面
POST /repos/import/local # 从浏览器导入本地代码文件夹
POST /repos/import/github # 克隆 GitHub 公开仓库
```

### Web 对话界面

启动服务后访问 `http://127.0.0.1:8000/`。界面提供本地文件夹导入、GitHub 公开仓库地址导入、对话式 Investigation（代码调查）与浏览器本地保存的对话历史。导入的代码会先转为服务端受控的临时 Git 仓库，再被 Agent（智能体）以只读快照方式调查；单次导入限制为 500 个文件、50 MB。

异步 Job API（任务接口）最多接纳 50 个 queued/running（排队/运行中）任务，
默认最多 8 个 worker（工作槽）同时执行，以保护本地文件系统和 LLM（大语言模型）上游。
可通过 `API_MAX_ACTIVE_JOBS`（活跃任务上限）与 `API_MAX_WORKERS`（工作槽上限）调整。

### 调查可靠性与预算控制

`Investigation Agent（调查智能体）`以 Relation（关系）和 Slot（证据槽位）而不是固定工具链推进调查：

- 结论只使用 verified evidence（已验证证据）；candidate evidence（候选证据）仅用于审计，不能关闭合同或进入答案。
- `Completion Gate（完成闸门）`同时检查结构槽位和 Claim（待回答断言），避免“找到定义就提前完成”。
- `GAP Scheduler（确定性补缺调度器）`按信息价值优先验证调用边，再考虑实现、定义与候选引用；普通问题逐条补缺并立即复判，调用链最多保留两条定向边补缺空间。
- LLM（大语言模型）不接管调度；它仅能在证据不足时申请一次 schema-validated RETOOL Task（结构校验后的受控补充任务）。
- 对“接口/路由枚举、项目结构、前端入口、Pipeline（管道）位置”等结构问题，Codebase QA（代码库问答）先生成受限代码地图，再让 LLM 阅读至多 6 个关键片段；回答只能引用该地图生成的 Evidence（证据）。

## 架构概览

```
用户输入 (repo + base/head 或 question)
        │
        ▼
┌──────────────────────────────────────────────┐
│  CLI / FastAPI API                           │
├──────────────────────────────────────────────┤
│  Review Pipeline           Investigation Agent│
│  ┌─────────────────┐      ┌──────────────┐   │
│  │ PlanBuilder     │      │ QueryPlanner │   │
│  │ (规则式计划)     │      │ (Relation 驱动)│   │
│  ├─────────────────┤      ├──────────────┤   │
│  │ Executor        │      │ TaskExplorer │   │
│  │ (工具链+容错)    │      │ (Slot 驱动调度)│   │
│  ├─────────────────┤      ├──────────────┤   │
│  │ Aggregator      │      │ Synthesizer  │   │
│  │ (分组去重)       │      │ (证据引用答案) │   │
│  ├─────────────────┤      └──────────────┘   │
│  │ ReportGenerator │                          │
│  │ (Markdown/JSON) │                          │
│  └─────────────────┘                          │
├──────────────────────────────────────────────┤
│  确定性工具层 (Tool Protocol)                  │
│  Git │ AST │ Ruff │ Bandit │ Dependency │Search│
├──────────────────────────────────────────────┤
│  领域模型 (Evidence/Finding/Issue/ChangeSet)   │
│  ← 全链路可追溯 + 确定性 ID                   │
└──────────────────────────────────────────────┘
```

## 里程碑进度

| 里程碑 | 描述 | 状态 |
|--------|------|------|
| M0 | 数据契约 (Evidence/Finding/Issue/ChangeSet) | ✅ |
| M1 | 确定性工具层 (Git/AST/Ruff/Bandit/Dependency/Search) | ✅ |
| M2 | 固定审查管道 (Plan→Execute→Aggregate→Report) | ✅ |
| M3 | LLM 语义审查 + 知识检索 | ✅ |
| M4 | 评测体系 + 700 条版控数据集 | ✅ |
| M5 | 服务化 (FastAPI + CLI + Docker) | ✅ |
| M5.1 | 质量工程 (容错/黄金基线/回归/性能) | ✅ |
| V1.1 | Investigation Agent + CI/CD | ✅ |
| V12–V13 | 结构化控制流 + Action Fingerprint + SearchTool 流式 Top-K | ✅ |
| V22 | Task 驱动探查 + 全局优先队列调度 | ✅ |
| V24–V27 | Relation 驱动 Planner + Slot 驱动调度 + Claim Gate 统一断言闸门 + 关系优先 GAP 补缺 | ✅ |

## 评测数据集 (700 条)

### Review 样本 — 550 条

覆盖 5 种语言 × 6 类变更类型 × 5 种风险信号组合，每条含 `input`（变更特征）与 `ground_truth`（期望工具/风险等级/原因码）。

| 维度 | 分布 |
|------|------|
| risk_level | low=81 / medium=344 / high=125 |
| 语言 | Python(507) / JS(8) / TS(5) / JSON(5) / TOML(5) / Go(3) / Java(2) / 其他(15) |

### Agent 样本 — 150 条 (内部) + 63 条 (外部三项目)

| 来源 | 样本数 | 描述 |
|------|--------|------|
| 内部评测集 | 150 | locate/explain/trace/grep 4 类问题 |
| click (8.1) | 21 | CLI 框架 (Command/Context/invoke) |
| httpx (0.28) | 21 | HTTP 客户端 (Client/AsyncClient/Transport) |
| typer (0.16) | 21 | CLI 构建器 (Typer/Option/Argument) |

评测指标：平均工具步数 / 预算超限率 / StepStatus 分布 / LLM Judge 语义评判 (correct/partial/incorrect/unjudgeable)。外部评测最新基线见 `eval_report/results_agent/v28_llm/`。

## 项目结构

```
├── app/                    # 应用包
│   ├── cli.py              # CLI 入口 (review/investigate/serve)
│   ├── agent/              # Investigation Agent (V27)
│   │   ├── investigator.py       # 6 阶段主探查流程
│   │   ├── query_planner.py      # Relation 驱动问题分析
│   │   ├── task_explorer.py      # Slot 驱动任务调度+证据验证
│   │   ├── evidence_closure.py   # 证据闭合合同+Slot 映射
│   │   └── symbol_resolver.py    # 符号解析+节流
│   ├── api/                # FastAPI 服务层
│   │   ├── routes.py       # REST 端点
│   │   ├── jobs.py         # 有界异步 Job + SSE 事件流
│   │   └── schemas.py      # Pydantic 请求/响应模型
│   ├── core/               # 核心基础设施
│   │   ├── pipeline.py     # Pipeline 编排器
│   │   └── workspace.py    # 受控工作区 (git archive 隔离)
│   ├── models/             # 领域模型 (数据契约)
│   │   ├── evidence.py     # Evidence (可引用事实原子)
│   │   ├── finding.py      # Finding (工具候选发现)
│   │   ├── issue.py        # Issue (统一问题模型)
│   │   ├── change.py       # ChangeSet/FileChange/Hunk
│   │   ├── target.py       # 结构化控制流 (TargetSpec/Relation/SufficiencyJudgment)
│   │   └── run.py          # ReviewRun (运行级容器)
│   ├── pipeline/           # 审查管道+评测框架
│   │   ├── plan_builder.py       # 规则式计划生成
│   │   ├── executor.py           # 工具链执行+容错
│   │   ├── aggregator.py         # 聚合去重
│   │   ├── report.py             # Markdown/JSON 报告
│   │   ├── review_pipeline.py    # 完整审查管道
│   │   ├── llm_reviewer.py       # LLM 语义审查
│   │   ├── agent_eval_runner.py  # Agent 评测执行器
│   │   ├── agent_eval_judge.py   # LLM Judge (JSON Schema 校验)
│   │   ├── agent_eval_metrics.py # Agent 评测指标
│   │   └── eval_benchmark.py     # 评测基准脚本
│   └── tools/              # 确定性工具集
│       ├── contract.py     # Tool 统一契约 (Protocol)
│       ├── git_tool.py     # Git diff → ChangeSet+Evidence
│       ├── ast_tool.py     # AST 符号提取
│       ├── ruff_tool.py    # Ruff 代码风格检查
│       ├── bandit_tool.py  # Bandit 安全扫描
│       ├── dependency_tool.py  # 依赖分析
│       ├── search_tool.py  # 代码搜索 (流式 Top-K)
│       └── llm_tool.py     # LLM 调用 (OpenAI 兼容接口)
├── tests/                  # 测试 (408 条)
│   ├── test_agent.py       # Agent 测试 (69 条)
│   ├── test_agent_eval.py  # Agent 评测测试 (77 条)
│   ├── test_task_explorer.py    # Task Explorer 测试
│   ├── test_search_tool.py      # SearchTool 测试 (46 条)
│   └── __snapshots__/
│       └── eval_dataset_v2.json # 700 条评测数据集
├── docs/
│   ├── CHANGELOG.md        # 修改留痕日志
│   └── INDEX.md            # 全项目文件索引
├── eval_report/            # Report 级评测框架
│   └── results_agent/      # Agent 外部评测结果
├── Dockerfile + docker-compose.yml  # Docker 部署
└── CLAUDE.md               # 项目编码规范
```

## 设计理念

1. **确定性优先**：规则引擎处理能确定的事（AST 解析、依赖分析、安全扫描），LLM 只补充语义推理
2. **全链路可追溯**：每个 Issue 可沿证据链回溯到原始代码行，Evidence 带确定性 ID
3. **工具不绑定 Pipeline**：所有工具遵循 `Tool` 协议，可独立使用，可用在 Review Pipeline 或 Investigation Agent 中
4. **可评测**：评测数据集从参数确定性生成 ground truth，不依赖 LLM 标注；LLM-as-Judge 引入 JSON Schema 严格校验
5. **受控工作区**：`git archive` 快照隔离，文件类型白名单 + 读路径越界检查

## 测试

```bash
# 运行全部测试
pytest

# 按标记分组
pytest -m "golden"      # 黄金基线
pytest -m "regression"  # 回归快照
pytest -m "slow"        # 性能基准
```

## 许可证

MIT License
