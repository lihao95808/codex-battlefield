from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from .config import ROOT_DIR
from .database import get_db, init_db
from .events import event_bus
from .models import ConstitutionRecord, TaskStatus
from .orchestrator import orchestrator
from .schemas import (
    ApiRestartDrainResponse,
    BudgetGuardRequest,
    BudgetGuardResponse,
    ConstitutionAmendRequest,
    ConstitutionDraftRequest,
    ConstitutionResponse,
    ConstitutionUpdateRequest,
    HibernateRequest,
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
    yield


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


def _ensure_mutable() -> None:
    if orchestrator.settings.frozen_mode:
        raise HTTPException(status_code=423, detail="Codex Battlefield is running in frozen read-only mode")


@app.get("/api/machine-safety")
def machine_safety() -> dict:
    path = orchestrator.settings.workspace_dir / "machine_safety.json"
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
