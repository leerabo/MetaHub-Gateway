# PRD-089 租户数据导出与注销流程设计

状态：**已完成**（2026-08-18）。本文记录落盘设计、两轮编码前自审、实现、迁移、专项测试、实际运行
与 PAGE-121 Browser 验收结果。PRD-088 已完成且不重复实现；本条不启动 PRD-090。总体状态保持
`all_108_prds_complete=false`。

## 1. 权威需求与范围

本设计交叉核对以下权威来源：

- Excel `01_PRD总表!90`、`03_M2-M3扩展交付!26`：PRD-089，M2/P2，租户数据导出与注销流程。
- Excel `04_页面清单`：PAGE-121 数据管理页。
- Excel `05_API接口清单`：API-402..407 是生成器把两个路径拆成的六个 token；真实合同是
  `POST /tenant/export` 与 `POST /tenant/deletion-request`，其中 API-407 是 `deletion-request`
  的末段，不得遗漏，也不得机械实现成六个接口。
- Excel `06_数据对象`：OBJ-122 `TenantExportJob`、OBJ-123 `DeletionRequest`，至少携带
  `id, tenant_id, created_at, updated_at, status`。
- Excel `07_Webhook事件`：EVT-101 `tenant.exported`、EVT-102
  `tenant.deletion_requested`，信封必须携带 event/tenant/time/actor/object/trace/payload/signature。
- Excel `08_验收用例`：TC-0177 主流程，TC-0178 权限、异常、隐私、缓存和跨租户边界。
- 架构设计 12.2/12.3：导出配置、账单/Usage、Trace/审计摘要；注销联动数据库、缓存、对象索引和
  Webhook 死信；账单与强审计按留存策略归档，不立即物理销毁。

### 1.1 本期交付

1. 创建可恢复的租户导出任务，打包 Provider 安全配置、Model、Alias、Usage/Cost/失败请求聚合与审计
   摘要；Provider Key、Virtual Key、Session Token、Prompt、Response 及凭据密文均不得进入导出包。
2. 提供任务状态和受保护 ZIP 下载，以便 PAGE-121 在刷新后恢复任务并显式领取制品。
3. 接收注销请求，强提醒先导出但不把成功导出作为前端或后端硬前置；后端再次核验权限、确认短语、
   tenant revision 和幂等语义。
4. 注销先在短事务冻结租户，再由可恢复 Worker 等待事件与既有租约收敛；最终 SQLite 排他事务归档
   强审计/Usage/Billing/FinOps 安全记录、删除可删除业务数据、失效缓存，并保留 tenant tombstone、
   DeletionRequest 和留存证明。
5. 创建 EVT-101/102 source event 和可恢复 outbox；事件 payload 只含状态、计数、hash、水位和留存
   策略版本，不含导出正文、确认短语、Key、Token、Prompt 或 Response。
6. 提供 PAGE-121 数据管理页，完整实现权限镜像、动态撤权、有限轮询、下载校验、危险操作 Dialog、
   owner/generation 清理与桌面/移动端实际页面验收。

### 1.2 非目标

- 不提供跨租户导出、全库备份、任意 SQL/表名选择、用户指定导出字段或导入恢复。
- 不导出 Provider 凭据密文、Key 指纹以外的认证材料、Virtual Key hash/prefix、Webhook secret 或 Session。
- 不导出 Prompt、Response、原始请求/响应 body、文件正文、Logo BLOB、Batch 输入输出或缓存正文。
- 不声称执行云对象存储、外部 Redis、DNS 或硬件擦除；本仓库未配置的外部后端必须由固定适配器
  返回 `not_configured`，不能伪造成功。SQLite 内部缓存必须实际清零并推进 generation。
- 不修改默认上游目录；OpenRouter、硅基流动仍只是竞品与迁移兼容对象。

## 2. 模块与数据流

### 2.1 后端修改

