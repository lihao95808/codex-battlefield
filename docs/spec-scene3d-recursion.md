# SPEC：scene3d 递归分件与精度出口（v2-r2 规则修订）

> 前置：[`spec-scene3d.md`](spec-scene3d.md)。
> 本文是下一阶段的**目标规则与开发验收合同**，不是现有实现能力声明。
> 修订日期：2026-10-19。本文替代原 v2 草稿中互相冲突的分包/精度规则。
> 本轮仅修订规则、执行逻辑诊断；没有按本文改完实现。真实运行与实现状态见 §12。

## 1. 目标与不可混淆的概念

目标：用结构化任务与独立验收，让普通 3D 能力的 Flash 能交付不断细化、但**有明确出口**的场景。

| 概念 | 含义 | 不等于 |
|---|---|---|
| object / assembly | 完整物体或装配体，例如城堡 C | 一个八叉树格子 |
| slot | 完整交付身份，例如一面墙、一个窗框资产 | 可以随格边界剪掉的几何切片 |
| bundle | 一组独占交付的 slot | 空间八等分 |
| scene scope | 冻结的部件责任域，例如 C、C1、C21 | 模型临时猜出的任务权限 |
| task | 执行某个 scope 的一次工作/修复尝试 | 场景细节层级本身 |
| check | 读取明确几何依赖的一项判据 | 所在坐标系对应的物体一定已交付 |

本版支持：已声明构件的分件委派、父级确定性装配、指标与产物展示（M1–M3）；冻结精度计划与有界修复自 M2 生效。
非目标：任意有机曲面、UV 连续性判定、无限世界流式加载、自动审美评分。

## 2. 完整物体与空间索引不互相切割

**R-O1：slot 只有一个交付 owner。** bundle 分配完整 slot，不能把同一个 slot 分给两个叶子各生成一半。
城堡 C 可以拥有墙、窗、旗帜等子部件，但最终由 C 的装配责任域交付完整物体。

**R-O2：八叉树只做地址、查询、调度索引。** 若一个物体跨多个 cell，整体归给能完全容纳它的最深公共容器；
其它 cell 只引用同一个身份。不得按中心点归属后裁边，不得为了塞进格子修改 mount 或缩放/裁切实体。

**R-O3：改变空间分辨率不得改变实体。** 在几何合同不变时，改变 cell 的划分只能改变索引，
slot ID、生成参数、顶点/几何摘要和交付 owner 必须保持不变。

设计的门洞/窗洞、构件库内部的确定性网格分解不属于“按 cell 裁物体”。
禁止的是把独立模型生产任务沿空间格边界无辜切开；不禁止正常建模操作。

不采用“面与格边界平行就说明被裁切”的推断：合法墙面本来就可能与格面重合。
保证来自**交付身份、生成输入、装配操作和几何不变性证据**，不是这种表面启发式。

> 当前没有八叉树实现。本节固定未来引入空间索引时必须遵守的边界，不宣称空间索引已经开发。

## 3. 契约、scope 与节点绑定

v1 完整场景契约保留原语义。v2-r2 使用明确 schema revision，避免把原型 v2 和修订规则混用。
下例是拟实施格式，**当前原型不能按此格式直接开生产任务**。

```json
{
  "version": 2,
  "schema_revision": "v2-r2",
  "declaration": "scene.declaration.json",
  "parts": "scene.parts.json",
  "root_scope": "C",
  "bundles": [
    {"id": "body", "slots": ["wallS", "wallN", "wallW", "wallE", "towerSW", "towerSE", "towerNE", "towerNW"], "file": "bundles/body.parts.json"},
    {"id": "openings", "slots": ["doorS", "wallS.frame0", "wallS.frame1", "wallN.frame0", "wallN.frame1", "wallW.frame0", "wallE.frame0"], "file": "bundles/openings.parts.json"},
    {"id": "ornaments", "slots": ["battleS", "battleN", "battleW", "battleE", "mouldS"], "file": "bundles/ornaments.parts.json"}
  ],
  "scopes": [
    {"id": "C", "kind": "assembly", "children": ["C1", "C2"], "importance": 1.0,
     "connectivity": "required", "output": "scene.parts.json"},
    {"id": "C1", "kind": "bundle", "bundle": "body"},
    {"id": "C2", "kind": "assembly", "children": ["C21", "C22"], "importance": 2.0,
     "connectivity": "not_required", "output": "assemblies/details.parts.json"},
    {"id": "C21", "kind": "bundle", "bundle": "openings"},
    {"id": "C22", "kind": "bundle", "bundle": "ornaments"}
  ]
}
```

