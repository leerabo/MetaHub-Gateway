# PRD-013 - Provider Adapter抽象接口设计

状态：已完成编码与验证。

完成记录（2026-07-08）：已实现 Provider Adapter 合同、OpenAI-compatible Adapter、通用 HTTP 配置 Adapter、Adapter 工厂、数据面主流程 Adapter 化、usage 归一化和 `provider_adapter.error` 安全日志。验证结果：`scripts/ci/check.ps1` 通过，后端 57 passed；控制台 41 passed；lint 35 files；真实 HTTP 验证 `default-chat` 经 Adapter 转发到 `local-chat-model`，`usage_ledger.raw_usage` 保留上游原始 usage，`normalized_usage` 保留归一化 token 字段。

设计来源：Excel `PRD-013 - Provider Adapter抽象接口`。阶段 `M0-W1`，优先级 `P0`，模块 `Provider Adapter`。范围是定义 `ProviderAdapter` 内部接口：`build_request`、`send_non_stream`、`send_stream`、`parse_usage`、`normalize_error`；实现 OpenAI 兼容 Adapter 和通用 HTTP 配置 Adapter。验收要求：新增 OpenAI-compatible Endpoint 不改 DataPlane 核心流程，错误和 usage 能归一化。

设计自审 1（需求完整性）：本 PRD 是研发内部抽象，不新增页面和公开管理 API。必须覆盖 Adapter 合同、OpenAI-compatible 实现、通用 HTTP 配置实现、usage 归一化、错误归一化和 `provider_adapter.error` 结构化事件。

设计自审 2（实现风险）：现有数据面已经能稳定透传 OpenAI-compatible JSON/SSE，重构不能改变 `/v1/chat/completions`、`/v1/embeddings`、request_logs、usage_ledger 和 Provider Key 脱敏行为。实现上应保留旧函数兼容包装，但数据面主流程改为依赖 Adapter 工厂和 `NormalizedResponse`。

参考边界：用户允许在界面和部分功能设计上参考 GitHub 开源项目 New API 与 Sub2Api。PRD-013 仅参考其“多上游统一网关、Key 管理、用量/计费、请求转发”方向上的产品分层思路，不复制外部源码、不引入其许可证约束，也不改变本项目 M0/M1 的中国境内企业 MVP 边界。

## 需求理解

当前网关数据面已经能调用 OpenAI-compatible Provider，但调用逻辑散落在 `metahub_gateway.providers.openai_compatible` 的函数里。后续新增厂商时，如果继续把 HTTP 构造、请求发送、usage 解析和错误处理写进主流程，会让数据面热路径越来越硬编码。

PRD-013 的目标是建立稳定的 Provider Adapter 内部合同：

- `build_request`：根据路由、endpoint 和原始 payload 构造上游请求 URL、headers、payload。
- `send_non_stream`：发送非流式请求，并返回标准化响应容器。
- `send_stream`：打开流式请求，返回可透传的 SSE/字节流响应。
- `parse_usage`：把不同上游 usage 字段归一为 `input_tokens/output_tokens/total_tokens`。
- `normalize_error`：把上游 HTTP/JSON 错误归一为安全的错误摘要，不泄漏 Provider Key。

明确不做：

- M0 不做所有国内厂商深度字段适配。
- M0 不做 Responses API、Assistants API 或多协议自动协商。
- M0 不做流式 usage 解析，流式请求仍只记录 request_logs。
- M0 不引入复杂插件系统或动态加载外部代码。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| Adapter 合同 | `gateway-data-plane/src/metahub_gateway/providers/base.py` | 新增 DTO、Protocol、标准响应和错误对象 |
| OpenAI Adapter | `gateway-data-plane/src/metahub_gateway/providers/openai_compatible.py` | 从函数式调用升级为 `OpenAICompatibleAdapter`，保留兼容包装 |
| 通用 HTTP Adapter | `gateway-data-plane/src/metahub_gateway/providers/generic_http.py` | 新增配置型 JSON/SSE Adapter 占位实现 |
| Adapter 工厂 | `gateway-data-plane/src/metahub_gateway/providers/factory.py` | 按 `provider_type` 选择 Adapter |
| 数据面路由对象 | `gateway-data-plane/src/metahub_gateway/routing/models.py` | 补充 `provider_type` 供 Adapter 工厂判断 |
| OpenAI-compatible API | `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py` | 主流程通过 Adapter 构造请求、发送、记录 usage |
| 测试 | `gateway-data-plane/tests/unit/`、`gateway-data-plane/tests/integration/` | Adapter 单测、usage/error 归一化、主流程回归 |

## 数据表/字段设计

本 PRD 不新增数据库表和字段。

使用既有数据：