| 模块 | 职责 |
| --- | --- |
| `shared-sdk/python/metahub_shared/rbac.py` | catalog revision 24，新增数据管理四项最小权限 |
| `shared-sdk/python/metahub_shared/runtime/errors.py` | 固定 `tenant_data.*` 错误码与安全公开文案 |
| `shared-sdk/python/metahub_shared/runtime/config.py` | 独立 tenant-data AES-GCM key/version、生产必填与密钥域隔离 |
| `shared-sdk/python/metahub_shared/runtime/webhooks.py` | EVT-101/102 白名单、ID 和 payload 合同 |
| `control-plane-api/src/metahub_control/tenant_data/` | models、routes、export service/worker、deletion service、outbox/scheduler |
| `control-plane-api/src/metahub_control/config_transfer/service.py` | 只复用安全投影，不扩大 PRD-062 导出合同 |
| `control-plane-api/src/metahub_control/console/service.py` | PAGE-121 后端导航权限 |
| `control-plane-api/src/metahub_control/app.py` | 注册路由、no-store 边界和可停止 scheduler |
| `infra/migrations/0082_tenant_data_lifecycle.sql` | OBJ-122/123、archive、event/outbox、schema manifest、RBAC revision 24 |
| `infra/migrations/0082_tenant_data_lifecycle.down.sql` | 仅未发生导出/注销/留存事实时允许原子回退 |

### 2.2 Console 修改

新增 `lib/tenantDataLifecycle.js` 和 `features/tenant-data-lifecycle/` 的 state/render/bind；在
`main.js`、`renderDeveloperStart.js`、`permissions.js`、`messages.js`、`main.css` 接入 PAGE-121。页面不是
营销卡片，而是两个全宽工作区：导出任务和注销危险区。

### 2.3 主要数据流

导出：认证/RBAC -> 幂等任务 + 快照水位 -> Worker 租约 -> 事务外生成 ZIP -> hash/size 校验 ->
完成 CAS + audit + EVT-101/outbox -> 受保护下载。

注销：认证/RBAC -> 确认短语/revision/幂等 -> 短事务预检 schema、写 Request/EVT-102/outbox 并冻结租户 ->
Worker 等待事件投递和在途租约收敛 -> `BEGIN EXCLUSIVE` -> 强审计与账务分段归档 -> 删除 manifest 中
可删除租户行 -> 缓存 generation 推进 -> FK/integrity/计数复核 -> completed receipt。每个阶段持久化且可恢复；
最终擦除阶段内部任一步失败整体回滚，绝不留下半归档或半删除数据。

## 3. RBAC 与租户隔离

catalog revision 固定为 24：

| 权限 | 风险 | 默认角色 | 后端含义 |
| --- | --- | --- | --- |
| `tenant_data.read` | read | admin、security_admin | 查看 PAGE-121 和本租户任务安全状态 |
| `tenant_data.export` | critical | admin | 创建仅当前租户的安全导出任务 |
| `tenant_data.download` | critical | admin | 下载当前租户完成且未过期的制品 |
| `tenant_data.delete` | critical | admin | 请求注销当前租户并执行数据生命周期事务 |

自定义角色矩阵不静默扩权；只迁移仍等于 revision 23 默认 hash 的固定角色。所有请求在解析 body、job id
和确认短语前完成 live RBAC；写事务内再次调用 transaction permission 校验，禁止动态撤权竞态。角色名、
导航可见性、前端按钮和旧 Session claim 都不能补权。

`tenant_id` 只来自 `AuthContext`，不得出现在 request body/query/path 选择器中。Job、Request、Event、
Archive、Outbox 都携带 `tenant_id`，按 `(tenant_id, object_id)` 复合 owner 查询。未知、跨租户或 owner
损坏统一返回固定 not-found/forbidden 语义，不泄露另一租户存在性。

## 4. 数据库设计

所有时间为 UTC Z 整秒；所有可变状态使用 generation CAS。ID 为固定前缀加 32 个小写 hex。

### 4.1 `tenant_export_jobs`（OBJ-122）

| 字段 | 约束/用途 |
| --- | --- |
| `tenant_export_job_id` | PK，`tex_` + 32 hex；租户复合唯一 |
| `tenant_id` | NOT NULL FK tenants；所有查询首条件 |
| `requested_by_user_id` | 当前 actor 复合 FK；注销归档前只保留不可逆 actor hash |
| `request_id, trace_id` | 有界链路标识；`(tenant_id, requested_by_user_id, request_id)` 唯一 |
| `idempotency_key_hash, request_fingerprint` | 不存原始幂等键；同键异形请求冲突 |
| `status` | `pending/processing/completed/failed/expired` |
| `schema_version` | 固定 `metahub.tenant-export/v1` |
| `snapshot_at` | 三类数据共同快照时间 |
| `usage_snapshot_sequence, audit_snapshot_sequence` | 有界高水位；生成时不得越界读未来行 |
| `lease_owner_id, lease_token, lease_expires_at` | Worker 所有权；token 不出 API/日志 |
| `attempt_count, generation` | 最多 3 次，所有 claim/renew/finish 走 CAS |
| `manifest_json` | 仅 schema、counts、水位、hash，不含正文或 secret |
| `content_ciphertext, content_key_version, content_size, content_sha256` | AES-GCM ZIP；过期时原子清密文 |
| `expires_at, download_count, error_code` | 下载租约、追踪和固定失败分类 |
| `created_at, updated_at, completed_at` | 生命周期时间 |

