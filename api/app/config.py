from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .state_layout import (
    StateLayout,
    config_file,
    load_layout,
    read_active_project,
    read_config_value,
    resolve_state_home,
)


ROOT_DIR = Path(__file__).resolve().parents[2]
# 老工作区路径。P2 之后它**不再是每工程状态的默认位置**，只保留两个用途：
#   1. 未绑工程时的兜底（保持"什么都没配也能跑"）
#   2. 迁移脚本识别旧状态的位置（`docs/spec-state-project-decoupling.md` §10）
#
# `CODEX_BATTLEFIELD_WORKSPACE` 可以把它整体挪走。**测试就靠这个保持洁净**：
# 测试会建真实的 git worktree（`git worktree add` 在仓库边上注册），
# 落在这个目录里就会往 checkout 里堆垃圾、还会留下悬空注册
# （实测：跑一次 verify_all 就多出 2 个 worktree 注册 + 2 个分支）。
_workspace_override = os.getenv("CODEX_BATTLEFIELD_WORKSPACE", "").strip()
WORKSPACE_DIR = Path(_workspace_override) if _workspace_override else (ROOT_DIR / "workspace")

# 显式传参的哨兵。为什么需要它：`workspace_dir` 的默认值必须**跟随布局**，
# 而布局依赖实例字段（`state_home`/`project_key`）——字段默认值在**类定义时**求值，
# 拿不到实例字段，所以只能在 `__post_init__` 里算。
# 用哨兵而不是 `None`：`None` 是"没给"与"给了 None"两种含义混在一起，容易误伤。
_UNSET = "__unset__"

# 有独立模型/推理档的角色。与 `RunKind` 同名；这里刻意用字面量而不是导入 `models`，
# 免得 config（最底层）反过来依赖 models。
CODEX_KINDS = ("constitution", "planner", "worker", "verifier", "acceptance", "steward")

# codex 认可的沙箱三档。`sandbox_for` 拒绝这三个以外的值（响亮失败，不静默退回默认）。
CODEX_SANDBOX_MODES = frozenset({"read-only", "workspace-write", "danger-full-access"})


def _env_or_config(env_name: str, config_key: str) -> str:
    """环境变量优先，其次用户配置文件，最后空串（= 用内置默认）。

    为什么要有这个统一入口（2026-10-15 补）：有些档位原先只认环境变量，
    而 API 常被**独立进程**拉起（看门狗、计划任务、GUI）—— 那些进程不继承
    `CODEX_BATTLEFIELD_*`，于是环境变量里的配置全部丢失，API 悄悄退回内置默认。
    这类失效不报错，只是"行为跟你在 shell 里试的不一样"，极难归因。

    放在 `machine_root_of` 之前是因为 `Settings` 的字段默认值在**类定义时**
    求值，而字段默认值会调用它 —— 定义必须早于类。
    """
    from_env = os.getenv(env_name, "").strip()
    if from_env:
        return from_env
    return (read_config_value(config_key) or "").strip()


