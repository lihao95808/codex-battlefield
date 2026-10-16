from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .codex_signals import (
    has_http_429_marker,
    is_local_skill_failure,
    is_transient_failure_text,
)
from .config import ROOT_DIR, Settings, machine_root_of
from . import runner_tools
from . import project_context, prompts
from . import retry
from . import trace
from .models import TaskNode
from .schemas import AcceptanceResult, ChildTaskSpec, PlannerDecision, VerifierResult, WorkerResult
from .utils import loads, repair_mojibake


CODEX_ENVIRONMENT_NOISE_MARKERS = (
    "codex_analytics::client: failed to send events request",
    "codex_core_plugins::manager: failed to warm featured plugin ids cache",
    "codex_core_plugins::manager: failed to refresh remote installed plugins cache",
    "codex_core_plugins::remote::remote_installed_plugin_sync: remote installed plugin bundle sync failed",
    "codex_core_skills::loader: ignoring",
    "codex_core::hook_runtime: after_agent hook failed",
    "codex_core_plugins::manifest: ignoring interface.defaultprompt",
    "codex_core::session::session: failed to load skill",
    "codex_core::shell_snapshot: failed to create shell snapshot",
    "codex_exec_server::client::http_client::reqwest_http_client: http/request send failed",
    "codex_mcp::rmcp_client: failed to initialize mcp client during shutdown",
    "codex_models_manager::manager: failed to refresh available models",
    "codex_rmcp_client::http_client_adapter: streamable http post_message failed",
    "rmcp::transport::worker: worker quit with fatal: transport channel closed",
)

MOCK_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)

# R1 的静态优先级：真实 Godot 槽位不够时谁先拿。角色越靠近"完成关键路径"越优先——
# 门禁/验收卡住会让整棵树都不动，而 worker 的探索性运行可以等一会儿。
# 老化由包装器负责（等得越久等效优先级越高），所以这里只给静态权重。
GODOT_QUEUE_ROLE_PRIORITY = {
    "acceptance": 3,
    "verifier": 2,
    "worker": 1,
    "planner": 0,
    "constitution": 0,
}

# 子进程环境里**必须**保留的键（系统 / 运行时 / 定位工具链用）。名单刻意写死而不是"按前缀放行"：
# 少了某个键的表现是"真实 Codex 起不来"，而那种失败很难归因到"环境被过滤"。
CHILD_ENV_KEEP_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "HOME",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "COMMONPROGRAMFILES",
        "COMMONPROGRAMFILES(X86)",
        "USERNAME",
        "USERDOMAIN",
        "COMPUTERNAME",
        "SESSIONNAME",
        "OS",
        "TERM",
        "TZ",
        "SHELL",
        "PWD",
        "LANG",
        "LC_ALL",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "PYTHONHOME",
        "PYTHONPATH",
    }
)
# 代理 / 证书 / 版本控制 / 包管理器这类"跑得起来才有的东西"，按前缀保留
CHILD_ENV_KEEP_PREFIXES = (
    "PROCESSOR_",
    "NUMBER_OF_PROCESSORS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "SSL_",
    "CURL_",
    "GIT_",
    "NODE_",
    "NPM_",
    "PNPM_",
)

# **已经决定要停之后**，最多再等子进程退出多久（秒）。见 `_await_exit_bounded` 的理由：
# asyncio 的 `Process.wait()` 要等"进程退出 **且** 所有管道 EOF"，一个脱离的孙进程攥着 stdout
# 就能让它永远不返回（2026-09-24 有确定性复现）。这个上界只在停止路径上真正生效。
EXIT_WAIT_AFTER_STOP_SECONDS = 20.0


def is_meaningful_codex_activity_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    lowered = stripped.lower()
    if stripped.startswith("--- codex ") or stripped.startswith("--- Codex "):
        return False
    return not any(marker in lowered for marker in CODEX_ENVIRONMENT_NOISE_MARKERS)


def is_meaningful_codex_activity_text(text: str) -> bool:
    return any(is_meaningful_codex_activity_line(line) for line in text.splitlines())


def usage_from_codex_logs(logs_path: Path | str | None, tail_bytes: int = 262144) -> dict | None:
    """从 `--json` 日志尾部取最后一次 `turn.completed` 的 usage。

    读文件尾部而非改流式循环：不依赖分块边界，也不改动 `_communicate_with_watchdog` 的返回签名。
    """
    if logs_path is None:
        return None
    try:
        with Path(logs_path).open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - tail_bytes))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line.startswith("{") or "turn.completed" not in line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = event.get("usage") if isinstance(event, dict) else None
        if isinstance(usage, dict):
            return usage
    return None


@dataclass(frozen=True)
class RunnerEnvelope:
    command: str
    exit_code: int
    logs_path: str | None
    payload: dict
    prompt_path: str | None = None
    last_message_path: str | None = None
    workdir: str | None = None
    usage: dict | None = None


class BaseCodexRunner:
    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        raise NotImplementedError

    async def steward_chat(
        self,
        status_snapshot: str,
        capabilities: str,
        history: str,
        user_message: str,
    ) -> RunnerEnvelope:
        raise NotImplementedError

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        raise NotImplementedError

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        raise NotImplementedError

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        raise NotImplementedError


