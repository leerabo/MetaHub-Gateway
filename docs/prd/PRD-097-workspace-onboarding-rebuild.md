# PRD-097 个人与企业初始化向导重构

状态：已完成
阶段：S2-激活体验
优先级：P0
依赖：PRD-093/094、PRD-095、现有 Provider/Model/Project/VirtualKey/Usage

## 1. 目标与边界

本条用 WorkspaceSession 和真实资源事实替换旧 `/tenant/onboarding/summary` 的 active 行推断。个人 Owner、
企业 Owner/admin 和获授权 developer 登录后看到按依赖排序的八步初始化中心；每一步显示来源、当前状态、
可执行动作和失败诊断，完成后仍可点击查看或重新检查。

向导不创建 seed Provider/Key/模型/Virtual Key，不生成演示响应，不把 default dev Environment、
`local-placeholder` Credential、active 占位行或历史 seed 模型当成用户完成。Provider 完成必须存在真实可
解密凭据或免密本地 Endpoint，并有成功健康探测；最终激活必须有真实 Gateway 成功调用事实。

本条不迁移全部 63 项企业 Console，也不新增平台角色、套餐或云 Gateway。为完成八步操作，只给明确
allowlist 的企业资料、Environment、Provider/Key、本地 Endpoint、Model/Alias、Project/Application、
VirtualKey API 增加 WorkspaceSession 认证适配；其他 Legacy Bearer API 保持原边界，不能借“向导兼容”
全局接受 Cookie。

## 2. 八步模型

固定步骤和依赖：

1. `workspace_profile`：个人资料确认或企业资料确认。
2. `environment`：确认一个 active dev Environment；system-created 环境必须由用户确认，不能自动完成。
3. `provider`：真实 Provider/Endpoint、认证材料和成功探测。
4. `model`：与已验证 Provider 绑定的 active 非 seed 模型。
5. `model_alias`：dev 环境 active alias 指向已验证模型。
6. `project_application`：active Project + Application，来源可证且 owner 一致。
7. `virtual_key`：active Virtual Key 绑定上述 Environment/Project/Application；seed key 不计。
8. `first_call`：使用该 Workspace 的有效 Virtual Key 和模型别名完成真实 Gateway success，留下 request_id
   与 Usage/ledger 安全事实；不保存 Prompt/Response 到向导。

personal 与 organization 使用相同步骤 ID 和 API 结构，但展示文案、profile 校验和可操作角色不同。个人
Workspace 的 owner 拥有完整初始化权限；企业 owner/admin 可做全部步骤；developer 只能操作技术步骤
2..8，不能确认企业法定/组织资料。页面不显示表名、migration、PRD、tenant、revision hash 或内部 source
row ID。

## 3. 来源与状态

每步返回：`id/order/title/status/source/action/navigation_target/blocking_code/last_checked_at`。

- status：`current/blocked/complete/deferred/needs_attention`。客户端不能提交 complete。
- source：`system_created/user_confirmed/verified/legacy_seed/none`。
- `system_created` 表示系统准备了可确认基础，不等于用户完成；`legacy_seed` 永不计完成。
- `user_confirmed` 只用于 profile/environment 等主观确认步骤，并绑定 actor Membership revision。
- `verified` 只由后端根据资源 owner、来源、密文/探测和调用事实计算。
- `deferred` 只是当前用户暂后处理的 acknowledgement，不计完成，也不解锁有依赖的下一步。
- 底层事实曾完成后变为 disabled、损坏、撤销或探测过期时，状态转为 `needs_attention`，Workspace activation
  回到 incomplete；历史 completion event 保留，不能覆写。

向导完成数只统计当前仍成立的八个步骤。空 Workspace 初始 completed=0；system-created dev 只让
environment 进入 current/可确认。进度 API 不返回 Provider 名称、Endpoint、Key 前缀、模型名、项目名、
Virtual Key、Prompt、Response、Token、tenant_id 或内部资源 ID。

## 4. 数据模型

0089 新增：

- `onboarding_progress`：workspace/tenant、kind、generation、completed count、status、projection hash、
  evaluated_at、activated_at 与 revision。
- `onboarding_step_facts`：每个 workspace+step 的 current status/source、evidence kind、安全 evidence hash、
  source generation、首次/最近成立时间和 last_checked_at。
- `onboarding_acknowledgements`：confirm/defer/reopen 的 append-only actor、Membership revision、step、固定
  reason code 和时间，不保存表单正文。
- `activation_checkpoints`：每次 recheck 的八步 bitset/hash、结果、触发 actor/source、request/trace 和时间；
  append-only。
- `onboarding_events` 与 `onboarding_event_outbox`：step completed/reopened、workspace activated/deactivated 的
  安全事件和投递引用。

