# PRD-147 存量控制台分域迁移、旧入口归零与调度治理

状态：编码前设计冻结。Migration 固定为 `0139`，要求 current head 为 `0138`。本条是 S7
平台运营分域批次的最后一个交付，只负责把 PRD-139..146 已建立的目标域统一回填、切换、对账、
退役旧入口并接管后台执行面；G6 通过前不得开始 0131..0139 migration 或业务编码，0139 完成并
独立提交后才统一执行 G7。

## 1. 产品闭环与非目标

存量控制台迁移不是一次删路由或改侧栏，而是一个可恢复、可审计、可证明静止终态的控制面。
平台迁移负责人必须能从同一工作台完成 inventory、backfill、shadow、quiesce、cutover、验证和
rollback，并证明每个 Workspace 的历史事实、owner、event/outbox、审计和 Scheduler 水位未丢失。

本条完成以下生产闭环：

1. 对 0131..0138 八个目标域逐 Workspace 建立显式迁移状态机、回填游标、源/目标对账和不可变操作记录；
   0139 SQL 只 expand schema 和种子合同，不在 DDL 事务内执行大规模业务回填。
2. 只有来源/目标 row count、keyed canonical digest、复合 owner、FK、event/outbox、审计、Scheduler
   watermark、导航合同和跨域负测试全部匹配，才允许 CAS 切换 reader/writer generation。
3. 64 项 Excel 导航和四个合法后增项保留唯一处置；六个迁出项以及 PRD-139..146 的旧 API、route、
   import、client、DOM、prefetch、permission、遥测和新审计写入从生产执行面归零。
4. 旧入口在外部 router 上不存在并返回普通统一 404。LegacyRouteTombstone 只保存内部迁移事实，
   不形成 retired 响应、查询 API、兼容页面或可枚举目录。
5. 源码当前 39 个受 `control_background_schedulers_enabled` 管理的 scheduler context、始终安装的
   status publication 和条件安装的 gateway snapshot 共 41 个基线 slot 必须逐项映射；按 PRD-140..146 的原位替换/
   合并合同，S7 最终为 36 个 installer context，另将 `platform-job-runtime` 登记为第 37 个集群 context。
   多 Web 副本通过逐 queue partition 的持久 lease、fencing generation 和水位接管，不能重复提交。
6. 每个 Web 副本只运行一个无业务提交权的有界 scheduler coordinator；它取得 queue partition lease 后才创建对应
   worker。`risk-fact-sink` 和 `control-telemetry` 继续作为每副本本地有界缓冲，不错误提升为集群单例；三个本地
   runtime 都进入运行面 inventory、容量、丢弃计数和关闭排空审计。
7. N/N-1、撤权、崩溃接管和生产 rollback 都不能解除旧表 write guard，不能复活旧权限、route、DOM、
   prefetch、API 或共享 Platform/Workspace 页面。

非目标包括：不重新实现 PRD-139..146 的业务命令，不创建第二套业务事实或审批执行器，不把导航可见性
当授权，不把 tombstone 暴露给外部请求，不执行 destructive production down，不从数据库动态加载 Python
模块/SQL/URL，不保留无限期 dual-write，不把 PlatformSession 转成 WorkspaceSession/SupportSession，不允许
平台身份绕过 SupportGrant 读取客户正文，也不把 Provider Key、Token、Prompt、Response、附件、日志正文、
联系人、内部备注、环境变量、文件路径或研发配置写入迁移表、事件、日志和验收证据。

## 2. 身份、职责分离与 owner

### 2.1 唯一入口主体

四个 Excel API 只接受有效 PlatformSession Cookie，并逐请求恢复 Operator、GlobalAccount、固定平台角色、
Operator revision、permission revision、完整 permission set、authorization fingerprint 和 session epoch。
父 GlobalSession Cookie可以共存但不参与授权；WorkspaceSession、SupportSession、ChannelMembership、Edge
Device、OAuth/Bearer、query credential、重复 Platform Cookie、错域 CSRF 和混合凭据全部失败关闭。

PlatformSession 只授权迁移控制事实。状态 DTO 不包含 Workspace 名称、联系人、业务对象正文、Prompt/Response、
Provider endpoint/secret、支付资料、工单正文或内部财务备注。需要查看客户正文时仍必须离开迁移工作台，按
PRD-095 使用实时 SupportSession + field-scoped SupportGrant；迁移 API 不接收 SupportGrant，也不返回其内容。

### 2.2 0139 固定权限与职责分离

0139 新增不可变 `platform_console_migration_permission_extensions/role_permissions`：

| Permission | 固定角色上限 | 语义 |
| --- | --- | --- |
| `platform.console_migration.read` | platform_owner/platform_ops/platform_security | 读取裁剪后的迁移、对账和运行面状态 |
| `platform.console_migration.backfill` | platform_ops | prepare/start/resume/cancel 回填 job |
| `platform.console_migration.approve` | platform_owner | 批准 exact cutover/rollback generation |
| `platform.console_migration.security_review` | platform_security | 复核固定跨域负测试 profile 与证据 digest |
| `platform.console_migration.execute` | platform_ops | 执行已批准且已安全复核的切换或回滚 |

固定角色只是 migration ceiling。运行时仍从当前 extension permission 和 revision 求交，不能按角色名、旧平台
admin、内部 token 或页面按钮推断能力。permission fingerprint 覆盖 Operator/GlobalAccount、role binding、全部
extension permission、session epoch 和 registry/contract generation；审批、复核、执行提交前都重新验证。

同一 GlobalAccount/Operator 不能同时成为某个 generation 的 preparer、platform_owner approver、
platform_security reviewer 和 platform_ops executor。角色叠加不能绕过该规则；审批人、复核人和执行人必须三方
不同，申请/prepare actor 也不能作为自己的 approver。批准或安全复核所绑定的 migration revision、reconciliation
digest、route manifest、scheduler manifest、permission fingerprint 任一变化都会使其失效，不能自动继承到下一代。

### 2.3 物理 owner 拆分

Excel 把五个逻辑对象标记为 Workspace/租户 owner，这是所有客户相关实例的硬下限。0139 不使用 nullable
`workspace_id/tenant_id`、泛化 `owner_type/owner_id` 或平台 sentinel 混表：

- `console_domain_migrations`、`console_backfill_cursors`、`workspace_legacy_route_tombstones`、
  `workspace_domain_reconciliations` 每行重复携带不可变 `workspace_id + tenant_id`，并以复合外键绑定唯一
  WorkspaceTenantBinding；所有 child、command、event、outbox 和 audit 行重复该 owner。
- 平台全局 batch/head、平台专属 source 和全局 route/scheduler manifest 使用独立
  `platform_console_migration_*`、`platform_legacy_route_retirements` 和 `scheduler_*` 表；它们没有 nullable
  tenant 列，也不能和 Workspace 行 UNION 后作为授权边界。
- Channel、Deployment、GlobalAccount 相关旧事实继续由各自业务表的物理 owner 约束。迁移控制行若关联客户，
  只保存不可逆 workspace reference 并通过 Workspace reconciliation child 证明复合 owner，不复制正文。
- Platform status 聚合只从安全计数和不可逆 reference 构建。单个 Workspace 详情仍先验证迁移权限和对象状态，
  不因平台角色而放宽来源业务表的读取权限。

## 3. 0139 目标数据模型

### 3.1 ConsoleDomainMigration

`console_domain_migrations/heads` 使用稳定 `cdm_*`，保存复合 Workspace owner、固定 domain code、source/target
migration version、source inventory revision、target generation、contract epoch、状态、progress basis points、当前
reconciliation ID/digest、route/scheduler manifest revision、approved/reviewed generation、quoted revision 和 UTC。
平台全局 counterpart 使用独立 `platform_console_domain_migrations/heads` 和 `pcd_*`。

