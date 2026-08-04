# PRD-032 - Provider 计费适配规范 MVP

## 文档状态

- 阶段：M0.5-W3/4
- 优先级：P0
- 当前状态：已实现并通过聚焦测试、前端测试与中文注释门禁
- 依赖：PRD-013 Provider Adapter、PRD-017 Usage Ledger、PRD-031 价格表与生效时间
- 边界：只处理文本 Token usage，不处理图片、语音、视频或复杂阶梯计费

## 需求理解

不同 Provider 对 Token 用量使用不同字段名和嵌套结构。当前 Adapter 已能识别 OpenAI 的 `prompt_tokens/completion_tokens` 和通用 `input_tokens/output_tokens`，但缺少统一成本/币种字段、Provider 上报成本口径、严格 raw usage 脱敏，以及可供用量详情排查的安全 raw usage。

PRD-032 要形成所有 Adapter 必须遵守的内部 `parse_usage` 标准：

1. 输出统一 `NormalizedUsage`：输入、输出、总 Token、缓存 Token、Provider 上报成本、币种、人民币成本、估算标记、归一警告和脱敏 raw usage。
2. 至少支持两类不同响应：OpenAI-compatible `usage` 与 Gemini 风格 `usageMetadata/usage_metadata`；同时保留通用 input/output 和缓存扩展兼容。
3. Provider 原始 usage 必须留存用于排查，但需限制数据类型、大小和深度，并删除 Authorization、API Key、Secret、Password 等敏感字段。
4. Adapter 上报成本与 MetaHub 根据 PricePlan 计算的内部成本分开保存。USD 或未知币种不做隐式换汇，不能伪造 `cost_cny`。
5. 字段缺失时继续使用 PRD-017 的文本长度估算，并明确 `estimated/estimate_reason`。
6. 每次成功归一输出 `usage.normalized` 结构化事件；事件不包含 raw usage 和请求/响应正文。

## 涉及模块

- `shared-sdk/python/metahub_shared/runtime/usage.py`：Provider raw usage 通用脱敏与体积限制。
- `gateway-data-plane/src/metahub_gateway/providers/base.py`：扩展 `NormalizedUsage` 契约。
- `gateway-data-plane/src/metahub_gateway/providers/openai_compatible.py`：多 dialect `parse_usage` 与 `usage.normalized` 事件。
- `gateway-data-plane/src/metahub_gateway/usage/ledger.py`：保存脱敏 raw usage 和 Provider/内部成本双口径。
- `control-plane-api/src/metahub_control/usage/`：用量记录返回再次脱敏的 `provider_raw_usage`。
- `console-web/src/features/usage/renderUsagePanel.js`：单条记录的规范化/Provider raw usage 详情。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：登记 `usage.normalized` 事件类型，当前数据面只写结构化事件，不同步阻塞调用主链路。
- `gateway-data-plane/tests/unit/test_provider_adapters.py`：至少两类 Provider 响应、缺失字段、币种和敏感字段测试。

## 数据表/字段设计

本 PRD 不新增业务表。`usage_ledger` 已具备：

- `input_tokens/output_tokens/total_tokens/cache_tokens`
- `cost_cny/cost_usd/currency`
- `raw_usage/normalized_usage`
- `estimated/usage_source/price_snapshot_id`

字段语义调整：

- `raw_usage`：只保存 Adapter 从 Provider usage 子对象提取并脱敏后的 JSON，不保存整个响应。
- `normalized_usage`：增加 `provider_cost_amount`、`provider_cost_currency`、`provider_cost_cny`、`normalization_warnings`；已有 `cost_cny/cost_usd/currency` 仍表示 MetaHub PricePlan 内部归一成本。
- `provider_cost_cny` 仅在 Provider 明确上报 CNY 时赋值。USD 成本保留原金额和 `USD` 币种，并加入 `currency_conversion_unavailable`，不做换汇。

### `NormalizedUsage` 内部契约

