# PRD-042 - Webhook 事件过滤与重放基础

状态：已完成设计、两轮自审、实现、专项与全量拆分回归、0031 运行库迁移及桌面/移动运行验收。

## 需求理解

### 来源与顺序

- Excel `01_PRD总表` 的实际行顺序在 PRD-041 后为 PRD-042，阶段为 `M0.5-W3/4`，优先级为 `P0`，能力层为“核心层”，模块为“基础 Webhook 扩展”。
- 接手时 README、`docs/prd` 与 `docs/testing` 的完成证据连续到 PRD-041，因此按 Excel 实际行顺序识别 PRD-042 为下一条未完成项；本文件随后先完成设计和两轮自审，再进入实现。
- 直接前置是已完成的 PRD-025“基础事件订阅与推送”；后续 PRD-057 才负责死信与持久重放。本 PRD 不实现对象存储死信桶、外部事件总线、长期归档或崩溃恢复器。

### 用户价值与范围

开发者或承担运维职责的管理员需要在 Webhook Endpoint 详情中：

1. 按事件类型和投递状态筛选发送记录。
2. 查看指定 Endpoint 最近最多 100 条发送记录及每次 attempt 的安全摘要。
3. 对失败 delivery 手工重放；相同幂等键的重复请求只允许产生一次出站发送。
4. 原子轮换 Endpoint 的 HMAC signing secret；新 secret 只在轮换响应中返回一次，轮换完成后所有新投递立即使用新 secret，旧 secret 不再有效。
5. 通过统一错误码、`request_id`、`trace_id`、管理审计和 replay 关联字段追踪成功与失败操作。

### API 中 `{id}` 的语义

- `POST /webhooks/{id}/replay` 中 `{id}` 继续沿用现有 `/webhooks/{id}/test` 的资源语义，表示 `webhook_endpoint_id`。
- 请求体显式携带 `webhook_delivery_id` 和 `idempotency_key`。后端必须同时按当前登录态的 `tenant_id + webhook_endpoint_id + webhook_delivery_id` 查询，禁止仅凭 delivery ID 重放。
- `POST /webhooks/{id}/rotate-secret` 中 `{id}` 同样表示 `webhook_endpoint_id`。

### 验收口径

- TC-0083：失败记录可以重放；列表可按事件类型过滤并读取最近 100 条；轮换后新投递使用新 secret，旧 secret 对新投递签名验签失败。
- TC-0084：viewer、匿名、跨租户、成功记录重放、禁用 Endpoint、非法筛选、非法/重复幂等键、数据库或网络异常均返回统一错误结构；请求和日志不泄露 Secret、签名、Prompt、Response、Token 或 Key。
- 重放幂等定义为：同一租户、同一 Endpoint、同一来源 delivery、同一 `idempotency_key` 的串行或并发请求只创建一个 replay delivery，并只执行一次最多三次的出站尝试；不同幂等键允许操作者再次手工重放。

## 涉及模块

- `docs/prd/`
  - 保存本设计、两轮自审、实现摘要和边界。
- `infra/migrations/`
  - 新增 `0031_webhook_replay_secret_rotation.sql` 和 down 迁移。
  - 调整 delivery 唯一性，增加 replay 关联字段和 Endpoint secret 版本字段。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`
  - 注册 `webhook.replayed` 稳定内部事件类型；把“事件目录”和“Endpoint 可订阅目录”分开，复用统一事件信封和 HMAC 工具。
- `shared-sdk/python/metahub_shared/runtime/webhook_delivery.py`
  - 保持原始治理事件按 `event_id` 幂等；兼容迁移后的 delivery 字段与唯一索引。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 增加 replay、详情筛选、Endpoint 状态和幂等冲突所需的统一错误码。
- `control-plane-api/src/metahub_control/webhooks/`
  - DTO、详情查询、失败 delivery 重放、Secret 轮换、租户校验、审计事务和安全日志。
- `control-plane-api/src/metahub_control/audit/`
  - 复用 PRD-034 的审计写入和提交后 `audit.recorded` 投递；扩展严格的 action/object/summary 白名单，不创建平行审计表。
- `console-web/src/lib/webhooks.js`
  - 增加详情查询、replay 和 rotate-secret 客户端；请求不得包含 `tenant_id`、用户 ID 或 secret。
- `console-web/src/features/webhooks/renderWebhookPanel.js`
  - 增加 Endpoint 详情、事件/状态筛选、100 条记录、失败记录重放和 Secret 轮换交互。
- `console-web/src/app/main.js`、`console-web/src/styles/main.css`
  - 接入筛选状态、操作状态、一次性新 Secret 展示和响应式布局。
- `control-plane-api/tests/`、`console-web/tests/`
  - 增加迁移、后端主流程/异常/权限/并发幂等和前端渲染/API 测试。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/`
  - 只有完整验收后才记录 PRD-042 已完成。