### 4.2 `deletion_requests`（OBJ-123）

| 字段 | 约束/用途 |
| --- | --- |
| `deletion_request_id` | PK，`del_` + 32 hex；每租户最多一条非失败/终态事实 |
| `tenant_id` | NOT NULL FK tenants；tombstone owner |
| `actor_id_hash` | actor ID 的域分离 SHA-256；不保留用户 FK，便于删除用户表 |
| `request_id, trace_id` | 链路与幂等 owner |
| `idempotency_key_hash, request_fingerprint` | 同键同形返回同一 receipt；异形冲突 |
| `status` | `requested/quiescing/deleting/completed/failed`；Worker 分阶段单向推进 |
| `tenant_revision` | 提交时 CAS；阻止陈旧确认作用于已变更租户 |
| `confirmation_hash` | 仅保存规范确认短语的域分离 hash，不保存原文 |
| `retention_policy_version, schema_manifest_sha256` | 归档政策和实际执行 schema 证明 |
| `archived_rows, deleted_rows, retained_records` | 可复核计数，不含表内正文 |
| `cache_invalidated_at, completed_at, error_code` | 注销结果与固定失败分类 |
| `created_at, updated_at` | 生命周期时间 |

### 4.3 `tenant_data_states`

现有 `tenants` 没有 revision，不能把不存在的字段写进 API/CAS。新增每租户唯一 companion row：
`tenant_id, lifecycle_status(active/deletion_requested/deleted), revision, cache_generation,
active_deletion_request_id, updated_at`。迁移为现有 tenant 初始化，新 tenant 由 owner trigger 初始化。PAGE-121 的
`tenant_revision` 来自这里；注销请求短事务以 `(tenant_id, revision, active)` CAS 为唯一冻结点。该行和 tenants
tombstone 一同保留，不添加绕过 `tenants.status='active'` 的普通认证能力。

### 4.4 留存、事件与 schema manifest

- `tenant_retention_archives`：按 `deletion_request_id + source_table + segment_no` 保存 canonical NDJSON 的
  分段压缩后 AES-GCM 密文、key_version、row_count、uncompressed/compressed size、content_sha256、
  retention_class、retain_until；
  不设源业务 FK。每段未压缩字节和行数均有上限，按稳定主键流式生成，避免大租户一次占满内存；所有段
  hash 汇总为 table/root manifest。密文列不得进入 repr、API、日志或测试证据。每段 AAD 精确绑定
  tenant/request/table/segment/schema/policy，防止跨租户或跨段调包。二进制/凭据列不归档，只存固定
  redacted marker、size/hash。
- `tenant_lifecycle_events`：不可变 EVT-101/102 source；actor 只存域分离 hash，payload 为固定白名单摘要。
- `tenant_lifecycle_event_outbox`：复用现有 Webhook original delivery 冻结机制，保存不可变 delivery ID
  快照；pending/leased/delivered/no_subscription/failed，最多 10 次，generation/token CAS。EVT-102 必须
  在 tenant 仍 active、Webhook 行仍存在的请求短事务内冻结；专用 worker 被明确允许消费已 disabled tenant
  的**已持久化快照**，但不能创建新订阅或读取新业务配置。注销 Worker 只有在 outbox 和所有 original
  delivery 均终态后才进入擦除；终态 source/outbox 摘要归档为 operational retention，原 Endpoint、secret、
  delivery 和 dead-letter 业务行随后删除，不复制 URL 或签名 secret 到 lifecycle 表。
- `tenant_deletion_table_manifest`：全局 schema 元数据，不是业务数据。迁移显式登记 0082 时所有带
  `tenant_id` 的表及 `delete/archive_then_delete/preserve/root` 分类、列签名和触发器签名。运行时实际集合、
  列签名或触发器集合与 manifest 不一致即 `tenant_data.schema_changed`，事务回滚。后续迁移新增租户表必须
  同步更新 manifest。
