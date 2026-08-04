# PRD-007 - 单租户与基础多租户模型设计

状态：已实现。

实现说明：本 PRD 已按设计完成当前租户读取/更新 API、后端租户上下文约束、租户信息控制台面板、跨租户请求体拒绝、MVP 表 `tenant_id` 审计测试和文档更新。M0 仍不做租户列表、租户创建/删除、自助注册、多租户计费隔离或数据库 RLS。

设计来源：Excel `PRD-007 - 单租户与基础多租户模型`、页面清单 `PAGE-009`、API 清单 `GET /tenant/current`、`PATCH /tenant/current`、数据对象 `Tenant`、事件 `tenant.updated`、验收用例 `TC-0013/TC-0014`。架构约束要求所有业务表必须包含 `tenant_id` 或可通过强外键追溯到 `tenant_id`，所有管理 API 必须做后端租户鉴权，不能只依赖前端隐藏按钮。

## 需求理解

本 PRD 的目标是在 M0 保持单默认租户可用的同时，把后续多租户能力的边界打牢。M0 不做租户自助注册，也不做多租户计费隔离；但所有控制面业务 API 必须从认证上下文拿当前租户，并按 `tenant_id` 查询和更新。

核心范围：

- 复用 PRD-003 已创建的 `tenants` 表和默认 seed 租户 `default`。
- 提供当前租户读取接口：`GET /tenant/current`。
- 提供当前租户基础信息更新接口：`PATCH /tenant/current`。
- 更新行为只能作用于当前登录用户所属租户，不能由前端传 `tenant_id` 指定目标。
- 所有 MVP 业务表必须保留 `tenant_id`；后端服务层需要形成“当前租户上下文”约定。
- 更新租户信息时产生 `tenant.updated` 结构化事件。M0 先写结构化日志，后续 Webhook PRD 再投递。

明确不做：

- 不做租户自助注册。
- 不做平台级租户列表、创建租户、删除租户。
- 不做跨租户管理后台。
- 不做多租户计费隔离、逻辑分库、数据库行级安全策略。
- 不允许普通当前租户接口更新 `tenant_id`、`slug` 或自助禁用租户。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 租户管理 API | `control-plane-api/src/metahub_control/tenants/` | 新增当前租户 models/service/routes |
| 控制面认证 | `control-plane-api/src/metahub_control/auth/` | 复用 `require_current_user` / `require_current_admin` 提供租户上下文 |
| 数据库迁移 | `infra/migrations/` | 复查 `tenants` 与所有 MVP 业务表的 `tenant_id`；预计不新增表 |
| 控制台 | `console-web/` | 新增租户信息面板，登录后读取和编辑当前租户名称/服务模式 |
| 文档与测试 | `docs/testing/`、pytest、Node test | 覆盖当前租户查询、更新、越权隔离、tenant_id 字段审计 |

编码时保留详细中文注释，重点解释为什么服务端只信任认证上下文里的 `tenant_id`，不能信任请求体或查询参数中的租户标识。

## 数据表/字段设计

### tenants

PRD-003 已创建 `tenants` 表，M0 字段足够支撑 PRD-007：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `tenant_id` | text | primary key | 租户 ID |
| `slug` | text | unique/not null | 租户唯一标识，M0 seed 为 `default` |
| `name` | text | not null | 租户展示名称，可由当前租户管理员更新 |
| `service_mode` | text | not null | `internal_only`、`public_service` |
| `status` | text | not null | `active`、`disabled` |
| `created_at` | text | not null | 创建时间 |
| `updated_at` | text | not null | 更新时间 |

本 PRD 不新增表；若实现中发现必须新增字段，应通过 `0003_*` 迁移追加，不能修改已应用的 `0001`/`0002`。

### MVP 业务表 tenant_id 审计

当前必须包含 `tenant_id` 的业务表：

- `tenants`
- `roles`
- `users`
- `sessions`
- `seed_runs`
- `virtual_keys`
- `providers`
- `provider_credentials`
- `models`
- `request_logs`
- `usage_ledger`

基础设施表例外：

- `schema_migrations`：全局迁移元数据，不属于租户业务数据。

### Tenant DTO

`TenantInfo`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `tenant_id` | string | 当前认证上下文所属租户 |
| `slug` | string | 租户唯一标识 |
| `name` | string | 租户名称 |
| `service_mode` | enum | `internal_only`、`public_service` |
| `status` | enum | `active`、`disabled` |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

`TenantUpdateRequest`：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string optional | 1-160 字符，更新展示名 |
| `service_mode` | enum optional | M0 可更新服务模式，但不触发复杂合规流程 |

不允许请求体包含或更新：

- `tenant_id`
- `slug`
- `status`

## API 设计

