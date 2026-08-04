# Webhook 死信与持久重放 API

本文档冻结 PRD-057“Webhook 死信与持久重放”的公开 Control API、状态机、持久 replay
语义、权限与租户边界，以及 PAGE-083 可读取的安全字段。完整技术设计和两轮编码前自审见
[PRD-057 技术设计](../prd/PRD-057-webhook-dead-letter-persistent-replay.md)。

当前状态：PRD-057 已完成。实现、专项与回归、实际运行库 48/0048、真实 Runtime、Browser、
停服静止终态和正式 artifact 均已通过；`all_108_prds_complete=false`。

## 1. 能力边界

PRD-057 为已经进入 Webhook 投递链路并最终失败的 original delivery 建立独立
`DeadLetterEvent` 治理事实。它提供租户内查询、持久重放和逻辑归档，不删除原 delivery 或
attempt，也不把事件正文、Endpoint URL、签名或 Secret 暴露给控制台。

公开端点：

| 方法 | 路径 | 成功状态 | 后端角色 |
| --- | --- | ---: | --- |
| `GET` | `/webhook-dead-letters` | 200 | `admin`、`developer` |
| `POST` | `/webhook-deliveries/{webhook_delivery_id}/replay` | 200 | `admin`、`developer` |
| `POST` | `/webhook-dead-letters/{dead_letter_event_id}/archive` | 200 | `admin`、`developer` |

公开 API 使用 `Authorization: Bearer <session_token>`。匿名请求统一返回 401，`viewer` 统一
返回 403。`tenant_id`、actor、角色和 Session 只从后端认证上下文恢复，query、path 和正文
都不能覆盖。跨租户 ID 与不存在 ID 使用同形 404。

所有成功响应携带 `X-Request-ID`、`X-Trace-ID`，正文中的 `request_id`、`trace_id` 与响应头
一致，并设置 `Cache-Control: no-store`。统一错误响应同样携带链路 ID。

本能力不新增对象存储、SIEM、MCP、硬件、信创或复杂数据出境链路。M1 只做数据库内逻辑归档
和固定保留期；外部冷归档属于 M2 可选模块。

## 2. DeadLetterEvent 与状态机

死信摘要示例：

```json
{
  "dead_letter_event_id": "whdl_0123456789abcdef0123456789abcdef",
  "webhook_delivery_id": "whd_0123456789abcdef0123456789abcdef",
  "webhook_endpoint_id": "wh_0123456789abcdef0123456789abcdef",
  "webhook_endpoint_name": "审计接收端",
  "event_id": "evt_01JABCDEF0123456789ABCDE",
  "event_type": "gateway.request_failed",
  "status": "dead_letter",
  "failure_error_code": "webhook.http_error",
  "failure_http_status": 500,
  "failed_at": "2026-07-30T08:00:00Z",
  "expires_at": "2026-08-29T08:00:00Z",
  "current_replay_delivery_id": null,
  "replay_requested_at": null,
  "replay_dispatch_attempts": 0,
  "last_replay_error_code": null,
  "replayed_at": null,
  "archived_at": null,
  "created_at": "2026-07-30T08:00:00Z",
  "updated_at": "2026-07-30T08:00:00Z",
  "latest_attempt": {
    "attempt_no": 3,
    "status": "failed",
    "http_status": 500,
    "duration_ms": 12,
    "error_code": "webhook.http_error",
    "created_at": "2026-07-30T08:00:00Z"
  },
  "current_replay_delivery": null,
  "replayable": true,
  "allowed_actions": ["replay", "archive"]
}
```

公开状态只有：

```text
dead_letter -> replay_pending -> replayed
            -> replay_pending -> dead_letter
dead_letter -> archived
replayed    -> archived
dead_letter -> expired
replayed    -> expired
```

- `dead_letter`：original delivery 最终失败，可重放、可归档。
- `replay_pending`：持久 replay intent 已提交，不能同时归档或再用新键重放。
- `replayed`：当前 replay delivery 成功，可归档但不能再次从同一根死信创建新 intent。
- `archived`：逻辑归档终态，delivery、attempt 和审计证据仍保留。
- `expired`：达到固定 30 天保留边界后的终态，不可重放或归档。

