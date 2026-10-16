/**
 * 界面用的**词表**与标签映射（从 `App.tsx` 拆出，P5-2）。
 *
 * 为什么单独一个文件：这些是**纯数据 + 纯函数**，与 React 无关，而它们原来的位置
 * 在一个 2900 行的组件文件里 —— 想核对"某个状态显示成什么"必须翻过半个页面。
 *
 * 两条例矩：
 *
 * 1. **映射表是封闭的**：查不到时**原样返回输入**（不是"未知"，也不是空）。
 *    原样返回让"后端新增了一个状态"在界面上表现为那个英文标识符 ——
 *    比显示"未知"更有信息量（能看到它到底叫什么）。
 * 2. **事件严重度靠类型名判断，不靠维护一张全表**：事件类型有上百个且还会增长，
 *    枚举它们必然滞后。按关键词判会有误判的可能，但那比"新事件一律显示成普通"
 *    要好 —— 后者会让失败事件看起来像正常事件。
 */

/** 任务状态 → 中文。查不到原样返回。 */
export const statusLabels: Record<string, string> = {
  planned: '已规划',
  planning: '规划中',
  decomposing: '拆解中',
  queued: '排队中',
  waiting_children: '等待子任务',
  running: '执行中',
  working: '执行中',
  verifying: '验收中',
  accepting: '整合验收',
  done: '已完成',
  repair: '待修复',
  blocked: '已阻塞',
  failed: '失败',
  paused: '已暂停',
  hibernating: '休眠中',
  canceled: '已取消',
};

export function statusLabel(status: string): string {
  return statusLabels[status] ?? status;
}

export const runKindLabels: Record<string, string> = {
  constitution: '项目宪法',
  planner: '规划',
  worker: '执行',
  verifier: '验收',
  acceptance: '父节点验收',
};

export function runKindLabel(kind: string): string {
  return runKindLabels[kind] ?? kind;
}

export const runStatusLabels: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
};

export function runStatusLabel(status: string): string {
  return runStatusLabels[status] ?? status;
}

export const eventTypeLabels: Record<string, string> = {
  'project.started': '项目启动',
  'constitution.run': '宪法运行',
  'constitution.drafted': '宪法草案',
  'constitution.updated': '宪法更新',
  'constitution.locked': '宪法锁定',
  'planner.decision': '规划决策',
  'planner.corrected': '规划修正',
  'planner.repair_converted': '规划修复转递归',
  'planner.blocked_converted': '规划阻塞转递归',
  'acceptance.blocked_converted': '整合阻塞转递归',
  'task.created': '子任务创建',
  'task.retry': '重新排队',
  'task.planned': '已规划',
  'task.decomposing': '拆解中',
  'task.planning': '规划中',
  'task.queued': '排队中',
  'task.waiting_children': '等待子任务',
  'task.running': '执行中',
  'task.working': '执行中',
  'task.verifying': '验收中',
  'task.accepting': '整合验收',
  'task.done': '已完成',
  'task.leaf_misclassified': '叶子转父',
  'task.repeated_split_converted': '重复拆分破环',
  'parent.integration': '父节点整合',
  'task.repair': '待修复',
  'task.blocked': '已阻塞',
  'child.blocked_report': '子节点阻塞上报',
  'parent.child_block_reports': '父节点收到阻塞',
  'parent.retry_blocked_children': '父节点重试子节点',
  'task.failed': '失败',
  'task.paused': '已暂停',
  'task.hibernating': '休眠中',
  'task.hibernate_resume': '休眠恢复',
  'tree.hibernate': '任务树休眠',
  'tree.hibernate_resume': '任务树恢复',
  'task.canceled': '已取消',
  'worktree.created': '工作树创建',
  'worktree.fallback': '工作树降级',
};

export function eventTypeLabel(type: string): string {
  return eventTypeLabels[type] ?? type;
}

export type EventSeverity = 'danger' | 'warn' | 'ok' | 'info';

/**
 * 事件严重度（决定事件流里的配色）。
 *
 * **按关键词判，不维护全表**：事件类型上百个且还在增长，枚举必然滞后。
 * 顺序也重要 —— `failed`/`blocked` 先判，因为一个名字里同时含
 * `hibernate` 和 `failed` 的事件应当按危险显示。
 */
