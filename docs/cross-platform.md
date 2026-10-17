# 跨平台（Linux / macOS）：做了什么，以及**哪些没有证据**

P6-2 在 ROADMAP 里的判据是：**仓库脚本在 Linux 跑通核心路径；`verify_all` 在 Linux CI 绿。**

本文要先说清一件事：**本机没有 Linux，也没有 WSL。** 所以那两句判据在这台机器上
**无法验证**。P6-3 的双平台 CI 是唯一能发现问题的机制，而"CI 会绿"这句话同样没有证据
（见 `docs/ci.md`）。本文记录的是**能做的部分**，以及不能做的部分到底缺什么。

## 1. 做之前先审计：哪些地方是真的 Windows 假设

审计方法：在 `scripts/` 与 `api/` 里搜硬编码路径与平台分支。

| 位置 | 情况 | 处置 |
|---|---|---|
| `api/.venv/Scripts/python.exe` | 真正的 Windows 假设（Unix 是 `.venv/bin/python`） | 收进 `scripts/platform_support.py`，**平台是参数** |
| `verify_browser_console.py` 的 Chrome/Edge 候选 | 只有 `C:\Program Files\...`，Unix 上一个都命中不了 | 候选按平台给（Unix 用可执行名 + macOS 的 `.app` 路径） |
| `local_kick_tree.py` 的文件锁 | **已经对了**：`msvcrt` / `fcntl` 按平台分支 | 不动 |
| `bench_supervise.py` 的 `py-spy.exe` / 符号服务器 | 平台分支已存在，符号服务器本来就是 Windows 专用诊断 | 不动（在允许清单里写明理由） |
| `scan_history_privacy.py` 的 `C:\Users\<用户名>` 正则 | **必须**认识 Windows 用户目录 —— 那是要抓的目标 | 不动（在允许清单里写明理由） |

"已经对了"的条目也写出来：审计的价值不只是找到问题，还包括**确认哪些地方不该动**。

## 2. 新增：`scripts/platform_support.py`

一份实现，谁需要谁调：

```python
from platform_support import venv_python, python_executable, find_browser, child_env_with_path

venv_python()            # api/.venv 里的解释器；不存在返回 None（**不猜**）
python_executable()      # 优先 venv，否则当前解释器
find_browser()           # 按平台找 Chrome/Chromium/Edge；都没有返回 None
child_env_with_path(dir) # 把 dir 加到 PATH 最前（用 os.pathsep，不写死 ; 或 :）
```

三条设计取舍：

1. **找不到就返回 `None`，不返回一个不存在的路径**。返回假路径会把失败推迟到真正用到
   它的时候 —— 那时更难归因（这条判据在 `test_platform_support.py` 里有对应用例）。
2. **`python_executable()` 回退到"当前解释器"而不是字面量 `"python3"`**：调用方多半
   已经用某个解释器在跑，凭空换成另一个可能连依赖都没有。
3. **平台是参数，不是全局状态**。见下一节 —— 这条是被实测逼出来的。

## 3. 实测踩到的坑：不要靠改写 `sys.platform` 来测另一个平台

第一版写了个 `pretend_platform` 上下文管理器，把 `os.name` / `sys.platform` 改成
`"posix"` / `"linux"` 来验 Unix 分支。**在 Windows 上直接炸**：

```
NotImplementedError: cannot instantiate 'PosixPath' on your system
```

`pathlib` 在导入时按平台选好 `WindowsPath`/`PosixPath`，改了 `sys.platform` 之后
再构造路径就会走到 `PosixPath` 的分支。所以改成**显式参数**：

```python
venv_python_candidates(windows=False)   # 只影响这一次调用的分支
browser_candidates(windows=False)
```

`test_platform_support.py::test_l2_platform_switch_does_not_rewrite_sys_platform`
把这条钉住：调用前后 `sys.platform` 必须一模一样。

## 4. 判据：`api/tests/test_platform_support.py`（17 例，L1–L9）

| 判据 | 验的是 |
|---|---|
| L1/L2 | Windows 选 `Scripts/python.exe`、Unix 选 `bin/python`；两边的候选**集合相同**、只有顺序不同 |
| L3 | 找不到 venv 时返回 `None`；`python_executable()` 回退到当前解释器；有 venv 时优先它 |
| L4 | Windows 候选是绝对安装路径且以 `.exe` 结尾，同时保留可执行名 |
| L5 | Unix 候选是可执行名 + macOS `.app` 路径；**不含** `Program Files`；`find_browser` 找不到时返回 `None` |
| L6 | `PATH` 用 `os.pathsep` 拼；空 `PATH` 时不留下悬空分隔符 |
| L7 | 仓库里**没有第二份** Windows 安装路径清单（有就红，并列出违规文件） |

L7 是防"修一处、别处长出来"的那条。它带一张**允许清单**，每条都要写理由 ——
清单短是设计目标。

