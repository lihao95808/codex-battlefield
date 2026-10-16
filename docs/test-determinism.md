# 判据的确定性：门禁的红绿不能取决于"什么时候跑"

一份只在某些时刻、某些负载下红的判据，比没有判据更糟：真回归会淹在噪声里
（`incident-conclusions.md` §8 就是这条：整套跑偶尔红、单跑必绿 = 典型的假失败）。
这一份记录的是**一轮专门找"不确定性"的审计**：找到的每一处、原始症状、根因、处置，
以及"怎么知道修好了"。

不确定性只有三类，但症状长得都很像：

| 类别 | 症状（原始数字） | 根因 | 处置 |
|---|---|---|---|
| 墙上时钟 | `assert 4 == 3`：白天绿、晚上红 | 燃料分在**另一个模块**的 `utcnow` 上算，冻结时钟只冻了一处 | 两处都冻；变异证明这行是承重的 |
| 墙上时钟 | 每天头一分钟必红（00:00:00–00:00:59） | `deadline_hour = (hour - 1) % 24` 在本地 0 点绕回前一天 23 点，"已过"分支当天不可达 | 现算加油点时避开午夜；洞本身另立判据钉住 |
| 负载 | `p50=1432.5ms > 700ms`、比例 `23.12 > 8`（`-n auto` 下） | 判据量的是"这台机器现在有多空"，不是代码多快 | 取最好一次（3 趟），不取中位数；阈值不动 |
| 负载 | 槽位容量：20 秒采样窗口里没采到 holders | 1 秒持有窗口被抢 CPU 的调度吃掉 | 持有窗口 1s → 5s |
| 读者被写者挡住 | `PermissionError: [Errno 13] ... godot.resource.state.json` | Windows 上 `os.replace` 换名的瞬间目标文件打不开，而**读者是测试自己的采样** | 采样把 `OSError` 当"这一拍没采到"，并记次数；产品侧读者另立判据 |
| 共享目录 | 心跳 fixture 偶发看到"陈旧" | `machine_root_of(settings)` 落在 xdist worker 共享的 `workspace` 上，残留文件翻面 | fixture 显式删掉残留状态文件 |
| 可控跳过 | 跳过数在 1↔2 之间变 | 竞争复现不出来时 `skip`，"跑两次一致"这条观察因此失效 | 改成两侧都断言（复现/未复现都是合法结果，但必须二选一为真） |

## 1. 时间：谁的时间？

### 1.1 燃料分（`time_pressure`）在别的模块里算

`test_orchestrator.py::test_auto_concurrency_fuel_score_drives_midrange_target` 实测
白天跑绿、晚上跑红（`assert 4 == 3`）。原因不是"阈值太紧"，而是**冻结错了时钟**：

* `time_pressure` 由 `api/app/concurrency.py` 用**它自己的** `utcnow` 算；
* 判据只 patch 了 `orchestrator_module.utcnow`，于是 concurrency 那边看到的还是墙上时钟，
  "距加油点还有几分钟"随一天里的时刻变 → 结果变。

**修法**：`app.concurrency.utcnow` 与 `orchestrator_module.utcnow` 一起冻在同一个时刻。

**怎么知道这行是承重的**：把 concurrency 的冻结时刻改到 19:30（落在 120 分钟冲刺窗口内），
判据立刻红成 `assert 5 == 3` —— 冻错/不冻就会被抓住，不是"改了也照样绿"。

### 1.2 每天头一分钟：午夜把"减一小时"绕回前一天

`test_orchestrator.py::test_auto_concurrency_sprints_after_fuel_deadline_when_queue_remains`
为了在**任意时刻**都能落到"已过加油点"分支，用现算的加油点：

```python
deadline_hour = now.hour if now.minute > 0 else (now.hour - 1) % 24   # 旧写法
```

本地 0 点整（`minute == 0`）走的是 `(0 - 1) % 24 = 23`，而 `local_now(00:0x) >= 23:00` 为假
→ "已过"分支当天不可达 → 那条判据**每天有 60 秒必红**。一分钟的红比一小时更难遇上，
也更容易被当成偶发（"重跑就绿了"）。

