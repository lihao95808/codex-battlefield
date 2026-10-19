"""把装配体变成图：写 GLB → 调 Blender 无头渲染 → **核对产物真的存在且非空**。

失效方向（AGENTS.md 硬约定 2/5）：
- 找不到 Blender → 明确报错（fail closed），不返回"渲染成功但没有图"。
- Blender 退出码为 0 也不算成功：必须逐张核对 PNG 存在且大于最小字节数。
  反例来自本仓库历史：只看退出码/只看路径存在，会把"没出图"读成"已出图"。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import struct
import uuid
from pathlib import Path
from typing import Any

from .assembly import PlacedPart
from .boxes import Box
from .builders import MATERIAL
from .declaration import Declaration
from .glb import write_glb

MIN_PNG_BYTES = 5000
DEFAULT_VIEWS = {
    "hero": {"yaw": 4.05, "pitch": 0.34, "dist": 42.0, "target": [5.0, 4.0, 5.0], "lens": 45},
    "gate": {"yaw": 4.30, "pitch": 0.18, "dist": 30.0, "target": [4.8, 1.4, 4.4], "lens": 42},
    "top": {"yaw": 3.62, "pitch": 0.95, "dist": 46.0, "target": [5.0, 4.2, 4.6], "lens": 45},
}

BLENDER_CANDIDATES = (
    r"D:\SOFT\Media\Blender",
    r"C:\Program Files\Blender Foundation",
    r"C:\Program Files (x86)\Blender Foundation",
)


class RenderError(RuntimeError):
    """渲染不可用或产物缺失。出现即阻断（不产出"假装成功"的图）。"""


def find_blender(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise RenderError(f"blender not found at {explicit}")
    env = os.environ.get("CODEX_BATTLEFIELD_BLENDER")
    if env:
        p = Path(env)
        if p.is_file():
            return p
        raise RenderError(f"CODEX_BATTLEFIELD_BLENDER points to a missing file: {env}")
    which = shutil.which("blender")
    if which:
        return Path(which)
    found: list[Path] = []
    for root in BLENDER_CANDIDATES:
        base = Path(root)
        if not base.is_dir():
            continue
        for cand in sorted(base.glob("*/blender.exe")):
            found.append(cand)
        direct = base / "blender.exe"
        if direct.is_file():
            found.append(direct)
    if not found:
        raise RenderError(
            "blender not found: pass --blender, set CODEX_BATTLEFIELD_BLENDER, or put blender.exe on PATH"
        )
    # 取版本号最大的那个（目录名形如 5.2.0）
    found.sort(key=lambda p: [int(x) for x in p.parent.name.split(".") if x.isdigit()] or [0], reverse=True)
    return found[0]


def _eye(yaw: float, pitch: float, dist: float, target: list[float]) -> list[float]:
    import math

    d = [math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)]
    return [round(target[i] + d[i] * dist, 3) for i in range(3)]


def material_groups(decl: Declaration, placed: list[PlacedPart]) -> dict[int, list[Box]]:
    """按材质码分组。塔身/塔顶分开（塔顶用瓦色）。"""
    groups: dict[int, list[Box]] = {}
    for part in placed:
        slot = decl.by_name(part.slot)
        code = MATERIAL.get(part.builder, 1)
        if part.builder == "tower" and slot is not None:
            top = slot.mount.origin[2] + float(slot.expect.get("height", 0.0)) - 1e-6
            body = [b for b in part.world if b.z0 < top]
            cap = [b for b in world_cap(part, top)]
            groups.setdefault(code, []).extend(body)
            groups.setdefault(5, []).extend(cap)
        else:
            groups.setdefault(code, []).extend(part.world)
    return groups


def world_cap(part: PlacedPart, top: float) -> list[Box]:
    return [b for b in part.world if b.z0 >= top]


def render(
    decl: Declaration,
    placed: list[PlacedPart],
    out_dir: Path,
    views: list[str] | None = None,
    blender: str | None = None,
    resolution: tuple[int, int] = (1280, 880),
    samples: int = 48,
    timeout: int = 900,
) -> dict[str, Any]:
    # Each render owns a fresh directory. Existing images cannot stand in for this run.
    out_dir = out_dir.resolve() / f"render-{uuid.uuid4().hex}"
    out_dir.mkdir(parents=True, exist_ok=False)
    blender_exe = find_blender(blender)

    glb_path = write_glb(out_dir / "scene.glb", material_groups(decl, placed), name=decl.name)
    if glb_path.stat().st_size < 1024:
        raise RenderError(f"scene.glb is suspiciously small: {glb_path.stat().st_size} bytes")

    names = views or ["hero", "gate", "top"]
    unknown = [v for v in names if v not in DEFAULT_VIEWS]
    if unknown:
        raise RenderError(f"unknown view(s) {unknown}; available: {sorted(DEFAULT_VIEWS)}")
    view_cfg = []
    for name in names:
        spec = DEFAULT_VIEWS[name]
        view_cfg.append({
            "name": name,
            "target": spec["target"],
            "eye": _eye(spec["yaw"], spec["pitch"], spec["dist"], spec["target"]),
            "lens": spec["lens"],
        })

    cfg_path = out_dir / "render_cfg.json"
    cfg_path.write_text(
        json.dumps(
            {"out_dir": str(out_dir), "glb": str(glb_path), "views": view_cfg, "resolution": list(resolution), "samples": samples},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    script = Path(__file__).with_name("blender_script.py")
    log_path = out_dir / "blender.log"
    # stdout 必须重定向到文件：capture_output 会等管道 EOF，而 Blender 的孙进程可能攥着写端（见 AGENTS.md）
    with log_path.open("wb") as log:
        proc = subprocess.run(
            [str(blender_exe), "-b", "-P", str(script), "--", "--config", str(cfg_path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )

    if proc.returncode != 0:
        raise RenderError(f"Blender exited with {proc.returncode}; see {log_path}")
    produced: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for name in names:
        png = out_dir / f"scene3d_{name}.png"
        if not png.exists():
            missing.append(f"{name}: 文件不存在")
            continue
        size = png.stat().st_size
        if size < MIN_PNG_BYTES:
            missing.append(f"{name}: 只有 {size} 字节（< {MIN_PNG_BYTES}），多半是空白图")
            continue
        with png.open("rb") as image:
            header = image.read(24)
        if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
            missing.append(f"{name}: invalid PNG header")
            continue
        if struct.unpack(">II", header[16:24]) != resolution:
            missing.append(f"{name}: PNG dimensions do not match requested resolution")
            continue
        produced[name] = {"path": str(png), "bytes": size}
    if missing:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        raise RenderError(f"渲染产物不合格: {missing}\n--- blender log tail ---\n{tail}")

    return {
        "ok": True,
        "blender": str(blender_exe),
        "returncode": proc.returncode,
        "glb": str(glb_path),
        "glb_bytes": glb_path.stat().st_size,
        "images": produced,
        "log": str(log_path),
    }
