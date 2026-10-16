/**
 * 术语表数据（P2-6）。
 *
 * ## 为什么这些词值得单独列一张表
 *
 * 它们要么是**自造词**（债务、验收面、上浮、下沉），要么是**改了日常含义的词**
 * （宪法、验收、阻塞、休眠、预授权）。两类都会让新使用者按日常意思理解，
 * 而**错理解不会有任何报错** —— 表现是"我按它说的做了，结果全错"。
 *
 * `interop` 一段专门写"跟日常用法的差别"，这正是这张表存在的理由：
 * 只写定义的话，读者仍然会用自己的先验去填空。
 *
 * ## 维护规矩
 *
 * 每条术语必须能指到仓库里的一份 SPEC（`spec`）。指不到说明它还没被定义清楚 ——
 * 那就该补 SPEC，而不是补这张表，否则这张表会变成第二份真源。
 *
 * 术语名两种语言都显示（`zh` / `en` 字段都在）：外部使用者要能对着中文界面
 * 和英文 SPEC 认出同一个东西。
 */
import type { Lang } from './i18n';

export interface TermText {
  zh: string;
  en: string;
}

export interface Term {
  /** 术语名 —— 两种语言都展示，不随界面语言切换。 */
  name: TermText;
  /** 在这个系统里的确切含义。 */
  meaning: TermText;
  /** **跟日常用法的差别** —— 这张表的重点。 */
  interop: TermText;
  /** 定义它的 SPEC（相对仓库根的路径）。 */
  spec: string;
}

/** 取一段双语文本在当前语言下的那一半。 */
export function pick(value: TermText, lang: Lang): string {
  return lang === 'zh' ? value.zh : value.en;
}