class MockCodexRunner(BaseCodexRunner):
    # **编排器自执的不变量**：说的是"树 / 父节点 / 记录"该怎么运转，**不是任何子任务能交付的东西**。
    # 它们以前写在宪法的 `global_acceptance` 里，而覆盖核对要求子节点的验收**逐字包含**这些条款
    # → 每层记一条永远清不掉的 `constraint_coverage_gap` 假债（实测 2026-09-22：7 个节点各 3 条）。
    # 现在它们是**工具侧常量**：渲染进宪法 markdown（节点看得见），但不进委托链、也不进覆盖核对。
    ORCHESTRATION_INVARIANTS = (
        "任务树必须保留总目标的品质下限。",
        "父节点必须通过整合验收后才能 done。",
        "真实运行记录必须保存 prompt、结果、证据和失败原因。",
    )

    def __init__(self, delay_seconds: float = 0.25) -> None:
        self.delay_seconds = delay_seconds

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        await asyncio.sleep(self.delay_seconds)
        data = {
            "project_name": "Codex Battlefield 项目",
            "quality_tier": "垂直切片",
            "quality_floor": [
                "最终成果必须形成一个可运行、可验收、可继续扩展的垂直切片。",
                "每个节点必须能说明自己是否 300% 可由单个 Codex CLI 完成。",
                "叶子任务必须有明确边界、证据和验收标准。",
            ],
            "quality_ceiling": [
                "不把目标扩张为完整商业产品。",
                "不为了安全感无限拆分；拆分必须降低风险并提高可验证性。",
                "不允许 worker 越过当前节点边界补做父节点工作。",
            ],
            "non_goals": ["联网功能", "大型资源管线", "无限内容扩展"],
            # `global_acceptance` = **产物级**底线：每一层都能逐字继承，缺了才会记 `constraint_coverage_gap`。
            "global_acceptance": [
                "不许删改既有测试或验收判据来表达需求。",
                "不许引入付费依赖、联网服务或需要凭据的组件。",
                "改动必须留在本目标工程的边界内。",
            ],
            "decomposition_rules": [
                "不能 300% 确定完成时必须拆分。",
                "子任务要相对独立、难度尽量均衡，并共同覆盖父节点验收项。",
                "父节点必须能整合并准确验收子节点结果。",
            ],
            "leaf_rules": [
                "单个 Codex CLI 能一次完成并验证。",
                "任何 fail/repair 验收都表示叶子误判，必须转父重新拆分。",
            ],
            "parent_acceptance_rules": [
                "子节点全 done 只是必要条件，不是父节点完成条件。",
                "父节点必须运行 acceptance agent 验证组合结果。",
            ],
            "goal": goal,
            "target_path": target_path,
        }
        markdown = (
            "# 项目宪法\n\n"
            f"## 总目标\n{goal}\n\n"
            "## 品质下限\n" + "\n".join(f"- {item}" for item in data["quality_floor"]) + "\n\n"
            "## 规模上限\n" + "\n".join(f"- {item}" for item in data["quality_ceiling"]) + "\n\n"
            "## 300% 可完成铁律\n"
            "- 任何节点不能确信单个 Codex CLI 能一次完成并验证时，必须拆分。\n"
            "- 叶子验收 fail/repair 一次即转父节点重新拆分。\n\n"
            # 这两条仍然要**让每个节点看见**（prompt 会带宪法），只是它们不进委托链的覆盖核对：
            # 它们由编排器自己执行（`_accept_parent` 的整合验收、`_record_run` 的运行记录）。
            "## 全局验收底线（可委托，逐层逐字继承）\n"
            + "\n".join(f"- {item}" for item in data["global_acceptance"])
            + "\n\n"
            "## 任务树不变量（由编排器自执，不委托给子任务）\n"
            + "\n".join(f"- {item}" for item in MockCodexRunner.ORCHESTRATION_INVARIANTS)
            + "\n"
        )
        return RunnerEnvelope(command="模拟宪法生成器", exit_code=0, logs_path=None, payload={"markdown": markdown, "data": data})

    async def steward_chat(
        self,
        status_snapshot: str,
        capabilities: str,
        history: str,
        user_message: str,
    ) -> RunnerEnvelope:
        """mock 版管家：**确定性回复**，用于测试与 mock 模式。

        刻意**不假装懂局势**：只回显收到了什么、看到多大快照。
        如果 mock 也编一段像样的分析，测试就会依赖那段文本，
        而真实 agent 的措辞永远不会一样 —— 那种测试是假绿。
        """
        await asyncio.sleep(self.delay_seconds)
        return RunnerEnvelope(
            command="mock steward_chat",
            exit_code=0,
            logs_path=None,
            payload={
                "reply": f"[mock steward] 收到：{user_message[:200]}",
                "situation": "mock 模式：不做真实判断。",
                "proposed_actions": [],
                "needs_user_input": False,
                "confidence": "low",
            },
        )

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        await asyncio.sleep(self.delay_seconds)
        forced_verifier_failure = " [force-verifier-fail]" if "[force-verifier-fail]" in task.description else ""
        if "[force-blocked]" in task.description:
            payload = PlannerDecision(
                decision="blocked",
                reason="模拟规划器按指令阻塞了这个节点。",
                risks=["强制阻塞"],
            ).model_dump()
        elif task.depth == 0:
            payload = PlannerDecision(
                decision="decompose",
                reason="根目标横跨任务编排、界面呈现和验收验证，需要继续拆解。",
                leaf_certainty="not_sure",
                why_not_leaf="根目标不是单个 Codex CLI 可 300% 一次完成的叶子。",
                coverage_ids=["global_acceptance"],
                integration_plan="父节点等待子任务完成后运行整合验收。",
                verification_plan="检查子任务是否共同满足项目宪法和根目标。",
                children=[
                    ChildTaskSpec(
                        title="核心玩法任务切片",
                        description=f"把游戏目标拆成可以独立执行和验收的纵向切片。{forced_verifier_failure}",
                        scope=["gameplay"],
                        acceptance=["至少创建两个可以执行的叶子切片。"],
                        coverage_ids=["quality_floor"],
                    ),
                    ChildTaskSpec(
                        title="战场可视化界面",
                        description="实现任务树视图和节点详情体验。",
                        scope=["web/src"],
                        acceptance=["当前前线节点必须可见。"],
                        coverage_ids=["debug_visibility"],
                    ),
                    ChildTaskSpec(
                        title="验收测试支架",
                        description="证明递归调度器可以在不消耗真实模型 token 的情况下被测试。",
                        scope=["api/tests"],
                        acceptance=["模拟规划器、执行器和验收器场景全部通过。"],
                        coverage_ids=["global_acceptance"],
                    ),
                ],
            ).model_dump()
        elif task.depth == 1 and ("gameplay" in task.title.lower() or "玩法" in task.title or "gameplay" in task.scope_json):
            payload = PlannerDecision(
                decision="decompose",
                reason="玩法范围仍然偏大，需要继续拆成两个可验收的纵向切片。",
                leaf_certainty="not_sure",
                why_not_leaf="玩法节点仍包含多个相互独立的战斗/移动能力，单个 worker 不能 300% 稳定完成。",
                integration_plan="父节点整合移动与命中检测证据。",
                verification_plan="检查两个切片是否共同覆盖核心玩法验收。",
                children=[
                    ChildTaskSpec(
                        title="玩家移动切片",
                        description=f"实现移动输入、模拟逻辑和验证证据。{forced_verifier_failure}",
                        scope=["src/player"],
                        acceptance=["移动场景可以被验证。"],
                        coverage_ids=["movement"],
                        estimated_leaf=True,
                    ),
                    ChildTaskSpec(
                        title="战斗命中检测切片",
                        description="实现攻击输入、命中检测和验证证据。",
                        scope=["src/combat"],
                        acceptance=["命中检测场景可以被验证。"],
                        coverage_ids=["combat_hit"],
                        estimated_leaf=True,
                    ),
                ],
            ).model_dump()
        else:
            payload = PlannerDecision(
                decision="execute",
                reason="该任务边界足够窄，且具备明确验收检查，可以交给单个叶子执行器。",
                leaf_certainty="300%",
                coverage_ids=loads(task.acceptance_json, []),
                verification_plan="执行后由 verifier 检查文件、测试或证据。",
            ).model_dump()
        return RunnerEnvelope(command="模拟规划器", exit_code=0, logs_path=None, payload=payload)

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        await asyncio.sleep(self.delay_seconds)
        if "[force-worker-decompose]" in task.description:
            payload = WorkerResult(
                status="needs_decomposition",
                summary="模拟执行器自检发现该任务无法 300% 一次完成，必须拆分。",
                risks=["任务粒度仍然偏大"],
                suggested_children=self._fallback_children(task, "执行器建议拆分"),
            ).model_dump()
        elif "[force-worker-fail]" in task.description:
            payload = WorkerResult(
                status="failed",
                summary="模拟执行器按指令失败。",
                evidence=["强制执行失败"],
                risks=["需要修复"],
            ).model_dump()
        else:
            changed_files = self._write_mock_artifacts(task)
            payload = WorkerResult(
                status="succeeded",
                summary=f"模拟执行器已完成：{task.title}。",
                changed_files=changed_files,
                tests=[f"mock-test::{task.id} 通过"],
                evidence=[f"叶子任务 {task.id} 已达到可执行颗粒度"],
            ).model_dump()
        return RunnerEnvelope(command="模拟执行器", exit_code=0, logs_path=None, payload=payload)

    def _write_mock_artifacts(self, task: TaskNode) -> list[str]:
        changed_files = [f"mock/{task.id}.txt"]
        if not task.worktree_path:
            return changed_files

        root = Path(task.worktree_path)
        mock_note = root / changed_files[0]
        mock_note.parent.mkdir(parents=True, exist_ok=True)
        mock_note.write_text(
            f"Mock artifact for {task.id}\nTitle: {task.title}\n",
            encoding="utf-8",
        )

        if self._should_write_mock_godot_project(task):
            changed_files.extend(self._write_mock_godot_brawler_project(root))
        return changed_files

    def _should_write_mock_godot_project(self, task: TaskNode) -> bool:
        meta = loads(task.node_meta_json, {})
        coverage_ids = meta.get("coverage_ids") if isinstance(meta, dict) else []
        if not isinstance(coverage_ids, list):
            coverage_ids = []
        text = " ".join(
            [
                task.title or "",
                task.description or "",
                task.acceptance_json or "",
                str(meta.get("verification_kind", "")) if isinstance(meta, dict) else "",
                " ".join(str(item) for item in coverage_ids),
            ]
        ).lower()
        return any(
            marker in text
            for marker in (
                "final-product-gate",
                "godot-playable-entry",
                "parent-godot-integration-gate",
                "project.godot",
                "run/main_scene",
            )
        )

    def _write_mock_godot_brawler_project(self, root: Path) -> list[str]:
        files: dict[str, str] = {
            "project.godot": """config_version=5

[application]
config/name="Mock Arcade Brawler"
run/main_scene="res://scenes/main.tscn"

[input]
move_left={
"deadzone": 0.5,
"events": []
}
move_right={
"deadzone": 0.5,
"events": []
}
attack_light={
"deadzone": 0.5,
"events": []
}
attack_heavy={
"deadzone": 0.5,
"events": []
}
""",
            "README.md": """# Mock Arcade Brawler

This mock Godot project is generated by the recursive battlefield test runner.

Start with Godot 4 and open `project.godot`. The main scene is `res://scenes/main.tscn`.

Implemented evidence: player movement, three attacks, hitbox/hurtbox combat, enemy AI, boss data, three stages, wave spawning, HUD, menu, pause, restart, score, victory, defeat, and next_level flow.

Validation: run the Codex Battlefield final product gate or inspect the listed scenes, scripts, and data files.
""",
            "scenes/main.tscn": """[gd_scene load_steps=10 format=3]

[ext_resource type="Script" path="res://scripts/game_controller.gd" id="1"]
[ext_resource type="PackedScene" path="res://scenes/player/player_hero.tscn" id="2"]
[ext_resource type="PackedScene" path="res://scenes/ui/hud.tscn" id="3"]
[ext_resource type="PackedScene" path="res://scenes/levels/level_01.tscn" id="4"]
[ext_resource type="Texture2D" path="res://assets/characters/player_hero.png" id="5"]
[ext_resource type="Texture2D" path="res://assets/enemies/thug.png" id="6"]
[ext_resource type="Texture2D" path="res://assets/levels/dock.png" id="7"]
[ext_resource type="Texture2D" path="res://assets/ui/hud_panel.png" id="8"]
[ext_resource type="AudioStream" path="res://assets/audio/hit.wav" id="9"]

[node name="GameController" type="Node2D"]
script = ExtResource("1")

[node name="Player" parent="." instance=ExtResource("2")]

[node name="HUD" parent="." instance=ExtResource("3")]

[node name="LevelStage" parent="." instance=ExtResource("4")]

[node name="WaveSpawnRoot" type="Node2D" parent="."]

[node name="StageTexture" type="Sprite2D" parent="."]
texture = ExtResource("7")

[node name="PlayerPortraitProbe" type="Sprite2D" parent="."]
texture = ExtResource("5")

[node name="EnemyProbe" type="Sprite2D" parent="."]
texture = ExtResource("6")

[node name="HudPanelProbe" type="Sprite2D" parent="."]
texture = ExtResource("8")

[node name="HitSound" type="AudioStreamPlayer" parent="."]
stream = ExtResource("9")
""",
            "scenes/player/player_hero.tscn": """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/player_controller.gd" id="1"]

[node name="PlayerHero" type="CharacterBody2D"]
script = ExtResource("1")

[node name="BodyCollision" type="CollisionShape2D" parent="."]

[node name="AnimatedSprite2D" type="AnimatedSprite2D" parent="."]

[node name="HitBox" type="Area2D" parent="."]

[node name="HurtBox" type="Area2D" parent="."]

[node name="AnimationPlayer" type="AnimationPlayer" parent="."]
""",
            "scenes/enemies/thug.tscn": self._mock_actor_scene("ThugEnemy"),
            "scenes/enemies/boxer.tscn": self._mock_actor_scene("BoxerEnemy"),
            "scenes/enemies/striker.tscn": self._mock_actor_scene("StrikerEnemy"),
            "scenes/enemies/guard.tscn": self._mock_actor_scene("GuardEnemy"),
            "scenes/enemies/iron_boss.tscn": self._mock_actor_scene("IronBoss"),
            "scenes/levels/level_01.tscn": self._mock_level_scene("Level01"),
            "scenes/levels/level_02.tscn": self._mock_level_scene("Level02"),
            "scenes/levels/level_03.tscn": self._mock_level_scene("Level03"),
            "scenes/ui/hud.tscn": """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/hud_controller.gd" id="1"]

[node name="HUD" type="CanvasLayer"]
script = ExtResource("1")

[node name="HealthBar" type="ProgressBar" parent="."]
[node name="ScoreLabel" type="Label" parent="."]
[node name="PauseMenu" type="Control" parent="."]
[node name="RestartButton" type="Button" parent="PauseMenu"]
""",
            "scripts/player_controller.gd": """extends CharacterBody2D

var health := 120
var combo := 0
var direction := Vector2.ZERO
var hitbox_active := false
var hurtbox_active := true
var attack_damage := 12

func _physics_process(delta):
    direction.x = Input.get_action_strength("move_right") - Input.get_action_strength("move_left")
    velocity = direction * 260.0
    move_and_slide()
    if Input.is_action_just_pressed("attack_light"):
        attack("light", 8)
    if Input.is_action_just_pressed("attack_heavy"):
        attack("heavy", 18)

func attack(kind, damage):
    combo += 1
    attack_damage = damage
    hitbox_active = true

func receive_hit(damage):
    health -= damage
    hurtbox_active = true
""",
            "scripts/enemy_ai.gd": """extends CharacterBody2D

var enemy_type := "enemy"
var boss := false
var ai_state := "patrol"
var target = null
var health := 60
var attack_damage := 10

func _physics_process(delta):
    if boss:
        ai_state = "boss_chase"
    elif target:
        ai_state = "chase"
    else:
        ai_state = "patrol"
    if ai_state in ["chase", "boss_chase"]:
        attack_target()

func attack_target():
    ai_state = "attack"

func receive_hit(damage):
    health -= damage
""",
            "scripts/combat_system.gd": """extends Node

func resolve_combat_attack(attacker, defender, hitbox, hurtbox):
    if hitbox and hurtbox:
        apply_hit_damage(defender, attacker.attack_damage)

func apply_hit_damage(defender, damage):
    defender.health -= damage
""",
            "scripts/hud_controller.gd": """extends CanvasLayer

var hud := true
var health_bar := 100
var score := 0
var menu := false
var pause := false

func set_score(value):
    score = value

func toggle_pause():
    pause = not pause
    menu = pause

func restart():
    score = 0
    pause = false
""",
            "scripts/game_controller.gd": """extends Node2D

var level := 1
var stage := 1
var wave := 0
var victory := false
var defeat := false
var game_over := false

func spawn_wave():
    wave += 1

func complete_stage():
    victory = true
    next_level()

func next_level():
    level += 1
    stage = level

func clear_level():
    complete_stage()

func player_defeat():
    defeat = true
    game_over = true
""",
        }
        files.update(self._mock_brawler_data_files())

        changed_files: list[str] = []
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            changed_files.append(rel)
        for rel, content in self._mock_brawler_binary_assets().items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            changed_files.append(rel)
        return sorted(changed_files)

    def _mock_brawler_binary_assets(self) -> dict[str, bytes]:
        return {
            "assets/characters/player_hero.png": MOCK_PNG_BYTES,
            "assets/enemies/thug.png": MOCK_PNG_BYTES,
            "assets/enemies/boxer.png": MOCK_PNG_BYTES,
            "assets/enemies/striker.png": MOCK_PNG_BYTES,
            "assets/enemies/guard.png": MOCK_PNG_BYTES,
            "assets/enemies/iron_boss.png": MOCK_PNG_BYTES,
            "assets/levels/dock.png": MOCK_PNG_BYTES,
            "assets/ui/hud_panel.png": MOCK_PNG_BYTES,
            "assets/effects/hit_spark.png": MOCK_PNG_BYTES,
            "assets/audio/hit.wav": self._mock_wav_bytes(),
            "assets/audio/victory.wav": self._mock_wav_bytes(sample_count=120),
        }

    def _mock_wav_bytes(self, sample_count: int = 80) -> bytes:
        data = b"\x00\x00" * sample_count
        return (
            b"RIFF"
            + (36 + len(data)).to_bytes(4, "little")
            + b"WAVE"
            + b"fmt "
            + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little")
            + (1).to_bytes(2, "little")
            + (8000).to_bytes(4, "little")
            + (16000).to_bytes(4, "little")
            + (2).to_bytes(2, "little")
            + (16).to_bytes(2, "little")
            + b"data"
            + len(data).to_bytes(4, "little")
            + data
        )

    def _mock_actor_scene(self, name: str) -> str:
        return f"""[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://scripts/enemy_ai.gd" id="1"]

[node name="{name}" type="CharacterBody2D"]
script = ExtResource("1")

[node name="BodyCollision" type="CollisionShape2D" parent="."]
[node name="HitBox" type="Area2D" parent="."]
[node name="HurtBox" type="Area2D" parent="."]
[node name="AnimationPlayer" type="AnimationPlayer" parent="."]
"""

    def _mock_level_scene(self, name: str) -> str:
        return f"""[gd_scene format=3]

[node name="{name}" type="Node2D"]

[node name="ArenaBoundary" type="StaticBody2D" parent="."]

[node name="PlayerSpawn" type="Marker2D" parent="."]

[node name="EnemySpawn" type="Marker2D" parent="."]

[node name="WaveSpawn" type="Marker2D" parent="."]

[node name="Camera2D" type="Camera2D" parent="."]
"""

    def _mock_brawler_data_files(self) -> dict[str, str]:
        player = {
            "id": "player_hero",
            "resource_paths": {"scene": "res://scenes/player/player_hero.tscn"},
            "attacks": [
                {"id": "jab", "damage": 8, "hitbox": "light"},
                {"id": "launcher", "damage": 14, "hitbox": "uppercut"},
                {"id": "super", "damage": 28, "hitbox": "wide"},
            ],
        }
        enemies = {
            "enemies": [
                {"id": "thug", "enemy_type": "grunt", "resource_paths": {"scene": "res://scenes/enemies/thug.tscn"}},
                {"id": "boxer", "enemy_type": "grunt", "resource_paths": {"scene": "res://scenes/enemies/boxer.tscn"}},
                {"id": "striker", "enemy_type": "grunt", "resource_paths": {"scene": "res://scenes/enemies/striker.tscn"}},
                {"id": "guard", "enemy_type": "grunt", "resource_paths": {"scene": "res://scenes/enemies/guard.tscn"}},
                {"id": "iron_boss", "enemy_type": "boss", "resource_paths": {"scene": "res://scenes/enemies/iron_boss.tscn"}},
            ]
        }
        levels = {
            "levels": [
                {"id": "level_01", "scene_path": "res://scenes/levels/level_01.tscn"},
                {"id": "level_02", "scene_path": "res://scenes/levels/level_02.tscn"},
                {"id": "level_03", "scene_path": "res://scenes/levels/level_03.tscn"},
            ]
        }
        spawns = {
            "wave_sets": [
                {"level_id": "level_01", "waves": [{"enemy": "thug", "count": 3}]},
                {"level_id": "level_02", "waves": [{"enemy": "boxer", "count": 2}, {"enemy": "striker", "count": 2}]},
                {"level_id": "level_03", "waves": [{"enemy": "guard", "count": 3}, {"enemy": "iron_boss", "count": 1}]},
            ]
        }
        return {
            "data/characters/player_hero.json": json.dumps(player, ensure_ascii=False, indent=2),
            "data/enemies/enemy_catalog.json": json.dumps(enemies, ensure_ascii=False, indent=2),
            "data/levels/level_catalog.json": json.dumps(levels, ensure_ascii=False, indent=2),
            "data/spawns/arcade_wave_plan.json": json.dumps(spawns, ensure_ascii=False, indent=2),
        }

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        await asyncio.sleep(self.delay_seconds)
        if "[force-verifier-fail]" in task.description or worker_result.status != "succeeded":
            payload = VerifierResult(
                verdict="repair",
                reason="模拟验收器发现证据缺失或执行失败。",
                checks=["执行结果必须成功", "必须提供证据"],
                evidence=worker_result.evidence,
                leaf_misclassified=True,
                constitution_verdict="within_bounds",
                failure_to_decomposition_reason="叶子验收失败一次，按铁律必须转父节点重新拆分。",
                suggested_children=self._fallback_children(task, "验收失败后重新拆分"),
            ).model_dump()
        else:
            payload = VerifierResult(
                verdict="pass",
                reason="模拟验收器已接受执行证据。",
                checks=["执行结果成功", "验收证据存在"],
                evidence=worker_result.evidence,
                leaf_misclassified=False,
                constitution_verdict="within_bounds",
            ).model_dump()
        return RunnerEnvelope(command="模拟验收器", exit_code=0, logs_path=None, payload=payload)

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        await asyncio.sleep(self.delay_seconds)
        if "[force-acceptance-repair]" in task.description:
            payload = AcceptanceResult(
                verdict="repair",
                reason="模拟父节点验收要求新增整合修复任务。",
                checks=["子节点必须组合成父节点目标"],
                evidence=[],
                constitution_verdict="within_bounds",
                suggested_children=self._fallback_children(task, "父节点整合修复"),
            ).model_dump()
        else:
            payload = AcceptanceResult(
                verdict="pass",
                reason="模拟父节点验收确认子节点组合后满足父节点目标。",
                checks=["所有子节点完成", "整合目标满足", "未突破宪法上下限"],
                evidence=[f"已验收 {len(children)} 个子节点"],
                constitution_verdict="within_bounds",
            ).model_dump()
        return RunnerEnvelope(command="模拟父节点验收器", exit_code=0, logs_path=None, payload=payload)

    def _fallback_children(self, task: TaskNode, prefix: str) -> list[ChildTaskSpec]:
        return [
            ChildTaskSpec(
                title=f"{prefix}：实现切片",
                description=f"缩小范围后完成 {task.title} 的最小实现部分。",
                scope=loads(task.scope_json, []),
                acceptance=["实现部分必须提供可检查证据。"],
                coverage_ids=["implementation"],
                estimated_leaf=True,
            ),
            ChildTaskSpec(
                title=f"{prefix}：验证切片",
                description=f"为 {task.title} 补齐验证、证据和边界检查。",
                scope=loads(task.scope_json, []),
                acceptance=["验证部分必须证明父任务可被准确验收。"],
                coverage_ids=["verification"],
                estimated_leaf=True,
            ),
        ]


