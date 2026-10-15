/**
 * 管家（steward）的对话组件。
 *
 * ## 为什么用 @assistant-ui/react
 *
 * 聊天交互里那些"看着简单、写起来琐碎"的部分（流式状态、自动滚动、
 * Enter 提交、空输入禁用、消息列表渲染）不该自己重写一遍。这个库提供了
 * **primitives 层（无样式）**，视觉全部交给我们 —— 正好配本项目"手写 CSS、
 * 不引入 Tailwind"的技术栈（它的依赖里确实没有 Tailwind）。
 *
 * ## 为什么不用它的 Vercel AI SDK 运行时
 *
 * 我们的后端是自己的 Python/FastAPI，不是 AI SDK 的形状。所以用
 * `useLocalRuntime` 配一个自定义 `ChatModelAdapter`：只实现一个 `run`，
 * 库里其余能力（消息状态、滚动、提交）照常工作。
 *
 * ## 管家不能自己执行
 *
 * 权限模型是三段：**管家提议 → 用户批准 → 服务端执行**。
 * 管家永远不能自己动树。所以这里的 `ActionInbox` 不是"它做过什么"的日志，
 * 而是"等你决定"的收件箱；每个动作都把 `why`（为什么建议）和 `risk`
 * （风险）摆在按钮旁边 —— 让人在知道代价的前提下点。
 *
 * ## 只读
 *
 * 管家默认只读。只读禁的是**执行**，不影响**提议**：建议是它最有价值的
 * 产出之一。所以只读模式下收件箱照样有人排队，但按钮是禁用的，
 * 并且写明怎么打开。
 */
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useLocalRuntime,
} from '@assistant-ui/react';
import type { ChatModelAdapter, ThreadMessage } from '@assistant-ui/react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  approveStewardAction,
  clearStewardMessages,
  fetchStewardActions,
  fetchStewardMessages,
  rejectStewardAction,
  sendStewardMessage,
  subscribeStewardAlerts,
} from './api';
import type {
  StateLayoutResponse,
  StewardActionRecord,
  StewardAlert,
  StewardAlertPush,
  StewardChatResponse,
  StewardMessageRecord,
} from './types';

/** 把一条 assistant-ui 的消息拼成纯文本（我们只发文本，不做多模态）。 */
function messageText(message: ThreadMessage): string {
  return message.content
    .map((part) => (part.type === 'text' ? part.text : ''))
    .join('')
    .trim();
}

function readOnlyBanner(layout: StateLayoutResponse | null): string {
  if (!layout) return '正在读取状态…';
  if (!layout.bound) {
    return '未绑定工程：管家看到的是空状态。先用 battlefield-project use <工程目录> 绑定。';
  }
  return `工程：${layout.project_root ?? '(未知)'}`;
}

/** 动作状态 -> 中文标签 + 语义色键（颜色写在 CSS 里，这里只给语义）。 */
const ACTION_STATUS: Record<string, { label: string; tone: string }> = {
  pending: { label: '等你决定', tone: 'wait' },
  approved: { label: '已批准', tone: 'ok' },
  executed: { label: '已执行', tone: 'ok' },
  rejected: { label: '已拒绝', tone: 'muted' },
  failed: { label: '执行失败', tone: 'bad' },
};

function actionStatus(status: string): { label: string; tone: string } {
  return ACTION_STATUS[status] ?? { label: status, tone: 'muted' };
}

function shortTime(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString();
}

/**
 * 对话历史来自**我们自己的数据库**（`/api/steward/messages`），不是浏览器本地。
 * 这样刷新页面、换浏览器、甚至换台机器，对话都还在 —— 而且可审计。
 */