## 数据表和字段设计

### webhook_endpoints 扩展

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `secret_version` | INTEGER NOT NULL DEFAULT 1 CHECK >= 1 | 当前 signing secret 版本；每次成功轮换原子加一。 |
| `secret_rotated_at` | TEXT NULL | 最近一次轮换的 UTC 时间；初始创建为 NULL。 |
| `secret_rotated_by` | TEXT NULL，FK users | 最近轮换操作者；必须属于当前认证租户。 |

现有 `encrypted_secret`、`secret_prefix`、`secret_fingerprint` 继续只保存密文和不可逆摘要。轮换事务一次性替换四个 secret 字段、版本、操作者、时间与 `updated_by/updated_at`，数据库中不保留旧 secret 或宽限版本。

### webhook_deliveries 扩展

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `delivery_kind` | TEXT NOT NULL DEFAULT `original`，CHECK `original/replay` | 区分原始投递和手工重放。 |
| `replayed_from_delivery_id` | TEXT NULL，自关联 FK | replay 指向来源失败 delivery；original 必须为 NULL。 |
| `replay_idempotency_key` | TEXT NULL | 规范化幂等键，仅 replay 使用，不回显到日志。 |
| `replayed_by` | TEXT NULL，FK users | 手工重放操作者。 |
| `replayed_at` | TEXT NULL | 手工重放开始时间。 |
| `secret_version` | INTEGER NOT NULL DEFAULT 1 CHECK >= 1 | 该 delivery 实际签名使用的 Endpoint secret 版本。 |

约束与索引：

1. 移除旧表级 `UNIQUE (tenant_id, webhook_endpoint_id, event_id)`，改为部分唯一索引：
   - original：`tenant_id + webhook_endpoint_id + event_id` 唯一，保持 shared runtime 的稳定事件幂等。
   - replay：`tenant_id + webhook_endpoint_id + replayed_from_delivery_id + replay_idempotency_key` 唯一，保证串行/并发手工重放幂等。
2. replay 保留来源事件的 `event_id/event_type/event_timestamp/payload_json`，让接收方看到同一个业务事件；新 `webhook_delivery_id` 和 `X-MetaHub-Delivery` 区分新的发送行为。
3. replay 的 `tenant_id` 和 `webhook_endpoint_id` 必须与来源 delivery 一致；服务层在事务内验证，数据库索引作为第二道约束。
4. `webhook_delivery_attempts` 不新增敏感字段；新 replay 使用新的 delivery ID，从 attempt 1 开始，最多三次。
5. `secret_version` 在创建 delivery 时从 Endpoint 快照写入，便于确认发送使用哪个版本，但 API 不返回密文或签名。
6. `secret_version` 是 delivery 创建时的签名版本快照；控制面和 shared runtime 两条现有原始投递路径都必须显式写入 Endpoint 当前版本，不能依赖默认值，否则轮换后会错误显示版本 1。

### 迁移策略

- up：重建 `webhook_deliveries` 以移除旧唯一约束并加入新 CHECK/关联字段，复制所有历史 delivery 为 `delivery_kind='original'`；重建相关索引，给 Endpoint 增加 secret 版本字段。
- up 必须保留既有 delivery/attempt 外键、历史数据和 PRD-025 查询语义，迁移前后 `PRAGMA foreign_key_check` 与 `integrity_check` 均通过。
- down：先删除 replay attempts 和 replay deliveries，再把 original delivery 重建回 PRD-025 表结构；移除新增索引和 Endpoint 新字段。回滚会丢弃仅属于 PRD-042 的 replay/rotation 元数据，但保留 PRD-025 原始投递与 attempts。
- 空库正向、重复迁移、0031 单步回滚、从 0030 升级、含历史失败记录升级都必须测试。

