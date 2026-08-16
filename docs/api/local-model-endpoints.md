# 本地模型 Endpoint API

本文档对应 `PRD-084 本地模型 Endpoint 接入`。本地 Endpoint 是现有 Provider、Model 和可选
Credential 的租户级聚合入口；推理仍走统一 OpenAI-compatible Gateway，不绕过 DLP、预算、配额、
RequestLog、Usage、成本或输出安全。

## 1. 类型与权限

- Provider 类型固定为 `local_endpoint`。
- `engine_kind` 只允许 `openai_compatible`、`vllm`、`ollama`；三者均使用 `/v1` 兼容协议。
- `local_endpoint.read`：Admin、Developer、Ops Admin 可读取当前租户脱敏列表。
- `local_endpoint.manage`：Admin、Ops Admin 可原子创建 Endpoint、初始模型和可选凭据。
- `local_endpoint.test`：Admin、Developer、Ops Admin 可执行受限 `/v1/models` 探测。

Console capability 只控制呈现，三个 API 都会根据当前 Session、tenant 和 RBAC revision 在后端重新校验。
跨租户资源与不存在资源统一返回 404。

## 2. 创建 Endpoint

### `POST /local-endpoints`

请求必须携带 Session Bearer 和严格 `X-Request-ID`。`X-Request-ID` 是当前租户内的幂等键；同一
request ID 与相同非秘密意图返回已有资源和 `created=false`，与不同意图返回 409。幂等摘要不包含
Bearer 凭据，重放也不会轮换或比较新凭据。

```json
{
  "provider_code": "local_inference",
  "name": "Local inference",
  "engine_kind": "vllm",
  "base_url": "https://local-model.internal/v1",
  "auth_type": "bearer",
  "bearer_token": "<one-time-secret>",
  "initial_model": {
    "internal_model_id": "local-chat",
    "provider_model_id": "served-model",
    "display_name": "Local chat",
    "capabilities": {
      "chat": true,
      "stream": true
    },
    "context_window": 32768,
    "input_price_per_1k": 0,
    "output_price_per_1k": 0,
    "currency": "CNY"
  }
}
```

创建返回 201；幂等重放返回 200。`capabilities` 只接受值为 `true` 的
`chat|embeddings|stream|tools|json_mode|vision` 非空子集。`auth_type=none` 时必须省略
`bearer_token`；`auth_type=bearer` 时必须提供一次性值。

Provider、初始 Model、LocalModelEndpoint、可选加密 Credential、幂等 receipt 和 Audit 在一个
`BEGIN IMMEDIATE` 事务中提交。响应只包含 `endpoint_hint` 和 `credential_configured`，不包含完整 URL、
凭据、密文或指纹。

## 3. 查询 Endpoint

### `GET /local-endpoints`

只返回当前认证租户的聚合列表，不接受 tenant query：

