from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import (
    action_risk,
    attention as attention_mod,
    auto_policy,
    conclusions as conclusions_mod,
    cost as cost_mod,
    cross_project,
    delivery as delivery_mod,
    watchdog_tuning as watchdog_tuning_mod,
    diagnostics as diagnostics_mod,
    project_store,
    migration as migration_mod,
    state_admin,
    settings_registry,
    obligation_index,
    steward,
    steward_actions,
    steward_proactive,
)
from . import steward_alerts as steward_alerts_mod
from .config import ROOT_DIR, machine_path_of
from .database import SessionLocal, get_db, init_db
from .events import event_bus
from .models import ConstitutionRecord, StewardAction, TaskNode, TaskStatus
from .orchestrator import orchestrator
from .runner import make_runner
from .project_store import (
    ProjectError,
    current_project,
    list_projects,
    mount,
    validate_project_root,
    worktree_drive_matches,
)
from .state_layout import config_file, read_active_project, set_config_value
from .steward_capabilities import catalog as steward_catalog
from .steward_status import build_status as build_steward_status
from .schemas import (
    MigrateStateRequest,
    ResetProjectRequest,
    SettingsUpdateRequest,
    BindProjectRequest,
    ApiRestartDrainResponse,
    BudgetGuardRequest,
    BudgetGuardResponse,
    ConstitutionAmendRequest,
    ConstitutionDraftRequest,
    ConstitutionResponse,
    ConstitutionUpdateRequest,
    HibernateRequest,
    ObligationIndexBoard,
    ObligationSettleRequest,
    OneShotProjectRequest,
    ProjectStartRequest,
    ProjectStartResponse,
    RunArtifactResponse,
    TaskDetailResponse,
    TreeResponse,
    WatchdogActionResponse,
    WatchdogStateResponse,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    if not orchestrator.settings.frozen_mode:
        orchestrator.recover_stale_agent_statuses()
        orchestrator.schedule_hibernation_wakeups()
    proactive_task = asyncio.create_task(_steward_proactive_loop())
    try:
        yield
    finally:
        proactive_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await proactive_task


# 主动汇报的评估间隔（秒）。比推送流的间隔**长得多**：推送只是"界面上多一行"，
# 而主动汇报要真的问一次模型、写一条对话消息 —— 代价高一个数量级。
PROACTIVE_INTERVAL_SECONDS = 60
# 进程内两个请求间的模型调用冷却，与 `steward_proactive.DEFAULT_MIN_INTERVAL_SECONDS` 对齐。
# 放在这里是因为它同时受配置控制（`steward_proactive_min_interval_seconds`）。
_proactive_engine = steward_proactive.ProactiveEngine()


async def _steward_proactive_loop() -> None:
    """让管家在**重要变化**时自己开口（`docs/spec-steward.md` §9）。

    为什么是一个独立的后台任务、而不是挂在 SSE 流里：流只在**有人打开界面**时存在。
    挂在流里等于"你盯着它它才主动"，那还是查询界面。
    独立任务 + 状态落库，才是"打开界面就看到它已经说过了"。

    为什么第一轮**不开口**：进程刚起来时 `previous is None`，`should_speak`
    直接返回 False。否则每次重启 API 都会来一句"你好，现在的情况是…"，
    而用户什么都没做 —— 这是最容易把主动功能做成噪声的方式。
    """
    settings = orchestrator.settings
    engine = _proactive_engine
    # 把配置的冷却**真的接到引擎上**。原先这里只是算出来赋值、没传进去，
    # 于是 `steward_proactive_min_interval_seconds` 完全不起作用
    # （引擎一直用模块默认的 900s）—— ruff 的"未使用变量"抓到了它。
    engine.min_interval_seconds = float(settings.steward_proactive_min_interval_seconds)

    # 先把当前状态记为基线：进程启动时不该立刻说话。
    try:
        db = SessionLocal()
        try:
            seed = steward_alerts_mod.evaluate(db, settings)
            engine.seed(seed["alerts"])
        finally:
            db.close()
    except Exception:  # noqa: BLE001 - 基线取不到不该让 API 起不来
        pass

    while True:
        await asyncio.sleep(PROACTIVE_INTERVAL_SECONDS)
        if not settings.steward_proactive_enabled:
            continue
        try:
            db = SessionLocal()
            try:
                # 同步的读库 + 可能的模型调用，都丢到线程池外面去：
                # 在事件循环里直接跑会阻塞整个 API。
                payload = await asyncio.to_thread(steward_alerts_mod.evaluate, db, settings)
                speak, reason = engine.evaluate(payload["alerts"], time.monotonic())
                if not speak:
                    continue
                instruction = steward.PROACTIVE_INSTRUCTION.format(trigger=reason)
                await _run_steward_turn(
                    db,
                    instruction,
                    extra_metadata={"autonomous": True, "trigger": reason},
                    record_user_message=False,
                )
            finally:
                db.close()
        except Exception:  # noqa: BLE001 - 主动汇报失败绝不能拖垮 API
            # 静默是**故意**的：这是后台循环，抛出去只会变成未处理的任务异常，
            # 而它每 60 秒就会再试一次。但记一行到日志，否则"它为什么从没说过话"
            # 会变成无法排查的问题。
            print("[steward] 主动汇报这一轮失败，下一轮重试。", file=sys.stderr)


app = FastAPI(title="Codex 任务战场 API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


IMAGE_PREVIEW_EXTENSIONS = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _preview_roots(db: Session) -> list[Path]:
    roots = [ROOT_DIR, orchestrator.settings.workspace_dir]
    for row in db.query(ConstitutionRecord.target_path).filter(ConstitutionRecord.target_path.isnot(None)).all():
        try:
            target = Path(str(row[0])).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        roots.append(target)
    resolved: list[Path] = []
    for root in roots:
        try:
            root_path = Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        if root_path not in resolved:
            resolved.append(root_path)
    return resolved


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "frozen_mode": str(orchestrator.settings.frozen_mode).lower()}


@app.get("/api/state")
def state_layout() -> dict:
    """当前状态布局自描述——回答"我现在在哪个工程、用哪个库、状态根在哪"（目标 G6）。

    没有这个端点时，最危险的错误形态是"看着是这个工程、其实在改另一个"：
    路径都是对的、进程也不报错，只有库里的内容不对。所以这里把**进程实际用的值**
    暴露出来，而不是让调用方自己去拼。

    信息全部来自启动快照（决策 D1），**不回读 `active.json`**：显示的必须是
    这个进程真正在用的东西，不是"现在磁盘上写着什么"。
    """
    settings = orchestrator.settings
    layout = settings.layout
    info = layout.describe()
    info["database_url"] = settings.database_url
    info["runner_mode"] = settings.runner_mode
    # 快照与磁盘现状是否一致：用户切了工程但没重启时，这里会是 false。
    # 明确报出来，而不是让"我切了怎么没生效"变成一个谜。
    active_key, active_root = read_active_project(layout.state_home)
    info["active_file_key"] = active_key
    info["active_file_root"] = str(active_root) if active_root else None
    info["snapshot_matches_active_file"] = (active_key == layout.project_key)
    return info


def _mounted_payload(project: Any, *, active_key: str) -> dict:
    return {
        "key": project.key,
        "root": str(project.root),
        "project_dir": str(project.project_dir),
        "db_path": str(project.db_path),
        "db_exists": bool(project.db_exists),
        "db_size_bytes": project.db_size_bytes(),
        "root_exists": Path(project.root).is_dir(),
        "active": project.key == active_key,
        # 状态目录与工程**不同盘**时 git worktree 不可靠（`worktree_drive_matches` 的判据）。
        # 界面要能提前提醒，而不是等用户跑到一半才发现。
        "drive_matches": worktree_drive_matches(project.project_dir, project.root),
    }


@app.get("/api/projects")
def list_mounted_projects() -> dict:
    """已挂载的工程清单 + 当前是哪个。

    为什么界面需要它：未绑定工程时，新用户看到的是空状态而不知道下一步；
    绑定之后又要能确认"我现在在哪个工程"。这两件事都需要一份**可读的清单**，
    而不是让用户去敲 `battlefield-project list`。
    """
    settings = orchestrator.settings
    state_home = settings.layout.state_home
    active = current_project(state_home=state_home)
    active_key = active.key if active else ""
    projects = list_projects(state_home=state_home)
    return {
        "state_home": str(state_home),
        "active_key": active_key,
        "projects": [_mounted_payload(p, active_key=active_key) for p in projects],
    }


@app.post("/api/projects/bind")
def bind_project(payload: BindProjectRequest) -> dict:
    """绑定（挂载）一个工程目录并设为当前。

    与 CLI 的 `battlefield-project use` 走**同一个** `project_store.mount()`：
    分成两份实现迟早漂移，而漂移的后果是"界面绑的工程 CLI 看不到"。
    """
    _ensure_mutable()
    # 先做**结构性**校验（绝对路径 / 盘符根 / 状态根），再查存在性。
    #
    # 顺序有讲究：相对路径即使**碰巧存在**也该被拒，而它一旦先撞上"目录不存在"
    # 就会拿到一条误导的错误（实测：`some/relative/dir` 报的是"不存在"）。
    # 先报结构性原因，用户才知道自己真正要改什么。
    try:
        validate_project_root(payload.path)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # **界面绑定要求目录真的存在**（CLI 不同：它允许"先绑、以后再建"）。
    #
    # 差别是有意的：CLI 的调用者知道自己打的是什么；而界面是输入/点选，
    # 打错一个字母就会静默绑到一个永远跑不起来的工程 —— 实测里
    # `Z:\definitely-not-here` 当时是**成功挂载**的，用户只会看到"绑定成功"。
    if not Path(payload.path.strip()).is_dir():
        raise HTTPException(
            status_code=400,
            detail=(
                f"这个目录不存在（或不是一个目录）：{payload.path.strip()}\n"
                "  界面绑定要求目录已存在。想先绑一个还没建的目录，用 CLI："
                "battlefield-project use <目录>"
            ),
        )
    try:
        project = mount(payload.path, state_home=orchestrator.settings.layout.state_home)
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 绑定写的是 active.json；本进程的库连接和 cwd 都是启动时定下的，
    # **不会**因此改变。这一点必须说清楚，否则用户会以为"点了就切好了"。
    needs_restart = project.key != orchestrator.settings.layout.project_key
    project_payload = _mounted_payload(project, active_key=project.key)
    warnings: list[str] = []
    if needs_restart:
        warnings.append("当前 API 仍连着启动时那个库，**重启 API 后生效**。")
    if not project_payload["drive_matches"]:
        # 跨盘时 `git worktree` 有已知兼容问题（见 `worktree_drive_matches`）。
        # 这是"能绑但会出错"的情形，必须提前讲，而不是等跑到一半才炸。
        warnings.append(
            "状态目录与工程**不在同一块盘**：git worktree 在跨盘时不可靠，"
            "建议把状态根换到工程所在盘。"
        )
    return {
        "status": "mounted",
        "project": project_payload,
        "restart_required": needs_restart,
        "warnings": warnings,
        "message": "已绑定。" + ("".join(warnings) if warnings else "API 本来就连着它。"),
    }


@app.get("/api/settings")
def get_settings_catalog() -> dict:
    """可编辑设置的登记表 + **当前生效值** + 每项来自哪里（P2-2）。

    为什么报"来源"而不只报值：优先级是「环境变量 > 用户配置文件 > 内置默认」。
    一项若被环境变量压着，在界面上改它**不会有任何效果** —— 不把这件事说出来，
    用户就会掉进"我改了、保存了、重启了，怎么还是旧的"。
    """
    settings = orchestrator.settings
    return {
        "groups": settings_registry.group_labels(),
        "order": [group for group, _ in settings_registry.GROUPS],
        "fields": settings_registry.catalog(),
        "current": settings_registry.resolve_current(settings),
        "restart_required": settings_registry.restart_required(),
        "config_file": str(config_file()),
    }


@app.post("/api/settings")
def update_settings(payload: SettingsUpdateRequest) -> dict:
    """把可编辑设置写进**用户配置文件**（P2-2）。

    三条硬规矩：

    1. **不在登记表里的键直接拒绝**，不静默忽略 —— 静默会让用户以为改成功了。
    2. **危险项要二次确认**（`confirm_risky`）：放宽沙箱或打开主动汇报都是
       "点一下下拉框就生效"的动作，事后很难归因。
    3. 先**全部**校验再落盘：一项非法就整批不写。分批写会留下
       "一半改了、一半没改"的状态，而用户只看到一条错误。
    """
    _ensure_mutable()

    unknown = settings_registry.unknown_keys(list(payload.values))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"这些键不可编辑：{', '.join(sorted(unknown))}\n"
                "  只有登记表里的项可以改（GET /api/settings 能看到清单）。"
            ),
        )

    prepared: dict[str, Any] = {}
    risky_touched: list[str] = []
    for key, raw in payload.values.items():
        item = settings_registry.BY_KEY[key]
        try:
            prepared[key] = settings_registry.coerce(item, raw)
        except settings_registry.SettingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if item.risky:
            risky_touched.append(item.label)

    if risky_touched and not payload.confirm_risky:
        raise HTTPException(
            status_code=409,
            detail=(
                "这些是危险项，需要二次确认："
                + "、".join(risky_touched)
                + "。确认后请带 confirm_risky=true 重发。"
            ),
        )

    for key, value in prepared.items():
        set_config_value(key, value)

    return {
        "status": "saved",
        "changed": sorted(prepared),
        "config_file": str(config_file()),
        "restart_required": settings_registry.restart_required(),
        "message": (
            "已写入用户配置文件。**重启 API 后生效** —— 设置是启动时读入的，"
            "当前进程仍在用旧值。"
        ),
    }


