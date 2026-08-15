# SIEM Webhook 与安全事件格式 API

本文档冻结 `PRD-081 - SIEM/安全事件格式` 的公开合同。能力复用现有 Webhook Endpoint、HMAC、重试、
死信与持久 replay，只增加 JSON/CEF wire、标准化安全事件、5 秒聚合和 Endpoint 独立限流；不提供
Splunk、QRadar、ArcSight、Sentinel 等厂商专有 API。

详细数据、并发与恢复边界见
[PRD-081 设计文档](../prd/PRD-081-siem-security-event-format.md)。

## 1. 路由、权限与租户边界

| 方法与路径 | 后端权限 | PRD-081 行为 |
| --- | --- | --- |
| `GET /webhooks` | `webhook.manage` | 返回 Endpoint 的 `format` 与安全事件分钟上限 |
| `POST /webhooks` | `webhook.manage` | 创建 JSON/CEF Endpoint |
| `POST /webhooks/{id}/test` | `webhook.manage` | 按 Endpoint format 发送固定测试事件 |
| `GET /webhooks/{id}/deliveries` | `webhook.manage` | 返回冻结 format/rate 与安全 attempt 摘要 |
| `POST /webhooks/{id}/replay` | `webhook.manage` | 沿用来源 delivery 的冻结 wire |
| `POST /webhooks/{id}/rotate-secret` | `webhook.manage` | 轮换当前 Endpoint HMAC Secret |

所有路由逐请求从 Console Session 恢复 tenant、actor 与实时权限；前端导航、隐藏按钮、角色名和请求正文
不能补权。跨租户 Endpoint/delivery 与不存在对象同形处理。

所有成功和错误响应携带服务端生成的 `request_id`、`trace_id`，整个 Webhook 路径设置：

```http
Cache-Control: no-store, private
```

客户端链路 Header 不用于 Webhook 写路径的公开 request/trace。错误使用统一 envelope，调用方必须按
HTTP 状态和 `code` 分支，不能解析 message。

## 2. `POST /webhooks` exact wire

路由先完成认证与 `webhook.manage`，再读取正文。未认证或无权限请求即使正文畸形也稳定返回 401/403。

- 只接受明确的 `application/json` 或 `+json` Content-Type。
- 正文最多 16 KiB，必须是严格 UTF-8 JSON object。
- 最大深度 8、最多 256 个节点；任意层重复 key、未知字段、数组/标量顶层、NaN/Infinity 均拒绝。
- Pydantic 使用 strict 类型；`true/false` 不能冒充整数。
- `tenant_id`、actor、Secret、Secret version、max attempts、delivery/outbox 字段只能由服务端决定。

请求字段：

| 字段 | 规则 |
| --- | --- |
| `name` | 既有 Webhook 名称合同 |
| `endpoint_url` | 既有安全 URL 合同；不跟随重定向 |
| `event_types` | 服务端稳定可订阅事件列表 |
| `status` | `active/disabled`，缺省 `active` |
| `timeout_ms` | 既有有界超时合同 |
| `format` | 可选 `json/cef`，缺省 `json` |
| `security_event_rate_limit_per_minute` | 可选原生整数 `1..600`，缺省 `60` |

CEF Endpoint 的 `event_types` 必须恰好为：

```json
["security.event_pushed"]
```

JSON Endpoint 可订阅任何服务端允许的既有事件，也可订阅 `security.event_pushed`。CEF 空订阅、混合订阅
或其他事件返回 `422 webhook.format_invalid`；非法限流返回
`422 webhook.security_rate_limit_invalid`。

成功响应 exact 顶层字段：

```json
{
  "webhook": {},
  "signing_secret": "仅本次返回",
  "request_id": "服务端生成",
  "trace_id": "服务端生成"
}
```

`webhook` 在既有 Endpoint 字段上增加：

```json
{
  "format": "json",
  "security_event_rate_limit_per_minute": 60
}
```

`signing_secret` 只在创建或轮换成功响应中出现一次；列表、delivery、日志、Audit、DOM 持久状态和验收证据
均不得返回它。

## 3. `SecurityEventPayload`

公开信封固定八个字段；签名只在 Header 中表达：

```json
{
  "event_id": "sevt_<32 lowercase hex>",
  "event_type": "security.event_pushed",
  "timestamp": "窗口结束 UTC 秒",
  "tenant_id": "服务端 owner",
  "actor_id": "security-event-aggregator",
  "object_id": "与 event_id 相同",
  "trace_id": "最后一条 source trace",
  "payload": {}
}
```

`payload` 必须恰好包含：

| 字段 | 合同 |
| --- | --- |
| `schema_version` | 原生整数 `1` |
| `security_event_id` | 与信封 `event_id/object_id` 相同 |
| `status` | `aggregated` |
| `category` | `data_loss_prevention/session_risk/provider_credential` |
| `source_event_type` | `security.dlp_matched/security.session_high_risk/security.provider_key_anomaly` |
| `severity` / `severity_score` | 固定映射的 `low/medium/high/critical` 与整数 `0..10` |
| `action` / `outcome` | 由 source 类型固定映射，调用方不能提交 |
| `resource_type` / `resource_id` | 安全资源类型与同租户不透明 ID；禁止 Credential ID/Key prefix |
| `event_count` | 窗口内 occurrence 正整数 |
| `source_event_count` | 成员 source 正整数，最大 1000 且不大于 `event_count` |
| `first_seen_at` / `last_seen_at` | 窗口内首尾 UTC 秒 |
| `window_started_at` / `window_ended_at` | 精确 5 秒半开窗口 |
| `first_request_id` / `last_request_id` | 首尾安全请求坐标，不返回列表 |
| `created_at` / `updated_at` | 聚合创建与最后更新时间 |