冻结前必须验证：

1. 叶子 bundles 的 slot 集互不相交，并集恰好等于活动声明中的全部 slot；重复、缺失、多出都拒绝。
2. scope 是单根有限树：无环、无孤儿、非根只有一个父级；每个 bundle 恰好由一个叶子 scope 交付。
3. assembly 的责任域由其后代 bundle 并集**计算**，不能让模型另报一个更小的集合。
4. IDs、文件和子引用合法、唯一；范围/路径规则见 §8。根 output 与顶层 parts 是同一约定路径。
5. 每个 slot 都能合法构造，每个责任域都有明确验收责任；不能冻结一个先天无法通过的计划。
6. `importance` 可选，默认 1.0，必须是 0.5–4 的有限正数；它只影响 M2 的细节下限，不影响归属。
7. `connectivity` 可选 `required` / `not_required`，**只有声明 required 的 scope 才要求单一实心接触组**。
   首版石堡仅根 `C` 声明 required；中间装配（例如 C2 的门窗+装饰）合法地彼此不相连。
8. bundle scope 不接受 `children` 字段（空数组也拒绝），避免"叶子还带子节点"的歧义。

### 节点绑定与权限

- 根 task 在开树时绑定 root_scope；planner 的子任务只可选择该 scope 已批准的直接 children。
- 若复用 `ChildTaskSpec.scope` 字符串入口，标记必须是完整独立项，例如 `scene3d-scope:C21`，**精确匹配**。
  不从标题、description、文件名或 token 子串猜身份。
- 编排器校验后写入权威 node metadata：root、scope_id、scope_kind、owned_bundle_ids、contract revision。
- v2 子节点无身份、身份重复/歧义、越过批准 children、相同 owner 被同时交付两次，均拒绝创建/启动。
- 修复 task 继承原 scope，不能通过换标题、换 token、缩小槽位集降低验收门槛。
- **装配父必须验全自己的责任域。** C2 验 openings+ornaments；根 C 验完整场景。
  中间父不必拥有整世界，但绝不能以单个叶子 bundle 的检查冒充其完整责任域验收。

## 4. 检查依赖、局部通过与父级履责

四种关系分开保存：slot 的装配/引用关系、bundle 的独占写入、task 的执行依赖、check 的几何依赖。
共享冻结尺寸/锚点不一定要求“另一任务先完成”；读取另一任务的实际产物才产生执行依赖。
不得为了风格一致而让每个叶子读取兄弟的可写工作树。

v2 check 必须显式写 `requires_slots`；它定义本检查要读取的几何集合。
`coordinate_slot` 仅提供冻结坐标系，可以不属于当前交付 bundle。旧 `check.slot` 不能替代这两者。

```json
{
  "id": "wallS.win0.frame_exists",
  "rule": "region_between",
  "coordinate_slot": "wallS",
  "requires_slots": ["wallS.frame0"],
  "box": [2.25, 0.27, 2.35, 2.95, 0.33, 3.65],
  "min": 0.04,
  "max": 0.70
}
```

这个例子只测窗框几何，借墙的冻结坐标系定位，不能把实心墙算成窗框存在。
另设窗框安装检查，其 `requires_slots` 为 `wallS` 与 `wallS.frame0`，由同时覆盖两者的装配祖先履责。

**R-C1：责任在冻结时分配，算法是 LCA 而不是"深度最低"。** 对每项 check：

1. `requires_slots` 的每个 slot 必须恰好有一个 owner bundle scope（否则合同非法，拒开树）；
2. 责任 scope = 这些 owner 的**最近公共祖先**（LCA）；图不是链，不能用"层数最浅"代替；
3. 该 LCA 必须存在且唯一；不存在则拒开树，不做"就近取父"的兜底；
4. 责任写入冻结视图：`check → responsible_scope`，报告里必须给出，而不是只给延后列表。

