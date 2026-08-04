# PRD-020 - 内置API调试控制台

状态：实现完成，已通过专项测试、前端测试、中文注释门禁和后端全量 pytest。

## 实现摘要

PRD-020 已按设计落地为控制面低频调试能力：后端新增 `POST /debug/chat-completions`，前端新增内置 API 调试台，数据库新增 `debug_console_events` 安全事件表。调试入口只接受当前登录用户选择的 `virtual_key_id` 与 `model_alias`，不接收 Virtual Key 明文、Provider Key、Authorization 或前端传入的 `tenant_id`；后端按认证上下文恢复租户、校验 active Virtual Key、复用数据面路由、Adapter、`request_logs` 与 `usage_ledger` 写入链路。

实现特别保留以下安全边界：`debug_console_events` 只保存安全元数据，不保存 Prompt、messages content、完整 response、Virtual Key 明文、Provider Key 或 Authorization；viewer 直接调用后端会返回 `auth.permission_denied`；成功响应提供 `log_detail_url`，控制台可跳转到调用日志详情。

## 需求理解

PRD-020 属于 M0-W2/P0 的 API 调试台能力。目标是在控制台内提供一个类似 Postman 的最小调试页：开发者或售前可以选择当前租户的 Virtual Key、模型别名，编辑 messages，发送一次非流式 Chat Completions 请求，并在页面看到响应、request_id、trace_id、延迟、usage、成本和错误建议。

本 PRD 和 PRD-019 快速开始的差异：

- PRD-019 偏向复制代码到本地执行，Key 用环境变量占位。
- PRD-020 偏向控制台内即时调试，用户选择已有 Virtual Key 元数据，不要求输入或回显明文 Key。
- 调试调用必须进入 request_logs/usage_ledger，成功后可跳到日志详情。

必须满足：

- 新增 `POST /debug/chat-completions`。
- 支持选择 Virtual Key 和模型别名。
- 支持编辑 OpenAI-compatible `messages`。
- 发送非流式 chat 请求，M0 不做 stream。
- 成功时展示响应、request_id、trace_id、latency_ms、usage 和成本摘要。
- 失败时展示统一错误码、request_id、trace_id 和 suggestion。
- 触发 `debug.request_sent` 事件。
- 调试请求和失败请求必须可追踪。

关键安全判断：

- 控制面不能还原 Virtual Key 明文，因为数据库只保存不可逆 `key_hash`。
- 因此本 PRD 不通过“拿明文 Key 再 HTTP 调 Gateway”的方式实现。
- 调试接口由控制面低频编排，按登录用户和所选 active Virtual Key 元数据恢复 `tenant_id/environment/virtual_key_id`，复用数据面的路由、Provider Adapter、request_logs 和 usage_ledger 写入能力。

边界：

- M0 不做集合管理、团队共享调试用例、历史请求保存、变量环境、批量测试、断言脚本、前置/后置脚本或导入 Postman Collection。
- M0 不做流式调试，不做长文本 Prompt 存档，不保存完整 Prompt/Response 明文。
- OpenRouter 和硅基流动仍只作为对标或迁移兼容对象，不作为默认模型厂家。
- 默认上游仍是模型厂商、云端模型 Endpoint 或客户本地 OpenAI-compatible Endpoint。

## 涉及模块

- `control-plane-api/src/metahub_control/debug_console/`
  - 新增调试台后端模块，承载 DebugRequest、DebugResult、调试编排和事件写入。
- `control-plane-api/src/metahub_control/auth/dependencies.py`
  - 新增或复用 admin/developer 调试权限依赖；viewer 后端拒绝。
- `gateway-data-plane/src/metahub_gateway/routing/models.py`
  - 复用 `resolve_model_route`，按当前租户、模型别名、Virtual Key 环境解析路由。
- `gateway-data-plane/src/metahub_gateway/providers/`
  - 复用 `adapter_for_route`、`ProviderAdapter` 和 KeyPool 选择逻辑。
