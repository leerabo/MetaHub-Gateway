# PRD-009 - OpenAI-Compatible 接口基础设计

状态：已实现，自动化测试、真实 HTTP 和浏览器验收通过。

设计来源：Excel `PRD-009 - OpenAI-Compatible接口基础`、页面清单 `PAGE-012/PAGE-013`、API 清单 `POST /v1/chat/completions`、`POST /v1/embeddings`、数据对象 `GatewayRequest/GatewayResponse`、事件 `request.received`、验收用例 `TC-0017/TC-0018`。架构约束要求数据面是模型调用热路径，必须统一 request_id/trace_id、鉴权、路由、Provider Adapter、Trace 与 Usage 记录；OpenRouter/硅基流动只是对标平台，不是默认上游。

## 需求理解

本 PRD 的目标是让开发者把 OpenAI Python/JS SDK 的 `base_url` 改成本项目网关地址后，可以通过最小 OpenAI-compatible 接口完成一次 chat 调用。M0 聚焦 Chat Completions 和 Embeddings 两个入口，只做基础请求接收、参数校验、OpenAI-compatible provider 透传、响应封装和 JSON/SSE 流式标识。

核心范围：

- 数据面新增 `POST /v1/chat/completions`。
- 数据面新增 `POST /v1/embeddings`。
- 支持 `Authorization: Bearer <virtual_key>` 鉴权。
- 按当前 `virtual_key` 恢复 `tenant_id`，所有日志、request_logs、usage_ledger 均按租户写入。
- 校验 OpenAI-compatible 必填参数：`model`、`messages` 或 `input`。
- 非流式请求透传到 OpenAI-compatible Provider Endpoint，并返回 OpenAI-compatible JSON。
- `stream=true` 时接受 SSE 流式标识，并返回 `text/event-stream`；M0 先做单上游透传，不做中途 fallback。
- 产生 `request.received` 结构化事件。
- 参数校验、认证失败、Provider 失败均返回统一错误码和 `request_id`/`trace_id`。
- 失败请求必须写结构化错误日志，并尽量写入 `request_logs`。

明确不做：

- 不做 Responses API 全量兼容。
- 不做 Assistants API。
- 不做 MCP 工具调用主链路。
- 不做复杂函数调用/工具调用编排。
- 不做多 Provider 质量/成本路由；M0 使用模型别名固定路由。
- 不做流式中途切换模型。
- 不做语义缓存、Prompt Cache 感知路由或复杂 DLP。
- 不做完整价格归一化；M0 可记录原始 usage 和零成本/未知成本占位，价格 PRD 后补齐。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 数据面 API | `gateway-data-plane/src/metahub_gateway/api/` | 新增 OpenAI-compatible 路由、请求模型、响应处理 |
| 数据面认证 | `gateway-data-plane/src/metahub_gateway/auth/` | 新增 Virtual Key Bearer 鉴权，按 `key_hash` 恢复 `tenant_id` |
| Provider Adapter | `gateway-data-plane/src/metahub_gateway/providers/` | 新增最小 OpenAI-compatible adapter，支持 JSON 与 SSE 透传 |
| 路由/模型解析 | `gateway-data-plane/src/metahub_gateway/routing/` | 按 `models.model_alias` 查找当前租户模型和 Provider |
| 用量与日志 | `gateway-data-plane/src/metahub_gateway/usage/`、`logging/` | 写 request_logs、usage_ledger 占位，记录 request.received |
| 共享运行时 | `shared-sdk/python/metahub_shared/runtime/` | 扩展 gateway 错误码 |
| 控制台 | `console-web/` | API 文档入口、调试台最小表单和 curl/Python/JS 示例 |
| 测试与文档 | `gateway-data-plane/tests/`、`console-web/tests/`、`docs/testing/` | 覆盖 SDK 兼容、参数缺失、鉴权、流式/非流式、错误码 |

