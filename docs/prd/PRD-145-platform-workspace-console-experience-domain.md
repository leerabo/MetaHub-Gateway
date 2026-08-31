# PRD-145 平台与 Workspace 控制台体验分域

状态：编码前设计冻结。Migration 固定为 `0137`，要求 current head 为 `0136`。本条只交付
Platform/Workspace 总览、审批、助手、状态、账号语言偏好与 Deployment 诊断的分域闭环，不提前实现
PRD-146 Workspace 导航重组或 PRD-147 全局迁移、tombstone 与 SchedulerRegistry。

## 1. 产品闭环与非目标

同名页面不再代表同一授权面。Platform 工作台服务平台运营、SRE、安全、商业和合规角色；Workspace 工作台
只服务当前企业 Gateway 的成员；账号语言偏好属于 GlobalAccount，不随 Workspace 切换；SupportSession 继续使用
受 Grant 约束的支持界面，不继承任一工作台导航。

本条完成以下生产闭环：

1. Platform 和 Workspace 各自从独立、版本化的安全投影读取总览，页面请求不再现场扫描请求、用量或客户正文。
2. 两域助手使用独立 source registry、DTO、client、内存 store 和审计投影；问题、回答和 citation 正文不持久化。
3. 两域审批页只聚合各业务域权威审批的安全只读投影；真实决定、step-up、幂等和 writeback 仍由来源业务域执行。
4. 语言设置从 tenant `user_preferences` 迁到 GlobalAccount `AccountPreference`，跨 Workspace 和 Platform 壳层一致。
5. Workspace 环境信息降级为角色受控的 Deployment 诊断；普通角色不再看到环境变量、文件路径、内部 token 状态或
   研发配置。Platform 状态只显示平台安全聚合，不借此读取客户业务正文。
6. 每个路由只加载自身依赖；切换、撤权、登出和 pagehide 立即淘汰旧请求，迟到响应不能写入新页面。

非目标包括：不建立第二套审批执行器，不把 Dashboard/Queue/Assistant 投影变成来源业务事实，不开放任意 SQL、日志
检索或 Agent tool call，不保存 Prompt/Response、Provider Key、Token、Header、附件正文或客户业务正文，不把
SupportGrant 转换为 PlatformSession/WorkspaceSession，也不在本条重排 PRD-146 的 64 项 Workspace 导航。

旧 `/dashboard/overview`、`/dashboard/role-view`、`/admin-assistant/query`、`GET /approvals`、
`/user/preferences` 和 `/internal/config/summary` 只作为迁移来源或有界兼容入口。新页面不 import、不 prefetch、
不回退调用这些路径；PRD-147 从外部 router、DOM、client 和导航合同最终卸载。rollback 不能重新挂载共享页面。

## 2. 身份、owner 与固定权限

### 2.1 三种交互主体与物理 owner

- Platform API 只接受有效 PlatformSession Cookie，并逐请求恢复 Operator、GlobalAccount、role binding、Operator
  revision、权限全集和权限指纹。它可以与父 GlobalSession Cookie 共存，但拒绝 WorkspaceSession、SupportSession、
  Bearer、query 凭据、重复 Platform Cookie 和错域 CSRF。
- Workspace API 只接受有效 WorkspaceSession Cookie，并逐请求恢复 `workspace_id + tenant_id`、GlobalAccount、
  Membership、Membership revision、WorkspaceTenantBinding、权限全集和指纹。它可以与父 GlobalSession 共存，
  但拒绝 PlatformSession、SupportSession、Bearer、query 凭据和重复 Workspace Cookie。
- Identity preference API 只使用 GlobalSession。浏览器中已有子级 Platform/Workspace Cookie 不作为该接口的凭据，
  也不能扩大账号本人边界；Bearer、query token、重复 GlobalSession/CSRF Cookie 均拒绝。写操作要求 exact Origin
  与 Global CSRF。

PlatformDashboardProjection 是全局平台事实，不带 tenant_id。WorkspaceDashboardProjection 与
DeploymentDiagnosticProjection 必须带不可变 `workspace_id + tenant_id` 并以复合外键绑定唯一 Workspace owner。
AccountPreference 只以 `account_id` 绑定 GlobalAccount，不带 tenant_id、workspace_id 或角色。

Excel 把 DomainApprovalQueue 与 AssistantQueryProjection 标为 Workspace owner，但同时要求 Platform API。逻辑对象
保持同名，物理事实必须拆分：

- `platform_domain_approval_queue_items` 与 `platform_assistant_query_projections` 是 Platform global；
- `workspace_domain_approval_queue_items` 与 `workspace_assistant_query_projections` 使用复合 Workspace owner；
- 两组表不使用 nullable owner 三元组、泛化 `owner_type/owner_id`、同一 sequence、同一 cursor 或跨表 UNION 作为
  授权边界。

