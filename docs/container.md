# 容器化（P6-1）

实现：`Dockerfile`（多阶段）、`docker-compose.yml`、`.dockerignore`。
判据：`api/tests/test_container.py`（21 例）。

```bash
docker compose up --build
# → http://127.0.0.1:8765
```

## 1. 一个服务就够

API **自己托管前端产物**（P1-3），所以不需要单独的 Web 容器，也不需要反向代理。
少一个容器 = 少一处会坏的地方。

镜像分两个阶段：

```
node:20-bookworm-slim  ── pnpm build ──▶  web/dist
                                            │ COPY --from
python:3.12-slim-bookworm ── pip install -e ──▶ /app  +  /app/web/dist
```

前端产物放 `/app/web/dist` 是**刻意的**：`app/main.py` 的 `_resolve_web_dist`
探的是 `ROOT_DIR / "web" / "dist"`，而 `ROOT_DIR` 是 `/app`（从 `api/app/config.py` 往上两级）。
落点不对的话 API 会**静默不托管**前端 —— 报错只在启动日志的一行提示里。

## 2. 三条刻意的取舍

### 2.1 状态根走卷，不烘进镜像

状态（库、运行记录、工作树、快照）属于**使用者**，不属于镜像 —— 重建镜像不该丢任务树。

```yaml
volumes:
  - battlefield-state:/state
```

容器内 `CODEX_BATTLEFIELD_STATE_HOME=/state`，与挂载点**必须一致**（不一致时状态会悄悄落到容器里，
而界面看起来一切正常，直到重建容器才发现任务树没了）。

### 2.2 非 root 运行

容器里跑 agent 会读写**挂进来的宿主机目录**。以 root 跑等于把那些目录也一起交给 root。

`/state` 要先 `chown` 给运行用户：卷挂载时 Docker 会把镜像里该路径的**属主**带过去，
不 chown 的话挂载出来的卷是 root 属主 → 一写状态就 EACCES，而报错在启动几秒后才出现。

### 2.3 端口只绑 `127.0.0.1`

```yaml
ports:
  - '127.0.0.1:8765:8765'
```

**这个 API 没有鉴权。** 写成 `8765:8765` 会把它暴露到所有网卡上，
而那次改动看起来只是"让外网也能访问"。

容器**内部**仍然绑 `0.0.0.0`（否则端口映射拿不到连接）—— 安全性由 compose 那一行负责，
监听地址在容器里管不了宿主机谁能访问。两条一起才成立。

## 3. 沙箱在容器内的差异（ROADMAP 要求写清的部分）

### 3.1 沙箱档位

`codex_sandbox_{constitution,planner,worker,verifier,acceptance,steward}` 有三个档：
`read-only` / `workspace-write` / `danger-full-access`。

**容器里这几档的含义会变**：

| 档位 | 裸机上 | 容器里 |
|---|---|---|
| `read-only` | 只读挂载 | 只读挂载（**相对容器**） |
| `workspace-write` | 可写工作目录 | 可写容器内路径，**包括卷与 bind mount** |
| `danger-full-access` | 去掉限制 | 去掉限制，但**仍然受容器的 namespace 与卷范围限制** |

关键一句：**容器提供的隔离与沙箱提供的是两层不同的东西**。

* 沙箱（codex 自己的）管"这个 agent 能不能改文件"；
* 容器管"这个进程能看到宿主机的哪些东西"。

容器**不会**让 `danger-full-access` 变得安全：挂进来的工程目录照样能被改、被删。
反过来说，容器**确实**能兜住沙箱挡不住的事（比如 agent 去读宿主机别处的文件）。

### 3.2 凭据

镜像里**没有任何凭据**，也**不带 Codex CLI**。它的安装方式与凭据都是使用者自己的事。
要在容器里跑真实 agent，有两处必须自己准备：

1. **CLI**：另建一层装它，或把宿主机的可执行文件挂进来；
2. **凭据**：`CODEX_BATTLEFIELD_CODEX_HOME` 指向的那个隔离 `CODEX_HOME`
   （里面有 `auth.json` / `config.toml`）要挂进来。

**不要把凭据烘进镜像**：镜像层是可被 `docker history` 翻出来的，
而且镜像会被推送、被缓存、被共享。

### 3.3 卷挂载与 `git worktree`

节点隔离用 `git worktree`，所以：

