# 阶段一：可追溯数据契约（对应里程碑 M0）

> 状态：✅ 完成 ｜ 日期：2026-07-12 ｜ 计划书：`_PLAN/AI Code Review Platform — V1 完整任务计划书.md`（阶段表第「阶段 1」行）

---

## 一、本阶段目标

定义长期稳定的"分析契约"：让一次审查的**输入、证据、计划、结论、失败状态**都有明确的数据结构，且**每条结论都能反查到证据**。这是后续所有工具（Git/AST/Ruff/Bandit/LLM）与两种编排（Review Pipeline / Investigation Agent）的共同地基。

本阶段**只做数据契约**，不实现受控工作区与静态工具（推迟到阶段二，与第一个真正用到它们的 GitTool 一起做，避免脱离使用者空测）。

## 二、完成后系统具备的能力

- 能在内存里构造一次完整的 `ReviewRun`（含计划、变更集、证据、发现、问题、执行 trace）。
- 能把 `ReviewRun` 及其所有子对象序列化为 dict 再还原（`to_dict`/`from_dict` 往返等价）。
- 能校验"每个 Issue 与每个 Finding 都至少关联一条 Evidence，且引用不悬空"。
- 有一套工具调用的统一契约，工具失败时返回结构化诊断而非抛异常。

## 三、新增 / 改动文件清单

| 文件 | 类型 | 内容 |
|---|---|---|
| `app/models/ids.py` | 新增 | `new_id(prefix)` 稳定短 id 生成 |
| `app/models/location.py` | 新增 | `CodeLocation` / `Symbol`（定位基础） |
| `app/models/change.py` | 新增 | `ChangeSet` / `FileChange` / `Hunk`（变更集契约） |
| `app/models/evidence.py` | 新增 | `Evidence`（可引用事实的原子） |
| `app/models/finding.py` | 新增 | `Finding`（工具候选发现） |
| `app/models/diagnostic.py` | 新增 | `ERROR_CODES` + `Diagnostic`（领域模型层，供 ReviewRun 与 ToolResult 共用） |
| `app/models/plan.py` | 新增 | `ReviewPlan`（执行计划，对齐 M4 微调 schema） |
| `app/models/run.py` | 新增 | `ReviewRun` / `TraceEntry` + 可追溯校验 |
| `app/tools/contract.py` | 新增 | `ToolRequest`/`ToolResult`/`Tool` 协议（`Diagnostic` 从 models 层导入） |
| `app/models/issue.py` | 改动 | 向后兼容追加 `id` + `evidence_ids`，新增 `from_dict` |
| `pytest.ini` | 新增 | 钉死 `testpaths=tests`，避免收集时向上走到 E:\ 根 |
| `tests/test_data_contracts.py` | 新增 | 10 例序列化往返 |
| `tests/test_tool_contract.py` | 新增 | 5 例工具契约/结构化诊断 |
| `tests/test_review_run.py` | 新增 | 6 例可追溯校验/往返 |

> **不属于本阶段交付的旧能力**：`app/tools/llm_tool.py`、`app/retriever/knowledge_base.py`、`app/retriever/kb_seed.py` 是迁移保留的旧原型，**尚未接入新 Pipeline**，也不依赖新数据模型/Tool 契约。计划书§七拟在后续阶段适配为统一 Tool 契约后复用，届时才算相应阶段的交付。

## 四、关键设计决策

1. **引用而非内嵌**：`ReviewRun` 用按 id 索引的字典存 `Evidence`/`Finding`，`Issue` 只持 `evidence_ids`。好处：同一条证据可被多个结论共享，阶段三去重合并时无需搬运证据本体。
2. **`ReviewContext` 暂不删除**：`ReviewRun` 作为新运行级容器并行引入，旧 `ReviewContext` 与现有 Pipeline/测试保持可用，符合计划书"保留兼容迁移路径"。
3. **模型层用 `dataclass`，不引入 pydantic**：沿用现有 `Issue` 与 Day1 spec 的既定约定；序列化用 `asdict` + 手写 `from_dict`（嵌套结构显式重建）。
4. **工具失败走结构化诊断**：`Tool.execute` 不抛业务异常，失败时返回 `ToolResult.failure(...)`（status=failed + `Diagnostic`），让编排层可继续跑其余步骤。
5. **分层方向：模型层不依赖工具层**。`Diagnostic` 放 `app/models/diagnostic.py` 而非 `app/tools/contract.py`——因为 `ReviewRun`（领域模型）与 `ToolResult`（工具层）都要用它，若定义在工具层会让模型反向依赖工具层。改后依赖单向：`tools → models`。

## 五、验收对照

| 计划书验收项 | 对应实现 / 测试 |
|---|---|
| 可构造并序列化完整 `ReviewRun` | `test_review_run_roundtrip` |
| 所有 Issue 可反向追溯 Evidence | `ReviewRun.validate_traceability()` + `test_full_run_is_traceable` |
| Finding 须带对应 Evidence（M1 验收前置） | `validate_traceability()` 空证据判错 + `test_finding_without_evidence_is_flagged` |
| 模型序列化有单测 | `test_data_contracts.py`（10 例） |
| 错误状态有单测 | `test_tool_contract.py`（失败不抛异常、结构化诊断） |
| 引用关系有单测 | `test_review_run.py`（无证据/悬空引用被标记、`resolve_evidence`） |

## 六、测试结果

```
python -m pytest        # 走 pytest.ini，testpaths=tests
26 passed
python -m compileall app tests   # 语法编译校验，通过
```

（原有 5 例 Pipeline 单测 + 本阶段新增 21 例。）

**验证状态如实说明**：本机（Claude 会话沙箱）`pytest` 跑出 26 passed、`compileall` 通过。但另一沙箱（Codex）因 `E:\` 根目录权限，pytest 收集阶段被拦、无法独立复现，只跑通了 `compileall`——这是环境/收集路径问题，非代码语法问题。已加 `pytest.ini`（`testpaths=tests`）收敛收集范围；**仍建议在本机或 CI 上补一次正式的绿色测试记录**存档。

## 七、已知限制 / 本阶段未做

- **受控工作区**（clone/worktree、路径白名单、大小上限、超时清理）未实现，已从 M0 移入 M1，与 `GitTool` 一起在阶段二落地。
- **`ReviewContext` 尚未真正收敛**：仅并行引入 `ReviewRun`，旧字段迁移留到有 Analyzer 实际写入 `ReviewRun` 时逐步进行。
- **id 为随机 uuid**：适合当前阶段；阶段三若要"相同输入得到逐字节一致的 run 快照"，需改为内容寻址或在比较时忽略 id。
- **旧原型未接线**：`llm_tool`/`knowledge_base`/`kb_seed` 仍游离于新架构之外（见第三节说明）。

## 八、下一阶段输入

阶段二（M1 确定性事实层）将：
- 实现受控工作区 + `GitTool`，产出本阶段定义的 `ChangeSet`。
- 让 `RuffTool`/`BanditTool` 产出 `Finding` 并挂 `Evidence`，全部走本阶段的 `Tool` 契约。
- 首次真正填充 `ReviewRun` 的证据与发现存储。