**执行者是责任 scope 的完成门禁（gate），不是 acceptance agent 的自由裁量。**
父 acceptance 只决定"要不要接受这次交付"，不决定"哪条检查该谁跑"；
门禁按冻结视图在各 scope 完成前执行自己的那部分，因此模型既不能跳过检查，也不能把检查挪走。

**R-C2：只评所需几何。** 局部区域检查使用 requires_slots 对应的实心几何，不能用其它 slot 遮蔽缺陷。
坐标系从完整冻结声明解析，不能因主体包未交墙就无法定位一个窗框检查。

**R-C3：外部检查显式延后。** 只涉及本包的 checks 必须过；跨包的安装/连接检查记录为
`deferred_to_scope`，不能算 passed，也不能因兄弟尚未交付而把正确的主体包判失败。

**R-C4：自己的缺件不可延后。** 检查依赖在该 scope 的责任域内，却缺实际交付/有效 receipt，立即失败。
不能把“我没完成”解释成“等父节点检查”。

**R-C5：每包必须可验。** 冻结时要求至少一项真实内在检查；不存在时补充具名的构造/几何检查，
不得靠空 checks、虚构恒真 check 或临时关闭检查取得局部通过。

**R-C6：根不得有未履责检查。** 子集通过仅说明该子集有效。根完成前重验全部活动 checks，
所有 mandatory checks 都须有实际执行结果；deferred/missing/unknown 不等于通过。

**R-C7：检查域必须与合法自由参数域相容。** 检查的采样区域必须对 `domain` 内**任意**合法取值都落在
"正确实现"的实体内（或落在明确声明的阈值内）。冻结时对受自由参数影响的检查做域角点抽样：
用参考构造在域的下界/中点/上界各生成一次，该检查必须**恒过**；否则拒开树。

这条来自实测：`moulding` 的检查按"整条带满外凸"取区域，但两级线脚的上半层合法地只有 0.55 倍外凸，
于是**合法实现被判失败**；而更早那次"通过"其实是被无关的塔身几何填满——两个方向的错都存在。

**R-C8：每项 check 必须有负样本证据，否则不算检查。** 冻结时机器生成一次负样本：
- `region_filled` / `region_between` / `runs_along_x`：移除其 `requires_slots` 的几何后必须失败；
- `region_empty`：把该区域填实后必须失败。

做不到（例如阈值 0 或 1、区域恒空、依赖恒缺）即为**无效检查**，拒开树。
理由：无法区分"检查通过"和"检查根本不会失败"的判据，等于没有判据。

连通性也按几何责任声明：分件包可暂时不相连，安装关系在覆盖相关部件的父级检验。
只有声明 `connectivity: required` 的 scope 要求单一实心接触组（首版仅根 C）；
未来多座建筑/飞鸟世界不能默认要求全世界物理相连。

## 5. 全局精度计划：谁定、测什么、何时停

### 5.1 分工与冻结

- spec/宪法规定**形式**：单位、度量算法、可细化对象、硬预算、成功/失败的区别，不许模型自行放宽。
- 开局设计 agent（可由 Kimi K3）提出**数值与细节清单**：哪些物体重点细化、最低尺度、批准的分件树。
- 在任何 worker 开始前由控制端校验、冻结，整个构建期只读。
- Flash 只在批准的自由参数域和责任域内实现；不能自行提高 importance、深度或任务预算。

### 5.2 精度不是 bbox 中位数

精度出口度量：**具名建模细节的名义设计尺度，统一换算成世界米**。
它不是最短网格边、所有盒子的最小尺寸、屏幕像素，也不是 part 的 bbox 中位尺寸。

由可信 builder feature profile 标明哪些参数/派生尺寸属于新增建模细节，例如：

| 类型 | 首版测量对象 | 精度规则 |
|---|---|---|
| `lattice` | 名义窗棂宽 `bar` | 按当前 owner 的细节下限约束 |
| `moulding` | 外凸量与带高 | 按细节下限约束 |
| `battlement` | 垛宽、垛高、设计间隙 | 按细节下限约束 |
| `tower` | 新增收分层的层高 | 按细节下限约束 |
| 安装接口 | `sink`、锚点容差 | 按安装域判，不冒充可见细节 |
| 基底/数值构造 | 固定基底纹理、孔洞网格分解、epsilon | 单独声明，不据此触发进一步递归 |

