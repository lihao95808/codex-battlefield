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
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import (
    obligation_index,
    steward,
    steward_actions,
    steward_proactive,
)
from . import steward_alerts as steward_alerts_mod
from .config import ROOT_DIR, machine_path_of
from .database import SessionLocal, get_db, init_db
from .events import event_bus
from .models import ConstitutionRecord, StewardAction, TaskStatus
from .orchestrator import orchestrator
from .runner import make_runner
from .state_layout import read_active_project
from .steward_capabilities import catalog as steward_catalog
from .steward_status import build_status as build_steward_status
from .schemas import (
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