- `tenant_deletion_trigger_manifest`：保存经迁移确认的目标表**全部触发器**名称、表名、SQL 和 SHA-256，
  不仅限于 BEFORE DELETE 保护。仅在 `BEGIN EXCLUSIVE`、manifest 完整、tenant 精确绑定且无其他连接
  写入时，按固定 manifest 临时移除对应触发器，避免 AFTER DELETE 产生未登记副作用；完成删除后用已
  校验的原始 SQL 恢复并逐一复核。DDL 与数据删除同一 SQLite 事务，崩溃自动回滚。任何未知触发器、
  SQL hash 变化或恢复失败均禁止提交。

留存源表至少覆盖 `audit_logs`、Usage/settlement、Billing/FinOps、不可抵赖 Security 事件；归档后原业务
行可删除，因为其完整 canonical 记录、hash、计数和留存期限仍存在。Provider credential、Session、Key、
Prompt、Response、缓存和普通业务配置不进入留存归档。

### 4.5 注销阶段与恢复顺序

1. POST 短 `BEGIN IMMEDIATE`：复核 live actor、tenant/state active revision、幂等指纹和完整 schema；写
   `DeletionRequest(requested)`、强审计请求事实、EVT-102 source/outbox 冻结快照；事件只含 request id、
   policy/schema hash 与阶段，不含确认原文。随后 CAS `tenant_data_states=deletion_requested`、
   `tenants.status=disabled`，撤销 Session/Virtual Key，提交并返回 202 requested receipt。
2. quiescing Worker 不再依赖已失效 Session 或 active actor，而只使用 POST 事务持久化的授权/owner/schema
   证明。它先完成该 tenant 的 EVT-101/102 全部冻结投递，再等待 Batch/File/Export/Webhook/Cache 等既有 lease 到终态或由各域
   reaper 安全回收；禁止因重试再次调用 Provider。pending/processing export generation 被推进并过期，
   旧 Worker 即使迟到也不能写回制品。
3. 所有 lease 收敛后 CAS 为 `deleting` 并开启 `BEGIN EXCLUSIVE`。再次核验 request、tenant tombstone、
   完整 schema/trigger manifest；任何新增未知租户表、触发器或 hash 漂移都关闭失败。
4. 对 archive 分类表按稳定主键流式生成有界 segment，canonical 序列化、压缩、hash 后写留存 BLOB；
   重读所有 segment count/hash/root manifest 复核，失败回滚整个排他事务。
5. 校验并临时移除 manifest 中目标表的全部触发器，`PRAGMA defer_foreign_keys=ON`；按完整 manifest 删除
   本 tenant 的所有 `delete/archive_then_delete` 行，包括 DB cache entries/fill leases、旧导出 BLOB、
   Webhook Endpoint/secret/delivery/dead-letter 和用户业务行。排他事务内同时执行 `integrity_check`，结果
   非唯一 `ok` 时回滚。
6. 保留 tenants tombstone、tenant_data_states、DeletionRequest、RetentionArchive、LifecycleEvent/Outbox
   归档摘要；恢复全部触发器并复核 schema hash。
7. 校验所有可删除表 tenant 行计数为 0、cache_generation 已推进、`foreign_key_check=0`、本事务计数与
   manifest 一致；将 request/state CAS 为 completed/deleted 后提交。擦除事务任何异常整体回滚；Worker
   保留 disabled/quiescing 状态并用同一 generation 恢复，不会把租户误恢复 active。

## 5. API 合同与错误码

所有受保护 JSON 成功/失败响应携带 `request_id, trace_id`，并返回
`Cache-Control: no-store, private`。客户端不得提交 `tenant_id`、actor、role、服务器 URL 或表名。

### 5.1 权威 POST

- `POST /tenant/export`：权限 `tenant_data.export`，body 必须是空 JSON object，要求单一
  `Idempotency-Key`；返回 202 exact Job DTO。
- `POST /tenant/deletion-request`：权限 `tenant_data.delete`，body exact keys：
  `confirmation, tenant_revision`。确认值必须精确等于当前 tenant slug；后端 hash 后立即丢弃原文。成功
  返回 202 requested receipt，并让当前 Session 在响应后失效。页面强提醒导出，但没有 export_job_id 前置。

### 5.2 闭环扩展接口

