# PRD-010 - Virtual Key 创建、鉴权与禁用设计

状态：已实现，自动化测试、真实 HTTP 和浏览器验收通过。

设计来源：Excel `PRD-010 - Virtual Key创建、鉴权与禁用`、页面清单 `PAGE-014/PAGE-015/PAGE-016`、API 清单 `POST /virtual-keys`、`GET /virtual-keys`、`PATCH /virtual-keys/{id}`、`DELETE /virtual-keys/{id}`、数据对象 `VirtualKey`、事件 `virtual_key.created` / `virtual_key.disabled`、验收用例 `TC-0019/TC-0020`。架构约束要求 Virtual Key 是数据面鉴权入口，绑定租户/项目/环境，明文不落库，数据面热路径轻量，所有管理 API 后端做租户与角色校验。

## 需求理解

本 PRD 的目标是让开发者和管理员在控制面生成平台 Virtual Key，用它调用 PRD-009 已实现的 OpenAI-compatible Gateway，而不是把模型厂家 Provider Key 暴露给业务系统。

核心范围：

- 控制面新增 Virtual Key 列表。
- 控制面新增 Virtual Key 创建能力，明文 Key 只在创建响应中展示一次。
- Virtual Key 只保存不可逆哈希、前缀和元数据，不保存明文。
- 支持备注、所属环境、启用/禁用、最近使用时间。
- 数据面 Bearer 鉴权继续使用 `virtual_keys.key_hash`，禁用 Key 不能调用。
- 数据面成功鉴权后更新 `last_used_at`。
- 管理操作写结构化事件日志：`virtual_key.created`、`virtual_key.disabled`。
- 控制台新增 Virtual Key 列表、创建入口和 Key 详情/禁用入口。

明确不做：

- M0 不做 Key 审批。
- M0 不做自动轮换。
- M0 不做项目模型完整体系；`project_id` 先预留，不强制引入项目表。
- M0 不做 Key 级限流、预算、额度。
- M0 不做完整 Webhook 投递；事件先进入结构化日志，正式订阅/投递由后续 Webhook PRD 完成。
- M0 不提供再次查看明文 Key 的能力。

## 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 数据库迁移 | `infra/migrations/` | 新增 `0004` 扩展 `virtual_keys` 元数据字段 |
| 控制面 API | `control-plane-api/src/metahub_control/virtual_keys/` | 新增模型、服务、路由 |
| 控制面权限 | `control-plane-api/src/metahub_control/auth/` | 复用 `require_virtual_key_writer`，必要时增加 reader 依赖 |
| 数据面鉴权 | `gateway-data-plane/src/metahub_gateway/auth/virtual_key.py` | 成功鉴权后更新 `last_used_at`，禁用 Key 保持拒绝 |
| 共享安全工具 | `shared-sdk/python/metahub_shared/db/security.py` | 复用 `hash_secret`、`secret_prefix`；新增 Key 生成 helper 可放业务服务内 |
| 错误码 | `shared-sdk/python/metahub_shared/runtime/errors.py` | 新增 Virtual Key 管理错误码 |
| 控制台 | `console-web/src/features/virtual-keys/`、`console-web/src/lib/virtualKeys.js` | 新增列表、创建表单、一次性明文展示、禁用入口 |
| 测试与文档 | `control-plane-api/tests/`、`gateway-data-plane/tests/`、`console-web/tests/`、`docs/testing/` | 覆盖主流程、禁用后鉴权失败、敏感值不泄露、权限 |

## 数据表/字段设计

当前 `virtual_keys` 已有字段：

| 字段 | 现状 | 使用方式 |
| --- | --- | --- |
| `virtual_key_id` | 主键 | 对外返回 id，写 request_logs |
| `tenant_id` | 已有 | 所有查询必须限制当前租户 |
| `name` | 已有 | 用户可编辑名称 |
| `key_prefix` | 已有 | 排障展示，不能用于鉴权 |
| `key_hash` | 已有唯一索引 | Bearer Token 哈希匹配 |
| `status` | `active/disabled` | 数据面只允许 active |
| `created_at` / `updated_at` | 已有 | 元数据展示 |

新增迁移 `0004_virtual_key_metadata.sql`：

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| `environment` | TEXT | NOT NULL DEFAULT `'dev'`，CHECK `dev/test/prod` | Key 所属环境 |
| `description` | TEXT | NULL | 备注，最长 240 字符 |
| `last_used_at` | TEXT | NULL | 数据面成功鉴权时更新 |
| `created_by` | TEXT | NULL，FK users 可后续补 | 创建人 user_id |
| `disabled_at` | TEXT | NULL | 禁用时间 |
| `disabled_by` | TEXT | NULL | 禁用操作人 user_id |

