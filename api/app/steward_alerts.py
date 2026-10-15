"""管家的**主动提醒**：什么值得打扰用户，以及怎么不把用户烦死。

## 为什么判定放在服务端、而且用规则

管家是 LLM。让它定时醒来"看看有没有问题"既慢又贵，而且**不确定**：
同一个状况可能这次报、下次不报。规则能判的（有阻塞、有失败、有动作等决定）
就用规则判 —— 便宜、确定、可测。

管家在回答时本来就看得见这些信号（它们也进状态快照）；本模块是让
**界面**也能主动提示，而不必等用户先开口问。

## 拉取与推送共用同一份判定

`/api/steward/alerts`（拉取）和 `/api/steward/alerts/stream`（推送）
都调 `evaluate()`。分成两份实现迟早漂移，而漂移的后果是
"界面上看到的"和"推送来的"不是同一件事 —— 那种不一致极难排查。

## 节流：为什么必须**边沿触发**

如果只是"有问题就推"，那么一个持续阻塞的节点会每轮都推一次，
一分钟一次，用户很快就学会无视提醒 —— 那时提醒就等于没有。

所以：
* **边沿触发**：只在「提醒集合或等级」**发生变化**时推。
  一个一直存在、等级不变的提醒不会被重复推送。
* **冷却**：两次推送之间至少隔 `steward_push_throttle_seconds`。

`alerts_digest()` 就是那个"边沿"：它把当前提醒压成一个稳定的指纹，
流只需要比较前后指纹。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# 等级从重到轻。只用于**比较**严重程度（比如判断"是否值得推送"），
# 不用于排序 —— 界面上按评估顺序显示更可预测。
LEVEL_RANK = {"action": 3, "warning": 2, "info": 1}

# 默认只推送这个等级及以上的提醒。
#
# 默认只推 `action`（有动作等你决定）的理由：那是唯一一种**用户必须动手**
# 的信号。其余（树被截断、用量不可采样）是"供参考"，让它们也弹出通知
# 会把真正需要动手的那条淹掉 —— 而这条恰恰是最不该被淹的。
DEFAULT_PUSH_MIN_LEVEL = "action"


def level_rank(level: str) -> int:
    return LEVEL_RANK.get(level, 0)


def evaluate(db: Any, settings: Any) -> dict[str, Any]:
    """算出当前的提醒集合与计数。**唯一**的判定实现。

    返回 `{"alerts": [...], "counts": {...}}`，形状与 `/api/steward/alerts` 一致。
    """
    # 延迟导入：`steward_status` 会拉进 orchestrator 相关的一堆东西，
    # 而本模块要在很低的位置被 import（流、端点、测试都用它）。
    from .steward_actions import PENDING, list_actions
    from .steward_status import build_status

    snapshot = build_status(db, settings)
    counts = snapshot["counts"]
    alerts: list[dict[str, str]] = []

    blocked = snapshot["blocked_nodes"]
    if blocked:
        retryable = [item for item in blocked if item.get("retryable")]
        alerts.append(
            {
                "level": "warning" if retryable else "info",
                "kind": "blocked_nodes",
                "message": (
                    f"{len(blocked)} 个节点被阻塞"
                    + (f"（其中 {len(retryable)} 个可重试）" if retryable else "")
                    + f"，最早一个：{blocked[0]['title'][:60]}"
                ),
            }
        )
    if counts.get("failed"):
        alerts.append(
            {
                "level": "warning",
                "kind": "failed_nodes",
                "message": f"{counts['failed']} 个节点失败，值得看一眼原因。",
            }
        )
    if snapshot["outline_truncated"]:
        alerts.append(
            {
                "level": "info",
                "kind": "outline_truncated",
                "message": (
                    f"树很大（{counts['total']} 个节点），状态快照只列了前 "
                    f"{len(snapshot['outline'])} 个；管家看到的不是全貌。"
                ),
            }
        )
    if not snapshot["usage"]["available"]:
        alerts.append(
            {
                "level": "info",
                "kind": "usage_unavailable",
                "message": "还没有可采样的用量数据（不是用量为 0，是没东西可统计）。",
            }
        )

    pending = list_actions(db, status=PENDING)
    if pending:
        alerts.append(
            {
                "level": "action",
                "kind": "pending_actions",
                "message": f"管家提了 {len(pending)} 个动作等你决定。",
            }
        )

    alerts.extend(_watchdog_alerts(db, settings, snapshot))

    return {"alerts": alerts, "counts": counts}


def _watchdog_alerts(db: Any, settings: Any, snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """看门狗 / 资源治理那一路的信号（见 `docs/spec-watchdog-steward.md`）。

    为什么由管家来报：看门狗是**纯规则**的自动组件，它自己不会跟用户说话。
    它的判断必须有人转达，否则"机器撑不住了所以我休眠了"这件事对用户不可见 ——
    而用户只会看到"树不动了"，然后去查一堆错误的原因。

    分工不变：**L4 处理"机器撑不撑得住"，管家处理"这事该怎么办"。**
    这里只是把前者的结论读出来讲给用户听。
    """
    alerts: list[dict[str, str]] = []

    watchdog = snapshot.get("watchdog") or {}
    heartbeat = watchdog.get("heartbeat") or {}
    guardian = watchdog.get("guardian") or {}

    # 看门狗"该跑却没跑"是**最该让用户知道**的一种：它不是树的故障，
    # 而是保护机制本身失效了。树可能正在无人看管地跑。
    #
    # 但**只在有活可看时才提**：空树上没有依赖看门狗的东西，
    # 这时报"看门狗没在跑"是打扰（新装、闲置都会命中）。
    total = int((snapshot.get("counts") or {}).get("total") or 0)
    if heartbeat.get("stale") and total > 0:
        alerts.append(
            {
                "level": "action",
                "kind": "watchdog_stale",
                "message": (
                    "看门狗好像停摆了，而树里还有 "
                    f"{total} 个节点 —— 现在没有东西在自动续跑或兜底。"
                    f"（{heartbeat.get('stale_reason') or '心跳过期'}）"
                ),
            }
        )
    elif heartbeat.get("status") in {"failed", "error"}:
        alerts.append(
            {
                "level": "warning",
                "kind": "watchdog_failed",
                "message": (
                    "看门狗上一次跑**失败**了："
                    f"{heartbeat.get('reason') or heartbeat.get('status')}。"
                ),
            }
        )

    if watchdog.get("hibernating"):
        alerts.append(
            {
                "level": "warning",
                "kind": "watchdog_hibernating",
                "message": (
                    "树正在休眠（资源或额度闸门）："
                    f"{guardian.get('summary') or watchdog.get('reason') or '原因未记录'}。"
                ),
            }
        )

    # 计划任务没装 = 看门狗根本不会被拉起来。这是一次性的安装问题，
    # 但用户不会自己发现 —— 界面上"看门狗"那一栏空着很容易被忽略。
    if not heartbeat.get("available") and total > 0:
        alerts.append(
            {
                "level": "info",
                "kind": "watchdog_no_heartbeat",
                "message": (
                    "还没收到过看门狗的心跳，而树里有节点。"
                    "如果是刚装好还没跑，忽略这条；否则看门狗可能没注册成计划任务。"
                ),
            }
        )

    return alerts


def alerts_digest(alerts: list[dict[str, str]]) -> str:
    """把提醒集合压成一个**稳定指纹**，用于边沿触发。

    只取 `kind` 与 `level`，**不取 message**：
    消息里带数字（"3 个节点失败"变成"4 个节点失败"）会让指纹每轮都变，
    于是节流失效、提醒退化成轮询。等级或种类变了才算真的"新情况"。
    """
    pairs = sorted((alert.get("kind", ""), alert.get("level", "")) for alert in alerts)
    blob = json.dumps(pairs, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def worth_pushing(alerts: list[dict[str, str]], min_level: str) -> list[dict[str, str]]:
    """挑出达到打扰阈值的提醒。"""
    floor = level_rank(min_level)
    return [alert for alert in alerts if level_rank(alert.get("level", "")) >= floor]
