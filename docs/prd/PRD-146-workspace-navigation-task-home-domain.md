# PRD-146 Workspace 导航、任务首页与高级入口合同

状态：编码前设计冻结。Migration 固定为 `0138`，要求 current head 为 `0137`。本条把企业 Gateway 工作区重组为
后端权威的角色导航、真实任务首页和受 entitlement 约束的高级入口；不提前实现 PRD-147 的全局迁移切换、外部旧 API
tombstone 或 SchedulerRegistry。

## 1. 产品闭环与非目标

Workspace 工作区只展示当前成员职责内的企业 Gateway 能力。Platform 增长、客户成功、伙伴、Provider CI 和商业裁决
不再混入企业导航；BYOK、Provider/模型/路由/评测、Project/Application、Virtual Key、额度预算、用量成本、内部 FinOps、
成员权限、安全治理、日志、Prompt、文件、批处理、MCP、数据生命周期和私有部署继续保留，但按任务、主导航、详情或高级
工具重新安置。

本条完成以下闭环：

1. `GET /workspace/navigation` 从当前 WorkspaceSession、Membership revision、权限指纹、合同版本和 entitlement 生成导航，
   不从前端角色名、旧 hash 或本地缓存恢复能力。
2. `GET /workspace/getting-started` 按角色和真实来源事实返回下一步任务；用户不能用勾选框伪造完成。
3. `PATCH /identity/preferences/navigation` 保存当前 GlobalAccount 在指定 Workspace 的纯 UI 偏好；偏好不能增加菜单、route
   或 API 权限。
4. `GET /workspace/advanced-tools` 只返回当前成员有权限且当前 Workspace 可用的高级工具；直接 URL 仍重新鉴权。
5. 64 项 Excel 导航逐项只有一个处置，源码合法后增的 4 项也有唯一合同；五个企业入口恢复，六个迁出项在
   Workspace Navigation、DOM、prefetch 和 route switch 中归零。
6. 默认导航组折叠、任一时刻最多展开一组；当前页面只加载自身数据，390/1280/1440 下完成真实鼠标、键盘和读屏流程。

非目标包括：不把导航可见性当 API 授权，不让 owner/admin 创建 `platform_*` 权限，不给 service account 浏览器导航，
不把 AdvancedToolEntitlement 变成权限授予表，不复制来源业务状态到任务勾选项，不保存 Provider Key、Token、Prompt、
Response、文件正文或客户业务正文，不在本条物理删除旧 source tables，也不恢复 PRD-139..145 已迁出的平台事实。

## 2. 身份、owner 与权限边界

### 2.1 WorkspaceSession、GlobalSession 与机器主体

三个 Workspace GET 只接受有效 WorkspaceSession Cookie，并逐请求恢复 `workspace_id + tenant_id`、GlobalAccount、
Membership、role、Membership/permission revision、全部 extension permission、authorization fingerprint 和 active
WorkspaceTenantBinding。父 GlobalSession Cookie可以共存，但不能替代 WorkspaceSession；PlatformSession、SupportSession、
Bearer、OAuth machine token、query credential、重复 Workspace Cookie 和错域 Cookie 全部失败关闭。

`GET /workspace/session` 在公开 `context` 之外返回独立且严格的 `authorization` 摘要，字段精确为
`permission_codes`、`permission_revision`、`permission_fingerprint` 和 `membership_revision`。Identity 页面只能从该摘要
取得完整权限集合；Navigation DTO 继续只负责路由投影，不返回权限全集。前端在启用任何权限控制的读取或操作前，必须把
Navigation 的 Membership revision、permission revision 和 fingerprint 与 Session 摘要逐项比对；任一漂移即清空
Workspace 私有状态并最多重新读取一次。Workspace switch 响应不携带授权摘要，切换成功后必须重新读取
`/workspace/session`，不得用旧 Navigation 或切换响应恢复权限。

NavigationPreference 写 API 只以 GlobalSession 认证账号本人。body 中的 `workspace_id` 只是选择目标，不是 owner 证明；服务端
必须重新查询该 account 在目标 Workspace 的唯一 active human Membership 和复合 WorkspaceTenantBinding。不存在、停用、
多重或 service_account Membership 均返回同形 404。子级 Workspace/Platform Cookie 可以在浏览器中共存，但不参与该
Identity API 授权；写入只接受 Global CSRF。

service account、OAuth client、Virtual Key、Edge Device、ChannelMembership 和 SupportSession 没有浏览器导航。即使它们
拥有某个来源 API scope，也不能调用导航、任务首页、偏好或高级工具 open command。Workspace owner/admin 可以通过
PRD-094/115 管理职能成员，但 Workspace permission catalog、role ceiling、写服务和 DB trigger 均拒绝任何
`platform.` permission；前端 role selector 不是安全边界。

### 2.2 0138 固定基础权限

0138 新增不可变 `workspace_navigation_permission_extensions/role_permissions`：

| Permission | 固定角色上限 | 语义 |
| --- | --- | --- |
| `workspace.navigation.read` | owner/admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer | 读取当前成员的导航投影 |
| `workspace.task_home.read` | owner/admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer | 读取当前成员的角色任务首页 |
| `workspace.advanced_tool.read` | owner/admin/developer/security_admin/auditor | 读取当前成员可用的高级工具 |
| `workspace.advanced_tool.open` | owner/admin/developer/security_admin/auditor | 显式打开已授权高级工具并记录事件 |

基础权限只允许进入对应投影 API。每个 navigation item、task action 和 advanced tool 还要求来源业务权限、Workspace owner、
产品 entitlement、Deployment kind/readiness 和对象状态的交集。比如有 `workspace.advanced_tool.open` 但没有
`policy.simulate` 时不会看到或打开策略模拟器；有 `policy.simulate` 但没有 advanced base permission 也不能把它恢复成
高级入口。来源 API 无论菜单是否显示都逐请求独立鉴权。

