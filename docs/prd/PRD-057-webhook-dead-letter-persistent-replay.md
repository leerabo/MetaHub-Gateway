# PRD-057 - Webhook 死信与持久重放

状态：已完成。设计与两轮编码前自审、0047/0048、后端 API/worker、PAGE-083、专项与回归、
真实 Runtime、Browser 双视口、停服静止终态及正式 artifact 均已通过。下一项为 PRD-058；
`all_108_prds_complete=false`。

## 1. 需求理解

### 1.1 来源、顺序与验收事实

- Excel `01_PRD总表` 与 `02_M0-M1核心开发` 的第 58 行均定义：
  `PRD-057 | M1-企业POC | P1 | 核心层 | Webhook可靠性 | Webhook死信与持久重放`。
- 直接前置是已完成的 PRD-025“基础事件订阅与推送”和 PRD-042“Webhook 事件过滤与重放基础”。
  PRD-042 已明确把 replay intent 提交后进程崩溃接管、`dead_letter`、归档和过期策略留给本 PRD。
- PAGE-083 是“Webhook死信页”。
- Excel 指定公开动作是 `POST /webhook-deliveries/{id}/replay`；这里的 `{id}` 按资源路径解释为
  `webhook_delivery_id`，不能继续沿用旧接口中 `{id}=webhook_endpoint_id` 的语义。
- 数据对象是 `WebhookDelivery`、`DeadLetterEvent`，稳定内部事件是 `webhook.dead_lettered`。
- TC-0113 要求接收方故障时事件不丢、恢复后可重放、重复投递携带相同 `event_id` 供接收方幂等。
- TC-0114 要求异常、权限和安全路径使用统一错误，不泄露敏感信息，状态可追踪。
- 不实现第三方 SIEM 格式全集，不新增 MCP、硬件、信创或复杂数据出境链路。

### 1.2 当前实现与真实缺口

现有实现已经具备：

1. `webhook_deliveries` 和 `webhook_delivery_attempts` 在 HTTP 前持久化 intent 与发送摘要。
2. 原始事件按 `tenant_id + endpoint + event_id` 唯一，手工 replay 按来源 delivery 与幂等键唯一。
3. PRD-053/054/055/056 的部分源 Outbox worker 可以续发 original pending delivery。
4. 现有 replay 会先提交 replay delivery 和管理审计，再同步出网。
5. Endpoint Secret 轮换、递归敏感键拒绝、HMAC 和不读取接收端正文已经落地。

仍需补齐：

1. replay intent 提交后、HTTP 前进程退出时，现有接口只回读 pending，不会接管发送。
2. 最终失败只有 `webhook_deliveries.status=failed`，没有独立、可筛选和可治理的死信对象。
3. 没有归档、过期、恢复 lease、全租户死信列表和 PAGE-083。
4. Excel 指定的顶层 delivery replay 路径尚不存在。
5. 现有前端 loader 会在其他导航页预取 Webhook，身份快照也未完整包含角色与 Session revision，
   不适合作为长期死信页面的生命周期边界。

### 1.3 本 PRD 对“先持久化”的定义

- 安全、账单及其他治理业务事实仍由各自既有业务表或源 Outbox 先落库。
- 对已经进入 Webhook 投递链路且命中订阅 Endpoint 的事件，必须在任何出站 HTTP 前持久化
  `WebhookDelivery` intent；最终失败与 `DeadLetterEvent` 必须在同一数据库事务形成。
- 本 PRD 不新造第二套通用业务事件总线，也不把 Gateway 模型调用改成同步等待外部 Webhook。
- 接收方故障属于 Webhook 投递阶段；只要 intent 已提交，进程重启、Endpoint 恢复或人工重放都不能丢失该事实。

### 1.4 可靠性承诺

- 数据库 intent、attempt、dead-letter 状态机提供可恢复的 at-least-once 投递语义。
- 如果接收端已经处理 HTTP、但进程在本地 attempt 提交前退出，worker 可能再次发送；新的发送继续使用原业务
  `event_id`，接收方必须按 `event_id` 幂等，不能宣称网络 exactly-once。
- 同一来源 delivery 与同一 `idempotency_key` 只对应一个 replay delivery；重复 API 请求不会创建第二个 intent。
- 新的 replay 使用新的 `webhook_delivery_id` 和 `X-MetaHub-Delivery`，但信封内 `event_id` 保持不变。

## 2. 涉及模块