* **镜像里装了 `git`**（这是必需项，不是可选项）；
* **工程目录必须挂进来**，否则容器里没有可改的东西；
* **要挂一个真实 git 仓库**（有 `.git`）——`git worktree add` 需要它。
  挂一个普通目录进去时节点隔离会退化成拷贝目录（`worktree.fallback`），能跑但没有隔离。

还有一个容器特有的坑：**卷里的 `.git` 与宿主机的属主/权限要一致**。
Linux 上容器内 UID 10001 与宿主机用户的 UID 不同，`git` 会报
`detected dubious ownership`。两种解法：把宿主机的 UID 传进来构建
（`--build-arg`），或者给该目录加 `safe.directory`。

## 4. 已知限制（如实写下）

* **我没有真跑过 `docker compose up`**。本机没有 Docker。所以"构建能成功""界面可用"
  这两句**没有证据**；有证据的是：结构与取舍都核过了（21 条判据），
  引用的仓库路径都存在，Python/Node 版本与 CI 和 `pyproject` 对齐。
  这与 P6-3（CI）是同一套做法：**把能验证的验证到位，把不能验证的如实标注**。
* **`Dockerfile` 里没有针对 Linux 的实测**。它引用的命令（`apt-get`、`useradd`、
  `corepack`）都是标准 Debian 上的东西，但没有在真机上跑过。
* **没有多架构**（`--platform`）。ARM 上要跑得自己加 `buildx`。
* **没有镜像体积优化**。`slim` + 清理 apt 缓存是基础动作，但没有做依赖裁剪
  （例如把 `pytest` 之类的 dev 依赖排除出运行镜像 —— 现在 `pip install -e /app/api`
  装的是运行依赖，dev 依赖在 `[project.optional-dependencies]` 里，**没有**被装进来）。
* **compose 里没有配资源上限**（`mem_limit` / `cpus`）。机器安全守卫因此被显式关掉了
  （`MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB=0`）—— 容器里读到的是**宿主机**的内存，
  守卫会误判。要保留守卫就得给容器设限额并把这个阈值调到相符的值。

## 不需要 Docker 也能验的那一半（D1–D8）

`api/tests/test_container.py` 核的是**结构与取舍**（非 root、卷、端口、版本对齐、
被引用的文件存在）。本轮补了 `api/tests/test_container_static.py`：核**语法与自洽**，
这些在语法层面就能验，而且**能红**。

为什么值得单独有一组：Dockerfile 里拼错的指令、`RUN` 里写了 `sudo`、缓存层顺序颠倒、
三处端口各写一个 —— 这些在真的 `docker build` 之前**完全静默**，而本机没有 Docker。

| 判据 | 查什么 |
|---|---|
| D1 | 每条指令都是已知的 Docker 关键字（拼错的指令不会在别处报错）；首条是 `FROM`；两个阶段齐备 |
| D2 | 全文件没有 `sudo`；`USER` 出现在装包/建用户**之后**（否则那个 `USER` 是装饰） |
| D3 | **源码 COPY 排在所有 install 之后**、依赖清单先于源码（否则缓存层白设） |
| D4 | 基础镜像都带 tag 且不是 `latest` |
| D5 | `WORKDIR` 在 `CMD` 之前；`CMD` 里的 `模块:app` 与 `pip install -e /app/api`、`api/app/main.py` 三处对得上 |
| D6 | `HEALTHCHECK` 打 `/api/health`；`HEALTHCHECK` / `CMD` / `EXPOSE` 三处端口一致 |
| D7 | compose 把卷挂到镜像里那个状态根上；端口只发布在 `127.0.0.1` |
| D8 | 解析器自身可信（折行折叠、`CMD` 的 JSON 形式可解析） |

五组变异全部能红：拼错指令名、`RUN` 里加 `sudo`、把源码 `COPY` 提到 `pip install` 之前、
健康检查换端口、compose 端口发布到所有网卡。

### 判据自己写错的三次（都记在这里，因为它们比判据本身更有价值）

1. **续行之间的整行注释**被当成新指令 —— `ENV A=1 \` 后面那行注释一插，
   后面的 `B=2` 就变成"未知指令"；
2. **反引号**（文档里写的 `` `/state` ``）被当成值的一部分，`/state` 与 `` `/state` `` 不等；
3. **"源码 COPY"认错了**：第一版找 `"pip install -e /app/api" in text`，结果先命中
   `COPY api/pyproject.toml` —— 判据变成"pip install 要在清单拷贝之前"，当然红。
   正确的不变量是"**套件源码目录的那个 COPY 在所有 install 之后**"，
   而且要按"源以 `/` 结尾"来区分目录拷贝与清单拷贝。