固定角色只是 migration ceiling，不进入运行时授权判断。自定义角色按当前 permission snapshot 与固定 ceiling 的交集计算；
不能按名称、旧 required_roles 或本地存储推断。权限指纹覆盖 Membership/role revision、全部固定 extension permission、
contract snapshot、entitlement generation 和 Workspace owner。Membership 撤销或权限变化后下一导航/API 请求立即失败，
旧响应因 fingerprint/epoch 不匹配被丢弃。

## 3. 0138 五个目标对象

### 3.1 NavigationContractSnapshot

`workspace_navigation_contract_snapshots/items/heads` 使用稳定 `ncs_*`，每个版本重复携带
`workspace_id + tenant_id`，保存 contract version、source catalog version/hash、item count、64 项 Excel disposition hash、
4 项后增来源 hash、group/order/placement、canonical route code、required permission expression、advanced/detail/account-shell
分类、feature/deployment requirement、status `candidate|active|superseded|rejected`、revision、generation 和 UTC。

snapshot item 使用稳定 source navigation ID，但对外只返回新的 canonical route code。编译期 route registry 只接受固定
route code，不把服务端字符串当 URL、module path、selector 或动态 import。released snapshot immutable；新增、改名、移动、
retire 或权限表达式变化必须创建新版本、完整验证 68 项审计合同唯一性后 CAS 推进 head。

被迁出的条目仍以 `placement=retired_internal` 保留在内部 snapshot 供迁移审计，但不会出现在公开 DTO、404 文案或前端
source registry。它们不形成兼容页面或 tombstone API；PRD-147 最终卸载旧 router/import。

### 3.2 WorkspaceNavigationProjection

`workspace_navigation_projection_versions/heads/items/sources` 使用稳定 `wnp_*`，按复合 Workspace owner 保存当前 contract
head、产品/Deployment entitlement、group catalog、active item placement、source watermarks、projection status
`ready|partial|stale`、generation/hash 与 UTC。它是 Workspace-wide 可重建投影，不保存账号、角色或用户可见菜单。

GET 在同一事务先认证当前 Membership，再把 current permission set 与 Workspace projection 的 item permission expression
求交，生成 member-specific response。因此缓存中的投影不能让已撤权成员继续看到入口；projection 与当前 contract/
entitlement generation 不匹配时失败关闭或在有界同步 resolver 中重建安全投影，绝不返回旧 permission snapshot。

公开 DTO 只含当前成员允许的 primary groups/items、detail route codes、developer-center child routes、account actions 和
advanced-tools entry。它不返回 denied/retired item、required_roles、其他角色菜单、任意 URL、旧 Gateway base URL、配置状态
或来源业务数据。Gateway 地址移到具备权限的 getting-started/developer source API，导航响应只承载导航合同。

### 3.3 RoleTaskHome

`workspace_role_task_home_versions/tasks/heads/sources` 使用稳定 `rth_*`，带复合 owner、Membership ID/revision、role code、
task catalog version、source type/revision/evidence digest、状态
`not_started|ready|blocked|in_progress|completed|needs_attention|not_applicable`、priority、canonical action route code、
completion generation、projection hash 和 UTC。任务正文使用固定 message code，不保存任意备注、Prompt、Response、Key、
Endpoint、文件或日志正文。

任务状态只从当前权威事实重算：PRD-097 onboarding、成员/角色、Provider/Model/Alias、Project/Application/VirtualKey、首次
Gateway 调用、订阅/额度/预算、Policy/DLP、审计、数据生命周期和 Deployment readiness。用户不能 POST “完成任务”；页面
action 只打开对应业务域，真实命令仍由来源 endpoint 执行。来源 revision 变化后 completed 可以成为 needs_attention，但历史
completion event 不删除或改写；新 completion generation 只有在重新满足当前合同后才产生。

角色首页固定职责：

| Role | 任务重点 |
| --- | --- |
| owner | Workspace 资料、成员职责、订阅、Provider/模型最小闭环、关键安全治理 |
| admin | 成员与角色、环境/Deployment、Provider、Project/Key、策略与恢复入口 |
| developer | Provider/模型/别名、Project/Key、首个真实调用、SDK/MCP 与错误恢复 |
| security_admin | MFA/SSO、Policy/DLP、会话/输出安全、Webhook 与审计覆盖 |
| finance_admin | 订阅/额度、企业成本率、内部 FinOps、预算、异常和争议 |
| legal_admin | 数据保留、legal hold、导出/删除、文件治理与合规证据 |
| auditor | 只读审计覆盖、过期证据、异常终态与恢复状态，不出现写任务 |
| viewer | 只读服务状态和被分配的可查看事项；无可执行任务时明确显示完成态 |

每个 response 只返回当前角色且当前来源 permission 允许的 task。`next_task` 必须是第一个 `ready|needs_attention` 且 action
仍可访问的任务；没有可执行任务时返回 null，不把越权任务作为“被阻断”泄漏。

### 3.4 NavigationPreference

`workspace_navigation_preferences/commands/source_mappings` 使用稳定 `nvp_*`，重复保存
`workspace_id + tenant_id + account_id + membership_id/membership_revision`，只对 status=`active` 建
`workspace + tenant + account` partial unique，保存 `last_primary_route_code`、唯一 nullable `expanded_group_code`、
`sidebar_collapsed`、contract snapshot ID、status `active|membership_revoked`、revision 与 UTC。它不保存 locale、role、
permission、业务筛选、cursor、search、表单草稿或敏感上下文；locale 继续由 PRD-145 AccountPreference 管理。

默认 `expanded_group_code=NULL`，即全部业务组折叠；overview 和 getting-started 保持独立可达。用户展开一组时新值原子替换
旧值，前端同一时刻只能渲染一组展开。进入某组内 route 可以在当前渲染代次临时展开该组，但只有成功 PATCH 才持久化。
desktop sidebar 默认可见，mobile drawer 默认关闭；`sidebar_collapsed` 是壳层密度偏好，不改变组单开或权限。

