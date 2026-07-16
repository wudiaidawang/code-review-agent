"""临时脚本：将当前项目 diff 发给智谱做代码审查。"""
import subprocess
import sys
from app.tools.llm_tool import chat

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

diff = subprocess.run(
    ["git", "diff", "HEAD"],
    capture_output=True,
    cwd="E:/Agent_project",
).stdout.decode("utf-8", errors="replace")

if len(diff) > 6000:
    diff = diff[:6000]

system = """你是一个资深代码审查专家。请审查以下代码变更，从以下维度评价：

1. 架构设计：模块划分是否合理、职责是否清晰
2. 代码质量：可读性、错误处理、边界情况
3. 安全性：是否有潜在安全问题
4. 正确性：逻辑是否正确、是否有遗漏

请用中文输出审查意见，按以下格式：

## 整体评价
（一句话总结）

## 具体问题
- [严重度] 文件:行号 — 问题描述 + 建议

## 亮点
（做得好的地方）

## 改进建议
（如果有的话）"""

prompt = f"请审查以下代码变更：\n\n```diff\n{diff}\n```"
print("发送给智谱 (GLM-4.5-Air) 审查...\n")
result = chat(prompt, system=system, temperature=0.3, max_tokens=2500)
print(result)