所有表同时保存 workspace_id/tenant_id 并以不可变 binding 复合 FK 证明 owner；actor 操作绑定 active
Membership 和 revision。StepFact/Progress 是可更新投影，Acknowledgement/Checkpoint/Event 不可 UPDATE/
DELETE。projection hash 只对固定布尔/枚举/时间规范值计算，不含 Secret、名称或正文。

## 5. 旧库与 seed 分类

0089 不删除任何客户资源。回填按可证明来源分类：

- `workspace_environment_origins=system_created|legacy_existing` 只建立 system/legacy source，不自动 complete；
  `user_created` 可进入后端验证。
- Provider Credential 的 `local-placeholder:*` 永远是 legacy_seed；只有 AES-GCM 密文能在当前 Provider Key
  根密钥下成功解密，才具备真实凭据候选资格。密文、明文和解密异常不进入 DTO/日志。
- Provider/Model/Alias/Project/Application/VirtualKey 的空 created_by、已知 seed owner、seed run 对应的
  确定性记录或缺失安全 owner 均标记 legacy_seed；用户创建且完整绑定的记录才进入 verified 计算。
- 历史 success request 若由 seed Virtual Key、缺失 owner、无 Usage/ledger 对账或发生在 Workspace 创建前，
  不计 first_call。

迁移只写来源/初始投影，不根据 `status='active'` 生成 completion event。旧客户真实资源若因历史字段不足
无法证明来源，状态为 needs_attention，并允许用户通过当前页面重新确认/创建；禁止静默猜测。

0089 为后续新 Workspace 安装八步初始事实 trigger，PRD-093 创建服务在 schema 可用时也显式初始化同一
组事实；唯一 `(workspace_id,step_id)` 约束保证一次。旧 0088 运行时和冻结迁移夹具没有 0089 表时继续
使用旧摘要能力，Identity 账号摘要和 Workspace 创建不得因 capability 不存在而查询新表失败。

## 6. 后端事实求值

事实求值在一个只读一致快照内按 WorkspaceSession 恢复的 workspace/tenant 运行：

- profile：organization profile 非 conflict 且有当前 owner/admin 的 confirm acknowledgement；personal profile
  由 owner confirm。
- environment：active dev Environment + 合法 origin + 当前 owner/admin/developer confirm；system-created
  未确认不完成。
- provider：active、非 seed、owner 一致；官方/云 Provider 至少一个 active Credential 可成功解密，且
  Provider `last_success_at` 不早于当前 credential/provider revision；免密 local Endpoint 要求 auth=none、
  health=healthy、last_success_at 存在。失败/过期探测不完成。
- model：active 非 seed model 绑定当前 verified Provider，provider_model_id 与能力结构合法。
- alias：active dev alias 绑定 verified model 和当前 Environment。
- project/application：两者 active、非 seed、复合 owner/FK 与 source 合法。
- virtual key：active 非 seed key，Environment/Project/Application 均属于本 Workspace，不返回明文或 hash。
- first_call：success request 绑定该 key、alias/model、Workspace tenant，时间晚于相关资源有效版本，并有
  对应 Usage/ledger 安全事实；模型上游正文不参与求值。

任一必需表、密钥、FK、时间、状态或证据损坏时失败关闭为固定 onboarding unavailable/needs_attention，
不得用旧投影或 active 行补齐。GET 可实时计算并返回当前投影但不写事件；`POST .../recheck` 在写事务中
重算、CAS 更新 Progress/StepFact、追加 Checkpoint/Event/Outbox。页面初始化先 GET，用户或资源操作成功后
调用 recheck。

## 7. 步骤操作

`PATCH /workspaces/current/onboarding/steps/{step_id}` 只接受：

- `{action:"confirm", reason_code}`：仅 profile/environment，且后端当前事实达到可确认条件；不能确认
  provider/model/key/call。
- `{action:"defer", reason_code}`：记录稍后处理，不计完成、不越过依赖；任何获授权操作者可 defer 当前步。
- `{action:"reopen", reason_code}`：只对 user-confirmed profile/environment 清除当前 acknowledgement 投影并
  写 append-only reopened 事实；不删除 Workspace/Profile/Environment 或其他资源。

请求要求 WorkspaceSession、Origin、workspace CSRF、If-Match progress revision 和 Idempotency-Key。服务端
不接受 workspace_id、tenant_id、actor、source、status、completed 或 evidence。相同 key/相同 body 重放，
异 body 冲突；Membership 降权或 Workspace suspend 后下一请求立即失败。

## 8. WorkspaceSession 资源 API allowlist

为让向导中的操作真实可执行，新增 `require_workspace_or_legacy_permissions` 认证依赖，仅替换以下路由组：

- current Workspace profile / Environment。
- Provider、Provider Credential 和 local Endpoint 创建/探测。
- Model 与 Model Alias 创建/读取。
- Project/Application 创建/读取。
- Virtual Key 创建/读取。