last route 只允许 current contract 中当前成员可见的 primary route。detail、advanced、account-shell、retired 或已撤权 route
不能持久化；偏好失效时 GET 安全回退 RoleTaskHome/overview，但不篡改历史 command。PATCH 使用 GlobalSession、active
Membership、current contract、当前 permission fingerprint、Idempotency-Key 和 quoted If-Match 共同校验；偏好永远不能
创建 Membership、role、permission 或 entitlement。

Membership 退出时当前 preference 只追加撤销 revision并冻结；同一账号以后重新加入该 Workspace 会为新 Membership 创建
新的 active preference，默认 overview/全组折叠。旧 last route 或 expanded group 不能自动跨 Membership 恢复，只有在明确
source mapping、current contract 与当前 permission 全部验证后才允许用户重新选择。

### 3.5 AdvancedToolEntitlement

`workspace_advanced_tool_entitlement_versions/heads/sources` 使用稳定 `ate_*`，按复合 Workspace owner + 固定 tool code 保存
required base/source permission expression、product/Deployment requirement、状态 `available|blocked|revoked|not_applicable`、
reason code、source revision、generation/hash 与 UTC。它是可重建 availability 投影，不是 grant；owner/admin 没有写 API，
也不能通过偏好、DOM 或 event 改变它。

首版工具目录固定为：

| Tool code | 默认固定角色映射 | 额外来源权限 | Canonical route |
| --- | --- | --- | --- |
| `provider_error_overrides` | owner/admin/developer | `workspace.provider_error_override.read` | `workspace.advanced.provider-errors` |
| `response_cache` | owner/admin/developer/security_admin | `response_cache.read` | `workspace.advanced.response-cache` |
| `policy_simulator` | owner/admin/developer/security_admin | `policy.simulate` | `workspace.advanced.policy-simulator` |
| `intent_security` | owner/admin/security_admin | `intent_risk.classify` | `workspace.advanced.intent-security` |
| `webhook_dead_letters` | owner/admin/security_admin | `webhook.manage` | `workspace.advanced.webhook-dead-letters` |
| `debug_console` | owner/admin/developer | `debug_console.use` | `workspace.advanced.debug-console` |
| `deployment_summary` | owner/admin/developer/auditor | PRD-145 diagnostic read/audit read | `workspace.advanced.deployment-summary` |

request explanation 与 file security 是来源详情子路由，不进入高级工具；SDK/migration/API/MCP 进入 Developer Center，不借
advanced entitlement 扩权。GET 只返回当前 actor 同时满足 base/source permission 的 tool，blocked 只对已经有资格的 actor
显示固定恢复原因；无资格工具完全不出现。

## 4. 64 项 Excel 唯一处置与最终位置

0137 源码实际有 63 个 `NAVIGATION_DEFINITIONS`：Excel 64 项中存在 59 项，另有 4 个合法后增项。缺失的 5 项里，
`growth-analytics`、`adapter-specs`、`adapter-contract-runs` 已先行从旧源码移除且属于最终六个退休项；`branding` 由
Deployment detail route 承接，`error-maps` 由 Advanced Tool route 承接，二者必须在 0138 新合同恢复。0138 migration 与
测试把以下 D01..D22 作为不可变 disposition catalog，同一 source ID 不允许第二种结论：

| Code | Excel 处置 |
| --- | --- |
| D01 | 平台/企业拆分 |
| D02 | 迁出企业 |
| D03 | 平台/私有部署拆分 |
| D04 | 企业保留并改名 |
| D05 | 企业保留 |
| D06 | 企业保留并归组 |
| D07 | 平台/企业拆分并降级 |
| D08 | 企业保留；平台另建计量 |
| D09 | 企业保留；平台账本拆分 |
| D10 | 企业保留并入高级配置 |
| D11 | 平台/企业拆分并改名 |
| D12 | 企业保留；平台发放拆分 |
| D13 | 企业保留；平台另建商业异常 |
| D14 | 企业保留并归高级工具 |
| D15 | 企业保留并降级 |
| D16 | 企业保留；平台审计拆分 |
| D17 | 企业保留并入日志详情 |
| D18 | 企业保留并任务化 |
| D19 | 企业保留并入开发者中心 |
| D20 | 迁出产品控制台 |
| D21 | 企业保留并入详情 |
| D22 | 迁到全局账号壳层 |

以下 64 行是唯一映射。`final placement` 是 Workspace canonical surface，不表示来源 API 改 owner：