编码时保留中文注释，重点解释数据面热路径为什么只做最小逻辑、Provider Key 不落日志、请求失败如何写日志和 request_logs。

## 数据表/字段设计

本 PRD 不新增业务表，复用 PRD-003 已建表：

### virtual_keys

用于 OpenAI SDK 请求鉴权。

| 字段 | 使用方式 |
| --- | --- |
| `tenant_id` | 恢复当前请求租户 |
| `virtual_key_id` | 写入 request_logs 和 usage_ledger |
| `key_hash` | 对 Bearer Token 做 SHA-256 后匹配 |
| `status` | 仅 `active` 可调用 |

### models

用于把 OpenAI 请求中的 `model` 解析为租户内模型别名。

| 字段 | 使用方式 |
| --- | --- |
| `tenant_id` | 与 Virtual Key 的租户一致 |
| `model_alias` | 匹配请求体 `model` |
| `provider_id` | 找到上游 Provider |
| `upstream_model_name` | 发送给上游的真实模型名 |
| `capabilities` | M0 检查 `chat`、`embeddings`、`stream` 能力 |
| `status` | 仅 `active` 可路由 |

### providers / provider_credentials

用于找到 OpenAI-compatible Endpoint。

M0 约定：

- `providers.base_url` 保存上游 OpenAI-compatible base URL，例如 `http://localhost:11434/v1`。
- Provider Key 不从日志或响应输出。
- 因 PRD-003 当前仅保存占位密文，M0 调用时优先从环境变量读取 `METAHUB_PROVIDER_<PROVIDER_CODE>_API_KEY`；未配置时允许无 Authorization 调用本地 OpenAI-compatible Endpoint。
- `provider_credentials.key_fingerprint` 用于排障关联，不用于反推出明文 Key。

### request_logs

每次网关请求尽量写入一条记录，失败也要写。

| 字段 | 取值 |
| --- | --- |
| `request_id` | 当前 request_id |
| `tenant_id` | Virtual Key 所属租户 |
| `trace_id` | 当前 trace_id |
| `virtual_key_id` | 鉴权成功时写入 |
| `provider_id` | 路由成功时写入 |
| `model_id` | 路由成功时写入 |
| `status` | `success` / `failed` |
| `error_code` | 失败时写统一错误码 |
| `latency_ms` | 请求耗时 |
| `created_at` | 创建时间 |

### usage_ledger

M0 先记录可追踪占位，价格归一化在后续 PRD 完善。

| 字段 | 取值 |
| --- | --- |
| `tenant_id` | 当前租户 |
| `request_id` | 对应 request_logs |
| `input_tokens/output_tokens` | 优先从上游 `usage` 读取，缺失则 0 |
| `raw_usage` | 上游原始 `usage` JSON；缺失为 `{}` |
| `normalized_usage` | M0 写基础结构或 `{}` |
| `cost_amount` | 未接价格表前为 0 |
| `currency` | 默认 `CNY` |

## API 设计

所有接口在 Gateway Data Plane，必须继续使用 `create_service_app` 提供的 request_id、trace_id、统一错误和结构化日志。

### 鉴权

请求头：

```http
Authorization: Bearer <virtual_key>
```

规则：

- 缺失或格式错误返回 `gateway.auth_missing` / `gateway.auth_invalid`。
- 对 token 做 SHA-256 后查询 `virtual_keys.key_hash`。
- Virtual Key、租户、模型、Provider 均必须为 `active`。
- 不接受请求体或查询参数里的 `tenant_id` 决定租户。

### POST /v1/chat/completions

请求示例：

```json
{
  "model": "default-chat",
  "messages": [
    {"role": "user", "content": "你好"}
  ],
  "temperature": 0.7,
  "stream": false
}
```

校验：

- `model` 必填，非空字符串。
- `messages` 必填，非空数组。
- 每条 message 至少包含 `role` 和 `content`。
- `stream` 可选，默认 false。
- 其他 OpenAI-compatible 字段保留并透传，不在 M0 逐项强校验。

