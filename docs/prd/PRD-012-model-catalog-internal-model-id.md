# PRD-012 - 模型目录与内部标准模型ID设计

状态：已完成编码与验证。

完成记录（2026-07-08）：已实现 `0006_model_catalog_metadata` 迁移、控制面 `/models` API、admin/developer 后端读权限、admin 写权限、Gateway `internal_model_id -> provider_model_id` 路由、控制台模型目录面板、后端/前端测试和文档更新。验证结果：`scripts/ci/check.ps1` 通过，后端 52 passed；控制台 41 passed；浏览器验收完成模型创建和禁用，控制台无 warn/error 日志。

实施前复核（2026-07-08）：当前 `control-plane-api/src/metahub_control/models/` 仅有占位 `__init__.py`，可按本 PRD 新增模型目录 API；Gateway 仍按 `models.model_alias` 解析请求体 `model`，并使用 `upstream_model_name` 转发上游，迁移时需要保持 `provider_model_id` 与 `upstream_model_name` 同步；当前 `.git` 目录为空壳，不能依赖 git 状态作为验收证据。

实现注意事项（2026-07-08 复核）：`GET /models` 需要新增或复用 admin/developer 后端依赖，不能沿用 Provider 的 admin-only 路由；数据面查询应使用 `COALESCE(m.provider_model_id, m.upstream_model_name)` 作为上游模型名，保证旧测试和旧数据在 0006 迁移后仍可路由；迁移集成测试需要把已应用版本从 `0001`-`0005` 扩展到 `0006`，回滚测试第一步应验证模型目录字段被移除。

设计自审 1（需求完整性）：已重新读取 Excel `PRD-012` 行，确认本 PRD 必须覆盖 ModelCatalog 的内部标准模型 ID、Provider 上游模型 ID、能力标签、上下文长度、输入/输出价格、币种和状态；接口范围为 `GET /models`、`POST /models`、`PATCH /models/{id}`；验收必须证明可新增/编辑模型，且数据面调用可通过内部模型 ID 解析到上游模型 ID。

设计自审 2（实现风险）：现有 `models.model_id` 已被日志外键引用，不能改作调用方可见 ID；将 `model_alias` 定义为 M0 内部标准模型 ID 是兼容性最好的选择。`provider_model_id` 必须与旧字段 `upstream_model_name` 同步，避免 PRD-009 的 OpenAI-compatible 调用链断裂。读权限需覆盖 admin/developer，写权限仅 admin，viewer 后端拒绝。
设计来源：Excel `PRD-012 - 模型目录与内部标准模型ID`、页面清单 `PAGE-019/PAGE-020`、API 清单 `GET /models`、`POST /models`、`PATCH /models/{id}`、数据对象 `ModelCatalog`、事件 `model.updated`、验收用例 `TC-0023/TC-0024`。架构设计要求控制面提供 Model Catalog Service，数据面路由依赖模型别名/端点，计费字段需要支持价格 schema、原始币种和成本追踪。

## 需求理解

本 PRD 的目标是把当前 seed 和数据面已使用的 `models` 表升级为可管理的模型目录。管理员可以新增和编辑模型；开发者可以查看统一模型目录，知道内部标准模型 ID、对应 Provider 上游模型 ID、能力标签、上下文长度、输入/输出价格、币种和状态。

当前实现已有：

- `models.model_id`：数据库主键，已被 `request_logs.model_id` 外键引用。
- `models.model_alias`：PRD-009 数据面当前用请求体 `model` 字段匹配它，并路由到上游。
- `models.upstream_model_name`：当前上游真实模型名。
- `models.capabilities`：JSON 字符串，当前已用于 chat/embeddings/stream 能力校验。

为避免破坏已验证调用链，本 PRD 设计将 `model_alias` 在 M0 中定义为“内部标准模型 ID / 调用方可见 model_id”。数据库主键 `models.model_id` 继续作为不可变技术主键。新增字段中使用 `provider_model_id` 明确上游模型 ID，并在数据面路由中优先使用该字段；迁移时以现有 `upstream_model_name` 回填。

明确不做：