| source navigation_id | 处置 | final placement |
| --- | --- | --- |
| overview | D01 | Home / Workspace overview |
| admin-assistant | D01 | Home / Workspace assistant |
| approvals | D01 | Home / Workspace approvals |
| growth-analytics | D02 | retired；Platform Growth |
| customer-health | D02 | retired；Platform Customer Success |
| partner-portal | D02 | retired；独立 `/partner` 壳层 |
| branding | D03 | Workspace / Deployment branding |
| tenant | D04 | Workspace / Workspace settings |
| users | D04 | Workspace / Members and access |
| role-permissions | D05 | Workspace / Roles and permissions |
| environments | D06 | Workspace / Deployments and environments |
| providers | D05 | Gateway / Provider connections |
| local-models | D06 | Gateway / Private endpoints |
| private-gateways | D06 | Gateway / Private Gateway deployments |
| adapter-specs | D02 | retired；Platform Provider Engineering |
| adapter-contract-runs | D02 | retired；Platform Provider Engineering |
| provider-health | D01 | Gateway / Workspace Provider health |
| error-maps | D07 | Advanced / Provider error overrides |
| models | D05 | Gateway / Models |
| model-aliases | D05 | Gateway / Aliases and routing |
| routing-policies | D05 | Gateway / Aliases and routing |
| model-quality-scores | D05 | Gateway / Quality |
| experiments | D05 | Gateway / Evaluations |
| projects | D05 | Gateway / Projects and applications |
| virtual-keys | D05 | Gateway / Virtual Keys |
| usage | D08 | Finance / Workspace usage and cost |
| finops-ledger | D09 | Finance / Internal FinOps ledger |
| model-prices | D01 | Finance / Workspace Provider cost rates |
| response-cache | D10 | Advanced / Response cache |
| plans | D11 | Finance / Workspace subscription |
| quota-buckets | D12 | Finance / Internal quota allocation |
| billing-disputes | D01 | Finance / Workspace dispute requests |
| budgets | D05 | Finance / Budgets |
| cost-anomalies | D13 | Finance / Workspace cost anomalies |
| policies | D05 | Governance / Policies |
| policy-simulator | D14 | Advanced / Policy simulator |
| policy-snapshots | D05 | Governance / Policy releases |
| policy-templates | D01 | Governance / Workspace template application |
| dlp-rules | D05 | Governance / DLP |
| intent-security | D15 | Advanced / Intent security test |
| session-risks | D05 | Governance / Session risks |
| output-safety | D05 | Governance / Output safety |
| webhooks | D05 | Governance / Webhooks |
| webhook-dead-letters | D15 | Advanced / Webhook dead letters |
| audit-logs | D16 | Governance / Workspace audit |
| request-logs | D05 | Observability / Request logs |
| request-explanations | D17 | Request log detail |
| feedback-summary | D05 | Observability / Feedback summary |
| quickstart | D18 | Getting started task home |
| sdk-examples | D19 | Developer Center / SDK |
| migration-guide | D19 | Developer Center / Migration |
| api-docs | D19 | Developer Center / API contract |
| competitor-comparison | D20 | retired；不在登录后产品 UI |
| enterprise-chat | D05 | Applications / Enterprise Chat |
| prompt-templates | D05 | Applications / Prompt assets |
| files | D05 | Applications / Files |
| file-security | D21 | File detail / Security report |
| batch-jobs | D05 | Applications / Batch jobs |
| mcp-tools | D19 | Developer Center / MCP governance |
| tenant-data-lifecycle | D04 | Workspace / Data management |
| debug-console | D15 | Advanced / Debug console |
| system-status | D01 | Observability / Workspace status |
| language-settings | D22 | Identity account menu / Preferences |
| config-summary | D07 | Advanced / Deployment summary |

四项后增源码合同为：`migration-operations -> Observability / Migration center`、
`support-tickets -> Observability / Workspace support`、`workspace-subscription -> Finance / Workspace subscription`、
`internal-finops -> Finance / Internal FinOps`。后两项是 PRD-144 的新 canonical primary route；旧 `plans` 与
`finops-ledger` 在 0138 只作为兼容 source mapping，不能与新入口同时成为 primary placement，PRD-147 再卸载旧入口。
四项均保留复合 Workspace owner 与现有来源权限，不占 Excel 64 行，也不能作为“额外项”掩盖 Excel 缺失。

0137 的 `WORKSPACE_NAVIGATION_EXCLUDED_IDS` 实际列出 8 项，其中 `growth-analytics` 已不在 source definition；
`providers`、`local-models`、`provider-health`、`billing-disputes` 四个 active source 被错误隐藏，`error-maps` 则缺少
Advanced Tool 承接。0138 恢复这五个企业裁剪入口；最终只从 Workspace
公开合同删除 `growth-analytics`、`customer-health`、`partner-portal`、`adapter-specs`、
`adapter-contract-runs`、`competitor-comparison` 六项。language-settings 作为 account action、detail/advanced/dev-center
子路由仍在内部 contract 中，但不占普通一级组。

## 5. 事件、outbox 与投影刷新

`workspace_navigation_events/outbox` 带复合 owner、event ID、aggregate type/ID、generation、payload hash、dispatch lease 与
UTC，只允许 Excel 三个 event type：

| Event | 唯一触发点 | 安全 payload |
| --- | --- | --- |
| `workspace_navigation.changed` | contract/entitlement 或 Membership permission 变化首次改变有效 item set | snapshot/generation、membership pseudonym、old/new keyed item-set digest、UTC |
| `role_task.completed` | 当前 task generation 首次从非完成变为来源可证明 completed | task code、role code、membership pseudonym、source evidence digest、UTC |
| `advanced_tool.opened` | 显式 open command 在同事务通过 entitlement/permission/epoch 校验 | tool code、entitlement revision、route code、actor pseudonym、UTC |

Membership 撤权不依赖 event 才生效；GET/API 始终读取当前 permission revision。`workspace_navigation.changed` 用于客户端和
下游缓存失效，不是授权 token。偏好折叠/展开只写 command/audit，不发 navigation changed；它没有改变允许 item set。

0138 扩展 PRD-145 `install_console_experience_scheduler`，增加 `workspace_navigation_projection`、`role_task_home`、
`advanced_tool_entitlement`、`workspace_navigation_outbox` 与 `navigation_reconciliation` 独立 queue code、lease、fence、
attempt、backoff 和 watermark，不安装第二个同域 supervisor。来源变更按 `(owner, source, revision)` 幂等；backfill 不发
新事件，旧 `console.page_viewed/dashboard.role_viewed/onboarding.step_completed` 保留原样，不翻译或双发。

每轮投影/head/event/outbox/watermark 提交验证当前 fencing generation/token digest。失租 worker 的迟到写失败关闭，接管从
持久 cursor 继续，不重置任务完成 generation 或重复 tool-open event。PRD-147 再把该 supervisor 纳入全局
SchedulerRegistry。

## 6. 0011/0057/PRD-097 与现有导航迁移

