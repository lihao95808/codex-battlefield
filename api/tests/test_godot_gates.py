from __future__ import annotations

from pathlib import Path

from app import godot_gates
from app.orchestrator import Orchestrator

PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
JPG_HEAD = b"\xff\xd8\xff" + b"\x00" * 13


def test_requires_parent_godot_integration_gate_needs_both_conditions() -> None:
    # 需要 godot 标记 **且**（显式集成语 或 断链 + 集成上下文词）
    assert godot_gates.requires_parent_godot_integration_gate("godot 集成验收") is True
    assert godot_gates.requires_parent_godot_integration_gate("godot 集成 断链检查") is True
    # 有 godot 标记但没有集成语义 → 不要求该门禁
    assert godot_gates.requires_parent_godot_integration_gate("godot 场景搭建") is False
    assert godot_gates.requires_parent_godot_integration_gate("普通的父级验收") is False
    # 有"断链"但缺集成上下文词 → 不要求该门禁（原实现如此，钉住它）
    assert godot_gates.requires_parent_godot_integration_gate("godot 断链检查") is False


def test_asset_header_is_valid_per_format() -> None:
    assert godot_gates.asset_header_is_valid(".png", PNG_HEAD) is True
    assert godot_gates.asset_header_is_valid(".png", b"not a png") is False
    assert godot_gates.asset_header_is_valid(".jpg", JPG_HEAD) is True
    assert godot_gates.asset_header_is_valid(".jpeg", JPG_HEAD) is True
    assert godot_gates.asset_header_is_valid(".webp", b"RIFF" + b"\x00" * 4 + b"WEBP") is True
    assert godot_gates.asset_header_is_valid(".wav", b"RIFF" + b"\x00" * 4 + b"WAVE") is True
    assert godot_gates.asset_header_is_valid(".ogg", b"OggS" + b"\x00" * 8) is True
    assert godot_gates.asset_header_is_valid(".mp3", b"ID3" + b"\x00" * 8) is True
    # 未知后缀一律视为有效（由调用方先按后缀过滤）
    assert godot_gates.asset_header_is_valid(".txt", b"whatever") is True


def test_asset_file_is_valid(tmp_path: Path) -> None:
    real_png = tmp_path / "ok.png"
    real_png.write_bytes(PNG_HEAD)
    empty_png = tmp_path / "empty.png"
    empty_png.write_bytes(b"")
    broken_png = tmp_path / "broken.png"
    broken_png.write_bytes(b"garbage-not-a-png")
    plain = tmp_path / "note.txt"
    plain.write_text("hello", encoding="utf-8")

    assert godot_gates.asset_file_is_valid(real_png) is True
    assert godot_gates.asset_file_is_valid(empty_png) is False
    assert godot_gates.asset_file_is_valid(broken_png) is False
    # 非图片/音频后缀直接视为有效，不做头部检查
    assert godot_gates.asset_file_is_valid(plain) is True


def test_missing_godot_res_references(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    (tmp_path / "art" / "hero.png").write_bytes(PNG_HEAD)
    (tmp_path / "main.gd").write_text(
        'const A = "res://art/hero.png"\nconst B = "res://art/missing.png"\n',
        encoding="utf-8",
    )
    missing = godot_gates.missing_godot_res_references(tmp_path)
    assert "res://art/missing.png" in missing
    assert "res://art/hero.png" not in missing


def test_invalid_referenced_asset_files_flags_empty_shells(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    (tmp_path / "art" / "good.png").write_bytes(PNG_HEAD)
    (tmp_path / "art" / "shell.png").write_bytes(b"")
    (tmp_path / "main.gd").write_text(
        'const A = "res://art/good.png"\nconst B = "res://art/shell.png"\n',
        encoding="utf-8",
    )
    invalid = godot_gates.invalid_referenced_asset_files(tmp_path)
    assert "res://art/shell.png" in invalid
    assert "res://art/good.png" not in invalid


def test_valid_asset_files_skips_ignored_dirs(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    (tmp_path / "art" / "ok.png").write_bytes(PNG_HEAD)
    ignored = tmp_path / "node_modules"
    ignored.mkdir()
    (ignored / "dep.png").write_bytes(PNG_HEAD)

    found = godot_gates.valid_asset_files(tmp_path, godot_gates.IMAGE_ASSET_SUFFIXES)
    names = sorted(p.name for p in found)
    assert names == ["ok.png"]


def test_readme_completion_issues_detects_stale_scope_markers(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# 项目骨架\n后续子系统接入\n", encoding="utf-8")
    issues = godot_gates.readme_completion_issues(tmp_path)
    assert issues and "stale incomplete-scope markers" in issues[0]

    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "README.md").write_text("# 完整实现\n这里已经可用。\n", encoding="utf-8")
    assert godot_gates.readme_completion_issues(clean) == []


def test_target_text_integrity_issues_clean_tree(tmp_path: Path) -> None:
    (tmp_path / "a.gd").write_text("extends Node\n", encoding="utf-8")
    (tmp_path / "b.json").write_text('{"ok": true}', encoding="utf-8")
    assert godot_gates.target_text_integrity_issues(tmp_path) == []


def test_target_indexes_collect_paths_and_drop_comments(tmp_path: Path) -> None:
    (tmp_path / "a.gd").write_text("# 这是注释\nvar x = 1\n", encoding="utf-8")
    (tmp_path / "b.tscn").write_text('[node name="A"]\n', encoding="utf-8")

    text_index = godot_gates.target_text_index(tmp_path)
    assert "a.gd" in text_index and "b.tscn" in text_index

    script_index = godot_gates.target_script_index(tmp_path)
    assert "var x = 1" in script_index
    assert "这是注释" not in script_index


def test_orchestrator_delegates_match_the_module(tmp_path: Path) -> None:
    """反漂移：搬移后 Orchestrator 的同名方法必须与模块函数一致。"""
    (tmp_path / "README.md").write_text("# 项目骨架\n", encoding="utf-8")

    assert Orchestrator._requires_parent_godot_integration_gate(None, "godot 集成验收") is True
    assert Orchestrator._asset_header_is_valid(None, ".png", PNG_HEAD) is True
    assert Orchestrator._readme_completion_issues(None, tmp_path) == godot_gates.readme_completion_issues(tmp_path)
    assert Orchestrator._missing_godot_res_references(None, tmp_path) == godot_gates.missing_godot_res_references(tmp_path)