不新增明文字段。`key_hash` 继续使用 SHA-256，不可逆；`key_prefix` 只保留前 12 位用于识别。`DELETE /virtual-keys/{id}` 做软禁用，不物理删除，避免历史 `request_logs` 与 `usage_ledger` 失去可追溯对象。

M0 暂不建立 `project_id`，因为当前 schema 还没有 project/environment 表。后续项目 PRD 上线时再把 `virtual_keys.project_id` 纳入正式外键或映射。

## API 设计

所有接口位于 Control Plane API，返回统一 `request_id` / `trace_id`，错误走统一错误结构。

### 权限

- `admin`、`developer` 可以访问 Virtual Key 管理接口。
- `viewer` 全部拒绝，返回 `auth.permission_denied`。
- 后端使用 `require_virtual_key_writer` 或等价 `require_any_role(admin, developer)`，不能只靠控制台隐藏入口。
- 所有 SQL 必须带 `tenant_id = context.user.tenant_id`。

### POST /virtual-keys

请求：

```json
{
  "name": "local dev key",
  "environment": "dev",
  "description": "本地开发调试"
}
```

响应：

```json
{
  "virtual_key": {
    "virtual_key_id": "vk_...",
    "tenant_id": "tenant_...",
    "name": "local dev key",
    "key_prefix": "mhv_dev_xxx",
    "environment": "dev",
    "description": "本地开发调试",
    "status": "active",
    "last_used_at": null,
    "created_at": "2026-07-07T10:00:00Z",
    "updated_at": "2026-07-07T10:00:00Z"
  },
  "plain_value": "mhv_dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

规则：

- `plain_value` 只在创建响应返回一次，列表、详情、PATCH、DELETE 均不返回。
- Key 生成使用 `secrets.token_urlsafe`，格式建议 `mhv_<environment>_<random>`。
- 存库前计算 `key_hash = hash_secret(plain_value)`。
- 只保存 `key_prefix = secret_prefix(plain_value)`。
- 创建后写 `virtual_key.created` 结构化日志，payload 只包含 `key_prefix`、`environment`、`name`，不包含明文 Key。

### GET /virtual-keys

查询参数：

- `status=active|disabled` 可选。
- `environment=dev|test|prod` 可选。

响应：

```json
{
  "virtual_keys": [
    {
      "virtual_key_id": "vk_...",
      "tenant_id": "tenant_...",
      "name": "local dev key",
      "key_prefix": "mhv_dev_xxx",
      "environment": "dev",
      "description": "本地开发调试",
      "status": "active",
      "last_used_at": "2026-07-07T10:05:00Z",
      "created_at": "2026-07-07T10:00:00Z",
      "updated_at": "2026-07-07T10:00:00Z"
    }
  ],
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

响应不返回 `key_hash`、`plain_value` 或任何可用于重放调用的字段。

### PATCH /virtual-keys/{id}

请求：

```json
{
  "name": "prod app key",
  "environment": "prod",
  "description": "生产业务系统",
  "status": "disabled"
}
```

规则：

- 可更新 `name`、`environment`、`description`、`status`。
- `status` 仅允许 `active` 或 `disabled`。
- 从 `active` 变为 `disabled` 时写 `disabled_at`、`disabled_by` 和 `virtual_key.disabled`。
- 从 `disabled` 改回 `active` 属于 M0 手工恢复能力，清空 `disabled_at/disabled_by` 或保留历史均可；建议清空以表达当前状态。
- 目标 Key 必须属于当前租户，否则返回 `virtual_key.not_found`。

### DELETE /virtual-keys/{id}

语义：软禁用。

响应返回禁用后的 Virtual Key 元数据，不返回明文。

规则：

- 如果已 disabled，保持幂等返回当前对象。
- 不物理删除数据库行。
- 写 `virtual_key.disabled` 结构化日志。

### 数据面鉴权行为

PRD-009 已实现：

- 缺失 Bearer 返回 `gateway.auth_missing`。
- Key 不存在或非 active 返回 `gateway.auth_invalid`。
- 成功鉴权恢复 `tenant_id`、`virtual_key_id`。

PRD-010 增强：

- 成功鉴权后更新 `virtual_keys.last_used_at`。
- 禁用 Key 仍按 `gateway.auth_invalid` 返回，不暴露 Key 是否存在。
- 更新 `last_used_at` 失败时不泄露内部错误；M0 可记录结构化 warning，后续改异步 telemetry。

## 前端页面/交互设计

### Virtual Key 列表

控制台登录后新增“Virtual Key”面板：

- 展示名称、前缀、环境、状态、最近使用时间、创建时间。
- 提供环境和状态筛选。
- admin/developer 可见；viewer 不渲染入口。
- 不展示 `key_hash` 和明文 Key。

### 创建弹窗 / 表单

M0 可实现为面板内创建表单，后续再升级为弹窗：

- 输入名称。
- 选择环境 `dev/test/prod`。
- 输入备注。
- 创建成功后展示一次性 Key 明文。
- 一次性 Key 区域提供复制按钮。
- 关闭或刷新后不再显示明文，只显示前缀。
- 明文 Key 不写入 sessionStorage/localStorage。

### Key 详情

M0 详情可内联在列表行：

- 展示备注、状态、环境、最近使用时间。
- 支持禁用/启用。
- 禁用前显示确认文案，说明业务系统将无法继续调用 Gateway。

## 错误码设计

| 错误码 | HTTP | 场景 | 建议 |
| --- | --- | --- | --- |
| `virtual_key.not_found` | 404 | 当前租户找不到目标 Key | 检查 key id 是否属于当前租户 |
| `virtual_key.create_invalid` | 422 | 创建请求缺少名称或字段类型错误 | 检查 name/environment/description |
| `virtual_key.name_invalid` | 422 | 名称为空或过长 | 使用 1-80 字符名称 |
| `virtual_key.environment_invalid` | 422 | environment 非 `dev/test/prod` | 选择 dev、test 或 prod |
| `virtual_key.status_invalid` | 422 | status 非 `active/disabled` | 选择 active 或 disabled |
| `virtual_key.update_empty` | 422 | PATCH 没有可更新字段 | 至少传入一个可更新字段 |
| `auth.permission_denied` | 403 | viewer 或无权限用户访问 | 联系管理员授予 developer/admin |
| `gateway.auth_invalid` | 401 | 禁用 Key 调用数据面 | 创建或启用新的 Virtual Key |

## 安全与权限约束

- Virtual Key 明文只在创建响应返回一次。
- 明文 Key 不写数据库、不写日志、不写 request_logs、不写 usage_ledger、不写控制台 storage。
- `key_hash` 不返回给前端。
- `key_prefix` 只能用于识别和排障，不能用于鉴权。
- 所有 Virtual Key 管理接口必须后端校验 admin/developer。
- 所有查询和更新必须限制 `tenant_id`。
- 跨租户 key id 返回 `virtual_key.not_found`，不泄露其他租户对象是否存在。
- 禁用 Key 后数据面调用返回 `gateway.auth_invalid`，不返回“已禁用”细节，避免 Key 枚举。
- 创建、禁用事件 payload 不含明文 Key。
- 并发鉴权只更新 `last_used_at`，不改变 Key 的安全属性；禁用与调用并发时以数据库当前 `status` 为准。

## 测试用例

### 主流程

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0019-1 | admin/developer 创建 Key | 返回 `plain_value` 一次，DB 只保存 hash/prefix |
| TC-0019-2 | 列表读取 | 返回前缀、环境、状态、最近使用时间，不返回明文/hash |
| TC-0019-3 | 新 Key 调用 Gateway | `/v1/chat/completions` 成功，request_logs 关联新 `virtual_key_id` |
| TC-0019-4 | 最近使用时间 | 成功鉴权后 `last_used_at` 更新 |
| TC-0019-5 | 禁用 Key | `DELETE` 或 `PATCH status=disabled` 后列表显示 disabled |
| TC-0019-6 | 禁用后调用 | 数据面返回 `gateway.auth_invalid` |
| TC-0019-7 | 控制台创建 | 创建成功后展示一次性明文 Key，刷新后不再出现 |

### 异常/权限/安全

| 用例 | 覆盖 | 预期 |
| --- | --- | --- |
| TC-0020-1 | viewer 调用 Virtual Key API | 返回 `auth.permission_denied` |
| TC-0020-2 | 非法 environment | 返回 `virtual_key.environment_invalid` |
| TC-0020-3 | 非法 status | 返回 `virtual_key.status_invalid` |
| TC-0020-4 | 空 PATCH | 返回 `virtual_key.update_empty` |
| TC-0020-5 | 跨租户 key id | 返回 `virtual_key.not_found` |
| TC-0020-6 | 敏感值泄露扫描 | 响应、日志、数据库不包含明文 Key |
| TC-0020-7 | 并发鉴权 | 多个请求同时用同一 Key 调用不报错，`last_used_at` 有值 |
| TC-0020-8 | 已禁用 Key 重复禁用 | 幂等返回 disabled，不新建明文 Key |

## 代码修改计划

已按以下顺序完成编码：

1. 新增迁移 `0004_virtual_key_metadata.sql` 和 down 脚本，扩展 `virtual_keys` 元数据字段。
2. 更新 seed 逻辑，为 seed Key 写入默认 `environment='dev'`。
3. 扩展错误码注册表，新增 `virtual_key.*` 管理错误码。
4. 新增控制面 `virtual_keys` 包：Pydantic models、service、routes。
5. 在 `control-plane-api/src/metahub_control/app.py` 注册 `/virtual-keys` router。
6. 复用 `require_virtual_key_writer`，保证 admin/developer 可访问，viewer 后端拒绝。
7. 更新数据面 `authenticate_virtual_key`，成功鉴权后写 `last_used_at`。
8. 新增后端集成测试：创建/列表/禁用/启用、权限拒绝、禁用后 Gateway 拒绝、敏感值不泄露、并发鉴权。
9. 控制台新增 `virtualKeys.js`、Virtual Key 面板、一次性明文展示和禁用交互。
10. 新增前端 Node test：列表渲染、创建响应展示一次、禁用按钮、viewer 不渲染、API 错误展示。
11. 更新 README、目录边界和 `docs/testing/PRD-010-tests.md`。
12. 运行 `scripts/ci/check.ps1`，重启本地服务，真实 HTTP + 浏览器验证：创建 Key、用新 Key 调用 Gateway、禁用后调用失败。

## 实现摘要

后端实现：

- 新增迁移 `0004_virtual_key_metadata.sql`，扩展 `virtual_keys.environment`、`description`、`last_used_at`、`created_by`、`disabled_at`、`disabled_by`。
- seed 默认 Virtual Key 写入 `environment='dev'`。
- 控制面新增 `/virtual-keys` 管理接口：列表、创建、更新、软禁用。
- 创建接口使用安全随机数生成 `mhv_<environment>_<random>`，只返回一次 `plain_value`。
- 数据库只保存 `key_hash` 和 `key_prefix`，不保存明文 Key。
- 管理接口复用后端 `require_virtual_key_writer`，admin/developer 可访问，viewer 拒绝。
- 数据面成功鉴权后更新 `last_used_at`，禁用 Key 调用仍返回 `gateway.auth_invalid`。
- 创建与禁用写 `virtual_key.created` / `virtual_key.disabled` 结构化日志，payload 不含明文 Key。

前端实现：

- 控制台新增 Virtual Key 面板，admin/developer 可见，viewer 不渲染。
- 支持创建 Key、一次性明文展示、复制、隐藏、启用/禁用。
- 列表只展示名称、前缀、环境、状态、最近使用时间和备注。
- 一次性明文 Key 只保存在运行时内存状态，不写入 sessionStorage/localStorage。

## 初步验证记录

- 后端专项测试：`control-plane-api/tests/integration/test_virtual_keys.py` 覆盖创建、列表、哈希存储、Gateway 调用、`last_used_at`、禁用后拒绝、viewer 权限、非法字段和并发鉴权。
- 前端专项测试：`console-web/tests/unit/virtualKeys.test.mjs` 覆盖 API 客户端、Virtual Key 面板、一次性明文展示、developer 可见、viewer 不渲染。
- 当前专项验证通过：后端相关测试 `13 passed`，控制台 Node test `31 pass`。
- 全量 `scripts/ci/check.ps1` 通过：后端 pytest `47 passed`，控制台 Node test `31 pass`，console lint `ok (29 files)`。
- 真实 HTTP 验收通过：控制面创建临时 Key、列表隐藏明文和 `key_hash`、新 Key 调用 Gateway 成功、`last_used_at` 更新、禁用后调用返回 `gateway.auth_invalid`。
- 浏览器验收通过：控制台 seed admin 登录后展示 Virtual Key 面板；页面创建 Key 后展示一次性明文；禁用按钮可用且状态变为 disabled；浏览器控制台无 warn/error。
- 验收中发现并修复 CORS 缺口：共享 FastAPI CORS 已允许 `DELETE`，并新增预检测试覆盖控制台 DELETE 管理接口。