0138 使用 expand/inventory/backfill/shadow/quiesce/cutover/contract，要求 current head 0137。每阶段保存 source/target row
count、canonical hash、owner coverage、Membership/permission revision、contract hash/keyed item-set digest、event/outbox、lease、审计和
watermark。

### 6.1 Catalog 与投影 backfill

1. 从 0137 当前 63 个 `NAVIGATION_DEFINITIONS` 解析固定 source ID/permission/order，并与 Excel 64 行逐项比对；结果必须
   精确为 59 个 Excel source、5 个已解释缺失和 4 个 approved extra；重复、未知 extra、处置码漂移或 source hash 不符在
   首个持久 DDL 前阻断。
2. 为每个 active WorkspaceTenantBinding 创建 NavigationContractSnapshot，精确包含 64 Excel disposition 与四个 approved
   extra；按 PRD-139..145 source cutover、产品 entitlement 和 Deployment kind 生成 WorkspaceNavigationProjection。
3. 当前 exclusion 的五个误隐藏项必须有可证明 Workspace source permission/owner 后才恢复；六个迁出项一律
   `retired_internal`，不能因 legacy permission 仍存在而回到公开投影。
4. 为每个 active human Membership 计算 role task source facts；service_account、conflict/missing account link、停用成员或
   多重 Workspace owner 进入隔离报告，不创建任务首页或偏好。
5. AdvancedToolEntitlement 从固定目录、当前 product/Deployment fact 和来源 permission catalog 生成；不从旧菜单可见性、
   page event 或用户点击次数推断 entitlement。

### 6.2 旧偏好与角色首页

0011/0019 `user_preferences.active_navigation_item_id/sidebar_collapsed` 通过
tenant/user -> account_identity_link -> active WorkspaceMembership 逐行映射到 NavigationPreference；0137 AccountPreference
locale 不复制到此对象。sidebar density 原值保留，expanded group 初始为 null。

last route 只在新 contract 当前可见时保留。固定映射为：`quickstart -> getting-started`，
`language-settings -> identity preferences` 并使 Workspace last route 回退 overview，`config-summary -> deployment-summary`，
`request-explanations -> request-logs`，`file-security -> files`，任一 advanced route -> 当前所属 primary/overview，六个 retired
route -> role task home/overview。选择规则和原 source hash 写 mapping；不静默保存不可达旧 hash。

0057 `role_dashboard_configs` 和 PRD-097 onboarding 是 RoleTaskHome 的迁移/来源，不复制第二套 role config。首个 task version
保留旧 role-view default/allowed view mapping 与 onboarding evidence hash，随后只按当前角色和来源事实生成。历史
`dashboard.role_viewed`、onboarding event/outbox 和 completion/reopened 状态原样保留；backfill completed task 不发
`role_task.completed`。

### 6.3 Shadow、切换与回滚

shadow 对每个 active human Membership 比较旧/new permission set、当前可见 source ID、safe fallback、preference、role-view
入口与 source readiness。目标不是让旧/new 顺序完全相同，而是证明 64 唯一处置、五项恢复、六项移出、detail/advanced/
account/dev-center 分类和所有 current source permissions 精确匹配。无历史调用的 route 标记 `not_observed`，不能伪造成功。

quiesce fence 旧 `/console/navigation` Workspace reader、`/console/preferences` writer 和旧 page-event ingress，等待在途请求/
event write 完成或过期；来源业务 API 和 PRD-097 onboarding 继续运行。最终 reconciliation 通过后 CAS 切换新 navigation、
task home、identity navigation preference 和 advanced-tools reader/open command；前端同一发布切换 route registry 与 shell。

兼容期旧 `GET /console/navigation` 对 WorkspaceSession 只读适配当前新 contract，不返回 `required_roles`、gateway base URL 或
retired route；旧 legacy Bearer UI 不得借此进入 Workspace。旧 PATCH preferences 在 cutover 时 write-fence，旧 page event
不再接受新 shell 事件。PRD-147 卸载旧 route/import/DOM/prefetch/API。

rollback 只恢复上一代 current-permission-compatible contract snapshot；仍按当前 Membership/permission/entitlement 求交。
它不能恢复六个 retired item、旧 exclusion 的错误状态、service-account 浏览器导航、required_roles 授权、旧 preference
writer、共享页面或 Platform source。无法证明时返回明确 unavailable/404，而不是加载旧侧栏。

0138 down 仅在所有 snapshot/item/head、navigation projection、task/source/head、preference/command/mapping、advanced
entitlement、event/outbox、reconciliation、permission extension、lease 和 epoch 水位 pristine 且从未 backfill 时允许。任一
事实存在即首个 DDL 前拒绝；down 不修改 0011/0057/PRD-097/0131..0137。空库验证
`0137 -> 0138 -> 0137 -> 0138`，生产 rollback 不执行 destructive down。

## 7. 四个 Excel API 与一个窄辅助命令

所有 DTO `extra=forbid`、strict type、固定枚举与有界数组；未知/重复 query、重复 JSON key、非规范 UTF-8、任意 role/
permission、非法 route/tool/group、超限 body 与混合凭据固定拒绝。响应含 request_id/trace_id、复合 owner safe ref、
Membership/permission revision/fingerprint、contract snapshot/generation、navigation epoch、`Cache-Control: no-store, private`
及适用 quoted ETag。

Identity 使用的权限全集不属于下列 Navigation API DTO；它由 2.1 所述 `/workspace/session.authorization` 单独提供并与
Navigation 证据交叉校验。

### 7.1 `GET /workspace/navigation`

不接受 query。要求 WorkspaceSession + `workspace.navigation.read`，先验证 current contract/head/source watermark，再按当前
Membership permission 和 entitlement 求交。返回 active primary route、groups/items、allowed detail route codes、
Developer Center children、account actions、advanced entry、preference projection 和 keyed item-set digest；不返回 denied/retired
items、required_roles、其他角色、业务数据、配置、Gateway URL 或任意可执行 URL。

