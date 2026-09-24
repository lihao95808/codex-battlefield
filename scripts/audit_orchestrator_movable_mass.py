"""可搬质量普查：用 AST 建方法依赖图，测算模块化拆分真正可达的行数。

用途：在开始一轮「只搬移」的重构前，先测出**可搬的行数**，而不是按方法组的
表面行数估算——两者在耦合的类里能差一个数量级。

判据（保守）
  一个方法「可搬」⟺ 它通过 `self.<方法>` 传递依赖到的所有方法都可搬。
  基础种子 = 完全不调用其它同类方法的那些（只用自身参数、纯函数，以及
  `self.settings.*` 这类可改为显式参数的配置标量）。
  因此任何触达核心基础设施（`_mark` / `_get_task` / `_add_event` / `_subtree` /
  `serialize_*` 等）的方法都会被判为不可搬。

用法
  python scripts/audit_orchestrator_movable_mass.py [目标文件] [--delegate-lines N]

输出
  可搬 / 不可搬的方法数与行数；全部搬移后目标文件的预计行数；以及构成耦合核心的
  方法排行，用来判断"不可搬性来自哪里"。
"""

from __future__ import annotations

import argparse
import ast
import pathlib
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_TARGET = ROOT / "api" / "app" / "orchestrator.py"


def body_lines(node: ast.AST) -> int:
    start = node.lineno
    end = max(getattr(child, "end_lineno", node.lineno) for child in ast.walk(node))
    return end - start + 1