- `gateway-data-plane/src/metahub_gateway/usage/ledger.py`
  - 复用 `RequestLogContext`、`safe_record_request_log`、`safe_record_usage_ledger`。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 新增 debug 调试台错误码。
- `infra/migrations`
  - 新增 `debug_console_events` 表迁移，记录 `debug.request_sent` 安全事件。
- `console-web/src/lib/debugConsole.js`
  - 新增调试接口客户端，不发送 `tenant_id` 或任何 Provider Key。
- `console-web/src/features/debug-console/`
  - 新增内置 API 调试台页面，替换 PRD-009 的最小手输 Key 调试台。
- `console-web/src/features/developer-start/renderDeveloperStart.js`
  - active item 为 `debug-console` 时渲染新版调试台。
- `console-web/src/app/main.js`
  - 加载 Virtual Key 和模型别名状态，绑定调试表单发送事件，支持跳转调用日志详情。
- `console-web/src/styles/main.css`
  - 增加调试台布局、messages 编辑器、响应视图、usage/cost 卡片和错误提示样式。
- `tests`
  - 新增后端集成测试和前端 Node 单元测试。

## 数据表/字段设计

### `debug_console_events`

用于记录调试台最小安全事件。完整 Prompt/Response 不入库；真实调用明细仍通过 `request_logs` 和 `usage_ledger` 追踪。

