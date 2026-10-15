# 状态与工程的彻底解耦 SPEC（R6）

状态：设计定稿，待实现
前置：`docs/spec-task-isolation.md`（每节点 `CODEX_HOME`）、`docs/provider-setup.md`（隔离 home 的 provider seed）
续跑锚点：`PLAN-STATUS.md`

## 0. 一句话

**把「工具的状态」和「被改的工程」拆成两个正交的轴**：状态固定在有身份的位置，工程可以随时挂载/切换；一个工程一份状态，互不污染。

## 1. 问题：一个 `workspace_dir` 干了三件事

现状 `api/app/config.py:9`：

```python
ROOT_DIR = Path(__file__).resolve().parents[2]
WORKSPACE_DIR = ROOT_DIR / "workspace"
```

这一个值同时是：

| 角色 | 实例 | 应该属于谁 |
|---|---|---|
| **A 工具全局状态** | 预算闸门、codex 槽位预留、守护进程状态、看门狗日志、helper 脚本 | 机器（跨工程共享） |
| **B 每工程状态** | sqlite 树、`runs/`、`state_snapshots/`、`checkpoint_ledger/`、`full_checkpoints/` | **一个工程** |
| **C 工程材料 / agent 的 cwd** | `worktrees/`、一次性 `projects/` | 一个工程 + 一个目标仓库 |

实测规模：`api/app/*.py` 里 **80 处**引用（config 6、orchestrator 40、runner 21、prompts 11、main 3、database 2、watchdog 4、recovery 1、runtime_logs 1），`scripts/` 里 **12 个脚本**再各自硬编码 `ROOT / "workspace"`，另有 `battlefield_watchdog.ps1`（1700+ 行）。

后果（都是"读起来像正常但其实是错的"）：

- 仓库换位置 / 删掉重克隆 → 状态全丢；
- 换一个目标工程 → 状态混在同一个库里，"这棵树属于哪个工程"只能靠 `constitutions.target_path` 猜；
- 多个 checkout 想共用一套工具状态 → 做不到（每份 checkout 各自一份 `workspace/`）。

## 2. 两个结构性障碍（不是改路径能过的）

### 2.1 DB 引擎在 import 期就被烧死了 URL

```python
# api/app/database.py:15-17
settings = get_settings()
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, ...)
```

`settings.database_url` 默认值是**在建 `Settings` 实例时**算出来的常量字符串（`config.py:21-24`），所以路径一旦确定就完全无法跟随"当前工程"变化。而且 `SessionLocal` 被 import 了 **522 处**（orchestrator 110、测试约 60 个文件、`main.py`、`budget.py`）。测试还依赖这个接缝：`tests/test_constitution_versions.py:422` 用 `monkeypatch.setattr(database, "engine", legacy_engine)`。

**这是一个承重的接缝，动它必须保形。**

### 2.2 `schema_dir` 是从 `workspace_dir.parent` 推出来的

```python
# api/app/runner.py:824
self.schema_dir = settings.workspace_dir.parent / "schemas"
```

也就是说「`workspace` 是仓库的子目录」这个**布局本身是代码依赖**。workspace 一搬家，codex 就找不到 `schemas/`；而 codex 找不到 output schema 的表现是**秒失败 + 0 token**（这个坑已经写在 `scripts/bench_run.py:139-140`）。

### 2.3 附带发现：`machine_safety.json` 等状态文件没有归属

`main.py:109` 的 `machine_safety.json`、`orchestrator.py:6857` 的 `l4_guardian_state.json`、`orchestrator.py:202` 的槽位预留——按名字是"机器级"，但按位置在 `workspace/` 里。分类时必须逐个定归属，不能一刀切。

## 3. 设计目标

| 编号 | 目标 | 怎么算做到 |
|---|---|---|
| G1 | 状态与仓库解耦 | 把仓库整个删掉重克隆，只要状态根还在，树与运行记录原样可用 |
| G2 | 工程可挂载/切换 | 一条命令切到另一个工程目录，状态自动跟着换到属于它的那份 |
| G3 | 工程级隔离 | A 工程的树、运行记录、快照**不出现在** B 工程 |
| G4 | 工具全局状态跨工程共享 | 预算闸门/槽位在机器层面唯一，多工程不会各算一套 |
| G5 | 迁移不丢数据 | 老 `workspace/` 里的树能被迁进新布局，且有据可查 |
| G6 | 可诊断 | 任何时刻能回答"我现在在哪个工程、用哪个库、状态根在哪" |

**非目标**（明确不做，避免范围蔓延）：

- 不做"一个进程内同时服务多个工程"（见 §6 决策 D1）；
- 不做跨机器状态同步；
- 不改 codex 沙箱档相关的既有行为（见 `docs/spec-task-isolation.md` 顶部警示）。

## 4. 目标布局