Platform 投影可以保留不可逆 Workspace pseudonym、聚合计数和来源水位，但不能保存可反查客户正文的 Workspace 名称、
联系人、Request/Prompt/Response、Provider Endpoint 或 Secret。Workspace 投影不能引用其他 Workspace，也不能复制
平台内部商业裁决、客户成功备注、渠道合同、Provider CI fixture 或平台运行配置正文。

### 2.2 0137 权限矩阵

0137 新增不可变 `platform_console_permission_extensions/role_permissions` 和
`workspace_console_permission_extensions/role_permissions`。基础页面权限与来源对象权限取交集；仅有页面权限不会让
来源可见，仅有来源权限也不会创建页面入口。

| Permission | 固定角色上限 | 用途 |
| --- | --- | --- |
| `platform.console.overview.read` | platform_owner/ops/security/billing/support | 读取按来源权限裁剪的平台总览 |
| `platform.console.assistant.query` | platform_owner/ops/security/billing | 查询允许的平台运营投影 |
| `platform.console.approval.read` | platform_owner/security/billing/compliance/release_manager | 读取所属平台域审批投影 |
| `workspace.console.overview.read` | owner/admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer | 读取当前 Workspace 总览 |
| `workspace.console.assistant.query` | owner/admin/developer/security_admin/finance_admin/legal_admin/auditor | 查询当前 Workspace 已授权来源 |
| `workspace.console.approval.read` | owner/admin/security_admin/finance_admin/legal_admin | 读取当前 Workspace 审批投影 |
| `workspace.deployment_diagnostic.read` | owner/admin/developer | 读取可操作 Deployment 诊断 |
| `workspace.deployment_diagnostic.audit_read` | auditor | 读取进一步裁剪的审计诊断 |

`platform_support` 表示持 PlatformSession 的平台角色；临时 SupportSession 即使有有效 SupportGrant 也不能调用上述 API。
支持人员需要客户正文时仍走 PRD-095 field-scoped 支持路径。Platform overview 中 status、billing、provider、customer、
growth、channel 等卡片还要求对应 `platform.*.read` 来源权限；Workspace overview/assistant/approval 对每个来源重新要求
当前 Workspace 的 source read/decision permission。

旧 `dashboard.read`、`admin_assistant.query`、`approval.read`、`system.config.read`、`console.basic.read` 和内部 token
不能授权新 API；新权限也不能反向授权旧共享 API。既有自定义角色只在“旧能力、0137 固定角色上限、当前来源权限”
三者交集内回填，不按角色名称猜测或扩大 viewer。Platform owner 同样必须在 extension table 显式映射，不能隐式获得
未来权限。

`platform_access.service._PERMISSION_EXTENSION_TABLES` 和 Workspace authority 均使用源码固定 tuple 与固定 SQL 模板
加载 0137 表，不扫描 sqlite schema、不接受运行时表名。权限指纹覆盖账号/Operator/Membership revision、角色、全部固定
extension permission、Workspace owner 和会话 revision。每次请求及助手响应提交前重新验证；撤权后下一请求立即失败，
在线页面必须在 2 秒内清除受保护状态。

## 3. 0137 目标数据模型与唯一事实

### 3.1 PlatformDashboardProjection

`platform_dashboard_projection_versions/heads/sources` 使用稳定 `pdp_*` ID、全局 generation、projection schema、
source code/revision/watermark、固定 metric code、整数值或 band、状态 `ready|partial|stale`、reason code、数据窗口、
content hash 和 UTC。version 只追加，head 通过 CAS 指向唯一已发布 generation。

来源是 PRD-139..144 的平台安全投影及既有平台 status/observability/job/capacity/deployment facts。刷新器只读取版本化
白名单字段并记录每个来源的上限水位，不把来源对象改写为 Dashboard 真值。客户相关卡片只允许不可逆 pseudonym、计数、
band、原币种聚合和固定解释因子；禁止 Workspace 列表、联系人、请求明细、Provider secret、支付/税务资料和自由备注。

读 API 从同一 head generation 返回全部卡片。关键来源不可用时不得混合新旧 generation 冒充完整快照：可以发布
`partial`，但必须逐来源返回 `fresh|stale|unavailable`、observed watermark、固定影响说明和恢复入口；无可证明快照时
固定 503。stale 由冻结的 source SLA 与读取时钟计算，不能由客户端声明。

### 3.2 WorkspaceDashboardProjection

`workspace_dashboard_projection_versions/heads/sources` 使用稳定 `wdp_*` ID，重复携带
`workspace_id + tenant_id`，保存 Membership-independent Workspace 安全聚合、source watermarks、generation、revision、
状态、固定 action capability code、content hash 与 UTC。复合 owner 贯穿 version、head、source、event 与 refresh cursor。

投影只组合当前企业的角色任务、Gateway/Provider 健康、用量成本 band、预算/额度状态、安全治理、审批计数、通知和
Deployment 摘要。Project/VirtualKey/Provider/Request 的明细仍从各自 endpoint 读取；Dashboard 不复制 Prompt、Response、
文件正文、日志正文、内部 FinOps memo、付款资料或平台运营 facts。

