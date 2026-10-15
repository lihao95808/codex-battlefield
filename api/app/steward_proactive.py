"""管家**主动汇报**：不等用户问，在重要变化时自己开口。

## 为什么需要它

只会应答的东西不是"管家"，是个查询界面。真正有价值的是它在出事时**先开口** ——
树卡住了、看门狗停摆了、有动作等决定，这些都不该等用户想起来问。

## 但主动开口有个硬约束：不能打扰

这是本模块全部的复杂度所在。一个"有任何变化就说话"的管家会变成噪声源，
而用户对噪声的反应是**关掉它** —— 那时它连原本有用的那部分也失效了。

所以触发条件是**重要边沿**，不是"变化"：

| 条件 | 为什么算重要 |
|---|---|
| 出现 `action` 级提醒 | 唯一需要用户**动手**的一类 |
| 出现 `warning` 级提醒且上一次没有 | 状态在变坏，值得知道 |
| 提醒清空了（原本有 action/warning） | 用户可能在等"好了没" —— 这是好消息，要说 |

而下面这些**刻意不算**：
* `info` 级提醒变化（供参考的，不是要动手的）；
* 同一个 `kind` 等级没变只是数字变了（那是 `alerts_digest` 已经处理的边沿）；
* 提醒集合完全没变（每轮都说话就等于没说）。

## 与推送流的关系

推送（`/api/steward/alerts/stream`）负责把**原始信号**实时送到界面，很轻。
本模块负责在少数重要时刻请模型**解释一句**，很贵（一次真实模型调用）。
两者共用同一个 `steward_alerts.evaluate()`，所以不会出现"推送说有事、
管家说没事"这种分裂。

## 权限

主动汇报**只读**：它只说话，不执行。就算提了动作，也仍然走
"提议 -> 用户批准 -> 服务端执行"那条链路。所以这里不需要任何权限放开。
"""

from __future__ import annotations

# 只在这些等级的变化上开口。`info` 不触发 —— 那是"供参考"，不是"要动手"。
IMPORTANT_LEVELS = frozenset({"action", "warning"})

# 每次主动汇报之间的最小间隔（秒）。比推送的冷却长得多：推送是"界面上多一行"，
# 主动汇报是**一次真实模型调用 + 一条对话消息**，代价高得多。
DEFAULT_MIN_INTERVAL_SECONDS = 900


def important_levels(alerts: list[dict[str, str]]) -> set[str]:
    return {alert.get("level", "") for alert in alerts} & IMPORTANT_LEVELS


def should_speak(
    previous: list[dict[str, str]] | None,
    current: list[dict[str, str]],
    *,
    seconds_since_last: float | None,
    min_interval: float = DEFAULT_MIN_INTERVAL_SECONDS,
) -> tuple[bool, str]:
    """要不要主动开口？返回 `(要不要, 为什么)`。

    `previous is None` 表示**第一次评估**（进程刚起来）：那时不开口。
    否则每次重启 API 都会来一条"你好，现在的情况是…"，而用户完全没做什么 ——
    这是最容易把功能做成噪声的一种实现方式。
    """
    if previous is None:
        return False, ""

    if seconds_since_last is not None and seconds_since_last < min_interval:
        return False, ""

    before = important_levels(previous)
    after = important_levels(current)

    # 出现新的重要等级（或从无到有）
    appeared = after - before
    if appeared:
        names = "、".join(sorted(appeared))
        return True, f"出现新的重要提醒（{names} 级）"

    # 重要提醒全部消失：用户可能在等这个结果
    if before and not after:
        return True, "重要提醒已全部清空"

    # 重要等级没变，但种类变了（例如从"节点失败"变成"看门狗停摆"）
    before_kinds = {a.get("kind", "") for a in previous if a.get("level") in IMPORTANT_LEVELS}
    after_kinds = {a.get("kind", "") for a in current if a.get("level") in IMPORTANT_LEVELS}
    if after_kinds - before_kinds:
        names = "、".join(sorted(after_kinds - before_kinds))
        return True, f"出现新的重要状况（{names}）"
    if before_kinds - after_kinds:
        names = "、".join(sorted(before_kinds - after_kinds))
        return True, f"重要状况已解除（{names}）"

    return False, ""


class ProactiveEngine:
    """按轮评估、少数时候请模型说一句话。

    状态是**实例字段**而不是模块级全局：测试要能建多个互不干扰的实例，
    而模块级全局会让"上一次提醒"在两个测试之间串味。
    """

    def __init__(self, *, min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._previous: list[dict[str, str]] | None = None
        self._last_spoke_at: float | None = None

    def evaluate(self, alerts: list[dict[str, str]], now: float) -> tuple[bool, str]:
        """判断这一轮要不要开口，并**推进内部状态**。

        注意：无论开不开口，`_previous` 都要更新 —— 不然"上一次"永远停在
        第一次的样子，每一轮都会被判成"出现新提醒"。
        """
        seconds_since = None if self._last_spoke_at is None else (now - self._last_spoke_at)
        speak, reason = should_speak(
            self._previous,
            alerts,
            seconds_since_last=seconds_since,
            min_interval=self.min_interval_seconds,
        )
        self._previous = alerts
        if speak:
            self._last_spoke_at = now
        return speak, reason

    def seed(self, alerts: list[dict[str, str]]) -> None:
        """把"当前状态"记为基线，但不开口（进程启动时用）。"""
        self._previous = alerts