```
<用户主目录>/.codex-battlefield/          # 状态根（可用环境变量覆盖）
├── active.json                           # 指向当前工程 key
├── registry.json                         # 已挂载工程清单
├── machine_safety.json                   # 机器级：A
├── l4_guardian_state.json                # 机器级：A
├── codex_slot_reservations.json(.lock)   # 机器级：A（并发预算是机器的）
├── budget_guard.json                     # 机器级：A
├── watchdog.log
├── tools/                                # helper 脚本（从模板生成，可共享）
└── projects/
    └── <project-key>/                    # 每个工程一份
        ├── project.json                  # {root: "D:\\work\\g10", created_at, ...}
        ├── battlefield.sqlite3(+wal/shm) # 该工程自己的树
        ├── runs/                         # 该工程的运行记录
        ├── worktrees/                    # 该工程的节点工作树（须与目标仓库同盘）
        ├── state_snapshots/
        ├── checkpoint_ledger/
        └── full_checkpoints/
```

### 4.1 project-key 的编码与碰撞

要求：**确定性、可读、文件系统安全、可逆**。

```
key = lower( <首段> + "--" + <其余段 slug 化> + "--" + sha1(lower(规范化路径))[:8] )
例：D:\work\g10        → d--work-g10--dc4eb916
    D:\work\g10\art_res → d--work-g10-art_res--342be65c
    \\server\share\proj\x → unc--server-share-proj-x--2a563632
    D:\工作\国风UI      → d--%e5b7a5%e4bd9c-%e59bbd%e9a38eui--3e5ea68b
```

- slug 化：ASCII 字母数字与 `._-` 原样保留，其余逐字符转 `%XX`（UTF-8 字节小写十六进制）；
- 安全字符集：`[A-Za-z0-9._%-]`——**`%` 必须允许**，因为 slug 化故意产出 `%XX`（它在 Windows 文件名里合法）；
- **末尾 8 位哈希是防碰撞的唯一保证**：不同路径 slug 化后可能相近（`a b` 与 `a-b`），哈希保证不同路径必得不同 key；
- `project.json` 里存**原始绝对路径**作为真相，key 只是目录名——key 算错了不影响正确性，只是不好看。

> **编码器已有实装 + 正式测试**：`api/app/state_layout.py`（`project_key()`），
> 判据在 `api/tests/test_state_layout.py`（确定性 / 文件系统安全 / 大小写折叠 / 防碰撞 / 尾部斜杠）。
> 落地过程中曾用一份独立参考实现 `scripts/project_key_reference.py` 先把算法跑通；
> 实装后它与正式代码重复，已删除（避免两份会各自漂移的实现），有价值的样本已并入正式测试。
>
> **第一版实现被自检证伪出两个 bug**（记录在案，因为它正说明为什么这类编码必须先跑再写）：
>
> | bug | 现象 | 修法 |
> |---|---|---|
> | 主体折叠与哈希折叠不一致 | 哈希对路径做 `lower()`，主体却保留原大小写 → `D:\work\g10` 与 `D:\WORK\G10` **主体不同、哈希相同**，同一工程得到**两个目录**（是"防碰撞"的反面） | slug 化前先做 ASCII `lower()` 折叠，**主体与哈希用同一个规范化串** |
> | 安全字符集漏了 `%` | 正则 `[A-Za-z0-9._-]` 拒绝了 slug 化自己产出的 `%XX`，所有含中文/空格的路径都被判"不安全" | 白名单加 `%` |
>
> 现在 8 个样本（含盘符、UNC、中文、空格、大小写差异）全部通过，且 `D:\work\g10` 与 `D:\WORK\G10` 正确得到同一 key。

### 4.2 规范化规则（决定"同一个工程"的判定）

`normalize(root)`：`resolve()` → 去尾部（`\`、`/`）→ Windows 折叠为 NFC + 大小写不敏感比较（保留原始大小写存储）。

> 已知取舍：同一工程换盘符（`D:\` → `E:\`）会被判成新工程。这是选"绝对路径"作标识的固有代价，已在决策中确认接受。

## 5. 实现：`StateLayout` 是唯一构造路径的地方

**核心手法（这是控制改动面的关键）**：不把 `workspace_dir` 改名后到处传，而是引入一个**纯值对象**，让 80 处调用点继续读 `settings.<同一个属性>`，只有 `config.py` 知道路径怎么拼。

```python
@dataclass(frozen=True)
class StateLayout:
    """纯值对象：只算路径，不建目录、不读写文件。"""
    state_home: Path      # A 的根
    project_key: str      # '' = 未绑定工程（只有 A 可用）
    project_root: Path | None

    # ---- A 工具全局（机器级）----
    def global_path(self, name: str) -> Path: return self.state_home / name
    def tools_dir(self) -> Path: return self.state_home / "tools"

    # ---- B 每工程 ----
    def project_dir(self) -> Path:
        if not self.project_key: raise NotBound("未绑定工程；先 codex-battlefield use <目录>")
        return self.state_home / "projects" / self.project_key
    def db_path(self) -> Path: return self.project_dir() / "battlefield.sqlite3"
    def runs_dir(self) -> Path: return self.project_dir() / "runs"
    def snapshots_dir(self) -> Path: return self.project_dir() / "state_snapshots"
    ...
    # ---- C 工程材料 ----
    def worktrees_dir(self) -> Path: return self.project_dir() / "worktrees"
