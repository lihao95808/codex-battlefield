"""**自动策略**（P3-5）：连续失败自动停、收益递减自动降并发、空闲自动休眠。

## 这个模块解决的不是"能不能自动"，而是"自动得看不看得见"

系统里本来就有这些动作（重试、降并发、休眠），但它们散在 `retry.py` /
`concurrency.py` / `orchestrator` 的各个分支里，**没有任何地方能回答**：

* 现在有哪几条自动策略？各自开着吗？
* 它上一次触发是什么时候、看到了什么、做了什么？
* 某条策略没触发，是因为"没到条件"还是因为"被关掉了"？

第三种情况最糟：用户以为有保护，其实那条策略根本没开。所以这里的每条策略
都是**具名的**、带**理由**的、并且把"没触发的原因"也算出来。

## 四条设计规矩

1. **具名 + 可关 + 说得出理由**。`Policy` 里必须写清"为什么存在"与
   "不触发时怎么知道它没被关掉"（`switch` 字段直接进报告）。
2. **判据是纯函数**。`evaluate(observation) -> Decision` 不读库、不看时钟 ——
   时间和数据都由调用方喂进来。这样"该不该触发"能被单元测试逐条钉住，
   而不用起树、跑 runner。
3. **触发留痕**：`Decision` 带 `reason` 与 `evidence`（触发依据的数字）。
   只有动作没有依据的记录，事后无法复盘"当时它看到了什么"。
4. **失效方向偏保守**：拿不到数据时一律**不触发**（`fired=False` +
   `reason` 写明"数据不足"），而不是猜。少触发一次最多是少省一点；
   误触发会打断一次正常的长跑。

## 与既有机制的关系

本模块**不重复实现**动作：它只做"判断"，动作由调用方（orchestrator / watchdog）
执行既有的方法。这样自动策略不会变成第二套并发控制 ——
两套并发控制互相打架是这里最容易犯的错。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# 动作词表。**封闭集合**：新增动作必须显式加进来，于是"谁在执行什么"
# 是一份可枚举的东西，而不是散落在字符串里。
ACTIONS = (
    "stop_retrying",  # 不再重试这个节点，交给人
    "lower_concurrency",  # 降并发
    "hibernate",  # 整树休眠
    "reclaim_slots",  # 回收僵死槽位
)


@dataclass(frozen=True)
class Decision:
    """一条策略的判定结果。**无论触发与否都要给 `reason`**。"""

    fired: bool
    action: str | None
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Policy:
    """一条具名自动策略。

    `switch` 是**设置里的名字**，报告里原样列出 —— 用户要能看出"这条为什么没触发"。
    """

    id: str
    label: str
    rationale: str
    switch: str
    action: str
    evaluate: Callable[[Mapping[str, Any]], Decision]


def _enabled(observation: Mapping[str, Any], switch: str, default: bool = True) -> bool:
    """读开关。**缺键按默认值**（与 `Settings` 的默认一致），不看环境变量 ——

    环境变量在 `Settings` 构造时就固化进去了，这里再读一次会出现
    "报告说开着、实际关着"。开关的唯一真源是传进来的 observation。
    """
    value = observation.get(switch)
    if value is None:
        return default
    return bool(value)


def _unavailable(switch: str, why: str) -> Decision:
    return Decision(fired=False, action=None, reason=f"{why}（策略开关 {switch} 之前不判定）")


# ---------- 1. 连续失败自动停 ----------


# 同一个节点连续失败到这个次数就停止重试。与 `max_acceptance_repair_cycles`
# 是两件事：那个管"父节点整合验收返工几轮"，这个管"单个叶子反复跑不成"。
CONSECUTIVE_FAILURE_LIMIT = 3


def evaluate_consecutive_failure(observation: Mapping[str, Any]) -> Decision:
    """同一节点连续失败达上限 → **不再重试**，列入要人处理的清单。

    为什么不是"继续重试"：同一个节点用同样的提示词反复失败，几乎总是
    **信息缺失**（需求不清、环境缺东西），而不是运气不好。继续烧 token
    只会把失败次数变成噪声，把真正要人看一眼的信号埋掉。
    """
    switch = "auto_stop_on_consecutive_failure"
    if not _enabled(observation, switch):
        return _unavailable(switch, "策略已关闭")

    raw_limit = observation.get("auto_consecutive_failure_limit")
    limit = raw_limit if isinstance(raw_limit, int) and raw_limit > 0 else CONSECUTIVE_FAILURE_LIMIT

    streaks = observation.get("consecutive_failures")
    if not isinstance(streaks, Mapping):
        return _unavailable(switch, "拿不到连续失败次数")

    hit = sorted(
        (str(node), int(count))
        for node, count in streaks.items()
        if isinstance(count, int) and count >= limit
    )
    if not hit:
        worst = max((int(v) for v in streaks.values() if isinstance(v, int)), default=0)
        return Decision(
            fired=False,
            action=None,
            reason=f"没有节点连续失败到 {limit} 次",
            evidence={"limit": limit, "worst_streak": worst},
        )
    return Decision(
        fired=True,
        action="stop_retrying",
        reason=f"{len(hit)} 个节点连续失败达 {limit} 次，停止重试并交给人",
        evidence={"limit": limit, "nodes": [node for node, _ in hit]},
    )


# ---------- 2. 收益递减自动降并发 ----------


# 最近若干次运行里成功率为 0 时降并发。用"窗口"而不是全局成功率：
# 全局成功率在长跑里被历史稀释，跌到 0 也看不出来。
DIMINISHING_WINDOW = 10


def evaluate_diminishing_returns(observation: Mapping[str, Any]) -> Decision:
    """最近一个窗口内**一次都没成功** → 降并发。

    这个信号的含义是"当前并发下每个 agent 都在互相挤或者都在踩同一类错误"。
    加并发不会让成功率变高，只会让失败更快更多。
    """
    switch = "auto_lower_concurrency_on_diminishing_returns"
    if not _enabled(observation, switch):
        return _unavailable(switch, "策略已关闭")

    results = observation.get("recent_results")
    if not isinstance(results, (list, tuple)) or len(results) < DIMINISHING_WINDOW:
        return _unavailable(switch, f"最近结果不足 {DIMINISHING_WINDOW} 条")

    window = list(results)[-DIMINISHING_WINDOW:]
    successes = sum(1 for item in window if item in {"succeeded", "done"})
    if successes > 0:
        return Decision(
            fired=False,
            action=None,
            reason=f"最近 {DIMINISHING_WINDOW} 次里有 {successes} 次成功",
            evidence={"window": DIMINISHING_WINDOW, "successes": successes},
        )

    current = observation.get("concurrency")
    floor = observation.get("auto_concurrency_min")
    if isinstance(current, int) and isinstance(floor, int) and current <= floor:
        # 已经在最低档：再"降"就是停摆，那是另一条策略（休眠）的事。
        return Decision(
            fired=False,
            action=None,
            reason=f"并发已在最低档 {floor}，不再下调",
            evidence={"concurrency": current, "floor": floor},
        )
    return Decision(
        fired=True,
        action="lower_concurrency",
        reason=f"最近 {DIMINISHING_WINDOW} 次运行无一成功，下调并发",
        evidence={
            "window": DIMINISHING_WINDOW,
            "successes": 0,
            "concurrency": current,
            "floor": floor,
        },
    )


# ---------- 3. 空闲自动休眠 ----------


def evaluate_idle_hibernate(observation: Mapping[str, Any]) -> Decision:
    """没有在跑的 agent、没有排队、且闲置超过阈值 → 休眠。

    为什么不看"有没有节点没做完"：一棵树会长期停在"等人"的状态（阻塞债、
    等额度）。那种情况下没有工作在进行，继续占着槽位与内存没有意义。
    """
    switch = "auto_hibernate_when_idle"
    if not _enabled(observation, switch):
        return _unavailable(switch, "策略已关闭")

    active = observation.get("active_agents")
    queued = observation.get("queued_tasks")
    idle_seconds = observation.get("idle_seconds")
    threshold = observation.get("idle_hibernate_seconds")
    if not all(isinstance(value, int) for value in (active, queued, idle_seconds, threshold)):
        return _unavailable(switch, "拿不到活跃数 / 排队数 / 闲置时长")

    if active or queued:
        return Decision(
            fired=False,
            action=None,
            reason=f"还有活在跑（活跃 {active}、排队 {queued}）",
            evidence={"active_agents": active, "queued_tasks": queued},
        )
    if idle_seconds < threshold:
        return Decision(
            fired=False,
            action=None,
            reason=f"闲置 {idle_seconds}s，未到 {threshold}s",
            evidence={"idle_seconds": idle_seconds, "threshold": threshold},
        )
    return Decision(
        fired=True,
        action="hibernate",
        reason=f"无活跃与排队任务且闲置 {idle_seconds}s（≥ {threshold}s），休眠整树",
        evidence={"idle_seconds": idle_seconds, "threshold": threshold},
    )


# ---------- 4. 僵死槽位回收（本次新增） ----------


# 槽位被僵死进程占住超过这个时长才回收。为什么要等：正常长跑里
# "没有进度"持续几分钟是常态，急着回收会打断真在干活的 agent。
STALE_SLOT_SECONDS = 900


def evaluate_stale_slots(observation: Mapping[str, Any]) -> Decision:
    """有活进程却没有进度的槽位超过阈值 → 回收。

    **与"降并发"的区别**：降并发是"少发车"，这条是"把占着车位却熄火的车拖走"。
    少了它，几个僵死槽位会把并发额度吃光，而看板上显示的是"在跑"——
    这正是"看起来在跑其实早死了"那类事故。
    """
    switch = "auto_reclaim_stale_slots"
    if not _enabled(observation, switch):
        return _unavailable(switch, "策略已关闭")

    slots = observation.get("slots")
    if not isinstance(slots, (list, tuple)):
        return _unavailable(switch, "拿不到槽位信息")

    stale: list[str] = []
    for item in slots:
        if not isinstance(item, Mapping):
            continue
        no_progress = item.get("no_progress_seconds")
        has_process = item.get("has_process")
        if not isinstance(no_progress, int) or not isinstance(has_process, bool):
            continue
        # 只有"还有进程"时才回收：进程都没了属于另一条通路（启动时的 stale 清扫），
        # 两条通路混在一起会让"到底谁清理的"查不清。
        if has_process and no_progress >= STALE_SLOT_SECONDS:
            stale.append(str(item.get("task_id") or "?"))
    if not stale:
        return Decision(
            fired=False,
            action=None,
            reason=f"没有僵死槽位（阈值 {STALE_SLOT_SECONDS}s）",
            evidence={"threshold": STALE_SLOT_SECONDS, "slots": len(slots)},
        )
    return Decision(
        fired=True,
        action="reclaim_slots",
        reason=f"{len(stale)} 个槽位有活进程但超过 {STALE_SLOT_SECONDS}s 没有进度，回收",
        evidence={"threshold": STALE_SLOT_SECONDS, "task_ids": stale},
    )


POLICIES: tuple[Policy, ...] = (
    Policy(
        id="stop_on_consecutive_failure",
        label="连续失败自动停",
        rationale=(
            "同一个节点反复失败几乎总是信息缺失（需求不清、环境缺东西），"
            "不是运气不好。继续重试只会把信号埋掉。"
        ),
        switch="auto_stop_on_consecutive_failure",
        action="stop_retrying",
        evaluate=evaluate_consecutive_failure,
    ),
    Policy(
        id="lower_concurrency_on_diminishing_returns",
        label="收益递减自动降并发",
        rationale=(
            "一个窗口内一次都没成功，说明当前并发下 agent 在互相挤或都踩同一类错误；"
            "加并发只会让失败更快更多。"
        ),
        switch="auto_lower_concurrency_on_diminishing_returns",
        action="lower_concurrency",
        evaluate=evaluate_diminishing_returns,
    ),
    Policy(
        id="hibernate_when_idle",
        label="空闲自动休眠",
        rationale=(
            "树会长期停在'等人'的状态（阻塞债、等额度）。那时没有工作在进行，"
            "继续占着槽位与内存没有意义。"
        ),
        switch="auto_hibernate_when_idle",
        action="hibernate",
        evaluate=evaluate_idle_hibernate,
    ),
    Policy(
        id="reclaim_stale_slots",
        label="僵死槽位回收",
        rationale=(
            "有进程却没进度的槽位会把并发额度吃光，而看板上显示的是'在跑'。"
            "降并发是少发车，这条是把占着车位却熄火的车拖走。"
        ),
        switch="auto_reclaim_stale_slots",
        action="reclaim_slots",
        evaluate=evaluate_stale_slots,
    ),
)


def report(observation: Mapping[str, Any]) -> dict[str, Any]:
    """每条策略的当前判定。**包括没触发的**，且带上它为什么没触发。

    只报"触发了什么"的话，用户无法分辨"这条策略没开"与"这条策略开着但没到条件"
    —— 而前者意味着他以为有保护其实没有。
    """
    items: list[dict[str, Any]] = []
    for policy in POLICIES:
        decision = policy.evaluate(observation)
        items.append(
            {
                "id": policy.id,
                "label": policy.label,
                "rationale": policy.rationale,
                "switch": policy.switch,
                "enabled": _enabled(observation, policy.switch),
                "action": policy.action,
                "fired": decision.fired,
                "reason": decision.reason,
                "evidence": decision.evidence,
            }
        )
    fired = [item for item in items if item["fired"]]
    return {
        "policies": items,
        "fired_count": len(fired),
        "fired": [item["id"] for item in fired],
        # "关掉的策略"要单独列：这正是"以为有保护其实没有"的那种状态。
        "disabled": [item["id"] for item in items if not item["enabled"]],
        "note": (
            "本表只做判定，动作由调用方执行既有方法 —— 自动策略不会成为第二套并发控制。"
        ),
    }


def observation_from_settings(settings: Any, **overrides: Any) -> dict[str, Any]:
    """把 `Settings` 上的开关翻译成观察字段。

    每个开关都**显式映射**，不做 `getattr` 扫描：扫描会把"新增了设置但忘了接线"
    变成静默的默认值，而那种错在界面上看起来完全正常。
    """
    observation: dict[str, Any] = {
        "auto_stop_on_consecutive_failure": bool(
            getattr(settings, "auto_stop_on_consecutive_failure", True)
        ),
        "auto_consecutive_failure_limit": int(
            getattr(settings, "auto_consecutive_failure_limit", CONSECUTIVE_FAILURE_LIMIT)
            or CONSECUTIVE_FAILURE_LIMIT
        ),
        "auto_lower_concurrency_on_diminishing_returns": bool(
            getattr(settings, "auto_concurrency_enabled", True)
        ),
        "auto_hibernate_when_idle": bool(getattr(settings, "auto_hibernate_when_idle", True)),
        "auto_reclaim_stale_slots": bool(getattr(settings, "auto_reclaim_stale_slots", True)),
        "auto_concurrency_min": int(getattr(settings, "auto_concurrency_min", 1) or 1),
        "idle_hibernate_seconds": int(getattr(settings, "codex_idle_timeout_seconds", 1800) or 1800),
    }
    observation.update(overrides)
    return observation


def observe(tree: Mapping[str, Any], *, recent_statuses: list[str], settings: Any) -> dict[str, Any]:
    """从一份**已经取好的树快照**读出策略判定需要的现场。**纯计算，不查库。**

    为什么不让这里自己去查：
    - 下层模块不许反向依赖 `orchestrator`（`tests/test_module_layering.py` 的 C2 判据，
      实测这条约束在我第一版实现上交过学费：`auto_policy: ['models', 'orchestrator']`）；
    - 更重要的是"策略看到的是哪一份树"必须由调用方决定。它自己去查一次，就会和
      同时刻的看板/动作读到**两份**快照 —— 而策略的判定依据事后无法复盘。

    字段与来源都写明，因为"策略为什么没触发"必须能追到具体数字：

    * `consecutive_failures`：非终态节点的 `leaf_attempts` 与 `verification_failures`
      取大者。这两列是**既有**的重试计数（不需要新增列），一个记"跑了多少次"，
      一个记"验证否了多少次"。
    * `recent_results`：调用方给的运行时序，**最旧 → 最新**
      （`evaluate` 用 `[-WINDOW:]` 取最近一个窗口）。
    * `active_agents` / `queued_tasks`：树统计里的活跃数与排队数。
    * `idle_seconds`：拿不到"最后一次活动时间"时**不给这个键** ——
      那条策略会因此报"拿不到数据"而不是猜一个 0（失效方向偏保守）。
    * `slots`：活跃节点的 `no_progress_seconds`（由 `updated_at` 推算）与是否有活进程。
    """
    from datetime import datetime, timezone

    tasks = tree.get("tasks") or []
    stats = tree.get("stats") or {}

    streaks: dict[str, int] = {}
    for task in tasks:
        if str(task.get("status") or "") in {"done", "canceled"}:
            continue
        attempts = int(task.get("leaf_attempts") or 0)
        failures = int(task.get("verification_failures") or 0)
        streak = max(attempts, failures)
        if streak:
            streaks[str(task.get("id"))] = streak

    now = datetime.now(timezone.utc)
    slots = []
    for task in tasks:
        if str(task.get("status") or "") not in {"planning", "running", "working", "verifying", "accepting"}:
            continue
        updated = task.get("updated_at")
        try:
            stamp = updated if isinstance(updated, datetime) else datetime.fromisoformat(str(updated))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = int((now - stamp).total_seconds())
        except (TypeError, ValueError):
            continue
        slots.append(
            {
                "task_id": task.get("id"),
                "no_progress_seconds": max(0, age),
                # `has_process` 由调用方/编排层的进程扫描补全；这里**不猜**。
                "has_process": bool((task.get("node_meta") or {}).get("external_process_alive")),
            }
        )

    last_activity = None
    for task in tasks:
        stamp = task.get("updated_at")
        if stamp is None:
            continue
        try:
            parsed = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        if last_activity is None or parsed > last_activity:
            last_activity = parsed

    observation = observation_from_settings(
        settings,
        consecutive_failures=streaks,
        recent_results=list(recent_statuses),
        active_agents=int(stats.get("active_agents") or 0),
        queued_tasks=int(stats.get("queued") or 0) or _count_queued(tasks),
        slots=slots,
        concurrency=int(stats.get("active_agents") or 0) or 1,
    )
    if last_activity is not None:
        observation["idle_seconds"] = max(0, int((now - last_activity).total_seconds()))
    observation["root_id"] = tree.get("root_id")
    return observation


def _count_queued(tasks: list[Mapping[str, Any]]) -> int:
    return sum(1 for task in tasks if str(task.get("status") or "") in {"queued", "planned"})

