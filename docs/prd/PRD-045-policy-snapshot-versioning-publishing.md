# PRD-045 - 策略快照版本化与发布

状态：已完成。设计及两轮独立自审先于编码通过，实现、专项/全量测试、运行库迁移、真实三服务/API 与三档浏览器验收均已封账。

## 需求理解

### 来源、顺序与验收合同

- Excel `01_PRD总表` 与 `02_M0-M1核心开发` 明确：PRD-045 属于 `M1-企业POC`、`P1`、核心层，模块为“策略快照”，名称为“策略快照版本化与发布”。
- 启动本条开发时，README、`docs/prd` 与 `docs/testing` 的完整完成证据连续到 PRD-044，因此 PRD-045 是按 `M0-W1 -> M0-W2 -> M0.5 -> M1 -> M2 -> M3` 顺序识别出的下一条未完成 PRD。
- Excel 指定页面为“策略发布页、快照历史”，指定写接口为 `POST /policy-snapshots/publish` 与 `POST /policy-snapshots/{id}/rollback`，对象为 `PolicySnapshot`，Webhook 事件为 `policy_snapshot.published`。
- `TC-0089` 要求：发布后数据面在可配置时间内更新；回滚后新请求使用旧内容；调用日志可查询实际使用的 `snapshot_version`。
- `TC-0090` 要求：覆盖一致性、回滚、异常、权限与安全边界；统一错误不得泄露敏感信息；M1 不做复杂灰度双跑。
- 架构 `5.3` 要求控制面生成版本，数据面定期拉取并保存在本地内存/Redis，每次请求记录 `snapshot_version`；`5.4` 要求控制面故障时继续使用 LKG，超过有效期进入保护模式；附录 `A.3` 要求配置先校验、后保存、再编译、最后下发，并记录版本、变更人、原因和审批状态。

### 用户价值与交付闭环

企业管理员不能让“已保存但未发布”的策略立即、隐式地改变线上流量。PRD-045 在 PRD-044 的草稿管理与纯 DSL 求值器之上交付以下闭环：

1. `policies.status=active` 只表示下一次发布的候选；管理员显式发布后，控制面把同一租户全部 active Policy 编译为一份不可变快照。
2. 每次发布生成租户内严格单调的 `snapshot_version`，快照内容、操作者、原因、审批状态、request_id/trace_id、审计与 Webhook 事件在一个事务中形成一致事实。
3. Gateway 通过强制内部服务身份按 tenant 从 Control API 拉取。未知 tenant 的首个已认证请求执行一次有界 singleflight 同步，之后后台只轮询已知 tenant；普通请求不访问控制面，也不在热路径编译表达式，完整响应校验通过后才原子替换该 tenant 的内存缓存。
4. 一次请求在完成 Virtual Key 鉴权后固定持有一个不可变快照引用。后续缓存刷新不能改变该请求在路由、Fallback、流式会话与日志中使用的版本。
5. 策略求值发生在现有 DLP、限流、预算和 Provider 调用之前；`allow` 不绕过现有 Guard，`deny` 与求值失败在 Provider 前拒绝，`route_to` 必须重新执行租户、环境、模型、Provider、健康与能力校验。
6. 回滚不是把当前 head 数字退回旧版本，而是复制上一版内容并创建新的单调版本。例如 `v1=A -> v2=B -> rollback(v1)` 生成 `v3=A`；请求日志记录 v3，并通过 `rollback_source_version=1` 解释来源。这样避免数据面只接受更高版本时发生 ABA。
7. 控制面短暂不可用时，Gateway 继续使用未过期 LKG；`internal_only` 在普通 max-age 后进入有硬时限的 emergency LKG，并保留现有全部 Guard，超过 emergency max-age 后也失败关闭；`public_service` 在普通 max-age 即失败关闭。没有任何成功同步且没有 LKG 的冷启动一律失败关闭。

### 范围边界

- 本 PRD 只快照和执行 PRD-044 的 Policy DSL，不提前把 KeyPool、预算、限流或 DLP 配置合并为统一全量配置快照。架构中的全配置快照由后续 PRD 按相同版本框架扩展。
- `mask` 当前没有能证明已真正修改 Prompt/Response 的执行器。active `mask` 策略必须在发布阶段以稳定错误拒绝；Gateway 对损坏快照中的 `mask` 仍防御性失败关闭，绝不能静默当作 allow。
- `data_class` 当前没有来自 Virtual Key、Project/Application 或服务端分类器的可信来源。active 且引用 `data_class` 的策略必须在发布阶段拒绝；不得接受请求体或任意 Header 伪造分类。
- `budget_remaining` 由 Gateway 按认证 tenant、Environment、Project/Application、Virtual Key 的适用 active 预算规则计算“最小非负剩余额度”。没有适用预算规则时该变量缺失；引用它的策略会按 DSL 合同失败关闭。最终 100% 阻断与 reservation 仍由既有 Budget Guard 权威执行。
- 不做复杂灰度、双跑、按实例 ACK、推送、跨地域一致性或 Redis 持久 LKG。页面只能声称“已发布，等待数据面在轮询周期内拉取”，不能在没有 consumer ACK 的情况下显示“全部实例已生效”。
- OpenRouter、硅基流动仍仅是竞品、对标和迁移兼容对象，快照不新增任何默认上游或竞品品牌 Provider 枚举。

## 核心不变量

1. 所有快照、快照项、head、事件、审计和请求日志查询均以可信 `tenant_id` 作为第一边界；公共管理 API 不接受客户端 `tenant_id`。
2. 同一租户的 `snapshot_version` 只增不减且连续加一；一次发布或回滚最多激活一个新版本。
3. 快照先以 `building` 写入完整 items，再一次性封印为 `published`；head 只能指向已封印快照。半快照永不对外可见。
4. 已发布 header、items、事件不可 UPDATE/DELETE；回滚只能复制旧内容创建新版本。
5. `content_hash` 绑定 tenant、schema 与策略执行内容，不覆盖版本、actor、时间、原因或纯 provenance；同 tenant 回滚副本与来源的 content hash 必须相同。另用 `artifact_hash` 覆盖 tenant、wire schema、快照版本、provenance 与完整 items，防止 envelope/来源字段被替换。
6. 发布事务重新读取并编译全部 active Policy，不能信任前端校验结果、已存 AST 或 hash；缺 Rule/Action、未知 DSL、失效 route target、`mask` 或不可信变量都会使整次发布回滚。
7. Gateway 不信任 JSON AST，不使用 `eval`。它只接受 canonical expression，在后台重新调用 shared 编译器并核对 condition hash、策略顺序、policy_count 与 content_hash。
8. 一次请求只使用一个快照引用和版本。缓存 swap、流式回调和 Fallback 不能改变该引用。
9. Provider Key、Virtual Key、Authorization、Token、Prompt、Response、原始 Provider 错误和策略表达式不得进入结构化日志、审计、Webhook 或公开快照历史响应。
10. 发布/回滚成功的快照、head、事件、审计必须全有；任一提交前步骤失败则全无。提交后 Webhook 投递失败不得回滚已激活事实。

## 涉及模块

### Shared SDK

- 新增 `metahub_shared.runtime.policy_snapshot`：快照 wire schema、严格反序列化、canonical content hash、不可变运行对象和版本常量。
- 扩展 `runtime.config`：轮询开关、轮询间隔、HTTP 超时、最大有效期与响应体上限；所有数值有安全上下界与交叉校验。
- 扩展 `runtime.errors`、`error_diagnosis` 与 `webhooks`：统一错误、诊断与 `policy_snapshot.published` 白名单。
- `policy_dsl` 继续作为唯一编译/求值实现；若需要从 canonical expression 恢复运行对象，只能通过现有编译入口，不能直接构造未经语义验证的 AST。

### Control Plane API

- 新增 `metahub_control.policy_snapshots` 包：models、routes、service。
- 公共管理 API 复用 `require_policy_admin`，只允许 admin；developer/viewer 即使手写 HTTP 也返回后端权限错误。
- 内部拉取 API 使用 `X-Internal-Token`，无论 dev/test/prod 都必须配置并用常量时间比较。它不接受 tenant query，不复用用户 Bearer Token，也不暴露表达式给浏览器。
- 发布/回滚复用审计事务与提交后 Webhook 投递模式；公共响应和内部快照响应都 `Cache-Control: no-store`，内部接口使用强 ETag 支持 304。
- Console Navigation 新增 admin-only `policy-snapshots` 入口。