API 按当前 source permission 精确裁剪 card，而不是返回禁止字段为 null 的宽 DTO。owner/admin、developer、security、
finance、legal、auditor、viewer 得到不同 exact section 集；所有角色共享的卡片也只能是非敏感 Workspace 摘要。投影中的
capability 只是当前快照提示，点击后来源 API 仍重新认证、授权并检查 revision。

### 3.3 DomainApprovalQueue

DomainApprovalQueue 是可重建只读索引，不是第二套 ApprovalTask。物理表为
`platform_domain_approval_queue_items/source_cursors/rejections` 和
`workspace_domain_approval_queue_items/source_cursors/rejections`。每条 item 保存稳定 projection ID、来源 domain/code、
来源 opaque ID/revision、状态、风险 band、申请 actor domain/pseudonym、所需 decision permission、deadline、固定 summary
codes、native decision contract code、projection generation/hash 与 UTC；Workspace item 额外保存复合 owner。

队列不保存通用 approve/reject command、step-up token、幂等键、decision reason 正文或 writeback payload。列表和详情只能
跳转到源码注册表中固定的 native client：0071 审批继续由既有 Workspace task/detail/decision/writeback 执行，PRD-139..144
的平台审批继续由各自 command endpoint 执行。新 `/platform/approvals` 和 `/workspace/approvals` 绝不接收 decision body，
也不根据前端 action 文本拼接 endpoint。

projector 在入队前验证来源 schema、owner、actor domain、source revision 单调性和 native decision contract；若来源显式
指定唯一 reviewer，还要证明 reviewer 与 applicant 不是同一账号。申请人与审批人可以同属 Workspace actor domain，
“同域”本身不是自批。无法证明时不发布 item，而是追加不可变 rejection，并在同事务写
`approval.domain_rejected` + outbox。固定 reason 仅限 `owner_unmapped|actor_domain_mismatch|applicant_decider_overlap|`
`source_revision_regressed|source_schema_invalid`。该事件表示投影入队被域边界拒绝，不表示业务审批被驳回；普通 HTTP
401/403 不生成该事件，避免用事件量探测身份。

来源域仍在 decision 事务内强制申请人与批准人不同、actor domain 一致、step-up、幂等和 CAS。队列只显示当前 actor
可读且来源权限仍有效的 item；来源撤权、owner 变化或终态通过事件/定期 reconciliation 更新投影，迟到 cursor 不能覆盖
更高 revision。

### 3.4 AssistantQueryProjection

`platform_assistant_query_projections` 使用 `paq_*`，保存 Platform operator/session revision、query type、outcome、
source type 集、claim/source/redaction counts、latency bucket、permission revision/fingerprint、execution mode、固定 failure
code 与 UTC。`workspace_assistant_query_projections` 使用 `waq_*` 并重复保存复合 Workspace owner、Membership/session
revision 和同类安全元数据。两表均只追加且各自索引，不共享 actor 或 cursor。

两表没有 question、answer、Prompt、Response、citation label/body、source object ID、可逆 body hash、Provider Key、Token、
Header、URL 或自由文本列。问题只存在于单次请求内存；回答和 citation 只存在于 HTTP response 与当前页面闭包。服务端
日志只记录 query ID、固定 outcome、计数、duration bucket、request_id/trace_id，不记录请求 body。不得为了去重保存问题
hash；低熵问题的普通 hash 可被字典反查。

初始实现延续 PRD-077 的 deterministic read model，不调用外部模型、不执行 tool、不写来源对象。Platform 与 Workspace
分别使用编译期固定 source registry；每项 source resolver 声明主体域、额外 permission、owner 规则、deadline、exact
claim/citation schema 和 redaction。解析完 query type 后先做 source permission 交集，再逐对象鉴权，然后读取安全投影；
任何 citation 被点击时重新调用来源 endpoint，不把旧授权快照当 capability。

查询开始、每个来源读取和回答提交前都验证会话与权限指纹。来源部分失败可返回显式 `partial` 和固定 unavailable sources；
认证、owner、schema 或 redaction 无法证明时失败关闭，不把上一次回答补入。网络未知结果不自动重试；用户显式再次提交是
新的审计 attempt。

### 3.5 AccountPreference

`account_preferences` 使用稳定 `acp_*`，以 `account_id` 唯一绑定 `global_accounts`，保存 locale
`zh-CN|en-US`、status `active`、revision、source `legacy_backfill|identity`、created/updated UTC。它不保存 tenant、Workspace、
role、浏览器原始 Accept-Language、IP、device fingerprint 或导航选择。available locales 是版本化产品目录，不重复入表。

`account_preference_commands/events/outbox/source_mappings` 保存幂等 HMAC、canonical body hash、quoted expected revision、
actor GlobalSession ID、旧/新 locale、result revision、来源 legacy preference/event ID hash 与 UTC。幂等键不明文落库；
event/outbox 只承载 Excel 的 `account_preference.changed`。PRD-146 的 NavigationPreference 是独立对象，
`PATCH /identity/preferences/navigation` 不得修改本表 locale 或复用其 revision。

