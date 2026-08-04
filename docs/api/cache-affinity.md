# Prompt Cache 感知粘性路由 API

本文档描述 PRD-050 的已实现公开合同：ModelAlias 的 CacheAffinity 配置投影、管理员完整快照写入、Gateway 运行选择事件，以及 RequestLog 中的安全 Selection 摘要。设计依据见 [PRD-050 设计文档](../prd/PRD-050-prompt-cache-aware-sticky-routing.md)。

## 1. 公共约束

- Control API 使用 Bearer Session 认证，Gateway 使用 Virtual Key 认证。`tenant_id` 只来自服务端认证上下文，客户端不能通过路径、查询参数、正文或 Header 选择租户。
- `GET /model-aliases` 允许 `admin`、`developer`；`viewer` 由后端返回 `403 auth.permission_denied`。
- `PATCH /model-aliases/{model_alias_id}/cache-affinity` 只允许 `admin`。`developer` 和 `viewer` 均由后端拒绝，且权限检查先于正文读取；前端只读或隐藏控件不是权限边界。
- 成功响应体携带 `request_id`、`trace_id`；所有响应头携带 `X-Request-ID`、`X-Trace-ID`。错误响应使用平台统一错误结构。
- 跨租户 Alias ID 与当前租户内不存在的 Alias ID 在 PATCH 上都返回 `404 model_alias.not_found`，不能据此枚举其他租户资源。
- Prompt、Response、Provider Key、Authorization、Credential、Binding、HMAC 摘要、Endpoint URL 和 endpoint revision 不进入配置响应、Webhook 或结构化日志。
- OpenRouter、硅基流动只属于竞品和迁移兼容对象，不是 CacheAffinity 的默认上游。该能力适用于智谱、通义、文心、火山、混元、DeepSeek 以及客户本地 OpenAI-compatible Endpoint。

统一错误响应示例：

```json
{
  "code": "cache_affinity.version_conflict",
  "message": "Cache affinity configuration was changed by another request.",
  "suggestion": "Refresh the model alias and retry from the latest cache affinity version.",
  "request_id": "req_console_cache_affinity_...",
  "trace_id": "trc_..."
}
```

错误体可能同时包含平台统一的安全诊断对象，但不会返回当前 CacheAffinity 快照、请求正文、SQL 或内部路径。

## 2. 读取 ModelAlias 投影

当前实现没有 `GET /model-aliases/{id}`。调用方通过列表接口读取当前租户 Alias 及其可空 CacheAffinity 投影：

```http
GET /model-aliases?status=active&environment_id={environment_id}&environment=dev
Authorization: Bearer {admin_or_developer_session_token}
X-Request-ID: {request_id}
```

`status`、`environment_id` 和 `environment` 均可省略；同时提交 Environment ID 与 code 时，两者必须在当前租户内指向同一 Environment。顶层响应为：

```json
{
  "model_aliases": [
    {
      "model_alias_id": "alias_...",
      "tenant_id": "tenant_...",
      "alias": "enterprise-chat",
      "environment": "dev",
      "cache_affinity": {
        "cache_affinity_id": "caf_0123456789abcdef0123456789abcdef",
        "tenant_id": "tenant_...",
        "model_alias_id": "alias_...",
        "status": "enabled",
        "data_class": "internal.general",
        "minimum_prefix_chars": 1024,
        "ttl_seconds": 3600,
        "version": 3,
        "created_at": "2026-07-21T00:00:00Z",
        "updated_at": "2026-07-21T00:05:00Z"
      }
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

尚未创建配置时 `cache_affinity=null`。配置投影固定包含以下字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `cache_affinity_id` | string | `caf_` 加 32 位小写十六进制的稳定配置 ID。 |
| `tenant_id` | string | 只读 owner，用于核对当前认证租户；客户端不得提交。 |
| `model_alias_id` | string | 一对一绑定的 ModelAlias ID。 |
| `status` | string | `enabled` 或 `disabled`。 |
| `data_class` | string 或 `null` | 服务端可信数据分类 code；启用时必须非空。它是配置值，不会进入 RequestLog Selection。 |
| `minimum_prefix_chars` | integer | 生成亲和上下文所需的 leading system/developer 文本最小字符数。 |
| `ttl_seconds` | integer | 当前 Alias 解释 warm 证据的滑动 TTL。 |
| `version` | integer | CacheAffinity 自身的独立 CAS 版本，不复用 RoutingPolicy 版本。 |
| `created_at`、`updated_at` | datetime | 配置创建和最后真实变化时间；no-op 不修改。 |

响应不包含 scope、Prompt 指纹、Binding、Credential、Key prefix、Endpoint、摘要或预计命中值。

## 3. 更新 CacheAffinity

### 3.1 严格请求 DTO

```http
PATCH /model-aliases/{model_alias_id}/cache-affinity
Authorization: Bearer {admin_session_token}
Content-Type: application/json
X-Request-ID: {stable_operation_request_id}
X-Trace-ID: {trace_id}

