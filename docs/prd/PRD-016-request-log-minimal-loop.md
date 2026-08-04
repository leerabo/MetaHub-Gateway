# PRD-016 - Request Log最小闭环设计

状态：已完成实现、测试与本地浏览器验收。
设计来源：Excel `PRD-016 - Request Log最小闭环`，阶段 `M0-W1`，优先级 `P0`，模块 `调用日志`。架构约束来自 `企业级AI_Gateway系统架构设计.docx`：调用日志要帮助开发者回答“为什么失败、为什么选中这个模型、成本多少”，但 M0 不做全文检索，不默认保存 Prompt/Response 明文。

设计自审 1（需求完整性）：当前 `request_logs` 只记录 `request_id/tenant_id/trace_id/virtual_key_id/provider_id/model_id/status/error_code/latency_ms`，不足以支撑控制台列表、模型过滤、resolved model、Provider 展示、token 估算和成本估算。本 PRD 必须扩展表结构，并补控制面查询 API 与控制台页面。

设计自审 2（安全与可运行性）：日志闭环不能以保存 prompt/input/messages 换可调试性。M0 只保存路由元数据、状态、错误码、token/cost 汇总、request_id/trace_id 和脱敏 key 元数据；不保存请求体、响应体、Authorization、Provider Key、Virtual Key 明文。失败请求仍要尽量写入日志；写库失败不能阻断 Gateway 主响应，但应产生日志事件，方便后续排障。

## 需求理解

开发者和运维需要在控制台按 `request_id`、状态、模型等条件查到最近调用，看到：

- 调用是否成功。
- 请求使用的稳定模型名/别名。
- 实际解析后的内部模型和 Provider。
- 延迟、token 估算和成本估算。
- 错误码与基础错误原因。

M0 的 Request Log 是“最小闭环”，目标是让每次已通过 Gateway 鉴权的调用都有可查记录，不做全文日志平台或 Prompt/Response 留存。

明确不做：

- 不默认保存完整 Prompt、messages、input、Response。
- 不做全文检索、高级日志查询语言或日志导出。
- 不做跨租户查询。
- 不做完整成本归因模型；成本先使用当前 usage ledger 的 0 CNY 占位和 token 汇总。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 数据库迁移 | `infra/migrations/` | 新增 `0009` 扩展 `request_logs` 的模型、Provider、token、成本、更新时间字段与索引 |
| 数据面日志写入 | `gateway-data-plane/src/metahub_gateway/usage/ledger.py` | 扩展 `RequestLogContext`，写入脱敏模型/Provider/usage 元数据，提供后台安全写入入口 |
| 数据面 OpenAI API | `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py` | 补全成功、参数异常、路由异常、上游异常的日志上下文 |
| 控制面 API | `control-plane-api/src/metahub_control/request_logs/` | 新增列表/详情 DTO、service、routes |
| 权限 | `control-plane-api/src/metahub_control/auth/` | 复用或新增 `require_request_log_reader`，admin/developer 可读，viewer 拒绝 |
| 控制台 | `console-web/src/features/request-logs/` | 新增调用日志列表、过滤和详情摘要 |
| 文档 | `README.md`、`docs/development/directory-map.md`、`docs/testing/` | 记录 API、边界、测试方式 |
| 测试 | `control-plane-api/tests/`、`gateway-data-plane/tests/`、`console-web/tests/` | 覆盖查询、过滤、分页、权限、脱敏、写入失败 |

## 数据表/字段设计

扩展现有 `request_logs`：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `endpoint` | TEXT | NULL | `/v1/chat/completions` 或 `/v1/embeddings` |
| `request_model` | TEXT | NULL | 请求体中的 `model` 字段，作为别名/内部模型 ID，不保存 prompt |
| `resolved_model` | TEXT | NULL | 实际内部模型 ID，例如 `deepseek-chat` |
| `provider_code` | TEXT | NULL | Provider code，便于列表展示和过滤 |
| `input_tokens` | INTEGER | NOT NULL DEFAULT 0 | 输入 token 汇总 |
| `output_tokens` | INTEGER | NOT NULL DEFAULT 0 | 输出 token 汇总 |
| `total_tokens` | INTEGER | NOT NULL DEFAULT 0 | token 合计 |
| `estimated_cost_amount` | NUMERIC | NOT NULL DEFAULT 0 | M0 成本估算，占位为 0 |
| `currency` | TEXT | NOT NULL DEFAULT `CNY` | 成本币种 |
| `price_schema_version` | TEXT | NULL | 成本模型版本，占位可为空 |
| `updated_at` | TEXT | NULL | 日志最后更新时间 |

