# PRD-015 - 模型别名与最小路由设计

状态：已完成设计、自审、编码、测试与文档更新。
设计来源：Excel `PRD-015 - 模型别名与最小路由`，阶段 `M0-W1`，优先级 `P0`，模块 `基础路由`。架构约束来自 `企业级AI_Gateway系统架构设计.docx`：MVP 支持模型别名、固定路由、按健康状态过滤与可解释路由日志，不引入复杂策略 DSL、质量路由、灰度策略或跨境复杂链路。

设计自审 1（需求完整性）：本 PRD 不能只沿用 `models.model_alias` 字段，因为 PRD-012 中该字段已经承担“内部标准模型 ID”职责。PRD-015 需要新增独立 `ModelAlias` 对象，支持调用方使用稳定别名（例如 `company-default`），再映射到默认模型和备用模型。数据面必须优先解析 `ModelAlias`，保留直接调用内部模型 ID 的兼容路径，避免破坏已有测试和本地开发。

设计自审 2（验收与安全）：路由决策必须可追踪，不能只在应用日志中打印。新增 `routing_decisions` 表记录 `request_id`、`trace_id`、请求别名、真实模型、Provider、选择原因和失败错误码。所有管理 API 带后端角色校验与租户过滤；所有业务表带 `tenant_id`；日志和响应不包含 Virtual Key、Provider Key、prompt/input 明文。

## 需求理解

开发者希望调用稳定别名，例如 `company-default`，而不关心当前实际使用 DeepSeek、通义、文心或客户本地 OpenAI-compatible Endpoint 的哪个具体模型。管理员需要在控制台配置别名的默认模型、备用模型、环境范围和状态。数据面收到 OpenAI-compatible 请求后，根据当前 Virtual Key 所属环境解析别名，选择真实模型并调用对应 Provider。

M0 的最小路由只做三件事：

- 精确别名解析：`tenant_id + alias + environment`。
- 固定优先级选择：默认模型可用则选默认模型，否则尝试备用模型。
- 可解释记录：成功和失败都尽量写入 `routing_decisions`，主请求失败继续写入 `request_logs`。

明确不做：

- 不做复杂 DSL 路由策略。
- 不做质量/成本打分、A/B 测试、灰度路由。
- 不做流式响应中途切换模型。
- 不做跨 Provider 动态全局调度；备用模型只来自管理员显式配置。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 数据库迁移 | `infra/migrations/` | 新增 `model_aliases` 与 `routing_decisions` 表 |
| 错误码 | `shared-sdk/python/metahub_shared/runtime/errors.py` | 新增 `model_alias.*` 与路由相关 `gateway.*` 错误码 |
| 控制面 API | `control-plane-api/src/metahub_control/model_aliases/` | 新增 DTO、service、routes，注册到 app |
| 数据面鉴权 | `gateway-data-plane/src/metahub_gateway/auth/virtual_key.py` | 在鉴权上下文中带出 Virtual Key 的 `environment` |
| 数据面路由 | `gateway-data-plane/src/metahub_gateway/routing/` | 扩展 `resolve_model_route` 支持 ModelAlias、备用模型和决策落库 |
| OpenAI-compatible API | `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py` | 传入环境、request_id、trace_id、virtual_key_id；日志补充 alias 与真实模型 |
| 控制台 | `console-web/src/features/model-aliases/` | 新增模型别名页/面板、客户端 API、事件绑定 |
| 测试 | `control-plane-api/tests/`、`gateway-data-plane/tests/`、`console-web/tests/` | 覆盖主流程、异常、权限和渲染 |
| 文档 | `README.md`、`docs/development/directory-map.md`、`docs/testing/` | 记录接口、边界和验证结果 |

## 数据表/字段设计

新增 `model_aliases`：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `model_alias_id` | TEXT | PK | 别名配置主键 |
| `tenant_id` | TEXT | NOT NULL | 当前租户 |
| `alias` | TEXT | NOT NULL | 调用方可见稳定别名，例如 `company-default` |
| `environment` | TEXT | NOT NULL CHECK dev/test/prod | 环境范围，按 Virtual Key 环境匹配 |
| `default_model_id` | TEXT | NOT NULL | 默认真实模型，引用 `models.model_id` |
| `fallback_model_id` | TEXT | NULL | 备用真实模型，引用 `models.model_id` |
| `status` | TEXT | NOT NULL CHECK active/disabled | 别名启停状态 |
| `description` | TEXT | NULL | 管理说明 |
| `created_by` | TEXT | NULL | 创建用户 |
| `disabled_at` | TEXT | NULL | 禁用时间 |
| `disabled_by` | TEXT | NULL | 禁用用户 |
| `created_at` | TEXT | NOT NULL | 创建时间 |
| `updated_at` | TEXT | NOT NULL | 更新时间 |

索引与约束：

