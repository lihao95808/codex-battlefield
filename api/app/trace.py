"""请求关联 ID（trace id）在**进程内**的传递（P5-5 的子进程侧）。

## 为什么需要它

P5-5 的目标是"一个请求从界面到**子进程**可串起来"。HTTP 侧已经有了
（`x-request-id` 透传/生成/回写 + 结构化日志，见 `docs/observability.md`），
但**真正干活的是 Codex 子进程**：它是另一个进程，日志在自己的 `runs/<id>/*.jsonl` 里。

所以要么把 id 传下去，要么承认"界面看到的 id 和后端跑出来的产物对不上"。
这个模块负责传下去，并且负责**它能覆盖到哪**这件事的诚实表达。

## 为什么不复用 `metrics.py`

`metrics.py` 带着 FastAPI 中间件与响应对象；runner 不需要那些东西。
把 contextvar 放在一个**只有标准库**的模块里，runner / orchestrator / main 都能引用，
不会把 web 框架拖进子进程那条路。

## 覆盖边界（写在代码里，免得被读大）

* **只在同一个逻辑请求内有效**：`contextvars` 在 asyncio 任务里是复制的，
  所以后台任务（树跑起来之后继续干活的那种）**看不到**发起请求的 id。
* 树是长跑物，一个请求只对应"起树"那一刻；后续每个节点的 run 由调度器发起，
  **没有**对应的 HTTP 请求。所以 `run_records.request_id` 允许为 `NULL`，
  那表示"这次运行不由某个 HTTP 请求触发"——**不是**"忘了记"。
* 因此这里**不伪造** id（不生成一个假的填进去）：没有就是 `None`。
"""

from __future__ import annotations

import contextvars
import os

#: 子进程拿到的环境变量名（runner 注入；子进程的工具脚本可以读它）。
ENV_NAME = "CODEX_BATTLEFIELD_REQUEST_ID"

_current: contextvars.ContextVar[str] = contextvars.ContextVar("codex_battlefield_request_id", default="")


def set_request_id(value: str) -> contextvars.Token:
    """给**当前逻辑请求**设置 id。返回 token，调用方可以用它恢复上一个值。"""

    return _current.set((value or "").strip())


def reset_request_id(token: contextvars.Token) -> None:
    try:
        _current.reset(token)
    except ValueError:
        # 跨上下文 reset 会抛 ValueError（token 属于另一个 Context）。
        # 这里不静默吞掉语义：直接清空当前上下文的值。
        _current.set("")


def current_request_id() -> str:
    """当前请求的 id：先看 contextvar（HTTP 请求里），再看环境变量（子进程里）。

    两处都没有时返回 **空字符串**（不是编一个假的）。调用方据此决定是写 `NULL` 还是不写。
    """

    value = _current.get()
    if value:
        return value
    return os.environ.get(ENV_NAME, "").strip()


def request_id_or_none() -> str | None:
    """给数据库列用的形态：没有就是 `None`（**不是**空串、也不是 0）。"""

    return current_request_id() or None
