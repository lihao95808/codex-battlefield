# 能力基准（考卷）

固定任务集 + **外部判据** + 对照报告：用来回答「换模型 / 换 harness / 改提示词之后是变强还是变弱」。

- 设计与判据：`docs/spec-capability-benchmark.md`
- 跑：`python scripts/bench_run.py --provider mock`（真实通道用 `--provider real`）
- 对照：`python scripts/bench_run.py --compare bench/reports/<a>.json bench/reports/<b>.json`
- 长跑（`--task-timeout` 超过 10 分钟）**必须放后台**：前台命令到 10 分钟会被工具层掐掉，连 traceback
  都拿不到。`bench/reports/*.json` 里 **`partial: true` 表示"进程还在跑"**，不是结论；
  `--compare` 会拒绝这种文件。中途可读 `bench/runs/<stamp>/` 的库看树跑到哪了。
- real 档再给两个**兜底**（都实测过）：
  - 进程内 `--hard-stop 900`：普通线程实现的硬停——事件循环一旦卡住，`--task-timeout` 这类 asyncio
    定时器**全部不会触发**（实测冻过 30-60 分钟）。到点落 `<report>.stacks.txt`（全线程栈）+
    一份 `partial` 报告，然后强制退出。
  - 进程外 `scripts/bench_supervise.py`：整解释器冻住时连硬停线程都不执行，只能靠**进程外**。
    它看报告 mtime 无进展就 `taskkill /T /F` + 落 `<report>.supervisor.txt`，退出码 4。
    长跑推荐都套上：`--report bench/reports/<stamp>-<provider>.json --stall-seconds 480 --max-seconds 1500`。
- **`blocked` 不再无条件递归**：环境性阻塞（例如"进程创建被执行策略拒绝"）以前每层都转成
  "规划阻塞恢复"子任务 → 螺旋（深度 4、并发 1→3）。现在恢复链**只允许一层**、可等待重试的不拆，
  见 `docs/spec-capability-benchmark.md` §3.8。

两条铁律：**成功率只由外部判据算**（树自称完成只进 `tree_claimed_done`）；**`unsolvable` 任务一旦声称完成，该档整体判失败**。

写新任务时的三条硬要求（都是踩过的坑）：

1. **fixture 拷贝出来必须自成 git 仓库**（`scripts/bench_run.py` 已自动 `git init`）：否则节点 worktree
   会是**外层仓库**的检出，agent 会把它当工程改。
2. **判据打在根节点的 worktree 上**（交付物在那里），不是那份种子的目标拷贝——打错了会得到
   「树说 done、判据说没交付」的假缺陷。
3. **能用行为判据就别用文本判据**：`contains "def multiply"` 只能证明那行文本在；优先写
   `command`（如 `["{python}", "-c", "from calc.ops import multiply; assert multiply(3,4)==12"]`）。