### Gateway Data Plane

- 新增 `metahub_gateway.policies.cache`：按 tenant singleflight 首次拉取、后台轮询、per-tenant ETag/304、大小限制、LKG 与原子缓存替换。
- 新增 `metahub_gateway.policies.runtime`：请求快照租约、可信上下文构造、纯求值、route_to 路由重解析和安全日志摘要。
- Gateway lifespan 只校验配置并安装独立同步 scheduler；首个已认证 tenant 才做有界同步，之后按配置轮询已知 tenant。关闭时有界停止，不能阻塞模型请求或覆盖既有 Webhook lifespan。
- `openai_compatible` 在现有主流程中固定快照、求值并把结果复制到 `RequestLogContext`；所有成功、异常、Fallback 与流终态沿用同一上下文。
- Control Debug Console 当前直接调用 Provider 并复用同一 request_logs/usage 写入；它必须在 Provider 前调用 shared request claim，固定 `snapshot_version=NULL/policy_snapshot_status=disabled`，避免 0034 一致性触发器破坏既有成功、失败与 Usage 闭环。
- `budgets` 提供只读的策略上下文余额函数；只有当前快照实际引用 `budget_remaining` 时才查询。
- `routing` 提供按 tenant 内 `model_id` 解析 Policy route target 的受控入口，重新检查模型/Provider/健康/能力，不复用原别名 Fallback。

### Console Web

- 新增 `src/lib/policySnapshots.js` 与 `features/policy-snapshots/renderPolicySnapshotsPanel.js`。
- 页面展示当前版本、内容 hash 摘要、策略数量、发布来源、原因、发布时间和历史；发布/回滚均要求原因并使用当前 ETag 做强 CAS。
- 只给上一激活版本显示回滚命令；确认后使用稳定 request_id 重试，网络结果不确定时不得生成新幂等键。
- 调用日志列表/详情展示 `snapshot_version`、快照状态、策略决策与安全 reason，不展示 DSL、AST、Prompt 或 Response。
- developer/viewer 不加载页面、不预取 API；这只是体验优化，真实权限仍由后端执行。

## 数据表和字段设计

迁移文件为 `infra/migrations/0034_policy_snapshot_versioning.sql` 与对应 down migration。

### `policy_snapshots`

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `policy_snapshot_id` | PK，`psnap_*` | 全局稳定 ID。 |
| `tenant_id` | NOT NULL，复合租户 FK | 业务隔离边界。 |
| `snapshot_version` | INTEGER >= 1 | 租户内严格单调版本；`UNIQUE(tenant_id, snapshot_version)`。 |
| `schema_version` | 固定 `policy-snapshot-v1` | wire 与 hash 解释版本。 |
| `state` | `building/published` | 只允许一次 building -> published。 |
| `operation` | `publish/rollback` | 激活原因类型。 |
| `previous_snapshot_id` | nullable 复合租户 FK | v1 为 NULL，后续必须指向激活前 head。 |
| `previous_snapshot_version` | nullable INTEGER | 与 previous ID 组成同租户三列 FK；必须等于当前版本减一。 |
| `rollback_source_snapshot_id` | nullable 复合租户 FK | publish 为 NULL；rollback 指向被复制的上一版。 |
| `rollback_source_snapshot_version` | nullable INTEGER | 与 source ID 组成同租户三列 FK；必须小于新版本。 |
| `request_id` / `trace_id` | 有界非空 | 幂等、审计与排障链路。 |
| `request_fingerprint` | 64 位小写 SHA-256 | actor、operation、head/candidate CAS、target、reason 的稳定指纹。 |
| `content_hash` | building 可空，published 64 位 SHA-256 | 快照内容完整性。 |
| `artifact_hash` | building 可空，published 64 位 SHA-256 | tenant/wire/version/provenance 完整性与 per-tenant ETag 输入。 |
| `policy_count` | building 可空，published >= 0 | 封印时校验 item 数。 |
| `change_reason` | 1..256 | 管理员必填原因；不允许控制字符。 |
| `approval_status` | 固定 `not_required` | M1 无审批流，但按架构保留可追踪状态。 |
| `created_by` | 当前租户 user 复合 FK | 变更人。 |
| `created_at` / `published_at` | UTC ISO | building/published 生命周期。 |

候选键必须显式创建：`UNIQUE(tenant_id, policy_snapshot_id)`、`UNIQUE(tenant_id, snapshot_version)`、`UNIQUE(tenant_id, policy_snapshot_id, snapshot_version)`；self previous/source、items、head、events 分别引用与自身字段完全同序的复合候选键，不能依赖全局 ID 推断租户。索引另含 tenant+version DESC、tenant+created_at DESC；`UNIQUE(tenant_id, request_id)` 支撑租户级 API 幂等。同 actor+同 fingerprint 才回放原结果；其他 actor 复用该 request_id 返回通用幂等冲突，不能读取原结果。

header 封印规则由触发器明确限制：building INSERT 时 hash/count/published_at 必须 NULL；唯一允许的 UPDATE 是 building -> published，且只能改变 `state/content_hash/artifact_hash/policy_count/published_at`，其余每列用 SQLite `IS` 做 NULL-safe 不变比较。seal 时 DB 只验证 item 实际 count、`MIN(order)=0`、`MAX(order)=count-1`、`COUNT(DISTINCT order)=count` 与字段格式；hash 正确性由 shared canonical 函数在服务事务内重算，文档不虚假声称 SQLite 会计算 SHA-256。published header 不可 UPDATE/DELETE。

普通服务从不提交 building：任一失败直接 rollback。为处理人工/故障恢复，DB 只允许在未被 head/event 引用时先删除 building items、再删除 building header；内部/public 查询始终只 JOIN head+published，因此即使存在异常 building 也不可见。恢复动作不属于管理 API，必须经备份和运维审批。

### `policy_snapshot_items`

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `policy_snapshot_item_id` | PK，`psitem_*` | 不可变快照项 ID。 |
| `tenant_id` | NOT NULL | 与 snapshot/source policy/target 同租户。 |
| `policy_snapshot_id` | 复合租户 FK | 父快照。 |
| `evaluation_order` | INTEGER >= 0 | 按 `priority, policy_id` 固定顺序；每快照唯一。 |
| `source_policy_id` | 复合租户 FK | 来源 Policy。 |
| `source_policy_version` | INTEGER >= 1 | 发布时 Policy 版本，属于 provenance；不改变执行 content hash。 |
| `policy_rule_id` / `policy_action_id` | 有界非空 | 可解释来源，不作为跨租户授权凭证。 |
| `priority` | 1..10000 | 求值顺序。 |
| `dsl_version` | 固定 `policy-dsl-v1` | 解释版本。 |
| `canonical_expression` | 有界 UTF-8 | 服务端重新编译后的 canonical 文本；只进入 DB 与内部 wire。 |
| `condition_hash` | 64 位 SHA-256 | Gateway 重编译后核对。 |
| `variables_json` | 最多 6 个白名单变量 | 快速判断上下文依赖。 |
| `action_type` | DSL 动作枚举 | v1 已发布快照实际不会含 mask。 |
| `target_model_id` | route_to 时非空 | 复合租户 FK；发布/回滚时复核 active。 |
| `created_at` | UTC ISO | 快照创建事实。 |

只允许向 building 快照 INSERT；UPDATE 全拒绝，DELETE 仅在父 snapshot 仍 building 且未激活时用于受控清理，published item 不可删。表不保存原始 `condition_expr`、Policy name/description、示例上下文、Prompt、Response 或任何 Key/Token。回滚项必须逐字段等于 source snapshot，防止伪造“回滚”。

### `policy_snapshot_heads`

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `tenant_id` | PK/FK | 每租户至多一个当前 head。 |
| `current_snapshot_id` | 复合租户 FK | 必须指向 published。 |
| `current_snapshot_version` | INTEGER >= 1 | 必须等于 snapshot 版本。 |
| `updated_by` / `updated_at` | 复合 user FK / UTC | 最近激活者与时间。 |

首次只能插入 v1；更新必须精确 `old+1` 且新 snapshot.previous 指向旧 head。tenant 不可变。

