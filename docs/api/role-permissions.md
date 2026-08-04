# 细粒度角色权限矩阵 API

本文冻结 PRD-059 的租户级 Permission、RolePermission、认证权限快照和 PAGE-085 接口合同。
字段级权限、自定义角色、ABAC、跨租户超级管理员和数据面 Virtual Key 鉴权不在本版本范围内。

## 1. 安全边界

- 所有接口只接受 Bearer Session；`tenant_id`、操作者、角色和权限集合均从后端认证上下文恢复。
- 客户端不得提交 `tenant_id`、用户 ID、角色 code、矩阵 hash、审计 ID 或 Webhook Endpoint。
- 每次受保护请求都从数据库重新读取当前用户角色、角色 revision 和 RolePermission，旧 Token 不缓存授权。
- 后端以 permission code 执行最终权限校验；前端导航、按钮和 checkbox 只改善交互，不能替代后端守卫。
- 矩阵缺失、重复、越界、跨租户、hash 不一致或目录版本损坏时整体失败关闭，不回退角色名硬编码。
- Provider Key、Virtual Key、Token、Authorization、Cookie、Prompt、Response、Secret、DLP pattern
  和 Webhook 正文不得进入 DTO、AuditLog 摘要、事件 payload 或普通日志。
- 所有成功和失败响应遵循统一错误合同并携带 `request_id`、`trace_id`；响应 Header 与 Body 保持一致。

## 2. 固定角色目录

| code | 展示职责 | 矩阵约束 |
| --- | --- | --- |
| `admin` | 租户最高管理员 | 拥有 48 项权限，角色和矩阵均不可修改 |
| `developer` | 接入、Key、调试和调用排障 | 不包含正式全租户 Billing 权限 |
| `viewer` | 基础概览和公开控制台信息 | 只读且不能进入受限治理页面 |
| `finance_admin` | 用量、账本、预算、价格和成本异常 | 不可获得 `provider.write` |
| `security_admin` | 审计、DLP、策略和安全治理 | 不可获得 `model_price.write` |
| `ops_admin` | Provider、环境、项目、模型和路由运维 | 不可获得正式 Billing 权限 |

六个角色由产品模板固定，不提供创建、删除或改名 API。除 admin 外，租户管理员只能在对应
`grantable_permission_codes` 上限内撤销或恢复权限，不能突破职责分离边界。

## 3. Permission 目录

目录 revision 为 `1`，共 48 个稳定 code，按业务域覆盖：

- 控制台和租户：`console.basic.read`、`dashboard.read`、`tenant.settings.read`、`user.manage`、
  `rbac.matrix.manage`。
- 环境和项目：`environment.read/write`、`project.read/write`。
- Key、Provider 和模型：`virtual_key.read/write`、`provider.read/write`、
  `provider.health.read/probe`、`model.read/write`、`model_price.read/write`。
- 路由和策略：`routing.read/write`、`model_quality.read/write`、`policy.read/write/publish/simulate`。
- 日志和反馈：`request_log.read`、`request_explanation.read`、`feedback.use`、
  `debug_console.use`。
- Billing：`billing.usage.read/export`、`billing.finops.read/write`、
  `billing.budget.read/write`、`billing.anomaly.read/write`。
- 安全和集成：`audit.read/export`、`dlp.read/write`、`provider_error_map.read/write`、
  `webhook.manage`。
- 系统与开发者入口：`developer_portal.read`、`system.config.read`。

公开响应同时返回 `category`、中文名称、说明、`risk_level`、`catalog_revision` 和 `status`。
客户端只能展示这些字段，不能用本地目录补齐后端没有返回的 Permission。

## 4. GET /permissions

读取当前认证租户的完整权限目录、六角色和 125 条默认授权关系投影。

### 4.1 权限

需要 `rbac.matrix.manage`。匿名返回 401；已登录但没有权限返回 403。角色名为 admin 但权限快照缺失
时仍返回 403，不能按名称补权。

### 4.2 请求

```http
GET /permissions HTTP/1.1
Authorization: Bearer <session-token>
X-Request-ID: req_example_matrix_read
```

接口没有查询参数。未知参数不能改变租户、角色或目录范围。

### 4.3 成功响应

```json
{
  "catalog_revision": 1,
  "permissions": [
    {
      "permission_id": "perm_example",
      "tenant_id": "tenant_example",
      "code": "provider.read",
      "category": "provider",
      "name": "读取 Provider",
      "description": "读取当前租户 Provider 与安全摘要。",
      "risk_level": "read",
      "catalog_revision": 1,
      "status": "active"
    }
  ],
  "roles": [
    {
      "role_id": "role_example",
      "tenant_id": "tenant_example",
      "code": "ops_admin",
      "name": "OpsAdmin",
      "description": "租户运维管理员",
      "status": "active",
      "permission_revision": 1,
      "matrix_sha256": "<64-lowercase-hex>",
      "immutable": false,
      "permission_codes": ["provider.read", "provider.write"],
      "grantable_permission_codes": ["provider.read", "provider.write"]
    }
  ],
  "request_id": "req_example_matrix_read",
  "trace_id": "trc_example_matrix_read"
}
```

真实响应必须恰有 48 个 Permission、6 个 Role；每个 Role 的 `matrix_sha256` 必须由规范排序后的
`permission_codes` 计算。任一 tenant、目录、关系、revision 或 hash 不一致都不返回部分矩阵。

