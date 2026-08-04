# PRD-008 - 基础 RBAC 角色设计

状态：已实现。

实现说明：本 PRD 已按设计完成 `roles` 固定角色目录、seed 幂等写入、后端角色守卫、用户管理 API、用户管理控制台面板、权限绕过测试、角色回归测试和文档更新。M0 仍不做自定义角色、部门权限、字段级权限、SSO/LDAP/SCIM、邀请邮件或跨租户超级管理员。

设计来源：Excel `PRD-008 - 基础RBAC角色`、页面清单 `PAGE-010/PAGE-011`、API 清单 `GET /users`、`POST /users`、`PATCH /users/{id}/role`、数据对象 `User/Role`、事件 `user.role_changed`、验收用例 `TC-0015/TC-0016`。架构约束要求所有管理 API 必须做后端租户鉴权，不能只依赖前端隐藏按钮；所有业务表必须包含 `tenant_id` 或可通过强外键追溯到 `tenant_id`。

## 需求理解

本 PRD 的目标是在 PRD-006 登录态和 PRD-007 租户上下文基础上，建立 M0 可用的最小 RBAC：`admin`、`developer`、`viewer` 三种角色。控制台可以根据角色隐藏入口，但真正的权限判断必须放在后端依赖和服务层。

核心范围：

- 管理员可查看当前租户用户列表。
- 管理员可创建当前租户内用户，并指定 `admin`、`developer` 或 `viewer`。
- 管理员可修改当前租户内用户角色。
- 后端提供可复用角色守卫，供 Provider、Virtual Key 等后续管理 API 接入。
- Developer 不能新增 Provider。
- Viewer 不能创建 Virtual Key。
- 所有用户管理查询和更新都只作用于当前认证上下文的 `tenant_id`。
- 角色变更产生 `user.role_changed` 结构化事件；M0 先写结构化日志，后续 Webhook PRD 接入投递。

明确不做：

- 不做复杂部门权限、组织树权限、项目级矩阵权限。
- 不做字段级权限。
- 不做自定义角色和权限点配置页面。
- 不做 SCIM/LDAP/SSO 用户同步。
- 不做邮件邀请、密码重置、自助注册。
- 不做跨租户平台超级管理员。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 控制面认证/权限 | `control-plane-api/src/metahub_control/auth/` | 收敛角色枚举，新增通用角色依赖，保留 `require_current_admin` |
| 用户管理 | `control-plane-api/src/metahub_control/users/` | 新增 models/service/routes，实现 `GET /users`、`POST /users`、`PATCH /users/{id}/role` |
| 共享运行时 | `shared-sdk/python/metahub_shared/runtime/` | 扩展用户/RBAC 错误码，统一返回 request_id/trace_id |
| 数据库迁移与种子 | `infra/migrations/`、`shared-sdk/python/metahub_shared/db/seed.py` | 新增角色目录表并写入三种固定角色；复用现有 `users.role` |
| 控制台 | `console-web/` | 新增用户管理面板、角色选择、按角色隐藏入口 |
| 测试与文档 | `control-plane-api/tests/`、`console-web/tests/`、`docs/testing/` | 覆盖用户管理主流程、权限绕过、角色回归和控制台隐藏逻辑 |

编码时保留中文注释，重点解释“前端隐藏入口只是体验，后端角色依赖才是权限边界”。

## 数据表/字段设计

### users

PRD-003 已创建 `users` 表，PRD-006 已扩展登录安全字段。本 PRD 继续复用 `users.role`，不迁移现有用户主表结构。

本 PRD 服务层强制 `role` 只能取以下值：

| role | 中文名 | M0 权限含义 |
| --- | --- | --- |
| `admin` | 管理员 | 当前租户配置管理、用户管理、Provider 管理、Virtual Key 管理 |
| `developer` | 开发者 | 后续可管理自己的 Virtual Key；不能管理 Provider、用户、租户系统配置 |
| `viewer` | 只读用户 | 只读控制台信息；不能创建 Virtual Key，不能修改配置 |

约束：

- 所有用户查询条件必须包含 `tenant_id = AuthContext.user.tenant_id`。
- API 响应不返回 `password_hash`、失败登录计数、锁定时间或 Session 信息。
- 创建用户时密码只进入哈希函数，不落日志、不返回响应。
- 禁止把最后一个 active admin 改成非 admin，避免租户锁死。
- M0 建议禁止管理员把自己的角色改成非 admin；如后续允许，仍必须先检查剩余 active admin。

### roles