同值 PATCH 是成功的无变化结果，不发事件；revision 变化时 preference、command、不可变 event 与 outbox 同事务提交。
语言变化不能创建/修改 Membership、Platform role、permission、owner 或当前 Workspace，也不能触发权限缓存复用。

### 3.6 DeploymentDiagnosticProjection 与状态来源

`workspace_deployment_diagnostic_versions/heads/sources` 使用稳定 `ddp_*`，带复合 Workspace owner，保存 Deployment opaque
ID、kind、region code、release/compatibility band、desired/observed generation、Gateway/Provider/status/config-distribution
水位、健康状态、固定 reason/action codes、evidence digest、projection revision 和 UTC。版本只追加，head 以 CAS 发布。

标准 DTO 允许 owner/admin/developer 看到定位与恢复所需的安全部署字段；auditor DTO 只显示 Deployment pseudonym、状态、
release band、证据摘要和 UTC。任何 DTO 都不含环境变量名/值、配置文件或源码路径、内部 token/Key 是否配置、Provider
Endpoint、主机名/IP、数据库 DSN、KMS material、Runner command、堆栈、原始日志或支持包正文。viewer、finance、legal 和
无对应权限的 security 角色固定 403，不能靠 `config-summary` 菜单存在进入。

PRD-112 `GET /incidents` 与 `GET /platform/status-incidents` 继续是状态详情权威 API；PRD-109/112 Provider/Cell 健康和
incident facts 只作为 Dashboard/diagnostic 来源，不被复制成可写状态。Platform 运行安全摘要进入 Platform overview 的
source-permission section；Excel 未定义独立 Platform deployment-summary API，因此本条不另造宽配置接口。

旧 `/internal/config/summary` 不再被浏览器 client 使用。0137 将其限定为内部兼容诊断并加 browser credential 拒绝与
feature flag；dev/test 的历史免 token 行为不得成为 Console 授权。PRD-147 从外部 app router 删除该路径。

### 3.7 事件、outbox、刷新与 Scheduler

物理事件按 owner 分开：`platform_console_events/outbox`、`workspace_console_events/outbox` 和
`account_preference_events/outbox`。禁止用一个 nullable owner 表混合 GlobalAccount、Platform 与 Workspace。只允许以下
四个 Excel event type：

| Event | 唯一触发点 | 安全 payload |
| --- | --- | --- |
| `platform_dashboard.refreshed` | 新 Platform head generation 成功 CAS 发布 | projection ID/generation、source watermarks hash、status、UTC |
| `workspace_dashboard.refreshed` | 新 Workspace head generation 成功 CAS 发布 | 复合 owner safe ref、projection ID/generation、status、UTC |
| `approval.domain_rejected` | projector 同事务拒绝不可证明的来源 item | actor domain、source type、keyed source digest、固定 reason、UTC |
| `account_preference.changed` | GlobalAccount locale revision 首次成功变化 | preference ID、revision、old/new locale、UTC |

事件不包含 Dashboard cards、问题/回答、审批备注、客户名称、内部配置、Key、Token、Prompt、Response 或自由文本。相同
source/revision/generation 的 replay 复用唯一 event key；dispatcher 至少一次投递，消费者按 event ID 幂等。

0137 安装一个 `install_console_experience_scheduler` supervisor，内部保留
`platform_dashboard_refresh`、`workspace_dashboard_refresh`、`platform_approval_projection`、
`workspace_approval_projection`、`workspace_deployment_diagnostic`、三域 outbox 和 reconciliation 的独立 queue code、
lease、fencing generation/token digest、attempt、next-attempt 与 watermark。一个 installer 不等于一个无界循环；每个队列
独立预算、退避、并发和失败恢复。

0070 assistant audit scheduler、0071 approval timeout/writeback scheduler、PRD-112 status publisher 继续拥有原事实和水位，
0137 不复制、不并行消费其工作项。PRD-147 再把各 installer 注册到全局 SchedulerRegistry。每轮 refresh/event 提交必须
验证当前 fence；失租 worker 的迟到 version/head/event/watermark 写入全部失败关闭。

## 4. 0018/0019/0070/0071 与共享页面迁移

0137 采用 expand/inventory/backfill/shadow/quiesce/cutover/contract。迁移要求 current head 0136，逐阶段保存 row count、
canonical hash、owner coverage、最大业务序列、pending/processing outbox、lease、审计和 source watermark。backfill 不伪造
四个新事件，不把历史旧 event 翻译或双发为新 event。

### 4.1 Dashboard、状态与配置来源

1. inventory 0018 dashboard/role-view 的 metric 来源、PRD-109/112 status、0131..0136 平台域与 Workspace 业务投影；记录
   每个 source 可用 revision、水位、owner coverage 和敏感字段白名单。
2. 对每个 active WorkspaceTenantBinding 建首个 Workspace projection；无法唯一映射 tenant 的旧聚合进入隔离报告并阻断
   该 owner cutover，不能映射到默认 Workspace。Platform projection 只从平台域重算，不从旧 tenant Dashboard 上卷。