- `infra/migrations/0047_webhook_dead_letter_persistent_replay.sql` 与 down 迁移：
  新建死信表、索引、租户/状态触发器，回填历史失败 delivery，并扩展审计动作。
- `infra/migrations/0048_webhook_dead_letter_state_consistency.sql` 与 down 迁移：
  不改写 0047 checksum 或历史事实，加固根死信与当前 replay delivery 的终态一致性；
  `replay_pending + terminal delivery` 仍作为崩溃恢复瞬时状态交给 worker 原子收敛。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：
  注册 `webhook.dead_lettered` 内部事件，明确禁止 Endpoint 订阅，避免失败递归。
- `shared-sdk/python/metahub_shared/runtime/errors.py`、`i18n.py`：
  新增死信查询、状态、归档和恢复错误码及中英文展示标签。
- `control-plane-api/src/metahub_control/webhooks/models.py`：
  新增安全死信 DTO、严格 replay 请求、列表游标和归档响应。
- `control-plane-api/src/metahub_control/webhooks/service.py`：
  租户级查询、顶层 replay、兼容旧 replay、归档、过期和安全 DTO 转换。
- `control-plane-api/src/metahub_control/webhooks/outbox.py`：
  新增 replay claim/lease/CAS、pending 接管、退避和终态提交。
- `control-plane-api/src/metahub_control/webhooks/scheduler.py`：
  把死信过期与 replay 恢复 worker 组合进 Control API lifespan。
- `control-plane-api/src/metahub_control/webhooks/routes.py`、`app.py`：
  注册公开 API 与调度器。
- `control-plane-api/src/metahub_control/audit/service.py`：
  增加死信归档审计白名单；replay 继续复用 `webhook_delivery.replayed`。
- `control-plane-api/src/metahub_control/console/service.py`：
  增加 PAGE-083 后端导航项，角色仍为 admin/developer。
- `console-web/src/lib/webhooks.js`：
  增加死信列表、顶层 replay 和归档客户端，不发送 `tenant_id`。
- `console-web/src/features/webhook-dead-letters/`：
  新增页面状态、身份 owner、渲染和交互。
- `console-web/src/features/developer-start/renderDeveloperStart.js`、`src/app/main.js`、`src/styles/main.css`：
  接入独立导航、仅活动页加载、轮询取消、响应式布局和状态反馈。
- `control-plane-api/tests/`、`shared-sdk/python/tests/`、`console-web/tests/`：
  增加迁移、服务、崩溃恢复、租户/RBAC、异常、安全、前端生命周期和兼容测试。
- `scripts/acceptance/`：
  新增 PRD-057 Runtime 与 Browser 验收器。
- `README.md`、`docs/api/`、`docs/testing/`、`docs/development/directory-map.md`：
  只在全部验收通过后更新完成状态。

## 3. 数据表和字段设计

### 3.1 新表 `webhook_dead_letter_events`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `dead_letter_event_id` | TEXT PK，`whdl_` + 32 hex | 死信对象 ID；由来源 delivery ID 确定性派生，支持 trigger 幂等。 |
| `tenant_id` | TEXT NOT NULL | 所有读写的首要隔离键。 |
| `webhook_delivery_id` | TEXT NOT NULL | 首次最终失败的来源 delivery。 |
| `webhook_endpoint_id` | TEXT NOT NULL | 来源 Endpoint，必须与 delivery 同租户。 |
| `event_id` | TEXT NOT NULL | 原业务事件 ID；所有 replay 保持该值。 |
| `event_type` | TEXT NOT NULL | 稳定事件类型；不保存 payload 正文到死信 DTO。 |
| `status` | TEXT NOT NULL | `dead_letter/replay_pending/replayed/archived/expired`。 |
| `failure_error_code` | TEXT NULL | 首次失败的固定错误码，不保存异常正文。 |
| `failure_http_status` | INTEGER NULL | 接收端 HTTP 状态，100-599。 |
| `failed_at` | TEXT NOT NULL | 最终失败时间。 |
| `expires_at` | TEXT NOT NULL | 默认 `failed_at + 30 天`，v1 固定策略。 |
| `current_replay_delivery_id` | TEXT NULL | 当前或最近 replay delivery。 |
| `replay_requested_by` | TEXT NULL | 当前 replay 操作者，必须属于同租户。 |
| `replay_requested_at` | TEXT NULL | 当前 replay intent 提交时间。 |
| `replay_dispatch_attempts` | INTEGER NOT NULL DEFAULT 0 | worker 领取/恢复次数，不等于 HTTP attempt。 |
| `replay_next_attempt_at` | TEXT NULL | 恢复 worker 的下次领取时间。 |
| `replay_claim_token` | TEXT NULL | 不可预测 claim token，不对外返回。 |
| `replay_lease_owner_id` | TEXT NULL | worker owner，不对外返回。 |
| `replay_lease_expires_at` | TEXT NULL | 30 秒 lease；过期后可接管。 |
| `last_replay_error_code` | TEXT NULL | 固定恢复错误码，不保存原始异常。 |
| `replayed_at` | TEXT NULL | replay delivery 成功时间。 |
| `archived_by` | TEXT NULL | 归档操作者，必须属于同租户。 |
| `archived_at` | TEXT NULL | 逻辑归档时间。 |
| `created_at/updated_at` | TEXT NOT NULL | UTC 生命周期时间。 |

