"""整体验收审计（判据 Q1–Q9）：把"每项都要有可反驳的判据、跑通 verify_all、双仓同步"
这三句话变成**能红**的检查。
## 为什么要写这一组

这个目标本身就是一条"验收标准"：

> 每项都要有可反驳的判据、跑通 verify_all、并提交推送到公开仓与同步内部仓。

如果不把它写成判据，它就只能靠**报告里的叙述**成立 —— 而叙述不会变红。具体防三件事：

1. **ROADMAP 里长出一条没有状态的行**：没人能判断它是做完了还是根本没做；
2. **声明"已落地"却没有对应的判据文件**：文档比代码乐观，这是最常见的腐坏方式
   （看到"未落地"也**必须**有记录 —— 悄悄退化成"没这回事"同样是骗）；
3. **某次提交忘了同步内部仓**：内部仓是公开仓 + 7 个 overlay 文件，
   一旦漂移，"内部那 7 个文件"就不在最新基线上。

## 边界

* 这一组**不判断"判据够不够强"**（那要靠变异测试，各阶段自己的测试文件里做了）；
  它只判断"声明与证据**对得上**"。
* 也不判断"功能好不好用" —— 那是使用者的事。
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROADMAP = REPO / "ROADMAP.md"
AUDIT = REPO / "docs" / "acceptance-audit.md"
TESTS = REPO / "api" / "tests"

#: 目标覆盖的阶段（P0/P1 是更早的里程碑，不在本次目标里）。
TARGET_STAGES = ("P2", "P3", "P4", "P5", "P6")

#: 任何"状态"都必须能被这几个词之一说清。**少一个都不行**：
#: 只写"已落地"而不说代价，等于把未验的部分藏起来。
STATUS_MARKERS = ("已落地", "部分落地", "未做", "未验证", "未验")

#: 声明"已落地/部分落地"的行 → 必须在仓库里能找到的判据文件。
#: **裸文件名**指 `api/tests/`，带 `/` 的按仓库根解析（前端判据就是这样写的）。
#: 这条表是**人工维护**的：它是"声明"与"证据"之间的那根线，不能让脚本自己猜。
#:
#: ⚠️ 2026-10-18 修掉一处**假的对应**：`P5-2`（前端体量/虚拟化）与 `P5-3`（端到端）原来分别
#: 指向 `test_provider_matrix.py` / `test_ci_workflow.py` —— Q2 只查"文件在不在"，于是**判据
#: 指着无关的文件也能绿**。现在它们指向真正的前端判据，并由 Q10 额外要求"文件里得有断言"。
JUDGEMENT_FILES: dict[str, tuple[str, ...]] = {
    "P2-2": ("test_settings_panel.py",),
    "P2-3": ("test_runtime_logs.py",),
    "P2-4": ("test_diagnostics.py",),
    "P2-5": ("test_state_admin.py",),
    "P2-6": ("web/src/i18n.test.ts", "web/src/i18n.detect.test.ts"),
    # P3-1 是"部分落地"（时间窗守卫 + burst 上限；token/金额硬闸未做），判据仍在同一条链上。
    "P3-1": ("test_budget.py", "test_budget_ceiling.py"),
    "P3-2": ("test_cost.py",),
    "P3-3": ("test_cost.py",),
    "P3-4": ("test_cross_project.py",),
    "P3-5": ("test_auto_policy.py",),
    "P3-6": ("test_scale.py",),
    "P4-1": ("test_delivery.py",),
    "P4-2": ("test_watchdog_tuning.py",),
    "P4-3": ("test_attention.py",),
    "P4-4": ("test_action_risk.py",),
    "P4-5": ("test_conclusions.py",),
    "P5-1": ("test_metrics.py",),
    "P5-2": ("web/src/virtualization.test.ts", "web/src/fileSize.test.ts"),
    "P5-3": ("web/e2e/l4-flow.spec.ts",),
    "P5-4": ("test_retention.py",),
    "P5-5": ("test_trace_correlation.py",),
    "P5-6": ("test_crash_injection.py",),
    "P6-1": ("test_container.py",),
    "P6-2": ("test_platform_support.py",),
    "P6-3": ("test_ci_workflow.py",),
    "P6-5": ("test_provider_matrix.py",),
}


def roadmap_rows() -> list[tuple[str, str]]:
    """ROADMAP 里所有交付物行：`(编号, 整行)`。只认 `P<阶段>-<序号>` 开头的行。"""

    rows: list[tuple[str, str]] = []
    for line in ROADMAP.read_text(encoding="utf-8").split("\n"):
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if not cells:
            continue
        match = re.fullmatch(r"(P\d-\d+)", cells[0])
        if match:
            rows.append((match.group(1), line))
    return rows


def target_rows() -> list[tuple[str, str]]:
    return [(item, line) for item, line in roadmap_rows() if item[:2] in TARGET_STAGES]


def judgement_path(name: str) -> Path:
    """判据文件的位置：裸文件名指 `api/tests/`，带 `/` 的按仓库根解析。"""

    return REPO / name if "/" in name else TESTS / name


# --- Q1 每个交付物都有状态 ---------------------------------------------------


def test_q1_every_target_deliverable_declares_a_status():
    rows = target_rows()
    assert len(rows) >= 25, f"目标阶段应当有 25 条以上交付物，实际 {len(rows)}"
    missing = [item for item, line in rows if not any(marker in line for marker in STATUS_MARKERS)]
    assert missing == [], (
        f"这些交付物没有说清状态：{missing}。"
        f"必须用 {'/'.join(STATUS_MARKERS)} 之一交代（只写交付物不写状态 = 没人知道做完没有）"
    )


def test_q1_the_audit_covers_every_target_deliverable():
    audit = AUDIT.read_text(encoding="utf-8")
    missing = [item for item, _line in target_rows() if item not in audit]
    assert missing == [], f"审计文档里缺这些交付物：{missing}"


# --- Q2 声明与证据对得上 -----------------------------------------------------


@pytest.mark.parametrize(("item", "files"), sorted(JUDGEMENT_FILES.items()))
def test_q2_claimed_deliverables_have_judgement_files(item: str, files: tuple[str, ...]):
    row = next((line for ident, line in roadmap_rows() if ident == item), None)
    assert row is not None, f"ROADMAP 里没有 {item}"
    assert any(marker in row for marker in ("已落地", "部分落地")), f"{item} 没有声明落地，不该在这张表里"
    for name in files:
        path = judgement_path(name)
        assert path.is_file(), f"{item} 声明的判据文件不存在：{path.relative_to(REPO).as_posix()}"


def test_q2_no_orphan_entries_in_the_table():
    """表里不许有 ROADMAP 里已经不存在的编号（否则这条表会慢慢变成化石）。"""

    known = {item for item, _line in roadmap_rows()}
    orphans = sorted(set(JUDGEMENT_FILES) - known)
    assert orphans == [], f"这张表引用了 ROADMAP 里没有的编号：{orphans}"


# --- Q3 未落地的部分必须被记录 -----------------------------------------------


def test_q3_roadmap_gaps_are_named_in_the_audit():
    """ROADMAP 里带"未验/未做"字样的交付物，编号必须出现在审计的**缺口清单那一节**里。

    注意是"那一节"，不是"整份文档"：第一版搜全文，结果**变异测试没抓住** ——
    把 ROADMAP 里 P6-1 的未验说明删掉之后，那个编号仍然出现在审计的"逐条对照"表里，
    判据照样绿（"进了缺口清单"这件事根本没被检查）。改成只搜第 2 节。
    """

    audit = AUDIT.read_text(encoding="utf-8")
    section = audit.split("## 2.", 1)[-1].split("## 3.", 1)[0] if "## 2." in audit else ""
    assert section, "审计里必须有第 2 节（缺口清单）"

    gaps = [item for item, line in target_rows() if any(marker in line for marker in ("未验证", "未做", "未验"))]
    assert gaps, "至少应当有带未验说明的条目（没有的话这条判据是空转的）"
    missing = [item for item in gaps if item not in section]
    assert missing == [], f"这些条目在 ROADMAP 里说明未验，但审计的第 2 节里没有：{missing}"


#: 审计第 2 节里那一行的编号（`| P6-1 | 缺什么 |`）。
AUDIT_GAP_ROW = re.compile(r"^\|\s*(P\d-\d+)\s*\|", re.MULTILINE)


def test_q3b_audit_gaps_are_still_gaps_in_the_roadmap():
    """反过来也要成立：审计缺口清单里列的条目，ROADMAP 里**必须仍然是缺口**。

    为什么需要这一半：Q3 只查"ROADMAP 的缺口 ⊆ 审计"。变异测试暴露出另一半是空的 ——
    把 ROADMAP 里 P6-1 的未验说明删掉之后 Q3 照样绿，而**审计还在说 P6-1 是缺口**：
    两边不一致，却没有任何判据会发现。补上反向检查，那个变异才会红。
    """

    audit = AUDIT.read_text(encoding="utf-8")
    section = audit.split("## 2.", 1)[-1].split("## 3.", 1)[0] if "## 2." in audit else ""
    listed = set(AUDIT_GAP_ROW.findall(section))
    assert listed, "审计的缺口清单里应当有条目"

    actually_gaps = {
        item for item, line in target_rows() if any(marker in line for marker in ("未验证", "未做", "未验"))
    }
    stale = sorted(listed - actually_gaps)
    assert stale == [], (
        f"审计还把这些条目列为缺口，但 ROADMAP 里已经不带未验说明了：{stale}。"
        "要么补上 ROADMAP 的说明，要么从审计的缺口清单里删掉 —— 两边不许各说各话"
    )


# --- Q4 门禁里真的有那些检查 ------------------------------------------------


def test_q4_verify_all_covers_the_new_gates():
    text = (REPO / "scripts" / "verify_all.py").read_text(encoding="utf-8")
    for token in ("verify_provider_matrix.py", "playwright", "check_markdown_layout.py", "check_ps1_encoding"):
        assert token in text, f"`verify_all` 里少了这一步：{token}"


def test_q4_frontend_side_judgements_exist_in_the_web_tree():
    """前端那部分判据在 `web/src/*.test.ts`（vitest），不在 api/tests —— 单独查一次。"""

    web_src = REPO / "web" / "src"
    empty: list[str] = []
    for name in ("fileSize.test.ts", "virtualization.test.ts", "i18n.test.ts", "polling.test.ts"):
        path = web_src / name
        assert path.is_file(), f"前端判据文件不存在：web/src/{name}"
        # 存在还不够：一个空文件、或者只有注释的文件同样能让"存在"成立。
        # 这与 Q10（后端判据必须真的有断言）是同一条规矩的前端版本。
        text = path.read_text(encoding="utf-8")
        if not ("expect(" in text and ("test(" in text or "it(" in text)):
            empty.append(name)
    assert empty == [], f"这些前端判据文件里找不到断言：{empty}"


def test_q4_e2e_spec_exists_and_the_workflow_runs_it():
    assert (REPO / "web" / "e2e" / "l4-flow.spec.ts").is_file()
    workflow = (REPO / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
    assert "verify_all.py" in workflow, "CI 必须跑同一套门禁"


def test_q4_scale_first_screen_judgement_exists():
    """P3-6/P5-2 的"千节点首屏"这条判据必须有**真浏览器**的落点，且树是同形的。

    只有 `virtualization.test.ts` 那种"交给布局器的节点有界"是不够的：有界 ≠ 快。
    """

    spec = REPO / "web" / "e2e" / "perf-first-screen.spec.ts"
    seeder = REPO / "web" / "e2e" / "seed_scale_tree.py"
    assert spec.is_file(), "千节点首屏的 E2E 判据不见了"
    assert seeder.is_file(), "造同形千节点树的脚本不见了（判据会退化成量一棵空树）"
    text = spec.read_text(encoding="utf-8")
    # 目标与棘轮必须都在：只有棘轮的话，"2s 没达到"这件事会从代码里消失。
    assert "TARGET_MS = 2000" in text, "判据里必须留着 ROADMAP 的目标值（2000ms）"
    assert "RATCHET_MS" in text, "判据里必须有防变慢的棘轮"
    # 走生产产物而不是 dev server：dev server 的数字不配叫"首屏"。
    assert "API_PORT" in text and "dist" in text, "这条判据必须量 API 托管的生产产物"


# --- Q11 文档里的"数量"必须与代码一致 --------------------------------------
#
# 真实腐坏（2026-10-18 发现）：ROADMAP P2-2 写"设置面板 19 项"，而
# `settings_registry.catalog()` 实际有 **27** 项 —— 这一年里加过模型/沙箱/预算三批设置，
# 文档停在了最早那个数字上。这类"数字过期"不会让任何东西变红，
# 却会让"面板有 19 项"这句话在评审时被当成事实引用。


def test_q11_the_documented_settings_count_matches_the_registry() -> None:
    import re

    from app import settings_registry

    row = next(line for item, line in roadmap_rows() if item == "P2-2")
    match = re.search(r"\*\*(\d+) 项\*\*", row) or re.search(r"(\d+) 项", row)
    assert match, f"P2-2 那行没有写设置项数：{row[:200]}"
    documented = int(match.group(1))
    actual = len(settings_registry.catalog())
    assert documented == actual, (
        f"ROADMAP 说设置面板有 {documented} 项，实际 `settings_registry.catalog()` 有 {actual} 项 ——"
        " 加了设置却没更新文档，或文档写了不存在的项"
    )


# --- Q12/Q13 文档里点名的文件与端点必须真实存在 ------------------------------
#
# 这两条防的是同一类腐坏：**文档指着不存在的东西**。
# 2026-10-18 扫出来的量：文档里点名的仓库内文件 372 处、不存在的只有 4 处，
# 而那 4 处都是**故意的**（扫描器的负样本、历史脚本名）—— 所以这里用一张
# 带理由的允许表，而不是放宽成"随便写"。
#
# 允许表两头都要判：**表里的东西如果哪天真的存在了，也要红**（说明这张表已经过期）。

#: 文档里点名、但**故意**不存在的路径 → 为什么允许。
MISSING_BY_DESIGN: dict[str, str] = {
    "docs/_leak_probe.md": "public-tree-hygiene 的负样本：扫描器要能抓到它（文件本身不该存在）",
    "web/_browser_probe.mjs": "e2e-browser 文档里的临时探针示例，用完即删",
    "scripts/battlefield-project.py": "spec 里记录的历史脚本名（已改名/并入别处），保留是为了追溯",
    "scripts/project_key_reference.py": "同上：spec 记录的历史脚本名",
}


def _doc_files() -> list[Path]:
    return [ROADMAP, *sorted((REPO / "docs").glob("*.md"))]


def _is_gitignored(relative: str) -> bool:
    """这个路径是否被 `.gitignore` 忽略（= 构建/运行产物，干净检出里本来就没有）。

    为什么 Q12 需要它（2026-10-18 第一次真 CI 的红）：
    文档里会自然地写 `api/.venv/Scripts/python.exe` 这类**由 CI 自己造出来**的路径
    （workflow 里就是 `python -m venv api/.venv`）。本地工作树有它、干净检出没有 ——
    Q12 因此在 **两个平台的 CI 上都红了**（`1 failed, 1742 passed`），
    而它要防的是"文档指着不该存在的东西"，不是"文档提到构建产物"。
    """

    import subprocess

    def matches(candidate: str) -> bool:
        try:
            completed = subprocess.run(
                ["git", "check-ignore", "-q", candidate],
                cwd=str(REPO),
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    # ⚠️ 要连"子路径"一起试：`.gitignore` 里的目录规则带尾斜杠（`dist/`），
    # 而 git 只有在路径**确实是个目录**时才认它 —— 干净检出里 `web/dist` 根本不存在，
    # 于是 `check-ignore web/dist` 返回"不忽略"，Q12 在 CI 上照样红
    # （本地有那个目录，所以本地永远绿）。试一下 `<path>/x` 就能让目录规则匹配上。
    return matches(relative) or matches(f"{relative}/x")


def test_q12_repo_file_references_in_docs_exist() -> None:
    pattern = re.compile(r"`((?:scripts|api|web|docs|schemas)/[A-Za-z0-9_\-./]+)`")
    missing: dict[str, set[str]] = {}
    for path in _doc_files():
        for ref in pattern.findall(path.read_text(encoding="utf-8")):
            if ref.endswith("/") or "*" in ref:
                continue
            if not (REPO / ref).exists():
                missing.setdefault(ref, set()).add(path.name)

    # 三类放行：① 明确"故意不存在"的允许表；② 被 gitignore 的构建/运行产物
    # （`api/.venv`、`web/dist` 这类由 CI 或本机自己造出来的路径）；
    # 其余一律算文档撒谎。
    unexpected = sorted(
        item
        for item in set(missing) - set(MISSING_BY_DESIGN)
        if not _is_gitignored(item)
    )
    assert unexpected == [], (
        "文档里点名了不存在的文件（要么补文件、要么写进 MISSING_BY_DESIGN 并说明理由，"
        "要么它本来就该是 gitignore 的产物）："
        f"{[(item, sorted(missing[item])[:3]) for item in unexpected]}"
    )
    stale = sorted(ref for ref in MISSING_BY_DESIGN if (REPO / ref).exists())
    assert stale == [], f"允许表里的路径现在真的存在了 —— 这张表过期了，删掉这些条目：{stale}"


def test_q13_api_paths_claimed_in_docs_are_routed() -> None:
    """文档里写的 `/api/...` 必须真的挂在 FastAPI 上（改名漏改文档时这里会红）。"""

    from app.main import app

    def normalize(path: str) -> str:
        path = re.sub(r"\{[^}]*\}", "*", path)
        path = re.sub(r"<[^>]*>", "*", path)
        return path.rstrip("/") or "/"

    routes = {
        normalize(route.path) for route in app.routes if getattr(route, "path", "").startswith("/api")
    }
    claimed: dict[str, set[str]] = {}
    for path in _doc_files():
        for found in re.findall(r"`(/api/[A-Za-z0-9_\-/{}.<>]+)`", path.read_text(encoding="utf-8")):
            claimed.setdefault(found, set()).add(path.name)
    assert claimed, "文档里一个 /api 路径都没写？那这条判据已经空转"

    missing = {item: sorted(where)[:3] for item, where in claimed.items() if normalize(item) not in routes}
    assert missing == {}, f"文档声称存在、但应用里没有的端点：{missing}"


# --- Q14 文档里的环境变量必须真的被读 --------------------------------------
#
# 一条真实教训（2026-10-18 写这条判据之前）：第一版只扫 `.py`，于是
# `CODEX_BATTLEFIELD_CHECKPOINT_GIT_ANCHOR` 被报成"文档写了、代码没有" —— 实际上它由
# `scripts/battlefield_watchdog.ps1` 读（`$env:CODEX_BATTLEFIELD_CHECKPOINT_GIT_ANCHOR`）。
# **扫描范围窄会制造假红**，而假红比漏检更快毁掉一条判据：看到一次误报之后，
# 之后每次红都会被当成噪声。所以这里刻意把 py / ps1 / ts / yml 一起扫。


def _implementation_files() -> list[Path]:
    """实现文件清单。**遍历时就剪枝**，不是遍历完再过滤。

    2026-10-18 的教训：第一版用 `rglob` 再过滤 `node_modules` —— 在 WSL 的 `/mnt/d`（drvfs）上，
    `rglob` 会把 `web/node_modules/.pnpm/**` 全部走一遍，于是 Q14 **跑到 90 秒超时**
    （Windows 上恰好够快，所以本地一直绿）。判据自己踩性能坑，和产品踩性能坑一样要修。
    """

    suffixes = {".py", ".ps1", ".ts", ".tsx", ".mjs", ".json", ".yml", ".yaml", ".sh"}
    skip_dirs = {"node_modules", ".venv", "dist", ".git", "__pycache__", "test-results", "playwright-report"}
    files: list[Path] = []
    for base in ("api", "scripts", "web", ".github"):
        root = REPO / base
        if not root.is_dir():
            continue
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in skip_dirs]
            for name in filenames:
                if Path(name).suffix in suffixes:
                    files.append(Path(current) / name)
    return files


def test_q14_documented_environment_variables_exist() -> None:
    env_re = re.compile(r"CODEX_BATTLEFIELD_[A-Z0-9_]+")
    documented: dict[str, set[str]] = {}
    for path in _doc_files():
        for name in env_re.findall(path.read_text(encoding="utf-8")):
            documented.setdefault(name, set()).add(path.name)
    assert documented, "文档里一个环境变量都没提？那这条判据已经空转"

    implemented: set[str] = set()
    for path in _implementation_files():
        try:
            implemented.update(env_re.findall(path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue

    missing = {name: sorted(where)[:3] for name, where in documented.items() if name not in implemented}
    assert missing == {}, (
        "这些环境变量只在文档里出现、没有任何实现文件读它们（写了也不会生效）："
        f"{missing}"
    )


# --- Q15 docs/README.md 必须索引到每一份文档 --------------------------------
#
# 2026-10-18 扫出来的：`docs/` 有 35 份文档，索引里只链了 32 份 ——
# 漏掉的三个是 `spec-state-project-decoupling.md` / `spec-steward.md` /
# `spec-watchdog-steward.md`（都是被代码注释反复引用的那种"核心 spec"）。
# 孤儿文档不会红、不会被读，只会在下一次"我看看有哪些文档"时不存在。


def test_q15_every_doc_is_linked_from_the_index() -> None:
    index = (REPO / "docs" / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\]\(([A-Za-z0-9_\-./]+\.md)\)", index))
    linked |= set(re.findall(r"`([A-Za-z0-9_\-]+\.md)`", index))
    existing = sorted(path.name for path in (REPO / "docs").glob("*.md") if path.name != "README.md")
    orphans = [name for name in existing if name not in linked]
    assert orphans == [], (
        "这些文档没有被 docs/README.md 索引（读者找不到它们）：" f"{orphans}"
    )


# --- Q5 双仓同步（有远端时才算） --------------------------------------------


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def test_q5_internal_overlay_is_exactly_seven_files():
    """内部仓 = 公开仓 + 7 个 overlay 文件（这件事写在**内部仓**的根文件里）。

    ⚠️ 这里刻意**不写那 7 个文件名**：本文件是公开树的一部分，而其中 5 个 overlay
    是只在内部仓存在的文件，名字本身就带内部标识 —— 写进公开仓就是泄漏
    （第一版审计正是这么写的，`scripts/gen_acceptance_audit.py` 与生成的
    `docs/acceptance-audit.md` 都把名字列了出来；见 `docs/public-tree-hygiene.md`）。
    要看到具体名字，失败信息里会打出来（`changed`），运行日志不属于公开树。

    没有远端引用时**跳过**（而不是假装通过）：这台机器上没配 `internal` remote 的克隆
    不该因为这条变红。
    """

    check = _git("rev-parse", "--verify", "internal/main")
    if check is None or check.returncode != 0:
        pytest.skip("本地没有 internal/main 引用（未同步过的克隆）")
    diff = _git("diff", "--name-status", "origin/main", "internal/main")
    if diff is None or diff.returncode != 0:
        pytest.skip("拿不到 origin/main 与 internal/main 的差异")
    changed = [line for line in diff.stdout.splitlines() if line.strip()]
    assert len(changed) == 7, f"内部仓差异应当是 7 个文件，实际 {len(changed)}：{changed}"
    # 审计里写的是"5 个只存在于内部仓、2 个是公开树里也有的（被改写了）"。
    # 把这两个数字也钉住：只钉总数的话，有人把一个"新增"改成"改写"（或反过来）
    # 仍然会过，而那句话就成了假的 —— 审计里每一句可核对的陈述都要有判据。
    added = [line for line in changed if line.startswith("A")]
    modified = [line for line in changed if line.startswith("M")]
    assert len(added) == 5, f"只存在于内部仓的应当是 5 个，实际 {len(added)}：{added}"
    assert len(modified) == 2, f"内部仓改写的公开文件应当是 2 个，实际 {len(modified)}：{modified}"


def test_q5_audit_records_the_two_remote_tips():
    """审计里要记下**当时的两个远端 tip** —— 报告说"已同步"，得能对上具体提交。"""

    audit = AUDIT.read_text(encoding="utf-8")
    assert re.search(r"公开仓[^\n]*`[0-9a-f]{7,40}`", audit), "审计里要写公开仓的具体提交"
    assert "内部仓" in audit, "审计里要写内部仓的状态"


# --- Q6 审计文档必须与生成器一致（防止"过期的安全感"） -----------------------


def test_q6_audit_is_in_sync_with_its_generator(tmp_path):
    """审计文档必须是 `scripts/gen_acceptance_audit.py` 的**当前输出**。

    为什么这条重要：审计里写着两个远端 tip、每条交付物的状态与判据文件。手改一次之后
    它就会**慢慢过期**，而过期的审计比没有审计更坏 —— 它给的是过期的安全感。
    生成器带 `--check`，这里直接调它做比对（同一个真相，不抄第二份逻辑）。
    """

    generator = REPO / "scripts" / "gen_acceptance_audit.py"
    assert generator.is_file(), "生成器不见了 —— 那审计就只能手改，必然过期"
    proc = subprocess.run(
        [sys.executable, str(generator), "--check"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, (
        "审计文档与生成器的输出不一致（过期了）。"
        f"跑 `python scripts/gen_acceptance_audit.py` 重新生成。输出：{proc.stdout.strip()}"
    )


def test_q6_generator_declares_a_judgement_for_every_deliverable():
    """生成器里的"判据在哪"这张表要覆盖所有交付物 —— 缺一条就会印出"（未登记）"。"""

    source = (REPO / "scripts" / "gen_acceptance_audit.py").read_text(encoding="utf-8")
    missing = [item for item, _line in target_rows() if f'"{item}":' not in source]
    assert missing == [], f"生成器的 JUDGEMENTS 里缺这些交付物：{missing}"

    audit = AUDIT.read_text(encoding="utf-8")
    assert "（未登记）" not in audit, "审计里有未登记的交付物"


# --- Q7 审计的 `--check` 必须在**干净 clone**（没有 internal/main）里也成立 -------


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "gen_acceptance_audit_under_test", REPO / "scripts" / "gen_acceptance_audit.py"
    )
    assert spec and spec.loader, "生成器加载不了"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_q7_normalise_treats_a_missing_tip_like_a_changed_tip():
    """`（取不到）` 与真实 tip 必须归一 —— 否则 CI 上 `--check` 必红。

    这是 CI 抓到的第二次自指问题：本地有 `internal/main`，干净 clone 里没有，
    生成器于是写"（取不到）"，而文档里记着上次的真实 tip。
    """

    normalise = _load_generator().normalise
    assert normalise("公开仓 `deadbee`") == normalise("公开仓 `（取不到）`")
    # 归一必须**只**作用于 tip 位置：别把正文里的数字/哈希也吃掉
    assert "`<tip>`" in normalise("公开仓 `deadbee`")
    assert "1681" in normalise("跑了 1681 个用例")


def test_q7_check_passes_without_the_internal_remote(tmp_path):
    """在"没有 `internal` 引用"的 git 环境里跑 `--check` 也必须一致。

    做法：把 `GIT_DIR` 指到一个不存在的仓库 —— 生成器里的 `git rev-parse` 全部失败，
    两个 tip 都渲染成"（取不到）"，这就**逐字复现了 CI（干净 clone）的条件**。
    """

    generator = REPO / "scripts" / "gen_acceptance_audit.py"
    env = {**os.environ, "GIT_DIR": str(tmp_path / "not-a-repo.git")}
    env.pop("GIT_WORK_TREE", None)
    proc = subprocess.run(
        [sys.executable, str(generator), "--check"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, (
        "在没有 internal 引用的环境里审计被判成'过期' —— 干净 clone（CI）上会红。"
        f"输出：{proc.stdout.strip()}"
    )


# --- Q10 判据文件里必须真的有断言 ------------------------------------------
#
# Q2 只证明"文件存在"。一个空文件、或者一个只放了文档字符串的文件，同样能让 Q2 绿 ——
# 而它一条断言都没有。Q10 补这一半：文件里必须能找到断言形状的东西。
#
# 变异（证明它会红）：把 `P5-2` 的判据临时指向 `docs/README.md`（存在、但里面没有断言）
# → Q10 立刻失败，Q2 仍然绿 —— 这正是修之前那张表的形态。

#: 各种判据文件里"断言"的样子。Python 用 `def test_`/`assert`，前端用 `test(`/`it(`/`expect(`。
ASSERTION_MARKERS = ("def test_", "expect(", "assert ", "test(", "it(")


@pytest.mark.parametrize(("item", "files"), sorted(JUDGEMENT_FILES.items()))
def test_q10_judgement_files_really_assert_something(item: str, files: tuple[str, ...]):
    empty: list[str] = []
    for name in files:
        path = judgement_path(name)
        assert path.is_file(), f"{item} 声明的判据文件不存在：{name}"
        text = path.read_text(encoding="utf-8", errors="replace")
        if not any(marker in text for marker in ASSERTION_MARKERS):
            empty.append(name)
    assert empty == [], (
        f"{item} 声明的判据文件里找不到任何断言（{empty}）—— "
        "一个只有文档字符串/说明的文件不能当判据；它会让\"已落地\"这句话凭空成立"
    )