@app.get("/api/state/overview")
def state_overview() -> dict:
    """状态根与各工程的**只读**概览（P2-5）。

    回答三个原先只能靠命令行的问题：状态落在哪、每个工程占多大、
    旧 `<repo>/workspace/` 里还剩什么需要迁移。**不写任何文件。**
    """
    return state_admin.overview(orchestrator.settings)


@app.get("/api/overview/cross-project")
def cross_project_overview() -> dict:
    """**跨工程总览**：每个已挂载工程的树状态、花费、未清阻断债（P3-4）。

    与 `/api/state/overview` 的分工：那个是**磁盘**视角（状态落在哪、占多大，
    用于迁移/重置），这个是**内容**视角（跑到什么程度、花了多少，用于决策）。

    纯只读，而且刻意**不建库**：库不存在就是"还没跑过"，不给它造一个空库。
    单个工程读不到不会让整个端点失败 —— 那会把"有一个工程坏了"变成
    "总览坏了"，而用户需要的恰恰是"哪一个坏了"。
    """
    return cross_project.cross_project(orchestrator.settings)


@app.get("/api/cost")
def get_cost(
    root_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """**花费归因 + 成本预测**（P3-3 / P3-2）。

    归因按节点 / 角色 / 模型三维拆；预测按"已完成节点的实际花费 × 剩余节点数"
    给一个区间。**只读**，不做任何拦截 —— 硬闸在 `/api/budget-guard`，
    两者口径不同且有意保持不同（一个管"还能发几次车"，一个管"一共花了多少"）。

    样本不足时预测会明确返回 `available: false` + 原因，而不是给一个凭空的数字。
    """
    resolved = root_id or _latest_root_id(db)
    if not resolved:
        raise HTTPException(status_code=404, detail="还没有任何任务树，没有可归因的花费。")
    return cost_mod.cost_report(db, resolved)


def _recent_run_statuses(db: Session, settings) -> list[str]:
    """最近若干次运行的状态，**时序（最旧 → 最新）**。

    按时序是关键：自动策略用 `[-WINDOW:]` 取"最近一个窗口"，传倒序会让它盯着
    **最早**那几次 —— 一次早期失败会永久把并发压低，而报告写的是"最近 N 次无一成功"。
    实测这条写反过（用例当场红），所以正序在函数名与注释里都写明。
    """
    from sqlalchemy import select as _select

    from .models import RunRecord

    limit = max(120, auto_policy.DIMINISHING_WINDOW)
    rows = db.execute(
        _select(RunRecord.status).order_by(RunRecord.id.desc()).limit(limit)
    ).all()
    return [str(row[0]) for row in reversed(rows)]


@app.get("/api/auto-policy")
def get_auto_policy(
    root_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    """**自动策略**的当前判定（P3-5）。

    回答三个原来无处可查的问题：有哪几条自动策略、各自开着吗、上一次为什么
    触发/没触发。**包括没触发的策略** —— 只报"触发了什么"的话，用户分不清
    "这条策略没开"与"开着但没到条件"，而前者意味着他以为有保护其实没有。

    本端点**只判定、不执行**：动作由编排层执行既有方法，避免出现第二套并发控制。
    同步端点（纯读 + 纯计算），不需要事件循环。
    """
    settings = orchestrator.settings
    resolved = root_id or _latest_root_id(db)
    if not resolved:
        # 没有树时也要能报告"策略开着、只是没条件可判" —— 报 404 会让面板
        # 在空工程上整个消失，而用户正是想确认那些保护在不在。
        observation = auto_policy.observation_from_settings(settings)
        report = auto_policy.report(observation)
        report["root_id"] = None
        report["note"] = "还没有任务树；只报告开关状态。" + report["note"]
        return report
    observation = auto_policy.observe(
        orchestrator.get_tree(db, root_id=resolved),
        recent_statuses=_recent_run_statuses(db, orchestrator.settings),
        settings=settings,
    )
    report = auto_policy.report(observation)
    report["root_id"] = resolved
    report["observed"] = {
        key: observation.get(key)
        for key in (
            "auto_consecutive_failure_limit",
            "idle_hibernate_seconds",
            "auto_concurrency_min",
        )
    }
    return report


@app.post("/api/state/migrate")
def state_migrate(payload: MigrateStateRequest) -> dict:
    """把旧库迁到某个工程的每工程状态目录（P2-5）。

    **默认只预演**（`dry_run=true`）。真正执行走既有的 `migration.migrate()`
    —— 那是"复制"，不动旧库，且按 sha256 幂等（迁过的不会产生第二份）。
    """
    settings = orchestrator.settings
    if payload.dry_run:
        return state_admin.migration_preview(
            settings, repo_root=Path(payload.project_root) if payload.project_root else None
        )

    _ensure_mutable()
    target = payload.project_root.strip() or (
        str(settings.layout.project_root) if settings.layout.project_root else ""
    )
    if not target:
        raise HTTPException(
            status_code=400,
            detail="没有指定目标工程，而且当前也没绑定工程。先绑定一个，或给出 project_root。",
        )
    try:
        result = migration_mod.migrate(
            repo_root=Path(ROOT_DIR),
            project_root=Path(target),
            state_home=settings.layout.state_home,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 迁移失败要如实回报，不是 500
        raise HTTPException(status_code=400, detail=f"{exc.__class__.__name__}: {exc}") from exc

    return {
        "status": "migrated" if result.copied else "skipped",
        "copied": result.copied,
        "skipped_reason": result.skipped_reason,
        "project_key": result.project.key,
        "target_db": str(result.project.db_path),
        "worktree_rows_rewritten": result.worktree_rows_rewritten,
        "codex_home_copied": result.codex_home_copied,
        "source_sha256": result.source_sha256,
        "note": "旧库**未被改动**（迁移是复制）。要清理它请自行删除。",
    }


@app.post("/api/state/reset")
def state_reset(payload: ResetProjectRequest) -> dict:
    """清空**当前工程**的状态目录（不可逆，P2-5）。

    两道闸，缺一不可：

    1. `confirm` 必须逐字等于当前工程 key —— 让手滑付出代价。
    2. 走既有的 `project_store.remove_project(purge=True)`，不在这里自己删目录
       —— 那个函数同时维护 registry 与 `active.json`，自己删会留下悬空记录。

    只允许重置**当前**工程：跨工程删别人的数据属于"我本想删 A 结果删了 B"，
    而那种错在界面上一眼看不出来。
    """
    _ensure_mutable()
    settings = orchestrator.settings
    layout = settings.layout
    if not layout.bound or not layout.project_key:
        raise HTTPException(status_code=400, detail="当前没有绑定工程，没有可重置的对象。")
    if payload.confirm.strip() != layout.project_key:
        raise HTTPException(
            status_code=409,
            detail=(
                "确认串不匹配，已拒绝。\n"
                f"  要清空这个工程的状态，请输入它的 key：{layout.project_key}"
            ),
        )
    try:
        result = project_store.remove_project(
            layout.project_key,
            state_home=layout.state_home,
            purge=bool(payload.purge),
        )
    except ProjectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "status": "reset",
        "key": result["key"],
        "root": result["root"],
        "purged": result["purged"],
        "was_active": result["was_active"],
        "note": (
            "状态目录已删除，`active.json` 已清除 —— **重启 API** 后它会回到未绑定状态"
            "（回退到仓库内 workspace）。工程目录本身没有被碰。"
            if result["purged"]
            else "只从 registry 摘除，状态目录**保留**在磁盘上。"
        ),
    }


@app.get("/api/diagnostics")
def diagnostics(format: str = Query(default="md", pattern="^(md|json)$")) -> Response:
    """一键导出**诊断包**（P2-4）。

    为什么做成一个下载端点而不是让用户去凑：出问题时最常见的成本不是修，
    而是**来回问** —— "API 起了吗""看门狗注册了吗""配置里写了什么"。
    这个工具的状态散在状态根、配置文件、计划任务、本地代理、日志好几处，
    与其每次让用户敲命令复制粘贴，不如一次收齐。

    **返回的文本已脱敏**（`app/diagnostics.py` 的 `redact`）：凭据值一律替换，
    只留变量名 —— 排障要看的是"有没有配"，不是值。环境变量也只收集白名单键名，
    绝不 dump 整个 `os.environ`（那里面可能有与本工具无关的公司凭据）。
    """
    payload = diagnostics_mod.collect(orchestrator.settings)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if format == "json":
        body = diagnostics_mod.render_json(payload)
        media = "application/json; charset=utf-8"
    else:
        body = diagnostics_mod.render_markdown(payload)
        media = "text/markdown; charset=utf-8"
    return Response(
        content=body,
        media_type=media,
        headers={
            "Content-Disposition": (
                f'attachment; filename="codex-battlefield-diagnostics-{stamp}.{format}"'
            ),
        },
    )


@app.get("/api/steward/status")
def steward_status(db: Session = Depends(get_db)) -> dict:
    """管家的"眼睛"：只读状态快照。

    为什么做成一个聚合端点而不是让 agent 逐个调：管家是 LLM，它需要**一次拿到**
    判断局势所需的全部事实（工程/库、计数、被阻塞节点及原因、用量、最近事件、
    树的紧凑轮廓）。让它自己拼多个请求既慢又容易漏，而漏掉"被阻塞的原因"
    恰恰是最要命的 —— 那正是"为什么卡住了"的答案。

    全部只读；取不到的项如实标注 `available: false`，不填 0。
    """
    return build_steward_status(db, orchestrator.settings)


@app.get("/api/steward/capabilities")
def steward_capabilities() -> dict:
    """管家能调用什么，以及每个能力是否改状态。

    把权限**声明**暴露给 agent 本身：它才知道自己不能动树，
    而不是猜或假装做了。改状态的能力会以 `available: false` 列出 ——
    "存在但被禁用"比"假装不存在"更有用，agent 才能告诉用户要放开什么。
    """
    return steward_catalog(read_only=orchestrator.settings.steward_read_only)


class StewardChatRequest(BaseModel):
    message: str = Field(min_length=1)


@app.get("/api/steward/messages")
def steward_messages(db: Session = Depends(get_db)) -> dict:
    """管家的对话记录（按时间正序）。

    **只读**：界面刷新、审计、以及"它当时看到的是什么"都靠这里。
    """
    rows = steward.recent_messages(db, limit=200)
    return {
        "read_only_mode": orchestrator.settings.steward_read_only,
        "messages": [
            {
                "id": row.id,
                "role": row.role,
                "content": row.content,
                "metadata": json.loads(row.metadata_json or "{}"),
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


async def _run_steward_turn(
    db: Session,
    message: str,
    *,
    extra_metadata: dict | None = None,
    record_user_message: bool = True,
) -> dict:
    """跑**一轮**管家对话。用户发起的与主动汇报的共用这一份。

    为什么抽出来：主动汇报（`steward_proactive`）与用户提问走的是同一条链路
    （同一个快照、同一份能力目录、同一个 runner、同一套动作落库）。
    分成两份实现迟早漂移，而漂移的后果是"主动说的"和"问出来的"不一样 ——
    那种不一致会让用户不再相信任何一边。

    `record_user_message=False` 用于主动汇报：它没有"用户说的话"，
    但**仍然要存一条 user 侧消息**（内容是系统指令），
    否则历史里会出现"管家凭空说话"，下一轮读历史时上下文是断的。
    """
    history_rows = steward.recent_messages(db)
    history_text = steward.render_history(history_rows)
    status_text, caps_text = steward.build_context(db, orchestrator.settings, message, history_text)

    if record_user_message:
        steward.append_message(db, role=steward.ROLE_USER, content=message)
    else:
        steward.append_message(
            db,
            role=steward.ROLE_USER,
            content=message,
            metadata={"autonomous": True, **(extra_metadata or {})},
        )

    runner = make_runner(orchestrator.settings)
    try:
        envelope = await runner.steward_chat(status_text, caps_text, history_text, message)
    except Exception as exc:  # noqa: BLE001 - 对话失败要落库并如实回报，不能只给 500
        note = f"管家这一轮没能跑起来：{exc.__class__.__name__}: {exc}"
        steward.append_message(
            db,
            role=steward.ROLE_STEWARD,
            content=note,
            metadata={"error": True, **(extra_metadata or {})},
        )
        return {"reply": note, "situation": "", "proposed_actions": [], "needs_user_input": False,
                "confidence": "low", "error": True}

    chat = steward.parse_chat(envelope.payload)

    # 把建议落成**待批准**的动作。管家永远不能自己执行 ——
    # 它提议、你批准、服务端执行（`app/steward_actions.py`）。
    # 不合法的建议不写库，而是把原因带回去：界面上摆一个"点了会失败"的按钮
    # 比直接说"这条建议不合法"更糟。
    queued, rejected = steward_actions.propose(
        db,
        [item.model_dump() for item in chat.proposed_actions],
        read_only=orchestrator.settings.steward_read_only,
    )

    metadata = {
        "situation": chat.situation,
        "proposed_actions": [item.model_dump() for item in chat.proposed_actions],
        "proposed_action_ids": [row.id for row in queued],
        "rejected_actions": rejected,
        "confidence": chat.confidence,
        "exit_code": envelope.exit_code,
        "prompt_path": envelope.prompt_path,
        "logs_path": envelope.logs_path,
        "read_only_mode": orchestrator.settings.steward_read_only,
        **(extra_metadata or {}),
    }
    steward.append_message(db, role=steward.ROLE_STEWARD, content=chat.reply, metadata=metadata)

    return {
        "reply": chat.reply,
        "situation": chat.situation,
        "proposed_actions": metadata["proposed_actions"],
        "proposed_action_ids": metadata["proposed_action_ids"],
        "rejected_actions": rejected,
        "needs_user_input": chat.needs_user_input,
        "confidence": chat.confidence,
        "exit_code": envelope.exit_code,
        "read_only_mode": orchestrator.settings.steward_read_only,
    }


@app.post("/api/steward/chat")
async def steward_chat(payload: StewardChatRequest, db: Session = Depends(get_db)) -> dict:
    """跟管家说一句话，拿回它的回复。

    流程与理由见 `app/steward.py` 的模块头。这里只强调两点：

    * **每轮重建状态快照**：管家要回答"现在怎么样"，喂缓存快照会让它拿旧状态
      回答新问题 —— 而那种错误极难发现，它的回答看起来永远合理。
    * **失败也要落库**：即使 runner 出错，用户的这条消息与错误说明也会存下来。
      否则用户会看到"我说了但记录里没有"，那比报错更让人不信任。
    """
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="消息不能为空")
    return await _run_steward_turn(db, message)


@app.delete("/api/steward/messages")
def steward_clear(db: Session = Depends(get_db)) -> dict:
    """清空管家的对话。**只清对话**，不碰树。"""
    removed = steward.clear_history(db)
    return {"status": "cleared", "removed": removed}


def _action_payload(row: Any) -> dict:
    return {
        "id": row.id,
        "action": row.action,
        "target": row.target,
        "why": row.why,
        "risk": row.risk,
        "status": row.status,
        "result": row.result,
        "error": row.error,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
        "executed_at": row.executed_at.isoformat() if row.executed_at else None,
    }


@app.get("/api/conclusions")
def conclusions_list(
    root_id: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict:
    """结论文档清单（P4-5）。最近在前 —— 复盘总是从最近的一次开始。"""
    rows = conclusions_mod.list_conclusions(db, root_id=root_id, limit=limit)
    return {"conclusions": [conclusions_mod.payload(row) for row in rows]}


@app.get("/api/conclusions/draft")
def conclusions_draft(
    root_id: str | None = Query(default=None),
    node_id: str = Query(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """**生成草稿但不落库**（P4-5）。

    事实由代码抽（影响面、决策点、代价、未清债），"当时为什么这么决定"留空
    由人补。分开的理由写在 `app/conclusions.py` 里：只有事实的文档不值得归档。

    刻意**不写库**：草稿是给人看和改的中间产物，每次点一下就在库里留一条，
    归档区很快就没法看了。
    """
    resolved = root_id or _latest_root_id(db)
    if not resolved:
        raise HTTPException(status_code=404, detail="还没有任务树，没有可归档的事故。")

    tree = orchestrator.get_tree(db, root_id=resolved)
    events = _recent_events(db, resolved)
    obligations = _open_blocking_obligations(db, resolved)
    cost = cost_mod.cost_report(db, resolved)["attribution"]
    facts, incomplete = conclusions_mod.build_facts(
        tree=tree,
        events=events,
        open_obligations=obligations,
        cost={
            "total_tokens": cost.get("total_tokens"),
            "cost_by_currency": cost.get("cost_by_currency"),
            "cost_unlabeled": cost.get("cost_unlabeled"),
        },
        node_id=node_id,
    )
    title = f"结论 {resolved}" + (f" / {node_id}" if node_id else "")
    return {
        "root_id": resolved,
        "node_id": node_id,
        "title": title,
        "facts": facts,
        "incomplete": incomplete,
        "markdown": conclusions_mod.render_markdown(
            title=title, facts=facts, incomplete=incomplete, authored=""
        ),
        "note": "这是草稿、**没有落库**。「当时为什么这么决定」那一段要人来写。",
    }


@app.post("/api/conclusions")
def conclusions_write(payload: dict, db: Session = Depends(get_db)) -> dict:
    """落一份结论（P4-5）。

    **`authored` 必填**：没有"当时为什么"的文档会被拒 —— 它只是另一份事件表，
    而且会因为"看起来像结论"而更少被人重新检查。

    写结论**不走管家只读那道闸**：只读禁的是"替用户动树"，而归档一份文档
    不动任何东西。要求先关只读才能记录复盘，只会让人不记录。
    """
    _ensure_mutable()
    try:
        row = conclusions_mod.write(
            db,
            root_id=str(payload.get("root_id") or ""),
            authored=str(payload.get("authored") or ""),
            title=str(payload.get("title") or ""),
            node_id=str(payload.get("node_id") or ""),
            author=str(payload.get("author") or ""),
            facts=payload.get("facts") or {},
            incomplete=payload.get("incomplete") or [],
            status=str(payload.get("status") or "draft"),
            conclusion_id=payload.get("conclusion_id"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return conclusions_mod.payload(row)


@app.get("/api/conclusions/{conclusion_id}")
def conclusions_detail(conclusion_id: int, db: Session = Depends(get_db)) -> dict:
    from .models import IncidentConclusion

    row = db.get(IncidentConclusion, conclusion_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"找不到结论 {conclusion_id}。")
    return conclusions_mod.payload(row)


@app.get("/api/conclusions/{conclusion_id}/export")
def conclusions_export(conclusion_id: int, db: Session = Depends(get_db)) -> Response:
    """把结论导出成 `.md`（P4-5）。**这是"可归档"的实际含义。**"""
    from .models import IncidentConclusion

    row = db.get(IncidentConclusion, conclusion_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"找不到结论 {conclusion_id}。")
    return Response(
        content=f"<!-- generated by codex-battlefield; root={row.root_id} id={row.id} -->\n{row.markdown}",
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="conclusion-{row.id}.md"',
        },
    )


def _recent_events(db: Session, root_id: str, limit: int = 500) -> list[dict]:
    """这棵树的最近事件，**时序（最旧 → 最新）**。草稿的决策点从这里筛。

    范围：树内节点的全部后代 + `task_id` 为空的事件。
    为什么要收 `task_id` 为空的那种：宪法锁定、预算闸这类**决策**常常记在全局
    （不属于某个节点），漏掉它们正好漏掉最该进时间线的东西。
    """
    from sqlalchemy import or_, select as _select

    from .models import EventRecord, TaskNode

    descendants: set[str] = set()
    frontier = [root_id]
    rows_all = db.execute(_select(TaskNode.id, TaskNode.parent_id)).all()
    children: dict[str, list[str]] = {}
    for node_id, parent_id in rows_all:
        if parent_id:
            children.setdefault(str(parent_id), []).append(str(node_id))
    while frontier:
        current = frontier.pop()
        if current in descendants:
            continue
        descendants.add(current)
        frontier.extend(children.get(current, []))

    rows = db.execute(
        _select(EventRecord.type, EventRecord.message, EventRecord.task_id, EventRecord.created_at)
        .where(or_(EventRecord.task_id.is_(None), EventRecord.task_id.in_(descendants)))
        .order_by(EventRecord.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "type": str(row[0] or ""),
            "message": str(row[1] or ""),
            "task_id": str(row[2] or ""),
            "created_at": row[3].isoformat() if row[3] is not None else "",
        }
        for row in reversed(rows)
    ]


def _open_blocking_obligations(db: Session, root_id: str) -> list[dict]:
    """未清的阻断债（草稿用来写"以后什么条件下重新打开"）。"""
    try:
        board = obligation_index.board(db, status="open")
    except Exception:  # noqa: BLE001 - 索引读不到不该让草稿整个失败
        return []
    items = board.get("items") if isinstance(board, dict) else None
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict) and str(item.get("severity") or "") == "blocking"
    ]


@app.get("/api/deliveries/latest")
def delivery_latest(
    author: str = Query(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """最近一次目标投递 + 它的**实时**进度（P4-1）。

    回答"第几步、卡在哪"。**不含后台推送**：管家是对话者，它的报告应当发生在
    有人看的时候 —— 推送会把"树在动"变成消息流，而这个工具已经有看门狗在做
    后台判断了。

    `author` 可选：多人共用一台机器时，"我投的那个"不该拿到别人的树。
    """
    row = delivery_mod.latest(db, author=author)
    if row is None:
        return {
            "available": False,
            "reason": "还没有通过管家投递过目标。",
            "delivery": None,
            "progress": None,
        }

    tree = orchestrator.get_tree(db, root_id=row.root_id)
    obligations = _open_blocking_obligations(db, row.root_id)
    # `get_tree` 的返回值里**没有** `root_id`（它不是树的一部分，是查询参数）。
    # 不显式补上的话 `progress()` 会报"读不到 root_id —— 这份进度可能不属于你以为的
    # 那棵树"，于是每一条投递报告都拖着一条假警报 —— 而假警报会让真警报被无视。
    snapshot = delivery_mod.progress(
        {"root_id": row.root_id, "tasks": tree.get("tasks") or []},
        open_obligations=obligations,
        goal=row.goal,
        author=row.author,
    )
    return {
        "available": True,
        "reason": "",
        "delivery": delivery_mod.payload(row),
        "progress": snapshot,
        "summary": delivery_mod.summary_line(snapshot),
    }


@app.post("/api/deliveries")
def delivery_record(payload: dict, db: Session = Depends(get_db)) -> dict:
    """记一次目标投递（P4-1）。

    管家起树时由服务端自己记（`steward_actions._record_delivery`），这个端点留给
    "通过别的路径起了树但想让它进管家视野"的情形。

    **`root_id` 必填**：一次"投递"的定义就是"确实起出了一棵树" ——
    起树失败时留下一条记录，会让"我投过这个目标"看起来像发生过。
    """
    _ensure_mutable()
    try:
        row = delivery_mod.record(
            db,
            root_id=str(payload.get("root_id") or ""),
            goal=str(payload.get("goal") or ""),
            author=str(payload.get("author") or ""),
            source=str(payload.get("source") or "manual"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return delivery_mod.payload(row)


@app.get("/api/attention")
def attention_list(db: Session = Depends(get_db)) -> dict:
    """当前生效的"注意"认领（P4-3）。

    管家与看门狗都能发现问题，但**谁也不知道对方已经看到了** —— 于是同一件事被处理
    两次。重复动作的代价不只是白干活：它会掩盖真实信号（"这个节点被重试了 3 次"
    看起来像节点不稳定，其实是两个组件各点了一次）。

    这张账是两边共用的：谁要处理谁记账，处理前先查账。
    """
    return attention_mod.payload(db)


@app.delete("/api/attention/{claim_id}")
def attention_release(
    claim_id: int,
    actor: str = Query(default="human"),
    force: bool = Query(default=False),
    declared_by: str = Query(default=""),
    reason: str = Query(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """放掉一条认领。

    **只有认领人自己能放**，除非 `force=true` 且 `declared_by` 非空（人在界面上
    强制解除）。这是权限边界：看门狗不该替管家宣布"管家的介入结束了"。
    强制解除必须留名 —— 否则"谁把这条锁解开的"事后无从查起。
    """
    _ensure_mutable()
    try:
        return attention_mod.release(
            db, claim_id, actor=actor, force=force, reason=reason, declared_by=declared_by
        )
    except attention_mod.ClaimError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/watchdog/tuning")
def watchdog_tuning_get(db: Session = Depends(get_db)) -> dict:
    """可调档位 + 当前生效值 + 调整历史（P4-2）。

    要解决的不是"能不能调"，而是"调了之后查得到、能改回去"。
    """
    return {
        "catalog": watchdog_tuning_mod.catalog(),
        "current": watchdog_tuning_mod.current_values(orchestrator.settings),
        "history": watchdog_tuning_mod.history(db),
        "note": (
            "记录**先于生效**：先写事件、再改配置。反过来会在崩溃时留下一个无人知道的"
            "改动，而它看起来和「配置一直都是这样」完全一样。"
        ),
    }


@app.post("/api/watchdog/tuning")
def watchdog_tuning_apply(payload: dict, db: Session = Depends(get_db)) -> dict:
    """调整档位（P4-2）。**一批要么全过、要么全不过**。

    一半生效一半失败是最难查的状态：用户只看到一条错误，而实际改了两项里的第一项。
    """
    _ensure_mutable()
    actor = str(payload.get("actor") or "")
    try:
        changes = watchdog_tuning_mod.plan(
            payload.get("changes") or [],
            current=watchdog_tuning_mod.current_values(orchestrator.settings),
            actor=actor,
        )
    except watchdog_tuning_mod.TuningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 记录先于生效（见模块文档）：先落审计，再写配置。
    watchdog_tuning_mod.record(db, changes, applied=True)
    watchdog_tuning_mod.apply_to_config_file(changes)
    return {
        "status": "applied",
        "changes": [change.as_payload() for change in changes],
        "message": (
            watchdog_tuning_mod.render(changes)
            + "。**重启 API 后生效**（档位是启动时读入的）。"
        ),
        "restart_required": True,
    }


@app.post("/api/watchdog/tuning/rollback")
def watchdog_tuning_rollback(payload: dict, db: Session = Depends(get_db)) -> dict:
    """回滚最近若干次档位调整（P4-2）。

    为什么要能回滚：一次调错的并发档会让机器被压垮，而"改回去"必须是一键的 ——
    否则人会去猜原来的值，猜错的概率不低。
    """
    _ensure_mutable()
    actor = str(payload.get("actor") or "")
    if not actor.strip():
        raise HTTPException(status_code=400, detail="回滚必须留名（actor）。")
    steps = int(payload.get("steps") or 1)
    history = watchdog_tuning_mod.history(db, limit=max(1, steps))
    if not history:
        raise HTTPException(status_code=400, detail="没有可回滚的档位调整记录。")
    try:
        targets = watchdog_tuning_mod.rollback_targets(history, limit=max(1, steps))
        changes = watchdog_tuning_mod.plan(
            [{"key": item.key, "value": item.new, "reason": item.reason} for item in targets],
            current=watchdog_tuning_mod.current_values(orchestrator.settings),
            actor=actor,
        )
    except watchdog_tuning_mod.TuningError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    watchdog_tuning_mod.record(db, changes, applied=True)
    watchdog_tuning_mod.apply_to_config_file(changes)
    return {
        "status": "rolled_back",
        "steps": len(history),
        "changes": [change.as_payload() for change in changes],
        "message": watchdog_tuning_mod.render(changes) + "。**重启 API 后生效**。",
        "restart_required": True,
    }


@app.get("/api/steward/pre-authorization")
def steward_pre_authorization() -> dict:
    """当前预授权状态 + 动作风险目录（P4-4）。

    刻意把**目录**一起报出来：用户要先看到"哪些动作是高风险的、永远不能预授权"，
    才谈得上决定要不要开预授权。只报当前那条授权的话，界面只能显示一个动作名列表，
    而"它有多危险"是这里唯一重要的信息。
    """
    from .steward_actions import known_actions

    names = list(known_actions())
    grant = steward_actions.pre_authorization.grant
    return {
        "catalog": action_risk.catalog(names),
        "levels": [
            {
                "level": level,
                "label": action_risk.RISK_LABEL[level],
                "rationale": action_risk.RISK_RATIONALE[level],
            }
            for level in action_risk.RISK_LEVELS
        ],
        "grant": (
            {
                "actions": list(grant.actions),
                "targets": list(grant.targets),
                "expires_at": grant.expires_at.isoformat(),
                "remaining_seconds": max(0, grant.remaining_seconds()),
                "granted_by": grant.granted_by,
                "reason": grant.reason,
            }
            if grant is not None
            else None
        ),
        "high_risk_never_pre_authorized": True,
        "note": (
            "预授权放宽的是**确认次数**，不是权限边界：高风险动作（取消、全停、改额度闸）"
            "在任何授权下都仍然需要人点。默认没有授权 —— 出厂状态是每次都要人确认。"
        ),
    }


@app.post("/api/steward/pre-authorization")
def steward_set_pre_authorization(payload: dict) -> dict:
    """授予一条预授权（P4-4）。**范围与期限都必填**，且必须署名。"""
    _ensure_mutable()
    _ensure_steward_mutable()
    try:
        grant = action_risk.make_grant(
            actions=[str(item) for item in (payload.get("actions") or [])],
            targets=[str(item) for item in (payload.get("targets") or [])],
            minutes=int(payload.get("minutes") or 0),
            granted_by=str(payload.get("granted_by") or ""),
            reason=str(payload.get("reason") or ""),
        )
    except (action_risk.RiskError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    steward_actions.pre_authorization.set(grant)
    return {
        "status": "granted",
        "actions": list(grant.actions),
        "targets": list(grant.targets),
        "expires_at": grant.expires_at.isoformat(),
        "remaining_seconds": max(0, grant.remaining_seconds()),
        "message": (
            f"预授权已生效：{len(grant.actions)} 个动作、"
            f"{len(grant.targets) or '不限'}个目标、{grant.remaining_seconds()} 秒后过期。"
            "高风险动作不在此范围内，仍然需要你逐个确认。"
        ),
    }


@app.delete("/api/steward/pre-authorization")
def steward_clear_pre_authorization() -> dict:
    """撤销预授权，立刻回到"每次都要人点"。"""
    _ensure_mutable()
    steward_actions.pre_authorization.clear()
    return {"status": "cleared", "message": "预授权已撤销，之后每个动作都需要你确认。"}


@app.get("/api/steward/actions")
def steward_actions_list(status: str = Query(default=""), db: Session = Depends(get_db)) -> dict:
    """管家提议过的动作及各自的审批/执行足迹。

    `status` 可筛 `pending`：界面只需要"等我决定"的那些。
    """
    rows = steward_actions.list_actions(db, status=status)
    pending = [row for row in steward_actions.list_actions(db, status=steward_actions.PENDING)]
    return {
        "read_only_mode": orchestrator.settings.steward_read_only,
        "pending_count": len(pending),
        "actions": [_action_payload(row) for row in rows],
    }


@app.post("/api/steward/actions/{action_id}/approve")
async def steward_action_approve(action_id: int, db: Session = Depends(get_db)) -> dict:
    """**批准并执行**一个待办动作。

    批准与执行在这一次调用里完成，但库里分两步写（`decided_at` 与 `executed_at`）：
    这样"批准了但执行失败"能被看见 —— 用户以为做了、其实没做，是最糟的状态。
    """
    _ensure_mutable()
    _ensure_steward_mutable()
    try:
        steward_actions.decide(db, action_id, approve=True)
        ok, message = await steward_actions.run_approved(db, orchestrator, action_id)
    except steward_actions.ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    row = db.get(StewardAction, action_id)
    return {"ok": ok, "message": message, "action": _action_payload(row) if row else None}


@app.post("/api/steward/actions/{action_id}/reject")
def steward_action_reject(action_id: int, db: Session = Depends(get_db)) -> dict:
    """拒绝一个待办动作。**只标状态，不执行**。"""
    try:
        row = steward_actions.decide(db, action_id, approve=False)
    except steward_actions.ActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "action": _action_payload(row)}


@app.get("/api/steward/alerts")
def steward_alerts(db: Session = Depends(get_db)) -> dict:
    """**值得打扰用户的信号**（拉取一次拿全量）。

    判定在 `steward_alerts.evaluate()` —— 与推送流共用同一份实现，
    所以"界面上看到的"和"推送来的"不可能不一致。
    """
    return steward_alerts_mod.evaluate(db, orchestrator.settings)


def _sse(event: str, payload: dict) -> str:
    """SSE 帧。带 `event:` 让前端能分辨种类（首帧快照 vs 后续提醒 vs 心跳）。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def _push_reason(
    alerts: list[dict], worth: list[dict], min_level: str, *, first: bool
) -> str:
    """一句话说明**为什么现在推**。

    用户看到一条提醒时，最想知道的是"这跟我上次看到的有何不同"。
    没有这句，一条"3 个节点失败"到底是新出现的还是已经挂了两小时，无从判断。
    """
    if first:
        return "首帧：当前提醒全貌。"
    if worth:
        kinds = "、".join(alert.get("kind", "") for alert in worth)
        return f"新出现的提醒达到打扰阈值（{min_level}）：{kinds}。"
    if alerts:
        return "提醒有变化，但都低于打扰阈值（仅供参考）。"
    return "提醒已清空。"


@app.get("/api/steward/alerts/stream")
async def steward_alerts_stream() -> StreamingResponse:
    """**主动推送**：提醒变化时推，没变化就不说话（SSE）。

    为什么用 SSE 而不是 WebSocket：这是**单向**的（服务端 -> 界面），
    SSE 自带断线重连，而且就是普通 HTTP —— 为了一个单向通知流
    引入双向协议是没必要的复杂度。

    三条节流规则（见 `steward_alerts` 模块注释）：
    * **边沿触发**：提醒集合或等级没变就**不推**（持续阻塞的节点只报一次）；
    * **阈值**：只推 `steward_push_min_level` 及以上的；
    * **冷却**：两次推送至少隔 `steward_push_throttle_seconds`。
    """
    settings = orchestrator.settings
    interval = max(0.5, float(settings.steward_stream_interval_seconds))
    throttle = max(0.0, float(settings.steward_push_throttle_seconds))
    min_level = settings.steward_push_min_level

    def _evaluate_once() -> dict:
        # `evaluate` 是**同步**的（SQLAlchemy 同步会话 + 文件 IO）。直接在事件循环里
        # 调它会阻塞整个 API —— 表现为"推送开着的时候其他请求变慢"。
        db = SessionLocal()
        try:
            return steward_alerts_mod.evaluate(db, settings)
        finally:
            db.close()

    async def stream():
        last_digest: str | None = None
        last_push_at = 0.0
        first_frame = True

        while True:
            try:
                # 丢进线程池，让事件循环继续转。
                payload = await asyncio.to_thread(_evaluate_once)
            except Exception as exc:  # noqa: BLE001 - 推送流不能因为一次读库失败就断掉
                yield _sse("error", {"message": f"评估提醒失败：{exc}"})
                await asyncio.sleep(interval)
                continue

            alerts = payload["alerts"]
            digest = steward_alerts_mod.alerts_digest(alerts)
            worth = steward_alerts_mod.worth_pushing(alerts, min_level)
            now = time.monotonic()

            changed = digest != last_digest
            cooled = (now - last_push_at) >= throttle
            # 首帧总是发（让界面立刻有东西显示），之后只发值得打扰的。
            send = changed and (cooled or first_frame) and (bool(worth) or first_frame)
            if send:
                yield _sse(
                    "alerts",
                    {
                        "alerts": alerts,
                        "worth_pushing": worth,
                        "counts": payload["counts"],
                        "reason": _push_reason(alerts, worth, min_level, first=first_frame),
                        "min_level": min_level,
                    },
                )
                first_frame = False
                last_push_at = now

            # 指纹**每轮都记**，包括被冷却/阈值挡下的那些。
            # 不记的话，一个只含 info 的变化会让指纹一直"悬着对不上"，
            # 等它终于升到 action 时，"变了"这个信号早就被消耗掉了。
            last_digest = digest

            await asyncio.sleep(interval)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # 关掉代理缓冲，否则推送会被攒着一起发，"主动"就没了。
            "X-Accel-Buffering": "no",
        },
    )


def _ensure_mutable() -> None:
    if orchestrator.settings.frozen_mode:
        raise HTTPException(status_code=423, detail="Codex Battlefield is running in frozen read-only mode")


def _latest_root_id(db: Session) -> str | None:
    """最近更新的那棵树的根节点 id。没有树时返回 `None`。

    为什么按 `updated_at` 而不是创建时间：用户问"花了多少"时指的是**正在关心的那棵**，
    而它几乎总是最近动过的那棵。没有这个默认值时，界面每次都要先问一次树才能归因。
    """
    row = db.execute(
        select(TaskNode.id)
        .where(TaskNode.parent_id.is_(None))
        .order_by(TaskNode.updated_at.desc())
        .limit(1)
    ).first()
    return str(row[0]) if row else None


def _ensure_steward_mutable() -> None:
    """管家自己那道闸：`steward_read_only` 为真时**不许代替用户执行**。

    为什么这必须单独判、不能只靠 `_ensure_mutable`：
    `frozen_mode` 默认关，而 `steward_read_only` 默认**开**。只看前者的话，
    默认配置下"只读的管家"批准完就能动树 —— 界面上写着"只读"、实际能执行，
    是最坏的一种误导。两道闸管的是两件事：整机冻结 vs 管家权限。

    提议（propose）不受此闸限制：只读禁的是执行，不是建议。
    """
    if orchestrator.settings.steward_read_only:
        raise HTTPException(
            status_code=423,
            detail=(
                "管家处于只读模式，不能代为执行动作。"
                "想让它能执行，设 CODEX_BATTLEFIELD_STEWARD_READ_ONLY=0 后重启 API。"
            ),
        )


@app.get("/api/machine-safety")
def machine_safety() -> dict:
    path = machine_path_of(orchestrator.settings, "machine_safety.json")
    if not path.exists():
        return {"status": "missing", "message": "machine safety has not been sampled yet"}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"status": "unreadable", "message": str(exc)}
    if isinstance(data, dict):
        data.setdefault("status", "ok")
        return data
    return {"status": "invalid", "message": "machine safety state is not a JSON object"}


@app.post("/api/projects/start", response_model=ProjectStartResponse)
async def start_project(payload: ProjectStartRequest) -> ProjectStartResponse:
    _ensure_mutable()
    try:
        root_id = await orchestrator.start_project(
            goal=payload.goal,
            target_path=payload.target_path,
            runner_mode=payload.runner_mode,
            constitution_id=payload.constitution_id,
            auto_run=payload.auto_run,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ProjectStartResponse(root_id=root_id, status="started")


@app.post("/api/projects/one-shot", response_model=ProjectStartResponse)
async def start_project_one_shot(
    payload: OneShotProjectRequest,
    request: Request,
) -> ProjectStartResponse:
    _ensure_mutable()
    if request.query_params:
        raise HTTPException(status_code=422, detail="one-shot project accepts exactly one input: goal")
    try:
        root_id = await orchestrator.start_one_shot_project(
            goal=payload.goal,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ProjectStartResponse(root_id=root_id, status="started")


@app.post("/api/constitutions/draft", response_model=ConstitutionResponse)
async def draft_constitution(payload: ConstitutionDraftRequest, db: Session = Depends(get_db)) -> ConstitutionResponse:
    _ensure_mutable()
    constitution_id = await orchestrator.draft_constitution(
        goal=payload.goal,
        target_path=payload.target_path,
        runner_mode=payload.runner_mode,
    )
    record = orchestrator.get_constitution(db, constitution_id)
    if record is None:
        raise HTTPException(status_code=500, detail="constitution draft was not saved")
    return ConstitutionResponse.model_validate(record)


@app.get("/api/constitutions/latest", response_model=ConstitutionResponse | None)
def latest_constitution(root_id: str | None = None, db: Session = Depends(get_db)) -> ConstitutionResponse | None:
    """不传 `root_id` 时只在**不歧义**的情况下给答案；多棵树并存时显式 409，不猜（§4 P2）。"""
    try:
        record = orchestrator.get_constitution(db, root_id=root_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ConstitutionResponse.model_validate(record) if record else None


@app.get("/api/constitutions/{constitution_id}", response_model=ConstitutionResponse)
def get_constitution(constitution_id: str, db: Session = Depends(get_db)) -> ConstitutionResponse:
    record = orchestrator.get_constitution(db, constitution_id)
    if record is None:
        raise HTTPException(status_code=404, detail="constitution not found")
    return ConstitutionResponse.model_validate(record)


@app.put("/api/constitutions/{constitution_id}", response_model=ConstitutionResponse)
def update_constitution(constitution_id: str, payload: ConstitutionUpdateRequest, db: Session = Depends(get_db)) -> ConstitutionResponse:
    _ensure_mutable()
    try:
        orchestrator.update_constitution(constitution_id, payload.markdown, payload.data)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record = orchestrator.get_constitution(db, constitution_id)
    if record is None:
        raise HTTPException(status_code=404, detail="constitution not found")
    return ConstitutionResponse.model_validate(record)


@app.post("/api/projects/{root_id}/constitution/amend")
async def amend_constitution(root_id: str, payload: ConstitutionAmendRequest) -> dict:
    """在树运行中修订宪法：新版本落库 + 按状态分治迁移 + 登记再验证队列。

    `direction` 与 `actor` 都必填：这是**人**的决定。root agent 只能产出修订建议，
    这条通路不暴露给 agent 的 prompt。
    """
    _ensure_mutable()
    try:
        summary = orchestrator.amend_constitution(
            root_id,
            markdown=payload.markdown,
            data=payload.data,
            direction=payload.direction,
            actor=payload.actor,
            note=payload.note,
            revalidate_exemptions=payload.revalidate_exemptions,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # §6：`blocked`/`failed` 的节点已在事务里"重新分类并重新排队"，但要有人推它们一把才真的跑。
    # 起跑放在**事务提交之后**：一次修订事务里顺手拉起一堆 agent 会把失败面变大，
    # 而且那时候版本行还没落库。
    if summary.get("reclassified_ids"):
        summary["kick"] = await orchestrator.kick_tree(root_id)
    return summary


@app.post("/api/constitutions/{constitution_id}/lock", response_model=ConstitutionResponse)
def lock_constitution(constitution_id: str, db: Session = Depends(get_db)) -> ConstitutionResponse:
    _ensure_mutable()
    try:
        orchestrator.lock_constitution(constitution_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    record = orchestrator.get_constitution(db, constitution_id)
    if record is None:
        raise HTTPException(status_code=404, detail="constitution not found")
    return ConstitutionResponse.model_validate(record)


@app.get("/api/tree", response_model=TreeResponse)
def get_tree(
    root_id: str | None = Query(default=None),
    compact_done: bool = Query(default=True),
    expanded_done_parent_id: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
) -> TreeResponse:
    return TreeResponse.model_validate(
        orchestrator.get_tree(
            db,
            root_id=root_id,
            compact_done=compact_done,
            expanded_done_parent_ids=expanded_done_parent_id,
        )
    )


@app.get("/api/obligation-index", response_model=ObligationIndexBoard)
def get_obligation_index(
    status: Literal["open", "settled", "all"] = Query(default="open"),
    db: Session = Depends(get_db),
) -> ObligationIndexBoard:
    """跨树债务聚合（**只读**）。

    读的是**派生缓存**（`obligation_index` 表），所以响应里同时带 `built_at` 与 `in_sync`：
    缓存过期时 `total` 不构成读数，界面不许把 0 读成"没有债"。重建仍只由
    `scripts/rebuild_obligation_index.py` 负责——这里**不自动重建**（那会抹掉漂移的原因）。
    """
    return ObligationIndexBoard.model_validate(obligation_index.board(db, status=status))


@app.get("/api/watchdog/state", response_model=WatchdogStateResponse)
def get_watchdog_state(root_id: str | None = Query(default=None), db: Session = Depends(get_db)) -> WatchdogStateResponse:
    return WatchdogStateResponse.model_validate(orchestrator.get_watchdog_state(db, root_id=root_id))


@app.post("/api/watchdog/install", response_model=WatchdogActionResponse)
def install_watchdog(root_id: str | None = Query(default=None)) -> WatchdogActionResponse:
    _ensure_mutable()
    return WatchdogActionResponse.model_validate(orchestrator.install_watchdog_hook(root_id=root_id))


@app.post("/api/watchdog/run-once", response_model=WatchdogActionResponse)
def run_watchdog_once(root_id: str | None = Query(default=None)) -> WatchdogActionResponse:
    _ensure_mutable()
    return WatchdogActionResponse.model_validate(orchestrator.run_watchdog_once(root_id=root_id))


@app.post("/api/watchdog/cleanup-stalled")
def cleanup_stalled_watchdog_agents(root_id: str | None = Query(default=None)) -> dict:
    _ensure_mutable()
    return orchestrator.cleanup_stalled_external_codex_agents(root_id=root_id)


@app.post("/api/watchdog/api-restart-drain", response_model=ApiRestartDrainResponse)
def arm_api_restart_drain() -> ApiRestartDrainResponse:
    _ensure_mutable()
    return ApiRestartDrainResponse.model_validate(orchestrator.set_api_restart_drain(True))


@app.delete("/api/watchdog/api-restart-drain", response_model=ApiRestartDrainResponse)
def disarm_api_restart_drain() -> ApiRestartDrainResponse:
    _ensure_mutable()
    return ApiRestartDrainResponse.model_validate(orchestrator.set_api_restart_drain(False))


@app.get("/api/budget-guard", response_model=BudgetGuardResponse)
def get_budget_guard(root_id: str | None = Query(default=None)) -> BudgetGuardResponse:
    return BudgetGuardResponse.model_validate(orchestrator.budget_guard_state(root_id=root_id))


@app.post("/api/budget-guard", response_model=BudgetGuardResponse)
async def set_budget_guard(payload: BudgetGuardRequest) -> BudgetGuardResponse:
    _ensure_mutable()
    result = await orchestrator.set_budget_guard(
        root_id=payload.root_id,
        resume_after_seconds=payload.resume_after_seconds,
        reason=payload.reason,
        hibernate_tree=payload.hibernate_tree,
    )
    return BudgetGuardResponse.model_validate(result)


@app.delete("/api/budget-guard", response_model=BudgetGuardResponse)
def clear_budget_guard(root_id: str | None = Query(default=None)) -> BudgetGuardResponse:
    _ensure_mutable()
    return BudgetGuardResponse.model_validate(orchestrator.clear_budget_guard(root_id=root_id))


@app.post("/api/tasks/{task_id}/obligations/settle")
def settle_obligation(task_id: str, payload: ObligationSettleRequest) -> dict:
    """人显式清债（§8.2 逃生舱）。理由与身份都必填：放宽不是禁止的，无声的放宽才是。

    `obligation_id` 走请求体而不是路径参数——它形如
    `integration_conflict:<父节点>:src/x.gd`，带冒号**和斜杠**，塞进路径参数会被切碎。

    清债不会自动把节点重新跑起来；继续干活请另调 `POST /api/tasks/{id}/retry`。
    """
    _ensure_mutable()
    try:
        return orchestrator.settle_obligation(
            task_id, payload.obligation_id, actor=payload.actor, reason=payload.reason
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/tasks/{task_id}", response_model=TaskDetailResponse)
def get_task(task_id: str, db: Session = Depends(get_db)) -> TaskDetailResponse:
    try:
        return TaskDetailResponse.model_validate(orchestrator.get_detail(db, task_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/runs/{run_id}/artifact", response_model=RunArtifactResponse)
def get_run_artifact(
    run_id: int,
    kind: str = Query(pattern="^(prompt|logs|last_message)$"),
    db: Session = Depends(get_db),
) -> RunArtifactResponse:
    try:
        return RunArtifactResponse.model_validate(orchestrator.get_run_artifact(db, run_id, kind))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/files/preview")
def preview_file(path: str = Query(min_length=1), db: Session = Depends(get_db)) -> FileResponse:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = orchestrator.settings.workspace_dir / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid preview path") from exc
    media_type = IMAGE_PREVIEW_EXTENSIONS.get(resolved.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=415, detail="preview only supports image files")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="preview file not found")
    if not any(_is_relative_to(resolved, root) for root in _preview_roots(db)):
        raise HTTPException(status_code=403, detail="preview file is outside allowed project roots")
    return FileResponse(str(resolved), media_type=media_type)


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str) -> dict[str, str]:
    _ensure_mutable()
    try:
        await orchestrator.retry_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "queued"}


@app.post("/api/tasks/{task_id}/pause")
def pause_task(task_id: str) -> dict[str, str]:
    _ensure_mutable()
    try:
        orchestrator.set_task_status(task_id, TaskStatus.PAUSED, "用户已暂停该任务。")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "paused"}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str) -> dict[str, str]:
    _ensure_mutable()
    try:
        result = await orchestrator.resume_hibernated_tree(task_id)
        if result.get("count", 0) == 0:
            kicked = await orchestrator.kick_tree(task_id)
            if kicked.get("status") in {"stopped", "terminal"}:
                await orchestrator.retry_task(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "queued"}


@app.post("/api/tasks/{task_id}/kick")
async def kick_task_tree(task_id: str) -> dict[str, str]:
    _ensure_mutable()
    try:
        result = await orchestrator.kick_tree(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": str(result.get("status") or "queued")}


@app.post("/api/tasks/{task_id}/hibernate")
async def hibernate_task(task_id: str, payload: HibernateRequest | None = None) -> dict[str, int | str]:
    _ensure_mutable()
    request = payload or HibernateRequest()
    try:
        result = await orchestrator.hibernate_tree(task_id, request.resume_after_seconds, request.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result


@app.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict[str, str]:
    _ensure_mutable()
    try:
        orchestrator.set_task_status(task_id, TaskStatus.CANCELED, "用户已取消该任务。")
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "canceled"}


@app.post("/api/projects/cancel-all")
async def cancel_all_projects(root_id: str | None = Query(default=None)) -> dict[str, int | str]:
    _ensure_mutable()
    try:
        canceled = await orchestrator.cancel_all(root_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "canceled", "count": canceled}


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def stream():
        async for payload in event_bus.subscribe():
            yield f"data: {payload}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


def _resolve_web_dist() -> Path | None:
    """找前端产物目录（`web/dist`）。找不到返回 None（**不报错**）。

    "找不到"是**正常状态**，不是出错：开发时只有 Vite dev server、没跑过
    `pnpm build`，这时 dist 不存在很正常。所以这里返回 None 让调用方静默跳过，
    而不是抛异常把 API 拖起来不了。
    """
    if not orchestrator.settings.serve_web_dist:
        return None
    configured = orchestrator.settings.web_dist_dir
    candidates = [Path(configured)] if configured else []
    # `ROOT_DIR` 是仓库根（config 里算好的）。也探一下 `api/` 旁边，
    # 因为有人会把 dist 拷到部署目录旁边。
    candidates.append(ROOT_DIR / "web" / "dist")
    candidates.append(Path(__file__).resolve().parents[2] / "web" / "dist")
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


# 前端产物挂到 `/`。**必须放在所有 `/api/*` 路由之后**：
# Starlette 按注册顺序匹配，先挂根路径会把后面的 API 全吃掉。
#
# `html=True` 让未知路径回落到 `index.html`。这个前端**没有路由库**
# （单页、无深链接），所以不需要更复杂的 SPA 回退逻辑。
_web_dist = _resolve_web_dist()
if _web_dist is not None:
    app.mount("/", StaticFiles(directory=str(_web_dist), html=True), name="web")
    print(f"[web] 托管前端产物：{_web_dist}", file=sys.stderr)
else:
    # 说清"为什么界面打不开"比静默 404 好。这条最常见的原因是没跑过 pnpm build。
    print(
        "[web] 未找到前端产物（web/dist/index.html），只提供 API。"
        "要用界面：cd web && pnpm build，或另起 Vite dev server（pnpm dev）。",
        file=sys.stderr,
    )