- `providers.provider_type`：PRD-011 已加入，用于选择 Adapter。
- `providers.provider_code/base_url/status`：用于构建 Provider 请求。
- `models.provider_model_id`：PRD-012 已加入，OpenAI Adapter 构造 payload 时替换请求体 `model`。
- `usage_ledger.raw_usage/normalized_usage/input_tokens/output_tokens`：继续记录 usage，输入来源改为 Adapter 的 `NormalizedUsage`。

## API设计

无新增公开 HTTP API。新增内部 Python 合同：

```python
class ProviderAdapter(Protocol):
    name: str

    def build_request(self, route: GatewayRoute, endpoint: str, payload: dict[str, Any]) -> ProviderRequest: ...
    async def send_non_stream(self, request: ProviderRequest) -> NormalizedResponse: ...
    async def send_stream(self, request: ProviderRequest) -> ProviderStreamResponse: ...
    def parse_usage(self, payload: Mapping[str, Any]) -> NormalizedUsage: ...
    def normalize_error(self, response: httpx.Response | None, exc: Exception | None = None) -> NormalizedProviderError: ...
```

DTO：

- `ProviderRequest`：`provider_code`、`provider_type`、`url`、`headers`、`payload`、`endpoint`。
- `NormalizedUsage`：`input_tokens`、`output_tokens`、`total_tokens`、`raw_usage`。
- `NormalizedResponse`：`payload`、`usage`、`status_code`。
- `ProviderStreamResponse`：`client`、`response`、`stream_context`。
- `NormalizedProviderError`：`code`、`message`、`http_status`、`retryable`、`provider_error_type`。

## 前端页面/交互设计

无前端页面。本 PRD 是数据面内部抽象，控制台不新增入口。

## 错误码设计

沿用现有数据面错误码：

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `gateway.upstream_unavailable` | 503 | URL 非法、网络错误、超时、连接失败 |
| `gateway.upstream_error` | 502 | 上游返回非 2xx、非 JSON、错误响应无法归一 |
| `gateway.stream_not_supported` | 422 | 非流式接口收到流式请求或模型不支持 |

结构化事件：

- `provider_adapter.error`
  - `request_id`
  - `trace_id`
  - `tenant_id`
  - `provider_id`
  - `provider_code`
  - `adapter`
  - `http_status`
  - `error_code`
  - `retryable`

事件 payload 不记录 Authorization、Provider Key、prompt、input 或 message。

## 安全与权限约束

- Provider Key 仍从环境变量读取，只进入上游 Authorization header。
- Adapter 错误日志只记录 provider_code、HTTP 状态和安全错误摘要。
- Adapter 不读取或信任请求体中的 `tenant_id`。
- 新增 Adapter 只能通过 `GatewayRoute` 获得当前租户内已解析的 Provider 和模型信息。
- 数据面失败仍尽量写入 `request_logs`，确保失败请求可追踪。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-PRD013-1 | OpenAI Adapter `build_request` | URL、headers、`model -> provider_model_id` 替换正确 |
| TC-PRD013-2 | OpenAI Adapter 非流式发送 | 返回 `NormalizedResponse`，usage 归一化 |
| TC-PRD013-3 | 通用 HTTP Adapter | 可构造并发送 JSON 请求，usage 使用通用字段归一 |
| TC-PRD013-4 | 数据面主流程 | 现有 chat/embeddings 集成测试不改调用方仍通过 |

### 异常/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-PRD013-5 | 上游 4xx/5xx | 返回 `gateway.upstream_error`，触发 `provider_adapter.error` |
| TC-PRD013-6 | 上游不可达/URL 非法 | 返回 `gateway.upstream_unavailable` |
| TC-PRD013-7 | usage 字段兼容 | `prompt_tokens/completion_tokens` 和 `input_tokens/output_tokens` 均能解析 |
| TC-PRD013-8 | 日志安全 | Adapter 日志不含 Provider Key、Authorization、prompt/input |

## 代码修改计划

1. 新增 `providers/base.py`，定义 Adapter Protocol 和标准 DTO。
2. 重构 `providers/openai_compatible.py`，实现 `OpenAICompatibleAdapter`，保留 `post_json/open_stream/iter_sse` 包装函数。
3. 新增 `providers/generic_http.py`，实现通用 HTTP 配置 Adapter。
4. 新增 `providers/factory.py`，按 `GatewayRoute.provider_type` 选择 Adapter。
5. 扩展 `GatewayRoute` 查询 `providers.provider_type`。
6. 修改 `api/openai_compatible.py`，主流程使用 Adapter 的 `build_request/send_non_stream/send_stream/parse_usage`。
7. 增加 Adapter 单元测试，覆盖请求构造、usage 解析、错误归一化和日志安全。
8. 运行既有 OpenAI-compatible 集成测试，证明主流程兼容。
9. 更新 README、目录边界和测试说明。