- `UNIQUE (tenant_id, alias, environment)`，同一租户同一环境不可重复。
- `idx_model_aliases_tenant_status`：`(tenant_id, status)`。
- `idx_model_aliases_default_model`：`(tenant_id, default_model_id)`。
- `idx_model_aliases_fallback_model`：`(tenant_id, fallback_model_id)`。

新增 `routing_decisions`：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `routing_decision_id` | TEXT | PK | 决策记录主键 |
| `tenant_id` | TEXT | NOT NULL | 当前租户 |
| `request_id` | TEXT | NOT NULL | 请求 ID |
| `trace_id` | TEXT | NOT NULL | Trace ID |
| `virtual_key_id` | TEXT | NULL | 调用方 Virtual Key |
| `model_alias_id` | TEXT | NULL | 命中的 ModelAlias；直接模型调用为空 |
| `requested_model` | TEXT | NOT NULL | OpenAI 请求中的 `model` 字段 |
| `environment` | TEXT | NOT NULL | Virtual Key 环境 |
| `selected_model_id` | TEXT | NULL | 最终真实模型 |
| `selected_internal_model_id` | TEXT | NULL | 模型目录中的内部标准模型 ID |
| `provider_id` | TEXT | NULL | 最终 Provider |
| `provider_code` | TEXT | NULL | Provider code |
| `decision_status` | TEXT | CHECK selected/failed | 成功或失败 |
| `decision_reason` | TEXT | NOT NULL | `alias.default`、`alias.fallback`、`direct_model` 等 |
| `candidate_summary` | TEXT | NOT NULL DEFAULT `{}` | 候选与剔除原因 JSON |
| `error_code` | TEXT | NULL | 失败时的统一错误码 |
| `created_at` | TEXT | NOT NULL | 记录时间 |

索引：

- `idx_routing_decisions_request_id`：`(tenant_id, request_id)`。
- `idx_routing_decisions_alias_time`：`(tenant_id, model_alias_id, created_at)`。
- `idx_routing_decisions_error_code`：`(tenant_id, error_code)`。

## API设计

所有接口位于 Control Plane API，统一返回 `request_id` 与 `trace_id`。GET 允许 `admin/developer` 查看；POST/PATCH 只允许 `admin` 修改。

### GET /model-aliases

查询参数：

- `environment`：可选，`dev/test/prod`。
- `status`：可选，`active/disabled`。

响应：