3. shadow 按相同 Workspace、窗口和 source watermark 比较旧/new metric code、整数值、状态和 action capability；旧 API
   没有对应平台语义时显式记录 `not_comparable`，不能用零值伪造通过。
4. status 详情继续引用 PRD-112 source ID；projection 只保存水位和安全摘要。`/internal/config/summary` 的 key/source/
   preview 不回填到 diagnostic，改为从 Deployment/Edge/config-distribution 的白名单事实重新生成。

### 4.2 Assistant 与 Approval

0070 `assistant_queries/assistant_audits` 是 tenant-owned 历史事实。逐行通过
tenant -> active WorkspaceTenantBinding、actor user -> account_identity_link/Membership 映射到 Workspace 安全 query
projection，保留 legacy query ID mapping、固定 outcome/counts/duration、权限 revision 和审计水位；问题/回答原本无列，
0137 也不新增。缺失或多重 owner、actor 漂移、非法 source type/hash 立即阻断，不创建 Platform 副本。

0071 `approval_tasks/decisions/step-up/writebacks/events/outbox/scheduler_tenants` 继续是 Workspace 审批权威事实。backfill 只为
每个可证明 owner 的 task 创建 Workspace queue item/source cursor；decision、票据、幂等、writeback 与 event 原样留在来源。
0131..0136 的平台 native approvals 只进入 Platform queue。tenant task 绝不因为平台 operator 能见聚合而迁入 Platform。

兼容期旧 `/admin-assistant/query` 和 `GET /approvals` 只服务旧客户端，不能被新页面回退调用。cutover 前停止接收新的旧
assistant query，等待已登记 attempt 与 audit outbox 完成/过期；审批决策 worker 不停机，新 queue 只切换列表 reader。
0071 native detail/decision endpoint 继续供其来源 item 使用，但永远不挂载到 Platform 页面。

### 4.3 tenant 偏好迁到 GlobalAccount

每个 `user_preferences` 通过 `(tenant_id,user_id) -> account_identity_links -> global_accounts` 建 source mapping；对应
`user.locale_changed` event 也按同一 account 归档水位。未映射、migration_conflict account、停用 link 或一个 legacy user
映射多个 account 都进入阻断报告，不允许按 email 临时关联。

一个 GlobalAccount 可有多个 legacy preference。0137 只迁 locale；`active_navigation_item_id/sidebar_collapsed` 留给
PRD-146 NavigationPreference，不能夹带进 AccountPreference。locale 选择规则固定且可审计：最新显式
`user.locale_changed` event 优先；没有
event 时使用最新 preference `updated_at`；时间相同且 locale 冲突时写 `needs_confirmation`，使用产品默认 locale 作为明确
临时值并在 Identity 设置中要求本人确认。所有候选、选中来源、规则版本与 source hash 保留在 mapping；不静默丢弃冲突。
本人首次 PATCH 后改为 `confirmed`。backfill 不发 `account_preference.changed`。

cutover 后 `/identity/preferences` 是唯一新读写 owner。旧 `GET /user/preferences` 在短兼容窗口通过已验证
account_identity_link 只读适配同一 AccountPreference；无法证明 link 时失败关闭。旧 `PATCH /user/preferences` 在切换时
立即 write-fence，不能用缺少 Global CSRF、Idempotency-Key 或 If-Match 的旧合同修改全局偏好。PRD-147 卸载旧 route 和
语言 tenant 页面。旧 `user_preference_events` 保留历史，不翻译、删除或继续双发。

### 4.4 Shadow、切换、回滚与 down guard

shadow 至少验证：两域 route/session 互斥；所有 active Workspace 的投影 owner/水位完整；Platform 聚合不含可反查客户
字段；旧/new Workspace Dashboard 在相同窗口匹配；assistant metadata、approval task 与 preference source mapping 行数/hash
匹配；诊断 whitelist 无禁用字段；event/outbox/lease 静止。

quiesce 先 fence legacy Dashboard refresh、旧 assistant ingress 和旧 preference writer，等待在途 lease 完成或过期；
Approval/Status native worker 保持运行。最终 reconciliation 通过后 CAS 切换 Platform/Workspace page reader、assistant
ingress、approval list reader、identity preference writer 和 diagnostic reader。不同 owner 可逐批切换，但同一 owner 的
route/client/store/reader 必须原子切换，不能同页混读。

rollback 只切回上一代 target-compatible reader/scheduler generation。它不能复活共享 Platform/Workspace store、恢复
`/internal/config/summary` 到普通 UI、让 tenant preference 覆盖 GlobalAccount、让旧 Assistant 读取新域，或让旧 approval
列表显示 Platform item。新域无法证明时页面应明确 unavailable，而不是回退到越权旧数据。

