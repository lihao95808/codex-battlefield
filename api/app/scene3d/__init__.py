"""scene3d：3D 场景的声明 / 校验 / 度量 / 渲染。

对外只有这几件事：
    castle_declaration()  内置石堡声明（L0 的样例产物）
    evaluate()            合规 + 几何两段判定，返回结构化拒绝
    compare()             相对参考答案的 IoU / 越界
    render()              Blender 无头渲染，核对产物真的存在
判据的失效方向写在各自模块顶部；本包不依赖 numpy / trimesh / manifold3d。
"""

from __future__ import annotations

from .assembly import AssemblyError, PlacedPart, connectivity, place
from .castle import castle_declaration
from .checks import evaluate, run_checks, validate_parts
from .declaration import Declaration, DeclarationError, reference_parts
from .metrics import compare
from .render import RenderError, find_blender, render

__all__ = [
    "AssemblyError",
    "Declaration",
    "DeclarationError",
    "PlacedPart",
    "RenderError",
    "castle_declaration",
    "compare",
    "connectivity",
    "evaluate",
    "find_blender",
    "place",
    "reference_parts",
    "render",
    "run_checks",
    "validate_parts",
]