继续保留已有字段：

- `request_id`
- `tenant_id`
- `trace_id`
- `virtual_key_id`
- `provider_id`
- `model_id`
- `status`
- `error_code`
- `latency_ms`
- `created_at`

新增索引：

- `idx_request_logs_created_at`：`(tenant_id, created_at DESC)`。
- `idx_request_logs_status_time`：`(tenant_id, status, created_at DESC)`。
- `idx_request_logs_request_model`：`(tenant_id, request_model)`。
- `idx_request_logs_resolved_model`：`(tenant_id, resolved_model)`。
- `idx_request_logs_provider_code`：`(tenant_id, provider_code)`。

字段命名说明：

- `request_model` 是调用方传入的稳定模型名/别名。
- `resolved_model` 是 Gateway 解析后的内部标准模型 ID。
- `models.model_id` 仍作为技术外键，用于跨表关联。

## API设计

所有接口位于 Control Plane API，统一返回 `request_id` 与 `trace_id`，只查询当前认证上下文 `tenant_id`。

### GET /request-logs

权限：admin/developer 可读，viewer 拒绝。

查询参数：

- `request_id`：可选，精确匹配。
- `status`：可选，`success/failed`。
- `model`：可选，同时匹配 `request_model` 或 `resolved_model`。
- `provider_code`：可选。
- `error_code`：可选。
- `limit`：默认 20，最大 100。
- `offset`：默认 0。

响应：

