"""代码审查入口 — 单文件审查 CLI"""

import sys
import io
from pathlib import Path

# 修复 Windows GBK 编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from kb_manager import KnowledgeBase
from kb_seed import seed_kb
from review_graph import build_graph


def main():
    if len(sys.argv) < 2:
        print("用法: python review.py <文件路径>")
        print("示例: python review.py sample_bad.py")
        sys.exit(1)

    file_path = sys.argv[1]
    if not Path(file_path).exists():
        print(f"错误: 文件不存在 — {file_path}")
        sys.exit(1)

    print(f"正在审查: {file_path}")
    print("=" * 60)
    print()

    # 初始化知识库 + 种子数据
    kb = KnowledgeBase(persist_dir="./chroma_db")
    seed_kb(kb)
    print()

    # 读取代码
    with open(file_path, "r", encoding="utf-8") as f:
        code = f.read()

    # 运行审查图
    graph = build_graph(kb)
    print("  [1/4] 检索知识库上下文...")
    print("  [2/4] 执行安全 + 质量审查...")
    print("  [3/4] 生成 Markdown 报告...")
    print("  [4/4] 写入审查历史...")
    print()

    result = graph.invoke({"code": code, "file_path": file_path})

    report = result["report"]

    # 保存报告
    report_path = Path(file_path).stem + "_review_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print("=" * 60)
    # 安全打印（处理 Windows GBK 编码）
    try:
        print(report)
    except UnicodeEncodeError:
        print(report.encode("utf-8", errors="replace").decode("utf-8", errors="replace"))
    print(f"\n报告已保存至: {report_path}")


if __name__ == "__main__":
    main()
