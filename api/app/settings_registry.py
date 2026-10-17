"""可编辑设置的**登记表**（P2-2 运行时配置面板）。

## 为什么需要一份登记表，而不是把 103 个字段全摆出来

`Settings` 有 100+ 个字段，但它们的来源分成三类，**能改的只有一类**：

| 来源 | 例子 | 能不能在界面改 |
|---|---|---|
| `_env_or_config(env, key)` | `runner_mode`、`codex_sandbox_*`、`steward_proactive_enabled` | **能** —— 写进用户配置文件即生效（重启后） |
| 只读环境变量 `os.getenv(...)` | `max_concurrency`、`codex_bin` | 不能 —— 写了文件也没人读，会变成"我改了但没用" |
| 派生值 | `workspace_dir`、`layout` | 不该改 —— 它们是算出来的 |

把三类混在一起摆出来，用户会花时间改一个**根本不生效**的项。所以这里逐项登记，
并且**只放第二类之外的**（即真的会被读的那些）。

## 为什么每项都要标"要不要重启"

`Settings` 是 frozen dataclass，在进程启动时构造一次。所以**任何**改动都需要重启 API
才能被读到 —— 但"需要重启"和"重启就够了"是两件事：

* 改写用户配置文件的项：重启即生效；
* 只认环境变量的项：这里根本不提供。

标出来是为了让用户知道"我现在点保存，什么时候能看到效果"。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Literal

FieldKind = Literal["str", "int", "float", "bool"]


@dataclass(frozen=True)
class EditableSetting:
    """一项可编辑的设置。"""

    key: str  # 用户配置文件里的键名（= `_env_or_config` 的 config_key）
    env: str  # 对应的环境变量名（优先级更高，界面上要提示它会压过文件）
    label: str
    kind: FieldKind
    group: str
    help: str = ""
    # 危险项：改动会放宽权限或显著增加开销。界面要二次确认。
    risky: bool = False
    # 合法取值（仅 str 类型）。非空时界面用下拉而不是自由文本。
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


# 分组顺序就是界面上的显示顺序：先"日常会调"的，再"权限相关"的。
#
# 刻意**没有** `concurrency` 组：`max_concurrency` 走的是只读环境变量
# （`os.getenv`），写用户配置文件没人读。把它摆出来只会让人改了没用。
GROUPS: tuple[tuple[str, str], ...] = (
    ("runner", "运行模式"),
    ("model", "模型档位"),
    ("sandbox", "沙箱（权限）"),
    ("policy", "自动策略"),
    ("steward", "管家"),
)

SETTINGS: tuple[EditableSetting, ...] = (
    # ---------- 运行模式 ----------
    EditableSetting(
        key="runner_mode",
        env="CODEX_BATTLEFIELD_RUNNER",
        label="runner 模式",
        kind="str",
        group="runner",
        choices=("mock", "real"),
        help="mock = 不调模型（不需要 Codex CLI 与凭据）；real = 走真实 agent。",
    ),
    # ---------- 模型档位（按角色分） ----------
    *(
        EditableSetting(
            key=f"codex_model_{role}",
            env=f"CODEX_BATTLEFIELD_CODEX_MODEL_{role.upper()}",
            label=f"{role} 模型",
            kind="str",
            group="model",
            help="留空 = 回退全局档；全局也空则由隔离 config.toml 或 Codex CLI 默认决定。",
        )
        for role in ("constitution", "planner", "worker", "verifier", "acceptance", "steward")
    ),
    # ---------- 沙箱 ----------
    *(
        EditableSetting(
            key=f"codex_sandbox_{role}",
            env=f"CODEX_BATTLEFIELD_CODEX_SANDBOX_{role.upper()}",
            label=f"{role} 沙箱",
            kind="str",
            group="sandbox",
            choices=("", "read-only", "workspace-write", "danger-full-access"),
            help="留空 = 用 runner 的硬编码默认。danger-full-access 等于不加限制。",
            risky=True,
        )
        for role in ("constitution", "planner", "worker", "verifier", "acceptance", "steward")
    ),
    # ---------- 自动策略（P3-5）----------
    #
    # 这些开关的**实际效果**由 `app/auto_policy.py` 的判定函数决定；报告里会把
    # 开关状态与"为什么没触发"一起列出来 —— 这正是要能分辨"没开"与"没到条件"。
    EditableSetting(
        key="auto_stop_on_consecutive_failure",
        env="CODEX_BATTLEFIELD_AUTO_STOP_ON_CONSECUTIVE_FAILURE",
        label="连续失败自动停",
        kind="bool",
        group="policy",
        help=(
            "同一节点反复失败到上限就停止重试并交给人。同一个节点用同样的提示词反复失败"
            "几乎总是信息缺失（需求不清、环境缺东西），继续重试只会把信号埋掉。"
        ),
    ),
    EditableSetting(
        key="auto_consecutive_failure_limit",
        env="CODEX_BATTLEFIELD_AUTO_CONSECUTIVE_FAILURE_LIMIT",
        label="连续失败上限（次）",
        kind="int",
        group="policy",
        minimum=1,
        maximum=20,
        help="与上一条配套。默认 3；设得太低会把正常的返工误判成'注定失败'。",
    ),
    # P3-1 的第二道闸：树级累计 token 上限。**默认 0（关）** —— 它会在每次起 agent 前
    # 扫一遍这棵树的 run_records，那是真实开销；想封顶成本的人明确打开它。
    EditableSetting(
        key="budget_cost_ceiling",
        env="CODEX_BATTLEFIELD_BUDGET_COST_CEILING",
        label="树级金额上限",
        kind="float",
        group="policy",
        minimum=0.0,
        maximum=100000000.0,
        help=(
            "0 = 关闭。与 token 闸不同：金额只有端点自报，**必须**同时配置币种；"
            "读不到金额时不会暂停（那会变成误报），但会写一条事件说明为什么没生效。"
        ),
    ),
    EditableSetting(
        key="budget_cost_currency",
        env="CODEX_BATTLEFIELD_BUDGET_COST_CURRENCY",
        label="金额上限的币种",
        kind="str",
        group="policy",
        help="例如 CNY / USD。留空 = 金额闸不生效（跨币种相加没有意义）。",
    ),
    EditableSetting(
        key="budget_token_ceiling",
        env="CODEX_BATTLEFIELD_BUDGET_TOKEN_CEILING",
        label="树级 token 上限",
        kind="int",
        group="policy",
        minimum=0,
        maximum=2_000_000_000,
        help=(
            "0 = 关闭。大于 0 时，每次起 agent 前统计这棵树的累计 token；超了就整树休眠并写事件，"
            "只能由人放行（清守卫或预授权），不会自动把上限改大。"
        ),
    ),
    EditableSetting(
        key="auto_hibernate_when_idle",
        env="CODEX_BATTLEFIELD_AUTO_HIBERNATE_WHEN_IDLE",
        label="空闲自动休眠",
        kind="bool",
        group="policy",
        help=(
            "没有活跃与排队任务、且闲置超过超时阈值时挂起整树。树会长期停在'等人'的"
            "状态（阻塞债、等额度），那时占着槽位与内存没有意义。"
        ),
    ),
    EditableSetting(
        key="auto_reclaim_stale_slots",
        env="CODEX_BATTLEFIELD_AUTO_RECLAIM_STALE_SLOTS",
        label="僵死槽位回收",
        kind="bool",
        group="policy",
        help=(
            "有活进程却长时间没有进度的槽位会被回收。少了它，几个僵死槽位会把并发额度"
            "吃光，而看板上显示的是'在跑'。"
        ),
    ),
    EditableSetting(
        key="data_retention_days",
        env="CODEX_BATTLEFIELD_DATA_RETENTION_DAYS",
        label="数据保留（天）",
        kind="int",
        group="policy",
        minimum=0,
        maximum=365,
        help=(
            "超过这个天数的运行日志/提示词/完整检查点会被清理。**0 表示只留还原锚点**。"
            "任务树、宪法、事件、债务与每棵树最新的那份快照**永不清理**。"
        ),
    ),
    # ---------- 管家 ----------
    EditableSetting(
        key="steward_read_only",
        env="CODEX_BATTLEFIELD_STEWARD_READ_ONLY",
        label="管家只读",
        kind="bool",
        group="steward",
        help="开 = 只能看/说/建议；关 = 你批准后它提议的动作可以被执行。",
        risky=True,
    ),
    EditableSetting(
        key="steward_proactive_enabled",
        env="CODEX_BATTLEFIELD_STEWARD_PROACTIVE_ENABLED",
        label="管家主动汇报",
        kind="bool",
        group="steward",
        help="开 = 重要变化时它自己开口（会真的调用模型，按次花钱）。",
        risky=True,
    ),
    EditableSetting(
        key="steward_proactive_min_interval_seconds",
        env="CODEX_BATTLEFIELD_STEWARD_PROACTIVE_MIN_INTERVAL_SECONDS",
        label="主动汇报最小间隔（秒）",
        kind="float",
        group="steward",
        minimum=0,
        help="两次主动开口之间至少隔多久。默认 900（15 分钟）。",
    ),
    EditableSetting(
        key="steward_push_min_level",
        env="CODEX_BATTLEFIELD_STEWARD_PUSH_MIN_LEVEL",
        label="推送打扰阈值",
        kind="str",
        group="steward",
        choices=("info", "warning", "action"),
        help="只有达到这个等级的提醒才会推送。默认 action（唯一需要你动手的一类）。",
    ),
    EditableSetting(
        key="steward_push_throttle_seconds",
        env="CODEX_BATTLEFIELD_STEWARD_PUSH_THROTTLE_SECONDS",
        label="推送冷却（秒）",
        kind="float",
        group="steward",
        minimum=0,
        help="两次推送之间至少隔多久。默认 30。",
    ),
    EditableSetting(
        key="steward_stream_interval_seconds",
        env="CODEX_BATTLEFIELD_STEWARD_STREAM_INTERVAL_SECONDS",
        label="推送评估间隔（秒）",
        kind="float",
        group="steward",
        minimum=0.5,
        help="多久重新评估一次提醒。默认 5。",
    ),
)

BY_KEY: dict[str, EditableSetting] = {item.key: item for item in SETTINGS}


class SettingError(ValueError):
    """设置值不合法。调用方把它当 4xx 报给用户。"""


def catalog() -> list[dict[str, Any]]:
    """给界面的登记表（按 GROUPS 顺序）。

    排序按 GROUPS 而不是按声明顺序：界面上"日常会调的"该排前面，
    而声明顺序是照着代码分块写的，两者不是一回事。
    """
    by_group: dict[str, list[dict[str, Any]]] = {group: [] for group, _ in GROUPS}
    for item in SETTINGS:
        by_group.setdefault(item.group, []).append(
            {
                "key": item.key,
                "env": item.env,
                "label": item.label,
                "kind": item.kind,
                "group": item.group,
                "help": item.help,
                "risky": item.risky,
                "choices": list(item.choices),
                "minimum": item.minimum,
                "maximum": item.maximum,
            }
        )
    ordered: list[dict[str, Any]] = []
    for group, _ in GROUPS:
        ordered.extend(by_group.pop(group, []))
    # 兜底：登记表里出现了 GROUPS 没声明的分组时**不能悄悄丢掉** ——
    # 那会让界面上少一项，而用户看不出少了什么。
    for leftover in by_group.values():
        ordered.extend(leftover)
    return ordered


def group_labels() -> dict[str, str]:
    return {group: label for group, label in GROUPS}


def coerce(item: EditableSetting, raw: Any) -> Any:
    """把界面传来的值转成正确类型，并在不合法时**说清为什么**。

    为什么不让 pydantic 自己转：这些值要写进 TOML，而 TOML 里 `"1"` 与 `1` 是
    不同类型。类型错了不会立刻报错，而是变成一个"看起来改了、其实读不到"的配置 ——
    这类静默失败在本项目里踩过（布尔档位曾经读不到）。
    """
    if item.kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise SettingError(f"{item.label} 需要是布尔值（true/false），收到 {raw!r}")

    if item.kind in {"int", "float"}:
        try:
            value = float(raw) if item.kind == "float" else int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise SettingError(f"{item.label} 需要是一个数字，收到 {raw!r}") from exc
        if item.minimum is not None and value < item.minimum:
            raise SettingError(f"{item.label} 不能小于 {item.minimum}（收到 {value}）")
        if item.maximum is not None and value > item.maximum:
            raise SettingError(f"{item.label} 不能大于 {item.maximum}（收到 {value}）")
        return value

    text = "" if raw is None else str(raw).strip()
    if item.choices and text not in item.choices:
        allowed = "、".join(repr(choice) for choice in item.choices)
        raise SettingError(f"{item.label} 只能是 {allowed}，收到 {text!r}")
    return text


def restart_required() -> bool:
    """改动是否需要重启 API。

    **恒为真**，而且是刻意的：`Settings` 是 frozen dataclass，在启动时构造一次，
    之后所有读取（含后台任务）拿到的都是那一份快照。把它做成一个函数而不是
    写死在响应里，是为了让"什么时候会变成假"这件事有个明确的位置 ——
    哪天真做了热重载，改这里一处即可。
    """
    return True


def unknown_keys(keys: list[str]) -> list[str]:
    return [key for key in keys if key not in BY_KEY]


def resolve_current(settings: Any) -> dict[str, dict[str, Any]]:
    """每个可编辑项的**当前生效值**，以及它来自哪里。

    为什么要报"来源"而不是只报值：优先级是「环境变量 > 用户配置文件 > 内置默认」。
    如果一项是被环境变量压着的，那么在界面上改它**不会有效果** ——
    只显示值而不显示来源，用户就会掉进"我改了、保存了、重启了，怎么还是旧的"。

    这里**直接读 TOML**，而不是用 `read_config_value`：那个函数只认字符串
    （`isinstance(value, str)`），而 TOML 里的 `30` 解析出来是 `int` ——
    于是"文件里明明写了"会被报成 `default`。实测踩到过；
    布尔档位当初也栽在同一个坑里。

    返回 `{key: {value, source, env_override, file_value}}`，`source ∈ env|config|default`。
    """
    import tomllib

    from .state_layout import config_file

    try:
        file_data = tomllib.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        file_data = {}
    if not isinstance(file_data, dict):
        file_data = {}

    out: dict[str, dict[str, Any]] = {}
    for item in SETTINGS:
        env_value = os.environ.get(item.env, "").strip()
        file_raw = file_data.get(item.key)
        effective = getattr(settings, item.key, None)
        if env_value:
            source = "env"
        elif file_raw is not None:
            source = "config"
        else:
            source = "default"
        out[item.key] = {
            "value": effective,
            "source": source,
            # 环境变量压着时，界面要明确说"改这里没用"，而不是让用户白改一遍。
            "env_override": bool(env_value),
            "file_value": None if file_raw is None else str(file_raw),
        }
    return out