- `event_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `user_id TEXT NOT NULL`
- `session_id TEXT`
- `event_type TEXT NOT NULL CHECK (event_type IN ('debug.request_sent'))`
- `debug_request_id TEXT NOT NULL`
- `gateway_request_id TEXT NOT NULL`
- `trace_id TEXT`
- `virtual_key_id TEXT`
- `model_alias TEXT NOT NULL`
- `status TEXT NOT NULL CHECK (status IN ('success', 'failed'))`
- `error_code TEXT`
- `latency_ms INTEGER NOT NULL DEFAULT 0`
- `payload_json TEXT NOT NULL DEFAULT '{}'`
- `created_at TEXT NOT NULL`
- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `FOREIGN KEY (user_id) REFERENCES users(user_id)`
- `FOREIGN KEY (session_id) REFERENCES sessions(session_id)`
- `FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id)`

索引：

- `idx_debug_console_events_tenant_time(tenant_id, created_at DESC)`
- `idx_debug_console_events_request_id(tenant_id, gateway_request_id)`
- `idx_debug_console_events_user_time(tenant_id, user_id, created_at DESC)`

说明：

- `debug_request_id` 是调试接口自身生成的 ID，用于页面局部追踪。
- `gateway_request_id` 复用本次调试调用写入 `request_logs` 的 request_id，成功后前端可跳到日志详情。
- `payload_json` 只允许安全元数据，例如 `message_count`、`model_alias`、`usage_source`、`estimated`，不得包含 message content、Prompt、Response、Virtual Key 明文、Provider Key 或 Authorization。

### `DebugRequest`

API 请求 DTO，不落表。

- `virtual_key_id`
- `model_alias`
- `messages`
- `temperature`
- `max_tokens`

`messages` 子项：

- `role`: `system | user | assistant`
- `content`: 字符串，M0 限制单条长度和总条数，防止调试台被当成大文本存储通道。

说明：

- 不接收 `tenant_id`。
- 不接收 Virtual Key 明文。
- 不接收 Provider Key 或 Authorization。
- M0 强制 `stream=false`。

### `DebugResult`

API 成功响应 DTO。

- `debug_request_id`
- `request_id`
- `trace_id`
- `status`
- `latency_ms`
- `virtual_key_id`
- `virtual_key_prefix`
- `model_alias`
- `resolved_model`
- `provider_code`
- `response_json`
- `response_preview`
- `usage`
- `cost`
- `log_detail_url`

说明：

- `response_json` 只返回给当前调用方，不写入调试事件表。
- `response_preview` 做长度限制，避免前端长响应撑爆布局。
- `usage/cost` 优先从 `usage_ledger` 读取写账结果；写账失败时可用 Adapter 返回 usage 做降级展示。

## API设计

### `POST /debug/chat-completions`

用途：控制台内发起一次低频非流式 Chat Completions 调试调用。

权限：当前用户必须为 `admin` 或 `developer`。`viewer` 返回 `auth.permission_denied`。

请求示例：

```json
{
  "virtual_key_id": "vk_x",
  "model_alias": "company-default",
  "messages": [
    {"role": "user", "content": "你好，MetaHub"}
  ],
  "temperature": 0.7,
  "max_tokens": 512
}
```

后端处理流程：

1. 从登录态恢复 `tenant_id/user_id/session_id`。
2. 校验 `virtual_key_id` 属于当前租户且 `status=active`，读取 `environment/key_prefix`，不读取 `key_hash`。
3. 校验 `model_alias` 和 `messages`。
4. 调用 `resolve_model_route(config, tenant_id, model_alias, environment, request_id, trace_id, virtual_key_id)`。
5. 使用 `adapter_for_route(route)` 构造 Provider 请求。
6. 强制 `stream=false` 后调用 `send_non_stream`。
7. 成功时写入 `request_logs`、`usage_ledger`、`debug_console_events`。
8. 失败时尽量写入 `request_logs` 和 `debug_console_events`，再返回统一错误。

成功响应示例：

```json
{
  "debug_request_id": "dbg_x",
  "request_id": "req_x",
  "trace_id": "trc_x",
  "status": "success",
  "latency_ms": 328,
  "virtual_key_id": "vk_x",
  "virtual_key_prefix": "mhv_dev_abcd",
  "model_alias": "company-default",
  "resolved_model": "deepseek-chat",
  "provider_code": "deepseek",
  "response_json": {
    "id": "chatcmpl_x",
    "choices": []
  },
  "response_preview": "{...}",
  "usage": {
    "input_tokens": 12,
    "output_tokens": 24,
    "total_tokens": 36,
    "cache_tokens": 0,
    "estimated": false,
    "usage_source": "upstream"
  },
  "cost": {
    "cost_cny": 0.0001,
    "cost_usd": 0,
    "currency": "CNY",
    "price_snapshot_id": "price_x"
  },
  "log_detail_url": "#request-logs?request_id=req_x"
}
```

失败响应：

- 使用统一错误结构。
- 至少包含 `code/message/request_id/trace_id/suggestion`。
- 常见错误复用 `gateway.*`、`debug.*`、`auth.permission_denied`。

## 前端页面/交互设计

### 页面结构

- 顶部工具条：
  - Virtual Key 下拉框：显示 `name + key_prefix + environment`，不显示明文。
  - 模型别名下拉框：显示 active alias 和环境。
  - 发送按钮。
- Messages 编辑区：
  - 默认一条 user message。
  - 支持新增 system/user/assistant message。
  - 支持删除 message。
  - M0 不做变量、集合、历史保存。
- 参数区：
  - `temperature` 数字输入。
  - `max_tokens` 数字输入。
  - `stream` 固定关闭或不可选。
- 响应区：
  - 成功展示 request_id、trace_id、latency、provider、resolved_model、usage、cost、response JSON。
  - 失败展示错误码、message、request_id、trace_id、suggestion。
  - 成功后提供“查看日志详情”按钮，跳转 `#request-logs` 并按 request_id 选中详情。

### 数据加载

- 复用已有 `/virtual-keys` 和 `/model-aliases` 页面状态作为下拉选项。
- 只展示 active Virtual Key 和 active ModelAlias。
- 如果没有 Key 或别名，页面显示 action_required 空状态，引导用户跳到对应页面。

### 与 PRD-009 旧调试台关系

