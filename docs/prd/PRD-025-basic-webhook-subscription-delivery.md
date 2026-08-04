# PRD-025 - 基础事件订阅与推送

状态：已实现并通过 PRD-025 聚焦测试；全量回归见 `docs/testing/PRD-025-tests.md`。

## 需求理解

PRD-025 属于 M0-W2/P0 的 Webhook 基础能力，目标是让企业客户系统可以订阅 MetaHub 的治理事件，并通过 HTTP Endpoint 接收事件推送。Excel 明确范围包括 Webhook Endpoint 注册、事件类型订阅、HMAC 签名、发送重试 3 次和发送日志；验收要求测试 Webhook 可发送，事件必须包含 `event_id`、`timestamp`、`tenant_id`、`payload`，失败必须有重试记录。

M0 不做死信桶、SIEM 格式、批量聚合、复杂重放队列或外部事件总线。系统架构把 Webhook 归入控制面低频治理能力和 Telemetry Emitter 能力；本 PRD 先实现数据库持久化、控制面管理 API、同步测试投递和可复用投递服务，为预算、DLP、调用失败等事件接入提供统一 Envelope。

## 涉及模块

- `infra/migrations/`
  - 新增 `0017_webhooks.sql` 与 down 迁移。
  - 新增 `webhook_endpoints`、`webhook_deliveries`、`webhook_delivery_attempts`。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 增加 Webhook 管理、签名、投递和测试相关错误码。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`
  - 新增事件 Envelope、事件类型白名单、HMAC 签名和安全 JSON 序列化工具。
- `control-plane-api/src/metahub_control/auth/dependencies.py`
  - 新增 `require_webhook_manager`，M0 允许 admin/developer 管理 Webhook，viewer 后端拒绝。
- `control-plane-api/src/metahub_control/webhooks/`
  - 新增 DTO、路由、服务和投递执行逻辑。
  - 注册 `GET /webhooks`、`POST /webhooks`、`POST /webhooks/{id}/test`。
- `control-plane-api/src/metahub_control/console/service.py`
  - 新增 Webhook 设置页导航，admin/developer 可见。
- `control-plane-api/src/metahub_control/webhooks/service.py`
  - 提供 `dispatch_webhook_event` 统一投递服务，预算/DLP/调用失败等生产事件后续接入时复用；PRD-025 M0 先以 `webhook.test` 验证端到端推送、签名和重试。
- `console-web/src/lib/webhooks.js`
  - 新增 Webhook API client。
- `console-web/src/features/webhooks/renderWebhookPanel.js`
  - 新增 Webhook 设置页和发送记录展示。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-025-tests.md`
  - 更新能力边界、API、数据表和测试记录。

## 数据表/字段设计

### 新表：`webhook_endpoints`

用途：保存当前租户的 Webhook Endpoint 配置和订阅事件类型。