所有接口沿用 PRD-004 契约：响应头包含 `X-Request-ID`、`X-Trace-ID`；错误响应包含 `code/message/request_id/trace_id/suggestion`。

### GET /tenant/current

认证：`Authorization: Bearer <access_token>`。

权限：已登录用户即可读取当前租户。

规则：

- 从 `AuthContext.user.tenant_id` 读取租户 ID。
- SQL 必须带 `WHERE tenant_id = ?`。
- 不接受 `tenant_id` 查询参数。
- 若租户不存在或已禁用，认证层优先返回认证错误；服务层兜底返回 `tenant.current_not_found`。

响应：

```json
{
  "tenant": {
    "tenant_id": "944f...",
    "slug": "default",
    "name": "MetaHub Default Tenant",
    "service_mode": "internal_only",
    "status": "active",
    "created_at": "2026-07-07T10:00:00Z",
    "updated_at": "2026-07-07T10:00:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### PATCH /tenant/current

认证：`Authorization: Bearer <access_token>`。

权限：`require_current_admin`。

请求：

```json
{
  "name": "MetaHub Demo Tenant",
  "service_mode": "internal_only"
}
```

规则：

- 更新目标只能是 `AuthContext.user.tenant_id`。
- 至少提供一个可更新字段。
- `name` 去首尾空格后不能为空。
- `service_mode` 只能是 `internal_only` 或 `public_service`。
- 不允许更新 `slug`、`tenant_id`、`status`。
- 更新成功写结构化日志事件 `tenant.updated`，payload 不包含密钥。

响应：同 `GET /tenant/current`，返回更新后的租户。

## 前端页面/交互设计

### 租户信息页/面板

M0 控制台还没有完整导航系统，因此先在登录后的控制台中新增“租户信息”面板。

展示：

- 租户名称。
- 租户标识 `slug`。
- 租户状态。
- 服务模式。
- 当前登录管理员邮箱。
- 最近一次更新时间。

编辑：

- 管理员可编辑租户名称。
- 服务模式可用下拉选择 `internal_only` / `public_service`。
- 保存按钮调用 `PATCH /tenant/current`。
- 保存成功后刷新面板并展示新的更新时间。
- 401/403 时清理或提示登录态/权限错误；不能只隐藏保存按钮。

不做：

- 不做租户列表。
- 不做创建/删除租户。
- 不做自助注册入口。
- 不做跨租户切换器。

## 错误码设计

| 错误码 | HTTP | 场景 | 建议 |
| --- | --- | --- | --- |
| `tenant.current_not_found` | 404 | 当前认证上下文对应的租户不存在 | 重新登录或联系管理员 |
| `tenant.update_empty` | 422 | PATCH 没有可更新字段 | 至少提供 `name` 或 `service_mode` |
| `tenant.update_invalid` | 422 | 租户名称为空、过长或服务模式非法 | 检查租户信息格式 |
| `auth.token_missing` | 401 | 未登录访问租户接口 | 重新登录 |
| `auth.session_revoked` | 401 | Session 已撤销 | 重新登录 |
| `auth.permission_denied` | 403 | 非 admin 用户更新当前租户 | 联系管理员 |

## 安全与权限约束

- 所有租户 API 必须依赖后端认证上下文，不接受前端传入的 `tenant_id` 决定目标。
- `GET /tenant/current` 只返回当前租户，不提供跨租户查询能力。
- `PATCH /tenant/current` 必须要求 admin 角色。
- 跨租户数据无法被普通接口读出：接口路径、请求体和查询参数均没有选择其他租户的入口。
- `tenant.updated` 日志只记录字段变更摘要、actor、tenant_id、trace_id，不记录 Token、Provider Key 或数据库连接信息。
- 控制台隐藏编辑按钮不算权限，后端必须返回 403。
- 当前租户被禁用时，PRD-006 的认证层会拒绝 Token；租户服务仍需做兜底检查。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0013-1 | seed 默认租户 | `tenants` 存在 `default`，状态为 `active` |
| TC-0013-2 | `GET /tenant/current` | 登录管理员可读取当前租户 |
| TC-0013-3 | `PATCH /tenant/current` 更新名称 | 返回更新后的租户，并写入 `updated_at` |
| TC-0013-4 | `PATCH /tenant/current` 更新服务模式 | 仅当前租户被更新 |
| TC-0013-5 | 控制台租户信息面板 | 登录后展示当前租户，保存后刷新 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0014-1 | 未登录访问 | 返回 `auth.token_missing` |
| TC-0014-2 | 非 admin 更新 | 返回 `auth.permission_denied` |
| TC-0014-3 | PATCH 空请求 | 返回 `tenant.update_empty` |
| TC-0014-4 | 请求体包含其他 `tenant_id` | 被忽略或校验拒绝，不能更新其他租户 |
| TC-0014-5 | 数据库存在第二租户 | 默认管理员只能读写自己的 `tenant_id` |
| TC-0014-6 | MVP 表审计 | 除 `schema_migrations` 外，业务表均含 `tenant_id` 或强外键追溯 |
| TC-0014-7 | 禁用租户 Session | 当前 Token 访问租户接口返回认证错误或 `tenant.current_not_found` |

## 代码修改计划

已按以下顺序完成：

1. 扩展错误码注册表：`tenant.current_not_found`、`tenant.update_empty`、`tenant.update_invalid`。
2. 新增 `control-plane-api/src/metahub_control/tenants/models.py`：`TenantInfo`、`TenantCurrentResponse`、`TenantUpdateRequest`。
3. 新增租户 service：按 `AuthContext.user.tenant_id` 查询/更新当前租户。
4. 新增 `routes.py`：`GET /tenant/current` 使用 `require_current_user`，`PATCH /tenant/current` 使用 `require_current_admin`。
5. 在控制面 app 注册 tenant router。
6. 新增 pytest：读取当前租户、更新当前租户、未登录、非 admin、跨租户隔离、MVP 表 `tenant_id` 审计。
7. 新增控制台 API client：`getCurrentTenant`、`updateCurrentTenant`。
8. 新增租户信息面板和保存交互，接入登录态 Token。
9. 新增 Node test：租户 client、面板渲染、保存失败/401/403 降级。
10. 更新 README、`docs/development/directory-map.md`、`docs/testing/PRD-007-tests.md`。
11. 运行 `scripts/ci/check.ps1`，重启本地三服务并做真实 HTTP/浏览器验证。

不进入本 PRD 的内容：

- 不做租户列表、创建、删除。
- 不做跨租户平台管理后台。
- 不做租户自助注册。
- 不做租户级计费隔离。
- 不做数据库 RLS 或逻辑分库。

## 验证结果

已运行后端测试：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH='D:\MetaHub\shared-sdk\python;D:\MetaHub\gateway-data-plane\src;D:\MetaHub\control-plane-api\src'
& $env:METAHUB_PYTHON -m pytest -q
```