Excel 只列两个命令 POST，但刷新恢复和制品领取需要只读闭环，故增加：

- `GET /tenant/data-management`：`tenant_data.read`，返回当前 tenant revision、最近安全 Job/Request 状态。
- `GET /tenant/exports/{job_id}`：`tenant_data.read`，只返回当前 owner 的状态、计数、hash、过期时间。
- `GET /tenant/exports/{job_id}/download`：`tenant_data.download`；仅 completed/未过期，固定 ZIP MIME、
  ASCII basename、Content-Length、SHA-256/row-count 头、`nosniff` 和 no-store。

注销请求提交后 tenant 已 disabled，普通认证接口不可继续轮询；POST 的 requested receipt 是用户侧最终
回执，后台以持久 DeletionRequest/审计/事件完成后续恢复。重新登录和旧 Session/Virtual Key 必须失败，
不能为查看注销状态新增绕过 tenant active 的认证后门。

### 5.3 固定错误码

`tenant_data.request_invalid`、`tenant_data.forbidden`、`tenant_data.not_found`、
`tenant_data.idempotency_conflict`、`tenant_data.export_not_ready`、`tenant_data.export_expired`、
`tenant_data.export_failed`、`tenant_data.revision_conflict`、`tenant_data.confirmation_mismatch`、
`tenant_data.deletion_in_progress`、`tenant_data.schema_changed`、`tenant_data.retention_failed`、
`tenant_data.unavailable`。

错误公开文案不包含 SQL、表名、文件路径、上游地址、secret、正文、确认短语或另一租户 ID；统一错误层
自动附加 request_id/trace_id。

## 6. 导出制品合同

ZIP 条目固定且使用 ASCII 文件名，无用户控制路径、注释、extra field 或 symlink：

- `manifest.json`：schema version、job id、snapshot time、各文件 size/hash/count、高水位。
- `providers.json`：provider code/name/status/endpoint 类型和 credential_present/key_version 等安全元数据；
  不读取或写入 encrypted_key、key prefix、key hash、Token 或请求头。
- `models.json`、`aliases.json`：当前租户安全配置，不含 Provider 认证材料。
- `usage.csv`：水位内 request/model/provider、输入/输出 token、cost、currency、success/error_code 摘要；
  不含 Prompt/Response/原请求正文字段。
- `audit-summary.json`：按 action/object_type/day 聚合 count、first/last time；不含 before/after summary、
  object_name、IP、user-agent 或自由文本。

Worker 在短事务内 claim 并冻结水位，在事务外生成有界 ZIP；ZIP 只在内存短暂存在，随后用独立
`METAHUB_TENANT_DATA_ENCRYPTION_SECRET` 和 version 执行 AES-256-GCM，AAD 绑定 tenant/job/snapshot/schema。
完成事务重新验证租约和 snapshot 后只写密文/hash/manifest。该 secret 必须与 Provider、Auth、Batch、File、
Prompt、MCP 和 Response Cache 密钥域不同，生产环境缺失或复用即 feature fail-closed；开发/测试使用固定
非生产默认值但不得出现在证据。两个 worker 只有一个 CAS winner；租约过期可恢复，三次失败终态 failed。
下载时在内存解密并同时复核 AEAD、plaintext hash/size 和 ZIP manifest，响应结束立即释放；过期任务先清
密文再返回固定错误。

## 7. PAGE-121 交互与生命周期

- 后端导航 ID 固定 `tenant-data-lifecycle`，位于配置组；仅 `tenant_data.read` 可见，Hash 仍必须与
  `/console/navigation` 求交，非法/越权 Hash 回退 overview。
- 页面 owner 固定 tenant/user/session、session revision、permission revision、authorization fingerprint、
  后端导航镜像、四项 capability identity 和 generation；不从 role 名推导按钮。
- 导出区展示安全范围和最近任务；创建、轮询、下载为独立 lane。轮询有上限，离页只 abort 浏览器轮询，
  不取消服务端 Job。Job ID 严格正则后拼固定同源路径，不跟随响应 URL。
- 下载严格核验 ZIP MIME、ASCII filename、no-store、nosniff、有界长度、完整性头；Blob 仅存在于点击
  调用栈，一次性 object URL 在 `finally` 回收，不写 Storage/DOM snapshot。
