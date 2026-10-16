# 端点耗时与关联 ID（P5-1 / P5-5）

实现：`api/app/metrics.py`、`GET /api/metrics`、`POST /api/metrics/baseline`、
`DELETE /api/metrics`、请求中间件（计时 + 关联 ID + 结构化日志）、界面 `MetricsPanel`、
探针 `scripts/probe_endpoint_latency.py`、基线 `bench/endpoint_baseline.json`。

判据：`api/tests/test_metrics.py`（19 例）、`test_metrics_api.py`（22 例）、
`test_metrics_baseline.py`（13 例）、`web/src/metricsView.test.ts`（12 例）。

## 1. 为什么 P5-1 与 P5-5 一起做

* **P5-1** 要回答"最慢的端点是谁、有没有退化"；
* **P5-5** 要回答"一个请求从界面到子进程能不能串起来"。

两者需要的底层是同一件东西：**每次请求都被计量，且带一个能一路带下去的 ID**。
分两套实现的结果会是两个各记一半的地方 —— 而排查问题时哪一套都不完整。

## 2. 实测结果：**没有需要立刻优化的慢端点**

用探针在**真实 API**（`uvicorn`，本机）上量了 26 个端点，预热 2 次 + 15 次取 p95：

| 端点 | p95 |
|---|---|
| `GET /api/tree` | 83.0 ms |
| `GET /api/auto-policy` | 74.0 ms |
| `GET /api/watchdog/state` | 62.9 ms |
| `GET /api/state/overview` | 45.9 ms |
| `GET /api/overview/cross-project` | 42.2 ms |

**全部端点都在 130 ms 以内。** 所以 P5-1 这次的交付价值在于
**"以后不会再悄悄慢下去"**，而不是"修好了一个慢端点"。这一点如实写进基线文件，
免得后来人以为这里曾经有过性能问题。

## 3. 四条设计规矩

| 规矩 | 理由 |
|---|---|
| **样本有上限**（每路由 1000 个） | 不设上限的指标记录**本身就是内存泄漏** —— 而它长得像一个很正常的监控功能 |
| 分位数是**滑动窗口**的，并在响应里写明 | 当全历史读，会在树跑了几小时之后得出错误结论 |
| **失败请求也进分位数** | 500 花掉的时间一样是真实负载；排除掉它们，指标会在系统最不稳定的时候最漂亮 |
| 按**路由模板**分组（`/api/tasks/{task_id}`） | 按真实路径分组会让每个 task_id 各占一行 —— 而那时正是最需要看清"哪个端点慢"的时候 |

**排序按 p95 不按平均**：平均会被大量快请求稀释，而用户感受到的是最慢那几次。

## 4. 关联 ID：一眼能对上的那条链

```
浏览器 fetch ──X-Request-Id──▶ 中间件（透传或生成）
                                 │
                                 ├─▶ 响应头回写同一个 ID
                                 └─▶ 结构化日志一行：{route, status, duration_ms, request_id}
```

三条必须成立：

1. **客户端带来的 ID 要透传**（网关/前端已有 ID 时链路才不断）；
2. **响应头要回写**（用户截图里的 ID 必须能在服务端日志里搜到）；
3. **CORSMiddleware 要 `expose_headers`**（否则前端 `fetch` 读不到非简单响应头）。

日志只记 `/api/*`：静态前端产物每个请求都记会把日志淹掉。

## 5. 基线怎么用

```powershell
# 1) 起一个带指标的 API
cd api; .venv\Scripts\python.exe -m uvicorn app.main:app --port 8766
# 2) 量
api\.venv\Scripts\python.exe scripts\probe_endpoint_latency.py http://127.0.0.1:8766
# 3) 把输出的 key/p95 写进 bench/endpoint_baseline.json（或 POST /api/metrics/baseline）
```

`GET /api/metrics` 会把超过基线 **1.5 倍**的路由列进 `regressions`，界面上排在最前并标红。

**容忍度 1.5 是刻意的**：p95 在共享机器上本来就会跳，抓 20% 的结果是所有人学会
忽略这个信号 —— 那时它就不再是判据了。判据要抓的是"慢了 5 倍"。

**没有基线时不做判断**：拿一个假想的阈值去卡，只会制造假警报。

## 6. 判据里三条"能红"的检查

| 检查 | 不钉住会怎样 |
|---|---|
| 基线**真的被加载**进注册表 | 文件躺在那里没人读，退化永远不报 |
| 基线里每个 key 都**对应真实路由** | 端点改名后基线变成死条目，静默失效 |
| 退化能被抓住、抖动不会被误报 | 判据恒真，或所有人学会无视它 |

## 7. 变异测试

| 变异 | 结果 |
|---|---|
| `route_key` 忽略路由模板 | ✅ M4 与 N1 红（每个 task_id 各占一行） |
| 日志里回读请求头取 ID（而不是用实际发出去的那个） | ✅ N4 红 —— **抓到的是真 bug** |

第二条值得单独说：第一版 `log_structured_request` 从 `request.headers` 里读 ID，
而"我们自己生成 ID"的那条路径上请求头里是空的 —— 于是**响应头里有 ID、日志里搜不到它**，
正好把关联 ID 的唯一用途废掉。

## 8. 已知限制（如实写下）

* **指标是进程内的**。多进程/多机汇总需要时序库或共享存储，那是另一个量级的依赖。
  每个 API 进程服务一台机器时，`GET /api/metrics` 读自己那份就够。
* **探针量不到流式端点**：`/api/events` 是 SSE，`urlopen` 会等到超时（量出来是 `inf`），
  已从探针里排除。要量它得量"首字节时间"，那是另一套方法。
* **基线是空树/小库下的数字**。大数据量下的绝对耗时由 `api/tests/test_scale.py` 单独钉
  （1000 节点、真实 ORM）。两者不重复：一个抓"查询写成了 O(n²)"，一个抓"比上周慢 5 倍"。
* **慢查询治理只做了"量"**：本次没有发现需要治理的慢查询，所以"治理"那半是空的。
  真的出现退化时，`GET /api/metrics?limit=5` 会直接指出来。
* 结构化日志目前只覆盖 HTTP 请求。**子进程侧**（codex exec 的调用）还没有 trace id ——
  "从界面到子进程串起来"这条目前只到 API 边界，最后一段要等 run_records 带上 request_id
  才能补齐。
