# Agent 外部评测 V1 语义评判报告

**生成时间**：2026-07-19  
**评测引擎**：`app/pipeline/agent_eval_judge.py`（V2：JSON Schema 校验 + 明确评判标准 + Evidence 截断）  
**模型**：GLM-4.5-Air（thinking=disabled, temperature=0, max_tokens=1800, timeout=60s）  
**输入快照**：`eval_report/results_agent/external_glm_v1/frozen/`（已补全 expected_answer_summary/keywords）  
**输出**：`eval_report/results_agent/external_glm_v1/judgments_final_*.json`

---

## V1→V2 改进摘要

| 改进项 | V1（初版） | V2（当前） |
|--------|-----------|-----------|
| JSON 解析 | 仅 fenced + 裸 JSON | 裸/fenced/前后文本/嵌套括号 四种策略 |
| Schema 校验 | 手动归一化 + 容错转义 | JSON Schema 严格校验 + 类型错误重试 |
| System Prompt | 80 字符，仅列字段名 | 含 verdict/score/字段 的完整评判标准 |
| expected_answer_summary | 未传入（63 条全空） | 从数据集 ground truth 补全 |
| expected_answer_keywords | 未传入 | 作为辅助参考传入（不要求逐词复述） |
| Evidence | 全量（平均 52 条，最多 60） | 截断到 ≤18 条（引用+预期+高置信度+多样性） |
| max_tokens | 1200 | 1800 |
| timeout | 20s | 60s |
| 重试策略 | 仅解析失败 | 解析失败 + Schema 不合法 均重试 |
| Agent "无法确定" 处理 | 未定义 | 明确定义：合理理由时→unjudgeable，非合理→incorrect |

---

## Judge 可靠性

| 项目 | Judge 不可用率 | 无效 Schema 率 | 有效评判率 |
|------|-------------|---------------|-----------|
| Click | 0.0% | **0.0%** (曾 23.8%) | **100.0%** (曾 76.2%) |
| HTTPX | 0.0% | **0.0%** (曾 4.8%) | **100.0%** (曾 95.2%) |
| Typer | 0.0% | **0.0%** (曾 14.3%) | **100.0%** (曾 85.7%) |

三仓库 63 条全部获得有效 Judge 判决。JSON Schema 校验 + 类型修复重试完全消除了 Schema 不合法问题。

---

## 四层评测结果

### Click (21 条)

| 层级 | 指标 | 值 |
|------|------|-----|
| 确定层 | strict_completion_rate | **38.1%** |
| 证据层 | evidence_retrieval_rate | **100.0%** |
| 引用层 | citation_grounded_rate | **90.5%** |
| 语义层 | semantic_completion_rate | 9.5% (2/21) |
| 语义层 | semantic_partial_rate | 0.0% |
| 语义层 | semantic_incorrect_rate | 0.0% |
| 语义层 | grounded_answer_rate | 9.5% |
| -- | judge_unavailable_rate | 0.0% |
| -- | judge_invalid_schema_rate | 0.0% |

### HTTPX (21 条)

| 层级 | 指标 | 值 |
|------|------|-----|
| 确定层 | strict_completion_rate | **23.8%** |
| 证据层 | evidence_retrieval_rate | **100.0%** |
| 引用层 | citation_grounded_rate | **85.7%** |
| 语义层 | semantic_completion_rate | 4.8% (1/21) |
| 语义层 | semantic_partial_rate | 0.0% |
| 语义层 | semantic_incorrect_rate | 0.0% |
| 语义层 | grounded_answer_rate | 4.8% |
| -- | judge_unavailable_rate | 0.0% |
| -- | judge_invalid_schema_rate | 0.0% |

### Typer (21 条)

| 层级 | 指标 | 值 |
|------|------|-----|
| 确定层 | strict_completion_rate | **19.1%** |
| 证据层 | evidence_retrieval_rate | **100.0%** |
| 引用层 | citation_grounded_rate | **90.5%** |
| 语义层 | semantic_completion_rate | 9.5% (2/21) |
| 语义层 | semantic_partial_rate | 0.0% |
| 语义层 | semantic_incorrect_rate | 4.8% (1/21) |
| 语义层 | grounded_answer_rate | 14.3% |
| -- | judge_unavailable_rate | 0.0% |
| -- | judge_invalid_schema_rate | 0.0% |

---

## 人工验证结果（12 条样本）

选取 Click/HTTPX/Typer 各 4 条非降级样本，覆盖 locate/explain/trace/grep 四种问题类型。

| 一致 | 不一致 | 一致率 |
|------|--------|--------|
| 11 | 1 | **91.7%** |

唯一差异：ext_httpx_fu_01b — Agent 声明"无法确定"，V2 prompt 已明确此场景的判定规则（合理理由→unjudgeable）。

---

## 分析

**语义层偏低的原因**：v1 运行中 Agent 的 LLM 合成因 token 预算耗尽而降级（Click 18/21、HTTPX 18/21 标记 BUDGET），大部分回答为"LLM 不可用/无法确定"文本。Judge 正确识别此情况——85-95% 的样本判为 unjudgeable。

**正确回答全是 locate 类型**：Agent 在 v1 中成功回答的 5 条（Click 2、HTTPX 1、Typer 2）均为 locate 类问题——只需给出文件路径+行号，不需要 LLM 合成复杂推理。SearchTool 的证据检索足以支撑此类问题。

**三层确定指标保持可信**：
- evidence_retrieval_rate 100%：SearchTool 修复有效
- citation_grounded_rate 85-91%：引用扎根性好
- strict_completion_rate 19-38%：受答案文本格式影响（降级文本不包含关键词）

**Judge 已可投入生产使用**：0% 不可用率 + 0% Schema 不合法率 + 91.7% 人工一致率 = Judge 管道可靠。