```json
{
  "local_endpoints": [
    {
      "local_endpoint_id": "lme_0123456789abcdef0123456789abcdef",
      "tenant_id": "tenant-example",
      "provider_id": "provider-example",
      "provider_code": "local_inference",
      "name": "Local inference",
      "engine_kind": "vllm",
      "endpoint_hint": "https://[private]/v1",
      "auth_type": "bearer",
      "status": "active",
      "health_status": "unknown",
      "last_tested_at": null,
      "revision": 1,
      "catalog_revision": 0,
      "catalog_status": "unknown",
      "last_discovered_at": null,
      "discovered_model_count": 0,
      "credential_configured": true,
      "initial_model": {
        "model_id": "model-example",
        "internal_model_id": "local-chat",
        "provider_model_id": "served-model",
        "display_name": "Local chat",
        "capabilities": {"chat": true, "stream": true},
        "context_window": 32768,
        "input_price_per_1k": 0,
        "output_price_per_1k": 0,
        "currency": "CNY",
        "status": "active"
      }
    }
  ],
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

`endpoint_hint` 只有 scheme、`private|public|loopback` 分类和 `/v1` 提示，不包含 hostname、IP、完整端口
或自定义 path。成功和错误响应均设置 `Cache-Control: no-store, private`。

## 4. 测试与目录刷新

### `POST /local-endpoints/{local_endpoint_id}/test`

请求体必须是 `{}`。Control 在一个健康 lease 内完成 DNS/ACL、pinned connect 和一次 `/v1/models`
请求；不先探测再进行第二次无 lease 请求。

成功目录最多读取 1 MiB、200 个唯一模型 ID，每个 ID 最长 160 字符。探测只更新发现目录的
`available|missing` 和 catalog revision，不删除 Model，也不覆盖管理员设置的 capability、价格、状态或描述。
响应只返回固定健康结果、HTTP status、latency、固定错误码、发现数量和截断标记，不返回上游 JSON。

并发探测、generation 漂移、DNS/ACL、认证、超时和目录格式错误都使用固定错误码。通用 Provider list/test/
PATCH/Key、Provider Health 和 Config Import/Export 入口拒绝 `local_endpoint`，防止绕过聚合 owner。

## 5. Gateway 与事件

Gateway 在创建 routing attempt 后执行部署级 DNS 全地址 ACL。所有地址必须获准；通过后固定一个 IP，保留原
Host 与 TLS SNI，禁止代理、redirect、HTTP/2 和跨请求 keep-alive。DNS/ACL 通过后、连接前，以 CAS 将 attempt
从 `not_called` 改为 `called`；只有 `called` 终态生成不可变 `local_model.called` source/outbox。

成功、上游失败、超时、断开与取消都保留 routing attempt、RequestLog、Usage/成本状态。DLP、预算或权限阻断
不会发起网络调用，也不会生成 `local_model.called`。事件 payload 只包含 tenant 归属下的资源 ID、request/
attempt、结果、固定错误、latency、usage status 和时间，不包含 URL、Credential、Header、Prompt、Response、
resolved IP 或上游正文。持久 outbox 使用稳定 event ID、lease/CAS 和有界重试实现进程崩溃恢复。

## 6. 部署配置

| 环境变量 | 默认值 | 约束 |
| --- | ---: | --- |
| `METAHUB_LOCAL_ENDPOINT_PRIVATE_CIDRS` | 空 | 只接受规范企业私网 CIDR；租户不能覆盖 |
| `METAHUB_LOCAL_ENDPOINT_ALLOWED_PORTS` | `80,443,8000,8080,8443,11434` | 非空、唯一、部署级端口白名单 |
| `METAHUB_LOCAL_ENDPOINT_ALLOW_LOOPBACK` | `false` | 仅 dev/test 可启用，prod 固定拒绝 |
| `METAHUB_LOCAL_ENDPOINT_CONNECT_TIMEOUT_SECONDS` | `5` | `1..30` 秒 |
| `METAHUB_LOCAL_ENDPOINT_OVERALL_TIMEOUT_SECONDS` | `120` | `1..600` 秒，覆盖 DNS、连接、TTFB 与正文/流 |
| `METAHUB_LOCAL_ENDPOINT_MAX_RESPONSE_BYTES` | `16777216` | 非流式成功正文上限 |
| `METAHUB_LOCAL_ENDPOINT_MAX_STREAM_BYTES` | `67108864` | 单次流式累计字节上限 |

配置非法时 Control/Gateway readiness 失败关闭。metadata、link-local、multicast、unspecified 永久拒绝；
loopback 不能通过 CIDR 绕过生产限制。

## 7. 主要错误码

| 错误码 | HTTP | 含义 |
| --- | ---: | --- |
| `local_endpoint.not_found` | 404 | 当前租户资源不存在 |
| `local_endpoint.request_invalid` | 422 | 基础字段非法 |
| `local_endpoint.engine_invalid` | 422 | engine 不属于闭集 |
| `local_endpoint.url_invalid` | 422 | URL 结构或 `/v1` 约束非法 |
| `local_endpoint.auth_invalid` | 422 | auth 与一次性凭据组合非法 |
| `local_endpoint.catalog_invalid` | 422 | 初始模型或发现目录非法 |
| `local_endpoint.conflict` | 409 | 资源唯一性冲突 |
| `local_endpoint.idempotency_conflict` | 409 | request ID 与既有创建意图不一致 |
| `local_endpoint.unavailable` | 503 | 聚合事务或探测不可用 |
| `provider.local_endpoint_requires_local_api` | 422 | 通用 Provider/Health/Config 入口被封闭 |
| `gateway.local_endpoint_blocked` | 502 | 部署级网络策略拒绝 |
| `gateway.local_endpoint_unavailable` | 503 | 本地 Endpoint 连接、deadline 或大小上限失败 |

所有错误使用统一 envelope，并携带 `request_id` 和 `trace_id`；底层 DNS、TLS、HTTP、数据库或加密异常文本不会
拼入响应或结构化日志。
