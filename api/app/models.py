from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TaskStatus(StrEnum):
    PLANNED = "planned"
    PLANNING = "planning"
    DECOMPOSING = "decomposing"
    QUEUED = "queued"
    WAITING_CHILDREN = "waiting_children"
    RUNNING = "running"
    WORKING = "working"
    VERIFYING = "verifying"
    ACCEPTING = "accepting"
    DONE = "done"
    REPAIR = "repair"
    BLOCKED = "blocked"
    FAILED = "failed"
    PAUSED = "paused"
    HIBERNATING = "hibernating"
    CANCELED = "canceled"


class RunKind(StrEnum):
    CONSTITUTION = "constitution"
    PLANNER = "planner"
    WORKER = "worker"
    VERIFIER = "verifier"
    ACCEPTANCE = "acceptance"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_STATUSES = {
    TaskStatus.DONE,
    TaskStatus.BLOCKED,
    TaskStatus.FAILED,
    TaskStatus.CANCELED,
}

STOPPED_STATUSES = {
    TaskStatus.PAUSED,
    TaskStatus.HIBERNATING,
}


class TaskNode(Base):
    __tablename__ = "task_nodes"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("task_nodes.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    depth: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PLANNED.value)
    scope_json: Mapped[str] = mapped_column(Text, default="[]")
    acceptance_json: Mapped[str] = mapped_column(Text, default="[]")
    node_meta_json: Mapped[str] = mapped_column(Text, default="{}")
    constitution_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    constitution_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    constitution_json: Mapped[str] = mapped_column(Text, default="{}")
    constitution_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    leaf_attempts: Mapped[int] = mapped_column(Integer, default=0)
    verification_failures: Mapped[int] = mapped_column(Integer, default=0)
    converted_from_leaf: Mapped[bool] = mapped_column(Boolean, default=False)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch: Mapped[str | None] = mapped_column(String(240), nullable=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    parent: Mapped["TaskNode | None"] = relationship(remote_side="TaskNode.id", back_populates="children")
    children: Mapped[list["TaskNode"]] = relationship(back_populates="parent", cascade="all, delete-orphan")
    runs: Mapped[list["RunRecord"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    events: Mapped[list["EventRecord"]] = relationship(back_populates="task", cascade="all, delete-orphan")


class ConstitutionRecord(Base):
    __tablename__ = "constitutions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    root_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    target_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    amendment_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    content_hash: Mapped[str] = mapped_column(String(80))
    markdown: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunRecord(Base):
    __tablename__ = "run_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("task_nodes.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.QUEUED.value)
    command: Mapped[str] = mapped_column(Text, default="")
    sandbox: Mapped[str] = mapped_column(String(64), default="read-only")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    logs_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_message_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    workdir: Mapped[str | None] = mapped_column(Text, nullable=True)
    constitution_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    constitution_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[TaskNode] = relationship(back_populates="runs")


class EventRecord(Base):
    __tablename__ = "event_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("task_nodes.id"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    task: Mapped[TaskNode | None] = relationship(back_populates="events")


class ObligationIndex(Base):
    """债务的**派生索引**（2026-09-22 决策，见 `docs/spec-parent-child-obligations.md` §13 Q3）。

    **它不是第二个真相来源**：真相仍然在 `TaskNode.node_meta["obligations"]` 里（那份是门禁读的），
    本表只是把它摊平成行，好让"按类型跨树聚合"能一句 SQL 查出来（JSON 里查不动）。
    因此：写入只由 `app/obligation_index.py` 的重建入口负责，且必须能用 `--check` 把
    "表 vs node_meta"的漂移报出来——漂移了就说明这张表不该被信。
    """

    __tablename__ = "obligation_index"

    id: Mapped[str] = mapped_column(String(200), primary_key=True)  # obligations.obligation_id
    root_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    node_id: Mapped[str] = mapped_column(String(64), index=True)
    depth: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    severity: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)  # open / settled
    evidence_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    recurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class StewardMessage(Base):
    """管家（常驻全局 agent）的对话记录。`docs/spec-steward.md`。

    ## 为什么存在库里、而不是另开一个全局库

    管家是"这个工程的全局管理者"——它管的就是当前工程的这棵树。
    存工程库里，切换工程就自然换了一份对话上下文，不会串台。
    另开全局库反而要自己去关联"这段对话属于哪个工程"。

    ## 为什么不做成树里的节点

    树的节点是**任务**：要被拆解、验收、受 300% 铁律约束、进并发槽位。
    管家是**对话**：它讨论树、不参与树。把它塞进树里会让"树"这个概念失真
    （凭空多一个不受验收约束的节点），也会让它跟 worker 抢并发槽位。

    ## 为什么存 role/content 两列就够

    不需要 tool_calls / parts 那套结构：管家第一版**只读**，
    它不调工具、只依据喂给它的状态快照说话。等放开写权限，
    动作会以 `proposed_actions` 记在 metadata 里（谁提的、用户批没批），
    那时再决定要不要拆成独立的动作表。
    """

    __tablename__ = "steward_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # user | steward | system
    role: Mapped[str] = mapped_column(String(16), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    # 结构化附注：管家的 situation / proposed_actions / confidence，以及用量等
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class StewardAction(Base):
    """管家提议的一个**动作**及其审批/执行足迹。`docs/spec-steward.md`。

    ## 为什么要有这张表（而不是只在回复里"说说建议"）

    管家是 LLM，判断会有错的时候。让它动树的真正风险不是"它可能做错"，
    而是**做错了没人拦、而且不可逆**。所以：

        管家提议 -> 用户批准 -> 服务端执行

    管家**永远**不能自己执行。这张表就是这条链路的账本：
    谁提的、为什么提、谁批的、什么时候执行的、结果如何。

    ## 为什么把 why/risk 存下来

    复盘时最需要知道的是"**当时基于什么建议了这一步**"。
    不存的话事后只看到一个 `cancel_node`，无从判断那是不是个合理的决定。

    ## 状态机

        pending --approve--> approved --(执行)--> executed | failed
           \\--reject--> rejected
        pending --(过期)--> expired
    """

    __tablename__ = "steward_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str] = mapped_column(String(200), default="")
    why: Mapped[str] = mapped_column(Text, default="")
    risk: Mapped[str] = mapped_column(Text, default="")
    # pending | approved | rejected | executed | failed | expired
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GoalDelivery(Base):
    """管家**投递目标**的记录（P4-1）。`docs/steward-delivery.md`。

    ## 要解决的不是"能不能起树"，而是"起完就断线"

    `start_task` 原先起完树就返回一个 root_id，之后管家与那棵树再无联系。用户于是
    只能自己去看板里翻："第几步了？卡在哪？" —— 而这两句话恰是管家唯一该回答的东西。

    所以交付要记两件事：**谁投的**（供事后归因）与**起点在哪**（`root_id`），
    然后由 `delivery.progress()` 把"第几步、卡在哪"实时读出来。

    ## 为什么记 `author` 而不是"当前用户"

    这个工具没有登录体系，"谁"在这里只能是**调用者自报的一个名字**。
    如实记录成 `author` 并允许为空 —— 假装知道用户是谁，比坦诚地不知道更糟。
    """

    __tablename__ = "goal_deliveries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    root_id: Mapped[str] = mapped_column(String(64), index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    # 谁投的（自报）。**允许为空** —— 没有登录体系时不该假知道。
    author: Mapped[str] = mapped_column(String(120), default="")
    # 靠什么起的（`steward_action` / `one_shot` / `manual`）
    source: Mapped[str] = mapped_column(String(32), default="steward_action", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class AttentionClaim(Base):
    """管家与看门狗之间"这件事我在处理"的记录（P4-3）。`docs/steward-watchdog-loop.md`。

    ## 为什么需要一张共享的账

    两边都能发现问题，但**谁也不知道对方已经看到了**。于是同一件事被处理两次：
    看门狗把僵死节点重新排队、管家同一时刻也建议重试。重复动作的代价不只是白干活 ——
    它会**掩盖真实信号**（"这个节点被重试了 3 次"看起来像节点不稳定，其实是两个组件
    各点了一次）。

    ## 为什么必须过期

    一条忘记释放的认领会让看门狗从此对那个目标闭眼，而看板上看起来一切正常。
    所以 `expires_at` 是必填的：**过期的认领意味着"没人管了，谁都可以接手"**，
    这与"还在处理"必须分得清。

    ## 与债务的区别

    债务回答"这棵树还欠什么"（给门禁看）；认领回答"这件事谁在处理"（给管家/看门狗看）。
    前者不会自动过期，后者必须过期。
    """

    __tablename__ = "attention_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 注意类型（封闭词表，见 `app/attention.KINDS`）
    kind: Mapped[str] = mapped_column(String(40), index=True)
    # 目标：节点 id / 树 id / 空串表示"整个工程"
    target: Mapped[str] = mapped_column(String(200), default="", index=True)
    # watchdog | steward | human
    actor: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    # **必填**：不过期的认领会变成永久阻塞
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by: Mapped[str] = mapped_column(String(120), default="")
    release_reason: Mapped[str] = mapped_column(Text, default="")


class IncidentConclusion(Base):
    """一次事故/一段长跑的**结论文档**（P4-5）。`docs/incident-conclusions.md`。

    ## 为什么需要它（而不是"翻事件表"）

    事件表记的是**发生了什么**，它回答不了"**当时为什么这么决定**"。
    而后者恰恰是复盘时唯一真正需要的东西 —— 事后看到一条 `cancel_node`，
    光看事件永远不知道那是不是个合理决定。

    所以结论是**人写的文档**（管家起草、你改），事实部分由代码从库里抽出来。

    ## 为什么把"已知"与"未知"分开存

    `facts_json` 里每段都可能是空的（读不到、或那类事件压根没发生过）。
    **空段被读成"没有"正是最坏的一种误读**（"没有决策点" vs "读不到决策点"）。
    所以 `incomplete_json` 单独列"这份草稿里哪些地方没能确定"，
    而 `authored` 一段是**人写的**，与代码抽出来的事实严格分开。

    ## 状态

        draft（草稿：事实已抽好，等人补"当时为什么"） -> final（定稿）

    `final` 只表示"人已经看过并定稿"，**不表示结论正确** —— 它是文档，不是判据。
    """

    __tablename__ = "incident_conclusions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    root_id: Mapped[str] = mapped_column(String(64), index=True)
    # 可选：结论针对某个节点（单节点事故）而不是整棵树。
    node_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    title: Mapped[str] = mapped_column(String(240), default="")
    # draft | final
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # 代码抽出来的事实（结构化，便于以后做统计）
    facts_json: Mapped[str] = mapped_column(Text, default="{}")
    # 这份草稿里**没能确定**的地方 —— 空段不等于"没有"
    incomplete_json: Mapped[str] = mapped_column(Text, default="[]")
    # 人写的"当时为什么这么决定"（Markdown）。**与事实严格分开。**
    authored: Mapped[str] = mapped_column(Text, default="")
    # 渲染好的完整文档（便于直接展示/导出，避免每次重算）
    markdown: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
