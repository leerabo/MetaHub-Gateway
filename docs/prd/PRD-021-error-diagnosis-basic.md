# PRD-021 - 智能错误提示基础版

状态：实现完成，已通过专项后端测试、前端测试、中文注释门禁和后端全量 pytest。

## 需求理解

PRD-021 属于 M0-W2/P0 的错误归因能力，目标不是做自动修复或大模型 RCA，而是在现有统一错误码、request_id、trace_id、request_logs 和 Provider Adapter 基础上，建立一套可解释、可测试、可脱敏的错误诊断规则库。

用户故事是：开发者或运维看到 401、429、5xx 等失败时，不只看到 `gateway.upstream_error` 这类通用错误，还能看到明确原因和可操作建议，例如“上游 Provider Key 失效”“Provider 限流”“模型不存在”“余额不足”“网关超时”“参数错误”。这些诊断需要同时出现在 API 错误响应、调用日志详情和调试台错误卡片中。

边界：

- M0 不做自动修复，不触发 Key 自动轮换、模型自动切换、预算自动扩容或工单创建。
- M0 不引入 LLM 做 RCA，不保存 Prompt、Response、Authorization、Provider Key 或 Virtual Key 明文。
- M0 的规则库以代码内静态规则为主，不做租户自定义规则管理；后续 M1/M2 如需运营配置再迁移到表。
- OpenRouter 和硅基流动仍只是对标/迁移兼容对象，不作为默认上游类型。

## 涉及模块

- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 扩展统一错误响应结构，增加 `diagnosis` 字段。
  - 新增更细粒度的上游错误码，例如上游鉴权失败、限流、余额不足、模型不存在、超时、参数错误。
- `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py`
  - 新增静态 `ErrorDiagnosisRule` 规则库、诊断 DTO 和 `diagnose_error`。
  - 统一按 `error_code/http_status/provider_error_type/message` 输出安全原因和建议。
- `shared-sdk/python/metahub_shared/runtime/fastapi_app.py`
  - 所有 `ApiError`、校验错误、404 和未捕获异常响应自动附加诊断。
  - 新增只读 `GET /error-codes`，返回错误码与诊断规则元数据。
- `gateway-data-plane/src/metahub_gateway/providers/openai_compatible.py`
  - 增强 `normalize_error`：按 HTTP 状态、Provider error type 和异常类型映射到更可操作的统一错误码。
  - Provider Key 明文仍只进入上游请求头，不进入诊断、日志或响应。
- `gateway-data-plane/src/metahub_gateway/providers/generic_http.py`
  - 继承 OpenAI-compatible 诊断行为，保持自定义 Endpoint 错误归一口径一致。
- `control-plane-api/src/metahub_control/request_logs/`
  - 调用日志详情增加 `error_diagnosis` 字段。
  - 列表可继续只显示 `error_code`，详情展示原因、建议和是否可重试。
- `control-plane-api/src/metahub_control/debug_console/`
  - 调试台后端复用统一错误响应，不单独拼装错误建议。
- `console-web/src/lib/*`
  - `AuthApiError` 与调试客户端保留后端返回的 `diagnosis`。
- `console-web/src/features/request-logs/renderRequestLogPanel.js`
  - 在失败日志详情中展示诊断卡片。
- `console-web/src/features/debug-console/renderDebugConsolePanel.js`
  - 错误卡片展示诊断原因、建议、可重试标记。
- `console-web/src/features/observability/renderObservabilityPanels.js`
  - 兼容旧最小调试台测试，错误响应可展示诊断字段。
- `tests`
  - 补共享诊断单测、Provider Adapter 错误归一测试、API 错误响应测试、调用日志详情测试和前端渲染测试。

## 数据表/字段设计

### ErrorDiagnosisRule

M0 不新增持久化规则表，`ErrorDiagnosisRule` 是共享运行时里的静态数据对象。原因：

- 规则面向全局统一错误语义，不是租户业务数据。
- PRD-021 不要求租户自定义规则或控制台编辑规则。
- 静态规则更容易与 `ERROR_SPECS`、单元测试和 Provider Adapter 合同保持一致。

