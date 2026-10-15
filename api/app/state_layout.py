"""状态布局：**工具状态**与**被改工程**的解耦。

规格：`docs/spec-state-project-decoupling.md`。

## 一句话

`StateLayout` 是**唯一构造路径的地方**，而且它是个**纯值对象**——
只算路径，不建目录、不读写文件。目录创建只发生在两处：
进程启动（`get_settings()`）与挂载工程（`ProjectStore.mount()`）。

## 为什么要有这一层

改造前 `settings.workspace_dir` 一个值同时当三件事：

| 角色 | 例子 | 归属 |
|---|---|---|
| **M** 机器级（跨工程共享） | 预算闸门、codex 槽位预留、看门狗状态、helper 脚本、Godot 锁 | 状态根 |
| **P** 每工程 | sqlite 树、`runs/`、状态快照、检查点 | `projects/<key>/` |
| **W** 工程材料 / agent 的 cwd | `worktrees/` | `projects/<key>/worktrees/` |

于是"换一个目标工程"会污染状态、"仓库挪个位置"会丢状态。

## 三条不变量（有测试守着，见 `tests/test_state_layout.py`）

1. `StateLayout` 的任何方法**不建目录、不读写文件**（纯函数）
2. 取 P/W 路径时若未绑定工程，必须**响亮失败**（`NotBoundError`），不得静默退回状态根
3. 工 project key 只由规范化绝对路径决定（`project_key()`，确定性 + 防碰撞）

## 已知取舍

工程标识用**规范化绝对路径**，所以同一工程换盘符会被判成新工程。
这是显式选择的代价，记录在 `spec` §4.2 / §13。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 老解释器兜底
    import tomli as tomllib  # type: ignore[no-redef]

# 安全字符集：`%` 必须在里面——`slug` 故意产出 `%XX`，且 `%` 在 Windows 文件名中合法。
# （第一版漏了 `%`，把所有含中文/空格的路径都误判成"不安全"，见 `scripts/project_key_reference.py`。）
SAFE_KEY = re.compile(r"^[A-Za-z0-9._%-]+$")

# 单段长度上限。
#
# 这个数字由**整条组合路径**反推，不是拍脑袋：最坏情况是
#   <状态根>/projects/<key>/state_snapshots/<root_id>/<时间戳>-<事件名>.json
# 后面的部分约 120 字符，Windows 默认整路径上限 260 —— 所以 key 必须留在
# **70 以内**，否则像 `<root>/runs` 这种短路径都会撞上 `[WinError 206] 文件名或扩展名太长`。
#
# 这条是实测出来的：用"完整路径 slug"当 key 时（临时目录下的工程可达 150+ 字符），
# `mkdir` 直接失败。可读性不能拿"能不能建目录"去换。
MAX_KEY_LEN = 70

PROJECTS_DIRNAME = "projects"
ACTIVE_FILENAME = "active.json"
REGISTRY_FILENAME = "registry.json"
TOOLS_DIRNAME = "tools"

# 机器级（跨工程共享）状态的固定文件名。**刻意做成常量**：
# 这些文件被多个进程读写（API、看门狗、kick 脚本），名字漂移会让"看门狗写的地方
# API 已经不看了"（`spec` §11-H4）。
MACHINE_SAFETY_FILENAME = "machine_safety.json"
GUARDIAN_STATE_FILENAME = "l4_guardian_state.json"
SLOT_RESERVATIONS_FILENAME = "codex_slot_reservations.json"
SLOT_RESERVATIONS_LOCK_FILENAME = "codex_slot_reservations.lock"
BUDGET_GUARD_FILENAME = "budget_guard.json"
CODEX_HEALTH_FILENAME = "codex_health.json"
WATCHDOG_LOG_FILENAME = "watchdog.log"
# 看门狗的**心跳 / 健康**（`docs/spec-watchdog-steward.md`）。
#
# 为什么需要它：看门狗的失败模式不是"报错"，而是**静默停摆** ——
# 计划任务被禁用、shell 崩了、脚本卡住。那时树在无人看管地跑，而没有任何地方
# 会报错。原先的观测手段只有 `watchdog.lock.json`（只在一次 pass 期间存在）
# 和 `watchdog.log` 的 mtime（API 不读），所以"看门狗还活着吗"根本无法回答。
#
# 心跳由看门狗**每次退出时**写（含失败路径），内容见
# `scripts/battlefield_watchdog.ps1` 的 `Write-WatchdogHeartbeat`。
WATCHDOG_STATUS_FILENAME = "watchdog_status.json"
GODOT_LOCK_FILENAME = "godot.global.lock"
GODOT_STATE_FILENAME = "godot.resource.state.json"


class NotBoundError(RuntimeError):
    """未绑定工程时访问每工程/工程材料路径。

    **刻意响亮**：静默退回状态根会让代码在状态目录里读写本该属于工程的产物，
    而这种错误不会立刻暴露——它会安静地把状态搞脏。
    """


def normalize_project_root(raw: str | Path) -> str:
    """把工程目录规范成用于比较的形态。

    只做与平台无关的规范化；**不做** `resolve()`——那会碰文件系统，
    而本函数要在"目录还不存在"时也能算出稳定结果。
    """
    text = str(raw).replace("/", "\\").rstrip("\\")
    return unicodedata.normalize("NFC", text)


def fold(text: str) -> str:
    """Windows 语义的大小写折叠。

    主体与哈希**必须**用同一个折叠后的串。第一版让哈希用 `lower()` 而主体保留
    原大小写，于是 `D:\\work\\g10` 与 `D:\\WORK\\G10` 主体不同、哈希相同 ——
    同一工程得到**两个目录**，正是"防碰撞"的反面。
    """
    return text.lower()


def slug(segment: str) -> str:
    """单段路径 → 文件系统安全串。ASCII 字母数字与 `._-` 原样，其余转 `%XX`。"""
    out: list[str] = []
    for ch in segment:
        if ch.isascii() and (ch.isalnum() or ch in "._-"):
            out.append(ch)
        else:
            out.append("%" + ch.encode("utf-8").hex().upper())
    return "".join(out) or "_"


def project_key(raw: str | Path) -> str:
    """规范化绝对路径 → 工程目录名。确定性、文件系统安全、防碰撞。

    **默认用"末段目录名 + 哈希"**（`g10--dc4eb916`），**不是**完整路径的 slug。

    为什么不用完整路径（2026-10-14 实测推翻的第一版）：完整 slug 会随路径深度线性变长，
    临时目录下的工程能到 150+ 字符。而 key 是**组合路径的一段**——
    `<状态根>/projects/<key>/state_snapshots/<root_id>/<文件名>` 很快就撞上
    Windows 默认 260 上限，表现是 `[WinError 206] 文件名或扩展名太长`，
    连 `<project>/runs` 这种短目录都建不出来。
    所以长度预算必须让给后面的路径，key 只保留"一眼看出是哪个工程"的能力。

    可读性由**末段目录名**提供（`g10`、`agent3`）；**防碰撞完全由哈希负责**
    （哈希取的是**完整**规范化路径，所以两个都叫 `g10` 的不同目录不会撞）。

    末段名本身过长时（少见）退到 `<盘符>--<哈希>`。
    """
    norm = normalize_project_root(raw)
    digest = hashlib.sha1(fold(norm).encode("utf-8")).hexdigest()[:8]

    parts = [p for p in norm.split("\\") if p]
    if norm.startswith("\\\\"):
        head, rest = "UNC", parts
    elif parts and parts[0].endswith(":"):
        head, rest = parts[0][:-1], parts[1:]
    else:
        head, rest = "ROOT", parts

    head_folded = fold(head)
    if rest:
        candidate = fold(f"{slug(rest[-1])}--{digest}")
        if len(candidate) <= MAX_KEY_LEN:
            return candidate

    return fold(f"{head_folded}--{digest}")


@dataclass(frozen=True)
class StateLayout:
    """纯值对象：状态根 + 当前工程。所有路径都由它算出来。"""

    state_home: Path
    project_key: str = ""
    project_root: Path | None = None

    # ---------- 机器级（M）：跨工程共享 ----------

    def machine_path(self, name: str) -> Path:
        return Path(self.state_home) / name

    @property
    def tools_dir(self) -> Path:
        """helper 脚本目录。

        **必须留机器级**：`tools/godot_with_timeout.py` 的锁/槽位回退路径是从
        `Path(__file__)` 推出来的，按工程复制会让 Godot 的主机级互斥被复制 N 份，
        实际并发变成 N×（`spec` §11-H1）。
        """
        return self.machine_path(TOOLS_DIRNAME)

    @property
    def active_file(self) -> Path:
        return self.machine_path(ACTIVE_FILENAME)

    @property
    def registry_file(self) -> Path:
        return self.machine_path(REGISTRY_FILENAME)

    @property
    def projects_dir(self) -> Path:
        return self.machine_path(PROJECTS_DIRNAME)

    # ---------- 每工程（P）/ 工程材料（W） ----------

    @property
    def bound(self) -> bool:
        return bool(self.project_key)

    def _require_bound(self) -> str:
        if not self.project_key:
            raise NotBoundError(
                "还没有绑定工程：每工程状态与工作树路径现在不可用。\n"
                "  原因：状态与工程已解耦，工具状态不再默认落到某个工程。\n"
                "  修：用 `battlefield-project use <工程目录>` 挂载并切换，"
                "或设 CODEX_BATTLEFIELD_PROJECT_KEY / CODEX_BATTLEFIELD_DATABASE_URL。"
            )
        return self.project_key

    @property
    def project_dir(self) -> Path:
        return self.projects_dir / self._require_bound()

    @property
    def project_manifest_file(self) -> Path:
        return self.project_dir / "project.json"

    @property
    def db_path(self) -> Path:
        return self.project_dir / "battlefield.sqlite3"

    @property
    def runs_dir(self) -> Path:
        return self.project_dir / "runs"

    @property
    def snapshots_dir(self) -> Path:
        return self.project_dir / "state_snapshots"

    @property
    def checkpoint_ledger_dir(self) -> Path:
        return self.project_dir / "checkpoint_ledger"

    @property
    def full_checkpoints_dir(self) -> Path:
        return self.project_dir / "full_checkpoints"

    @property
    def worktrees_dir(self) -> Path:
        """节点工作树根目录（**唯一构造点**，配合 `worktrees.worktree_path`）。"""
        return self.project_dir / "worktrees"

    @property
    def leases_dir(self) -> Path:
        return self.project_dir / "resource_leases"

    @property
    def derived_dirs(self) -> tuple[Path, ...]:
        """绑定工程后应当存在的目录。供 `ensure_dirs()` 用。"""
        return (
            self.project_dir,
            self.runs_dir,
            self.worktrees_dir,
            self.snapshots_dir,
            self.checkpoint_ledger_dir,
            self.full_checkpoints_dir,
            self.leases_dir,
        )

    # ---------- 诊断 ----------

    def describe(self) -> dict[str, object]:
        """给 `/api/state` 与启动日志用的自描述。**不碰文件系统。**"""
        info: dict[str, object] = {
            "state_home": str(self.state_home),
            "project_key": self.project_key,
            "project_root": str(self.project_root) if self.project_root else None,
            "bound": self.bound,
            "tools_dir": str(self.tools_dir),
        }
        if self.bound:
            info.update(
                {
                    "project_dir": str(self.project_dir),
                    "db_path": str(self.db_path),
                    "runs_dir": str(self.runs_dir),
                    "worktrees_dir": str(self.worktrees_dir),
                }
            )
        return info


# ---------- 状态根的引导读取（唯一允许碰文件系统的地方） ----------


def default_state_home() -> Path:
    """状态根的默认位置：`<用户主目录>/.codex-battlefield`。

    刻意**不放在仓库里**——仓库可以随便换位置、删掉重克隆，状态不受影响（目标 G1）。

    注意这里是**可移植**默认值，不写任何本机路径：想换位置用
    `set_state_home()` 写进 `~/.codex-battlefield.toml`，而不是改这个函数。
    """
    return Path(os.path.expanduser("~")) / ".codex-battlefield"


def config_file() -> Path:
    """用户级配置文件。**必须在状态根之外**，否则它会跟着状态根一起搬，读不到。"""
    override = os.getenv("CODEX_BATTLEFIELD_CONFIG", "").strip()
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".codex-battlefield.toml"


def read_state_home_config() -> Path | None:
    """从配置文件读 `state_home`。读不到返回 None；**存在但读不出来会出声**。

    为什么"坏文件不抛、但也不静默"：这是**引导阶段**，抛异常会让 API 连启动都做不到，
    而"配置写坏了"不该等于"工具报废"。但完全静默同样不行 ——
    实测踩过：写配置时忘了转义 Windows 路径里的反斜杠，TOML 非法，
    读端一声不响地退回默认状态根，表现是"搬完状态根后它还在用旧的"。
    所以：能降级就降级，但把原因写到 stderr。
    """
    path = config_file()
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        print(
            f"[codex-battlefield] 警告：配置文件读不出来，已退回默认状态根。\n"
            f"  文件：{path}\n"
            f"  原因：{exc}\n"
            f"  修：battlefield-project set-state-home <目录> 重新写一份。",
            file=sys.stderr,
        )
        return None
    value = data.get("state_home")
    if isinstance(value, str) and value.strip():
        return Path(os.path.expandvars(value.strip())).expanduser()
    return None


def _toml_value(value: object) -> str:
    """把值序列化成合法 TOML。

    **必须转义反斜杠**：Windows 路径里全是 `\\`，直接写成
    `state_home = "D:\\work\\x"` 是**非法 TOML**（`\\w` 不是合法转义），
    而读端会静默 `TOMLDecodeError -> None`，于是配置被忽略、悄悄退回默认值。
    实测踩过：搬完状态根后 `state show` 仍报旧位置 —— 就是这个。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    escaped = (
        text.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def read_config_value(key: str) -> str | None:
    """从用户配置读一个**字符串**键。读不到/不是字符串都返回 None。

    给"环境变量只对当前 shell 生效、而进程是被 GUI/计划任务拉起来的"这类设置用
    （例如 `runner_mode`）：写进配置才不用每次记着在启动命令里带。
    """
    try:
        data = tomllib.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def read_repo_root_config() -> Path | None:
    """从用户配置读 `repo_root`（这个工具装在哪）。读不到返回 None。

    为什么要落盘：`battlefield-project` 的包装器可能被放在 PATH 上的任意目录，
    它得知道去哪找真正的实现。靠"包装器同目录"就要求包装器留在仓库里，
    那没法"到处都能敲"；写进配置文件后，仓库搬走也只改一处。
    """
    try:
        data = tomllib.loads(config_file().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    value = data.get("repo_root")
    if isinstance(value, str) and value.strip():
        return Path(os.path.expandvars(value.strip())).expanduser()
    return None


def set_config_value(key: str, value: object) -> Path:
    """往用户配置写一个键（保留其余键）。"""
    config = config_file()
    try:
        existing = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    existing[key] = value
    lines = [
        "# Codex Battlefield 的用户级配置。",
        "# 由 `battlefield-project` 写入；也可以手改。",
        "# 优先级：环境变量 > 本文件 > 内置默认值。",
        "",
    ]
    for name in sorted(existing):
        lines.append(f"{name} = {_toml_value(existing[name])}")
    lines.append("")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines), encoding="utf-8")
    return config