## 7. **第一次真的在 Linux 上跑后端测试**（2026-10-18，WSL Ubuntu 24.04）

这一节把 §5「没做的」里最长那条（"没有在 Linux 上跑过任何东西"）改掉了。做法不是等 CI
（账单一直不解开），而是用本机已有的 **WSL2 + Ubuntu-24.04**（`wsl -l -v` 里一直是 `Stopped`，
我一直以为"本机没有 Linux" —— 这是本轮第二个被推翻的假设）：

```bash
# 一次性准备（WSL 里）：apt 装 python3.12-venv，然后
python3 -m venv ~/cb-venv && ~/cb-venv/bin/python -m pip install -e "/mnt/d/work/codex-battlefield/api[dev]"
# 跑后端整套
cd /mnt/d/work/codex-battlefield/api && ~/cb-venv/bin/python -m pytest -q -o addopts="--timeout=90" -n 4
```

### 7.1 结果：修掉 §7.2/§7.3 之后 **1728 通过 / 18 失败 / 12 跳过，4 分 32 秒**

（第一次跑、还没修时是 **1725 通过 / 20 失败**，4 分 23 秒 —— 两次都是同一台机器、同一份代码树。）

（Windows 同一份是 1753 通过 / 0 失败。也就是说 Linux 侧**不是"跑不完"**，
而是"跑得完，但有 20 条红的" —— 之前"45 分钟跑不完"的结论与本机这次实测对不上，
说明那是 CI runner 上的另一个问题，见 §7.4。）

### 7.2 那 20 条里，**先抓到一个会自杀的真缺陷**

`scripts/bench_supervise.py` 在 POSIX 上做 `os.killpg(os.getpgid(child), SIGKILL)`，
但子进程**没有** `start_new_session` —— 于是 `getpgid(child)` 返回的是**调用方那一组**，
`killpg` 把调它的 pytest 自己 SIGKILL 了。症状极具误导性：**日志停在用例名上、连 traceback
都没有**（内核杀进程不给解释器留遗言），看起来就像"Linux 上莫名其妙卡住/被杀"。

* 修法：POSIX 上 `Popen(..., start_new_session=True)`；`terminate_tree` 再加一道保险 ——
  目标与自己同组时**拒绝 killpg**，改为逐个 SIGKILL，且**绝不杀自己**；
* 判据：`test_supervisor_must_not_signal_its_own_process_group`（POSIX 专用）+
  `test_supervisor_spawns_the_child_in_its_own_session`（源码级，两个平台都跑）；
* 效果：修之前整套测试在 9%–26% 处被杀；修之后能跑完（4 分 23 秒）。

### 7.3 还抓到一处产品级路径缺陷：迁移时写死反斜杠

`migration.rewrite_worktree_paths` 重写旧路径时拼 `f"{新前缀}\{tail}"`（写死反斜杠）。
POSIX 上反斜杠是普通字符 → 迁移后的 worktree 路径变成 `…/worktrees\node-a`，
`worktrees.is_managed_worktree()` 的包含判断失败 → **那个 worktree 再也回收不掉**。
修法：分隔符跟着**原路径**走；判据 `test_migration.py::test_rewrite_keeps_the_original_separator`
（两个平台都跑，Windows 上等价于恒真、Linux 上就是反例）。

### 7.4 剩下 18 条红，逐类记账（没有一条影响 Linux 上的正常运行）

| 类别 | 条数 | 例子 | 性质 |
|---|---|---|---|
| **测试写成了 Windows-only，却没加平台守卫** | 5 | `test_watchdog_install_command_*`（产品明确"只有 Windows 支持"）、`test_l4_windows_candidates_*`、`test_project_binding::b1`（用 `Z:\…` 当"缺失目录"，POSIX 上它是相对路径）、`test_project_store::worktree_drive_comparison`（`Path.drive` 在 POSIX 恒为空） | 测试问题 |
| **本地跑法的伪失败** | 3 | `test_runner_tools` 逐字节比对（模板在 Windows 检出里是 CRLF，生成器写 LF）、脚手架/`web/dist` 相关 | 我们是在 `/mnt/d`（drvfs）上跑，**CI 的干净检出不会这样** |
| **需要 Windows 侧工具** | 5 | `test_provider_wire_format` F1/F2/F5/F6 选中了 PATH 里的 `codex.cmd`（`Exec format error`）；`test_provider_matrix` M8 需要 `pwsh` | 环境 |
| **扫描器在 drvfs 上超时** | 2 | `test_public_tree_hygiene` H1/H3（`pytest-timeout >90s`） | 环境（drvfs 慢），但值得查 SKIP_DIRS |
| 其它路径断言 | 3 | `test_workspace_follows_project`、`test_migration::migrate_copies_and_rewrites`（已随 §7.3 修复而变绿） | 混合 |