0137 down 仅在所有 projection/version/head/source、queue/rejection/cursor、assistant projection、AccountPreference/mapping/
command/event/outbox、diagnostic、refresh/reconciliation、permission extension、lease 和审计事实均 pristine 且从未 backfill
时允许。任一目标或 mapping 行存在即在首个 DDL 前拒绝；down 不修改 0018/0019/0070/0071/0083/PRD-112/0131..0136。
空库验证 `0136 -> 0137 -> 0136 -> 0137`，生产 rollback 不执行 destructive down。

## 5. 八个 Excel API exact 合同

所有 DTO `extra=forbid`、strict type、固定枚举与有界数组。未知/重复 query、重复 JSON key、非规范 UTF-8、客户端 owner、
任意 role/permission、非法 cursor 和超限 body 固定拒绝。响应携带 request_id/trace_id、当前 permission revision/fingerprint、
domain/navigation epoch、projection generation，并使用 `Cache-Control: no-store, private` 和适用的 quoted ETag。

认证在解析业务筛选/body 前完成；跨域身份不能利用 422 差异探测合同。跨 Workspace 同形资源 404，权限不足 403，依赖或
快照无法证明 503。GET 接受单个 `If-None-Match`；cursor 是绑定 domain、复合 owner、filter、sort、permission fingerprint、
epoch 和 TTL 的 HMAC，不接受 offset。

### 5.1 `GET /platform/overview`

不接受 query。要求 PlatformSession + `platform.console.overview.read`，按当前来源权限返回 exact section 数组、每段 metric/
state/action codes、source freshness、generation、observed_at 和 overall `ready|partial|stale`。billing/support/security 等角色
只获得自己的 section；不返回 Workspace 目录、客户名称、联系人、业务正文、Provider Endpoint、Secret 或配置明细。

### 5.2 `GET /workspace/overview`

不接受 owner/query。要求 WorkspaceSession + `workspace.console.overview.read`，只读取当前复合 owner 的单一 head，并按当前
source permission 裁剪 section。响应含角色任务、Gateway/Provider 健康、治理、审批、用量成本、通知和 Deployment 的安全
摘要及固定 actions，不返回 Platform 运营、其他 Workspace、请求/文件/Prompt 正文或客户提交 owner。

### 5.3 `POST /platform/assistant/query`

要求 PlatformSession、`platform.console.assistant.query`、exact Origin/Platform CSRF 和 body
`{"question": <1..2000 Unicode scalar>}`，不接受 Idempotency-Key、历史消息、system prompt、model、tool、temperature、owner、
Workspace ID 或 source list。响应是单轮 deterministic answer、typed claims、当前可读 citation 和 partial/source status。
每次 POST 是独立审计 attempt；网络未知不自动重试。

### 5.4 `POST /workspace/assistant/query`

传输合同与 Platform 版本相同，但只接受 WorkspaceSession + `workspace.console.assistant.query`，source resolver 额外绑定当前
复合 owner、Membership revision 和来源权限。Platform/其他 Workspace citation 固定不可解析；回答提交前 owner 或权限漂移
则整轮失败关闭。问题、回答和 citations 在 response 完成或断开后从服务内存释放。

### 5.5 `GET /platform/approvals`

允许固定 `status/source_domain/risk/limit<=100/cursor`。要求 PlatformSession + `platform.console.approval.read`，先按 Platform
queue 和 actor domain 分页，再按当前 native source permission 裁剪。item 只返回安全 summary、source revision、deadline、
status 和 native decision contract code；不返回 Workspace owner、申请备注、step-up/幂等/writeback payload 或通用 action
URL。无 Platform 权限的同形 ID 不可由该列表枚举。

### 5.6 `GET /workspace/approvals`

筛选合同同上，但 owner 只能来自 WorkspaceSession。先锁定当前复合 owner，再按来源权限与 applicant/decider domain 裁剪；
不返回 Platform item 或其他 Workspace。item 的 `can_open` 仅说明当前 native detail 可访问，实际决定仍重新鉴权、检查申请人
非本人、step-up、CAS 与幂等。新接口不提供 approve/reject command。

### 5.7 `GET /identity/preferences`

不接受 query，只接受 GlobalSession，返回当前本人 preference ID、locale、available locales、revision、confirmation state、
updated_at 和 quoted ETag；不返回 account email、tenant/Workspace、role、permission 或 source legacy IDs。语言设置从
Identity 壳层加载，不依赖当前 Platform/Workspace 页面成功。

为使 Excel 明确要求的“语言切换”和 `account_preference.changed` 可达，0137 同时提供窄辅助命令
`PATCH /identity/preferences`。它不是第九组业务对象 API，只接受 `{"locale":"zh-CN|en-US"}`、exact Origin/Global CSRF、
`Idempotency-Key` 和 quoted `If-Match`；不接受 navigation、owner、role 或其他偏好。PRD-146 的 navigation PATCH 使用独立
子路径和对象。

### 5.8 `GET /workspace/deployment-summary`

