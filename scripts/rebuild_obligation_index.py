r"""重建或检查债务派生索引（`obligation_index` 表），见 `api/app/obligation_index.py`。

```powershell
# 检查是否与 node_meta 一致（只读；漂移时退出码 1）
api\.venv\Scripts\python.exe scripts\rebuild_obligation_index.py --check
# 重建（表是派生缓存，真相在 node_meta）
api\.venv\Scripts\python.exe scripts\rebuild_obligation_index.py --rebuild
# 跨树按类型聚合（本表存在的唯一理由）
api\.venv\Scripts\python.exe scripts\rebuild_obligation_index.py --by-type
```

与 `bench_run.py` 同样的坑：**设库地址必须早于任何 `import app.*`**（`app.config` 的默认值在导入时求值）。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def prepare_imports() -> None:
    db_url = os.environ.get("CODEX_BATTLEFIELD_DATABASE_URL")
    if not db_url:
        # 必须与产品**算出来的**默认库一致。这里曾经写死 `codex_battlefield.sqlite3`
        # （产品默认是 `battlefield.sqlite3`），于是不带环境变量跑 `--check` 会去问一个
        # **不存在的空库**，输出 `projected: 0 / in_sync: true`，看起来正是"索引干净、没有债"。
        # 失效方向是最坏的那个：唯一的写入口自己报了一个假的"全绿"。
        #
        # 状态与工程解耦之后，位置**不再**能从仓库路径推出来（绑了工程就在状态根下），
        # 所以这里统一问 `app.config` 要它真正会用的那个 URL，而不是重新拼一遍。
        sys.path.insert(0, str(ROOT / "api"))
        from app.config import get_settings  # noqa: PLC0415

        os.environ.setdefault("CODEX_BATTLEFIELD_DATABASE_URL", get_settings().database_url)
    os.environ.setdefault("CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB", "0")
    sys.path.insert(0, str(ROOT / "api"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root-id", default="", help="只处理这一棵树（默认全库）")
    parser.add_argument("--rebuild", action="store_true", help="按 node_meta 重写索引行")
    parser.add_argument("--check", action="store_true", help="只读比对，漂移时退出码 1")
    parser.add_argument("--by-type", action="store_true", help="输出按类型的跨树聚合")
    parser.add_argument("--status", default="open", help="`--by-type` 的过滤：open / settled / 空=全都要")
    args = parser.parse_args()
    if not (args.rebuild or args.check or args.by_type):
        parser.error("至少要给一个动作：--rebuild / --check / --by-type")

    prepare_imports()
    from app import obligation_index
    from app.database import SessionLocal, init_db

    # 范围必须显式：问错范围过去会静默变成"`status == 'opne'` → 0 行 → total: 0"，
    # 与"真的没有债"长得一模一样（HTTP 那侧是 422，CLI 这侧不能更松）。空串沿用"全都要"。
    status = args.status.strip()
    if status and status not in obligation_index.STATUS_CHOICES:
        parser.error(f"--status 只能是 {sorted(obligation_index.STATUS_CHOICES)} 或空（全都要），收到 {args.status!r}")
    status_filter = obligation_index.STATUS_CHOICES.get(status, "")

    init_db()
    root_id = args.root_id.strip() or None
    exit_code = 0
    with SessionLocal() as db:
        if args.rebuild:
            print(json.dumps(obligation_index.rebuild(db, root_id), ensure_ascii=False, indent=2))
            db.commit()
        if args.by_type:
            print(json.dumps(obligation_index.cross_tree_by_type(db, status=status_filter), ensure_ascii=False, indent=2, default=str))
        if args.check:
            report = obligation_index.check(db, root_id)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not report["in_sync"]:
                print(
                    "索引与 node_meta 不一致：先跑 --rebuild 重建；**不要**直接信这张表（它是派生缓存）。",
                    file=sys.stderr,
                )
                exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
