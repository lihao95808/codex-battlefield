# 父节点复盘与经验检索（2026-10-19）

## 1. 复用优先，不重造记忆引擎

经验引擎选择 OpenViking 0.5.0，战场只做 HTTP 适配、证据账与流程接线。
它负责案例 / 轨迹 / 经验提取及检索；战场已有 SQLite 保存召回快照、父节点复盘与投递状态。
不复制上游实现、不把第三方源代码塞进本仓库。上游主项目当前是 AGPLv3；部署、修改与再分发
应核对对应版本许可证，HTTP 适配不是免除所有许可证义务的声明。

规划器在首次调用之前读取相关经验，而不是已经分拆完成才去查；向下派发时复制召回 URI、
内容 SHA-256 与当时内容，子节点继续规划会另做自己的查询。经验始终是非可信建议，不能覆盖
用户要求、项目宪法或验收标准；读过不代表采用，完成也不代表经验导致成功。

复用评估在分拆前执行：先看工程已有代码、组件、资产与工具，再用已授权且可用的外部搜索
做有限调查（最多两次查询、三个候选）。比較搜索、授权、适配、集成、验证与维护总成本和
自研成本。没有网络 / 搜索工具应写未知，不能编造没有现成方案；小任务不强制全网搜索。
美术资产检查风格、格式、骨骼与授权，代码检查接口、依赖、许可和维护状态。
选择写进已有规划结果的整合 / 验证计划及相关子任务说明，不增加另一套递归框架。

## 2. 接线与存储

- `api/app/experience_provider.py`：显式配置、限时限量 HTTP 请求、完整内容读取和哈希。
- `api/app/parent_learning.py`：召回快照、父节点复盘、SQLite 投递箱及后台处理状态查询。
- `api/app/models.py` 的 `LearningRecord`：无任务外键，清理任务不连带删除复盘证据。
- `api/app/orchestrator.py`：规划前召回；子节点继承；整合、acceptance 和最终门禁后分别记账。
- `api/app/prompts.py`：经验的非可信边界、复用优先规则、父节点结构化复盘。
- `web/src/ExperiencePanel.tsx`：在左栏“日志与诊断”下按需展开，不增加默认首屏请求。

父验收输出的 `retrospective` 可以是 null。非空时分原因假设、其他可能原因、候选建议与
采用反馈；事实另存于检查结果和运行 ID 中。不强迫每次成功编出一个经验。
独立产品门禁在 agent 声称 pass 后仍然执行；候选经验不自动晋升为硬规则。
前置门禁、产品 / 集成门禁及债务门禁的拒绝另记 failed 事实，不能只保存 agent 的预门禁报告。
复盘写失败会留提示，但不会把已经通过产品验收的任务改成失败。

投递箱状态区分：disabled、pending、sending、send_unknown、pending_extraction、
extraction_completed、extraction_failed、error。接受归档不等于完成提取；完成后台提取也
不保证生成了经验，更不是客观有效。`learned` 不被自动写成 true。
发生无法确认的发送结果不自动重试，避免重复归档。显式重试函数保留原记录。
发布前用 SQLite 条件更新 pending → sending 原子认领，跨进程失败者不发送；崩溃后 sending
不自动重发。已存在但未归档的远端消息不靠数量推断与本次相同，不自动追加 / commit；
需要人工检查，避免不同复盘被误当成同一批。记录绑定 user、peer 标签和提供方 URL 指纹，配置变更不能自动把旧工程材料
上传到另一个目的服务。身份 / 目的指纹不匹配时留下错误或不轮询，不默默改投。
复盘证据预留 30k 字符、投递回执单独预留 8k，回执增长不能重新裁剪已发送证据；
发送前读取已持久化快照，保存实际出站参数哈希。近上限证据与大回执有专项回归测试。

只读查看入口是 `/api/learning/records`，可按 task_id 筛选。页面分别展示事实、分析假设、
召回来源和当时的内容指纹，不将空结果解释为没有经验。

## 3. 配置与安全

默认关闭外部记忆功能，普通 mock / 本机门禁不产生模型开销。
显式设置 `CODEX_BATTLEFIELD_MEMORY_URL`、`CODEX_BATTLEFIELD_MEMORY_USER_ID`，
需要认证时设置 `CODEX_BATTLEFIELD_MEMORY_API_KEY`；可选 `CODEX_BATTLEFIELD_MEMORY_PEER_ID`
只做关联标签，不作为工程隔离或授权边界。密钥不存入快照或日志。