```

`Settings` 只加**一个**字段（其余全部变成派生）:

```python
state_home: Path = Path(os.getenv("CODEX_BATTLEFIELD_STATE_HOME") or (Path.home() / ".codex-battlefield"))
```

以及保留 `workspace_dir` **作为兼容别名**，指向 `layout.project_dir()`（这样 80 处调用点在 Phase 2 之前一行都不用改，行为却已经跟着工程走）。

### 5.1 三条不变量（有测试守着）

| 编号 | 不变量 |
|---|---|
| L1 | `StateLayout` 的任何方法都**不**创建目录、不读写文件（纯函数，可断言） |
| L2 | 目录创建只发生在两个地方：进程启动、`mount_project` |
| L3 | 未绑定工程时，取任何 B/C 路径必须**响亮失败**，不得静默退回状态根 |

## 6. 关键决策

| 编号 | 决策 | 理由 | 被推翻会怎样 |
|---|---|---|---|
| D1 | 一个进程只服务**一个**工程；切换 = 换 `active.json` + 重启 API | 一个进程内换库会让缓存的 `SessionLocal` 指向旧库，是静默写错库的重大风险；重启 API 是秒级且已有先例 | 若必须热切换，需引入按工程分片的 session 工厂 + 每请求绑定，复杂度高一个量级 |
| D2 | 库路径优先级：`CODEX_BATTLEFIELD_DATABASE_URL` > `active.json` 指向的工程库 | 保测试不变（测试全靠这个环境变量做进程级隔离，约 60 个文件） | 若测试被迫改，改动面会爆炸 |
| D3 | `machine_safety.json` / `l4_guardian_state` / 槽位 / 预算闸门 = **机器级**（状态根） | 并发槽位是**机器**资源；每工程一份会让 N 个工程各跑满 `max_concurrency`，把机器打死 | 若改每工程，需先做跨进程的全局配额仲裁 |
| D4 | helper 脚本放状态根 `tools/`，不放每工程 | 它们无状态、纯模板产物；**更硬的理由见 §11-H1**——`tools/godot_with_timeout.py` 的锁/槽位路径是从**自身位置**推出来的，放每工程会让 Godot 主机级互斥被按工程复制 | 必须同时保留 `CODEX_BATTLEFIELD_GODOT_LOCK_PATH` / `_STATE_PATH` 显式覆盖，不能只靠自动推导 |
| D5 | `worktrees` 放工程目录下 | 需要与**目标仓库同盘**（见 §8），工程目录是唯一已知的、可覆盖"用户随便挑的多块盘"的位置 | 见 §8 fallback |
| D6 | `schema_dir` 改为显式 `Settings.schema_dir`，默认 `ROOT_DIR / "schemas"` | 修掉 §2.2 的布局耦合 | 无 |
| D7 | **每个工程一份库**意味着"跨工程的全局扫描"必须逐个重新设计 | 现状有 3 处语义上依赖"只有一个工程"（见 §11-H5/H6），不做处理会变成静默错误 | 无——这是每工程一库的必付代价 |

## 7. 数据库边界（§2.1 的解法）

目标：**保形**——`from .database import SessionLocal, engine` 这 522 处一行不改，但库随工程走。

手法：把模块级的 `engine` / `SessionLocal` 变成**按当前工程解析一次、并支持重绑**的模块属性。

```python
# api/app/database.py
_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None

def _resolve_url() -> str:
    if url := os.getenv("CODEX_BATTLEFIELD_DATABASE_URL"):   # D2：环境变量优先
        return url
    return str(get_settings().layout.db_path())              # 落到当前工程的库

def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = create_engine(_resolve_url(), connect_args={"check_same_thread": False})
    return _engine

