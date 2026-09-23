"""只读历史敏感信息扫描：在全部提交的**内容**里找可疑模式。

为什么要扫历史而不能只看工作区：一个文件今天被删掉，它在旧提交里仍然完整存在，
`git clone` 会把它们全带走。公开前的判断必须以「历史里还有什么」为准。

用法
  python scripts/scan_history_privacy.py            # 扫描全部提交
  python scripts/scan_history_privacy.py --revs 50  # 只扫最近 50 个提交

输出
  每个模式命中的 (提交, 文件, 行) 三元组，按模式分组，并对结果去重。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# (模式名, 正则) —— 只放“出现即需要人工判断”的形态，避免泛化到噪音
PATTERNS: list[tuple[str, str]] = [
    ("本地绝对路径（Windows 用户目录）", r"C:\\\\Users\\\\[A-Za-z0-9._-]+"),
    ("本地绝对路径（任意盘符的本地目录）", r"[A-Za-z]:\\\\(?:work|projects|dev|src|repos?)\\\\"),
    ("内网/私有域名（.internal/.corp/.lan）", r"@?[A-Za-z0-9.-]*\.(?:internal|corp|lan)\b"),
    ("私有 IP 直连", r"https?://(?:10|127|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.[0-9.]+"),
    ("疑似 OpenAI 风格密钥", r"sk-[A-Za-z0-9_-]{20,}"),
    ("疑似 GitHub PAT", r"ghp_[A-Za-z0-9]{20,}"),
    ("疑似 AWS AK", r"AKIA[0-9A-Z]{16}"),
    ("Bearer 令牌", r"Bearer [A-Za-z0-9._-]{24,}"),
    ("赋值形态的密钥字段", r"(?i)(api[_-]?key|secret|passwd|password|token)\s*[=:]\s*[\"'][^\"'\s]{12,}[\"']"),
]

# 组织专属模式**不进仓库**：把它写进仓库等于把要防的东西一起公开。
# 本机需要时用环境变量补，或在仓库外维护一份 `scripts/scan_history_privacy.local.txt`（已 gitignore），
# 每行形如 `模式名<TAB>正则`。
LOCAL_PATTERN_FILE = ROOT / "scripts" / "scan_history_privacy.local.txt"


def _extra_patterns() -> list[tuple[str, str]]:
    patterns: list[tuple[str, str]] = []
    for line in os.environ.get("PRIVACY_EXTRA_PATTERNS", "").splitlines():
        name, _, regex = line.partition("\t")
        if name.strip() and regex.strip():
            patterns.append((name.strip(), regex.strip()))
    if LOCAL_PATTERN_FILE.is_file():
        for line in LOCAL_PATTERN_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name, _, regex = stripped.partition("\t")
            if name.strip() and regex.strip():
                patterns.append((name.strip(), regex.strip()))
    return patterns


def _git(args: list[str]) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="只读扫描 git 历史里的敏感模式。")
    parser.add_argument("--revs", type=int, default=0, help="只扫最近 N 个提交；0 表示全部。")
    parser.add_argument(
        "--paths-only",
        action="store_true",
        help="不逐条打印命中，只按模式汇总「涉及哪些文件、各出现在多少个提交里」——命中量上万时用这个。",
    )
    args = parser.parse_args()

    revs = _git(["rev-list", "--all"]).split()
    if args.revs:
        revs = revs[: args.revs]
    if not revs:
        print("没有查到提交")
        return 0
    patterns = [*PATTERNS, *_extra_patterns()]
    print(f"扫描提交数：{len(revs)}")

    hits: dict[str, dict[str, str]] = {name: {} for name, _ in patterns}
    path_counts: dict[str, dict[str, int]] = {name: {} for name, _ in patterns}
    # 对每个模式跑一次 git grep；-l 时只回文件名，用于上万命中的汇总视图
    for name, pattern in patterns:
        if args.paths_only:
            output = _git(["grep", "-I", "-l", "-E", pattern, *revs])
            for line in output.splitlines():
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue
                path_counts[name][parts[1]] = path_counts[name].get(parts[1], 0) + 1
            continue
        output = _git(["grep", "-I", "-E", "-n", pattern, *revs])
        if not output.strip():
            continue
        for line in output.splitlines():
            # 形如 <rev>:<path>:<lineno>:<content>
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            rev, path, lineno, content = parts
            hits[name][f"{rev[:10]}:{path}:{lineno}"] = content.strip()[:160]

    total = 0
    for name, _ in patterns:
        if args.paths_only:
            found = path_counts[name]
            print()
            print(f"== {name} == 涉及 {len(found)} 个文件")
            for path, count in sorted(found.items(), key=lambda item: -item[1])[:15]:
                print(f"   {count:>7} 个提交  {path}")
            total += len(found)
            continue
        found = hits[name]
        print()
        print(f"== {name} == {len(found)} 处")
        for key, content in sorted(found.items())[:20]:
            print(f"   {key}\n      {content}")
        if len(found) > 20:
            print(f"   …还有 {len(found) - 20} 处")
        total += len(found)
    print()
    print(f"合计 {total} {'个文件' if args.paths_only else '处命中'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
