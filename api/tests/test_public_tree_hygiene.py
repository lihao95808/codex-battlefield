"""公开树卫生：内部标识不得出现在**会推送到公开仓**的文件里。

这一簇是"扫描器有没有真的在扫"的判据。起因是一次真实事故：
整体验收审计的第一版把内部仓的 7 个 overlay 文件名逐个列了出来，而
`scripts/gen_acceptance_audit.py` 与它生成的 `docs/acceptance-audit.md`
都在**公开树**里 —— 于是内部标识被亲手推上了公开仓
（`scripts/check_public_tree.py` 的私有词表命中 10 处，其中 6 处在已推送的提交里）。
同一轮还清掉了一处更隐蔽的：脱敏判据里用了内部网关的**真实前缀 + 真实 hex 后缀**
当"随便举的例子"。

三条判据按"能不能在没有私有词表的克隆里跑"分层：

* H1 **派生式**：内部仓**新增**的文件名（现算，不写死在测试里）不得出现在工作树里。
  不需要私有词表，只要求本地有 `internal/main`（没有就跳过）。在内部分支上会跳过 ——
  那边的文件本来就该出现这些名字。
* H2 **扫描器自测**：在临时目录里植入一个假词，扫描器必须**抓到**它。否则"扫描通过"
  可能只是扫描器坏了 —— 门禁最怕的就是这个。
* H3 **通用规则**：仓库现存的通用命中必须**恰好**是测试里的那些假样张。
  任何新增命中都会红，包括无意中写进文档的本机路径。
* H4 **私有词表**（能配就配）：`CODEX_BATTLEFIELD_PUBLIC_TERMS` 指向词表时，
  用它跑一次完整扫描，必须 0 命中。没配就跳过并说明 —— 这条**必须**在发布前手工配一次，
  理由见下。

私有词表**不放在本仓库**（理由写在 `scripts/check_public_tree.py` 顶部）：把内部标识
写进"扫描规则"本身就是泄漏。因此 H1/H3 是这条防线上**永远在场**的那部分，
私有词表那部分靠 `CODEX_BATTLEFIELD_PUBLIC_TERMS`（发布前手工跑一次）。

> 事故里最值得记的是：H4 这条**当时根本不存在**，本机也没有词表文件 ——
> 于是 `check_public_tree.py` 一直只跑通用规则，"私有标识不进公开树"这条规矩
> **从来没有被自动检查过**。判据不在，规矩就只是意愿。

判据是**进程内**调用扫描器的 `main()`（不是 subprocess）：Windows 上子进程 stdout 走
本地代码页，抓回来解析分类名会变成乱码 —— 实测踩到一次，然后才改成进程内。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCANNER = REPO / "scripts" / "check_public_tree.py"

#: 假样张所在的文件。允许清单**从它派生**，而不是把那些字面量再抄一遍 ——
#: 抄一遍的后果实测到了：清单自己也会被扫描器命中，于是判据永远红。
#: 同一个坑在这份仓库里出现过三次（审计的 `--check` 比对自己、tip 写进文档又改 tip、
#: 现在的清单抄样张），所以这里的规则是：清单只记**位置与用途**，字面量从样张文件派生。
FIXTURE_FILE = "api/tests/test_diagnostics.py"


def _load_scanner():
    spec = importlib.util.spec_from_file_location("check_public_tree_under_test", SCANNER)
    assert spec and spec.loader, "扫描器加载不了"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_scanner(*args: str) -> tuple[int, str]:
    module = _load_scanner()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = module.main(list(args))
    return code, buffer.getvalue()


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
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _internal_only_names() -> list[str] | None:
    """内部仓**新增**的文件名（= 公开树里不该出现的名字）。拿不到就返回 None。"""

    for ref in ("origin/main", "internal/main"):
        probe = _git("rev-parse", "--verify", ref)
        if probe is None or probe.returncode != 0:
            return None
    diff = _git("diff", "--diff-filter=A", "--name-only", "origin/main", "internal/main")
    if diff is None or diff.returncode != 0:
        return None
    return [line.strip() for line in diff.stdout.splitlines() if line.strip()]


def _on_internal_checkout(names: list[str]) -> bool:
    """当前 HEAD 里已经有内部新增文件 → 这是内部分支，H1 在这里没有意义。"""

    if not names:
        return False
    probe = _git("cat-file", "-e", f"HEAD:{names[0]}")
    return probe is not None and probe.returncode == 0


def test_h1_internal_only_filenames_never_appear_in_the_public_tree():
    """内部仓新增的文件名，在**工作树**里一个都不许出现。

    扫工作树而不是扫 `origin/main`：这样在**提交之前**就能红 —— 否则唯一的反馈
    来自推送之后，而公开出去的东西收不回来。
    """

    names = _internal_only_names()
    if names is None:
        pytest.skip("本地没有 origin/main 或 internal/main 引用（未同步过的克隆）")
    # 覆盖是**逐条**做的：如果哪天内部仓不再新增文件，这条判据会变成空转 ——
    # 空转的门禁比没有门禁更坏（它会给出"检查过了"的错觉）。
    assert names, "内部仓没有新增文件了？那这条判据已经空转，必须重新设计"
    if _on_internal_checkout(names):
        pytest.skip("当前 HEAD 是内部分支（含内部新增文件），请在公开分支上跑这条判据")

    module = _load_scanner()
    leaks: list[str] = []
    for path in module.iter_files(REPO):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for name in names:
            if name not in text:
                continue
            line_no = text.count("\n", 0, text.index(name)) + 1
            leaks.append(f"{path.relative_to(REPO)}:{line_no}: 命中 {name}")

    assert leaks == [], (
        "内部标识出现在会推送到公开仓的文件里（这些名字来自内部仓新增的文件）：\n  "
        + "\n  ".join(leaks)
        + "\n修法：把公开树里的这些名字换成中性描述（例如'内部仓新增的 N 个文件'），"
        "名字只在运行输出里出现（`git diff --name-status origin/main internal/main`）。"
    )


def test_h2_scanner_can_actually_catch_a_planted_term(tmp_path: Path):
    """扫描器必须**能抓到**植入的词。

    为什么必须自测：一条"扫描通过"的判据只有在扫描器真的会红时才有意义。
    植入的词是当场造的（不是任何内部标识），所以这条判据本身不会泄漏。
    """

    planted_term = "PLANTEDTERM9F2C7A11"
    terms = tmp_path / "terms.txt"
    terms.write_text(f"# 假词表\n{planted_term}\n", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "notes.md").write_text(f"这里不该出现 {planted_term}\n", encoding="utf-8")

    code, output = _run_scanner("--root", str(tree), "--terms-file", str(terms))
    assert code == 1, f"植入的词没被抓到 —— 扫描器坏了。输出：{output.strip()}"
    assert planted_term in output, output

    (tree / "notes.md").write_text("这里什么都没有\n", encoding="utf-8")
    code, output = _run_scanner("--root", str(tree), "--terms-file", str(terms))
    assert code == 0, f"没有命中时应当干净。输出：{output.strip()}"


def _scan_hits(*args: str) -> set[tuple[str, str, str]]:
    """跑一次扫描，返回 (相对路径, 命中类别, 命中字面量) 的集合。"""

    code, output = _run_scanner(*args)
    assert code in (0, 1), f"扫描器异常退出 {code}：{output.strip()[:300]}"
    hits: set[tuple[str, str, str]] = set()
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("[x] "):
            continue
        # 形如： `[x] api\tests\...py:37: 疑似密钥 -> 'sk-...'`
        head, _, quoted = line[4:].partition(" -> ")
        path_part, _, label = head.partition(": ")
        rel = path_part.split(":")[0].replace("\\", "/")
        hits.add((rel, label, quoted.strip().strip("'")))
    return hits


def _unexpected_hits(hits: set[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """允许集合是**派生**的，不是抄出来的。

    * `api/tests/test_diagnostics.py` 里的命中 = 假样张本身（判据要验脱敏，就必须有
      形如真密钥的东西）；该文件里的**任何**命中都算允许；
    * 别处的命中只有在"类别 + 字面量"与该文件里某个样张**完全相同**时才允许
      （本文件引用那些样张时就是这种情形）。

    抄一遍字面量的后果实测到了：清单自己也被扫描器命中，判据永远红。
    """

    fixtures = {(label, value) for path, label, value in hits if path == FIXTURE_FILE}
    assert fixtures, f"{FIXTURE_FILE} 里一条假样张都没扫到 —— 样张没了，这条判据在空转"
    return sorted(
        hit for hit in hits if hit[0] != FIXTURE_FILE and (hit[1], hit[2]) not in fixtures
    )


def test_h3_generic_rule_hits_are_exactly_the_allowlisted_fixtures():
    """通用规则（路径/密钥/内网地址/大文件）的命中必须**恰好**是那些假样张。

    这样"新命中一定会红"：无意中把本机路径、真实 token 形态、内网地址写进文档或测试，
    都会在这里被抓住。放宽规则永远比加一条带用途说明的允许更坏 —— 放宽之后就是**永远不响**。
    """

    assert SCANNER.is_file(), "扫描器不见了 —— 那这条判据只是在测空气"
    unexpected = _unexpected_hits(_scan_hits())
    assert unexpected == [], (
        "公开树里出现了新的通用命中（本机路径 / 疑似密钥 / 内网地址 / 大文件）：\n  "
        + "\n  ".join(f"{path} | {label} | {value[:60]}" for path, label, value in unexpected)
        + "\n如果是有意为之的假样张，放进 api/tests/test_diagnostics.py（那是样张的单一来源）；"
        "否则请改成中性写法。"
    )


def test_h4_private_term_list_passes_when_configured():
    """配了私有词表时，用它跑一次完整扫描：除假样张外**一个命中都不许有**。

    这条跳过时**不是"通过"**：它意味着"私有标识不进公开树"这条规矩这一次没人验。
    发布前必须配一次（`CODEX_BATTLEFIELD_PUBLIC_TERMS`，词表文件放在仓库之外）。

    为什么不断言"退出码 0"：扫描器是通用规则 + 词表规则一起跑的，而假样张必然命中
    通用规则。所以这里断言的是"没有**意料之外**的命中"，同时要求扫描器真的读到了词表
    （读到 0 个词的词表等于没扫，那是另一种空转）。
    """

    terms_file = os.environ.get("CODEX_BATTLEFIELD_PUBLIC_TERMS", "")
    if not terms_file:
        pytest.skip(
            "没有配 CODEX_BATTLEFIELD_PUBLIC_TERMS —— 私有标识的完整扫描这次没跑。"
            "发布前必须配一次（词表不能放仓库里，理由见 scripts/check_public_tree.py 顶部）"
        )
    assert Path(terms_file).is_file(), f"词表文件不存在：{terms_file}"

    code, output = _run_scanner("--root", str(REPO), "--terms-file", terms_file)
    assert "私有词表：" in output, f"扫描器没有读到词表 —— 这次扫描是空转。输出：{output[:500]}"
    assert "0 个词" not in output, f"词表是空的 —— 这次扫描是空转。输出：{output[:500]}"
    assert code in (0, 1), f"扫描器异常退出 {code}：{output.strip()[:300]}"

    unexpected = _unexpected_hits(_scan_hits("--root", str(REPO), "--terms-file", terms_file))
    assert unexpected == [], (
        "私有词表命中了公开树（= 内部标识进了公开树）：\n  "
        + "\n  ".join(f"{path} | {label} | {value[:60]}" for path, label, value in unexpected)
    )