head 使用三列 FK `(tenant_id, current_snapshot_id, current_snapshot_version)` 指向 snapshots 同序候选键；`updated_by` 使用 `(tenant_id, user_id)`。items/events 的 snapshot 关系也使用 tenant+ID 或 tenant+ID+version 复合 FK；item 的 live source Policy、route target 与所有 actor 分别复用 0033 已存在的 `(tenant_id, policy_id)`、`(tenant_id, model_id)`、`(tenant_id, user_id)` 唯一键。0034 up/down 都必须保留这些父候选索引，不能因重建 audit/request logs 误删。

### `policy_snapshot_events`

追加保存 `policy_snapshot.published` 的持久事件事实：event ID、tenant、snapshot ID/version、operation、actor、request_id/trace_id、安全 payload 与时间。snapshot 使用三列复合 FK，actor 使用 tenant+user FK。payload 只含 version、content_hash、policy_count、operation、previous_version、rollback_source_version；事件 UPDATE/DELETE 拒绝，tenant+snapshot/event 与 tenant+request/event 双重唯一。

### `request_logs` 变更

0034 原子重建 `request_logs`，把历史全局 `request_id` 主键升级为 `PRIMARY KEY(tenant_id, request_id)`，从数据库层消除一个租户抢占另一个租户 request_id 的可用性耦合；全部既有 43 列、15 个显式索引与 4 条触发器逐字段恢复。新增以下安全追踪字段：

- `snapshot_version INTEGER NULL CHECK(snapshot_version >= 1)`：历史记录和明确无已发布快照的请求为 NULL。
- `policy_snapshot_status TEXT NOT NULL DEFAULT 'disabled'`：CHECK 仅允许 `disabled/not_published/current/lkg/emergency_lkg/expired/unavailable`；历史行统一 disabled，不伪造曾执行 Policy。
- `policy_decision`：`allow/deny/route_to` 或 NULL。
- `policy_reason_code`：稳定 reason，不包含表达式或上下文值。
- `policy_id` / `policy_action_type` / `policy_target_model_id`：仅保存最终决定性命中摘要，可空。

0034 同时原子重建 `usage_ledger`，完整保留既有 32 列、索引/触发器和 `usage_ledger_id` 主键，把外键改为 `FOREIGN KEY(tenant_id, request_id) REFERENCES request_logs(tenant_id, request_id)`。`record_request_log` 必须改用 `ON CONFLICT(tenant_id, request_id)`；所有 request_log/Usage 的 UPDATE、SELECT、JOIN、幂等检查和控制面详情都必须同时带 tenant，任何遗留 `ON CONFLICT(request_id)` 会在专项静态扫描和跨租户同 ID 测试中失败。日志触发器要求非空 version 属于同 tenant 的 published snapshot，并要求 tenant、trace_id、snapshot_version（NULL-safe）与 policy_snapshot_status 全部等于 claim；version/status 一经 INSERT 不得改写。索引为 `(tenant_id, snapshot_version, created_at DESC, request_id DESC)` 的非空部分索引。

### `gateway_request_claims`

新增轻量请求占位表，精确字段为：`tenant_id TEXT NOT NULL`、`request_id TEXT NOT NULL`、`trace_id TEXT NOT NULL`、`virtual_key_id TEXT NULL`、`snapshot_version INTEGER NULL CHECK(NULL OR >=1)`、`policy_snapshot_status TEXT NOT NULL`（固定枚举）、`claimed_at TEXT NOT NULL`，主键为 `(tenant_id, request_id)`；tenant 与可选 Virtual Key 使用同租户复合 FK。0034 必须先创建 `UNIQUE(tenant_id, virtual_key_id)` 候选索引，down 在 claims 删除后最后移除。DB CHECK 要求非空 version 只能配 current/lkg/emergency_lkg/expired；NULL 可配 disabled/not_published/unavailable，也可配 lkg/emergency_lkg/expired 来表达“已成功同步的无-head 空基线”在后续故障中的 LKG 生命周期，但不能配 current。非空 version 另由触发器验证属于同 tenant published 快照。身份、trace/version/status UPDATE/DELETE 全拒绝。0034 在启用一致性触发器前把全部历史 request_logs 的新 status 统一回填 `disabled`，再逐行回填同 tenant/trace/version/status claim；新请求在认证并取得 snapshot lease decision 后、读取 JSON 前用独立短事务 INSERT：

- INSERT 成功后本次 HTTP 请求才可继续；同 tenant 并发/历史复用 request_id 在 Provider 前返回 `gateway.request_id_reused`，不回放前次响应；不同 tenant 可安全使用相同外部 ID，所有查询/关联仍必须同时带 tenant。
- request_logs INSERT/UPSERT 必须存在 tenant/request_id 的 claim，且 trace/version/status NULL-safe 一致；这些字段不能在同一流式/Fallback 请求补写时漂移。
- 这允许同一请求的流式终态、Fallback 和 Usage 补写继续 UPSERT 同一版本，同时阻止第二次 HTTP 调用在 v1 后实际执行 v2 却尝试改写日志版本。
- 进程异常时 claim 仍提供 request_id/trace_id/tenant/snapshot 的最小追踪事实；M1 不做自动重放。后续完整 started 状态展示可独立演进。
- Debug Console 与测试中直接调用 ledger 的入口都迁移到同一 claim 函数；Debug Console 不是 Gateway 数据面策略消费者，固定 version=NULL、status=disabled。该边界必须在页面和日志中如实展示，不能伪造已执行线上 Policy。
- 0034 down 在重建旧全局 request_id 主键前先检测跨 tenant 重复 ID；存在冲突时稳定失败并要求先导出/消解，绝不静默改 ID、丢日志或错绑 Usage。受控 up/down/up 验收数据保证无该冲突。

### `audit_logs` 变更

按 0033 完整字段重建 CHECK，保留全部 17 个字段、既有动作/对象、5 个索引与 3 个触发器；新增：

- action：`policy_snapshot.published`、`policy_snapshot.rolled_back`
- object_type：`policy_snapshot`

before/after 只含版本、hash、数量、operation、previous/rollback source 等安全摘要。down migration 只过滤 PRD-045 新审计事实，再恢复 0033 合同。

### 0034 down 依赖顺序

1. 先按 delivery.event_type 结构化定位 `policy_snapshot.published`：删除其全部 attempts，再删除原始/重放 deliveries；不得按 payload 文本模糊匹配。
2. 用 SQLite `json_each` 读取每个 Endpoint 订阅，过滤该事件后以稳定顺序 `json_group_array` 重建；无剩余事件写严格 `[]`。其他订阅、Endpoint 字段、delivery/attempt 必须逐字段保真。
3. 删除 request_log 新触发器/部分索引；检查跨 tenant 重复 request_id 后，原子重建 usage_ledger 与 request_logs 为 0033 全局 request_id/FK 合同，恢复全部旧列、索引和触发器；再删除 claims，最后删除 PRD-045 新增的 Virtual Key 复合候选索引。
4. 按 events -> heads -> items -> snapshots 的外键逆序删除快照对象；building/published 的保护触发器先按名称解除。
5. 重建 audit 为完整 0033 合同，只过滤两类 PRD-045 audit；shared 旧事件白名单属于代码回滚，DB 必须先完成上述数据清理，使旧进程不会读到无法解释的订阅或 delivery。

up/down/up 测试会同时插入 PRD-045 与非 PRD-045 Endpoint 订阅、原始/重放 delivery、attempt、request log/Usage；down 后只移除 PRD-045 事实，其余逐字段、索引、触发器、JSON 类型、foreign key 与 integrity 全部一致。

## 快照 canonical 与 hash 合同

快照 v1 canonical JSON 使用 UTF-8、`ensure_ascii=false`、对象 key 排序、紧凑分隔符、数组保持 evaluation order。两个 hash 分工如下：

- `content_hash` 用于判断“执行内容是否改变”。它绑定 `tenant_id`、content schema 和全部会影响排序、求值、动作或解释关联的字段，但不含 `source_policy_version`、snapshot version、actor、时间或原因。description-only PATCH 不制造数据面版本。
- `artifact_hash` 用于校验内部 wire。它覆盖 `wire_schema_version`、tenant、snapshot ID/version、operation、previous/rollback source、policy_count、content_hash 以及包含 `source_policy_version` 的完整 items。任何 tenant relabel、schema、provenance 或 envelope 篡改都会失败。

`content_hash` 输入为：

