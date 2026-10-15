"""工程的挂载与注册：`active.json` / `registry.json` 的唯一读写口。

规格：`docs/spec-state-project-decoupling.md` §9。

## 职责

| 文件 | 内容 | 谁写 |
|---|---|---|
| `<state_home>/active.json` | 当前工程（`project_key` + `project_root`） | `set_active()`，**原子写** |
| `<state_home>/registry.json` | 已挂载工程清单 | `register()` |
| `<state_home>/projects/<key>/project.json` | 该工程自己的身份（真相是 root） | `register()` |

## 为什么 `active.json` 必须原子写

它决定"这个进程用哪个库"。半写状态（截断的 JSON）会让下次启动读到坏文件，
`read_active_project` 只能返回"未绑定"——于是**悄悄退回老工作区**，
而用户以为自己在跑另一个工程。用 `临时文件 + os.replace` 保证要么旧要么新。

## 为什么 key 与 root 都要落盘

key 是目录名（可读、防碰撞），root 是**真相**。只存 key 时，一个算错的 key
就让人无法知道它对应哪个目录；只存 root 时，每次都要重算 key。
两个都存，并在读取时用 root 重算 key 做校验。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state_layout import (
    ACTIVE_FILENAME,
    REGISTRY_FILENAME,
    StateLayout,
    normalize_project_root,
    project_key,
    resolve_state_home,
)

REGISTRY_VERSION = 1


class ProjectError(RuntimeError):
    """挂载/切换工程时的可预期失败。CLI 直接把它当人话打出来。"""


@dataclass(frozen=True)
class MountedProject:
    key: str
    root: Path
    project_dir: Path
    db_path: Path
    created_at: str = ""

    @property
    def db_exists(self) -> bool:
        return self.db_path.is_file()

    def db_size_bytes(self) -> int:
        try:
            return self.db_path.stat().st_size
        except OSError:
            return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: Any) -> None:
    """原子写 JSON：临时文件 + `os.replace`。

    同目录的临时文件是**必须**的——跨盘 `os.replace` 不是原子的，
    而状态根可能和当前工作目录不在同一块盘。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _read_json(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def validate_project_root(raw: str | Path) -> Path:
    """校验并规范化要挂载的工程目录。**拒绝**几种会静默搞坏状态的输入。"""
    text = str(raw or "").strip()
    if not text:
        raise ProjectError("工程目录不能为空。")

    candidate = Path(text)
    if not candidate.is_absolute():
        raise ProjectError(
            f"需要**绝对路径**（工程标识由规范化绝对路径决定）：{text}\n"
            "  相对路径会随当前工作目录变化，同一个工程会得到不同的 key。"
        )

    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise ProjectError(f"无法解析路径 {text}：{exc}") from exc

    if resolved == resolved.parent:
        raise ProjectError(f"拒绝把盘符根目录当工程：{resolved}")

    # 状态根自己（或它下面）不能当工程：那会让状态根被当成被改的工程，
    # 于是 worktree、runs、甚至 registry 都被卷进"工程材料"。
    state_home = resolve_state_home().resolve()
    if resolved == state_home or state_home in resolved.parents:
        raise ProjectError(
            f"拒绝把状态根（或它下面的目录）当工程：{resolved}\n"
            f"  状态根是 {state_home} —— 工具状态与被改工程必须分开。"
        )

    return resolved


def project_manifest_path(state_home: Path, key: str) -> Path:
    return Path(state_home) / "projects" / key / "project.json"


def ensure_project_dirs(layout: StateLayout) -> None:
    """给绑定的工程建齐目录（L2：目录创建只在这里与进程启动两处发生）。"""
    for path in layout.derived_dirs:
        path.mkdir(parents=True, exist_ok=True)


def mount(
    root: str | Path,
    *,
    state_home: Path | None = None,
    make_active: bool = True,
) -> MountedProject:
    """挂载一个工程：建目录、写 registry、写 project.json，可选设为当前。

    可重入：重复挂载同一目录只是刷新记录，不会产生第二份状态。
    """
    home = Path(state_home) if state_home is not None else resolve_state_home()
    resolved = validate_project_root(root)
    key = project_key(resolved)

    layout = StateLayout(state_home=home, project_key=key, project_root=resolved)
    ensure_project_dirs(layout)

    manifest = {
        "project_key": key,
        "project_root": str(resolved),
        "normalized_root": normalize_project_root(resolved),
        "created_at": _now(),
    }
    _write_json_atomic(layout.project_manifest_file, manifest)

    registry = _read_json(home / REGISTRY_FILENAME)
    projects = registry.get("projects")
    if not isinstance(projects, dict):
        projects = {}
    projects[key] = {
        "project_root": str(resolved),
        "project_dir": str(layout.project_dir),
        "mounted_at": _now(),
    }
    registry = {"version": REGISTRY_VERSION, "projects": projects, "updated_at": _now()}
    _write_json_atomic(home / REGISTRY_FILENAME, registry)

    if make_active:
        _write_json_atomic(
            home / ACTIVE_FILENAME,
            {"project_key": key, "project_root": str(resolved), "activated_at": _now()},
        )

    return MountedProject(
        key=key,
        root=resolved,
        project_dir=layout.project_dir,
        db_path=layout.db_path,
        created_at=manifest["created_at"],
    )


def list_projects(*, state_home: Path | None = None) -> list[MountedProject]:
    """已挂载工程清单。

    以 `registry.json` 为准，但**以磁盘上的 `projects/` 目录做补全**：
    registry 被删/写坏时，磁盘上真实存在的工程不该凭空消失
    （那会让用户以为状态没了，而其实只是索引丢了）。
    """
    home = Path(state_home) if state_home is not None else resolve_state_home()
    registry = _read_json(home / REGISTRY_FILENAME)
    entries = registry.get("projects")
    if not isinstance(entries, dict):
        entries = {}

    found: dict[str, MountedProject] = {}
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        raw_root = entry.get("project_root")
        if not raw_root:
            continue
        project_dir = home / "projects" / key
        found[key] = MountedProject(
            key=key,
            root=Path(str(raw_root)),
            project_dir=project_dir,
            db_path=project_dir / "battlefield.sqlite3",
            created_at=str(entry.get("mounted_at") or ""),
        )

    projects_root = home / "projects"
    if projects_root.is_dir():
        for child in sorted(projects_root.iterdir()):
            if not child.is_dir() or child.name in found:
                continue
            # 补全**必须验证**，不能"看到目录就当工程"。
            # 实测踩过：状态根从 C: 搬到 D: 之后，`project.json` 里的 project_root
            # 是相对路径，于是被判成"状态根里的一个工程"，`doctor` 直接报
            # key 不一致。只认**自洽**的 manifest：
            #   1. project.json 存在且是对象
            #   2. project_root 是绝对路径
            #   3. 目录名 == 由该 root 重算的 key
            manifest = _read_json(child / "project.json")
            if not manifest:
                continue
            raw_root = manifest.get("project_root")
            if not isinstance(raw_root, str) or not raw_root.strip():
                continue
            candidate = Path(raw_root)
            if not candidate.is_absolute():
                continue
            if project_key(candidate) != child.name:
                continue
            found[child.name] = MountedProject(
                key=child.name,
                root=candidate,
                project_dir=child,
                db_path=child / "battlefield.sqlite3",
                created_at=str(manifest.get("created_at") or ""),
            )

    return sorted(found.values(), key=lambda item: item.key)


def current_project(*, state_home: Path | None = None) -> MountedProject | None:
    home = Path(state_home) if state_home is not None else resolve_state_home()
    payload = _read_json(home / ACTIVE_FILENAME)
    key = str(payload.get("project_key") or "")
    if not key:
        return None
    raw_root = payload.get("project_root")
    if not raw_root:
        return None
    project_dir = home / "projects" / key
    return MountedProject(
        key=key,
        root=Path(str(raw_root)),
        project_dir=project_dir,
        db_path=project_dir / "battlefield.sqlite3",
        created_at=str(payload.get("activated_at") or ""),
    )


def clear_active(*, state_home: Path | None = None) -> None:
    home = Path(state_home) if state_home is not None else resolve_state_home()
    (home / ACTIVE_FILENAME).unlink(missing_ok=True)


def directory_size_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def worktree_drive_matches(project_dir: Path, project_root: Path) -> bool:
    """worktree 目录与目标仓库是否同一块盘。

    跨盘时 `git worktree` 有已知的兼容性问题，所以要能提前判出来
    （`spec` §8）。
    """
    try:
        return Path(project_dir).drive.lower() == Path(project_root).drive.lower()
    except (OSError, AttributeError):
        return True


def remove_project(key: str, *, state_home: Path | None = None, purge: bool = False) -> dict:
    """从 registry 摘掉一个工程；`purge=True` 才删磁盘目录。

    默认**不删目录**：删状态是不可逆动作，不该是"顺手"的结果。
    """
    home = Path(state_home) if state_home is not None else resolve_state_home()
    registry = _read_json(home / REGISTRY_FILENAME)
    projects = registry.get("projects")
    if not isinstance(projects, dict) or key not in projects:
        raise ProjectError(f"registry 里没有这个工程：{key}")

    removed = projects.pop(key)
    registry = {"version": REGISTRY_VERSION, "projects": projects, "updated_at": _now()}
    _write_json_atomic(home / REGISTRY_FILENAME, registry)

    active = _read_json(home / ACTIVE_FILENAME)
    was_active = str(active.get("project_key") or "") == key
    if was_active:
        clear_active(state_home=home)

    purged = False
    if purge:
        target = home / "projects" / key
        if target.is_dir():
            shutil.rmtree(target)
            purged = True

    return {"key": key, "root": removed.get("project_root"), "was_active": was_active, "purged": purged}