| 字段 | 类型 | 说明 |
|---|---|---|
| `input_tokens` | int | 非负输入 Token |
| `output_tokens` | int | 非负输出 Token |
| `total_tokens` | int | 上游总量或输入+输出 |
| `cache_tokens` | int | 缓存读取/创建 Token 归一总量 |
| `cost_cny` | Decimal/null | Provider 明确上报的 CNY 成本 |
| `currency` | str/null | Provider 上报成本币种 |
| `provider_cost_amount` | Decimal/null | Provider 原币成本 |
| `estimated` | bool | 是否本地估算 Token |
| `estimate_reason` | str/null | 估算原因 |
| `normalization_warnings` | tuple[str] | 缺失/非法/不换汇等安全警告 |
| `provider_raw_usage` | dict | 脱敏、限深、限长后的 Provider usage |

`raw_usage` 属性保留兼容别名，避免破坏 PRD-013/017 既有调用方；新代码和 API 使用 `provider_raw_usage` 名称。

## API 设计

### 内部 `ProviderAdapter.parse_usage(payload) -> NormalizedUsage`

不新增外部 HTTP API。解析优先级：

1. `payload.usage`
2. `payload.usage_metadata`
3. `payload.usageMetadata`
4. `payload.output.usage`

支持字段：

- OpenAI：`prompt_tokens/completion_tokens/total_tokens`
- 通用/Anthropic：`input_tokens/output_tokens`
- Gemini：`promptTokenCount/candidatesTokenCount/totalTokenCount/cachedContentTokenCount` 及 snake_case
- 缓存扩展：`cache_tokens/cached_tokens/cache_read_input_tokens/cache_creation_input_tokens` 和 details 子对象
- 成本扩展：`cost_cny`，或 `cost/total_cost/cost_amount + currency`

非法、负数或非有限 Token/成本按安全规则归零或忽略，并加入警告；绝不抛出上游原始字段到 API。

### `GET /usage/records` 响应扩展

每条记录增加：

```json
{
  "provider_raw_usage": {
    "prompt_tokens": 11,
    "completion_tokens": 7
  },
  "normalized_usage": {
    "input_tokens": 11,
    "output_tokens": 7,
    "provider_cost_amount": null,
    "provider_cost_currency": null,
    "normalization_warnings": []
  }
}
```

控制面读取时再次执行脱敏，保证 PRD-017 之前写入的历史 raw usage 也不会把测试 Secret 或厂商私有凭据返回前端。

## 前端页面/交互设计

- 复用“用量与成本”页面，不新增导航。
- 每条 Usage 增加可展开“归一化详情”，展示统一 Token、内部成本、Provider 上报成本、币种、警告和安全 raw usage JSON。
- raw usage 只读、格式化、HTML 转义；不提供编辑、复制整条响应或下载原始响应。
- 默认表格仍保持紧凑，详情使用原生 `<details>` 按需展开，避免每条记录高度膨胀。
- estimated 记录继续显示估算标识和原因。

## 错误码设计

内部 `parse_usage` 对单个 Provider 字段异常采用可追踪降级，不新增用户可见失败错误码：

- 字段缺失：返回 estimated usage，`estimate_reason=missing_upstream_usage`。
- 非法 Token：对应字段归零，记录 `invalid_*_tokens` warning。
- 非法成本：忽略 Provider 成本，记录 `invalid_provider_cost`。
- USD/未知币种：不换汇，记录 `currency_conversion_unavailable` 或 `unsupported_provider_currency`。

已有 Gateway/Usage API 错误继续使用统一错误体和 `request_id/trace_id`。raw usage 解析或持久化异常由 `usage_ledger.write_failed` 安全日志追踪，不泄露原值。

## 安全与权限约束

1. 只提取 usage 子对象，不保存整个 Provider 响应、Prompt、Response、headers 或 Authorization。
2. raw usage 在 Adapter 写入前和控制面返回前双重脱敏。
3. 敏感键使用精确/后缀规则识别，不能误删 `input_tokens` 等正常计费字段。
4. raw usage 限制最大深度、键数量、数组长度和字符串长度，防止日志/数据库膨胀。
5. `usage.normalized` 事件只记录 Token、币种、估算、warning 代码和 Provider 元数据，不记录 raw usage。
6. 内部成本只由 PRD-031 PricePlan 计算；Provider 上报成本不能覆盖预算和内部账本成本。
7. 用量读取继续由后端 `require_usage_reader` 校验 admin/developer，viewer 无法通过手写请求读取 raw usage。

## 测试用例

### 主流程