关键约束：

1. `UNIQUE (tenant_id, webhook_delivery_id)`；来源失败只能形成一个死信对象。
2. delivery、Endpoint、replay delivery、操作者都用复合租户外键或 owner trigger 校验。
3. owner、来源 ID、`event_id/event_type/failed_at/expires_at` 创建后不可改。
4. lease 三元组必须全 NULL 或全非 NULL；只有 `replay_pending` 可持有 lease。
5. `replayed` 必须有 replay delivery 与 `replayed_at`；`archived` 必须有 actor/time；
   `expired` 不允许 replay/归档 lease 残留，但必须保留此前成功 replay 的 `replayed_at` 和关联 delivery，
   过期只撤销再次重放资格，不能销毁历史投递证据。
6. 禁止 DELETE 和 `INSERT OR REPLACE` 身份复用；归档与过期是状态变更，不删除审计事实。

### 3.2 `webhook_deliveries` 兼容策略

- 保留现有 `pending/success/failed` 传输状态，避免破坏 PRD-025/042 API、详情筛选和历史测试。
- `dead_letter` 是 `DeadLetterEvent.status`，不把历史 `failed` 批量改成新 delivery 状态。
- 复用 PRD-042 已有 `delivery_kind/replayed_from_delivery_id/replay_idempotency_key/replayed_by/replayed_at`。
- 增加 `UNIQUE (tenant_id, webhook_delivery_id)` 供复合外键使用，不改变全局主键语义。
- migration 只回填历史 `delivery_kind='original' AND status='failed'` delivery，并按稳定排序读取最新历史 replay：
  pending 映射 `replay_pending`、success 映射 `replayed`、failed 映射回 `dead_letter`；后续 original
  INSERT/状态转为 failed 由普通 `INSERT` trigger 同事务创建死信，约束错误不得被静默忽略。

### 3.3 状态机

```text
delivery failed
  -> dead_letter
      -> replay_pending -> replayed
                       -> dead_letter   (HTTP 终态失败或恢复失败关闭)
      -> archived
      -> expired

replayed -> archived
         -> expired
```

- `replay_pending` 期间 archive/expire 返回冲突，避免 worker 与治理动作竞态。
- `archived/expired` 都不可 replay；恢复需要创建新的业务事件，而不是绕过留存策略。
- replay 失败后更新根 `DeadLetterEvent` 回到 `dead_letter`，绝不为失败 replay 创建子死信；不同幂等键
  可再次发起，旧幂等键仍由 replay delivery 唯一索引回读。

### 3.4 归档与过期策略

- 架构文档给出的 MVP 口径是“先保留可重放记录”；M1 使用数据库内逻辑归档，不强制引入 MinIO。
- 归档保留安全事件信封和 attempt 审计，但从可重放队列移除；公开列表仍可按 `archived` 查询。
- 过期策略固定 30 天，由服务端 UTC 时间计算；worker 原子把到期 `dead_letter/replayed` 标成 `expired`。
  过期保留原始安全信封、attempt、最近 replay delivery 和 `replayed_at`，只撤销公开 replay 资格。
- PAGE-083 不返回 payload，归档/过期也不把 payload 写入日志、浏览器存储或对象存储。
- 外部 S3/MinIO 冷归档适配器留作 M2 可选模块，不影响本 PRD 可运行验收。

### 3.5 审计表变更

