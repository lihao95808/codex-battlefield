# 真浏览器 E2E（P5-3）

## 1. 为什么不用 `page.route` 回放假数据

这是本文件唯一真正重要的取舍。

把 `/api` 拦住、回放写死的 JSON，测出来的是"前端在**给定的** JSON 下长什么样"。它跑得快、
不依赖后端，但它恰好绕过了最容易坏的那一段：**接缝**。前后端字段名、形状、空值语义、
路由前缀一旦漂移，回放数据的测试**永远绿**——而真实用户看到的是白屏。

所以这里起**两个真进程**（`web/playwright.config.ts` 的 `webServer` 数组）：

| 进程 | 端口 | 隔离手段 |
|---|---|---|
| 后端 `uvicorn app.main:app` | `8877`（`CODEX_BATTLEFIELD_E2E_API_PORT`） | `CODEX_BATTLEFIELD_STATE_HOME=web/.e2e/state` + `CODEX_BATTLEFIELD_DATABASE_URL` 指到同一个目录下的 sqlite |
| 前端 `vite` | `5273`（`CODEX_BATTLEFIELD_E2E_WEB_PORT`） | `CODEX_BATTLEFIELD_API_PORT=8877` 传给 vite 的代理配置 |

两个端口都刻意避开 `8765` / `5173`：那是开发者本机常驻的实例。
**不隔离的 E2E 会改掉你正在看的那个库**，这比没有 E2E 更坏。

`web/vite.config.ts` 因此多了一行 `const apiPort = process.env.CODEX_BATTLEFIELD_API_PORT || '8765'` ——
这是为了 E2E 而做的最小改动，默认行为一字不变。

## 2. 实测踩到的三件事

### 2.1 本机的 headless shell 下不下来，但完整 chromium 能跑

`pnpm exec playwright install chromium` 在这台机器上**下完 197.5MB 的完整包后，
在 118MB 的 `chromium_headless_shell` 那一步超时**（10 分钟没动）。
于是 `chromium.launch()` 报 `Executable doesn't exist at ...chrome-headless-shell.exe`。

修法不是重试下载，而是**别用 headless shell**：

```ts
use: { ...devices['Desktop Chrome'], channel: 'chromium' }
```

`channel: 'chromium'` = 完整浏览器 + 新 headless 模式。实测能起来：
`web/_browser_probe.mjs` 打印 `{"ok":true,"text":"ok","version":"156.0.8078.4"}`。

> 这条值得单独记：`docs/frontend-split.md` 里写着"本机 headless Chrome 探针挂过 120 秒"，
> 所以 P5-3 一度被当成跑不通。真实的墙是**那个包的下载**，不是浏览器本身。

### 2.2 `webServer` 的解释器不能靠 PATH

第一版写 `python -m uvicorn ...`，Playwright 只报一句
`Process from config.webServer was not able to start. Exit code: 1`。真因藏在它的 stderr 里：

```
No module named uvicorn
```

命中的是系统 Python 3.11，而 `uvicorn` 装在 `api/.venv` 的 3.12 里。
现在配置里显式解析 `api/.venv/Scripts/python.exe`（Unix 上是 `bin/python`），
解析不到才回退 `python`。

### 2.3 `__dirname` 在 ESM 里不存在

`web/package.json` 有 `"type": "module"`，所以 `playwright.config.ts` 是 ESM，
`__dirname` 直接 `ReferenceError`。用 `import.meta.dirname`（Node ≥ 20.11）。

## 3. 覆盖到哪条链

ROADMAP 的判据是"绑定工程 → 起树 → 看到进度 → 批准动作 → 导出诊断"。逐条对应：

| 判据 | 测试 | 怎么判的 |
|---|---|---|
| 后端在隔离端口上活着 | `后端在隔离端口上活着，并给出可关联的 request id` | `/api/health` 的**精确形状** + `x-request-id` 头存在 |
| 绑定引导可操作 | `未绑定时界面给出可操作的引导` | 要么有"绑定"按钮、要么已经绑好（`当前` 徽标）——两者都算，但不许两者都没有 |
| 起树 | 后续两个用例的前置 | 填目标 → 生成草案 → **锁定按钮变为可点** → 点启动 |
| **看到进度** | `起树之后画布上能看到根节点与进度` | `.react-flow__node` 真的有节点渲染出来（不是接口返回值，是 DOM） |
| **批准动作** | `预授权面板：高风险动作不可勾选` | 展开 `details.preauth-panel`，逐行读 `input.pa-check` 的 `disabled`，至少一个必须是禁用的 |
| **导出诊断** | `导出诊断：拿到诊断包，且里面包含真实证据而不是空壳` | `/api/diagnostics?format=json` 返回 >200 字符且能解析 |

## 4. "批准动作"这一条的口径（别读大了）

这一组**没有**走实时 steward 动作流（`POST /api/steward/actions/{id}/approve`）：
那条路要求先有真实模型产出动作，`RUNNER=mock` 下没有。

走的是**预授权面板**那条路，并且刻意验证的是里面最硬的那条规矩：
**高风险动作不可预授权**。`PreAuthPanel.tsx` 里 `const disabled = !item.pre_authorizable`，
测试读的就是这个 `disabled`——把这一行改成 `false` 会让用例红（变异已验证）。

所以"批准动作"在这份 E2E 里的准确含义是：**授权界面在，且它的安全约束真的生效**；
不是"一次真实的动作审批被完整走完"。

**2026-10-18 补上了后半句**：`scripts/verify_steward_real_turn.py` 用**真实模型**跑一轮
管家对话，拿到它自己提议的 `retry_node`，然后走产品自己的 `steward_actions.propose`：
没授权 → `pending`；给了覆盖该动作+目标的预授权 → `approved` 且 `result` 以 `[预授权]` 开头；
高风险动作（`cancel_all`）连 `make_grant` 都不让建。数字与边界见 `docs/action-risk.md` §8。
它仍然**不进**这套 E2E（要真实额度、且与本文件的接缝目标不同）。

## 5. 怎么跑

```powershell
cd web
pnpm exec playwright install chromium   # 一次就够（本机受限网络下可能要重试）
pnpm exec playwright test               # 起两个真进程 + chromium
pnpm exec playwright test --ui          # 想肉眼看时用这个
```

`scripts/verify_all.py` 里也有这一步（`前端 E2E（真浏览器 + 真后端）`），
CI 上会先跑 `pnpm exec playwright install --with-deps chromium` 再进门禁。

* 跑完的痕迹（失败时的 trace / 截图、`test-results/`、`.e2e/`）都在 `.gitignore` 里；
* 清理：删掉 `web/.e2e/` 即回到干净状态（那是**隔离的状态根**，不是你的开发状态）。

## 6. 边界与未做

* **`RUNNER=mock`**：证明"界面与 API 的接缝是对的"，**不证明**"真实模型能跑完一棵树"。
* 只覆盖 Chromium 一个内核（WebKit/Firefox 没跑）：这个产品的使用场景是本机浏览器，
  多内核的边际信息很低，而每个内核都是真实的等待时间。
* 视觉回归（截图比对）**没做**：截图比对在不同 DPI/字体下噪声很大，
  在没有基线管理方案之前，它只会变成"经常要更新基线"的负担。
* 绑定**切换工程**这条分支没覆盖（E2E 里只验证了引导可见；真实切换需要重启 API，
  见 `ProjectOnboarding.tsx` 的说明）。