profile 由程序注册并冻结 revision；未知 builder/缺 profile 不得自行猜“最小细节”。
基底或接口中更小的数值可以存在，但报告必须解释口径：不能宣称“整个模型最短尺寸已达 δ”。
几何退化、零体积、非有限数另由 `degeneration_guard` 管理，**不替代精度出口**。

### 5.3 重要度与有效下限

```text
floor(scope) = default_detail_floor_m / importance(scope)
```

importance 是冻结的正数，首版允许 0.5–4。越重要越细，不是反过来。
例如默认 5cm，importance=2 的主体细部可细到 2.5cm，importance=0.5 的远景下限为 10cm。

编译器在冻结前产生有限的活动细节清单：

- 不重要/低于其下限的候选细节记为 `omitted_by_precision`，不成为必交槽位。
- 被选为活动细节的 slot 仍须完整交付；不能因为“有个小尺寸”就跳过整座城堡。
- 活动细节的有效自由参数域须与 floor 兼容；无交集则声明先天不可完成，拒开树。
- 例如 `bar` 原域 3–10cm、floor=5cm，执行域为 5–10cm；`sink` 仍按其安装域判。
- 修改 floor 必须改变活动清单/执行域/精度报告，不能像当前原型那样只保存一个闲置 δ 字段。

### 5.4 成功出口与失败出口分开

**STOP_OK**：本 scope 的所有活动责任已交付，全部本地检查通过，祖先责任已正确标明；
根还须完成所有跨包检查、全量装配与请求的真实渲染，且没有违反运行预算。

**OMITTED_BY_PRECISION**：低于批准尺度的候选细节在冻结时已被明确省略。
它不是缺件、失败或一个 worker 的临时决定。

**STOP_LIMIT**：时间/任务/修复/深度预算用尽，但责任未完成。必须记录限制原因并停止，
不能改为 DONE，不能把它说成“已经达到精度”。

**STOP_BLOCKED**：工具、权限或冻结契约不可用；不能靠持续细分解决环境问题。

首版不把 D、IoU 或未经标定的 `ΔD/credits` 当精度/审美出口。
具名细节清单+有效尺度+完成证据，是首版可测且有限的出口；动态残差生成细节是后续独立阶段。

## 6. 递归与修复在创建任务前受限

精度计划示例片段（拟实施字段）：

```json
{
  "default_detail_floor_m": 0.05,
  "feature_profile_revision": "builtin-box-v2-r2",
  "max_refinement_levels": 2,
  "max_task_depth": 4,
  "max_total_tasks": 12,
  "max_agent_runs": 16,
  "max_repairs_per_scope": 1,
  "deadline_seconds": 480,
  "supervisor_seconds": 540
}
```

- **上限只有一个来源**：几何槽位上限 `declaration.precision.slot_ceiling` 属声明（几何合同）；
  `max_total_tasks` / `max_agent_runs` / `deadline_seconds` 属运行预算（本计划）。
  两处都不得再各写一个"最多几个槽位"，否则冲突时无法裁决。
- 场景细化层级由冻结 scope 图计算；任务深度 root=0，所有工作/修复子任务计入，二者不是同一个量。
- 每次创建子任务、修复任务或启动 agent **之前**检查限额并原子预扣；并发和重启不能重置。
- 上限整数必须严格为正整数，拒绝 bool、字符串和 `2.9 → 2` 的静默转换；所有尺度必须有限、为正。
- 同 scope 的修复次数跨 task ID/标题累计；取消/新建同级“修复任务”不能洗掉次数。
- deadline 从获准开跑开始计算，包含排队、调用、外部命令、合并与验收；保持 UTC 记录并在进程内用单调时钟。
- 配额休眠不能把 8 分钟测试拖到下一晚；恢复若超 deadline，停并报告，需要用户明确批准新预算。
- `max_agent_runs` 是角色进程调用数，**不是底层模型 HTTP 请求数**。token 用量另沿用真实 run usage 累计。
- 调度器暂时返回/槽位延后不是完成；必须保持产品调度生命周期到终态或总上限。
- 达上限但仍有工作时，现有 task 状态用 `blocked` 等合适停止态，附 `scene3d_stop.reason`；不随意发明枚举。

