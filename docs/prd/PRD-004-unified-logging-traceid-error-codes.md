# PRD-004 - 统一日志、TraceID 与错误码设计

状态：已实现。

实现说明：本文档先完成设计，随后按 PRD-004 范围实现 trace_id 透传、错误响应扩展、错误码元数据、结构化日志字段、`/internal/health`、控制台错误页预览、调试台预留、测试和文档。未接入 OpenTelemetry，也未实现完整 Trace 可视化。

## 需求理解

本 PRD 的目标是让任何 API 请求都具备可追踪能力：调用方能从响应头和响应体拿到 `request_id` / `trace_id`，研发和运维能用这些 ID 在结构化 JSON 日志中快速定位失败原因。

来自 Excel 的核心要求：

- 实现 `request_id` / `trace_id` 生成和透传。
- 所有 API 返回 `X-Request-ID`。
- 统一错误码结构。
- 结构化日志输出 JSON。
- 日志中禁止输出 Provider Key 明文。
- 提供 `GET /internal/health`。
- 页面入口为错误页、调试台预留。
- 不做完整 Trace 可视化，不接入 OpenTelemetry 后端。

当前 PRD-001/002 已经提供了 `request_id`、基础错误响应和 JSON 日志；PRD-004 要把这些能力收敛成更正式的可观测契约，增加 `trace_id`、错误码注册表、一致性测试和并发隔离验证。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 共享运行时 | `shared-sdk/python/metahub_shared/runtime/` | 增加 trace_id 解析/生成、错误码注册表、错误响应扩展、日志字段统一 |
| 数据面 | `gateway-data-plane/` | 所有响应透出 `X-Request-ID`、`X-Trace-ID`，新增 `/internal/health` |
| 控制面 API | `control-plane-api/` | 同步数据面的可观测契约，新增 `/internal/health` |
| 控制台 | `console-web/` | 新增错误页组件和调试台预留入口，不接业务调试功能 |
| 数据库 | `request_logs`、后续 `request_traces` | PRD-003 已有 `request_logs` 表；本 PRD 不新增迁移，先定义写入契约 |
| 文档与测试 | `docs/testing/`、pytest、Node test | 覆盖错误码一致性、敏感日志脱敏、并发 trace 不串 |

编码时要保留详细中文注释，重点解释：为什么 trace_id 与 request_id 分离、为什么错误响应不能泄露内部堆栈、为什么日志脱敏要覆盖结构化字段和 message 文本。

## 数据表/字段设计

本 PRD 不新增迁移。PRD-003 已经创建 `request_logs`，可承接失败请求的最小记录。后续若需要更完整 Trace，可新增 `request_traces` 或 `trace_events`。

### RequestTrace

运行时 DTO，后续可落库。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `request_id` | string | 单次 HTTP 请求 ID，响应头 `X-Request-ID` |
| `trace_id` | string | 业务链路追踪 ID，响应头 `X-Trace-ID` |
| `tenant_id` | uuid nullable | 未认证前可为空，认证后必须填 |
| `path` | string | 请求路径 |
| `method` | string | HTTP 方法 |
| `status_code` | integer | 响应状态码 |
| `error_code` | string nullable | 归一错误码 |
| `latency_ms` | integer | 请求耗时 |
| `started_at` | datetime | 开始时间 |
| `finished_at` | datetime | 结束时间 |

### ErrorCode

错误码注册表 DTO，不一定落库。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `code` | string | 稳定错误码，如 `infra.not_found` |
| `http_status` | integer | HTTP 状态 |
| `retryable` | boolean | 是否建议重试 |
| `message` | string | 用户可见错误消息 |
| `suggestion` | string | 修复建议 |
| `owner` | string | 模块归属，如 `infra`、`db`、`auth` |

后续若落库，业务相关错误码表必须带 `tenant_id`；全局错误码注册表属于基础设施元数据，可不带 `tenant_id`。

## API 设计

### 全局响应头

所有 API 响应都必须包含：

| Header | 说明 |
| --- | --- |
| `X-Request-ID` | 当前 HTTP 请求 ID。客户端传入时复用，否则服务端生成 |
| `X-Trace-ID` | 当前链路 ID。客户端传入 `X-Trace-ID` 时复用，否则与 request_id 同源生成 |

### 错误响应

在 PRD-001 基础上增加 `trace_id`：

```json
{
  "code": "infra.not_found",
  "message": "Requested resource was not found.",
  "request_id": "req_...",
  "trace_id": "trc_...",
  "suggestion": "Check the API path and method."
}
```

### GET /internal/health

用途：内部统一健康检查入口，便于后续探针和运维脚本使用。

响应：

