# 文档地图

这个仓库的文档分三类：**怎么用**、**为什么这么设计**、**做到哪一步了**。先读哪份取决于你要干什么。

## 只想用起来

| 文档 | 内容 |
|---|---|
| [`../README.md`](../README.md) | 安装、5 分钟上手（模拟 runner，不需要 Codex CLI）、跑测试、配置速查 |
| [`usage-accounting.md`](usage-accounting.md) | token/费用是怎么统计与展示的 |
| [`cost-accounting.md`](cost-accounting.md) | 花费归因与成本预测的口径：四类"读不到"怎么分辨、预测为什么给区间、`blocked` 为什么不算样本 |
| [`scale-baseline.md`](scale-baseline.md) | 千节点规模基线：怎么量的、实测数字、阈值凭什么定这么高、变异测试记录 |
| [`auto-policy.md`](auto-policy.md) | 自动策略：四条策略的触发条件、为什么"拿不到数据不触发"、为什么只判定不执行 |
| [`action-risk.md`](action-risk.md) | 动作风险分级与预授权：为什么高风险是常量不可预授权、三道闸的关系、授权记录里留什么痕 |
| [`incident-conclusions.md`](incident-conclusions.md) | 结论文档：事实与人的判断怎么分开、为什么"空段≠没有"、为什么只有人写的才算结论 |
| [`steward-watchdog-loop.md`](steward-watchdog-loop.md) | 管家×看门狗：注意账为什么必须过期、档位审计为什么"记录先于生效"、危险方向怎么判 |
| [`steward-delivery.md`](steward-delivery.md) | 目标投递与进度：为什么不做后台推送、四条"不编"的规矩、`get_tree` 没有 `root_id` 那个坑 |
| [`observability.md`](observability.md) | 端点耗时与关联 ID：实测基线、为什么分位数是滑动窗口、失败请求为什么也要计量 |
| [`data-retention.md`](data-retention.md) | 数据保留：三种东西三种待遇、为什么必须先列"不能删什么"、锚点为什么永不清理 |
| [`crash-injection.md`](crash-injection.md) | 崩溃注入：还原锚点为什么必须原子写、`.backup` 怎么救回第一次写、源级判据要断完整调用 |
| [`frontend-split.md`](frontend-split.md) | 前端拆分与按需轮询：为什么不停在"看不见也拉"、文件体量棘轮怎么用、批量改写的三个坑 |
| [`checkpoint-git-anchor.md`](checkpoint-git-anchor.md) | 检查点、账本、Git 锚点与恢复 |
| [`provider-setup.md`](provider-setup.md) | 可选：把节点的 Codex CLI 指向你自己的 OpenAI 兼容端点，不装不影响使用 |
| [`bilingual-ui.md`](bilingual-ui.md) | 界面双语与术语表：覆盖范围怎么声明、8 条不变量是什么、加语言/加文案的操作顺序 |

## 想知道它为什么长这样

按依赖顺序读，前面的定了后面的判据：

1. [`spec-modularization.md`](spec-modularization.md) — 为什么把判据从 `orchestrator.py` 拆成纯模块；
   里面有反缩水契约（§7.3）与其例外，后面几份 spec 都引它。
2. [`spec-parent-child-obligations.md`](spec-parent-child-obligations.md) — **核心**：父子之间的
   "债务"抽象。父节点能否完成，由入站债务是否为空决定，而不是由子节点状态决定。
   含 §5 schema、§6 门控判据与两条接线硬约束、§7 两个方向的传递规则、§9 看板。
3. [`spec-constitution-amendment.md`](spec-constitution-amendment.md) — 宪法在树运行中怎么修订：
   版本下沉/上浮、按状态分治迁移、方向敏感（收紧必须再验证、放宽不得洗白）。
4. [`spec-resource-governance.md`](spec-resource-governance.md) — 机器安全守卫、额度池、
   自动休眠与自启动；含 R0–R6 的设计与一轮对抗性审计的修正记录。
5. [`spec-capability-benchmark.md`](spec-capability-benchmark.md) — **能力基准**：固定任务集 + 外部判据 +
   对照报告。用来回答"换模型 / 换 harness / 改提示词之后是变强还是变弱"，以及"它会不会谎报完成"。
6. [`spec-verification-independence.md`](spec-verification-independence.md) — **验收面独立**：把"验收判据"
   冻结成指纹、产出方改了它立刻成为阻断债；按角色分层模型（便宜 worker + 强 acceptance）；
   自评校准先只采集。
7. [`spec-requirement-traceability.md`](spec-requirement-traceability.md) — **需求→证据追溯**：每条原始要求
   都要有可核对判据，漏需求（没判据）与没做到（判据没过）都是阻断债。它回答的是"宪法**符合目标**吗"，
   而前六份回答的是"实现**符合宪法**吗"。
8. [`spec-task-isolation.md`](spec-task-isolation.md) — **任务间隔离（R2）**：写边界/读边界/凭据面各靠什么、
   哪些做不到（如实列出），以及**推翻 R0 旧结论**的那次实测。读隔离靠的是 codex 沙箱（不是我们的代码），
   所以留了"升级 CLI 后重跑"的手工步骤。

## 想知道"现在到底做到了哪一步"

| 文档 | 内容 |
|---|---|
| [`../PLAN-STATUS.md`](../PLAN-STATUS.md) | 分步计划与完成状态（含"已回退/未接线"的标记） |
| [`../VERIFICATION_STATUS.md`](../VERIFICATION_STATUS.md) | 最近一次验证的证据与命令 |
| [`../FINAL_GOAL.md`](../FINAL_GOAL.md) | 目标定义与验收边界 |