- M0 不做模型质量评分。
- M0 不做模型备案标签。
- M0 不做复杂价格历史、生效区间和多计费维度；只保存当前输入/输出 token 价格和 price_schema_version 占位。
- M0 不做模型别名多路由、灰度、A/B、质量成本路由；这些归后续基础路由 PRD。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 数据库迁移 | `infra/migrations/` | 新增 `0006` 扩展 `models` 为 ModelCatalog |
| seed 数据 | `shared-sdk/python/metahub_shared/db/seed.py` | 默认模型写入 `provider_model_id`、上下文和价格默认值 |
| 控制面 API | `control-plane-api/src/metahub_control/models/` | 新增 models、service、routes |
| 控制面权限 | `control-plane-api/src/metahub_control/auth/` | 复用后端角色依赖：读接口 admin/developer，写接口 admin |
| 数据面路由 | `gateway-data-plane/src/metahub_gateway/routing/models.py` | 兼容从内部标准模型 ID 解析到 `provider_model_id` |
| 错误码 | `shared-sdk/python/metahub_shared/runtime/errors.py` | 新增 `model.*` 管理错误码 |
| 控制台 | `console-web/src/features/models/`、`console-web/src/lib/models.js` | 新增模型目录面板、创建表单、启停编辑入口 |
| 测试文档 | `control-plane-api/tests/`、`gateway-data-plane/tests/`、`console-web/tests/`、`docs/testing/` | 覆盖主流程、字段校验、权限和数据面解析 |

## 数据表/字段设计

现有 `models` 表：

| 字段 | 现状 | PRD-012 语义 |
| --- | --- | --- |
| `model_id` | 主键 UUID | 技术主键，内部引用和日志外键使用 |
| `tenant_id` | 已有 | 租户隔离字段 |
| `provider_id` | 已有 | 关联 Provider |
| `model_alias` | 已有 | M0 的内部标准模型 ID，调用方在 OpenAI-compatible `model` 字段传入 |
| `upstream_model_name` | 已有 | 兼容旧字段，后续与 `provider_model_id` 同步 |
| `capabilities` | 已有 JSON | 能力标签，如 chat/embeddings/stream/tools/json_mode |
| `status` | active/disabled | 禁用后数据面不可路由 |

新增迁移 `0006_model_catalog_metadata.sql`：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `provider_model_id` | TEXT | NULL，迁移后回填 | 上游模型 ID；新建时必填 |
| `display_name` | TEXT | NULL | 控制台显示名 |
| `context_window` | INTEGER | NOT NULL DEFAULT 0 | 最大上下文长度，0 表示未知 |
| `input_price_per_1k` | NUMERIC | NOT NULL DEFAULT 0 | 输入 Token 每百万价格（字段名为兼容历史客户端保留） |
| `output_price_per_1k` | NUMERIC | NOT NULL DEFAULT 0 | 输出 Token 每百万价格（字段名为兼容历史客户端保留） |
| `cache_price_per_1k` | NUMERIC | NOT NULL DEFAULT 0 | 缓存命中 Token 每百万价格（缺省时按输入价兼容回填） |
| `currency` | TEXT | NOT NULL DEFAULT `'CNY'` | 币种，M0 支持 `CNY`/`USD` |
| `price_schema_version` | TEXT | NULL | 价格版本占位，后续 FinOps 使用 |
| `description` | TEXT | NULL | 备注 |
| `created_by` | TEXT | NULL | 创建人 user_id |
| `disabled_at` | TEXT | NULL | 禁用时间 |
| `disabled_by` | TEXT | NULL | 禁用操作人 |

索引：

- `idx_models_model_alias`：`(tenant_id, model_alias)`，沿用唯一约束。
- `idx_models_provider_model_id`：`(tenant_id, provider_id, provider_model_id)`。
- `idx_models_status`：`(tenant_id, status)`。

兼容规则：

- 迁移执行后将 `provider_model_id = upstream_model_name`。
- 新建/更新时保持 `upstream_model_name` 与 `provider_model_id` 一致，避免 PRD-009 数据面代码和旧 seed 断裂。
- `capabilities` 继续以 JSON 存储，M0 不拆分子表；后续复杂能力检索可扩展 `model_capabilities`。

## API设计

所有接口位于 Control Plane API，响应带 `request_id` / `trace_id`，错误走统一错误结构。所有 SQL 都必须使用认证上下文中的 `tenant_id`。

### 权限

- `GET /models`：admin/developer 可访问。viewer M0 不开放，返回 `auth.permission_denied`。
- `POST /models`：仅 admin。
- `PATCH /models/{id}`：仅 admin。
- 控制台隐藏入口只作为体验优化，后端必须做角色校验。

### GET /models

查询参数：

- `status=active|disabled` 可选。
- `provider_id=<uuid>` 可选。
- `capability=chat|embeddings|stream|tools|json_mode` 可选。

响应：

