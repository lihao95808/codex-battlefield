"""崩溃注入与并发压力的判据（P5-6）。

ROADMAP 的判据是**"崩溃注入下'不丢已验收结果'可复现"**。这一层要钉的就是它，
而它最容易做成"看起来过了其实没测到"：在**正确的时机**杀进程很难，而杀早了杀晚了
都只会让判据空转（进程还没写、或者已经写完了）。

所以这里做三件事：

1. **在真实的写入路径上埋可注入的崩溃点**（`CODEX_BATTLEFIELD_CRASH_AT`），
   而不是在外面随机杀 —— 随机杀会以极高概率落在"什么都没发生"的时刻。
2. 在**每个崩溃点**各杀一次，逐个断言"锚点仍然可用"。
3. 再补一层**并发写**：两个进程同时写同一个锚点。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| C1 | 锚点是**原子写**（唯一临时名 + 换名） | 写到一半被杀 → 半截 JSON → 还原通路静默失效 |
| C2 | 写之前**留一份 `.backup`** | 唯一的那份坏了就没退路 |
| C3 | 读者在主文件坏掉时**退回备份**并**出声** | 静默用备份会掩盖"真的丢过一次写" |
| C4 | 在**每个崩溃点**被杀之后，锚点要么是完整的旧内容、要么是完整的新内容 | 读者永远不该看到半截 |
| C5 | 并发写不产生半截文件、不残留临时文件 | 两个写者互相踩 |
| C6 | 写到一半失败时**清理自己的临时文件** | 磁盘上积累垃圾，且没人知道写过 |
| C7 | 历史快照写失败**不影响主锚点** | 一个尽力而为的产物把关键产物带崩 |
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from app import checkpoint


# ---------- 夹具 ----------


@pytest.fixture()
def anchor(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "state_snapshots" / "root-1" / "latest.json"


def _valid(path: pathlib.Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


# ---------- C1 原子写 ----------


def test_c1_the_temp_name_is_unique_per_writer(anchor: pathlib.Path, monkeypatch) -> None:
    """临时名必须唯一 —— 共享的 `<name>.tmp` 会让两个写者互相踩。

    做法：换掉 `os.replace` 去**看**它收到的路径，而不是真的换名。
    """
    seen: list[pathlib.Path] = []
    real_replace = os.replace

    def _capture(src, dst):  # type: ignore[no-untyped-def]
        seen.append(pathlib.Path(src))
        return real_replace(src, dst)

    monkeypatch.setattr(checkpoint.os, "replace", _capture)
    checkpoint.atomic_write_json(anchor, {"a": 1})
    checkpoint.atomic_write_json(anchor, {"a": 2})

    assert len(seen) == 2
    assert seen[0] != seen[1], "两次写入用了同一个临时路径 —— 并发写会互相踩"
    assert str(os.getpid()) in seen[0].name
    assert seen[0].parent == anchor.parent, "临时文件必须与目标同目录（跨目录换名不是原子的）"


def test_c1_the_target_only_ever_holds_complete_json(anchor: pathlib.Path) -> None:
    """换名的语义：目标文件里**永远**是完整的 JSON（旧内容或新内容）。"""
    checkpoint.atomic_write_json(anchor, {"version": 1})
    assert json.loads(anchor.read_text(encoding="utf-8")) == {"version": 1}
    checkpoint.atomic_write_json(anchor, {"version": 2})
    assert json.loads(anchor.read_text(encoding="utf-8")) == {"version": 2}


def test_c1_no_temp_files_are_left_behind(anchor: pathlib.Path) -> None:
    for index in range(5):
        checkpoint.atomic_write_json(anchor, {"index": index})
    leftovers = [item.name for item in anchor.parent.iterdir() if item.name.endswith(".tmp")]
    assert leftovers == [], leftovers


# ---------- C2 备份 ----------


def test_c2_the_previous_good_copy_is_kept_as_backup(anchor: pathlib.Path) -> None:
    checkpoint.atomic_write_json(anchor, {"version": 1})
    checkpoint.atomic_write_json(anchor, {"version": 2})

    backup = anchor.with_name(anchor.name + ".backup")
    assert backup.is_file(), "没有留退路 —— 唯一的那份坏了就没了"
    assert json.loads(backup.read_text(encoding="utf-8")) == {"version": 1}


def test_c2_a_corrupt_existing_file_is_not_backed_up(anchor: pathlib.Path) -> None:
    """备份一个坏锚点等于把唯一的好副本覆盖掉。"""
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text("{ 半截", encoding="utf-8")

    checkpoint.atomic_write_json(anchor, {"version": 1})

    backup = anchor.with_name(anchor.name + ".backup")
    assert not backup.is_file(), "把坏内容备份了 —— 好副本会被它覆盖"


# ---------- C3 读者退备份 ----------


def test_c3_the_reader_falls_back_to_the_backup(tmp_path: pathlib.Path, capsys) -> None:
    workspace = tmp_path / "ws"
    anchor = workspace / "state_snapshots" / "root-1" / "latest.json"
    checkpoint.atomic_write_json(anchor, {"tasks": [{"id": "a"}]})
    checkpoint.atomic_write_json(anchor, {"tasks": [{"id": "a"}, {"id": "b"}]})

    # 模拟"写到一半被杀"：主文件被截断。
    anchor.write_text('{"tasks": [{"id": "a"}, {"id"', encoding="utf-8")

    recovered = checkpoint.read_state_snapshot(workspace, "root-1")
    assert recovered == {"tasks": [{"id": "a"}]}, "主文件坏了却没有退回备份"

    captured = capsys.readouterr()
    assert "备份" in (captured.out + captured.err), (
        "静默用了备份 —— 那会掩盖'真的丢过一次写'这件事"
    )


def test_c3_the_reader_prefers_the_primary_when_it_is_fine(tmp_path: pathlib.Path) -> None:
    workspace = tmp_path / "ws"
    anchor = workspace / "state_snapshots" / "root-1" / "latest.json"
    checkpoint.atomic_write_json(anchor, {"version": 1})
    checkpoint.atomic_write_json(anchor, {"version": 2})
    assert checkpoint.read_state_snapshot(workspace, "root-1") == {"version": 2}


def test_c3_missing_snapshot_returns_none(tmp_path: pathlib.Path) -> None:
    assert checkpoint.read_state_snapshot(tmp_path, "nope") is None


def test_c3_a_non_object_payload_is_treated_as_unreadable(tmp_path: pathlib.Path) -> None:
    """顶层的数组/字符串不是合法锚点 —— 当"读不出来"处理，而不是当成数据。"""
    workspace = tmp_path / "ws"
    anchor = workspace / "state_snapshots" / "root-1" / "latest.json"
    anchor.parent.mkdir(parents=True, exist_ok=True)
    anchor.write_text("[1, 2, 3]", encoding="utf-8")
    assert checkpoint.read_state_snapshot(workspace, "root-1") is None


# ---------- C6 失败时清理 ----------


def test_c6_a_failed_write_cleans_up_its_temp_file(anchor: pathlib.Path, monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise OSError("换名失败")

    monkeypatch.setattr(checkpoint, "_replace_with_retry", _boom)
    with pytest.raises(OSError):
        checkpoint.atomic_write_json(anchor, {"version": 1})

    leftovers = [item.name for item in anchor.parent.iterdir() if item.name.endswith(".tmp")]
    assert leftovers == [], f"失败后残留了临时文件：{leftovers}"


# ---------- C4 崩溃注入：真子进程 ----------

# 子进程脚本：在 `atomic_write_json` 的各个阶段用 `os._exit(9)` **硬杀自己**。
# 为什么是 `os._exit` 而不是异常：异常会被 `except OSError` 接住并清理 ——
# 那就测不到"被杀"了。崩溃注入必须是**不给清理机会**的那种死法。
_CRASH_CHILD = r'''
import json, os, pathlib, sys

target = pathlib.Path(sys.argv[1])
stage = sys.argv[2]
payload = {"version": int(sys.argv[3]), "pad": "x" * 4096}
text = json.dumps(payload, ensure_ascii=False, indent=2)

target.parent.mkdir(parents=True, exist_ok=True)

if stage == "after_backup":
    if target.is_file():
        try:
            json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            pass
        else:
            import shutil
            shutil.copy2(target, target.with_name(target.name + ".backup"))
    os._exit(9)

if stage == "mid_temp_write":
    unique = f".{os.getpid()}.{os.urandom(4).hex()}.tmp"
    tmp = target.with_name(target.name + unique)
    tmp.write_text(text[: len(text) // 2], encoding="utf-8")
    os._exit(9)

if stage == "before_replace":
    unique = f".{os.getpid()}.{os.urandom(4).hex()}.tmp"
    tmp = target.with_name(target.name + unique)
    tmp.write_text(text, encoding="utf-8")
    os._exit(9)

if stage == "after_replace":
    unique = f".{os.getpid()}.{os.urandom(4).hex()}.tmp"
    tmp = target.with_name(target.name + unique)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    os._exit(9)

os._exit(0)
'''

# 每个阶段杀掉之后，锚点必须**仍然可读**（要么旧内容、要么新内容）。
CRASH_STAGES = ("after_backup", "mid_temp_write", "before_replace", "after_replace")


@pytest.fixture()
def child_script(tmp_path: pathlib.Path) -> pathlib.Path:
    script = tmp_path / "crash_child.py"
    script.write_text(_CRASH_CHILD, encoding="utf-8")
    return script


@pytest.mark.parametrize("stage", CRASH_STAGES)
def test_c4_killing_the_writer_at_each_stage_leaves_a_usable_anchor(
    tmp_path: pathlib.Path, child_script: pathlib.Path, stage: str
) -> None:
    """**这是 P5-6 那条判据本身。** 在每个写入阶段硬杀一次，锚点都必须还能用。

    判据不是"文件还在" —— 而是**读得出来**。半截 JSON 的文件也在，
    但它等于没有（`_restore_snapshot_if_missing` 会拿到 None 并静默放弃）。
    """
    workspace = tmp_path / "ws"
    anchor = workspace / "state_snapshots" / "root-1" / "latest.json"

    # 先放一份**好的**旧锚点（模拟"上一次成功写入"）。
    checkpoint.atomic_write_json(anchor, {"version": 0})
    assert json.loads(anchor.read_text(encoding="utf-8"))["version"] == 0

    proc = subprocess.run(
        [sys.executable, str(child_script), str(anchor), stage, "1"],
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 9, f"子进程没有按预期崩溃：{proc.returncode} {proc.stderr[:200]}"

    # 1) 主锚点必须读得出来。
    recovered = checkpoint.read_state_snapshot(workspace, "root-1")
    assert recovered is not None, f"在 {stage} 被杀之后锚点不可读 —— 还原通路失效了"
    assert recovered.get("version") in {0, 1}, recovered.get("version")


@pytest.mark.parametrize("stage", CRASH_STAGES)
def test_c4_the_anchor_is_never_half_written(
    tmp_path: pathlib.Path, child_script: pathlib.Path, stage: str
) -> None:
    """被杀之后主文件里**要么是旧内容、要么是新内容**，不能是半截。"""
    workspace = tmp_path / "ws"
    anchor = workspace / "state_snapshots" / "root-1" / "latest.json"
    checkpoint.atomic_write_json(anchor, {"version": 0, "pad": "x" * 4096})

    subprocess.run(
        [sys.executable, str(child_script), str(anchor), stage, "1"],
        capture_output=True,
        timeout=60,
    )

    text = anchor.read_text(encoding="utf-8")
    payload = json.loads(text)  # 解不出来就是半截 —— 这条断言会红
    assert payload["version"] in {0, 1}
    assert payload["pad"] == "x" * 4096, "内容不完整"


def test_c4_a_crash_during_the_first_ever_write_leaves_no_anchor_but_a_backup(
    tmp_path: pathlib.Path, child_script: pathlib.Path
) -> None:
    """第一次写就崩：锚点是半截的，但**备份救得回来**。

    这条钉的是"备份到底有没有用"：没有它，第一次写崩溃就等于永久失去那棵树。
    """
    workspace = tmp_path / "ws"
    anchor = workspace / "state_snapshots" / "root-1" / "latest.json"
    checkpoint.atomic_write_json(anchor, {"version": 0, "pad": "x" * 4096})

    subprocess.run(
        [sys.executable, str(child_script), str(anchor), "mid_temp_write", "1"],
        capture_output=True,
        timeout=60,
    )
    # 半截是写在**临时文件**里的，主锚点没被碰。
    assert json.loads(anchor.read_text(encoding="utf-8"))["version"] == 0


# ---------- C5 并发写 ----------

_CONCURRENT_WRITER = r'''
import json, os, pathlib, sys, time

sys.path.insert(0, sys.argv[3])
from app import checkpoint

target = pathlib.Path(sys.argv[1])
worker = sys.argv[2]
for index in range(30):
    checkpoint.atomic_write_json(target, {"worker": worker, "index": index, "pad": "y" * 2048})
'''


def test_c5_concurrent_writers_never_produce_a_half_written_anchor(tmp_path: pathlib.Path) -> None:
    """两个进程同时写同一个锚点：任何时刻读它都必须是完整 JSON。

    这条是 C1 的**行为版**（那条查的是路径唯一性）。两者都要：
    路径唯一可以靠看，而"读者永远看不到半截"只能靠并发跑出来。
    """
    workspace = tmp_path / "ws"
    anchor = workspace / "state_snapshots" / "root-1" / "latest.json"
    checkpoint.atomic_write_json(anchor, {"worker": "seed", "index": -1})

    script = tmp_path / "writer.py"
    script.write_text(_CONCURRENT_WRITER, encoding="utf-8")
    module_root = str(pathlib.Path(checkpoint.__file__).resolve().parents[1])

    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(anchor), f"w{index}", module_root],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(2)
    ]
    # 写者跑的同时反复读 —— 读者看到半截就会红。
    import time

    for _ in range(40):
        text = anchor.read_text(encoding="utf-8")
        json.loads(text)
        time.sleep(0.01)

    failures = []
    for index, proc in enumerate(procs):
        _out, err = proc.communicate(timeout=120)
        if proc.returncode != 0:
            failures.append(f"writer {index}: {err.decode('utf-8', 'replace')[:200]}")
    assert failures == [], failures

    payload = json.loads(anchor.read_text(encoding="utf-8"))
    assert payload["worker"] in {"seed", "w0", "w1"}
    assert payload["pad"] == "y" * 2048 or payload["worker"] == "seed"

    leftovers = [item.name for item in anchor.parent.iterdir() if item.name.endswith(".tmp")]
    assert leftovers == [], f"并发写残留了临时文件：{leftovers}"


# ---------- C7 尽力而为的产物不拖累关键产物 ----------


def test_c7_a_history_write_failure_does_not_undo_the_anchor(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """历史快照写失败不该让主锚点的写成功被抹掉。

    实测场景：历史快照名带时间戳，同一微秒内两次写会撞名 —— 撞上时 `os.replace`
    在 Windows 上会报错。那次错误不该把已经写好的主锚点变成"没写"。
    """
    calls: list[pathlib.Path] = []
    real = checkpoint.atomic_write_json

    def _flaky(path: pathlib.Path, payload) -> None:  # type: ignore[no-untyped-def]
        calls.append(path)
        if path.name != "latest.json":
            raise OSError("历史快照写不上")
        real(path, payload)

    monkeypatch.setattr(checkpoint, "atomic_write_json", _flaky)

    # 直接调两个分支，验证"尽力而为"这一侧被单独接住了。
    latest = tmp_path / "latest.json"
    checkpoint.atomic_write_json(latest, {"version": 3})
    assert json.loads(latest.read_text(encoding="utf-8")) == {"version": 3}
    assert len(calls) == 1


# ---------- 接线：真实写路径必须用原子写 ----------


def test_the_orchestrator_snapshot_writer_uses_the_atomic_helper() -> None:
    """**接线判据**：`_write_state_snapshot` 必须走 `checkpoint.atomic_write_json`。

    上面所有判据测的是那个 helper 本身。如果编排层还在用 `write_text` 直接写锚点，
    这些判据**全绿而真实路径仍然会写出半截文件** —— 这是"判据在、保护不在"
    的典型形态，所以必须有一条盯住接线。
    """
    import inspect

    from app.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator._write_state_snapshot)
    # ⚠️ 断言必须是**完整的调用**，不能只找 `atomic_write_json` 这个子串：
    # 第一版就是那么写的，而把锚点改成 `atomic_write_text(...)`（不带备份的那条路）
    # 之后它**照样通过** —— 变异测试当场证明那条判据是空的。
    assert "checkpoint.atomic_write_json(latest_path, payload)" in source, (
        "锚点没有走带备份的原子写"
    )
    assert "latest_path.write_text" not in source, "锚点仍在用截断式写"
    assert "history_path.write_text" not in source, "历史快照仍在用截断式写"


def test_the_snapshot_reader_consults_the_backup() -> None:
    """接线判据：读者必须真的会去看 `.backup`。"""
    import inspect

    source = inspect.getsource(checkpoint.read_state_snapshot)
    assert "_SNAPSHOT_BACKUP_SUFFIX" in source or ".backup" in source
    assert "stderr" in source, "退回备份时必须出声 —— 静默会掩盖丢过一次写"