## API 设计

所有接口复用统一中间件，响应 Header 和 Body 携带 `request_id/trace_id`；后端使用 `require_webhook_manager`，当前只允许 admin/developer。

### GET /webhooks/{webhook_endpoint_id}/deliveries

查询参数：

- `event_type`：可选，必须是稳定 Webhook 事件白名单中的值。
- `status`：可选，`all/pending/success/failed`，默认 `all`。
- `limit`：可选，1-100，默认 100，硬上限 100。

行为：

- 先按 `tenant_id + webhook_endpoint_id` 确认 Endpoint 归属，再查询该 Endpoint 的历史 delivery。
- 过滤只作用于历史 delivery，不要求事件仍在 Endpoint 当前订阅列表中，避免取消订阅后历史记录不可见。
- 返回按 `created_at DESC, webhook_delivery_id DESC` 排序的最近记录及 attempts；不返回 `payload_json` 正文、幂等键、密文 secret 或签名头。现有 `GET /webhooks` 保持兼容，列表摘要不再承担 100 条详情查询。

响应 DTO：

```json
{
  "webhook_endpoint_id": "wh_...",
  "deliveries": [],
  "filters": {"event_type": null, "status": "all", "limit": 100},
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /webhooks/{webhook_endpoint_id}/replay

请求体：

```json
{
  "webhook_delivery_id": "whd_...",
  "idempotency_key": "console_<uuid>"
}
```

约束：

- DTO 使用 `extra='forbid'`，拒绝 `tenant_id/user_id/signing_secret/payload` 等夹带字段。
- 幂等键去除首尾空格后长度 8-128，只允许 ASCII 字母、数字、点、下划线、冒号和短横线。
- 仅 `status='failed'` 的 delivery 可重放；来源 replay 失败后也允许再次重放，但新记录仍直接关联本次来源 delivery，形成可追踪链而不做死信归档。
- Endpoint 必须为 active；disabled Endpoint 返回冲突错误，不允许通过 replay 绕过停用。
- 服务层先在短事务中按唯一键创建 replay delivery 和管理审计事实并提交，再执行网络发送；同键冲突时回读并返回既有 delivery，不再次出网。
- 若并发重复请求在首个请求完成网络发送前命中同一 replay delivery，可以返回同一 `webhook_delivery_id` 的 `pending` 状态；调用方随后通过详情查询观察终态。幂等合同保证“不重复出网”，不通过持有数据库事务等待外部 HTTP 来保证两个响应同时到达终态。
- 出站发送复用来源事件原始安全信封，发送前再次递归检查敏感键；旧的非法/损坏 payload 返回统一安全错误，不发送。
- replay 不实现第三套 HTTP 投递循环；它复用现有 attempt/update 原语，并把控制面发送行为对齐 shared runtime：使用 streaming 响应只读取 HTTP 状态，不下载或保存接收端正文。
- 返回 `delivery`、`replayed`（首次为 true，幂等复用为 false）、`request_id/trace_id`。

### POST /webhooks/{webhook_endpoint_id}/rotate-secret

请求体为空。行为：

1. 按当前租户读取 Endpoint，生成 `whsec_` 新 secret。
2. 在同一事务中写入新密文、prefix、fingerprint、版本、操作者、时间和管理审计。
3. 提交后只在本次响应返回新 `signing_secret`；普通列表、详情、审计、日志和后续响应都不返回明文。
4. 事务失败必须 rollback，旧 secret 继续有效；事务成功后新 delivery 只使用新 secret，不保留旧 secret 宽限。

响应复用一次性 Secret 语义并增加 `secret_version`：

```json
{
  "webhook": {},
  "signing_secret": "whsec_...",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### webhook.replayed 事件

- replay 首次创建后记录安全结构化 `webhook.replayed` 事实，并写管理审计 `webhook_delivery.replayed`。
- 事件只包含 `source_delivery_id/replay_delivery_id/endpoint_id/event_type/actor_id/trace_id`，不含 URL、payload 正文、幂等键、Secret、签名或接收端响应。
- `webhook.replayed` 进入共享事件目录，但不进入 Endpoint 可订阅目录；创建/更新 Endpoint 时必须拒绝订阅它，列表 API 也不能把它作为可选订阅项返回。
- 本 PRD 不把 `webhook.replayed` 再同步投递到任何 Endpoint，避免手工重放引发递归或额外失败风暴；后续异步事件总线可复用该稳定内部事件码。

### 管理审计合同

- 扩展 `ACTION_OBJECT_TYPES`：`webhook_delivery.replayed -> webhook_delivery`、`webhook_endpoint.secret_rotated -> webhook_endpoint`。
- 扩展 `SUMMARY_FIELDS`，只允许 Endpoint/Delivery ID、事件类型、状态、secret version、来源 delivery ID 和 replay delivery ID；禁止 URL、payload、幂等键、prefix/fingerprint、Secret 和签名。
- replay 首次创建与 audit row 在同一事务；命中已有幂等记录不重复写审计。
- rotation 的 secret 替换与 audit row 在同一事务；提交后才派发 `audit.recorded`，派发失败不得回滚已经提交的轮换。

## 前端页面和交互设计

### 页面结构

- 保留“Webhook 设置”主入口和创建表单。
- Endpoint 表格增加“查看记录”和“轮换 Secret”图标/文本命令；轮换是敏感操作，点击后显示确认对话框，确认文案不展示旧 secret。
- 详情区固定展示当前选中 Endpoint 名称、状态、secret prefix/fingerprint/version、事件类型与发送记录。
- 详情工具栏提供事件类型下拉框、状态分段/下拉筛选和刷新命令；查询上限固定 100，不让用户在 MVP 触发无界查询。
- 失败记录显示“重放”命令；pending/success 不显示。操作期间按钮禁用，完成后刷新详情并在顶部展示成功/失败及 `request_id`。
- 新创建或新轮换的 Secret 共用一次性 Secret 提示区，只保存在内存和当前 DOM；复制后不写 localStorage/sessionStorage、console event 或 URL。

### 响应式和可访问性

- 桌面端详情使用稳定列宽表格；移动端将 delivery 行转为纵向字段组，筛选控件可换行，按钮不覆盖事件 ID 或错误码。
- 图标按钮提供 `aria-label/title`；确认框可用键盘操作；加载、错误和成功反馈使用 `role=status/alert`。
- 所有 Endpoint 名称、事件类型、错误摘要和 ID 继续 HTML/属性转义。
- viewer 不显示页面入口和操作，但这只是体验优化，后端始终执行权限校验。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `webhook.endpoint_not_found` | 404 | Endpoint 不存在或不属于当前租户。 |
| `webhook.delivery_not_found` | 404 | delivery 不存在、属于其他 Endpoint 或其他租户。 |
| `webhook.delivery_not_replayable` | 409 | delivery 不是 failed，不能手工重放。 |
| `webhook.endpoint_disabled` | 409 | Endpoint 已停用，禁止测试/重放。 |
| `webhook.idempotency_key_invalid` | 422 | 幂等键缺失、格式或长度非法。 |
| `webhook.delivery_filter_invalid` | 422 | 事件、状态或 limit 筛选非法。 |
| `webhook.replay_unavailable` | 503 | replay 持久化、回读或安全载荷校验失败。 |
| `webhook.secret_unavailable` | 503 | Secret 生成、加密、解密或原子轮换失败。 |
| `auth.not_authenticated` | 401 | 未登录或 Token 无效。 |
| `auth.permission_denied` | 403 | viewer 或其他无管理权限角色。 |

所有错误响应保持 `code/message/request_id/trace_id/suggestion` 结构，未知 Endpoint 与跨租户 Endpoint 使用同一 404，不泄露资源是否存在。

## 安全与权限约束

1. 所有新字段和查询都显式带 `tenant_id`；API 不接受前端传 tenant，数据库读写使用 `tenant_id + endpoint_id + delivery_id`。
2. admin/developer 通过 `require_webhook_manager` 管理；viewer 和匿名由后端拒绝。
3. Secret 明文只在创建/轮换成功响应中出现一次；不得进入日志、审计 before/after、delivery payload、测试快照、浏览器持久存储或错误消息。
4. replay 的历史 `payload_json` 必须解析为对象并再次执行递归敏感键拒绝；禁止 Prompt、Response、messages、input、Authorization、Provider Key、Virtual Key、Token、password 或 secret。
5. 出站客户端继续 `trust_env=False`、不跟随重定向、使用有界超时和最多三次尝试；不读取或保存接收端响应正文。
6. replay 与 rotate 的管理审计只保存 ID、状态、版本和安全摘要；幂等键只用于数据库唯一性，不写结构化日志。
7. replay 的数据库事实先于出站网络；Webhook 失败只改变 replay delivery 状态，不回滚已提交管理事实，也不反压 Gateway 数据面热路径。
8. Secret 轮换和审计同事务；任一写入失败必须整体 rollback，不能出现 Secret 已换但审计缺失。
9. 轮换后已在其他线程中开始发送的请求可能已经持有旧密钥快照；验收定义为“轮换事务成功后新创建的 delivery 使用新版本”，不宣称能撤回已经发出的 HTTP 请求。
10. 不新增对象存储、死信桶、跨境链路、MCP、硬件或信创依赖。

## 测试用例

### 数据迁移

1. 空库执行 0001-0031 成功，重复执行幂等。
2. 0030 历史 Endpoint/delivery/attempt 升级后数据、租户、索引和外键完整。
3. original 事件唯一索引继续阻止重复原始投递。
4. replay 相同来源和幂等键唯一，不同幂等键允许创建不同 replay。
5. 0031 down 删除 replay 数据并恢复 PRD-025 表结构，保留 original delivery/attempt。
6. `PRAGMA integrity_check` 和 `foreign_key_check` 通过。

### 后端主流程

1. admin/developer 按事件类型、状态查询指定 Endpoint 最近 100 条，排序稳定且不返回 payload 正文/secret/幂等键。
2. failed delivery 重放成功，新 delivery 保留原 `event_id/event_type/payload`，拥有新 delivery ID 和 attempts。
3. 接收端失败时 replay 最多三次并保留失败状态、HTTP/固定错误摘要。
4. 同一幂等键串行两次请求返回同一 replay delivery，只收到一组网络尝试。
5. 并发相同幂等键只允许一个请求创建和发送，另一个复用同一 delivery。
6. Secret 轮换返回一次性新 secret，版本加一；使用同一 body、同一固定签名时间戳分别计算/校验，轮换后测试/重放签名可由新 secret 验证，旧 secret 验证失败。
7. replay/rotation 响应和错误都包含 request_id/trace_id；数据库记录 tenant、actor 和版本正确。
8. 管理审计包含 replay/rotate 动作，摘要不含 secret、URL、payload 或幂等键。
9. 轮换后控制面测试投递、控制面 replay 和 shared runtime 原始治理事件都把 Endpoint 当前 `secret_version` 快照写入 delivery。

### 后端异常、权限与安全

1. 匿名返回 401，viewer 返回 `auth.permission_denied`。
2. 其他租户 Endpoint/delivery 返回同一 not-found 错误，且数据库无写入、接收端无请求。
3. success/pending delivery、disabled Endpoint、非法 ID、非法筛选、limit > 100、非法幂等键均返回稳定错误码。
4. 请求体夹带 `tenant_id/user_id/signing_secret/payload` 被 DTO 拒绝。
5. 历史 payload 损坏、非对象或含递归敏感键时拒绝发送并返回统一错误，不泄露原文。
6. 加密失败、数据库约束失败和审计写入失败回滚 rotation；旧 secret 与版本保持不变。
7. HTTP 超时、网络错误和非 2xx 不保存响应正文、Header、Secret 或 URL。
8. `webhook.replayed` 安全事件不递归投递到来源 Endpoint。
9. 审计 action/object/summary 白名单拒绝任何未声明字段；审计失败时 replay intent 或 rotation 整体回滚且不开始网络发送。

### 前端

1. 客户端详情、replay、rotate 请求不包含 `tenant_id/user_id`；replay 只发送 delivery ID 和幂等键。
2. 页面展示 Endpoint 详情、事件/状态筛选、最多 100 条记录、failed 重放按钮和 Secret 版本。
3. success/pending 不展示重放按钮；viewer 不展示入口；后端权限测试仍独立存在。
4. 轮换确认、一次性新 Secret 展示、复制和隐藏状态正确，刷新后明文消失。
5. 操作错误展示统一错误和 request_id；Endpoint/错误/ID 全部转义。
6. 1280x720 与 390x844 下筛选、详情表、重放和轮换控件无重叠或横向溢出。

### 回归与运行验收

1. PRD-025 创建、列表、test、HMAC、三次重试和 viewer 权限测试继续通过。
2. shared runtime 原始治理事件幂等、预算/DLP/健康/Fallback/流式事件回归通过。
3. Python 全量、Node 示例、Console 全量与 lint 通过。
4. `scripts/ci/check-comments.py` 通过。
5. 运行库应用 0031，迁移记录和完整性检查通过。
6. Gateway、Control API、Console 健康检查通过；使用真实浏览器完成桌面/移动主流程、异常和权限验收。

## 代码修改计划

1. 完成本设计的两轮自审，先修正 ID 语义、租户/RBAC、幂等并发、迁移回滚和 Secret 生命周期风险。
2. 实现 0031 正反迁移和迁移测试，保证 PRD-025 历史数据可升级/回滚。
3. 扩展共享错误码、事件类型和原始投递兼容逻辑。
4. 扩展审计 action/object/summary 白名单；扩展 Webhook DTO、路由和服务：详情筛选、重放、轮换、审计、统一错误和安全日志。
5. 扩展前端 API、页面状态、筛选、重放、轮换和响应式样式。
6. 增加后端主流程、异常、权限、跨租户、签名轮换和串行/并发幂等测试。
7. 增加前端 API、渲染、交互、转义和角色测试，保留 PRD-025 回归。
8. 更新 README、目录边界和 `docs/testing/PRD-042-tests.md`，但只有所有自动化与运行验收通过后才把状态改为已完成。

## 设计自审 1

第一轮重点检查需求边界、兼容性、租户/RBAC 和安全语义，结论如下：

- **重要：内部事件与可订阅事件混用。** 现有 `WEBHOOK_EVENT_TYPES` 同时承担事件构造和 Endpoint 订阅白名单。若直接加入 `webhook.replayed`，控制台会允许订阅并可能制造额外失败或递归语义。设计已改为区分共享事件目录和可订阅目录，`webhook.replayed` 仅作为内部稳定事件与审计事实。
- **重要：并发幂等响应语义不清。** 首个请求在短事务提交后执行外部 HTTP，重复请求可能读到同一 replay delivery 的 `pending`。设计已明确相同幂等键只保证同一 delivery 和不重复出网；重复调用可返回 pending，并通过详情 API 观察终态，禁止为了等待外部 HTTP 长时间占用 SQLite 写事务。
- **重要：Secret 轮换验收可能产生假阳性。** 若用不同 body 或过期时间戳测试，新旧签名都会失败，无法证明轮换生效。测试已固定为同一 body 和固定时间戳，只改变 secret。
- **边界：历史事件过滤不应依赖当前订阅。** 已保留独立详情查询，防止 Endpoint 取消某事件订阅后历史 delivery 消失。
- **权限与租户：路径 Endpoint 和请求体 delivery 必须三者联查。** 已要求所有查询使用认证租户，并对跨 Endpoint/跨租户统一返回 not-found。
- **兼容性：原始 event_id 必须继续幂等。** 已采用 original/replay 两组部分唯一索引，避免破坏 shared runtime 的 `INSERT OR IGNORE` 合同。

## 设计自审 2

第二轮重点检查数据完整性、事务边界、失败恢复、审计和实际运行风险，结论如下：

- **阻断：审计白名单未纳入修改计划。** PRD-034 的 `record_audit_log` 会严格校验 action、object_type 和摘要字段；只调用审计函数会使 replay/rotation 全部返回 `audit_log.summary_invalid`。设计已补充两个稳定动作、两个对象类型及最小安全摘要白名单，并要求审计失败与业务写入一起回滚。
- **阻断：Secret 版本默认值会产生错误追踪。** 迁移默认 1 只能覆盖历史数据；轮换后两条原始投递路径若不显式复制 Endpoint 当前版本，delivery 会继续显示版本 1。设计已要求控制面与 shared runtime 的所有创建路径显式写 `secret_version`。
- **重要：控制面发送会隐式读取响应正文。** 现有 `httpx.Client.post` 与 shared runtime 的 streaming 行为不同，客户接收端可能返回敏感或超长正文。设计已要求 replay 复用现有 attempt/update 原语，并把控制面发送改为 streaming，只读取状态码。
- **重要：重放不能长事务包住外部 HTTP。** 设计保持“replay intent + 审计”短事务先提交，再出网；同幂等键并发请求复用已存在 delivery，允许暂时返回 pending，避免 SQLite 写锁覆盖网络等待。
- **重要：迁移回滚必须处理 replay attempts。** down 顺序已固定为先移除 replay attempts、再移除 replay deliveries、最后恢复 PRD-025 表结构，避免悬空外键。
- **安全：历史 payload 不能因为已经落库就被信任。** 设计要求在 shared runtime 提供递归敏感键验证，并在原始事件与 replay 发送前共同复用，避免控制面与数据面规则漂移。
- **运行验收：轮换必须覆盖三条实际签名路径。** 已补充 control test、control replay 和 shared runtime 原始治理事件的 secret version 与验签回归；页面验收同时覆盖桌面和移动端。

## 实现结果与最终边界

### 已落地能力

1. `0031_webhook_replay_secret_rotation` 为 Endpoint 增加 Secret 版本和轮换操作者/时间，为 delivery 增加 original/replay 类型、来源、操作者、幂等键和 Secret 版本快照，并通过部分唯一索引与触发器保护租户归属。
2. 控制面新增 `GET /webhooks/{id}/deliveries`、`POST /webhooks/{id}/replay` 和 `POST /webhooks/{id}/rotate-secret`；所有路径继续由后端 `require_webhook_manager` 执行 admin/developer 权限和认证租户过滤。
3. replay intent、`webhook_delivery.replayed` 管理审计与安全 `webhook.replayed` 内部事实先在短事务中提交，再执行最多三次的出站 HTTP；attempt 与 delivery/Endpoint 摘要在单事务中更新。
4. 共享运行时和控制面发送都在创建 intent 前重读当前 Endpoint Secret 与版本，并复用递归敏感键、事件信封一致性和 HMAC 规则；发送端只读取接收端 HTTP 状态，不下载响应正文。
5. Secret 轮换和审计同事务提交；新明文只在轮换响应返回一次。列表、详情、审计、日志、事件和后续响应只展示版本、前缀或指纹等安全元数据。
6. 控制台提供事件/状态筛选、固定 100 条上限、显式刷新、failed 重放、轮换确认和响应式字段标签。一次性 Secret、错误、反馈、旧 request_id 与未决幂等键在登出或用户/租户/Session 变化时共同清除，且不写浏览器持久存储。
7. 控制台定时刷新在已有刷新执行时跳过，避免慢环境下 15 秒周期持续淘汰交互刷新；较新的用户操作仍通过 `refreshSequence` 覆盖更早结果。

### 验收结果摘要

- Webhook 专项 `8 passed`；Python 完整收集清单拆分回归 `239/239`，Node 示例 `33/33`，最终控制台 `159/159`，lint 94 文件和中文注释门禁通过。
- 运行库已应用 31 条迁移，最高 `0031`，`PRAGMA integrity_check=ok`，`foreign_key_check` 为空；Gateway、Control API、Console 健康检查均为 HTTP 200。
- 真实 HTTP 验收确认：来源失败三次、同键只出网一次、不同键可再次重放、轮换前旧 Secret 验签成功、轮换后新 Secret 成功且旧 Secret 失败；viewer/匿名分别返回 403/401，跨租户详情/重放/轮换统一返回 404。
- 1280x720 与 390x844 浏览器验收均无根节点横向溢出、控件越界或按钮截断；78 个响应式字段均显示伪标签，viewer 看不到导航、面板或直接 hash 页面。

### 明确保留给 PRD-057 的边界

replay intent 和审计提交后才出网。如果进程在两者之间崩溃，记录可能停留在 `pending`；相同幂等键只回读该记录，不接管再次发送。因此 PRD-042 对单个幂等键提供 at-most-once 出网语义，不提供进程崩溃后的持久接管。恢复扫描、`dead_letter`、对象存储、归档和过期策略由 PRD-057 的 TC-0113/TC-0114 实现，不能把该后续范围伪装成本 PRD 已完成能力。
