# 看门狗与管家的兼容（SPEC）

状态：已落地
相关：`docs/spec-steward.md`（管家）、`docs/spec-resource-governance.md`（L4 监护者）、
`docs/spec-state-project-decoupling.md`（状态与工程解耦）

## 1. 一句话

**看门狗管"机器撑不撑得住"，管家管"这事该怎么办"。** 两者共存、不互替，
但**必须互相看得见** —— 否则看门狗的结论没人转达，管家的建议可能撞上看门狗刚做的决定。

| | 看门狗（watchdog） | 管家（steward） |
|---|---|---|
| 性质 | 定时、纯规则、无人值守 | 对话、按判断、用户驱动 |
| 触发 | 计划任务每分钟 | 用户说话 / 提醒推送 |
| 输出 | 改调度状态、拉服务、写状态文件 | 对用户说的话 + 待批准的动作 |
| 会不会动树 | **会**（自动续跑、休眠、清理） | **不会**（提议，用户批准后由服务端执行） |

## 2. 加的那一层：心跳

### 2.1 为什么需要（这是本次最重要的修复）

看门狗的失败模式**不是报错，而是静默停摆**：

* 计划任务被禁用；
* PowerShell shell 崩了；
* 脚本卡住。

那时树在**无人看管地跑**，而没有任何地方会报错。原先唯一的痕迹是
`watchdog.lock.json`（只在一次 pass 期间存在）和 `watchdog.log` 的 mtime
（API 不读），所以"看门狗还活着吗"**根本无法回答**。

### 2.2 做法

看门狗在**每次退出时**写 `<state_home>/watchdog_status.json`：

```json
{
  "started_at": "...", "finished_at": "...", "status": "completed",
  "reason": "", "duration_seconds": 4.2, "pid": 74044, "root_id": "",
  "api_url": "http://127.0.0.1:8765", "state_home": "C:\\work\\.codex-battlefield",
  "stale_after_seconds": 60, "interval_seconds": 60,
  "repo_root": "C:\\work\\codex-battlefield"
}
```

挂在 `Exit-WatchdogSingleInstance` 上 —— 那条路径**每次退出都会走**
（正常、异常、提前 return）。把"必写"挂在唯一的退出点上，而不是散在每条
`return` 前：散着写迟早有一条分支忘了写，而那条分支恰恰是出错的那条。

### 2.3 停摆怎么判：阈值来自**声明**，不是硬编码

```
停摆 ⇔ (now - finished_at) > interval_seconds + stale_after_seconds
```

两个数都由看门狗自己写进心跳。观测方（API / 管家）读它，不自己定阈值 ——
两边各自硬编码的话，把间隔改成 5 分钟后一边认为正常、另一边报警，
而这是**最难查的一类不一致**。

### 2.4 没有心跳 ≠ 停摆

文件不存在只说明"看门狗从未在这个状态根跑过"（新装、刚清零）。
这时报停摆就是每次新装都来一次假警，而假警会训练用户无视真警。
所以 `available: false` 与 `stale: false` 是**分开**的两个字段。

### 2.5 一个必须一起修的缺口：卡住的进程会让告警变哑

`Enter-WatchdogSingleInstance` 原先只判"持锁的进程还活着吗"。这留了个致命缝隙：

一个**卡住**的 powershell 进程是活的、也持有锁 → 之后每一次调度都走
"already running, exit 0" 分支 → **连心跳都不写**。
而观测方靠心跳判断停摆 —— 心跳不写就永远判不出来。
结果就是：**看门狗已经死了，而告警是哑的**，恰好在最需要它的场景失效。

两处修补：

1. **持锁过久也当失效**：锁龄超过 `max(600s, WatchdogStaleAfterSeconds × 10)`
   就回收锁并继续（那个"卡死"宽限特意比 `Test-StaleWatchdogShell` 的 60 秒大得多：
   后者是"一次正常 pass 该多久"，前者是"卡死该多久才算数"）。
