# Provider 错误归一化规则 API

本文档冻结 PRD-056“Provider 错误归一化规则库”的公开 Control API、内部快照协议、
权限与租户边界、幂等写入、错误码以及 Gateway 冻结事实合同。完整技术设计和两轮编码前
自审见 [PRD-056 技术设计](../prd/PRD-056-provider-error-normalization-rule-library.md)。

## 1. 能力边界

本能力允许租户管理员用确定性规则把 Provider 的 HTTP 状态、固定错误字段和有界 message
信号映射为平台统一错误码。它不执行正则表达式、JSONPath、用户脚本、自然语言分类或 LLM
推理，也不保存或转发原始上游错误正文。

公开端点：

| 方法 | 路径 | 成功状态 | 后端角色 |
| --- | --- | ---: | --- |
| `GET` | `/error-maps` | 200 | `admin`、`developer` |
| `POST` | `/error-maps` | 201 | `admin` |
| `PATCH` | `/error-maps/{error_map_id}` | 200 | `admin` |

内部端点：

| 方法 | 路径 | 成功状态 | 身份 |
| --- | --- | ---: | --- |
| `GET` | `/internal/error-maps/{tenant_id}/current` | 200/304 | `X-Internal-Token` |

公开 API 使用 `Authorization: Bearer <session_token>`。匿名请求返回统一 401；`viewer` 对全部
公开端点返回统一 403。后端先执行 RBAC，再解析 query、path 或正文，因此畸形或超大输入不能
成为权限探针。`tenant_id`、actor、Session 和角色只来自认证上下文，客户端不能提交或覆盖。

成功响应和统一错误响应均带 `X-Request-ID`、`X-Trace-ID`，JSON 正文中的 `request_id`、
`trace_id` 与响应头一致。所有读取响应设置 `Cache-Control: no-store`。

## 2. 规则对象

租户规则示例：

```json
{
  "error_map_id": "pem_0123456789abcdef0123456789abcdef",
  "source": "tenant",
  "readonly": false,
  "provider_id": "prv_0123456789abcdef0123456789abcdef",
  "provider_code": "deepseek",
  "provider_name": "DeepSeek 官方",
  "priority": 500,
  "http_statuses": [400, 404],
  "http_status_class": null,
  "error_field": "error.code",
  "field_operator": "equals",
  "field_pattern": "model_not_found",
  "message_operator": null,
  "message_pattern": null,
  "output_error_code": "gateway.upstream_model_not_found",
  "retryable": false,
  "user_action": "检查模型名称与租户模型映射。",
  "status": "active",
  "version": 1,
  "pattern_hash": "<64 lowercase hex>",
  "updated_at": "2026-07-29T08:00:00Z"
}
```

内置规则使用稳定 `builtin_*` ID、`source=builtin` 和 `readonly=true`。它们可被读取和筛选，
不能通过 POST/PATCH 修改。安全 guard 不作为租户可编辑资源；租户规则不能覆盖鉴权、超时、
网络故障或 5xx 等 guard 分类。

### 2.1 可编辑字段

| 字段 | 约束 |
| --- | --- |
| `provider_id` | 当前租户 Provider ID 或 `null`；只含 message 条件时必须指定 |
| `priority` | 1..1000，数值越大越先匹配 |
| `http_statuses` | 1..16 个去重升序整数，范围 100..599；与 status class 互斥 |
| `http_status_class` | 仅 `4xx` 或 `null`；`5xx` 由不可覆盖 guard 独占 |
| `error_field` | `error.code`、`error.type`、`code`、`type` 或 `null` |
| `field_operator` | `equals`、`contains` 或 `null` |
| `field_pattern` | NFC 后 1..256 字符；与 error field/operator 成组 |
| `message_operator` | 仅 `contains` 或 `null` |
| `message_pattern` | NFC 后 1..256 字符；与 message operator 成组 |
| `output_error_code` | 租户可选统一错误码白名单 |
| `status` | `active` 或 `disabled` |

规则至少配置 HTTP、固定错误字段或 message 条件之一。所有已配置条件使用 AND；
`http_statuses` 内部使用 OR。相同租户、Provider scope、priority 和规范化条件不能存在两条
active 规则。

租户可选输出码仅为：

```text
gateway.upstream_rate_limited
gateway.upstream_quota_exceeded
gateway.upstream_model_not_found
gateway.upstream_resource_not_found
gateway.upstream_request_invalid
```

公开 HTTP 状态、`retryable`、Fallback 分类和用户建议由 Shared 静态目录推导，不能由管理员
输入。规则 pattern 会对 developer 可见，因此服务端拒绝 Authorization、Bearer、API Key、
URL 凭据和高熵 token 形态。

## 3. GET /error-maps

允许的单值 query：