- 增加 `webhook_dead_letter.archived -> webhook_dead_letter`。
- `SUMMARY_FIELDS.webhook_dead_letter` 只允许死信 ID、delivery ID、Endpoint ID、事件类型、前后状态和时间。
- 归档状态与 audit row 同一事务；commit 后才记录成功日志。
- replay 继续写 `webhook_delivery.replayed`，同幂等 intent 不重复 audit。
- 0047 up/down 重建 audit CHECK 白名单时必须逐行、rowid、NULL 保真并做双向 `EXCEPT` 校验；
  0048 只替换 owner trigger 的状态关系约束，down 不删除或改写任何业务事实。

## 4. API 设计

所有 API 复用统一中间件，Header 与 Body 保持同一 `request_id/trace_id`；后端使用
`require_webhook_manager`，当前只允许 admin/developer。PRD-059 前不提前引入 FinanceAdmin/SecurityAdmin。

### 4.1 `GET /webhook-dead-letters`

查询参数：

- `status`：`all/dead_letter/replay_pending/replayed/archived/expired`，默认 `all`。
- `event_type`：可选，稳定事件目录中的值。
- `webhook_endpoint_id`：可选，只能收窄当前认证租户。
- `limit`：1-100，默认 50。
- `cursor`：可选，服务端解析 `failed_at + dead_letter_event_id` 的 URL-safe 游标；非法值返回 422。

排序固定为 `failed_at DESC, dead_letter_event_id DESC`，使用 seek pagination，不使用会漂移的 offset。
响应只包含安全摘要、最新 attempt、`replayable` 和后端计算的 `allowed_actions`，不包含 payload、URL、
幂等键、claim/lease、Secret、签名或接收端正文。

### 4.2 `POST /webhook-deliveries/{webhook_delivery_id}/replay`

请求体：

```json
{"idempotency_key":"console_<uuid>"}
```

约束与行为：

1. DTO `extra='forbid'`，拒绝 `tenant_id/user_id/endpoint_id/payload/secret`。
2. 后端只按认证 `tenant_id + delivery_id` 查询；跨租户与不存在统一 404。
3. delivery 必须 failed，关联死信必须为 `dead_letter`；archived/expired/replay_pending 返回 409。
4. 同一来源与幂等键若已有 replay delivery，直接回读，不能重复 audit、intent 或出网。
5. 首次请求在 `BEGIN IMMEDIATE` 中创建 replay delivery、把死信改为 `replay_pending`、写 audit 后提交。
6. commit 后当前请求尝试领取并处理一次；未能领取或进程退出时由 scheduler 在 lease 到期后接管。
7. 返回安全 delivery 摘要和死信状态；不返回原事件 payload。

### 4.3 旧接口兼容

`POST /webhooks/{webhook_endpoint_id}/replay` 保留，继续验证路径 Endpoint 与来源 delivery 同租户同归属，
内部改用同一个持久 replay intent/worker。旧调用方仍得到 `WebhookReplayResponse`，但发送事实不再停留在
PRD-042 的不可接管 at-most-once 边界。

### 4.4 `POST /webhook-dead-letters/{dead_letter_event_id}/archive`

- 请求体为空，admin/developer 可用；viewer 和匿名由后端拒绝。
- 仅 `dead_letter/replayed` 可归档；pending、已归档、已过期返回稳定 409。
- 状态更新、`archived_by/at` 与管理审计同事务。
- 返回安全死信 DTO 与 `request_id/trace_id`。

### 4.5 内部 worker 合同

- 每轮先过期到期记录，再领取一条 `replay_pending`。
- claim 使用 `BEGIN IMMEDIATE`、不可预测 token、worker ID、30 秒 lease 和 CAS。
- worker 复用现有 `_resume_pending_delivery`：从数据库 `payload_json` 重建 canonical body，从
  `attempt_count + 1` 继续，重读当前 active Endpoint 与 Secret 版本，不创建第三套 HTTP 循环。
- 成功后 CAS 为 `replayed`；HTTP 终态失败回 `dead_letter`；数据库暂时错误按 1 秒、5 秒、30 秒、
  5 分钟封顶退避。
- 旧 worker lease 过期后即失权，finish 必须同时匹配 tenant、死信 ID、replay delivery、token 和 owner。

## 5. 前端页面和交互设计

### 5.1 PAGE-083 页面结构

