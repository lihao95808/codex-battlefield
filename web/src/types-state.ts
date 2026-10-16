/**
 * 工程绑定、状态管理、迁移与设置（P5-2：从 `types.ts` 拆出来，纯类型、无逻辑）。
 *
 * `types.ts` 现在是聚合出口（re-export），老调用方不用改。
 */

export interface StateLayoutResponse {
  state_home: string;
  project_key: string;
  project_root: string | null;
  bound: boolean;
  tools_dir: string;
  project_dir?: string;
  db_path?: string;
  runs_dir?: string;
  worktrees_dir?: string;
  database_url: string;
  runner_mode: string;
  /** 磁盘上 `active.json` 现在写的工程（进程**没有**回读它）。 */
  active_file_key: string;
  active_file_root: string | null;
  /**
   * 进程的启动快照与磁盘现状是否一致。
   * false = 用户切过工程但**没重启**，当前进程仍用旧的那个（决策 D1：不热切库）。
   */
  snapshot_matches_active_file: boolean;
}

export interface MountedProject {
  key: string;
  root: string;
  project_dir: string;
  db_path: string;
  db_exists: boolean;
  db_size_bytes: number;
  /** 工程目录现在是否真的存在（可能被删或移走过）。 */
  root_exists: boolean;
  /** 是否为当前工程。 */
  active: boolean;
  /**
   * 状态目录与工程是否同一块盘。false 时 `git worktree` 不可靠 ——
   * 这是"能绑但会出错"的情形，界面必须提前说，而不是等跑到一半才炸。
   */
  drive_matches: boolean;
}

export interface MountedProjectsResponse {
  state_home: string;
  active_key: string;
  projects: MountedProject[];
}

export interface BindProjectResponse {
  status: string;
  project: MountedProject;
  /** 绑定写的是 active.json；当前进程仍连着旧库，需要重启才生效。 */
  restart_required: boolean;
  message: string;
  warnings: string[];
}

export interface SettingField {
  key: string;
  env: string;
  label: string;
  kind: 'str' | 'int' | 'float' | 'bool';
  group: string;
  help: string;
  /** 会放宽权限或增加开销 —— 界面要二次确认。 */
  risky: boolean;
  choices: string[];
  minimum: number | null;
  maximum: number | null;
}

export interface SettingCurrent {
  value: unknown;
  source: 'env' | 'config' | 'default' | string;
  /** 被环境变量压着时为真 —— 此时在界面上改它**不会有效果**。 */
  env_override: boolean;
  file_value: string | null;
}

export interface SettingsResponse {
  groups: Record<string, string>;
  order: string[];
  fields: SettingField[];
  current: Record<string, SettingCurrent>;
  /** 恒为真：Settings 是启动时构造一次的 frozen dataclass。 */
  restart_required: boolean;
  config_file: string;
}

export interface SettingsSaveResponse {
  status: string;
  changed: string[];
  config_file: string;
  restart_required: boolean;
  message: string;
}

export interface StateProjectEntry {
  key: string;
  root: string;
  project_dir: string;
  active: boolean;
  root_exists: boolean;
  /** 状态目录与工程是否同盘 —— 跨盘时 git worktree 不可靠。 */
  drive_matches: boolean;
  db_bytes: number;
  /** 整个状态目录（含 runs / worktrees / 快照）。清空重来要删的就是它。 */
  state_bytes: number;
}

export interface StateOverviewResponse {
  state_home: string;
  config_file: string;
  bound: boolean;
  active_key: string;
  project_root: string | null;
  snapshot_matches_active_file: boolean;
  projects: StateProjectEntry[];
  /** 旧 `<repo>/workspace/` 里还剩什么 —— 迁移的入口。 */
  legacy: { present: boolean; db_path: string; db_bytes?: number };
}

export interface MigrationPreviewCandidate {
  path: string;
  source: string;
  usable: boolean;
  reason: string;
}

export interface MigrationPreviewResponse {
  available: boolean;
  reason?: string;
  repo_root?: string;
  legacy_db?: string;
  legacy_db_bytes?: number;
  legacy_task_count?: number;
  legacy_worktree_rows?: number;
  current_project_root?: string | null;
  target_key?: string | null;
  candidates?: MigrationPreviewCandidate[];
}

export interface MigrationApplyResponse {
  status: string;
  copied: boolean;
  skipped_reason: string;
  project_key: string;
  target_db: string;
  worktree_rows_rewritten: number;
  codex_home_copied: boolean;
  note: string;
}

export interface StateResetResponse {
  status: string;
  key: string;
  root: string;
  purged: boolean;
  was_active: boolean;
  note: string;
}