三类 source 映射只来自已提交的 DLP、会话风险与 Provider credential 事实。事件不包含规则 pattern、命中文本、
Prompt、Response、Provider Key、Credential ID、Authorization、Endpoint URL、签名或接收端响应正文。

## 4. JSON 与 CEF wire

### 4.1 JSON

- `Content-Type: application/json`。
- UTF-8 canonical JSON：key 排序、无多余空白、禁止 NaN/Infinity。
- 冻结 `wire_body_text` 逐字等于逻辑 `payload_json`。

### 4.2 CEF

- `Content-Type: text/plain; charset=utf-8`。
- 一条 delivery 恰好一行，无 CR/LF 终止符，UTF-8 最大 16,384 bytes；超限零网络失败关闭。
- Header 固定为
  `CEF:0|MetaHub|AI Gateway|1|<source_event_type>|<fixed name>|<severity_score>|...`。
- `rt/start/end` 使用 Unix epoch milliseconds 十进制整数。
- extension 顺序固定为 `externalId, rt, start, end, cnt, cat, act, outcome, cs1..cs6, cn1` 及对应 Label。
- Header 转义 `\\`、`|`；extension value 转义 `\\`、`=`、CR、LF。formatter 必须先通过 exact
  payload/envelope validator，再执行转义。

`webhook.test` 在 CEF Endpoint 上使用固定 `Webhook test` 单行，不冒充生产
`security.event_pushed`，也不消耗安全事件 rate claim。

## 5. HMAC 与投递 Header

每次真实投递固定发送：

```http
X-MetaHub-Event: <冻结 event_type>
X-MetaHub-Delivery: <稳定 delivery id>
X-MetaHub-Signature: t=<epoch>,v1=<HMAC-SHA256>
```

HMAC 输入是本次实际发送的 JSON/CEF UTF-8 bytes，不是重新序列化对象。Endpoint Secret 可轮换；format、
wire body 与 rate 在 delivery 创建时永久冻结，而真实 attempt 在限流通过后、网络前以事务重读当前 Secret
版本并签名。响应正文不读取、不保存。

## 6. 聚合、限流与恢复

- 同 tenant、category/source、severity/action/outcome、resource 与 5 秒窗口聚合。
- 单 generation 最多 1000 个 source；满额在同窗口开启下一 generation。
- Endpoint 按 UTC 分钟独立限流，只限制 `security.event_pushed`；达到上限时 delivery 保持 pending，
  不增加 attempt、不发网络、不伪造失败。
- rate claim 保留 30 天，只有关联 delivery 终态后才可清理；pending 证据不能删除。
- source/aggregate/outbox 和 delivery intent 均持久化。Control 停服或 lease 过期后由新 owner CAS 接管；
  stale worker 不能继续出网或覆盖终态。
- replay 逐字复制来源 format/wire/rate；限流延迟保持 `replay_pending`，只有真实 delivery 终态推进根死信。
- 网络语义是稳定 event/delivery ID 的 at-least-once；接收端必须按稳定 ID 幂等。

## 7. 列表与 PAGE-113 安全摘要

`GET /webhooks`、创建响应和 delivery 摘要增加 `format`；Endpoint 与 delivery 都返回
`security_event_rate_limit_per_minute`。PAGE-113 可展示这些冻结元数据，但不得展示：

- `wire_body_text`、CEF/JSON 正文或完整 payload；
- signing Secret、签名 Header、加密 Secret 或 rate claim；
- source request 列表、aggregate member、Credential ID 或接收端响应正文。

viewer 或缺少 `webhook.manage` 的用户后端返回 403，前端不渲染页面 DOM、不预取；身份、权限或 Session
变化必须清除一次性 Secret、反馈和旧 owner 状态。

## 8. 稳定错误

| code | HTTP | 场景 |
| --- | ---: | --- |
| `webhook.format_invalid` | 422 | format 未知、CEF 订阅不唯一或 wire 合同不成立 |
| `webhook.security_rate_limit_invalid` | 422 | 限流不是原生整数或不在 1..600 |
| `security_event.unavailable` | 503 | source/aggregate/outbox/scheduler 无法可靠恢复 |
| `webhook.delivery_unavailable` | 503 | 冻结 delivery、Secret 或发送状态不可安全使用 |
| `auth.permission_denied` | 403 | 后端实时权限不足 |

认证、Endpoint、重试、死信、replay 和 Secret 轮换的其他错误继续使用既有 Webhook 稳定 code；任何错误都
不得包含请求正文、URL、Secret、签名、Provider Key、Prompt、Response、SQL 或底层异常文本。
