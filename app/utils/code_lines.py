"""代码行号工具"""


def add_line_numbers(code: str) -> str:
    """给代码每行加 '行号 | ' 前缀，让 LLM 直接引用准确行号（整文件从第1行起，显示行号=原始行号）"""
    lines = code.split("\n")
    width = len(str(len(lines)))
    return "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(lines, 1))