手写 hash 只作为候选 route code。候选不在本次 allowed set 时新 router 返回普通 unknown route、清空旧 DOM 且不发来源
请求；候选在 detail/advanced set 时仍需对应 source permission/entitlement。前端不能从静态 import 或旧 localStorage 恢复
服务端未返回的 route。

### 7.2 `GET /workspace/getting-started`

不接受 query。要求 WorkspaceSession + `workspace.task_home.read`，返回当前 Membership 的 task generation、role-safe tasks、
`next_task`、完成/需关注计数、source freshness、evidence digest 和 canonical action route codes。task action 不带 owner、
command body、Secret 或 arbitrary URL；打开后来源 endpoint 重新鉴权。无可执行任务返回 null next task，不返回别的角色任务。

### 7.3 `PATCH /identity/preferences/navigation`

只接受 GlobalSession、exact Origin/Global CSRF、`Idempotency-Key`、quoted `If-Match` 和 exact body：目标 `workspace_id`、current
`contract_snapshot_id`，以及至少一个 `last_primary_route_code|expanded_group_code|sidebar_collapsed`。字段可以显式 null 以
恢复默认；不接受 locale、role、permission、entitlement、cursor、filter 或业务草稿。

服务端在写事务中恢复 active human Membership、Workspace owner、current contract 和 permission fingerprint，再验证 route/
group 当前可见。不存在 preference 时用 ETag `"0"` 创建；同键同体精确 replay，同键异体 409，revision/contract/permission
漂移 412。写入 command/audit 与 preference CAS 同事务，但不发 workspace_navigation.changed，因为允许 item set 未变化。

### 7.4 `GET /workspace/advanced-tools`

不接受 query。要求 WorkspaceSession + `workspace.advanced_tool.read`，只返回当前成员满足 base/source permission 的 tool code、
canonical route、entitlement revision、availability/readiness、固定 reason/action 和 observed UTC。无资格 tool 不出现；eligible
但 blocked 可返回固定恢复入口，不暴露配置、内部 path、token 状态或其他角色 entitlement。

### 7.5 `POST /workspace/advanced-tools/{tool_code}/open`

为使 Excel 的 `advanced_tool.opened` 有无副作用 GET 之外的明确触发点，0138 增加此窄辅助命令；它不是第五个业务对象 API。
命令拒绝 body/query，要求 WorkspaceSession、exact Origin/Workspace CSRF、`workspace.advanced_tool.open`、来源 permission、
`Idempotency-Key` 和 quoted entitlement `If-Match`。事务内重验 entitlement/current epoch 后写 event/outbox，返回 canonical
route code 和 epoch。它不签发 capability、session 或 token；随后页面及业务 API 仍完整重新鉴权。

## 8. 前端信息架构与交互合同

主壳层固定分为 Home、Workspace、Gateway、Finance、Governance、Applications、Observability 和 Developer Center。Home 的
overview/getting-started 始终在授权后可达；业务组默认折叠，点击组 header 只展开该组并关闭上一组。高级工具通过单独入口
打开，不把 debug、intent test、dead letter 或 Deployment diagnostics 混进普通一级菜单。Identity locale 与 navigation
preference 分属 Account/Workspace scope，Workspace switch 后分别保留正确状态。

Identity 的 Conditional Access、Data Governance、Privacy、Compliance、Machine Identity 等入口和动作只读取当前
WorkspaceSession 的 `authorization.permission_codes`。旧 `/console/navigation.permission_codes`、静态 role 推断、DOM 状态
或 Navigation item 可见性均不能启用这些能力。

所有 route 使用后端返回的 canonical code 与编译期 registry。每次 contract/permission/Workspace/route change 先推进 epoch、
使全部 ticket 失效并 abort，再同步清空旧 DOM/store/cursor/form/answer/detail，最后只加载当前 route API。当前 item 的 label、
group、description、empty/recovery 文案来自固定 i18n key；屏幕不显示 migration ID、table、permission code、hash、token、env
var、internal path 等研发术语。

前端可以乐观展开单组，但 preference PATCH 失败必须回滚到最后服务端 revision；偏好永远不能让 route 出现在 navigation
response 之外。按钮只根据本次 DTO 的 action capability 启用，点击后来源 API 重新授权。隐藏菜单、disabled button、DOM
dataset、history state、直接 hash 和 Service Worker cache 均不是权限边界。

390px 使用关闭的 navigation drawer、稳定 44px 触控目标和单列任务；1280/1440 使用紧凑工作台、固定 sidebar 宽度、单组
accordion 和可扫描任务表。DPR 1/1.25/1.5、键盘 focus order、Escape/返回、读屏 label/live region、无重叠/裁剪/横向
溢出和真人鼠标主流程在 G7 真实浏览器验证。

## 9. 安全、恢复与 SLO

导航/任务/entitlement projection、command、event 和审计只保存 actor/account pseudonym、复合 owner、Membership/permission
revision、contract/route/tool/task code、source revision/evidence digest、outcome、request_id/trace_id 与 UTC。不得保存 Cookie、
CSRF、Bearer、Idempotency-Key 明文、Provider Key/Endpoint、Prompt/Response、文件/日志正文、业务筛选或前端 DOM 文本。

DB/source/scheduler 故障时 navigation/advanced 失败关闭；task home 可以在 owner/current permission 可证明时返回明确 stale，
但不能用旧 action capability。contract hash、item count、64 disposition、4 extras、Membership fingerprint 或 source watermark
任一不一致阻断 head/cutover。reconciliation 从持久 cursor 恢复，失租接管不清空 preference 或重复 completion/open event。

固定 Excel SLO：

- 导航投影 GET p95 `<200ms`，包含当前 Session/Membership/permission 恢复、contract 校验和 item intersection；
- route 切换达到可交互 p75 `<1.5s`，包含真实 API、render 和 focus，不以只改 active class 通过；
- 首屏关键任务可见并可操作 p75 `<1.5s`，包含 task source freshness 与 next-task 计算。

