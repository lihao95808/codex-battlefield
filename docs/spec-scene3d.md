# spec：scene3d（3D 场景的声明 / 校验 / 渲染）

状态：**能力包、runner 工具投递与显式契约硬门禁已完成（2026-10-19）**。
仅有提示词指引不构成硬门禁；现在显式 opt-in 工程在叶子与父节点完成前由编排器独立判定。真实运行证据见 `VERIFICATION_STATUS.md` 的最新记录。

## 2026-10-19 真实端到端任务已完成

根任务 `root-71c57668cd` 最终 DONE，完整成功轮 **183.95 秒**。
五次真实 CodexExecRunner 调用：根 planner / 叶子 planner / worker / verifier 用 `deepseek-flash`，
父 acceptance 用 `gpt-6.1-sol`。另有一次 synthetic acceptance 占位，明确不计为模型验收。
没有下发 reference parts；worker 自行编写生成脚本、选择域内自由参数、执行 validate 与 Blender。
父节点复核已集成产物；叶子和父各记录一次 `scene3d_gate.passed`。
实际 PNG 900×620、646,347 字节；GLB 34,476 字节；28 项特征与强制连通通过。

成功测试用独立数据库/配置/工作树，未修改全局配置或导入当前 GUI。
Windows 受限沙箱确实拒绝 shell，因此测试在隔离目标中显式用 danger-full-access。
宪法是固定模板冻结，不计作模型生成；这是一层完整场景委派，不是深层构件递归。

实际失败推动的修正：

- 根节点未必有 worktree，门禁必须验证父的实际 integration target；叶子不能回退 seed。
- 隔离 CODEX_HOME 必须有正确模型 catalog，避免 unknown-model 降级；Flash 使用支持的 none 推理档。
- `describe` 只打印精简槽位与域，不填自由值；PowerShell 命令以 `&` 调用带空格路径。
- `run_tree` 一次调度返回不等于结束；真实 driver 保持产品延迟调度器活跃到终态，仍有整体超时。

可复现入口：`scripts/real_scene3d_probe.py`，模型调用仍全部由产品编排器执行。
每轮内层总预算 480 秒、外层监督 540 秒；失败保留日志，不用图片或参考烟测补齐成功。

## 2026-10-19 复核与 runner 接线补记（历史阶段）

上一轮的测试全绿并不能证明 fail-closed 全面成立。本轮确认并修复：

- 空 checks 不再通过；NaN/Infinity、非法几何尺寸、非整数计数被拒绝。
- 参考填写使用深拷贝，修改它不再改变声明中的受约束洞口。
- `evaluate` 必须检查装配连通性：从只看总 AABB 改为实心盒接触图（允许正面积面接触，不认边/点接触）。
- 退化采样盒被拒；删除上一版测试中 `assert ... or True` 的恒真断言。
- GLB 三个面的绕序反了，现已修复并逐三角形验证外法向。
- 每次渲染使用独立目录，非零退出阻断；验 PNG 文件头与请求尺寸，旧文件或同名大文本不再算产物。
- 修复容差带空间索引仍使用未扩张 AABB 的问题。

`runner_tools.ensure_scene3d_tool` 将包投递至工作区 `tools/_scene3d_runtime/`，
不依赖节点能导入 API 应用，也不要求节点的 PATH 上有应用虚拟环境。
`prompts.py` 根据任务标题/描述/验收中的 3D、Blender、三维标识提供工具。
worker 可验证/渲染；verifier 和 acceptance 只独立验证，不改声明或产物。
对不属于盒体装配的 Blender 任务，不套用内置石堡声明。

**当时的边界（已由后续章节补齐硬门禁和真实运行）**：仅节点工具投递和提示词接线时，
不能宣称任务战场已经跑通一次真实 Flash 递归构建。
复现实验：`api/.venv/Scripts/python.exe scripts/verify_scene3d_runtime.py`。
它采用参考输入、经投递工具验证并调用真实 Blender，不调用模型，也不创建任务树记录。

## 2026-10-19 显式契约硬门禁

工程 seed 中明确声明 `scene3d.contract.json`：

```json
{"version":1,"declaration":"scene.declaration.json","parts":"scene.parts.json"}
```

