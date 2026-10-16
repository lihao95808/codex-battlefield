"""档位审计与回滚的判据（P4-2）。

要解决的不是"能不能调"，而是"**调了之后查得到、能改回去**"。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| U1 | 硬边界：并发 0 / 999 这类值直接拒 | 0 = 静默停摆，999 = 压垮机器 |
| U2 | 危险方向按**方向**判断，不是"值变了" | 界面对收紧也弹确认 → 人开始无脑点确定 |
| U3 | 一批改动**要么全过、要么全不过** | 一半生效一半失败是最难查的状态 |
| U4 | 审计记录含旧值 + 谁 + 为什么 | 事后只能看到"现在是 5"，回滚也没依据 |
| U5 | 回滚目标是**上一次改动的旧值** | 让人去猜原来的值，猜错的概率不低 |
| U6 | 调档必须留名 | 事后无法回答"是谁改的" |
| U7 | 同一档位一次请求里不许重复 | 结果取决于应用顺序，不可预期 |
| U8 | 登记表里的键必须真的存在于 `Settings` | 面板显示一个永远不会生效的档位 |
"""
from __future__ import annotations

import pytest

from app import watchdog_tuning as wt
from app.config import Settings
from app.database import SessionLocal, engine, init_db
from app.models import Base, EventRecord


@pytest.fixture(autouse=True)
def clean_db():
    Base.metadata.drop_all(bind=engine)
    init_db()
    yield
    Base.metadata.drop_all(bind=engine)


CURRENT = {
    "max_concurrency": 13,
    "auto_concurrency_enabled": True,
    "auto_concurrency_min": 1,
    "auto_concurrency_max": 13,
    "agent_burst_limit": 0,
    "agent_burst_reserve": 0,
    "codex_idle_timeout_seconds": 1800,
    "machine_safety_min_available_memory_mb": 4096,
}


# ---------- U8 登记表与 Settings 对齐 ----------


def test_u8_every_registered_key_exists_on_settings() -> None:
    """**接线判据**：登记表里的键必须真的在 `Settings` 上。

    登记了一个不存在（或名字写错）的键，界面会显示它、能改它、写进配置文件 ——
    然后它永远不生效。这种失效没有任何报错。
    """
    values = wt.current_values(Settings())
    assert set(values) == {item["key"] for item in wt.REGISTRY}


def test_u8_every_registered_key_has_an_env_var_and_a_rationale() -> None:
    for item in wt.catalog():
        assert item["env"].startswith("CODEX_BATTLEFIELD_"), item["key"]
        assert item["rationale"], f"{item['key']} 没有写明为什么存在"
        assert item["kind"] in {"int", "bool"}


# ---------- U1 硬边界 ----------


@pytest.mark.parametrize("value", [0, -1])
def test_u1_concurrency_cannot_be_zero_or_negative(value: int) -> None:
    """并发 0 是**静默停摆** —— 它不是"用户的自由"，是危险值。"""
    with pytest.raises(wt.TuningError) as excinfo:
        wt.coerce("max_concurrency", value)
    assert "不能小于" in str(excinfo.value)


def test_u1_concurrency_has_an_upper_bound() -> None:
    with pytest.raises(wt.TuningError) as excinfo:
        wt.coerce("max_concurrency", 999)
    assert "不能大于" in str(excinfo.value)
    assert wt.coerce("max_concurrency", 64) == 64


def test_u1_non_integer_values_are_refused_for_numeric_keys() -> None:
    with pytest.raises(wt.TuningError):
        wt.coerce("max_concurrency", "many")


def test_u1_bool_keys_accept_the_usual_truthy_spellings() -> None:
    for text in ("1", "true", "YES", "on"):
        assert wt.coerce("auto_concurrency_enabled", text) is True
    for text in ("0", "false", "NO", "off"):
        assert wt.coerce("auto_concurrency_enabled", text) is False
    with pytest.raises(wt.TuningError):
        wt.coerce("auto_concurrency_enabled", "maybe")


def test_u1_unknown_keys_are_refused_with_the_list() -> None:
    with pytest.raises(wt.TuningError) as excinfo:
        wt.coerce("not_a_knob", 1)
    assert "max_concurrency" in str(excinfo.value)


# ---------- U2 危险方向 ----------


def test_u2_risky_is_judged_by_direction_not_by_change() -> None:
    """**方向**决定危险，不是"值变了"。

    机器安全水位调**小**才危险（守卫变松）；调大是收紧，不该弹确认。
    把方向搞反会让界面对收紧也弹确认，于是人开始无脑点"确定"。
    """
    assert wt.is_risky("max_concurrency", 5, 8) is True
    assert wt.is_risky("max_concurrency", 8, 5) is False

    assert wt.is_risky("machine_safety_min_available_memory_mb", 4096, 1024) is True
    assert wt.is_risky("machine_safety_min_available_memory_mb", 1024, 4096) is False