固定 domain code 与 migration 一一对应：

| Domain code | Migration | 目标域 |
| --- | --- | --- |
| `channel_attribution` | 0131 | Channel Partner、lead、Workspace attribution 与 statement |
| `customer_success` | 0132 | Platform Customer Success、health、plan、task、renewal 与 QBR |
| `growth_analytics` | 0133 | Platform funnel、cohort、campaign、experiment、attribution 与 export |
| `brand_distribution` | 0134 | Platform/Channel/Deployment brand、domain、asset 与 Edge LKG |
| `provider_engineering` | 0135 | Adapter release/run、builtin error catalog 与 Workspace override |
| `commercial_accounting` | 0136 | Platform commercial ledger、Workspace subscription/dispute 与 Internal FinOps |
| `console_experience` | 0137 | Platform/Workspace overview、approval、assistant、preference 与 diagnostics |
| `workspace_navigation` | 0138 | Navigation、task home、preference、advanced tool 与 contract snapshot |

状态固定为
`inventoried|backfill_queued|backfilling|shadowing|ready|quiescing|cutting_over|validating|active|`
`rollback_queued|rolling_back|rolled_back|blocked`。状态推进只允许：

```text
inventoried -> backfill_queued -> backfilling -> shadowing -> ready
ready -> quiescing -> cutting_over -> validating -> active
ready|quiescing|cutting_over|validating|active -> rollback_queued -> rolling_back -> rolled_back
任一非终态 -> blocked；修复后只能回到同 generation 的上一个安全 checkpoint
```

`active` 和 `rolled_back` 是 generation 终态；相同命令重放返回原结果，不重复事件。任何回退都创建更高
operation generation 和补偿记录，不倒写旧状态、不减少 revision、不删除已成功 backfill 的事实。

`console_domain_migration_commands/approvals/security_reviews/events/outbox` 使用稳定 ID、幂等 HMAC、canonical
body keyed digest、actor pseudonym、quoted revision、old/new state、result generation 和 UTC。幂等 key 不明文落库；
同键异体固定冲突。审批/安全复核只保存固定 profile/version、结果、计数和证据 digest，不保存测试凭据、请求正文、
真实客户 ID、响应 body 或证据文件路径。

### 3.2 ConsoleBackfillCursor

`console_backfill_cursors/checkpoints/attempts` 使用稳定 `cbf_*`，带复合 owner、domain/mapping code、固定 source
table/version、target table/version、source upper watermark、last stable source key digest、source/target row count、
keyed canonical digest、owner/FK coverage、event/outbox/audit/scheduler watermarks、attempt、状态、fencing generation、
lease token digest、deadline、revision 和 UTC。

cursor 只保存不透明 source key 的 HMAC，不保存可枚举的连续客户 ID、自由 JSON 或业务行快照。canonical digest 使用
KMS 管理、按 `domain + table + schema_version` 分域的 HMAC key；普通 SHA-256 只允许用于高熵公开制品。key version
持久化但 key/token 不落库。small-domain 值、email、hostname、route query、备注和正文不能进入普通 hash 证据。

每个 cursor 在读取前冻结 source upper watermark，按稳定 keyset 分页；同一 source row + source revision 对 target
唯一。重试先比较 target mapping 和 digest，匹配即 no-op，不匹配进入 `blocked`，不覆盖较高 revision。checkpoint、target
写、mapping、审计和 outbox 在同一事务提交，并验证当前 registry/domain 双重 fence。

### 3.3 LegacyRouteTombstone

`legacy_route_catalog` 是编译期固定平台目录，按 normalized method + route code 保存 source PRD、owner domain、retire
contract epoch、replacement code 或 `none`、permission code、production manifest digest 和状态。客户相关 retirement 使用
`workspace_legacy_route_tombstones` 的复合 owner 行；平台专属 retirement 使用独立
`platform_legacy_route_retirements`。逻辑稳定 ID 为 `lrt_*`。

状态只允许 `planned -> write_fenced -> router_removed -> verified`，不可反向。`verified` 要求 route registration、OpenAPI、
shipped client/import、DOM/selector、prefetch、Navigation、new telemetry writer 和 new audit writer 全部零命中；历史数据库
event、outbox、migration mapping、测试 fixture 和设计文档不删除，也不计作生产执行面命中。

tombstone 没有公开 GET、redirect 或 retired response。请求到达外部 router 时目标 route 已不存在，只得到与未知路径相同的
普通 404、统一 request_id 和 `Cache-Control: no-store`；不能通过状态码、body、时延或 header 区分“曾经存在”。

### 3.4 NavigationContractSnapshot 与 ConsoleContractRelease

Excel 再次列出 NavigationContractSnapshot，但 0138 已建立唯一权威
`workspace_navigation_contract_snapshots/items/heads`。0139 不创建同名第二套表，也不复制 64+4 item；它只引用当前
0138 immutable snapshot、disposition digest、keyed item-set digest 和 head generation。

0139 新增平台全局 `console_contract_releases/items/heads`，把 0131..0138 target generation、0138 navigation snapshot、
external route deny manifest、API/OpenAPI manifest、permission retirement manifest、frontend bundle manifest、telemetry/audit
manifest 和 scheduler manifest 固定到同一 release generation。released row 只追加，head 只用 CAS 推进。

64 项 Excel disposition 必须零重复、零缺失；源码合法 extras 精确为 `migration-operations`、`support-tickets`、
`workspace-subscription` 和 `internal-finops`。后两项替代旧 `plans`/`finops-ledger` primary placement。最终
Workspace 公开合同只退休 `growth-analytics`、`customer-health`、`partner-portal`、`adapter-specs`、
`adapter-contract-runs`、`competitor-comparison` 六项；五个此前误隐藏的 `providers`、`local-models`、
`provider-health`、`error-maps`、`billing-disputes` 必须在来源权限和 owner 可证明时恢复。

### 3.5 DomainReconciliation

`workspace_domain_reconciliations/checks` 使用稳定 `drc_*`，带复合 owner、domain/operation generation、source/target
watermark、contract release、check profile/version、状态 `pending|matched|mismatched|unavailable|superseded`、固定 reason、
expected/observed count、expected/observed keyed digest、coverage basis points、checked_at、revision 和 UTC。平台全局检查使用
独立 `platform_domain_reconciliations/checks`。

check type 固定为：

- `source_target_rows`、`canonical_digest`、`owner_coverage`、`foreign_keys`；
- `event_outbox`、`audit_watermark`、`scheduler_watermark`、`legacy_write_fence`；
- `route_registration_zero`、`openapi_zero`、`frontend_runtime_zero`、`prefetch_zero`；
- `permission_retirement`、`navigation_64_plus_4`、`cross_domain_negative`、`target_smoke`；
- `n_minus_one_fenced`、`late_writer_fenced`、`static_sensitive_zero`。

check row 不保存 arbitrary payload、SQL、URL、selector、文件路径、错误堆栈或响应正文。详细机器证据只写 `.artifacts`，
数据库保存固定 artifact kind、不可逆 artifact digest、tool version、result count 和 retention class。所有 mandatory check
必须同一 generation 为 `matched`；`unavailable`、过期、缺失或额外未知 check 都阻断 cutover。

### 3.6 SchedulerRegistry、lease 与 watermark

0139 新增 `scheduler_registry_entries/queue_contracts/heads`、`scheduler_leases`、`scheduler_watermarks` 和
`runtime_surface_observations`。registry entry 保存编译期 scheduler code、installer/callback code、execution class、允许 owner
domain、queue code、config condition、lease/poll/backoff policy code、watermark schema version、manifest digest、enabled、revision
和 UTC；不保存可执行 module path、任意 SQL、URL 或动态 callable。