`start_project` 在模型工作前将原始声明与 contract 冻结到
`workspace/scene3d_contracts/<root_id>/`，根 metadata 绑定 snapshot 摘要。
叶子 `_apply_verifier_result` 在模型 pass 转 DONE 前、父 `_accept_parent` 在 acceptance pass 转 DONE 前，
使用应用内可信 `gate.evaluate_contract` 独立检查冻结声明和当前交付工作树。
读盘异常、声明变动、冻结快照损坏、几何错误均不得完成；复用既有 repair 路径反馈完整场景修复子任务。
模型原始 run 保留，最终结果另附 `scene3d_gate`，事件为 `scene3d_gate.passed/repair_required`。
恢复 verifier 与宪法再验证经过同一个最后边界，不能绕过。

普通工程没有 seed contract 时行为不变；contract 已冻结后，worker 删除工作树 manifest 不能退出门禁。
目前是 **完整场景交付** 契约，不支持每个部件叶子分别只交部分槽位；未来必须显式增加子集契约，
不能把“所有叶子必须完整交城堡”当作深层构件递归方案。
门禁仅检查几何，PNG/GLB交付在真实 probe 中独立核对，不将几何 pass 等同于渲染或审美通过。

专项 `tests/test_scene3d_gate.py`：23 passed，含 fake pass、恢复入口、父已集成工作树、根 integration target、篡改与普通工程不变。

## 1. 要解决的问题

让"DeepSeek V4.1 Flash 能不能通过结构化多次构建出细化 3D 世界"这件事**由战场自己判定**，
而不是靠某个会话里手写的一次性脚本。判据必须：

- 便宜：纯 Python、无数值栈依赖（战场 API 依赖只有 FastAPI/SQLAlchemy/Pydantic）；
- 可重放：同一份输入给出同一个 IoU（确定性采样）；
- fail closed：拿不到、采不到、认不出 → 一律判失败，不"按默认值继续"。

## 2. 模型：实体 = 一组轴对齐实心盒

不做 CSG 减法。洞（窗/门）在 `builders.wall_panel` 里被**分解成盒带**。
这样三条操作都变成常数级：

| 操作 | 实现 |
|---|---|
| 点是否在实体内 | `any(box.contains(p))` |
| 体积 | 每盒解析体积（重叠部分由指标吸收） |
| 导出 GLB | 每盒 12 个三角形（`glb.write_glb`，纯标准库） |

代价：重叠盒的内部面留在模型里（不透明渲染下不可见）；不追求水密性。
这正是"搭接不切缝"的取向——判据只看"有没有料、在哪"，不要求水密。

## 3. 声明（L0 产物，只读）

`Declaration` = `slots[] + checks[] + size_m`。

- `slots[]`：`slot / builder / mount{origin,rot} / expect{...} / free[] / domain{...}`
  - `expect`：被钉死的参数，逐字核对（dims ±0.02m，位置 ±0.03m）
  - `free`：允许 worker 自选，但**必须有域**（缺域直接判声明不合法）
- `checks[]`：特征校验清单，是**数据不是代码**。规则目前四种：
  `region_empty` / `region_filled` / `region_between` / `runs_along_x`；
  区域默认在槽位局部系，`space: "world"` 时用世界系。

内置场景用 `castle.castle_declaration()`：20 个槽位、28 条特征校验。

## 4. 判定流程

```
validate_parts(decl, parts)     # ① 合规：槽位/数量/builder/参数/洞口/域
   └─ 有拒绝 → stage=schema，直接返回（不建几何，省时间也让拒绝更聚焦）
place(decl, parts)              # ② 装配：mount 变换 + AABB + 连通性
run_checks(decl, placed)        # ③ 几何：特征区域采样
evaluate()                      # 串起来，返回 {ok, stage, rejects, checks}
```

拒绝结构统一为 `{slot, field, expected, actual, rule}`，`field` 形如
`params.w` / `params.openings[2].z` / `builder` / `slot`。
**同一套字段口径**很关键：worker 的回灌提示直接用它，口径混用会让模型反复改错地方
（本轮实测踩过一次：builder 不符原本报 `/parts/0/builder`，与 `params.w` 不是同一种写法）。

