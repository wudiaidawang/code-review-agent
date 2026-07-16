"""GitHub CVE 真实案例采样 — 从公开仓库搜索 CVE fix commits 并跑 Pipeline.

策略:
1. 用 gh CLI 搜索近年的 Python 相关 CVE fix commit
2. 克隆仓库到临时目录
3. checkout 漏洞版本 (fix commit 的 parent)
4. 跑 ReviewPipeline，对比检出结果与 CVE 描述的漏洞类型
5. 目标: 3-5 个真实案例

用法:
    python -m eval_report.sample_cve                         # 自动搜索并采样
    python -m eval_report.sample_cve --dry-run               # 只搜索，不克隆/执行
    python -m eval_report.sample_cve --repo <url> --commit <hash>  # 手动指定

注意:
- 需要安装 gh CLI 并已登录 (gh auth login)
- 如果 GitHub API 限流或找不到合适样本，此脚本可跳过
- 大仓库克隆可能很慢，设置 120s 超时
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from app.pipeline.review_pipeline import ReviewPipeline

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "results"
CVE_REPOS_DIR = ROOT / "cve_repos"

# 已知的 Python CVE 案例（备选，如果 gh api 不可用时直接使用）
# 格式: (仓库 URL, fix commit hash, CVE 编号, 漏洞类型)
_KNOWN_CVES: list[tuple[str, str, str, str]] = [
    # Django SQL 注入 CVE-2022-28347
    # 注: 以下为示例条目，实际使用时会尝试通过 gh api 搜索更近期的案例
]


def _search_github(keywords: list[str], max_results: int = 10) -> list[dict]:
    """用 gh api 搜索 GitHub commits。"""
    results = []
    for kw in keywords:
        try:
            query = f"fix sql injection python CVE language:python"
            result = subprocess.run(
                ["gh", "search", "commits", query, "--limit", str(max_results), "--json",
                 "sha,repository,commit"],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout.decode("utf-8", errors="replace"))
                results.extend(data)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
            print(f"  [WARN] gh api 搜索失败 ({kw}): {e}")
            continue
    return results


def _clone_repo(repo_url: str, target_dir: Path, timeout: int = 120) -> bool:
    """浅克隆仓库到目标目录。"""
    if target_dir.exists():
        return True  # 已存在
    try:
        subprocess.run(
            ["git", "clone", "--depth=10", repo_url, str(target_dir)],
            check=True, capture_output=True, timeout=timeout,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"    [WARN] 克隆失败: {e}")
        return False


def run_on_cve(repo_url: str, fix_commit: str, cve_id: str, vuln_type: str) -> dict | None:
    """在 CVE fix commit 的 parent 版本上运行 Pipeline。

    Args:
        repo_url: GitHub 仓库 URL
        fix_commit: 修复漏洞的 commit hash
        cve_id: CVE 编号
        vuln_type: 漏洞类型描述 (如 "SQL injection")
    """
    repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
    safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', repo_name)
    repo_dir = CVE_REPOS_DIR / f"{safe_name}_{cve_id}"

    print(f"  [{cve_id}] {vuln_type}")
    print(f"    仓库: {repo_url}")
    print(f"    Fix commit: {fix_commit[:12]}")

    # 克隆
    if not _clone_repo(repo_url, repo_dir):
        return None

    try:
        # 获取 fix commit 的 parent (漏洞版本)
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", f"{fix_commit}~1"],
            check=True, capture_output=True, timeout=10,
        )
        vuln_commit = result.stdout.decode().strip()

        # Checkout 漏洞版本
        subprocess.run(
            ["git", "-C", str(repo_dir), "checkout", vuln_commit],
            check=True, capture_output=True, timeout=10,
        )

        # 获取 fix commit 的 diff（用于展示和 Judge）
        diff_result = subprocess.run(
            ["git", "-C", str(repo_dir), "diff", f"{fix_commit}~1..{fix_commit}"],
            capture_output=True, timeout=30,
        )
        diff_text = diff_result.stdout.decode("utf-8", errors="replace")
        if len(diff_text) > 6000:
            diff_text = diff_text[:6000] + "\n... (truncated)"

        # 运行 Pipeline（目标: HEAD 即当前的漏洞版本, base 为 parent）
        print(f"    运行 Pipeline ...", end=" ", flush=True)
        t0 = time.perf_counter()
        output = ReviewPipeline().run(str(repo_dir), "HEAD~1", "HEAD")
        elapsed = time.perf_counter() - t0
        print(f"OK ({len(output.issues)} issues, {elapsed:.1f}s)")

        result = {
            "cve_id": cve_id,
            "vuln_type": vuln_type,
            "repo_url": repo_url,
            "fix_commit": fix_commit,
            "vuln_commit": vuln_commit,
            "issues": [i.to_dict() for i in output.issues],
            "evidence_count": len(output.evidence),
            "trace": [{"step": t.step, "status": t.status, "duration_ms": t.duration_ms}
                      for t in output.trace],
            "diff_sample": diff_text[:2000],  # 保存部分 diff 供人工查看
            "duration_ms": round(elapsed * 1000),
        }

        # 保存结果
        out_path = RESULTS_DIR / f"cve_{cve_id}_pipeline_output.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        return result

    except subprocess.CalledProcessError as e:
        print(f"    [ERROR] git 操作失败: {e}")
        return None
    except Exception as e:
        print(f"    [ERROR] Pipeline 失败: {e}")
        return {"cve_id": cve_id, "error": str(e)}


def _fallback_known_cves() -> list[dict]:
    """使用手动整理的已知 CVE 列表作为备选. 这些是可以在本地模拟测试的 case."""
    # 由于大面积克隆外部仓库可能超时/失败，这里构造几个模拟的 CVE 案例
    # 这些案例的漏洞模式已知，可以直接生成对应代码来验证 Pipeline 的检出能力
    return [
        {
            "id": "CVE-sim-001", "type": "SQL injection",
            "desc": "用户输入直接拼接到 SQL 查询，无参数化处理",
            "code_pattern": "cursor.execute(f\"SELECT * FROM users WHERE id={user_id}\")",
            "expected_rule": "B608",
        },
        {
            "id": "CVE-sim-002", "type": "Command injection",
            "desc": "subprocess.run 使用 shell=True 且包含用户输入",
            "code_pattern": "subprocess.run(f\"ping -c 1 {host}\", shell=True)",
            "expected_rule": "B602",
        },
        {
            "id": "CVE-sim-003", "type": "Deserialization",
            "desc": "pickle.loads 加载未验证的网络数据",
            "code_pattern": "pickle.loads(request.data)",
            "expected_rule": "B301",
        },
        {
            "id": "CVE-sim-004", "type": "Hardcoded credentials",
            "desc": "代码中硬编码了数据库密码",
            "code_pattern": "DB_PASSWORD = 'admin123'",
            "expected_rule": "B105",
        },
        {
            "id": "CVE-sim-005", "type": "Insecure YAML loading",
            "desc": "yaml.load 使用不安全的 Loader",
            "code_pattern": "yaml.load(user_data, Loader=yaml.Loader)",
            "expected_rule": "B506",
        },
    ]


def sample(dry_run: bool = False, repo: str | None = None, commit: str | None = None):
    """主入口。"""
    CVE_REPOS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # 手动指定
    if repo and commit:
        run_on_cve(repo, commit, "manual", "manual")
        return

    # 尝试 gh api 搜索
    print("搜索 GitHub CVE fix commits ...")
    gh_results = []
    try:
        gh_results = _search_github(["CVE python fix"], max_results=5)
        print(f"  找到 {len(gh_results)} 个结果")
    except Exception:
        print("  gh CLI 不可用，使用内置案例列表")

    if dry_run:
        for r in gh_results[:5]:
            sha = r.get("sha", "?")[:12]
            repo_name = r.get("repository", {}).get("fullName", "?")
            msg = r.get("commit", {}).get("message", "?").split("\n")[0][:80]
            print(f"  {sha} @ {repo_name}: {msg}")
        print("\n内置备选案例:")
        for cve in _fallback_known_cves():
            print(f"  {cve['id']}: {cve['type']} ({cve['expected_rule']})")
        return

    # 实际执行: 处理 gh 搜索结果
    cve_samples = []
    for r in gh_results[:5]:
        sha = r.get("sha", "")
        repo_full = r.get("repository", {}).get("fullName", "")
        repo_url = f"https://github.com/{repo_full}"
        msg = r.get("commit", {}).get("message", "")

        cve_match = re.search(r'CVE-\d{4}-\d{4,}', msg, re.IGNORECASE)
        cve_id = cve_match.group(0) if cve_match else f"GH-{sha[:8]}"
        vuln_type_match = re.search(r'(sql injection|command injection|xss|rce|deserialization|ssrf|path traversal)',
                                     msg, re.IGNORECASE)
        vuln_type = vuln_type_match.group(0) if vuln_type_match else "unknown"

        try:
            result = run_on_cve(repo_url, sha, cve_id, vuln_type)
            if result:
                cve_samples.append(result)
        except Exception as e:
            print(f"  [ERROR] {e}")

    print(f"\nCVE 采样完成: {len(cve_samples)} 个成功")
    if not cve_samples:
        print("（未找到可用的 CVE 案例，可使用内置 simulate_CVE 样本替代）")

    # 保存索引
    if cve_samples:
        summary = {
            "total": len(cve_samples),
            "samples": [{"cve_id": s.get("cve_id"), "issues_found": len(s.get("issues", []))}
                        for s in cve_samples],
        }
        (RESULTS_DIR / "_cve_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- CLI ----

if __name__ == "__main__":
    import argparse

    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Report 级评测 — GitHub CVE 采样")
    parser.add_argument("--dry-run", action="store_true", help="只搜索，不克隆/执行")
    parser.add_argument("--repo", type=str, default=None, help="手动指定仓库 URL")
    parser.add_argument("--commit", type=str, default=None, help="手动指定 fix commit hash")
    args = parser.parse_args()

    sample(dry_run=args.dry_run, repo=args.repo, commit=args.commit)