新增 `roles` 作为租户内固定角色目录，支撑页面角色选择和后续角色展示。M0 不开放角色 CRUD，也不允许新增自定义角色。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `role_id` | text | primary key | 稳定 ID，建议由 `tenant_id + code` 生成 |
| `tenant_id` | text | not null, FK, index | 所属租户 |
| `code` | text | not null | `admin`、`developer`、`viewer` |
| `name` | text | not null | 展示名，例如“管理员” |
| `description` | text | not null default '' | 面向控制台的角色说明 |
| `status` | text | not null | `active`、`disabled`；M0 全部 active |
| `created_at` | text | not null | 创建时间 |
| `updated_at` | text | not null | 更新时间 |

索引与约束：

- `UNIQUE (tenant_id, code)`。
- `CHECK (code IN ('admin', 'developer', 'viewer'))`。
- `CHECK (status IN ('active', 'disabled'))`。
- `idx_roles_tenant_id`。

迁移计划：

- 新增 `0003_basic_rbac_roles.sql` 创建 `roles` 表和索引。
- 新增 `0003_basic_rbac_roles.down.sql` 删除 `roles` 表。
- 扩展 seed：默认租户写入三种角色；默认管理员仍为 `admin`。
- 暂不为 `users.role` 增加数据库 FK；由于 SQLite 现有表改约束成本高，M0 由服务层和测试强制枚举。未来 PostgreSQL 迁移可追加 `(tenant_id, role)` 外键或 CHECK 约束。

## API 设计

所有接口都在 Control Plane API，均使用 PRD-006 Bearer Token 和 PRD-007 当前租户上下文。所有错误响应沿用统一结构：`code`、`message`、`request_id`、`trace_id`、`suggestion`。

### GET /users

用途：管理员查看当前租户用户列表，并获取固定角色选项。

权限：`admin`。

查询参数：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `status` | string | 否 | `active`、`disabled`；默认返回全部 |

响应：

```json
{
  "users": [
    {
      "user_id": "usr_xxx",
      "tenant_id": "tenant_xxx",
      "email": "dev@example.com",
      "display_name": "Dev User",
      "role": "developer",
      "status": "active",
      "created_at": "2026-07-07T00:00:00Z",
      "updated_at": "2026-07-07T00:00:00Z",
      "last_login_at": null
    }
  ],
  "roles": [
    {
      "code": "admin",
      "name": "管理员",
      "description": "管理当前租户配置、用户和 Key。"
    }
  ],
  "request_id": "req_xxx",
  "trace_id": "trc_xxx"
}
```

### POST /users

用途：管理员在当前租户内创建本地用户。

权限：`admin`。

请求体：

```json
{
  "email": "dev@example.com",
  "display_name": "Dev User",
  "password": "LocalDev!2026",
  "role": "developer",
  "status": "active"
}
```

规则：

- `email` 规范化为小写，按 `(tenant_id, email)` 唯一。
- `password` 最小 8 位，最大 256 位；只保存 PBKDF2 哈希。
- `role` 必须是 active 角色目录中的 `admin`、`developer`、`viewer`。
- `status` 可选，默认 `active`；M0 只支持 `active`、`disabled`。
- 不记录明文密码，不返回明文密码。

响应：201，返回创建后的用户对象、`request_id`、`trace_id`。

事件：`user.created` 可先作为结构化日志写入；本 PRD 验收重点是 `user.role_changed`。

### PATCH /users/{id}/role

用途：管理员修改当前租户内用户角色。

权限：`admin`。

路径参数：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 目标 `user_id` |

请求体：

```json
{
  "role": "viewer"
}
```

规则：

- 目标用户必须属于当前 `tenant_id`，否则统一返回 `user.not_found`。
- `role` 必须是 active 角色目录中的合法值。
- 如果目标用户是最后一个 active admin，不能改为非 admin，返回 `user.last_admin_required`。
- 如果目标用户是当前登录管理员本人，M0 默认禁止降级为非 admin，返回 `user.self_role_change_denied`。
- 成功后写 `user.role_changed` 结构化日志，payload 包含 `before_role`、`after_role`，不包含密码、Token、Provider Key。

响应：200，返回更新后的用户对象、`request_id`、`trace_id`。

### 后续业务接口的角色守卫

本 PRD 不实现 Provider 和 Virtual Key 的完整业务接口，但要提供可复用后端依赖并在测试中覆盖验收边界：

| 依赖 | 允许角色 | 用途 |
| --- | --- | --- |
| `require_current_admin` | `admin` | 用户、租户、Provider、系统配置 |
| `require_virtual_key_writer` | `admin`、`developer` | 后续创建/管理 Virtual Key |
| `require_current_user` | `admin`、`developer`、`viewer` | 当前登录用户只读接口 |

Provider 新增接口后必须使用 `require_current_admin`；Virtual Key 创建接口后必须使用 `require_virtual_key_writer`，因此 viewer 会被后端 403 拦截。

## 前端页面/交互设计

### 用户管理

M0 控制台尚未建立完整导航，因此先在登录后的控制台首页新增“用户管理”面板。

管理员视图：