字段设计：

- `rule_id TEXT`
- `error_code TEXT`
- `category TEXT`
- `reason TEXT`
- `suggestion TEXT`
- `action TEXT`
- `severity TEXT`
- `retryable BOOLEAN`
- `http_statuses list[int]`
- `provider_error_types list[str]`
- `keywords list[str]`
- `safe_summary_template TEXT`

### ErrorDiagnosis

API 响应和日志详情中的诊断 DTO，不落库：

- `rule_id`
- `category`
- `reason`
- `suggestion`
- `action`
- `severity`
- `retryable`
- `matched_by`
- `safe_summary`

`safe_summary` 只允许包含错误码、HTTP 状态、Provider code、request_id 等安全元数据，禁止包含上游原文、headers、Key、Prompt、Response。

### request_logs

M0 不扩展 `request_logs` 表。调用日志详情按已有字段派生诊断：

- `request_logs.error_code`
- `request_logs.provider_code`
- `routing_decisions.error_code`
- `routing_decisions.candidate_summary`

上游 401/429/402/404/408/504 等场景会在 Provider Adapter 归一为更细错误码后写入 `request_logs.error_code`，因此日志详情可以稳定派生诊断。

### error.diagnosed 事件

M0 不新增事件表；`error.diagnosed` 作为结构化日志事件输出。事件 payload 只包含：

- `request_id`
- `trace_id`
- `error_code`
- `diagnosis_rule_id`
- `category`
- `retryable`
- `http_status`
- `service_name`

没有租户上下文的 401/404/校验错误只写结构化日志，不写业务表；已经通过鉴权的数据面失败继续进入 `request_logs`。

## API设计

### 统一错误响应扩展

所有统一错误响应保持原字段：

- `code`
- `message`
- `request_id`
- `trace_id`
- `suggestion`

新增：

```json
{
  "diagnosis": {
    "rule_id": "upstream_rate_limited",
    "category": "provider_rate_limit",
    "reason": "上游 Provider 返回限流或配额保护。",
    "suggestion": "稍后重试，或在 Provider KeyPool 中启用其他可用 Key。",
    "action": "检查 Provider 控制台限流、KeyPool 状态和本租户流量峰值。",
    "severity": "warning",
    "retryable": true,
    "matched_by": "error_code",
    "safe_summary": "gateway.upstream_rate_limited"
  }
}
```

兼容策略：

- 旧客户端仍可读取顶层 `suggestion`。
- 新客户端优先展示 `diagnosis.reason/action/suggestion`。
- `diagnosis` 不包含敏感字段，不影响 Provider Key 和 Prompt 脱敏边界。

### `GET /error-codes`

用途：返回统一错误码、HTTP 状态、retryable、owner 和诊断规则说明，用于 API 文档、调试台和开发者排障。

权限：M0 作为只读服务元数据接口，随 Gateway 和 Control Plane 公共开放；它不返回租户业务数据，也不接受 `tenant_id`。

响应：

```json
{
  "error_codes": [
    {
      "code": "gateway.upstream_rate_limited",
      "http_status": 429,
      "retryable": true,
      "message": "Upstream provider is rate limited.",
      "suggestion": "Retry later or enable another provider key.",
      "owner": "gateway",
      "diagnosis": {
        "rule_id": "upstream_rate_limited",
        "category": "provider_rate_limit",
        "reason": "上游 Provider 返回限流或配额保护。",
        "suggestion": "稍后重试，或在 Provider KeyPool 中启用其他可用 Key。",
        "action": "检查 Provider 控制台限流、KeyPool 状态和本租户流量峰值。",
        "severity": "warning",
        "retryable": true,
        "matched_by": "error_code",
        "safe_summary": "gateway.upstream_rate_limited"
      }
    }
  ],
  "request_id": "req_x",
  "trace_id": "trc_x"
}
```

### `GET /request-logs/{request_id}`

扩展 `RequestLogDetail`：