路由：

- 在当前租户内按 `models.model_alias = request.model` 查找 active 模型。
- 将请求体中的 `model` 替换为 `models.upstream_model_name` 后发给上游。
- M0 固定路由到该模型绑定的 Provider，不做质量/成本路由。

非流式响应：

- 上游返回 2xx 时，将 JSON 原样返回给客户端。
- 若上游没有返回 `usage`，usage_ledger 记录 0 token。
- 响应头保留 `X-Request-ID` 和 `X-Trace-ID`。

流式响应：

- `stream=true` 时，向上游透传 `stream=true`。
- 返回 `text/event-stream`。
- 每个 SSE chunk 原样透传。
- M0 不做中途模型切换；上游中断时结束流并写失败日志。
- Inter-Token Timeout Watchdog 只预留配置点，完整能力在后续流式 PRD 扩展。

### POST /v1/embeddings

请求示例：

```json
{
  "model": "default-embedding",
  "input": "hello"
}
```

校验：

- `model` 必填。
- `input` 必填，可为字符串或字符串数组。
- Embeddings M0 不支持 `stream=true`，若出现则返回 `gateway.stream_not_supported`。

响应：

- 上游返回 2xx 时，将 OpenAI-compatible JSON 原样返回。
- usage 处理同 chat。

### request.received 事件

每次鉴权成功并进入业务处理后写结构化日志事件：

```json
{
  "event": "request.received",
  "tenant_id": "tenant_xxx",
  "actor_id": "virtual_key_id",
  "object_id": "request_id",
  "trace_id": "trc_xxx",
  "payload": {
    "endpoint": "/v1/chat/completions",
    "model": "default-chat",
    "stream": false
  }
}
```

payload 不包含 prompt 明文、message content、input 明文、Virtual Key、Provider Key。

## 前端页面/交互设计

### API 文档

在控制台登录后新增“API 文档”面板，面向开发者展示：

- Gateway Base URL：`http://localhost:8000/v1`。
- Chat Completions endpoint。
- Embeddings endpoint。
- Authorization Header 说明。
- Python 和 JavaScript 最小示例。
- request_id/trace_id 排障说明。

M0 不展示真实 Virtual Key 明文；只提示去后续 Virtual Key 页面创建或使用 seed key。

### 调试台

将现有调试台预留从 disabled 状态推进到最小可用：

- 输入 Virtual Key。
- 输入 model alias。
- 输入一条 user message。
- `stream` 开关。
- 发送到 `/v1/chat/completions`。
- 展示响应 JSON 或 SSE 文本摘要。
- 错误时展示统一错误码、request_id、trace_id 和 suggestion。

调试台只用于开发者验证，不存储 Virtual Key，不把 Virtual Key 写入日志。

## 错误码设计

| 错误码 | HTTP | 场景 | 建议 |
| --- | --- | --- | --- |
| `gateway.auth_missing` | 401 | 缺少 Bearer Virtual Key | 传入 `Authorization: Bearer <virtual_key>` |
| `gateway.auth_invalid` | 401 | Virtual Key 格式错误、不存在或已禁用 | 检查 Key 是否正确且 active |
| `gateway.request_invalid` | 422 | 请求体不是合法 JSON 或字段类型错误 | 检查 OpenAI-compatible 请求体 |
| `gateway.model_missing` | 422 | 缺少 `model` | 传入模型别名 |
| `gateway.messages_missing` | 422 | chat 请求缺少 `messages` | 传入非空 messages 数组 |
| `gateway.input_missing` | 422 | embeddings 请求缺少 `input` | 传入字符串或字符串数组 |
| `gateway.model_not_found` | 404 | 当前租户找不到 active 模型别名 | 检查模型目录和租户 |
| `gateway.provider_not_found` | 404 | 模型绑定的 Provider 不存在或禁用 | 检查 Provider 配置 |
| `gateway.stream_not_supported` | 422 | embeddings 或不支持流式的模型请求 stream | 关闭 stream 或更换模型 |
| `gateway.upstream_unavailable` | 503 | 上游连接失败或超时 | 检查 Provider Endpoint |
| `gateway.upstream_error` | 502 | 上游返回非 2xx | 使用 request_id 查看上游错误摘要 |

