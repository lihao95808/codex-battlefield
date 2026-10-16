"""容器化的判据（P6-1）。

## 这一层能验证什么、不能验证什么

**不能**验证"构建能成功""`docker compose up` 后界面可用" —— 本机没有 Docker。
所以判据不去断言那些，而是核对**结构与取舍**，每一条都对应一种"看起来能跑、
其实不能用（或不安全）"的具体形态：

| 编号 | 要钉住的事 | 不钉住会怎样 |
|---|---|---|
| K1 | 端口**只绑 127.0.0.1** | 这个 API 没有鉴权，绑 0.0.0.0 = 放到局域网上给所有人用 |
| K2 | 状态根走**卷**，不烘进镜像 | 重建镜像丢掉整棵任务树 |
| K3 | **非 root** 运行，且状态目录属主正确 | 容器里跑 agent 会读写挂进来的宿主机目录 |
| K4 | 装了 **git** | 节点隔离用 `git worktree`，没有它任务根本跑不起来 |
| K5 | 前端产物**在镜像里构建**（多阶段） | 搬宿主机的 `dist`，而它可能与源码不同步 |
| K6 | Python 版本与 `requires-python` 对齐 | 镜像里跑的是一个项目不支持的版本 |
| K7 | 有**健康检查**，且打的是不碰库的端点 | 编排分不清"起来了"与"还在启动" |
| K8 | `.dockerignore` 排除 `.venv` / `node_modules` / 状态文件 | 把宿主机路径的软链接与状态拷进镜像 |
| K9 | Dockerfile 引用的每个仓库路径**真的存在** | 构建在一个拼错的 COPY 上失败 |
| K10 | 安装命令与本地**同一条**（`pip install -e`） | 两份依赖声明会漂移 |
"""

from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def service(compose: dict) -> dict:
    services = compose.get("services") or {}
    assert services, "compose 里没有 service"
    name = next(iter(services))
    return services[name]


# ---------- K1 端口只绑本机 ----------


def test_k1_the_port_is_bound_to_loopback_only(service: dict) -> None:
    """**这条是安全边界**，不是风格问题。

    `8765:8765` 会把服务暴露到所有网卡上，而这个 API **没有鉴权**。
    改成那样看起来只是"让外网也能访问"。
    """
    ports = [str(item) for item in service.get("ports") or []]
    assert ports, "没有 ports 映射 —— 界面访问不到"
    for entry in ports:
        assert entry.startswith("127.0.0.1:"), (
            f"端口映射 {entry!r} 没有绑到 127.0.0.1 —— 这个 API 没有鉴权"
        )


def test_k1_the_container_itself_listens_on_all_interfaces(dockerfile: str) -> None:
    """容器内必须绑 `0.0.0.0`，否则端口映射拿不到连接。

    安全性由 compose 的 `127.0.0.1:` 负责 —— 监听地址在容器里管不了
    宿主机谁能访问。两条一起才成立。
    """
    assert "--host" in dockerfile
    assert "0.0.0.0" in dockerfile


# ---------- K2 状态根走卷 ----------


def test_k2_the_state_home_is_a_named_volume(service: dict, compose: dict) -> None:
    """工具的状态属于**使用者**，不属于镜像 —— 重建镜像不该丢任务树。"""
    volumes = [str(item) for item in service.get("volumes") or []]
    assert any("/state" in entry for entry in volumes), (
        "没有把状态根挂出来 —— 重建容器会丢掉整棵任务树"
    )
    named = [entry.split(":")[0] for entry in volumes if ":" in entry]
    declared = set((compose.get("volumes") or {}).keys())
    for source in named:
        if not source.startswith(("/", ".", "~")):
            assert source in declared, f"{source} 用了命名卷但没有在顶层 volumes 里声明"


def test_k2_the_state_home_env_points_at_the_mount(dockerfile: str, service: dict) -> None:
    """环境变量的位置必须与卷挂载点**一致** —— 不一致时状态悄悄落到容器里。"""
    assert "CODEX_BATTLEFIELD_STATE_HOME=/state" in dockerfile
    volumes = [str(item) for item in service.get("volumes") or []]
    assert any(entry.endswith(":/state") for entry in volumes), volumes


# ---------- K3 非 root ----------


def test_k3_it_runs_as_a_non_root_user(dockerfile: str) -> None:
    assert re.search(r"^USER\s+(?!root)\S+", dockerfile, re.MULTILINE), (
        "没有切到非 root 用户 —— 容器里跑 agent 会以 root 读写挂进来的宿主机目录"
    )


def test_k3_the_state_directory_is_chowned_before_the_volume_mount(dockerfile: str) -> None:
    """卷挂载时 Docker 会把镜像里该路径的**属主**带过去。

    不 chown 的话挂载出来的卷是 root 属主，而进程是非 root → 一写状态就 EACCES，
    而报错在启动几秒后才出现。
    """
    assert re.search(r"chown[^\n]*/state", dockerfile), "没有把 /state 的属主交给运行用户"


def test_k3_the_user_is_created_with_an_explicit_uid(dockerfile: str) -> None:
    assert re.search(r"useradd[^\n]*--uid\s+\d+", dockerfile), "没有显式 UID（bind mount 时属主对不上）"


# ---------- K4 git ----------


def test_k4_git_is_installed(dockerfile: str) -> None:
    """节点隔离用 `git worktree` —— 没有 git 的任务树跑不起来。"""
    assert re.search(r"apt-get install[^\n]*\bgit\b", dockerfile), "镜像里没有 git"


def test_k4_the_apt_layer_is_cleaned(dockerfile: str) -> None:
    assert "rm -rf /var/lib/apt/lists/*" in dockerfile, "apt 缓存留在镜像层里"


# ---------- K5 多阶段、前端在镜像里构建 ----------


