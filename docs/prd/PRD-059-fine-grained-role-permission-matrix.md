# PRD-059 - 细粒度角色权限矩阵

状态：**已完成**。设计初稿、两轮编码前自审、两轮范围纠偏复审、正式实现、0050 开发库迁移、
专项与直接受影响回归、三服务 API、Microsoft Edge 真实 Browser 双视口和停服静止终态均已通过。
该状态只代表 PRD-059 完成；全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。

## 1. 需求理解

### 1.1 权威来源与开发顺序

- Excel `01_PRD总表` 与 `02_M0-M1核心开发` 第 60 行一致定义：
  `PRD-059 | M1-企业POC | P1 | 核心层 | RBAC增强 | 细粒度角色权限矩阵`。
- 用户故事：企业管理员希望财务、安全、开发者看到不同功能。
- 实现范围：增加 FinanceAdmin、SecurityAdmin、OpsAdmin；权限矩阵表驱动；控制台与后端一致。
- 明确不做：字段级权限。
- 页面、接口、对象与事件分别是 `PAGE-085 角色权限页`、`GET /permissions`、
  `PATCH /roles/{id}/permissions`、`Permission/RolePermission` 和 `role.permission_changed`。
- `TC-0117` 要求 Finance 不能修改 Provider、Security 不能修改价格、Developer 不能读取全租户账单。
- `TC-0118` 要求越权、异常输入和依赖故障返回统一错误码，不泄漏敏感信息且状态可追踪。
- PRD-058 已完整完成。按里程碑优先级和 Excel 行顺序，PRD-059 是下一条未完成项，不是仅凭
  编号推断。

### 1.2 架构约束

- 控制面服务管理员、开发者、财务、安全和运维角色；权限判断必须位于后端，前端裁剪只改善体验。
- 所有业务域表携带 `tenant_id`，或由强复合关系追溯到 tenant；任何管理查询都必须租户限定。
- OpenRouter、硅基流动仍是竞品和迁移兼容对象，不是默认模型厂商；本 PRD 不改变 Provider 边界。
- 默认部署和数据留存位于中国境内；权限目录、审计、事件和矩阵事实不新增出境链路。
- API 继续返回稳定英文错误码、`request_id` 和 `trace_id`。
- Provider Key、Virtual Key、Token、Prompt、Response、Authorization、Cookie、Secret、DLP pattern
  和 Webhook 正文不得进入权限 DTO、审计摘要、事件或普通日志。
- 强权限与审计链路失败关闭；权限矩阵不可用时不能回退到前端角色判断或历史硬编码角色集合。

### 1.3 现有能力与缺口

PRD-008 已提供 `admin/developer/viewer`、租户内 `roles` 目录、用户角色分配和后端角色依赖。其后
各 PRD 又增加了 30 余个 `require_*` 依赖，但授权仍由 Python 角色元组和 Console 角色元组决定。

当前真实缺口：

1. `RoleCode`、`roles.code` CHECK 和 `audit_logs.actor_role` CHECK 只接受三个旧角色；直接插入新角色
   会在认证或审计链路失败。
2. 多个路由直接使用 `require_current_admin`，Provider、模型、Alias 等资源没有独立权限点；只扩展
   角色元组会继续形成散落硬编码，不能称为表驱动矩阵。
3. `GET /usage/summary` 与 `/usage/records` 当前允许 developer，返回全租户用量和成本；这与
   “Developer 不能看全租户账单”冲突。
4. Console 导航和多个编辑面板直接判断 `role === "admin"`。即使后端扩展角色，新角色仍会看到
   错误入口或无法操作获授权功能。
5. 现有 `roles` 只是展示目录，没有 Permission、RolePermission、版本、CAS、审计或
   `role.permission_changed` 可追踪事件。
6. 权限变更后已有 Session 虽会重新读取数据库中的用户角色，但当前 AuthContext 不加载权限集合，
   长生命周期页面也没有权限 revision owner，可能短暂保留过期 DOM 或发起错误预取。
7. PRD-058 的 AuditLog 稳定序列、复合 FK 和不可变导出链依赖当前 `audit_logs` 结构；0050 若简单
   重建角色 CHECK，可能破坏导出水位、rowid 保真或下游 trigger。

### 1.4 实现范围

- 新增规范角色代码 `finance_admin`、`security_admin`、`ops_admin`；API 展示名分别为
  FinanceAdmin、SecurityAdmin、OpsAdmin，保留三个旧角色行为兼容。
- 建立租户内 Permission/RolePermission、角色矩阵 revision、产品级角色授权上限模板和默认矩阵。
- 认证时从当前数据库状态加载有效权限；所有业务路由通过稳定 permission code 做后端授权。
- 新增矩阵查询和完整替换 API，提供 revision CAS、不可变 admin 保护、同事务审计及
  `role.permission_changed` 可追踪事件；网络投递复用现有 Webhook delivery，不新建第二套 source worker。
- 将 Console 导航、数据预取、编辑控件和 PAGE-085 改为消费后端权限集合和 revision。
- 重建 0050 所需的角色与 AuditLog 合同，保留 0049 的历史事实、稳定序列、复合关系和不可变门禁。
- 补齐错误码、i18n、迁移、主流程、异常、权限、跨租户、并发、隐私、Runtime 和 Browser 验收。

### 1.5 明确不做

- 不做字段级权限、数据脱敏列策略、项目成员关系、资源实例级 ACL 或任意 ABAC 表达式。
- 不做自定义角色创建/删除；六个角色是本版本固定角色目录。
- 不做跨租户平台超级管理员、代理登录、组织树、部门继承、SCIM、LDAP、SSO 或审批流。
- 不允许通过矩阵给命名角色突破职责分离上限；本版本 PATCH 只能在产品定义的角色上限内撤销或恢复。
- 不改 Gateway 的 Virtual Key 鉴权模型，不把控制面用户权限带入数据面热路径。
- 不做分布式权限缓存；控制面低频 API 每次认证读取当前矩阵，优先保证即时失败关闭。

