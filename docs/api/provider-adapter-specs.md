# Provider Adapter 规范目录 API

本文档对应 `PRD-063 国内模型 Endpoint 适配规范`，说明 PAGE-090 使用的租户只读目录、
内部发布激活接口，以及 Gateway 执行合同。本文不提供 Provider Key、客户 Endpoint、Prompt、
Response 或可执行插件上传能力。

## 1. 当前目录身份

| 字段 | 当前值 |
| --- | --- |
| catalog version | `adapter-spec-v2` |
| spec count | `10` |
| canonical UTF-8 bytes | `15151` |
| SHA-256 | `c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf` |

目录正文随 Shared SDK 发布并在进程内冻结。数据库只保存每个租户激活的
`version/hash/spec_count` 事实，不保存或复制 spec JSON。Gateway 也不会在请求热路径查询
Control API 或数据库加载可执行 spec。

## 2. 产品分类

| classification | Provider 类型 | 说明 |
| --- | --- | --- |
| `model_vendor_cloud` | `zhipu`、`dashscope`、`wenxin`、`volcengine`、`hunyuan`、`deepseek` | 默认上游候选，使用厂家官方或云服务 Endpoint |
| `local_compatible` | `openai_compatible`、`local_endpoint` | 客户本地或客户指定的 OpenAI-compatible Endpoint；后者只能通过 PRD-084 聚合 API 创建 |
| `customer_managed` | `custom` | 客户自管的通用 JSON/SSE Endpoint |
| `migration_platform` | `migration_compatible` | 可选迁移入口，不是模型厂家或默认上游 |

OpenRouter、硅基流动只作为 `migration_compatible` 的固定平台示例，不是 Provider 类型，
也不会进入厂家集合、默认推荐或自动路由目标。

## 3. 用户只读目录

### `GET /adapter-specs`

身份与权限：

- 使用 `Authorization: Bearer <session token>`；
- 后端实时校验 `provider.health.read`；
- tenant 只能来自 Session，不能通过 query、Header 或请求体选择；
- 默认 Admin、Developer、Ops Admin 可读，Viewer 不可读；最终结果以当前租户权限矩阵为准。

允许的唯一 query：

| 参数 | 可选值 |
| --- | --- |
| `classification` | `model_vendor_cloud`、`local_compatible`、`customer_managed`、`migration_platform` |
| `status` | `active`、`optional` |

未知、重复、控制字符或 `tenant_id` query 均返回 `adapter.spec_filter_invalid`。成功与错误响应
统一设置 `Cache-Control: no-store`。

请求示例：

```http
GET /adapter-specs?classification=model_vendor_cloud&status=active HTTP/1.1
Authorization: Bearer <session-token>
X-Request-ID: req_adapter_specs_example
```

响应示例：

```json
{
  "catalog": {
    "version": "adapter-spec-v2",
    "hash": "c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf",
    "activated_at": "2026-08-01T00:00:00Z"
  },
  "specs": [
    {
      "spec_id": "adapter-spec-deepseek-v1",
      "tenant_id": "default",
      "provider_type": "deepseek",
      "display_name": "DeepSeek 官方或云服务",
      "classification": "model_vendor_cloud",
      "adapter_name": "openai_compatible",
      "adapter_version": "1.0.0",
      "protocol_family": "openai_chat_completions",
      "allowed_auth_types": ["bearer"],
      "model_id_strategy": "provider_model_id",
      "usage_profile": "openai_compatible_usage_v1",
      "error_profile": "provider_error_map_v1",
      "stream_profile": "openai_sse_final_usage_v1",
      "price_fields": [
        "input_price_per_1k",
        "output_price_per_1k",
        "currency",
        "price_schema_version"
      ],
      "probe_profile": "openai_models",
      "platform_examples": [],
      "status": "active",
      "contract_case_id": "prd063_deepseek_minimum_contract"
    }
  ],
  "request_id": "req_adapter_specs_example",
  "trace_id": "trc_adapter_specs_example"
}
```

Control API 会先读取当前 Session tenant 的唯一 active 激活事实，再与当前进程的
`version/hash/spec_count` 精确比较。缺少激活或任一维度漂移时返回 503，不展示尚未激活的
新制品目录。

## 4. 内部目录激活

### `POST /internal/adapter-specs/{tenant_id}/activate`

该接口只供受信发布服务使用：

- 只接受 `X-Internal-Token`，不能和用户 `Authorization` 混用；
- 认证发生在 tenant 数据库查询和正文解析之前；
- 必须提供严格 `X-Request-ID`，它同时是租户内幂等键；
- 不允许 query；正文只允许 `catalog_version`、`catalog_hash` 两个字段；
- Content-Type 必须为 `application/json`，正文上限为 1024 bytes；
- 调用方不能上传 spec JSON、Header、Endpoint 或 Adapter 代码。

请求示例：

```http
POST /internal/adapter-specs/default/activate HTTP/1.1
X-Internal-Token: <internal-service-token>
X-Request-ID: req_adapter_activation_20260801
Content-Type: application/json

{
  "catalog_version": "adapter-spec-v2",
  "catalog_hash": "c1cc9a5c9f977308fa3e06ab6914c28d9f9bef4ad3fe07eb950d1c6a76a2addf"
}
```

幂等和状态规则：