- 注销危险区明确显示“先导出”强提醒但不阻塞直接注销。原生 `<dialog>` 要求 checkbox 和输入当前 tenant
  slug；busy 时阻止 Escape/关闭、陷阱 Tab，成功后清空输入和受保护 DOM 并转到登录页。
- Hash 离页、pagehide、身份/tenant/revision/fingerprint 漂移、导航裁剪、登录页重建、注销按钮、侧栏切换
  均先清空确认值/tenant 事实、关闭 Dialog、回收 URL、推进 generation，再 abort 所有 lane。
- 只有 `auth.permission_denied` 触发全局撤权；业务 403/409 不得误判为 Session 撤权。

## 8. 安全、隐私与可追踪性

- Provider Key、Token、Prompt、Response 不得明文进入日志、audit、event/outbox、API 错误、测试证据、
  ZIP、DOM、Storage、Console 或 URL。测试使用合成 canary，仅记录零命中计数，不打印 canary 本身。
- Usage/Cost/失败请求在导出聚合和注销留存中有 tenant、水位、request/error、token/cost/count/hash，可追踪
  但不包含正文。
- 所有动态 SQL 标识符只能来自已迁移、hash 校验的 manifest，先按 SQLite identifier grammar 和 exact
  sqlite_schema 复核，再双引号转义；绝不接受 HTTP/环境变量/文件输入的表名或触发器 SQL。
- 注销事务排他且原子；不使用 `reset --hard`、数据库文件替换或绕过迁移。错误路径必须 rollback 并恢复
  tenant active、Session/Key、触发器和所有业务行。
- scheduler shutdown 必须停止新 claim、等待当前短数据库阶段并释放连接；不得在 shutdown 或重试时记录
  制品、secret、确认值或内部异常正文。
- migration runner 在应用任何 0083+ schema 迁移前必须确认没有 `quiescing/deleting` request；0082 之后新增、
  改名或删除 tenant 表/trigger 的迁移必须在同一 migration 更新 deletion manifest。Worker 持有
  `BEGIN EXCLUSIVE` 时迁移只能等待/失败，不得并发改 schema。

## 9. 测试与验收计划

### 9.1 后端专项

- 导出主流程：exact ZIP、Provider/Model/Alias、Usage/Cost/failure、audit aggregate、水位/hash/count 可复核。
- 隐私：Key/Token/Prompt/Response/credential ciphertext/secret canary 在解密 ZIP、API、audit/event/outbox/log
  0 命中；数据库只命中预期 AES-GCM 密文，禁止明文 ZIP/NDJSON 残留。
- 幂等/并发：同 request/key 同形重放；异形冲突；双 worker 一 winner；租约过期恢复；三次失败终态；
  过期 BLOB 清除；旧 token/generation 不能完成。
- RBAC：admin 可执行；security_admin 只读；其他固定角色与伪造按钮/body 均被后端拒绝；事务中撤权关闭失败。
- 跨租户：A 的 job/request/event/archive/download 绝不命中 B；非法 ID 与不存在/跨租户同形。
- 注销：有/无成功导出均可确认；短语/revision 错误无副作用；A 注销不影响 B；完整业务行删除、强审计/
  Usage/Billing/FinOps archive 可复核、cache entries/fills 0、旧 Session/Virtual Key/Gateway 请求失败。
- 原子恢复：archive/encrypt/trigger drop/delete/trigger restore/FK/integrity/commit 各阶段注入失败均全量 rollback；未知表、
  未知触发器、schema/hash 变化关闭失败；8 组 FK cycle 与 immutable DELETE trigger 回归。
- outbox：冻结订阅、双 worker、续租、重试、无订阅、终态和旧 generation；删除前快照不追随后来订阅。

### 9.2 迁移与回退

- 空 SQLite 0001..0082 实际 up：最高 `82/0082`、`integrity_check=ok`、外键违规 0。
- 0081 seeded -> 0082 -> guarded down -> 0082 往返 hash；无业务事实时可回退。
- 任一 completed/processing export、deletion、archive、event/outbox 或 revision-24 自定义角色授权存在时，down
  原子拒绝；不可逆擦除完成后永远不得伪造恢复。
- 迁移后 manifest 精确覆盖所有带 tenant_id 业务表和目标表全部 trigger；不存在 `.sqlite*` sidecar 泄漏。

### 9.3 Console 与实际运行