## 2. 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| 认证与授权 | `control-plane-api/src/metahub_control/auth/` | 六角色、AuthContext 权限快照、通用 permission 守卫、旧依赖到 permission code 映射 |
| 权限矩阵服务 | `control-plane-api/src/metahub_control/permissions/` | models、严格 wire、service、routes 和提交后 Webhook 投递 |
| 既有业务路由 | `control-plane-api/src/metahub_control/*/routes.py` | 把直接 admin/角色元组依赖替换为资源读写权限依赖 |
| 用户管理 | `control-plane-api/src/metahub_control/users/` | 六角色目录、排序、分配与当前矩阵摘要兼容 |
| Console 导航 | `control-plane-api/src/metahub_control/console/` | PAGE-085、permission-based 导航、authorization revision |
| 审计与 Webhook | `audit/`、`webhooks/`、Shared runtime | `role.permission_changed` 白名单、最小 payload、恢复投递 |
| 数据库 | `infra/migrations/0050_*.sql/.down.sql` | 角色、权限、矩阵、事件、owner/CAS/不可变 trigger 与 AuditLog 保真重建 |
| 种子与租户初始化 | `shared-sdk/python/metahub_shared/db/seed.py` | 六角色、租户权限目录和默认矩阵幂等写入 |
| Console 前端 | `console-web/src/lib/permissions.js`、`features/role-permissions/`、`app/main.js`、相关面板 | PAGE-085、权限解析、导航/预取/编辑能力收敛 |
| 测试与验收 | Python、Node、`scripts/acceptance/`、`docs/testing/` | 主流程、异常、权限、租户、迁移、Runtime、双视口 Browser |

## 3. 角色与权限模型

### 3.1 角色代码

| code | 展示名 | 核心职责 |
| --- | --- | --- |
| `admin` | Admin | 租户最高管理员；完整权限且矩阵不可修改，保证恢复入口 |
| `developer` | Developer | 接入、Key、调试、请求日志；不能读正式全租户账单 |
| `viewer` | Viewer | 基础概览和公开控制台信息，不写治理配置 |
| `finance_admin` | FinanceAdmin | 用量、账本、预算、价格和成本异常；不能改 Provider |
| `security_admin` | SecurityAdmin | 审计、DLP、策略、错误归一和安全事件；不能改价格 |
| `ops_admin` | OpsAdmin | Provider、环境、项目、Key、模型和路由运维；不能读正式账单 |

数据库和 API 只接受小写 snake_case code。Excel 的 CamelCase 名称只用于展示，避免 Windows、SQLite
和 JSON 客户端出现大小写双义。

### 3.2 Permission catalog v1

| permission code | 含义 | 默认角色及可授权上限 |
| --- | --- | --- |
| `console.basic.read` | 概览、API 文档、竞品、系统状态、语言入口 | 全部六角色 |
| `dashboard.read` | 租户聚合概览；不是正式账本或可导出明细 | 全部六角色 |
| `tenant.settings.read` | 租户配置摘要 | admin |
| `user.manage` | 用户列表、创建和角色分配 | admin |
| `rbac.matrix.manage` | PAGE-085、矩阵 GET/PATCH | admin |
| `environment.read` / `environment.write` | Environment 读/写 | read: admin/developer/ops；write: admin/ops |
| `project.read` / `project.write` | Project/Application 读/写 | read: admin/developer/ops；write: admin/ops |
| `virtual_key.read` / `virtual_key.write` | Virtual Key 读/写 | admin/developer/ops |
| `provider.read` / `provider.write` | Provider/KeyPool 读/写 | admin/ops |
| `provider.health.read` / `provider.health.probe` | 健康读取/主动探测 | admin/developer/ops |
| `provider_error_map.read` | 错误归一规则读取 | admin/developer/security/ops |
| `provider_error_map.write` | 错误归一规则写入 | admin/security |
| `model.read` | 模型目录读取 | admin/developer/finance/security/ops |
| `model.write` | 模型目录写入 | admin/ops |
| `model_price.read` | 价格时间线读取 | admin/developer/finance/security/ops |
| `model_price.write` | 价格计划创建 | admin/finance；security 上限中不存在 |
| `routing.read` / `routing.write` | Alias、Fallback、Routing、CacheAffinity | read: admin/developer/ops；write: admin/ops |
| `model_quality.read` / `model_quality.write` | 模型评分读/写 | read: admin/developer/ops；write: admin/ops |
| `policy.read` / `policy.write` | 策略读取/编辑 | admin/security |
| `policy.simulate` / `policy.publish` | Dry-Run 与快照发布/回滚 | admin/security |
| `dlp.read` / `dlp.write` | DLP 规则读/写 | admin/security |
| `request_log.read` | Trace、SavedFilter 和日志详情 | admin/developer/security/ops |
| `request_explanation.read` | 请求解释中心 | admin/developer/security/ops |
| `feedback.use` | 反馈提交与汇总 | admin/developer |
| `billing.usage.read` | 全租户 Usage 汇总/明细 | admin/finance；developer 上限中不存在 |
| `billing.usage.export` | 同步/异步用量导出 | admin/finance |
| `billing.finops.read` / `billing.finops.write` | 正式账本读取/调整 | admin/finance |
| `billing.budget.read` / `billing.budget.write` | 预算读取/写入 | admin/finance |
| `billing.anomaly.read` / `billing.anomaly.write` | 成本异常读取/处置 | admin/finance/security |
| `audit.read` / `audit.export` | 审计读取/不可变导出 | admin/security |
| `webhook.manage` | Endpoint、死信、重放与归档 | admin/developer/security/ops |
| `developer_portal.read` | 快速开始、SDK 与迁移指南 | admin/developer |
| `debug_console.use` | 内置 API 调试台 | admin/developer |
| `system.config.read` | 脱敏运行配置摘要 | admin/ops |