- 展示当前租户用户列表：邮箱、显示名、角色、状态、最近登录时间。
- 提供创建用户表单：邮箱、显示名、初始密码、角色选择。
- 每个用户提供角色下拉选择。
- 保存角色调用 `PATCH /users/{id}/role`。
- 创建用户调用 `POST /users`。
- 显示统一错误文案和 `request_id`。

Developer / Viewer 视图：

- 不展示用户管理入口或编辑按钮。
- 这只是体验优化；如果直接调用 `/users` 后端仍返回 `auth.permission_denied`。

### 角色选择

- 角色选项优先使用 `GET /users` 响应中的 `roles`。
- 若接口失败，不允许提交创建/改角色；显示错误态。
- 角色说明使用简短中文文案，避免把权限矩阵塞进 UI。

### Provider / Virtual Key 入口预留

- Provider 创建入口仅 admin 可见。
- Virtual Key 创建入口 admin 和 developer 可见，viewer 不可见。
- 真正的权限仍以后端依赖为准。

## 错误码设计

| 错误码 | HTTP | 场景 | 建议 |
| --- | --- | --- | --- |
| `auth.permission_denied` | 403 | 非 admin 调用用户管理或 Provider 管理；viewer 创建 Virtual Key | 联系管理员授予角色 |
| `user.not_found` | 404 | 目标用户不存在或不属于当前租户 | 检查用户 ID |
| `user.email_exists` | 409 | 当前租户内邮箱已存在 | 换用其他邮箱 |
| `user.role_invalid` | 422 | 角色不是 `admin/developer/viewer` 或角色已 disabled | 选择有效角色 |
| `user.password_invalid` | 422 | 初始密码为空、过短或过长 | 使用符合要求的临时密码 |
| `user.create_invalid` | 422 | 创建用户请求缺少必要字段或状态非法 | 检查用户信息 |
| `user.last_admin_required` | 409 | 降级最后一个 active admin | 先创建或提升另一个管理员 |
| `user.self_role_change_denied` | 409 | 管理员尝试把自己降级为非 admin | 请由其他管理员操作 |

## 安全与权限约束

- 所有 `/users` 接口必须使用后端 `require_current_admin`，不能只靠控制台隐藏按钮。
- 所有查询和更新条件都必须包含当前认证上下文的 `tenant_id`。
- 请求体中的 `tenant_id` 一律不作为目标租户依据；用户创建只能落到当前租户。
- 用户响应不暴露 `password_hash`、Session、Token、锁定原因和失败登录次数。
- 初始密码不得落日志；错误日志只记录错误码、request_id、trace_id、tenant_id、actor_id。
- 角色变更事件只记录 `before_role`、`after_role`、目标用户和操作人，不记录敏感字段。
- 跨租户 `user_id` 应返回 `user.not_found`，避免泄露其他租户用户存在性。
- Provider Key 仍不得明文落日志；本 PRD 的权限测试要确保非 admin 无法触达 Provider 创建入口。
- 后续任何跨租户平台管理能力都不进入 M0。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0015-1 | seed 角色目录 | 默认租户存在 active `admin/developer/viewer` |
| TC-0015-2 | `GET /users` | admin 可读取当前租户用户和角色选项，响应不含密码哈希 |
| TC-0015-3 | `POST /users` | admin 可创建 developer/viewer 用户，数据库写入当前 `tenant_id` 和哈希密码 |
| TC-0015-4 | 新用户登录 | 被创建的 developer 可登录，`/auth/me` 返回 role=`developer` |
| TC-0015-5 | `PATCH /users/{id}/role` | admin 可把 developer 改为 viewer，产生 `user.role_changed` 日志 |
| TC-0015-6 | 控制台用户管理 | admin 登录后可见用户管理面板和角色选择 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0016-1 | 非 admin 调用 `/users` | 返回 403 `auth.permission_denied` |
| TC-0016-2 | Developer 新增 Provider | 后端依赖返回 403 `auth.permission_denied` |
| TC-0016-3 | Viewer 创建 Virtual Key | 后端依赖返回 403 `auth.permission_denied` |
| TC-0016-4 | 非法角色 | 返回 422 `user.role_invalid` |
| TC-0016-5 | 重复邮箱 | 返回 409 `user.email_exists` |
| TC-0016-6 | 跨租户 user_id 改角色 | 返回 404 `user.not_found`，其他租户用户不变 |
| TC-0016-7 | 降级最后一个 admin | 返回 409 `user.last_admin_required` |
| TC-0016-8 | 密码泄露回归 | API 响应、结构化日志、数据库 Session 中不出现明文初始密码 |
| TC-0016-9 | 控制台隐藏回归 | developer/viewer 登录后不渲染用户管理按钮或 Provider 创建入口 |

## 代码修改计划

已按以下顺序完成：