## 安全与权限约束

- 数据面 API 只信任 Virtual Key 鉴权恢复出的 `tenant_id`。
- 请求体中的 `tenant_id`、`organization` 等字段不参与租户选择。
- Virtual Key、Provider Key、Authorization Header 不允许明文落日志、request_logs 或 usage_ledger。
- `request.received` 事件不记录 prompt/input/message 明文，避免日志保存敏感业务内容。
- Provider Key 优先从环境变量读取，运行时只在内存中用于上游 Authorization。
- 参数校验失败、认证失败、上游失败都必须返回统一错误结构并进入结构化错误日志。
- 成功和失败请求都尽量写 request_logs；鉴权失败时无 tenant_id，可只写结构化错误日志。
- Usage 与成本必须可追踪：M0 至少保存 request_id、tenant_id、virtual_key_id、raw_usage、token 占位和 cost=0。
- 流式请求开始输出后不允许中途 fallback 或切换模型。
- M0 不接入境外模型作为默认路径；上游来自租户配置的 Provider Endpoint。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0017-1 | OpenAI SDK chat 非流式 | SDK 设置 `base_url=http://localhost:8000/v1` 后可完成一次 chat 调用 |
| TC-0017-2 | `POST /v1/chat/completions` JSON | 参数透传到 fake OpenAI-compatible upstream，返回 JSON |
| TC-0017-3 | `POST /v1/chat/completions` stream | `stream=true` 返回 `text/event-stream` |
| TC-0017-4 | `POST /v1/embeddings` | input 透传到 fake upstream，返回 embeddings JSON |
| TC-0017-5 | request.received | 鉴权成功后写结构化事件，不包含 prompt 明文 |
| TC-0017-6 | request_logs/usage_ledger | 成功请求写入租户、virtual_key、model、provider、usage 占位 |
| TC-0017-7 | 控制台 API 文档/调试台 | 展示 base_url、curl/Python/JS 示例，调试台可发送一次 chat |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0018-1 | 缺少 Authorization | 返回 401 `gateway.auth_missing` |
| TC-0018-2 | 无效 Virtual Key | 返回 401 `gateway.auth_invalid` |
| TC-0018-3 | 缺少 model | 返回 422 `gateway.model_missing` |
| TC-0018-4 | chat 缺少 messages | 返回 422 `gateway.messages_missing` |
| TC-0018-5 | embeddings 缺少 input | 返回 422 `gateway.input_missing` |
| TC-0018-6 | 当前租户模型不存在 | 返回 404 `gateway.model_not_found` |
| TC-0018-7 | 上游不可用 | 返回 503 `gateway.upstream_unavailable`，失败进入日志或 request_logs |
| TC-0018-8 | 上游非 2xx | 返回 502 `gateway.upstream_error`，不泄露 Provider Key |
| TC-0018-9 | 日志脱敏 | Authorization、Virtual Key、Provider Key、prompt/input 明文不出现在日志 |
| TC-0018-10 | 不支持接口 | `/v1/responses`、Assistants 路径返回统一 `infra.not_found` |

## 代码修改计划

已按以下顺序完成编码：