撤权验证要求下一请求失败、旧 DOM/请求同步淘汰；hidden item 不作为通过。固定状态包含 loading、empty、partial、stale、
permission revoked、contract conflict、dependency unavailable 和 recovery in progress。未知 open/preference POST 结果只用原
幂等键恢复，不生成第二事件或覆盖新 revision。

## 10. 聚焦测试与批次证据

PRD-146 开发中只跑受影响 unit/API/browser smoke：

- migration verifier 从 Excel 与源码解析 ID：Excel disposition `64/64`、0137 source 63（59 个 Excel source + 4 extras）、
  新审计合同 68、零重复、5 个缺失来源均有固定承接/退休解释、extras 精确为四项、每项一个 D code、最终 retired 精确六项、
  恢复精确五项；
- 0137 current head、fresh/up/down guard/replay、复合 FK、Membership/account mapping、preference conflict、contract hash/
  keyed item-set digest、event/outbox、lease/fence 与空库往返；
- 八个 human role、custom role 交集、service account/OAuth/VirtualKey/Edge/Platform/Support/Bearer 全负例；owner/admin 不能
  grant `platform.*`；动态撤权、role/contract/entitlement drift 与跨 Workspace 同形 404；
- 四 API strict/no-store/ETag/cursor-free、preference CAS/idempotency、task source truth、advanced GET 无副作用、open event
  唯一、未知/retired/direct route 不发 source request；
- 前端默认折叠/单组展开、route-only fetch、five restored/six retired、detail/advanced/account/dev-center placement、late
  response、switch/logout/revoke/pagehide 清理、390/1280/1440 与键盘/焦点/读屏 smoke；
- 静态敏感审计扫描代码、日志、fixture、snapshot 和正式证据中的 Key/Token/Cookie/CSRF/Prompt/Response、配置值、内部地址、
  业务正文和测试凭据；本地管理员凭据只在内存输入，不打印、不持久化。

PRD-146 保留独立设计、0138/migration/API/unit/browser smoke 记录、敏感审计和 Git commit。PRD-147 独立提交后统一跑 G7
数据库、集成/FLOW、真实 API/浏览器、性能、安全、故障恢复、Windows/Linux、SQLite/PostgreSQL、N/N-1、长稳和必要
回归；正式证据写 `.artifacts`，不使用 `.tmp`。

## 11. 预计改动边界

实现只修改 0138 up/down、workspace navigation/task/advanced backend、Identity navigation preference adapter、固定 Workspace
permission loader、PRD-145 console-experience scheduler queues、shared exact contracts、Console shell/router/state/i18n 和
PRD-146 专项测试/文档。允许复用纯 route code 校验、owner-bound cursor/ETag、event/outbox、fence、message key 和无状态
presentational primitive；不共享 Platform/Workspace mutable store 或鉴权 client。

不修改 PRD-139..145 来源状态机，不重新开放迁出企业的业务 API，不把 AdvancedToolEntitlement 写入 role grant，不替代
PRD-097 onboarding 权威事实，不实施 PRD-147 外部 router tombstone、全局 migration reconciliation 或 SchedulerRegistry，
不删除 0011/0057 历史，不读取或保存客户 Prompt、Response、Provider Secret 或本地测试凭据。

## 12. 两轮编码前自审

### 第一轮：owner、权限、64 项完整性与事件可达性

- P0：NavigationPreference 属于 Workspace owner，但 API 位于 Identity 且只有 GlobalSession；若按 body workspace_id 直接写会
  越权。已要求 body 只作 selector，写事务重查 active human Membership、复合 owner、contract、permission 与 preference
  revision；跨 Workspace 同形 404。
- P0：若 WorkspaceNavigationProjection 持久化用户可见集合，撤权后旧投影可能继续授权。已改为 Workspace-wide contract/
  entitlement 投影，GET 每次与当前 Membership permission 求交并绑定 fingerprint/epoch。
- P0：`advanced_tool.opened` 不能由 GET 或前端 page event可靠生成。已补拒绝 body 的窄 open command，在同事务重验
  entitlement/source permission 并写 event/outbox；返回 route 不是 capability。
- P0：只隐藏侧栏无法阻止 direct hash/import/prefetch。已把 primary/detail/advanced/account/dev-center/retired 作为后端合同，
  新 router 只认本次 allowed route code；retired 不进入 DTO 或 source request。
- P1：当前 exclusion 错误隐藏四个 Excel 要求保留的入口且 `error-maps` 缺少 Advanced 承接。已精确恢复五项，最终 retired
  精确六项；64 Excel + 4 extras
  独立计数，extra 不能补齐缺失。
- P1：PRD-145 locale 与本条导航偏好容易混表。已固定 AccountPreference 只管 locale，NavigationPreference 绑定
  account+Workspace 且不保存 locale/role/permission。

第一轮结论：五个对象、四 API、三事件、64+4 处置、三类主体和迁移来源都有明确 owner 与唯一事实；无未解决 P0/P1。

### 第二轮：任务真实性、交互、恢复与 SLO

- P0：把 quickstart/role home 变成用户勾选清单会伪造业务完成。已要求 task 只从 PRD-097 和业务域 source revision/evidence
  重算，用户只跳转执行真实命令；backfill 完成不发新 event，失效后显示 needs_attention。
- P0：AdvancedToolEntitlement 若可由 owner 写入就会成为第二套权限系统。已冻结为无公开 writer 的 availability 投影，
  base permission、source permission、固定 grant ceiling、product/Deployment readiness 缺一不可，实际 API 再鉴权。
- P0：旧 navigation response 携带 required_roles 和 Gateway base URL，会诱导前端角色授权并让每页加载非当前数据。已从新
  DTO 删除 required_roles/denied items/Gateway config，仅返回当前 actor allowed route codes；Gateway 数据由目标页面加载。