2. **正常重叠时也写心跳**（`status: "overlapped"`）：否则"调度重叠频繁"在观测方
   看来和"停摆"一模一样，而这两种情况要做的处理完全不同。

## 3. 管家怎么看这一路

`build_status()` 的快照里多一段 `watchdog`：

* `heartbeat` —— 上面那个文件（含 `stale` 与 `stale_reason`）；
* `guardian` —— `l4_guardian_state.json`（L4 的结论：状态/模式/风险/摘要/七项检查）；
* `machine` —— `machine_safety.json`（内存压力）；
* `hibernating` / `reason` —— 归一化后的"现在是不是在休眠、为什么"。

**刻意只读文件，不调 API**：管家跑在 API 进程里，再回头 HTTP 调自己是没必要的
绕路，还会在 API 忙着的时候自己把自己卡住。

管家的提醒里对应四类信号（见 `steward_alerts._watchdog_alerts`）：

| kind | 等级 | 什么时候 |
|---|---|---|
| `watchdog_stale` | **action** | 心跳过期**且树里有节点** |
| `watchdog_failed` | warning | 上次跑失败 |
| `watchdog_hibernating` | warning | 资源/额度闸门让它休眠了 |
| `watchdog_no_heartbeat` | info | 从没收到心跳但树里有节点 |

`watchdog_stale` 给的是**最高等级**（`action`）：它不是树的故障，而是
**保护机制本身失效**了 —— 用户需要动手去查，而不是等它自己好。

**空树上不报停摆**：空树没有依赖看门狗的东西，这时报停摆是打扰
（新装、闲置都会命中）。只在真有活可看时才值得打扰用户。

## 4. 修掉的两处真实偏差

### 4.1 状态根解析不一致（会静默错位）

`battlefield_watchdog.ps1` 自己实现了一遍状态根优先级
（`-StateHome` → 环境变量 → `%USERPROFILE%\.codex-battlefield`），
**不读**用户配置文件 `~/.codex-battlefield.toml`；而 Python 侧
`resolve_state_home()` 的优先级是"环境变量 > 配置文件 > 默认"。

后果（实测）：配置把状态根指到 `C:\work\.codex-battlefield`，
而看门狗落到 `C:\Users\<user>\.codex-battlefield` —— 写的心跳没人看，
观测方于是认为"看门狗没在跑"。

**修法**：ps1 不再自己实现优先级。没显式给 `-StateHome` 时，它**问解析器**
要 `state_home`（`state_paths.py --get state_home`，那个会读配置文件），
只在解析失败时才退回猜测并记日志。显式给的 `-StateHome` 永远优先 ——
计划任务注册时会传，那是运维的明确意图。

### 4.2 安装/执行命令没传 `-StateHome`（会让下一条也失效）

`api/app/watchdog.py` 拼 `watchdog_install_command` / `watchdog_run_command` 时
不传 `-StateHome`。计划任务**不继承** `CODEX_BATTLEFIELD_*` 环境变量，
所以那两条命令生成的进程会退回默认状态根。已补上。

顺带：`_env_or_config()` 让 `codex_sandbox_*` 也能从配置文件读。
原先这些档位只认环境变量，而"看门狗重启 API"会丢掉它们 ——
在本机（codex 的 Windows 受限令牌建不起来，必须 `danger-full-access`）上，
表现是**整条真实执行链路失效**，而远端只报一个 `CreateProcessAsUserW failed`。

## 5. 一个必须记住的坑：`.ps1` 需要 BOM

**PowerShell 5.1（Windows PowerShell）在没有 BOM 时按系统 ANSI 代码页解码脚本**。
本机是 GBK，于是含中文注释的 UTF-8 `.ps1` 会被解成乱码，`}` 之类结构字符落在
字符中间 —— 表现是一串**看不懂的语法错误**（`Unexpected token '}'`），
而文件本身完全正确。