```json
{
  "model_aliases": [
    {
      "model_alias_id": "alias_...",
      "tenant_id": "tenant_...",
      "alias": "company-default",
      "environment": "dev",
      "default_model_id": "model_...",
      "default_internal_model_id": "deepseek-chat",
      "fallback_model_id": "model_...",
      "fallback_internal_model_id": "qwen-plus",
      "status": "active",
      "description": "默认生产聊天模型",
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /model-aliases

请求：

```json
{
  "alias": "company-default",
  "environment": "dev",
  "default_model_id": "model_default",
  "fallback_model_id": "model_backup",
  "description": "默认聊天别名",
  "status": "active"
}
```

规则：

- `alias` 使用小写字母、数字、点、下划线、短横线，最长 120。
- 默认模型必须属于当前租户。
- 备用模型可为空；若提供，必须属于当前租户且不能等于默认模型。
- disabled 创建时写入 `disabled_at/disabled_by`。

### PATCH /model-aliases/{id}

可更新字段：

- `alias`
- `environment`
- `default_model_id`
- `fallback_model_id`
- `description`
- `status`

空 PATCH 返回 `model_alias.update_empty`。`status=disabled` 写入禁用审计字段；重新启用清理禁用字段。

## 前端页面/交互设计

新增“模型别名”面板，放在模型目录之后，作为 M0 的模型路由配置入口。

- admin 可见列表、创建表单、启用/禁用操作。
- developer 可只读查看别名映射与环境范围。
- viewer 不展示入口；后端也会拒绝接口访问。
- 创建表单字段：别名、环境、默认模型、备用模型、备注。
- 列表展示：别名、环境、默认模型、备用模型、状态、更新时间、操作。
- 错误提示展示统一 `message + request_id`。
- 不展示 Provider Key、Virtual Key 明文、prompt/input。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `model_alias.not_found` | 404 | 当前租户找不到别名配置 |
| `model_alias.create_invalid` | 422 | 创建 payload 非法 |
| `model_alias.alias_invalid` | 422 | 别名为空、过长或格式非法 |
| `model_alias.environment_invalid` | 422 | 环境不是 dev/test/prod |
| `model_alias.model_invalid` | 422 | 默认/备用模型不存在、跨租户或重复 |
| `model_alias.status_invalid` | 422 | 状态不是 active/disabled |
| `model_alias.duplicate` | 409 | 同租户同环境别名重复 |
| `model_alias.update_empty` | 422 | PATCH 无有效字段 |
| `gateway.model_alias_disabled` | 409 | 命中的模型别名被禁用 |
| `gateway.model_disabled` | 409 | 别名指向的默认/备用模型均不可用 |

新增事件：

- `model_alias.created`
- `model_alias.updated`
- `model_alias.disabled`
- `route.selected`
- `route.failed`

事件 payload 只包含租户、对象 ID、别名、环境、真实模型 ID、Provider ID、错误码和选择原因，不包含密钥或请求内容。

## 安全与权限约束

- 所有 `model_aliases` 与 `routing_decisions` 数据默认带 `tenant_id`。
- 管理 API 不信任请求体中的租户字段，只使用认证上下文的 `tenant_id`。
- GET 使用 `require_model_catalog_reader`，POST/PATCH 使用 `require_current_admin`。
- 数据面使用 Virtual Key 恢复 `tenant_id` 与 `environment`，不接受客户端传入租户或环境覆盖。
- 路由日志只记录模型路由元数据，不记录 prompt、input、messages、Virtual Key、Provider Key 或 Authorization。
- Provider Key 仍由 PRD-014 KeyPool 注入上游请求头，PRD-015 不改变密钥存储与脱敏边界。
- 控制台隐藏按钮不是权限边界，后端必须按角色拒绝。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-PRD015-1 | admin 创建 `company-default` 别名 | 返回别名元数据，带 `tenant_id/request_id/trace_id` |
| TC-PRD015-2 | developer 列表读取 | 可查看别名、环境、默认/备用模型，不可修改 |
| TC-PRD015-3 | Gateway 调用别名 | 上游收到默认模型的 `provider_model_id` |
| TC-PRD015-4 | 默认模型禁用但备用模型可用 | Gateway 路由到备用模型，`routing_decisions.decision_reason=alias.fallback` |
| TC-PRD015-5 | 成功路由决策落库 | `routing_decisions` 记录请求别名、真实模型、Provider 和 `route.selected` 语义 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-PRD015-6 | viewer 调用列表、developer 调用创建 | 返回 `auth.permission_denied` |
| TC-PRD015-7 | 重复别名 | 返回 `model_alias.duplicate` |
| TC-PRD015-8 | 非法别名、环境、模型 ID | 返回对应 `model_alias.*` 错误码 |
| TC-PRD015-9 | 别名禁用 | Gateway 返回 `gateway.model_alias_disabled`，失败请求写入 `request_logs` |
| TC-PRD015-10 | 默认/备用模型均禁用 | Gateway 返回 `gateway.model_disabled`，`routing_decisions` 记录失败原因 |
| TC-PRD015-11 | 安全日志扫描 | 请求内容、Virtual Key、Provider Key 不进入 `routing_decisions/request_logs/usage_ledger` |

## 代码修改计划

1. 新增迁移 `0008_model_alias_minimal_routing.sql` 与 down 脚本。
2. 扩展错误码注册表，加入 `model_alias.*`、`gateway.model_alias_disabled`、`gateway.model_disabled`。
3. 新增控制面 `model_aliases` 模块：Pydantic DTO、service 校验、routes 与 app 注册。
4. 扩展 Gateway Virtual Key 鉴权上下文，返回 `environment`。
5. 扩展 `GatewayRoute` 与 `resolve_model_route`：优先解析 ModelAlias；默认模型不可用时尝试备用模型；保留直接模型 ID 兼容路径；记录 `routing_decisions`。
6. 调整 OpenAI-compatible API 调用路由解析时传入 `environment/request_id/trace_id/virtual_key_id`，并在安全日志中记录请求别名和真实模型。
7. 新增控制台 `modelAliases` API client、`renderModelAliasPanel`、状态加载与事件绑定。
8. 更新 CSS，保证模型别名表单和列表在移动/桌面视口不重叠。
9. 增加后端集成测试、Gateway 路由集成测试、前端 Node 单元测试。
10. 更新 README、目录边界和 `docs/testing/PRD-015-tests.md`，运行目标测试、全量 CI 和本地 HTTP/browser 验收。

## 完成记录

- 数据库：新增 `0008_model_alias_minimal_routing.sql` 与 down 脚本，创建 `model_aliases` 和 `routing_decisions`。
- 控制面：新增 `GET /model-aliases`、`POST /model-aliases`、`PATCH /model-aliases/{id}`，读取使用 `require_model_catalog_reader`，写入使用 `require_current_admin`。
- 数据面：Virtual Key 鉴权上下文新增 `environment`；`resolve_model_route` 优先解析 ModelAlias，默认模型不可用时尝试备用模型，未配置别名时保留内部模型 ID 兼容路径。
- 路由追踪：成功记录 `route.selected` 和 `routing_decisions`；禁用别名、无可用模型等失败场景记录 `route.failed`、`routing_decisions` 和 `request_logs`。
- 前端：新增模型别名 API client、模型别名面板、创建/启停交互和响应式样式；admin 可写，developer 只读，viewer 隐藏入口。
- 测试：新增控制面 `test_model_aliases.py`、数据面 `test_model_alias_routing.py`、前端 `modelAliases.test.mjs`，并更新迁移测试。
- 文档：更新 README、目录边界和 `docs/testing/PRD-015-tests.md`。