- Client exact DTO、未知字段、跨租户 owner、非法 ID/status、no tenant 参数、错误 request_id。
- 状态机动态撤权、tenant/session/revision/fingerprint 漂移、迟到 200/401/403、clear-before-abort、lane 隔离。
- Dialog 确认、不强制先导出、busy Escape/Tab、焦点恢复、成功后 protected DOM/Storage 归零。
- 390x844、1280x720、1440x900 实际 Browser：无横向溢出、遮挡、文本溢出；下载 URL 回收；第二租户
  登录无旧 DOM；Network/Console/Storage/证据无敏感正文。
- 真实 Control runtime 健康、创建/处理/下载导出、实际注销隔离 tenant，Gateway 旧 key 拒绝；只记录
  status、request_id、hash/count 和零敏感命中。

## 10. 代码修改顺序

1. 完成本文两轮编码前自审并修正 P0/P1。
2. 新增 0082 正反迁移、RBAC/error/webhook 合同和迁移专项。
3. 实现 tenant_data models/routes/export worker/deletion transaction/outbox/scheduler 及后端专项。
4. 注册 app、Console navigation 与 PAGE-121 client/state/render/bind，完成 JS 单元与 Browser driver/harness。
5. 执行受影响专项、必要相邻回归、迁移、中文注释、真实 Runtime/Browser 和隐私门禁。
6. 更新 README、目录说明、Excel 状态与 `docs/testing/PRD-089-tests.md`；交叉确认下一条但不启动。
7. 审计 Git 禁止文件和敏感信息，确认 `all_108_prds_complete=false`，提交并推送 `origin/main`。

## 11. 编码前自审记录

### 11.1 第一轮：边界、并发与恢复

结论：初稿发现 5 个 P0/P1，已在本文修正后关闭。

1. **同步注销无法可靠投递 EVT-102（P0）**：若同一事务先删 Webhook 行，提交后已无可用 endpoint；若在
   事务中出网又破坏原子性。修正为 POST 短事务冻结 original delivery、禁用 tenant，专用 Worker 先让
   outbox 终态，再执行最终擦除。
2. **缺少在途 Worker 收敛（P0）**：仅用排他数据库锁不能阻止已在事务外调用 Provider 的旧 worker 迟到。
   修正为 quiescing 阶段推进 generation、终止 pending export，并等待各域 lease/reaper 终态；迟到 CAS
   不能写回，且不允许再次调用 Provider。
3. **不存在 tenant revision（P1）**：原设计引用了现有 schema 没有的字段。修正为新增
   `tenant_data_states` companion revision/CAS，不篡改认证合同或虚构 deleted tenant status。
4. **单表归档可能无界（P1）**：修正为稳定游标、行数/字节双上限的分段压缩归档和 root manifest。
5. **只移除 DELETE 触发器不完整（P0）**：AFTER/其他 trigger 仍可能产生副作用。修正为 manifest/hash
   校验目标表全部 trigger，在同一排他事务临时移除并完整恢复；未知项一律关闭失败。

### 11.2 第二轮：隐私、兼容与运维

结论：复审发现 4 个 P0/P1，已在本文修正后关闭；编码入口条件满足。

1. **ZIP/留存明文驻留（P0）**：安全导出仍包含租户 endpoint、Usage 和审计元数据，普通 BLOB 不足。
   修正为独立 tenant-data AES-256-GCM 密钥域、version 和 owner AAD；数据库只存密文，下载/归档复核
   AEAD/hash/size，生产缺密钥或密钥复用关闭失败。
2. **只等待 EVT-102 会遗漏 EVT-101（P1）**：注销可能与已完成导出的 outbox 竞态。修正为 quiescing
   等待该 tenant 全部 lifecycle source/outbox 和 original deliveries 终态，再删除 Webhook 数据。
3. **迁移与删除 manifest 竞态（P0）**：0083+ 若在 quiescing/deleting 时改表会破坏固定清单。
   修正为 migration guard、排他锁和后续 migration 同步更新 manifest 的发布合同。
4. **归档可能继承历史自由文本（P1）**：即使源 audit 违反预期，也不能让 Prompt/Response 明文扩散。
   修正为 archive 只选明确强审计/Usage/Billing/FinOps 表，二进制/credential 列固定脱敏，其余 canonical
   记录压缩后立即 AEAD；API、日志、event、证据从不解密或打印归档正文。

两轮自审后无未关闭 P0/P1。编码必须逐项实现上述修正；任何实现偏离都需要先回到本文重新自审。