错误分流：可修的部件/安装错误 → 同责任域的有限修复；权限、端点、冻结数据不可读 → 外部阻塞；
未知规则/无合法可完成计划 → 合同错误。后两类不得默认反复生成“完整场景修复”子树。

## 7. 父级合并与真实交付来源

**局部 CLI merge 不等于可信验收。** 它可产出诊断预览，但不能自行签发“已验收”身份。
编排器装配使用自己的权威任务绑定与 receipt，不接受 worker 自报的成功凭证。

每个已接受子交付的 receipt 至少绑定：root、contract/declaration 摘要、scope、producer task、
交付 revision、内容 hash、effective gate 与独立 verifier 结果、只读产物快照位置。
只有本树、本批准 scope、本次有效 revision 的已验收来源可合并。

装配规则：

1. 每个批准的直接 child scope 恰好一份有效交付；缺、重复、多余、跨 root、旧 revision 均拒。
2. slot 按冻结 owner 合并，不“后写覆盖先写”；输出顺序按冻结声明规范化。
3. hash 与解析使用同一次读取的字节；输入来自可信快照，不读取兄弟可变工作树来拼结果。
4. 装配不是单纯拼 JSON：同时复核 builder、参数、mount、精度域与该 scope 的全部责任检查。
5. 首次装配由独立确定性步骤在父验收前执行，让父 agent 能检查实际合并结果。
6. 父 acceptance 后的最后硬门禁再次核对 inputs/receipt/output hash 与全部责任检查。
   无有效本轮装配 receipt 的裸 `scene.parts.json` 不得冒充已装配场景。
7. 不透明几何无需全局焊接；安装接触、漂浮、错误比例、重叠及需要的组连通仍必须判断。

render 使用成熟 Blender，不写自有渲染器。灯光、摄影背景地板等 staging 不计入任务几何槽位，
也不能替代已声明物体；渲染记录绑定实际输入 parts/GLB hash 与真实 PNG。

## 8. 路径、旧件与发布一致性

所有引用、CLI merge 输入/输出、产物访问都受冻结工程根约束。
按实际解析路径及平台大小写规则去重，处理 symlink/junction；拒绝绝对路径、越界、路径别名覆盖声明。
contract、declaration、各 bundle 文件及非根 assembly 输出两两不同；根 output 仅与顶层 parts 相同。

合并先完整校验输入，再发布**新 generation**。parts 与 receipt 写入同一新 generation，
由一个提交标记/权威记录使其可见，不假设两次独立文件 rename 自动构成原子事务。
失败可以保留旧产物用于诊断，但不得把旧 generation 当成本轮成功交付。
界面分别标明“当前失败尝试”和“上次已接受产物”，不能用旧绿灯覆盖当前失败。

旧 v1 冻结快照可能用 `manifest` 字段，v2-r2 用 `contract`。兼容读取必须**两者都认**：
先按原摘要验证快照未被改动，再把旧 `manifest` 归一化成 v1 契约视图；
不重写冻结字节、不降低 v1 验收规则。读不出任一格式 → 阻断，不"当没有契约"。
当前实现只读 `contract`，旧快照会失败关闭——这是 M1 待补项，已列入 §12。
未经迁移/验证的原型 v2 不自动视为 v2-r2。未知 revision 阻断，普通非 scene3d 任务不变。

上述 hash/receipt 管住模型漂移与执行链，不宣称能防同一 OS 用户的恶意管理员篡改。
本机若需 danger-full-access，只用于明确授权的隔离测试工程，不悄悄修改全局设置。

## 9. GUI 只显示有来源的事实

节点报告字段名固定（前端与门禁共用，不得各写一套）：

