from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from app.project_explorer import MAX_ENTRIES, SKIPPED_DIRECTORIES, list_directory


def test_unbound() -> None:
    assert list_directory(None) == {
        "root": None, "path": "", "entries": [], "truncated": False, "status": "unbound",
    }


def test_directories_first_casefold_sort_and_nonrecursive(tmp_path: Path) -> None:
    for name in ("Zoo", "alpha"):
        (tmp_path / name).mkdir()
    (tmp_path / "alpha" / "not-enumerated.txt").touch()
    for name in ("z.txt", "B.txt", "a.txt"):
        (tmp_path / name).touch()
    for name in SKIPPED_DIRECTORIES:
        (tmp_path / name).mkdir()
    result = list_directory(tmp_path)
    assert result["root"] == str(tmp_path.resolve())
    assert result["path"] == ""
    assert result["status"] == "ok"
    assert result["truncated"] is False
    assert result["entries"] == [
        {"name": "alpha", "path": "alpha", "kind": "directory"},
        {"name": "Zoo", "path": "Zoo", "kind": "directory"},
        {"name": "a.txt", "path": "a.txt", "kind": "file"},
        {"name": "B.txt", "path": "B.txt", "kind": "file"},
        {"name": "z.txt", "path": "z.txt", "kind": "file"},
    ]
    nested = list_directory(tmp_path, "./alpha/")
    assert nested["path"] == "alpha"
    assert nested["entries"] == [{"name": "not-enumerated.txt", "path": "alpha/not-enumerated.txt", "kind": "file"}]


def test_normalized_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "src" / "nested").mkdir(parents=True)
    assert list_directory(tmp_path, "src\\nested")["path"] == "src/nested"


def test_truncation_is_sorted_and_directories_keep_priority(tmp_path: Path) -> None:
    for number in reversed(range(MAX_ENTRIES + 2)):
        (tmp_path / f"file-{number:03}.txt").touch()
    (tmp_path / "zzz-directory").mkdir()
    result = list_directory(tmp_path)
    assert result["truncated"] is True
    assert len(result["entries"]) == MAX_ENTRIES
    assert result["entries"][0]["name"] == "zzz-directory"
    assert result["entries"][-1]["name"] == "file-298.txt"


def test_exact_limit_is_not_truncated(tmp_path: Path) -> None:
    for number in range(MAX_ENTRIES):
        (tmp_path / str(number)).touch()
    assert list_directory(tmp_path)["truncated"] is False


@pytest.mark.parametrize("relative", [
    "..", "../outside", "nested/../outside", "nested\\..\\outside",
    "/absolute", "\\absolute", "C:\\absolute", "C:relative",
    "//server/share", "\\\\server\\share", "nested/C:drive", "bad\x00name",
    ".. ", "nested/.. ", "file:stream", "nested.",
])
def test_rejects_unsafe_paths(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        list_directory(tmp_path, relative)


def test_missing_and_not_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        list_directory(tmp_path, "missing")
    (tmp_path / "file.txt").touch()
    with pytest.raises(FileNotFoundError):
        list_directory(tmp_path, "file.txt")
    with pytest.raises(FileNotFoundError):
        list_directory(tmp_path, "file.txt/child")
    with pytest.raises(FileNotFoundError):
        list_directory(tmp_path / "missing-root")


def test_excluded_directory_cannot_be_requested(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError):
        list_directory(tmp_path, ".git")


def test_symlinks_are_hidden_and_cannot_be_traversed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").touch()
    (root / "safe").mkdir()
    try:
        (root / "escape").symlink_to(outside, target_is_directory=True)
        (root / "internal").symlink_to(root / "safe", target_is_directory=True)
        (root / "file-link").symlink_to(outside / "secret.txt")
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    assert [entry["name"] for entry in list_directory(root)["entries"]] == ["safe"]
    for path in ("escape", "escape/secret.txt", "internal", "file-link"):
        with pytest.raises(ValueError):
            list_directory(root, path)
    with pytest.raises(ValueError):
        list_directory(root / "escape")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_junctions_are_hidden_and_cannot_be_traversed(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").touch()
    junction = root / "escape"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(outside)], check=True, capture_output=True)
    try:
        assert list_directory(root)["entries"] == []
        with pytest.raises(ValueError):
            list_directory(root, "escape")
        with pytest.raises(ValueError):
            list_directory(root, "escape/secret.txt")
    finally:
        junction.rmdir()