lease 以 `(scheduler_code, partition_code)` 唯一，保存 lease owner pseudonym、deadline、fencing generation、token HMAC、
last started/completed/error code、revision 和 UTC。原始 token 只存在 worker 内存；时间以数据库时钟为准。watermark 保存固定
source code、opaque checkpoint HMAC、业务 generation、last committed fence 和 UTC，不保存业务 payload。

数据库 registry 只能启用当前二进制固定 manifest 中存在且 digest 匹配的 code；未知、重复、缺失或 owner contract 漂移时
worker fail closed。配置关闭的 entry 即使数据库 enabled 也不能启动；数据库 disabled 可进一步收缩。旧 scheduler 自身已有
job/owner lease 时继续保留，外层 registry lease 不替代业务 lease，两层 generation 都必须在最终事实/event/outbox/watermark
提交条件中匹配。

## 4. 0131..0138 inventory 与迁移状态机

### 4.1 固定来源与旧执行面

| Migration | 必须保真的来源 | 目标切换 | 最终旧执行面 |
| --- | --- | --- | --- |
| 0131 | PRD-087 partner/commission 六表、mapping、event/outbox、水位 | Channel Partner/Attribution/Statement | `/partners/*`、企业 `partner-portal`、旧 permission/client/prefetch |
| 0132 | PRD-086 tenant health score、scheduler state、event/outbox | Platform Customer Success | `GET /customer-health`、企业 `customer-health`、旧 permission/restore target |
| 0133 | ProductEvent/outbox、consent-at-event、旧 funnel 算法水位 | Platform Growth | `GET /product-analytics/funnel`、企业 `growth-analytics`、内嵌旧组件 |
| 0134 | PRD-088 brand/domain/asset/event/outbox/LKG | Platform/Channel/Deployment brand | tenant `GET/PATCH /branding`、`/branding/logo`、tenant_slug public query |
| 0135 | 0046/0055/0056 adapter run/error map/fact/outbox | Platform Provider Engineering + Workspace override | `/adapter-specs`、`/adapter-contract-runs`、顶级 `/error-maps` writer |
| 0136 | 0072/0073、0090..0096、price/ledger/payment/invoice/outbox | Platform commercial + Workspace/Internal FinOps | tenant `/plans`、`/model-prices`、`/billing-disputes`、`/subscriptions/current` |
| 0137 | dashboard/role view/assistant/approval/user preference/config summary | 独立 Platform/Workspace/Identity console | `/dashboard/overview`、`/dashboard/role-view`、`/admin-assistant/query`、`GET /approvals`、`/user/preferences`、`/internal/config/summary` |
| 0138 | 63 个 0137 source definitions、0011/0019 preference、0057 role config、PRD-097 onboarding | 64+4 contract、task home、advanced tools | `/console/navigation`、`/console/preferences`、旧 page-event ingress、六个 retired UI route |

`/branding/public` 路径本身不退休；它切换为只按受信 canonical Host 解析 Platform/verified Channel brand。带
`tenant_slug` 的旧 query shape 和 `/branding/public/logo?tenant_slug=` 必须普通 404。PRD-103/104 的 `/payments/*`、
`/refunds`、`/invoices*` 继续是受控 Workspace/Provider 业务 API，不因 0136 被误删；现有受控
`POST /platform/plans` 也不是 tenant `/plans` tombstone。

每个 external method/path 必须由编译期 retirement manifest 精确列举，不能用字符串前缀误伤新 route。动态 OpenAPI 与
router registration 必须和 manifest 双向相等：manifest 中 route 仍挂载或生产 router 中出现未分类 legacy route 都阻断。

### 4.2 Expand 与 inventory

0139 up 只创建表、约束、索引、固定 permission/manifest seed 和 write-fence 支撑，不扫描 10 万 Workspace、不执行网络
请求、不启动 backfill。启动命令先验证 schema head 0139、0131..0138 target tables/heads、源码 manifest digest、当前
63 个 0137 navigation source、68 个 0138 audit contract item、41 scheduler installer contexts 和三个额外 runtime surface
全部符合冻结合同。

inventory 对每个 active WorkspaceTenantBinding 和平台全局 source 保存 source upper watermark、row count、owner/FK coverage、
keyed digest、event/outbox/audit/scheduler watermark。缺失/重复 owner、orphan、unknown source schema、active old write lease、
不可解密 legacy value、低熵普通 hash、额外 route/scheduler 或 mapping conflict 进入 `blocked`；不猜 owner、不丢弃行、
不以空值代替无法证明的事实。

### 4.3 Backfill 与 shadow

platform_ops 以 `workspace` 或 `all_active` scope 创建有界 job。调度器按固定 shard + stable keyset 分页，单 transaction/batch
有行数、字节、CPU、deadline 和 retry 上限；噪声 Workspace 不得占满全部 worker。数据库/KMS/Redis/网络依赖不可用时保存
固定失败码和 checkpoint，指数退避，不把 partial target 提升为 current head。

backfill 只写 0131..0138 已定义的 target/mapping/reconciliation，不重写 legacy event type/ID/payload/hash，不把历史事件
翻译或双发为新事件。旧正文需要迁移时沿原业务 PRD 的 KMS/field whitelist 合同处理；ConsoleBackfillCursor 和证据永远
不承载正文。一个 generation 首次完成并通过基础对账时才发 `console_domain.backfilled`；重试/no-op 不重复发。

shadow 使用同一 owner、同一 source upper watermark、同一业务窗口和固定算法比较 source/target。允许明确记录的目标域
裁剪或不可逆聚合，但每种合法变换必须有 versioned transform code 和可证明 digest；“页面看起来一样”、抽样无异常或只有
row count 相等都不是通过。无历史调用标记 `not_observed`，不能伪造成功。

### 4.4 Quiesce、cutover 与 contract

cutover prepare 冻结 exact Workspace/domain generation，获取 registry/domain lease，fence legacy writer/scheduler/page-event
ingress，并等待在途事务完成或 lease 过期。等待超时进入 blocked 或 rollback，不强行删除 lease。随后执行最后 delta、重算
全部 reconciliation、验证当前 owner/permission/approval/security review 和 target smoke。

platform_ops 的 execute 在一个短事务 CAS 推进 target reader/writer head、console contract release、route deny epoch 和
migration state。事实回填不塞进该事务。CAS 成功后新 writer 成为唯一 writer，旧表 DB write guard 保持；event/outbox/audit
与 head 同事务提交。迟到旧 worker 即使仍持有内存数据，也因 registry/domain fence 和 DB guard 失败关闭。

contract 在同一 release 移除外部 router registration、legacy permission grant、frontend import/client/store/DOM/prefetch 和
新 telemetry/audit writer。静态检查覆盖生产 Python、生成 OpenAPI、console shipped source/chunks、route registry、service
worker manifest 和部署 route deny manifest；历史 SQL/DB rows/tests/docs 进入显式 history allowlist，不能为了“零字符串”删除
审计事实。所有 mandatory zero check 通过后 tombstone 才推进 `verified` 并发 `legacy_route.retired`。

### 4.5 N/N-1 与 rollback

部署先发布能识别新 contract epoch 和 DB write guard 的兼容二进制，再切数据 head，最后收缩 route。外部 gateway/ingress
执行单调 route deny manifest，所以仍在排空的 N-1 app 不能对退休路径提供客户可达读面；N-1 直接旧写由 DB guard 拒绝。
内部未经过 ingress 的应用端口不对客户网络开放。新 shell 响应携带 contract/navigation epoch，旧 chunk 或迟到响应 epoch
不匹配时同步 abort、清空 DOM/store/cursor/form 并要求安全 reload。

