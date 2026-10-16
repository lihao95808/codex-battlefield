"""给 E2E 造一棵**千节点**树 —— "千节点下界面首屏 < 2s"这条判据需要一个真规模。

## 为什么不走 API

产品**没有**"批量造节点"的接口，这是刻意的：造树是编排器的职责，给它开一条批量写入口
等于给状态机开旁路。所以这里直接写库，和 `api/tests/test_scale.py` 的夹具**同形**
（根 + 每层 4 叉 + 叶子带运行记录）—— 两棵树形状不同的话，"后端 130ms"与"前端首屏"
就不是同一件事的两个数字了。

## 为什么单独一条命令、而不是塞进 spec

`web/e2e/perf-first-screen.spec.ts` 用 `execFileSync` 调它。分开的理由：脚本能单独跑、
能用真实数据核对（见 `--nodes`/`--root-id`），而 spec 里不该出现 SQL。

## 用法

    python web/e2e/seed_scale_tree.py \
        --state-home web/.e2e/state \
        --database-url "sqlite:///D:/work/codex-battlefield/web/.e2e/state/e2e.sqlite3" \
        --nodes 1000 --root-id perf-root-1000

可重复执行：会先删掉同一 `--root-id` 前缀的旧数据（含运行记录），再重建。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
API_DIR = REPO_ROOT / "api"

# 状态与库由环境变量决定，**必须在 import app 之前设好**：`app.config` 在导入时求值。
STATUS_CYCLE = ("queued", "working", "verifying", "blocked", "done")


def _prepare_env(state_home: Path, database_url: str) -> None:
    os.environ["CODEX_BATTLEFIELD_STATE_HOME"] = str(state_home)
    os.environ["CODEX_BATTLEFIELD_DATABASE_URL"] = database_url
    # 这条 fixture 只碰库，不该顺带接上真实 runner（少一次"E2E 偷偷调真模型"的可能）。
    os.environ.setdefault("CODEX_BATTLEFIELD_RUNNER", "mock")
    if str(API_DIR) not in sys.path:
        sys.path.insert(0, str(API_DIR))


def seed(state_home: Path, database_url: str, *, nodes: int, root_id: str) -> dict[str, object]:
    _prepare_env(state_home, database_url)

    from sqlalchemy import delete, func, select

    from app.database import SessionLocal, init_db
    from app.models import EventRecord, RunRecord, TaskNode, TaskStatus

    init_db()

    # 幂等：先清掉同一 root 前缀的旧节点与它们的所有引用行。
    #
    # ⚠️ 删除顺序不是风格问题：`task_nodes.parent_id` 是**自引用外键**，SQLite 的约束是
    # 立即检查的 —— 一条 `DELETE` 里父行先于子行被删就会
    # `FOREIGN KEY constraint failed`（实测第二次跑就撞上了，第一次跑没有旧数据所以看不出来）。
    # 所以按 depth **从深到浅**逐层删；`run_records`/`event_records` 也必须先删。
    with SessionLocal() as db:
        deepest = db.execute(
            select(func.max(TaskNode.depth)).where(TaskNode.id.like(f"{root_id}%"))
        ).scalar_one_or_none()
        if deepest is not None:
            db.execute(delete(RunRecord).where(RunRecord.task_id.like(f"{root_id}%")))
            db.execute(delete(EventRecord).where(EventRecord.task_id.like(f"{root_id}%")))
            for depth in range(int(deepest), -1, -1):
                db.execute(
                    delete(TaskNode).where(TaskNode.id.like(f"{root_id}%"), TaskNode.depth == depth)
                )
            db.commit()

    with SessionLocal() as db:
        db.add(
            TaskNode(
                id=root_id,
                title="千节点规模根",
                description="",
                depth=0,
                status=TaskStatus.WAITING_CHILDREN.value,
            )
        )
        made = 1
        level = [root_id]
        depth = 1
        leaf_ids: list[str] = []
        while made < nodes:
            nxt: list[str] = []
            for parent in level:
                if made >= nodes:
                    break
                for index in range(4):
                    if made >= nodes:
                        break
                    node_id = f"{root_id}-n{depth}-{made:05d}-{index}"
                    db.add(
                        TaskNode(
                            id=node_id,
                            parent_id=parent,
                            title=f"任务 {made}",
                            description="",
                            depth=depth,
                            status=STATUS_CYCLE[made % len(STATUS_CYCLE)],
                            scope_json=json.dumps([f"src/module_{made % 50}.gd"]),
                        )
                    )
                    nxt.append(node_id)
                    leaf_ids.append(node_id)
                    made += 1
            level = nxt
            depth += 1
            if not level:
                break
        db.commit()

    # 运行记录单独一个 session：与 `test_scale.py` 同一个理由 ——
    # 未提交事务里插 run_records 时 SQLite 的外键检查看不到父行（实测踩到）。
    with SessionLocal() as db:
        targets = leaf_ids[:8] or [root_id]
        for index in range(min(400, nodes)):
            db.add(
                RunRecord(
                    task_id=targets[index % len(targets)],
                    kind=["worker", "verifier", "acceptance", "planner"][index % 4],
                    status="succeeded",
                    command="codex exec",
                    sandbox="workspace-write",
                    exit_code=0,
                    usage_json=json.dumps(
                        {
                            "model": ["cheap", "strong"][index % 2],
                            "input_tokens": 100 + index,
                            "output_tokens": 10,
                            "cost": {"amount": 0.001 * index, "currency": "USD"},
                        }
                    ),
                )
            )
        db.commit()
        total = len(list(db.execute(select(TaskNode.id).where(TaskNode.id.like(f"{root_id}%")))))
        runs = len(list(db.execute(select(RunRecord.id))))

    return {"root_id": root_id, "nodes": total, "runs": runs}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给 E2E 造千节点树（只写库，不发请求）。")
    parser.add_argument("--state-home", required=True, help="E2E 的状态根（与后端同一个）")
    parser.add_argument("--database-url", required=True, help="与后端同一个库 URL")
    parser.add_argument("--nodes", type=int, default=1000, help="节点总数（含根）")
    parser.add_argument("--root-id", default="perf-root-1000", help="根节点 id（会被前端 URL 用）")
    args = parser.parse_args(argv)

    if args.nodes < 2:
        print("--nodes 至少要 2（根 + 一个子节点），否则量不出什么", file=sys.stderr)
        return 2

    summary = seed(Path(args.state_home), args.database_url, nodes=args.nodes, root_id=args.root_id)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