`replayable` 与 `allowed_actions` 由后端根据持久状态和 `expires_at` 计算，前端不得自行放宽。
`webhook_deliveries.status` 继续只使用 `pending/success/failed`，避免破坏 PRD-025/042 兼容合同。

## 3. GET /webhook-dead-letters

允许的 query：

| 参数 | 默认值 | 约束 |
| --- | --- | --- |
| `status` | `all` | `all/dead_letter/replay_pending/replayed/archived/expired` |
| `event_type` | 空 | 必须来自服务端稳定 Webhook 事件目录 |
| `webhook_endpoint_id` | 空 | 最长 80 字符，不得含 NUL；只收窄当前租户查询 |
| `limit` | `50` | 1..100 |
| `cursor` | 空 | 最长 1024 字符的服务端 URL-safe seek cursor |

列表固定按 `failed_at DESC, dead_letter_event_id DESC` 排序。`cursor` 只编码这两个排序键，不含
tenant、payload、Endpoint URL 或内部 lease；客户端必须把它视为不透明值，并与同一组筛选条件
一起使用。非法 base64、JSON、键集合、时间、状态、事件、Endpoint 或 limit 统一返回
`webhook.dead_letter_filter_invalid`。

成功响应：

```json
{
  "dead_letters": [],
  "next_cursor": null,
  "filters": {
    "status": "all",
    "event_type": null,
    "webhook_endpoint_id": null,
    "limit": 50
  },
  "request_id": "req_control_01J...",
  "trace_id": "trc_control_01J..."
}
```

`filters` 是后端规范化回显，PAGE-083 必须用它更新当前查询状态。响应中的死信、最新 attempt
和 current replay delivery 均使用显式白名单 DTO，不返回 `tenant_id`、payload、URL、actor、
幂等键、Secret 版本、claim token、lease owner 或接收端错误正文。

## 4. POST /webhook-deliveries/{webhook_delivery_id}/replay

路径中的 ID 指 original `webhook_delivery_id`，不是 Endpoint ID。ID 为空、超过 96 字符、
不存在、跨租户或归属不匹配时统一返回 `webhook.dead_letter_not_found`。

### 4.1 严格请求 wire

后端先完成 Session、RBAC 和租户恢复，再读取正文。viewer 或匿名请求即使携带畸形 JSON、
超大正文或敏感字段，也先得到稳定 403/401。

请求要求：

- `Content-Type` 必须是 `application/json` 或 `+json`；
- 正文最大 4 KiB；重复/含糊 `Content-Length`、分块超限均在有界读取中拒绝；
- 必须是严格 UTF-8 JSON object；拒绝重复 key、NaN/Infinity、数组、标量和 `null`；
- 只允许 `idempotency_key`，未知字段、类型混淆和额外 tenant/payload/secret 字段统一拒绝；
- 幂等键必须匹配 `[A-Za-z0-9._:-]{8,128}`。

示例：

```json
{
  "idempotency_key": "console_replay_01JABCDEF0123456789"
}
```

严格 wire 错误返回 `infra.validation_failed`；幂等键缺失或格式错误返回
`webhook.idempotency_key_invalid`。响应和普通日志都不得回显正文、键值或解析异常。

### 4.2 持久化与幂等语义

服务端在一个数据库事务中完成：

1. 按认证 tenant 回读 source delivery、Endpoint 和根死信；
2. 重新解析并递归验证存量事件信封，拒绝损坏元数据或敏感键；
3. 创建新的 pending replay delivery；
4. 把根死信 CAS 推进为 `replay_pending`；
5. 写入 `webhook_delivery.replayed` 管理审计；
6. 提交后才允许任何出站 HTTP。

唯一幂等边界为 `tenant + endpoint + source_delivery + idempotency_key`。同键重试只回读原 replay
delivery，`replayed=false`，不写第二个 intent、不重复审计，也不触发根死信当前的其他 pending
任务。不同新键只有在根死信重新回到 `dead_letter` 后才可创建；已有活跃 intent 返回
`webhook.dead_letter_replay_pending`。

成功响应：

