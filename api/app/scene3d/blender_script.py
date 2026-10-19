"""在 Blender 内部运行的渲染脚本（**只能由 blender 执行，不要 import**）。

用法：blender.exe -b -P blender_script.py -- --config <render_cfg.json>

三条踩过的坑（2026-10-19 实测，均在下面修掉）：
1. glTF 是 Y-up、Blender 是 Z-up。写进文件的是我们自己的 Z-up 坐标，导入器又转一次，
   所以要绕 X 轴 -90° 转回来。
2. glTF 导入的物体 `rotation_mode` 是 QUATERNION，直接写 `rotation_euler` **无效**。
3. Blender 5.x 里默认材质的节点名不是 "Principled BSDF"（而且本机是本地化版本），
   按名字取会拿到 None，颜色写进孤立节点、渲染仍是白色 → 一律清空节点树、自己重建。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

PALETTE = {
    "mat1": dict(color=(0.335, 0.310, 0.262), rough=0.88, scale=24.0, bump=0.18, metallic=0.0),  # 石墙
    "mat2": dict(color=(0.455, 0.428, 0.362), rough=0.78, scale=32.0, bump=0.14, metallic=0.0),  # 石线脚
    "mat3": dict(color=(0.135, 0.068, 0.032), rough=0.64, scale=22.0, bump=0.42, metallic=0.0),  # 木
    "mat4": dict(color=(0.045, 0.052, 0.062), rough=0.34, scale=28.0, bump=0.12, metallic=0.9),  # 金属
    "mat5": dict(color=(0.085, 0.100, 0.150), rough=0.50, scale=9.0, bump=0.20, metallic=0.0),   # 塔顶瓦
}


def clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.lights, bpy.data.cameras, bpy.data.images):
        for b in list(coll):
            if b.users == 0:
                coll.remove(b)


def fresh_tree(idblock, kind: str):
    idblock.use_nodes = True
    nt = idblock.node_tree
    nt.nodes.clear()
    if kind == "world":
        out = nt.nodes.new("ShaderNodeOutputWorld")
        bg = nt.nodes.new("ShaderNodeBackground")
        nt.links.new(bg.outputs["Background"], out.inputs["Surface"])
        return nt, bg
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    return nt, bsdf


def procedural(name: str, color, rough: float, scale: float, bump: float, metallic: float):
    mat = bpy.data.materials.new(name)
    nt, bsdf = fresh_tree(mat, "material")
    bsdf.inputs["Roughness"].default_value = rough
    bsdf.inputs["Metallic"].default_value = metallic
    tex = nt.nodes.new("ShaderNodeTexNoise")
    tex.location = (-800, -100)
    tex.inputs["Scale"].default_value = scale
    tex.inputs["Detail"].default_value = 9.0
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.location = (-540, -100)
    e0, e1 = ramp.color_ramp.elements[0], ramp.color_ramp.elements[1]
    e0.position, e1.position = 0.30, 0.72
    e0.color = (color[0] * 0.74, color[1] * 0.74, color[2] * 0.74, 1.0)
    e1.color = (min(color[0] * 1.18, 1), min(color[1] * 1.18, 1), min(color[2] * 1.16, 1), 1.0)
    nt.links.new(tex.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
    bmp_nodes = nt.nodes.new("ShaderNodeBump")
    bmp_nodes.location = (-300, -420)
    bmp_nodes.inputs["Strength"].default_value = bump
    bmp_nodes.inputs["Distance"].default_value = 0.015
    nt.links.new(tex.outputs["Fac"], bmp_nodes.inputs["Height"])
    nt.links.new(bmp_nodes.outputs["Normal"], bsdf.inputs["Normal"])
    return mat


def build_materials():
    mats = {}
    for name, spec in PALETTE.items():
        mats[name] = procedural(name, **spec)
    return mats


def import_glb(path: Path, mats: dict):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    new = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    total = 0
    for o in new:
        o.rotation_mode = "XYZ"
        o.rotation_euler = (math.radians(-90), 0, 0)
        bpy.context.view_layer.objects.active = o
        o.select_set(True)
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        o.select_set(False)
        for poly in o.data.polygons:
            poly.use_smooth = False
        for i, slot in enumerate(o.data.materials):
            key = (slot.name if slot else "").split(".")[0]
            if key in mats:
                o.data.materials[i] = mats[key]
        bev = o.modifiers.new("bevel", "BEVEL")
        bev.width = 0.025
        bev.segments = 2
        bev.limit_method = "ANGLE"
        bev.angle_limit = math.radians(30)
        total += len(o.data.polygons)
    return new, total


def add_ground(size: float = 140.0, center=(5.0, 4.0)):
    bpy.ops.mesh.primitive_plane_add(size=size, location=(center[0], center[1], 0))
    plane = bpy.context.active_object
    mat = bpy.data.materials.new("ground")
    nt, bsdf = fresh_tree(mat, "material")
    checker = nt.nodes.new("ShaderNodeTexChecker")
    checker.inputs["Scale"].default_value = 3.0
    checker.inputs["Color1"].default_value = (0.205, 0.215, 0.150, 1)
    checker.inputs["Color2"].default_value = (0.175, 0.185, 0.128, 1)
    nt.links.new(checker.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.95
    plane.data.materials.append(mat)
    return plane


def add_lights():
    sun_data = bpy.data.lights.new("sun", "SUN")
    sun_data.energy = 3.0
    sun_data.angle = math.radians(2.5)
    sun_data.color = (1.0, 0.945, 0.865)
    sun = bpy.data.objects.new("sun", sun_data)
    bpy.context.collection.objects.link(sun)
    d = Vector((-0.42, -0.78, 0.42)).normalized()
    sun.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    sun.location = (20, -14, 26)

    fill_data = bpy.data.lights.new("fill", "AREA")
    fill_data.energy = 120.0
    fill_data.size = 22.0
    fill_data.color = (0.72, 0.82, 1.0)
    fill = bpy.data.objects.new("fill", fill_data)
    bpy.context.collection.objects.link(fill)
    fill.location = (14, -18, 13)
    fill.rotation_euler = (Vector((5, 4, 5)) - fill.location).to_track_quat("-Z", "Y").to_euler()
    return sun, fill


def add_world():
    world = bpy.data.worlds.new("sky")
    bpy.context.scene.world = world
    nt, bg = fresh_tree(world, "world")
    bg.inputs["Strength"].default_value = 0.60
    try:
        sky = nt.nodes.new("ShaderNodeTexSky")
        try:
            sky.sky_type = "NISHITA"
        except TypeError:
            pass
        sky.sun_disc = False
        sky.sun_elevation = math.radians(42)
        sky.sun_rotation = math.radians(220)
        if hasattr(sky, "air_density"):
            sky.air_density = 1.8
        if hasattr(sky, "dust_density"):
            sky.dust_density = 2.6
        nt.links.new(sky.outputs["Color"], bg.inputs["Color"])
    except Exception:  # noqa: BLE001 - 天空纹理不可用时退回纯色，渲染仍要出图
        bg.inputs["Color"].default_value = (0.55, 0.65, 0.78, 1)


def add_camera(eye, target, lens: float):
    cam_data = bpy.data.cameras.new("cam")
    cam_data.lens = lens
    cam = bpy.data.objects.new("cam", cam_data)
    bpy.context.collection.objects.link(cam)
    e = Vector(eye)
    cam.location = e
    cam.rotation_euler = (Vector(target) - e).to_track_quat("-Z", "Y").to_euler()
    bpy.context.scene.camera = cam
    return cam


def setup_render(res, samples: int):
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    sc.cycles.device = "CPU"
    sc.cycles.samples = samples
    sc.cycles.use_denoising = True
    sc.cycles.max_bounces = 6
    sc.render.resolution_x, sc.render.resolution_y = res
    sc.render.resolution_percentage = 100
    sc.render.image_settings.file_format = "PNG"
    sc.render.film_transparent = False
    sc.view_settings.exposure = -0.95
    try:
        sc.view_settings.view_transform = "Standard"
    except TypeError:
        pass
    try:
        sc.view_settings.look = "None"
    except TypeError:
        pass


def main() -> None:
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    cfg_path = Path(argv[argv.index("--config") + 1]) if "--config" in argv else Path("render_cfg.json")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    out_dir = Path(cfg["out_dir"])

    clear_scene()
    add_world()
    add_lights()
    add_ground(center=tuple(cfg.get("ground_center", [5.0, 4.0])))
    mats = build_materials()
    objs, polys = import_glb(Path(cfg["glb"]), mats)
    print(f"[scene3d] imported {len(objs)} object(s), {polys} polys")

    setup_render(tuple(cfg.get("resolution", [1280, 880])), int(cfg.get("samples", 48)))
    for view in cfg["views"]:
        add_camera(view["eye"], view["target"], float(view.get("lens", 48)))
        bpy.context.scene.render.filepath = str(out_dir / f"scene3d_{view['name']}.png")
        bpy.ops.render.render(write_still=True)
        print(f"[scene3d] rendered {view['name']}")


if __name__ == "__main__":
    main()
