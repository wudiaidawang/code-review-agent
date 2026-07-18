# SearchTool 排序系统说明书

本文档面向想理解代码实现的学习者，逐层拆解 SearchTool 的搜索结果排序机制。

---

## 一、整体流程（一张图）

```
用户搜索 "get_command"
        │
        ▼
┌─────────────────┐
│  git grep -n -i  │  ← 拿原始文本行（文件:行号:内容）
└────────┬────────┘
         │  lines: ["core.py:1875:def get_command...", ...]
         ▼
┌─────────────────┐
│  _build_results  │  ← 解析 → 分类 → 打分 → 排序 → 输出
└────────┬────────┘
         │
         ▼
   ┌──────────┐     ┌──────────┐
   │ artifacts │     │ evidence │   ← 两份并行的输出
   │ (matches) │     │  (置信度) │
   └──────────┘     └──────────┘
```

`execute()` 是入口，`_build_results()` 是核心。

---

## 二、核心概念：排序 = 文件分类 × 命中分类

每条搜索结果被赋予一个 **分数**，分数越高排名越靠前：

```
score = file_weight × 100 + hit_weight + exact_bonus
```

### 2.1 文件分类（file_weight）

根据文件 **路径** 判断它属于哪一类，不需要读取文件内容：

| 分类 | 权重 | 匹配规则 | 示例 |
|------|------|----------|------|
| `source` | **5** | 扩展名是 .py/.js/.ts/.go/.java... | `src/click/core.py` |
| `example` | **4** | 路径含 examples/samples/demo | `examples/aliases/cli.py` |
| `config` | **3** | .cfg/.ini/.toml/.json 等配置文件 | `pyproject.toml` |
| `test` | **2** | 路径含 tests/、文件名 test_*/_test | `tests/test_basic.py` |
| `build` | **1** | setup.py/Makefile/Dockerfile | `setup.py` |
| `doc` | **1** | 路径含 docs/、.md/.rst 文件 | `docs/guide.md` |
| `other` | **0** | 以上都不匹配 | `data/unknown.xyz` |

**判断顺序很重要**：先检查是不是文档/测试/示例 → 最后才是源码，避免误判。

### 2.2 命中分类（hit_weight）— AST 分析

对于 Python 文件，用 **`ast` 标准库** 解析代码结构，判断搜索结果命中的那行代码是什么角色：

| 命中类型 | 权重 | AST 判断依据 | 示例 |
|----------|------|-------------|------|
| `definition` | **50** | 行号落在 `FunctionDef` / `ClassDef` 节点，且函数名/类名含关键词 | `def get_command(...)` |
| `call` | **40** | 行号落在 `Call` 节点，且被调用函数名含关键词 | `self.get_command(...)` |
| `import` | **30** | 行号落在 `Import` / `ImportFrom` 节点 | `from click import command` |
| `reference` | **20** | 以上都不是，但关键词出现在源码行中 | `# 参考 get_command 实现` |
| `comment` | **10** | 行以 `#` / `//` / `--` 开头 | `# get_command is used here` |

**AST 解析失败或非 Python 文件的回退方案**：用正则判断。
- 行包含 `def xxx` / `class xxx` / `function xxx` → `definition`
- 行包含 `(` → `call`
- 行以 `import` / `from` 开头 → `import`
- 其余 → `reference`

### 2.3 精确符号匹配加分（exact_bonus）

```python
exact_bonus = 5  如果关键词作为完整单词出现（\bkeyword\b）
             = 0  如果只是子串匹配
```

例如搜索 `get`：
- `self.get_command()` → +5（`get` 是完整单词）
- `self.target_name` → 0（`get` 只是 `target` 的子串）

---

## 三、评分实例推演

以搜索 `get_command` 为例（click 仓库）：

| 结果 | 文件 | 行内容 | file | hit | exact | **总分** |
|------|------|--------|------|-----|-------|----------|
| 1 | `src/click/core.py:1875` | `def get_command(...)` | source(5) | definition(50) | +5 | **555** |
| 2 | `src/click/core.py:72` | `multi.get_command(...)` | source(5) | call(40) | +5 | **545** |
| 3 | `examples/aliases.py:42` | `def get_command(...)` | example(4) | definition(50) | +5 | **455** |
| 4 | `docs/complex.md:266` | `def get_command(...)` | doc(1) | definition(50) | +5 | **155** |

**关键设计**：即使文档中出现了 "定义"（`def get_command`），它的得分 (155) 也远低于源码中的一次普通调用 (545)。这是因为 `file_weight × 100` 的乘数效应，保证了 **文件质量 > 命中类型**。

