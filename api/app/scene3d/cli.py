"""命令行入口：把 3D 判据做成战场可调用的本地工具。

  python -m app.scene3d declaration --out castle.declaration.json
  python -m app.scene3d reference   --declaration d.json --out parts.json
  python -m app.scene3d validate    --declaration d.json --parts p.json [--json]
  python -m app.scene3d metrics     --declaration d.json --parts p.json
  python -m app.scene3d render      --declaration d.json --parts p.json --out <dir>

退出码（门禁要认这三个数，别发明第四种）：
  0  通过
  2  判定为不通过（声明/填写/几何任一阶段）
  3  工具自身出错（声明读不出、Blender 不可用等）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .assembly import AssemblyError, place
from .castle import castle_declaration
from .checks import evaluate
from .declaration import Declaration, DeclarationError, reference_parts
from .metrics import compare
from .render import RenderError, render

EXIT_OK = 0
EXIT_REJECT = 2
EXIT_ERROR = 3

SCENES = {"castle": castle_declaration}


def _load_decl(path: Path) -> Declaration:
    return Declaration.from_path(path)


def _load_parts(path: Path):
    if not path.exists():
        raise ValueError(f"parts file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _cmd_declaration(args) -> int:
    if args.scene not in SCENES:
        print(f"unknown scene {args.scene!r}; available: {sorted(SCENES)}", file=sys.stderr)
        return EXIT_ERROR
    text = SCENES[args.scene]().dumps()
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote declaration: {args.out} ({len(text)} bytes)")
    else:
        print(text)
    return EXIT_OK


def _cmd_describe(args) -> int:
    """Read only the contract inputs; never fill free parameters or emit a reference answer."""
    decl = _load_decl(Path(args.declaration))
    print(json.dumps({
        "name": decl.name,
        "check_count": len(decl.checks),
        "integer_parameters": ["tiers", "planks", "steps", "mullions", "rails"],
        "slots": [{"slot": s.name, "builder": s.builder, "expect": s.expect,
                   "free": list(s.free), "domain": {k: list(v) for k, v in s.domain.items()}}
                  for s in decl.slots],
    }, ensure_ascii=True, separators=(",", ":")))
    return EXIT_OK


def _cmd_reference(args) -> int:
    decl = _load_decl(Path(args.declaration))
    parts = reference_parts(decl)
    text = json.dumps({"parts": parts}, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote reference parts: {args.out} ({len(parts)} slots)")
    else:
        print(text)
    return EXIT_OK


def _cmd_validate(args) -> int:
    decl = _load_decl(Path(args.declaration))
    raw = _load_parts(Path(args.parts))
    parts = raw.get("parts") if isinstance(raw, dict) else raw
    result = evaluate(decl, parts, samples=args.samples)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"stage={result['stage']} ok={result['ok']} rejects={len(result['rejects'])}")
        for c in result.get("checks", []):
            mark = "ok  " if c["ok"] else "FAIL"
            print(f"  [{mark}] {c['id']:<28} actual={c['actual']!s:<10} expect={c['expected']}")
        for r in result["rejects"]:
            if result["stage"] != "checks":
                print(f"  REJECT {r['slot']}.{r['field']}: expected={r['expected']!r} actual={r['actual']!r} ({r['rule']})")
    return EXIT_OK if result["ok"] else EXIT_REJECT


def _cmd_audit(args) -> int:
    """冻结期审计的离线入口：负样本 + 合法域相容性。"""
    from .audit import audit_declaration

    decl = _load_decl(Path(args.declaration))
    report = audit_declaration(decl, samples=args.samples)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK if report["ok"] else EXIT_REJECT


def _cmd_merge(args) -> int:
    from .contract import merge_bundles, parse_contract

    root = Path(args.root).resolve()
    contract = parse_contract(Path(args.contract).read_bytes(), root)
    inputs = {}
    for item in args.bundle:
        if "=" not in item:
            print(f"error: --bundle 需要 id=path 形式：{item!r}", file=sys.stderr)
            return EXIT_ERROR
        bid, path = item.split("=", 1)
        inputs[bid] = (root / path).resolve()
    try:
        merged = merge_bundles(contract, root, inputs)
    except ValueError as exc:
        print(f"merge rejected: {exc}", file=sys.stderr)
        return EXIT_REJECT
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"parts": merged}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": True, "slots": len(merged), "out": str(out)}, ensure_ascii=True))
    return EXIT_OK


def _cmd_metrics(args) -> int:
    decl = _load_decl(Path(args.declaration))
    raw = _load_parts(Path(args.parts))
    parts = raw.get("parts") if isinstance(raw, dict) else raw
    cand = place(decl, parts)
    target = place(decl, reference_parts(decl))
    print(json.dumps(compare(cand, target, samples=args.samples), ensure_ascii=False, indent=2))
    return EXIT_OK


def _cmd_render(args) -> int:
    decl = _load_decl(Path(args.declaration))
    raw = _load_parts(Path(args.parts))
    parts = raw.get("parts") if isinstance(raw, dict) else raw
    validation = evaluate(decl, parts)
    if not validation["ok"]:
        print(json.dumps(validation, ensure_ascii=False, indent=2))
        return EXIT_REJECT
    placed = place(decl, parts)
    views = [v.strip() for v in args.views.split(",") if v.strip()] if args.views else None
    out = render(
        decl,
        placed,
        Path(args.out),
        views=views,
        blender=args.blender,
        resolution=(args.width, args.height),
        samples=args.samples,
    )
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.scene3d", description="3D 场景声明 / 校验 / 渲染")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("declaration", help="写出内置场景的声明（L0 用）")
    d.add_argument("--scene", default="castle")
    d.add_argument("--out")
    d.set_defaults(func=_cmd_declaration)

    info = sub.add_parser("describe", help="compact read-only slot/parameter contract, no reference filling")
    info.add_argument("--declaration", required=True)
    info.set_defaults(func=_cmd_describe)

    r = sub.add_parser("reference", help="把声明翻成一份参考答案（free 参数取域中点）")
    r.add_argument("--declaration", required=True)
    r.add_argument("--out")
    r.set_defaults(func=_cmd_reference)

    v = sub.add_parser("validate", help="校验填写与几何；不通过退出码 2")
    v.add_argument("--declaration", required=True)
    v.add_argument("--parts", required=True)
    v.add_argument("--samples", type=int, default=512)
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=_cmd_validate)

    au = sub.add_parser("audit", help="冻结期审计：恒真检查与合法参数域相容性")
    au.add_argument("--declaration", required=True)
    au.add_argument("--samples", type=int, default=64)
    au.set_defaults(func=_cmd_audit)

    mg = sub.add_parser("merge", help="确定性合并 bundle 文件为完整 parts（门不做合并，只有这里做）")
    mg.add_argument("--contract", required=True)
    mg.add_argument("--root", default=".")
    mg.add_argument("--bundle", action="append", required=True, help="id=相对路径，可重复")
    mg.add_argument("--out", required=True)
    mg.set_defaults(func=_cmd_merge)

    m = sub.add_parser("metrics", help="相对参考答案的 IoU / 越界")
    m.add_argument("--declaration", required=True)
    m.add_argument("--parts", required=True)
    m.add_argument("--samples", type=int, default=12000)
    m.set_defaults(func=_cmd_metrics)

    g = sub.add_parser("render", help="Blender 无头渲染出图")
    g.add_argument("--declaration", required=True)
    g.add_argument("--parts", required=True)
    g.add_argument("--out", required=True)
    g.add_argument("--views", help="逗号分隔，默认 hero,gate,top")
    g.add_argument("--blender")
    g.add_argument("--width", type=int, default=1280)
    g.add_argument("--height", type=int, default=880)
    g.add_argument("--samples", type=int, default=48)
    g.set_defaults(func=_cmd_render)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (DeclarationError, AssemblyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except RenderError as exc:
        print(f"render error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
