"""管家**主动推送**与**看门狗兼容层**的判据（`docs/spec-steward.md` §7、
`docs/spec-watchdog-steward.md`）。

推送最容易犯的错不是"推不出去"，而是**推太多**：一个持续存在的状况每轮都推一次，
用户很快就学会无视提醒 —— 那时提醒就等于没有。所以本文件大半在钉"什么时候**不该**推"。

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| P1 | 提醒**没变化就不推**（边沿触发） | 持续阻塞的节点每轮弹一次，用户对提醒脱敏 |
| P2 | 消息里的**数字变化不算"新情况"** | "3 个失败"变"4 个失败"会让节流形同虚设 |
| P3 | 低于打扰阈值的不推，但**仍要能看见** | 要么把用户淹掉，要么用户看不到全貌 |
| P4 | 每次推送说明**为什么现在推** | 用户分不清"刚出现的"和"挂了两小时的" |
| P5 | 评估不阻塞事件循环 | 推送开着时其他请求变慢 |
| W1 | 看门狗心跳可读，且**停摆可判** | 看门狗静默停摆时无人知道，树在无人看管地跑 |
| W2 | 无心跳时**不谎报**停摆 | 新装/清零后每次启动都假警 |
| W3 | 心跳的时间是**字符串**也要解析 | 直接传给 `coerce_utc` 会 AttributeError（实测踩到） |
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import steward_alerts, steward_proactive
from app.config import get_settings, machine_root_of
from app.database import SessionLocal, init_db
from app.watchdog import _parse_iso_utc, watchdog_heartbeat_state


def _settings(**overrides):
    base = get_settings()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update(overrides)
    return base.__class__(**fields)


@pytest.fixture()
def heartbeat_home(tmp_path, monkeypatch):
    """把**机器级状态落点**指到 tmp_path，并返回它。

    这里有个真实的不一致（实测踩到，值得记下来）：

    * `CODEX_BATTLEFIELD_STATE_HOME` 是 `load_layout()` 在**运行时**读的，
      所以 monkeypatch 它有效；
    * 但 `CODEX_BATTLEFIELD_WORKSPACE` 是 `config.py` 在**模块导入时**读的
      （`_workspace_override`），monkeypatch 它**无效**；
    * 而未绑定工程时 `machine_root` 的语义恰恰是"退回 `workspace_dir`"
      （见 `config.py` 的 `machine_root` / `machine_root_of`）。

    于是"只设状态根"并不能把机器级文件挪走 —— 它们仍落在**仓库的 `workspace/`**，
    测试会去读写真实目录（典型的环境泄漏，也正是 `CODEX_BATTLEFIELD_WORKSPACE`
    当初被加进 conftest 的原因）。

    所以这里不跟它较劲：直接接受代码实际解析出来的落点，但**断言它在 tmp 下**
    （conftest 已把 `workspace` 指到进程级临时目录），然后在那个位置写心跳。
    测的是真实解析行为，而不是我以为的解析行为。
    """
    monkeypatch.setenv("CODEX_BATTLEFIELD_STATE_HOME", str(tmp_path))
    settings = get_settings()
    root = machine_root_of(settings)
    # conftest 会把 workspace 指到 %TEMP%/codex_battlefield_state_test_<pid>/workspace。
    # 断言它不在仓库里 —— 这条守住"测试不脏化 checkout"。
    assert "codex-battlefield\\workspace" not in str(root), (
        f"机器级状态落点指向了仓库（{root}）—— 测试会脏化 checkout"
    )
    root.mkdir(parents=True, exist_ok=True)
    # **清掉可能残留的心跳文件**：未绑定工程时机器级落点与 `workspace_dir` 是同一个目录，
    # 而那个目录在同一个 worker 里是**跨用例共享**的。别的用例写过心跳之后，
    # 这里就会拿到 `available=True`，于是 W2（"没有心跳不谎报停摆"）偶发失败 ——
    # 实测到的形态是 `assert True is False`，而且只在部分运行里出现。
    # 这一条不是在跟代码较劲，是把前置条件**做实**：没有心跳就该从"确实没有"开始。
    (root / "watchdog_status.json").unlink(missing_ok=True)
    return root


def _alert(kind: str, level: str, message: str = "x") -> dict[str, str]:
    return {"kind": kind, "level": level, "message": message}


# ---------- P1 边沿触发 ----------


def test_p1_digest_is_stable_for_the_same_alert_set() -> None:
    """同一组提醒 → 同一指纹。没有这条，"没变化就不推"根本无从判断。"""
    a = [_alert("blocked_nodes", "warning"), _alert("pending_actions", "action")]
    b = [_alert("pending_actions", "action"), _alert("blocked_nodes", "warning")]
    assert steward_alerts.alerts_digest(a) == steward_alerts.alerts_digest(b), "顺序不该影响指纹"


def test_p1_digest_changes_when_the_level_changes() -> None:
    """等级变了 = 新的情况，必须能触发推送。"""
    before = [_alert("blocked_nodes", "info")]
    after = [_alert("blocked_nodes", "warning")]
    assert steward_alerts.alerts_digest(before) != steward_alerts.alerts_digest(after)


def test_p1_digest_changes_when_a_kind_appears_or_disappears() -> None:
    """出现/消失也是新情况。"""
    base = [_alert("blocked_nodes", "warning")]
    assert steward_alerts.alerts_digest(base) != steward_alerts.alerts_digest(
        base + [_alert("pending_actions", "action")]
    )
    assert steward_alerts.alerts_digest(base) != steward_alerts.alerts_digest([])


def test_p1_digest_ignores_message_wording() -> None:
    """**这一条是节流的关键**：消息里的数字/标题变了，不算"新情况"。

    否则"3 个节点失败"变"4 个节点失败"会让每一轮都变成"有变化"，
    节流退化成轮询 —— 而轮询正是推送要取代的东西。
    """
    before = [_alert("failed_nodes", "warning", "3 个节点失败，值得看一眼原因。")]
    after = [_alert("failed_nodes", "warning", "4 个节点失败，值得看一眼原因。")]
    assert steward_alerts.alerts_digest(before) == steward_alerts.alerts_digest(after), (
        "消息措辞变化被当成了新情况 —— 持续存在的状况会被反复推送"
    )


def test_p1_a_persistent_alert_produces_exactly_one_edge() -> None:
    """模拟流的核心循环：同一个提醒持续 5 轮，**只应产生 1 次推送**。

    这是在测"节流算法"，不是在测某个函数 —— 因为要钉的正是这个循环不变量。
    """
    alerts = [_alert("pending_actions", "action")]
    last_digest: str | None = None
    pushes = 0
    for _ in range(5):
        digest = steward_alerts.alerts_digest(alerts)
        if digest != last_digest:
            pushes += 1
        last_digest = digest
    assert pushes == 1, f"持续存在的提醒被推了 {pushes} 次（应恰好 1 次）"


# ---------- P2/P3 阈值 ----------


def test_p3_worth_pushing_filters_by_level() -> None:
    alerts = [
        _alert("usage_unavailable", "info"),
        _alert("failed_nodes", "warning"),
        _alert("pending_actions", "action"),
    ]
    assert [a["kind"] for a in steward_alerts.worth_pushing(alerts, "action")] == ["pending_actions"]
    assert [a["kind"] for a in steward_alerts.worth_pushing(alerts, "warning")] == [
        "failed_nodes",
        "pending_actions",
    ]
    assert len(steward_alerts.worth_pushing(alerts, "info")) == 3


def test_p3_default_threshold_is_action_only() -> None:
    """默认阈值必须是 `action`：那是唯一需要用户**动手**的信号。

    把参考信息（info）也推出来，会把"有动作等你决定"淹掉 —— 而那条最不该被淹。
    """
    assert steward_alerts.DEFAULT_PUSH_MIN_LEVEL == "action"
    assert _settings().steward_push_min_level == "action"


def test_p3_unknown_level_never_passes_a_real_threshold() -> None:
    """未知等级不该被当成"很严重"而漏出去 —— 失效方向必须偏向安静。"""
    assert steward_alerts.level_rank("nonsense") == 0
    assert steward_alerts.worth_pushing([_alert("x", "nonsense")], "info") == []


# ---------- P4 为什么现在推 ----------


def test_p4_push_events_carry_a_reason() -> None:
    """每次推送都要带 `reason`。没有它，用户分不清"刚出现"和"挂着呢"。"""
    from app.main import _push_reason

    first = _push_reason([_alert("pending_actions", "action")], [_alert("pending_actions", "action")], "action", first=True)
    assert "首帧" in first

    new = _push_reason(
        [_alert("pending_actions", "action")], [_alert("pending_actions", "action")], "action", first=False
    )
    assert "pending_actions" in new, "没说是哪种提醒触发的"

    quiet = _push_reason([_alert("usage_unavailable", "info")], [], "action", first=False)
    assert "阈值" in quiet, "低等级提醒该说明'低于阈值'"

    cleared = _push_reason([], [], "action", first=False)
    assert "清空" in cleared


# ---------- P5 不阻塞事件循环 ----------


def test_p5_the_stream_evaluates_off_the_event_loop() -> None:
    """推送流必须把同步的 `evaluate` 丢进线程池。

    直接在事件循环里跑同步 SQLAlchemy + 文件 IO 会**阻塞整个 API** ——
    表现为"推送开着的时候其他请求变慢"，而这种症状很难归因到推送。
    判据：`asyncio.to_thread` 真的被用到了（源码级断言，因为它是个结构性约束）。
    """
    import inspect

    from app import main

    source = inspect.getsource(main.steward_alerts_stream)
    assert "asyncio.to_thread" in source, (
        "推送流在事件循环里直接跑同步 evaluate —— 会拖慢整个 API"
    )


def test_p5_sse_frames_are_well_formed() -> None:
    """SSE 帧格式：`event:` 行 + `data:` JSON + 空行结尾。

    格式错了浏览器会**静默丢弃**整个事件（不报错），表现为"推送永远是空的"。
    """
    from app.main import _sse

    frame = _sse("alerts", {"a": 1})
    assert frame.startswith("event: alerts\n")
    assert "data: " in frame
    assert frame.endswith("\n\n"), "SSE 帧必须以空行结尾，否则浏览器不派发"
    payload = json.loads(frame.split("data: ", 1)[1].strip())
    assert payload == {"a": 1}


# ---------- W1/W2/W3 看门狗心跳 ----------


def _write_heartbeat(state_home, payload: dict) -> None:
    path = state_home / "watchdog_status.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_w3_parses_iso_strings_with_z_suffix() -> None:
    """心跳里的时间是**字符串**，而且 PowerShell 的 `o` 格式带 `Z` 后缀。

    直接把它交给 `coerce_utc` 会在 `value.tzinfo` 上抛 AttributeError —— 实测踩到过。
    """
    parsed = _parse_iso_utc("2026-10-15T07:14:38.4035819Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2026 and parsed.hour == 7


def test_w3_rejects_garbage_without_inventing_a_time() -> None:
    """解析不了就返回 None，**绝不**退回"当前时间"—— 那会把停摆伪装成正常。"""
    assert _parse_iso_utc("not a time") is None
    assert _parse_iso_utc("") is None
    assert _parse_iso_utc(None) is None
    assert _parse_iso_utc(12345) is None


def test_w2_missing_heartbeat_does_not_claim_staleness(heartbeat_home) -> None:
    """**没有心跳文件 ≠ 看门狗停摆。**

    新装、刚清零、或换了个状态根，都会没有心跳文件。这时若报"停摆"，
    就是每次新装都来一次假警 —— 而假警会训练用户无视真警。
    """
    state = watchdog_heartbeat_state(get_settings())
    assert state["available"] is False
    assert state["stale"] is False, "没有心跳文件却断言停摆（新装/清零就会假警）"
    assert state["stale_reason"], "该说明为什么没有数据"


def test_w1_fresh_heartbeat_is_not_stale(heartbeat_home) -> None:
    """刚跑完的心跳不该被判停摆（对照，证明上一条不是恒真）。"""
    now = datetime.now(timezone.utc)
    _write_heartbeat(
        heartbeat_home,
        {
            "finished_at": now.isoformat(),
            "interval_seconds": 180,
            "stale_after_seconds": 60,
            "status": "completed",
        },
    )
    state = watchdog_heartbeat_state(get_settings())
    assert state["available"] is True
    assert state["stale"] is False, f"刚跑完却被判停摆：{state['stale_reason']}"
    assert state["age_seconds"] is not None and state["age_seconds"] < 60


def test_w1_old_heartbeat_is_stale_with_an_explanation(heartbeat_home) -> None:
    """心跳过期 → 判停摆，并且**说清阈值怎么来的**（间隔 + 宽限）。

    这是本层最核心的判据：看门狗静默停摆时，树在无人看管地跑，
    而没有任何地方会报错 —— 能不能发现就靠这一条。
    """
    old = datetime.now(timezone.utc) - timedelta(seconds=3600)
    _write_heartbeat(
        heartbeat_home,
        {
            "finished_at": old.isoformat(),
            "interval_seconds": 180,
            "stale_after_seconds": 60,
            "status": "completed",
        },
    )
    state = watchdog_heartbeat_state(get_settings())
    assert state["stale"] is True, "一小时没心跳却没判停摆"
    # 阈值必须可解释：180 + 60 = 240
    assert "240" in state["stale_reason"], f"没说清阈值来源：{state['stale_reason']}"
    assert state["age_seconds"] > 3000


def test_w1_threshold_follows_the_declared_interval(heartbeat_home) -> None:
    """阈值跟着**心跳里声明的间隔**走，而不是硬编码。

    两边各自硬编码的话，把间隔改成 5 分钟后一边认为正常、另一边报警 ——
    这是最难查的一类不一致。
    """
    age = 400  # 秒
    old = datetime.now(timezone.utc) - timedelta(seconds=age)
    base = {"finished_at": old.isoformat(), "stale_after_seconds": 60, "status": "completed"}

    # 间隔 180s + 宽限 60s = 240s < 400s → 停摆
    _write_heartbeat(heartbeat_home, {**base, "interval_seconds": 180})
    assert watchdog_heartbeat_state(get_settings())["stale"] is True

    # 间隔 600s + 宽限 60s = 660s > 400s → 正常
    _write_heartbeat(heartbeat_home, {**base, "interval_seconds": 600})
    assert watchdog_heartbeat_state(get_settings())["stale"] is False, (
        "把间隔调大之后仍按旧阈值报警 —— 阈值没有跟着声明走"
    )


def test_w2_failed_status_is_reported(heartbeat_home) -> None:
    """`status=failed` 要能读出来（区别于"没跑"和"跑成功了"）。"""
    now = datetime.now(timezone.utc)
    _write_heartbeat(
        heartbeat_home,
        {
            "finished_at": now.isoformat(),
            "interval_seconds": 60,
            "stale_after_seconds": 60,
            "status": "failed",
            "reason": "树读取失败",
        },
    )
    state = watchdog_heartbeat_state(get_settings())
    assert state["status"] == "failed"
    assert state["reason"] == "树读取失败"


def test_w1_unreadable_heartbeat_is_reported_not_swallowed(heartbeat_home) -> None:
    """心跳文件坏了要说出来，不能静默当成"没有心跳"。"""
    (heartbeat_home / "watchdog_status.json").write_text("{not json", encoding="utf-8")
    state = watchdog_heartbeat_state(get_settings())
    assert state["available"] is False
    assert state["stale"] is False
    assert "读取失败" in state["stale_reason"] or "解析" in state["stale_reason"]


# ---------- 兼容层：快照与提醒 ----------


def test_watchdog_snapshot_is_present_and_shaped() -> None:
    """状态快照里必须有 `watchdog` 段 —— 管家靠它讲"机器那边怎么样"。"""
    init_db()
    settings = _settings()
    with SessionLocal() as db:
        snapshot = steward_alerts.evaluate(db, settings)
    assert "counts" in snapshot
    # evaluate 只返回 alerts/counts，watchdog 段在 build_status 里 —— 直接验后者
    from app.steward_status import build_status

    with SessionLocal() as db:
        full = build_status(db, settings)
    watchdog = full["watchdog"]
    assert set(watchdog) >= {"heartbeat", "guardian", "machine", "hibernating", "reason"}
    assert "available" in watchdog["heartbeat"]


def test_stale_watchdog_on_an_empty_tree_is_not_an_alert() -> None:
    """**空树上不报"看门狗停摆"**。

    空树没有依赖看门狗的东西，这时报停摆是打扰（新装、闲置都会命中）。
    只在真有活可看时才值得打扰用户。
    """
    init_db()
    settings = _settings()
    with SessionLocal() as db:
        snapshot = steward_alerts.evaluate(db, settings)
        # 确保这棵"树"是空的（测试库），且提醒里没有 watchdog_stale
        assert snapshot["counts"]["total"] == 0
        kinds = [a["kind"] for a in snapshot["alerts"]]
        assert "watchdog_stale" not in kinds, f"空树上报了看门狗停摆：{kinds}"


def test_evaluate_is_callable_off_the_main_thread() -> None:
    """`evaluate` 是同步函数，必须能从线程池里调用（推送流就是这么用的）。

    这条测的是**可被 to_thread 调用**这个前提本身；如果哪天它变成 async，
    推送流的 `asyncio.to_thread` 会拿到一个协程对象而不是结果 —— 静默错类型。
    """
    import inspect

    assert not inspect.iscoroutinefunction(steward_alerts.evaluate), (
        "evaluate 变成 async 了，而推送流仍用 asyncio.to_thread 调它"
    )

    init_db()
    settings = _settings()
    with SessionLocal() as db:
        result = asyncio.run(asyncio.to_thread(lambda: steward_alerts.evaluate(db, settings)))
    assert "alerts" in result


# ---------- 主动汇报：核心是**克制** ----------


def test_proactive_stays_silent_on_first_evaluation() -> None:
    """进程刚起来时**不开口**。

    否则每次重启 API 都来一句"你好，现在的情况是…"，而用户什么都没做 ——
    这是最容易把主动功能做成噪声的方式，而一旦成了噪声，用户就会关掉它，
    连原本有用的那部分也一起失效。
    """
    engine = steward_proactive.ProactiveEngine()
    speak, reason = engine.evaluate([_alert("pending_actions", "action")], now=1000.0)
    assert speak is False, f"第一次评估就开口了：{reason}"
    assert reason == ""


def test_proactive_ignores_info_only_changes() -> None:
    """`info` 级变化不触发 —— 那是"供参考"，不是"要动手"。"""
    engine = steward_proactive.ProactiveEngine()
    engine.seed([_alert("usage_unavailable", "info")])
    speak, _ = engine.evaluate(
        [_alert("usage_unavailable", "info"), _alert("outline_truncated", "info")], 10_000.0
    )
    assert speak is False, "只有 info 级变化却开口了"


def test_proactive_speaks_when_an_action_alert_appears() -> None:
    """出现 `action` 级提醒（唯一需要用户动手的一类）→ 开口。"""
    engine = steward_proactive.ProactiveEngine()
    engine.seed([_alert("usage_unavailable", "info")])
    speak, reason = engine.evaluate([_alert("pending_actions", "action")], 10_000.0)
    assert speak is True, "出现 action 级提醒却没开口"
    assert "action" in reason


def test_proactive_speaks_when_important_alerts_clear() -> None:
    """重要提醒**清空**也要说 —— 用户可能在等"好了没"。"""
    engine = steward_proactive.ProactiveEngine()
    engine.seed([_alert("pending_actions", "action")])
    speak, reason = engine.evaluate([], 10_000.0)
    assert speak is True, "重要提醒清空了却没告诉用户"
    assert "清空" in reason


def test_proactive_does_not_repeat_itself() -> None:
    """同一个重要状况持续存在 → **只在第一次开口**。

    这条是"克制"的核心判据：否则一个持续阻塞的节点会每 60 秒说一次，
    而用户对重复的反应是关掉它。
    """
    engine = steward_proactive.ProactiveEngine()
    engine.seed([_alert("usage_unavailable", "info")])
    alerts = [_alert("pending_actions", "action")]

    spoke = sum(1 for i in range(5) if engine.evaluate(alerts, 10_000.0 + i * 10_000)[0])
    assert spoke == 1, f"同一个状况说了 {spoke} 次（应恰好 1 次）"


def test_proactive_respects_the_cooldown() -> None:
    """两次开口之间必须隔够冷却时间（那是一次真实的模型调用）。

    注意这里每一轮的提醒**内容都不同**：如果内容相同，那本来就因为"没变化"而不开口，
    测出来的就不是冷却了（第一版测试踩过这个坑）。
    """
    engine = steward_proactive.ProactiveEngine(min_interval_seconds=900)
    engine.seed([])
    # 第一次：出现 action -> 开口
    assert engine.evaluate([_alert("pending_actions", "action")], 10_000.0)[0] is True
    # 立刻换成另一类重要提醒（内容确实变了），但还在冷却里 -> 不开口
    speak, _ = engine.evaluate([_alert("watchdog_stale", "action")], 10_100.0)
    assert speak is False, "冷却期内又开口了"
    # 过了冷却，且再换一类 -> 开口
    speak, _ = engine.evaluate([_alert("failed_nodes", "warning")], 10_000.0 + 901)
    assert speak is True, "过了冷却仍不开口"


def test_proactive_kind_change_is_noticed_even_at_the_same_level() -> None:
    """等级没变但**种类**变了（节点失败 -> 看门狗停摆）也算新情况。"""
    engine = steward_proactive.ProactiveEngine(min_interval_seconds=0)
    engine.seed([_alert("failed_nodes", "action")])
    speak, reason = engine.evaluate([_alert("watchdog_stale", "action")], 10_000.0)
    assert speak is True
    # reason 里要带上**具体是哪个状况**，否则用户不知道发生了什么
    assert "watchdog_stale" in reason or "watchdog" in reason, f"没说是哪个状况：{reason}"


def test_proactive_disabled_by_default() -> None:
    """主动汇报默认**关**：它会花钱、会打扰，该由用户显式打开。"""
    assert _settings().steward_proactive_enabled is False


def test_proactive_bool_reads_from_the_user_config_file(tmp_path, monkeypatch) -> None:
    """布尔档位必须能从**用户配置文件**读出来。

    回归（2026-10-15 实测踩到）：这个字段原先只认环境变量，于是
    `get_settings()` 在配置文件写了 `true` 时仍然是 `False`。
    这类失效没有任何症状 —— 只是"我明明在配置文件里开了它，它还是不动"。

    根因是**布尔不能走读字符串那条路**：`read_config_value` 只认 `str`，
    而 TOML 的 `true` 解析出来是 Python `bool`，于是静默读不到。

    注意这里直接测 `_env_truthy` 而**不是** `get_settings()`：
    `Settings` 的字段默认值在**类定义时**（即模块 import 时）求值，
    所以 monkeypatch `CODEX_BATTLEFIELD_CONFIG` 对已经建好的类没有任何影响 ——
    第一版测试就栽在这里（断言 `get_settings()` 会变，它不会）。
    测那个真正读配置的函数，才是这条回归该钉的地方。
    """
    config = tmp_path / "cfg.toml"
    config.write_text(
        "probe_flag = true\nprobe_number = 42\n", encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(config))
    monkeypatch.delenv("PROBE_FLAG_ENV", raising=False)
    monkeypatch.delenv("PROBE_NUM_ENV", raising=False)

    from app.config import _env_float_or_config, _env_truthy

    assert _env_truthy("PROBE_FLAG_ENV", "probe_flag") is True, (
        "配置文件里的 `true` 没被读到 —— 布尔档位又被当成字符串读了"
    )
    assert _env_float_or_config("PROBE_NUM_ENV", "probe_number", 0.0) == 42.0


def test_config_bool_helper_handles_every_shape(tmp_path, monkeypatch) -> None:
    """`_env_truthy` 的完整真值表：字符串真值、字符串假值、缺键、坏文件。"""
    from app.config import _env_truthy

    monkeypatch.delenv("PROBE_FLAG_ENV", raising=False)

    config = tmp_path / "cfg.toml"
    config.write_text('a = "yes"\nb = "0"\nc = false\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(config))

    assert _env_truthy("PROBE_FLAG_ENV", "a") is True, "字符串 'yes' 该为真"
    assert _env_truthy("PROBE_FLAG_ENV", "b") is False, "字符串 '0' 该为假"
    assert _env_truthy("PROBE_FLAG_ENV", "c") is False, "bool false 该为假"
    assert _env_truthy("PROBE_FLAG_ENV", "missing") is False, "缺键该为假（默认关）"

    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(tmp_path / "does-not-exist.toml"))
    assert _env_truthy("PROBE_FLAG_ENV", "a") is False, "配置文件不存在该退回默认关"


def test_proactive_bool_env_overrides_the_config_file(tmp_path, monkeypatch) -> None:
    """对照：环境变量仍然优先（配置文件说 true，环境变量说 false）。"""
    config = tmp_path / "cfg.toml"
    config.write_text("probe_flag = true\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_BATTLEFIELD_CONFIG", str(config))
    monkeypatch.setenv("PROBE_FLAG_ENV", "0")

    from app.config import _env_truthy

    assert _env_truthy("PROBE_FLAG_ENV", "probe_flag") is False, "环境变量没有优先于配置文件"


def test_proactive_settings_are_wired_to_the_config_helpers() -> None:
    """字段必须**接在**读配置文件的那两个函数上。

    这条与上面两条互补：上面证明"读配置的函数是对的"，
    这条证明"字段确实用了它们"。少任何一半，那句配置就仍然是死的。
    """
    import inspect

    from app.config import Settings

    source = inspect.getsource(Settings)
    assert "_env_truthy" in source, "steward_proactive_enabled 没有接在读配置的辅助函数上"
    assert "_env_float_or_config" in source, (
        "steward_proactive_min_interval_seconds 没有接在读配置的辅助函数上"
    )


def test_proactive_interval_setting_reaches_the_engine() -> None:
    """配置的冷却时间必须**真的接到引擎上**。

    回归：第一版里 `min_interval` 算出来却忘了赋给引擎，于是
    `steward_proactive_min_interval_seconds` 完全不起作用（引擎一直用模块默认值）。
    是 ruff 的"未使用变量"抓到的 —— 而这类 bug 没有任何症状，只是"配置不生效"。
    """
    import inspect

    from app import main

    source = inspect.getsource(main._steward_proactive_loop)
    assert "min_interval_seconds" in source, (
        "配置的冷却时间没有传给引擎 —— 这个设置会是死的"
    )


def test_proactive_loop_exists_and_is_cancelled_on_shutdown() -> None:
    """lifespan 必须真的启动并**取消**后台任务。

    不取消的话，热重载/测试里会攒下一堆每 60 秒跑一次的循环，
    而它们各自持有一个数据库会话 —— 表现为"跑久了连接池不够用"。
    """
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    assert "_steward_proactive_loop" in source, "lifespan 没启动主动汇报任务"
    assert "cancel" in source, "lifespan 没取消主动汇报任务"


def test_stale_watchdog_still_not_reported_on_an_empty_tree() -> None:
    """回归：给主动汇报加完触发条件后，空树仍然不该报看门狗停摆。"""
    init_db()
    settings = _settings()
    with SessionLocal() as db:
        snapshot = steward_alerts.evaluate(db, settings)
        assert snapshot["counts"]["total"] == 0
        kinds = [a["kind"] for a in snapshot["alerts"]]
        assert "watchdog_stale" not in kinds, f"空树上报了看门狗停摆：{kinds}"
