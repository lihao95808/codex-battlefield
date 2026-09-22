from __future__ import annotations

from pathlib import Path

from app import godot_files


def test_godot_resource_path_rejects_escaping_and_non_res_paths() -> None:
    root = Path("C:/proj")
    assert godot_files.godot_resource_path(root, None) is None
    assert godot_files.godot_resource_path(root, "user://save.json") is None
    assert godot_files.godot_resource_path(root, "res://../secret.txt") is None
    # 查询串与片段必须被剥离，尾随标点也要去掉
    assert godot_files.godot_resource_path(root, "res://a/b.tscn#node?x=1,") == root / "a" / "b.tscn"


def test_godot_resource_path_leading_slash_is_platform_dependent() -> None:
    """记录一个**既有**的跨平台差异，本次搬移不改变它。

    `res:///abs.txt` 剥掉前缀后剩 `/abs.txt`：在 POSIX 上 `Path.is_absolute()` 为真因而被拒；
    在 Windows 上 `/abs.txt` 没有盘符、`is_absolute()` 为假，于是被当作相对路径拼到 root 下。
    这是原实现的行为，W4 只搬移不改逻辑，故此处将其钉住而不是"修正"。
    """
    root = Path("C:/proj")
    result = godot_files.godot_resource_path(root, "res:///abs.txt")
    if Path("/abs.txt").is_absolute():
        assert result is None
    else:
        # Windows 上 `root / Path("/abs.txt")` 会拼成 `C:/abs.txt` —— 即**越出 root 之外**。
        # 这是原实现的行为（W4 只搬移），但它在读取路径上构成一次边界逃逸，值得记录。
        assert result is not None
        assert result == root / Path("/abs.txt")
        assert root not in result.parents, "既有权衡：结果位于项目根之外"


def test_resource_exists_and_text(tmp_path: Path) -> None:
    (tmp_path / "scenes").mkdir()
    scene = tmp_path / "scenes" / "main.tscn"
    scene.write_text('[node name="Main" type="Node2D"]\nScript = "res://x.gd"\n', encoding="utf-8")

    assert godot_files.resource_exists(tmp_path, "res://scenes/main.tscn") is True
    assert godot_files.resource_exists(tmp_path, "res://scenes/missing.tscn") is False

    text = godot_files.resource_text(tmp_path, "res://scenes/main.tscn")
    assert text.startswith('[node name="main"')  # 读取结果小写化，保持原行为
    assert godot_files.resource_text(tmp_path, "res://nope.tscn") == ""


def test_scene_helpers() -> None:
    scene = '[node name="A"]\n[node name="B"]\nscript = preload("res://b.gd")\n'
    assert godot_files.godot_scene_node_count(scene) == 2
    assert godot_files.godot_scene_has_script_reference(scene) is True
    assert godot_files.godot_scene_has_script_reference("[node name=\"A\"]") is False


def test_godot_res_references_skips_docs_and_strips_trailing_punctuation(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "scripts" / "a.gd").write_text('const S = "res://art/hero.png",\n', encoding="utf-8")
    (tmp_path / "docs" / "note.md").write_text("res://docs-only/should-not-count.png", encoding="utf-8")

    refs = godot_files.godot_res_references(tmp_path)
    assert "res://art/hero.png" in refs
    # 尾随逗号被去掉，而不是产生 "res://art/hero.png,"
    assert not any(ref.endswith(",") for ref in refs)
    # docs/ 下的引用按原行为被跳过
    assert not any("docs-only" in ref for ref in refs)


def test_extract_godot_main_scene() -> None:
    text = '[application]\nrun/main_scene="res://scenes/main.tscn"\n'
    assert godot_files.extract_godot_main_scene(text) == "res://scenes/main.tscn"
    assert godot_files.extract_godot_main_scene("run/main_scene=\n") is None
    assert godot_files.extract_godot_main_scene("[application]\n") is None


def test_godot_line_key() -> None:
    assert godot_files.godot_line_key("  key = value  ") == "key"
    assert godot_files.godot_line_key("") is None
    assert godot_files.godot_line_key("; comment = x") is None
    assert godot_files.godot_line_key("# comment") is None
    assert godot_files.godot_line_key("no equals sign") is None


def test_parse_godot_project_sections() -> None:
    sections = godot_files.parse_godot_project_sections("[a]\nx=1\n[b]\ny=2\n")
    assert sections["[a]"] == ["x=1"]
    assert sections["[b]"] == ["y=2"]
    assert "" in sections


def test_resource_value_walks_nested_keys() -> None:
    payload = {"outer": {"inner": "hit"}}
    assert godot_files.resource_value(payload, ("outer", "inner")) == "hit"
    assert godot_files.resource_value(payload, ("outer", "missing")) == ""
    assert godot_files.resource_value(payload, ("outer", "inner", "deeper")) == ""


def test_count_files(tmp_path: Path) -> None:
    (tmp_path / "a.tscn").write_text("x", encoding="utf-8")
    (tmp_path / "b.tscn").write_text("x", encoding="utf-8")
    (tmp_path / "c.gd").write_text("x", encoding="utf-8")
    assert godot_files.count_files(tmp_path, "*.tscn") == 2
    assert godot_files.count_files(tmp_path, "*.gd") == 1


def test_orchestrator_delegates_match_the_module() -> None:
    """反漂移：搬移后 Orchestrator 的同名方法必须与模块函数结果一致。"""
    from app.orchestrator import Orchestrator

    assert Orchestrator._godot_line_key(None, "a = b") == godot_files.godot_line_key("a = b")
    assert Orchestrator._extract_godot_main_scene(None, "run/main_scene=\"res://m.tscn\"") == "res://m.tscn"
