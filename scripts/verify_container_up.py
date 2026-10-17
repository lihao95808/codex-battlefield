"""P6-1 的**运行期**判据：`docker compose up` 之后界面到底能不能用。

## 为什么需要一个脚本

`docs/container.md` 一直写着"未验证：本机没有 Docker，`docker compose up` 一次都没跑过"。
2026-10-18 在 WSL 里真跑了一次，发现**三个只能靠真构建才能暴露的缺陷**（见文件末尾的"已抓到"）。

这个脚本把"界面可用"拆成**可断言的四件事**，每件都能红：

| 判据 | 断言 |
|---|---|
| 服务起来 | `/api/health` 在超时内返回 200 |
| 界面被托管 | `GET /` 返回 200 且 HTML 里有 `id="root"`（`web/dist/index.html` 真进了镜像） |
| 静态资源可达 | 从 `index.html` 里取一个 `/assets/...` 真的 200，且 `content-type` 不是 `text/html` |
| 非 root 运行 | 容器内 `id -u` = 10001（`Dockerfile` 里的 `battlefield` 用户） |

## 端口为什么要可覆盖

本机（Windows）上 8765 常常已经被开发用的 uvicorn 占着；WSL 是 mirrored networking，
所以在 WSL 里 `docker compose up` 会撞端口。脚本因此接受 `--host-port`，用一份**临时 override**
把宿主端口换掉 —— 镜像内仍然是 8765，"界面可用"这件事没有变。

## 用法（在**有 Docker 的机器**上跑，例如 WSL 里的 Ubuntu）

```bash
python3 scripts/verify_container_up.py --host-port 18765 --json-out /tmp/container.json
```
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def run(command: list[str], *, cwd: Path | None = None, timeout: int = 900) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd or ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return completed.returncode, f"{completed.stdout}\n{completed.stderr}".strip()


def http_get(url: str, timeout: float = 5.0) -> tuple[int, str, str]:
    request = urllib.request.Request(url, headers={"accept": "*/*"})  # noqa: S310 - 本机回环
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body, response.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace"), exc.headers.get("content-type", "")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"{exc.__class__.__name__}: {exc}", ""


def wait_for_health(host_port: int, timeout_seconds: float) -> tuple[bool, list[str]]:
    base = f"http://127.0.0.1:{host_port}"
    seen: list[str] = []
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status, body, _ctype = http_get(f"{base}/api/health", timeout=3.0)
        if status == 200:
            return True, [*seen, f"/api/health → 200（{body.strip()[:120]}）"]
        seen.append(f"/api/health → {status}")
        time.sleep(2.0)
    return False, seen


def first_asset(html: str) -> str:
    marker = 'src="/assets/'
    index = html.find(marker)
    if index < 0:
        return ""
    start = index + len('src="')
    end = html.find('"', start)
    return html[start:end] if end > start else ""


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 docker compose 起来之后界面可用（P6-1）。")
    parser.add_argument("--host-port", type=int, default=8765, help="宿主端口（默认与 compose 一致）")
    parser.add_argument("--timeout", type=float, default=180.0, help="等健康检查的秒数")
    parser.add_argument("--keep", action="store_true", help="验证完不 down（便于手看）")
    parser.add_argument("--no-build", action="store_true", help="跳过 --build（镜像已存在时更快）")
    parser.add_argument("--json-out", default="", help="把结果另存一份 JSON")
    args = parser.parse_args()

    summary: dict[str, object] = {"ok": False, "host_port": args.host_port, "checks": {}}
    if not args.keep:
        pass  # 无论成功失败都在 finally 里 down（除非 --keep）

    override = Path(tempfile.mkdtemp(prefix="cb-compose-")) / "verify.override.yml"
    override.write_text(
        # !override 是必须的：compose 对 ports 这类**列表**默认是**追加合并**，
        # 不写它就会两条绑定都留（基线的 8765 仍然被抢），报 ddress already in use。
        "services:\n  battlefield:\n    ports: !override\n"
        f'      - "127.0.0.1:{args.host_port}:8765"\n',
        encoding="utf-8",
    )
    compose = [
        "docker",
        "compose",
        "-f",
        str(ROOT / "docker-compose.yml"),
        "-f",
        str(override),
        "--project-directory",
        str(ROOT),
    ]
    up = [*compose, "up", "-d"] + ([] if args.no_build else ["--build"])
    try:
        code, output = run(up)
        summary["checks"]["compose_up"] = {"ok": code == 0, "detail": output[-1500:]}
        if code != 0:
            return 1

        healthy, attempts = wait_for_health(args.host_port, args.timeout)
        summary["checks"]["health"] = {"ok": healthy, "detail": attempts[-3:]}
        if not healthy:
            code, logs = run([*compose, "logs", "--no-color", "--tail", "60"])
            summary["checks"]["logs_tail"] = logs[-2000:]
            return 1

        base = f"http://127.0.0.1:{args.host_port}"
        status, html, ctype = http_get(f"{base}/")
        index_ok = status == 200 and 'id="root"' in html
        summary["checks"]["index"] = {
            "ok": index_ok,
            "detail": {"status": status, "content_type": ctype, "bytes": len(html)},
        }

        asset = first_asset(html)
        if asset:
            astatus, _abody, actype = http_get(f"{base}{asset}")
            asset_ok = astatus == 200 and "text/html" not in actype
            summary["checks"]["asset"] = {
                "ok": asset_ok,
                "detail": {"path": asset, "status": astatus, "content_type": actype},
            }
        else:
            summary["checks"]["asset"] = {"ok": False, "detail": "index.html 里找不到 /assets/ 引用"}

        state_status, state_body, _ = http_get(f"{base}/api/state")
        summary["checks"]["api_state"] = {
            "ok": state_status == 200 and state_body.strip().startswith("{"),
            "detail": {"status": state_status, "bytes": len(state_body)},
        }

        code, uid = run([*compose, "exec", "-T", "battlefield", "id", "-u"], timeout=120)
        summary["checks"]["non_root"] = {"ok": code == 0 and uid.strip() == "10001", "detail": uid.strip()}

        summary["ok"] = all(item.get("ok") for item in summary["checks"].values() if isinstance(item, dict))
        return 0 if summary["ok"] else 1
    finally:
        if not args.keep:
            run([*compose, "down", "-v"], timeout=300)
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
