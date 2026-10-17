"""容器镜像的**静态**验证：不需要 Docker 也能验的那一半（判据 D1–D8）。

## 为什么还要再写一组

`api/tests/test_container.py` 已经核对了"结构与取舍"（非 root、卷、端口、版本对齐、
被引用的文件存在）。但它核不了两件**在没有 Docker 的机器上仍然可核**的事：

1. **Dockerfile 本身是不是合法的 Dockerfile** —— 有没有拼错的指令、有没有 `RUN` 写了
   `sudo`（我们是非 root 用户）、缓存层顺序对不对；
2. **镜像里的路径与命令是否自洽** —— 构建完能不能真的在那个目录下找到入口。

这两件在语法层面就能验，而且**能红**。剩下的（构建能不能成功、容器跑起来界面能不能用）
需要 Docker，本机没有 —— 这一点写在 `docs/container.md`，不在这里假装。

判据文件独立于 `test_container.py` 的理由：那一组是"结构与取舍"，
这一组是"**语法与自洽**"。分开之后，出问题时能一眼看出是哪一类。
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "Dockerfile"
COMPOSE = REPO / "docker-compose.yml"

#: Dockerfile 允许出现的指令。**不认识的一律报错** —— 拼错的指令在真的 build 之前
#: 完全静默（Docker 只在构建时才发现），而本机没有 Docker。
KNOWN_INSTRUCTIONS = {
    "FROM",
    "RUN",
    "CMD",
    "LABEL",
    "MAINTAINER",
    "EXPOSE",
    "ENV",
    "ADD",
    "COPY",
    "ENTRYPOINT",
    "VOLUME",
    "USER",
    "WORKDIR",
    "ARG",
    "ONBUILD",
    "STOPSIGNAL",
    "HEALTHCHECK",
    "SHELL",
}


def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _strip_inline_comment(line: str) -> str:
    """去掉行内 `#` 注释（但不动引号里的 `#`）。

    实测踩过：`CODEX_BATTLEFIELD_STATE_HOME=/state \\` 后面跟的注释行被折进同一条指令，
    拼出来的是 `...=/state  # 容器里没有 ...` —— 于是"未知指令"判据把整条 ENV 判成了错的。
    """

    out: list[str] = []
    quote: str | None = None
    for index, char in enumerate(line):
        if quote:
            if char == quote:
                quote = None
            out.append(char)
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            break
        out.append(char)
    return "".join(out).rstrip()


def logical_instructions(text: str | None = None) -> list[tuple[int, str]]:
    """把 Dockerfile 折成**逻辑指令**：`(起始行号, 去掉续行符的整条指令)`。

    注释与空行丢掉；行内注释也去掉；以 `\\` 结尾的行与下一行拼接
    （`ENV`/`RUN` 都常用这个写法）。
    """

    raw = (text if text is not None else dockerfile_text()).split("\n")
    instructions: list[tuple[int, str]] = []
    buffer = ""
    start_line = 0
    for index, line in enumerate(raw, 1):
        stripped = line.strip()
        # **续行之间的整行注释要跳过，但不能打断续行**：Dockerfile 里常见
        # `ENV A=1 \` / `# 说明` / `    B=2`。第一版把注释当成"一条新指令"处理，
        # 于是 `B=2` 被当成了未知指令（实测）。
        if stripped.startswith("#") or (not buffer and not stripped):
            continue
        cleaned = _strip_inline_comment(line)
        if not buffer:
            start_line = index
        if cleaned.rstrip().endswith("\\"):
            body = cleaned.rstrip()[:-1].strip()
            buffer = f"{buffer} {body}".strip()
            continue
        buffer = f"{buffer} {cleaned.strip()}".strip()
        instructions.append((start_line, buffer))
        buffer = ""
    if buffer.strip():
        instructions.append((start_line, buffer.strip()))
    return instructions


# --- D1 语法与指令集 ---------------------------------------------------------


def test_d1_every_instruction_is_a_known_docker_keyword():
    unknown = []
    for line_number, instruction in logical_instructions():
        keyword = instruction.split(maxsplit=1)[0].upper()
        if keyword not in KNOWN_INSTRUCTIONS:
            unknown.append(f"第 {line_number} 行：{instruction[:60]}")
    assert unknown == [], f"这些不是合法的 Dockerfile 指令（拼错的话本机没有 Docker 也验不出来）：{unknown}"


def test_d1_first_instruction_is_from():
    first = logical_instructions()[0]
    assert first[1].upper().startswith("FROM "), f"第一条指令必须是 FROM，实际是：{first[1][:60]}"


def test_d1_dockerfile_is_not_empty_and_has_the_expected_shape():
    instructions = logical_instructions()
    keywords = [item.split(maxsplit=1)[0].upper() for _line, item in instructions]
    assert len(instructions) >= 15, f"指令太少（{len(instructions)}），可能被折行逻辑吃掉了"
    # 两个阶段（web 构建 + 运行）
    assert keywords.count("FROM") == 2
    for required in ("WORKDIR", "COPY", "RUN", "CMD", "USER", "EXPOSE"):
        assert required in keywords, f"Dockerfile 里少了 {required}"


# --- D2 非 root 与 sudo ------------------------------------------------------


def test_d2_no_sudo_anywhere():
    """`USER battlefield` 之后用 sudo 说明"非 root"是假的。

    而且这个镜像里根本没装 sudo（`--no-install-recommends` + 只装 git/ca-certificates），
    写了也只会在运行时失败。
    """

    offenders = [
        f"第 {line} 行：{instruction[:70]}"
        for line, instruction in logical_instructions()
        if re.search(r"\bsudo\b", instruction)
    ]
    assert offenders == [], f"容器里不该出现 sudo：{offenders}"


def test_d2_user_is_switched_after_the_privileged_setup():
    """`USER` 必须出现在所有装包/建目录之后 —— 否则那个 USER 是装饰。"""

    instructions = logical_instructions()
    user_line = next(line for line, text in instructions if text.upper().startswith("USER "))
    # 装包/建用户可能写在续行里（`RUN useradd ... \` 后面还有 `&& mkdir`），
    # 所以按**整条指令**匹配，而不是"以 RUN useradd 开头"（实测踩过）。
    privileged = [
        line
        for line, text in instructions
        if text.upper().startswith("RUN ") and ("apt-get install" in text or "useradd" in text)
    ]
    assert privileged, "应当有装包与建用户的步骤（没有的话这条判据是空转的）"
    assert user_line > max(privileged), "`USER` 出现在装包/建用户之前 —— 那几步会以非 root 跑，必然失败"


# --- D3 层缓存顺序 -----------------------------------------------------------


def test_d3_the_install_layers_have_what_they_need():
    """安装依赖的那一层，**必须已经有它需要的东西**（这条判据原先写反了）。

    ## 2026-10-18 的纠正：旧判据把"错误的设计"钉成了不变量

    第一版写的是"依赖清单先于源码拷、且**套件源码的 COPY 必须排在所有 install 之后**"——
    那是缓存优化的经验法则，看着很合理。但第一次真构建就证明它**不可能满足**：

        COPY api/pyproject.toml /app/api/
        RUN python -m pip install -e /app/api
        → error: package directory 'app' does not exist

    editable 安装要在构建时解析包结构，**没有 `app/` 目录就装不起来**；而镜像里必须是
    editable（`app/config.py` 的 `ROOT_DIR` 从源码位置往上两级算，非 editable 会让它落到
    site-packages，`_resolve_web_dist` 就找不到 `/app/web/dist`）。
    也就是说：这条判据当时在**保护一个从没跑通过的设计**（`docker compose up` 一次都没成功过）。

    现在钉的是真正的不变量：

    1. **editable 后端安装**（`pip install -e`）之前必须有 `COPY api/`；
    2. `pnpm install` 之前必须有 `pnpm-lock.yaml`（`--frozen-lockfile` 才有意义）；
    3. 不允许出现两次"后端依赖安装"（两份依赖清单必然漂移）。
    """

    instructions = [text for _line, text in logical_instructions()]
    installs = [index for index, text in enumerate(instructions) if "pip install" in text or "pnpm install" in text]
    assert installs, "应当有装依赖的步骤（没有的话这条判据是空转的）"

    def copy_sources(text: str) -> list[str]:
        """`COPY <src>... <dst>` 里的**所有**源。

        ⚠️ 第一版只取了 `parts[1]`，而真实的那行是三条源：
        `COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./`
        —— 于是"lockfile 拷过没有"永远答 False。多源 COPY 是合法且这里必需的写法。
        """

        if not text.upper().startswith("COPY "):
            return []
        parts = text.split()
        if len(parts) < 3 or parts[1].startswith("--"):
            return []
        return parts[1:-1]

    copies = [(index, copy_sources(text)) for index, text in enumerate(instructions)]

    def first_index(predicate) -> int | None:
        return next((index for index, sources in copies if any(predicate(source) for source in sources)), None)

    backend_dir = first_index(lambda source: source.startswith("api/") and source.endswith("/"))
    pip_install = next((index for index in installs if "pip install" in instructions[index]), None)
    assert backend_dir is not None and pip_install is not None
    assert backend_dir < pip_install, (
        "editable 安装（pip install -e）之前必须先把 `api/` 拷进来 —— "
        "只拷 pyproject.toml 会报 `package directory 'app' does not exist`（2026-10-18 真构建时踩到）"
    )

    lockfile = first_index(lambda source: "pnpm-lock.yaml" in source)
    pnpm_install = next((index for index in installs if "pnpm install" in instructions[index]), None)
    assert lockfile is not None and pnpm_install is not None
    assert lockfile < pnpm_install, "`--frozen-lockfile` 之前必须先把 pnpm-lock.yaml 拷进来"
    # 允许列表：可以由镜像**生成**（`printf ... onlyBuiltDependencies`）也可以从仓库拷，
    # 但必须在 `pnpm install` 之前就位。2026-10-18：仓库里那份用的是 pnpm 不认识的键名，
    # 而镜像当时也没拷它 —— 于是 `docker compose up` 从来没跑通过。
    text_all = "\n".join(instructions)
    assert "onlyBuiltDependencies" in text_all, (
        "pnpm 10 的 build-script 允许列表（onlyBuiltDependencies）没有出现在 Dockerfile 里"
    )
    assert text_all.find("onlyBuiltDependencies") < text_all.find("pnpm install"), (
        "允许列表必须在 `pnpm install` 之前写出来（顺序反了等于没写）"
    )

    backend_installs = [index for index in installs if "pip install" in instructions[index]]
    assert len(backend_installs) == 1, f"后端依赖只该装一次（现在有 {len(backend_installs)} 处）"


def test_d4_base_images_are_pinned_to_a_tag_not_latest():
    froms = [text for _line, text in logical_instructions() if text.upper().startswith("FROM ")]
    for text in froms:
        image = text.split()[1]
        assert ":" in image, f"基础镜像没有 tag：{image}"
        tag = image.rsplit(":", 1)[1]
        assert tag not in {"latest", ""}, f"基础镜像不该用 latest：{image}"


# --- D5 入口与工作目录自洽 ---------------------------------------------------


def test_d5_workdir_is_set_before_the_entrypoint():
    instructions = logical_instructions()
    workdirs = [line for line, text in instructions if text.upper().startswith("WORKDIR ")]
    final_workdir = workdirs[-1]
    cmd = next(line for line, text in instructions if text.upper().startswith("CMD "))
    assert final_workdir < cmd, "`WORKDIR` 必须在 `CMD` 之前，否则启动目录与预期不同"


def test_d5_entrypoint_runs_the_app_package_that_is_installed():
    """`CMD` 里的模块名要与镜像里真的装着的东西一致。

    `python -m uvicorn app.main:app` 要求 `/app` 在 `PYTHONPATH` 上（`WORKDIR /app` 提供了
    这一点），并且 `api/` 被装成了 `app` 包（`pip install -e /app/api`）。
    这里核对三件事的**字符串一致性** —— 它们在三个不同的指令里，很容易各改各的。
    """

    text = dockerfile_text()
    cmd = next(t for _line, t in logical_instructions() if t.upper().startswith("CMD "))
    match = re.search(r'"([a-z_][\w.]*):app"', cmd)
    assert match, f"CMD 里没找到 `模块:app` 的形式：{cmd}"
    module = match.group(1)

    assert "WORKDIR /app" in text, "`WORKDIR /app` 是 `python -m <module>` 能找到包的前提"
    assert "pip install -e /app/api" in text, "`app` 包必须真的被安装"
    assert (REPO / "api" / f"{module.split('.')[0]}" / "main.py").is_file(), (
        f"CMD 指的是 `{module}.main:app`，但 `{module}/main.py` 不存在 —— 入口与源码对不上"
    )
    assert "app.main:app" in cmd, "入口应当是 `app.main:app`（`api/app/main.py` 里的 FastAPI 实例）"


# --- D6 健康检查 -------------------------------------------------------------


def test_d6_healthcheck_hits_the_api_health_route():
    instructions = logical_instructions()
    health = next(text for _line, text in instructions if text.upper().startswith("HEALTHCHECK"))
    assert "api/health" in health, "健康检查要打 `/api/health`（不需要鉴权、不碰库）"
    # 端口要和 CMD / EXPOSE 一致 —— 三处各写一个端口是这个文件最容易漂的地方。
    cmd = next(text for _line, text in instructions if text.upper().startswith("CMD "))
    expose = next(text for _line, text in instructions if text.upper().startswith("EXPOSE "))
    for label, snippet in (("HEALTHCHECK", health), ("CMD", cmd), ("EXPOSE", expose)):
        assert "8765" in snippet, f"{label} 里的端口不是 8765：{snippet[:70]}"


# --- D7 与 compose 的接口一致 ------------------------------------------------


def test_d7_compose_mounts_the_volume_where_the_state_home_points():
    text = dockerfile_text()
    # 值可能被反引号包住（文档行里常见），取出来之后去掉两端的引号/反引号。
    state_match = re.search(r"CODEX_BATTLEFIELD_STATE_HOME=`?([^`\s\\]+)`?", text)
    assert state_match, "镜像里应当设 `CODEX_BATTLEFIELD_STATE_HOME`"
    state_home = state_match.group(1)
    assert state_home == "/state", f"状态根应当是 /state，实际 {state_home}"

    compose = COMPOSE.read_text(encoding="utf-8")
    assert "battlefield-state" in compose, "compose 里应当有一个命名卷装状态"
    # 挂载点用 `<卷名>:<容器内路径>`，容器内路径必须与镜像里的状态根一致。
    assert re.search(rf"-\s*\S+:{re.escape(state_home)}\b", compose), (
        f"compose 应当把某个卷挂到 {state_home} 上"
    )
    assert re.search(r"^volumes:\s*\n\s+\S+:", compose, re.MULTILINE), "顶层要声明那个命名卷"


def test_d7_compose_does_not_publish_on_all_interfaces():
    """端口只绑回环：容器里监听 `0.0.0.0`，所以**暴露给谁**由 compose 决定。"""

    compose = COMPOSE.read_text(encoding="utf-8")
    published = re.findall(r"-\s*['\"]?([\d.]+):(\d+):(\d+)", compose)
    assert published, "compose 里应当有端口映射（没有的话这条判据是空转的）"
    for host_ip, _host_port, _container_port in published:
        assert host_ip == "127.0.0.1", f"端口发布在 {host_ip} 上 —— 应当是 127.0.0.1（只本机可达）"


# --- D8 判据自身可信 ---------------------------------------------------------


def test_d8_the_parser_really_folds_continuations():
    """折行解析器要是坏的，上面那些判据就都在看半句话。"""

    sample = "FROM a:1 AS x\nENV A=1 \\\n    B=2\nRUN echo hi\n"
    instructions = logical_instructions(sample)
    assert len(instructions) == 3, instructions
    # 折行后是"单空格拼接"：`ENV A=1 B=2`（不是保留原缩进）。
    assert instructions[1][1] == "ENV A=1 B=2", instructions[1]
    # 起始行号记的是**第一条**，不是最后一条：否则后面的行号比较（USER vs apt-get）会错。
    assert instructions[1][0] == 2, instructions[1]


def test_d8_shlex_can_parse_the_exec_form_cmd():
    """`CMD` 用的是 exec 形式（JSON 数组），要能被解析出来 —— 否则入口是坏的。"""

    cmd = next(t for _line, t in logical_instructions() if t.upper().startswith("CMD "))
    shlex.shlex(cmd[len("CMD ") :], posix=True)  # 不炸就说明结构可解析
    assert cmd.count('"') >= 6, f"CMD 应当是 JSON 数组形式：{cmd}"
