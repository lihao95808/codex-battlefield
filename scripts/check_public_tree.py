"""发布前扫描：确认公开树里没有不该出现的标识与形态。

## 为什么词表不放在本仓库

把内部标识当成字符串写进这个文件，**本身就是泄漏** —— 哪怕它写在"扫描规则"里，
读公开仓库的人照样能看到那些名字。所以：

* 本文件只留**方法与判据**；
* 内部词表由 `--terms-file` 指向私有位置（私有仓里放一份即可）；
* 不给词表时仍然会跑 —— 检查那些**通用**且同样该拦的东西（见下）。

## 不给词表时检查什么（通用项）

| 类别 | 为什么该拦 |
|---|---|
| 本机绝对路径 | 会把开发机目录结构带进公开仓库 |
| 邮箱以外的用户名痕迹 | 同上，且可能撞到别人的用户名 |
| 疑似 token / 密钥形态 | 真泄漏里最常见的一类，且代价最高 |
| 大文件与二进制误入 | 撑爆仓库、不可 diff |

## 这份扫描器**曾经一直只跑通用项**（真实事故）

"内部标识不进公开树"这条规矩原先没有任何自动检查：词表不在仓库（这一点是对的，
理由见上），但**本机也没有那份词表**，而这份扫描器也不在 `verify_all` 里。
于是"整体验收审计"的第一版把内部仓 7 个 overlay 文件名逐个列进了
`docs/acceptance-audit.md` 与 `scripts/gen_acceptance_audit.py` —— 两个都在公开树里 ——
直到有人顺手建了一份词表才被扫出来（命中 10 处，6 处已在推送出去的提交里）。

现在的防线分三层（判据在 `api/tests/test_public_tree_hygiene.py`）：

1. **H1 派生式**：内部仓新增的文件名不得出现在工作树里（名字现算，提交前就会红）；
2. **H2/H3 通用项 + 扫描器自测**：不依赖词表，永远在场；
3. **H4 私有词表**：`CODEX_BATTLEFIELD_PUBLIC_TERMS` 指向仓库之外的一份词表，
   配了就会真的扫 —— **发布前必须配一次**。

教训写在这里而不是只写在文档里：把内部名字写进公开文件，动机通常是"这样更清楚"，
所以规矩必须落在判据上。细节见 `docs/public-tree-hygiene.md`。

退出码 0 = 干净，1 = 有命中（用于门禁）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".playwright",
    # **运行时状态根**（gitignored）。它会同时出现在两处：仓库根的 `workspace/`
    # 与 `api/workspace/`。里面必然含本机绝对路径与真实端点地址 —— 那是**运行产物**，
    # 不是要发布的源码；扫它只会制造假命中并掩盖真实命中。
    # 实测（2026-10-17）：跑一次 `scripts/real_codex_smoke.py` 之后，
    # `workspace/runs/.../summary.json` 里就带 `C:\\Users\\<真名>\\...`，H3 立刻变红。
    "workspace",
    ".e2e",
}
TEXT_SUFFIXES = {
    ".py", ".md", ".ps1", ".cmd", ".toml", ".json", ".ts", ".tsx", ".js",
    ".css", ".html", ".yml", ".yaml", ".txt", ".example", ".gitignore", "",
}

# 通用项：与具体公司无关，任何公开仓库都该干净。
GENERIC_RULES: list[tuple[str, re.Pattern[str]]] = [
    # 开发机绝对路径：重点是**用户目录**，因为那里带真实用户名。
    # 刻意**不**列举具体目录名（比如某个人的 `work` / `DOC`）—— 那既暴露
    # "平时把代码放哪"，也会把中性示例路径误报成问题（实测踩到：把示例改成
    # `C:\work\x` 之后，规则反过来把它标红了）。
    ("本机用户目录", re.compile(r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_.-]+", re.I)),
    # 常见密钥形态：sk-…、ghp_…、AKIA…
    ("疑似密钥", re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16})\b")),
    # 内网地址。**必须写全四段**，否则会撞上版本号：`10.4.1`、`10.13.0`
    # 这类三段版本在 lockfile 里成百上千个（实测误报 25 处）。
    # 宁可只认"写全了"的地址，也不要制造一堆假警 —— 假警会让人直接无视门禁。
    (
        "疑似内网地址",
        re.compile(
            r"(?<![\d.])"
            r"(?:192\.168|172\.(?:1[6-9]|2\d|3[01])|10)\."
            r"\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"(?![\d.])"
        ),
    ),
]


def iter_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name == ".gitignore":
            yield path


def load_terms(path: Path) -> list[str]:
    """私有词表：一行一个词，`#` 开头是注释。"""
    terms: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描公开树，确认没有不该出现的标识。")
    parser.add_argument("--root", default=str(ROOT), help="扫描根目录（默认仓库根）")
    parser.add_argument(
        "--terms-file",
        default="",
        help="私有词表路径（一行一词）。不给则只跑通用规则。",
    )
    parser.add_argument("--max-mb", type=float, default=5.0, help="单个文本文件体积上限（MB）")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    terms: list[str] = []
    if args.terms_file:
        terms_path = Path(args.terms_file)
        if not terms_path.is_file():
            print(f"词表不存在：{terms_path}", file=sys.stderr)
            return 2
        terms = load_terms(terms_path)
        print(f"私有词表：{len(terms)} 个词（来自 {terms_path}）")
    else:
        print("未给 --terms-file：只跑通用规则（路径 / 密钥 / 内网地址 / 大文件）")

    term_rules = [(f"私有词表命中「{t}」", re.compile(re.escape(t), re.I)) for t in terms]
    rules = GENERIC_RULES + term_rules

    findings: list[str] = []
    scanned = 0
    for path in iter_files(root):
        rel = path.relative_to(root)
        try:
            size_mb = path.stat().st_size / (1024 * 1024)
        except OSError:
            continue
        if size_mb > args.max_mb:
            findings.append(f"{rel}: 文件 {size_mb:.1f}MB 超过上限 {args.max_mb}MB")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        for label, rx in rules:
            for match in rx.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                findings.append(f"{rel}:{line_no}: {label} -> {match.group(0)[:60]!r}")

    print(f"扫描文件数：{scanned}")
    if findings:
        print(f"\n发现 {len(findings)} 处命中：")
        for item in findings[:60]:
            print(f"  [x] {item}")
        if len(findings) > 60:
            print(f"  ... 另有 {len(findings) - 60} 处")
        return 1
    print("干净：0 处命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