function useHistorySeed(version: number): { seed: ThreadMessage[] | null; readOnly: boolean | null } {
  const [rows, setRows] = useState<StewardMessageRecord[] | null>(null);
  const [readOnly, setReadOnly] = useState<boolean | null>(null);

  useEffect(() => {
    let alive = true;
    fetchStewardMessages()
      .then((payload) => {
        if (!alive) return;
        setRows(payload.messages);
        setReadOnly(payload.read_only_mode);
      })
      .catch(() => {
        if (!alive) return;
        setRows([]);
        setReadOnly(null);
      });
    return () => {
      alive = false;
    };
  }, [version]);

  const seed = useMemo(() => {
    if (!rows) return null;
    return rows
      .filter((row) => row.role === 'user' || row.role === 'steward')
      .map<ThreadMessage>((row) => {
        const id = `hist-${row.id}`;
        // 主动汇报在库里是 user 侧的系统指令 + steward 侧的回复，两者都带
        // `autonomous: true`。界面上要能区分"它自己说的"和"我问出来的" ——
        // 否则用户会疑惑"我什么时候问过这个"。
        // 这里把标记塞进 assistant-ui 认可的 `metadata.custom`，
        // 后面渲染时再读回来（它只保证保留这个字段）。
        const meta = row.metadata ?? {};
        const custom = {
          autonomous: meta.autonomous === true,
          trigger: typeof meta.trigger === 'string' ? meta.trigger : '',
        };
        // 这里只造最小可用形状：assistant-ui 需要 role + content，
        // 其余字段（时间戳、状态）用它的约定填齐。
        const parts = [{ type: 'text' as const, text: row.content }];
        if (row.role === 'user') {
          return {
            id,
            role: 'user',
            content: parts,
            createdAt: row.created_at ? new Date(row.created_at) : new Date(),
            attachments: [],
            metadata: { custom },
          } as unknown as ThreadMessage;
        }
        return {
          id,
          role: 'assistant',
          content: parts,
          createdAt: row.created_at ? new Date(row.created_at) : new Date(),
          status: { type: 'complete', reason: 'stop' },
          metadata: { custom, steps: [] },
        } as unknown as ThreadMessage;
      });
  }, [rows]);

  return { seed, readOnly };
}

/**
 * **值得打扰用户的信号**。
 *
 * 由服务端规则判定（不是让管家定时醒来"看看有没有问题"）：规则便宜、确定、
 * 可测，而定时唤醒 LLM 又慢又贵。这里只负责显示。
 *
 * `reason` 是"为什么现在推给你" —— 一条"3 个节点失败"到底是刚出现的、
 * 还是已经挂了两小时，用户必须能分辨，否则提醒会被学会无视。
 */
