from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
WORKSPACE_DIR = ROOT_DIR / "workspace"

# 有独立模型/推理档的角色。与 `RunKind` 同名；这里刻意用字面量而不是导入 `models`，
# 免得 config（最底层）反过来依赖 models。
CODEX_KINDS = ("constitution", "planner", "worker", "verifier", "acceptance")


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "CODEX_BATTLEFIELD_DATABASE_URL",
        f"sqlite:///{(WORKSPACE_DIR / 'battlefield.sqlite3').as_posix()}",
    )
    runner_mode: str = os.getenv("CODEX_BATTLEFIELD_RUNNER", "mock")
    codex_bin: str = os.getenv("CODEX_BATTLEFIELD_CODEX_BIN", "codex")
    codex_home: str = os.getenv("CODEX_BATTLEFIELD_CODEX_HOME", "")
    codex_profile: str = os.getenv("CODEX_BATTLEFIELD_CODEX_PROFILE", "")
    codex_model: str = os.getenv("CODEX_BATTLEFIELD_CODEX_MODEL", "")
    codex_reasoning_effort: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT", "")
    # 按角色分层（`docs/spec-verification-independence.md` §4）：`kind` 有独立档位时优先，空则回退全局。
    # 这是"用便宜模型当 worker/verifier、用强模型当 acceptance/constitution"的唯一入口——
    # 也是"flash 级模型能不能吃大工程"的前提之一。
    codex_model_constitution: str = os.getenv("CODEX_BATTLEFIELD_CODEX_MODEL_CONSTITUTION", "")
    codex_model_planner: str = os.getenv("CODEX_BATTLEFIELD_CODEX_MODEL_PLANNER", "")
    codex_model_worker: str = os.getenv("CODEX_BATTLEFIELD_CODEX_MODEL_WORKER", "")
    codex_model_verifier: str = os.getenv("CODEX_BATTLEFIELD_CODEX_MODEL_VERIFIER", "")
    codex_model_acceptance: str = os.getenv("CODEX_BATTLEFIELD_CODEX_MODEL_ACCEPTANCE", "")
    codex_reasoning_effort_constitution: str = os.getenv(
        "CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_CONSTITUTION", ""
    )
    codex_reasoning_effort_planner: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_PLANNER", "")
    codex_reasoning_effort_worker: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_WORKER", "")
    codex_reasoning_effort_verifier: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_VERIFIER", "")
    codex_reasoning_effort_acceptance: str = os.getenv("CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT_ACCEPTANCE", "")
    # 子进程环境**默认收窄**（`docs/spec-task-isolation.md` §2.4）：宿主上无关平台的凭据
    # （实测见过 AIGW_API_KEY / DREAMMAKER_LIBRARY_SERVICE_TOKEN / G10_ASSETGRAPH_TOKEN）不该被
    # 顺手交给每个节点。设 0 回到"原样继承"（老行为，便于对照排查）。
    codex_env_strict: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_ENV_STRICT", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    codex_env_allow_prefixes: str = os.getenv(
        "CODEX_BATTLEFIELD_CODEX_ENV_ALLOW_PREFIXES", "CODEX_,OPENAI_,AZURE_OPENAI_,ANTHROPIC_"
    )
    godot_bin: str = os.getenv("CODEX_BATTLEFIELD_GODOT_BIN", "")
    godot_timeout_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_GODOT_TIMEOUT_SECONDS", "45"))
    godot_max_concurrency: int = int(os.getenv("CODEX_BATTLEFIELD_GODOT_MAX_CONCURRENCY", "1"))
    # R1 的"静态优先级 + 老化"：槽位不够时谁先拿。OS 字节锁只保证互斥（容量绝不超过），
    # 优先级只能靠**应用层仲裁**：只有排名靠前的等待者去抢槽，其他人继续等。
    # 静态优先级按角色（acceptance/成品门禁 > verifier > worker > planner）；
    # 老化保证不饿死——每等 `godot_queue_aging_seconds` 秒，等效优先级 +1。设 0 = 关掉老化（纯优先级）。
    godot_queue_priority_enabled: bool = os.getenv("CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    godot_queue_aging_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS", "300"))
    codex_timeout_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_CODEX_TIMEOUT_SECONDS", "0"))
    codex_idle_timeout_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_CODEX_IDLE_TIMEOUT_SECONDS", "1800"))
    codex_retry_attempts: int = int(os.getenv("CODEX_BATTLEFIELD_CODEX_RETRY_ATTEMPTS", "2"))
    codex_retry_base_delay_seconds: float = float(os.getenv("CODEX_BATTLEFIELD_CODEX_RETRY_BASE_DELAY_SECONDS", "8"))
    # 进程内重试的最长等待。CLI 已经明说"X 分钟后重试"时，8s/16s 的指数退避是**白跑一次真实
    # Codex 启动**（额度不会等 8 秒就回来）。窗口不长就在原地等到点，比整棵树休眠划算；
    # 超过这个上限就放弃进程内重试，把窗口写进块报告交给上层休眠。设 0 表示一律不进程内重试。
    codex_retry_max_in_process_wait_seconds: int = int(
        os.getenv("CODEX_BATTLEFIELD_CODEX_RETRY_MAX_IN_PROCESS_WAIT_SECONDS", "300")
    )
    goal_mode: bool = os.getenv("CODEX_BATTLEFIELD_GOAL_MODE", "0").lower() in {"1", "true", "yes", "on"}
    agent_burst_limit: int = int(os.getenv("CODEX_BATTLEFIELD_AGENT_BURST_LIMIT", "0"))
    agent_burst_reserve: int = int(os.getenv("CODEX_BATTLEFIELD_AGENT_BURST_RESERVE", "0"))
    agent_burst_resume_after_seconds: int = int(
        os.getenv(
            "CODEX_BATTLEFIELD_AGENT_BURST_RESUME_AFTER_SECONDS",
            os.getenv("CODEX_BATTLEFIELD_RESOURCE_BLOCK_RESUME_AFTER_SECONDS", "18000"),
        )
    )
    auto_hibernate_on_resource_block: bool = os.getenv("CODEX_BATTLEFIELD_AUTO_HIBERNATE_ON_RESOURCE_BLOCK", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    resource_block_resume_after_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_RESOURCE_BLOCK_RESUME_AFTER_SECONDS", "18000"))
    watchdog_orphan_resume_after_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_WATCHDOG_ORPHAN_RESUME_AFTER_SECONDS", "300"))
    watchdog_interval_minutes: int = int(os.getenv("CODEX_BATTLEFIELD_WATCHDOG_INTERVAL_MINUTES", "1"))
    codex_ignore_rules: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_IGNORE_RULES", "1").lower() not in {"0", "false", "no"}
    codex_ignore_user_config: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    codex_disable_plugins: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_DISABLE_PLUGINS", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    codex_ephemeral: bool = os.getenv("CODEX_BATTLEFIELD_CODEX_EPHEMERAL", "1").lower() not in {"0", "false", "no"}
    max_concurrency: int = int(os.getenv("CODEX_BATTLEFIELD_MAX_CONCURRENCY", "13"))
    auto_concurrency_enabled: bool = os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    auto_concurrency_min: int = int(os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MIN", "1"))
    auto_concurrency_max: int = int(
        os.getenv(
            "CODEX_BATTLEFIELD_AUTO_CONCURRENCY_MAX",
            os.getenv("CODEX_BATTLEFIELD_MAX_CONCURRENCY", "13"),
        )
    )
    auto_concurrency_queue_threshold: int = int(os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_QUEUE_THRESHOLD", "6"))
    auto_concurrency_resource_cooldown_seconds: int = int(
        os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_RESOURCE_COOLDOWN_SECONDS", "900")
    )
    auto_concurrency_fuel_deadline_hour: int = int(
        os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_FUEL_DEADLINE_HOUR", "20")
    )
    auto_concurrency_fuel_timezone_offset_hours: float = float(
        os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_FUEL_TIMEZONE_OFFSET_HOURS", "8")
    )
    auto_concurrency_fuel_sprint_window_minutes: int = int(
        os.getenv("CODEX_BATTLEFIELD_AUTO_CONCURRENCY_FUEL_SPRINT_WINDOW_MINUTES", "120")
    )
    machine_safety_min_available_memory_mb: int = int(
        os.getenv("CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB", "4096")
    )
    machine_safety_resume_after_seconds: int = int(
        os.getenv("CODEX_BATTLEFIELD_MACHINE_SAFETY_RESUME_AFTER_SECONDS", "1800")
    )
    scheduler_deferred_retry_seconds: float = float(os.getenv("CODEX_BATTLEFIELD_SCHEDULER_DEFERRED_RETRY_SECONDS", "8"))
    max_repeated_child_signature: int = int(os.getenv("CODEX_BATTLEFIELD_MAX_REPEATED_CHILD_SIGNATURE", "2"))
    # 父节点多轮 `repair` 之后进人工审查的上限（铁律 8）。**默认 2**（2026-09-22 决策，
    # 见 `docs/spec-modularization.md` §6 D1）：以前默认 0 = "不限"，那等于把这条保护默认关掉了。
    # 设 0 仍然表示"不限"，需要旧行为就显式设 0。
    max_acceptance_repair_cycles: int = int(os.getenv("CODEX_BATTLEFIELD_MAX_ACCEPTANCE_REPAIR_CYCLES", "2"))
    artifact_preview_chars: int = int(os.getenv("CODEX_BATTLEFIELD_ARTIFACT_PREVIEW_CHARS", "20000"))
    preview_url: str = os.getenv("CODEX_BATTLEFIELD_PREVIEW_URL", "http://127.0.0.1:5173/")
    open_preview_on_start: bool = os.getenv("CODEX_BATTLEFIELD_OPEN_PREVIEW_ON_START", "0").lower() in {"1", "true", "yes"}
    skill_lookup_enabled: bool = os.getenv("CODEX_BATTLEFIELD_SKILL_LOOKUP_ENABLED", "1").lower() not in {"0", "false", "no"}
    skill_km_url: str = os.getenv(
        "CODEX_BATTLEFIELD_SKILL_KM_URL",
        "https://skill-library.example/skill?skillId=00000000-0000-0000-0000-000000000000",
    )
    project_skill_enabled: bool = os.getenv("CODEX_BATTLEFIELD_PROJECT_SKILL_ENABLED", "1").lower() not in {"0", "false", "no"}
    project_skill_path: str = os.getenv(
        "CODEX_BATTLEFIELD_PROJECT_SKILL_PATH",
        str(ROOT_DIR / "skills" / "codex-battlefield-recursive" / "SKILL.md"),
    )
    project_skill_max_chars: int = int(os.getenv("CODEX_BATTLEFIELD_PROJECT_SKILL_MAX_CHARS", "12000"))
    # 集成冲突策略：overwrite（默认，记录并上浮，但后写仍覆盖）/ keep_both（冲突文件两份都留）/
    # repair（冲突阻断验收）。非法值由 integration.normalize_strategy 收敛到默认。
    integration_conflict_strategy: str = os.getenv("CODEX_BATTLEFIELD_INTEGRATION_CONFLICT_STRATEGY", "overwrite")
    # 上浮到验收 prompt 的冲突/跳过明细条数上限；超出只留计数并标 truncated。
    integration_conflict_detail_limit: int = int(
        os.getenv("CODEX_BATTLEFIELD_INTEGRATION_CONFLICT_DETAIL_LIMIT", "20")
    )
    # 债务门控连续拦下多少次后升级为人工阻塞。有界是必须的：
    # 见 docs/spec-parent-child-obligations.md §6.4 硬约束 2。
    obligation_gate_max_refusals: int = int(os.getenv("CODEX_BATTLEFIELD_OBLIGATION_GATE_MAX_REFUSALS", "2"))
    # R5 跨版本回归门禁：验收通过时记下"这一版的产物指纹"，**跨版本**再验收时比对上一版基线。
    # 缺失一律阻断；内容改变由档位决定（`record` 只记录并上浮，`repair` 才阻断——字节变化本身
    # 可能就是修复动作）。同一版内的正常返工不比对，避免门禁拦下自己的修复。
    integration_regression_enabled: bool = os.getenv("CODEX_BATTLEFIELD_INTEGRATION_REGRESSION", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    integration_regression_strategy: str = os.getenv("CODEX_BATTLEFIELD_INTEGRATION_REGRESSION_STRATEGY", "record")
    # R3 worktree 生命周期：创建串行化 + 终态回收。默认**保守**：开回收但保留 24 小时，
    # 且只有"终态（done/canceled）+ 声明产物已在归档处 + 过了保留期 + 在受管根下"才回收。
    # `failed` 一律不回收（失败现场是修复与复盘要读的证据）。保留 0 = 立即回收（腾盘/测试用）。
    worktree_recycle_enabled: bool = os.getenv("CODEX_BATTLEFIELD_WORKTREE_RECYCLE", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    worktree_retention_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_WORKTREE_RETENTION_SECONDS", "86400"))
    # 一次清扫最多回收几个：避免某一轮里一口气删掉一大批（删错了也留有余地）。
    worktree_recycle_limit_per_sweep: int = int(os.getenv("CODEX_BATTLEFIELD_WORKTREE_RECYCLE_LIMIT", "20"))
    # 宪法修订（收紧）后，用新宪法把"落后且可再验证"的已完成后代叶子重跑一次 verifier。
    # **只控制要不要花 token**，不控制严重度：关掉它版本债照旧阻断（人选了收紧却拒绝复查），
    # 最终走升级通路交给人，而不是被静默放行。收紧必须触发再验证（宪法 SPEC §7），所以默认开。
    constitution_revalidate_enabled: bool = os.getenv(
        "CODEX_BATTLEFIELD_CONSTITUTION_REVALIDATE_ENABLED", "1"
    ).lower() not in {"0", "false", "no"}
    # 约束覆盖缺口（§7.1.4）是否阻断父节点完成。默认**只记录并上浮**：
    # LLM 规划器会改写措辞，默认阻断会让每一棵树的父节点都永远拿不到 done（实测 14 条既有契约变红）。
    # 要把它当硬门槛，显式打开。
    constraint_coverage_gap_blocks: bool = os.getenv(
        "CODEX_BATTLEFIELD_CONSTRAINT_COVERAGE_GAP_BLOCKS", "0"
    ).lower() in {"1", "true", "yes"}
    parent_skill_note_task_enabled: bool = os.getenv("CODEX_BATTLEFIELD_PARENT_SKILL_NOTE_TASK_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    frozen_mode: bool = os.getenv("CODEX_BATTLEFIELD_FROZEN_MODE", "0").lower() in {"1", "true", "yes", "on"}
    full_checkpoint_enabled: bool = os.getenv("CODEX_BATTLEFIELD_FULL_CHECKPOINT_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    watchdog_state_snapshot_interval_seconds: int = int(
        os.getenv("CODEX_BATTLEFIELD_WATCHDOG_STATE_SNAPSHOT_INTERVAL_SECONDS", "300")
    )
    full_checkpoint_interval_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_FULL_CHECKPOINT_INTERVAL_SECONDS", "3600"))
    # 每个完整检查点都含一份数据库拷贝 + 目标工程拷贝，所以**保留份数直接乘上目标体积**。
    # 实测（一个中等 Godot 目标）：15 份 = 9.6 GB；而旧默认 48 份在长时间自主运行里足以撑爆磁盘，
    # 那正好是"跑着跑着停下来"的一类成因。恢复只用得到**最新**那一份，12 份足够回退若干小时。
    full_checkpoint_max_count: int = int(os.getenv("CODEX_BATTLEFIELD_FULL_CHECKPOINT_MAX_COUNT", "12"))
    checkpoint_ledger_enabled: bool = os.getenv("CODEX_BATTLEFIELD_CHECKPOINT_LEDGER_ENABLED", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    api_detached_real_scheduler: bool = os.getenv("CODEX_BATTLEFIELD_API_DETACHED_REAL_SCHEDULER", "1").lower() not in {
        "0",
        "false",
        "no",
    }
    checkpoint_git_probe_timeout_seconds: int = int(os.getenv("CODEX_BATTLEFIELD_CHECKPOINT_GIT_PROBE_TIMEOUT_SECONDS", "20"))
    mock_delay_seconds: float = float(os.getenv("CODEX_BATTLEFIELD_MOCK_DELAY", "0.25"))
    workspace_dir: Path = WORKSPACE_DIR

    def model_for(self, kind: str) -> str:
        """按角色的模型档；空则回退全局 `codex_model`。`kind` 未知也回退——**不猜**。"""
        return self._per_kind(kind, "codex_model") or self.codex_model

    def reasoning_effort_for(self, kind: str) -> str:
        return self._per_kind(kind, "codex_reasoning_effort") or self.codex_reasoning_effort

    def _per_kind(self, kind: str, prefix: str) -> str:
        key = str(kind or "").strip().lower()
        if key not in CODEX_KINDS:
            return ""
        return str(getattr(self, f"{prefix}_{key}", "") or "").strip()

    def model_tier_warnings(self) -> list[str]:
        """分层配置的可疑之处。**只报不自动纠正**——静默改配置比报出来更糟。

        刻意**不**做的事：比较两个模型名谁更"强"。名字里没有强度信息，硬排会造出假的判据。
        能自动判的只有一种情形：**worker 配了独立档、acceptance 还在走全局档**——
        那正是"以为自己在分层其实没分"的典型，值得让人看一眼。
        """
        warnings: list[str] = []
        worker = self._per_kind("worker", "codex_model")
        acceptance = self._per_kind("acceptance", "codex_model")
        if worker and not acceptance:
            warnings.append(
                "worker 配了独立模型档，但 acceptance 仍在走全局档：请确认全局档不弱于 worker 档"
                "（模型强弱无法从名字判断，这里只报事实）"
            )
        if acceptance and not worker:
            warnings.append("acceptance 配了独立模型档、worker 走全局档：这是期望的配比（强验收 + 便宜执行）")
        return warnings


def get_settings() -> Settings:
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / "runs").mkdir(parents=True, exist_ok=True)
    (WORKSPACE_DIR / "worktrees").mkdir(parents=True, exist_ok=True)
    return Settings()