def set_state_home(path: Path) -> Path:
    """把状态根写进用户级配置文件（持久，**GUI 启动也能生效**）。

    为什么需要它：`CODEX_BATTLEFIELD_STATE_HOME` 只对**当前 shell** 生效，
    而 API 常常是被 GUI/计划任务拉起来的，拿不到你 shell 里的环境变量。
    配置落盘之后，"状态根在哪"就不再依赖谁用什么方式启动进程。

    只改 `state_home` 这一项，其余配置原样保留（方便将来放更多设置）。
    """
    target = Path(path)
    set_config_value("state_home", str(target))
    return target


def resolve_state_home() -> Path:
    """状态根。优先级：环境变量 > 用户级配置文件 > 内置默认值。

    环境变量优先是**刻意的**：它让测试与一次性脚本能完全绕开用户配置
    （测试就靠这个隔离），而配置文件服务的是"这台机器平时用哪个"。
    """
    override = os.getenv("CODEX_BATTLEFIELD_STATE_HOME", "").strip()
    if override:
        return Path(override)
    configured = read_state_home_config()
    if configured is not None:
        return configured
    return default_state_home()


def read_active_project(state_home: Path) -> tuple[str, Path | None]:
    """读 `active.json`。**只读，不创建任何东西。**

    返回 `(project_key, project_root)`；读不到或坏了都返回 `("", None)`。

    为什么坏文件不抛：这里的语义是"当前没绑工程"，而这在 P1/P2 阶段是**正常状态**
    （还没挂载过）。真正的失败在读工 project 路径时由 `NotBoundError` 响亮给出。
    """
    active = Path(state_home) / ACTIVE_FILENAME
    try:
        raw = active.read_text(encoding="utf-8")
    except OSError:
        return "", None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return "", None
    if not isinstance(payload, dict):
        return "", None
    key = str(payload.get("project_key") or "")
    root_raw = payload.get("project_root")
    return key, (Path(str(root_raw)) if root_raw else None)


def load_layout(
    *,
    state_home: Path | None = None,
    project_key_override: str = "",
    project_root_override: Path | None = None,
) -> StateLayout:
    """构造当前生效的 `StateLayout`。**纯读**，不建目录。

    优先级：显式 override > `active.json`。
    """
    home = Path(state_home) if state_home is not None else resolve_state_home()
    if project_key_override:
        return StateLayout(state_home=home, project_key=project_key_override, project_root=project_root_override)
    key, root = read_active_project(home)
    return StateLayout(state_home=home, project_key=key, project_root=root)
