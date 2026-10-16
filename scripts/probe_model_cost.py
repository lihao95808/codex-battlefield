"""看不同模型在**同一个本地网关**上的实际用量与费用（P6-4 的成本表来源）。

为什么单开一个脚本：`probe_weak_model_planner.py` 走的是 Codex CLI，费用元数据在
CLI 的那一层就被丢掉了；要报"每档花了多少"，就得直接打网关的 `/v1/responses`
并把它回报的用量/费用字段原样记下来。

约定：

* **不推算金额**。网关报什么就记什么；报不出来记 `null`（不是 0）。
* **不假设字段名与路径**：用 `--cost-path` 指（不同网关放的位置不一样），
  默认只找顶层 `usage`。
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def post(url: str, payload: dict, headers: dict[str, str], timeout: float) -> tuple[int, dict | str, float]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, json.loads(response.read().decode("utf-8", errors="replace")), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace"), time.monotonic() - started
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}", time.monotonic() - started


def dig(payload: object, path: str) -> object:
    """按点分路径取值；取不到返回 `None`（**不是** 0）。"""

    current = payload
    for part in path.split("."):
        if not part:
            continue
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def main() -> int:
    parser = argparse.ArgumentParser(description="记录同一网关上不同模型的用量与费用。")
    parser.add_argument("--base", default="http://127.0.0.1:15721/v1")
    parser.add_argument("--models", required=True, help="逗号分隔的模型名。")
    parser.add_argument("--prompt", default="用一句话说明什么是二分查找。")
    parser.add_argument("--max-output-tokens", type=int, default=128)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--key", default="hub-local")
    parser.add_argument(
        "--cost-path",
        default="",
        help="费用在响应里的点分路径（例如 a.b.c）；留空则不记录费用。",
    )
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    headers = {"content-type": "application/json", "authorization": f"Bearer {args.key}"}
    records = []
    for model in [item.strip() for item in args.models.split(",") if item.strip()]:
        payload = {
            "model": model,
            "input": args.prompt,
            "stream": False,
            "store": False,
            "max_output_tokens": args.max_output_tokens,
        }
        status, data, seconds = post(f"{args.base}/responses", payload, headers, args.timeout)
        record: dict[str, object] = {"model": model, "status": status, "seconds": round(seconds, 2)}
        if isinstance(data, dict):
            usage = data.get("usage")
            record["usage"] = usage if isinstance(usage, dict) else None
            record["cost"] = dig(data, args.cost_path) if args.cost_path else None
            record["status_text"] = data.get("status")
            record["error"] = data.get("error")
        else:
            record["usage"] = None
            record["cost"] = None
            record["body_head"] = str(data)[:300]
        records.append(record)
        print(json.dumps(record, ensure_ascii=False))

    report = {"base": args.base, "cost_path": args.cost_path or None, "records": records}
    if args.json_out:
        from pathlib import Path

        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if any(item["status"] == 200 for item in records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