```json
{
  "request_log": {
    "log": {
      "error_code": "gateway.upstream_rate_limited"
    },
    "error_diagnosis": {
      "rule_id": "upstream_rate_limited",
      "reason": "上游 Provider 返回限流或配额保护。",
      "action": "检查 Provider 控制台限流、KeyPool 状态和本租户流量峰值。"
    }
  }
}
```

权限继续使用 `require_request_log_reader`，只读当前认证上下文的 `tenant_id`。

## 前端页面/交互设计

### 调用日志详情

在失败日志详情摘要下方新增“错误诊断”区域：

- 原因：来自 `diagnosis.reason`
- 建议：来自 `diagnosis.suggestion`
- 操作：来自 `diagnosis.action`
- 标记：`retryable=true` 显示“可重试”；`severity=error/warning/info` 显示轻量状态标签

空态：

- 成功请求不展示诊断卡片。
- 未匹配规则时展示通用诊断：“未命中特定规则，请用 request_id 查看日志和上游摘要。”

### 调试台错误卡片

在现有错误码、message、request_id、trace_id、suggestion 后增加诊断摘要。调试台不展示上游原始错误正文，只展示安全归因。

### 错误码文档入口

M0 不做独立页面，先在 API 文档区域或开发者入口中复用 `GET /error-codes` 的数据；如果当期实现成本过高，前端仅保留客户端函数和单测，不把入口放进导航。

## 错误码设计

新增统一错误码：

- `gateway.upstream_auth_failed`
  - HTTP 502 或 401 映射策略：对客户端保持 502，避免把上游账号状态误解为本平台认证失败。
  - 原因：上游 Provider Key 失效、权限不足或账号被拒绝。
- `gateway.upstream_rate_limited`
  - HTTP 429。
  - 原因：Provider 限流、KeyPool 当前 Key 被上游限速或配额保护。
- `gateway.upstream_quota_exceeded`
  - HTTP 402 或上游错误类型包含 `insufficient_quota/quota/billing/balance`。
  - 原因：上游余额不足、额度不足或账单状态异常。
- `gateway.upstream_model_not_found`
  - HTTP 404 或上游错误类型包含 `model_not_found/model_not_exist`。
  - 原因：Provider 侧模型 ID 不存在、下线或当前 Key 无权限访问。
- `gateway.upstream_timeout`
  - HTTP 504。
  - 原因：网关访问上游超时或网络不可达。
- `gateway.upstream_request_invalid`
  - HTTP 502 或 422 映射策略：请求已通过本网关基础校验，但被上游判定为参数错误。
  - 原因：厂商字段兼容性、模型能力限制或参数取值不符合上游要求。

保留并继续诊断：

- `gateway.upstream_error`
- `gateway.upstream_unavailable`
- `gateway.model_not_found`
- `gateway.messages_missing`
- `gateway.request_invalid`
- `provider_key.unavailable`
- `auth.permission_denied`
- `infra.unhandled`

未知错误策略：

- 不拼接上游错误原文到响应。
- `diagnosis.rule_id=unknown_error`
- `safe_summary` 只包含 `error_code` 和可公开 HTTP 状态。

## 安全与权限约束

- 诊断规则库是全局静态元数据，不包含租户数据和密钥，不需要 `tenant_id`。
- 调用日志详情仍只允许 admin/developer 读取当前租户数据，viewer 后端拒绝。
- 诊断输出不得包含 Provider Key、Virtual Key 明文、Authorization、Prompt、Response、messages、input、数据库 URL 或堆栈。
- Provider Adapter 只能把上游 HTTP 状态、错误类型和安全分类交给诊断模块，不把 response body 原文写入日志或响应。
- 结构化 `error.diagnosed` 日志只输出安全元数据，不能输出上游 headers 或原始请求体。
- 前端必须 HTML 转义诊断字段，模型或上游错误文本不能直接插入 DOM。

## 测试用例

### 主流程

