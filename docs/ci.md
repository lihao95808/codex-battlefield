# CI 与跨平台（P6-3 / P6-2）

实现：`.github/workflows/verify.yml`。判据：`api/tests/test_ci_workflow.py`（18 例，W1–W11）。

> **先说结论**：CI **一直在跑**（`on: push`），但在我"推完就走"的节奏下，
> 每一次运行都被下一次推送取消 —— 所以直到第 20 次运行都没有一次绿灯记录。
> 唯一一次真正给出结论的 `failure` 抓到的正是双平台 CI 该抓的东西：
> **Windows runner 的 cp1252 把门禁死在了第一步**（详见 §5、§6）。

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

## 5. **它一直在跑，是我没看** —— 一条被推翻的旧结论

这一节是本轮最值得记的部分：**下面第 6 节原来的第一句话（"我没有真跑过这个工作流，
本机没有触发 GitHub Actions 的手段"）是错的。** 工作流由 `on: push` 触发，
所以本仓库**每一次推送都在跑 CI**。

用仓库里缓存的那份 HTTPS 凭据调 GitHub API 就能看到（只读、不需要额外授权）：

```powershell
$cred  = "protocol=https`nhost=github.com`n`n" | git credential fill
$token = (($cred | Where-Object { $_ -like "password=*" }) -replace "^password=", "")
$h = @{ Authorization = "Bearer $token"; Accept = "application/vnd.github+json" }
Invoke-RestMethod "https://api.github.com/repos/lihao95808/codex-battlefield/actions/runs?per_page=20" -Headers $h
```

截至写这一节：**20 次运行、17 次 `cancelled`、2 次未完成、1 次 `failure`，
一次都没绿过。** `cancelled` 占绝对多数不是意外：工作流里有

```yaml
concurrency:
  group: verify-${{ github.ref }}
  cancel-in-progress: true
```

而我在同一轮里几分钟内连推好几次 —— **每一次新推送都把上一次的运行取消掉**。
所以"没有绿灯"的真实原因不是"没跑过"，而是两条流程缺陷：

1. **推完就走**：不等结论出来就推下一版；
2. **把"没看"当成"没跑"**：结论写进了 ROADMAP 与 `docs/acceptance-audit.md`，
   而它建立在"我无法触发 CI"这个当时就没核实的判断上。

> 教训：**"我做不到"和"我没看"是两件事。** 前者要写进缺口清单，后者只需一次查询 ——
> 但它们都会表现为"没有证据"，所以很容易互相冒充。这条同样适用于任何"没法验证"的结论。

## 6. 那次 `failure` 给出的东西（P6-3 的核心价值）

`cancelled` 里没有信息，但那一次 `failure` 有：

* run **38895747296**（commit `5fc0cc8`）
* **windows-latest**：作业 85 秒，而 `跑 verify_all（门禁）` 只跑了 **1 秒**就死；
  日志原文 `UnicodeEncodeError: 'charmap' codec can't encode characters in position 2-4`，
  位置是 `scripts/check_markdown_layout.py:96` —— 打印中文摘要那一行。
  根因：**GitHub 的 Windows runner 上 Python stdout 默认是 cp1252**，而本仓库脚本打印中文。
  本机是中文 Windows（cp936），所以本地永远绿。
* **ubuntu-latest**：同一作业跑了 **45 分钟整**，最后 `跑 verify_all` 只有开始时间、
  没有完成记录，作业以 `failure` 结束；这份 job 的日志下载是 **404**（runner 侧没有留下
  可下载的日志）。**没有结论就是没有结论** —— 记成"Linux 上 `verify_all` 没有一次跑完的
  记录"，而不是"Linux 有问题"或"Linux 没问题"。

这正是 P6-3 存在的理由：**这是"只有本机能过"的教科书案例**，而它的形态比预想的更基础 ——
不是路径分隔符，是**输出编码**。

### 6.1 修法

* `scripts/verify_all.py` 新增 `step_env()`：给**每个子进程**钉 `PYTHONIOENCODING=utf-8`
  与 `PYTHONUTF8=1`（含 pytest）；
* `scripts/check_markdown_layout.py` 自己兜底（能改 UTF-8 就改，改不了退化 `errors="replace"`）——
  **打印摘要永远不许把检查本身搞崩**，直接调用它时也安全；