- 新增独立导航 `webhook-dead-letters`，显示名“Webhook 死信”，紧随“Webhook 设置”。
- 首行是状态分段控件、事件类型菜单、Endpoint 菜单和刷新图标按钮。
- 主列表展示 Endpoint 安全名称/ID、事件类型、`event_id`、死信时间、过期时间、固定错误码、
  attempt 数、状态和最近 replay 摘要。
- `allowed_actions` 包含 replay 时显示重放图标/文本命令；可归档时显示归档图标并二次确认。
- replay 使用内存幂等键；网络结果不确定时用同一键重试，不写 localStorage/sessionStorage/URL。
- `replay_pending` 行显示非跳动的状态与轮询结果，按钮禁用；终态后停止该行轮询。
- 翻页使用服务端 `next_cursor`；切换筛选清空游标和旧结果。

### 5.2 页面生命周期

- 只有当前后端导航为 `webhook-dead-letters` 且角色为 admin/developer 时才加载，其他页面零预取。
- identity owner 必须包含 `tenant_id/user_id/role/session_id/session_revision`。
- 每次加载和动作持有 identity/navigation/generation ticket 与 AbortController；迟到成功和迟到错误都丢弃。
- 登出、401、用户/租户/角色/Session revision 变化、离开导航和 `pagehide` 同时取消请求、轮询并清空状态、
  幂等键、反馈和 request_id。
- viewer 无导航、无 DOM、无网络；直接 hash 自动回退到有权限页面，但后端 403 仍是最终权限边界。

### 5.3 响应式与可访问性

- 桌面使用稳定列宽表格；移动端转为带 `data-label` 的纵向字段组，不横向挤压 ID 或按钮。
- 过滤控件可换行，图标按钮提供 `title/aria-label`，确认对话框支持键盘和焦点恢复。
- loading/empty/error/partial/replay_pending/archived/expired 都有明确状态；反馈携带 request_id。
- 所有名称、ID、错误码和时间均 HTML/属性转义，长 ID 使用可换行 code 样式。

## 6. 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `webhook.dead_letter_not_found` | 404 | 死信或 delivery 不存在、跨租户或归属不匹配。 |
| `webhook.dead_letter_not_replayable` | 409 | 已归档、已过期、已成功或其他不可 replay 状态。 |
| `webhook.dead_letter_replay_pending` | 409 | 已有不同幂等键 replay 正在恢复。 |
| `webhook.dead_letter_not_archivable` | 409 | pending、已归档或已过期不能归档。 |
| `webhook.dead_letter_filter_invalid` | 422 | 状态、事件、Endpoint、limit 或 cursor 非法。 |
| `webhook.dead_letter_unavailable` | 503 | 死信持久化、claim、CAS、回读或过期事务失败。 |
| `webhook.idempotency_key_invalid` | 422 | replay 幂等键格式或长度非法。 |
| `webhook.replay_unavailable` | 503 | replay intent 或发送恢复不可用。 |
| `auth.not_authenticated` | 401 | 未登录或 Token 无效。 |
| `auth.permission_denied` | 403 | viewer 或其他无管理权限角色。 |

所有错误由统一中间件生成 `code/message/request_id/trace_id/suggestion`，不得返回 SQL、数据库路径、
URL、payload、幂等键、claim token、lease owner、Secret、签名、Authorization 或原始异常。

## 7. 安全与权限约束

1. 所有新表、索引、查询、更新和 CAS 显式包含 `tenant_id`；API 不接受 tenant 字段。
2. delivery、Endpoint、DeadLetterEvent、replay delivery 和 actor 通过复合外键或 owner trigger 绑定同租户。
3. admin/developer 通过后端 `require_webhook_manager`；前端隐藏按钮仅用于体验。
4. 跨租户 ID 与不存在 ID 返回同形 404，不暴露资源存在性或 Endpoint URL。
5. 死信 API 和 PAGE-083 不返回 payload；worker 仅从数据库内部读取并再次执行严格信封与敏感键校验。
6. HTTP 客户端继续 `trust_env=False`、不跟随重定向、有界超时、streaming 只读状态码。
7. 日志只记录 tenant、死信/delivery/event 不透明 ID、状态、固定错误码和 request/trace；不记录正文和异常文本。
8. `webhook.dead_lettered` 是内部事实，不进入 Endpoint 可订阅集合，避免失败事件递归风暴。
9. archive 与 audit 同事务；replay intent 与 audit 同事务；网络永远在 commit 后。
10. 归档/过期与 replay claim 串行化，`replay_pending` 期间不能改变治理终态。
11. worker 的 claim token、owner、lease、退避字段不进入 API、审计、前端或普通日志。
12. 数据库触发器拒绝 owner 改写、非法状态迁移、DELETE、REPLACE 和跨租户引用。