```json
{
  "content_schema_version": "policy-snapshot-content-v1",
  "tenant_id": "tenant_...",
  "policies": [
    {
      "evaluation_order": 0,
      "policy_id": "pol_...",
      "policy_rule_id": "prule_...",
      "policy_action_id": "pact_...",
      "priority": 100,
      "dsl_version": "policy-dsl-v1",
      "canonical_expression": "env == \"prod\"",
      "condition_hash": "...",
      "variables": ["env"],
      "action_type": "deny",
      "target_model_id": null
    }
  ]
}
```

`artifact_hash` 输入必须精确为下列对象；所有可空值保留 JSON `null`，整数禁止 bool，policies 与 content hash 使用同一 evaluation order：

```json
{
  "wire_schema_version": "policy-snapshot-wire-v1",
  "tenant_id": "tenant_...",
  "policy_snapshot_id": "psnap_...",
  "snapshot_version": 3,
  "operation": "rollback",
  "previous_snapshot_id": "psnap_v2",
  "previous_snapshot_version": 2,
  "rollback_source_snapshot_id": "psnap_v1",
  "rollback_source_snapshot_version": 1,
  "policy_count": 1,
  "content_hash": "...",
  "policies": [
    {
      "evaluation_order": 0,
      "policy_id": "pol_...",
      "policy_version": 2,
      "policy_rule_id": "prule_...",
      "policy_action_id": "pact_...",
      "priority": 100,
      "dsl_version": "policy-dsl-v1",
      "canonical_expression": "env == \"prod\"",
      "condition_hash": "...",
      "variables": ["env"],
      "action_type": "deny",
      "target_model_id": null
    }
  ]
}
```

两个 hash 均使用 `sha256(UTF-8 canonical JSON)` 的 64 位小写十六进制结果；不加隐式换行、BOM、浮点转换或平台相关序列化。Control 与 Gateway 必须调用同一 shared 函数，测试仍以独立固定向量防止双方同时漂移。

同 tenant 回滚复制的 content hash 必须等于来源；新 v3 的 artifact hash 因 snapshot version、operation 与 rollback source 不同而必然不同。Gateway 对每一项重新编译 canonical expression，要求 compiler 输出的 canonical expression、condition hash、variables 与 payload 完全一致，再分别重算 content/artifact hash。hash 不是租户授权凭证；内部路径参数、payload tenant、缓存 key 与已认证请求 tenant 还必须四者一致。

## API 设计

所有公共响应继续返回 body `request_id/trace_id` 与响应头 `X-Request-ID/X-Trace-ID`；所有错误走统一 ErrorSpec。公共快照接口均 admin-only、tenant 从 Bearer 登录态恢复、`Cache-Control: no-store`。

### `GET /policy-snapshots/current`

返回当前租户 head；从未发布时 `current_snapshot=null`，响应 `ETag: "0"`。已有 head 时 ETag 为强版本，例如 `"3"`。不返回 canonical expression 或 items。响应同时在只读事务中编译 active 候选，给出 `candidate_content_hash/candidate_policy_count/candidate_matches_current` 和强 `X-Policy-Candidate-ETag: "<content_hash>"`；回滚不修改 Policy 草稿，因此页面可明确提示下一次 publish 是否会重新上线回滚前候选。候选图损坏时只返回固定 `candidate_status=invalid` 且不发 candidate ETag，不泄露表达式。

### `GET /policy-snapshots?limit=20&offset=0`

返回当前租户历史，按 version DESC；limit 1..100、offset 0..10000，只接受 `limit/offset` query key。每项包含 ID、version、operation、content/artifact hash、count、reason、approval_status、previous_version、rollback_source_version、actor 安全标识和时间。跨租户 ID 不会出现在结果中。

### `GET /request-logs?snapshot_version=3`

在既有调用日志列表增加可选正整数 `snapshot_version` 过滤，并把它加入 query allowlist 与分页 DTO；非法、bool、0、超界值返回 `request_log.query_invalid`。读取权限保持既有 admin/developer，viewer 仍由后端拒绝；SQL 固定 `rl.tenant_id=? AND rl.snapshot_version=?`，不能只凭全局版本查询。列表与详情 DTO 返回 snapshot/status/decision/reason/Policy 安全摘要。

### `POST /policy-snapshots/publish`

请求：

```http
If-Match: "2"
If-Policy-Candidate-Match: "candidate_content_hash"
X-Request-ID: req_...
Content-Type: application/json

{"reason":"上线已验证的生产路由策略"}
```

- head `If-Match` 与 `If-Policy-Candidate-Match` 均强制必填；首次 head 使用 `"0"`。弱 ETag、多值、通配符、非法版本/hash 拒绝。Control 在同一 `BEGIN IMMEDIATE` 内重编候选并执行 head+candidate 双 CAS，防止管理员无感发布读取后被别人 PATCH 的策略集。
- tenant+request_id 已存在时，仅同 actor+同 fingerprint 回放原成功结果；不同 actor 或 fingerprint 一律返回通用幂等冲突且不返回原快照。幂等检查早于当前版本/候选冲突，确保“提交成功但响应丢失”的原 actor 重试可取回原结果。合法回放还必须从同一事务已提交的 event/audit 行重建两个安全事件并再次调用幂等 Webhook 派发器，关闭 COMMIT 后、首次 delivery intent 前进程退出导致的永久漏投窗口；稳定 event_id 保证已经创建 delivery 的 Endpoint 不会重复出网。
- `BEGIN IMMEDIATE` 内读取 head 与完整 active Policy 图，重新编译、验证动作/变量/route target，写 building/items，核对 count/hash，封印、事件、审计并推进 head。
- active 策略集允许为空，用于显式发布“无 Policy”的受控基线；与当前 content hash 完全相同则返回 no-changes，不制造空版本。失败/no-changes 不占用持久幂等键，策略真正变化后可用新的 request_id 重试。
- 成功返回 201、快照摘要与新 ETag；提交后投递 `policy_snapshot.published`。

### `POST /policy-snapshots/{id}/rollback`

请求 body 同样只含必填 `reason`，并要求当前 head 的 If-Match；rollback 复制历史制品，不读取 active 候选，因此不要求 candidate ETag。

- `{id}` 必须是当前 head 的 `previous_snapshot_id`；M1 只支持“上一版”，不支持任意历史挑选。跨租户或不存在统一 `policy_snapshot.not_found`，不泄露对象存在性。
- 在锁内验证 target published、内容/hash 完整、所有 route target 当前仍 active；然后复制为 `head+1` 的新 building 快照，`operation=rollback`、`rollback_source_snapshot_id=target`。
- 新快照 content hash 必须等于来源，artifact hash 必须因新 envelope 而不同；事件仍为稳定 `policy_snapshot.published`，payload.operation 为 rollback；审计 action 为 rolled_back。回滚不改写 active Policy 候选，确认文案和 current API 必须暴露候选/线上漂移，避免下一次 publish 意外恢复 B。
- 成功返回 201、新 ETag 与 rollback_source_version。

### `GET /internal/policy-snapshots/{tenant_id}/current`

- 仅接受 `X-Internal-Token`，无论环境都必须配置并常量时间比较；不接受 Bearer、query 或浏览器凭据。路径 tenant 只来自 Gateway 已认证 Virtual Key 的 tenant/cache key；Control 查询仍以该 tenant 限定并让响应 tenant 与路径一致。
- Control 先要求路径 tenant 存在且 status=active；未知/disabled 返回固定 `policy_snapshot.tenant_unavailable`，绝不能把它们伪装为“无策略”。active tenant 从未发布时才返回 200 `{tenant_id, snapshot: null, ...request ids}`，使“明确无 head”与网络失败可区分。
- per-tenant 强 ETag 绑定 `wire_schema_version + tenant_id + snapshot_version + artifact_hash`；无 head 也有绑定 schema/tenant 的稳定 ETag。匹配时 304 只刷新该 tenant 新鲜度，不会遮蔽其他 tenant 的损坏或重试。
- 设置单 tenant 响应体大小硬上限；超过上限返回统一 503，不能发送截断 JSON。内部 wire 必须携带 canonical expression 供 Gateway 安全重编，但访问日志、结构化日志、异常、审计和 Webhook 均不得记录 token、canonical expression 或响应 payload。
- 不提供全租户 DSL 聚合响应，避免一个超大/损坏 tenant 阻断其他 tenant、一次 token 泄漏放大全量策略暴露，以及任一变更触发所有 tenant 重编译。
- Gateway 使用 `trust_env=false`、`follow_redirects=false`，禁止 URL credentials/query/fragment。prod Control base URL 必须是通过系统证书校验的 HTTPS；dev/test 仅额外允许 loopback HTTP。3xx、跨主机与降级重定向全部按同步失败处理，避免内部 token/DSL 被转发。