| 字段 | 含义 |
|---|---|
| `required` / `passed` / `stage` | 是否受契约约束、结论、失败阶段 |
| `scope` / `scope_id` / `scope_kind` | `full` / `bundle` / `assembly` 与具体 scope |
| `expected_slots` | 本 scope 的责任域（由 scope 图算出，不是自报） |
| `deferred_checks` / `responsible_scope` | 延后的检查 id 与各自的责任 scope |
| `checks[] {id, ok, expected, actual, samples, why}` | 实际执行的检查 |
| `rejects[] {slot, field, expected, actual, rule}` | 结构化拒绝 |
| `declaration_sha256` / `actual_declaration_sha256` / `parts_sha256` | 冻结与交付身份 |
| `schema_revision` / `contract_version` | 契约版本 |
| `precision_exit` | `not_evaluated`（M1/M2 之间必须显式标明，不能静默省略） |
| `geometry_only` | 本门禁只判几何，不判渲染/审美 |

`deferred` 不等于 passed；缺字段一律显示"未记录"，不从 `status=done` 反推。

- 叶子展示“子集通过”，不展示“全场景已完成”；根有可信全量结果才展示装配完成。
- deferred 不计 passed；未知值显示“未记录”，不从 task.done、文件存在或模型 summary 反推。
- 当前盒体体积统计为**体积之和，含重叠**，不能标成精确实体体积。
- PNG 预览与 GLB 下载来自注册 artifact descriptor（task/root/revision/hash），不是扫描任意字符串拼绝对路径。
- 服务端验证所属任务、允许根、hash、文件头和尺寸，禁止任意文件读取、外部 URL 或路径穿越。
- 首版提供 PNG 和 GLB 下载，不强求 WebGL 查看器；如果引入查看器，优先成熟库而非自写渲染。

## 10. 验收矩阵（按里程碑归属；M1 未完成前不得拿 M2–M4 行当验收依据）

| 里程碑 | 项 | 正面 | 必须失败/有区别的反面 |
|---|---|---|---|
| M1 | 完整 owner | 每个活动 slot 恰好一个叶 owner | cell 分辨率变化导致裁切/重复交付 |
| M1 | scope 图 | C → C1/C2 → C21/C22 合法 | 环、孤儿、重复 bundle、未知 child |
| M1 | 主体子包 | 墙和塔可独立验，不要求兄弟窗棂 | 主体墙错误不能靠 deferred 隐藏 |
| M1 | 窗框检查 | 只读窗框几何，墙只供坐标 | 实心墙遮蔽缺失窗框不得通过 |
| M1 | 装配校验 | 输入齐全、slot 唯一、hash 记录 | 缺 bundle、重复 slot、多余输入 |
| M1 | v1 兼容 | 历史 manifest 快照能按原规则复验 | 裸 v2 / 未知 revision 被静默接受 |
| M1b | 身份 | 精确 scope 标记由控制端绑定 | 父 token 降级、无身份回退、子串碰撞 |
| M1b | 跨包安装 | 两包齐后祖先真正执行 | 根仍有 deferred 不得 DONE |
| M1b | receipt | inputs/output hash 与已验收来源一致 | 旧件、跨 root、未验收件被合并 |
| M2 | 精度 | bar 5cm 在 floor 5cm 合格，sink 2cm 按安装判 | bar 3cm 失败；改 floor 必须改变有效域与出口证据 |
| M2 | 退化与精度 | 两项分别报告 | bbox 中位数通过不等于精度达标 |
| M2 | 预算 | 创建/启动前检查，计数跨重启保存 | 同级修复或重启洗预算、超限仍创建 |
| M2 | 路径/发布 | 本根的新 generation | 别名覆盖声明、半发布或旧 parts 假通过 |
| M3 | UI | 显示有效报告和注册产物 | done 反推通过、伪路径泄露或 deferred 算 passed |

测试必须先确认量具会响应反例，不准删除断言、换恒真条件或把真实缺陷说成“只是测试问题”。
源码可读诊断不等于测试通过；单测/烟测不等于真实模型链。

## 11. 开发里程碑与模型分工