依赖恰好接受一种凭据：Legacy Bearer 或 WorkspaceSession。两者并存、Platform/Support/Global Cookie 被用于
这些路由时固定拒绝。WorkspaceSession 分支从数据库恢复 AuthContext，按原 permission code 执行，不按
Workspace role 名补权；unsafe method 在业务 body 解析前强制 Origin + workspace CSRF。服务层任何依赖
Legacy `sessions` token/JTI 的二次认证必须改为主体感知的 transaction refresh，不能把 Workspace token
编码成 Legacy JWT 或跳过撤权复核。

allowlist 外的所有 tenant API 仍保持现状。这个适配不是平台支持入口，也不允许客户端提交 tenant_id。
运行时日志只记录 credential domain 枚举，不记录 Cookie/Bearer。

## 9. HTTP 合同

| 方法与路径 | 主体 | 边界 |
| --- | --- | --- |
| `GET /workspaces/current/onboarding` | active WorkspaceSession | 八步安全投影、progress revision，不写事件 |
| `PATCH /workspaces/current/onboarding/steps/{step_id}` | owner/admin；技术步允许 developer | confirm/defer/reopen，客户端不能标 complete |
| `POST /workspaces/current/onboarding/recheck` | owner/admin/developer | 重算、CAS 持久 projection/checkpoint/event |
| allowlist 既有资源 GET/POST/PATCH | WorkspaceSession 或 Legacy Bearer | 复用原 DTO/权限/owner；Cookie 写操作加 CSRF |

GET/PATCH/recheck 和 allowlist 私有响应均 `no-store, private`。错误统一包含 request_id/trace_id，但不携带
底层表、SQL、密钥解密、Endpoint、Provider 错误正文或“某 Secret 是否存在”的 oracle。步骤 path 和
action 使用固定枚举，duplicate JSON key、重复 Header、超长 body、query owner 参数固定拒绝。

## 10. 激活与事件

八步在同一次 recheck 中全部 complete 时，若上一 Progress 不是 active，则追加唯一
`workspace.activated` checkpoint/event 并设置 activated_at。后续任一事实失效时追加
`workspace.activation_lost`，但不冻结 Workspace、删除资源或伪造客户故障；恢复事实并 recheck 可生成新
generation 的 activated event。

`onboarding.step_completed/reopened` payload 只含 workspace internal ID、step ID、source、generation、
schema version 和安全 evidence hash。Provider Key、Virtual Key、Endpoint、模型、项目、request body、
Prompt/Response、邮箱和自由文本不进入 Event/Outbox。向导事件不计为模型 Usage 或客户调用。

## 11. 旧接口兼容

`GET /tenant/onboarding/summary` 在 N/N-1 期间保留 Legacy Bearer 路由，但改为调用同一事实求值器并映射旧
DTO；不再沿用旧 `_load_readiness_facts` active EXISTS。旧 DTO 的 target hash 仍指向既有企业 Console，
但 seed/placeholder 不能恢复为 complete。

旧客户端不能执行 confirm/defer/recheck，也不能获得 Workspace activation 事件。运行时 feature flag 可
把首页退回旧呈现，但事实求值、seed 排除和安全事件不得回退；否则会恢复假完成。

## 12. Console 交互

Identity Workspace 页面新增“初始化中心”，作为真实 WorkspaceSession 首屏，不制作营销 hero 或说明卡片。
页面使用紧凑步骤列表、稳定进度条、当前步骤操作区和可展开诊断；已完成步骤仍是可点击按钮，可查看当前
事实并重新检查。source 对普通用户显示为“系统准备/你已确认/已验证/历史配置/未配置”，不显示内部 code。

每个技术步骤在当前页面提供实际表单或打开对应 allowlist 资源编辑面：

- Provider Key、Virtual Key 只在提交闭包和一次性结果内存中出现；先清 DOM，再发请求；不进 snapshot、
  URL、BroadcastChannel、Clipboard 之外的持久介质或 Storage。
- Provider 探测显示固定成功/失败与 request_id，不展示第三方原始异常或 Endpoint 全文。
- 首次调用使用用户刚创建或明确提供的 Virtual Key，在 Gateway 真实返回 success 后 recheck；不生成演示
  response，页面卸载立即清 Key、Prompt 和 Response 内存。
- defer/continue/retry 使用常规按钮和状态，不使用阻塞式 confirm；resource action 完成后 abort 旧 GET、
  递增 generation 并 recheck，迟到响应不能回填。

个人与企业文案、profile 操作和权限不同；developer 看不到企业资料确认控件。390/1280/1440 无横向溢出、
裁剪或重叠，键盘可完成八步、展开诊断、回到已完成步骤和错误恢复，焦点回到触发控件/新状态标题。