## 发布与回滚事务

### 发布

1. 规范化 reason、head/candidate If-Match、request_id/trace_id，连同 actor ID 计算不含敏感数据的 fingerprint。
2. `BEGIN IMMEDIATE` 后先按 tenant+request_id 查幂等记录；同 actor+同 fingerprint 返回既有，其他情况统一冲突且不回放。
3. 读取当前 head：与客户端 head ETag 不同返回 `version_conflict`。随后读取同 tenant 全部 active Policy/Rule/Action；图不完整/腐损返回 `graph_invalid`，成功重算的 content hash 与客户端 candidate ETag 不同才返回 `candidate_conflict`。`BEGIN IMMEDIATE` 已阻止锁内后续 PATCH，不设计“锁内又发生变化”的不可达分支。
4. 重新编译 `condition_expr`；要求持久化 DSL version/hash/variables 与编译结果一致，拒绝 mask、data_class 与失效 route target。
5. 以 `priority, policy_id` 排序并生成 canonical items；插入 building header/items。
6. 重算 count/content/artifact hash 后唯一一次封印 published；写持久事件与 audit；首次 INSERT 或精确 +1 更新 head。
7. 使用既有 `commit_audited_transaction` 提交。故障注入覆盖 header/items/seal/event/audit/head/commit 每一步。
8. COMMIT 后使用持久 event_id 投递 `audit.recorded` 与 `policy_snapshot.published`；投递失败只形成 delivery 失败事实，不回滚发布。
9. 同 actor+同 fingerprint 的幂等回放必须先按 tenant、snapshot、request_id 回读并严格核对已提交 event/audit 安全事实，再重试两个稳定 event_id 的派发；事实缺失、重复或与 snapshot 不一致统一返回 `policy_snapshot.unavailable`，不得根据不可信持久数据构造 Webhook。

### 回滚

回滚复用相同幂等/CAS/原子边界，但来源只允许当前 previous。逐字段复制来源 items、重新编译并核对 hash、重新验证 route target，生成新单调版本；任何腐损或依赖失效保持旧 head 不变。

## Gateway 缓存、LKG 与保护模式

### 配置

| 配置 | 默认 | 边界 |
| --- | --- | --- |
| `METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED` | 内置 `false`，部署示例 `true` | dev/test 可显式关闭；prod/public_service 不允许关闭。 |
| `METAHUB_POLICY_SNAPSHOT_POLL_INTERVAL_SECONDS` | 2 | 1..60。 |
| `METAHUB_POLICY_SNAPSHOT_HTTP_TIMEOUT_SECONDS` | 2 | 1..10，且小于轮询间隔允许但不要求。 |
| `METAHUB_POLICY_SNAPSHOT_MAX_AGE_SECONDS` | 300 | 5..86400，且不得小于轮询间隔。 |
| `METAHUB_POLICY_SNAPSHOT_EMERGENCY_MAX_AGE_SECONDS` | 3600 | 60..604800，且不得小于普通 max-age；只适用于 internal_only。 |
| `METAHUB_POLICY_SNAPSHOT_MAX_PAYLOAD_BYTES` | 4194304 | 1024..16777216。 |

内置默认关闭，避免没有内部 token/Control 服务的新克隆和既有单元测试启动即产生后台网络副作用；`.env.example` 与部署文档会同时设置 `METAHUB_POLICY_SNAPSHOT_SYNC_ENABLED=true` 和非占位 `METAHUB_INTERNAL_CONFIG_TOKEN`。任一服务启用同步/内部快照接口时 token 必填；prod/public_service 还必须启用同步，否则启动校验拒绝。PRD-045 真实运行验收明确以同一高熵本地 token 启动 Control/Gateway，并验证 token 不出现在进程日志或配置摘要。

### 同步状态机

1. lifespan 只校验配置并启动后台 scheduler，不做无法确定 tenant 的伪同步。已认证请求首次遇到某 tenant 时注册 cache key，并创建 keyed singleflight refresh task；所有 HTTP waiter 与 scheduler 复用同一 task，不重复打 Control。
2. waiter 使用 `asyncio.shield` 等待 shared task；一个客户端取消不能取消其他 waiter 或 scheduler 的刷新。task 自身有 HTTP timeout，完成后在锁内移除；shutdown 先停止调度，再有界等待所有 refresh task，超时才统一取消并回收。
3. scheduler 只轮询已注册 tenant，使用有界并发和每 tenant 独立 If-None-Match；Control 故障或一个 tenant 超大/损坏不会阻止其他 tenant 刷新。
4. 200 响应先检查 Content-Length/实际字节、wire/envelope/content schema、路径/payload/cache tenant 四者一致、items、重新编译、count、content/artifact hash，再更新该 tenant 缓存。
5. 新版本只能大于旧版本；相同 version 必须 content/artifact hash 都相同。低版本、同版异 hash 或损坏 payload 均保留该 tenant LKG，且绝不保存失败响应 ETag。
6. snapshot/no-head marker、ETag、last-success monotonic timestamp、last-error 与 generation 在同一锁内原子更新；304 只更新该 tenant 单调新鲜时间。wall clock 只用于展示，不参与 max-age。wire schema 已进入 ETag，服务升级不会因业务 head 未变而永久 304。
7. 请求在 Virtual Key/Environment 校验后、读取 JSON 前调用 cache acquire，始终得到不可变 `PolicySnapshotLeaseDecision`，而不是在阻断状态直接抛异常。decision 含 executable、snapshot 引用、version、status、固定 error_code 与 monotonic 取得时刻：冷启动不可用为 version=NULL/status=unavailable；有真实快照的 LKG 超期保留其 version/status=expired；明确无-head 的空基线在同步故障后 version 继续为 NULL，但 status 依次为 lkg/emergency_lkg/expired，不能退化成“从未同步”。
8. Gateway 先用 decision 写 claim 和带 version/status 的最小 RequestLogContext；若 executable=false，再写失败 request_log 后抛固定 503。可执行 lease 后续参数异常、路由、Fallback、流终态都只传该值，不再查询 cache。这样 unavailable/stale/expired 同样满足失败可追踪，而不是被 claim 触发器拒绝。
9. 同步异常日志只记录固定 event/error_code、request_id/trace_id 和安全计数，不记录 token、URL、响应体、表达式或异常链文本。

### 可达性与保护决策

| 状态 | internal_only | public_service |
| --- | --- | --- |
| 已成功同步，租户无 head | 明确空基线，继续全部既有 Guard | 同左 |
| 有快照且新鲜 | 执行 current | 执行 current |
| 最近同步失败但 LKG 未超期 | 执行 lkg | 执行 lkg |
| LKG 超过普通 max age、未超过 emergency max age | executable emergency_lkg，仍保留 DLP/限流/预算/鉴权，并输出去重告警 | blocked expired，先 claim/LKG version 失败日志，再 `gateway.policy_snapshot_stale` |
| LKG 超过 emergency max age | blocked expired，先 claim/LKG version 失败日志，再 `gateway.policy_snapshot_expired` | 同左（通常已在普通 max age 阻断） |
| 冷启动从未成功同步且无 LKG | blocked unavailable，先 claim version=NULL 失败日志，再 `gateway.policy_snapshot_unavailable` | 同左 |
| sync 显式关闭 | 仅 dev/test internal 可作为 disabled 基线 | 启动配置拒绝 |

“无 head”必须来自该 tenant 至少一次成功的 200/304 同步；不能把网络失败时的空缓存误判为没有策略。emergency LKG 有硬上限，不允许只改状态标签后无限放行。

## Gateway 热路径与执行语义

顺序固定为：

```text
Virtual Key 鉴权 -> Environment 状态 -> 固定 snapshot lease -> request_id claim
-> JSON/模型字段校验 -> 无副作用校验源模型归属/active 权威性
-> 构造可信 PolicyContext -> Policy 纯求值
-> deny/异常 fail-closed；allow 再准备完整原始路由计划，route_to 解析目标模型
-> 只写一个最终 routing decision
-> 能力校验 -> DLP -> Rate Limit -> Budget reservation
-> Provider/受控 Fallback -> Usage/RequestLog
```

