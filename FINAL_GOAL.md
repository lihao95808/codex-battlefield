# Codex Battlefield 终极目标与验收计划

## 终极目标

在 `C:\projects\codex-battlefield` 中完成一套可运行、可调试、可测试、可视化的递归 Codex Agent 战场系统。

它要解决的问题不是“把大任务固定拆成 3-5 个 worker”，而是让任务树动态生长：每个节点 Agent 都先判断自己是否能被一个 Codex CLI 以 300% 把握完成；不能就继续拆分；误判为叶子的节点只要验证失败一次，就转成父节点重新拆分；父节点必须真实整合并验收所有子节点成果后才允许 done。

最终这套系统要能承接类似下面的大任务：

> 制作一款 2D 横板过关格斗游戏的可玩垂直切片。游戏必须包含玩家移动、跳跃、基础攻击、敌人、命中/受击反馈、一个可完成的小关卡、基础 UI、运行入口和自动化验证。任务树必须遵守项目宪法：防止无限拆分，防止缩成小 demo；每个叶子节点必须由单个 Codex CLI 具备 300% 完成把握；任何叶子验证失败一次即转父节点；每个父节点必须真实验收并整合所有子节点成果。

这不是要求一次性生成商业完整游戏，而是要求递归 Agent 系统稳定地产出一个边界清楚、质量可验收、可继续扩展的游戏垂直切片。

## 核心铁律

1. 总任务先生成并锁定“项目宪法”，宪法定义质量下限、规模上限、非目标、全局验收标准和拆分规则。
2. 不设置固定最大深度和固定最大宽度；是否继续递归，由节点基于宪法和 300% 规则判断。
3. 节点只有在“一个 Codex CLI 进程能一次完成并可验证”时，才允许成为叶子。
4. worker 如果返回 `needs_decomposition`，节点立即转父节点。
5. verifier 只要返回 `fail` 或 `repair` 一次，叶子立即转父节点。
6. 父节点等待子节点时不占用“正在运行的 Agent”名额。
7. 父节点不能因为所有子节点 done 就直接 done；必须运行 acceptance agent 做真实整合验收。
8. 重复拆分、无收敛拆分、父节点多轮 repair 会进入人工审查状态，防止无限递归。
9. 每次真实 Codex CLI 调用必须保存 prompt、日志、最后消息、工作目录、宪法 hash，方便调试和复盘。
10. 看板必须显示树状层级、连线、状态、折叠、当前战场位置和每个节点的验收证据。

## 成熟技术路线

- 后端：FastAPI + SQLAlchemy + SQLite。
- 前端：Vite + React + TypeScript + React Flow。
- 执行器：MockCodexRunner 用于低成本测试；CodexExecRunner 用于真实 `codex exec`。
- 结构化协议：constitution、planner、worker、verifier、acceptance 全部使用严格 JSON Schema。
- 可视化：树状战场、状态连线、层级折叠、宪法面板、节点详情、运行记录、调试文件预览。
- 验证：单元测试、API 测试、前端测试、lint、build、真实 Codex CLI smoke、真实递归 probe。

## 强模式 Codex CLI

本机真实 Codex CLI 默认按强模式运行：

```powershell
$env:CODEX_BATTLEFIELD_CODEX_MODEL="gpt-5.5"
$env:CODEX_BATTLEFIELD_CODEX_REASONING_EFFORT="xhigh"
```

脚本参数也默认使用：

```powershell
--model gpt-5.5 --reasoning-effort xhigh
```

## 完成定义

这套流程只有在下面全部通过时，才算工程流程完成：

- `python .\scripts\verify_all.py` 通过。
- 后端 pytest 全部通过，目前为 18 个测试。
- 前端 vitest、lint、build 全部通过。
- 真实 Codex CLI smoke 通过：源头宪法、planner、worker、verifier、acceptance 都真实运行，并写出目标文件。
- 真实递归 probe 通过：源头宪法生成，root planner 拆分，子节点 worker/verifier 完成，父节点 acceptance 真实验收，root done。
- 看板能显示中文、任务树、连线状态、折叠、宪法、节点详情、run 调试文件，并支持生成草案、保存修订、锁定宪法、启动任务树的人工流程。
- API `http://127.0.0.1:8765/api/health` 返回 ok。
- Web `http://127.0.0.1:5173` 能打开。

## 推荐验证命令

```powershell
cd C:\projects\codex-battlefield
python .\scripts\verify_all.py
python .\scripts\real_codex_smoke.py --timeout 600 --model gpt-5.5 --reasoning-effort xhigh --constitution-runner real
python .\scripts\real_recursive_probe.py --timeout 900 --model gpt-5.5 --reasoning-effort xhigh --constitution-runner real
```

如果要一条命令跑完整验证，包括真实 Codex：

```powershell
python .\scripts\verify_all.py --real-smoke --real-recursive-probe --real-constitution --timeout 900 --model gpt-5.5 --reasoning-effort xhigh
```

## 启动真实大任务的最终目标描述

请基于 `C:\projects\codex-battlefield` 当前系统，启动一次真实 Codex Battlefield 任务：

“制作一款 2D 横板过关格斗游戏的可玩垂直切片。游戏必须包含玩家移动、跳跃、基础攻击、敌人、命中/受击反馈、一个可完成的小关卡、基础 UI、运行入口和自动化验证。先生成并锁定项目宪法，明确质量下限、规模上限、非目标、拆分规则和父节点验收规则。任务树不得固定最大深度或宽度，而是由每个节点根据宪法和 300% 规则判断是否继续拆分。任何节点如果不能确认由单个 Codex CLI 一次完成并验证，就必须拆分；任何叶子节点验证失败或 repair 一次，就必须转成父节点继续拆分。所有父节点必须真实整合并验收子节点成果后才允许 done。最终产物必须是一个可运行、可测试、边界清楚、质量可验收的游戏垂直切片，而不是过小 demo，也不是无限扩张的商业完整游戏。”