1. OpenAI usage 归一为 input/output/total/cache Token，raw usage 保留安全字段。
2. Gemini `usageMetadata` 归一为同一结构。
3. 通用 input/output 和 Anthropic 缓存字段归一。
4. CNY Provider 成本写入 provider cost 字段；内部 PricePlan 成本保持独立。
5. USD Provider 成本保留原币金额，不生成虚假 CNY 换算。
6. Usage Ledger 保存安全 raw usage，控制台详情展示 normalized/raw 双口径。
7. 记录 `usage.normalized` 事件并携带 request_id/trace_id。

### 异常/权限/安全

1. usage 缺失时估算 Token，不保存请求/响应原文。
2. 负数、字符串、NaN、Infinity、总量小于输入+输出等异常字段安全归一并记录 warning。
3. raw usage 中 `api_key/authorization/access_token/secret/password` 被删除或替换，`input_tokens` 不被误删。
4. 超深、超长和超大数组被截断，数据库和 API 响应保持有界。
5. 历史 raw usage 即使含敏感字段，控制面响应也二次脱敏。
6. viewer 调用 `/usage/records` 仍返回 `auth.permission_denied`。

## 代码修改计划

1. 新增共享 raw usage 脱敏器及单元测试。
2. 扩展 `NormalizedUsage` 契约与账本 payload。
3. 重构 Adapter `parse_usage`，支持 OpenAI、Gemini、通用/Anthropic 字段与成本口径。
4. 扩展 Usage Ledger normalized JSON 和 `usage.normalized` 结构化事件。
5. 扩展控制面 Usage DTO/查询与前端可展开详情。
6. 更新 README、目录和测试记录，执行注释、编译、聚焦与全量门禁。

## 第一轮设计自审

结论：修订后通过。

1. 原方案若让 Provider 上报成本覆盖 `usage_ledger.cost_cny`，会绕过 PRD-031 PricePlan 和预算口径；已改为 Provider 成本仅进入 normalized JSON，内部成本保持权威。
2. 原方案直接返回数据库 raw usage 会泄露历史测试 Secret；已增加 Adapter 写入前与控制面读取前双层脱敏。
3. 仅测试 OpenAI 的两种字段名不足以证明“两类 Provider”；已明确加入 Gemini `usageMetadata` dialect。
4. `usage.normalized` 不应同步发送 Webhook 阻塞数据面热路径；本 PRD 只登记事件类型并写结构化事件，异步事件总线由后续能力接入。

## 第二轮设计自审

结论：通过，可以编码。

1. `NormalizedUsage` 保留 `raw_usage` 兼容属性，新字段不会破坏现有 Adapter 与账本调用。
2. 成本和 Token 使用 Decimal/非负整数，NaN、Infinity 和负值均有确定降级语义。
3. 币种转换边界明确：只识别币种，不做隐式汇率；USD 金额可追溯但 `provider_cost_cny` 为空。
4. 不扩展图片、语音、视频、工具计费、阶梯价或自动抓价，符合 M0.5 范围。
5. 用量详情只展示安全 usage 子对象，不展示完整 Provider 响应或任何认证信息。

## 实现结果

1. 新增共享 `sanitize_provider_usage`，对 raw usage 执行敏感键脱敏、最大 4 层递归、64 个键、32 个数组项和 256 字符串长度限制。
2. `NormalizedUsage` 已扩展 Provider 原币成本、CNY 成本、币种、警告和 `provider_raw_usage` 标准访问属性，并保持旧 `raw_usage` 构造兼容。
3. Adapter 已支持 OpenAI、通用/Anthropic、Gemini 和 `output.usage` 方言；上游只有成本而缺少 Token 字段时仍会保留安全成本数据并估算 Token。
4. Usage Ledger 同时保存 PricePlan 内部成本和 Provider 自报成本，两套口径互不覆盖；raw usage 在 Adapter 与账本入口均执行脱敏。
5. `GET /usage/records` 返回再次脱敏的 `provider_raw_usage`，原有后端 admin/developer 权限约束保持不变。
6. 用量页面增加按记录展开的归一化详情，展示双成本口径、警告及安全 raw usage；所有动态 JSON 均经过 HTML 转义。
7. 每次非流式成功响应记录安全 `usage.normalized` 结构化事件，携带 request_id、trace_id、tenant_id 和 Provider 元数据，不携带 raw usage。

测试结果详见 `docs/testing/PRD-032-tests.md`。