- `diagnose_error("gateway.upstream_rate_limited")` 返回 Provider 限流原因、建议、可重试标记。
- `GET /error-codes` 返回所有错误码，并给典型错误带上 `diagnosis`。
- Gateway 上游 429 返回 `gateway.upstream_rate_limited`，API 响应包含诊断字段。
- Gateway 上游 401/403 返回 `gateway.upstream_auth_failed`，响应和日志不包含 Provider Key。
- Request Log 详情对失败请求返回 `error_diagnosis`。
- 调试台错误卡片展示诊断原因和建议。

### 异常/权限/安全

- 未知错误码返回 `unknown_error` 诊断，不暴露堆栈和上游正文。
- HTTP timeout 映射为 `gateway.upstream_timeout`，retryable 为 true。
- 上游 402 或 `insufficient_quota` 映射为 `gateway.upstream_quota_exceeded`。
- 上游 404 或 `model_not_found` 映射为 `gateway.upstream_model_not_found`。
- 上游 400/422 映射为 `gateway.upstream_request_invalid`。
- viewer 访问 request log 详情仍返回 `auth.permission_denied`。
- `diagnosis.safe_summary` 和结构化日志不包含 `sk-`、`Authorization`、Prompt、Response、messages。
- 前端渲染含 `<script>` 的诊断字段时必须转义。

## 代码修改计划

1. 新增 `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py`。
2. 扩展 `errors.py`：
   - 增加 `ErrorDiagnosisInfo`。
   - `ErrorResponse` 和 `ErrorCodeInfo` 增加 `diagnosis`。
   - 新增上游细分错误码。
3. 修改 `fastapi_app.py`：
   - `_error_response` 调用 `diagnose_error`。
   - 记录 `error.diagnosed` 安全结构化日志。
   - 增加 `GET /error-codes`。
4. 修改 `OpenAICompatibleAdapter.normalize_error`：
   - HTTP 401/403、429、402、404、408/504、400/422 和 TimeoutException 映射到细分错误码。
   - 保持 Provider Key 不进入日志或响应。
5. 修改 request log models/service：
   - `RequestLogDetail` 增加 `error_diagnosis`。
   - 详情读取时按 `log.error_code` 派生诊断。
6. 修改前端错误模型与渲染：
   - `AuthApiError`、`GatewayApiError` 保存 `diagnosis`。
   - request log 详情和 debug console 展示诊断卡片。
7. 补测试：
   - 共享诊断规则测试。
   - 统一错误响应和 `/error-codes` 测试。
   - Adapter 错误归一测试。
   - Request Log 详情诊断测试。
   - 前端诊断渲染与 HTML 转义测试。
8. 更新 README、目录边界和 `docs/testing/PRD-021-tests.md`。

## 自审 1：需求完整性

- 已覆盖 401、429、5xx、Key 失效、Provider 限流、模型不存在、余额不足、网关超时、参数错误。
- API 响应、调用日志详情和调试台错误卡片都有诊断展示路径。
- 设计保留 `request_id/trace_id/suggestion` 原有契约，同时新增结构化 `diagnosis`。
- 未越界到自动修复、RCA 模型、租户自定义规则或复杂 Trace 可视化。

结论：需求范围完整，符合 M0-W2/P0 的基础版定位。

## 自审 2：安全、可维护性与实现风险

- 最大兼容风险是扩展错误响应会影响现有精确 JSON 断言；编码时需要同步更新测试并保证旧字段不变。
- 最大归因风险是仅靠 `gateway.upstream_error` 无法区分 429/401/402；设计通过 Provider Adapter 提前归一为细分错误码解决。
- 最大安全风险是上游 response body 可能含密钥、账号信息或 Prompt；设计要求诊断只使用 HTTP 状态、错误类型和关键词，不把原文传给响应。
- 最大维护风险是错误码和诊断规则重复；设计把规则集中到 `error_diagnosis.py`，`ERROR_SPECS` 只保留 HTTP/message/suggestion 元信息。

结论：可以进入编码，但必须把“诊断不泄漏上游原文/密钥”和“失败请求日志详情能显示诊断”作为硬验收。