- `webhook_endpoint_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `name TEXT NOT NULL`
- `endpoint_url TEXT NOT NULL`
- `event_types_json TEXT NOT NULL`
- `encrypted_secret TEXT NOT NULL`
- `secret_prefix TEXT NOT NULL`
- `secret_fingerprint TEXT NOT NULL`
- `status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled'))`
- `timeout_ms INTEGER NOT NULL DEFAULT 5000 CHECK (timeout_ms >= 1000 AND timeout_ms <= 30000)`
- `max_attempts INTEGER NOT NULL DEFAULT 3 CHECK (max_attempts >= 1 AND max_attempts <= 3)`
- `last_delivery_status TEXT`
- `last_delivery_at TEXT`
- `last_success_at TEXT`
- `last_failure_at TEXT`
- `created_by TEXT`
- `updated_by TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

约束与索引：

- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `FOREIGN KEY (created_by) REFERENCES users(user_id)`
- `FOREIGN KEY (updated_by) REFERENCES users(user_id)`
- `idx_webhook_endpoints_tenant_status(tenant_id, status)`
- `idx_webhook_endpoints_tenant_secret(tenant_id, secret_fingerprint)`

安全说明：

- `encrypted_secret` 使用现有 AES-GCM 密钥能力加密保存；API 只在创建响应返回一次明文 `signing_secret`，列表和发送记录永不返回。
- `endpoint_url` 是管理员/开发者配置数据，可在控制台展示，但普通结构化日志不输出完整 URL，避免 URL query 中的客户自带 token 泄露。

### 新表：`webhook_deliveries`

用途：保存每个 Endpoint 对某个事件的投递摘要，支撑发送记录页、重试状态和排障。

- `webhook_delivery_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `webhook_endpoint_id TEXT NOT NULL`
- `event_id TEXT NOT NULL`
- `event_type TEXT NOT NULL`
- `event_timestamp TEXT NOT NULL`
- `payload_json TEXT NOT NULL`
- `status TEXT NOT NULL CHECK (status IN ('pending', 'success', 'failed'))`
- `attempt_count INTEGER NOT NULL DEFAULT 0`
- `max_attempts INTEGER NOT NULL DEFAULT 3`
- `http_status INTEGER`
- `duration_ms INTEGER`
- `error_code TEXT`
- `error_summary TEXT`
- `next_retry_at TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

约束与索引：

- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `FOREIGN KEY (webhook_endpoint_id) REFERENCES webhook_endpoints(webhook_endpoint_id)`
- `UNIQUE (tenant_id, webhook_endpoint_id, event_id)`
- `idx_webhook_deliveries_endpoint_time(tenant_id, webhook_endpoint_id, created_at DESC)`
- `idx_webhook_deliveries_event(tenant_id, event_id)`
- `idx_webhook_deliveries_status_time(tenant_id, status, created_at DESC)`

### 新表：`webhook_delivery_attempts`

用途：保存每次投递尝试，满足“失败有重试记录”的验收要求。

- `webhook_delivery_attempt_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `webhook_delivery_id TEXT NOT NULL`
- `webhook_endpoint_id TEXT NOT NULL`
- `attempt_no INTEGER NOT NULL`
- `status TEXT NOT NULL CHECK (status IN ('success', 'failed'))`
- `http_status INTEGER`
- `duration_ms INTEGER`
- `error_code TEXT`
- `error_summary TEXT`
- `created_at TEXT NOT NULL`

约束与索引：

- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `FOREIGN KEY (webhook_delivery_id) REFERENCES webhook_deliveries(webhook_delivery_id)`
- `FOREIGN KEY (webhook_endpoint_id) REFERENCES webhook_endpoints(webhook_endpoint_id)`
- `UNIQUE (tenant_id, webhook_delivery_id, attempt_no)`
- `idx_webhook_attempts_delivery(tenant_id, webhook_delivery_id, attempt_no)`

## API设计

所有 Webhook 管理 API 必须携带 `Authorization: Bearer <token>`，并从认证上下文恢复 `tenant_id`，请求体和查询参数不接受 `tenant_id`。

### `GET /webhooks`

权限：admin/developer。

查询参数：

- `status`：`active`、`disabled`、`all`，默认 `all`。
- `event_type`：可选，用于筛选订阅了某个事件类型的 Endpoint。
- `include_deliveries`：布尔值，默认 `true`；M0 返回每个 Endpoint 最近 20 条发送记录，支撑“发送记录”页面。

响应：

```json
{
  "webhooks": [
    {
      "webhook_endpoint_id": "wh_...",
      "tenant_id": "tenant_...",
      "name": "预算告警接收器",
      "endpoint_url": "https://example.com/metahub/webhook",
      "event_types": ["budget.threshold_reached", "budget.exhausted"],
      "secret_prefix": "whsec_xxxx",
      "status": "active",
      "timeout_ms": 5000,
      "max_attempts": 3,
      "last_delivery_status": "success",
      "recent_deliveries": []
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### `POST /webhooks`

权限：admin/developer。

请求：

```json
{
  "name": "预算告警接收器",
  "endpoint_url": "https://example.com/metahub/webhook",
  "event_types": ["budget.threshold_reached", "budget.exhausted", "security.dlp_matched"],
  "status": "active",
  "timeout_ms": 5000
}
```

响应：返回创建后的 Endpoint，并额外返回一次性 `signing_secret`。

验证：

- `name` 必填，最大 120 字符。
- `endpoint_url` 必须是 `http` 或 `https`，最大 2048 字符；生产环境默认要求 `https`；开发和测试环境允许本地 `http://127.0.0.1` 或 `http://localhost` 接收端。
- URL 不允许包含用户名密码，不允许 `file://`、`ftp://` 等非 HTTP 协议。
- `event_types` 必须来自白名单，至少 1 个；`webhook.test` 是测试专用事件，不允许保存为订阅类型。
- M0 每租户最多 20 个 Webhook Endpoint。