1. 新增 `0003_basic_rbac_roles.sql` / `.down.sql`，创建 `roles` 表和索引。
2. 扩展 seed，将默认租户三种固定角色幂等写入 `roles`。
3. 在认证模块新增角色常量和通用角色守卫：`require_any_role`、`require_virtual_key_writer`，保留 `require_current_admin`。
4. 扩展错误码注册表：`user.not_found`、`user.email_exists`、`user.role_invalid`、`user.password_invalid`、`user.create_invalid`、`user.last_admin_required`、`user.self_role_change_denied`。
5. 新增 `control-plane-api/src/metahub_control/users/models.py`，定义 `UserInfo`、`RoleInfo`、`UserListResponse`、`CreateUserRequest`、`UpdateUserRoleRequest`。
6. 新增 `users/service.py`，实现当前租户用户列表、创建用户、角色更新、最后 admin 保护、结构化事件日志。
7. 新增 `users/routes.py` 并在 `control-plane-api/src/metahub_control/app.py` 注册 router。
8. 控制台新增 `console-web/src/lib/users.js` 和用户管理渲染组件，接入登录态 Token。
9. 控制台按 `authContext.user.role` 隐藏用户管理、Provider 创建、Virtual Key 创建入口预留位。
10. 新增后端集成测试，覆盖主流程、非 admin、Developer Provider 拦截、Viewer Virtual Key 拦截、跨租户、最后 admin 保护。
11. 新增前端 Node test，覆盖 users client、用户面板、角色选择、隐藏逻辑和错误展示。
12. 更新 README、`docs/development/directory-map.md`、`docs/testing/PRD-008-tests.md`。
13. 运行 `scripts/ci/check.ps1`，重启本地三服务并做真实 HTTP/浏览器验证。

## 验证结果

已运行后端专项测试：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH='D:\MetaHub\shared-sdk\python;D:\MetaHub\gateway-data-plane\src;D:\MetaHub\control-plane-api\src'
& $env:METAHUB_PYTHON -m pytest control-plane-api/tests/integration/test_basic_rbac_users.py -q
```

当前结果：`4 passed`。

最终统一检查结果：后端 pytest `38 passed`。

已运行控制台专项测试：

```powershell
cd D:\MetaHub\console-web
npm test
npm run lint
```

当前结果：Node test `21 pass`，console lint `ok`。

最终统一检查结果：Node test `21 pass`，console lint `ok`。

当前已覆盖：

- 默认租户 seed 后存在 active `admin/developer/viewer` 角色目录。
- admin 可调用 `GET /users` 读取当前租户用户和角色选项，响应不包含 `password_hash`。
- admin 可调用 `POST /users` 创建 developer/viewer/admin，本地数据库只保存密码哈希。
- 新创建用户可登录，`GET /auth/me` 返回最新角色。
- admin 可调用 `PATCH /users/{id}/role` 修改当前租户用户角色。
- 非 admin 调用 `/users` 返回 `auth.permission_denied`。
- Developer 不能新增 Provider，Viewer 不能创建 Virtual Key；测试通过后端依赖模拟业务接口验证。
- 非法角色、重复邮箱、跨租户 user_id、自降级和最后 active admin 降级均被后端拒绝。
- 控制台 admin 可见用户管理和角色选择；developer/viewer 不渲染用户管理入口。

已完成真实 HTTP 验证：

- `POST /auth/login` 使用默认租户管理员登录返回 200，当前角色为 `admin`。
- 本地默认租户 `roles` 表包含 active `admin/developer/viewer`。
- `GET /users` 返回用户列表和角色选项，响应中不含 `password_hash`。
- `POST /users` 创建 developer 用户返回 201，响应中不含明文初始密码。
- 新创建 developer 可登录，`GET /auth/me` 返回 role=`developer`。
- developer 直接调用 `GET /users` 返回 403 `auth.permission_denied`。
- `PATCH /users/{id}/role` 使用非法角色返回 422 `user.role_invalid`。
- admin 将 developer 改为 viewer 返回 200，并写入 `user.role_changed` 结构化事件。
- viewer 直接调用 `GET /users` 返回 403 `auth.permission_denied`。
- 跨租户 `user_id` 改角色返回 404 `user.not_found`，其他租户用户角色保持不变。
- 验证结束后已清理临时用户和临时 session。

已完成真实浏览器验证：

- admin 登录控制台后，页面存在 `.users-panel`、`#create-user-form`，并能看到临时 developer 用户行。
- admin 页面用户角色表单数量大于 0，浏览器控制台无 error/warning。
- developer 登录控制台后，`.users-panel`、`#create-user-form`、`[data-user-role-form]` 均不渲染。
- developer 页面显示当前登录邮箱和 role=`developer`，浏览器控制台无 error/warning。
- 验证结束后已清理临时 developer 用户。
