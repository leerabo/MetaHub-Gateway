# PRD-006 - 管理员登录与 Session 设计

状态：已实现。

实现说明：本 PRD 已按设计完成本地管理员登录、PBKDF2 密码校验、HMAC-SHA256 JWT、可撤销 Session、登出、失败限制、后端管理员权限依赖、控制台登录页和个人菜单。M0 仍不做 SSO/LDAP/SCIM/MFA，不做 refresh token。

设计来源：Excel `PRD-006 - 管理员登录与Session`、页面清单 `PAGE-007/PAGE-008`、API 清单 `POST /auth/login`、`POST /auth/logout`、`GET /auth/me`、数据对象 `User/Session`、事件 `user.logged_in`、验收用例 `TC-0011/TC-0012`。架构约束要求控制面服务企业管理员，所有管理 API 必须做后端租户鉴权，业务数据必须包含 `tenant_id` 或能通过强外键追溯到 `tenant_id`。

## 需求理解

本 PRD 的目标是让企业管理员可以用本地账号登录控制台，并让后续控制面管理 API 拿到可信的用户、租户和角色上下文。M0 只支持本地账号，不接入 SSO/LDAP/SCIM，不做多因子认证。

核心范围：

- 管理员通过邮箱、密码、租户标识登录。
- 密码只保存哈希，不保存明文。
- 登录成功签发短期访问 Token，并创建可撤销 Session。
- `GET /auth/me` 返回当前登录用户、租户、角色和 Session 信息。
- `POST /auth/logout` 撤销当前 Session。
- 错误密码达到阈值后锁定账号一段时间，降低暴力破解风险。
- Token 过期、缺失、伪造或已登出后，后端返回统一 401 错误结构。
- 登录成功产生 `user.logged_in` 事件。M0 先进入结构化日志；后续 Webhook PRD 再接入投递。

明确不做：

- 不做 SSO、LDAP、SCIM。
- 不做 MFA、短信/邮件验证码。
- 不做复杂组织/部门权限模型，M0 只使用 `users.role`。
- 不把登录状态只放在前端；后端必须校验 Token 和 Session 状态。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 控制面认证 | `control-plane-api/src/metahub_control/auth/` | 新增登录、登出、当前用户接口和认证依赖 |
| 共享安全工具 | `shared-sdk/python/metahub_shared/db/security.py` | 复用 `hash_password/verify_password`，新增 token 哈希辅助 |
| 共享运行时 | `shared-sdk/python/metahub_shared/runtime/` | 扩展认证错误码、配置项、鉴权上下文类型 |
| 数据库迁移 | `infra/migrations/` | 新增 Session 表，扩展 User 登录状态字段 |
| 控制台 | `console-web/` | 新增登录页、登录态 API 客户端、个人菜单和登出交互 |
| 测试与文档 | `control-plane-api/tests/`、`console-web/tests/`、`docs/testing/` | 覆盖登录主流程、失败锁定、Token 过期/登出/权限异常 |

编码时保留详细中文注释，重点解释密码哈希、Token 不落明文、Session 撤销、租户隔离和失败限制的安全边界。

## 数据表/字段设计

### users 表扩展

PRD-003 已创建 `users`，包含 `tenant_id`、`email`、`password_hash`、`role`、`status`。PRD-006 通过新迁移扩展登录安全字段：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `failed_login_count` | integer | not null default 0 | 连续错误密码次数 |
| `locked_until` | datetime nullable | index | 达到阈值后的锁定截止时间 |
| `last_login_at` | datetime nullable |  | 最近一次成功登录时间 |
| `password_changed_at` | datetime nullable |  | 后续强制重新登录/密码轮换预留 |

设计约束：

- `password_hash` 继续使用 PBKDF2-HMAC-SHA256；不引入本机不可用依赖。
- 成功登录后清零 `failed_login_count` 和 `locked_until`。
- 禁用用户 `status='disabled'` 不能登录，返回统一认证失败，不泄露账号状态。

### sessions 表