admin 的默认权限为 catalog 全集且不可 PATCH。其余角色 v1 的默认集合等于各自授权上限；管理员可以
撤销并恢复上限内权限，但不能给 FinanceAdmin 增加 `provider.write`、给 SecurityAdmin 增加
`model_price.write`、给 Developer 增加任何 `billing.*`。后续扩大上限必须通过新迁移和 catalog
revision，不允许租户 PATCH 绕过职责分离。

### 3.3 权限判定

1. Bearer Token 只定位 Session；用户 role、角色状态、matrix revision 和有效 permission codes 每次
   请求都从当前租户数据库读取，不相信 Token、Header、query 或前端状态。
2. AuthContext 新增不可变 `permissions` 和 `permission_revision`；未知角色、disabled 角色、缺失目录、
   跨租户映射、重复 permission 或 catalog 不完整均失败关闭。
3. 业务依赖调用 `require_permission(context, "stable.code")`；前端角色名不参与后端授权。
4. `require_current_admin` 仅保留给真正的 `user.manage/rbac.matrix.manage` 兼容入口；Provider、价格、
   审计等路由必须改用各自权限依赖，禁止继续把“admin”当作所有领域权限的代称。
5. service-token 内部快照接口、Gateway Virtual Key 和公开 health/version 不进入用户矩阵。

## 4. 数据表与字段设计

### 4.1 产品模板表

`rbac_permission_templates` 和 `rbac_role_permission_templates` 是产品随迁移发布的系统元数据，不是
租户业务数据，因此不携带 tenant_id；租户可见的 Permission/RolePermission 仍全部携带 tenant_id。
模板表只允许迁移写入，数据库 trigger 禁止运行期 UPDATE/DELETE/REPLACE。

`rbac_permission_templates`：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `permission_code` | PK，严格小写枚举 | catalog v1 稳定 code |
| `category` | 非空 | Console 分组 |
| `name` / `description` | 非空有界 | 中文展示，不含动态租户信息 |
| `risk_level` | `read/write/critical` | PAGE-085 风险标记 |
| `catalog_revision` | `1` | 未来迁移升级 |

`rbac_role_permission_templates`：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `role_code` + `permission_code` | 复合 PK/FK | 角色授权上限和默认集合 |
| `default_granted` | 固定 `1` | v1 默认等于上限 |

### 4.2 roles 保真重建

0050 重建现有 `roles`，保留 `role_id/tenant_id/code/name/description/status/created_at/updated_at`，把
code CHECK 扩展到六角色，并新增：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `permission_revision` | integer >= 1 | 每次真实矩阵变更单调 +1 |
| `matrix_sha256` | 64 位小写 hex | 规范化 permission code 列表哈希 |
| `is_permission_immutable` | 0/1 | admin 固定为 1，其余为 0 |

保留 `UNIQUE(tenant_id, code)`，并增加 `UNIQUE(tenant_id, role_id)` 供复合 FK 使用。用户 INSERT 或
UPDATE role 时由 tenant-qualified trigger 验证目标 role active；不重建 incoming FK 众多的 users 表。

矩阵哈希的字节级合同固定为：唯一 permission code 按 ASCII 升序形成 JSON 数组，使用
`ensure_ascii=True`、无空白分隔符 `(',', ':')` 编码为 UTF-8，再计算 SHA-256。0050 使用预计算字面量
初始化六角色；运行期由 Python 同一算法复核。SQLite 没有内置 SHA-256，不在 trigger 中伪造派生计算。

### 4.3 permissions

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `permission_id` | PK | 稳定租户内 ID |
| `tenant_id` | 非空，复合 FK/索引 | 所属租户 |
| `code` | 非空 | 对应模板 code |
| `category/name/description/risk_level` | 非空 | 模板投影，不允许租户任意文本 |
| `catalog_revision` | integer | 当前 catalog 版本 |
| `status` | `active` | v1 不提供禁用 API |
| `created_at/updated_at` | UTC | 初始化时间 |

约束：`UNIQUE(tenant_id, code)`、`UNIQUE(tenant_id, permission_id)`；owner trigger 保证字段与产品模板
完全一致，并禁止 UPDATE/DELETE/REPLACE/负 rowid 绕过。

### 4.4 role_permissions

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `role_permission_id` | PK | 稳定关联 ID |
| `tenant_id` | 非空 | 租户边界 |
| `role_id` / `permission_id` | tenant 复合 FK | 角色与权限 |
| `created_at/updated_at` | UTC | 授予与最近恢复时间 |

一行表示 granted；`UNIQUE(tenant_id, role_id, permission_id)`。INSERT 必须同时命中产品角色上限模板，
admin 的行禁止任何运行期增删，其他角色只允许权限服务在事务内做集合差异。跨租户关系、未知模板、
REPLACE、重复/负 rowid 全部由 FK 和 trigger 拒绝。

### 4.5 role_permission_events

该表保存一次真实矩阵变更的不可变事实和 Webhook 投递状态，不承担另一套通用 outbox：

| 字段组 | 字段 |
| --- | --- |
| 身份 | `event_id, tenant_id, role_id, actor_user_id, actor_role` |
| CAS | `request_id, before_revision, after_revision` |
| 可复核 | `before_sha256, after_sha256, added_permissions_json, removed_permissions_json` |
| 审计绑定 | `audit_log_id, trace_id, payload_json` |
| 投递状态 | `delivery_status, last_error_code` |
| 时间 | `created_at, updated_at, dispatched_at` |

- role、actor、AuditLog 和 permission code 均用 tenant 复合关系或严格 payload decoder 绑定。
- 业务字段及 event ID 不可变；只允许投递状态从 `pending` 转为 `dispatched` 或 `failed`，用于排障，
  不承诺本 PRD 范围外的网络 exactly-once。