rollback 是新的高 generation 补偿 job，只能切回上一代 target-compatible reader/worker/LKG，恢复 target head 或重新执行
幂等 backfill。它不能删除 target facts、减少 revision、解除 legacy write guard、把 tombstone 反向推进、恢复旧 permission、
旧外部 route、共享页面、DOM、prefetch、旧 preference writer 或旧 scheduler。若没有可证明的 target-compatible generation，
返回明确 unavailable 并保持安全关闭。

0139 down 只在所有 migration/batch/cursor/tombstone/contract release/reconciliation/command/approval/review/event/outbox/audit、
scheduler registry 非 seed 行、lease、watermark 和 runtime observation 均 pristine，且从未 backfill 时允许；任一事实存在即在
首个 DDL 前拒绝。空库验证 `0138 -> 0139 -> 0138 -> 0139`；生产 rollback 永不执行 destructive down，也不修改
0131..0138 业务事实。

## 5. 旧入口、导航、遥测与审计归零

### 5.1 四层归零定义

每个 retirement 必须同时满足：

1. 合同层：旧 permission 不再由固定/自定义角色授予，Navigation 64+4 disposition、API/OpenAPI 和 ingress deny manifest
   精确匹配；六个 retired ID 不出现在公开 Workspace DTO。
2. 服务层：FastAPI router/import/client 不可达，直接 method/path、旧 Cookie、Bearer、query credential、大小写/编码变体和
   N-1 请求只得到普通 unknown route；不存在 tombstone controller 或 fallback proxy。
3. 前端层：生产 bundle 没有旧 route case、lazy import、prefetch、restore target、DOM root、selector、action handler、缓存或
   localStorage 恢复；手写 hash 不在 allowed set 时不发来源请求。
4. 事实层：切换后没有新的 legacy page telemetry/audit/outbox；历史 `console.page_viewed`、
   `dashboard.role_viewed`、`onboarding.step_completed`、`help.competitor_viewed` 及业务 event 原样保留，不翻译、不删除、
   不重新投递 sent outbox。

静态零命中不是对整个仓库运行无差别 grep。扫描器使用版本化 production surface manifest，明确区分 shipped runtime 与
history allowlist；任何 runtime allowlist 例外必须是 exact file + symbol + digest，不接受宽目录、通配字符串或“临时兼容”。

### 5.2 64+4 导航证明

0139 读取 0138 immutable NavigationContractSnapshot，不重新按前端角色名生成菜单。自动证明 Excel 64 项零重复、零缺失、
源码 extras 精确为四项；五项恢复、六项退休、detail/advanced/account/dev-center 分类、source permission expression、
entitlement generation 和 current Membership 求交全部匹配。

成员可见 item set 只保存 keyed digest，不能以可反推角色/权限的普通 hash 落库。撤权不等待 migration event；下一 API 请求
立即读取 current Operator/Membership/permission revision。旧导航响应、prefetch 和 pending page request 因 epoch/fingerprint
变化被丢弃，不得写回新 route。

### 5.3 Tombstone 与历史事实

LegacyRouteTombstone 证明“执行面已退休”，不表示历史 tenant_id 事实可以删除。legacy source/mapping/event/outbox/audit、
原始业务 ID 和 scheduler watermark 按来源 retention 保留；若业务 PRD 已要求 KMS scrub，自由正文只保留其受控 tombstone
和原 keyed digest。0139 不二次 scrub、不恢复明文，也不把旧 source ID 暴露到公共 DTO。

## 6. SchedulerRegistry 与多副本执行

### 6.1 41 个基线 slot 与 36 个 S7 installer context

Excel 的“23 个后台循环”是过期估算。当前 `app.py` 权威源码在总开关下有 39 个 installer，另有 1 个始终安装和 1 个
gateway-placement 条件安装 context。0139 baseline slot catalog 必须逐项包含：

1. `install_status_publication_scheduler`（always）
2. `install_gateway_snapshot_scheduler`（`gateway_placement_enabled`）
3. `install_admin_assistant_scheduler`
4. `install_provider_health_scheduler`
5. `install_usage_export_scheduler`
6. `install_cost_anomaly_scheduler`
7. `install_customer_health_scheduler`
8. `install_customer_support_scheduler`
9. `install_developer_contract_scheduler`
10. `install_privacy_scheduler`
11. `install_compliance_scheduler`
12. `install_supply_chain_scheduler`
13. `install_security_assurance_scheduler`
14. `install_launch_governance_scheduler`
15. `install_request_explanation_scheduler`
16. `install_request_log_query_event_scheduler`
17. `install_provider_error_map_scheduler`
18. `install_audit_export_scheduler`
19. `install_webhook_replay_scheduler`
20. `install_file_security_event_scheduler`
21. `install_adapter_contract_event_scheduler`
22. `install_experiment_scheduler`
23. `install_response_cache_outbox_scheduler`
24. `install_product_event_scheduler`
25. `install_approval_scheduler`
26. `install_plan_billing_scheduler`
27. `install_saas_billing_scheduler`
28. `install_platform_billing_scheduler`
29. `install_payment_commerce_scheduler`
30. `install_invoice_reconciliation_scheduler`
31. `install_notification_scheduler`
32. `install_observability_scheduler`
33. `install_risk_scheduler`
34. `install_partner_portal_scheduler`
35. `install_branding_scheduler`
36. `install_billing_dispute_scheduler`
37. `install_security_event_scheduler`
38. `install_mcp_scheduler`
39. `install_tenant_data_scheduler`
40. `install_data_governance_scheduler`
41. `install_edge_support_bundle_scheduler`

1..2 使用各自 config condition；3..41 共 39 项受 `control_background_schedulers_enabled` 约束。它们是 S7 开始前的
兼容基线，不是要求合并后继续启动 41 个旧 worker。PRD-140/141/142 一对一替换 customer-health/product-event/branding；
PRD-143 把 adapter-contract + provider-error-map 两个 slot 合为一个；PRD-144 把 plan/saas/platform billing、payment、invoice、
billing-dispute 六个 slot 合为一个；PRD-139 的 channel queue 原位接管 partner-portal slot；PRD-145 新增一个 console
experience context，PRD-146 只扩展其内部 queue。

0139 最终 active installer manifest 精确为以下 36 项：

1. `install_status_publication_scheduler`
2. `install_gateway_snapshot_scheduler`
3. `install_admin_assistant_scheduler`
4. `install_provider_health_scheduler`
5. `install_usage_export_scheduler`
6. `install_cost_anomaly_scheduler`
7. `install_platform_customer_success_scheduler`
8. `install_customer_support_scheduler`
9. `install_developer_contract_scheduler`
10. `install_privacy_scheduler`
11. `install_compliance_scheduler`
12. `install_supply_chain_scheduler`
13. `install_security_assurance_scheduler`
14. `install_launch_governance_scheduler`
15. `install_request_explanation_scheduler`
16. `install_request_log_query_event_scheduler`
17. `install_platform_provider_engineering_scheduler`
18. `install_audit_export_scheduler`
19. `install_webhook_replay_scheduler`
20. `install_file_security_event_scheduler`
21. `install_experiment_scheduler`
22. `install_response_cache_outbox_scheduler`
23. `install_platform_growth_scheduler`
24. `install_approval_scheduler`
25. `install_commercial_accounting_scheduler`
26. `install_notification_scheduler`
27. `install_observability_scheduler`
28. `install_risk_scheduler`
29. `install_channel_attribution_scheduler`
30. `install_brand_domain_scheduler`
31. `install_security_event_scheduler`
32. `install_mcp_scheduler`
33. `install_tenant_data_scheduler`
34. `install_data_governance_scheduler`
35. `install_edge_support_bundle_scheduler`
36. `install_console_experience_scheduler`

固定源码 manifest 是唯一装配来源，`app.py` 不再维护第二份手写 installer tuple。0139 seed 保存 41 个 baseline slot 到
36 个 final context 的 exact mapping；一对一替换、二合一、六合一、新增和无遗漏必须可自动证明。配置关闭只影响 active
count，不改变 catalog。若某条前序 PRD 实际代码未按冻结替换完成，manifest drift 阻断 0139，而不是退回启动 legacy worker。