### `POST /webhooks/{id}/test`

权限：admin/developer。

行为：生成 `webhook.test` 事件，使用目标 Endpoint 的签名密钥立即发送，最多尝试 3 次；返回 delivery 和 attempts 摘要。

响应：

```json
{
  "delivery": {
    "webhook_delivery_id": "whd_...",
    "event_id": "evt_...",
    "event_type": "webhook.test",
    "status": "success",
    "attempt_count": 1,
    "max_attempts": 3,
    "http_status": 200,
    "attempts": [
      {
        "attempt_no": 1,
        "status": "success",
        "http_status": 200,
        "duration_ms": 42
      }
    ]
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### Webhook HTTP Payload

发送 Body 使用稳定 JSON：

```json
{
  "event_id": "evt_...",
  "event_type": "budget.exhausted",
  "timestamp": "2026-07-10T10:00:00Z",
  "tenant_id": "tenant_...",
  "actor_id": "optional_user_or_key",
  "object_id": "budget_rule_or_request_id",
  "trace_id": "trc_...",
  "payload": {
    "safe_summary": "only safe metadata"
  }
}
```

Headers：

- `Content-Type: application/json`
- `User-Agent: MetaHub-Webhook/<env>`
- `X-MetaHub-Event`
- `X-MetaHub-Delivery`
- `X-MetaHub-Signature: t=<unix_seconds>,v1=<hex_hmac_sha256>`

签名输入：`f"{timestamp}.{body_bytes}"`，密钥为 Endpoint 的 `signing_secret`。接收方使用恒定时间比较校验签名。

## 前端页面/交互设计

### Webhook 设置页

入口：控制台侧边栏新增“Webhook 设置”，归类到“开发者与集成”或“安全与治理”。M0 admin/developer 可见，viewer 不展示；后端仍做权限校验。

页面内容：

- 顶部摘要：Endpoint 总数、active 数、最近成功数、最近失败数。
- 创建表单：
  - 名称
  - Endpoint URL
  - 事件类型多选
  - 状态
  - 超时毫秒
- Endpoint 列表：
  - 名称、URL、订阅事件、状态、secret prefix、最近投递状态、最近成功/失败时间。
  - 创建成功后展示一次性 `signing_secret`，页面刷新后不再显示。
- 发送记录：
  - 最近 delivery 的 `event_id`、`event_type`、状态、尝试次数、HTTP 状态、耗时、错误码和时间。
  - 不展示签名密钥、完整 Authorization、Provider Key、Virtual Key 明文、Prompt 或 Response。
- 测试按钮：
  - 对单个 Endpoint 触发 `POST /webhooks/{id}/test`。
  - 展示每次尝试记录和最终状态。

## 错误码设计

- `webhook.endpoint_invalid`：422，Endpoint 字段非法，如 URL scheme 不支持、名称为空或超出限制。
- `webhook.endpoint_not_found`：404，Endpoint 不存在或不属于当前租户。
- `webhook.event_type_invalid`：422，订阅事件类型不在 M0 白名单内。
- `webhook.secret_unavailable`：503，签名密钥无法生成、加密或解密。
- `webhook.limit_exceeded`：422，当前租户 Endpoint 数量超过 M0 上限。
- `webhook.delivery_failed`：502，保留给后续生产事件投递 API 的失败错误；当前测试发送失败时返回 200，并在 `delivery.status=failed` 与 attempts 中展示失败原因。
- `webhook.delivery_unavailable`：503，投递服务或数据库不可用，发送记录无法可靠写入。

所有错误响应必须包含 `code`、`message`、`request_id`、`trace_id`、`suggestion` 和可选 `diagnosis`。

## 安全与权限约束

- 所有 Webhook Endpoint、Delivery 和 Attempt 记录必须带 `tenant_id`。
- Webhook 管理 API M0 允许 admin/developer，viewer 后端拒绝；控制台隐藏按钮不算权限。
- `signing_secret` 只在创建响应返回一次；数据库只保存密文、prefix 和 fingerprint。
- 普通结构化日志不输出完整 `endpoint_url`、`signing_secret`、签名头、请求体原文或客户响应正文。
- 投递 payload 必须使用安全摘要，不包含 Prompt、Response、messages 原文、Authorization、Virtual Key 明文、Provider Key 或 DLP 命中文本。
- 出站 HTTP 客户端使用 `trust_env=False`，避免系统代理污染客户内网 Endpoint 或本地测试 Endpoint。
- URL 校验拒绝非 HTTP 协议和 URL 用户名密码；生产环境默认要求 HTTPS。
- Webhook 投递失败不阻断原始网关请求；预算/DLP/调用失败事件先保证业务日志落库，再进行 Webhook 投递或记录投递不可用。
- `webhook.delivery_failed` 在 M0 写入发送记录和结构化事件；避免递归向同一失败 Endpoint 无限发送失败事件。

## 测试用例

### 迁移

1. 空库迁移到 `0017` 成功，创建 `webhook_endpoints`、`webhook_deliveries`、`webhook_delivery_attempts`。
2. 所有新增业务表包含 `tenant_id` 和必要索引。
3. down 迁移删除 Webhook 表，不影响 `dlp_*`、`budget_*` 和 request log 表。

### 控制面 API

1. admin/developer 创建 Webhook Endpoint 成功，响应只返回一次 `signing_secret`。
2. `GET /webhooks` 返回当前租户 Endpoint 和最近发送记录，不返回 secret 明文。
3. viewer 调用 `GET/POST /webhooks` 和 `POST /webhooks/{id}/test` 返回 `auth.permission_denied`。
4. 请求体带 `tenant_id` 不会改变 Endpoint 归属。
5. 非法 URL、非法事件类型、超过租户 Endpoint 上限返回统一错误码。
6. `POST /webhooks/{id}/test` 成功时假接收端能校验 HMAC 签名。
7. `POST /webhooks/{id}/test` 超时或 5xx 时最多尝试 3 次，并写入 3 条 attempt 记录。

### 事件投递

1. 预算 80%/100% 事件命中时生成安全 Webhook Envelope，包含 `event_id`、`timestamp`、`tenant_id`、`payload`。
2. DLP 命中事件只传规则 ID、动作、哈希摘要，不传命中文本。
3. 调用失败事件只传 request_id、错误码、模型/Provider 摘要，不传 Prompt、Response 或上游原始 body。
4. Webhook 投递失败不影响原业务请求结果，失败可在发送记录中追踪。

### 前端

1. admin/developer 可看到 Webhook 设置页、创建表单、发送记录和测试按钮。
2. viewer 不显示 Webhook 设置入口。
3. 创建表单不发送 `tenant_id`。
4. 创建后一次性显示 `signing_secret`，刷新或重新加载列表后不再显示。
5. Endpoint 名称、URL、错误消息和发送记录均 HTML 转义。

### 回归

1. `scripts/ci/check-comments.py` 必须通过。
2. PRD-025 聚焦后端测试、迁移测试、前端单测和控制台 lint 必须通过。
3. 后端全量 `pytest -q` 与前端全量 Node test 作为收口回归。

## 代码修改计划

1. 迁移：
   - 新增 `0017_webhooks.sql`、`0017_webhooks.down.sql`。
   - 更新迁移测试，校验新表、字段、索引和回滚顺序。
2. 共享运行时：
   - 新增 Webhook 事件类型白名单、Envelope 构造、HMAC 签名和安全 JSON 序列化。
   - 增加 Webhook 错误码和诊断建议。
3. 控制面权限：
   - 新增 `require_webhook_manager`，M0 admin/developer 可管理，viewer 拒绝。
4. 控制面 Webhook 模块：
   - 新增 `webhooks/models.py`、`webhooks/service.py`、`webhooks/routes.py`。
   - 实现创建、列表、测试发送、重试记录和安全日志。
   - 在 `app.py` 注册路由。
5. 事件接入：
   - PRD-025 已提供统一投递服务和事件类型白名单；预算事件、DLP 事件和调用失败摘要的生产触发点按后续相关 PRD 接入，避免本 PRD 扩大数据面热路径风险。
6. 前端：
   - 新增 `console-web/src/lib/webhooks.js`。
   - 新增 `console-web/src/features/webhooks/renderWebhookPanel.js`。
   - 更新 `renderDeveloperStart.js`、`main.js`、导航和 CSS。
7. 测试：
   - 新增控制面 Webhook 集成测试。
   - 新增 Webhook 签名单元测试和重试测试。
   - 更新迁移测试、导航测试和前端单测。
8. 文档：
   - 更新 README、目录边界文档。
   - 新增 `docs/testing/PRD-025-tests.md`。

## 自检一

- 是否满足 M0 范围：是，只做 Endpoint 注册、事件订阅、HMAC 签名、3 次重试和发送日志。
- 是否覆盖验收字段：是，Envelope 固定包含 `event_id`、`timestamp`、`tenant_id` 和 `payload`。
- 是否避免敏感信息泄露：是，secret 只一次性返回，payload 只放安全摘要，不记录 Prompt/Response/Key。
- 是否后端权限覆盖：是，使用 `require_webhook_manager`，viewer 后端拒绝。
- 是否失败可追踪：是，`webhook_deliveries` 与 `webhook_delivery_attempts` 保存最终状态和每次尝试。

## 自检二

- 最大安全风险：Webhook URL SSRF 与 secret 泄露。设计通过 URL scheme/凭据校验、生产 HTTPS 约束、密文存储、日志脱敏和出站客户端 `trust_env=False` 降低风险。
- 最大可靠性风险：同步重试拖慢热路径。设计要求先写业务日志，再进行有限尝试；后续用独立队列和死信桶升级。
- 最大一致性风险：事件来自控制面和数据面两个进程。设计把 Envelope、签名和事件类型放到 shared-sdk，避免两边格式漂移。
- 最大产品风险：M0 没有完整重放和死信。设计在发送记录中保存失败与 attempts，满足排障和验收，死信桶按 PRD 边界留后续。

## 设计审核一

- 结论：设计覆盖 Excel 主表、页面清单、API 清单、数据对象、Webhook 事件和验收用例。
- 调整：新增 `webhook_delivery_attempts`，虽然 Excel 只列出 WebhookDelivery，但这是满足“失败有重试记录”的最小可审计实现。
- 保留：M0 不新增 `PATCH/DELETE /webhooks`，避免超出 PRD；状态变更和删除可在后续 Webhook 增强 PRD 中加入。

## 设计审核二

- 结论：架构边界与系统架构文档一致，Webhook 作为控制面低频集成能力，不进入模型调用主链路的强依赖。
- 调整：`webhook.delivery_failed` 在 M0 先作为发送记录和结构化事件，不做递归投递，避免失败风暴。
- 保留：预算/DLP/调用失败事件先只发送安全摘要；不做 SIEM 格式、批量聚合、死信桶和复杂重放。

## 实现摘要

- 已新增 `0017_webhooks.sql`/down 迁移和 `webhook_endpoints`、`webhook_deliveries`、`webhook_delivery_attempts` 三张租户级表。
- 已新增 shared-sdk Webhook Envelope、事件类型白名单、稳定 JSON、HMAC 签名和验签工具。
- 已在控制面注册 `GET /webhooks`、`POST /webhooks`、`POST /webhooks/{id}/test`，并通过 `require_webhook_manager` 限定 admin/developer。
- 已在控制台新增“Webhook 设置”导航、创建表单、一次性 signing secret 展示、测试发送按钮和发送记录。
- 已补控制面集成测试、迁移测试、前端单测和中文注释门禁。

## M0 保留边界

- 当前 PRD 不实现死信桶、SIEM 格式、批量聚合、复杂重放队列或外部事件总线。
- 预算/DLP/调用失败等生产事件触发点暂不强行接入数据面热路径；后续 PRD 复用 `dispatch_webhook_event` 和 shared-sdk 事件信封逐步接入。