---

## 四、关键函数职责

### `execute(request)` — 入口

```
输入: ToolRequest(params={query, repo_path, search_type, max_results, file_patterns, rank})
输出: ToolResult(artifacts, evidence)
```

- `rank=True`（默认）：启用排序
- `rank=False`：原始顺序，confidence 固定 0.95（向后兼容）

### `_classify_file(file_path)` — 文件分类

纯路径字符串匹配，不读文件。判断优先级：

```
doc 目录/文件名 → test 目录/文件名 → example 目录 → 构建文件 → 源码扩展名 → other
```

### `_analyze_python_ast(file_path, query_str)` — AST 分析

```
输入: 文件路径, "get_command"
输出: {1875: "definition", 72: "call", ...}
```

工作方式：
1. `ast.parse(source)` 解析整个 Python 文件为一棵 AST 树
2. `ast.walk(tree)` 遍历所有节点
3. 遇到 `FunctionDef`/`ClassDef` → 检查名字是否含关键词 → 标记为 definition 行
4. 遇到 `Call` → 检查 `node.func.id` 或 `node.func.attr` → 标记为 call 行
5. 遇到 `Import`/`ImportFrom` → 标记为 import 行

**保护措施**：
- 非 `.py` 文件跳过
- 超过 1MB 文件跳过
- 解析失败（语法错误等）→ 返回空 `{}`，调用方自动回退正则

### `_build_results(lines, search_type, query, repo, limit)` — 核心

1. 解析 git grep 原始行 → `[{file, line, snippet}, ...]`
2. 调用 `_score_matches()` 为每条附加 `_score`、`_hit_kind`、`_file_type`
3. 按 `_score` 降序排列
4. 构建 `Evidence` 列表（置信度随命中类型变化）
5. 截断到 `limit` 条
6. 清理临时字段（`_score` 等不暴露给外部）
7. 返回 `(artifacts, evidence)`

### `_score_matches(matches, query_str, repo)` — 打分

对每条匹配：
1. `_classify_file(fpath)` → file_type → file_weight
2. `_classify_hit(fpath, lineno, snippet, query_str, repo, ast_cache)` → hit_kind → hit_weight
3. `_is_exact_symbol_match(snippet, query_str)` → exact_bonus
4. 写入 `_score`, `_hit_kind`, `_file_type` 到 match dict

**AST 缓存**：同一文件只解析一次，结果缓存在 `ast_cache` 字典中。

### `_classify_hit(...)` — 单条命中分类

决策链：
```
1. snippet 以 # / // / -- 开头 → comment（快速路径，无需 AST）
2. 是 .py 文件且 AST 已解析 → 查 ast_cache 看该行号是否命中
3. 以上都不是 → 正则启发式（_RE_DEF_PATTERN / 括号判断）
```

---

## 五、Evidence 置信度映射

置信度反映 "这条命中被正确分类的可信程度"，不影响排序：

| 命中类型 | 置信度 | 含义 |
|----------|--------|------|
| definition | 0.98 | AST 确认是函数/类定义，几乎不会错 |
| call | 0.95 | AST 确认是调用，但可能有同名函数 |
| import | 0.90 | AST 确认是导入，但可能是重导出 |
| reference | 0.80 | 正则或 AST 未命中，可能是普通文本出现 |
| comment | 0.60 | 注释中的出现，大概率不是有效引用 |

---

## 六、扩展指南

### 支持新语言

1. 在 `SOURCE_EXTS` 加扩展名
2. 在 `_analyze_python_ast` 同级写一个新语言的 AST 分析函数（如 `_analyze_javascript_ast`）
3. 在 `_classify_hit` 中按扩展名分发到对应的分析函数
4. 更新 `_RE_DEF_PATTERN` 正则覆盖新语言的定义语法

### 调权重

修改 `FILE_WEIGHTS` 和 `HIT_WEIGHTS` 字典即可。乘数关系保证文件类型优先级 > 命中类型优先级。

### 禁用排序

```python
SearchTool().execute(ToolRequest(tool="search", params={
    "query": "...", "repo_path": "...", "rank": False
}))
```

---

## 七、相关文件

| 文件 | 关系 |
|------|------|
| `app/tools/search_tool.py` | 本文档描述的文件 |
| `app/tools/contract.py` | ToolRequest / ToolResult 契约定义 |
| `app/models/evidence.py` | Evidence 数据类（置信度字段在此） |
| `app/models/location.py` | CodeLocation 数据类 |
| `app/agent/investigator.py` | InvestigationAgent — SearchTool 的主要调用方 |
