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

#: 声明"已落地/部分落地"的行 → 必须在仓库里能找到的判据文件（相对 `api/tests/`）。
#: 这条表是**人工维护**的：它是"声明"与"证据"之间的那根线，不能让脚本自己猜。
#: 前端侧的判据在 `web/src/*.test.ts`，由 `test_q4_frontend_side_judgements_exist_in_the_web_tree` 单独查。
JUDGEMENT_FILES: dict[str, tuple[str, ...]] = {
    "P2-2": ("test_settings_panel.py",),
    "P2-3": ("test_runtime_logs.py",),
    "P2-4": ("test_diagnostics.py",),
    "P2-5": ("test_state_admin.py",),
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
    "P5-2": ("test_provider_matrix.py",),  # 前端体量/虚拟化的判据在 web/src/*.test.ts，见 Q4
    "P5-3": ("test_ci_workflow.py",),
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
        assert (TESTS / name).is_file(), f"{item} 声明的判据文件不存在：api/tests/{name}"


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
    for name in ("fileSize.test.ts", "virtualization.test.ts", "i18n.test.ts", "polling.test.ts"):
        assert (web_src / name).is_file(), f"前端判据文件不存在：web/src/{name}"


def test_q4_e2e_spec_exists_and_the_workflow_runs_it():
    assert (REPO / "web" / "e2e" / "l4-flow.spec.ts").is_file()
    workflow = (REPO / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
    assert "verify_all.py" in workflow, "CI 必须跑同一套门禁"


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