### 可信 PolicyContext

- `tenant`：认证 `tenant_id`。
- `env`：Virtual Key 绑定的 Environment code。
- `project`：认证 Key 绑定的 `project_id`；没有绑定时严格使用 PRD-044 已固定的空字符串 `""`，不得改成缺失变量。
- `model`：客户端已校验的请求模型 alias/ID 字符串，不接受另一个 Header 覆盖。
- `data_class`：本 PRD 不提供；发布已阻止引用它的 active 策略。
- `budget_remaining`：只有快照声明需要时才读取；复用 Budget Guard 的 `Asia/Shanghai` 业务日、6 位 Decimal、Usage 与 outstanding reservation 聚合。当前 tenant/environment 下全部适用 active tenant/project/application/virtual_key 规则（无论 block/alert_only）分别计算 `max(limit - usage - reservation, 0)` 并取最小值；同 scope 多规则也按最小值收敛。没有规则则缺失；只读函数不创建 evaluation/alert/block/reservation，数据库异常返回基础设施 503，最终 Budget Guard 仍是并发准入权威。

### 源模型权威预校验

Policy 不能让一个 disabled、不存在或不属于当前 Environment 的 requested model 通过宽泛 `route_to` 绕过既有语义。Policy 前的 `validate_requested_model_authority` 只检查 tenant/environment 下 alias 或 direct model 是否存在且自身 active，不检查 Provider 状态/健康、不选择模型、不构建 Fallback，也不写 `routing_decisions`。因此显式 deny 不会被无关 Provider 故障抢先覆盖，route_to 可以从有效但上游异常的源模型路由到健康目标。Policy allow 后才执行现有完整 route/provider/health/fallback 计划；route_to 只解析具体 target。两条成功路径最后都通过单独入口只写一个最终 routing decision，明确关联 snapshot/policy reason。

### 决策

- 无快照或空快照：`allow / policy.no_snapshot_default_allow`，但继续 DLP、限流与预算。
- 无规则命中：`allow / policy.no_match_default_allow`，继续全部 Guard。
- `allow`：停止后续 Policy 规则，但不跳过现有 Guard。
- `log_only`：保存安全命中摘要并继续求值；最终日志只保存决定性 Policy ID 与稳定 reason，不保存 comparison 值。
- `deny`：Provider 前返回 403 `gateway.policy_denied` 并写失败 request_log。
- timeout：按 PRD-044 的策略阻断语义在 Provider 前返回不可重试 403 `gateway.policy_evaluation_timeout`，写 `policy.evaluation_timeout` reason 并输出安全告警。
- context missing：返回不可重试 403 `gateway.policy_context_missing`；缓存运行对象损坏不进入求值，刷新失败保留 LKG。预算只读上下文的数据库故障单独返回 503 `gateway.policy_context_unavailable`。
- `route_to`：按 target model ID 重新解析单候选路线，必须同 tenant、active，Provider active 且健康，并支持 endpoint/stream 能力；不继承原 alias 的 runtime Fallback。失败返回 `gateway.policy_route_unavailable`。
- `mask` 或未知动作：防御性不可重试 403 `gateway.policy_action_unsupported`，绝不放行。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `policy_snapshot.payload_invalid` | 422 | reason/query/header 结构或长度非法。 |
| `policy_snapshot.if_match_required` | 428 | 发布/回滚缺强 If-Match。 |
| `policy_snapshot.version_conflict` | 409 | head 已变化。 |
| `policy_snapshot.candidate_conflict` | 409 | 客户端 candidate ETag 与锁内权威重编 hash 不同。 |
| `policy_snapshot.idempotency_conflict` | 409 | 同 request_id 不同 fingerprint。 |
| `policy_snapshot.not_found` | 404 | 当前 tenant 不存在目标；跨租户同码。 |
| `policy_snapshot.tenant_unavailable` | 404 | 内部路径 tenant 不存在或 disabled。 |
| `policy_snapshot.rollback_target_invalid` | 409 | 目标不是当前上一版。 |
| `policy_snapshot.no_changes` | 409 | publish 内容与当前一致。 |
| `policy_snapshot.action_unsupported` | 422 | active mask 无真实执行器。 |
| `policy_snapshot.variable_unsupported` | 422 | active 策略引用不可信 data_class。 |
| `policy_snapshot.route_target_invalid` | 422 | route target 不属于 tenant 或非 active。 |
| `policy_snapshot.graph_invalid` | 503 | Policy/Rule/Action 或持久编译事实腐损。 |
| `policy_snapshot.unavailable` | 503 | DB、审计、事件、hash 或内部响应生成失败。 |
| `auth.internal_token_required/invalid` | 401/403 | 内部拉取身份缺失或错误。 |
| `gateway.policy_snapshot_unavailable` | 503 | 冷启动无可信同步/LKG。 |
| `gateway.policy_snapshot_stale` | 503 | public_service LKG 超期。 |
| `gateway.policy_snapshot_expired` | 503 | internal emergency LKG 也超过硬上限。 |
| `gateway.request_id_reused` | 409 | request_id 已被历史或并发 HTTP 请求 claim；必须换新 ID。 |
| `gateway.policy_denied` | 403 | 显式 deny。 |
| `gateway.policy_evaluation_timeout` | 403 | 全策略集超时按策略拒绝，非可重试 Control 故障。 |
| `gateway.policy_context_missing` | 403 | 策略实际访问了没有语义值的可信变量。 |
| `gateway.policy_context_unavailable` | 503 | budget 只读上下文基础设施不可用。 |
| `gateway.policy_route_unavailable` | 503 | route_to 目标运行时不可用。 |
| `gateway.policy_action_unsupported` | 403 | 防御性未知/mask 动作。 |

所有错误只返回固定 message/suggestion/request_id/trace_id；不回显 DSL、canonical expression、上下文值、内部 token、数据库路径或原始异常。

## 安全与权限约束

- 公共历史/发布/回滚统一 `require_policy_admin`；前端入口隐藏不能代替 RBAC。
- 公共 API 不接受 tenant、actor、server version、approval_status 或 Policy items；唯一可回传的 hash 是 current 响应给出的 opaque `If-Policy-Candidate-Match`，后端仍会重编并比较，不能把它当权威输入。
- 内部 API 与公共 API 使用不同认证边界。Token 用 `secrets.compare_digest`，缺配置也返回固定错误，不写明文日志；生产只走可信 HTTPS，客户端禁代理环境与重定向。
- SQL 的每次业务读写都带 tenant，复合 FK/触发器校验 parent、actor、route target、head 和 request_log snapshot 归属。
- 幂等键在 tenant 内全局唯一；仅同 actor+同 fingerprint 才回放，其他 actor 或参数复用同键只返回冲突，响应丢失重试不会多发布，也不能跨管理员回读结果。
- canonical expression 仅存在策略表、快照项和内部服务响应；公开历史、审计、Webhook、结构化日志和 request_logs 都不包含它。
- Webhook 签名与既有 endpoint secret 机制复用；事件使用持久 `psevt_*` 派生稳定 event_id，payload 不含 Policy name/description、DSL、AST、Prompt、Response、change reason 或 Key。0034 down 会先删除该事件类型的 delivery/attempt 并从 Endpoint 订阅 JSON 中移除它，再恢复旧应用可解释合同。
- 缓存大小、策略数、字符串、JSON、轮询并发和网络超时有硬上限；损坏响应不能部分覆盖同 tenant 的 LKG。
- Control CORS 仅额外允许 `If-Policy-Candidate-Match` 并暴露 `X-Policy-Candidate-ETag`；Gateway 不暴露这些管理头。每个已认证 HTTP 请求先 claim request_id，客户端复用不能造成跨版本日志被静默覆盖。
- `change_reason` 是 admin-only 租户机密元数据，只进入快照表和 admin 历史响应；审计仅保存 reason hash，Webhook/结构化日志/内部 wire 不携带正文。页面 DOM 所有服务端文本转义，reason 不使用 `innerHTML` 拼接；离页/换 tenant/退出时 abort 请求并清理内存状态。

## 前端页面和交互设计

### 策略发布页

