/**
 * 看门狗面板（`App.tsx` → `panels.tsx` → 这里，P5-2）。
 */
import { displaySlotCount, formatDateTime, formatDuration, formatPercent, secondsUntil } from './format';
import { autoConcurrencyLabel, guardianCheckLabels, guardianRiskLabels, guardianStatusLabels, statusLabel, watchdogActionLabel } from './labels';
import type { GuardianCheck, StateLayoutResponse, TaskStatus, WatchdogStateResponse } from './types';
import { Clock, Pause, RefreshCw, ShieldCheck } from 'lucide-react';

export function WatchdogPanel({
  state,
  layout,
  busy,
  result,
  onInstall,
  onRunOnce,
  onToggleDrain,
}: {
  state: WatchdogStateResponse | null;
  layout: StateLayoutResponse | null;
  busy: boolean;
  result: string | null;
  onInstall: () => void;
  onRunOnce: () => void;
  onToggleDrain: () => void;
}) {
  if (!state) {
    return (
      <section className="watchdog-panel" aria-label="自动续跑状态">
        <div className="watchdog-head">
          <h3>
            <Clock size={15} />
            自动续跑
          </h3>
          <span>等待状态</span>
        </div>
        <p>正在等待 API 返回看门狗状态。</p>
        <div className="watchdog-actions">
          <button className="secondary" onClick={onInstall} disabled={busy} type="button" title="安装或更新 Windows 定时巡检和登录自启钩子">
            <ShieldCheck size={14} />
            安装钩子
          </button>
          <button className="secondary" onClick={onRunOnce} disabled={busy} type="button" title="立即执行一次本地 watchdog 巡检">
            <RefreshCw size={14} />
            巡检一次
          </button>
        </div>
        {result && <p className="watchdog-result">{result}</p>}
      </section>
    );
  }
  const secondsLeft = secondsUntil(state.resume_at);
  const actionLabel = watchdogActionLabel(state.recommended_action) ?? state.recommended_action;
  const rootStatus = state.root_status ? (statusLabel(state.root_status) ?? state.root_status) : '无根节点';
  const budgetValue =
    state.agent_burst_limit > 0 ? `${state.agent_burst_used}/${state.agent_burst_work_limit}` : '不限';
  const reserveValue = state.agent_burst_limit > 0 ? `${state.agent_burst_reserve} 次` : '未启用';
  const goalModeValue = state.goal_mode ? '长跑' : '保守';
  const budgetWarningLabel =
    state.budget_warning_level === 'paused'
      ? '已暂停'
      : state.budget_warning_level === 'warning'
        ? '预警'
        : state.budget_warning_level === 'sleeping'
          ? '休眠中'
        : state.budget_warning_level === 'goal'
          ? '长跑'
        : state.budget_warning_level === 'resource_pressure'
          ? '资源压力'
          : '正常';
  const scheduledTaskValue = state.scheduled_task_exists ? '已安装' : '可安装';
  const startupHookValue =
    state.logon_task_exists
      ? '计划任务已安装'
      : state.startup_hook_exists || state.startup_hook_mode === 'startup_folder'
        ? 'Startup 已安装'
        : '可安装';
  const intervalValue = `${state.watchdog_interval_minutes} 分钟`;
  const apiRestartPending = state.api_restart_pending === true;
  const apiRestartDrainArmed = state.api_restart_drain_armed === true;
  const apiRestartFlagPath = state.api_restart_flag_path || '等待新版 API 返回安全重启路径';
  const apiRestartDrainPath = state.api_restart_drain_flag_path || '等待新版 API 返回维护排空路径';
  const supportsApiRestartDrain = typeof state.api_restart_drain_armed === 'boolean' && Boolean(state.api_restart_drain_flag_path);
  const apiRestartStatusLabel = !supportsApiRestartDrain
    ? '待新版接口'
    : apiRestartDrainArmed
      ? '维护排空'
      : apiRestartPending
        ? '等待空闲'
        : '已加载';
  const quotaObservation =
    state.codex_quota_observability || '旧 API 暂未返回额度观测字段，等待安全重启后刷新。';
  const rawActiveSlots = state.codex_slots_active ?? state.active_agents;
  const slotCapacityValue = state.codex_slots_capacity ?? rawActiveSlots;
  const activeSlots = displaySlotCount(rawActiveSlots, slotCapacityValue);
  const activeStatusNodes = state.active_agent_statuses ?? state.active_agents;
  const observedActiveUpperBound = state.active_agents;
  const slotCapacity = state.codex_slots_capacity ?? '?';
  const effectiveSlotNote = rawActiveSlots > activeSlots ? ` · 有效占用 ${rawActiveSlots}（含预约）` : '';
  const quotaRetryAfter =
    typeof state.codex_quota_retry_after_seconds === 'number' && state.codex_quota_retry_after_seconds > 0
      ? formatDuration(state.codex_quota_retry_after_seconds)
      : null;
  const quotaResetAt = state.codex_quota_reset_at ? formatDateTime(state.codex_quota_reset_at) : null;
  const quotaDetail = quotaResetAt
    ? `真实退避信号：${quotaResetAt}${quotaRetryAfter ? ` 后可重试，等待 ${quotaRetryAfter}` : ''}`
    : quotaObservation === 'codex_cli_has_no_direct_quota_or_5h_reset_api' ||
        quotaObservation === 'codex_cli_no_direct_quota_endpoint'
      ? 'CLI 没有暴露可直接读取的额度刷新时间；L4 只使用实际限流输出、退避字段和当前队列压力。'
      : quotaObservation;
  const healthSummary = state.codex_health_summary || '旧 API 暂未返回预检摘要，等待安全重启后刷新。';
  const codexHealthLabel =
    state.codex_health_status === 'ok'
      ? '正常'
      : state.codex_health_status === 'warning'
        ? '有告警'
        : state.codex_health_status === 'resource_block'
          ? '限流等待'
          : state.codex_health_status === 'blocked'
            ? '已拦截'
            : state.codex_health_exists
              ? state.codex_health_status
              : '未预检';
  const hookState = state.should_resume
    ? '可触发'
    : rawActiveSlots > 0 || activeStatusNodes > 0
      ? '观察中'
      : state.hibernating > 0
        ? '已布防'
        : state.active_frontier > 0
          ? '待巡检'
          : '空闲';
  const guardianStatus = state.guardian_status ?? 'unknown';
  const guardianRisk = state.guardian_risk_level ?? 'watch';
  const guardianChecks = (state.guardian_checks ?? []) as GuardianCheck[];
  const guardianSummary = state.guardian_summary || 'L4 监护者正在等待下一次状态刷新。';
  const guardianMode = state.guardian_mode || state.recommended_action;
  const fuelMode = state.l4_fuel_mode || 'pending';
  const fuelPhase = state.l4_fuel_phase || 'pending';
  const fuelTarget =
    typeof state.l4_fuel_target === 'number'
      ? state.l4_fuel_target
      : (state.codex_slots_capacity ?? state.active_agents);
  const fuelCapacity =
    typeof state.l4_fuel_current_capacity === 'number'
      ? state.l4_fuel_current_capacity
      : (state.codex_slots_capacity ?? fuelTarget);
  const fuelSlots = `${displaySlotCount(rawActiveSlots, fuelCapacity)}/${fuelCapacity}`;
  const fuelScore = formatPercent(state.l4_fuel_score);
  const fuelQueuePressure = formatPercent(state.l4_fuel_queue_pressure);
  const fuelFrontierPressure = formatPercent(state.l4_fuel_frontier_pressure);
  const fuelTimePressure = formatPercent(state.l4_fuel_time_pressure);
  const fuelRecommendedTarget =
    typeof state.l4_fuel_recommended_target === 'number' ? state.l4_fuel_recommended_target : fuelTarget;
  const fuelOpenFrontier =
    typeof state.l4_fuel_open_frontier === 'number' ? state.l4_fuel_open_frontier : state.active_frontier;
  const fuelNextDeadline = formatDateTime(state.l4_fuel_next_deadline_local ?? state.l4_fuel_deadline_local);
  const fuelMinutes = formatDuration(
    typeof state.l4_fuel_minutes_to_deadline === 'number' ? state.l4_fuel_minutes_to_deadline * 60 : null,
  );
  const fuelQuotaReset = state.l4_fuel_quota_reset_at
    ? formatDateTime(state.l4_fuel_quota_reset_at)
    : (quotaResetAt ?? 'not observed');
  const guardianStatePath = state.guardian_state_path || '等待新版 API 写入 l4_guardian_state.json';
  return (
    <section className={`watchdog-panel ${state.should_resume ? 'ready' : ''}`} aria-label="自动续跑状态">
      <div className="watchdog-head">
        <h3>
          <Clock size={15} />
          L4 监护者
        </h3>
        <span>{actionLabel}</span>
      </div>
      {/*
        状态布局：**当前进程到底在用哪个工程/哪个库**。
        状态与工程解耦之后最危险的错误是"看着是这个工程、其实在改另一个"——
        路径都对、也不报错，只有库里的内容不对。摆在这里就让错位一眼可见。
        快照与 active.json 不一致时明确提示要重启（决策 D1：不热切库）。
      */}
      {layout && (
        <div
          className={`state-layout-strip ${layout.snapshot_matches_active_file ? '' : 'stale'}`}
          aria-label="状态布局"
        >
          <code>
            {layout.bound
              ? `工程：${layout.project_root ?? '（未知）'}`
              : '未绑定工程（每工程状态回退仓库内 workspace）'}
          </code>
          {layout.bound && <code>库：{layout.db_path ?? layout.database_url}</code>}
          <code>状态根：{layout.state_home}</code>
          {!layout.snapshot_matches_active_file && (
            <code className="state-layout-warning">
              ⚠️ 磁盘上的 active.json 已改为
              {layout.active_file_root || layout.active_file_key || '（空）'}
              ，但本进程仍在用启动时的工程 —— 请重启 API（不会热切库）
            </code>
          )}
        </div>
      )}
      <div className={`guardian-strip ${guardianRisk}`}>
        <div>
          <small>
            {(state.guardian_level ?? 'L4')} · {guardianStatusLabels[guardianStatus] ?? guardianStatus} · 风险：
            {guardianRiskLabels[guardianRisk] ?? guardianRisk}
          </small>
          <strong>{guardianSummary}</strong>
        </div>
        <span>{guardianMode}</span>
      </div>
      {guardianChecks.length > 0 && (
        <div className="guardian-checks" aria-label="L4 监护检查">
          {guardianChecks.slice(0, 6).map((check) => (
            <article className={`guardian-check ${check.status}`} key={check.key}>
              <span>{check.label}</span>
              <b>{guardianCheckLabels[check.status] ?? check.status}</b>
              <p>{check.detail}</p>
            </article>
          ))}
        </div>
      )}
      <div className="l4-fuel-panel" aria-label="L4 fuel control">
        <div>
          <small>L4 fuel</small>
          <strong>{autoConcurrencyLabel(fuelMode)}</strong>
        </div>
        <div>
          <small>slots</small>
          <strong>{fuelSlots}</strong>
        </div>
        <div>
          <small>target</small>
          <strong>{fuelTarget}</strong>
        </div>
        <div>
          <small>suggested</small>
          <strong>{fuelRecommendedTarget}</strong>
        </div>
        <div>
          <small>phase</small>
          <strong>{fuelPhase}</strong>
        </div>
        <div>
          <small>score</small>
          <strong>{fuelScore}</strong>
        </div>
        <div>
          <small>queue</small>
          <strong>{fuelQueuePressure}</strong>
        </div>
        <div>
          <small>frontier</small>
          <strong>{fuelFrontierPressure}</strong>
        </div>
        <div>
          <small>open front</small>
          <strong>{fuelOpenFrontier}</strong>
        </div>
        <div>
          <small>time pressure</small>
          <strong>{fuelTimePressure}</strong>
        </div>
        <div>
          <small>next fuel point</small>
          <strong>{fuelNextDeadline}</strong>
        </div>
        <div>
          <small>time left</small>
          <strong>{fuelMinutes}</strong>
        </div>
        <div>
          <small>quota reset</small>
          <strong>{fuelQuotaReset}</strong>
        </div>
      </div>
      <div className="watchdog-grid">
        <div>
          <small>根节点状态</small>
          <strong>{rootStatus}</strong>
        </div>
        <div>
          <small>启动预算</small>
          <strong>{budgetValue}</strong>
        </div>
        <div>
          <small>预算预警</small>
          <strong>{budgetWarningLabel}</strong>
        </div>
        <div>
          <small>Goal 模式</small>
          <strong>{goalModeValue}</strong>
        </div>
        <div>
          <small>自动保留</small>
          <strong>{reserveValue}</strong>
        </div>
        <div>
          <small>钩子状态</small>
          <strong>{hookState}</strong>
        </div>
        <div>
          <small>巡检任务</small>
          <strong>{scheduledTaskValue}</strong>
        </div>
        <div>
          <small>巡检周期</small>
          <strong>{intervalValue}</strong>
        </div>
        <div>
          <small>登录自启</small>
          <strong>{startupHookValue}</strong>
        </div>
        <div>
          <small>Codex 预检</small>
          <strong>{codexHealthLabel}</strong>
        </div>
        <div>
          <small>API 更新</small>
          <strong>{apiRestartStatusLabel}</strong>
        </div>
        <div>
          <small>恢复时间</small>
          <strong>{formatDateTime(state.resume_at)}</strong>
        </div>
        <div>
          <small>剩余等待</small>
          <strong>{formatDuration(secondsLeft)}</strong>
        </div>
      </div>
      <div className="watchdog-actions">
        <button className="secondary" onClick={onInstall} disabled={busy} type="button" title="安装或更新 Windows 定时巡检和登录自启钩子">
          <ShieldCheck size={14} />
          安装/更新钩子
        </button>
        <button className="secondary" onClick={onRunOnce} disabled={busy} type="button" title="立即执行一次本地 watchdog 巡检；未到恢复时间时不会启动真实 Codex agent">
          <RefreshCw size={14} />
          手动巡检
        </button>
        {supportsApiRestartDrain && (
          <button
            className={`secondary ${apiRestartDrainArmed ? 'danger-soft' : ''}`}
            onClick={onToggleDrain}
            disabled={busy}
            type="button"
            title={
              apiRestartDrainArmed
                ? '取消维护排空；API 继续等待任务自然空闲后重启'
                : '武装维护排空；watchdog 会短暂休眠当前任务树，等活跃 agent 归零后安全重启 API'
            }
          >
            <Pause size={14} />
            {apiRestartDrainArmed ? '取消排空' : '维护排空'}
          </button>
        )}
      </div>
      {result && <p className="watchdog-result">{result}</p>}
      <p className={`budget-warning ${state.budget_warning_level}`}>{state.budget_warning_message}</p>
      {state.active_resource_pressure && (
        <p className="budget-warning resource_pressure">
          活跃 Agent 已报告限流/额度压力：{state.active_resource_pressure_reason || '等待下一次 watchdog 采样确认。'}
        </p>
      )}
      <p>{state.token_reserve_summary}</p>
      <p>
        Codex 额度观测：{quotaObservation === 'codex_cli_has_no_direct_quota_or_5h_reset_api'
          ? 'CLI 暂无直接读取 5 小时额度/刷新时间的接口，watchdog 依靠限流输出与退避时间自救。'
          : quotaObservation}
      </p>
      <p>最近预检：{healthSummary}</p>
      <p className="quota-observation">{quotaDetail}</p>
      {state.codex_quota_signal && <p className="quota-observation">Codex quota signal: {state.codex_quota_signal}</p>}
      <p className="quota-observation">
        Codex 额度：当前 Codex CLI 不暴露剩余额度数字；L4 监护者只能根据登录状态、限流日志、失败原因和退避时间推断是否该低功耗。
      </p>
      <p>{state.reason || '暂无休眠原因。'}</p>
      <code>监护快照：{guardianStatePath}</code>
      <code>
        休眠 {state.hibernating} · 真实槽位 {activeSlots}/{slotCapacity}{effectiveSlotNote} · 状态节点 {activeStatusNodes} · 观测上界 {observedActiveUpperBound}（不参与调速） · 前线 {state.active_frontier}
      </code>
      <code>
        API 安全重启：{supportsApiRestartDrain
          ? apiRestartPending
            ? `等待真实槽位和状态节点归零 · ${apiRestartFlagPath}`
            : '无待加载更新'
          : '旧 API 进程尚未暴露新版状态字段；watchdog 会继续按本地 flag 处理'}
      </code>
      <code>
        维护排空：{supportsApiRestartDrain
          ? apiRestartDrainArmed
            ? `已武装 · watchdog 可短暂休眠后重启 · ${apiRestartDrainPath}`
            : `未武装 · 可用面板按钮开启，或创建 ${apiRestartDrainPath}`
          : '等待 API 加载新版维护排空接口'}
      </code>
      <code>Codex 预检：{state.codex_health_checked_at ? formatDateTime(state.codex_health_checked_at) : '未执行'} · {state.codex_health_path}</code>
      <code>日志：{state.watchdog_log_path}</code>
      {state.scheduled_task_path && <code>周期巡检任务：{state.scheduled_task_path}</code>}
      {state.logon_task_path && <code>登录计划任务：{state.logon_task_path}</code>}
      {state.startup_hook_path && <code>登录钩子：{state.startup_hook_path}</code>}
      {(state.active_agent_diagnostics ?? []).length > 0 && (
        <div className="agent-diagnostics" aria-label="运行中 Agent 诊断">
          <strong>运行中 Agent 诊断</strong>
          {(state.active_agent_diagnostics ?? []).map((item) => (
            <article className={`agent-diagnostic ${item.severity}`} key={item.task_id}>
              <div>
                <span>
                  第 {item.depth} 层 · {item.phase} · {statusLabel(item.status as TaskStatus) ?? item.status}
                </span>
                <b>{item.title}</b>
              </div>
              <p>{item.summary}</p>
              <small>
                无新输出 {formatDuration(item.no_progress_seconds)} / 空闲看门狗 {formatDuration(item.idle_timeout_seconds)}
              </small>
              {item.markers.length > 0 && <small>信号：{item.markers.join(' / ')}</small>}
              {item.latest_progress_at && <small>最近输出：{formatDateTime(item.latest_progress_at)}</small>}
            </article>
          ))}
        </div>
      )}
      <code className="watchdog-command">安装/更新自启动：{state.watchdog_install_command}</code>
      <code className="watchdog-command">手动巡检一次：{state.watchdog_run_command}</code>
    </section>
  );
}