最终统一检查结果：后端 pytest `34 passed`。

当前已覆盖：

- 登录管理员可调用 `GET /tenant/current` 读取当前租户。
- admin 可调用 `PATCH /tenant/current` 更新当前租户名称和 `service_mode`。
- 未登录访问返回 `auth.token_missing`。
- 非 admin 更新返回 `auth.permission_denied`。
- PATCH 空请求返回 `tenant.update_empty`。
- 请求体包含 `tenant_id` 的跨租户尝试被拒绝，其他租户不被更新。
- MVP 业务表 `tenants`、`seed_runs`、`roles`、`users`、`sessions`、`virtual_keys`、`providers`、`provider_credentials`、`models`、`request_logs`、`usage_ledger` 均含 `tenant_id`；`schema_migrations` 作为基础设施表例外。

已运行控制台测试：

```powershell
cd D:\MetaHub\console-web
node --test tests/unit/*.test.mjs
node scripts/lint.mjs
```

最终统一检查结果：Node test `16 pass`，console lint `ok`。

当前已覆盖：

- `getCurrentTenant` / `updateCurrentTenant` 携带 Bearer Token。
- PATCH 请求体不包含 `tenant_id`。
- 租户 API 结构化错误返回 `AuthApiError`。
- 租户信息面板渲染、转义和错误展示。

已完成真实 HTTP 验证：

- `POST /auth/login` 使用默认租户管理员登录返回 200。
- `GET /tenant/current` 返回默认租户 `default`。
- `PATCH /tenant/current` 将名称更新为 `MetaHub Verified Tenant`、`service_mode=internal_only` 返回 200。
- 再次 `GET /tenant/current` 可读到更新后的名称。
- PATCH 请求体携带 `tenant_id` 被 Pydantic 拒绝，返回 422 与统一错误码 `infra.validation_failed`。
- 未携带 Token 访问 `GET /tenant/current` 返回 401 与统一错误码 `auth.token_missing`。
- 验证结束后通过 `PATCH /tenant/current` 恢复为 `MetaHub Default Tenant`。

已完成真实浏览器验证：

- 控制台页面登录后展示租户信息面板，`#tenant-form`、`input[name="tenantName"]`、`select[name="serviceMode"]`、保存按钮均唯一可定位。
- 在浏览器内提交 `MetaHub Browser Tenant` 后，后端写入 `tenant.updated` 事件，`PATCH /tenant/current` 返回 200。
- 页面重新渲染后，租户名称输入框读回值为 `MetaHub Browser Tenant`，浏览器控制台无 error/warning。
- 验证结束后通过 API 恢复为 `MetaHub Default Tenant`，避免本地开发库保留测试租户名。