实测（`battlefield_watchdog.ps1`，3165 个非 ASCII 字节）：

| | 无 BOM | 有 BOM |
|---|---|---|
| PS 5.1 解析 | **4 个错误** | 0 个错误 |

也就是说这个脚本在修之前**根本跑不起来**（`powershell -File` 直接语法错），
只有 `powershell -Command` + `ParseInput` 看起来正常 —— 那正是当初误判
"这不是真 bug，是 PS 5.1 解码问题"的原因。

**规则**：仓库里**非 ASCII 的 `.ps1` 必须带 UTF-8 BOM**。仓库其余源码保持
无 BOM、LF-only（Python 与 TS 都不需要）。注意 `edit` 类工具会**剥掉 BOM**，
改完 `.ps1` 要重新确认。

## 6. 验收（可反驳）

| 编号 | 判据 | 怎么反驳 |
|---|---|---|
| P1 | 提醒指纹只看 kind+level，措辞/数字变化不算新情况 | `test_steward_push.py::test_p1_*` |
| P1 | 持续存在的提醒在 5 轮循环里**只推 1 次** | `test_p1_a_persistent_alert_produces_exactly_one_edge` |
| P3 | 默认阈值是 `action`；未知等级永远不过阈值 | `test_p3_*` |
| P4 | 每次推送都带 `reason`，且四种情形文案不同 | `test_p4_push_events_carry_a_reason` |
| P5 | 推送流用 `asyncio.to_thread`（不阻塞事件循环） | `test_p5_the_stream_evaluates_off_the_event_loop` |
| P5 | SSE 帧格式合法（格式错会被浏览器静默丢弃） | `test_p5_sse_frames_are_well_formed` |
| W1 | 心跳过期 → `stale=true`，且说明阈值来源 | `test_w1_old_heartbeat_is_stale_with_an_explanation` |
| W1 | 阈值跟着心跳里声明的间隔走 | `test_w1_threshold_follows_the_declared_interval` |
| W2 | **没有心跳文件不判停摆** | `test_w2_missing_heartbeat_does_not_claim_staleness` |
| W3 | 心跳的 ISO 字符串（含 `Z`）能解析 | `test_w3_*` |

## 7. 运维

```powershell
# 注册（每分钟；含登录时启动）
powershell -File scripts\register_battlefield_watchdog_task.ps1 `
  -StateHome "C:\work\.codex-battlefield" -IntervalMinutes 1 -WithLogonTrigger

# 查
schtasks /Query /TN CodexBattlefieldWatchdog /V /FO LIST
Get-Content <state_home>\watchdog_status.json

# 卸
schtasks /Delete /TN CodexBattlefieldWatchdog /F
schtasks /Delete /TN CodexBattlefieldWatchdog-Logon /F
```

**注册脚本目前没有 unregister 参数**，只有打印出来的删除命令；
上面这两条就是卸载方式（`-Logon` 那个若被 Windows 拒绝会退化成
`Startup` 文件夹里的 `.cmd`，删文件即可）。

## 8. 一个反直觉的运维坑：删了库里的节点，它可能自己回来

`Orchestrator._restore_snapshot_if_missing` 的行为是：**库里没有根节点时，
从状态快照把它还原回来**。这是防数据丢失的好设计，但它意味着
"手动清库"清不干净 —— 实测（2026-10-15）：删掉 `task_nodes` 里的一行后，
约 20 秒内它就被还原了（快照里还留着它），连 `event_records` 一起。

要真的清掉一棵树，得同时删三处：

```
<project>/state_snapshots/<root_id>/
<project>/full_checkpoints/<root_id>/
<project>/checkpoint_ledger/<root_id>/
```

外加库里的 `task_nodes` / `event_records` 行。**先删文件、再删行**，
否则还原会赢。删之前务必确认这些快照里只有你要删的那棵树
（`latest.json` 的 `tasks` 列表）—— 那是真的数据，不是缓存。
