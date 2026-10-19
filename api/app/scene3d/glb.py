"""纯 Python GLB 写出（不做 CSG，直接按盒出三角形）。

为什么自己写而不引第三方库：战场 API 的依赖只有 FastAPI/SQLAlchemy/Pydantic。
一个预览用的 GLB 只需要 position + index 两个 accessor，规范本身只有一页；
引入 trimesh/manifold3d 会让"能不能出图"变成"能不能装包"。
重叠盒之间的内部面留在模型里，但不透明渲染下看不见 —— 预览足够。
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

from .boxes import Box

# 盒的 8 个顶点与 12 个三角形（外法向一致，便于只用背面剔除的查看器）
_CORNERS = (
    (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
    (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1),
)
_QUADS = (
    (3, 2, 1, 0),  # z-
    (4, 5, 6, 7),  # z+
    (0, 1, 5, 4),  # y-
    (7, 6, 2, 3),  # y+
    (4, 7, 3, 0),  # x-
    (1, 2, 6, 5),  # x+
)

MATERIAL_RGBA = {
    1: (0.42, 0.39, 0.33, 1.0),
    2: (0.56, 0.53, 0.46, 1.0),
    3: (0.14, 0.07, 0.03, 1.0),
    4: (0.05, 0.055, 0.065, 1.0),
    5: (0.09, 0.10, 0.15, 1.0),
}


def _boxes_to_arrays(boxes: list[Box]) -> tuple[list[float], list[int]]:
    pos: list[float] = []
    idx: list[int] = []
    for b in boxes:
        base = len(pos) // 3
        for cx, cy, cz in _CORNERS:
            pos.extend((b.x0 + (b.x1 - b.x0) * cx, b.y0 + (b.y1 - b.y0) * cy, b.z0 + (b.z1 - b.z0) * cz))
        for q in _QUADS:
            a, bb, c, d = (base + i for i in q)
            idx.extend((a, bb, c, a, c, d))
    return pos, idx


def write_glb(path: Path, groups: dict[int, list[Box]], name: str = "scene") -> Path:
    """把 {材质码: [盒...]} 写成一个 GLB。返回路径。"""
    bin_parts: list[bytes] = []
    accessors: list[dict] = []
    buffer_views: list[dict] = []
    materials: list[dict] = []
    primitives: list[dict] = []
    offset = 0
    mat_index: dict[int, int] = {}

    for code, boxes in sorted(groups.items()):
        if not boxes:
            continue
        pos, idx = _boxes_to_arrays(boxes)
        pos_bytes = struct.pack(f"<{len(pos)}f", *pos)
        idx_bytes = struct.pack(f"<{len(idx)}I", *idx)

        buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(pos_bytes), "target": 34962})
        accessors.append({
            "bufferView": len(buffer_views) - 1, "componentType": 5126, "count": len(pos) // 3, "type": "VEC3",
            "min": [min(pos[0::3]), min(pos[1::3]), min(pos[2::3])],
            "max": [max(pos[0::3]), max(pos[1::3]), max(pos[2::3])],
        })
        pos_acc = len(accessors) - 1
        bin_parts.append(pos_bytes)
        offset += len(pos_bytes)

        buffer_views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(idx_bytes), "target": 34963})
        accessors.append({
            "bufferView": len(buffer_views) - 1, "componentType": 5125, "count": len(idx), "type": "SCALAR",
        })
        idx_acc = len(accessors) - 1
        bin_parts.append(idx_bytes)
        offset += len(idx_bytes)

        if code not in mat_index:
            materials.append({
                "pbrMetallicRoughness": {
                    "baseColorFactor": list(MATERIAL_RGBA.get(code, (0.6, 0.6, 0.6, 1.0))),
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.85,
                },
                "name": f"mat{code}",
            })
            mat_index[code] = len(materials) - 1
        primitives.append({"attributes": {"POSITION": pos_acc}, "indices": idx_acc, "material": mat_index[code], "mode": 4})

    if not primitives:
        raise ValueError("write_glb: nothing to write")

    gltf = {
        "asset": {"version": "2.0", "generator": "codex-battlefield/scene3d"},
        "scene": 0,
        "scenes": [{"nodes": [0], "name": name}],
        "nodes": [{"mesh": 0, "name": name}],
        "meshes": [{"primitives": primitives, "name": name}],
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": offset}],
    }
    bin_blob = b"".join(bin_parts)
    json_blob = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_blob += b" " * ((4 - len(json_blob) % 4) % 4)
    bin_blob += b"\x00" * ((4 - len(bin_blob) % 4) % 4)

    total = 12 + 8 + len(json_blob) + 8 + len(bin_blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total))
        fh.write(struct.pack("<II", len(json_blob), 0x4E4F534A))
        fh.write(json_blob)
        fh.write(struct.pack("<II", len(bin_blob), 0x004E4942))
        fh.write(bin_blob)
    return path