## 8. 测试用例

### 8.1 迁移与数据库

1. 空库迁移到 0048，0047 的表、字段、复合 FK、索引、trigger 和审计枚举完整，0048 的状态一致性约束生效。
2. 从 0046 经 0047/0048 升级，历史 success/pending 不产生死信，历史 failed 每条恰好回填一个死信。
3. 重复 up 幂等；无 PRD-057 事实时 0047 down 回到 0046，rowid/NULL/审计逐行保真；0048 down/up
   只撤销/恢复状态关系加固。
4. 存在死信、replay、archive 或 0047 audit 时 0047 down 原子拒绝，不能静默丢证据。
5. 跨租户 delivery/Endpoint/replay/actor、非法根状态与 replay delivery 终态关系、owner UPDATE、
   DELETE/REPLACE 均失败。
6. `integrity_check=ok`、`foreign_key_check=0`，迁移总数/最高版本为 48/0048。

### 8.2 后端主流程

1. 本地接收端持续 500，业务 delivery intent 先入库，最多三次 attempt 后同事务形成 dead_letter。
2. `GET /webhook-dead-letters` 返回当前租户安全摘要、seek cursor、replayable/allowed_actions，零 payload。
3. 接收端恢复后，顶层 POST 创建持久 replay，event_id 与来源相同，delivery ID 不同，最终 replayed。
4. 同幂等键串行和并发请求只有一个 replay delivery，接收端只观察一个逻辑 intent。
5. 不同幂等键允许再次重放；失败 replay 回到 dead_letter 并保留最近结果。
6. archive 成功并写同事务 audit；archived/expired 不可 replay。
7. 注入时钟证明 30 天到期，过期不依赖真实等待。

### 8.3 崩溃恢复与并发

1. 在 replay intent/audit commit 后、HTTP 前注入退出；新进程 scheduler 领取并完成。
2. 在 HTTP 成功后、attempt commit 前注入退出；恢复可能重发，但两次信封 event_id 完全相同。
3. lease 未过期时第二 worker 不领取；到期后新 token 接管，旧 worker finish CAS 为 no-op。
4. archive/expire 与 replay claim 并发时只有一个合法状态获胜，无网络发生在已归档/过期记录上。
5. Endpoint 禁用、删除或 Secret 轮换后恢复：禁用安全失败，轮换使用当前版本，均不泄露 Secret。
6. 数据库 lock/close/schema 异常返回统一 503，释放连接和事务，恢复后同幂等键可继续。

### 8.4 权限、租户和安全

1. admin/developer 可读、replay、archive；viewer 403；匿名 401。
2. 甲租户不能读取、replay 或 archive 乙租户 delivery/死信，统一 404 且零出网。
3. 请求体夹带 tenant/user/endpoint/payload/secret 由严格 DTO 拒绝。
4. 损坏或含 Prompt、Response、messages、input、Authorization、Token、Key、password、secret 的历史 payload
   在任何 HTTP 前拒绝，响应和日志不回显命中内容。
5. 数据库、服务日志、Browser JSON、DOM、URL、Storage 和截图执行固定及动态敏感标记扫描。

### 8.5 前端

1. Client 使用精确新路径，不发送 tenant，不解析或保存 payload。
2. 全部状态、筛选、cursor、allowed_actions、错误与 request_id 正确渲染并转义。
3. 网络结果不确定复用同一内存幂等键；成功后清除，刷新为后端终态。
4. identity/navigation/generation/Abort 门禁丢弃迟到成功与错误。
5. 登出、401、降权、Session revision 变化、离页和 pagehide 清空状态并停止轮询。
6. viewer 零导航、零 DOM、零预取；直接 hash 回退。
7. 1280x720 与 390x844 无根横向溢出、按钮/控件/文本重叠或截断。

### 8.6 回归与运行验收