**修法**：本地小时为 0 时把时区偏移放到 +1（本地时间就成了 1 点），再取 `local_hour - 1`。
**洞本身另立判据**：`test_concurrency.py::test_fuel_deadline_hour_wraps_at_midnight` 把两种取值
并排钉在真实生产函数上 —— 同一批输入下 `deadline=23` 落不到 `utilization_sprint`、
`deadline=0` 能落到。这样"午夜不可达"这件事以后是**判据在管**，不是注释在管。

**已知的重复实现**：同一套燃料公式在 `api/app/concurrency.py` 与
`scripts/local_watchdog_state.py` 各有一份。脚本那份用脚本模块自己的 `utcnow`，
所以它的判据（`test_local_watchdog_state.py`）冻的是那个模块 —— 那条是确定的。
但改公式必须同时改两处，这一点还没有判据盯着（见 §5 未验证）。

## 2. 负载：不是时钟，是机器被抢

### 2.1 规模基线取"最好一次"

`test_scale.py` 的 S3/S4 在 `-n auto` 下红过：`p50=1432.5ms > 700ms`、`best/p50` 比例
`23.12 > 8`，而同一条用例单独跑 14.6 秒全绿。区分两件事之后就好办了：

* **S3 量的是"这份代码在这台机器上快不快"** → 取 3 趟里最好的一次（趟间隔 0.05 秒）；
* **"这台机器能扛多少并发"** 属于 `bench/`（容量基线），不该由一个单元判据兼职。

**阈值一个都没放松**：放松阈值等于承认"就是这么慢"。改的是"哪一次测量算数"及其理由。

### 2.2 槽位容量：1 秒窗口输给调度

`test_godot_slot_capacity.py` 让 3 个真进程争 2 个槽，再用 20 秒的采样循环去观测 holders。
持有窗口原来是 1 秒：整机被抢时，"进程都跑完了"和"状态被采到"之间会输掉。改成 5 秒
（仍然是短测试，但不再和调度器赌运气）。

### 2.3 读者被写者挡住：`PermissionError: [Errno 13]`

这条在 `verify_all` 里又红了一次，症状是 **测试自己的采样读** 炸了：

```text
PermissionError: [Errno 13] Permission denied: ...\test_godot_slot_capacity_limit0\godot.resource.state.json
```

根因：写者用 `os.replace` 换名（原子落盘的另一半），换名瞬间目标文件处于"待替换"状态，
**读者打不开**。注意这与 `incident-conclusions.md` §8.2 记的两层修法不是同一处：

* 那两层是**写者侧**（临时名唯一 + 换名重试），仍然有效；
* 这次红的是**读者侧**。产品的读者 `_may_attempt_now` 本来就把 `OSError` 当"放行"
  （仲裁是优化，坏了不能让人永远等），但测试的采样循环只捕了 `ValueError`。

**修法**：采样循环同时把 `OSError` 当"这一拍没采到"，继续采；并且统计 `read_errors`
写进失败信息 —— 免得"文件一直读不了"被这个 `except` 吞成"只是没采到"。

**新增判据**：`test_godot_queue_priority.py::test_may_attempt_now_fails_open_when_state_file_is_unreadable`
拿一个**目录**冒充状态文件（Windows 上 `open(目录)` 正是 `Errno 13`，POSIX 上是
`IsADirectoryError`，两者都是 `OSError`），先断言这个替身真的读不了，再断言仲裁放行。

**变异记录**：把产品的 `except (OSError, ValueError)` 改成 `except ValueError`
→ 判据在 fail-open 那一行红，报的就是 `PermissionError: [Errno 13]`（与事故同一种错误）。

## 3. 共享状态：不属于任何人的目录