```text
error_map_id provider_id source status output_error_code limit offset
```

未知参数、重复参数、非法枚举或越界分页返回 `error_map.input_invalid`。`limit` 默认 100，
`offset` 默认 0。精确 `error_map_id=pem_<32 lower hex>` 与所有其他筛选和分页互斥，专用于
版本冲突后的 tenant-qualified 重取；不存在、跨租户和不可见资源均返回同形 404。

成功响应：

```json
{
  "error_maps": [],
  "provider_options": [
    {
      "provider_id": "prv_0123456789abcdef0123456789abcdef",
      "provider_code": "deepseek",
      "name": "DeepSeek 官方",
      "status": "active"
    }
  ],
  "head_revision": 0,
  "content_hash": "994a863c694e05021d65d0f8b862f24a28da461080286b7b35d1695ee0775fdc",
  "builtin_catalog_version": "provider-error-builtin-v1",
  "updated_at": null,
  "total": 0,
  "limit": 100,
  "offset": 0,
  "request_id": "req_control_01J...",
  "trace_id": "trc_control_01J..."
}
```

`provider_options` 只包含当前租户的安全展示字段，不返回 Endpoint、Provider Key、KeyPool 或
凭据。GET 是纯读取：没有 head 时合成 revision 0，不创建数据库行、Audit 或事件。

## 4. POST /error-maps

请求必须是 UTF-8 JSON object，最大 16 KiB、深度 16、节点 128；拒绝重复 key、未知字段、
NaN/Infinity 和类型混淆。

```json
{
  "provider_id": null,
  "priority": 500,
  "http_statuses": [429],
  "http_status_class": null,
  "error_field": "error.code",
  "field_operator": "contains",
  "field_pattern": "insufficient_balance",
  "message_operator": null,
  "message_pattern": null,
  "output_error_code": "gateway.upstream_quota_exceeded",
  "status": "active"
}
```

`X-Request-ID` 是当前 tenant + actor 维度的幂等键。相同 ID 与相同规范化意图重放首次安全结果；
相同 ID 与不同意图返回 `error_map.request_conflict`。成功响应：

```json
{
  "changed": true,
  "error_map": {"error_map_id": "pem_0123456789abcdef0123456789abcdef", "version": 1},
  "head_revision": 1,
  "content_hash": "<64 lowercase hex>",
  "builtin_catalog_version": "provider-error-builtin-v1",
  "request_id": "req_console_error_map_...",
  "trace_id": "trc_control_01J..."
}
```

真实创建在同一 `BEGIN IMMEDIATE` 事务提交不可变 rule version、head history/current head、
`error_map.updated` Source Outbox、Audit 和 operation ledger。任一部分失败时全部回滚。

## 5. PATCH /error-maps/{error_map_id}

path ID 固定为 `pem_<32 lower hex>`。正文必须包含原生正整数 `expected_version`，并至少提供一个
可编辑字段：

```json
{
  "expected_version": 1,
  "priority": 600,
  "status": "disabled"
}
```

服务端先按认证 tenant 查找，再执行 CAS。旧版本返回 `error_map.version_conflict`；响应不回显
当前规则、pattern 或数据库 details。客户端必须用新的 request_id 发起精确 GET，让用户显式载入
最新版本，再用第三个 request_id 提交新的 PATCH。

同值 PATCH 返回 `changed=false`，不推进 rule version/head，不写 Audit 或事件，只记录可重放的
幂等结果。任何已解析 HTTP 响应都会终结当前 mutation request_id；只有网络中断导致结果未知时
才允许用相同意图和相同 ID 重试。

## 6. 内部快照协议

Gateway 调用：

```http
GET /internal/error-maps/{tenant_id}/current HTTP/1.1
X-Internal-Token: <service token>
X-Request-ID: <safe id>
X-Trace-ID: <safe id>
Accept: application/json
Accept-Encoding: identity
If-None-Match: "<64 lowercase hex>"
```

用户 Bearer 不能替代内部 Token；query 必须为空。Token、tenant、服务状态或快照不合法时返回
统一安全错误，不回显 Token、SQL 或内部 URL。

200 wire：

```json
{
  "wire_schema_version": 1,
  "tenant_id": "tenant_01J...",
  "revision": 1,
  "overlay_content_hash": "<64 lowercase hex>",
  "builtin_catalog_version": "provider-error-builtin-v1",
  "rules": []
}
```

响应使用绑定 schema、tenant、revision、content hash 和 builtin catalog 的强 ETag。匹配
`If-None-Match` 时返回严格 304、空正文、相同 ETag。200/304 均为 `no-store`，不包含 actor、
Audit、事件或管理元数据。