- payload 只含 role ID/code、前后 revision/hash、added/removed code 和 schema version，不含用户邮箱、
  tenant 名称、矩阵页面状态或凭据。

### 4.6 新租户初始化

0050 先回填所有已有 tenant，再安装 `AFTER INSERT ON tenants` trigger：从不可变产品模板按 tenant
生成六个 roles、Permission 和默认 RolePermission。这样后续测试租户或未来租户不会因缺失矩阵而
回退硬编码。seed 仍执行相同的幂等完整性复核，而不是盲目覆盖租户已撤销的权限。

### 4.7 AuditLog 保真与 down guard

- 0050 重建 `audit_logs.actor_role` 为六角色，并增加 action `role.permission_changed`、object type
  `role_permission`。
- 必须逐列复制显式 rowid、NULL、时间、JSON 和全部历史 action；迁移前后执行双向 EXCEPT、count、
  min/max rowid 和 SHA-256 证据。
- 必须恢复 PRD-058 的稳定 sequence trigger、`audit_log_export_sequences`、AuditExport 复合延迟 FK、
  owner/不可变 trigger 和既有后续依赖，不得用删除序列或重编号完成迁移。
- down 在任何用户已分配新角色、存在矩阵变更事件/AuditLog/Webhook 事实、存在新 actor_role、订阅或
  未知后续 view/trigger/FK 时，于第一项持久 DDL 前原子拒绝。只有纯默认矩阵且无业务事实时才能删除
  新角色投影并恢复 0049 合同。

## 5. API 设计

### 5.1 通用合同

- Bearer Session 必填；两个矩阵 API 均只允许具有 `rbac.matrix.manage` 的 admin。
- tenant、actor、role 和 permission revision 只从 Session/数据库恢复；不接受 tenant Header/query。
- 所有成功与错误响应同时返回 Header 和 Body 一致的 `request_id/trace_id`。
- 匿名或无权限调用必须在读取/解析 PATCH 正文前返回 401/403，避免通过 wire 差异探测 permission code。

### 5.2 GET /permissions

无 query 参数。返回当前租户完整安全目录：

```json
{
  "catalog_revision": 1,
  "permissions": [
    {
      "permission_id": "perm_xxx",
      "tenant_id": "tenant_xxx",
      "code": "provider.write",
      "category": "provider",
      "name": "修改 Provider",
      "description": "创建、更新和维护当前租户 Provider 与 KeyPool。",
      "risk_level": "critical",
      "status": "active"
    }
  ],
  "roles": [
    {
      "role_id": "role_xxx",
      "tenant_id": "tenant_xxx",
      "code": "finance_admin",
      "name": "FinanceAdmin",
      "permission_revision": 1,
      "matrix_sha256": "<64位小写SHA-256>",
      "immutable": false,
      "permission_codes": ["billing.finops.read"],
      "grantable_permission_codes": ["billing.finops.read"]
    }
  ],
  "request_id": "req_xxx",
  "trace_id": "trc_xxx"
}
```

排序固定为角色产品顺序、权限 category/code；损坏、重复、越界或跨租户矩阵整包失败，不返回部分目录。

### 5.3 PATCH /roles/{role_id}/permissions

完整替换一个非 admin 固定角色的 granted 集合：

```json
{
  "expected_revision": 3,
  "permission_codes": [
    "audit.read",
    "audit.export",
    "dlp.read",
    "dlp.write"
  ]
}
```

规则：

- 正文最大 32 KiB、只允许两个字段、permission 不超过 catalog 总数；code 必须唯一、规范小写并排序。
- `role_id` 必须属于当前 tenant；跨租户和不存在同形 404。
- admin role 永久不可 PATCH；请求 security/finance/developer 上限外权限返回统一 422。
- `expected_revision` 必须等于当前 revision；真实变更执行 CAS +1。相同集合的新 request ID 是无副作用
  no-op，不写假审计或事件。
- 事务顺序：完整性校验 -> 冻结 before -> 计算 diff/hash -> 更新 RolePermission -> CAS role revision ->
  写 `role.permission_changed` AuditLog -> 写不可变 event -> commit。任一持久步骤失败整体回滚。
- 网络 Webhook 只在 commit 后复用现有 delivery 服务尝试投递，并把终态写回 event；接收端失败不能回滚
  已提交矩阵和审计事实，失败状态仍可按 event/delivery/request/trace ID 追踪。

响应返回更新后的 role matrix、`changed`、稳定 `event_id`（no-op 为 null）及 request/trace ID。

### 5.4 登录与导航兼容

- Login 和 `GET /auth/me` 的安全用户摘要增加 `permission_codes`、`permission_revision` 和
  `authorization_fingerprint`；不返回其他角色矩阵。
- `GET /console/navigation` 按当前 AuthContext permissions 生成入口，并返回同一 authorization
  fingerprint。现有 `required_roles` 字段保留兼容，但由当前租户矩阵反算；新增
  `required_permissions` 作为权威字段。
- 角色或矩阵变更不要求重发 Token：下一次后端请求立即读取新矩阵并失败关闭。

## 6. 后端路由映射

所有现有业务入口必须登记一项 permission code。重点映射：

| 既有依赖/入口 | 新权限 |
| --- | --- |
| Provider 列表/详情 | `provider.read` |
| Provider/KeyPool 创建、更新、测试 | `provider.write` |
| 模型价格 GET / POST | `model_price.read` / `model_price.write` |
| Usage summary/records / export | `billing.usage.read` / `billing.usage.export` |
| FinOps GET / adjustment | `billing.finops.read` / `billing.finops.write` |
| Budget GET / write | `billing.budget.read` / `billing.budget.write` |
| Audit GET / export | `audit.read` / `audit.export` |
| DLP/Policy/ErrorMap | 对应 `.read/.write/.simulate/.publish` |
| Project/Environment/Model/Alias/Key | 对应资源 `.read/.write` |
| RequestLog/Explain | `request_log.read` / `request_explanation.read` |
| Webhook Endpoint/DeadLetter | `webhook.manage` |
| User API / Permission API | `user.manage` / `rbac.matrix.manage` |