* 工作流里也给 `跑 verify_all` 那一步设同样两个变量（覆盖"workflow 里直接调 python"的步骤）。

### 6.2 判据与变异（W10/W11）

| 判据 | 做法 | 变异 | 结果 |
|---|---|---|---|
| W10 | 用**真子进程**跑 `verify_all.run_step` 打印中文，宿主环境设 `PYTHONIOENCODING=cp1252` | 去掉 `run_step` 的 `env=step_env()` | ✅ 红，报的正是 CI 那句 `'charmap' codec can't encode characters in position 0-6` |
| W11 | 直接跑 `check_markdown_layout.py` 且 `PYTHONIOENCODING=cp1252`，要求退出 0 且打出摘要 | 去掉 `_ensure_output_encoding()` | ✅ 红，报错与 CI 日志**逐字相同**（`position 2-4`） |
| W10b | 工作流里必须有一步设 `PYTHONIOENCODING=utf-8` | —— | 静态，但覆盖"workflow 直接调 python"的路径 |

## 7. 修完编码之后的第一次运行（`38949119428`，commit `4917d75`）

这一节的数字是**从 Actions 日志里抄的**，不是估计：

| 平台 | 作业耗时 | 结果 | 失败点 |
|---|---|---|---|
| ubuntu-latest | **76 秒** | failure | `仓库静态检查（ruff）`：`F401 importlib.util imported but unused` |
| windows-latest | **4 分** | failure | 同上（逐字相同） |

三件事因此从"推测"变成了"事实"：

1. **cp1252 那一修真的管用**：Windows 作业这次跑过了「文档排版检查」（打印中文摘要那一步），
   而在修复前它在**第 1 步、1 秒内**就死了。
2. **双平台 CI 现在给出可行动的结论**，而且这次的结论抓的正是我自己的疏漏 ——
   我加了 W10 的探针测试、没在提交前跑 ruff。**门禁抓到了提交者**，这就是它该有的样子。
3. **ubuntu 侧不再"45 分钟不结束"**：同一套 `verify_all` 在 76 秒内走到了 ruff
   （先前那次 45 分钟未完成的原因仍未查清，只能记为"没有复现"）。

> 顺带说明：这次 ubuntu 与 windows **失败点相同**，不是巧合 —— 两边的失败原因相同，
> 而 `fail-fast: false` 的价值在于"原因不同时也能同时看到两边"。§2 的 W7 就是这么来的。

## 8. 已知限制（如实写下）

* **Linux 上 `verify_all` 没有一次跑完的记录**（45 分钟未完成，日志 404）。
  所以"CI 在 Linux 上会绿"这句话仍然**没有证据**；有证据的是：工作流结构合法、
  与本地门禁一致、安装命令当场验证过、Windows 那一侧的具体故障已被定位并修掉。
* **Linux 上可能有测试要改**。本机扫了一遍，发现的 Windows 依赖大多是
  **被 mock 掉的**（例如 `test_orchestrator.py` 里 `subprocess.run` 被替换成
  `fake_run`，所以 `["powershell.exe", ...]` 从不真的执行），
  而 `test_migration.py` 的路径断言**已经同时接受两种分隔符**。
  但"扫一遍"不等于"跑过" —— 真有问题要等 CI 第一次跑完才能说。
  这正是 P6-2（Linux 支持）与本次**是两件事**的原因：本次交付的是"能发现问题的机制"，
  不是"问题已经解决"。
* **`pnpm` 版本写的是 10**，而本机用的是 11.8.0。`lib` 里没有 `packageManager` 字段，
  所以这是我选的而不是仓库声明的。若 CI 因锁文件版本差异失败，改这里的版本号即可。
* **没有缓存 Python 依赖**（只缓存了 pnpm）。给 pip 加缓存需要在干净环境里先跑一次
  才能确定 key，目前没做 —— 代价是每次 CI 多几十秒。
* **CI 状态查询不进判据**：读 GitHub API 需要凭据，而凭据不能进仓库（见
  `docs/public-tree-hygiene.md`）。所以"CI 现在是什么状态"只能人工查 ——
  这条"看板"缺口如实记下，不做成假门禁。
