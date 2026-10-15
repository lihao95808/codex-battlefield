"""验证"中枢挂掉后能自己接着干"这条安全网。

**为什么值得单独一个脚本**：长时间自主运行的前提是人不在场。如果 API 挂了、机器重启了，
或者调度线程丢了，树就会静静停在那里——而没有任何人会发现。看门狗对此有一条判据：
"活着的树 + 有前线工作 + 没有活跃 agent + 超过 N 秒没推进" ⇒ 建议 `resume_orphaned`。

本脚本把这条链路端到端验一遍（全部在 mock runner 上，不花 token）：

1. 起草宪法 → 锁定 → 以 `auto_run=False` 启树（树停在原地，模拟"没人推"）；
2. 读 `/api/watchdog/state`：此刻**不该**判成孤儿；
3. 把 `task_nodes.updated_at` 往回拨（默认 20 分钟，阈值见 `CODEX_BATTLEFIELD_WATCHDOG_ORPHAN_RESUME_AFTER_SECONDS`）；
4. 再读：**该**判成孤儿，`recommended_action == resume_orphaned`；
5. 发一次看门狗会发的 `POST /api/tasks/{root}/kick`，确认树真的继续推进到终态；
6. 把验证用的树 `cancel` 掉，不在 workspace 里留垃圾。

用法
  python scripts/verify_orphan_resume.py
  python scripts/verify_orphan_resume.py --base-url http://127.0.0.1:8765 --age-minutes 20
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "workspace" / "battlefield.sqlite3"
GOAL = "孤儿树自恢复验证（脚本自动创建，结束后会 cancel）。"


def call(base_url: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def watchdog_state(base_url: str, root_id: str) -> dict:
    return call(base_url, "GET", f"/api/watchdog/state?root_id={root_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="workspace 里的 SQLite 路径")
    parser.add_argument("--age-minutes", type=int, default=20, help="把最后活动时间往回拨多少分钟")
    parser.add_argument("--progress-seconds", type=float, default=20.0, help="kick 后最多等多久看它推进")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    try:
        call(base_url, "GET", "/api/health")
    except (urllib.error.URLError, OSError) as exc:
        print(f"API 不可用（{base_url}）：{exc}")
        return 2
    if not pathlib.Path(args.db).is_file():
        print(f"找不到 workspace 数据库：{args.db}（先启动过一次 API 才会创建）")
        return 2

    draft = call(base_url, "POST", "/api/constitutions/draft", {"goal": GOAL, "runner_mode": "mock"})
    call(base_url, "POST", f"/api/constitutions/{draft['id']}/lock")
    started = call(
        base_url,
        "POST",
        "/api/projects/start",
        {"goal": GOAL, "runner_mode": "mock", "constitution_id": draft["id"], "auto_run": False},
    )
    root_id = started["root_id"]
    print(f"验证用任务树：{root_id}")

    before = watchdog_state(base_url, root_id)
    print(
        f"等待前：orphaned={before['orphaned_active_tree']} 前线={before['active_frontier']} "
        f"年龄={before['last_activity_age_seconds']}"
    )

    connection = sqlite3.connect(args.db, timeout=30)
    try:
        connection.execute(
            "UPDATE task_nodes SET updated_at = datetime('now', ?) WHERE updated_at IS NOT NULL",
            (f"-{max(1, args.age_minutes)} minutes",),
        )
        connection.commit()
    finally:
        connection.close()

    after = watchdog_state(base_url, root_id)
    print(
        f"回拨 {args.age_minutes} 分钟后：orphaned={after['orphaned_active_tree']} "
        f"should_resume={after['should_resume']} 建议动作={after['recommended_action']}"
    )
    recognized = bool(after["orphaned_active_tree"]) and after["recommended_action"] == "resume_orphaned"
    print("认出孤儿树：" + ("OK" if recognized else "失败"))

    call(base_url, "POST", f"/api/tasks/{root_id}/kick")
    deadline = time.monotonic() + max(1.0, args.progress_seconds)
    status = ""
    while time.monotonic() < deadline:
        tree = call(base_url, "GET", f"/api/tree?root_id={root_id}")
        root = next((task for task in tree["tasks"] if task["id"] == root_id), None)
        status = str(root["status"]) if root else ""
        if status in {"done", "blocked", "failed", "canceled"}:
            break
        time.sleep(2)
    resumed = status in {"done", "blocked", "failed"}
    print(f"kick 之后 root 状态：{status or '未知'}")
    print("能接着干：" + ("OK" if resumed else "失败"))

    call(base_url, "POST", f"/api/tasks/{root_id}/cancel")
    print("已 cancel 掉验证用任务树")

    ok = recognized and resumed
    print("RESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