不接受 owner/query。`workspace.deployment_diagnostic.read` 返回可操作 exact DTO；仅有
`workspace.deployment_diagnostic.audit_read` 返回更窄 auditor DTO。响应按当前 Workspace 的 Deployment 列出状态、region、
release/compatibility band、desired/observed generation、固定 reason/action、source freshness 和 evidence digest，不包含内部
配置、地址、路径、环境变量、token 状态、日志正文或研发术语。无权限角色固定 403，不降级返回旧 config summary。

## 6. 前端 route、store 与迟到响应

Platform 使用 `/platform.html#overview|admin-assistant|approvals|system-status|config-summary`；Workspace 继续使用
`/#overview|admin-assistant|approvals|system-status`，语言设置迁到 `/identity.html#preferences`，Workspace 环境信息改为
`/#deployment-summary` 高级入口。PRD-146 后端导航合同将接管 Workspace 可见菜单，但本条先完成页面物理分域。

同名页面可以复用无状态 presentational primitive、格式化器、错误码字典和可访问性组件；不得共享鉴权 client、DTO parser、
mutable store、cache key、AbortController、cursor、navigation epoch、permission fingerprint 或响应对象。Platform 与 Workspace
分别建立 overview/assistant/approval/status clients 与 page-state modules；AccountPreference store 只属于 Identity shell。

每次 route change、session change、permission refresh、Workspace switch、403/401、logout、pagehide 和 navigation revoke 固定
执行：先推进当前 shell epoch并使 ticket 失效，再 abort 全部 lane，随后同步擦除 question/answer/citation、approval cursor/
detail/action、Dashboard/diagnostic DOM 和错误，最后才发新路由请求。所有 accept/reject callback 同时核对 shell domain、
session/account、复合 owner、permission fingerprint/revision、epoch、route、generation 和 AbortSignal；fetch 忽略 abort 时也不能
提交迟到值。

当前路由只加载其 exact API 与必需的 auth/navigation/preference 壳层数据。overview 不预取 assistant/approval/status detail，
assistant 不保留上一 Workspace 回答，Platform refresh 不刷新 Workspace store。切换时先显示尺寸稳定的 loading/empty/
partial/stale/revoked/unavailable 状态，禁止旧内容留在 loading 背后。390/1280/1440、DPR 1/1.25/1.5、键盘、焦点、读屏、
无重叠/裁剪/横向溢出与直接 URL 负测试在 G7 真实浏览器统一验收。

## 7. 安全、审计、恢复与 SLO

所有读取也记录有界安全审计：actor domain/pseudonym、owner digest、action、permission revision/fingerprint、projection/source
watermark、outcome、request_id/trace_id 和 UTC。审计、应用日志、event/outbox 与验收证据不得出现 Cookie、CSRF、Bearer、
Idempotency-Key、question/answer/citation 正文、Prompt/Response、Provider Key/Endpoint、配置值、路径、审批备注或客户业务正文。

助手解析与 source resolver 使用固定 deadline、并发上限和 per-actor/owner rate limit；Dashboard/approval/diagnostic API 只读
覆盖索引与当前 head，禁止 request-time 全表聚合。DB/Redis/KMS/source/scheduler 故障均失败关闭或返回明确 partial/stale，
不能用空数组或零值冒充成功。reconciliation 从持久 cursor 恢复，processing lease 到期后接管，不重放已发布 head/event。

固定 SLO：

- 页面切换并达到可交互状态 p75 `<1.5s`；只加载当前 route 依赖，基准和测量均剔除人为等待但包含真实网络与渲染。
- 普通 GET 读 p95 `<300ms`；命中已发布投影和覆盖索引，性能验收同时报告 error rate、DB time 和冷/热分布。
- 在线撤权 `<2s`；从权限 revision 提交成功到页面清理且下一请求失败计算，不以仅隐藏按钮为通过。

页面和 API 固定支持 loading、empty、partial、stale、permission revoked、conflict、dependency unavailable 与 recovery in
progress。未知 POST 结果不自动重复；AccountPreference 可用同一幂等键安全恢复，assistant 明确提示用户重新发起新 attempt。

## 8. 聚焦测试与批次证据

PRD-145 开发期只运行受影响的 unit/API smoke，不重复 G7 全套：

- migration：0136 current head、fresh/up/down guard/replay、复合 FK、GlobalAccount mapping、冲突 preference、row/hash/watermark、
  immutable event/outbox、fence 与空库往返；
- 后端：三类 Session exact/mixed/duplicate、旧权限不能授权新 route、角色/source permission 交集、跨 Workspace 同形 404、
  dynamic revoke、projection generation、partial/stale、cursor/ETag、strict body、assistant no-persist、approval read-only、
  preference CAS/idempotency、diagnostic whitelist；
- scheduler：多 replica 单 leader、失租迟到写失败、lease 接管、outbox replay、source revision regression、rejection event 唯一、
  restart 静止终态；
- 前端：Platform/Workspace/Identity store 不共享、route-only fetch、switch/logout/revoke/pagehide 清理、迟到成功/失败都丢弃、
  direct hash、unknown route、无权限不 prefetch、敏感 DOM 同步清空；
