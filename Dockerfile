# Codex 任务战场 —— 容器镜像（P6-1）
#
# ## 两个阶段，因为两套工具链
#
# 前端要 Node + pnpm，后端要 Python + venv，而且**前端产物是后端的输入**
# （API 会托管 `web/dist`，见 `app/main.py` 的 `_resolve_web_dist`）。
# 所以先建前端，再把产物搬进运行阶段。
#
# ## 三条刻意的取舍
#
# 1. **状态根走卷，不烘进镜像**。工具的状态（库、运行记录、工作树、快照）
#    属于**使用者**，不属于镜像 —— 重建镜像不该丢任务树。所以
#    `CODEX_BATTLEFIELD_STATE_HOME=/state` 配上 compose 里的命名卷。
# 2. **非 root 运行**。容器里跑 agent 会读代码、写产物，以 root 跑等于把
#    宿主机上挂进来的目录也一起暴露给 root 权限。
# 3. **不带 Codex CLI**。它的安装方式与凭据都是使用者自己的事；
#    镜像里只放编排器。要在容器里跑真实 agent 就把 CLI 挂进来或另建一层 ——
#    这条差异写在 `docs/container.md` 里。
#
# ## 构建
#
#   docker build -t codex-battlefield .
#   docker compose up
#
# 判据在 `api/tests/test_container.py`：它核对**结构与取舍**（阶段、非 root、
# 卷、端口、版本、被引用的文件真的存在），核不了"构建能成功"——
# 那要在有 Docker 的机器上跑。

# ---------- 阶段 1：前端产物 ----------
FROM node:20-bookworm-slim AS web
WORKDIR /build
# 先只拷锁文件、package.json 与 `pnpm-workspace.yaml`：依赖没变时这一层能命中缓存，
# 而源码改动只让后面几步重跑。
#
# ⚠️ `pnpm-workspace.yaml` **必须**一起拷进来：pnpm 10 的"允许哪些依赖执行 build script"
# （`onlyBuiltDependencies`）就在这个文件里。少了它，`pnpm install` 会以
# `ERR_PNPM_IGNORED_BUILDS` 失败 —— 2026-10-18 第一次真构建时就是这么失败的
# （也就是说这条 `docker compose up` 从来没跑通过）。
COPY web/package.json web/pnpm-lock.yaml ./
# pnpm 10 要求"哪些依赖可以执行 build script"显式写在 `pnpm-workspace.yaml` 里，
# 不写就 `ERR_PNPM_IGNORED_BUILDS`。这份文件在镜像里**生成**而不是从仓库拷：
# 仓库里那份是历史遗留（键名写错、对本地 pnpm 又不坏），改它会影响本地依赖状态哈希 ——
# 2026-10-18 实测：改了它之后本地 `pnpm test` 反而因为重新评估 build script 而失败。
RUN printf 'onlyBuiltDependencies:\n  - esbuild\n' > pnpm-workspace.yaml
# `--frozen-lockfile` 在 CI 与镜像里都要：否则装出的是"当时最新的"而不是锁定的那套。
#
# ⚠️ **pnpm 版本要显式钉在 CI 的同一个大版本**（CI：`pnpm/action-setup@v4` + `version: 10`）。
# 只写 `corepack enable` 会拿到"当前最新"——2026-10-18 实测拿到的是 **pnpm 12.14.0**，
# 而 12 的依赖 build script 策略又变了：即使 `pnpm-workspace.yaml` 里写了
# `onlyBuiltDependencies: [esbuild]`，`pnpm install` 仍然以 `ERR_PNPM_IGNORED_BUILDS` 失败。
# 结果是"镜像里构建不了、CI 里跑得好"——正是那种只有真跑一次才会发现的漂移。
RUN corepack enable && corepack prepare pnpm@10 --activate && pnpm -v && pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ---------- 阶段 2：运行 ----------
FROM python:3.12-slim-bookworm AS runtime