每个 context 内的每个独立 job/queue 都必须成为 registry queue contract；installer 数量不是 worker/lease 数量。queue contract
以固定 owner 声明 `platform_global|workspace_composite|channel|deployment|global_account|fixed_split`。
`fixed_split` 必须把各物理 queue code 分开并由对应复合 FK/平台表约束，不能用 nullable generic owner 授权。现有一个 context
处理多个 queue 时只共享 lifecycle，不共享 cursor、lease、fence 或 owner。

### 6.2 第 37 个集群 context 与三个本地运行面

`install_platform_job_supervisor` 内部创建的 `platform-job-runtime` 是第 37 个持久、无界、集群 context。它必须有独立
registry entry、leader lease、fence 和 watermark；现有 per-job lease 继续保留。未当选 Web replica 不创建该 task，失租后
不能领取新 job，迟到 job commit 同时验证 registry fence 和 job fence。

现有 installer 不再各自改写 FastAPI lifespan 并在每个副本创建永久 loop，而是向 supervisor 返回固定 one-shot adapter、
queue contract 和关闭 callback。每个副本只有一个 `scheduler-lease-coordinator` 按固定上限轮询 registry/lease；它只能做
manifest 对账、lease CAS、续租、停止和创建/回收已持租 child task，不能直接读取业务 queue 或提交业务 fact。

以下三个不是集群 singleton：

| Runtime surface | 当前边界 | 0139 合同 |
| --- | --- | --- |
| `scheduler-lease-coordinator` | 0139 新 supervisor 控制面，每副本最多一个 task | `per_replica_control_bounded`；固定 poll/backoff、同时持租/child-task 上限，只拥有 lease CAS，无业务提交权 |
| `risk-fact-sink` | 每副本 asyncio queue，默认容量 4096，最多 100 条一批 | `per_replica_bounded`；只排空本副本产生的事实，记录 accepted/dropped/backend_failure/queued，不获取 cluster lease |
| `control-telemetry` | 每副本 queue，默认容量 4096、batch 200、flush 0.25s | `per_replica_bounded`；逐请求仅进入所在副本缓冲，记录 accepted/dropped/persisted/failure，不获取 cluster lease |

三者进入固定 `runtime_surface_manifest` 和 status inventory，但不进入 37 个 cluster context count。把两个数据 buffer 做成
leader-only 会让
非 leader Web 副本的本地请求事实无人排空；完全忽略又会漏掉后台任务、关闭排空和容量风险。其写入使用稳定 fact identity/
watermark 去重，队列满按既有固定策略计数，不能阻塞请求或把敏感正文写日志。每次 sink/flush 都有固定 deadline；shutdown
时 coordinator 先停止新 lease claim，fence/排空有界 child task，再处理两个数据 buffer；最多在固定 drain deadline 内处理，
超时把剩余数量计入 `shutdown_timeout` 并取消本地 task，不能让 Web 进程无限挂起。

### 6.3 lease、fencing 与接管

集群执行面每轮先以数据库原子 CAS 获取/续租 `(scheduler_code, partition_code)`。新 owner 接管时 generation 单调加一；
旧 token 仅存 digest。每个 source read、target fact、event/outbox、audit 和 watermark 最终 commit 都带 scheduler code、partition、
generation 和 token digest predicate，并同时验证未过数据库 deadline。仅在循环入口检查 lease 不够，失租后的迟到 commit
必须由 DB 条件更新/trigger 拒绝。

worker shutdown 先停止领取，完成当前有界 batch，提交 checkpoint，再逆序释放 lease；超时则 fence generation 并取消。
崩溃不清空持久 queue/cursor。接管从 last committed watermark 继续，unique source revision + idempotency 保证不重复业务事实
和 event/outbox。config/permission/owner/contract revision 变化使下一轮重新取 manifest；不能靠进程内 bool 继续运行。

多副本 G7 至少启动两个 app replica，逐 `(queue_code, partition_code)` 证明同时最多一个有效 leader、不同 partition 可公平
并行、disabled 不启动、过期 leader 迟到写被拒、deadline 后接管、event/outbox 不重复、水位不倒退；同时证明 41 个 baseline
slot 映射到 36 个 final installer 零重复、零缺失，platform-job 是第 37 个 context。每个副本只能有一个 coordinator，
未持租 queue 不得存在业务 child task；两个 per-replica buffer 则证明每副本各自可排空、容量有界、关闭无永久 task，
并且不被 singleton 断流。

## 7. 四个 Excel API exact 合同

所有 DTO 使用 strict type、`extra=forbid`、固定枚举、有界数组和 canonical JSON；拒绝重复 JSON key、未知/重复 query、
非规范 UTF-8、NaN/Infinity、任意 SQL/path/module/URL/selector、超限 body 和混合凭据。写操作要求 exact Origin、Platform
CSRF、一次性 step-up、`Idempotency-Key` 和 quoted `If-Match`。响应统一含 request_id/trace_id、contract/registry generation、
当前 permission fingerprint、`Cache-Control: no-store, private` 及适用 ETag。

### 7.1 `GET /platform/console-migration/status`

要求 `platform.console_migration.read`。只允许 query：固定 `domain_code`、固定 `state`、不可逆 `workspace_ref`、HMAC cursor
和 `limit=1..100`；不带 query 返回八域安全汇总、41 个 baseline slot 映射结果、36 个 final installer context、
platform-job context、逐 queue contract/partition 状态、三个本地 runtime class 汇总、contract release 和 blocking check counts。

item 只返回 safe migration ID、workspace pseudonym、domain/state/progress、source/target generation、reconciliation status/reason
code、approval/security-review capability、route/scheduler readiness、last safe UTC 和允许 action code。不返回 tenant/workspace
原 ID、客户名称、业务 row、raw hash、lease token/owner、内部路径、环境变量、堆栈或 arbitrary message。

cursor 绑定 actor permission fingerprint、filters、contract generation 和 expiry；撤权、筛选变化或 generation 变化固定失效。
不存在和无权对象同形 404。依赖不可用时返回固定 partial/unavailable source，不用上一用户缓存冒充 fresh。

### 7.2 `POST /platform/console-migration/backfills`

要求 `platform.console_migration.backfill`。body 是 exact discriminated command：

- `prepare`：`domain_code + scope(workspace|all_active) + optional workspace_ref + expected_revision`，冻结 inventory 和 upper watermark；
- `start|resume`：引用 prepared migration ID、expected revision/reconciliation generation 和固定 resource profile；
- `cancel`：只停止继续领取并保存 checkpoint，不删除已写 target，不回退 event/outbox。

返回 202、job/migration safe ID、state、quoted ETag 和 polling location code，不返回 worker/host/path。`all_active` 要求更短的
step-up age 和固定并发/资源 ceiling。重复同键同体返回原 job；相同 generation 已完成返回 no-op，不重复
`console_domain.backfilled`。

### 7.3 `POST /platform/console-migration/cutovers`

同一路径承载职责分离 action，不新增隐式审批 API：

- `prepare`：platform_ops + backfill permission，冻结 exact migration/reconciliation/route/scheduler/permission generation；
- `approve`：platform_owner + approve permission，绑定 prepare actor、digest、expiry 和一次性 step-up；
- `security_review`：platform_security + security_review permission，只能引用当前 generation 已由服务端写入的
  `cross_domain_negative` reconciliation ID/revision；服务端重新读取固定 profile/version、mandatory case count、tool version、
  artifact digest、freshness 和全部 `matched` 结果。客户端不能提交或覆盖 `passed`、case count、digest 或测试结果；