Gateway 首次见到已认证 tenant 时后台拉取，不阻塞模型调用；当前请求冻结一个 immutable lease，
主调用、Fallback 和流式握手都使用同一 revision/hash/catalog/source。同步失败使用仍在硬上限内
的 LKG，否则回落 builtin，不绕过鉴权、预算、DLP、路由或用量追踪。

## 7. 冻结错误事实

失败的 `RoutingAttempt` 或流式握手 `StreamSession` 最多关联一个 `provider_error_fact`。Fact 与
owner 终态在同一事务提交，记录：

- 冻结规则 ID/source/version、head revision、overlay hash、builtin catalog 和快照来源；
- 统一输出码、owner 终态码、原始 HTTP 状态、固定 transport kind 和 retryable；
- code/type/message 是否存在、平台固定 class、正文长度桶、JSON/截断位；
- 脱敏投影及其 SHA-256。

Fact 不含原始 code/type/message/body、Prompt、Response、Header、Endpoint、Provider Key 或
Token。RequestLog 详情直接读取冻结 Fact，不按当前规则重算历史结果。

## 8. error_map.updated

真实创建、更新、启用或停用会提交不可变 Source Outbox；no-op 和业务失败不产生事件。payload
精确包含：

```json
{
  "schema_version": 1,
  "operation": "update",
  "provider_error_map_id": "pem_0123456789abcdef0123456789abcdef",
  "rule_version": 2,
  "head_revision": 3,
  "provider_scope": "all",
  "status": "active",
  "priority": 500,
  "output_error_code": "gateway.upstream_quota_exceeded",
  "pattern_hash": "<64 lowercase hex>",
  "request_id": "req_console_error_map_..."
}
```

事件不包含 pattern、匹配文本、原始错误、Provider name/Endpoint、actor email 或任何密钥。
投递沿用 30 秒 lease、最多 10 次尝试和 5/30/300 秒封顶退避。

## 9. 统一错误码

| 错误码 | HTTP | 可重试 | 说明 |
| --- | ---: | --- | --- |
| `error_map.not_found` | 404 | 否 | 规则不存在、跨租户或不可见，同形返回 |
| `error_map.tenant_unavailable` | 404 | 否 | 内部快照 tenant 不存在或不可用 |
| `error_map.input_invalid` | 422 | 否 | query、path、媒体类型或 JSON wire 非法 |
| `error_map.update_empty` | 422 | 否 | PATCH 没有可编辑字段 |
| `error_map.output_code_invalid` | 422 | 否 | 输出码不在白名单或与条件矩阵冲突 |
| `error_map.pattern_invalid` | 422 | 否 | operator 或 pattern 结构非法 |
| `error_map.pattern_sensitive` | 422 | 否 | pattern 命中敏感信息门禁 |
| `error_map.conflict` | 409 | 否 | active 规则条件冲突 |
| `error_map.version_conflict` | 409 | 否 | expected_version 已过期 |
| `error_map.request_conflict` | 409 | 否 | 同一幂等 ID 对应不同意图 |
| `error_map.limit_exceeded` | 409 | 否 | 规则数或 head revision 达到上限 |
| `error_map.unavailable` | 503 | 是 | 规则存储暂不可用 |
| `error_map.snapshot_unavailable` | 503 | 是 | 快照构建、传输或校验失败 |
| `error_map.event_unavailable` | 503 | 是 | Source Outbox 状态不可用 |
| `gateway.upstream_resource_not_found` | 502 | 否 | 无模型信号的普通上游 404 |

统一错误正文只返回固定 code、稳定 message/suggestion/diagnosis、`request_id` 和 `trace_id`；
不得回显 pattern、上游正文、解析异常、SQL、数据库路径、Token 或凭据。

## 10. 运维配置

Gateway 配置：

```text
METAHUB_PROVIDER_ERROR_MAP_SYNC_ENABLED=false
METAHUB_PROVIDER_ERROR_MAP_POLL_INTERVAL_SECONDS=2
METAHUB_PROVIDER_ERROR_MAP_HTTP_TIMEOUT_SECONDS=2
METAHUB_PROVIDER_ERROR_MAP_REFRESH_QUEUE_TIMEOUT_SECONDS=2
METAHUB_PROVIDER_ERROR_MAP_FRESH_SECONDS=300
METAHUB_PROVIDER_ERROR_MAP_LKG_SECONDS=3600
METAHUB_PROVIDER_ERROR_MAP_MAX_PAYLOAD_BYTES=262144
METAHUB_PROVIDER_ERROR_MAP_REFRESH_CONCURRENCY=4
```

默认关闭只影响租户 overlay 热同步，Gateway 仍使用版本化 builtin 目录。生产和验收环境启用时，
必须同时提供高熵 `METAHUB_INTERNAL_CONFIG_TOKEN` 和安全 Control API 地址；非 TLS HTTP 只允许
字面量回环地址。