**这些没有在本轮全修**：它们不影响"Linux 上能不能跑起来"，而且其中 3 类是**本地跑法的伪失败**
（在 drvfs 上跑 Windows 检出）。下一轮的第一件事是把"测试自己该不该跑"按平台分清楚
（`skipif(sys.platform == 'win32')` / 反向），让 Linux 的这 20 条变成 0 条 —— 那时 CI 的
ubuntu 作业才有意义。

**仍未验**：真 Linux CI（`ubuntu-latest`，账单解开之前拿不到）；macOS（本机没有）。

## 5. 没做的（如实写下）

### 5.1 还有一处**产品级**的 Windows 假定（不是路径问题）

路径与解释器解析已经跨平台了，但**提示词与看门狗**仍然假定 Windows：

* worker / verifier 的**提示词**里给的是 PowerShell 命令写法
  （`powershell.exe -NoProfile -Command ...`、`Test-Path -LiteralPath`），
  在 Linux 上 worker 会被误导到不存在的命令；
* **看门狗**由 PowerShell 脚本（`scripts/battlefield_watchdog.ps1`）与 **Windows 计划任务**
  组成，没有 cron / systemd 等价物。

这不是"改几个路径"能解决的，属于**产品级移植**，所以不在 P6-2 里假装完成。
为了让这件事**可反驳**而不是停留在"我们不知道"，加了
`api/tests/test_cross_platform_windows_only.py`（静态扫描）：

| 判据 | 查什么 |
|---|---|
| W1 | 扫描面本身可信（确实扫到 `taskkill` / `msvcrt`，数量不少于 5） |
| W2 | **每个** Windows-only 调用点要么在**同一个函数体内**有平台守卫，要么在允许清单里并写清理由 |
| W3 | 看门狗的两个入口走 `_powershell_exe()`；非 Windows 上抛的是**能行动的错误**（写明"只有 Windows 支持 + 用 cron/systemd"），而不是 `FileNotFoundError` |
| W4 | 已知的**产品级**边界（提示词、看门狗）写在本文档里 |

扫描第一次跑就抓到两处**无守卫**的 `powershell.exe`（`install_watchdog_hook`、
`run_watchdog_once`）—— 在 Linux 上用户会看到"找不到文件"，而不是"这个功能只有 Windows 有"。
已修成显式的 `RuntimeError`（带"用 cron / systemd timer"的行动提示）。

### 5.2 其余未验证的部分

* **没有在 Linux/macOS 上跑过任何东西**。本机没有 Linux、没有 WSL、没有容器运行时的
  可用实例（`docs/container.md` 同理）。所以下面这些**仍然是假设**：
  * `verify_all` 在 Linux 上能跑完（尤其 `api/tests/` 里那些跟平台相关的用例）；
  * Unix 分支的 `fcntl` 锁、`bin/python` 路径、`google-chrome` 查找**真的能用**；
  * 前端 `pnpm` 链路在 Linux 上没有额外依赖。
* **没有做打包/发行**（`.deb` / Homebrew 之类）：ROADMAP 没要求，也不该在没有实机
  验证的情况下先做。
* **P6-3 的 CI 是唯一发现机制**，而 CI 本身也还没有一次真实的绿灯记录
  （`docs/ci.md` 里如实记着）。也就是说：**这一项的验收标准目前只能靠"机制已就位"
  来交付，不能靠"实测通过"**。ROADMAP 的 P6-2 行按这个口径写。

## 6. 想继续推进的话，缺的是什么

按能带来的信息量排序：

1. **让 CI 真跑一次**（哪怕只是 push 到公开仓触发）——这一步能把上面一半的假设变成事实
   或反例，成本几乎为零；
2. 一台 Linux 机器（或可用容器）跑 `verify_all`，能覆盖 CI 覆盖不到的本机状态差异；
3. macOS 只能靠别人帮忙：本项目的用户群以 Windows 为主，macOS 分支（`.app` 路径、
   `fcntl`）目前是**照文档写的**，没有实机验证。

### 5.3 判据与变异（这一组自己也被验过）

`api/tests/test_cross_platform_windows_only.py` 的 W1–W4 有四组变异，
**每一组都先把判据的第一版打回过**：

| 变异 | 第一版判据的结果 | 修法 |
|---|---|---|
| 把调用换回裸 `"powershell.exe"` | **照样绿** —— "往上看 30 行"跨过了函数边界，把隔壁函数的守卫当成了自己的 | 改成**在同一个函数体内**看守卫（AST 找所属函数） |
| 删掉 `taskkill` 的平台分支 | 绿（同上） | 同上 |
| 守卫里不再抛错 | 绿（同上） | 同上 |

修好之后四组全部能红。**这一节留在这里是因为它比判据本身更有价值**：
"我在检查什么"和"我以为我在检查什么"经常不是一回事。