- 静态敏感审计：代码、日志、fixture、snapshot 与正式证据扫描 Key/Token/Cookie/CSRF/Prompt/Response、问题回答、配置值、
  内部地址、路径和自由备注；本地测试凭据只在内存输入，不打印、不持久化。

PRD-145 保留独立设计、migration/API/unit/browser smoke 记录、敏感审计和 Git commit。PRD-147 独立提交后再统一执行 G7：
SQLite/PostgreSQL、0130->0139 迁移往返、集成/FLOW/真实 API/浏览器、性能、安全、故障恢复、N/N-1、Windows/Linux、长稳、
必要回归和正式 `.artifacts` 证据。`.tmp` 不作为交付证据。

## 9. 预计改动边界

实现限定在 0137 up/down migration、Platform/Workspace console-experience backend、Identity preference adapter、固定权限加载、
projection scheduler、shared exact contracts、Platform/Workspace/Identity 独立前端 feature/client/state、i18n 与 PRD-145 专项测试/
文档。可复用的代码只限纯校验、cursor/ETag、固定 error envelope、presentational primitive 和不可变 DTO helper。

不修改 PRD-139..144 来源业务状态机，不把 0071 decision/writeback 移进 queue，不改变 PRD-112 incident publisher，不实施
PRD-146 navigation/getting-started/advanced-tools，不创建 PRD-147 tombstone/reconciliation/SchedulerRegistry，不删除历史
0018/0019/0070/0071 表，也不触碰客户 Prompt、Response、Provider Secret 或本地测试凭据。

## 10. 两轮编码前自审

### 第一轮：主体、物理 owner、唯一事实与迁移

- P0：Excel 将 Approval/Assistant 标为 Workspace owner，但又要求 Platform API；若用一个 nullable owner 表会形成跨域枚举。
  已改为 Platform global 与 Workspace composite 两套物理表、cursor、事件和索引，逻辑 DTO 共享不等于 owner 共享。
- P0：统一审批页若提供 approve/reject 会成为第二套通用执行器并绕过来源 step-up/writeback。已冻结为只读 queue，decision
  contract 只路由到编译期注册的 native endpoint，来源事务继续执行 SoD、CAS、幂等和审计。
- P0：旧 locale 绑定 tenant，与 GlobalAccount 目标冲突。已增加 account source mapping、多 tenant 冲突选择规则、
  `needs_confirmation` 和窄 Identity PATCH；backfill 不发新事件，旧历史不翻译或双发。
- P1：旧 Dashboard request-time 聚合无法证明一致水位。已改为不可变 version + CAS head + per-source watermark，partial/stale
  明确展示且无快照时 503。
- P1：旧 config summary 可暴露研发配置且 dev/test 有历史免 token 行为。已从 Console 数据流移除，Deployment diagnostic
  仅重建白名单字段，旧 path 只留内部兼容并由 PRD-147 卸载。
- P1：Assistant 审计若保存问题 hash 仍可反查低熵输入。已明确禁止 body 和可逆/普通 hash，只保留固定类型、计数、权限
  指纹和 duration bucket。

第一轮结论：六个 Excel 对象均有明确唯一事实、物理 owner、迁移来源、事件 owner 和 down guard；无未解决 P0/P1。

### 第二轮：API 可达性、迟到响应、恢复与验收真实性

- P0：Excel 只列 preference GET，但要求语言切换和 `account_preference.changed`；没有写入口则闭环不可达。已补充仅 locale 的
  `PATCH /identity/preferences`，使用 Global CSRF、幂等与 If-Match，并与 PRD-146 navigation preference 分离。
- P0：Platform/Workspace 共享现有 assistant/approval state 会让 late response 污染切换后的页面。已要求 route、DTO、client、
  store、cursor、AbortController 和 epoch 全部分离，回调同时验证 domain/session/owner/permission/route/generation。
- P0：assistant citation 若只在查询时鉴权，撤权后仍能从旧回答打开对象。已要求 citation 只在当前 response 内存存在，点击
  必须重新访问来源 endpoint；query 提交前也重验权限指纹。
- P1：`approval.domain_rejected` 容易被误解为业务 reject 或由每个 403 产生噪声。已限定为 projector 拒绝不可证明 source
  item 的持久事件，固定 reason、keyed digest，同一 source revision 唯一；业务驳回继续使用来源事件。
- P1：rollback 若重新挂载共享页面会恢复越权风险。已限制为 target-compatible reader/scheduler generation，失败时明确
  unavailable，不能恢复共享 store、tenant preference、config summary UI 或跨域 queue。
- P1：SLO 若只测 fetch 或菜单隐藏会产生虚假通过。已固定页面 SLO 包含渲染，撤权从服务端 revision 提交到 DOM 清理与
  下一请求失败，读 SLO报告冷/热、error rate 和 DB time。

第二轮结论：八个 Excel API、四个事件、三个 SLO、失败恢复、前端生命周期和批次证据均有可执行验收；无未解决 P0/P1，
可以在 G6 通过后进入 S7 连续编码。
