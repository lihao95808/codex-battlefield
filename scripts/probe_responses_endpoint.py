"""直接问一次本地 `/v1/responses`：端点通不通、哪些字段必须给（P6-4 的第一步）。

为什么先做这一步：`verify_provider_planner.py` 那条路要起 Codex CLI、写隔离
`CODEX_HOME`、还要跑完整 planner 契约 —— 拿它当"端点通不通"的探针太贵，
失败了也分不清是端点的事还是 CLI 的事。这里只发一次最小请求。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def post(url: str, payload: dict, headers: dict[str, str], timeout: float) -> tuple[int, str, float]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 (本机回环)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", errors="replace"), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace"), time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}", time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:15721/v1")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--key", default="hub-local")
    parser.add_argument("--no-auth", action="store_true", help="不带头，看端点是否要求凭据")
    args = parser.parse_args()

    headers = {"content-type": "application/json"}
    if not args.no_auth:
        headers["authorization"] = f"Bearer {args.key}"

    cases: list[tuple[str, dict]] = [
        ("minimal", {"model": args.model, "input": "只回一个词：好"}),
        ("with_instructions", {"model": args.model, "instructions": "你是测试探针，只回一个词。", "input": "只回一个词：好"}),
        (
            "with_store_stream",
            {"model": args.model, "input": "只回一个词：好", "stream": False, "store": False},
        ),
    ]
    report = []
    for name, payload in cases:
        status, text, seconds = post(f"{args.base}/responses", payload, headers, args.timeout)
        entry = {"case": name, "status": status, "seconds": round(seconds, 2), "body_head": text[:400]}
        report.append(entry)
        print(json.dumps(entry, ensure_ascii=False))

    ok = [item for item in report if item["status"] == 200]
    print(json.dumps({"model": args.model, "ok_count": len(ok), "total": len(report)}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