- `execute`：platform_ops + execute permission，只能执行当前有效 owner approval 和 security review 均存在、三方账号不同、
  全部 mandatory reconciliation matched 且无 active legacy lease 的 generation。

execute 返回 202；worker 完成 CAS 后状态转 active 并发事件。网络未知结果禁止客户端自动换 idempotency key；同 key 查询原
result。任一 revision/permission/route/scheduler manifest 漂移使 approval/review 失效并回到 ready/blocked，不带旧授权继续。

### 7.4 `POST /platform/console-migration/rollbacks`

body action 固定为 `prepare|approve|security_review|execute|resume`，角色和三方分离与 cutover 相同，并额外要求固定
`reason_code`、source active generation、target-compatible generation 和 expected revision。不能请求 legacy generation、
任意 migration version、删除事实、解除 write guard 或重新挂载 route。

execute 返回 202 补偿 job；完成后发 `console_domain.rolled_back`，migration generation 终态为 rolled_back，route tombstone
仍单调 verified。没有安全 target generation 时固定 `console_migration.rollback_unavailable`，不静默切旧 reader。

### 7.5 统一错误语义

固定错误至少包括 `console_migration.invalid`、`console_migration.not_found`、`console_migration.conflict`、
`console_migration.approval_required`、`console_migration.security_review_required`、
`console_migration.reconciliation_failed`、`console_migration.lease_active`、
`console_migration.registry_drift`、`console_migration.contract_not_ready`、
`console_migration.dependency_unavailable`、`console_migration.permission_revoked` 和
`console_migration.rollback_unavailable`。错误 body 只有 code、固定 message code、request_id/trace_id 和安全 retry hint；
404 不暴露 owner，409 不回显 canonical body/hash，503 不返回 DSN/host/path/secret 状态。

## 8. 事件、outbox 与审计

Workspace event/outbox 每行重复复合 owner；平台事件使用独立平台表。payload exact schema 共用
`event_id/type/schema_version/aggregate_ref/domain_code/operation_generation/contract_generation/reconciliation_digest/occurred_at`，
Workspace payload 只使用不可逆 owner ref。禁止 arbitrary metadata 和客户正文。

| Event | 唯一触发点 | 额外安全字段 |
| --- | --- | --- |
| `console_domain.backfilled` | 一个 owner/domain/generation 首次完成 backfill 且基础 reconciliation matched | source/target watermark digest、row count、mapping generation |
| `console_domain.cutover` | target reader/writer head 与 contract release CAS 同事务成功 | old/new target generation、route/scheduler manifest revision |
| `console_domain.rolled_back` | target-compatible 补偿 head CAS 成功 | from/to target generation、固定 reason code |
| `legacy_route.retired` | method/route/owner/contract generation 首次四层 verified | route code、source PRD、replacement code 或 none、zero-check digest |

backfill/shadow 重试、no-op、历史 replay、普通 HTTP 401/403/404 和 static scan 不直接发业务 event。event、outbox、audit、
head、watermark 同事务；dispatch 使用稳定 event ID、attempt/backoff/deadline 和 fence。sent outbox 永不因 migration 重发；
恢复只处理未 sent 的原 event，不翻译 old type。

不可变审计记录 actor pseudonym、role/permission revision、session/step-up epoch、action、owner safe ref、old/new state、quoted
revision、idempotency HMAC、approval/review references、result/error code、request_id/trace_id 和 UTC。日志只写同类固定元数据与
duration bucket；不记录 request body、response body、query、Cookie/Header、credential、raw hash、客户正文或 evidence path。

## 9. Platform Console 工作台

新增 `/platform/console-migration` 只由 Platform shell 和独立 strict client/store 加载。首屏是紧凑状态表与阻断摘要，按八域、
状态和 safe Workspace ref 筛选；详情显示阶段 timeline、固定 reconciliation checklist、operation history、route contract 和
scheduler/runtime status。客户业务正文、联系人、环境配置和内部 worker identity 不进入页面。

按钮只根据后端 action capability 显示：ops prepare/backfill/execute，owner approve，security review。高风险 action 使用确认
dialog 展示 domain、safe owner、generation、check counts、approval expiry 和失败后的恢复入口；不使用自由 reason 文本。
完成态仍可查看不可变历史，blocked/unavailable 明确下一安全动作，不出现“手工改库/重启服务/删除游标”等研发指令。

状态固定 loading、empty、partial、ready、backfilling、quiescing、cutting over、validating、active、blocked、rolling back、
rolled back、permission revoked 和 dependency unavailable。页面切换先增加 navigation epoch，abort 旧请求，清空旧 detail/
form/cursor，再加载当前 route；迟到 200/401/403 不能写回。logout、PlatformSession revoke、pagehide 和 route change 同步淘汰
受保护 store，响应不进 service worker/localStorage。

390/1280/1440、DPR 1/1.25/1.5 下无重叠、裁剪和横向页面溢出；宽表在自身区域滚动并保留可读行标题。键盘顺序、焦点恢复、
读屏 live status 和 dialog focus trap 可用；状态不只用颜色表达。页面只加载当前 detail，不预取客户正文或其他 Workspace。

## 10. SLO、容量与恢复

- 单 Workspace 切换 `< 5 分钟`：从 execute 被服务端接受并成功进入 quiescing，到 active 或安全 rolled_back；包含 in-flight
  drain、final delta、全部 mandatory reconciliation、head/route epoch CAS 和 target smoke，不包含等待人工审批时间。
- 10 万 Workspace 回填 `< 4 小时`：从 all_active inventory 冻结 upper watermark，到全部非隔离 Workspace 达到 ready；包含
  owner/FK/hash/event/outbox/audit/scheduler 对账、合理重试和最终 reconciliation。隔离项必须在结果中明确计数，不能从分母删除。
- 页面切换 p75 `< 1.5 秒`：从用户激活可信 route 到当前页首个可操作且非旧缓存状态；包含 navigation/permission 校验和
  必需 API，不以 skeleton、空壳或 stale old DOM 假装完成。

性能基线固定记录硬件/OS、SQLite/PostgreSQL、数据分布、Workspace/row/event 数、shard/batch/concurrency、依赖延迟和版本。
至少测 baseline、并发、压力、长稳、资源 ceiling、热/冷 owner、失败恢复和噪声邻居。backfill 使用公平分区和有界 batch；
页面 status 聚合读只读安全 head/summary，不扫描业务表。任何超标都保留 profile 和结果 digest，不通过调小验收分母规避。

## 11. 聚焦测试、G7 与证据

PRD-147 编码期间只运行受影响的 0139 migration、状态机/API、registry/lease/fence、route/static contract 和前端 smoke；不在每个
小改动重复全套产品 QA。实现完成、独立测试记录和敏感审计提交后统一执行 S7/G7：

- Migration：fresh 0139、0138 current-head、0130..0139 逐级、空库往返、事实 down guard、重复 apply、SQLite/PostgreSQL、
  Windows/Linux、N/N-1 compatibility 和 production rollback；逐表 row/digest/FK/event/outbox/audit/watermark 保真。
- API：四路径全部 action 的成功/匿名/错域/mixed/duplicate Cookie、CSRF/Origin/step-up、unknown/duplicate field/query、
  idempotency 同键同体/异体、If-Match、分页 cursor、撤权和统一 no-store/error/request_id。
- 状态机：八域全部合法/非法边、并发 prepare/execute、审批/复核过期、网络未知结果、cancel/resume、blocked repair、重启接管、
  late commit、event exactly-once 和静止终态。
- owner/security：复合 FK、缺失/重复 link、跨 Workspace 同形 404、Platform/Workspace/Partner/Support/Device 五域负矩阵、
  三方 SoD、PlatformSession 不读客户正文、SupportGrant 不被迁移 API 消费、SSRF/注入/重放和敏感日志。
