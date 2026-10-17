"""生成 `docs/acceptance-audit.md`（整体验收审计）。

## 为什么单独留一个脚本（而不是"写一次就算了"）

这份文档的**每个字段都来自可读的输入**：交付物编号与状态来自 `ROADMAP.md`，
判据文件来自下面那张表，两个远端 tip 来自 `git`。"写一次"的代价是下一轮它就会过期
—— 而过期的审计比没有审计更坏（它给的是**过期的安全感**）。

跑法：

    python scripts/gen_acceptance_audit.py            # 写到 docs/acceptance-audit.md
    python scripts/gen_acceptance_audit.py --check    # 只检查是否与当前状态一致（CI 友好）

`api/tests/test_acceptance_audit.py` 的 Q1–Q5 会核对生成结果（状态齐全、判据文件存在、
缺口双向一致、双仓差异 7 个文件、两个 tip 写进文档）。
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
ROADMAP = ROOT / "ROADMAP.md"
OUT = ROOT / "docs" / "acceptance-audit.md"

TARGET_STAGES = ("P2", "P3", "P4", "P5", "P6")
GAP_MARKERS = ("未验证", "未做", "未验")

#: 交付物 → 判据在哪。**人工维护**：这是"声明"与"证据"之间的那根线，不能让脚本自己猜。
#: 名字必须与 `api/tests/test_acceptance_audit.py` 的 `JUDGEMENT_FILES` 对得上（那边会核对文件名存在）。
JUDGEMENTS: dict[str, str] = {
    "P2-1": "`ProjectOnboarding.tsx` + `web/e2e/l4-flow.spec.ts`（绑定引导可见）",
    "P2-2": "`api/tests/test_settings_panel.py`（19 项设置 + 生效说明）",
    "P2-3": "`api/tests/test_runtime_logs.py`",
    "P2-4": "`api/tests/test_diagnostics.py`（脱敏 + 非空壳）",
    "P2-5": "`api/tests/test_state_admin.py`（迁移先预演、重置要抄 key）",
    "P2-6": "`web/src/i18n.test.ts` + `i18n.detect.test.ts`（32 例，逐条变异过）",
    "P3-1": "`api/tests/` 的预算硬闸用例 + `test_settings_panel.py`（额度项）",
    "P3-2": "`api/tests/test_cost.py`（区间而不是单值；样本不足拒绝给数）",
    "P3-3": "`api/tests/test_cost.py`（三维归因；取不到模型名不造假桶）",
    "P3-4": "`api/tests/test_cross_project.py`（按币种分桶、读不到≠0）",
    "P3-5": "`api/tests/test_auto_policy.py`（4 条策略 + 开关 + 触发依据）",
    "P3-6": "`api/tests/test_scale.py`（1000 节点：130ms/640KB，规模比 < 8）+ `web/e2e/perf-first-screen.spec.ts`（真浏览器首屏实测 + 棘轮）",
    "P4-1": "`api/tests/test_delivery.py`",
    "P4-2": "`api/tests/test_watchdog_tuning.py`（记录先于生效）",
    "P4-3": "`api/tests/test_attention.py`（注意账会过期；时间冻结过的用例）",
    "P4-4": "`api/tests/test_action_risk.py`（高风险不可预授权）",
    "P4-5": "`api/tests/test_conclusions.py`（事实与判断分开；空段≠没有）",
    "P5-1": "`api/tests/test_metrics.py`（按路由模板记 p95 + 相对基线退化）",
    "P5-2": "`web/src/fileSize.test.ts`（棘轮，**预算表现在是空的**）+ `virtualization.test.ts`（13 例）+ `web/e2e/perf-first-screen.spec.ts`（千节点首屏实测）",
    "P5-3": "`web/e2e/l4-flow.spec.ts`（6 例，真后端 + 真浏览器）",
    "P5-4": "`api/tests/test_retention.py`（清理后仍能还原）",
    "P5-5": "`api/tests/test_trace_correlation.py`（10 例 + 四组变异）",
    "P5-6": "`api/tests/test_crash_injection.py`（每个写入阶段硬杀一次）",
    "P6-1": "`api/tests/test_container.py`（21 例）+ `test_container_static.py`（D1–D8 语法与自洽）+ "
    "`test_web_dist_serving.py`（W1–W4 **运行期**：API 真的托管 `web/dist`，且 `/api/*` 没被挂载吃掉）",
    "P6-2": "`api/tests/test_platform_support.py`（17 例）+ `test_cross_platform_windows_only.py`（W1–W4）",
    "P6-3": "`api/tests/test_ci_workflow.py`（18 例，含步骤名逐字同步 + W10/W11 子进程编码）",
    "P6-4": "`scripts/probe_weak_model_planner.py` + `scripts/real_codex_smoke.py`（完整链路：worker 真改文件）"
    " + `docs/weak-model-usability.md`（planner 矩阵 §3、worker 档 §7）",
    "P6-5": "`api/tests/test_provider_matrix.py`（25 例）+ `test_provider_wire_format.py`（F1–F4：假网关 + 真实 CLI 试出端点契约）",
}

#: 缺口 → 缺什么。带"未验"字样的条目必须有这里的一条说明（没有就是审计自己的缺口）。
GAP_REASONS: dict[str, str] = {
    "P3-6": "后端规模判据达标（1000 节点 130ms/640KB）。**前端首屏本机已达标**：两连跑 "
    "cold **1601 / 1891ms**（目标 < 2s），一路是量出来的 —— 侧栏重活改成“树到位再挂”（4406→3033ms）、"
    "cProfile 定住第一次 `get_tree` 的 0.895s 是 **PowerShell/WMI 全表扫进程**（缓存 TTL 2 秒）→ "
    "换 WQL 服务端过滤（0.895→0.672s）+ **启动预热 + 后台每 1.5 秒刷新缓存** → 页面取树 TTFB "
    "**1165→253–277ms**。判据三层：目标 2000ms、棘轮 6000ms、TTFB<700ms（回归钉子）。"
    "**仍未验证**：慢机器/CI 上的首屏（CI 下跳过；2 vCPU runner 上千节点 >180s，量的是 runner 容量）。"
    "下一步最大项是客户端渲染（长任务 ~0.9s，DOM 只有 400 节点）—— 见 `docs/scale-baseline.md` §5",
    "P5-2": "长列表虚拟化已做（交给布局器的节点有界且会说出挡住多少）；**“千节点首屏 < 2s”本机两连跑已达标**"
    "（cold 1601 / 1891ms；同一判据此前 2.4–4.8s），判据 `web/e2e/perf-first-screen.spec.ts` 除目标与棘轮外"
    "还多了一条回归钉子（页面取树 TTFB < 700ms）；**仍未验证**：慢机器/CI 上的首屏（CI 下跳过）",
    "P6-1": "本机**没有 Docker**，`docker compose up` 一次都没跑过（语法与自洽已用 D1–D8 静态验过）。"
    "**补上了“界面可用”的运行期那一半**：`test_web_dist_serving.py` 真的 `GET /`（要求 200 + `#root`）、"
    "真的取 `index.html` 引用的 `/assets/*`、并确认挂载在 `/` 没吃掉 `/api/*` —— 这是容器里"
    "“界面可用”依赖的**同一条机制**（仍不等于跑了容器）。",
    "P6-2": "本机**没有 Linux/WSL/可用容器**，没在 Linux 上跑过任何东西（平台分派已用 L1–L9 验过）",
    "P6-3": "**Windows 侧已全绿**（run 38956861930，18.5 分钟，含 pytest + 前端 build + E2E），"
    "绿之前 CI 自己抓出并逼我们修了三处“只有本机能过”（cp1252 编码、未用 import、审计 `normalise` 不认“取不到”）。"
    "**Linux 侧仍无完整记录**，但**原因与量级都查清了**：加 `timeout-minutes` 后注解给出原文"
    "`The job has exceeded the maximum execution time of 30m0s`，把预算提到 **45 分钟**再跑"
    "（run 38973770051 / commit 8d48868）—— Windows 侧 **16.4 分钟 success**（连续第三次全绿），"
    "ubuntu 侧 **>44.9 分钟仍没跑完**。**已做结构性处置**：`verify_all --group {python,web}` 把同一套"
    "门禁拆成两个并行作业（W13 盯着“并集等于全部步骤、两组不重叠、两个作业各自覆盖双平台”），"
    "顺带修掉一处静默变弱（抽步骤名原来漏掉多行的 `provider 矩阵配置检查`）。"
    "**仍未验证**：拆组之后 Linux 侧的第一次完整记录；拆组当次已拿到 **首次 Linux 绿灯**（verify-web ubuntu 2.5 分钟），只剩“Python 组在 Linux 上 45 分钟跑不完”（同一份 Windows 6.7 分钟、本机 83 秒）——已给每条用例装 `--timeout=120`，下次会指名道姓。**另有账号侧阻塞**：`run 38985269338` 四个作业 3 秒内全失败，注解 `The job was not started because recent account payments have failed or your spending limit needs to be increased...` —— **不是代码问题**，是 Actions 额度/账单需要仓库所有者处理（自我记账与据此定的“一轮只推一次”纪律见 `docs/ci.md` §11）。"
    "另：旧结论“本机没有触发 Actions 的手段”是错的 —— CI 一直在跑（`on: push`），是“推完就走”把运行自己取消了",
    "P6-4": "**worker 档已实测通过**（2026-10-17，`scripts/real_codex_smoke.py` 完整链路、单节点一轮）："
    "planner / worker / verifier 三次真实运行全 `exit_code=0`，worker 交回 "
    "`changed_files=[BATTLEFIELD_SMOKE.txt]`、31 字节逐字节正确，verifier 独立 `Format-Hex` 复核 "
    "`verdict=pass`，`root_status=done`、`debug_paths_ok=true`，整链约 40 秒（`docs/weak-model-usability.md` §7）。"
    "**仍未覆盖**：acceptance agent（单节点树里没跑）、多节点/长跑/并发、成功率统计与失败模式样本、"
    "费用金额（端点不回传 → 记 null，不推算）",
    "P6-5": "三类端点的**真实可达性与鉴权未验**（本机没凭据）；**端点契约已离线实测**"
    "（F1–F4：必须支持 SSE 流式 + `response.output_item.done`）",
}


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def roadmap_rows() -> list[tuple[str, str]]:
    """目标阶段里的交付物行：`(编号, 整行)`。"""

    rows: list[tuple[str, str]] = []
    for line in ROADMAP.read_text(encoding="utf-8").split("\n"):
        if not line.startswith("| "):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells and re.fullmatch(r"(P\d-\d+)", cells[0]) and cells[0][:2] in TARGET_STAGES:
            rows.append((cells[0], line))
    return rows


def status_of(line: str) -> str:
    if "部分落地" in line:
        return "部分落地"
    if "已落地" in line:
        return "已落地（有未验项）" if any(marker in line for marker in GAP_MARKERS) else "已落地"
    if "未做" in line:
        return "未做"
    return "**未标状态**"


def render() -> str:
    rows = sorted(roadmap_rows())
    gaps = [(item, line) for item, line in rows if any(marker in line for marker in GAP_MARKERS)]

    public_tip = git("rev-parse", "origin/main") or "（取不到）"
    internal_tip = git("rev-parse", "internal/main") or "（取不到）"

    lines = [
        "# 整体验收审计（P2–P6）",
        "",
        "这份文件回答一个问题：**目标里那些交付物，哪些真的有可反驳的判据，哪些还缺条件。**",
        "",
        "它本身也受判据约束：`api/tests/test_acceptance_audit.py` 的 Q1–Q6 会检查",
        "（每条交付物都说了状态、声明的判据文件真的存在、未验项没被悄悄删掉、双仓差异仍是 7 个文件、",
        "**本文件与生成器的输出一致**）。**文档与代码脱节时会红**，这是这份文件存在的意义。",
        "",
        f"审计时的两个远端 tip：公开仓 `{public_tip}`、内部仓 `{internal_tip}`。",
        "（本文件由 `scripts/gen_acceptance_audit.py` 生成 —— 手改会在下一次生成时被覆盖。）",
        "",
        "> **这里刻意不写“本地 HEAD”**：写了它会自相矛盾 —— 文档里含 HEAD 的哈希，",
        "> 而把这个文件提交进去又会让 HEAD 变化，于是 `--check` 永远不过（实测踩到）。",
        "> 两个远端 tip 是“已经推上去的事实”，不会因为一次本地提交而失效。",
        "",
        "## 1. 逐条对照",
        "",
        "| 交付物 | 状态 | 判据（在哪） |",
        "|---|---|---|",
    ]
    for item, line in rows:
        lines.append(f"| {item} | {status_of(line)} | {JUDGEMENTS.get(item, '（未登记）')} |")

    lines += [
        "",
        "## 2. 缺口清单（**没有证据的部分**）",
        "",
        "这些条目的 ROADMAP 行里带“未做/未验证”字样，逐条写清缺什么条件。",
        "**不把它们藏起来**是这份审计的主要目的：把未验写成已验比不写更坏。",
        "",
        "| 交付物 | 缺什么 |",
        "|---|---|",
    ]
    for item, _line in gaps:
        lines.append(f"| {item} | {GAP_REASONS.get(item, '（未写原因 —— 这是审计的缺口，必须补）')} |")

    lines += [
        "",
        "## 3. 双仓状态",
        "",
        f"* 公开仓：`{public_tip}`（`origin/main`）",
        f"* 内部仓：`{internal_tip}`（`internal/main`）",
        "* 差异必须**恰好 7 个文件**（公开仓 + 7 个 overlay）：其中 2 个是公开树里也有的",
        "  文件（内部仓改写了它们），另外 5 个只存在于内部仓。",
        "  多一个少一个都说明同步出了问题（Q5 会红，它会把具体文件名打在失败信息里）。",
        "",
        "> **这里刻意不列出那 5 个内部文件的名字。** 本文件会提交到公开仓，而它们的名字就是",
        "> 内部标识；第一版把 7 个名字全列了出来，等于亲手把内部标识推上公开仓",
        "> （`scripts/check_public_tree.py` 的私有词表会命中）。要核对名字请跑 Q5 或",
        "> `git diff --name-status origin/main internal/main` —— 运行输出不属于公开树。",
        "",
        "## 4. 结论",
        "",
        "**P2–P5 的交付物都有判据，且判据在 `verify_all` 里跑。** P6 的五条里，",
        "两条（容器化 / 跨平台）缺的是**执行环境**：本机没有 Docker、没有 Linux，",
        "所以“界面可用”“在 Linux 上跑通”这两句都没有证据（语法、平台分派、工作流结构都已静态验过）。",
        "CI 那条**有了新证据**：它一直在跑（`on: push`），唯一一次给出结论的运行抓到了",
        "**Windows 侧 cp1252 把门禁死在第 1 步**（已修，W10/W11 钉住）；但 **Linux 侧没有一次跑完的记录**",
        "（45 分钟未完成、日志 404）。一条（弱模型）只到 planner 粒度，一条（provider 矩阵）",
        "缺真实端点的可达性与鉴权（**端点契约**已用本机假网关实测）。",
        "",
        "所以整体目标**不标记完成**：缺口是**环境**造成的（没有 Docker、没有 Linux、",
        "没有真实端点凭据、没有真实模型额度）。注意“没有 CI 触发手段”这条**已作废** ——",
        "CI 从来就在跑，是我们没等它跑完（见 `docs/ci.md` §5）。补上它们的成本按信息量排序：",
        "",
        "1. **等一次完整的双平台 CI 跑完** —— 现在它真的会跑，代价只是时间；",
        "   能把 P6-2 与 P6-3 的一半假设变成事实或反例；",
        "2. 一台有 Docker 的机器跑 `docker compose up` —— 验 P6-1 的“界面可用”；",
        "3. 一个真实端点（任意 OpenAI 兼容）跑 `verify_provider_planner.py` —— 验 P6-5 的通道；",
        "4. 授权真实模型额度后跑 `probe_weak_model_planner.py` 的 worker 档 —— 验 P6-4 的后半段。",
        "",
    ]
    return "\n".join(lines)


def normalise(text: str) -> str:
    """比对用：忽略行尾差异（CRLF/LF 由 git 管），并**抹掉两个远端 tip**。

    为什么连公开仓的 tip 也要抹掉：**这份文档本身会被提交并推送**，而推送就会改
    `origin/main` —— 于是"文档里写的 tip"与"生成时的 tip"永远差一个提交。
    实测踩到两次（先是写了本地 HEAD，再是保留了公开仓 tip），两次都是
    `--check` 恒不过。**一个恒不过的判据等于没有判据**（而且会训练所有人忽略它）。

    ⚠️ **"取不到"也必须和"有值"等价。** 第三次踩到的是 CI：干净 clone 里没有
    `internal/main`，生成器于是写"（取不到）"，而文档里记的是上次生成时的真实 tip
    → `--check` 在 CI 上红（本地永远绿，因为本地有这个引用）。所以两处占位都归一到
    同一个记号：判据守的是**内容**，不是"tip 当时拿没拿到"。

    抹掉 tip 之后，`--check` 真正守住的是**每条交付物的状态、判据在哪、缺口清单、
    双仓差异的描述**。tip 仍然生成并写进文档（它是有用的信息，
    只是不适合参与"是否过期"的判定）。
    """

    cleaned = text.replace("\r\n", "\n")
    cleaned = re.sub(r"`[0-9a-f]{7,40}`", "`<tip>`", cleaned)
    return cleaned.replace("`（取不到）`", "`<tip>`")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成验收审计文档。")
    parser.add_argument("--check", action="store_true", help="只检查是否与当前状态一致（不写文件）")
    args = parser.parse_args()

    rendered = render()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        same = normalise(current) == normalise(rendered)
        print("一致" if same else "过期：请跑 python scripts/gen_acceptance_audit.py")
        return 0 if same else 1

    OUT.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"写出 {OUT.relative_to(ROOT)}（{rendered.count(chr(10))} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