```json
{
  "delivery": {
    "webhook_delivery_id": "whd_fedcba9876543210fedcba9876543210",
    "webhook_endpoint_id": "wh_0123456789abcdef0123456789abcdef",
    "event_id": "evt_01JABCDEF0123456789ABCDE",
    "event_type": "gateway.request_failed",
    "event_timestamp": "2026-07-30T08:00:00Z",
    "status": "pending",
    "attempt_count": 0,
    "max_attempts": 3,
    "http_status": null,
    "duration_ms": null,
    "error_code": null,
    "next_retry_at": null,
    "delivery_kind": "replay",
    "replayed_from_delivery_id": "whd_0123456789abcdef0123456789abcdef",
    "replayed_at": "2026-07-30T08:05:00Z",
    "created_at": "2026-07-30T08:05:00Z",
    "updated_at": "2026-07-30T08:05:00Z",
    "attempts": []
  },
  "dead_letter": {},
  "replayed": true,
  "request_id": "req_console_replay_01J...",
  "trace_id": "trc_control_01J..."
}
```

API 会在提交后尽力即时领取刚创建的指定 intent 一次，因此响应中的 delivery 也可能已经是
`success` 或 `failed`。客户端必须以后端返回事实为准；网络结果不确定时只能复用原幂等键。

## 5. POST /webhook-dead-letters/{dead_letter_event_id}/archive

`dead_letter_event_id` 必须匹配 `whdl_<32 lower hex>`。非法、不存在和跨租户 ID 统一返回
`webhook.dead_letter_not_found`。接口不接收业务正文。

只有未到期的 `dead_letter` 或 `replayed` 可归档；`replay_pending`、`archived`、`expired` 或时间
边界已到期时返回 `webhook.dead_letter_not_archivable`。状态 CAS 与
`webhook_dead_letter.archived` 审计在同一事务提交，任一失败全部回滚。归档不删除 source/replay
delivery、attempt 或失败摘要。

成功响应：

```json
{
  "dead_letter": {
    "dead_letter_event_id": "whdl_0123456789abcdef0123456789abcdef",
    "status": "archived",
    "replayable": false,
    "allowed_actions": []
  },
  "request_id": "req_console_archive_01J...",
  "trace_id": "trc_control_01J..."
}
```

示例只省略了与第 2 节相同的安全字段；真实响应符合完整 `WebhookDeadLetterInfo`。

## 6. Worker、lease 与可靠性语义

Control API lifespan 安装串行 scheduler。空闲时每 2 秒轮询；每轮先过期到期死信，再至多领取
一个 `replay_pending`。claim 使用 tenant、死信 ID、current replay delivery、随机 token 和
worker owner 的组合 CAS，并计算 30..120 秒 lease；API 即时 drain 同时限定死信 ID 和刚创建的
replay delivery ID。

可分类失败释放 lease，并按 `1/5/30/300` 秒有界退避；进程异常保留 lease，过期后允许新 token
接管。完成提交再次校验完整 owner 五元组，旧 worker 失权后只能 no-op。归档/过期不会越过
`replay_pending` 竞态出网。

系统提供数据库可恢复的 at-least-once 语义，不承诺网络 exactly-once。若接收方已经成功处理，
而进程在本地 attempt 提交前退出，接管 worker 可能再次发送；重复发送继续携带相同业务
`event_id`，接收方必须按 `event_id` 幂等。新 replay 使用新的 `webhook_delivery_id` 和
`X-MetaHub-Delivery`。

## 7. 兼容与内部事件

旧 `POST /webhooks/{webhook_endpoint_id}/replay` 保留 PRD-042 的请求/响应和公开错误码，但内部
复用同一持久 replay intent 与 worker。旧同键请求同样是纯读取，不得借历史键领取另一条新任务。

`webhook.dead_lettered` 是内部稳定事实，用于日志和治理追踪，不进入租户 Endpoint 可订阅集合，
避免接收端故障递归制造新死信。事件只携带死信/source delivery/Endpoint/event 的不透明 ID、
固定错误码、HTTP 状态和时间，不携带正文、URL、Secret 或接收端响应。

## 8. 安全与隐私约束