function AlertStrip({ alerts, reason }: { alerts: StewardAlert[]; reason: string }) {
  if (!alerts.length) return null;
  return (
    <div className="steward-alerts-wrap">
      {reason && <p className="steward-alerts-reason">{reason}</p>}
      <ul className="steward-alerts" aria-label="提醒">
        {alerts.map((alert) => (
          <li key={alert.kind} className={`steward-alert steward-alert-${alert.level}`}>
            {alert.message}
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * **动作收件箱**：管家提议、你批准、服务端执行。
 *
 * 三件事在界面上必须同时可见，否则用户会误判：
 *   1. `why` —— 它为什么建议这个（不然只能盲信）；
 *   2. `risk` —— 代价是什么（cancel 不可逆，尤其要说）；
 *   3. `error` —— 批准了但**执行失败**的原因。
 *
 * 第 3 点最重要：库里把 `decided_at` 与 `executed_at` 分开写，就是为了让
 * "你以为做了、其实没做"这件事没法藏起来。界面必须把它显示出来。
 */
function ActionInbox({
  actions,
  readOnly,
  busyId,
  onDecide,
}: {
  actions: StewardActionRecord[];
  readOnly: boolean;
  busyId: number | null;
  onDecide: (action: StewardActionRecord, approve: boolean) => void;
}) {
  if (!actions.length) return null;

  const pending = actions.filter((row) => row.status === 'pending');
  const settled = actions.filter((row) => row.status !== 'pending');

  const renderRow = (row: StewardActionRecord) => {
    const status = actionStatus(row.status);
    const waiting = row.status === 'pending';
    const busy = busyId === row.id;
    return (
      <li key={row.id} className={`steward-action steward-action-${status.tone}`}>
        <div className="steward-action-head">
          <code className="steward-action-name">{row.action}</code>
          {row.target && <span className="steward-action-target">{row.target}</span>}
          <span className={`steward-action-status steward-tone-${status.tone}`}>{status.label}</span>
        </div>
        {row.why && <p className="steward-action-why">{row.why}</p>}
        {row.risk && <p className="steward-action-risk">风险：{row.risk}</p>}
        {row.result && <p className="steward-action-result">{row.result}</p>}
        {row.error && <p className="steward-action-error">没能做成：{row.error}</p>}
        {row.executed_at && (
          <p className="steward-action-time">执行于 {shortTime(row.executed_at)}</p>
        )}

        {waiting && (
          <div className="steward-action-actions">
            <button
              type="button"
              className="steward-approve"
              disabled={readOnly || busy}
              onClick={() => onDecide(row, true)}
              title={readOnly ? '管家只读模式下不能代为执行' : '批准并立即执行'}
            >
              {busy ? '执行中…' : '批准并执行'}
            </button>
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() => onDecide(row, false)}
            >
              拒绝
            </button>
          </div>
        )}
      </li>
    );
  };

  return (
    <div className="steward-inbox" aria-label="管家动作收件箱">
      <div className="steward-inbox-head">
        <h4>待你决定的动作</h4>
        {pending.length > 0 && <span className="steward-inbox-count">{pending.length}</span>}
      </div>
      {readOnly && pending.length > 0 && (
        <p className="steward-inbox-note">
          只读模式下可以提议、不能执行。要让它能执行，设
          <code>CODEX_BATTLEFIELD_STEWARD_READ_ONLY=0</code> 后重启 API。
        </p>
      )}
      {pending.length > 0 && <ul className="steward-action-list">{pending.map(renderRow)}</ul>}
      {settled.length > 0 && (
        <details className="steward-actions-settled">
          <summary>已决定的（{settled.length}）</summary>
          <ul className="steward-action-list">{settled.map(renderRow)}</ul>
        </details>
      )}
    </div>
  );
}

export function StewardPanel({
  layout,
  onOpen,
}: {
  layout: StateLayoutResponse | null;
  onOpen?: () => void;
}) {
  const [historyVersion, setHistoryVersion] = useState(0);
  const { seed, readOnly } = useHistorySeed(historyVersion);

  const [actions, setActions] = useState<StewardActionRecord[]>([]);
  const [alerts, setAlerts] = useState<StewardAlert[]>([]);
  const [alertReason, setAlertReason] = useState<string>('');
  const [streamState, setStreamState] = useState<'connecting' | 'live' | 'error'>('connecting');
  const [busyId, setBusyId] = useState<number | null>(null);
  const [inboxError, setInboxError] = useState<string>('');

  /**
   * 收件箱的刷新函数放进 ref，好让**适配器**在管家答完之后调用它。
   *
   * 为什么必须这样绕一下：适配器在 `useMemo` 里，闭包捕获的是**那一刻**的
   * `refreshInbox`；直接引用会把它变成 `useMemo` 的依赖，runtime 随之重建、
   * 对话被清空。ref 让"当前那一版"始终可寻址，而适配器保持稳定。
   *
   * ref 只在 effect 里写（不在渲染期间写）：渲染期间写 ref 是 React 明令
   * 禁止的（`react-hooks/refs`）。effect 在挂载后、用户能发出第一条消息
   * 之前就跑完，所以这个窗口期是空的。
   */
  const refreshRef = useRef<() => void>(() => {});

  /**
   * 动作收件箱是**拉取**的，提醒是**推送**的。
   *
   * 为什么分开：动作是"等你决定"的待办，它变化时一定有明确的触发点
   * （管家答完、你批准/拒绝）—— 那些点我们已经知道，直接拉一次最省事，
   * 也避免了"推送到了但列表还是旧的"这种不一致。提醒则是**异步冒出**的
   * （树在后台跑着跑着就失败了），没有触发点可挂，必须服务端推过来。
   */
  const refreshInbox = useCallback(() => {
    fetchStewardActions()
      .then((payload) => {
        setActions(payload.actions);
        setInboxError('');
      })
      .catch((error: unknown) => {
        setInboxError(error instanceof Error ? error.message : String(error));
      });
  }, []);

  useEffect(() => {
    refreshRef.current = refreshInbox;
    refreshInbox();
  }, [refreshInbox, historyVersion]);

  /**
   * 挂上提醒推送流。`EventSource` 自己负责断线重连，所以这里不写退避逻辑 ——
   * 只维护"当前是不是活的"，好在界面上如实标注（而不是假装一切正常）。
   */
  useEffect(() => {
    const unsubscribe = subscribeStewardAlerts(
      (push: StewardAlertPush) => {
        setAlerts(push.alerts);
        setAlertReason(push.reason);
        setStreamState('live');
        // 提醒里出现"有动作等你决定"往往意味着管家刚提了动作 —— 顺手刷新收件箱。
        if (push.worth_pushing.some((alert) => alert.kind === 'pending_actions')) {
          refreshRef.current();
        }
      },
      (message: string) => {
        setStreamState('error');
        setAlertReason(message);
      },
    );
    return unsubscribe;
  }, []);

  /**
   * 适配器：把我们自己的后端做成 assistant-ui 的"模型"。
   *
   * 它**只发最后一条用户消息**，历史由后端从库里取（后端才是历史的真相来源）。
   * 这样界面刷新、重新挂载都不会把上下文弄丢或重复。
   */
  const adapter = useMemo<ChatModelAdapter>(
    () => ({
      async run({ messages }) {
        const lastUser = [...messages].reverse().find((message) => message.role === 'user');
        const text = lastUser ? messageText(lastUser) : '';
        if (!text) {
          return { content: [{ type: 'text', text: '（空消息）' }] };
        }

        let response: StewardChatResponse;
        try {
          response = await sendStewardMessage(text);
        } catch (error) {
          const reason = error instanceof Error ? error.message : String(error);
          return {
            content: [{ type: 'text', text: `管家没能回答：${reason}` }],
          };
        }

        // 管家刚提了新动作 —— 收件箱要**立刻**反映出来，不能让用户自己刷新
        // 才发现有东西等他决定。
        refreshRef.current();

        const detail: string[] = [];
        if (response.situation) detail.push(`局势：${response.situation}`);
        if (response.confidence) detail.push(`把握：${response.confidence}`);
        const proposed = response.proposed_actions ?? [];
        if (proposed.length) {
          detail.push(
            '建议（见下面的收件箱）：' +
              proposed
                .map((item) => `${item.action}${item.target ? `(${item.target})` : ''} —— ${item.why}`)
                .join('；'),
          );
        }
        const rejected = response.rejected_actions ?? [];
        if (rejected.length) {
          // 服务端拒掉的建议要说出来：不然用户以为"它建议了但没显示"，
          // 而真相是"模型幻觉了一个不存在的动作"。
          detail.push('被拒的建议：' + rejected.join('；'));
        }

        return {
          content: [{ type: 'text', text: [response.reply, ...detail].join('\n\n') }],
          metadata: { custom: { confidence: response.confidence, readOnly: response.read_only_mode } },
        };
      },
    }),
    [],
  );

  const runtime = useLocalRuntime(adapter, seed ? { initialMessages: seed } : undefined);

  const onClear = async () => {
    await clearStewardMessages();
    setHistoryVersion((value) => value + 1);
  };

  const onDecide = async (row: StewardActionRecord, approve: boolean) => {
    setBusyId(row.id);
    try {
      const decision = approve
        ? await approveStewardAction(row.id)
        : await rejectStewardAction(row.id);
      if (approve && !decision.ok) {
        // 批准成功但**执行失败** —— 这是最需要被看见的一种结果。
        setInboxError(`动作 #${row.id} 批准了但没能执行：${decision.message ?? '未知原因'}`);
      } else {
        setInboxError('');
      }
    } catch (error) {
      setInboxError(error instanceof Error ? error.message : String(error));
    } finally {
      setBusyId(null);
      refreshInbox();
    }
  };

  return (
    <section className="steward-panel" aria-label="管家">
      <div className="steward-head">
        <h3>管家</h3>
        <span className="steward-head-badges">
          <span
            className={`steward-stream steward-stream-${streamState}`}
            title={
              streamState === 'live'
                ? '提醒推送已连上（服务端只在提醒变化时说话）'
                : streamState === 'connecting'
                  ? '正在连接提醒推送…'
                  : '提醒推送断开；界面不再自动更新，请检查 API 是否在跑'
            }
          >
            {streamState === 'live' ? '推送在线' : streamState === 'connecting' ? '连接中' : '推送断开'}
          </span>
          <span className={readOnly === false ? 'steward-mode-write' : 'steward-mode-read'}>
            {readOnly === false ? '可执行' : '只读'}
          </span>
        </span>
      </div>
      <p className="steward-scope">
        {readOnlyBanner(layout)}
        {readOnly !== false && (
          <span> · 它只读状态、给建议，<b>动树要你批准</b></span>
        )}
      </p>

      <AlertStrip alerts={alerts} reason={alertReason} />

      {inboxError && <p className="steward-inbox-error">{inboxError}</p>}

      <ActionInbox
        actions={actions}
        readOnly={readOnly !== false}
        busyId={busyId}
        onDecide={onDecide}
      />

      {seed === null ? (
        <p className="steward-loading">正在读取对话…</p>
      ) : (
        <AssistantRuntimeProvider key={`steward-${historyVersion}`} runtime={runtime}>
          <ThreadPrimitive.Root className="steward-thread">
            <ThreadPrimitive.Viewport className="steward-viewport">
              <ThreadPrimitive.Empty>
                <p className="steward-empty">
                  问它"现在什么状态"、"哪个节点卡住了"、"下一步该做什么"。
                </p>
              </ThreadPrimitive.Empty>
              <ThreadPrimitive.Messages>
                {({ message }) => {
                  const custom = (message.metadata?.custom ?? {}) as {
                    autonomous?: boolean;
                    trigger?: string;
                  };
                  const proactive = custom.autonomous === true;
                  return (
                    <div
                      className={`steward-msg steward-msg-${message.role}${proactive ? ' steward-msg-proactive' : ''}`}
                    >
                      <div className="steward-msg-role">
                        {message.role === 'user'
                          ? proactive
                            ? '系统（触发主动汇报）'
                            : '你'
                          : proactive
                            ? '管家（主动汇报）'
                            : '管家'}
                      </div>
                      <div className="steward-msg-body">
                        <MessagePrimitive.Parts />
                      </div>
                      {proactive && custom.trigger && (
                        <div className="steward-msg-trigger">触发原因：{custom.trigger}</div>
                      )}
                    </div>
                  );
                }}
              </ThreadPrimitive.Messages>
            </ThreadPrimitive.Viewport>

            <ComposerPrimitive.Root className="steward-composer">
              <ComposerPrimitive.Input
                className="steward-input"
                placeholder="跟管家说点什么…"
                rows={2}
                autoFocus
              />
              <div className="steward-composer-actions">
                <button type="button" className="secondary" onClick={onClear} title="清空对话（不动任务树）">
                  清空对话
                </button>
                <ComposerPrimitive.Send className="steward-send">发送</ComposerPrimitive.Send>
              </div>
            </ComposerPrimitive.Root>
          </ThreadPrimitive.Root>
        </AssistantRuntimeProvider>
      )}
      {onOpen && (
        <button type="button" className="secondary steward-refresh" onClick={onOpen}>
          重新读取状态
        </button>
      )}
    </section>
  );
}