def test_u2_no_direction_means_never_risky() -> None:
    """没登记方向的档位永远不算危险 —— 未登记不等于"大概危险"。"""
    assert wt.is_risky("agent_burst_reserve", 0, 100) is False
    assert wt.is_risky("auto_concurrency_min", 1, 8) is False


def test_u2_missing_old_value_is_not_risky() -> None:
    """拿不到旧值时**不判定为危险**（不猜）—— 弹一个没有依据的确认比不弹更坏。"""
    assert wt.is_risky("max_concurrency", None, 64) is False


def test_u2_risky_is_marked_in_the_change_record() -> None:
    changes = wt.plan(
        [{"key": "max_concurrency", "value": 32, "reason": "赶进度"}],
        current=CURRENT,
        actor="duzuguang",
    )
    assert changes[0].risky is True
    assert "危险方向" in wt.render(changes)


# ---------- U3 全过或全不过 ----------


def test_u3_one_bad_value_rejects_the_whole_batch() -> None:
    """一半生效一半失败是最难查的状态：用户只看到一条错误，而第一项已经改了。"""
    with pytest.raises(wt.TuningError):
        wt.plan(
            [
                {"key": "max_concurrency", "value": 8},
                {"key": "auto_concurrency_min", "value": 0},  # 非法
            ],
            current=CURRENT,
            actor="duzuguang",
        )


def test_u3_empty_batch_is_refused() -> None:
    with pytest.raises(wt.TuningError) as excinfo:
        wt.plan([], current=CURRENT, actor="duzuguang")
    assert "没有要改" in str(excinfo.value)


def test_u3_duplicate_keys_in_one_request_are_refused() -> None:
    """结果取决于应用顺序 = 不可预期。"""
    with pytest.raises(wt.TuningError) as excinfo:
        wt.plan(
            [
                {"key": "max_concurrency", "value": 8},
                {"key": "max_concurrency", "value": 4},
            ],
            current=CURRENT,
            actor="duzuguang",
        )
    assert "两次" in str(excinfo.value)


# ---------- U6 留名 ----------


def test_u6_planning_requires_an_actor() -> None:
    with pytest.raises(wt.TuningError) as excinfo:
        wt.plan([{"key": "max_concurrency", "value": 8}], current=CURRENT, actor="   ")
    assert "留名" in str(excinfo.value)


# ---------- U4 审计 ----------


def test_u4_record_keeps_old_new_actor_and_reason() -> None:
    changes = wt.plan(
        [{"key": "max_concurrency", "value": 8, "reason": "机器空着"}],
        current=CURRENT,
        actor="duzuguang",
    )
    with SessionLocal() as db:
        wt.record(db, changes, applied=True)
        rows = db.query(EventRecord).filter(EventRecord.type == wt.EVENT_TYPE).all()
        history = wt.history(db)

    assert len(rows) == 1
    assert "13 → 8" in rows[0].message
    assert len(history) == 1
    entry = history[0]
    assert entry["actor"] == "duzuguang"
    assert entry["reason"] == "机器空着"
    assert entry["applied"] is True
    assert entry["changes"][0] == {
        "key": "max_concurrency",
        "label": "最大并发",
        "old": 13,
        "new": 8,
        "actor": "duzuguang",
        "reason": "机器空着",
        "risky": False,
    }


def test_u4_rolled_back_changes_are_marked_as_such() -> None:
    """回滚本身也要留痕 —— 否则"改了又改回去"看起来像什么都没发生。"""
    changes = wt.plan([{"key": "max_concurrency", "value": 8}], current=CURRENT, actor="me")
    with SessionLocal() as db:
        wt.record(db, changes, applied=False)
        entry = wt.history(db)[0]
    assert entry["applied"] is False
    assert "[已回滚]" in entry["message"]


def test_u4_history_is_newest_first_and_bounded() -> None:
    with SessionLocal() as db:
        for value in (8, 9, 10):
            changes = wt.plan(
                [{"key": "max_concurrency", "value": value}], current=CURRENT, actor="me"
            )
            wt.record(db, changes, applied=True)
        history = wt.history(db, limit=2)

    assert len(history) == 2
    assert "13 → 10" in history[0]["message"]
    assert "13 → 9" in history[1]["message"]


# ---------- U5 回滚 ----------


