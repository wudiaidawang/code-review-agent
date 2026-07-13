"""CLI 入口 — 命令行代码审查 + 代码库探索工具。

用法：
    python -m app.cli review .                        # 审查最近一次提交
    python -m app.cli review . --base HEAD~3 --head HEAD  # 审查最近 3 次提交
    python -m app.cli review . --output report.md     # 输出 Markdown 报告到文件
    python -m app.cli investigate . "where is the login function?"  # 探索代码库
    python -m app.cli serve                           # 启动 API 服务 (uvicorn)
"""

import argparse
import sys
import os


def cmd_review(args):
    """执行一次代码审查并输出报告。"""
    from app.pipeline.review_pipeline import ReviewPipeline

    repo_path = os.path.abspath(args.repo)
    print(f"审查范围: {repo_path}  ({args.base}..{args.head})\n")

    pipeline = ReviewPipeline()
    output = pipeline.run(repo_path, args.base, args.head)

    # 输出 Markdown 报告
    if args.output:
        out_path = os.path.abspath(args.output)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(output.markdown)
        print(f"报告已写入: {out_path}")
    else:
        print(output.markdown)

    # 输出 JSON（可选）
    if args.json:
        json_path = args.json if isinstance(args.json, str) else "review_output.json"
        if json_path is True:
            json_path = "review_output.json"
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(output.json)
        print(f"JSON 已写入: {json_path}")

    # 摘要
    print(f"\n完成: {len(output.issues)} 条 Issue, {len(output.evidence)} 条 Evidence, {output.duration_ms:.0f}ms")

    return output


def cmd_investigate(args):
    """探索代码库，回答关于代码结构的问题。"""
    from app.agent.investigator import InvestigationAgent

    repo_path = os.path.abspath(args.repo)
    print(f"探索仓库: {repo_path}")
    print(f"问题: {args.question}\n")

    agent = InvestigationAgent()
    result = agent.investigate(repo_path, args.question)

    print(f"回答: {result.answer}\n")
    if result.files_visited:
        print(f"涉及文件 ({len(result.files_visited)}):")
        for f in result.files_visited:
            print(f"  - {f}")
    if result.evidence:
        print(f"\n证据 ({len(result.evidence)} 条):")
        for ev in result.evidence:
            loc = ev.location
            loc_str = f"{loc.file}:{loc.start_line}" if loc else "(无位置)"
            print(f"  [{ev.source}] {loc_str}: {ev.snippet[:120]}")
    print(f"\n耗时: {result.duration_ms:.0f}ms")
    return result


def cmd_serve(args):
    """启动 FastAPI 服务。"""
    import uvicorn
    from app.api import create_app

    app = create_app()
    host = args.host or "127.0.0.1"
    port = args.port or 8000
    print(f"API 服务启动: http://{host}:{port}")
    print(f"API 文档: http://{host}:{port}/docs")
    uvicorn.run(app, host=host, port=port, log_level="info")


def main():
    parser = argparse.ArgumentParser(
        description="AI Code Review Platform CLI",
        prog="python -m app.cli",
    )
    sub = parser.add_subparsers(dest="command")

    # ---- review ----
    p_review = sub.add_parser("review", help="执行代码审查")
    p_review.add_argument("repo", help="仓库路径")
    p_review.add_argument("--base", default="HEAD~1", help="基准 ref（默认 HEAD~1）")
    p_review.add_argument("--head", default="HEAD", help="目标 ref（默认 HEAD）")
    p_review.add_argument("--output", "-o", default=None, help="Markdown 报告输出路径")
    p_review.add_argument("--json", "-j", nargs="?", const=True, default=None,
                          help="同时输出 JSON 报告（可选路径）")

    # ---- investigate ----
    p_investigate = sub.add_parser("investigate", help="探索代码库")
    p_investigate.add_argument("repo", help="仓库路径")
    p_investigate.add_argument("question", help="关于代码库的问题（中文/英文）")

    # ---- serve ----
    p_serve = sub.add_parser("serve", help="启动 API 服务")
    p_serve.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    p_serve.add_argument("--port", type=int, default=8000, help="端口（默认 8000）")

    args = parser.parse_args()

    if args.command == "review":
        cmd_review(args)
    elif args.command == "investigate":
        cmd_investigate(args)
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