* **跨用例的"工程绑定"泄漏（本轮修掉，第三次同类）**：`api/tests/conftest.py` 已经把状态根
  隔离到"每个 pytest 进程一个临时目录"，但一个进程要跑几百个用例 —— 其中若干用例会
  `project_store.mount()` / `set_active()` 写出 `<state_home>/active.json`，
  于是**同一进程里排在后面的用例继承了那个绑定**：`Settings.machine_root` 指向共享状态根
  而不是自己的 `tmp_path`，`_watchdog_hook_state` 读不到自己刚写的 `codex_health.json`
  → `assert state["codex_health_exists"] is True` 变成 `assert False is True`。
  症状极具迷惑性：**连跑三次，红的集合分别是 5 条 / 1 条 / 4 条，互不相同**，
  而每一条单独跑都在 5 秒内通过（xdist 的动态分配决定谁继承谁的绑定）。
  修法：autouse fixture 在每个用例开始前删掉 `<state_home>/active.json` 并清掉
  `CODEX_BATTLEFIELD_PROJECT_KEY`；修完 `pytest -q` 从"每次红一批"变成 **1693 passed**。
  **只清绑定、不搬状态根**是刻意的：状态根本身在进程内是 import 期快照
  （`orchestrator.settings`），搬走会让"校验用的状态根"与 `/api/projects` 报出来的状态根
  不一致 —— 实测那样会打红 `test_project_binding.py` 的两条 B2。
  **两处口径不一致本身是产品侧的账**（生产里两者恰好总是同一个，所以看不见），
  留在这里作为已知项。
* `test_steward_push.py` 的心跳 fixture 落在 `machine_root_of(settings)`，而 xdist worker 之间
  共享同一个 `workspace` → 别的用例留下的 `watchdog_status.json` 会把"陈旧"判定翻面。
  修法：fixture 里显式 `unlink(missing_ok=True)`，不依赖"上次跑完是干净的"。
* `test_godot_state_writes.py` 的反面对照原本在竞争复现不出来时 `skip`。**跳过数本身就是噪声**：
  两次 `verify_all` 的 `1 skipped` 与 `2 skipped` 无法比较，"跑两次一致"这条观察因此失效。
  改成"复现/未复现两种结果都断言为真"——它验的是"竞争不会产生非法 JSON 与残留临时文件"，
  这件事在两种结果下都成立。

## 4. 写判据的五条（这一轮总结）

1. 时间要么**注入**（函数收 `now=`），要么**冻结到真正做计算的那个模块**——不是调用方。
2. 要"任意时刻可达"的分支，先在本地午夜这类边界上验一遍可达性；`-1`、`% 24`、
   `+ timedelta(days=1)` 这些绕回表达式都要单独看。
3. 负载敏感的判据取"最好一次"，并在文档里说清它量的是**代码能力**还是**机器容量**。
4. 采样循环必须把"这一拍读不到"与"一直没有"分开记，失败信息里带次数。
5. 不确定的观测写成"两种结果都为真"，不要用 `skip`——跳过数变化会让你失去"两次跑一致"这个信号。

## 5. 仍未验证（如实记）

* **换时区跑一遍没做过**。本轮的午夜洞是靠**冻结时钟**证明的，不是靠真的在午夜跑一遍；
  `TZ=UTC-8` / `TZ=Asia/Tokyo` 下的结果没有实测数据。
* `os.getppid()` 在 xdist 下"一定存活"是**假设**（pytest 控制器进程活着），
  没验过 worker 被单独杀掉的情形。
* 两份燃料公式的**一致性**没有判据盯着（只是"都要改"这条口头约定）。
* 真实 CI（双平台）没有触发过，见 `ci.md`。

## 来源

* 判据：`api/tests/test_concurrency.py`、`api/tests/test_orchestrator.py`、
  `api/tests/test_scale.py`、`api/tests/test_godot_slot_capacity.py`、
  `api/tests/test_godot_queue_priority.py`、`api/tests/test_godot_state_writes.py`、
  `api/tests/test_steward_push.py`
* 实现：`api/app/concurrency.py`、`api/app/tools/godot_with_timeout.py`
* 相关记录：`incident-conclusions.md` §8（假失败与 §8.2 的两层修法）、
  `scale-baseline.md`（阈值余量为什么定那么高）