## 5. PATCH /roles/{role_id}/permissions

以完整集合和 revision CAS 替换当前租户内一个非 admin 固定角色的授权。

### 5.1 权限与资源隐藏

需要 `rbac.matrix.manage`。`role_id` 不存在和属于其他租户均返回同形 404，不能借状态、消息或耗时
枚举另一租户角色。admin 角色返回 `rbac.role_immutable`，不能修改恢复入口。

### 5.2 严格请求合同

```http
PATCH /roles/role_example/permissions HTTP/1.1
Authorization: Bearer <session-token>
Content-Type: application/json; charset=utf-8
X-Request-ID: req_example_matrix_patch

{
  "expected_revision": 1,
  "permission_codes": ["provider.read", "provider.write"]
}
```

- 正文最大 32 KiB，只接受严格 UTF-8 JSON object。
- 字段必须恰为 `expected_revision` 和 `permission_codes`；拒绝重复 key、未知字段、float、指数、
  非有限数、重复 permission、非字符串项、过深或过宽 JSON。
- `expected_revision` 必须为正整数；permission code 必须来自当前目录且位于目标角色产品上限内。
- 权限依赖先于正文读取。匿名或失权调用即使携带畸形正文，也稳定返回 401/403，不形成 wire oracle。

### 5.3 成功响应

```json
{
  "role": {
    "role_id": "role_example",
    "tenant_id": "tenant_example",
    "code": "ops_admin",
    "permission_revision": 2,
    "matrix_sha256": "<64-lowercase-hex>",
    "immutable": false,
    "permission_codes": ["provider.read", "provider.write"],
    "grantable_permission_codes": ["provider.read", "provider.write"]
  },
  "changed": true,
  "event_id": "rpevt_<32-lowercase-hex>",
  "request_id": "req_example_matrix_patch",
  "trace_id": "trc_example_matrix_patch"
}
```

请求集合与当前集合相同时返回 `changed=false`、`event_id=null`，revision、AuditLog 和 source event
均不变化。真实变更使用 `BEGIN IMMEDIATE`、集合差异和 revision CAS；stale revision 返回 409，
不能覆盖并发管理员结果。

## 6. 事务、审计与事件

真实变更在同一数据库事务内完成：

1. 删除和新增目标 RolePermission 差异。
2. 以旧 revision/hash 为 CAS 条件更新 Role 的新 revision/hash。
3. 写入 `role.permission_changed` AuditLog，摘要只包含角色、revision、hash 和权限数量。
4. 写入不可变 `role_permission_events` source，payload 固定九字段：schema version、role ID/code、
   before/after revision、before/after hash、added/removed permission codes。
5. 事务提交后才尝试 Webhook 投递；网络失败不回滚已提交矩阵，source 只记录固定
   `webhook.delivery_unavailable`。

矩阵、审计或 source 任一步失败都整体回滚。事件、AuditLog、用户和角色均用
`(tenant_id, id)` 复合 owner 关系，不能拼接另一租户全局 ID。

## 7. 统一错误码

| HTTP | code | 场景 |
| ---: | --- | --- |
| 400 | `rbac.payload_invalid` | wire、JSON、字段、类型或集合结构非法 |
| 401 | `auth.token_missing` / `auth.token_invalid` | 缺少或无效 Session |
| 403 | `auth.permission_denied` | 当前数据库权限快照不包含管理权限 |
| 404 | `rbac.role_not_found` | 本租户不存在或跨租户角色 |
| 409 | `rbac.role_immutable` | 尝试修改 admin |
| 409 | `rbac.revision_conflict` | expected revision 已陈旧 |
| 422 | `rbac.permission_not_grantable` | permission 不在目标角色产品上限内 |
| 500 | `rbac.matrix_integrity_failed` | 目录、关系、hash 或 tenant 投影损坏 |
| 503 | `rbac.unavailable` | 数据库或权限服务暂不可用 |

公开错误不得附带 SQL、数据库路径、另一租户 ID、原始 JSON、Webhook URL、签名或底层异常文本。

## 8. Console 合同

- 后端导航只有在 `rbac.matrix.manage` 存在时返回 PAGE-085 `role-permissions`。
- 页面只消费 GET DTO 的 `immutable`、`permission_codes` 和 `grantable_permission_codes`。
- 1280x720 使用六角色紧凑矩阵；390x844 使用当前角色 Tab 和单列 Permission 列表。
- dirty 草稿禁止切换其他角色；放弃草稿恢复服务端集合；保存携带冻结 revision 和稳定 request ID。
- 401 清理 Session；403 清除 PAGE-085 owner/DOM；409 保留草稿并要求读取新 revision。
- 身份、tenant、Session、permission revision、authorization fingerprint 或导航变化都会销毁旧草稿。
- 矩阵、权限 code、role ID、hash 和 request ID 不进入 URL 或持久 Storage。

## 9. 数据库迁移

`0050_fine_grained_role_permissions.sql` 提供六角色目录、48 Permission、RolePermission、
角色 revision/hash、租户初始化、不可变 trigger、AuditLog 扩展和 source event。

down 只允许没有新角色用户、矩阵变更、权限事件/Audit、目标 Webhook 订阅/投递和未知后续依赖的
默认投影回退。任何真实业务事实都在第一项持久 DDL 前以 `prd059_down_blocked` 原子拒绝；0049 的
AuditLog、稳定导出序列、AuditExport、source、rowid、NULL、索引和 trigger 必须完整恢复。