1. PRD-025/042 Webhook、PRD-053 pending resume、PRD-054/055/056 source outbox 相关测试通过。
2. Shared Webhook 合同、Control API、迁移、Console 专项与 lint 通过。
3. `scripts/ci/check-comments.py` 在最终源码通过。
4. 只在专项稳定后执行一次完整 Python、Node 示例和 Console 回归；局部失败只重跑相关 node。
5. 使用 `D:\MetaHub\.tmp\prd057` 短 basetemp/cache，不提权、不使用 `C:\tmp`。
6. 真实 Gateway/Control/Console/receiver 健康 200；500 -> dead-letter -> receiver 恢复 -> replayed 全链路通过。
7. Browser 覆盖 admin 桌面列表/重放/归档、admin 移动、developer、viewer 六类状态，截图非空且无泄露。
8. 停服后 Session、端口、SQLite sidecar、spool 与 ReparsePoint 清理符合既有终态规范；只删除
   `D:\MetaHub\.tmp` 内确认的链接节点，保留目标目录、普通目录和测试证据。

## 9. 代码修改计划

1. 新增 0047 up/down，以迁移测试冻结 schema、回填、状态机、租户 FK 和审计保真；交叉复审后追加
   0048 up/down，在不改写既有 checksum 的前提下冻结根死信与 replay delivery 终态关系。
2. 扩展 Shared 事件目录与统一错误，不改变 Endpoint 可订阅递归边界。
3. 新增 DeadLetter DTO、查询/归档服务和公开路由。
4. 把现有 replay intent 创建改造成可领取事实；新增 outbox claim/dispatch/finish 与 scheduler。
5. 让旧 Endpoint replay 和新顶层 delivery replay 共用同一实现，保持 PRD-042 兼容。
6. 新增后端导航和 PAGE-083 client/state/render/lifecycle/CSS。
7. 先跑迁移、后端、Shared、前端专项；修复后跑相关相邻回归。
8. 执行一次全量回归、注释门禁、真实 Runtime/Browser 与静止终态验收。
9. 验收全部通过后更新 API 文档、测试记录、README 和目录说明，再标记 PRD-057 完成。

## 10. 设计自审第一轮：边界、兼容与数据安全

### 发现的问题

1. 直接把 `webhook_deliveries.status` 从 failed 改成 dead_letter 会破坏 PRD-025/042 的筛选、DTO 和存量测试。
2. 只扫描 pending replay delivery 无法表达归档/过期，也无法把 replay 操作者和源失败事实稳定关联。
3. 把 `webhook.dead_lettered` 设为可订阅会在接收端故障时递归制造更多死信。
4. 强制新增 MinIO 会把可选基础设施变成 M1 阻断依赖，不符合架构“MVP 先保留可重放记录”。
5. 仅使用全局 delivery 主键做 FK 虽能定位，但缺少数据库层同租户所有权证明。
6. 若 API 返回现有 `WebhookDeliveryInfo.payload`，PAGE-083 会获得不需要的事件正文。

### 已修正

1. 保留 delivery 的 pending/success/failed；新增独立 DeadLetterEvent 状态机。
2. replay job 状态、lease、归档和过期全部归属于 DeadLetterEvent，HTTP attempt 仍复用 delivery。
3. `webhook.dead_lettered` 只进内部稳定事件目录，从可订阅集合排除。
4. M1 做数据库逻辑归档；外部冷归档留作可选扩展，不影响本 PRD 验收。
5. 增加复合租户 FK/owner trigger 与所有 CAS 的 tenant 条件。
6. 新顶层 API 使用无 payload 的安全 DTO；旧接口只保留兼容合同。

第一轮结论：范围覆盖 Excel 和 PRD-042 保留边界，没有提前引入 SIEM/对象存储主链路，兼容与租户隔离可实现。

## 11. 设计自审第二轮：恢复竞态、权限与前端生命周期

### 发现的问题

1. intent 提交后由当前请求直接发送仍可能在崩溃后停留 pending，必须有独立 lease worker。
2. 远端成功、本地未提交时无法证明 exactly-once；若隐藏该事实会形成错误验收口径。
3. archive/expire 与 worker 并发可能出现已归档后仍出网。
4. 只把当前幂等键保存在 DeadLetterEvent 会丢失旧键历史，之后可能重复发送。
5. 现有 Webhook 前端身份只比较 tenant/user/session，角色原地降权和 Session revision 变化可能残留数据。
6. 全局预取与迟到 replay Promise 可能在离页或换 Session 后重新写入旧状态。
7. 迁移只测试空库会遗漏历史 failed 回填、旧 audit row 保真和 down 数据丢失。

### 已修正