## 5. 判据的失效方向（AGENTS.md 硬约定 5）

| 判据 | 反常时倒向 | 默认动作 | 反面自检 |
|---|---|---|---|
| 声明不可解析/字段缺失/rot 非法 | **阻断** | 报错退出码 3 | `test_declaration_rejects_illegal_mount` 等 |
| 自由参数缺域 | **阻断** | 声明不合法 | `test_declaration_rejects_free_param_without_domain` |
| 槽位缺/多/重/builder 不符 | **阻断** | stage=schema，退出码 2 | 参数化用例 7 例 |
| 洞口数量或位置不符 | **阻断** | `opening-count` / `opening-missing` | 同上 |
| 特征区域采不到样本 | **阻断** | `0 个样本` 直接判失败 | `test_empty_region_fails_closed` |
| 未知 check 规则 | **阻断** | 判失败，不跳过 | `test_unknown_rule_fails_closed` |
| 构件数 0 / builder 抛错 | **阻断** | `AssemblyError`，退出码 3 | `test_missing_geometry_cannot_be_assembled` |
| Blender 未安装 | **阻断** | `RenderError`，退出码 3 | `test_blender_discovery_fails_closed` |
| Blender 退出码 0 但没出图 | **阻断** | 逐张核对存在且 ≥5000 字节 | `render()` 内 `missing` 分支 |

**最容易恒真的两条**（本轮专门写了反面自检）：

1. `region_empty`：区域为空 → 比例恒为 0 → 恰好等于"空洞"的合格值。
   所以采不到样本必须判失败。
2. 检查条数：如果某条 check 因解析失败被"跳过"，整体仍可能全绿。
   所以 `test_reference_parts_pass_every_check` 断言 `len(result["checks"]) == len(decl.checks) + 1`（额外强制装配连通性）。

## 6. 命令与退出码

```powershell
cd api
.\.venv\Scripts\python.exe -m app.scene3d declaration --out castle.declaration.json
.\.venv\Scripts\python.exe -m app.scene3d reference   --declaration castle.declaration.json --out parts.json
.\.venv\Scripts\python.exe -m app.scene3d validate    --declaration castle.declaration.json --parts parts.json
.\.venv\Scripts\python.exe -m app.scene3d metrics     --declaration castle.declaration.json --parts parts.json
.\.venv\Scripts\python.exe -m app.scene3d render      --declaration castle.declaration.json --parts parts.json --out out/ --views hero,gate,top
```

退出码只有三种，门禁只认这三种：`0` 通过 / `2` 判定不通过 / `3` 工具自身出错。
`render` 额外支持 `--blender`、`--width/--height`、`--samples`；
Blender 发现顺序：`--blender` → `CODEX_BATTLEFIELD_BLENDER` → `PATH` → 常见安装目录（取版本最大）。

## 7. 未做（下一刀）

1. **未提供独立 HTTP 端点**：真实任务通过现有 Orchestrator/Codex runner 与工作区工具完成。
2. **前端专用几何指标卡尚未接**：现有图片面板可以读产物，但本次隔离测试树未导入当前 GUI。
3. **深层分件递归未完成**：当前所有交付叶子适用完整场景契约，后续需要明确槽位子集与父级装配规则。
4. **全局精度出口未成为通用硬门禁**：本例是固定 20 槽位/指定窗棂宽度，不等于任意世界精度自动收敛。
5. **渲染不进默认单测**：真实 Blender 在有界集成/模型任务中运行；使用独立输出目录、非零退出阻断、PNG 头/尺寸核对。
   文件大小和头正确不证明审美质量，真实截图还要查看。

## 8. 实测读数（2026-10-19，本机）

| 项 | 值 |
|---|---|
| 参考填写 20 槽位 | 138 个盒 / 体积 484.8 m³ / bbox 12.4×10.4×11.2 |
| `validate`（28 条特征） | 全绿，**116 ms** |
| `metrics` vs 参考答案 | IoU **1.0** / spill 0.0 / gap 0.0 |
| `render --views hero` 900×620 / 24 samples | GLB 35,936 B → PNG 643,853 B，**Blender 5.2.0** |
| 后端全量 pytest | 见 `VERIFICATION_STATUS.md`（本文件不写死数字） |