本 PRD 把现有领域守卫统一改为稳定 permission code，并直接覆盖 Provider、价格、Usage、FinOps、Audit、
Budget、DLP、Policy、用户和矩阵 API。全 OpenAPI 自动注册表属于后续平台化质量门禁，不扩大到本 PRD。

## 7. PAGE-085 前端设计

### 7.1 页面布局

- PAGE-085 位于“租户治理”组，只对后端导航授予 `rbac.matrix.manage` 的 admin 可见。
- 桌面端使用紧凑矩阵：权限行为行、固定角色列、分组筛选和风险标识；admin 列显示锁定图标。
- 移动端改为角色 Tab + 权限复选列表，避免六角色矩阵横向滚动；最长 code 和 request ID 可换行。
- 每个复选框的 disabled 状态来自 `grantable_permission_codes`，不能靠前端硬编码 Finance/Security
  边界。熟悉图标使用现有图标库并提供 tooltip，不新增说明性营销卡片。
- 保存是明确命令，只有草稿与服务器矩阵不同且 revision 未过期时启用。

### 7.2 状态与生命周期

- PAGE-085 冻结 tenant、user、session、authorization fingerprint、target role 和 role revision；保存使用
  `expected_revision`，409 时保留草稿并要求重新加载。
- 复用主应用既有 auth/navigation 周期刷新；有未保存草稿时只提示服务器版本变化，不静默覆盖。
- 离页、注销、401/403 或 authorization fingerprint 改变时清除 PAGE-085 草稿和响应。
- 页面和其他业务面板的预取、编辑按钮统一消费后端 permissions；Finance 不产生 Provider 写请求，
  Security 不产生价格写请求，Developer 不产生 Usage/FinOps/导出请求。

### 7.3 授权刷新

主应用复用现有纯读 navigation/auth refresh。发现 authorization fingerprint 变化时清除 PAGE-085 及
已失权页面状态，再按新导航重渲染；刷新绝不调用 PATCH 或产生审计事件。后端权限在下一请求立即生效，
即使前端尚未刷新，过期按钮请求也会被后端拒绝。

## 8. 错误码设计

| 错误码 | HTTP | 可重试 | 场景 |
| --- | ---: | --- | --- |
| `auth.token_missing` / `auth.token_invalid` | 401 | 否 | 匿名、失效 Session |
| `auth.permission_denied` | 403 | 否 | 当前矩阵没有所需 permission |
| `rbac.role_not_found` | 404 | 否 | 角色不存在或属于另一租户，同形防枚举 |
| `rbac.payload_invalid` | 422 | 否 | 正文超限、字段/类型/code/重复非法 |
| `rbac.permission_not_grantable` | 422 | 否 | 请求超出角色产品授权上限 |
| `rbac.role_immutable` | 409 | 否 | 尝试修改 admin 矩阵 |
| `rbac.revision_conflict` | 409 | 否 | expected revision 过期 |
| `rbac.matrix_integrity_failed` | 500 | 否 | catalog、hash、复合关系或存量矩阵损坏 |
| `rbac.unavailable` | 503 | 是 | 数据库或矩阵事务基础设施暂不可用 |

错误正文只返回稳定 code/message/suggestion/diagnosis/request/trace ID。SQL、数据库路径、完整矩阵、
其他角色权限、异常 repr 和敏感值不得进入错误或普通日志。

## 9. 安全、租户与可靠性约束

- tenant/actor/role/permission 只从认证 Session 和 tenant-qualified 数据库关系恢复。
- 权限检查发生在任何业务 query/path/body 解析和数据库资源探测之前；未知输入不能成为权限 oracle。
- Permission、RolePermission、Event、AuditLog、Webhook subscription/delivery 均有 tenant 复合 owner
  约束；跨租户 role_id 或 permission_id 不得通过全局主键命中。
- 管理矩阵的 `admin` 权限不可移除；最后 active admin 和管理员自降级保护继续生效。
- 角色上限是后端和数据库共同约束，不能通过客户端篡改 `grantable_permission_codes` 绕过。
- 每次业务授权读取当前矩阵；数据库不可用或矩阵不完整时返回稳定 503/500，不回落旧角色元组。
- Webhook 只能在矩阵、AuditLog 和 event 同事务提交后调用；失败只写固定错误码，不记录 endpoint 正文。
- 日志只记录 tenant ID、actor ID、role ID、permission code diff、revision、hash 和 request/trace ID；
  不记录 Session Token、Provider/Virtual Key、Prompt/Response 或权限页面完整 DOM。
- 所有数据库连接在成功、权限拒绝、解析失败、取消和异常路径关闭；Windows 专项用重命名/删除证明。

## 10. 测试用例

### 10.1 主流程 TC-0117

1. 0050 后每个已有/新建 tenant 恰有六个 active role、完整 Permission 和默认 RolePermission。
2. admin GET `/permissions` 获得固定 catalog、六角色、稳定排序/revision/hash；不含密码、Token 或 Key。
3. admin 在 SecurityAdmin 上限内撤销/恢复权限，revision 单调、hash 可复算，并产生同事务 AuditLog、
   不可变 event 和正确 HMAC Webhook。
4. admin 通过现有用户 API 分配三个新角色；新用户登录后 `/auth/me`、后端导航和实际 API 一致。
5. FinanceAdmin 可读取 Usage/FinOps、管理预算和价格，但 Provider PATCH/POST/test/key 为 403，零写入。
6. SecurityAdmin 可读/导出 Audit、管理 DLP/Policy，但模型价格 POST 为 403，零写入。
7. OpsAdmin 可管理 Provider/环境/项目/模型/路由，但 Billing Usage/FinOps/导出为 403。
8. Developer 可使用 Key、日志、解释和调试台，但 Usage/FinOps/导出为 403，不能读取全租户账单。
9. Viewer 只保留基础导航；手写 hash/直接 API 均无受限 DOM、预取或数据。
10. admin 旧角色完整回归，不因表驱动改造丢失既有管理能力。