```json
{
  "request_logs": [
    {
      "request_id": "req_...",
      "trace_id": "trc_...",
      "tenant_id": "tenant_...",
      "virtual_key_id": "vk_...",
      "virtual_key_prefix": "mhv_dev_xxx",
      "endpoint": "/v1/chat/completions",
      "request_model": "company-default",
      "resolved_model": "deepseek-chat",
      "provider_id": "provider_...",
      "provider_code": "deepseek",
      "status": "success",
      "error_code": null,
      "latency_ms": 321,
      "input_tokens": 100,
      "output_tokens": 28,
      "total_tokens": 128,
      "estimated_cost_amount": 0,
      "currency": "CNY",
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z"
    }
  ],
  "pagination": {
    "limit": 20,
    "offset": 0,
    "total": 1,
    "has_more": false
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /request-logs/{request_id}

权限：admin/developer 可读，viewer 拒绝。

返回单条日志详情，附带同 `request_id` 的 usage 摘要和 routing decision 摘要：

```json
{
  "request_log": {
    "request_id": "req_...",
    "status": "failed",
    "error_code": "gateway.upstream_error",
    "request_model": "company-default",
    "resolved_model": "deepseek-chat",
    "provider_code": "deepseek",
    "routing_decision": {
      "decision_status": "selected",
      "decision_reason": "alias.default"
    },
    "usage": {
      "input_tokens": 0,
      "output_tokens": 0,
      "total_tokens": 0,
      "estimated_cost_amount": 0,
      "currency": "CNY"
    }
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

## 前端页面/交互设计

新增“调用日志”面板，位于 OpenAI 调试台附近，便于调试完立即看结果。

- admin/developer 可见；viewer 不展示，后端也拒绝。
- 顶部过滤：request_id、状态、模型、Provider code。
- 列表列：时间、状态、request_id、请求模型、解析模型、Provider、token、成本、延迟、错误码。
- 详情区域：点击或通过 request_id 查询后展示同一条日志的 trace_id、routing decision 摘要、usage 摘要和错误码。
- 不展示 Prompt、Response、Virtual Key 明文、Provider Key 或 Authorization。
- M0 分页使用 limit/offset，列表默认最近 20 条。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `request_log.not_found` | 404 | 当前租户找不到 request_id |
| `request_log.query_invalid` | 422 | 过滤参数、limit 或 offset 非法 |
| `request_log.status_invalid` | 422 | status 不是 success/failed |

事件：

- `request.logged`：Gateway 写入或更新 request log 后产生结构化日志事件。
- `request_log.write_failed`：日志写库失败但主请求不中断时产生 warning。

## 安全与权限约束

- 所有查询都使用认证上下文中的 `tenant_id`，不接受请求参数传入租户。
- admin/developer 可读；viewer 直接调用后端返回 `auth.permission_denied`。
- `request_logs`、`usage_ledger`、`routing_decisions` 都不保存 Prompt、Response、messages、input、Authorization、Virtual Key 明文或 Provider Key 明文。
- API 响应不返回 `key_hash`、`encrypted_key`、Provider Key 或 Session Token。
- `virtual_key_prefix` 仅用于排障展示；Virtual Key 明文不进入日志。
- 日志写库失败不能让已经完成的模型调用变成 500；必须产生日志事件供排查。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-PRD016-1 | 成功 chat 调用后查询列表 | `/request-logs` 可看到 request_id、模型、Provider、latency、token、成本 |
| TC-PRD016-2 | 成功 embeddings 调用后查询详情 | `/request-logs/{request_id}` 返回 usage 摘要 |
| TC-PRD016-3 | 过滤 request_id/status/model/provider | 返回当前租户匹配日志，分页字段正确 |
| TC-PRD016-4 | 别名路由调用 | 日志记录 `request_model` 和 `resolved_model` |
| TC-PRD016-5 | 控制台渲染列表和详情 | admin/developer 可见，字段脱敏 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-PRD016-6 | 上游错误请求 | 失败日志可查，`error_code=gateway.upstream_error` |
| TC-PRD016-7 | 参数错误请求 | 已通过鉴权的失败请求尽量写入日志 |
| TC-PRD016-8 | viewer 调用日志 API | 返回 `auth.permission_denied` |
| TC-PRD016-9 | 非法 status/limit/offset | 返回 `request_log.*` 错误码 |
| TC-PRD016-10 | 敏感内容扫描 | Prompt、Response、Virtual Key 明文、Provider Key 不进入日志表和 API 响应 |
| TC-PRD016-11 | 写库失败 | Gateway 主响应不因日志写入失败而中断，并记录 `request_log.write_failed` |

## 代码修改计划

1. 新增迁移 `0009_request_log_minimal_loop.sql` 与 down 脚本，扩展 `request_logs`。
2. 扩展 shared error registry，新增 `request_log.*` 错误码。
3. 扩展 `RequestLogContext` 和 request log/usage ledger 写入逻辑，补齐模型、Provider、endpoint、token、成本字段，并产生日志事件。
4. 调整 OpenAI-compatible API，在认证成功后尽早构建可用的失败日志上下文；成功、上游失败、参数失败、路由失败都尽量落库。
5. 新增控制面 `request_logs` 模块：models、service、routes、app 注册。
6. 新增控制台 request log API client 和调用日志面板，接入主页面状态加载与过滤表单。
7. 增加控制面集成测试、Gateway 集成测试、前端 Node 测试。
8. 更新 README、目录边界和 `docs/testing/PRD-016-tests.md`。
9. 运行目标测试、全量 `scripts/ci/check.ps1`、本地 HTTP/browser 验收。


## 完成记录

- 数据库：新增 `0009_request_log_minimal_loop.sql` 与 down 脚本，扩展 `request_logs` 的 endpoint、模型、Provider、token、成本、币种、价格版本和更新时间字段。
- 数据面：扩展 `RequestLogContext`、`record_request_log`、`record_usage_ledger` 和 OpenAI-compatible 入口，成功/失败请求均尽量写入调用日志，usage 写入后回写 token/成本摘要。
- 控制面：新增 `request_logs` models/service/routes，并注册 `/request-logs` 与 `/request-logs/{request_id}`；权限依赖为 `require_request_log_reader`。
- 控制台：新增 `requestLogs` client、调用日志面板、过滤、分页、详情、路由决策和用量展示。
- 中文注释：PRD-016 新增/修改的核心类、函数、路由、服务函数和关键安全分支已补中文说明，覆盖租户隔离、脱敏、写库容错和分页过滤。
- 测试：补充控制面集成测试、数据面集成测试、迁移回滚测试和前端 Node 单测，并完成本地控制台浏览器验收。