```json
{
  "models": [
    {
      "model_id": "uuid-tech-pk",
      "tenant_id": "tenant_...",
      "internal_model_id": "default-chat",
      "provider_id": "provider_...",
      "provider_code": "local_openai",
      "provider_name": "Local OpenAI Compatible Endpoint",
      "provider_model_id": "qwen-plus",
      "display_name": "Default Chat",
      "capabilities": {
        "chat": true,
        "stream": true,
        "embeddings": false
      },
      "context_window": 32768,
      "input_price_per_1k": 0,
      "output_price_per_1k": 0,
      "currency": "CNY",
      "price_schema_version": "manual-v1",
      "status": "active",
      "description": "local dev model",
      "created_at": "2026-07-08T10:00:00Z",
      "updated_at": "2026-07-08T10:00:00Z",
      "disabled_at": null
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

说明：

- 对外响应使用 `internal_model_id` 表示调用方可用的标准模型 ID，对应数据库 `model_alias`。
- 不返回 Provider Key、KeyPool、上游认证头。

### POST /models

请求：

```json
{
  "internal_model_id": "deepseek-chat",
  "provider_id": "provider_...",
  "provider_model_id": "deepseek-chat",
  "display_name": "DeepSeek Chat",
  "capabilities": {
    "chat": true,
    "stream": true,
    "embeddings": false
  },
  "context_window": 64000,
  "input_price_per_1k": 0.001,
  "output_price_per_1k": 0.002,
  "currency": "CNY",
  "price_schema_version": "manual-202607",
  "description": "生产候选模型"
}
```

规则：

- `internal_model_id` 在当前租户内唯一，建议小写字母、数字、点、下划线、短横线。
- `provider_id` 必须属于当前租户且 Provider 状态不是物理删除对象；M0 允许绑定 disabled Provider，但数据面调用时会因 Provider disabled 返回 `gateway.provider_not_found`。
- `provider_model_id` 为上游模型 ID，必填。
- `capabilities` 必须是对象，只接受布尔值标签；M0 推荐标签：`chat`、`embeddings`、`stream`、`tools`、`json_mode`、`vision`。
- 价格字段必须为非负数；当前单位固定为每百万 Token。数据库/API 字段名仍保留 `_per_1k` 以兼容历史客户端。
- 创建后写 `model.updated` 结构化事件，payload 不含密钥。

### PATCH /models/{id}

可更新字段：

- `provider_id`
- `provider_model_id`
- `display_name`
- `capabilities`
- `context_window`
- `input_price_per_1k`
- `output_price_per_1k`
- `currency`
- `price_schema_version`
- `description`
- `status`

说明：

- 路径 `{id}` 使用技术主键 `models.model_id`。
- `internal_model_id` M0 不允许 PATCH 修改，避免破坏调用方配置；如需重命名，后续通过别名/路由 PRD 实现。
- `status=disabled` 后数据面不能再通过该内部模型 ID 路由，返回 `gateway.model_not_found`。
- PATCH 成功写 `model.updated` 事件。

## 前端页面/交互设计

新增控制台“模型目录”面板：

- admin/developer 可见模型目录列表。
- admin 可见创建模型表单和启用/禁用操作。
- developer 只读，能看到内部标准模型 ID、Provider、上游模型 ID、能力、上下文、价格、币种和状态。
- viewer 不显示入口。

列表字段：

- 内部模型 ID
- 显示名
- Provider
- 上游模型 ID
- 能力标签
- 上下文长度
- 输入/输出价格
- 币种
- 状态

创建表单：

- `internal_model_id`
- Provider 下拉，使用已加载 Provider 列表
- `provider_model_id`
- 显示名
- 能力 checkbox：chat、embeddings、stream、tools、json_mode、vision
- 上下文长度
- 输入/输出价格
- 币种
- 备注

M0 不做独立模型详情路由页面，先在面板中展开/列表展示详情；后续页面路由系统完善后再拆成 PAGE-020。

## 错误码设计

| 错误码 | HTTP | 场景 | 建议 |
| --- | --- | --- | --- |
| `model.not_found` | 404 | 当前租户找不到模型 | 检查 model_id 是否属于当前租户 |
| `model.create_invalid` | 422 | 创建 payload 非法 | 检查 Provider、内部 ID、价格和能力字段 |
| `model.internal_id_invalid` | 422 | 内部标准模型 ID 非法 | 使用小写字母、数字、点、下划线、短横线 |
| `model.internal_id_exists` | 409 | 当前租户内部模型 ID 已存在 | 更换 internal_model_id |
| `model.provider_invalid` | 422 | provider_id 不存在或不属于当前租户 | 先创建或选择当前租户 Provider |
| `model.provider_model_id_invalid` | 422 | 上游模型 ID 为空或过长 | 填写厂商真实模型 ID |
| `model.capabilities_invalid` | 422 | capabilities 不是对象或值非法 | 使用布尔能力标签 |
| `model.context_window_invalid` | 422 | 上下文长度为负数或过大 | 填写非负整数 |
| `model.price_invalid` | 422 | 价格为负数或非数字 | 填写非负数字 |
| `model.currency_invalid` | 422 | 币种不支持 | M0 选择 CNY 或 USD |
| `model.status_invalid` | 422 | status 非 active/disabled | 选择 active 或 disabled |
| `model.update_empty` | 422 | PATCH 无可更新字段 | 至少传入一个可更新字段 |
| `auth.permission_denied` | 403 | 非授权角色访问 | 联系管理员授权 |

数据面沿用：

- 禁用模型：`gateway.model_not_found`
- Provider 禁用：`gateway.provider_not_found`
- 能力不支持：`gateway.model_not_found` 或 `gateway.stream_not_supported`

## 安全与权限约束

- 所有模型目录数据默认带 `tenant_id`。
- 管理 API 只使用认证上下文中的 `tenant_id`，不接受请求体指定租户。
- `GET /models` 允许 admin/developer，写接口仅 admin。
- viewer 不允许查看 M0 模型目录，避免扩大企业内部模型可用范围信息暴露。
- 响应不返回 Provider Key、Virtual Key、Authorization 或任何上游认证材料。
- 日志事件 `model.updated` 只记录模型元数据变更摘要，不记录 prompt、input 或密钥。
- 价格字段用于成本可追踪，但 M0 不直接据此修改已完成历史账单。
- 禁用模型必须在数据面后端生效，不能只在控制台隐藏。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0023-1 | admin 创建模型 | 返回完整 ModelCatalog，`internal_model_id` 当前租户唯一 |
| TC-0023-2 | developer 列表读取 | 返回当前租户模型目录，不能看到密钥 |
| TC-0023-3 | admin 更新价格/上下文/能力 | 字段更新并返回 `model.updated` 语义 |
| TC-0023-4 | 数据面按内部模型 ID 调用 | 请求 `model=internal_model_id` 时路由到 `provider_model_id` |
| TC-0023-5 | 禁用模型 | Gateway 调用返回 `gateway.model_not_found` |
| TC-0023-6 | 控制台模型目录 | admin 可创建/禁用，developer 只读可见 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0024-1 | developer POST/PATCH | 返回 `auth.permission_denied` |
| TC-0024-2 | viewer GET | 返回 `auth.permission_denied` |
| TC-0024-3 | 重复 internal_model_id | 返回 `model.internal_id_exists` |
| TC-0024-4 | 非法 provider_id | 返回 `model.provider_invalid` |
| TC-0024-5 | 非法 capabilities | 返回 `model.capabilities_invalid` |
| TC-0024-6 | 负价格/负上下文长度 | 返回 `model.price_invalid` 或 `model.context_window_invalid` |
| TC-0024-7 | PATCH 空对象 | 返回 `model.update_empty` |
| TC-0024-8 | 跨租户模型 ID | 返回 `model.not_found` |

## 代码修改计划

审核通过后按以下顺序编码：

1. 新增迁移 `0006_model_catalog_metadata.sql` 和 down 脚本，扩展 `models` 元数据字段并回填 `provider_model_id`。
2. 更新 seed，使默认模型写入 `provider_model_id`、`context_window`、价格和币种默认值。
3. 扩展 `shared-sdk/python/metahub_shared/runtime/errors.py`，新增 `model.*` 错误码。
4. 新增 `control-plane-api/src/metahub_control/models/` 包：Pydantic models、service、routes。
5. 在 `control-plane-api/src/metahub_control/app.py` 注册 `/models` router。
6. `GET /models` 使用 admin/developer 读权限；`POST/PATCH` 使用 `require_current_admin`。
7. 更新 `gateway-data-plane/src/metahub_gateway/routing/models.py`，按 `model_alias` 解析内部标准模型 ID，并优先使用 `provider_model_id` 作为上游模型名。
8. 新增后端集成测试：创建/列表/更新/禁用/权限/字段校验/数据面路由解析。
9. 新增前端 `console-web/src/lib/models.js`、`features/models/renderModelCatalogPanel.js`，并接入 `renderDeveloperStart` 和 `main.js`。
10. 新增前端 Node test：API client、admin 创建控件、developer 只读、viewer 隐藏、HTML 转义。
11. 更新 README、目录边界和 `docs/testing/PRD-012-tests.md`。
12. 运行 `scripts/ci/check.ps1`，并做真实 HTTP + 浏览器验收：创建模型、数据面通过内部模型 ID 调用、禁用后调用失败。