1. 扩展错误码注册表，新增 gateway API 兼容层错误码。
2. 新增 `gateway-data-plane/src/metahub_gateway/auth/virtual_key.py`：解析 Bearer、hash token、查询 active virtual_key 和 tenant。
3. 新增 `gateway-data-plane/src/metahub_gateway/routing/models.py`：按租户和 model alias 解析 active model/provider。
4. 新增 `gateway-data-plane/src/metahub_gateway/providers/openai_compatible.py`：最小 JSON/SSE 上游调用，Provider Key 从环境变量读取并脱敏。
5. 新增 `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`：注册 `/v1/chat/completions` 和 `/v1/embeddings`。
6. 在 `gateway-data-plane/src/metahub_gateway/app.py` 注册 OpenAI-compatible router。
7. 新增 usage/request log 写入辅助，成功和失败尽量写 `request_logs`、`usage_ledger`。
8. 新增后端集成测试：fake upstream、chat JSON、chat SSE、embeddings、SDK base_url、缺参、无效 Key、上游失败、日志脱敏。
9. 控制台新增 API 文档面板和最小调试台，错误展示 request_id/trace_id。
10. 新增前端 Node test，覆盖 API 文档渲染、调试台请求、统一错误展示和 Virtual Key 不持久化。
11. 更新 README、`docs/development/directory-map.md`、`docs/testing/PRD-009-tests.md`。
12. 运行 `scripts/ci/check.ps1`，重启本地三服务，使用 OpenAI Python/JS SDK 或等价 HTTP 客户端做真实调用验证。

## 实现摘要

后端实现：

- `POST /v1/chat/completions` 与 `POST /v1/embeddings` 已注册到 Gateway Data Plane。
- 数据面通过 `Authorization: Bearer <virtual_key>` 鉴权，Virtual Key 明文只在内存中哈希匹配。
- 路由只按 Virtual Key 恢复出的 `tenant_id` 查询当前租户 active 模型和 Provider，不接受请求体中的租户字段。
- OpenAI-compatible Provider Adapter 支持 JSON 非流式请求和 `text/event-stream` 流式透传。
- 成功请求写 `request_logs` 和 `usage_ledger`；参数、路由、上游错误等失败请求在鉴权成功后尽量写 `request_logs`。
- `request.received` 结构化事件只记录 endpoint/model/stream 等路由元数据，不记录 prompt、input、Virtual Key 或 Provider Key。
- Provider Key 从 `METAHUB_PROVIDER_<PROVIDER_CODE>_API_KEY` 读取，只进入上游请求头。

前端实现：

- 控制台新增 API 文档面板，展示 Gateway `/v1` base URL、Chat Completions、Embeddings、Python/JS SDK 最小示例。
- 调试台预留升级为最小 OpenAI 调试台，可输入 Virtual Key、model、user message、stream 开关并发送 chat 请求。
- 调试结果展示 JSON/SSE 摘要和 `request_id`/`trace_id`；统一错误展示 `code/message/suggestion`。
- 调试台不保存 Virtual Key，刷新后需要重新输入。

## 初步验证记录

- 后端专项测试：`gateway-data-plane/tests/integration/test_openai_compatible_api.py` 覆盖 chat JSON、chat SSE、embeddings、缺失/无效 Virtual Key、缺参数、上游 5xx、Provider URL 不可用、request_logs/usage_ledger 和敏感值不落库。
- 前端专项测试：`console-web/tests/unit/observabilityPanels.test.mjs`、`console-web/tests/unit/gatewayApi.test.mjs` 覆盖 API 文档、调试台渲染、JSON/SSE 请求和统一错误解析。
- 全量 `scripts/ci/check.ps1` 通过：后端 pytest `43 passed`，控制台 Node test `26 pass`，console lint `ok (26 files)`。
- 真实 HTTP 验收通过：本地 fake OpenAI-compatible upstream + Gateway 完成 chat JSON、chat SSE、embeddings、缺失 model、无效 Virtual Key；`request_logs` 记录 success/failed，`usage_ledger` 记录 token 和 `0 CNY` 成本占位。
- 浏览器验收通过：控制台登录 seed admin 后展示 API 文档和 OpenAI 调试台；页面调试台使用 `mhv_dev_seed_virtual_key` 调用 `default-chat` 返回 `live pong`，浏览器控制台无 warn/error。