# 与 `api/pyproject.toml` 的 `requires-python = ">=3.12,<3.14"` 对齐。
# 用 `slim` 而不是 `alpine`：这个工具会起子进程跑 git 与 agent，
# glibc 环境下的兼容性更可预期（alpine 的 musl 在 Python 扩展上是常见坑）。

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # 状态根在容器内固定为 `/state`，由卷挂载；
    # 不设它的话会落到容器内的 `~/.codex-battlefield`，重建容器就丢。
    CODEX_BATTLEFIELD_STATE_HOME=/state \
    # **必须**同时指定工作区：未绑定工程时 workspace_dir 会回退到老路径
    # <repo>/workspace（= /app/workspace，属主是 root）——2026-10-18 真跑时
    # 就是这里抛 PermissionError 让 API 起不来（/api/health 一直 503）。
    # 容器里没有“仓库边上的工作区”这个概念，让它落在卷里才对。
    CODEX_BATTLEFIELD_WORKSPACE=/state/workspace \
    # 容器里没有 Codex CLI，真实档必然失败 —— 默认 mock，让界面先跑起来。
    # 要用真实 runner 就显式覆盖这个环境变量并自己提供 CLI。
    CODEX_BATTLEFIELD_RUNNER=mock \
    # 容器内存有限，机器安全守卫的默认阈值（4096MB）在容器里会误触发休眠。
    # 设 0 = 关掉这条守卫；要保留就把这个值调到与容器限额相符。
    CODEX_BATTLEFIELD_MACHINE_SAFETY_MIN_AVAILABLE_MEMORY_MB=0

# git 是**必需**的：节点隔离用 `git worktree`，没有它任务跑不起来。
# 单独一行并在最后清理 apt 缓存，避免把索引层留在镜像里。
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# 非 root：先建用户与状态目录，并把它交给该用户。
# 卷挂载到 `/state` 时 Docker 会把镜像里该路径的属主带过去，所以这一步是必要的。
RUN useradd --create-home --uid 10001 battlefield \
 && mkdir -p /state \
 && chown -R battlefield:battlefield /state

WORKDIR /app

# 后端：**必须先把源码拷进来再装**（这条以前是先拷 pyproject 再装，装不起来）。
#
# 为什么不能只拷 `pyproject.toml`：镜像里用的是 `pip install -e`（editable），
# 而 editable 安装要在构建时解析包结构 —— 没有 `app/` 目录时 setuptools 直接报
#   `error: package directory 'app' does not exist`
# （2026-10-18 第一次真构建时就是这个错，说明"先拷清单再装"这套缓存优化从没被验证过）。
#
# 为什么必须是 editable：`app/config.py` 的 `ROOT_DIR` 是从源码位置往上两级算出来的，
# 非 editable 安装会让它落到 site-packages 里，`_resolve_web_dist` 就找不到 `/app/web/dist`。
COPY api/ /app/api/
RUN python -m pip install --upgrade pip \
 && python -m pip install -e /app/api

COPY scripts/ /app/scripts/
# 前端产物来自阶段 1。放在 `/app/web/dist`：`_resolve_web_dist` 会探
# `ROOT_DIR/web/dist`，而 `ROOT_DIR` 是 `/app`（`api/app/config.py` 往上两级）。
COPY --from=web /build/dist /app/web/dist

USER battlefield

EXPOSE 8765

# 健康检查用 `/api/health`（不需要鉴权、不碰库）：容器编排放它判"到底起来了没有"。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/api/health', timeout=4).status == 200 else 1)"

# 绑 `0.0.0.0` 是**容器里必须**的：默认只绑 127.0.0.1 的话端口映射拿不到连接。
# 安全性由 compose 的 `ports: 127.0.0.1:8765:8765` 负责（只在本机可达），
# 不靠监听地址 —— 监听地址在容器里管不了宿主机谁能访问。
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