def test_u5_rollback_targets_are_the_previous_values() -> None:
    history = [
        {
            "actor": "duzuguang",
            "changes": [
                {"key": "max_concurrency", "label": "最大并发", "old": 13, "new": 8},
            ],
        }
    ]
    targets = wt.rollback_targets(history)
    assert len(targets) == 1
    assert targets[0].key == "max_concurrency"
    assert targets[0].new == 13, "回滚目标应当是**旧值**"
    assert targets[0].old == 8
    assert "回滚" in targets[0].reason


def test_u5_rollback_ignores_unknown_keys() -> None:
    """历史里可能有已下线的档位 —— 回滚时跳过它，而不是崩。"""
    history = [
        {
            "actor": "me",
            "changes": [
                {"key": "removed_knob", "label": "旧档位", "old": 1, "new": 2},
                {"key": "max_concurrency", "label": "最大并发", "old": 13, "new": 8},
            ],
        }
    ]
    targets = wt.rollback_targets(history)
    assert [item.key for item in targets] == ["max_concurrency"]


def test_u5_rollback_of_multiple_steps_dedupes_the_same_key() -> None:
    """同一个档位改过多次时，"回滚 N 步"只产生**一条**改动，目标是这批里最早的旧值。

    为什么必须去重：逐条收集会让"回滚 2 步"在同一个键上产生两条 `Change`，
    而 `plan()` 的重复键校验会拒绝整批 —— 也就是说**多步回滚会完全不可用**，
    而界面上的表现只是一句"同一个档位出现了两次"。实测由端点判据抓到。
    """
    history = [
        {"actor": "a", "changes": [{"key": "max_concurrency", "label": "最大并发", "old": 9, "new": 5}]},
        {"actor": "b", "changes": [{"key": "max_concurrency", "label": "最大并发", "old": 13, "new": 9}]},
    ]
    targets = wt.rollback_targets(history, limit=2)
    assert len(targets) == 1, "同一个键不该产生两条回滚改动"
    assert targets[0].key == "max_concurrency"
    assert targets[0].new == 13, "目标应当是这批里**最早**那次的旧值（回到改动之前）"
    # `old` 是"当前值"＝这批里**最新**那次的新值，回滚记录靠它显示 9 → 13。
    assert targets[0].old == 9


def test_u5_rollback_with_limit_one_takes_the_latest_change() -> None:
    history = [
        {"actor": "a", "changes": [{"key": "max_concurrency", "label": "最大并发", "old": 9, "new": 5}]},
        {"actor": "b", "changes": [{"key": "max_concurrency", "label": "最大并发", "old": 13, "new": 9}]},
    ]
    targets = wt.rollback_targets(history, limit=1)
    assert [item.new for item in targets] == [9]


def test_u5_rollback_keeps_distinct_keys_separate() -> None:
    history = [
        {
            "actor": "me",
            "changes": [
                {"key": "max_concurrency", "label": "最大并发", "old": 13, "new": 8},
                {"key": "agent_burst_limit", "label": "本轮 agent 上限", "old": 0, "new": 5},
            ],
        }
    ]
    targets = {item.key: item.new for item in wt.rollback_targets(history)}
    assert targets == {"max_concurrency": 13, "agent_burst_limit": 0}


def test_u5_corrupt_history_payload_does_not_crash_history() -> None:
    with SessionLocal() as db:
        db.add(
            EventRecord(
                task_id=None,
                type=wt.EVENT_TYPE,
                message="坏记录",
                payload_json="{not json",
            )
        )
        db.commit()
        entry = wt.history(db)[0]
    assert entry["changes"] == []
    assert entry["message"] == "坏记录"


# ---------- 配置写入 ----------


def test_apply_writes_through_the_same_path_as_the_settings_panel(tmp_path, monkeypatch) -> None:
    """与 P2-2 的面板走**同一条写路径**。

    两条写路径会漂移，而漂移的表现是"面板里改了、这里改了、结果只剩一个生效"。
    """
    config = tmp_path / "cfg.toml"
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(config))

    changes = wt.plan([{"key": "max_concurrency", "value": 8}], current=CURRENT, actor="me")
    wt.apply_to_config_file(changes)

    assert config.is_file()
    assert "max_concurrency = 8" in config.read_text(encoding="utf-8")


def test_apply_keeps_other_keys(tmp_path, monkeypatch) -> None:
    config = tmp_path / "cfg.toml"
    config.write_text('runner_mode = "real"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(config))

    changes = wt.plan([{"key": "max_concurrency", "value": 4}], current=CURRENT, actor="me")
    wt.apply_to_config_file(changes)

    text = config.read_text(encoding="utf-8")
    assert 'runner_mode = "real"' in text
    assert "max_concurrency = 4" in text