class CodexExecRunner(BaseCodexRunner):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # 输出 schema 是**仓库里已入库的资产**（`<repo>/schemas/*.json`），不是运行状态。
        #
        # 这里以前写的是 `settings.workspace_dir.parent / "schemas"`——它**只是碰巧**等于
        # `<repo>/schemas`，因为 `workspace_dir` 的默认值恰好是 `<repo>/workspace`。
        # 一旦 workspace 不再是仓库的子目录（例如搬去 ~/.codex-battlefield 或
        # <state_home>/projects/<key>/），`.parent` 就指向 `~` 或 `projects/`，
        # `--output-schema` 指到不存在的文件。
        #
        # 失败形态值得记住（`docs/spec-state-project-decoupling.md` §11-H2）：
        # codex 报 `Failed to read output schema file ...` 后**秒退、0 token**，
        # 而状态机把它当成"这一轮跑完了"继续往前走——表面看像"跑完了但没做对"。
        # 实测过：5 秒、0 token。
        #
        # 判据：schema 的位置只能由 ROOT_DIR 决定，**永远不要**从任何
        # workspace/state 路径推导。有回归测试盯着这一点
        #（`tests/test_schema_dir_is_repo_anchored.py`）。
        self.schema_dir = ROOT_DIR / "schemas"
        self.codex_executable = self._resolve_codex_executable(settings.codex_bin)

    async def draft_constitution(self, goal: str, target_path: str | None = None) -> RunnerEnvelope:
        prompt = self._constitution_prompt(goal, target_path)
        return await self._run_codex_io(
            run_id="constitution-draft",
            cwd=self.settings.workspace_dir,
            kind="constitution",
            prompt=prompt,
            schema_name="constitution.schema.json",
            sandbox=self.settings.sandbox_for("constitution", "read-only"),
        )

    async def steward_chat(
        self,
        status_snapshot: str,
        capabilities: str,
        history: str,
        user_message: str,
    ) -> RunnerEnvelope:
        """管家（常驻全局 agent）的一轮对话。

        **与树里的调用有几处刻意不同**，每处都有理由：

        * **固定 run_id**：对话是**有状态的一条线**，不是每轮一份新记录。
          用固定 id 让最近一轮的提示词/日志可以被覆盖查看，不会把 `runs/` 撑爆。
        * **产物写到 `steward/` 而不是 `runs/`**：`runs/` 是**任务树**的运行历史，
          界面上按节点聚合。把对话混进去会让"这棵树跑了什么"变得读不懂。
        * **cwd = 状态根**：管家"在主工程树之外"。给它工程目录会让它有能力
          顺手改工程文件——而它第一版只读，不该有那个位置。
        * **无 max_concurrency 语义**：它不占树里的 agent 槽位（调用方不经调度器）。
        """
        prompt = self._steward_prompt(status_snapshot, capabilities, history, user_message)
        return await self._run_codex_io(
            run_id="steward-chat",
            cwd=Path(self.settings.workspace_dir),
            kind="steward",
            prompt=prompt,
            schema_name="steward-chat.schema.json",
            sandbox=self.settings.sandbox_for("steward", "read-only"),
            run_dir_override=Path(self.settings.workspace_dir) / "steward",
        )

    async def plan(self, task: TaskNode) -> RunnerEnvelope:
        prompt = self._planner_prompt(task)
        return await self._run_codex(
            task, "planner", prompt, "planner-decision.schema.json", self.settings.sandbox_for("planner", "read-only")
        )

    async def work(self, task: TaskNode) -> RunnerEnvelope:
        prompt = self._worker_prompt(task)
        return await self._run_codex(
            task, "worker", prompt, "worker-result.schema.json", self.settings.sandbox_for("worker", "workspace-write")
        )

    async def verify(self, task: TaskNode, worker_result: WorkerResult) -> RunnerEnvelope:
        prompt = self._verifier_prompt(task, worker_result)
        return await self._run_codex(
            task, "verifier", prompt, "verifier-result.schema.json", self.settings.sandbox_for("verifier", "workspace-write")
        )

    async def accept(self, task: TaskNode, children: list[dict]) -> RunnerEnvelope:
        prompt = self._acceptance_prompt(task, children)
        return await self._run_codex(
            task, "acceptance", prompt, "acceptance-result.schema.json", self.settings.sandbox_for("acceptance", "workspace-write")
        )

    async def _run_codex(
        self,
        task: TaskNode,
        kind: str,
        prompt: str,
        schema_name: str,
        sandbox: str,
    ) -> RunnerEnvelope:
        return await self._run_codex_io(
            run_id=task.id,
            cwd=Path(task.worktree_path or self.settings.workspace_dir),
            kind=kind,
            prompt=prompt,
            schema_name=schema_name,
            sandbox=sandbox,
        )

    async def _run_codex_io(
        self,
        run_id: str,
        cwd: Path,
        kind: str,
        prompt: str,
        schema_name: str,
        sandbox: str,
        run_dir_override: Path | None = None,
    ) -> RunnerEnvelope:
        # `run_dir_override` 给管家用：它的产物要落在 `steward/` 而不是 `runs/`，
        # 因为 `runs/` 是**任务树**的运行历史（界面按节点聚合），
        # 把一条长对话混进去会让"这棵树跑了什么"读不懂。
        run_dir = run_dir_override if run_dir_override is not None else self.settings.workspace_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        output_path = run_dir / f"{kind}-last-message.json"
        logs_path = run_dir / f"{kind}.jsonl"
        prompt_path = run_dir / f"{kind}-prompt.txt"
        activity_path = run_dir / f"{kind}-activity.txt"
        schema_path = self.schema_dir / schema_name
        prompt_path.write_text(prompt, encoding="utf-8")

        args = self._codex_exec_args(cwd, schema_path, output_path, sandbox, kind)
        command_display = " ".join(shlex.quote(part) for part in args) + " < prompt"
        attempts = 1 + max(0, self.settings.codex_retry_attempts)
        log_chunks: list[bytes] = []
        usage: dict | None = None

        for attempt in range(1, attempts + 1):
            if output_path.exists():
                output_path.unlink()
            if activity_path.exists():
                activity_path.unlink()
            log_chunks.append(f"\n--- codex {kind} attempt {attempt}/{attempts} ---\n".encode("utf-8"))
            logs_path.write_bytes(b"".join(log_chunks))
            stdout = b""
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=self._codex_env(kind, run_id),
                )
                exit_code, stdout, watchdog_message = await self._communicate_with_watchdog(
                    process,
                    prompt.encode("utf-8"),
                    output_path,
                    logs_path,
                    activity_path,
                    log_chunks,
                    kind,
                )
                usage = usage_from_codex_logs(logs_path)
                if watchdog_message:
                    if attempt < attempts and await self._sleep_before_retry(
                        logs_path, log_chunks, kind, attempt, attempts, watchdog_message
                    ):
                        continue
                    return RunnerEnvelope(
                        command=command_display,
                        exit_code=124,
                        logs_path=str(logs_path),
                        payload=self._attach_retryable_block_report(self._failure_payload(kind, watchdog_message), kind, attempts),
                        prompt_path=str(prompt_path),
                        last_message_path=str(output_path),
                        workdir=str(cwd),
                        usage=usage,
                    )
            except asyncio.CancelledError:
                if process is not None and process.returncode is None:
                    await self._terminate_process_tree(process)
                log_chunks.append(f"\n--- codex {kind} canceled by orchestrator ---\n".encode("utf-8"))
                logs_path.write_bytes(b"".join(log_chunks))
                raise
            except OSError as exc:
                logs_path.write_text(str(exc), encoding="utf-8")
                return RunnerEnvelope(
                    command=command_display,
                    exit_code=1,
                    logs_path=str(logs_path),
                    payload=self._failure_payload(kind, f"Codex CLI start failed: {exc}"),
                    prompt_path=str(prompt_path),
                    last_message_path=str(output_path),
                    workdir=str(cwd),
                )

            try:
                payload = self._read_payload(output_path, kind)
            except Exception as exc:
                payload = self._failure_payload(kind, f"Codex {kind} output parse failed: {exc}")
                if exit_code == 0:
                    exit_code = 1

            if exit_code == 0:
                return RunnerEnvelope(
                    command=command_display,
                    exit_code=exit_code,
                    logs_path=str(logs_path),
                    payload=payload,
                    prompt_path=str(prompt_path),
                    last_message_path=str(output_path),
                    workdir=str(cwd),
                    usage=usage,
                )

            if not output_path.exists():
                stdout_text = stdout.decode("utf-8", errors="replace")
                message = f"Codex {kind} exited with code {exit_code}."
                if stdout_text.strip():
                    message = f"{message}\n{stdout_text.strip()[:2000]}"
                payload = self._failure_payload(kind, message)

            retry_context = f"{self._payload_message(payload)}\n{stdout.decode('utf-8', errors='replace')}"
            if self._is_tool_internal_failure(retry_context):
                payload = self._attach_tool_internal_block_report(payload, kind, attempts)
                return RunnerEnvelope(
                    command=command_display,
                    exit_code=exit_code,
                    logs_path=str(logs_path),
                    payload=payload,
                    prompt_path=str(prompt_path),
                    last_message_path=str(output_path),
                    workdir=str(cwd),
                    usage=usage,
                )
            transient_failure = self._is_transient_failure(retry_context)
            if transient_failure and attempt < attempts:
                if await self._sleep_before_retry(
                    logs_path,
                    log_chunks,
                    kind,
                    attempt,
                    attempts,
                    self._payload_message(payload),
                    observed_text=retry_context,
                ):
                    continue
            if transient_failure:
                payload = self._attach_retryable_block_report(payload, kind, attempts)

            return RunnerEnvelope(
                command=command_display,
                exit_code=exit_code,
                logs_path=str(logs_path),
                payload=payload,
                prompt_path=str(prompt_path),
                last_message_path=str(output_path),
                workdir=str(cwd),
                usage=usage,
            )

        return RunnerEnvelope(
            command=command_display,
            exit_code=1,
            logs_path=str(logs_path),
            payload=self._failure_payload(kind, f"Codex {kind} failed after {attempts} attempts."),
            prompt_path=str(prompt_path),
            last_message_path=str(output_path),
            workdir=str(cwd),
            usage=usage,
        )

    def _codex_exec_args(
        self, cwd: Path, schema_path: Path, output_path: Path, sandbox: str, kind: str = ""
    ) -> list[str]:
        args = [
            self.codex_executable,
            "--ask-for-approval",
            "never",
        ]
        if self.settings.codex_disable_plugins:
            args.extend(["--disable", "plugins"])
        if self.settings.codex_profile:
            args.extend(["--profile", self.settings.codex_profile])
        args.extend(
            [
                "exec",
                "--cd",
                str(cwd),
                "--skip-git-repo-check",
                "--sandbox",
                sandbox,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "--json",
            ]
        )
        # 按角色取模型/推理档（B 部分）：`kind` 有独立档位就用它，没有就回退全局。
        # 不在这里做"acceptance 不许比 worker 弱"的自动纠正——静默改配置比报出来更糟，那条改由
        # `Settings.model_tier_warnings()` 暴露到看板上。
        model = self.settings.model_for(kind)
        if model:
            args.extend(["--model", model])
        effort = self.settings.reasoning_effort_for(kind)
        if effort:
            args.extend(["--config", f'model_reasoning_effort="{effort}"'])
        if self.settings.codex_ephemeral:
            args.append("--ephemeral")
        if self._should_ignore_user_config_for_sandbox(sandbox):
            args.append("--ignore-user-config")
        if self.settings.codex_ignore_rules:
            args.append("--ignore-rules")
        args.append("-")
        return args

    def _should_ignore_user_config_for_sandbox(self, sandbox: str) -> bool:
        """只读调用要不要加 `--ignore-user-config`。

        **配了隔离 `CODEX_HOME` 时绝不能加。**（2026-10-14 实测，codex 0.162.1）

        该 flag 的用途是让只读调用不需要用户的 auth/config；但它同时会让隔离
        `CODEX_HOME/config.toml` 里的 `model_provider` 失效——于是请求落到默认的
        `api.openai.com`，而那里没有可用的 bearer 凭据：

            ERROR codex_api::endpoint::responses_websocket:
            failed to connect ... 401 Unauthorized, url: wss://api.openai.com/v1/responses

        四种组合的实测（子进程、隔离 CODEX_HOME、provider=<你的> 指向自建端点）：

        | 额外 flag | 结果 |
        |---|---|
        | 无 | ✅ 走 Hub，正常回复 |
        | 仅 `--ignore-rules` | ✅ 走 Hub |
        | **仅 `--ignore-user-config`** | ❌ provider 被忽略，打到 api.openai.com |
        | 两者都有 | ❌ 同上 |

        所以元凶只有这一个 flag。Hub 接入（`docs/provider-setup.md`）本来就要求
        `CODEX_BATTLEFIELD_CODEX_IGNORE_USER_CONFIG=0`；现在把这条要求内建在这里，
        免得漏配一次就得到一堆 401 而不是一句"配置错了"。
        """
        if self.settings.codex_home:
            return False
        return self.settings.codex_ignore_user_config and sandbox == "read-only" and not self.settings.codex_profile

    async def _communicate_with_watchdog(
        self,
        process: asyncio.subprocess.Process,
        prompt_bytes: bytes,
        output_path: Path,
        logs_path: Path,
        activity_path: Path,
        log_chunks: list[bytes],
        kind: str,
    ) -> tuple[int, bytes, str | None]:
        stdout_chunks: list[bytes] = []
        start_time = time.monotonic()
        last_activity = start_time
        last_output_mtime = 0.0
        stdout_line_buffer = ""
        early_payload: dict | None = None
        early_stop_event = asyncio.Event()
        hard_timeout = max(0, self.settings.codex_timeout_seconds)
        idle_timeout = max(0, self.settings.codex_idle_timeout_seconds)

        timeout_note = (
            f"\n--- codex {kind} watchdog: hard_timeout={'disabled' if hard_timeout <= 0 else f'{hard_timeout}s'}, "
            f"idle_timeout={'disabled' if idle_timeout <= 0 else f'{idle_timeout}s'} ---\n"
        ).encode("utf-8")
        log_chunks.append(timeout_note)
        logs_path.write_bytes(b"".join(log_chunks))

        async def write_stdin() -> None:
            if process.stdin is None:
                return
            process.stdin.write(prompt_bytes)
            await process.stdin.drain()
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass

        async def read_stdout() -> None:
            nonlocal last_activity, stdout_line_buffer, early_payload
            if process.stdout is None:
                return
            while True:
                chunk = await process.stdout.read(4096)
                if not chunk:
                    if stdout_line_buffer and is_meaningful_codex_activity_line(stdout_line_buffer):
                        last_activity = time.monotonic()
                        self._write_activity_marker(activity_path, "stdout")
                    return
                stdout_chunks.append(chunk)
                log_chunks.append(chunk)
                self._append_log_chunk(logs_path, chunk)
                text = chunk.decode("utf-8", errors="replace")
                combined = stdout_line_buffer + text
                lines = combined.splitlines(keepends=True)
                if lines and not lines[-1].endswith(("\n", "\r")):
                    stdout_line_buffer = lines.pop()
                else:
                    stdout_line_buffer = ""
                if any(is_meaningful_codex_activity_line(line) for line in lines):
                    last_activity = time.monotonic()
                    self._write_activity_marker(activity_path, "stdout")
                if early_payload is None:
                    for line in lines:
                        payload = self._early_structured_payload_from_stdout_line(line, kind)
                        if payload is None:
                            continue
                        early_payload = payload
                        try:
                            output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                        except OSError:
                            early_payload = None
                            continue
                        last_activity = time.monotonic()
                        self._write_activity_marker(activity_path, "early-last-message")
                        early_stop_event.set()
                        break

        stdin_task = asyncio.create_task(write_stdin())
        stdout_task = asyncio.create_task(read_stdout())
        wait_task = asyncio.create_task(process.wait())
        early_stop_task = asyncio.create_task(early_stop_event.wait())
        watchdog_message: str | None = None

        try:
            while not wait_task.done():
                await asyncio.wait({wait_task, stdout_task, early_stop_task}, timeout=5.0, return_when=asyncio.FIRST_COMPLETED)
                now = time.monotonic()
                if output_path.exists():
                    try:
                        output_mtime = output_path.stat().st_mtime
                    except OSError:
                        output_mtime = last_output_mtime
                    if output_mtime > last_output_mtime:
                        last_output_mtime = output_mtime
                        last_activity = now
                        self._write_activity_marker(activity_path, "last-message")
                if early_payload is not None and not wait_task.done():
                    log_chunks.append(
                        f"\n--- codex {kind} early structured result captured; terminating extra CLI work ---\n".encode("utf-8")
                    )
                    termination_note = await self._terminate_process_tree(process)
                    if termination_note:
                        log_chunks.append(termination_note.encode("utf-8", errors="replace"))
                    logs_path.write_bytes(b"".join(log_chunks))
                    break
                if hard_timeout > 0 and now - start_time > hard_timeout:
                    watchdog_message = f"Codex {kind} hard timeout after {hard_timeout}s."
                elif idle_timeout > 0 and now - last_activity > idle_timeout:
                    watchdog_message = f"Codex {kind} idle watchdog fired after {idle_timeout}s without stdout or output-file activity."
                if watchdog_message:
                    log_chunks.append(f"\n--- {watchdog_message} ---\n".encode("utf-8"))
                    termination_note = await self._terminate_process_tree(process)
                    if termination_note:
                        log_chunks.append(termination_note.encode("utf-8", errors="replace"))
                    logs_path.write_bytes(b"".join(log_chunks))
                    break

            exit_code = await self._await_exit_bounded(process, wait_task, logs_path, log_chunks, kind)
            await asyncio.wait({stdout_task}, timeout=2.0)
            if early_payload is not None:
                return 0, b"".join(stdout_chunks), None
            return exit_code, b"".join(stdout_chunks), watchdog_message
        finally:
            for task in (stdin_task, stdout_task, wait_task, early_stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stdin_task, stdout_task, wait_task, early_stop_task, return_exceptions=True)

    def _early_structured_payload_from_stdout_line(self, line: str, kind: str) -> dict | None:
        if kind != "worker":
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            return None
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            return None
        text = item.get("text")
        if not isinstance(text, str):
            return None
        try:
            payload = repair_mojibake(json.loads(text))
            result = WorkerResult.model_validate(payload)
        except (json.JSONDecodeError, ValueError):
            return None
        if result.status != "needs_decomposition":
            return None
        return result.model_dump()

    @staticmethod
    def _write_activity_marker(activity_path: Path, source: str) -> None:
        try:
            activity_path.write_text(f"{time.time():.6f} {source}\n", encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _append_log_chunk(logs_path: Path, chunk: bytes) -> None:
        try:
            with logs_path.open("ab") as handle:
                handle.write(chunk)
        except OSError:
            pass

    async def _await_exit_bounded(
        self,
        process: asyncio.subprocess.Process,
        wait_task: "asyncio.Task[int]",
        logs_path: Path,
        log_chunks: list[bytes],
        kind: str,
    ) -> int:
        """等子进程退出，但**有上界**。超时就放弃等待并写明，绝不无限等下去。

        为什么必须有上界（2026-09-24 零 token 确定性复现，`workspace/_d4_repro.py --behaviors orphan`）：
        asyncio 的 `Process.wait()` 并不是"进程退出就返回"——`BaseSubprocessTransport._try_finish()`
        要求**进程已退出且所有管道都 disconnected**，唤醒 `wait()` 的那一步在 `_call_connection_lost` 里。
        所以只要有**任何一个后代进程**还攥着 stdout 的写端（codex 在 Windows 上是 `.cmd` → cmd.exe →
        node，node 再起孙进程；`taskkill /T /F` 杀不到已脱离的那个），EOF 就永远不来，
        `await wait_task` 就会一直挂着 —— 实测：空闲看门狗 6s 开火，之后**静默 55s**直到那个孙进程
        自己睡醒退出；进程树里当时**一个后代都没有**。这条路径上事件循环没有任何定时器
        （`py-spy` 看到 `_poll(ms=4294967295)` = INFINITE），与 2026-09-22 真实档冻结现场同形。

        **失效方向**：宁可"提前收手、把话说清楚"，也不要"为了拿一个准确的退出码而无限挂着"——
        挂着的代价是整棵任务树停在 `verifying` 且没有任何读数。只在**已经决定要停**之后才会真正用到
        这个上界（正常路径进来时 `wait_task` 已经 done，`await` 立刻返回，语义不变）。
        反面自检：给一个正常退出的子进程，这里必须返回**它真正的退出码**而不是 124
        （否则这条判据就成了"永远超时"）。
        """
        if wait_task.done():
            return self._exit_code_of(wait_task)
        done, _pending = await asyncio.wait({wait_task}, timeout=EXIT_WAIT_AFTER_STOP_SECONDS)
        if done:
            return self._exit_code_of(wait_task)
        note = (
            f"\n--- codex {kind} exit wait gave up after {EXIT_WAIT_AFTER_STOP_SECONDS}s: "
            "进程树已杀，但仍有后代进程持有 stdout 管道（asyncio 的 Process.wait 要等所有管道 EOF）。"
            "主动关闭本端管道后收尾。 ---\n"
        )
        log_chunks.append(note.encode("utf-8"))
        try:
            logs_path.write_bytes(b"".join(log_chunks))
        except OSError:
            pass
        # 关掉**我们自己**这一端：不关就一直被孙进程绑着（实测会留下 `unclosed transport`
        # + `Event loop is closed` 的退出噪声，等于每次放弃都漏一副管道句柄）。
        # 关掉之后 `read_stdout` 立刻 EOF、`_try_finish` 才跑得完，所以再给一次**短**机会拿真退出码。
        # 注意这里用 `asyncio.wait` 而不是 `wait_for`：后者超时会把 `wait_task` **取消**，
        # 第二次机会就永远拿不到退出码了（那条判据会恒定返回 124）。
        transport = getattr(process, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:  # noqa: BLE001 - 清理路径不许抛（一抛就又变成新的挂死点）
                pass
        done, _pending = await asyncio.wait({wait_task}, timeout=2.0)
        return self._exit_code_of(wait_task) if done else 124

    @staticmethod
    def _exit_code_of(wait_task: "asyncio.Task[int]") -> int:
        """已完成的 `process.wait()` 任务取退出码；被取消或抛异常时按超时算（124）。"""
        if wait_task.cancelled():
            return 124
        if wait_task.exception() is not None:
            return 124
        return int(wait_task.result())

    async def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> str:
        if process.returncode is not None:
            return ""
        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(killer.communicate(), timeout=10)
                return "\n--- Windows taskkill process tree ---\n" + stdout.decode("utf-8", errors="replace") + "\n"
            except (OSError, asyncio.TimeoutError) as exc:
                try:
                    process.kill()
                except ProcessLookupError:
                    return f"\n--- process tree termination fallback: process already exited after taskkill failure: {exc} ---\n"
                await self._wait_after_kill(process)
                return f"\n--- process tree termination fallback: killed root process only after taskkill failure: {exc} ---\n"
        try:
            process.kill()
        except ProcessLookupError:
            return "\n--- process termination skipped: process already exited ---\n"
        await self._wait_after_kill(process)
        return "\n--- process terminated ---\n"

    @staticmethod
    async def _wait_after_kill(process: asyncio.subprocess.Process) -> None:
        """杀完之后等一下就走：**不许**在这里无界等待（理由同 `_await_exit_bounded`）。

        失效方向：等不到就往下走（调用方还会再判一次退出码），而不是把"清理"变成新的挂死点。
        """
        try:
            await asyncio.wait_for(process.wait(), timeout=EXIT_WAIT_AFTER_STOP_SECONDS)
        except (asyncio.TimeoutError, TimeoutError):
            return

    async def _sleep_before_retry(
        self,
        logs_path: Path,
        log_chunks: list[bytes],
        kind: str,
        attempt: int,
        attempts: int,
        reason: str,
        observed_text: str = "",
    ) -> bool:
        return await retry.sleep_before_retry(self.settings, logs_path, log_chunks, kind, attempt, attempts, reason, observed_text)

    def _is_transient_failure(self, message: str) -> bool:
        return is_transient_failure_text(message)

    @staticmethod
    def _has_http_429_marker(text: str) -> bool:
        return has_http_429_marker(text)

    def _transient_failure_category(self, message: str) -> str:
        return retry.transient_failure_category(message)

    def _retry_after_seconds_from_message(self, message: str) -> int | None:
        return retry.retry_after_seconds_from_message(message)

    def _clock_retry_after_seconds_from_message(self, message: str) -> int | None:
        return retry.clock_retry_after_seconds_from_message(message)

    @staticmethod
    @staticmethod
    def _observed_local_time_from_message(message: str) -> datetime | None:
        return retry.observed_local_time_from_message(message)

    def _payload_message(self, payload: dict) -> str:
        return retry.payload_message(payload)

    def _attach_retryable_block_report(self, payload: dict, kind: str, attempts: int) -> dict:
        return retry.attach_retryable_block_report(self.settings, payload, kind, attempts)

    def _attach_tool_internal_block_report(self, payload: dict, kind: str, attempts: int) -> dict:
        return retry.attach_tool_internal_block_report(payload, kind, attempts)

    def _is_tool_internal_failure(self, text: str) -> bool:
        return retry.is_tool_internal_failure(text)

    @staticmethod
    def _is_local_codex_skill_failure(text: str) -> bool:
        return is_local_skill_failure(text)

    def _read_payload(self, output_path: Path, kind: str) -> dict:
        return retry.read_payload(output_path, kind)

    def _failure_payload(self, kind: str, message: str) -> dict:
        return retry.failure_payload(kind, message)

    def _resolve_codex_executable(self, configured: str) -> str:
        configured_path = Path(configured)
        if configured_path.exists():
            return str(configured_path)
        candidates = [configured]
        if sys.platform.startswith("win") and not configured.lower().endswith((".exe", ".cmd", ".bat")):
            candidates = [f"{configured}.cmd", f"{configured}.exe", configured]
        for candidate in candidates:
            found = shutil.which(candidate)
            if found and not found.lower().endswith(".ps1"):
                return found
        return configured

    def _resolve_godot_executable(self) -> str:
        return runner_tools.resolve_godot_executable(self.settings)

    def _ensure_godot_timeout_wrapper(self) -> Path:
        return runner_tools.ensure_godot_timeout_wrapper(self.settings.workspace_dir)

    def _ensure_placeholder_asset_tool(self) -> Path:
        return runner_tools.ensure_placeholder_asset_tool(self.settings.workspace_dir)

    def _asset_placeholder_guidance(self) -> str:
        return runner_tools.asset_placeholder_guidance(self.settings.workspace_dir)

    def _ensure_assert_no_match_tool(self) -> Path:
        return runner_tools.ensure_assert_no_match_tool(self.settings.workspace_dir)

    def _negative_match_guidance(self) -> str:
        return runner_tools.negative_match_guidance(self.settings.workspace_dir)

    def _ensure_json_check_tool(self) -> Path:
        return runner_tools.ensure_json_check_tool(self.settings.workspace_dir)

    def _json_check_guidance(self) -> str:
        return runner_tools.json_check_guidance(self.settings.workspace_dir)

    def _child_env(self) -> dict[str, str]:
        """宿主环境 → Codex 子进程环境。

        **默认收窄**（`settings.codex_env_strict`，见 `docs/spec-task-isolation.md` §2.4）。
        实测证据：原样继承会把宿主上**与此无关**的凭据交给每个节点
        （`scripts/verify_isolation_boundary.py` 打印出 `<env_key>`、`provider`、
        `provider`）——那就是"节点能读到不该有的凭据"，是 R2 要治的一条。

        失效方向：**判不了就丢**（fail-closed）。系统/运行时必需的键在固定名单里，凭据类按前缀放行；
        其余一律不传。要回到老行为设 `CODEX_BATTLEFIELD_CODEX_ENV_STRICT=0`。
        """
        if not self.settings.codex_env_strict:
            return dict(os.environ)
        prefixes = tuple(
            item.strip().upper()
            for item in str(self.settings.codex_env_allow_prefixes or "").split(",")
            if item.strip()
        )
        keep: dict[str, str] = {}
        for key, value in os.environ.items():
            upper = key.upper()
            if upper in CHILD_ENV_KEEP_NAMES or upper.startswith(CHILD_ENV_KEEP_PREFIXES) or upper.startswith(prefixes):
                keep[key] = value
        return keep

    def _ensure_provider_env_key(self, env: dict[str, str]) -> None:
        """保证隔离 `config.toml` 里 `env_key` 指名的变量**存在**。

        **为什么必须做**（2026-10-15 实测）：`<env_key>` 只在开发机当前 shell 里，
        而 API 常被 GUI/计划任务拉起 —— 那时环境变量不存在，codex 直接：

            {"type":"turn.failed","error":{"message":"Missing environment variable: `<env_key>`."}}

        整条 Hub 通道断掉。也就是说"认证在 Hub 里做"并不够：codex 仍要求
        `env_key` 指名的变量**存在**（本地代理不校验它的值）。

        所以这里按 config 里声明的 `env_key` 补一个**占位值**——不是塞真凭据：
        真的认证在 Hub 内部完成，这里只是满足 codex 的存在性要求。

        只补"用到的那个 provider 的 env_key"，不无脑塞所有 key：
        多塞一个变量就是多一处泄露面。
        """
        if not self.settings.codex_home:
            return
        config_path = Path(self.settings.codex_home) / "config.toml"
        try:
            text = config_path.read_text(encoding="utf-8")
        except OSError:
            return
        matches = re.findall(r'env_key\s*=\s*"([A-Za-z_][A-Za-z0-9_]*)"', text)
        for name in dict.fromkeys(matches):  # 保序去重
            if not env.get(name):
                env[name] = "local-placeholder"

    def _codex_env(self, kind: str = "", node_id: str = "") -> dict[str, str]:
        """给 Codex 子进程的环境。

        `kind` 用于算 Godot 槽位的静态优先级（见 `GODOT_QUEUE_ROLE_PRIORITY`）；
        `node_id` 用于**每节点隔离的 `CODEX_HOME`**（`docs/spec-task-isolation.md` §2.1）：
        会话、缓存、插件状态互不可见，且该目录里**没有** `auth.json`（R0-E4）。
        它治不了"凭据在子进程环境变量里"这一条（那是 `env_key` 模式的固有暴露面），别夸大。
        """
        # 宿主环境 → 子进程环境：**默认不把宿主的无关凭据交给每个节点**
        env = self._child_env()
        if self.settings.codex_home:
            home = Path(self.settings.codex_home)
            if node_id:
                home = home / "nodes" / node_id.replace("/", "-")
            home.mkdir(parents=True, exist_ok=True)
            if node_id:
                self._seed_node_home_config(Path(self.settings.codex_home), home)
            env["CODEX_HOME"] = str(home)
        env.update(
            {
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "LANG": env.get("LANG", "C.UTF-8"),
                "LC_ALL": env.get("LC_ALL", "C.UTF-8"),
                "POWERSHELL_TELEMETRY_OPTOUT": "1",
                "CODEX_BATTLEFIELD_TEXT_ENCODING": "utf-8",
                "CODEX_BATTLEFIELD_GODOT_STATIC_FALLBACK": "1",
                "CODEX_BATTLEFIELD_ASSET_TOOL": str(self._ensure_placeholder_asset_tool()),
                "CODEX_BATTLEFIELD_ASSERT_NO_MATCH_TOOL": str(self._ensure_assert_no_match_tool()),
                "CODEX_BATTLEFIELD_JSON_CHECK_TOOL": str(self._ensure_json_check_tool()),
                "CODEX_BATTLEFIELD_SKILL_LOOKUP_ENABLED": "1" if self.settings.skill_lookup_enabled else "0",
                "CODEX_BATTLEFIELD_SKILL_KM_URL": self.settings.skill_km_url,
            }
        )
        # P5-5：把当前请求的关联 ID 传进子进程。子进程（以及它调的工具脚本）读同一个名字，
        # 于是"界面上的那个 id"和"产物里的那个 id"能对上。
        # **没有就不写**（不塞空串）：下游按"变量不存在 = 不关联"处理，
        # 而空串会被读成"有关联但 id 是空的"。
        request_id = trace.current_request_id()
        if request_id:
            env[trace.ENV_NAME] = request_id
        # 隔离 config.toml 里 env_key 指名的变量必须存在，否则 codex 直接
        # `Missing environment variable` 失败（实测）。补占位值，见该方法注释。
        self._ensure_provider_env_key(env)
        godot_bin = self._resolve_godot_executable()
        if godot_bin:
            wrapper_bin = str(self._ensure_godot_timeout_wrapper())
            wrapper_script = str(Path(wrapper_bin).with_name("godot_with_timeout.py"))
            env["CODEX_BATTLEFIELD_REAL_GODOT_BIN"] = godot_bin
            env["CODEX_BATTLEFIELD_GODOT_TIMEOUT_SECONDS"] = str(max(1, self.settings.godot_timeout_seconds))
            env["CODEX_BATTLEFIELD_GODOT_LOCK_PATH"] = str(machine_root_of(self.settings) / "godot.global.lock")
            env["CODEX_BATTLEFIELD_GODOT_LOCK_WAIT_SECONDS"] = "1800"
            env["CODEX_BATTLEFIELD_GODOT_MAX_CONCURRENCY"] = str(max(1, self.settings.godot_max_concurrency))
            env["CODEX_BATTLEFIELD_GODOT_STATE_PATH"] = str(machine_root_of(self.settings) / "godot.resource.state.json")
            env["CODEX_BATTLEFIELD_GODOT_QUEUE_WEIGHT"] = str(GODOT_QUEUE_ROLE_PRIORITY.get(kind, 0))
            env["CODEX_BATTLEFIELD_GODOT_QUEUE_PRIORITY"] = "1" if self.settings.godot_queue_priority_enabled else "0"
            env["CODEX_BATTLEFIELD_GODOT_QUEUE_AGING_SECONDS"] = str(max(0, int(self.settings.godot_queue_aging_seconds)))
            env["CODEX_BATTLEFIELD_GODOT_WRAPPER_SCRIPT"] = wrapper_script
            env["GODOT_BIN"] = wrapper_bin
            env["GODOT4_BIN"] = wrapper_bin
            godot_dir = str(Path(godot_bin).parent)
            wrapper_dir = str(Path(wrapper_bin).parent)
            path = env.get("PATH", "")
            parts = [part for part in path.split(os.pathsep) if part]
            if wrapper_dir not in parts:
                parts = [wrapper_dir, *parts]
            if godot_dir not in parts:
                parts = [godot_dir, *parts]
            env["PATH"] = os.pathsep.join(parts)
        return env

    def _seed_node_home_config(self, base_home: Path, node_home: Path) -> None:
        """把基座 `CODEX_HOME/config.toml` 复制进每节点 home。

        **为什么必须有这一步**（2026-10-14 实测事故）：§2.1 的每节点隔离把
        `CODEX_HOME` 指到 `<base>/nodes/<node-id>`。codex **不会**从父目录继承配置——
        它只读自己 `CODEX_HOME` 下的 `config.toml`。于是节点 home 里那个文件是空的，
        `model_provider = "provider"` 和 `[model_providers.provider]` 整段丢失，请求回落到默认
        `api.openai.com`，而那里没有凭据：

            ERROR codex_api::endpoint::responses_websocket:
            failed to connect ... 401 Unauthorized, url: wss://api.openai.com/v1/responses

        危险之处在于它**不是报错停机**，而是"树照跑、只是全打错端点"。所以这一条不是
        可选优化，是隔离与 Hub 两件事的唯一相容点。

        语义选择：**每次覆盖**（而不是"只在缺失时写一次"）。基座配置是 provider/模型/沙箱档
        的唯一真相；一旦 node home 留下过期副本，改基座就只对"新节点"生效，
        老的继续静默打错端点——那正是本 bug 的形态本身。文件只有几百字节，树规模下无压力。

        基座缺 `config.toml` 时**静默跳过**（不凭猜测造一份）：此时行为退回改动前，
        不会比原来更糟；真需要 provider 的场景由 `scripts/use_provider.ps1` 先落基座配置。
        """
        source = base_home / "config.toml"
        if not source.is_file():
            return
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            return
        target = node_home / "config.toml"
        if target.is_file():
            try:
                if target.read_text(encoding="utf-8") == text:
                    return
            except OSError:
                pass
        try:
            target.write_text(text, encoding="utf-8")
        except OSError:
            # 基座不可读/节点 home 不可写时不让整棵树死在环境准备阶段：
            # 拿不到 provider 的表现是节点 401 并最终 blocked，那是可诊断的；
            # 抛在这里的表现是"树根本没起来"。
            return

    def _godot_validation_guidance(self) -> str:
        return prompts.godot_validation_guidance(self.settings)

    def _skill_lookup_guidance(self) -> str:
        return prompts.skill_lookup_guidance(self.settings)

    def _project_skill_guidance(self) -> str:
        return prompts.project_skill_guidance(self.settings)

    def _visual_validation_guidance(self) -> str:
        return prompts.visual_validation_guidance()

    def _read_only_validation_guidance(self) -> str:
        return prompts.read_only_validation_guidance()

    def _windows_shell_guidance(self) -> str:
        return prompts.windows_shell_guidance()

    def _text_integrity_guidance(self) -> str:
        return prompts.text_integrity_guidance()

    def _project_context_summary(self, task: TaskNode) -> str:
        return project_context.project_context_summary(task)

    def _context_roots(self, task: TaskNode) -> list[tuple[str, Path]]:
        return project_context.context_roots(task)

    def _looks_like_scope_path(self, value: str) -> bool:
        return project_context.looks_like_scope_path(value)

    def _summarize_context_root(self, label: str, root: Path) -> str:
        return project_context.summarize_context_root(label, root)

    def _safe_text_excerpt(self, path: Path, max_chars: int) -> str:
        return project_context.safe_text_excerpt(path, max_chars)

    def _planner_prompt(self, task: TaskNode) -> str:
        return prompts.planner_prompt(self.settings, task)

    def _inherited_constraints_block(self, task: TaskNode) -> str:
        return prompts.inherited_constraints_block(task)

    def _worker_prompt(self, task: TaskNode) -> str:
        return prompts.worker_prompt(self.settings, task)

    def _verifier_prompt(self, task: TaskNode, worker_result: WorkerResult) -> str:
        return prompts.verifier_prompt(self.settings, task, worker_result)

    def _steward_prompt(
        self, status_snapshot: str, capabilities: str, history: str, user_message: str
    ) -> str:
        return prompts.steward_prompt(
            status_snapshot,
            capabilities,
            history,
            user_message,
            read_only=self.settings.steward_read_only,
        )

    def _constitution_prompt(self, goal: str, target_path: str | None) -> str:
        return prompts.constitution_prompt(goal, target_path)

    def _acceptance_prompt(self, task: TaskNode, children: list[dict]) -> str:
        return prompts.acceptance_prompt(self.settings, task, children)

    def _default_constitution_data(self, message: str) -> dict:
        return prompts.default_constitution_data(message)


def make_runner(settings: Settings, mode: str | None = None) -> BaseCodexRunner:
    runner_mode = mode or settings.runner_mode
    if runner_mode == "real":
        return CodexExecRunner(settings)
    return MockCodexRunner(settings.mock_delay_seconds)