- Scheduler：41 baseline slot 到 36 final installer 的静态映射、第 37 platform job、全部内部 queue contract、多 replica
  partition leader/fence/takeover、disabled/config drift、nested business lease、水位不倒退；单 coordinator/持租才创建 child
  task，两个 per-replica data buffer 独立排空、queue full、sink/flush timeout、backend failure 和 bounded shutdown。
- Route/navigation：64+4 自动证明、五项恢复、六项退休、production router/OpenAPI/client/import/DOM/prefetch/permission/
  telemetry/audit 零命中、ordinary 404、N-1 ingress deny、历史 event/outbox 仍可对账。
- 浏览器/流程：Chromium/Edge/Firefox 真实服务，390/1280/1440 与 DPR 1/1.25/1.5，鼠标主要流程、键盘/焦点/读屏、
  Abort/迟到响应/logout/revoke、Network/Console/Page error 为零、完成态和失败恢复入口真实可用。
- 故障与性能：DB/Redis/KMS/network/Edge 失败、lease expiry、worker crash、outbox recovery、三个 Excel SLO、长稳和静止终态。

正式机器证据只写 `.artifacts`，不使用 `.tmp` 作为验收来源。测试凭据只在本地进程内读取，不能打印、写文档、落库或进入
artifact。`docs/testing/PRD-147-tests.md` 独立记录命令、版本、case/result、artifact digest、敏感审计和结论；G7 另有批次
总证据，不能用既有 97 条 G6 回归单独宣称 S7/G7 通过。

## 12. 预计改动边界

实现仅在 G6 通过后开始，预计包括：

- `infra/migrations/0139_console_domain_migration_governance.sql/.down.sql`；
- `control-plane-api/src/metahub_control/console_migration/` 的 models/service/routes/scheduler、固定 route/domain/reconciliation
  manifest、strict DTO、事件/outbox/audit；
- `control-plane-api/src/metahub_control/platform_jobs/supervisor.py`、`app.py`、41 个 baseline slot 到 36 个 final installer 的
  固定 mapping/registry adapter，
  platform-job outer lease/fence，risk/telemetry runtime surface observation；
- `platform_access` 固定 permission extension tuple、ingress/contract epoch/write guard 和 OpenAPI/static verifier；
- `console-web` 独立 Platform migration client/store/page/route、现有旧 import/client/DOM/prefetch/restore target 物理卸载；
- 0139 migration/API/unit/integration/flow/browser/performance/security/compat tests、PRD-147 独立测试记录与 G7 批次 harness。

不修改 PRD-101..105 用户未跟踪文档，不提交本地凭据、数据库、截图、trace、`.artifacts` 或临时文件，不提前实现
PRD-148。每条前序 PRD 仍保留独立 migration、测试记录、敏感审计和 commit；PRD-147 不把前序交付事实压成一个提交。

## 13. 两轮编码前自审

### 第一轮：owner、事实保真、tombstone 与 API

1. P0：把 Excel 五个 Workspace 对象做成 nullable platform/workspace 泛表会让 owner 校验依赖应用分支。修正为客户行全部
   复合 Workspace FK，平台/global auxiliary 使用物理分表；不以 `owner_type/owner_id` 授权。
2. P0：0139 再创建 NavigationContractSnapshot 会和 0138 形成两套 current head。修正为 0139 只引用 0138 immutable
   snapshot，并用独立 ConsoleContractRelease 绑定 route/API/permission/scheduler manifest。
3. P0：外部 tombstone response 会让攻击者枚举历史能力，也不满足旧 API 归零。修正为 tombstone 只在内部表；外部 router
   不挂载，返回与未知路径同形 ordinary 404。
4. P0：无差别删除含旧 route/event 字符串的行会破坏历史审计。修正为 production surface manifest 与 history allowlist
   分离；运行面零命中，legacy event/outbox/mapping 原样保真。
5. P0：row count + 普通 hash 既可能漏 owner 漂移，也可能反查低熵客户值。修正为复合 FK/coverage、versioned transform、
   分域 KMS HMAC、event/outbox/audit/scheduler watermark 共同对账。
6. P0：把 10 万 Workspace backfill 放入 0139 DDL 会锁库且无法恢复。修正为 migration 只 expand；平台 ops 通过有界、
   checkpointed、fenced job 执行。
7. P1：四个 Excel API 若另开隐式审批 endpoint 会扩大合同面。修正为 cutover/rollback POST 使用 strict discriminated
   prepare/approve/security_review/execute actions，并强制三方不同账号。
8. P1：状态页若返回原 Workspace ID、lease owner 或 raw digest 会形成批量枚举面。修正为 safe pseudonym、固定 reason/count
   和裁剪 DTO，详细正文仍走原业务授权/SupportGrant。

第一轮修正后无未关闭 P0/P1。

### 第二轮：Scheduler、多副本、N/N-1、rollback 与 SLO

1. P0：照抄 Excel“23 个循环”会遗漏当前 39 个受开关 installer 及两个外围 context；反过来把 41 个旧 slot 都继续启动又
   会违反 PRD-143/144 的合并合同并重复消费。修正为 41 项基线逐名盘点、exact mapping 到 36 个 final installer，queue
   contract 逐项注册，配置只收缩 active count。
2. P0：只登记 installer contexts 会遗漏 supervisor 内 `platform-job-runtime`。修正为第 37 个 cluster context，保留
   per-job lease 并要求双重 fence。
3. P0：把 risk/telemetry worker 做成 leader-only 会丢掉非 leader Web 副本的本地请求事实。修正为两个
   `per_replica_bounded` runtime surface，进入 inventory/容量/关闭审计但不获取 cluster lease。
4. P0：即使有 DB lease，若 36 个 installer 仍各自在每个 Web 副本启动 loop，仍会重复承担无界 worker。修正为每副本一个
   `per_replica_control_bounded` coordinator；未持有 queue partition lease 时不创建业务 child task。
5. P0：只在 loop 开始检查 lease，旧 leader 失租后仍可能提交迟到 event。修正为 fact/event/outbox/audit/watermark 最终
   DB commit 同时验证 registry generation/token/deadline 和既有业务 fence。
6. P0：N-1 app 在排空期间仍挂旧 read route 会复活越权面。修正为单调 ingress route deny manifest、contract epoch、
   客户网络隔离和 DB write guard；N-1 不能对退休 route 提供外部读写。
7. P0：rollback 恢复 legacy reader/writer/DOM 会撤销安全切换。修正为 rollback 只能到 target-compatible generation，
   tombstone/write guard/permission/route retirement 永不反向。
8. P1：一个 context 处理 platform/workspace/channel/deployment queue 时共享泛 owner 会跨域。修正为 fixed queue contract、
   物理 FK 和独立 partition cursor/lease/fence；只共享 lifecycle。
9. P1：以 skeleton 或排除隔离 Workspace 计算 SLO 会虚假通过。修正为固定计时边界、完整分母、环境/数据 profile，页面以
   首个可操作非旧状态计时，backfill 隔离项仍在结果中明确计数。
10. P1：用旧 G6 97 条回归代表 G7 无法覆盖 0139、平台/伙伴/Support 负矩阵、37 个 cluster context、全部 queue contract
   和三个本地运行面。修正为 PRD-147 后统一新增 S7 数据库、FLOW、三浏览器、性能、安全、N/N-1、route zero 和多副本
   证据；97 条只作必要回归。

第二轮修正后无未关闭 P0/P1，设计可在 G6 通过后进入连续编码。

## 2026-09-07 文件域 Workspace 接线补充

PRD-137 真实注册上传复现：导航已登记 workspace.applications.files，但文件 read/write 依赖
只接受旧 Bearer，Workspace Cookie 上传返回 401；文件两笔写事务只查旧 sessions 表。此为实际
迁移接线缺口，不能为探针签发旧 Token 或改用内部 create_file 绕过 HTTP 鉴权。