### 10.2 异常、权限和安全 TC-0118

1. 匿名/非 admin 对 GET/PATCH 返回 401/403；畸形 JSON、超限正文或非法 code 不改变优先错误。
2. 跨租户 role ID、permission ID 和同形 matrix hash 返回 404/整包失败，不泄漏另一租户存在性。
3. 修改 admin 返回 409；Finance 请求 provider.write、Security 请求 model_price.write、Developer 请求
   billing.* 返回 422，数据库 revision/矩阵/Audit/Event 均无变化。
4. stale revision 返回 409；两个并发 PATCH 只有一个 CAS 成功，失败者不留下部分 RolePermission。
5. no-op 请求不增加 revision，不生成假 Audit/Event；矩阵、CAS、Audit 或 event 任一点失败整体回滚。
6. 提交后的 Webhook 失败不回滚矩阵，但 event/delivery 记录失败固定错误码且不泄漏 Endpoint 正文。
7. catalog 缺失、重复 permission、越界 grant、错 tenant FK、hash 不一致时认证/GET/PATCH失败关闭。
8. 权限变更后旧 Token 下一次请求立即按新矩阵失败关闭，不依赖重新登录或前端角色判断。
9. API/事件/Audit/日志/正式证据扫描 Provider Key、Virtual Key、Token、Prompt、Response、Cookie、
   Secret、DLP pattern 动态 canary 命中 0。
10. Windows 成功/错误/取消后专项数据库可立即重命名和删除，无残留句柄。

### 10.3 迁移与兼容

1. fresh 0001 -> 0050、0049 -> 0050、0050 down/up；migration count/head/checksum 连续。
2. roles、audit_logs 的 rowid/NULL/全列双向 EXCEPT 保真；PRD-058 sequence 映射一一对应。
3. AuditLog/AuditExport/sequence/source 的 owner、不可变、REPLACE、负 rowid 和 down guard 继续通过。
4. users 新角色 trigger、RolePermission 复合 FK、角色上限、admin 和 event 不可变直接测试。
5. 有新角色用户、matrix event/audit/webhook 或未知后续依赖时 down 在第一项 DDL 前原子拒绝。
6. 旧 PRD 冻结迁移测试更新 `LATER_MIGRATIONS`，不能把 0050 错判为旧 PRD 自身栈顶。

### 10.4 Console 与 Browser

1. PAGE-085 桌面矩阵和移动角色 Tab；grantable/locked/revision/dirty/no-op/409/503 状态正确。
2. 既有周期刷新只有 GET；装载、timer、重渲染、身份变化均不产生 PATCH。
3. 身份或 fingerprint 变化清除草稿；迟到成功/失败不能覆盖新 Session 或降权页面。
4. 六角色在 1280x720、390x844 实际 Chromium（Microsoft Edge）中导航、DOM、预取和后端结果一致。
5. Finance Provider 写、Security 价格写、Developer Billing、Viewer 全部受限页面手写 hash 后零预取。
6. 最长 permission code、role ID、hash、request ID 无重叠和横向溢出；焦点、键盘和复选框标签完整。
7. Console/Page error、blocked request、HTTP 5xx 和敏感动态 canary 命中为 0。

### 10.5 回归与运行验收

- 先执行 PRD-059 Shared/Control/Console/0050 专项及受影响领域权限测试。
- 再执行受影响认证、用户、Provider、价格、Usage、FinOps、Budget、Audit、DLP、Policy、Webhook、
  Console 导航和既有 PRD-058 回归。
- Console 全量、lint、Python 语法/Ruff 致命规则和中文注释门禁；Node 示例未改动，不重复运行。
- 按用户最新约束不执行根级 Python 全量；只运行 PRD-059 专项和直接受影响回归，失败节点定向闭环。
- 隔离库、开发库在线备份与 0050 正式升级；真实三服务、六角色、第二租户、Webhook HMAC、
  双视口 Browser、停服后端口/Session/event/sidecar/spool/日志隐私静止终态。
- 所有 TEMP/TMP/basetemp/pycache/Runtime/Browser 证据只写 `D:/MetaHub/.tmp`，不提权、不使用
  `C:/tmp`；链接清理只删除获准链接节点并保留目标和证据。

## 11. 代码修改计划

1. 新增 0050 up/down，先完成隔离迁移专项和历史保真测试。
2. 新增共享 permission catalog、六角色常量、错误/i18n/Webhook 合同。
3. 扩展 AuthContext，在认证事务内严格加载当前角色、revision 和 permission set。
4. 实现 `permissions/` models、严格 wire、GET/PATCH service、审计、revision CAS 和提交后 Webhook。
5. 把现有领域守卫迁移到 permission dependency，并直接修复 TC-0117 涉及的 Provider、价格和账单入口。
6. 扩展用户角色分配、seed、新租户初始化和 Console permission-based navigation。
7. 实现 PAGE-085、矩阵/移动布局，并改造相关页面的预取和写按钮权限。
8. 补主流程、异常、权限、跨租户、迁移、并发、事件、隐私和 Browser 合同测试。
9. 更新 API 文档、README、目录说明和 `docs/testing/PRD-059-tests.md`。
10. 执行专项、受影响回归、开发库迁移、Runtime、Browser 和停服验收；全部通过后
    才能标记完成并从 Excel 识别下一项。

最终实现、测试和运行证据见 `docs/api/role-permissions.md` 和
`docs/testing/PRD-059-tests.md`。PRD-059 的设计、实现、测试、文档和运行验收门禁均已关闭。

