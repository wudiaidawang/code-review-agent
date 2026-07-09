"""多角色审查 — Strategy 模式，按维度拆分审查器

将审查拆为 SecurityReviewer（安全）+ QualityReviewer（Bug/代码异味），
每个策略自带 system prompt 与产出的 findings 类别键，调用方调度并合并各策略结果。
"""

import json
import re
from abc import ABC, abstractmethod

from app.tools.llm_tool import chat_completion

# JSON 输出格式说明（各策略共用，只列出各自负责的类别）
_JSON_FORMAT_NOTE = (
    "请严格输出纯 JSON（不要用 Markdown 代码块包裹）。"
    "line 必须是具体行号数字，取自代码每行开头 `行号 |` 前缀中的数字。"
    "如果没有问题，对应数组返回 []。"
)


def _extract_json(raw: str) -> dict:
    """从 LLM 回复中稳健提取 JSON（容忍 ```json 包裹或前后杂文本）。解析失败抛 JSONDecodeError。"""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def build_user_prompt(numbered_code: str, file_path: str, kb_text: str) -> str:
    """构造发送给 LLM 的用户消息（各策略共用；代码已带行号前缀）"""
    return f"""## 知识库参考（以下规范/漏洞模式供审查时参考）

{kb_text}

## 待审查代码

文件: {file_path}

以下代码每行以 `行号 |` 开头，请直接引用该行号填入结果的 line 字段（不要自行数行）：

```python
{numbered_code}
```
"""


class ReviewStrategy(ABC):
    """审查策略基类：定义 system prompt 与该策略负责填充的 findings 顶层键"""

    name: str = "base"
    categories: tuple[str, ...] = ()

    @abstractmethod
    def get_system_prompt(self) -> str:
        """返回该策略的 system prompt"""

    def review(self, numbered_code: str, file_path: str, kb_text: str) -> dict:
        """调用 LLM 执行审查，返回解析后的 dict。

        LLM 网络调用失败时由 chat_completion 重试；重试仍失败或 JSON 解析失败时，
        返回空类别（不中断整体流程，实现优雅降级）。
        """
        try:
            raw = chat_completion(
                messages=[
                    {"role": "system", "content": self.get_system_prompt()},
                    {"role": "user", "content": build_user_prompt(numbered_code, file_path, kb_text)},
                ],
                temperature=0.2,
                max_tokens=3000,
            )
        except Exception:
            return {c: [] for c in self.categories}
        try:
            return _extract_json(raw)
        except json.JSONDecodeError:
            return {c: [] for c in self.categories}


class SecurityReviewer(ReviewStrategy):
    """安全审查：专注 OWASP/CWE 类漏洞"""

    name = "security"
    categories = ("security",)

    def get_system_prompt(self) -> str:
        return f"""你是一位资深应用安全审计专家，精通 OWASP Top 10 与 CWE。

请逐行审查代码，只关注**安全漏洞**，重点检查：
- SQL/命令注入（字符串拼接构造查询、os.system + 用户输入、shell=True）
- 硬编码密钥/密码/Token
- 不安全的反序列化 (pickle.loads)
- 路径遍历
- 弱加密算法 (MD5/SHA1)
- 不安全的随机数 (random.random 用于安全场景)
- XSS、SSRF、不安全的临时文件等

{_JSON_FORMAT_NOTE}

输出格式：
{{
  "security": [
    {{"severity": "critical|high|medium|low", "line": 行号, "title": "简短标题", "cwe": "CWE编号", "description": "详细描述", "suggestion": "修复建议"}}
  ],
  "summary": "安全方面的整体评价"
}}"""


class QualityReviewer(ReviewStrategy):
    """质量审查：专注 Bug 与代码异味"""

    name = "quality"
    categories = ("bugs", "code_smells")

    def get_system_prompt(self) -> str:
        return f"""你是一位资深 Python 工程质量审查专家，精通编码规范与常见缺陷。

请逐行审查代码，关注 **Bug** 与 **代码异味**（不含安全漏洞，那由安全审查负责）：

**Bug 类：**
- 可变默认参数 (def f(x=[]))
- 除以零、索引越界、空值解引用
- 逻辑错误、错误返回值
- 未关闭的文件/连接/资源

**代码异味类：**
- 裸 except:（未指定异常类型）
- 全局变量
- 过多嵌套、过长的函数
- 缺少类型注解 / docstring
- 未使用的变量/导入
- 使用 assert 而非 raise

{_JSON_FORMAT_NOTE}

输出格式：
{{
  "bugs": [
    {{"severity": "high|medium|low", "line": 行号, "title": "简短标题", "description": "详细描述", "suggestion": "修复建议"}}
  ],
  "code_smells": [
    {{"severity": "high|medium|low", "line": 行号, "title": "简短标题", "description": "详细描述", "suggestion": "重构建议"}}
  ],
  "summary": "质量方面的整体评价"
}}"""


def get_default_strategies() -> list[ReviewStrategy]:
    """返回默认启用的审查策略集合。未来新增维度（如性能、可维护性）在此追加即可。"""
    return [SecurityReviewer(), QualityReviewer()]