新增 `sessions` 表，所有记录必须带 `tenant_id`。

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `session_id` | text | primary key | Session ID，写入 JWT `sid` |
| `tenant_id` | text | not null, FK, index | 所属租户 |
| `user_id` | text | not null, FK, index | 登录用户 |
| `token_jti` | text | not null unique | JWT ID，用于定位 Token |
| `token_hash` | text | not null unique | 访问 Token 哈希，数据库不保存明文 Token |
| `status` | text | not null | `active`、`revoked`、`expired` |
| `expires_at` | datetime | not null, index | Token/Session 过期时间 |
| `revoked_at` | datetime nullable |  | 登出或强制失效时间 |
| `last_seen_at` | datetime nullable |  | 最近一次 `auth/me` 或鉴权使用时间 |
| `user_agent` | text nullable |  | 登录设备摘要，限制长度，不记录敏感 Header |
| `created_ip` | text nullable |  | 登录来源 IP，M0 用于排障，后续可改为哈希 |
| `created_at` | datetime | not null | 创建时间 |
| `updated_at` | datetime | not null | 更新时间 |

索引：

- `idx_sessions_tenant_id`
- `idx_sessions_user_id`
- `idx_sessions_status_expires_at`
- `idx_sessions_token_jti`

### Token 设计

M0 使用标准库实现 HMAC-SHA256 JWT，不新增外部依赖。

JWT payload：

| 字段 | 说明 |
| --- | --- |
| `iss` | `metahub-control-plane` |
| `aud` | `metahub-console` |
| `sub` | `user_id` |
| `tid` | `tenant_id` |
| `sid` | `session_id` |
| `role` | 用户角色，M0 为 `admin` |
| `jti` | Token 唯一 ID |
| `iat` | 签发时间 |
| `exp` | 过期时间 |

配置项：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `METAHUB_AUTH_TOKEN_SECRET` | dev/test 可使用本地默认；prod 必填 | JWT HMAC 密钥，必须脱敏 |
| `METAHUB_SESSION_TTL_SECONDS` | `28800` | 默认 8 小时 |
| `METAHUB_LOGIN_MAX_FAILURES` | `5` | 连续错误密码上限 |
| `METAHUB_LOGIN_LOCK_SECONDS` | `900` | 锁定 15 分钟 |
| `METAHUB_DEFAULT_TENANT_SLUG` | `default` | 登录页未填写租户时的默认租户 |

## API 设计

所有接口沿用 PRD-004 契约：响应头包含 `X-Request-ID`、`X-Trace-ID`；错误响应包含 `code/message/request_id/trace_id/suggestion`。

### POST /auth/login

请求：

```json
{
  "tenant_slug": "default",
  "email": "admin@metahub.local",
  "password": "MetaHubLocal!2026"
}
```

规则：

- `tenant_slug` 可选，缺省读取 `METAHUB_DEFAULT_TENANT_SLUG`。
- `email` 统一转小写、去首尾空格。
- 不论用户不存在、密码错误、用户禁用、租户禁用，默认返回统一认证失败，避免账号枚举。
- 对已存在本地账号，错误密码累计 `failed_login_count`；达到阈值后写入 `locked_until`。
- 成功登录创建 `sessions`，写入 `token_hash`，返回明文 Token 给客户端一次。
- 登录成功记录结构化事件 `user.logged_in`，payload 不包含密码、Token、Provider Key。

成功响应：

