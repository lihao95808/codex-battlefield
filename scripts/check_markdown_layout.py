"""文档排版自检：代码围栏配对 + 表格逐行对照表头列数 + 行尾空白。

**为什么放在 `scripts/` 而不是一次性脚本**：这三类错都是**改文档时改坏的**，而且坏得很安静——
表格少一个 `|` 在源码里看着还行，渲染出来整行错位；围栏少一个收尾会把后面整段吞掉。
仓库没有 markdown linter（也没有 python linter），所以这里给一条可复跑的判据：

    python scripts/check_markdown_layout.py .        # 全仓库（跳过 .git/.venv/node_modules/...）

退出码 1 表示发现问题，便于串进别的检查里。

单元切分按**未转义**的 `|` 走：表格单元里的 `\\|`（例如代码跨度里的 `dict \\| None`）是合法字面竖线，
不是列分隔符——第一版量具把它们当分隔符，报了假阳性，这条是那时踩出来的。
"""

from __future__ import annotations

import pathlib
import re
import sys

SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "__pycache__", "workspace", ".godot", ".pytest_cache", ".mypy_cache"}


def split_cells(stripped: str) -> list[str]:
    """按**未转义**的 `|` 切分表格行。"""
    body = stripped.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return re.split(r"(?<!\\)\|", body)


def check(path: pathlib.Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    problems: list[str] = []

    inside_fence = False
    open_fence_line: int | None = None
    table: list[tuple[int, list[str]]] = []

    def flush_table() -> None:
        if len(table) >= 2:
            header_count = len(table[0][1])
            for line_no, cells in table[1:]:
                if len(cells) != header_count:
                    snippet = lines[line_no - 1].strip()[:100]
                    problems.append(f"表格列数不一致：表头 {header_count} 列，L{line_no} 是 {len(cells)} 列 → {snippet}")
        table.clear()

    for index, line in enumerate(lines, start=1):
        if line.strip().startswith("```"):
            inside_fence = not inside_fence
            open_fence_line = index if inside_fence else None
            flush_table()
            continue
        if inside_fence:
            continue
        stripped = line.strip()
        if stripped.startswith("|") and len(stripped) > 1:
            table.append((index, split_cells(stripped)))
            continue
        flush_table()
        if line != line.rstrip():
            problems.append(f"行尾空白：L{index}")

    flush_table()
    if inside_fence:
        problems.append(f"L{open_fence_line} 的代码围栏没有收尾（后面整段会被当成代码）")
    return problems


def main() -> int:
    targets: list[pathlib.Path] = []
    for raw in sys.argv[1:]:
        path = pathlib.Path(raw)
        if path.is_dir():
            targets.extend(sorted(item for item in path.rglob("*.md") if not SKIP_DIRS & set(item.parts)))
        elif path.is_file():
            targets.append(path)
        else:
            print(f"[跳过] 不是文件也不是目录：{raw}")
    if not targets:
        print("用法：python scripts/check_markdown_layout.py <文件或目录> ...")
        return 2
    total = 0
    for path in targets:
        problems = check(path)
        if problems:
            total += len(problems)
            print(f"[问题] {path}")
            for item in problems:
                print(f"    - {item}")
        else:
            print(f"[OK] {path}")
    print(f"\n共检查 {len(targets)} 个 .md，发现问题 {total} 处")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