## 12. 设计自审第一轮

状态：已完成。范围：需求边界、表驱动真实性、职责分离、迁移兼容、并发和可靠事件。

### 12.1 发现与修正

1. **阻断：只扩展 `RoleCode` 会被 `audit_logs.actor_role` 三角色 CHECK 拒绝。** 已把 AuditLog 保真
   重建纳入 0050，并明确恢复 PRD-058 sequence/FK/trigger，不能只改 Python 枚举。
2. **阻断：仅把现有角色元组加新角色不属于表驱动。** 已改为认证时加载 RolePermission，业务依赖
   只检查 permission code；范围纠偏后直接覆盖现有领域守卫和 TC-0117 入口，全 OpenAPI 注册表后移。
3. **阻断：可任意 PATCH 的矩阵会让 Finance 获得 Provider 写、Security 获得价格写。** 已增加
   不可变产品授权上限；v1 默认等于上限，租户只能撤销/恢复，职责分离不受配置绕过。
4. **阻断：Developer 当前能读取全租户 Usage。** 已明确迁移 `require_usage_reader` 到
   `billing.usage.read`，developer 默认和上限均不含任何 `billing.*`。
5. **阻断：admin 可编辑自身矩阵会锁死租户。** 已固定 admin catalog 全集、数据库不可变 trigger 和
   API 409；现有最后 admin、自降级保护继续保留。
6. **兼容：迁移后新建 tenant 若没有矩阵会迫使代码回落硬编码。** 已新增不可变产品模板和 tenant
   AFTER INSERT 初始化，认证对缺失矩阵始终失败关闭。
7. **可靠性：矩阵成功但无事件会失去追踪。** 已固定矩阵、AuditLog、不可变 event 同事务；范围纠偏后
   网络投递复用现有 Webhook delivery，并记录 `dispatched/failed`，不新增第二套 lease worker。
8. **并发：全量 PATCH 没有 revision 会最后写覆盖。** 已增加 expected revision、BEGIN IMMEDIATE、
   revision CAS 和规范矩阵 hash；no-op 不产生审计，网络重试仍由 expected revision 防止覆盖。
9. **前端：只裁剪导航仍会让已渲染面板短暂保留旧权限。** 已把 authorization fingerprint 加入
   既有 auth/navigation 周期刷新；失权时清理 PAGE-085 和受限页面，后端下一请求立即拒绝。
10. **范围：字段级隐藏会违反明确非目标。** Finance/Developer 若没有完整资源权限就拒绝整个
    endpoint，不在同一 DTO 内按角色删价格或账单字段。

第一轮结论：表驱动、职责分离、开发者账单、admin 恢复、租户初始化、迁移保真、可追踪事件、CAS 和
前端失权收敛的阻断风险已关闭，可以进入第二轮安全与兼容自审，仍禁止编码。

## 13. 设计自审第二轮

状态：已完成。范围：租户关系、认证时序、存量数据、no-op 语义、隐私、down 和 UI/API 一致性。

### 13.1 发现与修正

1. **认证：只在登录时加载权限会让 Matrix 变更在 Token 生命周期内不生效。** 已冻结为每次 Bearer
   认证从当前 DB 加载 role/revision/permissions；Token 不携带可授权角色集合。
2. **角色变更：用户已有 Session 不能继续相信旧前端角色。** 后端每请求读取 users.role；Console
   周期纯读刷新 fingerprint，403/401 同样触发清理，避免要求重发 Token 才生效。
3. **租户：单列 role_id/permission_id FK 不能证明关系属于同 tenant。** 所有 RolePermission、Event、
   Audit 关系改为 `(tenant_id, id)` 复合 FK/owner trigger；跨租户主键同形失败关闭。
4. **wire：FastAPI 自动 Pydantic 错误可能先于 RBAC 泄漏字段合同。** PATCH 在 admin dependency 后
   执行 32 KiB 有界严格 JSON 解析；匿名/无权限畸形正文仍稳定 401/403。
5. **no-op：相同集合若也写事件会制造虚假审计。** no-op 不增加 revision、不写 Audit/Event，返回
   当前矩阵并明确 `changed=false`。
6. **存量：AuditLog 重建只比较 count 会漏 rowid/NULL/JSON 漂移。** 已要求全列双向 EXCEPT、显式
   rowid、sequence 一一映射、完整性/FK 和所有 0049 trigger 复验。
7. **down：默认新角色行本身会让任何 down 永远失败。** down 可删除未被用户使用且从未变更的模板
   投影；只对新角色用户、matrix event/audit/webhook、新 actor role 和未知依赖做前置 guard。
8. **模板：permissions 虽由模板投影，仍可能被直接 SQL 篡改。** 模板和租户 Permission 均不可变，
   认证/GET/PATCH 复核 catalog revision、字段、集合和 hash，异常不回落静态允许。
9. **隐私：事件若携带完整矩阵会扩大 Webhook 泄漏面。** payload 只保留 added/removed code、role、
   revision 和 hash；不含用户邮箱、页面状态、tenant 名称或任何业务凭据。
10. **Console：用 role name 决定 checkbox 会与后端上限漂移。** locked/grantable、导航、预取和编辑
    能力全部来自后端 permission DTO；前端本地常量只允许做中文标签，不作为授权依据。
11. **资源：权限在认证阶段增加数据库查询，异常路径可能复制连接泄漏。** 权限加载复用认证连接和
    同一 deadline/busy_timeout，所有返回/异常 finally-close；专项在 Windows 验证句柄释放。
12. **回归：大量直接 `require_current_admin` 路由容易漏迁。** 范围纠偏后逐一改造现有领域守卫，
    Provider/Model/Alias 的直接 admin 路由显式拆读写，并用受影响路由专项阻断漏项。

第二轮结论：认证即时性、租户复合关系、先鉴权后 wire、no-op、0049 保真、可回退边界、模板
完整性、事件隐私、Console 权威来源和路由覆盖均已关闭；可以进入实现。