- 页面首屏是紧凑的当前版本带状摘要，不使用嵌套卡片：版本、状态、策略数、内容 hash 前 12 位、发布时间、最近操作和“候选与线上是否一致”。
- 发布表单使用多行“变更原因”和带发布图标的明确命令按钮；未加载 ETag、正在请求、reason 为空或角色非 admin 时禁用。
- 点击发布先显示确认对话，明确“active 策略将在下一个轮询周期影响新请求”；不声称全实例已 ACK。
- 409 version conflict/no changes 保留用户 reason 并刷新当前版本；网络结果不确定时保留相同 request_id，允许原操作重试。

### 快照历史

- 版本按 DESC 表格展示；移动端转换为逐行 label/value，不产生根节点横向滚动。
- 当前行有“当前”状态；仅 previous 行显示回滚按钮。回滚展开行内 reason 表单并二次确认，明确回滚只改线上快照、不改 active Policy 候选；候选不一致时下一次 publish 会重新上线候选内容。
- 显示 `v3 <- 回滚 v1`，使“新版本使用旧内容”可解释；hash 只显示摘要，完整值可在可复制 code 中查看但不进入 URL。
- 空历史是明确“尚未发布”；loading/error/permission 状态不伪造版本。错误展示 request_id。

### 调用日志

- 列表新增紧凑版本列；详情新增 Policy 快照小节，展示 version、current/LKG 状态、decision、reason、Policy ID、action 与 route target ID。
- NULL version 显示“未发布基线”或 unavailable 状态，不能显示 v0 冒充真实快照。

## 测试用例

### Shared 单元测试

1. content/artifact canonical hash 与 Unicode/Decimal/数组顺序确定性；tenant relabel、wire schema、snapshot envelope 或 provenance 篡改必须失败。
2. wire schema 拒绝未知字段、路径/payload tenant 不同、重复 order、bool 伪装 int、超长字符串、未知 schema/action/variable。
3. Gateway 重编 canonical expression 并核对 expression/hash/variables/content/artifact hash；篡改任一字段失败。
4. AppConfig 的默认、上下界、emergency >= max_age >= poll、prod/public_service 禁止关闭、prod HTTPS/dev loopback HTTP、敏感 token exclude。

### Control API 集成测试

1. 首次空策略集发布 v1；后续策略变更发布 v2；历史/current/ETag 正确。
2. v1=A、v2=B，回滚 v1 生成 v3=A；v3 hash 等于 v1、source=1、head=3。
3. developer/viewer 全部 GET/POST 被后端拒绝；跨租户 rollback 404；公共 body/query 的 tenant/未知字段拒绝。
4. head/candidate If-Match 缺失/弱值/旧值；用两个真实 SQLite 连接覆盖 Policy PATCH-vs-publish、publish-vs-publish、publish-vs-rollback，验证幂等回放最先、head conflict/graph invalid/candidate conflict 分支可达且语义互斥；current/history 未知 query 拒绝。
5. tenant-wide request_id 下，同 actor+同 fingerprint 幂等；不同 reason/target/CAS 或另一 admin 同 request_id 只返回冲突且不能回读；幂等检查早于版本/候选检查。
6. active mask、data_class、失效/cross-tenant route target、损坏 Policy graph/hash/DSL 全部拒绝且旧 head 不变。
7. header/items/seal/event/audit/head/commit 故障注入均全回滚；commit 后 Webhook 失败仍只产生一版；模拟 COMMIT 后、delivery intent 前退出后，用同一 request_id 回放 publish/rollback，必须补建 audit/snapshot delivery 且稳定 event_id 不重复发送既有 delivery。
8. per-tenant 内部 token 缺失/错误/常量时间路径、active/disabled/unknown tenant、未知 query、200 null/200 snapshot/304、schema-bound ETag；响应不含原始 condition_expr/name/description/secret，但按内部合同包含 canonical expression。
9. Webhook filter/签名/delivery 只含安全 payload；审计前后摘要完整且 tenant/request_id 可追踪。
10. description-only PATCH 后 publish 返回 no-changes；rollback 后 current 明确 candidate hash 漂移，下一次 publish 恢复候选内容。

### 数据库迁移测试

1. 跨租户 snapshot/actor/previous/source/target、head 与 request_log version 全部由 DB 拒绝。
2. building 外插 item、二次 seal、published header/items/event UPDATE/DELETE、head 跳版/降版拒绝。
3. 历史 request_log claim 回填；Virtual Key 复合候选 FK 生效；同 tenant 重复 claim、claim UPDATE/DELETE 拒绝，不同 tenant 同 request_id 隔离成功；request_log 同值 UPSERT 允许，version/status/trace 改写、未知/未发布/跨 tenant version 和 claim 不匹配拒绝。
4. `0033 -> 0034 -> down -> up`：旧 audit 17 字段、全部既有 action/object、5 索引/3 触发器，request_logs 43 列/15 索引/4 触发器和 usage_ledger 32 列/全部索引触发器逐字段保真；down 过滤两类新 audit 与快照 Webhook，恢复 0033 合同，并覆盖跨 tenant 重复 request_id 时安全中止 down。
5. 每阶段 `PRAGMA integrity_check=ok`、`foreign_key_check=0`，schema_migrations 状态正确。

### Gateway 单元/集成测试

1. 未知 tenant singleflight 首次 200、If-None-Match 304、单 tenant 大小/hash/JSON/schema/count 校验、tenant relabel、低版本与同版异 hash 拒绝；失败 ETag 不保存，修复后同版可重试成功；prod HTTP/redirect/proxy-env 与 dev 非 loopback HTTP 拒绝。
2. 每 tenant 原子 swap 与独立 ETag；一个损坏/超大 tenant 不覆盖其 LKG，也不阻止其他有效 tenant 更新或刷新新鲜度。首 waiter 取消不取消 shared refresh，scheduler/HTTP waiter 复用 singleflight，timeout/shutdown 有界；wall clock 回拨不改变 monotonic max-age。
3. 冷启动无成功同步 fail closed；成功同步无 head 空基线及其后续 NULL-version lkg/emergency/expired；真实快照的 current、未超期 LKG、internal emergency LKG、hard expiry、public stale block。每种阻断都先生成 LeaseDecision、claim 和带正确 NULL/真实 version/status 的失败 request_log。
4. 请求在 JSON 前持有 v1 并 claim；畸形 JSON/模型字段也记录 v1。处理中缓存切到 v2，非流式、Fallback、流式 completed/timeout/client_disconnect 的日志仍固定 v1；下一请求用新 request_id 使用 v2。
5. deny/timeout/context missing 在 Provider 前以约定 403 拒绝并写失败日志；timeout 非 503，Provider adapter 调用次数为 0。
6. allow/no-match/log_only 不绕过 DLP、Rate Limit、Budget；现有 Guard 仍按原错误码工作。
7. side-effect-free authority check 在 Policy 前只拒绝 disabled/not-found/wrong-environment requested model；源 Provider unhealthy 时 deny 仍按策略拒绝、route_to 仍可转向健康目标，allow 才进入完整 Provider/健康/Fallback 解析。deny 不写伪路由，allow/route_to 只产生一个最终 routing decision；非法 target 失败关闭且不继承原 alias Fallback。
8. project 无绑定固定空字符串；budget_remaining 使用上海业务日、6 位 Decimal、全部适用 active block/alert_only 规则最小余额、无规则 missing、usage+reservation、DB 故障且无副作用；最终硬 Budget Guard 仍不可绕过。
9. 同 tenant 并发及 v1->v2 后复用 X-Request-ID 均在 Provider 前 409，第一次日志/claim 版本不变；不同 tenant 同 ID 可各自 INSERT/UPSERT 并生成各自 Usage/Debug Console 记录，不串租户。专项扫描禁止运行代码残留 `ON CONFLICT(request_id)` 或无 tenant 的 request_log/Usage 关联；日志 API 按 tenant+version 过滤，非法值拒绝，所有终态保存安全策略摘要。
10. 同步日志和 request_logs 敏感扫描：无 token、Authorization、Prompt、Response、canonical expression、Provider Key。
11. Control Debug Console 在 Provider 前 claim version=NULL/status=disabled；成功、参数/上游失败、Usage 与 request log 回归均通过，重复 request_id 同样在网络前拒绝。

### Console 单元与浏览器验收