## 13. 迁移与回滚

0089 只从完整 0088 升级，创建 Progress/StepFact/Acknowledgement/Checkpoint/Event/Outbox，并确定性回填
system/legacy source。升级后复核每个 active binding 恰有 8 个 step fact、completed=0 或由可复算真实
证据支持、placeholder/seed completion=0、owner/FK/integrity 合法。

pristine down 只允许全部投影等于迁移初始值、无 acknowledgement/checkpoint/event/outbox、无 activated
事实、无 0089 后 Workspace onboarding 事实。任何 user confirm、defer、verified resource checkpoint 或
activation 存在时原子阻止。运行时回退可显示旧页面，但必须继续使用新事实求值和 seed deny overlay，
不得删除客户资源或恢复假完成。

## 14. 性能、观测与隐私

- 向导 GET/recheck warm p95 `<300 ms`；步骤切换立即更新本地选中态，页面可交互 p75 `<1.5s`。
- 求值最多一次解密每个候选 Credential，并先用状态/来源/格式筛选；候选数量有界，异常统一失败。
- 日志只含 workspace/step/generation/status/source/reason code/request_id/trace_id，不含 tenant、名称、
  Endpoint、Key/Token、模型/项目名、Prompt/Response、credential ciphertext 或第三方异常。
- 前端 Storage API 调用为 0；敏感输入、一次性 key、请求/响应正文不进入 DOM snapshot、截图或证据。

## 15. 专项测试

覆盖 0088->0089/fresh/down/up、空 Workspace completed=0、personal/organization 差异、system-created env
确认、legacy seed/placeholder 排除、真实 AES-GCM Credential 解密、no-auth local Endpoint、probe success/
failure/stale、Model/Alias/Project/Application/VirtualKey owner 链、真实 first call+Usage、事实失效与重新激活、
confirm/defer/reopen CAS/幂等、owner/admin/developer 权限、allowlist WorkspaceSession/Legacy Bearer 与其余
Platform/Support/Global/混合凭据负矩阵、CSRF/Origin、迟到响应/动态撤权、八步真人鼠标、三视口/键盘/
焦点、p95、敏感 canary 0 命中及旧 onboarding/provider/model/project/key 回归。

## 16. 第一轮自审：事实、seed 与兼容

结论：通过，P0/P1 已修正。

- P0：旧向导只查 active 行，会把默认 Environment、placeholder Provider Key、seed Model/VirtualKey 当成
  完成。修正为显式 source、Credential 可解密、probe success、owner 链和真实 call+Usage 联合证明。
- P0：让客户端 PATCH `status=complete` 会把 UI 变成授权来源。修正为客户端只提交 confirm/defer/reopen
  意图，技术步骤永远由后端求值。
- P0：为方便向导而让全部 tenant API 接受 Workspace Cookie，会扩大 CSRF 和兼容爆炸半径。修正为明确
  route allowlist、单凭据解释、unsafe Origin+CSRF 和主体感知 transaction refresh。
- P1：历史真实资源缺 created_by 时若静默算 seed 会阻塞客户，若静默算完成又会造假。修正为
  needs_attention，可由用户当前重新确认/创建，绝不删除旧资源。
- P1：GET 若写事件会因刷新/爬虫重复副作用。修正为 GET 只返回实时投影，显式 recheck 才 CAS 持久化和
  发事件。

## 17. 第二轮自审：流程、撤权与敏感信息

结论：通过，P0/P1 已修正。

- P0：Provider 只验证有 Credential，不验证密文和探测，会把损坏/placeholder Key 当可用。修正为短生命
  解密 + 当前 revision 后成功探测；异常固定映射且不形成 Key oracle。
- P0：旧 success request 不绑定当前 Key/Alias/Usage，可能用 seed 或其他 Workspace 调用激活。修正为
  tenant owner、资源版本、时间和 ledger 事实完整关联。
- P0：WorkspaceSession 降权后前端旧表单若继续提交会越权。修正为每请求 Membership/permission revision
  复核、操作后 generation/abort、迟到响应丢弃。
- P1：defer 若计完成或解锁依赖，会成为“跳过真实接入”。修正为 defer 仅记录稍后处理，不增加 completed，
  依赖步骤仍 blocked。
- P1：已激活后底层资源撤销若继续显示 100%，会掩盖真实故障。修正为当前事实动态求值、
  activation_lost 事件和可恢复 generation；不自动冻结 Workspace。
- P1：一次性 Provider/Virtual Key 或首次调用正文若进入 snapshot/截图，会破坏敏感审计。修正为 DOM 先清、
  closure 内存、Storage 零调用和证据白名单；验收只保存状态、计数、request hash 和截图安全摘要。

两轮修正已写回本设计，编码不得弱化这些边界。
