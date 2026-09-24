"""R3 worktree 生命周期（判据 + 租约）的回归测试。

**这个文件的存在理由**：回收是**删目录**，判据每一条都得能单独被反驳。所以这里逐条钉：

| 编号 | 要钉住的事 |
|---|---|
| 路径守卫 | 只回收 `<workspace>/worktrees/` 底下的目录——写坏的 `worktree_path` 不能让系统删仓库外的东西 |
| 证据前置 | 声明的产物在归档处（集成目标）找不到时**不许回收**（spec §R3 风险条款） |
| 保守默认 | `failed` 节点不回收（失败现场是修复/复盘要读的证据）；保留期内不回收；完成时间判不出来也不回收 |
| 串行化 | 创建/回收共用的命名资源租约是排他的，且异常路径上会释放 |

接线部分（真正的删除与计数）在 `test_worktree_recycle.py`。
"""

from __future__ import annotations

import pathlib
import time

from app import worktrees


def _tree(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    workspace = tmp_path / "workspace"
    worktree = workspace / "worktrees" / "node-1"
    (worktree / "src").mkdir(parents=True, exist_ok=True)
    (worktree / "src" / "a.gd").write_text("A", encoding="utf-8")
    archive = tmp_path / "target"
    (archive / "src").mkdir(parents=True, exist_ok=True)
    (archive / "src" / "a.gd").write_text("A", encoding="utf-8")
    return workspace, worktree, archive


def test_path_guard_accepts_only_managed_root(tmp_path: pathlib.Path) -> None:
    workspace, worktree, _ = _tree(tmp_path)
    assert worktrees.is_managed_worktree(workspace, worktree) is True
    assert worktrees.is_managed_worktree(workspace, str(worktree)) is True
    assert worktrees.is_managed_worktree(workspace, None) is False
    assert worktrees.is_managed_worktree(workspace, "") is False
    # 根目录本身、外面的目录、以及"看起来像但不是"的都得挡掉
    assert worktrees.is_managed_worktree(workspace, worktrees.managed_root(workspace)) is False
    assert worktrees.is_managed_worktree(workspace, tmp_path / "elsewhere") is False
    assert worktrees.is_managed_worktree(workspace, workspace / "worktrees-other" / "node-1") is False


def test_evidence_gaps_needs_content_identity_not_just_the_name(tmp_path: pathlib.Path) -> None:
    """证据前置的判据是**指纹一致**，不是"归档处有这个路径"。

    这条是"同名旧文件"那个坑的回归钉子：节点改的多半是工程里本来就有的文件，归档处当然存在
    这个路径；只查 `exists()` 就会判成"已归档"、把 worktree 删掉，而父节点随后集成时源文件
    已经没了（`source missing`，默认档只上浮不阻断）→ 产物静默丢失。
    """
    _, worktree, archive = _tree(tmp_path)
    assert worktrees.evidence_gaps(archive, ["src/a.gd"], worktree) == []
    assert worktrees.evidence_gaps(archive, [], worktree) == []
    assert worktrees.evidence_gaps(archive, ["src\\a.gd"], worktree) == []  # 反斜杠归一化后能对上

    # 名字在、内容不同（归档处还是旧版）→ 未归档
    (archive / "src" / "a.gd").write_text("OLD", encoding="utf-8")
    assert worktrees.evidence_gaps(archive, ["src/a.gd"], worktree) == ["src/a.gd"]
    (archive / "src" / "a.gd").write_text("A", encoding="utf-8")

    # 归档处没有 / 源里没有 → 未归档
    assert worktrees.evidence_gaps(archive, ["src/missing.gd"], worktree) == ["src/missing.gd"]
    (archive / "src" / "b.gd").write_text("B", encoding="utf-8")
    assert worktrees.evidence_gaps(archive, ["src/b.gd"], worktree) == ["src/b.gd"]

    # 目录同样要比内容（`changed_files` 允许声明目录）
    for root in (worktree, archive):
        (root / "assets").mkdir()
        (root / "assets" / "x.png").write_text("X", encoding="utf-8")
    assert worktrees.evidence_gaps(archive, ["assets"], worktree) == []
    (archive / "assets" / "x.png").write_text("Y", encoding="utf-8")
    assert worktrees.evidence_gaps(archive, ["assets"], worktree) == ["assets"]

    # 没有归档处可判 → 只要有声明就算未判定（不许因为"判不了"就当成已归档）
    assert worktrees.evidence_gaps(None, ["src/a.gd"], worktree) == ["src/a.gd"]
    assert worktrees.evidence_gaps(None, [], worktree) == []
    # 没有源可比对（判不了"归档处这份是不是它写上去的"）→ 同样算未判定
    assert worktrees.evidence_gaps(archive, ["src/a.gd"]) == ["src/a.gd"]
    # 绝对路径与越界路径判不了"归档处有没有" → 当作未判定项（不许据此放行）
    assert worktrees.evidence_gaps(archive, ["/abs/a.gd"], worktree) == ["/abs/a.gd"]
    assert worktrees.evidence_gaps(archive, ["../outside.gd"], worktree) == ["../outside.gd"]


def test_decide_recycles_only_when_every_precondition_holds(tmp_path: pathlib.Path) -> None:
    workspace, worktree, archive = _tree(tmp_path)
    decision = worktrees.decide(
        enabled=True,
        status="done",
        worktree_path=worktree,
        workspace_dir=workspace,
        changed_files=["src/a.gd"],
        archive_root=archive,
        source_root=worktree,
        parent_terminal=True,
        retention_seconds=0,
    )
    assert decision["recycle"] is True
    assert decision["reason"] == worktrees.DECISION_RECYCLE


def test_decide_refusals_each_carry_their_own_reason(tmp_path: pathlib.Path) -> None:
    workspace, worktree, archive = _tree(tmp_path)
    common = {
        "worktree_path": worktree,
        "workspace_dir": workspace,
        "changed_files": ["src/a.gd"],
        "archive_root": archive,
        "source_root": worktree,
        "parent_terminal": True,
    }

    assert worktrees.decide(enabled=False, status="done", **common)["reason"] == "disabled"
    assert worktrees.decide(enabled=True, status="running", **common)["reason"] == "status"
    # failed 是**故意**不回收的：失败现场正是修复与复盘要读的证据
    assert worktrees.decide(enabled=True, status="failed", **common)["reason"] == "status"
    assert worktrees.decide(enabled=True, status="blocked", **common)["reason"] == "status"

    unmanaged = worktrees.decide(enabled=True, status="done", worktree_path=pathlib.Path("/tmp/not-ours"), workspace_dir=workspace, changed_files=[], archive_root=archive)
    assert unmanaged["reason"] == "path_guard"

    # 父节点还在跑：它的集成要读这个 worktree（`child_root = output_node.worktree_path`）
    active = worktrees.decide(
        enabled=True, status="done", worktree_path=worktree, workspace_dir=workspace,
        changed_files=["src/a.gd"], archive_root=archive, source_root=worktree, parent_terminal=False,
    )
    assert active["reason"] == "parent_active"

    # 归档处已经不在了（父节点 worktree 被回收）→ 判不了，不回收
    gone = worktrees.decide(
        enabled=True, status="done", worktree_path=worktree, workspace_dir=workspace,
        changed_files=["src/a.gd"], archive_root=tmp_path / "never-was", source_root=worktree, parent_terminal=True,
    )
    assert gone["reason"] == "archive_gone"

    evidence = worktrees.decide(enabled=True, status="done", worktree_path=worktree, workspace_dir=workspace, changed_files=["src/missing.gd"], archive_root=archive, source_root=worktree, parent_terminal=True)
    assert evidence["reason"] == "evidence"
    assert evidence["evidence_gaps"] == ["src/missing.gd"]


def test_decide_honours_the_retention_window(tmp_path: pathlib.Path) -> None:
    workspace, worktree, archive = _tree(tmp_path)
    now = time.time()
    base = {
        "worktree_path": worktree,
        "workspace_dir": workspace,
        "changed_files": ["src/a.gd"],
        "archive_root": archive,
        "source_root": worktree,
        "parent_terminal": True,
    }
    recent = worktrees.decide(
        enabled=True,
        status="done",
        finished_at=now - 10,
        now=now,
        retention_seconds=100,
        **base,
    )
    assert recent["reason"] == "retention"
    assert recent["waited_seconds"] == 10

    old = worktrees.decide(
        enabled=True,
        status="done",
        finished_at=now - 200,
        now=now,
        retention_seconds=100,
        **base,
    )
    assert old["recycle"] is True

    # 完成时间判不出来 → 保守不回收（不用"现在"冒充完成时间）
    unknown = worktrees.decide(
        enabled=True,
        status="done",
        finished_at=None,
        now=now,
        retention_seconds=100,
        **base,
    )
    assert unknown["reason"] == "retention"


def test_lease_key_is_per_target_repository(tmp_path: pathlib.Path) -> None:
    assert worktrees.lease_key("G:/repo-a") == worktrees.lease_key("G:/repo-a")
    assert worktrees.lease_key("G:/repo-a") != worktrees.lease_key("G:/repo-b")
    assert worktrees.lease_key(None) == worktrees.lease_key(None)
    # 租约文件名安全：key 里不该出现路径分隔符
    assert "/" not in worktrees.lease_path(tmp_path, "../weird/key").name


def test_lease_is_exclusive_and_released(tmp_path: pathlib.Path) -> None:
    with worktrees.resource_lease(tmp_path, "worktree-abc", wait_seconds=0.2) as first:
        assert first is True
        with worktrees.resource_lease(tmp_path, "worktree-abc", wait_seconds=0.2) as second:
            assert second is False  # 同一把锁：第二个拿不到
    # 出了 with 就该释放，下次还能拿到
    with worktrees.resource_lease(tmp_path, "worktree-abc", wait_seconds=0.2) as again:
        assert again is True


def test_lease_releases_even_when_the_body_raises(tmp_path: pathlib.Path) -> None:
    try:
        with worktrees.resource_lease(tmp_path, "worktree-boom", wait_seconds=0.2):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with worktrees.resource_lease(tmp_path, "worktree-boom", wait_seconds=0.2) as again:
        assert again is True
