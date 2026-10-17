"""把「现在的环境把哪条交付物挡住了」写成一份**可复现的报告**。

## 为什么需要它

一份"环境阻塞"的结论如果不能被一条命令复现，它就只是叙述 —— 而叙述不会变红，
也没法让另一个人确认"我这边是不是同一个原因"。交付审计里 P6 的未验项（容器化 / Linux /
厂商端点）都卡在**本机没有的东西**上；这份报告把"没有"与"不知道"分开，并给出**该补什么**。

## 三种状态，绝不能混用

| 状态 | 含义 | 典型原因 |
|---|---|---|
| `ok` | 这一项在本机可用 | 装了、在 PATH 上 |
| `missing` | 明确没有 | `docker` 不在 PATH |
| `blocked` | 有，但被外部条件挡住 | Actions 因账单不启动作业 |
| `unknown` | **查不出来** | 没有 GitHub 凭据、命令超时 |

`unknown` 不等于 `missing`：把"没查"写成"没有"是这一类报告最容易犯的错
（本仓库的规矩：`null` ≠ `0`，`没查` ≠ `没有`）。

## 用法

```powershell
api\\.venv\\Scripts\\python.exe scripts\\report_blockers.py          # 人读的表格
api\\.venv\\Scripts\\python.exe scripts\\report_blockers.py --json   # 机器读
```

退出码**总是 0**：它是报告，不是门禁。门禁里该拦的东西在 `verify_all` 里。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_REPO = "lihao95808/codex-battlefield"

#: 允许出现的状态。新增状态必须同时改这里与文档 —— 否则报告会变成自由文本。
STATUSES = ("ok", "missing", "blocked", "unknown")

#: Actions 被账单挡住时 GitHub 给的那句话（逐字来自 check-run 注解）。
BILLING_MARKERS = (
    "recent account payments have failed",
    "spending limit needs to be increased",
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def _run(command: list[str], *, timeout: int = 20) -> tuple[int, str]:
    """跑一条命令，返回 `(exit_code, 输出)`；跑不起来时 `(127, 原因)`。"""

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{exc.__class__.__name__}: {exc}"
    return completed.returncode, f"{completed.stdout}\n{completed.stderr}".strip()


def classify_ci_annotation(text: str) -> str:
    """把 check-run 的失败注解分类：`billing` / `other`。

    为什么单独一个函数：报告里最容易错的地方就是**把任何失败都写成"被账单挡住"**。
    只有逐字命中账单措辞才算 `billing`；其它失败必须落到 `other`，并且原文要带出来。
    """

    normalized = str(text or "").lower()
    if any(marker in normalized for marker in BILLING_MARKERS):
        return "billing"
    return "other"


def _github_token() -> str | None:
    """从 git 的凭据助手拿 token（不落盘、不进日志）。拿不到就返回 None。"""

    if not shutil.which("git"):
        return None
    try:
        completed = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in (completed.stdout or "").splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip() or None
    return None


def _api(url: str, token: str) -> dict[str, Any] | None:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 - 固定 api.github.com
        url,
        headers={
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "user-agent": "codex-battlefield-blockers",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def check_docker() -> dict[str, Any]:
    if not shutil.which("docker"):
        return {
            "check": "docker",
            "status": "missing",
            "detail": "docker 不在 PATH 上",
            "fix": "在一台装了 Docker 的机器上跑 docker compose up（P6-1 的“界面可用”没有别的验法）",
        }
    code, output = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=25)
    status = "ok" if code == 0 else "blocked"
    return {
        "check": "docker",
        "status": status,
        "detail": output[-300:] or "（无输出）",
        "fix": "服务端没起来时先启动 Docker Desktop / dockerd" if status == "blocked" else "",
    }


def check_wsl() -> dict[str, Any]:
    if not shutil.which("wsl"):
        return {
            "check": "wsl",
            "status": "missing",
            "detail": "wsl 不在 PATH 上",
            "fix": "P6-2 的 Linux 运行期验证只能靠 CI 或一台真 Linux（本机无 WSL）",
        }
    code, output = _run(["wsl", "-l", "-v"], timeout=25)
    if code != 0:
        return {
            "check": "wsl",
            "status": "blocked",
            "detail": output[-300:],
            "fix": "WSL 装了但没启起来；先 `wsl --install` 或检查虚拟化",
        }
    return {"check": "wsl", "status": "ok", "detail": output[-300:], "fix": ""}


def check_codex() -> dict[str, Any]:
    for name in ("codex", "codex.cmd"):
        path = shutil.which(name)
        if path:
            return {
                "check": "codex-cli",
                "status": "ok",
                "detail": path,
                "fix": "",
            }
    return {
        "check": "codex-cli",
        "status": "missing",
        "detail": "PATH 上没有 codex",
        "fix": "P6-4/P6-5 的真实调用需要 Codex CLI",
    }


def check_pnpm() -> dict[str, Any]:
    path = shutil.which("pnpm")
    if not path:
        return {"check": "pnpm", "status": "missing", "detail": "PATH 上没有 pnpm", "fix": "前端门禁需要 pnpm"}
    return {"check": "pnpm", "status": "ok", "detail": path, "fix": ""}


def check_playwright() -> dict[str, Any]:
    """Playwright 的 chromium 是否装过。装没装只看目录在不在，不启动浏览器。"""

    cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if not cache.is_dir():
        return {
            "check": "playwright-chromium",
            "status": "unknown",
            "detail": f"找不到浏览器缓存目录：{cache}",
            "fix": "cd web; pnpm exec playwright install chromium",
        }
    browsers = sorted(item.name for item in cache.iterdir() if item.name.startswith("chromium"))
    if not browsers:
        return {
            "check": "playwright-chromium",
            "status": "missing",
            "detail": f"{cache} 里没有 chromium",
            "fix": "cd web; pnpm exec playwright install chromium",
        }
    return {"check": "playwright-chromium", "status": "ok", "detail": ", ".join(browsers), "fix": ""}


def check_ci_billing() -> dict[str, Any]:
    """CI 作业能不能启动：看最近一次运行/注解。

    只有在**拿得到凭据**时才下结论；拿不到就是 `unknown`（不是 `missing`、更不是 `blocked`）。
    """

    token = _github_token()
    if not token:
        return {
            "check": "github-actions",
            "status": "unknown",
            "detail": "没有可用的 GitHub 凭据（git credential 没给出 token）",
            "fix": "要么配好凭据，要么让人在网页上看 Actions 的注解",
        }
    runs = _api(f"https://api.github.com/repos/{PUBLIC_REPO}/actions/runs?per_page=1", token)
    if not runs or not runs.get("workflow_runs"):
        return {
            "check": "github-actions",
            "status": "unknown",
            "detail": "GitHub API 没有返回运行记录（网络或权限问题）",
            "fix": "",
        }
    latest = runs["workflow_runs"][0]
    head = latest.get("head_sha") or ""
    checks = _api(
        f"https://api.github.com/repos/{PUBLIC_REPO}/commits/{head}/check-runs", token
    )
    if not checks or not checks.get("check_runs"):
        return {
            "check": "github-actions",
            "status": "unknown",
            "detail": f"运行 {latest.get('id')} 没有 check-run",
            "fix": "",
        }
    first = checks["check_runs"][0]
    annotations = _api(
        f"https://api.github.com/repos/{PUBLIC_REPO}/check-runs/{first['id']}/annotations", token
    )
    failure_annotations = [
        str(item.get("message") or "")
        for item in (annotations or [])
        if isinstance(item, dict) and item.get("annotation_level") == "failure"
    ]
    if first.get("conclusion") == "success":
        return {
            "check": "github-actions",
            "status": "ok",
            "detail": f"最近一次作业 {first.get('name')} success（run {latest.get('id')}）",
            "fix": "",
        }
    kinds = {classify_ci_annotation(text) for text in failure_annotations}
    if kinds == {"billing"}:
        return {
            "check": "github-actions",
            "status": "blocked",
            "detail": f"run {latest.get('id')} 的作业**没有启动**，注解是账单措辞：{failure_annotations[0][:120]}",
            "fix": "GitHub 侧 Billing & plans → Actions 的 spending limit / 付款方式",
        }
    return {
        "check": "github-actions",
        "status": "blocked",
        "detail": f"run {latest.get('id')} 失败了，但**不是**账单原因：{failure_annotations[:2]}",
        "fix": "按注解修代码/工作流（不是账单问题）",
    }


def collect() -> list[dict[str, Any]]:
    checks = [
        check_docker(),
        check_wsl(),
        check_codex(),
        check_pnpm(),
        check_playwright(),
        check_ci_billing(),
    ]
    for item in checks:  # 状态集合是封闭的，写错会当场暴露
        assert item["status"] in STATUSES, item
    return checks


def render(checks: list[dict[str, Any]]) -> str:
    lines = [
        "环境阻塞报告（复现：api\\.venv\\Scripts\\python.exe scripts\\report_blockers.py）",
        "",
        f"{'检查项':<22}{'状态':<10}说明",
        "-" * 100,
    ]
    for item in checks:
        lines.append(f"{item['check']:<22}{item['status']:<10}{item['detail'][:150]}")
        if item.get("fix"):
            lines.append(f"{'':<32}→ {item['fix'][:150]}")
    blocking = [item for item in checks if item["status"] in ("missing", "blocked")]
    unknown = [item for item in checks if item["status"] == "unknown"]
    lines += [
        "",
        f"明确挡住 / 缺失：{len(blocking)} 项；查不出来：{len(unknown)} 项。",
        "注意 `unknown` ≠ `missing`：那几项只是**没查到**，别当成“本机没有”。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="把环境阻塞写成可复现的报告（不是门禁）。")
    parser.add_argument("--json", action="store_true", help="输出 JSON（机器读）")
    args = parser.parse_args()
    checks = collect()
    if args.json:
        print(json.dumps({"root": str(ROOT), "checks": checks}, ensure_ascii=False, indent=2))
    else:
        print(render(checks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
