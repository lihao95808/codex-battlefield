# CI 与跨平台（P6-3 / P6-2）

实现：`.github/workflows/verify.yml`。判据：`api/tests/test_ci_workflow.py`（15 例）。

## 1. 为什么必须双平台

这个仓库原本是 **Windows 优先**写出来的：PowerShell 脚本、路径判断、`.ps1` 的 BOM 约定、
看门狗计划任务。而"只有本机能过"的提交是真实存在的风险 —— 本地绿、别人机器上红，
而红的往往是最基础的东西（路径分隔符、编码、行尾）。

双平台把这类问题挡在 PR 上，而不是等使用者踩到。

**矩阵刻意只有两个平台**（`ubuntu-latest` + `windows-latest`）：再加 macOS 的边际信息很低，
而每个平台都是一份真实的等待时间。

## 2. 这一层判据的难点：我在本机跑不了 GitHub Actions

所以判据**不能**断言"CI 会绿"—— 那是我无法验证的承诺。能验证的是那些**必然导致 CI 无用**
的具体缺陷，每一条都对应一种真实失败：

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| W1 | 合法 YAML 且结构完整 | 语法错 → GitHub 直接不认这个工作流，而本机看不出来 |
| W2 | **Windows + Linux 两个平台** | "只有本机能过"正是 P6-3 要挡的东西 |
| W3 | 跑的是**同一套门禁**（`scripts/verify_all.py`） | CI 跑另一套命令 → 两边判据不同，CI 绿不代表本机绿 |
| W4 | **不带** `--real-smoke` / `--real-recursive-probe` | CI 没有 Codex CLI 与凭据 → 必然红，而那不是代码坏了 |
| W5 | 显式设 `RUNNER=mock` | 少一次"CI 上偷偷调了真模型"的可能 |
| W6 | 装好依赖；Python 版本与 `requires-python` 对齐 | 缺步 / 版本不符 → CI 红在与代码无关的地方 |
| W7 | `fail-fast: false` | 一个平台失败会取消另一个 → 只看到一边，误以为另一边是好的 |
| W8 | 失败时收集诊断 | PR 上只看到 `exit 1`，还得本地复现 |
| W9 | **工作流与 `verify_all` 的实际步骤对得上** | 两份门禁慢慢变成不同的东西 |

### W9 是这一层最有价值的一条

它防的是"两份门禁悄悄分叉"。做法是从 `scripts/verify_all.py` 源码里抽出步骤名，
再核 CI 注释里逐字提到它们 —— 比硬编码一份清单可靠（硬编码会在改 `verify_all` 时过期）。

写完就**当场抓到一次**：我的 CI 注释原本是概述（"文档排版、语法、ruff、pytest…"），
而 W9 要求逐字，于是红了。修法是把注释改成逐字列出 8 个步骤名。

**W9 只统计无条件步骤**：`--real-smoke` / `--real-recursive-probe` 底下的两步
CI 里**故意不跑**，算进来会变成"要求 CI 声明它不跑的东西"—— 那是噪声，不是同步。

## 3. 我在本机**实际验证过**的部分

判据之外，我做了一次真实验证（不只是断言）：

```powershell
api\.venv\Scripts\python.exe -m pip install --dry-run -e "api[dev]"
# → Would install codex-battlefield-api-0.1.0
```

`-e "api[dev]"` 是 CI 里用的那一条。**从仓库根**执行能解析出这个包 ——
说明 CI 的安装命令不是想当然写下的。（`api/pyproject.toml` 在 `api/` 下，
从仓库根用路径形式的 extras 是可行的，但这一点值得当场确认。）

另外确认了 `.github/` **没有被 `.gitignore` 挡住**（`git check-ignore` 退出码 1），
否则工作流提交不上去，而 `verify_all` 的判据仍然会全绿。

## 4. 变异测试

| 变异 | 结果 |
|---|---|
| 矩阵只剩 `windows-latest` | ✅ W2 红 |
| CI 传 `--real-smoke` | ✅ W4 红 |
| CI 注释改成概述（不逐字） | ✅ W9 红（**写的时候当场抓到**） |

## 5. 已知限制（如实写下）

* **我没有真跑过这个工作流**。本机没有 Linux，也没有触发 GitHub Actions 的手段。
  所以"CI 在 Linux 上会绿"这句话我**没有证据** —— 有证据的是：
  工作流结构合法、与本地门禁一致、安装命令当场验证过。
* **Linux 上可能有测试要改**。本机扫了一遍，发现的 Windows 依赖大多是
  **被 mock 掉的**（例如 `test_orchestrator.py` 里 `subprocess.run` 被替换成
  `fake_run`，所以 `["powershell.exe", ...]` 从不真的执行），
  而 `test_migration.py` 的路径断言**已经同时接受两种分隔符**。
  但"扫一遍"不等于"跑过" —— 真有问题要等 CI 第一次跑出来。
  这正是 P6-2（Linux 支持）与本次**是两件事**的原因：本次交付的是"能发现问题的机制"，
  不是"问题已经解决"。
* **`pnpm` 版本写的是 10**，而本机用的是 11.8.0。`lib` 里没有 `packageManager` 字段，
  所以这是我选的而不是仓库声明的。若 CI 因锁文件版本差异失败，改这里的版本号即可。
* **没有缓存 Python 依赖**（只缓存了 pnpm）。给 pip 加缓存需要在干净环境里先跑一次
  才能确定 key，目前没做 —— 代价是每次 CI 多几十秒。