- P1：“默认折叠”若解释为隐藏整个 desktop sidebar 会损害高频工作流。已明确业务组默认折叠且单组展开，desktop shell
  保持稳定可见，mobile drawer 默认关闭，sidebar density 是独立偏好。
- P1：preference 改变 presentation 不应产生 workspace_navigation.changed，否则折叠操作会污染领域事件。已限定 changed
  事件只表示有效 item set 的 keyed digest 改变，偏好仅写 command/audit。
- P1：SLO 若只测导航 JSON 或 active class 会虚假通过。已要求 navigation p95 包含权限求交，route/first-task p75 包含真实
  API、render、focus 和 source freshness；异常率、冷/热与 DB time 一并报告。

第二轮结论：交互、直接 URL、任务终态、event/outbox、失败恢复、三项 SLO 和批次证据均可执行验收；无未解决 P0/P1，
可以在 G6 通过后进入 S7 连续编码。

## 13. 主控制台 Cookie 入口与跨页 owner 补充批次

2026-09-07 主 Console 启动检查仍依赖 Legacy sessionStorage 和 `/auth/me`，而导航已要求
Workspace Cookie，身份页也不建立旧 Bearer。与 PRD-094/147 同批修复真实进入链路，不将旧 Token
注入测试来绕过缺口。PRD-137 先以真实注册/登录和无旧槽位的新标签回归确认断点。

第一轮自审：Console 启动、刷新、切换和退出必须使用服务器确认的 WorkspaceSession，来源域
显式，不伪造旧 token 或用 Workspace ID 充当凭据。身份/owner 仅驻留内存，导航与后续页面
必须同 owner；账号失效回真实 Identity 登录，空间未选择回真实选择器。不能将旧 `/auth/me`
静默放宽为任意域认证，也不能仅让首页绕开旧登录而留下其他页面继续依赖旧 Token。

第二轮自审：批量核对主页面状态机和请求客户端：导航立即清空/中止、旧成功与旧失败均不能
覆盖新 owner；各业务域只向当前实际授权的 API 发起请求，不预取迁出的平台资源。保留已有
Legacy 专项兼容合同，由 PRD-147 独立处理迁移边界。单元、真实 Cookie API 和桌面/移动
流程逐层验证，批次完成再统一回归。入口通过不代替完整 64 项处置或 G6/G7 全部通过。

### 13.1 Cookie 导航共享 owner 的实施复核

主入口迁移先修正现有导航控制器的共享会话边界。导航读取复用真实 `/identity/me` 和
`/workspace/session` 客户端，不签发或缓存 Legacy Token。内存 owner 纳入 GlobalSession
公开句柄、撤销代次、两个会话到期时间、Workspace、角色及三项授权证据；不把 Workspace ID
映射成旧 tenant/user/session ID。该 owner 只用于前端迟到响应隔离，不能充当服务端凭据。

第一轮自审：仅比较 workspace/role 会把同角色的新账号会话及权限变化视为同一 owner。
改为完整公开会话 owner，导航返回的 permission revision/fingerprint/membership revision
必须与当前 WorkspaceSession 一致。过期回调的 401/403 不得清除后来建立的会话；旧偏好
成功不得让新的 ready 状态变为 conflict。新导航加载开始即淘汰旧导航读取，不能等认证返回。

第二轮自审：已确认的撤权或合同冲突必须清空导航、任务、高级工具及活动请求，不能只改
status 继续保留旧 action。临时偏好失败仍保留原幂等键用于相同 owner 重试。真实切换由
现有跨页广播同步失效；多个独立 HTTP 响应不声称具有跨请求原子快照。main 在处理导航失败
前再次核对当前刷新 owner。本增量不宣称修复 index.html 的 Legacy 启动守卫，后续仍须完成
主入口及所有可达业务客户端的 Cookie 接线，并复验真实无旧存储的进入流程。

### 13.2 主应用启动、刷新与退出实施设计

第一轮自审：启动不读 Legacy Storage、不调用 auth/me，也不保留第二套密码表单。统一使用
现有 loadConsoleShellState 完成 Identity/Workspace/导航验证，内存 session 只保存公开 owner
与原 Workspace authorization DTO；不构造 tenant/user/session 镜像。菜单直接读 shell 的
账号与空间 DTO。刷新票据以请求代次和内存 session 对象归属淘汰旧响应，首次无会话也允许
发起验证，但只有完整 Cookie owner/导航一致才进入活动页面 dispatcher。

第二轮自审：公共失效、退出和 pagehide 共用现有全页面清理，先销毁 DOM、正文、草稿和异步
请求，再验证或注销。退出改用 Identity Cookie API，并在网络前后发送无身份内容的跨页失效
通知，旧退出回调不能重绘新会话。导航、分组和侧栏的成功/失败也核对当前 session 与代次。
匿名或未选择空间回 identity.html，网络失败提供重新验证入口，不默认为已登录或自动签发
Legacy Token。既有资源/页面迁移继续按所属域完成，不将入口成功视为全部业务可用。

实施复核第一轮：注销 HTTP 尚未完成时，hash/手动刷新或其他标签的失效通知可能重新读取仍有效
的 Cookie。增加当前页面的注销中状态，进入时已统一销毁旧事实，完成前不发起认证刷新或重复
注销；pagehide 的代次仍能淘汰旧完成回调。注销失败允许重新验证，不伪称服务器已撤销。

实施复核第二轮：用真实主函数和可控 Promise 覆盖无旧 Storage 启动、重叠刷新、迟到错误、
当前 401、注销前同步清理、注销中刷新/广播、旧完成回调。公开 owner 复用导航校验器，授权
DTO 及权限数组在内存中冻结，不能通过外部引用修改已确认权限；页面菜单不输出 undefined。
