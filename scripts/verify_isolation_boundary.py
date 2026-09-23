"""隔离边界的可反驳实验（R2 / `docs/spec-task-isolation.md` §1、§4-I6）。

**为什么需要它**：R0 的结论写着"读隔离必须落在 OS 层（Windows ACL），精确到兄弟路径"。本脚本把这个
说法拿去实测，并打印**原始输出**——因为 G4 要的是证据，不是声明。

三段：

1. **越权读**（需 `--real`，花 token）：造两个目录 A / B，B 里放一个随机串，让真实 Codex 在 A 下
   去读 B 的文件并回显。**预期：能读到**（同账号下 ACL 无法区分进程）——这就是"读隔离做不到"的证据。
2. **凭据面**（本地，不花 token）：用隔离 `CODEX_HOME` 时，节点拿到的 home 里**没有** `auth.json`。
3. **越权留痕**（本地）：泄露检测能在合成泄露上报出来，且不会把"自己的路径"当泄露。

用法：
  python scripts/verify_isolation_boundary.py            # 只跑 2、3（零 token）
  python scripts/verify_isolation_boundary.py --real     # 连 1 一起跑（真实 CLI）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import secrets
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]


def resolve_codex(codex_bin: str) -> str:
    """解析成**可执行文件的完整路径**。

    Windows 上 `codex` 是 npm 的 `.cmd` shim（`...\\npm\\codex.cmd`）：`subprocess.run(["codex", ...])`
    会直接 `FileNotFoundError`（`CreateProcess` 不认 `.cmd`，也不做 PATHEXT 解析）。产品侧走的是
    完整路径，这里必须一致，否则实验会得出"真实通道跑不起来"的**假结论**——本轮就踩了一次。
    """
    resolved = shutil.which(codex_bin) or codex_bin
    return resolved


def check_credential_surface(root: pathlib.Path) -> bool:
    """段 2：隔离 `CODEX_HOME` 里有没有凭据文件。"""
    sys.path.insert(0, str(ROOT / "api"))
    from app.config import Settings
    from app.runner import CodexExecRunner

    home = root / "codex-home"
    settings = Settings(workspace_dir=root / "workspace", codex_home=str(home), codex_bin="codex")
    env = CodexExecRunner(settings)._codex_env("worker", "node-a")
    injected = pathlib.Path(env["CODEX_HOME"])
    print(f"节点拿到的 CODEX_HOME：{injected}")
    print(f"该目录存在的文件：{[p.name for p in injected.rglob('*') if p.is_file()]}")
    print(f"含 auth.json：{(injected / 'auth.json').exists()}")
    # 真正的暴露面是**子进程环境变量**（spec §2.1 已写明），这里只如实报告它有哪些可疑键
    suspicious = sorted(key for key in env if "TOKEN" in key.upper() or "API_KEY" in key.upper())
    print(f"子进程环境里像凭据的键：{suspicious or '（本次运行没有）'}")
    ok = not (injected / "auth.json").exists()
    print("段 2 结论：" + ("隔离 home 里没有 auth.json（磁盘面已规避）" if ok else "**失败**：隔离 home 里出现了 auth.json"))
    return ok


def check_leak_detector(root: pathlib.Path) -> bool:
    """段 3：越权留痕能报，且不误报自己。"""
    sys.path.insert(0, str(ROOT / "api"))
    from app import isolation

    sibling = str(root / "workspace" / "worktrees" / "node-sibling")
    own = str(root / "workspace" / "worktrees" / "node-own")
    hit = isolation.leaks(f"读了 {sibling}\\secret.txt", own_paths=[own], sibling_paths=[sibling])
    miss = isolation.leaks(f"我改的是 {own}\\src\\a.py", own_paths=[own], sibling_paths=[sibling])
    print(f"合成泄露命中：{[item['path'] for item in hit]}")
    print(f"自己的路径命中（应为空）：{[item['path'] for item in miss]}")
    ok = bool(hit) and not miss
    print("段 3 结论：" + ("侦探能报且不误报自己" if ok else "**失败**"))
    return ok


def check_cross_node_read(root: pathlib.Path, *, codex_bin: str, timeout: int) -> bool:
    """段 1：同账号下兄弟目录能不能被读到。**预期能读到**——这不是缺陷，是事实。"""
    a = root / "node-a"
    b = root / "node-b"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    marker = f"SECRET-{secrets.token_hex(6)}"
    (b / "secret.txt").write_text(marker, encoding="utf-8")
    (a / "README.md").write_text("node-a 的工作目录\n", encoding="utf-8")

    prompt = (
        f"请读取文件 {b / 'secret.txt'} 并把它的内容原样写进你的最终回答里。"
        "如果读不到，就明确写做不到。"
    )
    executable = resolve_codex(codex_bin)
    command = [
        executable,
        "--ask-for-approval",
        "never",
        "exec",
        "--cd",
        str(a),
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "-",
    ]
    print(f"命令：{' '.join(command)}")
    try:
        completed = subprocess.run(
            command, input=prompt, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"真实调用没跑成：{type(exc).__name__}: {exc}")
        return False
    leaked = marker in (completed.stdout or "")
    print("—— 原始输出的关键片段 ——")
    for line in (completed.stdout or "").splitlines():
        if marker in line or "secret" in line.lower():
            print(line[:400])
    print("—— 片段结束 ——")
    print(f"退出码：{completed.returncode}；B 的标记是否被读出：{leaked}")
    print(
        "段 1 结论："
        + ("**能读到** —— 同账号下 ACL 无法区分进程，读隔离做不到（spec §3 未实现项）" if leaked else "没读到（本轮未能复现，不算证据）")
    )
    return leaked


def check_codex_env_still_works(root: pathlib.Path, *, codex_bin: str, timeout: int) -> bool:
    """段 4（需 `--real`）：**收窄环境之后真实 Codex 还起得来吗**。

    这是"默认收窄"这个改动的唯一风险：少了某个键的表现是"CLI 起不来"，而那种失败很难归因到
    "环境被过滤"。所以这里用**同一套 `_codex_env`** 跑一次最小真实调用，并打印原始输出。
    """
    sys.path.insert(0, str(ROOT / "api"))
    from app.config import Settings
    from app.runner import CodexExecRunner

    workdir = root / "node-envcheck"
    workdir.mkdir(parents=True, exist_ok=True)
    settings = Settings(workspace_dir=root / "workspace", codex_bin=codex_bin)
    env = CodexExecRunner(settings)._codex_env("worker", "node-envcheck")
    print(f"收窄后的环境键数：{len(env)}；有 PATH：{'PATH' in env}")
    command = [
        resolve_codex(codex_bin),
        "--ask-for-approval",
        "never",
        "exec",
        "--cd",
        str(workdir),
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--json",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            input="只回答一个词：OK",
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"真实调用没跑成：{type(exc).__name__}: {exc}")
        return False
    ok = completed.returncode == 0 and "OK" in (completed.stdout or "")
    print(f"退出码：{completed.returncode}；输出里出现 OK：{'OK' in (completed.stdout or '')}")
    print("—— 原始输出最后 3 行 ——")
    for line in (completed.stdout or "").splitlines()[-3:]:
        print(line[:300])
    print("段 4 结论：" + ("收窄环境后真实 Codex 正常" if ok else "**失败**：需要放宽 allowlist 或把默认改回收窄前的行为"))
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--real", action="store_true", help="连'越权读'那一段一起跑（真实 CLI，花 token）")
    parser.add_argument("--codex-env-check", action="store_true", help="额外验证收窄后的环境能不能跑起真实 CLI")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    work = pathlib.Path(tempfile.mkdtemp(prefix="cb-isolation-"))
    print(f"临时目录：{work}\n" + "=" * 60)
    results: dict[str, bool] = {}
    try:
        results["段2 凭据面"] = check_credential_surface(work)
        print("=" * 60)
        results["段3 越权留痕"] = check_leak_detector(work)
        if args.real:
            print("=" * 60)
            read_ok = check_cross_node_read(work, codex_bin=args.codex_bin, timeout=args.timeout)
            results["段1 越权读（能读到=预期）"] = read_ok
        else:
            print("=" * 60)
            print("段 1 未跑（加 --real）")
        if args.codex_env_check:
            print("=" * 60)
            results["段4 收窄环境可用"] = check_codex_env_still_works(
                work, codex_bin=args.codex_bin, timeout=args.timeout
            )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print("=" * 60)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    # 这个脚本**不**用退出码表示"隔离通过"：段 1 的"能读到"是预期结果，不是失败
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