- 新版调试台替代现有手输 Virtual Key 的最小调试台。
- 不再要求用户粘贴 Virtual Key 明文。
- 仍保留 OpenAI-compatible 语义，便于和快速开始页示例对齐。

## 错误码设计

新增：

- `debug.request_invalid`
  - HTTP 422。
  - 请求体非法、messages 为空、role 非法、参数越界。
- `debug.virtual_key_invalid`
  - HTTP 422 或 404。
  - Virtual Key 不存在、禁用或不属于当前租户。
- `debug.event_invalid`
  - HTTP 422。
  - debug event payload 超出白名单；客户端通常不会直接触发。
- `debug.upstream_failed`
  - HTTP 502。
  - 调试编排捕获到非标准上游失败且无法归一到既有 gateway 错误码时使用。

复用：

- `auth.token_missing`
- `auth.token_invalid`
- `auth.permission_denied`
- `gateway.model_missing`
- `gateway.messages_missing`
- `gateway.model_not_found`
- `gateway.model_alias_disabled`
- `gateway.model_disabled`
- `gateway.upstream_unavailable`
- `gateway.upstream_error`
- `infra.unhandled`

所有响应继续携带 `request_id` 和 `trace_id`。

## 安全与权限约束

- `/debug/chat-completions` 只允许 admin/developer 调用，viewer 后端拒绝。
- 请求不接受 `tenant_id`、`user_id`、`provider_key`、`authorization` 或 Virtual Key 明文。
- Virtual Key 选择只接受当前租户 active `virtual_key_id`；后端只读取 `environment/key_prefix/status` 等元数据，不读取或返回 `key_hash`。
- Provider Key 只由 KeyPool/Adapter 进入上游请求头，不写入 API 响应、日志、调试事件、request_logs 或 usage_ledger。
- Prompt/messages 和 response 只存在于本次请求/响应内，不写入 `debug_console_events`，request_logs 继续只保存安全元数据。
- 调试请求必须写 `request_logs`；成功时写 `usage_ledger`，失败时写失败状态和错误码。
- 前端隐藏按钮只是 UX；后端权限依赖必须拒绝 viewer 直接调用。
- 响应 JSON 在前端渲染时必须 HTML 转义，避免模型输出造成 XSS。

## 测试用例

### 主流程

- Admin/Developer 调用 `POST /debug/chat-completions`：
  - 选择当前租户 active Virtual Key 和 active 模型别名。
  - fake upstream 返回成功。
  - 响应包含 `debug_request_id/request_id/trace_id/latency_ms/usage/cost/response_json/log_detail_url`。
  - `request_logs` 有 success 记录。
  - `usage_ledger` 有 usage/cost 记录。
  - `debug_console_events` 有 `debug.request_sent` success 事件。
- 前端渲染：
  - 下拉框显示 Key 前缀和模型别名。
  - messages 默认可编辑。
  - 发送成功后展示响应、usage、成本和日志详情入口。

### 异常/权限/安全

- Viewer 调用返回 `auth.permission_denied`。
- `virtual_key_id` 不存在或禁用返回 `debug.virtual_key_invalid`。
- messages 为空、role 非法、content 过长返回 `debug.request_invalid`。
- model alias 不存在返回 `gateway.model_not_found` 并写失败 request_log。
- fake upstream 返回 500，响应归一为 `gateway.upstream_error` 并写失败 request_log/debug event。
- API 请求体包含 `tenant_id`、`provider_key`、`authorization` 等额外字段时拒绝。
- 调试事件表不包含 messages content、response content、Provider Key、Virtual Key 明文或 Authorization。
- 前端渲染模型输出和错误文本时做 HTML 转义。

## 代码修改计划

1. 新增 `0013_debug_console.sql/down.sql`。
2. 新增 `debug_console` 后端模块：
   - `models.py`
   - `service.py`
   - `routes.py`
   - `__init__.py`