export const TERMS: Term[] = [
  {
    name: { zh: '债务', en: 'obligation' },
    meaning: {
      zh: '挂在父节点上的一条"欠账"。父节点能否标为完成，由**入站债务是否为空**决定，而不由子节点状态决定。',
      en: 'A ledger entry attached to a parent node. Whether the parent may be marked done is decided by **whether its inbound obligations are settled** — not by its children\u2019s statuses.',
    },
    interop: {
      zh: '不是日常的"待办事项"：它不可协商、不会自动过期，且只有 6 条指定通路能清掉，清债必须留痕。',
      en: 'Not an everyday "todo": it is non-negotiable, never expires on its own, and only six designated paths may settle it — and settling always leaves a trace.',
    },
    spec: 'docs/spec-parent-child-obligations.md',
  },
  {
    name: { zh: '上浮 / 下沉', en: 'upward / downward propagation' },
    meaning: {
      zh: '下沉＝父节点的约束与宪法版本按结构继承给子节点；上浮＝子节点的问题变成父节点的一条债务，逐层往上长。',
      en: 'Downward: a parent\u2019s constraints and constitution version are inherited structurally by its children. Upward: a child\u2019s problem becomes one obligation on the parent, growing one level at a time.',
    },
    interop: {
      zh: '不是"汇报"：两者都必须是结构化字段。禁止文本重述 —— 文本重述会逐跳失真。',
      en: 'Neither is "reporting": both must be structured fields. Prose restatement is forbidden — it drifts at every hop.',
    },
    spec: 'docs/spec-parent-child-obligations.md',
  },
  {
    name: { zh: '阻塞', en: 'blocked' },
    meaning: {
      zh: '一种**终态**：这个节点不会再自己往下跑了。要继续，必须有人显式 retry，或由系统建出修复子任务。',
      en: 'A **terminal** state: this node will not continue on its own. Progress needs an explicit retry, or a repair child created for it.',
    },
    interop: {
      zh: '不等于"失败"：失败是这一次没跑成；阻塞是"当前条件下跑不成，停在这里等你"。',
      en: 'Not "failed": failing means this attempt did not work; blocked means "it cannot work under current conditions, so it stops and waits for you".',
    },
    spec: 'docs/spec-parent-child-obligations.md',
  },
  {
    name: { zh: '验收面', en: 'acceptance surface' },
    meaning: {
      zh: '宪法里显式声明的那组文件（测试、校验脚本、门禁），用来判定"这棵树到底做成了没有"。',
      en: 'The explicitly declared set of files (tests, check scripts, gates) in the constitution that decides whether the tree actually succeeded.',
    },
    interop: {
      zh: '它不是"测试覆盖率"：面被冻结成内容指纹，产出方改了它立刻成为阻断债 —— 防的是"自己改考卷"。',
      en: 'Not "test coverage": the surface is frozen as content digests, so a producer editing it becomes a blocking obligation instantly — it guards against grading your own exam.',
    },
    spec: 'docs/spec-verification-independence.md',
  },
  {
    name: { zh: '验收 / 验证', en: 'acceptance / verification' },
    meaning: {
      zh: '验证是叶子级（worker 的产物过不过），验收是父节点级（一组子节点的整合在整体上成不成立）。',
      en: 'Verification is leaf-level (does the worker\u2019s artifact pass). Acceptance is parent-level (do a set of children, integrated, hold up as a whole).',
    },
    interop: {
      zh: '两者都不是"跑一遍测试"：它们是独立的子进程，各有独立提示词，不复用 worker 的会话。',
      en: 'Neither is "run the tests once": both are separate subprocesses with their own prompts, and neither reuses the worker\u2019s session.',
    },
    spec: 'docs/spec-verification-independence.md',
  },
  {
    name: { zh: '宪法', en: 'constitution' },
    meaning: {
      zh: '一棵树的约束与判据集合。**锁定后不可改**，只能由人发起修订，修订必须带 actor 与方向。',
      en: 'A tree\u2019s constraints and criteria. Once locked it cannot be edited; only a human-initiated amendment may change it, and it must carry an actor and a direction.',
    },
    interop: {
      zh: '不是"项目规范文档"：收紧修订会让已完成的节点进入再验证队列，放宽修订**不许**洗白已阻塞/失败的节点。',
      en: 'Not a "style guide": a tightening amendment sends already-done nodes back for re-verification, and a loosening amendment must **not** launder blocked or failed nodes.',
    },
    spec: 'docs/spec-constitution-amendment.md',
  },
  {
    name: { zh: '证据', en: 'evidence' },
    meaning: {
      zh: '产物指纹（`sha256:` 文件 / `tree:` 目录）、命令退出码、跨版本回归基线。',
      en: 'Artifact digests (`sha256:` for files, `tree:` for directories), command exit codes, cross-version regression baselines.',
    },
    interop: {
      zh: '不是"agent 说它做了"：自称成功但产物声明为空会**直接判失败**，根本进不到验收。',
      en: 'Not "the agent said it did it": a self-declared success with no artifact declarations fails outright and never reaches verification.',
    },
    spec: 'docs/spec-requirement-traceability.md',
  },
  {
    name: { zh: '回归基线', en: 'regression baseline' },
    meaning: {
      zh: '上一版验收通过时记下的产物指纹，用来回答"这一版有没有把上一版悄悄弄坏"。',
      en: 'Digests recorded when the previous version passed acceptance, answering "did this version quietly break the last one".',
    },
    interop: {
      zh: '不是"上次的测试结果"：比的是**产物内容**，且只在版本推进时比（同版返工不比）。',
      en: 'Not "last run\u2019s test results": it compares **artifact contents**, and only when the version advanced (same-version rework is not compared).',
    },
    spec: 'docs/spec-requirement-traceability.md',
  },
  {
    name: { zh: '工作树', en: 'worktree' },
    meaning: {
      zh: '每个节点一个独立的 git worktree，互不改同一个工作目录。',
      en: 'One independent git worktree per node, so no two nodes edit the same working directory.',
    },
    interop: {
      zh: '不是"临时分支"：它在**终态之后**才回收，且回收前必须验明产物已归档 —— 按指纹，不按文件名。',
      en: 'Not a "scratch branch": it is recycled only after the node reaches a terminal state, and only once its artifacts are proven archived — by digest, not by filename.',
    },
    spec: 'docs/spec-task-isolation.md',
  },
  {
    name: { zh: '状态根', en: 'state home' },
    meaning: {
      zh: '工具自己的状态（库、运行记录、工作树、快照）存放的位置，**在仓库之外**。',
      en: 'Where the tool keeps its own state (database, run records, worktrees, snapshots) — **outside the repository**.',
    },
    interop: {
      zh: '不是"工程目录"：工程可以被删掉重克隆，状态根不动 —— 把两者分开就是为了这件事。',
      en: 'Not a "project directory": the project can be deleted and re-cloned while the state home stays put. That separation is the whole point.',
    },
    spec: 'docs/spec-state-project-decoupling.md',
  },
  {
    name: { zh: '休眠 / 额度闸', en: 'hibernate / budget guard' },
    meaning: {
      zh: '到达额度上限时**停止派发新工作**并把整棵树挂起，到点或有放行后自动恢复。',
      en: 'On reaching the spend ceiling, new work stops being dispatched and the whole tree is suspended, resuming on schedule or on an explicit release.',
    },
    interop: {
      zh: '不是"统计花了多少"：它是发车前的硬闸。统计只是事后读数，拦不住任何东西。',
      en: 'Not "how much was spent": it is a hard gate in front of dispatch. Accounting is a reading; it stops nothing.',
    },
    spec: 'docs/spec-resource-governance.md',
  },
  {
    name: { zh: '检查点 / 账本', en: 'checkpoint / ledger' },
    meaning: {
      zh: '把"跑到哪了"落成可恢复的落盘记录，跨进程重启后接着跑。',
      en: 'Durable records of how far the run got, so a process restart can pick it up.',
    },
    interop: {
      zh: '不是"日志"：日志给人看，账本给恢复流程读 —— 它必须能被机器无歧义地解释。',
      en: 'Not "logs": logs are for humans, the ledger is read by the recovery path — it has to be unambiguous to a machine.',
    },
    spec: 'docs/checkpoint-git-anchor.md',
  },
  {
    name: { zh: '看门狗', en: 'watchdog' },
    meaning: {
      zh: '无人盯着时替你发现"卡住了"的角色：心跳、僵死进程、额度闸状态。',
      en: 'The role that notices "this is stuck" while nobody is watching: heartbeats, dead processes, budget-gate state.',
    },
    interop: {
      zh: '不是"重试器"：它先判"有没有活可看"，宁可漏报也不假警 —— 假警会让人把告警整个关掉。',
      en: 'Not a "retryer": it first checks whether there is anything alive to look at, and prefers missing a problem over raising a false alarm — false alarms get alerting turned off.',
    },
    spec: 'docs/spec-watchdog-steward.md',
  },
  {
    name: { zh: '管家', en: 'steward' },
    meaning: {
      zh: '跟你对话、把目标投递成任务树、并把结论讲回来的角色。',
      en: 'The role that talks with you, submits goals as a task tree, and reports conclusions back.',
    },
    interop: {
      zh: '**默认只读**：它只能提议，执行要你批。这是有意的权限取舍，不是"还没做完"。',
      en: '**Read-only by default**: it may only propose; execution needs your approval. That is a deliberate permission tradeoff, not unfinished work.',
    },
    spec: 'docs/spec-steward.md',
  },
  {
    name: { zh: '预授权', en: 'pre-authorization' },
    meaning: {
      zh: '在明确范围与期限内，允许低风险动作（重试、续跑）不经逐次确认。',
      en: 'Within a stated scope and time window, low-risk actions (retry, resume) may skip per-action confirmation.',
    },
    interop: {
      zh: '它放宽的是**确认次数**，不是权限边界；高风险动作（取消、全停）永不可预授权。',
      en: 'It relaxes the **number of confirmations**, not the permission boundary; high-risk actions (cancel, stop everything) can never be pre-authorized.',
    },
    spec: 'ROADMAP.md P4-4',
  },
];