def _env_truthy(env_name: str, config_key: str) -> bool:
    """布尔档位：环境变量优先，其次用户配置文件的 `true/false`，默认关。

    与 `_env_or_config` 分开是因为**布尔不能走字符串那条路**：
    `read_config_value` 只认字符串（`isinstance(value, str)`），
    而 TOML 里的 `true` 解析出来是 Python `bool` —— 会静默读不到，
    表现是"配置文件里明明开了，程序还是关的"。实测踩到过。

    真值表与仓库其它布尔档位一致（`1/true/yes/on` 为真）。
    """
    from_env = os.getenv(env_name, "").strip()
    if from_env:
        return from_env.lower() in {"1", "true", "yes", "on"}
    try:
        data = tomllib.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False
    value = data.get(config_key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _env_float_or_config(env_name: str, config_key: str, default: float) -> float:
    """浮点档位：环境变量优先，其次配置文件，最后默认值。

    TOML 的整数/浮点解析出来是 `int`/`float`，而 `read_config_value` 只认字符串 ——
    所以这里自己读 TOML，理由同 `_env_truthy`。
    """
    from_env = os.getenv(env_name, "").strip()
    if from_env:
        try:
            return float(from_env)
        except ValueError:
            return default
    try:
        data = tomllib.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return default
    value = data.get(config_key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _env_truthy_default_true(env_name: str, config_key: str) -> bool:
    """布尔档位，**默认真**：环境变量优先，其次配置文件，都没有则 `True`。

    为什么不能直接用 `_env_truthy`：那个是"默认关"的语义。`steward_read_only`
    的旧实现是 `os.getenv(..., "1").lower() not in {...}` —— 默认**开**。
    换成 `_env_truthy` 后，未配置时它返回 `False`，也就是**静默把只读关掉了**。
    实测就是这么把三个只读判据弄红的。而"管家默认只读"是用户有意选的权限取舍，
    默认值翻转属于安全问题，不是普通的默认值差异 —— 所以单独给一个函数，
    让这个"默认真"的意图留在类型和名字里，而不是散在条件表达式中。
    """
    from_env = os.getenv(env_name, "").strip()
    if from_env:
        return from_env.lower() in {"1", "true", "yes", "on"}
    try:
        data = tomllib.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return True
    value = data.get(config_key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return True


def machine_root_of(settings: object) -> Path:
    """取"机器级状态落点"，对**鸭子类型**的 settings 也成立。

    为什么需要它：生产用真 `Settings`（有 `machine_root`），但测试里有 8 个
    轻量 `_Settings` stub 只提供 `workspace_dir`。它们代表的是"最小可用的 settings"，
    加一个字段就要改 8 处、而且以后每加一个字段还要再改。所以这里做一次兜底：
    没有 `machine_root` 就按未绑定处理（= `workspace_dir`），语义完全一致。

    这也让"机器级状态落点"只有**一个**判定口径，不必在每个调用点重复三元表达式。
    """
    explicit = getattr(settings, "machine_root", None)
    if explicit is not None:
        return Path(explicit)
    return Path(getattr(settings, "workspace_dir", "."))


def machine_path_of(settings: object, name: str) -> Path:
    return machine_root_of(settings) / name


@dataclass(frozen=True)
class Settings:
    database_url: str = _UNSET
    # runner 模式也可以写进用户配置：环境变量只对当前 shell 生效，而 API 常被
    # GUI/计划任务拉起 —— 否则"我想用真实 runner"每次都得记着在启动命令里带上。
    # 优先级：环境变量 > 用户配置文件 > 内置默认（mock）。
    runner_mode: str = os.getenv("CODEX_BATTLEFIELD_RUNNER", "") or (read_config_value("runner_mode") or "mock")
    codex_bin: str = os.getenv("CODEX_BATTLEFIELD_CODEX_BIN", "codex")
    codex_home: str = os.getenv("CODEX_BATTLEFIELD_CODEX_HOME", "")
    codex_profile: str = os.getenv("CODEX_BATTLEFIELD_CODEX_PROFILE", "")
    codex_model: str = _env_or_config("CODEX_BATTLEFIELD_CODEX_MODEL", "codex_model")
    codex_reasoning_effort: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT", "")
    # 按角色分层（`docs/spec-verification-independence.md` §4）：`kind` 有独立档位时优先，空则回退全局。
    # 这是"用便宜模型当 worker/verifier、用强模型当 acceptance/constitution"的唯一入口——
    # 也是"flash 级模型能不能吃大工程"的前提之一。
    #
    # **也读用户配置文件**（P2-2 运行时配置面板）：否则界面上改了它、重启了也不生效，
    # 而"改了没用"是最容易让用户白忙一场的失效形态。
    codex_model_constitution: str = _env_or_config(
        "CODEX_BATTLEFIELD_CODEX_MODEL_CONSTITUTION", "codex_model_constitution"
    )
    codex_model_planner: str = _env_or_config(
        "CODEX_BATTLEFIELD_CODEX_MODEL_PLANNER", "codex_model_planner"
    )
    codex_model_worker: str = _env_or_config(
        "CODEX_BATTLEFIELD_CODEX_MODEL_WORKER", "codex_model_worker"
    )
    codex_model_verifier: str = _env_or_config(
        "CODEX_BATTLEFIELD_CODEX_MODEL_VERIFIER", "codex_model_verifier"
    )
    codex_model_acceptance: str = _env_or_config(
        "CODEX_BATTLEFIELD_CODEX_MODEL_ACCEPTANCE", "codex_model_acceptance"
    )
    # ---- 管家（steward）：常驻的全局对话 agent（`docs/spec-steward.md`）----
    # 专用档位。默认**跟 acceptance 同级**：管家做的是判断与沟通，值得用好模型；
    # 但它自己的档位独立可配，不至于为了管家把全局模型改掉。
    codex_model_steward: str = _env_or_config(
        "CODEX_BATTLEFIELD_CODEX_MODEL_STEWARD", "codex_model_steward"
    )
    codex_reasoning_effort_steward: str = os.getenv(
        "CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_STEWARD", ""
    )
    # 管家第一版**只读**：能看、能说、能建议，但不能动树。
    # 理由：它跑在与状态同权限的位置，“能改”就意味着能改坏状态；
    # 先让它证明判断可靠，再逐级放开（spec-steward §权限）。
    steward_read_only: bool = _env_truthy_default_true(
        "CODEX_BATTLEFIELD_STEWARD_READ_ONLY", "steward_read_only"
    )
    # ---- 管家的主动推送（`docs/spec-steward.md` §7）----
    # 推送是**拉取式 + 轮询评估**：不是每来一个事件就推，而是按这个间隔重新评估
    # 提醒集合。为什么不做成纯事件驱动：提醒的判定要读库、算快照，成本比一次比较高；
    # 而"有节点失败"这类信号本来就不需要毫秒级延迟。
    steward_stream_interval_seconds: float = _env_float_or_config(
        "CODEX_BATTLEFIELD_STEWARD_STREAM_INTERVAL_SECONDS",
        "steward_stream_interval_seconds",
        5.0,
    )
    # 只有达到这个等级的提醒才会被推送。默认 `action`（唯一需要用户动手的那种）：
    # 把参考信息也弹出来，会把真正要动手的那条淹掉。
    steward_push_min_level: str = _env_or_config(
        "CODEX_BATTLEFIELD_STEWARD_PUSH_MIN_LEVEL", "steward_push_min_level"
    ) or "action"
    # 两次推送之间的最小间隔。作用不是省流量，而是**防止用户对提醒脱敏**：
    # 频繁的提醒等于没有提醒。
    steward_push_throttle_seconds: float = _env_float_or_config(
        "CODEX_BATTLEFIELD_STEWARD_PUSH_THROTTLE_SECONDS",
        "steward_push_throttle_seconds",
        30.0,
    )
    # ---- 管家的主动汇报（`docs/spec-steward.md` §9）----
    # 开不开"不等用户问就自己开口"。默认**关**：
    # 它会真的调用模型、写进对话，属于会花钱、会打扰的能力 ——
    # 这种默认值应该由用户显式打开，而不是装完就开始。
    #
    # **也读用户配置文件**（否则 UI/看门狗拉起的 API 永远打开不了它 ——
    # 实测踩到：配置文件里写了 true，`get_settings()` 仍是 False，
    # 因为这里只认环境变量，而"只认环境变量"的设置在独立进程里必然丢失）。
    steward_proactive_enabled: bool = _env_truthy(
        "CODEX_BATTLEFIELD_STEWARD_PROACTIVE_ENABLED", "steward_proactive_enabled"
    )
    # 两次主动汇报之间的最小间隔（秒）。比推送冷却长得多：主动汇报是
    # 一次真实模型调用 + 一条对话消息，代价高一个数量级。
    steward_proactive_min_interval_seconds: float = _env_float_or_config(
        "CODEX_BATTLEFIELD_STEWARD_PROACTIVE_MIN_INTERVAL_SECONDS",
        "steward_proactive_min_interval_seconds",
        900.0,
    )
    codex_reasoning_effort_constitution: str = os.getenv(
        "CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_CONSTITUTION", ""
    )
    codex_reasoning_effort_planner: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_PLANNER", "")
    codex_reasoning_effort_worker: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_WORKER", "")
    codex_reasoning_effort_verifier: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_VERIFIER", "")
    codex_reasoning_effort_acceptance: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_ACCEPTANCE", "")
    # 按角色的沙箱档覆盖；空串 = 用 runner 里的硬编码默认（constitution/planner=read-only，
    # worker/verifier/acceptance=workspace-write）。存在的理由见 `Settings.sandbox_for`：
    # 有些主机上 codex 的 Windows 受限令牌建不起来，必须能显式降级到 danger-full-access。
    #
    # **也读用户配置文件**（与 `runner_mode` 同一个理由，2026-10-15 补）：
    # 这些档位原先只认环境变量，于是"看门狗重启 API"就丢了它们 —— 看门狗是
    # 独立进程、不继承 `CODEX_BATTLEFIELD_*`，它拉起的 API 会用回内置默认，
    # 于是在本机（受限令牌建不起来）上**整条真实执行链路失效**，而表现只是
    # 远端报一个 CreateProcessAsUserW 失败。运维需要在配置文件里一次性钉住它。
    codex_sandbox_constitution: str = _env_or_config("CODEX_BATTLEFIELD_CODEX_SANDBOX_CONSTITUTION", "codex_sandbox_constitution")
    codex_sandbox_planner: str = _env_or_config("CODEX_BATTLEFIELD_CODEX_SANDBOX_PLANNER", "codex_sandbox_planner")
    codex_sandbox_worker: str = _env_or_config("CODEX_BATTLEFIELD_CODEX_SANDBOX_WORKER", "codex_sandbox_worker")
    codex_sandbox_verifier: str = _env_or_config("CODEX_BATTLEFIELD_CODEX_SANDBOX_VERIFIER", "codex_sandbox_verifier")
    codex_sandbox_acceptance: str = _env_or_config("CODEX_BATTLEFIELD_CODEX_SANDBOX_ACCEPTANCE", "codex_sandbox_acceptance")
    codex_sandbox_steward: str = _env_or_config("CODEX_BATTLEFIELD_CODEX_SANDBOX_STEWARD", "codex_sandbox_steward")
    # 子进程环境**默认收窄**（`docs/spec-task-isolation.md` §2.4）：宿主上与本工具无关的
    # 平台凭据不该被顺手交给每个节点。设 0 回到"原样继承"（老行为，便于对照排查）。
    #
    # 要额外放行你自己网关的凭据前缀，用 `CODEX_BATTLEFIELD_CODEX_ENV_ALLOW_PREFIXES`，
    # 例如 `CODEX_,OPENAI_,MY_GATEWAY_`。
    codex_env_strict: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_ENV_STRICT", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    # 这里**只放行通用前缀**，不预置任何厂商名。
    #
    # 为什么要小心（`docs/provider-setup.md` §3.4）：`env_key` 指名的变量必须能下发，
    # 否则收窄就从"防泄露"变成"断通道"——表现是"节点全部 401 / 无输出"，
    # 而不是"凭据被过滤"，很容易查错方向。
    # 通用前缀不够用时有两个正规办法，按推荐顺序：
    #   1. 把 `env_key` 设成 `OPENAI_API_KEY` 这类已放行的名字（本地代理通常不看内容）；
    #   2. 用上面的环境变量加你自己的前缀。
    codex_env_allow_prefixes: str = os.getenv(
        "CODEX_BATTLEFIELD_CODEX_ENV_ALLOW_PREFIXES", "CODEX_,OPENAI_,AZURE_OPENAI_,ANTHROPIC_"
    )
    godot_bin: str = os.getenv("CODEX_BATTLEFIELD_GODOT_BIN", "")
    godot_timeout_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_GODOT_TIMEOUT_SECONDS", "45"))
    godot_max_concurrency: int = int(os.getenv("CODEX_BATTLEFIELD_GODOT_MAX_CONCURRENCY", "1"))
    # R1 的"静态优先级 + 老化"：槽位不够时谁先拿。OS 字节锁只保证互斥（容量绝不超过），
    # 优先级只能靠**应用层仲裁**：只有排名靠前的等待者去抢槽，其他人继续等。
    # 静态优先级按角色（acceptance/成品门禁 > verifier > worker > planner）；
    # 老化保证不饿死——每等 `godot_queue_aging_seconds` 秒，等效优先级 +1。设 0 = 关掉老化（纯优先级）。
    godot_queue_priority_enabled: bool = os.getenv("CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    godot_queue_aging_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS", "300"))
    codex_timeout_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_CODEX_TIMEOUT_SECONDS", "0"))
    codex_idle_timeout_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_CODEX_IDLE_TIMEOUT_SECONDS", "1800"))
    codex_retry_attempts: int = int(os.getenv("CODEX_BATTLEFIELD_CODEX_RETRY_ATTEMPTS", "2"))
    codex_retry_base_delay_seconds: float = float(os.getenv("CODEX_BATTLEFIELD_CODEX_RETRY_BASE_DELAY_SECONDS", "8"))
    # 进程内重试的最长等待。CLI 已经明说"X 分钟后重试"时，8s/16s 的指数退避是**白跑一次真实
    # Codex 启动**（额度不会等 8 秒就回来）。窗口不长就在原地等到点，比整棵树休眠划算；
    # 超过这个上限就放弃进程内重试，把窗口写进块报告交给上层休眠。设 0 表示一律不进程内重试。
    codex_retry_max_in_process_wait_seconds: int = int(
        os.getenv("CODEX_BATTLEFIELD_CODEX_RETRY_MAX_IN_PROCESS_WAIT_SECONDS", "300")
    )
    goal_mode: bool = os.getenv("CODEX_BATTLEFIELD_GOAL_MODE", "0").lower() in {"1", "true", "yes", "on"}
    agent_burst_limit: int = int(os.getenv("CODEX_BATTLEFIELD_AGENT_BURST_LIMIT", "0"))
    agent_burst_reserve: int = int(os.getenv("CODEX_BATTLEFIELD_AGENT_BURST_RESERVE", "0"))
    agent_burst_resume_after_seconds: int = int(
        os.getenv(
            "CODEX_BATTLEFIELD_AGENT_BURST_RESUME_AFTER_SECONDS",
            os.getenv("CODEX_BATTLEFIELD_RESOURCE_BLOCK_RESUME_AFTER_SECONDS", "18000"),
        )
    )
    auto_hibernate_on_resource_block: bool = os.getenv("CODEX_BATTLEFIELD_AUTO_HIBERNATE_ON_RESOURCE_BLOCK", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    resource_block_resume_after_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_RESOURCE_BLOCK_RESUME_AFTER_SECONDS", "18000"))
    watchdog_orphan_resume_after_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_WATCHDOG_ORPHAN_RESUME_AFTER_SECONDS", "300"))
    watchdog_interval_minutes: int = int(os.getenv("CODEX_BATTLEFIELD_WATCHDOG_INTERVAL_MINUTES", "1"))
    codex_ignore_rules: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_IGNORE_RULES", "1").lower() not in {"0", "false", "no"}
    codex_ignore_user_config: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    codex_disable_plugins: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_DISABLE_PLUGINS", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    codex_ephemeral: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_EPHEMERAL", "1").lower() not in {"0", "false", "no"}
    max_concurrency: int = int(os.getenv("CODEX_BATTLEFIELD_MAX_CONCURRENCY", "13"))
    auto_concurrency_enabled: bool = os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    auto_concurrency_min: int = int(os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MIN", "1"))
    auto_concurrency_max: int = int(
        os.getenv(
            "CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MAX",
            os.getenv("CODEX_BATTLEFIELD_MAX_CONCURRENCY", "13"),
        )
    )
    auto_concurrency_queue_threshold: int = int(os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_QUEUE_THRESHOLD", "6"))

    # ---- 自动策略（P3-5）----
    #
    # 三条新策略的开关。**默认全开**：它们的作用是"别把额度烧在注定失败的重复上"，
    # 关掉是主动选择（要显式改配置），而不是默认状态。
    #
    # 为什么必须能被配置文件读到（而不是只认环境变量）：设置面板要能列出来、
    # 看门狗那个独立进程也要能看到 —— 只认环境变量时，独立进程会退回默认值，
    # 表现是"面板里关了、守护进程照旧"，极难归因（`_env_or_config` 的注释里
    # 记着这类事故）。
    auto_stop_on_consecutive_failure: bool = _env_truthy_default_true(
        "CODEX_BATTLEFIELD_AUTO_STOP_ON_CONSECUTIVE_FAILURE", "auto_stop_on_consecutive_failure"
    )
    auto_hibernate_when_idle: bool = _env_truthy_default_true(
        "CODEX_BATTLEFIELD_AUTO_HIBERNATE_WHEN_IDLE", "auto_hibernate_when_idle"
    )
    auto_reclaim_stale_slots: bool = _env_truthy_default_true(
        "CODEX_BATTLEFIELD_AUTO_RECLAIM_STALE_SLOTS", "auto_reclaim_stale_slots"
    )
    # 同一个节点连续失败到这个次数就停止重试（P3-5）。0 = 用内置默认。
    auto_consecutive_failure_limit: int = int(
        _env_or_config(
            "CODEX_BATTLEFIELD_AUTO_CONSECUTIVE_FAILURE_LIMIT", "auto_consecutive_failure_limit"
        )
        or 3
    )

    # 数据保留窗口（天，P5-4）。默认为内置保守值 —— 见 `app/retention.py` 的模块文档：
    # 这一层的失效方向是"某天需要恢复时才发现恢复不了"，而少留的那一侧不可逆。
    data_retention_days: int = int(
        _env_or_config("CODEX_BATTLEFIELD_DATA_RETENTION_DAYS", "data_retention_days") or 0
    )
    auto_concurrency_resource_cooldown_seconds: int = int(
        os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_RESOURCE_COOLDOWN_SECONDS", "900")
    )
    auto_concurrency_fuel_deadline_hour: int = int(
        os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_FUEL_DEADLINE_HOUR", "20")
    )
    auto_concurrency_fuel_timezone_offset_hours: float = float(
        os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_FUEL_TIMEZONE_OFFSET_HOURS", "8")
    )
    auto_concurrency_fuel_sprint_window_minutes: int = int(
        os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_FUEL_SPRINT_WINDOW_MINUTES", "120")
    )
    machine_safety_min_available_memory_mb: int = int(
        os.getenv("CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB", "4096")
    )
    machine_safety_resume_after_seconds: int = int(
        os.getenv("CODEX_BATTLEFIELD_MACHINE_SAFETY_RESUME_AFTER_SECONDS", "1800")
    )
    scheduler_deferred_retry_seconds: float = float(os.getenv("CODEX_BATTLEFIELD_SCHEDULER_DEFERRED_RETRY_SECONDS", "8"))
    # 让 API 直接托管前端产物（`web/dist`）。
    #
    # 为什么要它：原先跑界面必须同时起 Vite dev server —— 两个进程、还要 Node。
    # 而"只想打开界面看看"是最常见的用法，尤其是把工具发给别人的时候。
    # 托管之后 `pnpm build` 一次，只起 API 就有完整界面（`start.ps1 -ApiOnly`）。
    #
    # 默认开；`web/dist` 不存在时**自动跳过**（不影响开发：dev 模式仍然走 Vite，
    # 而且 Vite 的 /api 代理指向本服务，两边互不干扰）。
    serve_web_dist: bool = os.getenv("CODEX_BATTLEFIELD_SERVE_WEB", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    # 前端产物目录。留空 = 自动找（`<repo>/web/dist`）。
    web_dist_dir: str = os.getenv("CODEX_BATTLEFIELD_WEB_DIST", "").strip()
    max_repeated_child_signature: int = int(os.getenv("CODEX_BATTLEFIELD_MAX_REPEATED_CHILD_SIGNATURE", "2"))
    # 父节点多轮 `repair` 之后进人工审查的上限（铁律 8）。**默认 2**（2026-09-22 决策，
    # 见 `docs/spec-modularization.md` §6 D1）：以前默认 0 = "不限"，那等于把这条保护默认关掉了。
    # 设 0 仍然表示"不限"，需要旧行为就显式设 0。
    max_acceptance_repair_cycles: int = int(os.getenv("CODEX_BATTLEFIELD_MAX_ACCEPTANCE_REPAIR_CYCLES", "2"))
    artifact_preview_chars: int = int(os.getenv("CODEX_BATTLEFIELD_ARTIFACT_PREVIEW_CHARS", "20000"))
    preview_url: str = os.getenv("CODEX_BATTLEFIELD_PREVIEW_URL", "http://127.0.0.1:5173/")
    open_preview_on_start: bool = os.getenv("CODEX_BATTLEFIELD_OPEN_PREVIEW_ON_START", "0").lower() in {"1", "true", "yes"}
    skill_lookup_enabled: bool = os.getenv("CODEX_BATTLEFIELD_SKILL_LOOKUP_ENABLED", "1").lower() not in {"0", "false", "no"}
    skill_km_url: str = os.getenv(
        "CODEX_BATTLEFIELD_SKILL_KM_URL",
        "https://skill-library.example/skill?skillId=00000000-0000-0000-0000-000000000000",
    )
    project_skill_enabled: bool = os.getenv("CODEX_BATTLEFIELD_PROJECT_SKILL_ENABLED", "1").lower() not in {"0", "false", "no"}
    project_skill_path: str = os.getenv(
        "CODEX_BATTLEFIELD_PROJECT_SKILL_PATH",
        str(ROOT_DIR / "skills" / "codex-battlefield-recursive" / "SKILL.md"),
    )
    project_skill_max_chars: int = int(os.getenv("CODEX_BATTLEFIELD_PROJECT_SKILL_MAX_CHARS", "12000"))
    # 集成冲突策略：overwrite（默认，记录并上浮，但后写仍覆盖）/ keep_both（冲突文件两份都留）/
    # repair（冲突阻断验收）。非法值由 integration.normalize_strategy 收敛到默认。
    integration_conflict_strategy: str = os.getenv("CODEX_BATTLEFIELD_INTEGRATION_CONFLICT_STRATEGY", "overwrite")
    # 上浮到验收 prompt 的冲突/跳过明细条数上限；超出只留计数并标 truncated。
    integration_conflict_detail_limit: int = int(
        os.getenv("CODEX_BATTLEFIELD_INTEGRATION_CONFLICT_DETAIL_LIMIT", "20")
    )
    # 债务门控连续拦下多少次后升级为人工阻塞。有界是必须的：
    # 见 docs/spec-parent-child-obligations.md §6.4 硬约束 2。
    obligation_gate_max_refusals: int = int(os.getenv("CODEX_BATTLEFIELD_OBLIGATION_GATE_MAX_REFUSALS", "2"))
    # R5 跨版本回归门禁：验收通过时记下"这一版的产物指纹"，**跨版本**再验收时比对上一版基线。
    # 缺失一律阻断；内容改变由档位决定（`record` 只记录并上浮，`repair` 才阻断——字节变化本身
    # 可能就是修复动作）。同一版内的正常返工不比对，避免门禁拦下自己的修复。
    integration_regression_enabled: bool = os.getenv("CODEX_BATTLEFIELD_INTEGRATION_REGRESSION", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    integration_regression_strategy: str = os.getenv("CODEX_BATTLEFIELD_INTEGRATION_REGRESSION_STRATEGY", "record")
    # R3 worktree 生命周期：创建串行化 + 终态回收。默认**保守**：开回收但保留 24 小时，
    # 且只有"终态（done/canceled）+ 声明产物已在归档处 + 过了保留期 + 在受管根下"才回收。
    # `failed` 一律不回收（失败现场是修复与复盘要读的证据）。保留 0 = 立即回收（腾盘/测试用）。
    worktree_recycle_enabled: bool = os.getenv("CODEX_BATTLEFIELD_WORKTREE_RECYCLE", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    worktree_retention_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_WORKTREE_RETENTION_SECONDS", "86400"))
    # 一次清扫最多回收几个：避免某一轮里一口气删掉一大批（删错了也留有余地）。
    worktree_recycle_limit_per_sweep: int = int(os.getenv("CODEX_BATTLEFIELD_WORKTREE_RECYCLE_LIMIT", "20"))
    # 宪法修订（收紧）后，用新宪法把"落后且可再验证"的已完成后代叶子重跑一次 verifier。
    # **只控制要不要花 token**，不控制严重度：关掉它版本债照旧阻断（人选了收紧却拒绝复查），
    # 最终走升级通路交给人，而不是被静默放行。收紧必须触发再验证（宪法 SPEC §7），所以默认开。
    constitution_revalidate_enabled: bool = os.getenv(
        "CODEX_BATTLEFIELD_CONSTITUTION_REVALIDATE_ENABLED", "1"
    ).lower() not in {"0", "false", "no"}
    # 约束覆盖缺口（§7.1.4）是否阻断父节点完成。默认**只记录并上浮**：
    # LLM 规划器会改写措辞，默认阻断会让每一棵树的父节点都永远拿不到 done（实测 14 条既有契约变红）。
    # 要把它当硬门槛，显式打开。
    constraint_coverage_gap_blocks: bool = os.getenv(
        "CODEX_BATTLEFIELD_CONSTRAINT_COVERAGE_GAP_BLOCKS", "0"
    ).lower() in {"1", "true", "yes"}
    parent_skill_note_task_enabled: bool = os.getenv("CODEX_BATTLEFIELD_PARENT_SKILL_NOTE_TASK_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    frozen_mode: bool = os.getenv("CODEX_BATTLEFIELD_FROZEN_MODE", "0").lower() in {"1", "true", "yes", "on"}
    full_checkpoint_enabled: bool = os.getenv("CODEX_BATTLEFIELD_FULL_CHECKPOINT_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    watchdog_state_snapshot_interval_seconds: int = int(
        os.getenv("CODEX_BATTLEFIELD_WATCHDOG_STATE_SNAPSHOT_INTERVAL_SECONDS", "300")
    )
    full_checkpoint_interval_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_FULL_CHECKPOINT_INTERVAL_SECONDS", "3600"))
    # 每个完整检查点都含一份数据库拷贝 + 目标工程拷贝，所以**保留份数直接乘上目标体积**。
    # 实测（一个中等 Godot 目标）：15 份 = 9.6 GB；而旧默认 48 份在长时间自主运行里足以撑爆磁盘，
    # 那正好是"跑着跑着停下来"的一类成因。恢复只用得到**最新**那一份，12 份足够回退若干小时。
    full_checkpoint_max_count: int = int(os.getenv("CODEX_BATTLEFIELD_FULL_CHECKPOINT_MAX_COUNT", "12"))
    checkpoint_ledger_enabled: bool = os.getenv("CODEX_BATTLEFIELD_CHECKPOINT_LEDGER_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    api_detached_real_scheduler: bool = os.getenv("CODEX_BATTLEFIELD_API_DETACHED_REAL_SCHEDULER", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    checkpoint_git_probe_timeout_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_CHECKPOINT_GIT_PROBE_TIMEOUT_SECONDS", "20"))
    mock_delay_seconds: float = float(os.getenv("CODEX_BATTLEFIELD_MOCK_DELAY", "0.25"))

    # ---- 状态与工程的解耦（`docs/spec-state-project-decoupling.md`）----
    #
    # 状态根：**不在仓库里**，所以仓库换位置/删掉重克隆都不影响状态（目标 G1）。
    # 覆盖：CODEX_BATTLEFIELD_STATE_HOME。
    state_home: Path = field(default_factory=resolve_state_home)
    # 当前工程 key（规范化绝对路径算出来的目录名）。空 = 还没挂载工程。
    project_key: str = os.getenv("CODEX_BATTLEFIELD_PROJECT_KEY", "")
    # 工程根的原始路径，仅用于显示与诊断（真相在 `active.json` 的 project_root）。
    project_root: Path | None = None

    # ⚠️ P2：默认值**跟随布局**（绑了工程就落到该工程目录），但**显式传入仍优先**
    # ——测试与脚本约 150 处依赖显式传参。实际解析在 `__post_init__`（见 `_UNSET` 注释）。
    workspace_dir: Path = _UNSET

    def __post_init__(self) -> None:
        """把"跟随布局"的两个默认值解析出来（`workspace_dir`、`database_url`）。

        为什么必须在这里而不是字段默认值：字段默认值在**类定义时**求值，
        拿不到实例字段（`state_home` / `project_key`），所以无法跟随绑定的工程。

        **显式传入一律优先**（哨兵机制），这是保住约 150 处测试/脚本显式传参的关键。
        """
        # 用 `load_layout` 而不是自己拼 StateLayout：当调用方没给 `project_key`/
        # `project_root` 时，它**必须**和 `get_settings()` 一样回退到 `active.json`。
        # 少了这个回退，直接 `Settings()` 的场景（脚本、测试、一次性调用）会看不到
        # 已绑定的工程，于是 `workspace_dir`/`database_url` 悄悄退回老仓库路径
        # ——实测踩到过：`/api/state` 报 bound=true，而 database_url 指向老库。
        layout = load_layout(
            state_home=self.state_home,
            project_key_override=self.project_key,
            project_root_override=self.project_root,
        )
        project_dir = layout.project_dir if layout.bound else WORKSPACE_DIR

        # 把解析出来的绑定**写回实例**，让 `settings.layout` 与 `workspace_dir`/
        # `database_url` 说的是同一件事。
        #
        # 不写回就会出现"派生的对了、属性说没绑"的分裂：`workspace_dir` 指向
        # `projects/<key>/`，而 `settings.layout.bound` 是 False——调用方拿不到
        # `runs_dir`/`worktrees_dir`，且 `project_dir` 会抛 NotBoundError。
        if layout.bound and not self.project_key:
            object.__setattr__(self, "project_key", layout.project_key)
            if self.project_root is None:
                object.__setattr__(self, "project_root", layout.project_root)

        if self.workspace_dir == _UNSET:
            object.__setattr__(self, "workspace_dir", project_dir)

        if self.database_url == _UNSET:
            url = os.getenv("CODEX_BATTLEFIELD_DATABASE_URL", "").strip()
            if not url:
                url = f"sqlite:///{(project_dir / 'battlefield.sqlite3').as_posix()}"
            object.__setattr__(self, "database_url", url)

    @property
    def machine_root(self) -> Path:
        """机器级（跨工程共享）状态文件的落点。

        * **绑定了工程** -> 状态根（`layout.state_home`）。这才是"机器级"该在的位置：
          预算闸门、codex 槽位预留、Godot 锁都按**机器**算；放仓库里会让每个
          checkout 各拿一套锁，等于没有互斥（spec D3 / §11-H1）。
        * **未绑定** -> `workspace_dir`（老位置）。

        为什么未绑定时不搬：所有测试都以"未绑定 + `workspace_dir=tmp_path`"跑，
        它们直接往那个目录写 `codex_health.json` 之类的文件。未绑定保持老语义
        是**完全向后兼容**的；绑定后的新语义才是新布局的目标形态。
        """
        if self.layout.bound:
            return self.layout.state_home
        return Path(self.workspace_dir)

    def machine_path(self, name: str) -> Path:
        return self.machine_root / name

    def model_for(self, kind: str) -> str:
        """按角色的模型档；空则回退全局 `codex_model`。`kind` 未知也回退——**不猜**。

        例外：**管家（steward）没单独配时回退到 acceptance 档**，而不是全局档。
        理由是用户的选择："管家跟 acceptance 同级"——它做的是判断与沟通，
        和验收一样值得用好模型；而独立可配是为了不必为了管家改全局。
        """
        if str(kind or "").strip().lower() == "steward":
            return (
                self._per_kind("steward", "codex_model")
                or self.codex_model_acceptance
                or self.codex_model
            )
        return self._per_kind(kind, "codex_model") or self.codex_model

    def reasoning_effort_for(self, kind: str) -> str:
        """按角色的推理档。管家同样回退到 acceptance 档（与模型档一致的口径）。"""
        if str(kind or "").strip().lower() == "steward":
            return (
                self._per_kind("steward", "codex_reasoning_effort")
                or self.codex_reasoning_effort_acceptance
                or self.codex_reasoning_effort
            )
        return self._per_kind(kind, "codex_reasoning_effort") or self.codex_reasoning_effort

    def sandbox_for(self, kind: str, default: str) -> str:
        """按角色的沙箱档；未配置时返回调用方给的默认值。

        **为什么要可配**（2026-10-14 实测）：codex 在 Windows 上的进程沙箱靠
        `CreateProcessAsUserW` 建立受限令牌。在某些进程链上（实测：DSH Desktop →
        uvicorn → codex 的孙进程）该调用被系统拒绝：

            Failed to create unified exec process:
            CreateProcessAsUserW failed: 5 (拒绝访问。)

        后果是 worker **既不能执行命令也不能写文件**——连 `python --version` 都跑不起来，
        最终只能如实上报环境阻塞。四种组合的实测结论：

        | sandbox            | profile 覆盖 unelevated | 结果 |
        |---|---|---|
        | `read-only`        | 是 | ❌ CreateProcessAsUserW 拒绝访问 |
        | `workspace-write`  | 是 | ❌ 同样 |
        | `danger-full-access` | 是 | ✅ 命令真的跑起来 |
        | `workspace-write`  | 否（全局 elevated） | ❌ helper_unknown_error |

        所以这不是"配置写错了"，而是**当前主机上受限令牌根本建不起来**。可配之后，
        在这种主机上可以显式降级到 `danger-full-access` 把链路跑通；在沙箱正常的主机上
        保持默认的严格档不受影响。

        **默认值不变**是本方法的设计前提：调用方传 `default`，没有覆盖时原样返回。
        合法取值只有 codex 认可的三档；非法值**响亮失败**，不静默退回默认——静默退回
        会让人以为配置生效了。
        """
        key = str(kind or "").strip().lower()
        override = ""
        if key in CODEX_KINDS:
            override = str(getattr(self, f"codex_sandbox_{key}", "") or "").strip()
        if not override:
            return default
        if override not in CODEX_SANDBOX_MODES:
            raise ValueError(
                f"codex_sandbox_{key}={override!r} 不是合法沙箱档；"
                f"只能是 {sorted(CODEX_SANDBOX_MODES)} 之一"
            )
        return override

    def _per_kind(self, kind: str, prefix: str) -> str:
        key = str(kind or "").strip().lower()
        if key not in CODEX_KINDS:
            return ""
        return str(getattr(self, f"{prefix}_{key}", "") or "").strip()

    @property
    def layout(self) -> StateLayout:
        """当前生效的状态布局。

        **纯计算**：不建目录、不读写文件。

        注意这里**刻意不回读 `active.json`**：`project_key` 是在 `get_settings()`
        时定下的**启动快照**（决策 D1）。运行期回读会让"进程还开着但用户切了工程"
        变成静默写错库——进程内的引擎/会话缓存仍指向旧库，而路径已经指向新库。
        要换工程就重启（`battlefield-project use` 会提示）。
        """
        return StateLayout(
            state_home=self.state_home,
            project_key=self.project_key,
            project_root=self.project_root,
        )

    def model_tier_warnings(self) -> list[str]:
        """分层配置的可疑之处。**只报不自动纠正**——静默改配置比报出来更糟。

        刻意**不**做的事：比较两个模型名谁更"强"。名字里没有强度信息，硬排会造出假的判据。
        能自动判的只有一种情形：**worker 配了独立档、acceptance 还在走全局档**——
        那正是"以为自己在分层其实没分"的典型，值得让人看一眼。
        """
        warnings: list[str] = []
        worker = self._per_kind("worker", "codex_model")
        acceptance = self._per_kind("acceptance", "codex_model")
        if worker and not acceptance:
            warnings.append(
                "worker 配了独立模型档，但 acceptance 仍在走全局档：请确认全局档不弱于 worker 档"
                "（模型强弱无法从名字判断，这里只报事实）"
            )
        if acceptance and not worker:
            warnings.append("acceptance 配了独立模型档、worker 走全局档：这是期望的配比（强验收 + 便宜执行）")
        return warnings


def get_settings() -> Settings:
    """进程启动时**唯一**做状态目录创建的地方（不变量 L2）。

    工程 key 在这里定成**启动快照**：显式环境变量优先，否则读一次 `active.json`。
    运行期不再回读（决策 D1，理由见 `Settings.layout`）。
    """
    # P1 阶段仍创建老工作区，**行为刻意不变**——P1 只引入抽象，
    # 让 StateLayout 可以被独立验证；切换实际路径是 P2 的事。
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / "runs").mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / "worktrees").mkdir(parents=True, exist_ok=True)

    settings = Settings()
    home = settings.state_home
    # 工程 key：显式环境变量优先；否则**读一次** `active.json` 定成启动快照。
    if not settings.project_key:
        active_key, active_root = read_active_project(home)
        if active_key:
            settings = replace(settings, project_key=active_key, project_root=active_root)
    home.mkdir(parents=True, exist_ok=True)
    settings.layout.tools_dir.mkdir(parents=True, exist_ok=True)
    # 绑定到工程时，把该工程的目录补齐。未绑定时**不建** `projects/`——
    # 不凭空造一个没人用的工程。
    if settings.layout.bound:
        for path in settings.layout.derived_dirs:
            path.mkdir(parents=True, exist_ok=True)
    return settings