{
  "status": "enabled",
  "data_class": "internal.general",
  "minimum_prefix_chars": 1024,
  "ttl_seconds": 3600,
  "expected_version": 2
}
```

该接口采用完整快照语义，根对象的字段集合必须精确等于上述五项：

- 媒体类型必须是 `application/json`，允许标准参数，例如 `application/json; charset=utf-8`。
- 正文必须为 1 至 8192 bytes 的严格 UTF-8 JSON 对象。声明长度和实际流长度均受限。
- 任意层级重复 key、控制字符、超过 16 层或超过 64 个扫描节点的 JSON 均拒绝。
- 缺少任何字段或携带 `tenant_id`、`cache_affinity_id`、Prompt、预计命中等额外字段均返回 `422 cache_affinity.input_invalid`。
- `status` 必须精确为 `enabled` 或 `disabled`，不做 trim、大小写转换或未知值回落。
- `data_class` 为 `null` 或匹配 `^[a-z0-9][a-z0-9._-]{0,63}$` 的字符串；`enabled` 时必须非空。
- `minimum_prefix_chars` 必须是严格 JSON integer，范围 `256..16384`。
- `ttl_seconds` 必须是严格 JSON integer，范围 `60..86400`。
- `expected_version` 必须是严格 JSON integer，范围 `0..9223372036854775807`。布尔、字符串和浮点均不接受。
- 首次创建必须提交 `expected_version=0`；已有配置必须精确提交当前正整数版本。

### 3.2 成功响应

```json
{
  "cache_affinity": {
    "cache_affinity_id": "caf_0123456789abcdef0123456789abcdef",
    "tenant_id": "tenant_...",
    "model_alias_id": "alias_...",
    "status": "enabled",
    "data_class": "internal.general",
    "minimum_prefix_chars": 1024,
    "ttl_seconds": 3600,
    "version": 3,
    "created_at": "2026-07-21T00:00:00Z",
    "updated_at": "2026-07-21T00:05:00Z"
  },
  "changed": true,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

`changed` 的语义如下：

- 首次创建或任一执行字段真实变化：`changed=true`。首次版本为 1，后续版本精确增加 1。
- 当前版本匹配且四个配置字段全部相同：`changed=false`。版本和时间不变，但该 request ID 会写入不可变 no-op ConfigEvent，防止之后被另一意图复用。
- CAS 在 no-op 判断之前执行；旧版本即使提交当前同值也返回版本冲突。

### 3.3 CAS 与幂等

一次请求在同一个 `BEGIN IMMEDIATE` 事务中完成幂等恢复、租户限定 Alias 查询、CAS、配置、ConfigEvent 和管理审计。真实变化提交后才投递 `cache.affinity_selected` 的 `configuration_changed` 事件；投递失败不会回滚已提交配置。

幂等键是 `tenant_id + actor_user_id + X-Request-ID`，指纹绑定路径 Alias、完整四字段配置和 `expected_version`：

- 网络结果未知时，原样复用第一次的 actor、路径、request ID 和正文，可恢复原提交快照，不会重复升版。
- 同一 actor 和 request ID 改用不同 Alias、配置或版本，返回 `409 cache_affinity.request_conflict`。
- 不同 actor 的 request ID 空间相互独立，不能恢复其他管理员的操作。

### 3.4 409 后刷新

全局错误响应当前不返回领域 `details`，因此客户端不能从 409 正文取得“最新配置”。正确流程是：

1. 保留 409 的 `request_id`、`trace_id` 用于排障，不自动覆盖本地草稿。
2. 使用当前 Session 和同一租户筛选重新调用 `GET /model-aliases`。
3. 从目标 Alias 的 `cache_affinity` 读取最新版本并显式让用户 rebase。
4. 用户确认后生成新的 request ID，以最新 `expected_version` 提交完整快照。

禁止 last-write-wins，也禁止跨租户 GET 或在错误体缺字段时猜测版本。

## 4. 权限与租户矩阵

| 操作 | admin | developer | viewer | 匿名 |
| --- | --- | --- | --- | --- |
| `GET /model-aliases` | 200 | 200，只读 | 403 | 401 |
| CacheAffinity PATCH | 200 | 403，正文解析前 | 403，正文解析前 | 401 |
| `GET /request-logs/{request_id}` | 200 | 200 | 403 | 401 |

所有查询和写入都在服务层再次限定认证 `tenant_id`。路径中猜测到其他租户的 Alias ID 时，PATCH 与不存在 ID 同为 `404 model_alias.not_found`；不得返回其他租户配置或产生跨租户 ConfigEvent、Audit、Scope、Binding、Selection。

## 5. 错误码

| HTTP | code | 场景 |
| --- | --- | --- |
| 401 | `auth.token_missing`、`auth.token_invalid`、`auth.token_expired`、`auth.session_revoked` | Session 不可用。 |
| 403 | `auth.permission_denied` | 非 admin 调用 PATCH，或 viewer 调用读取接口。 |
| 404 | `model_alias.not_found` | 当前认证租户内不存在 Alias；跨租户 ID 使用同一码。 |
| 409 | `cache_affinity.version_conflict` | `expected_version` 与当前配置不一致。 |
| 409 | `cache_affinity.request_conflict` | 同一 actor 和 request ID 已绑定另一意图。 |
| 422 | `cache_affinity.input_invalid` | Content-Type、UTF-8/JSON、大小、结构、字段集合、类型、分类、门限、TTL 或版本非法。 |
| 503 | `cache_affinity.update_failed` | 配置、ConfigEvent 或审计事务无法安全提交；可重试。 |
| 503 | `gateway.cache_affinity_unavailable` | 已形成有效亲和上下文后，路由 reservation、Binding、Selection 或提交失败；可重试。 |
| 503 | `provider_key.unavailable` | 基础 KeyPool 没有可用凭据，保留既有错误码。 |

错误响应不会回显正文、分类值、摘要、Key、Credential、SQL、数据库路径或底层异常文本。

## 6. Gateway 执行语义

CacheAffinity 只在以下条件全部成立时启用：请求通过既有限流、预算、DLP 与执行门；请求命中 ModelAlias；Alias 配置为 `enabled`；服务端能取得可信 `tenant_id`、Virtual Key ID 和 `data_class`；Chat 请求具有达到门限且可严格规范化的连续 leading `system`/`developer` 前缀。

亲和域由以下五维共同隔离：

```text
tenant_id + virtual_key_id + data_class + model_endpoint_id + prompt_fingerprint
```

- Direct Model 和 Policy `route_to` 不启用亲和。
- 短 Prompt、缺可信维度或无法唯一规范化的请求走原有路由，不创建 Scope、Binding、Selection 或 runtime Webhook。
- `fixed` 模式不重排端点；动态模式也只能在原算法同一健康/质量证据层内应用偏好，不能复活 rejected 候选。
- Selection 在 Provider 调用前提交。首次或 pending 复用为 `estimated_hit=false`；只有同一完整域已有成功 attempt 或 completed stream 的 warm 证据时才为 `true`。
- TTL 每次按 `last_success_at + 当前 Alias ttl_seconds` 读取解释，不持久化跨 Alias 的共享过期时间。
- Selection 冻结的 Key/节点会原样传给 Adapter，不能在 Adapter 中二次选择另一把 Key。
- Provider 已成功后，warm CAS 失败只记录脱敏告警，不得改写客户端成功结果。

`estimated_hit` 只是本平台对既有成功绑定的预计复用，不代表 Provider 实际缓存命中，也不改变 Token 或成本。

## 7. RequestLog 安全投影

`GET /request-logs/{request_id}` 的 `request_log.cache_affinity_selections` 最多返回两个 attempt，按 `attempt_sequence` 升序排列。每项字段集合固定为：

```json
{
  "cache_affinity_selection_id": "cas_0123456789abcdef0123456789abcdef",
  "cache_affinity_id": "caf_0123456789abcdef0123456789abcdef",
  "cache_affinity_version": 3,
  "attempt_sequence": 1,
  "selection_reason": "binding_warm_reused",
  "estimated_hit": true,
  "endpoint_preference_applied": false,
  "target_kind": "managed",
  "model_id": "model_...",
  "provider_id": "provider_...",
  "provider_code": "local_openai_compatible",
  "created_at": "2026-07-21T00:06:00Z"
}
```

`selection_reason` 只可能是：

- `binding_created`
- `binding_pending_reused`
- `binding_warm_reused`
- `binding_rebound`

只有 `binding_warm_reused` 对应 `estimated_hit=true`。`target_kind` 为 `managed`、`environment` 或 `none`。

Selection 投影禁止加入 Key prefix、Provider Credential ID、Binding/Scope ID、`data_class`、Prompt 指纹、任意 digest、Endpoint URL、Authorization、Prompt 或 Response。

同一详情中的 `usage[].cache_tokens` 是 Provider 方言归一后的“上游报告缓存相关 Token”。部分 Provider 会把 cache read 与 cache creation 合并，因此：

- `estimated_hit=false` 与 `cache_tokens>0` 可以同时成立。
- `estimated_hit=true` 与 `cache_tokens=0` 也可以同时成立。
- 页面和 API 消费方不得从任一字段推导另一个字段，也不得用预计命中改写成本。

短 Prompt 或未启用请求返回空 `cache_affinity_selections`，而不是伪造 `estimated_hit=false` 行。

## 8. Webhook

事件类型统一为 `cache.affinity_selected`，通过 `payload.trigger` 区分运行选择和配置变化。

运行选择 `request_selected` 精确包含：

```json
{
  "trigger": "request_selected",
  "request_id": "req_...",
  "cache_affinity_selection_id": "cas_...",
  "cache_affinity_id": "caf_...",
  "cache_affinity_version": 3,
  "model_alias_id": "alias_...",
  "model_endpoint_id": "model_...",
  "selected_provider_id": "provider_...",
  "target_kind": "managed",
  "selection_reason": "binding_warm_reused",
  "estimated_hit": true
}
```

该信封使用 `event_id=cache_affinity_selection_id`、`object_id=cache_affinity_id`、`actor_id=null`，并强制携带 `trace_id`。

配置变化 `configuration_changed` 精确包含：

```json
{
  "trigger": "configuration_changed",
  "request_id": "req_...",
  "cache_affinity_id": "caf_...",
  "cache_affinity_version": 3,
  "model_alias_id": "alias_...",
  "change_kind": "updated",
  "changed_fields": ["ttl_seconds"],
  "status": "enabled"
}
```

配置事件使用持久 `cae_` event ID、可信管理员 actor 和同一 CacheAffinity object ID。no-op 不投递配置 Webhook；原样幂等重放可以复派同一 event ID，接收端必须按 event ID 幂等处理。

## 9. 实现与验收状态

PRD-050 已完成实现并通过风险分层验收。权威运行批次为 `artifacts/prd050/20260725T005725`：真实 Runtime、三角色三视口 Browser、后端 RBAC、租户隔离、0040 迁移、统一错误响应与 `request_id/trace_id`、日志隐私及停止后 SQLite 终态审计均已通过。

本轮针对 Browser 验收器局部修复执行相关合同与静态验证；2026-07-24 的 Python `921/921`、Console `354/354`、Browser 单测 `71/71`、Node 示例 `33/33` 和 lint `118 files` 仅作为变更前全量基线，没有伪称在本轮重跑。详细范围、失败证据和最终哈希见 [PRD-050 测试记录](../testing/PRD-050-tests.md) 与批次内 `final-manifest.json`。