## 两份 spec 里的"实施状态"小节

`spec-parent-child-obligations.md` §11.1 与 `spec-constitution-amendment.md` §11.1 是**按事实写的**
状态表：哪一步已完成、哪一步只完成了一半、哪一步被回退过以及为什么。设计文档容易读成
"这些都是做好的"，这两张表就是为了防这种误读——包括记录未做成的事。例如前端构建只在干净 clone
里用 `pnpm build` 验证过（15.67 秒通过），而**那一版候选快照里没有重跑完整的 Python 全量**
（代码与测试树与当时已跑的 682 绿完全一致，只是没有在那个目录里再跑一遍）。

## 读文档时值得注意的三条惯例

1. **实测教训直接写在 spec 里**，不另起"经验总结"：例如"默认阻断会让 14 条既有契约变红"
   就写在 §7.1.4 的严重度规则旁边。数字与症状保留原样，不写成"有点慢"。
2. **回退过的方案保留记录**：被回退的接线连同症状（5993 秒后 `RecursionError`）一起留在
   §6.4，因为下一版必须满足的那两条硬约束正是从它来的。
3. **"接线看着在、实际是死的"是这里最常见的坑**，所以接线类用例断言的是**可观测结果**
   （verifier 真的被调用、子节点数不变、债务真的被清），而不是"函数被调用过"。

## 给维护者：再发布一次

**不要从开发仓库直接 `git push` 到这个仓库。** 它保持**孤儿快照**的形态——上面的历史里
带着不该带出去的东西（本机绝对路径、旧项目名、内网域名——用 `scripts/scan_history_privacy.py` 扫一遍
就知道，历史上这类命中是六位数级别）。直接推历史 = 把它们一起带出去。

这条策略**与当前可见性无关**：仓库现在是私有的，但可见性是随时可能改的设置（已经改过一次），
而历史一旦推上去就很难收回。所以按"随时可能公开"来定流程，而不是按"现在是私有"来定。

### 当前形态：2 个提交，仍然没有开发历史（2026-10-14 起）

```text
f137097  修复：真实 runner 在三处环境依赖上必然失败，并让沙箱档可配
d837bd2  Codex Battlefield: recursive Codex task-tree orchestrator   ← 孤儿根
```

**这个仓库不再是"只有一个提交"。** 2026-10-14 在 `d837bd2` 之上加了一个提交（修了三个必然失败 +
一个可配项），它的父提交就是孤儿根，所以仍然**不携带任何开发历史**——安全性来自"根是孤儿"，
不是来自"只有一个提交"。

**因此下面那段旧流程里的 `git push --force` 现在是危险的**：它会用一份孤儿快照覆盖掉 `f137097`。
要做新一轮快照时，按"**在孤儿根之上追加**"来发，而不是重建根：

```powershell
# 1) 在开发仓库提交到最新
git -C <dev-repo> add -A
git -C <dev-repo> commit -m "..."

# 2) 在临时目录取最新树
cd $env:TEMP
git clone -q -b master <dev-repo> cb-pub
cd cb-pub

# 3) 推前自检（本仓库自带，只读历史）
python scripts/scan_history_privacy.py --paths-only

# 4) 把这份树作为【一次新提交】推到目标仓库的 main 之上
#    —— 用 --orphan 重建根只在"必须重置历史"时才做，且要先确认愿意丢掉现有 main
git remote add pub https://github.com/lihao95808/codex-battlefield.git
git fetch pub main
git checkout -b pub-main pub/main
git checkout <dev-repo> -- .          # 或 rsync 整棵树
git add -A
git commit -m "..."
git push pub pub-main:main
```

判断标准只有一条：**根是孤儿 + 每次发布都不带开发历史**。提交计数本身不是契约。

发布前的最小检查清单：

1. `python scripts/scan_history_privacy.py --paths-only`：无本机路径 / 内网域名命中；
2. `api/` 里 `python -m pytest -q` 全绿（配置已开 `-n auto` 并行；一条命令约 100 秒）；
3. `python -m ruff check --config api/pyproject.toml api scripts` 干净——**范围必须同时覆盖 `api/` 和
   仓库根的 `scripts/`**（静态检查抓"名字写错/变量没用/不变量没写"这类真错；2026-09-22 那次
   `scripts/bench_run.py` 里的未定义名字只在 real 档触发，门禁却只看了 `api/`，于是连丢两轮真实基准）；
4. `web/` 里 `pnpm test && pnpm lint && pnpm build` 全绿；
5. `python scripts/check_markdown_layout.py .`：文档表格列数与代码围栏配对无问题
   （改文档时最容易被改坏的地方，且坏得很安静——少一个 `|` 渲染出来整行错位）；
6. 干净 clone 里真的跑得起来：`cd api && py -3.12 -m venv .venv` → `.\.venv\Scripts\python.exe -m pip
   install -e ".[dev]"` → 起 API → `GET /api/tree` 返回 200。

`scripts/verify_all.py` 一次跑完上面第 2–5 条（外加 JSON Schema 校验与语法检查）。它跑的静态检查
覆盖哪些目录由 `LINT_PATHS` 单一来源决定，并有测试盯着这份范围（`api/tests/test_repo_gates.py`）——
**门禁少看一个目录不会报错，只会安静地变弱**。

（venv 放 `api/.venv` 是约定位置：看门狗与 goal 模式的分离式 scheduler 按它找解释器；
放仓库根 `.venv` 也能跑，代码会两种都探。）