export function eventSeverity(type: string): EventSeverity {
  if (type.includes('failed') || type.includes('blocked')) {
    return 'danger';
  }
  if (
    type.includes('hibernate') ||
    type.includes('misclassified') ||
    type.includes('repair') ||
    type.includes('canceled')
  ) {
    return 'warn';
  }
  if (type.endsWith('.done') || type.includes('resume') || type.includes('locked')) {
    return 'ok';
  }
  return 'info';
}

/** 自动并发模式 → 中文。查不到原样返回（后端新增模式时能看到它叫什么）。 */
export function autoConcurrencyLabel(mode: string): string {
  return (
    {
      high_throughput: '高峰',
      balanced: '均衡',
      low_power: '低功率',
      hibernate_low_power: '休眠',
      resource_cooldown: '冷却',
      budget_preserve: '保留',
      fixed_single_agent: '固定并发',
      fixed_capacity: '固定并发',
      manual: '手动',
      pending: '待评估',
    }[mode] ?? mode
  );
}

export const watchdogActionLabels: Record<string, string> = {
  budget_guard_sleep: '预算守卫',
  resume: '到点恢复',
  resume_orphaned: '复活调度',
  observe_running: '运行观察',
  wait_for_resume_at: '等待自启',
  observe: '持续观察',
  idle: '空闲',
};

export function watchdogActionLabel(action: string): string {
  return watchdogActionLabels[action] ?? action;
}

export const guardianStatusLabels: Record<string, string> = {
  watching: '监护中',
  low_power: '低功耗',
  sleeping: '休眠守护',
  recovery_ready: '恢复就绪',
  ready: '可继续',
  needs_attention: '需要关注',
  idle: '空闲',
  unknown: '等待状态',
};

export const guardianRiskLabels: Record<string, string> = {
  ok: '正常',
  watch: '观察',
  warning: '预警',
  sleeping: '休眠',
  critical: '高风险',
};

export const guardianCheckLabels: Record<string, string> = {
  ok: '正常',
  warning: '预警',
  critical: '高风险',
  sleeping: '休眠',
  idle: '空闲',
};

/** 阻塞时父节点该做什么 → 中文。**默认给"人工判断"**（最保守的那一档）。 */
export function blockParentActionLabel(value: unknown): string {
  switch (String(value ?? 'manual_review')) {
    case 'wait_and_retry':
      return '等待池子恢复后重试';
    case 'recursive_recovery':
      return '转为递归修复';
    case 'recursive_recovery_static_wrapper':
      return '递归修复：改用静态 wrapper/替代验证';
    case 'skill_lookup_recovery':
      return '先用 Skill/KM 自救';
    case 'human_auth_required':
      return '人工授权/登录';
    case 'human_visual_review':
      return '人工视觉/审美验收';
    case 'pause_tool_fix':
      return '暂停：等待工具自身修复';
    case 'manual_review':
      return '人工判断';
    default:
      return String(value);
  }
}

/**
 * "这个阻塞能自动重试吗" → 中文。
 *
 * 先看后端给的布尔值，它说了算；没有时**按母动作推断** —— 推断不出来一律给
 * "需要人工审查"，因为**把不能重试的说成能重试**会让用户干等。
 */
export function blockRetryableLabel(value: unknown, parentAction: unknown): string {
  if (value) return '是，等待后可重试';
  const action = String(parentAction ?? '');
  if (action === 'pause_tool_fix') return '否，工具修复后自动恢复';
  if (action === 'skill_lookup_recovery') return '否，先尝试 Skill/KM 自救';
  if (action === 'human_auth_required') return '否，需要人工登录或授权';
  if (action === 'human_visual_review') return '否，需要人工视觉判断';
  if (action === 'recursive_recovery' || action === 'recursive_recovery_static_wrapper') {
    return '否，交给递归修复';
  }
  return '否，需要人工审查';
}

export const DEFAULT_GOAL =
  '通过递归拆解未知深度的开发任务，制作一个可玩的小游戏原型；每个叶子任务都必须执行并通过验收。';