3. 在 `app.py` 注册 debug router。
4. 在 `auth/dependencies.py` 新增 `require_debug_console_user`。
5. 在错误码表增加 `debug.request_invalid`、`debug.virtual_key_invalid`、`debug.event_invalid`、`debug.upstream_failed`。
6. 复用数据面 `resolve_model_route`、`adapter_for_route`、`RequestLogContext`、`safe_record_request_log`、`safe_record_usage_ledger`。
7. 新增前端 `debugConsole.js` 客户端。
8. 新增前端 `renderDebugConsolePanel.js`，替换旧手输 Key 调试台。
9. 调整 `main.js`：
   - 绑定 debug 表单。
   - 调用 `/debug/chat-completions`。
   - 成功后可跳转 request log 详情。
10. 更新 CSS。
11. 补测试：
   - 后端主流程、失败、权限、参数校验和敏感字段测试。
   - 前端客户端、渲染、表单状态和错误态测试。
12. 更新 README、目录边界和测试文档。

## 自审 1：需求完整性

- 已覆盖类似 Postman 的最小调试页：Key/模型选择、messages 编辑、发送请求、响应展示。
- 已覆盖 request_id、trace_id、延迟、usage、成本和错误建议。
- 已覆盖成功后跳到日志详情。
- 已覆盖 `POST /debug/chat-completions`、`DebugRequest`、`DebugResult` 和 `debug.request_sent`。
- 已明确 M0 不做集合管理和团队共享调试用例。

结论：需求范围完整，未越界到 Postman Collection 或团队协作能力。

## 自审 2：安全、可维护性与风险

- 最大技术风险是控制面绕过 Gateway 鉴权；设计通过登录态 + 当前租户 active Virtual Key 元数据约束，且仍把调用写入 request_logs/usage_ledger。
- 最大安全风险是保存 Prompt/Response；设计明确 DebugRequest/DebugResult 不落表，事件表只保存安全元数据。
- Provider Key 仍由 Adapter/KeyPool 注入上游请求头，不进入响应或日志。
- 复用数据面路由和 Adapter 能减少分叉，但必须保持调用入口为低频 debug，不替代 Gateway 热路径。
- 前端要替换旧“手输 Key 调试台”，避免两个调试入口行为不一致。

结论：可以进入编码，但必须把“调试事件不保存 Prompt/Response”和“失败请求写 request_logs”作为硬验收。

## 实现验收记录

- 后端实现文件：
  - `control-plane-api/src/metahub_control/debug_console/models.py`
  - `control-plane-api/src/metahub_control/debug_console/service.py`
  - `control-plane-api/src/metahub_control/debug_console/routes.py`
  - `infra/migrations/0013_debug_console_events.sql`
  - `infra/migrations/0013_debug_console_events.down.sql`
- 前端实现文件：
  - `console-web/src/lib/debugConsole.js`
  - `console-web/src/features/debug-console/renderDebugConsolePanel.js`
  - `console-web/src/app/main.js`
  - `console-web/src/features/developer-start/renderDeveloperStart.js`
  - `console-web/src/styles/main.css`
- 自动化测试：
  - `control-plane-api/tests/integration/test_debug_console.py`
  - `console-web/tests/unit/debugConsole.test.mjs`
  - `control-plane-api/tests/integration/test_db_migrations.py`
- 已验证的核心场景：
  - admin/developer 成功发起调试请求，写入 `request_logs`、`usage_ledger` 与 `debug_console_events`。
  - viewer 被后端权限依赖拒绝。
  - 非法 payload、敏感字段、`stream=true` 和上游失败返回统一错误码与 request_id/trace_id。
  - 调试事件和调用日志不保存 Prompt/Response 明文、Virtual Key 明文、Provider Key 或 Authorization。
  - 前端不再要求用户粘贴 Virtual Key 明文，成功后可跳转调用日志详情。