每个工程应配独立的 OpenViking user 与用户 API key；战场查询明确的该用户 experiences
子树，发布时检查返回的会话 user 身份。不能用 ROOT key 做日常任务记忆。
没有真实认证的开发服务中，user 命名空间只是分类，不是访问安全保证。

OpenViking 服务单独部署，战场不自动读取个人记忆配置，不自动连接聊天记忆，更不上传整个
工程。投递的目标、验收报告与原因假设仍可能包含工程信息，启用前应确认目的服务可信及
数据授权；脱敏只是一层保护，不承诺识别所有隐私。外部提取 / embedding 有独立费用，
目前不纳入战场 Codex 运行金额硬闸，必须给外部服务自身设置预算。

## 4. 现成库实测与纠正

测试下载、缓存、虚拟环境和隔离数据全部在用户允许的 D 盘测试目录，未触碰真实记忆。
OpenViking 0.5.0 原生 Windows wheel 安装通过，基础安装 171 个包；虚拟环境逻辑体积约
702 MB，缓存约 661 MB。不是一个轻量 SQLite 插件。

默认 embedding 会下载约 48 MB GGUF，但缺少可选 llama-cpp-python 时失效；health / ready
仍返回 200，debug health 已显示失败。因此不能拿 health 200 当经验闭环通过。
使用上游官方 CPU wheel 补齐本地 embedding 后，以合成叙事（不是实际 2D / 3D 任务）测试：

- CSV BOM 案例生成真实 case、trajectory、experience；提取约 42 秒。
- SQLite 案例后台完成但生成零记忆，明确不能声称学到了。
- CSV 相关查询召回经验约 0.859；无关香蕉蛋糕查询不返回该经验。
- 停服务再启动后，经验内容与相关召回结果保留。

最重要的纠正：peer-only 经验提取生成零记忆。该版本 cases 不允许 peer-only 提取，经验
训练输出在 user experiences 根下；actor-peer 过滤也保留 user-level 记忆。
因此初始 peer 子树方案被实测推翻，改成工程独立 user 的明确根。开发模式不同 user 的
home 查询内容不同，但显式跨 user 读取仍可成功，因为 dev 是 ROOT；生产必须配置用户 key。
没有宣称已实测生产 ACL，也没有宣称这几条合成材料能提高实际游戏任务成功率。
真实适配回放还发现中文经验文件名被 ASCII 路径校验错误排除：上游生成了中文名称，
查询正常却返回零条可用内容。现改为身份严格校验、文件名允许合法 Unicode，仍拦截
跨用户、路径穿越、控制字符及编码绕过。修正后用战场真实适配器回放：default 用户返回
一条完整经验及 SHA-256，另一个空的测试用户返回零条，均为 ok；只读回放未额外调用生成模型。
此例说明假服务单测不替代实际库回放。

来源：
- [OpenViking 0.5.0](https://github.com/volcengine/OpenViking/tree/v0.5.0)
- [会话与经验管线](https://docs.openviking.ai/en/concepts/08-session)
- [经验读取与结果关联不是因果证明](https://docs.openviking.ai/en/api/19-agent-evolution)
- [认证](https://docs.openviking.ai/en/guides/04-authentication)

## 5. 判据

`api/tests/test_experience_provider.py` 验 HTTP 范围、错误、不可信内容、限量、身份与异步状态。
`api/tests/test_parent_learning.py` 验脱敏、幂等、出站箱、未知结果、检索故障和反思边界。
`api/tests/test_learning_prompts.py` 验提示词真实包含经验、上下文上限、复用优先和严格输出形状。
`api/tests/test_parent_learning_wiring.py` 驱动真正规划与整合入口，断言经验影响分拆、逐层读取、
内容继承、独立门禁仍能推翻 pass，以及复盘 SQL 写失败不丢掉产品交付。
`web/src/experienceView.test.ts` 验不把归档 / 提取完成读成已学习。

测试失效方向：无数据 / 异常记未知，不晋升；读取不可用不默认为没有经验；存储失败不放宽
产品验收。现成检查没有替代真实引擎项目验证，下一步用固定 2D / 3D 项目比较策略收益。