```json
{
  "status": "ok",
  "service_name": "control-plane-api",
  "environment": "dev",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

约束：

- M0 内部 health 不依赖数据库、Redis、Provider。
- 依赖型 readiness 仍归 PRD-005。

## 前端页面/交互设计

### 错误页

控制台增加统一错误页组件，用于显示：

- 错误码 `code`
- 用户可读消息 `message`
- 修复建议 `suggestion`
- `request_id`
- `trace_id`
- 返回首页/刷新按钮

页面不能显示服务端堆栈、Provider Key、Virtual Key、Authorization。

### 调试台预留

只新增入口和空状态，不实现完整 Debug Center：

- 显示“调试台将在后续 PRD 接入请求日志、路由解释和成本归因”。
- 提供粘贴 `request_id` / `trace_id` 的输入框 UI 预留，但后端不查询。
- 不展示未授权业务数据。

## 错误码设计

继续使用 PRD-001/002/003 已有错误码，并补充错误码注册元数据。

| 错误码 | HTTP | retryable | owner | 场景 |
| --- | --- | --- | --- | --- |
| `infra.not_found` | 404 | false | infra | 未知路径 |
| `infra.unhandled` | 500 | true | infra | 未捕获异常 |
| `infra.config_invalid` | 500 | false | config | 配置非法 |
| `auth.internal_token_required` | 401 | false | auth | 内部 token 缺失 |
| `auth.internal_token_invalid` | 403 | false | auth | 内部 token 错误 |
| `db.migration_failed` | 500 | false | db | 迁移失败 |

设计要求：

- 错误码必须稳定，不把异常消息拼进 `code`。
- `message` 面向调用方，不能包含堆栈和密钥。
- `suggestion` 必须可执行。
- 新错误码必须补测试，防止响应结构漂移。

## 安全与权限约束

- 日志脱敏覆盖结构化字段、嵌套对象、list/tuple 和自由文本 message。
- 敏感关键词包括 `authorization`、`api_key`、`provider_key`、`secret`、`token`、`credential`、`password`。
- 错误响应不返回堆栈、文件路径、数据库连接串、Provider Key。
- 控制台错误页只展示 request_id/trace_id，不展示内部日志。
- `trace_id` 可由客户端传入，但要限制长度和字符集，避免日志污染。
- 并发请求的 request_id/trace_id 必须通过 request state/context 传递，不能使用全局变量。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0007-1 | 正常请求未传 ID | 自动生成 `request_id`、`trace_id`，响应头和响应体一致 |
| TC-0007-2 | 正常请求传入 ID | 复用 `X-Request-ID`、`X-Trace-ID` |
| TC-0007-3 | `GET /internal/health` | 返回 200、`status=ok`、`request_id`、`trace_id` |
| TC-0007-4 | 结构化日志 | JSON 日志包含 request_id、trace_id、path、method、status_code |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0008-1 | 未知路径 | 返回 `infra.not_found`，包含 request_id、trace_id、suggestion |
| TC-0008-2 | 未捕获异常 | 返回 `infra.unhandled`，日志记录异常，响应不泄露堆栈 |
| TC-0008-3 | 敏感日志脱敏 | Provider Key、Authorization、Token 不出现在日志 |
| TC-0008-4 | 并发请求 | 多个并发请求 request_id/trace_id 不串 |
| TC-0008-5 | 错误页渲染 | 页面展示 code/message/request_id/trace_id，不展示敏感信息 |

## 代码修改计划

已按以下顺序编码：

1. 增加 `trace_id` 工具：解析 `X-Trace-ID`，缺失时生成 `trc_` 前缀 ID，限制长度和字符。
2. 扩展错误响应模型：增加 `trace_id` 字段。
3. 扩展 FastAPI 中间件：request state 同时保存 request_id/trace_id，所有响应头返回两者。
4. 扩展 JSON 日志字段：记录 trace_id、latency_ms、error_code。
5. 新增 `/internal/health`，数据面和控制面保持同一契约。
6. 扩展错误码注册表：增加 retryable/owner 元数据和一致性测试。
7. 控制台新增错误页组件和调试台预留空状态。
8. 增加测试：正常透传、未知路径、未捕获异常、敏感日志、并发隔离、前端错误页。
9. 更新 README、`docs/development/directory-map.md`、`docs/testing/PRD-004-tests.md`。

不进入本 PRD 的内容：

- 不做完整 Trace 可视化。
- 不接 OpenTelemetry 后端。
- 不做 Debug Center 查询后端。
- 不做请求日志搜索页面。

## 验证结果

已运行：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
powershell -ExecutionPolicy Bypass -File .\scripts\ci\check.ps1
```

结果：

- 后端 pytest：`22 passed`
- 控制台 Node test：`6 passed`
- 控制台 lint：通过

已覆盖：

- `X-Request-ID` / `X-Trace-ID` 透传和自动生成。
- `/internal/health`。
- 错误响应包含 `trace_id`。
- 未捕获异常归一为 `infra.unhandled` 且不泄露堆栈/密钥。
- 并发请求 trace 不串。
- 控制台错误页预览和调试台预留。

真实 HTTP 验证：

- `GET http://localhost:8001/internal/health` 携带 `X-Request-ID=req_real_health`、`X-Trace-ID=trc_real_health`，响应头和响应体均透传两者。
- `GET http://localhost:8000/missing` 携带 `X-Request-ID=req_real_missing_py`、`X-Trace-ID=trc_real_missing_py`，返回 404，响应体包含 `infra.not_found`、`request_id`、`trace_id`、`suggestion`。
- `GET http://localhost:5173` 返回 HTTP 200。
