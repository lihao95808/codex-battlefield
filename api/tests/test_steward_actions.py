"""管家动作层的判据（`docs/spec-steward.md` 的审批段）。

管家从"只能建议"变成"你批准后能执行"之后，最危险的不是它判断错，
而是**执行路径上的每一个缺口都不可见**。本文件钉的是这些缺口：

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| A1 | 白名单与执行器**一一对应** | 白名单里有、实现没有 → 界面上摆一个点了必失败的按钮 |
| A2 | 非法建议**不入库**，且说明原因 | 用户看见按钮、点了才失败，还看不出为什么 |
| A3 | 只读**能提议、不能执行** | "只读"标签变成谎言，或者建议功能被一起废掉 |
| A4 | 必须**先批准**才能执行 | 运行期一句调用就能动树，审批层等于不存在 |
| A5 | 执行失败**也要落库**（`failed` + 原因） | 用户以为做了、其实没做 —— 最糟的一种状态 |
| A6 | 不能**重复决定**同一个动作 | 连点两次会把已取消的东西再取消一遍 |
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import steward_actions
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.models import StewardAction
from app.steward_actions import (
    EXECUTORS,
    ActionError,
    known_actions,
    propose,
    run_approved,
)
from app.steward_capabilities import CAPABILITIES


def _settings(**overrides):
    base = get_settings()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update(overrides)
    return base.__class__(**fields)


@pytest.fixture()
def db_session():
    """干净的动作表。动作用例断言"库里有几条"，所以每条用例都得自己起跑线。"""
    init_db()
    with SessionLocal() as db:
        db.query(StewardAction).delete()
        db.commit()
        yield db
        db.query(StewardAction).delete()
        db.commit()


# ---------- A1 白名单与执行器一一对应 ----------


def test_a1_every_executable_action_has_an_executor() -> None:
    """白名单里的每个动作都必须有实现。

    这是**编码疏漏**检测，不是用户错误检测：写好了能力声明却忘了写执行器，
    表现是"界面上有个按钮，点了永远报错"。测试里必须炸，别等到用户点。
    """
    missing = sorted(set(known_actions()) - set(EXECUTORS))
    assert not missing, f"白名单里有动作没有执行器：{missing}"


def test_a1_no_executor_for_an_unknown_action() -> None:
    """反向：也不许有执行器没进白名单。

    那种执行器永远不会被提议、也永远不该被调用 —— 留着就是一条
    "绕开白名单直接执行"的后门。两个方向都钉，才是真的对应。
    """
    extra = sorted(set(EXECUTORS) - set(known_actions()))
    assert not extra, f"有执行器不在白名单里（绕过了审批）：{extra}"


def test_a1_whitelist_is_exactly_the_mutating_capabilities() -> None:
    """动作集必须**等于**能力目录里 mutating 的那些，不多不少。

    分成两份手写清单迟早漂移，而漂移的后果是"文档说不可用、实际能执行"。
    """
    expected = {cap.name for cap in CAPABILITIES if cap.mutating}
    assert set(known_actions()) == expected


# ---------- A2 非法建议不入库 ----------


def test_a2_hallucinated_action_is_rejected_with_the_valid_options(db_session) -> None:
    """模型幻觉出的动作名不能入库，并且要把**可用选项**回给用户。"""
    queued, rejected = propose(
        db_session,
        [{"action": "explode_tree", "target": "root-1", "why": "看起来很乱"}],
        read_only=False,
    )

    assert queued == [], "幻觉动作被写进了库"
    assert len(rejected) == 1
    assert "explode_tree" in rejected[0]
    # 说明原因还不够 —— 得说清**能**用什么，否则用户没法纠正
    assert "kick_root" in rejected[0], f"没列出可用动作：{rejected[0]}"
    assert db_session.query(StewardAction).count() == 0


def test_a2_missing_target_is_rejected_before_it_can_fail_later(db_session) -> None:
    """需要目标却给了空目标 —— 现在就拒，别等批准之后才失败。

    批准之后才失败是最坏的顺序：用户已经点了"批准"，心理上认为事情做完了。
    """
    queued, rejected = propose(
        db_session,
        [{"action": "retry_node", "target": "  ", "why": "重试一下"}],
        read_only=False,
    )

    assert queued == []
    assert len(rejected) == 1
    assert "target" in rejected[0] or "目标" in rejected[0]


def test_a2_valid_proposal_is_persisted_as_pending(db_session) -> None:
    """对照：合法建议确实入库，且初始状态是 `pending`（= 等用户决定）。"""
    queued, rejected = propose(
        db_session,
        [{"action": "kick_root", "target": "root-1", "why": "树停了", "risk": "会开始花钱"}],
        read_only=False,
    )

    assert len(queued) == 1
    assert rejected == []
    row = db_session.get(StewardAction, queued[0].id)
    assert row.status == steward_actions.PENDING
    assert row.target == "root-1"
    assert row.risk == "会开始花钱", "risk 必须原样留给界面显示"


def test_a2_one_bad_proposal_does_not_discard_the_good_ones(db_session) -> None:
    """一批里混了一个幻觉动作，合法的那些**不能**被连坐丢掉。"""
    queued, rejected = propose(
        db_session,
        [
            {"action": "kick_root", "target": "root-1", "why": "树停了"},
            {"action": "nonsense", "target": "x", "why": "?"},
            {"action": "cancel_all", "target": "测试用：收工", "why": "收工"},
        ],
        read_only=False,
    )

    assert [row.action for row in queued] == ["kick_root", "cancel_all"]
    assert len(rejected) == 1


def test_a2_every_proposable_action_rejects_an_empty_target(db_session) -> None:
    """**每一个**可提议的动作，给了空目标都必须被拒。

    为什么逐个核对而不是抽查（2026-10-15 实测踩到）：`start_task` 的目标描述
    走的是 `target` 槽位，而它的 `params` 里写的是 `goal` 而不是 `task_id`。
    旧实现用 `"{task_id}" in summary` 推断"要不要目标"，于是把 `start_task`
    判成"不需要目标"，放行了一条 `target=""` 的提议。

    真实模型随后**真的**这么提了（响应里 `proposed_action_ids: [1]`，
    target 为空），而 `_exec_start_task` 必然拒绝空 goal ——
    也就是说用户在界面上看到了一个"点了必定失败"的按钮。

    这个判据是"全称"的：将来新增任何动作，只要漏写 `target_label`，这里就会响。
    """
    empty_targets = []
    for name in sorted(known_actions()):
        queued, rejected = propose(db_session, [{"action": name, "target": "", "why": "x"}], read_only=False)
        if queued:
            empty_targets.append(name)
        assert rejected, f"{name} 空目标竟然没被拒"

    assert not empty_targets, (
        f"这些动作接受空目标，会在界面上变成'点了必失败'的按钮：{empty_targets}"
    )


def test_a2_start_task_carries_the_goal_label_in_the_rejection(db_session) -> None:
    """拒掉 `start_task` 时要说清该补什么（"目标描述"），不然用户没法纠正。"""
    queued, rejected = propose(
        db_session, [{"action": "start_task", "target": "", "why": "起一棵树"}], read_only=False
    )

    assert queued == [], "空 goal 的 start_task 被写进了库（界面会显示一个必失败的按钮）"
    assert len(rejected) == 1
    assert "目标描述" in rejected[0], f"没说清该补什么：{rejected[0]}"


def test_a2_start_task_with_a_goal_is_accepted(db_session) -> None:
    """对照：`start_task` 带上目标描述就能入队 —— 证明上一条不是恒真。"""
    queued, rejected = propose(
        db_session,
        [{"action": "start_task", "target": "只改一个文件的错别字", "why": "小步验证"}],
        read_only=False,
    )

    assert len(queued) == 1
    assert rejected == []
    assert queued[0].target == "只改一个文件的错别字"


# ---------- A3 只读：能提议、不能执行 ----------


def test_a3_read_only_still_allows_proposing(db_session) -> None:
    """只读禁的是**执行**，不是**建议** —— 禁掉建议等于把管家废了。"""
    queued, rejected = propose(
        db_session,
        [{"action": "cancel_all", "target": "测试用：看起来失控了", "why": "看起来失控了"}],
        read_only=True,
    )

    assert len(queued) == 1, "只读模式下连建议都不让提了"
    assert rejected == []


def test_a3_execution_itself_is_blocked_by_the_endpoint_in_read_only(client: TestClient) -> None:
    """端点上：只读模式批准动作必须 **423**，不能真的执行。

    这是独立于 `frozen_mode` 的一道闸。只看 `frozen_mode` 的话，默认配置下
    "只读的管家"批准完就能动树 —— 界面上写着只读、实际能执行。
    """
    init_db()
    with SessionLocal() as db:
        db.query(StewardAction).delete()
        rows, _ = propose(
            db, [{"action": "cancel_all", "target": "测试用：收工", "why": "测试"}], read_only=True
        )
        action_id = rows[0].id

    response = client.post(f"/api/steward/actions/{action_id}/approve")

    assert response.status_code == 423, response.text
    with SessionLocal() as db:
        row = db.get(StewardAction, action_id)
        assert row.status == steward_actions.PENDING, "被拒的批准却改了状态"


# ---------- A4 必须先批准 ----------


def test_a4_running_an_unapproved_action_is_refused(db_session) -> None:
    """没批准就执行 —— 必须拒绝。审批层不能是"建议性"的。"""
    rows, _ = propose(db_session, [{"action": "cancel_all", "target": "测试用：收工", "why": "x"}], read_only=False)
    action_id = rows[0].id

    with pytest.raises(ActionError):
        asyncio.run(run_approved(db_session, _ExplodingOrchestrator(), action_id))


def test_a4_refused_execution_leaves_the_row_pending(db_session) -> None:
    """被拒的执行不能顺手把状态改成 executed/failed。"""
    rows, _ = propose(db_session, [{"action": "cancel_all", "target": "测试用：收工", "why": "x"}], read_only=False)
    action_id = rows[0].id

    with pytest.raises(ActionError):
        asyncio.run(run_approved(db_session, _ExplodingOrchestrator(), action_id))

    db_session.expire_all()
    assert db_session.get(StewardAction, action_id).status == steward_actions.PENDING


def test_a4_second_validation_rechecks_the_whitelist(db_session) -> None:
    """执行时的**第二道校验**：批准之后白名单若不再认这个动作，必须拒。

    提议与批准之间可能过了很久（用户第二天才点），而执行是最危险的时刻 ——
    它必须自己判，不能只信"当时验过"。
    """
    rows, _ = propose(db_session, [{"action": "kick_root", "target": "root-1", "why": "x"}], read_only=False)
    row = rows[0]
    steward_actions.decide(db_session, row.id, approve=True)

    # 模拟"提议之后白名单变了"：把这条动作从映射里摘掉
    saved = EXECUTORS.pop("kick_root")
    try:
        ok, message = asyncio.run(run_approved(db_session, _ExplodingOrchestrator(), row.id))
    finally:
        EXECUTORS["kick_root"] = saved

    assert ok is False, "白名单已不认这个动作，却执行成功了"
    assert "白名单" in message


# ---------- A5 失败也要留痕 ----------


def test_a5_execution_failure_is_recorded_not_swallowed(db_session) -> None:
    """执行抛异常 → 状态 `failed`、`error` 有原因、`executed_at` 有时间。

    这一条是整个审批层最要紧的判据：用户点了"批准"，界面上必须能区分
    "做成了"和"批准了但没做成"。没有这条，失败会静默，而用户以为做完了。
    """
    rows, _ = propose(db_session, [{"action": "cancel_all", "target": "测试用：收工", "why": "x"}], read_only=False)
    action_id = rows[0].id
    steward_actions.decide(db_session, action_id, approve=True)

    ok, message = asyncio.run(run_approved(db_session, _ExplodingOrchestrator(), action_id))

    assert ok is False
    db_session.expire_all()
    row = db_session.get(StewardAction, action_id)
    assert row.status == steward_actions.FAILED
    assert "BoomError" in row.error, f"异常类型没记下来：{row.error}"
    assert row.executed_at is not None, "失败也必须留执行时间"
    assert row.decided_at is not None, "批准时间丢了"


def test_a5_success_records_the_result_message(db_session) -> None:
    """对照：成功时状态 `executed`、`result` 是人话说明，且 `error` 为空。"""
    spy = _AsyncOrchestrator()
    rows, _ = propose(db_session, [{"action": "cancel_all", "target": "测试用：收工", "why": "x"}], read_only=False)
    action_id = rows[0].id
    steward_actions.decide(db_session, action_id, approve=True)

    ok, message = asyncio.run(run_approved(db_session, spy, action_id))

    assert ok is True, message
    assert spy.completed == ["cancel_all"], "成功路径上 orchestrator 没被调起来"
    db_session.expire_all()
    row = db_session.get(StewardAction, action_id)
    assert row.status == steward_actions.EXECUTED
    assert row.result
    assert not row.error


# ---------- A6 不能重复决定 ----------


def test_a6_deciding_twice_is_refused(db_session) -> None:
    """已决定过的动作不能再决定一次 —— 否则连点两下会再执行一遍。"""
    rows, _ = propose(db_session, [{"action": "cancel_all", "target": "测试用：收工", "why": "x"}], read_only=False)
    action_id = rows[0].id
    steward_actions.decide(db_session, action_id, approve=False)

    with pytest.raises(ActionError):
        steward_actions.decide(db_session, action_id, approve=True)


def test_a6_reject_does_not_execute(db_session) -> None:
    """拒绝就是拒绝：状态变 `rejected`，绝不去碰 orchestrator。"""
    rows, _ = propose(db_session, [{"action": "cancel_all", "target": "测试用：收工", "why": "x"}], read_only=False)
    action_id = rows[0].id
    steward_actions.decide(db_session, action_id, approve=False)

    db_session.expire_all()
    row = db_session.get(StewardAction, action_id)
    assert row.status == steward_actions.REJECTED
    assert row.executed_at is None, "拒绝了却留下执行时间"
    assert row.result == ""


def test_a6_deciding_a_missing_action_is_an_error_not_a_crash(db_session) -> None:
    """找不到的动作给 4xx 语义的 `ActionError`，不是让 KeyError 冒出去变成 500。"""
    with pytest.raises(ActionError):
        steward_actions.decide(db_session, 10_000_000, approve=True)


# ---------- A7 每个执行器都真的把 orchestrator 调起来了 ----------
#
# 为什么要有这一组（2026-10-15 实测）：`cancel_all` 与 `set_budget_guard` 在
# orchestrator 上是 **async**。执行器里漏掉 `await` 时 ——
#   * 不抛异常；
#   * `run_approved` 记 `executed`（成功！）；
#   * 唯一痕迹是一条 RuntimeWarning: coroutine ... was never awaited，
#     而 pytest 默认只把它记成 warning，测试照样绿。
# 也就是"界面说你批准的事做成了，实际上什么都没发生"—— 执行层最坏的一种错。
#
# 所以判据不能只断言 `ok is True`，必须断言**副作用真的发生了**。


def test_a7_cancel_all_actually_awaits_the_orchestrator(db_session) -> None:
    """`cancel_all` 是协程：没 await 就会"报告成功但什么都没做"。"""
    spy = _AsyncOrchestrator()
    rows, _ = propose(db_session, [{"action": "cancel_all", "target": "测试用：收工", "why": "x"}], read_only=False)
    action_id = rows[0].id
    steward_actions.decide(db_session, action_id, approve=True)

    ok, message = asyncio.run(run_approved(db_session, spy, action_id))

    assert ok is True, message
    assert spy.completed == ["cancel_all"], (
        f"cancel_all 没有被真正执行（侧作用没发生）：completed={spy.completed}"
    )
    assert "3" in message, f"没有把真实返回的条数带回来（说明拿到的是协程对象）：{message}"


def test_a7_set_budget_guard_actually_awaits_the_orchestrator(db_session) -> None:
    """同理：`set_budget_guard` 也是协程。"""
    spy = _AsyncOrchestrator()
    rows, _ = propose(
        db_session, [{"action": "set_budget_guard", "target": "测试用：额度不足", "why": "x"}], read_only=False
    )
    action_id = rows[0].id
    steward_actions.decide(db_session, action_id, approve=True)

    ok, message = asyncio.run(run_approved(db_session, spy, action_id))

    assert ok is True, message
    assert spy.completed == ["set_budget_guard"], (
        f"set_budget_guard 没有被真正执行：completed={spy.completed}"
    )


def test_a7_every_async_orchestrator_call_is_awaited(db_session) -> None:
    """把**所有**动作跑一遍，逐个确认侧作用发生。

    逐个而不是抽查：执行器是九条独立的分支，漏一个就是一条静默失效的路径。
    """
    from app.steward_actions import execute

    for name in sorted(known_actions()):
        spy = _AsyncOrchestrator()
        # start_task 需要非空 goal；其余需要非空 target
        target = "做一个能跑的东西" if name == "start_task" else "root-1"
        asyncio.run(execute(spy, name, target))
        assert spy.completed, f"动作 {name} 没有产生任何副作用（漏了 await？）"


# ---------- 端点接线 ----------


def test_endpoints_list_pending_and_report_count(client: TestClient) -> None:
    """收件箱端点：`pending_count` 要和列出来的待办对得上。"""
    init_db()
    with SessionLocal() as db:
        db.query(StewardAction).delete()
        db.commit()
        propose(db, [{"action": "kick_root", "target": "root-1", "why": "树停了"}], read_only=True)

    body = client.get("/api/steward/actions").json()

    assert body["pending_count"] == 1, body
    pending = [row for row in body["actions"] if row["status"] == "pending"]
    assert len(pending) == 1
    assert body["actions"][0]["why"] == "树停了", "why 必须给到界面（用户靠它决定）"


def test_endpoint_reject_marks_the_row(client: TestClient) -> None:
    """拒绝端点在只读模式下**也要能用** —— 拒绝不改变任何工程状态。"""
    init_db()
    with SessionLocal() as db:
        db.query(StewardAction).delete()
        db.commit()
        rows, _ = propose(db, [{"action": "cancel_all", "target": "测试用：收工", "why": "x"}], read_only=True)
        action_id = rows[0].id

    response = client.post(f"/api/steward/actions/{action_id}/reject")

    assert response.status_code == 200, response.text
    assert response.json()["action"]["status"] == "rejected"


def test_endpoint_alerts_flag_pending_actions(client: TestClient) -> None:
    """有待办动作时，提醒里必须有 `action` 级的那一条（界面靠它提示用户）。"""
    init_db()
    with SessionLocal() as db:
        db.query(StewardAction).delete()
        db.commit()
        propose(db, [{"action": "kick_root", "target": "root-1", "why": "x"}], read_only=True)

    body = client.get("/api/steward/alerts").json()
    kinds = [alert["kind"] for alert in body["alerts"]]
    assert "pending_actions" in kinds, f"有待办却没提醒：{kinds}"


def test_endpoint_alerts_are_empty_of_action_kind_when_nothing_pends(client: TestClient) -> None:
    """对照：没有待办时不该出现 `pending_actions`（证明上一条不是恒真）。"""
    init_db()
    with SessionLocal() as db:
        db.query(StewardAction).delete()
        db.commit()

    body = client.get("/api/steward/alerts").json()
    kinds = [alert["kind"] for alert in body["alerts"]]
    assert "pending_actions" not in kinds, f"没有待办却报了待办：{kinds}"


@pytest.fixture()
def client() -> TestClient:
    """复用 `test_steward.py` 的做法：mock runner（conftest 已钉住）。"""
    from app.main import app

    init_db()
    return TestClient(app)


# ---------- 替身 ----------


class _ExplodingOrchestrator:
    """任何执行都炸 —— 用来验证失败路径，而不是让测试真的去动树。"""

    def __getattr__(self, name: str):
        def _boom(*args, **kwargs):
            raise BoomError(f"替身故意失败：{name}")

        return _boom


class BoomError(RuntimeError):
    """故意用的异常类型：断言 `error` 里留下了**类型名**，而不只是一句话。"""


class _AsyncOrchestrator:
    """**异步**替身：只有协程体真的被执行，才记一笔副作用。

    为什么替身的方法必须写成 `async`：漏掉 `await` 在**同步**替身上完全看不出来 ——
    `orchestrator.cancel_all()` 会立刻返回一个整数，执行器"成功"，测试照样绿。
    写成协程之后，漏 `await` 的调用不会执行协程体，`completed` 就是空的。

    这个区别是实测出来的：`_exec_cancel_all` / `_exec_set_budget_guard` /
    `_exec_hibernate` 三个执行器原先都漏了 `await`，而对同步替身的测试全绿。
    """

    def __init__(self) -> None:
        self.completed: list[str] = []

    async def cancel_all(self, root_id: str | None = None) -> int:
        self.completed.append("cancel_all")
        return 3

    async def set_budget_guard(
        self,
        root_id: str | None,
        resume_after_seconds: int,
        reason: str,
        *,
        hibernate_tree: bool = False,
    ) -> dict:
        self.completed.append("set_budget_guard")
        return {}

    async def kick_tree(self, root_id: str, runner_mode: str | None = None) -> dict:
        self.completed.append("kick_tree")
        return {}

    async def retry_task(self, task_id: str) -> None:
        self.completed.append("retry_task")

    async def start_one_shot_project(
        self, goal: str, runner_mode: str | None = None, auto_run: bool = True
    ) -> str:
        self.completed.append("start_one_shot_project")
        return "root-new"

    async def hibernate_tree(
        self, root_id: str, resume_after_seconds: int, reason: str
    ) -> dict:
        self.completed.append("hibernate_tree")
        return {}

    def set_task_status(self, task_id: str, status, message: str) -> None:
        """这个是**同步**的（实测签名）—— 所以它不需要 await，替身也不该是协程。"""
        self.completed.append("set_task_status")