第一轮自审：仅把 file.read/file.write 入口接到现有 Workspace/Legacy 分域守卫，保持显式单凭据、
Origin/CSRF、权限先于 multipart 读取、旧 Bearer 兼容及混合凭据拒绝。文件名与正文仍走现有加密、
tenant 查询和解析流程，不新增数据库迁移、权限默认授予或测试专用后门。

第二轮自审：不能只改路由。文件 processing 与 terminal 两笔写事务都复用真实 Workspace 事务
鉴权，并核对 tenant/user/session 与路由恢复的上下文一致，再检查 file.write；撤权后不得使用
旧 AuthContext 提交。Legacy 保持原事务复核，不修改全局 auth helper 的其他业务行为。单独验证
双租户上传/读取、CSRF、混合凭据、Viewer、路由后撤权及 Legacy 相邻回归。此修复不代表文件安全
扫描等其他旧入口已全部迁移，也不把专项测试扩张为 G7 完整复验。

## 2026-09-07 主 Console 认证域接线批次

与 PRD-094/146 批量复核：主 Console 仍使用旧 Bearer 启动和 auth_context，而身份与导航已改为
Global/Workspace Cookie。先保留真实无旧 Token 登录后的入口失败证据，再完成消费端迁移；
不增加 Legacy 密码登录夹具伪装 SaaS 已接通。

第一轮自审：逐当前可达页面确认客户端凭据、服务端入口和写事务三者使用同一 domain/owner。
已有 Workspace DTO/CSRF/权限 helper 优先复用；只给明确 Workspace 资源路由接线，不能一次
放宽全局旧鉴权函数使平台、支持、机器或退休路由跨域。旧 Bearer 兼容仅按原合同保留，混合拒绝。

第二轮自审：前端页面可见不等于业务 API 已迁移。实际双空间切换后旧读取、写入、失败回调、
导航预取与浏览器存储均核对；服务端写事务在提交前再次恢复实际会话，不复用旧快照授权。
PRD-146 拥有 Console 入口与导航状态，本条保留每个迁移资源的独立测试与审计记录；完整
路由处置、历史保真及批次流程门禁仍需复验，不因首页能打开而宣称全域完成。

### 基础资源客户端认证实施批次

2026-09-07 核对项目/应用、Environment、Provider/Key、Model、Virtual Key 和 File 入口已使用
既有 Workspace/Legacy 分域守卫，但前端六组客户端仍强制 Bearer，File 还固定 omit Cookie。
本组连续迁移请求构造，共用显式认证传输，不修改后端全局守卫、DTO、权限默认值或写事务。

第一轮自审：调用方必须显式选择 `authentication: "workspace"`，只能发送浏览器 Cookie；
旧兼容调用只接收真实非空 token，禁止缺少 token 时自动回退 Cookie。两者混用、未知认证域或
调用方插入 Cookie/Authorization/CSRF Header 均在出网前拒绝。Workspace 的 POST/PATCH/DELETE
读取既有专用 CSRF Cookie；Legacy 显式 omit Cookie，防止同源环境把两个凭据域意外混发。

第二轮自审：传输固定 no-store 和拒绝重定向，不记录 URL、凭据、正文或异常原文；AbortSignal
贯穿所有读取和写入，不提供自动重试。Multipart 仍只含一个 file，不手工设置 Content-Type。
每个客户端必须验证实际请求头、Cookie 策略、正文、取消和 Legacy 相邻合同；共用传输不是 owner
证明，主页面仍必须使用真实 Cookie 会话和当前 owner 淘汰迟到响应。本增量为主入口接线准备
完整基础资源请求接口，不单独宣称所有页面已迁移或 G6/G7 通过。

实施复核补充：取消不仅可能发生在 fetch 等待时，也可能发生在 response.json 读取正文时。
六组客户端原有 JSON 容错必须原样抛出 AbortError，不能把中途取消变为成功空对象或文件 DTO
错误。保留非取消 JSON 解析失败的既有处理，定向覆盖全部 27 项资源操作的响应体取消。

### 基础资源写事务与业务页 owner 连续实施批次

2026-09-07 当前代码复核：五组基础资源服务的 16 项创建/更新/禁用操作只使用路由恢复的
AuthContext，缺少实际写事务内的 Workspace 会话复核。Project/Application、Environment、
Provider/Key、Model、Virtual Key 共享该缺口；File 的两笔写事务已经单独复核。先补写事务，
随后连续接入同组前端读写，不把客户端支持 Cookie 当作业务页完成。

第一轮自审（凭据、权限和事务）：只为明确迁移的资源写服务增加共用事务 owner。用真实
Workspace token 重新恢复会话，核对 tenant/user/session/token_jti 并再次校验具体 write 权限；
Legacy 使用既有 require_transaction_permission，不改全局路由守卫。事务在业务读取之前开始，
提交之前再复核；业务、事件和已有审计保持同一提交。所有失败退出明确回滚并关闭连接，提交
后的既有 webhook 仍不回滚已提交事实。随机 Virtual Key 冲突重试必须重新开启事务、重新鉴权
并重读环境/项目绑定，不能把第一次成功鉴权复用于下一次尝试。

第二轮自审（异步、证据和边界）：网络探测不能在持有数据库写锁时等待上游；Provider
connection-test 的外部请求与结果写回单独处理，不能因其他 16 项已完成就宣称它已修复。
前端使用真实 public owner、精确 Session 引用、刷新代次和后端导航，拒绝旧 DOM 发起请求，
并丢弃迟到成功、401/403、finally 和刷新。切换 owner 同步清空表单反馈、选择、cursor 和
一次性 Virtual Key；不伪造 tenant/user/session。File 保留专用分页/上传 ticket，迁移其 owner
输入而非去掉所有权校验。先用实际注册、撤销/降权后的上下文验证 16 项写入、事件与审计均不
提交，再跑受影响 Legacy 相邻测试。PostgreSQL 并发撤权锁定、网络探测和整组真实业务页流程
仍须独立验证，未取得证据前维持未完成；不开始 PRD-148，不重启 72 小时等待。

### 2026-09-08 Provider 连通性探测补充自审

第一轮（授权和网络边界）：复用 ResourceWriteTransaction，在第一笔短事务中用真实会话
复核 provider.write，并一次读取 Provider 配置和健康水位。退出时回滚只读事务并关闭连接，
然后执行原有无 Key 连通性探测。第二笔短事务重新鉴权，在写回及提交前检查权限；中途 logout、
成员降权或过期均不得提交健康结果。同步探测通过既有 FastAPI 线程池执行，避免上游等待阻塞
同进程 logout/配置变更。继续保留 Shared spec、SSRF/DNS、认证失败固定摘要和 Legacy 合同。

第二轮（竞争、精度和失败退出）：仅比较 provider.updated_at 不够，它是秒级且不是独立配置
版本。写回使用原有 health_generation、last_tested_at、health_updated_at、next_probe_at 和
probe_lease_token/until 的空值安全条件更新；配置改回原值仍因 generation 递增而拒绝。
已有未过期健康探测租约在出网前拒绝，在途租约或健康水位变化返回现有 probe_superseded。
此入口不抢占或清除健康任务租约，不产生失败后需要清理的初始写入。连通性结果保存原始 UTC
微秒时间；不晚于已保存 last_tested_at 的结果拒绝，不能用秒级截断掩盖同秒内的新结果。
任何 CAS、鉴权或提交失败都回滚且关闭连接，只有成功提交后才能写 health_checked 日志。
受影响测试覆盖实际注册、撤销/降权、配置/Key 变更、同秒竞争、租约、提交故障和原有网络冒烟。
本增量不替代 PostgreSQL 并发撤权锁定、前端 owner 消费端或全批次 QA，Excel 不修改完成状态。