1. admin 加载 current/history、候选漂移、发布、冲突刷新、同 request_id 重试、上一版回滚及“下一次发布恢复候选”警示；developer/viewer 无 DOM 且请求数为 0。
2. HTML 转义 reason/ID/hash/error；离页/换身份/旧响应竞态不覆盖新页面。
3. request log version/status/decision/reason 正确展示，NULL 不伪装 v0。
4. 1280x720、900x800、390x844：按钮/文本不截断，表格转换无横向根溢出，确认/错误/展开表单可操作。
5. 真实浏览器从运行库现有基线 vN 发布 vN+1，再回滚基线内容生成 vN+2；从调用日志筛选和详情核对实际版本，并验证 developer/viewer 前后端权限及三档布局。
6. v1/v2/v3、Gateway 轮询/请求、内部 Token、Webhook、Usage 与故障响应由独立真实三服务运行脚本覆盖，不能把该层证据误记为浏览器动作。

### 回归与运行验收

- 运行 Policy/快照/Gateway/RequestLog/Migration 专项，再运行 Control API、Gateway、Python 示例、Node 示例、Console 全量测试和 lint。
- 运行 `scripts/ci/check-comments.py`、局部 Ruff、compileall 与验收脚本语法检查。
- 备份运行库后应用 0034，核对迁移数、最高版本、integrity/foreign key、快照/head/event/audit/request_logs 实际行。
- 重启 Gateway、Control API、Console；三项健康检查 HTTP 200。
- 执行真实 API、内部拉取 200/304、Webhook、日志敏感扫描和三档浏览器验收；证据写入 `artifacts/prd045/` 与 `docs/testing/PRD-045-tests.md`。

## 代码修改计划

1. 完成两轮设计自审并修订本文；自审门禁未通过前不改代码。
2. 新增 shared snapshot canonical/wire 模块、配置、错误与 Webhook 合同及单元测试。
3. 编写 0034 up/down 与严格迁移往返测试，先证明数据/触发器边界。
4. 实现 Control models/routes/service、内部拉取、事务/幂等/CAS/审计/Webhook 与集成测试。
5. 实现 Gateway cache/lifespan/LKG、PolicyContext/求值/route_to 与热路径日志，补全请求类型测试。
6. 扩展 RequestLog Control DTO/查询和 Console 调用日志展示。
7. 实现 admin-only 策略发布/历史页面与前端竞态、权限、响应式测试。
8. 更新 `.env.example`、README、目录图和测试记录，执行专项、全量、迁移与真实运行验收。

## 第一轮设计自审

状态：通过。由独立 Agent 完整核对 Excel、架构 5.3/5.4/A.3、PRD-044 合同、安全、RBAC、tenant、敏感数据与热路径；经历两次“不通过”修订后最终复审通过。

第一轮发现并修订：

1. 全租户聚合 ETag 会让单租户损坏后被 304 永久遮蔽，也放大体积与泄漏半径；改为按已认证 tenant singleflight 首拉、per-tenant ETag 和后台轮询，失败 ETag 不保存。
2. 内部 wire 必须包含 canonical expression 才能安全重编；修正为“wire 可含、任何日志/事件/审计不得记录”，并增加 prod HTTPS、dev loopback HTTP、禁代理环境/重定向。
3. 原 content hash 未绑定 tenant 且混入纯 provenance；改为 tenant-bound execution `content_hash` 与完整 wire `artifact_hash` 双合同，补精确 canonical JSON、tenant relabel、schema 与 description-only no-change。
4. 恢复 PRD-044 已固定的 `project=""` 兼容语义；timeout/context missing 按不可重试策略拒绝，不再伪装成 Control 503。
5. 增加普通 max-age 与 emergency hard max-age 两级保护；internal_only 不再无限 stale 放行。
6. 发布增加 head+candidate 双 CAS，阻止并发 Policy PATCH 被无感发布；幂等统一 tenant-wide request_id，仅同 actor+fingerprint 回放。
7. lease 和 request claim 前移到 JSON 校验前，使参数异常也固定版本；新增不可变 claim 阻止客户端复用 request_id 跨版本改写日志。
8. claim 同时接入既有 Debug Console，固定 version=NULL/status=disabled，避免 0034 触发器破坏其 request_log/Usage。
9. Policy 前只做 source model tenant/environment/active 权威校验；Provider 健康/Fallback 延后到 allow，deny/route_to 不被无关源 Provider 故障抢先覆盖。
10. 补齐 `GET /request-logs?snapshot_version=`、回滚后候选漂移、跨管理员冲突、最终 routing decision 和 down migration Webhook 清理合同。

## 第二轮设计自审

状态：通过。由独立 Agent 聚焦 SQLite schema/trigger、up/down/up、事务/CAS、claim、缓存并发与运行验收；两次“不通过”修订后最终复审确认无残余阻断。

第二轮发现并修订：

1. claim 与 request_logs 只校验 version 不足；固定 tenant/trace/version/status 的 NULL-safe 合同，历史日志统一回填 disabled，所有写入方先 claim。
2. cold unavailable 与 LKG expired 原先会在 claim 前抛错；改为不可变 LeaseDecision，阻断也先 claim、写失败 request_log，再抛统一错误。
3. 补齐 snapshots 的 tenant+ID/version 候选唯一键、全部复合 FK、building 唯一 seal 白名单、连续 order 与受控 building 清理；明确 hash 由 shared 服务重算而非 SQLite。
4. 把 request_logs 主键与 Usage FK 升级为 `(tenant_id, request_id)`，消除跨租户 ID 抢占；全部 UPSERT/SELECT/JOIN 强制 tenant，并定义 down 冲突安全中止。
5. singleflight 使用 shield、keyed shared task、monotonic freshness 与锁内 snapshot/ETag/time 原子更新；补取消、timeout、shutdown 和时钟回拨测试。
6. 消除“启动立即同步”矛盾：lifespan 只启动 scheduler，首个认证 tenant 同步；内置 dev 默认关闭，部署/验收显式配置 sync+高熵 internal token，prod 强制启用与 HTTPS。
7. 明确 down 先结构化删除 PRD-045 Webhook attempt/delivery、JSON 移除订阅，再重建 request_logs/Usage、删除 claims/快照、恢复 audit；非 PRD-045 事实逐字段保真。
8. 收敛 CAS：幂等回放最先，head mismatch、graph corruption、candidate mismatch 三类错误互斥，并用两个真实连接覆盖竞争。
9. 空策略基线在后续同步故障时允许 NULL-version lkg/emergency/expired；不能与 cold unavailable 混淆。
10. 为 claim 的 Virtual Key 复合 FK新增候选索引，并明确 `ON CONFLICT(tenant_id, request_id)`、跨 tenant 同 ID RequestLog/Usage/Debug Console 回归。
11. 最终交叉审查发现幂等命中曾直接返回，无法恢复 COMMIT 后、Webhook 调用前的进程退出窗口；修订为从同事务 event/audit 行重建并核对安全事实后重派两个稳定 event_id，同时增加 publish 与 rollback 崩溃恢复集成测试。

## 实现后封账复审

状态：通过。2026-07-18 依据最终工作区和运行库重新核对设计边界、tenant/RBAC、统一错误/request_id、敏感信息、迁移可逆性和实际页面，不以实现意图替代运行证据。

封账结论：

1. Python 根级 `434 passed`、Node 示例 `33 passed`、Console `194 passed`、Console lint `100 files passed`、迁移 `21 passed`、快照/缓存 `22 passed`，中文注释与静态门禁通过。
2. 运行库已备份并应用 0034；34 条迁移、最高 0034、`integrity_check=ok`、外键违规 0。当前运行验收脚本 SHA-256 `8DDBF560662C18012672FF0C081BAFBC397B07187DF4F68497CE60EEC6B49CE6` 已由批次 `20260718T121929` 实际运行通过，完成 v1/v2/v3、cleanup 恢复 v4、Gateway、内部身份、RBAC、Webhook、Usage 和敏感扫描。
3. 浏览器批次 `20260718T111530` 完成 v9 发布、回滚 v8 生成 v10、调用日志 v2 关联检查、8 个后端权限拒绝和 1280/900/390 三档截图；临时资源全部失活。
4. no-head、route_to、LKG/public stale/emergency/hard expiry 等故障注入由自动化证明，真实 API 与浏览器只声明其实际执行范围；详细证据见 `docs/testing/PRD-045-tests.md`。