| 阶段 | 交付 | 收口标准 |
|---|---|---|
| M0 | 本文规则审查、接口与负面样例 | 分包/依赖/精度口径不矛盾（本轮完成） |
| M1 | **库内**：v2-r2 解析、scope 图与责任域、检查依赖、子集评估、确定性装配校验 | 分包责任与依赖回归全绿，v1 历史兼容，裸 v2 被拒 |
| M1b | **接线**：编排器 scope 绑定、父/叶权限、deferred 履责、可信 receipt | fake pass、父降级、未验收件合并全部被拒 |
| M2 | 精度计划编译（floor/importance/有效域/omitted）、退化守卫改名、事前预算与有限修复 | 改 floor 确实改变结果；上限前停止 |
| M3 | 指标与延后状态、注册图片与 GLB 下载 | 前端测试、构建与真实预览验证 |
| M4 | 真正的分件递归场景 | C 与 C2 两次父验收，三个叶子，全部硬门禁与真实 Blender 图 |

**顺序不可压缩**：receipt 依赖 scope 绑定（M1b），scope 绑定依赖库内 scope 图（M1）。
把 receipt 塞进 M1 会让 M1 无法交付，只能靠“看起来做了”收尾——这正是本仓库反对的做法。

设计 agent 提供冻结 spec 与验收样例；DeepSeek Flash 逐个里程碑实施。
涉及接口或验收形式变动时回到设计审查，不能由 worker 自改 spec 来过测试。
强模型独立收口，仍以实际测试、receipt、模型运行记录与截图为依据。

M4 示例是根 C、主体叶 C1、细部父 C2、门窗叶 C21、装饰叶 C22。
若每个节点规划一次，正常路径为 5 planner + 3 worker + 3 verifier + 2 acceptance = **13 次真实 agent run**；
synthetic 占位不计。若宪法也真实生成，另计该次；固定模板须标明，不假称模型生成。
每轮运行仍有 480 秒内层与 540 秒监督上限；超时只保留失败证据，不能承诺首轮一定完成。

## 12. 当前实施状态与已确认差距

**M1（库内规则）已完成的部分**（2026-10-19，101 个 scene3d 测试通过）：

- 契约：v1 历史语义不变；v2 必须带 `schema_revision: v2-r2`，裸 v2 与未知 revision 直接拒。
- scope 图：单根树校验（环、孤儿、多父、未知 child、未拥有 bundle、根覆盖不全，全部拒）；
  `importance`（0.5–4）与 `connectivity`（required / not_required）成为契约字段。
- 责任域：由 scope 图**计算**；每项检查的责任 scope = 依赖 owner 的 **LCA**；报告给出
  `deferred_checks` 与 `responsible_scope`；根重验全部检查。
- 检查依赖：`requires_slots` 与 `coordinate_slot` 分离；每个检查只读自己依赖的几何；
  缺依赖显式失败；坐标系从完整冻结声明解析（本包没交墙，也能给窗框检查定位）。
- 冻结期校验：叶子 scope 必须至少有一项真正由它执行的检查，否则"子集通过"是空的。
- 装配：确定性 merge、中间装配输出、原子发布；未知输入、缺输入、槽位不符、重复槽位全拒。
- 兼容：旧 v1 快照的 `manifest` 字段归一化后按原规则验收。
- 退化守卫与精度出口分离，改名 `degeneration_guard`，报告 `precision_exit=not_evaluated`。

**M1 仍未完成**：

- R-C7 检查域与合法自由参数域的相容性抽样（冻结期强制执行）。本轮只手工修正了线脚检查区域，
  通用校验还没有，同类"合法实现被判失败"仍可能复发。
- R-C8 每项检查的负样本证据（冻结期机器生成）。阈值写成恒真时仍然能通过。

**其它里程碑未开始**：M1b（编排器 scope 绑定、延后检查履责接线、可信 receipt）、
M2（精度计划编译、事前预算、有限修复）、M3（UI）、M4（真实分件递归运行）。

**本轮发现并修复的实现缺陷**（记录事实，不改写历史）：

1. 线脚检查按"整条带满外凸"取区域，两级线脚上半层合法地只有 0.55 倍外凸，导致合法实现被判失败；
   而更早那次 0.83 的"通过"其实是被塔身几何填满——两个方向的错同时存在。
2. 窗框检查借墙的坐标系，但 C21 视图里没有 wallS，坐标解析失败，跨包检查永远解不出。
3. `Precision.load(2.9)` 静默截断为 2，已改为严格正整数，拒绝 bool、字符串与小数。