- 所有查询、更新、CAS 和 owner 关系都显式包含 `tenant_id`；数据库使用复合 FK/trigger 拒绝跨租户。
- source delivery、Endpoint、replay actor 和 archive actor 必须属于认证 tenant。
- API DTO 使用显式字段复制，内部模型以后新增字段也不会自动扩散到 PAGE-083。
- replay 出网前重新执行存量信封递归敏感键检查和元数据一致性检查。
- HTTP 客户端不跟随重定向、`trust_env=false`、超时有界，接收端正文不进入成功或失败响应。
- 普通日志只记录不透明 ID、状态、固定错误码和 request/trace；不记录 payload、Prompt、Response、
  Authorization、Key、Token、Secret、签名、幂等键、claim token 或 lease owner。
- PAGE-083 不把筛选、资源 ID、敏感正文或幂等键写入 URL/Storage；viewer 无导航、DOM 或预取，
  但后端 RBAC 始终是最终权限边界。

## 9. 统一错误码

| 错误码 | HTTP | 可重试 | 场景 |
| --- | ---: | --- | --- |
| `auth.token_missing` / `auth.token_invalid` | 401 | 否 | 匿名、Token 无效或 Session 不可用 |
| `auth.permission_denied` | 403 | 否 | viewer 或其他无管理权限角色 |
| `webhook.dead_letter_not_found` | 404 | 否 | delivery/死信不存在、跨租户、归属不匹配或 ID 非法 |
| `webhook.dead_letter_not_replayable` | 409 | 否 | source 或根死信不是可重放状态、已归档或已过期 |
| `webhook.dead_letter_replay_pending` | 409 | 否 | 已有不同幂等键的 replay 正在恢复 |
| `webhook.dead_letter_not_archivable` | 409 | 否 | 当前状态或时间边界不允许归档 |
| `webhook.endpoint_disabled` | 409 | 否 | Endpoint 已停用，不能创建 replay intent |
| `webhook.dead_letter_filter_invalid` | 422 | 否 | 列表状态、事件、Endpoint、limit 或 cursor 非法 |
| `webhook.idempotency_key_invalid` | 422 | 否 | 幂等键不满足 8..128 字符 ASCII 白名单 |
| `infra.validation_failed` | 422 | 否 | replay JSON wire、媒体类型、大小、结构或字段非法 |
| `webhook.dead_letter_unavailable` | 503 | 是 | 死信查询、回读、CAS、归档或过期存储不可用 |
| `webhook.replay_unavailable` | 503 | 是 | replay intent、存量信封验证或发送恢复不可用 |

统一错误正文只返回固定 `code/message/suggestion/diagnosis/request_id/trace_id`，不得回显 SQL、
数据库路径、cursor、正文、幂等键、异常文本或凭据。

## 10. 最终验收证据

1. 隔离运行库为 `48/0048`，完整性正常、外键违规 0、PRD-057 trigger 9，0048 文件与数据库
   checksum 一致。
2. 真实 receiver 已通过 `500 x 3 -> dead_letter -> /ok -> replayed`，保持相同 `event_id`，同一
   幂等键零重复出网。
3. admin/developer 允许、viewer 403、匿名 401、第二租户同形 404，统一错误与 Header/Body
   request_id 通过；公开 DTO、HMAC 与日志去敏通过。
4. PAGE-083 已通过 1280x720 与 390x844 Browser、焦点、注销、viewer 零预取、网络白名单和
   隐私扫描，共 5 张非空截图。
5. 唯一一次根级 Python 全量为 `1496 = 1407 passed / 89 failed`；没有重跑根级全量，原失败节点
   已按受影响范围 `89/89` 精确定向闭环。Node `33/33`、Console `462/462`、lint `151 files`。
6. 停服后端口、Session、replay lease/pending、spool 与应用运行库 sidecar 均为 0。链接清理只在
   `D:/MetaHub/.tmp` 删除已确认节点并保留全部目标；22 个既有 ACL 目录未提权绕过，因此结论限定
   为授权可见范围。
7. 权威证据为 `artifacts/prd057/20260731T005000/`，最终审计 SHA-256 为
   `b92e8b8fefd4cd0a215b84f3b56ea3f083edbf91c2aa5ec760d20ca04a0bc05b`。

PRD-057 已完成，下一项为 PRD-058；`all_108_prds_complete=false`。