1. 新增 30 秒 lease/CAS scheduler；请求只做一次即时 drain，恢复不依赖原请求存活。
2. 明确 at-least-once 和相同 event_id 幂等，测试两个崩溃窗口，不宣称网络 exactly-once。
3. replay_pending 期间禁止 archive/expire；finish 必须匹配 tenant、token、owner、delivery 和状态。
4. 幂等历史继续由 PRD-042 replay delivery 部分唯一索引保存，DeadLetterEvent 只指向当前/最近 replay。
5. PAGE-083 identity 纳入 tenant/user/role/session_id/session_revision。
6. 仅活动导航加载，所有请求使用 identity/navigation/generation/Abort owner，离页与 pagehide 清理。
7. 增加 0046 升级、历史回填、审计双向 EXCEPT、含事实 down 拒绝和硬编码迁移计数兼容测试。

第二轮结论：崩溃接管、并发状态机、后端权限、前端零预取/迟到响应和迁移兼容边界已闭合；可以进入编码。

## 12. 最终完成边界

- 本文件已完成需求设计和两轮编码前自审；0047/0048、后端 worker/API、PAGE-083 与配套文档均已落地。
- 代码冻结后的 PRD-057 专项为 `24 passed`。唯一一次根级 Python 全量共收集 `1496` 个节点，真实
  结果为 `1407 passed / 89 failed`（pytest `5960.85s`，wrapper `5970.009s`）；没有重跑根级全量。
  原 75 个迁移失败节点以 `14 + 21 + 40 passed` 闭环，原 14 个非迁移失败节点以相关六文件
  `53 passed` 覆盖，最终原失败节点 `89/89` 精确定向闭环且无新增失败。
- Node 示例 `33 passed`、Console `462 passed`、Console lint `151 files passed`、Python 内存语法
  `26 files passed`、Ruff 致命规则和中文注释门禁均通过。
- 隔离运行库共应用 48 条迁移、最高版本 0048；0048 checksum 为
  `5d3a6b1a76e41e6586e75d42946164c3d7de4cd4c2fcf4268994176f7282ac20`，
  `integrity_check=ok`、外键违规 0、PRD-057 trigger 9。
- 真实 Runtime 已验证四项健康检查 HTTP 200、500 三次后形成唯一根死信、恢复后 replayed、同
  `event_id`、同幂等键零重复出网、admin/developer 允许、viewer 403、匿名 401、第二租户隔离、
  HMAC 有效及公开 DTO 去敏；Runtime 证据 SHA-256 为
  `a99a7138492e3b272938b30116b0f5a4efdcedc005b21a6a6904725bd4469070`。
- Browser 在 1280x720 与 390x844 共生成 5 张非空截图，三角色、重放/归档、焦点、零预取、零横向
  溢出和零敏感命中通过；Browser 与数据库审计 SHA-256 分别为
  `6e2d9bf3560d6df2099b856abd7a863517803793ffd75ba6014b5de82b274c0c` 和
  `51feaf2c68a9cd6b395da95d5cd139306ea0a7460e5595de86ae5dd1efdc2c64`。
- 停服后 8000/8001/5173/18057 的 IPv4/IPv6 监听、active Session、replay pending、active lease、
  pending replay delivery、spool 与应用运行库 sidecar 均为 0，日志没有非预期 ERROR、CRITICAL、
  Traceback 或未捕获异常。
- 链接清理只作用于 `D:/MetaHub/.tmp` 内确认过的链接节点：根级全量阶段删除 540 个并保留 540 个
  目标，增量阶段删除 159 个并保留 159 个目标，可见范围剩余 0、目标缺失 0。22 个既有 ACL 目录
  无法遍历且未提权或绕过，因此结论严格记为 `passed_with_authorized_visible_scope`，不宣称无条件
  全局 0；全过程未使用 `C:/tmp`。
- 权威 artifact 为 `artifacts/prd057/20260731T005000/`，共 38 个文件，manifest 覆盖 36/36 且
  hash/字节复核一致，artifact 内 ReparsePoint 与 SQLite sidecar 均为 0。最终审计、manifest 和
  运行库 SHA-256 分别为 `b92e8b8fefd4cd0a215b84f3b56ea3f083edbf91c2aa5ec760d20ca04a0bc05b`、
  `a3327905175b997016c49d01f50d5d1937b52a3c793c67ab33d6b1657a2cfd33` 和
  `b8cb28d630dd2b52ed4f36999a493dc89b8f7e00f552e2554c7d984541534723`。
- PRD-057 已完成，下一项为 PRD-058；`all_108_prds_complete=false`。