def get_session_factory() -> sessionmaker: ...
```

**兼容层（必须有，否则 522 处会碎）**：

- 保留模块级 `engine` / `SessionLocal` 名字；
- 用**模块级 `__getattr__`**（PEP 562）惰性解析，使 `database.engine` 在首次访问时才建引擎；
- `monkeypatch.setattr(database, "engine", legacy_engine)` 仍然有效（setattr 写入模块字典，优先于 `__getattr__`）；
- 新增 `reset_engine()` 供测试与"换工程后重启"使用。

> **实测取证（2026-10-14，`probe-module-getattr.py`）**：上述手法四条全部成立——
> import 后 `_engine` 仍为 `None`（真惰性）；首次访问触发解析；
> `setattr` 后 `database.engine` 拿到被打桩的值；未定义属性仍正常抛 `AttributeError`。
>
> ⚠️ **同时测出一条必须写明的限制**：**`from-import` 已绑定的名字不跟随重绑**。
> `bound = database.engine`（或 `from app.database import engine`）拿到的是**当时**那个引擎，
> 此后 `setattr` 不会改变 `bound`。
> 这对现有测试是**安全的**，但顺序必须成立：`engine` 在 import 期被绑定，
> 而打桩发生在 import **之后**——所以 `tests/test_constitution_versions.py:422` 的
> `monkeypatch.setattr(database, "engine", legacy_engine)` 影响的是
> **模块属性访问**（`database.engine`），不是文件顶部那个已绑定的名字。
> 改这一块时必须确认该测试里后续用的是 `database.engine` 而非裸 `engine`；
> 否则要把它改成模块属性访问，或改用 `reset_engine()` 显式重绑。
> 这条已列进 P3 的出口判据（DB4 按此细化）。

**P3 必须处理的一个既有测试耦合**（已核对源码，不是推测）：

`tests/test_constitution_versions.py:406-426` 做了两件事：

```python
monkeypatch.setattr(database, "engine", legacy_engine)
monkeypatch.setattr(database, "settings", SimpleNamespace(database_url=url))
database._migrate_sqlite_columns()      # 内部 `with engine.begin()` —— 走模块属性，兼容层成立
```

- 第一行**安全**：`_migrate_sqlite_columns` 里是 `with engine.begin()`（`database.py:90`），
  走模块属性访问，`__getattr__` 会让它拿到被打桩的引擎。
- 第二行**会破**：它把 `database.settings` 换成只有 `database_url` 的 `SimpleNamespace`。
  一旦 `_resolve_url()` / `_migrate_sqlite_columns` 开始访问 `settings.layout`，
  这个 stub 就会 `AttributeError`。

**对策（二选一，P3 时定并写进测试）**：
  1. `_migrate_sqlite_columns` 不再读 `settings`，改判引擎自身的 URL
     （`get_engine().url.database`）——顺带去掉一处 `settings` 依赖，**推荐**；
  2. 或把该测试的 stub 补上完整字段（`SimpleNamespace(database_url=url, layout=<stub>)`）。

> 一般原则：`_resolve_url()` 的判定应尽量只依赖**环境变量**与**引擎自身**，
> 少依赖 `settings` 的内部形状——否则每个打桩 `settings` 的测试都会变成耦合点。

`_is_production_database_url`（`database.py:47-56`）当前硬编码 `WORKSPACE_DIR / battlefield.sqlite3`，改为**命中当前工程的库路径**即视为生产库。

### 7.1 验收（可反驳）

| 编号 | 判据 | 怎么反驳 |
|---|---|---|
| DB1 | 522 处 import 一行未改，全量测试仍绿 | 直接看 `git diff --stat`：`app/*.py` 里不得出现批量改 import |
| DB2 | 未设环境变量时，引擎落到 `active.json` 指向工程的库 | 起进程打印 `database.get_engine().url` |
| DB3 | 设了 `CODEX_BATTLEFIELD_DATABASE_URL` 时，工程机制被完全旁路 | 同上，指向环境变量给的库 |
| DB4 | `monkeypatch.setattr(database, "engine", ...)` 仍生效 | 现有 `test_constitution_versions.py` 不改也能过 |
| DB5 | 换 `active.json` 后重启，进程用的是新库 | 起两次，比对 `PRAGMA database_list` |

## 8. worktree 与跨盘约束（必须显式处理）

`git worktree add` 的实测结论（2026-10-14）：worktree **可以放在仓库目录之外**（同盘）。但跨盘有已知的 `.git` 文件 → 兼容性问题，且工作树与仓库跨盘会让 diff/status 变慢且易错。

因此：

1. **首选**：`<state_home>/projects/<key>/worktrees/`。若 `state_home` 与目标仓库**不同盘**，则：
2. **回退**：`<目标仓库>/.battlefield/worktrees/`（保证同盘）。
3. **必须响亮**：回退发生时写一条 WARNING 到运行记录与启动日志——"你的状态根和工程不在同一块盘，worktree 被放到仓库内"是个用户应当知道的事实，不能静默。

判据：`os.path.splitdrive(worktree_dir)[0] == os.path.splitdrive(project_root)[0]`。

### 8.1 worktree 根目前被**三处各自**推导（必须先收口）

审计确认同一个约定散在三处，任何一处漂移都会静默破坏行为：

| 位置 | 作用 |
|---|---|
| `orchestrator.py:10312` | **写入方**：`task.worktree_path = ws / "worktrees" / safe_id` |
| `worktrees.py:40-41`（+ 守卫 `44-58`） | **删除守卫**：`managed_root` / `is_managed_worktree` |
| `isolation.py:69-74` | 跨节点泄漏检测用的 worktree 路径映射 |

**P1 就要把它们收口到 `StateLayout.worktrees_dir()` 一个来源**，否则 P2 搬根时会漏改一处。
另外 `orchestrator.py:10092` 的泄漏检测用**全库** `TaskNode` 建映射（见 §11-H9），
收口时要一起限定到当前工程。

## 9. 工程挂载：一条命令

新增 CLI（`scripts/battlefield-project.py`，配 `.cmd` 包装器与 PATH 注册，做法照抄已落地的 `codex-account`）：

```
battlefield-project list                 列出已挂载工程 + 各库路径 + 大小
battlefield-project use D:\work\g10      挂载并切为当前工程（写 active.json + registry.json）
battlefield-project current              显示当前工程、状态根、库路径、是否回退 worktree
battlefield-project migrate [--from ...] 迁移老 workspace/（见 §10）
battlefield-project doctor               体检：状态根可写、库可打开、worktree 同盘、无 key 碰撞
```

`use` 必须原子：写 `active.json` 用"临时文件 + `os.replace`"，避免半写状态。

### 9.1 可诊断性（G6）

- 启动时打印一行：`state=<状态根> project=<key> (<root>) db=<库路径> worktrees=<路径>[ fallback]`
- 新增 `GET /api/state`：`{state_home, project_key, project_root, db_path, db_url, worktrees_dir, worktree_fallback}`；
- 前端头部显示当前工程名（数据来自 `/api/state`），避免"看着是这个工程其实在改另一个"。

## 10. 迁移（G5）

老布局：`<repo>/workspace/battlefield.sqlite3`。新布局：**每工程一库**。

规则：

1. 检测到老库，且**没有**任何已挂载工程 → 用老库里的 `constitutions.target_path`（`main.py:79-80` 已经在枚举它）推断工程根；推断不出或多于一个 → **不猜**，要求显式 `battlefield-project migrate --to <目录>`。
2. 迁移 = **复制**（不移动），并在新库写一条 `migration_source` 标记（表已存在则加列，沿用 `database.py:64` 的 `_migrate_sqlite_columns` 手法）。
3. **必须重写持久化的 worktree 路径**：`TaskNode.worktree_path` 是列，值里带着旧的 worktree 根。
   迁移时对每一行做**前缀重写**（`<旧 worktree 根>` → `<新 worktree 根>`）。
   不做的后果不是"路径难看"，而是 §11-H7：worktree 删除守卫
   （`worktrees.py:44-58`）与实际路径不一致，会**挡住清理或误删**。
4. 老 `workspace/` **不删**，改名 `workspace.migrated-<ts>/` 或原样留着，由用户决定——删除是不可逆动作，不代做。
5. 迁移脚本必须可重入：重复跑不产生第二份副本（比对库文件 sha256）。

## 11. 风险与对策

> §11-H* 是**逐行审计**（144 处 `workspace_dir`/`WORKSPACE_DIR` 引用，分类 A=84 / B=36 / C=19）得到的
> 具体危险点。它们比一般性风险更重要：每一条都是"改完看着正常、实际静默错"的形态。

### 11-H 具体危险点（按危险程度排序）

| 编号 | 危险点 | 为什么危险 | 对策 |
|---|---|---|---|
| **H1** | `api/app/tools/godot_with_timeout.py:17-41` 的锁/槽位路径由 `Path(__file__).with_name(...)` 推出 | `tools/` 一旦按工程复制，**Godot 的主机级互斥就被按工程复制**，多个工程各自拿锁 → 实际并发数变成 N×，正是"槽位"要防的事 | `tools/` 必须留在 A（状态根）；且必须保持 `CODEX_BATTLEFIELD_GODOT_LOCK_PATH`/`_STATE_PATH` 显式覆盖（`runner.py:1542/1545` 已经在给） |
| **H2** | `runner.py:824` `workspace_dir.parent / "schemas"` | 今天只是**碰巧**等于 `ROOT_DIR/schemas`。移走后 `.parent` 指向 `~` 或 `projects/` → `--output-schema` 指到不存在的文件 → codex **秒退、0 token**，而状态机当成"跑完了"继续走 | **已修（P0）**：改为 `ROOT_DIR / "schemas"` + 回归测试 `tests/test_schema_dir_is_repo_anchored.py`（3 条，已双向证伪）。连带删掉 `bench_run.py` 的 `prepare_schemas()` 及其调用——它**不是**死代码（本表初稿说错了），而是为绕开本 bug 而生的复制步骤 |
| **H3** | `database.py:53` 生产库判定硬编码老路径 | 移库后它与真库**永远不相等** → `DROP TABLE` 保护**静默失效**在活库上 | **已修（P3）**：判据从"是不是生产库"**反过来**为"是不是**明确可丢**的库"（只有 SQLite 临时目录下的放行）。理由：生产库现在可以在任意位置，没有可枚举特征；而挡错只是多敲一个环境变量，放过是不可逆的数据丢失。另加回归测试覆盖"工程状态目录里的库"这个**原先没人盯**的位置 |
| **H4** | `scripts/battlefield_watchdog.ps1:33` 是**独立进程**，自己重算 `$RepoRoot\workspace` | 它同时管 A 与 B：`watchdog.log`、`codex_health.json`、`machine_safety.json`、4 个 restart/drain flag、`runs/`(788)、kick 日志(1064-1065)、`full_checkpoints/<root>`(1330)、`checkpoint_ledger/<root>`(1343/1424)、**以及 sqlite**(1605/1650/2285)。不一起改 → 看门狗读写的地方 API 已经不看 | 必须同步教它状态根 + 每工程布局；新增的状态根环境变量也要在它和 `register_battlefield_watchdog_task.ps1` 里落地 |
| **H5** | `local_watchdog_state.py:88` 与 `repair_retryable_block_reports.py:97` 用 `db_path.parent / "codex_health.json"` 定位健康文件 | 这**只在"库和健康文件同目录"时成立**。A/B 拆分（健康文件在状态根、库在工程目录）会立刻打断这两个脚本 | 健康文件路径改为显式从状态根取；两个脚本一起改 |
| **H6** | `checkpoint.py:102-103` + `orchestrator.py:5861` `latest_snapshot_root_id` 扫"所有工程里最新的快照" | 它是**库丢失恢复**路径（`_restore_snapshot_if_missing`，自 5967/6300 调用）的输入。每工程一库后它必须限定在当前工程，否则恢复不确定 | 限定扫描范围到当前工程目录；补测试：A 工程不会恢复出 B 工程的快照 |
| **H7** | `TaskNode.worktree_path` 是**持久化列**，值里带着旧 worktree 根 | 移动 worktree 根会让**所有历史行**指向不存在的路径；而 `worktrees.is_managed_worktree`(44-58) 是**删除守卫**，不一致会挡住清理或误删 | 迁移时按新旧根做**前缀重写**（§10 增加一步）；守卫根必须与 `orchestrator.py:10312`、`isolation.py:69-74` 三处保持同一来源 |
| **H8** | `orchestrator.py:1920` 用 `workspace_dir` 做子串去匹配外部 `codex.exe` 命令行 | 移走后要么**匹配不到**（外部 CLI 不计入 → `max_concurrency` 不再被真正执行），要么指向状态根 → 匹配到**所有工程** | 改为匹配当前工程的 worktrees 根 |
| **H9** | `orchestrator.py:10092` `_flag_cross_node_leaks` 用**全库**的 `TaskNode` 建 worktree 映射 | 多工程共用一个状态根时会**把别的工程的 worktree 报成本工程兄弟泄漏**（advisory 噪声，且排除表 10093-10097 不再覆盖） | 限定到当前工程的节点 |
| **H10** | `orchestrator.py:7544-7550` `_safe_workspace_path` 是**产物读取的允许根** | 留在状态根 → 过宽（能读任意工程产物）；缩太窄 → 运行产物调试视图直接"拒绝读取" | 绑定到当前工程目录；注意 `RunRecord` 存的是绝对路径，7547 的相对分支是潜在陷阱 |
| **H11** | `orchestrator.py:906` `_new_one_shot_target_path` 把**一次性工程材料**建在 `workspace/projects/` | 若 `workspace_dir` 变成状态根，真实工程根会落进隐藏状态目录，而 `main.py:79` 的预览允许表与 `_root_target_path`(10375) 又指回它 | 一次性目标目录改到状态根之外（用户可见的项目区），或明确划到工程目录并同步允许表 |
| **H12** | `main.py:79` / `main.py:380` 预览允许表只含 `[ROOT_DIR, workspace_dir]` | 移走后 worktree 图片预览会 403（"preview file is outside allowed project roots"）；相对路径的含义也变了 | 允许表改为 `[ROOT_DIR, 当前工程目录]` |
| **H13** | `runner.py:831` 与 `872` 把 workspace 当 **codex 的 cwd** | `872` 的 fallback 在移走后会把 **状态根当成工作仓库**交给 agent | 831（constitution 无 root 阶段）改用当前工程目录或一次性目标目录；872 的 fallback 不得是状态根 |
| **H14** | 12 处把状态路径**写进模型提示词/子进程环境**（`prompts.py:38/221/224/225/251/287/288/298/307/375/378/379` + `runner.py:1529-1531/1538-1549`） | 状态根若在 `~/.codex-battlefield`，模型会被明确告知它的工作根与工具都在工具的隐藏状态目录里 | 提示词改引用**工具名/相对调用**而非绝对状态路径；或接受并记录这个暴露面 |

### 11-R 一般性风险

| 风险 | 触发条件 | 对策 |
|---|---|---|
| **静默写错库** | 进程没重启就换了 `active.json` | D1：启动时把 `project_key` 与库路径**绑进一次快照**；运行期不回读 `active.json`；`/api/state` 显示的是快照值 |
| 144 处路径拼接写错 | 逐个手改 | §5：只改 `config.py` 一处，其余用兼容别名；P3 再逐个语义化 |
| 522 处 import 碎掉 | 直接删掉模块级 `engine` | §7 兼容层 + DB1/DB4 判据 |
| 跨盘 worktree 静默降级 | 状态根与仓库不同盘 | §8 判据 + 响亮 WARNING |
| 机器级状态被误做成每工程 | 一刀切 | D3 + §11-H1：`tools/`、槽位、预算闸门、健康文件、Godot 锁全部留状态根 |
| 测试大面积飘红 | 默认库路径变了 | D2：环境变量优先，测试路径不变；注意约 150 处 `workspace_dir=` 传参（`test_orchestrator.py`、`test_budget.py:184`、`bench_run.py:248` 等），保留该字段名可免于大批改名 |
| 测试把老默认库路径**钉死** | 改动后这些断言必挂 | `test_repo_gates.py:156-168` 断言脚本默认 URL、`test_orchestrator.py:1862` 自建"生产 URL"、`conftest.py:25-26` 覆盖测试库——P1 就要一起更新，且**新断言要表达新语义**而不是删掉 |

## 12. 实施顺序（每阶段独立可验证、可提交）

| 阶段 | 内容 | 出口判据 |
|---|---|---|
| **P0 先修独立缺陷** | `schema_dir` 改为 `ROOT_DIR/"schemas"`（H2）；worktree 根三处收口到单一来源（§8.1） | 这两条**与状态搬迁无关、可以立刻单独修**，且各自都是静默故障。先修它们能让后续阶段少两个混淆变量 |
| **P1 地基** | `StateLayout` + `state_home`/`project_key` 进 `Settings` + `/api/state` + 目录创建收口（L2） | L1/L2/L3 断言过；`/api/state` 可读；全量测试绿 |
| **P2 路径收口** | `workspace_dir`/`database_url` 默认值跟随布局（绑工程即落到工程目录）；`local-kick-*` 日志转 B | ✅ 已落地。**原计划里的"A 类状态搬到状态根"推迟到 P6**，理由见下方偏差说明 |
| **P3 数据库边界** | 生产库判定改判（H3） | ✅ 已落地（4 条判据 + 新增"工程状态库"这个原先没人盯的位置）。**惰性引擎/兼容层未做**，理由见下方偏差说明 |
| **P4 挂载 CLI** | `battlefield-project` 四命令 + 原子 `active.json` | G2/G6 可演示：切工程后 `/api/state` 与库都变 |
| **P5 迁移** | §10 迁移脚本 + worktree 路径前缀重写 + 可重入 | ✅ 已落地（`api/app/migration.py` + `battlefield-project migrate`）。**默认只出方案，`--apply` 才动手**；实测真实旧库：4 行保住、2 行 worktree 重写、幂等复跑不产生第二份 |
| **P6 看门狗与脚本** | A 类状态搬到状态根 + `battlefield_watchdog.ps1`（H4）+ `local_watchdog_state.py`/`repair_retryable_block_reports.py`（H5）+ 其余 10 个脚本 | 看门狗写的位置与 API 读的位置**逐项对齐**；健康文件不再靠 `db_path.parent` 推 |
| **P7 前端与文档** | 头部显示当前工程；更新 `docs/*` 里所有 `workspace/...` 路径引用 | ✅ 已落地：`/api/state` 接到界面上（`WatchdogPanel` 里的状态布局条），`provider-setup.md` 的隔离 home 落点改为按布局解析 |

### 12.1 落地偏差（如实记录）

**① 惰性引擎 + 模块级 `__getattr__` 兼容层：未做，且判断为不需要。**

原设计的理由是"URL 在 import 期被烧死，无法跟随工程"。但 **P2 换了更省事的解法**：
`workspace_dir`/`database_url` 在 `Settings.__post_init__` 里解析（不再是 dataclass
字段默认值），而 `database.py` 本来就是 `settings = get_settings()` 之后立刻建引擎
—— 于是引擎**自然**落在正确的库上。

而"运行期跟随"是**不该有**的能力（决策 D1：一个进程只服务一个工程，换工程要重启）：
热换库会让缓存的 `SessionLocal` 指向旧库 = 静默写错库。

更要紧的是 `__getattr__` 方案**解决不了它声称要解决的问题**：`from app.database import engine`
在 import 期就把名字绑定了，`__getattr__` 改变不了已绑定的局部名
（这一点在 P1 前的探针里实测过，见 §7）。

所以：**不做**。DB1/DB4 这两条判据随之作废（它们是为兼容层写的）；
DB2/DB3/DB5 由 P2 的 `test_workspace_follows_project.py` 覆盖。

**② "A 类状态搬到状态根"从 P2 推迟到 P6。**

理由：它影响的文件（`machine_safety.json`、`codex_slot_reservations.json`、
`budget_guard.json`、`l4_guardian_state.json`、`codex_health.json`、
`godot.global.lock`/`godot.resource.state.json`）**同时被看门狗进程读写**（§11-H4/H5）。
分成两步做会产生一个**中间态**：API 搬到状态根、看门狗还在读 `workspace/`，
表现就是"看门狗读写的地方 API 已经不看了"——正是要避免的那个静默错位。
放在 P6 与看门狗一起改，中间态不存在。

> **P6 不能省**：`battlefield_watchdog.ps1` 是**独立进程**，它自己重算 `$RepoRoot\workspace`，
> 同时管 A 与 B（含 sqlite 本身）。漏改它的后果是"看门狗读写的地方 API 已经不看了"——
> 典型的静默错位，且只在出问题时才暴露。

> 注：P7 的文档面比看起来大——`docs/provider-setup.md`、`docs/checkpoint-git-anchor.md`、
> `scripts/use_provider.ps1`、`battlefield_watchdog.ps1` 都硬编码了 `workspace/`。
> **Hub 的隔离 `CODEX_HOME` 迁移时必须一起搬**（已实现：`use_provider.ps1` 现在问
> `state_paths.py` 要落点；`migration.py` 也会把老的 `workspace/codex-home/` 一并复制过去），
> 否则会重演"provider 配置丢失 → 401"那个事故（`docs/provider-setup.md` §2.1）。

## 12.2 落地后的实际状态（2026-10-14）

| 阶段 | 结果 |
|---|---|
| P0 | ✅ `schema_dir` 锚回 `ROOT_DIR`；worktree 根收口到 `worktrees.worktree_path()` |
| P1 | ✅ `StateLayout` + `Settings.state_home`/`project_key` + `GET /api/state` |
| P2 | ✅ `workspace_dir`/`database_url` 默认跟随绑定工程（显式传参优先） |
| P3 | ✅ 生产库判定反转为"只有临时目录里的库可丢" |
| P4 | ✅ `battlefield-project`（list/current/use/where/unmount/doctor/migrate）+ PATH 包装器 |
| P5 | ✅ `migration.py`：复制不移动、`worktree_path` 前缀重写、sha256 幂等、不猜工程根 |
| P6 | ✅ 看门狗解析新布局（`-StateHome` + 回退旧位置）；机器级状态搬状态根；任务注册透传 `-StateHome`；Hub 脚本走新布局 |
| P7 | ✅ 前端状态布局条；Hub 文档更新 |

**机器级落点的精确语义**（P6-B）：`Settings.machine_root` = 绑定工程 ? 状态根 : `workspace_dir`。
未绑定保持老位置是刻意的——所有测试都跑在"未绑定 + `workspace_dir=tmp_path`"，
这样这次改动不需要改任何测试的落点。

**测试隔离**（P6-B）：`conftest.py` 把 `CODEX_BATTLEFIELD_STATE_HOME` 指到每会话独立的
临时目录，并清掉 `CODEX_BATTLEFIELD_PROJECT_KEY`。不加这条时，开发机真实状态根里的
`active.json` 会被 `Settings.__post_init__` 读到，28 个用例在并行全量时失败、单跑通过。

## 12.3 可用性补强（P8，2026-10-14）

落地后立刻暴露出一类**"装好了但用不顺"**的问题，逐条修掉：

| 问题 | 表现 | 修法 |
|---|---|---|
| 状态根只能靠环境变量配置 | API 常被 GUI/计划任务拉起，**拿不到你 shell 的环境变量** | 新增用户级配置 `~/.codex-battlefield.toml`；优先级 **环境变量 > 配置文件 > 内置默认值** |
| 跨盘 worktree | 状态根默认在 `C:`、工程在 `D:` → `git worktree` 不可靠 | `battlefield-project state move <目录>`（先预演、复制不移动、可选 `--purge-old`）；`doctor`/`current`/`state show` 都会把跨盘指出来 |
| 命令只在仓库里能用 | 包装器放 PATH 上就找不到实现 | 配置文件记 `repo_root`；包装器按 **环境变量 → 配置 → 同目录** 三级查找 |
| 补全逻辑太宽 | 状态根搬完后 `doctor` 报"key 不一致"——它把**状态根自己**当成一个工程 | 只认**自洽**的 `project.json`（存在 + root 绝对路径 + 目录名 == 重算的 key） |

### 12.3.1 三个实测踩到的坑（都留了回归测试）

1. **TOML 写 Windows 路径忘了转义反斜杠。** `state_home = "D:\work\x"` 里 `\w` 不是合法
   转义 → 读端 `TOMLDecodeError` → 静默退回默认状态根。表现：搬完状态根后
   `state show` 仍报旧位置。修法：`_toml_value()` 统一转义；
   并且**坏配置不再静默**——降级到默认值的同时把原因写到 stderr。
2. **`.cmd` 包装器用了 LF 行尾。** cmd.exe 需要 CRLF，否则 `rem` 行被拆开、
   碎片当成命令执行，报 `'battlefield-project:' is not recognized ...`。
   同时 `.cmd` 里的非 ASCII（中文注释/路径）会被 OEM 码页解坏，所以包装器**保持纯 ASCII**。
3. **`$LASTEXITCODE` 陈旧。** `$x = (& native ... | Select-Object -First 1)` 之后它是 `-1`，
   于是"校验退出码"会把**成功**输出当成失败丢掉（见 `use_provider.ps1` 里的注释）。

## 13. 不做与已知限制

| 项 | 为什么不做 | 要真做需要什么 |
|---|---|---|
| 进程内热切换工程 | 见 D1 | 按工程分片的 session 工厂 + 每请求绑定 |
| 同一工程多 checkout 共享状态 | 标识用的是绝对路径（§4.2） | 改标识为 git 远端 + 仓内相对路径，并处理无远端仓库 |
| 跨机器状态同步 | 不在本 spec 范围 | 版本化状态格式 + 冲突解决 |
| 状态加密 / 多用户隔离 | 现状是单用户本机工具 | 引入凭据服务与用户模型 |