## 14. 范围纠偏决定

后端与迁移并行调查确认：Excel 的直接验收边界是六角色、表驱动矩阵、控制台/后端一致和 TC-0117/0118。
原设计追加的第二套 source lease、全 OpenAPI 注册表、全站 owner 重构和根级全量会显著扩大周期，并非本条
PRD 的必要条件。按用户“只测相关部分、全程沙箱内”的最新约束，本文件以上章节已收敛；0050 AuditLog
六角色保真、逐请求数据库权限、职责分离、PAGE-085、统一错误和实际运行验收仍全部保留。

## 15. 范围纠偏自审第一轮

状态：已完成。范围：Excel 可追溯性、最小企业闭环、迁移不可省略项和测试爆炸风险。

1. **保留：AuditLog 六角色重建。** 0049 的 `actor_role` 只接受旧三角色，若省略，OpsAdmin 的 Provider
   强审计事务会直接失败；因此 0050 必须在事务外关闭 FK、显式 rowid/全列保真并保留 PRD-058 子表。
2. **保留：逐请求矩阵加载。** Token 只定位 Session，旧 role claim 不参与授权；数据库缺失或损坏失败关闭。
3. **收敛：事件投递。** 矩阵、AuditLog、不可变 event 同事务；提交后复用现有 Webhook delivery 和 HMAC，
   不复制 audit export 的 lease worker。投递失败状态可追踪，但本 PRD 不声明网络 exactly-once。
4. **收敛：路由范围。** 统一改造现有领域守卫，并直接覆盖 TC-0117 的 Provider、价格、Usage/FinOps/导出；
   不引入全 OpenAPI 注册表平台工程。
5. **收敛：前端生命周期。** PAGE-085 和失权页面消费后端 permission/fingerprint，复用既有周期刷新；
   不把所有历史页面重写为新 owner 框架。
6. **收敛：测试。** 不重跑根级 Python 全量，不运行未受影响 Node 示例；专项、直接受影响回归、Console
   全量/lint、迁移、Runtime 和双视口 Browser 足以形成可复核证据。

第一轮结论：没有删除 Excel 验收项；四项平台化扩展已移出本 PRD，测试与实现范围恢复可控。

## 16. 范围纠偏自审第二轮

状态：已完成。范围：租户隔离、哈希兼容、0049 入向 FK、失败语义和隐私。

1. 0050 对所有存量 tenant 回填六角色和 48 个权限，并安装 `AFTER INSERT ON tenants` 初始化；seed 只做
   幂等目录校验，不恢复租户已撤销权限。
2. matrix hash 字节合同已冻结为排序 JSON 数组的紧凑 UTF-8 SHA-256；迁移用预计算值，Python 每请求复核。
3. `audit_log_export_sequences`、`audit_exports`、`audit_export_events` 原位保留；0050 只保真重建父表，
   结束前执行 FK、sequence 一一映射和双向差集检查。
4. PATCH 用 `BEGIN IMMEDIATE`、tenant-qualified 查询和 expected revision CAS；admin、越权上限、跨租户、
   no-op 和审计失败均有明确原子语义。
5. Webhook 失败不伪装成矩阵失败，也不记录 Endpoint 正文；event/delivery 只保存固定错误码和安全摘要。
6. Finance Provider 写、Security 价格写、Developer Billing 和 Viewer 受限入口同时在后端与前端验证，
   前端隐藏从不替代后端 403。

第二轮结论：范围收敛没有削弱租户、权限、错误、审计、隐私和运行验收边界，可以继续编码。

## 17. 编码门禁

当前门禁：**已关闭并通过**。最终实现遵守六角色、产品授权上限、permission-based 后端守卫、
每请求矩阵加载、复合 tenant 关系、admin 不可变、revision CAS、Audit/Event 同事务、0050 保真重建
和分层测试合同。

## 18. 实现后收口复核

### 18.1 第一轮：安全、权限与租户边界

状态：已完成。

1. 复核六角色授权上限、逐请求数据库权限快照和前后端 permission code 映射；角色名不能补权。
2. 复核 Permission、RolePermission、角色、用户、AuditLog 和权限事件的 tenant-qualified 关系；
   跨租户读取、写入和同形 ID 均由后端或数据库失败关闭。
3. 复核匿名、失权、职责越界、strict wire、stale CAS、no-op 和事务故障；统一错误码与
   request/trace ID 合同保持成立，敏感正文未进入 DTO、日志或 Browser 证据。
4. 复核 Admin 矩阵不可变、Finance Provider 写、Security 价格写和 Developer Billing 读取门禁；
   前端隐藏仅用于体验，最终授权仍由后端执行。

结论：未发现未关闭的安全、权限或租户隔离问题。

### 18.2 第二轮：兼容、运行与验收证据

状态：已完成。

1. 复核 0050 fresh/0049 历史/up-down-re-up、PRD-058 AuditExport trigger 优先级、开发库完整性和外键；
   既有 Audit/Export 事实与 rowid 合同未回退。
2. 复核专项、直接受影响 Console 回归、lint、中文注释门禁及三服务真实 API 结果；未执行用户明确
   排除的根级 Python 全量，也未把未安装的 Ruff 写成通过。
3. 使用本机 Microsoft Edge 和 Playwright 1.60 完成 PAGE-085 真实登录、6 角色/48 权限、Admin
   锁定、草稿放弃、隐私、注销及 1280x720/390x844 截图；页面、Console error 和外部请求均为 0。
4. 停止三服务后由 SQLite 自身执行 checkpoint；8000/8001/5173 监听、WAL/SHM、pending 权限事件
   均为 0，`integrity_check=ok` 且外键违规为 0。

结论：PRD-059 可运行、可测试、可验收，正式完成。下一项必须从 Excel 优先级重新识别；
`all_108_prds_complete=false`。