def analyse(target: pathlib.Path, delegate_lines: int) -> dict[str, object]:
    text = target.read_text(encoding="utf-8")
    tree = ast.parse(text)
    total_lines = len(text.splitlines())

    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    results: dict[str, object] = {"total_lines": total_lines, "classes": {}}

    for cls in classes:
        methods = {
            node.name: node
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        if len(methods) < 20:
            continue
        method_names = set(methods)

        edges: dict[str, set[str]] = {}
        for name, node in methods.items():
            found: set[str] = set()
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                    and sub.attr in method_names
                    and sub.attr != name
                ):
                    found.add(sub.attr)
            edges[name] = found

        movable = {name for name, deps in edges.items() if not deps}
        changed = True
        while changed:
            changed = False
            for name, deps in edges.items():
                if name in movable:
                    continue
                if deps <= movable:
                    movable.add(name)
                    changed = True

        unmovable = method_names - movable
        movable_lines = sum(body_lines(methods[n]) for n in movable)
        unmovable_lines = sum(body_lines(methods[n]) for n in unmovable)
        projected = total_lines - movable_lines + delegate_lines * len(movable)

        # "可搬"只回答它不调用别的同类方法；但若它读写了 self 上的**实例状态**
        # （缓存、信号量、预留表…），改写成自由函数就必须把这些状态穿进穿出——
        # 那已经不是搬移，而是改变状态所有权。所以状态耦合必须单独测。
        state_attrs: dict[str, set[str]] = {}
        for name, node in methods.items():
            found: set[str] = set()
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Attribute)
                    and isinstance(sub.value, ast.Name)
                    and sub.value.id == "self"
                    and sub.attr not in method_names
                ):
                    found.add(sub.attr)
            state_attrs[name] = found

        fan_in = Counter()
        for deps in edges.values():
            for dep in deps:
                if dep in unmovable:
                    fan_in[dep] += 1

        results["classes"][cls.name] = {
            "methods": len(methods),
            "movable": len(movable),
            "movable_names": sorted(movable),
            "edges": edges,
            "state_attrs": state_attrs,
            "line_counts_all": {name: body_lines(node) for name, node in methods.items()},
            "movable_lines": movable_lines,
            "unmovable": len(unmovable),
            "unmovable_lines": unmovable_lines,
            "projected_lines": projected,
            "fan_in": fan_in,
            "line_counts": {name: body_lines(node) for name, node in methods.items()},
        }
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="测算模块化拆分真正可达的行数。")
    parser.add_argument("target", nargs="?", default=str(DEFAULT_TARGET))
    parser.add_argument("--delegate-lines", type=int, default=2, help="每个搬走的方法留下几行委托。")
    parser.add_argument("--names", default="", help="逗号分隔的方法名；只回答这些方法是否可搬。")
    parser.add_argument(
        "--closure",
        default="",
        help="逗号分隔的入口方法名；打印把它们整体抽出所需的传递依赖闭包（即模块必须包含哪些方法）。",
    )
    parser.add_argument(
        "--state",
        action="store_true",
        help="配合 --closure：额外列出闭包成员读写的 self 实例状态，用来判断能否改写成自由函数。",
    )
    args = parser.parse_args()

    target = pathlib.Path(args.target)
    report = analyse(target, args.delegate_lines)

    if args.closure:
        entries = [name.strip() for name in args.closure.split(",") if name.strip()]
        for data in report["classes"].values():  # type: ignore[union-attr]
            edges = data["edges"] or {}
            counts = data["line_counts_all"] or {}
            movable_names = set(data["movable_names"])  # type: ignore[arg-type]

            closure: set[str] = set()
            stack = [name for name in entries if name in edges]
            missing = [name for name in entries if name not in edges]
            while stack:
                current = stack.pop()
                if current in closure:
                    continue
                closure.add(current)
                stack.extend(edges[current])

            unmovable_hit = sorted(closure - movable_names)
            total = sum(counts.get(name, 0) for name in closure)
            print(f"entries      : {', '.join(entries)}")
            if missing:
                print(f"not found    : {', '.join(missing)}")
            print(f"closure size : {len(closure)} methods, {total} body lines")
            print(f"reaches unmovable: {bool(unmovable_hit)}  {unmovable_hit[:8]}")
            print()
            for name in sorted(closure, key=lambda n: counts.get(n, 0), reverse=True):
                flag = " " if name in movable_names else "!"
                print(f"  {flag} {counts.get(name, 0):>5} lines  {name}")

            if args.state:
                state_attrs = data["state_attrs"] or {}
                touched: Counter = Counter()
                print()
                print("-- instance state touched by closure (blocks free-function extraction) --")
                blocking: list[tuple[str, list[str]]] = []
                for name in sorted(closure, key=lambda n: counts.get(n, 0), reverse=True):
                    attrs = sorted(state_attrs.get(name, set()))
                    # settings / 已有的可搬门面不算障碍，它们是显式参数化的通道
                    attrs = [a for a in attrs if a != "settings"]
                    for attr in attrs:
                        touched[attr] += 1
                    if attrs:
                        blocking.append((name, attrs))
                for name, attrs in blocking:
                    print(f"  {counts.get(name, 0):>5} lines  {name}: {', '.join(attrs)}")
                clean = len(closure) - len(blocking)
                print()
                print(f"  methods with zero instance-state use: {clean}/{len(closure)}")
                print(f"  most shared state: {', '.join(f'{a}({c})' for a, c in touched.most_common(10))}")
        return 0

    if args.names:
        wanted = [name.strip() for name in args.names.split(",") if name.strip()]
        for data in report["classes"].values():  # type: ignore[union-attr]
            counts = data["line_counts"]
            movable_names = set(data["movable_names"])  # type: ignore[arg-type]
            for name in wanted:
                if name not in counts:
                    print(f"  {name:<44} (not found on this class)")
                    continue
                verdict = "MOVABLE  " if name in movable_names else "unmovable"
                print(f"  {verdict} {name:<44} {counts[name]} lines")
        return 0

    print(f"target: {target}")
    print(f"file lines: {report['total_lines']}")

    for class_name, data in report["classes"].items():  # type: ignore[union-attr]
        print()
        print(f"=== {class_name} ===")
        print(f"  methods            : {data['methods']}")
        print(f"  movable            : {data['movable']}  ({data['movable_lines']} body lines)")
        print(f"  unmovable          : {data['unmovable']}  ({data['unmovable_lines']} body lines)")
        print(f"  projected file size: {data['projected_lines']}")

        counts = data["line_counts"]
        print()
        print("  -- top coupling points (unmovable, most depended upon) --")
        for name, count in data["fan_in"].most_common(12):
            print(f"     {count:>4} dependents  {name:<42} {counts[name]} lines")
        print()
        print("  -- largest unmovable methods (the real core) --")
        for name in sorted(data["fan_in"], key=lambda n: counts[n], reverse=True)[:12]:
            print(f"     {counts[name]:>5} lines  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
