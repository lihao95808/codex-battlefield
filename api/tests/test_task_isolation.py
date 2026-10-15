"""任务间隔离（R2）的回归测试：`docs/spec-task-isolation.md` 判据 I1–I5。

**这个文件存在的理由**：R2 最容易变成"看起来很安全"。它的两条真实交付是
（a）**每节点隔离 `CODEX_HOME`**（治磁盘上的会话/缓存共享），
（b）**越权留痕**（侦探，不是防线）——而 (b) 必须证明它**能真报**又**不误报**。
读隔离同账号下做不到，这一点写在 spec §3 的未实现项里，不在这里假装。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app import isolation, obligations
from app.config import Settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord, TaskNode, TaskStatus
from app.orchestrator import Orchestrator
from app.runner import CodexExecRunner
from app.utils import dumps

OWN = "worktrees/node-own"
SIBLING = "worktrees/node-sibling"


@pytest.fixture(autouse=True)
def clean_database():
    Base.metadata.drop_all(engine)
    init_db()
    yield
    Base.metadata.drop_all(engine)


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = {"workspace_dir": tmp_path / "workspace", "codex_bin": "codex"}
    base.update(overrides)
    return Settings(**base)


def test_detector_finds_a_sibling_path_and_never_a_self_path() -> None:
    text = "我读了 /x/worktrees/node-sibling/secret.txt，也看了自己的 /x/worktrees/node-own/a.py"
    found = isolation.leaks(
        text,
        own_paths=["/x/worktrees/node-own"],
        sibling_paths=["/x/worktrees/node-sibling", "/x/worktrees/node-own"],
    )
    assert [item["path"] for item in found] == ["/x/worktrees/node-sibling"]
    assert "node-sibling" in found[0]["excerpt"]


def test_detector_ignores_short_noise_and_empty_text() -> None:
    """短串（如 "a/b"）不参与匹配：它们在文本里到处都是，匹配它们等于制造误报。"""
    assert isolation.leaks("", own_paths=[], sibling_paths=["/x/worktrees/node-sibling"]) == []
    assert isolation.leaks("a/b", own_paths=[], sibling_paths=["a/b"]) == []
    assert isolation.leaks("nothing here", own_paths=[], sibling_paths=["/x/worktrees/node-sibling"]) == []


def test_detector_treats_nested_own_paths_as_own() -> None:
    """自己的子路径（如自己的 worktree 下的文件）也算自己人——否则每个节点都会给自己报警。"""
    found = isolation.leaks(
        "见 /x/worktrees/node-own/src/a.py",
        own_paths=["/x/worktrees/node-own"],
        sibling_paths=["/x/worktrees/node-own"],
    )
    assert found == []


def test_worktree_paths_match_the_assign_naming(tmp_path: Path) -> None:
    paths = isolation.worktree_paths(str(tmp_path / "workspace"), ["node-a", "tricky/id"])
    assert paths["node-a"] == str(tmp_path / "workspace" / "worktrees" / "node-a")
    assert paths["tricky/id"] == str(tmp_path / "workspace" / "worktrees" / "tricky-id")  # 与 _assign_worktree 同规则


def test_codex_home_is_isolated_per_node(tmp_path: Path) -> None:
    """I1：每节点独立的 `CODEX_HOME`（会话/缓存互不可见），且未配置时不凭空造一个。"""
    runner = CodexExecRunner(_settings(tmp_path, codex_home=str(tmp_path / "codex-home")))
    env_a = runner._codex_env("worker", "node-a")
    env_b = runner._codex_env("worker", "node-b")
    assert env_a["CODEX_HOME"] != env_b["CODEX_HOME"]
    assert env_a["CODEX_HOME"].replace("\\", "/").endswith("nodes/node-a")
    assert Path(env_a["CODEX_HOME"]).is_dir()  # 注入前先建好，否则 CLI 自己造一个又变成共享

    plain = CodexExecRunner(_settings(tmp_path))
    assert "CODEX_HOME" not in plain._codex_env("worker", "node-a")


def test_no_add_dir_in_the_command_line(tmp_path: Path) -> None:
    """I2："我们没加 `--add-dir`"与"我们保证不加"是两件事，后者可被测住。"""
    runner = CodexExecRunner(_settings(tmp_path))
    args = runner._codex_exec_args(tmp_path / "cwd", tmp_path / "s.json", tmp_path / "o.json", "workspace-write", "worker")
    assert "--add-dir" not in args


def test_child_env_drops_unrelated_host_secrets(tmp_path: Path, monkeypatch) -> None:
    """I8：宿主上**与此无关**的凭据不该被顺手交给每个节点（实测见过三个真实的平台 token）。"""
    monkeypatch.setenv("VENDOR_A_API_KEY", "leak-me")
    monkeypatch.setenv("VENDOR_B_LIBRARY_TOKEN", "leak-me-too")
    monkeypatch.setenv("CODEX_BATTLEFIELD_HOME_MARKER", "keep-me")  # `CODEX_` 前缀是显式放行的那一类

    env = CodexExecRunner(_settings(tmp_path))._codex_env("worker", "node-a")

    assert "VENDOR_A_API_KEY" not in env
    assert "VENDOR_B_LIBRARY_TOKEN" not in env
    assert env.get("CODEX_BATTLEFIELD_HOME_MARKER") == "keep-me"
    # 系统必需的键不能在收窄里被误伤（少了 PATH 的表现是"真实 CLI 起不来"，极难归因）
    assert "PATH" in env


def test_child_env_strict_off_restores_the_old_behaviour(tmp_path: Path, monkeypatch) -> None:
    """留一条回到老行为的通道：排查"是不是环境被过滤害的"时要有对照。"""
    monkeypatch.setenv("VENDOR_A_API_KEY", "leak-me")
    env = CodexExecRunner(_settings(tmp_path, codex_env_strict=False))._codex_env("worker", "node-a")
    assert env.get("VENDOR_A_API_KEY") == "leak-me"


def test_leak_is_recorded_as_advisory_and_does_not_block(tmp_path: Path) -> None:
    """I3 + advisory 方向：真泄露要报出来，但**不阻断**（文本里出现路径 ≠ 真的读了它）。"""
    orchestrator = Orchestrator(_settings(tmp_path, mock_delay_seconds=0))
    sibling_dir = tmp_path / "workspace" / "worktrees" / "node-sibling"
    with SessionLocal() as db:
        db.add(TaskNode(id="node-own", title="own", description="", depth=1, status=TaskStatus.RUNNING.value))
        db.add(TaskNode(id="node-sibling", title="sib", description="", depth=1, status=TaskStatus.RUNNING.value))
        db.commit()
        task = db.get(TaskNode, "node-own")
        orchestrator._flag_cross_node_leaks(db, task, f"顺手读了 {sibling_dir}\\secret.txt")
        db.commit()
        meta = json.loads(task.node_meta_json)

    leaks = [item for item in meta["obligations"] if item["type"] == obligations.CROSS_NODE_LEAK]
    assert len(leaks) == 1
    assert leaks[0]["severity"] == obligations.SEVERITY_ADVISORY
    assert obligations.has_blocking(meta["obligations"]) is False
    assert str(sibling_dir) in leaks[0]["payload"]["path"]
    with SessionLocal() as db:
        types = [event.type for event in db.scalars(select(EventRecord)).all()]
    assert "task.cross_node_leak" in types


def test_own_paths_do_not_trigger_the_detector(tmp_path: Path) -> None:
    """I4：自己的 worktree / 根目标出现在自己的产物里是正常的，绝不能报。"""
    orchestrator = Orchestrator(_settings(tmp_path, mock_delay_seconds=0))
    own_dir = tmp_path / "workspace" / "worktrees" / "node-own"
    with SessionLocal() as db:
        db.add(
            TaskNode(
                id="node-own",
                title="own",
                description="",
                depth=1,
                status=TaskStatus.RUNNING.value,
                worktree_path=str(own_dir),
                node_meta_json=dumps({}),
            )
        )
        db.add(TaskNode(id="node-sibling", title="sib", description="", depth=1, status=TaskStatus.RUNNING.value))
        db.commit()
        task = db.get(TaskNode, "node-own")
        orchestrator._flag_cross_node_leaks(db, task, f"我改的是 {own_dir}\\src\\a.py")
        db.commit()
        meta = json.loads(task.node_meta_json)
    assert [item for item in meta.get("obligations") or [] if item["type"] == obligations.CROSS_NODE_LEAK] == []


def test_repeated_leak_does_not_pile_up_obligations(tmp_path: Path) -> None:
    """幂等：同一路径重复出现只留一条债（否则每轮都长一条，看板立刻没法看）。"""
    orchestrator = Orchestrator(_settings(tmp_path, mock_delay_seconds=0))
    sibling_dir = tmp_path / "workspace" / "worktrees" / "node-sibling"
    with SessionLocal() as db:
        db.add(TaskNode(id="node-own", title="own", description="", depth=1, status=TaskStatus.RUNNING.value))
        db.add(TaskNode(id="node-sibling", title="sib", description="", depth=1, status=TaskStatus.RUNNING.value))
        db.commit()
        task = db.get(TaskNode, "node-own")
        for _ in range(3):
            orchestrator._flag_cross_node_leaks(db, task, f"又读了一次 {sibling_dir}\\x.txt")
        db.commit()
        meta = json.loads(task.node_meta_json)
    assert len([item for item in meta["obligations"] if item["type"] == obligations.CROSS_NODE_LEAK]) == 1