def test_k5_the_web_is_built_inside_the_image(dockerfile: str) -> None:
    assert "pnpm build" in dockerfile, "没有在镜像里构建前端"
    assert "COPY --from=" in dockerfile, "没有从前端阶段搬产物 —— 多阶段形同虚设"
    assert re.search(r"--frozen-lockfile", dockerfile), (
        "没有用 --frozen-lockfile：装出的是'当时最新的'而不是锁定的那套"
    )


def test_k5_the_build_lands_where_the_api_looks_for_it(dockerfile: str) -> None:
    """`_resolve_web_dist` 探的是 `ROOT_DIR/web/dist`，而 `ROOT_DIR` 是 `/app`。"""
    assert re.search(r"COPY --from=\S+\s+\S+\s+/app/web/dist", dockerfile), (
        "前端产物的落点与 `_resolve_web_dist` 探测的路径不一致 —— API 会找不到它并静默不托管"
    )


# ---------- K6 版本对齐 ----------


def test_k6_the_python_version_matches_the_project_requirement(dockerfile: str) -> None:
    requirement = re.search(
        r'requires-python\s*=\s*"([^"]+)"', (ROOT / "api" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert requirement
    assert "3.12" in requirement.group(1)

    base = re.search(r"^FROM\s+python:([0-9.]+)", dockerfile, re.MULTILINE)
    assert base, "找不到 python 基础镜像"
    assert base.group(1).startswith("3.12"), f"镜像用的是 python:{base.group(1)}"


def test_k6_the_node_version_matches_ci(dockerfile: str) -> None:
    """与 P6-3 的 CI 用同一个 Node 主版本 —— 两处不同会让"本地/CI/镜像"三种结果。"""
    workflow = (ROOT / ".github" / "workflows" / "verify.yml").read_text(encoding="utf-8")
    ci_node = re.search(r"node-version:\s*'?(\d+)", workflow)
    image_node = re.search(r"^FROM\s+node:(\d+)", dockerfile, re.MULTILINE)
    assert ci_node and image_node
    assert ci_node.group(1) == image_node.group(1), (
        f"CI 用 Node {ci_node.group(1)} 而镜像用 Node {image_node.group(1)}"
    )


# ---------- K7 健康检查 ----------


def test_k7_there_is_a_healthcheck_that_does_not_touch_the_database(dockerfile: str, service: dict) -> None:
    assert "HEALTHCHECK" in dockerfile
    assert service.get("healthcheck"), "compose 里没有 healthcheck"
    text = dockerfile + str(service.get("healthcheck"))
    assert "/api/health" in text, "健康检查打的不是 /api/health"


# ---------- K8 .dockerignore ----------


def test_k8_venv_and_node_modules_are_excluded() -> None:
    """**必须排除 `.venv`**：里面是宿主机路径的软链接，拷进容器会让
    `pip install -e` 装出一份指向不存在路径的包，而报错在很远的地方。"""
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    for pattern in ("api/.venv", "node_modules", "web/dist", "*.sqlite3"):
        assert pattern in text, f".dockerignore 没有排除 {pattern}"


def test_k8_the_dockerignore_is_not_a_copy_of_gitignore() -> None:
    """两份文件的目的不同：`.gitignore` 管"什么不该进版本库"，
    `.dockerignore` 管"什么不该进镜像"。直接抄一份会漏掉镜像特有的东西
    （`docs/`、`bench/reports/`）。"""
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    assert "docs" in text, "文档不该进镜像"
    assert "bench/reports" in text or "bench/runs" in text


# ---------- K9 引用的路径真的存在 ----------


def test_k9_every_copied_repo_path_exists(dockerfile: str) -> None:
    """COPY 的源都在**构建上下文**里 —— 拼错一个路径会在构建时失败。"""
    missing: list[str] = []
    for line in dockerfile.split("\n"):
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        if "--from=" in stripped:
            continue  # 阶段间拷贝，不是仓库路径
        parts = stripped.split()[1:]
        for source in parts[:-1]:
            if not (ROOT / source).exists():
                missing.append(source)
    assert missing == [], f"Dockerfile 引用了不存在的路径：{missing}"


# ---------- K10 安装命令与本地一致 ----------


def test_k10_it_installs_with_the_same_command_as_ci(dockerfile: str) -> None:
    """两份依赖声明会漂移，而漂移的表现是"镜像里版本与本地不同"。"""
    assert re.search(r"pip install\s+-e\s+/app/api", dockerfile), (
        "镜像没有用 `pip install -e`（与 CI 同一条路径）"
    )
    # `uvicorn[standard]` 已经在 pyproject 的 dependencies 里 —— 重复声明会漂移。
    assert "pip install \"uvicorn" not in dockerfile, "又单独装了一遍 uvicorn（两份清单会漂移）"


def test_k10_the_api_deps_include_uvicorn() -> None:
    """上一条的前提：pyproject 里真的声明了 uvicorn。"""
    pyproject = (ROOT / "api" / "pyproject.toml").read_text(encoding="utf-8")
    assert "uvicorn[standard]" in pyproject


# ---------- 判据本身可信 ----------


def test_the_container_files_exist() -> None:
    assert DOCKERFILE.is_file()
    assert COMPOSE.is_file()
    assert DOCKERIGNORE.is_file()


def test_the_docs_explain_the_sandbox_difference() -> None:
    """ROADMAP 要求"文档写清沙箱在容器内的差异" —— 那是交付物的一部分。"""
    doc = ROOT / "docs" / "container.md"
    assert doc.is_file(), "没有 docs/container.md"
    text = doc.read_text(encoding="utf-8")
    for topic in ("沙箱", "凭据", "卷"):
        assert topic in text, f"容器文档里没写清 {topic} 的差异"