1. 同一 tenant/request ID 与相同 body 返回原收据；代码升级后仍可精确重放旧事实。
2. 同一 request ID 与不同 version/hash 返回 `adapter.spec_activation_conflict`。
3. 新 request 只有与当前进程目录完全一致才可继续。
4. 已激活同一目录时写入不可变 no-op 收据，返回 `changed=false`，不替换 active，不发事件。
5. 真实目录切换在一个事务内 supersede 旧 active 并插入新 active，返回 `changed=true`。
6. `0077` 和新租户 bootstrap 均为首次 v2 激活，不伪造 `adapter.spec_updated`。
7. Webhook 在数据库提交后尽力派发；失败不会回滚激活，也不会记录 Endpoint、正文或异常字符串。

## 5. Gateway 执行合同

Provider 管理、配置导入、健康探测和 Gateway 建连都会重新校验相同的
`provider_type/auth_type` 组合：

- 六类国内厂家只允许 `bearer`；
- `openai_compatible`、`local_endpoint`、`custom` 允许 `none` 或 `bearer`；
- `local_endpoint` 只能通过 `/local-endpoints` 创建；通用 Provider/Health/Config 入口固定拒绝；
- `migration_compatible` 只允许 `bearer`；
- 历史 `custom_header` 可继续作为旧元数据显示，但创建、更新、导入、探测和调用都在网络 I/O
  前返回固定错误，绝不回退为 Bearer；
- 未登记类型不再回退 Generic Adapter；
- OpenAI-compatible spec 才注入 `stream_options.include_usage=true`；通用 JSON/SSE 不猜测该选项；
- `ProviderRequest` 同时绑定 `adapter_version` 与 `adapter_catalog_hash`，用量和失败安全日志可对账
  到具体发布制品；Header、payload、URL 和凭据字段保持 `repr=False` 且不得进入日志。

## 6. 事件

真实版本切换发送 `adapter.spec_updated`：

```json
{
  "catalog_version": "adapter-spec-v3",
  "catalog_hash": "<64 位小写 SHA-256>",
  "previous_catalog_version": "adapter-spec-v2",
  "spec_count": 10,
  "activated_at": "2026-08-01T00:00:00Z"
}
```

事件 envelope 必须绑定：

- actor：`adapter-spec-publisher`；
- object：`paca_<32 位小写十六进制>`；
- event ID：`evt_<object_id>`；
- 同一个 tenant、来源 request/trace ID 与激活 UTC 秒。

同版本、额外字段、敏感字段、非法 hash/count/time、错误 actor 或对象漂移都会在签名前失败关闭。

## 7. 错误码

| 错误码 | HTTP | 含义 |
| --- | ---: | --- |
| `provider.adapter_spec_missing` | 422 | Control 收到未登记 Provider 类型 |
| `provider.auth_not_supported` | 422 | type/auth 组合不属于冻结 spec |
| `adapter.spec_catalog_unavailable` | 503 | 租户 active 激活缺失或与当前制品不一致 |
| `adapter.spec_catalog_invalid` | 503 | Shared 冻结目录结构或身份损坏 |
| `adapter.spec_filter_invalid` | 422 | 用户筛选未知、重复或含禁用字段 |
| `adapter.spec_tenant_not_found` | 404 | 内部激活目标租户不存在 |
| `adapter.spec_activation_conflict` | 409 | 幂等 request 与已有事实不一致 |
| `adapter.spec_activation_invalid` | 422 | 内部激活 Header、query 或 body 非法 |
| `gateway.provider_adapter_spec_missing` | 503 | Gateway 路由类型没有当前 spec |
| `gateway.provider_adapter_spec_invalid` | 503 | Gateway Adapter 名称、版本或鉴权与 spec 漂移 |
| `auth.permission_denied` | 403 | 当前用户缺少 `provider.health.read` |
| `auth.internal_token_required/invalid` | 401/403 | 内部发布身份缺失或错误 |

所有错误使用统一 envelope，包含 `code/message/suggestion/request_id/trace_id`；错误正文不会回显
Provider 类型脏值、Token、Header、Endpoint、SQL 或上游响应。

## 8. 新增 Provider 开发门禁

新增 Provider 不能只修改下拉框或工厂映射，必须在同一个 PRD/变更中完成：

1. 明确其分类是模型厂家/云服务、客户 Endpoint，还是迁移兼容平台；竞品聚合平台不得归入厂家。
2. 在 Shared 冻结目录增加完整 spec，声明 auth、模型 ID、usage、错误、stream、价格字段、探测
   profile、Adapter 版本和唯一 `contract_case_id`。
3. 更新 canonical version、byte length 和 SHA-256 字面量；hash 变化但版本不变会在模块导入时失败。
4. 为该类型补最小合同测试，至少覆盖工厂选择、请求模型映射、鉴权 Header、stream option、usage、
   错误归一、未知/损坏配置网络调用为 0，以及敏感 canary 不进入日志。
5. 同步 Control 创建/更新/导入/探测、PAGE-090 分类和数据库目录激活迁移；不能在某个入口另建类型集合。
6. 运行 Shared、Gateway、Control、迁移和 Console 相关专项，并完成隔离数据库、真实 API、Stub 和
   双视口运行验收；全部证据通过后才能把新目录标记为可发布。

完整可复用契约测试框架已由 PRD-064 交付；PRD-084 的十类 103 条测试是当前冻结目录的发布证明，
不能用公网探测或真实厂商 Key 替代。