```json
{
  "access_token": "eyJ...",
  "token_type": "Bearer",
  "expires_at": "2026-07-07T18:00:00Z",
  "session_id": "ses_...",
  "user": {
    "user_id": "usr_...",
    "tenant_id": "ten_...",
    "email": "admin@metahub.local",
    "display_name": "MetaHub Admin",
    "role": "admin"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### GET /auth/me

认证：`Authorization: Bearer <access_token>`。

规则：

- 校验 JWT 签名、`exp`、`aud`、`iss`。
- 使用 `sid/jti/token_hash` 查 `sessions`，必须是 `active` 且未过期。
- 校验用户、租户仍为 active。
- 返回用户、租户、角色和 session 到期时间，不返回 `password_hash`、`token_hash`。

响应：

```json
{
  "user": {
    "user_id": "usr_...",
    "tenant_id": "ten_...",
    "email": "admin@metahub.local",
    "display_name": "MetaHub Admin",
    "role": "admin"
  },
  "tenant": {
    "tenant_id": "ten_...",
    "slug": "default",
    "name": "MetaHub Default Tenant"
  },
  "session": {
    "session_id": "ses_...",
    "expires_at": "2026-07-07T18:00:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### POST /auth/logout

认证：`Authorization: Bearer <access_token>`。

规则：

- 校验当前 Token 和 Session。
- 将当前 Session 更新为 `revoked`，写入 `revoked_at`。
- 重复登出或已撤销 Token 返回 401，避免客户端误以为仍持有有效登录态。

响应：

```json
{
  "logged_out": true,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

## 前端页面/交互设计

### 登录页

页面入口：未登录访问控制台时展示登录页。

控件：

- 租户标识输入框，默认 `default`。
- 邮箱输入框。
- 密码输入框。
- 登录按钮，提交时显示加载态，防止重复提交。
- 错误提示区展示统一错误信息和 `request_id`，不展示账号是否存在。

交互：

- 登录成功后将 Token 放入 `sessionStorage`，并立即调用 `/auth/me` 获取用户上下文。
- M0 不使用长期 refresh token；关闭浏览器会话后需要重新登录。
- API 客户端自动携带 `Authorization`，但不把 Token 打印到控制台。

### 个人菜单

页面入口：登录后控制台右上角。

展示：

- 用户显示名、邮箱、角色。
- 当前租户名称。
- Session 到期时间。
- 登出按钮。

交互：

- 点击登出调用 `/auth/logout`，成功或 401 后清理本地登录态并返回登录页。
- `/auth/me` 返回 401 时自动清理登录态，避免前端只隐藏按钮造成伪登录状态。

## 错误码设计

| 错误码 | HTTP | 场景 | 建议 |
| --- | --- | --- | --- |
| `auth.login_invalid` | 401 | 租户、账号或密码无效；用户/租户禁用 | 检查登录信息 |
| `auth.login_locked` | 429 | 连续失败达到阈值，账号临时锁定 | 等待锁定结束或联系管理员 |
| `auth.token_missing` | 401 | 缺少 Bearer Token | 重新登录 |
| `auth.token_invalid` | 401 | Token 格式错误、签名错误、aud/iss 不匹配 | 重新登录 |
| `auth.token_expired` | 401 | Token 或 Session 已过期 | 重新登录 |
| `auth.session_revoked` | 401 | Session 已登出或撤销 | 重新登录 |
| `auth.permission_denied` | 403 | 已认证但角色不满足后端权限要求 | 联系管理员开通权限 |

错误响应不得包含密码、Token、`password_hash`、`token_hash` 或内部 SQL 错误。

## 安全与权限约束

- Provider Key、Authorization、access token、password、session token 不能明文落日志。
- 数据库只保存 `token_hash`，不保存明文 Bearer Token。
- `sessions` 属于业务安全数据，必须带 `tenant_id`。
- 后端认证依赖负责校验租户、用户、Session 状态；控制台隐藏按钮不能替代后端鉴权。
- 登录失败响应统一，避免通过错误文案枚举账号、租户或禁用状态。
- `METAHUB_AUTH_TOKEN_SECRET` 在 prod 必须配置；配置摘要只显示 `***MASKED***`。
- 成功登录和失败登录都要进入结构化日志；失败日志只记录安全摘要，不记录密码或 Token。
- `user.logged_in` 事件 payload 至少包含 `event_id`、`tenant_id`、`timestamp`、`actor_id`、`object_id`、`trace_id`、`payload`，M0 先作为结构化日志输出。
- 后续管理 API 统一复用 `require_current_admin`，不能每个路由自行解析 Token。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0011-1 | 迁移 + seed 后管理员登录 | 正确邮箱和密码返回 Bearer Token、Session、用户信息 |
| TC-0011-2 | 密码哈希 | `users.password_hash` 不等于明文密码，且 `verify_password` 可通过 |
| TC-0011-3 | `/auth/me` | 携带有效 Token 返回当前用户、租户、Session 信息 |
| TC-0011-4 | `/auth/logout` | 登出后 Session 变为 `revoked` |
| TC-0011-5 | 控制台登录页 | 登录成功后进入控制台并展示个人菜单 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0012-1 | 错误密码 | 返回 `auth.login_invalid`，累计失败次数 |
| TC-0012-2 | 暴力破解限制 | 连续失败达到阈值后返回 `auth.login_locked` |
| TC-0012-3 | Token 过期 | `/auth/me` 返回 `auth.token_expired` |
| TC-0012-4 | 伪造 Token | `/auth/me` 返回 `auth.token_invalid` |
| TC-0012-5 | 已登出 Token | `/auth/me` 返回 `auth.session_revoked` |
| TC-0012-6 | 无 Token | `/auth/me` 返回 `auth.token_missing` |
| TC-0012-7 | 禁用用户 | 登录返回统一 `auth.login_invalid` |
| TC-0012-8 | 敏感信息 | 日志和 API 响应不包含明文密码、Bearer Token、`password_hash`、`token_hash` |
| TC-0012-9 | 前端 401 降级 | Token 失效后控制台清理登录态并显示登录页 |

## 代码修改计划

已按以下顺序完成：

1. 新增迁移 `0002_admin_sessions.sql` 和 down migration：扩展 `users`，创建 `sessions` 表和索引。
2. 扩展配置加载：读取 `METAHUB_AUTH_TOKEN_SECRET`、Session TTL、失败阈值、锁定时间、默认租户。
3. 扩展错误码注册表：新增 `auth.login_invalid`、`auth.login_locked`、`auth.token_*`、`auth.session_revoked`、`auth.permission_denied`。
4. 新增认证模型和服务层：登录校验、失败累计、Session 创建/撤销、JWT 签发/校验。
5. 新增 FastAPI 路由：`POST /auth/login`、`GET /auth/me`、`POST /auth/logout`。
6. 注册控制面 auth router，并调整 CORS 允许 `Authorization` 和 `POST`。
7. 新增共享鉴权依赖 `require_current_user` / `require_current_admin`，供后续 PRD-007+ 管理 API 复用。
8. 新增后端集成测试：主流程、锁定、过期、伪造、登出、敏感信息。
9. 新增控制台登录态模块：auth API client、登录页、个人菜单、401 清理。
10. 新增前端单元测试：登录成功、登录失败、登出、过期降级。
11. 更新 `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-006-tests.md`。
12. 运行 `scripts/ci/check.ps1`，再重启本地三服务并做真实 HTTP 验证。

不进入本 PRD 的内容：

- 不做密码重置、邀请用户、用户列表管理。
- 不做 SSO/LDAP/SCIM/MFA。
- 不做 refresh token 和长期登录。
- 不做完整 RBAC 策略矩阵；只提供 `role='admin'` 的后端校验基础。

## 验证结果

已运行后端测试：

```powershell
$env:METAHUB_PYTHON='C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$env:PYTHONPATH='D:\MetaHub\shared-sdk\python;D:\MetaHub\gateway-data-plane\src;D:\MetaHub\control-plane-api\src'
& $env:METAHUB_PYTHON -m pytest -q
```

结果：

- 后端 pytest：`30 passed`

已运行控制台测试：

```powershell
cd D:\MetaHub\console-web
node --test tests/unit/*.test.mjs
node scripts/lint.mjs
```

结果：

- 控制台 Node test：`12 passed`
- 控制台 lint：通过

已覆盖：

- 正确管理员账号可登录，返回 Bearer Token、Session 和用户信息。
- `/auth/me` 能返回当前用户、租户、Session 信息。
- `/auth/logout` 后原 Token 返回 `auth.session_revoked`。
- 错误密码达到阈值后返回 `auth.login_locked`。
- 缺失、伪造、过期 Token 分别返回统一错误码。
- 非 admin 角色访问后端管理员依赖返回 `auth.permission_denied`。
- `users.password_hash` 不等于明文密码，且访问 Token 不以明文写入 `sessions`。
- 控制台登录页、个人菜单、认证 API client 和 401 降级逻辑通过单元测试。

真实 HTTP 验证：

- `POST http://localhost:8001/auth/login` 使用 `default / admin@metahub.local / MetaHubLocal!2026` 返回 HTTP 200，`request_id=req_real_post_auth_login`。
- `GET http://localhost:8001/auth/me` 携带 Bearer Token 返回 HTTP 200，用户邮箱为 `admin@metahub.local`，租户为 `default`。
- 将当前 Session `expires_at` 改为过去时间后，`GET /auth/me` 返回 HTTP 401，错误码为 `auth.token_expired`。
- 再次登录后调用 `POST /auth/logout` 返回 HTTP 200，`logged_out=true`。
- 登出后复用原 Token 调用 `GET /auth/me` 返回 HTTP 401，错误码为 `auth.session_revoked`。
- 不带 Token 调用 `GET /auth/me` 返回 HTTP 401，错误码为 `auth.token_missing`。
- `GET http://localhost:5173` 返回 HTTP 200。

浏览器验证：

- 使用本机 Chrome 打开 `http://localhost:5173`，首屏显示 `MetaHub 控制台登录`。
- 输入 seed 管理员后能进入控制台，个人菜单包含 `admin@metahub.local`。
- 控制台展示系统状态/本地服务状态。
- 点击登出后返回登录页。
- 页面无 JavaScript error 或资源 404。
