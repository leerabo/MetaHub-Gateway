# PRD-029 - Project与Application模型

状态：已完成编码、测试与文档更新。

## 需求理解

PRD-029 属于 M0.5-W3/4、P0、核心层能力，目标是在当前企业级 AI Gateway 中建立“项目 Project”和“应用 Application”的业务归属层。团队负责人需要按项目区分不同业务系统的调用、预算和日志，管理员/开发者需要能创建项目、创建应用、把 Virtual Key 绑定到项目或应用，并在日志、用量、预算里按项目维度追踪。

Excel 约束：
- 主要用户：管理员、开发者。
- 需求范围：Project/Application 创建、绑定 Virtual Key、日志按项目过滤、预算归属。
- 明确不做：不做复杂部门组织树。
- 页面/入口：项目列表、应用列表。
- API/动作：`POST/GET/PATCH /projects`、`POST/GET/PATCH /applications`。
- 核心数据对象：`Project`、`Application`。
- 事件/Webhook：`project.created`。
- 验收标准：可创建项目并绑定 Key；日志和用量可按项目聚合。
- 依赖关系：Virtual Key、Usage。
- 测试重点：项目归属、过滤。

架构约束补充：
- OpenRouter 和硅基流动只作为对标/迁移兼容对象，不作为模型厂家建模。
- 默认上游仍是模型厂商或云端 OpenAI-compatible Endpoint，本 PRD 不改变 Provider/Model 路由主链路。
- MVP 面向中国境内企业，所有业务表默认必须带 `tenant_id` 或可通过强外键追溯到 `tenant_id`。
- 不引入硬件盒子、MCP 主链路、信创或复杂数据出境能力。

## 涉及模块

- `infra/migrations/`
  - 新增 `0020_projects_applications.sql` 与 down migration。
  - 建立 `projects`、`applications`、`project_events`，并扩展 `virtual_keys`、`request_logs`、`usage_ledger`、预算表 scope 字段。
- `control-plane-api/src/metahub_control/projects/`
  - 新增 Project/Application 的模型、路由和服务层。
  - 所有写接口从后端校验角色和 `tenant_id`，不依赖前端隐藏按钮。
- `control-plane-api/src/metahub_control/virtual_keys/`
  - 扩展 Virtual Key DTO 和 PATCH 请求，支持绑定 `project_id`、`application_id`。
  - 绑定应用时校验应用属于当前租户且应用的 `project_id` 与传入项目一致。
- `control-plane-api/src/metahub_control/request_logs/`
  - 扩展查询参数和响应 DTO，支持 `project_id`、`application_id` 过滤与展示。
- `control-plane-api/src/metahub_control/usage/`
  - 扩展 Usage 过滤、明细响应和聚合响应，增加按项目和应用聚合。
- `control-plane-api/src/metahub_control/budgets/`
  - 扩展预算 scope 到 `project`、`application`。
  - 预算状态、scope label 和今日花费读取需要识别项目/应用维度。
- `gateway-data-plane/src/metahub_gateway/auth/`
  - `GatewayAuthContext` 增加 `project_id`、`application_id`，从 Virtual Key 鉴权结果恢复归属。
- `gateway-data-plane/src/metahub_gateway/usage/`
  - `RequestLogContext` 增加 `project_id`、`application_id`。
  - `request_logs` 与 `usage_ledger` 写入归属快照，保证后续 Virtual Key 改绑不影响历史账本。
- `gateway-data-plane/src/metahub_gateway/budgets/`
  - 预算准入加载 tenant、project、application、virtual_key 适用规则。
  - 命中记录写入项目/应用归属，便于日志详情解释。
- `console-web/src/`
  - 新增项目与应用页面、API client、导航入口和测试。
  - Virtual Key 页面展示并可编辑项目/应用绑定。
- `docs/`
  - 更新 README、目录文档和 PRD 测试文档。

## 数据表/字段设计

### `projects`

用途：保存当前租户内的项目，用于预算、日志和用量聚合的一级业务归属。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_id` | TEXT PK | 稳定 ID，格式建议 `prj_...`。 |
| `tenant_id` | TEXT NOT NULL | 租户隔离字段，所有查询必须带当前租户。 |
| `project_code` | TEXT NOT NULL | 租户内唯一的短编码，用于控制台展示和外部对账。 |
| `name` | TEXT NOT NULL | 项目名称。 |
| `description` | TEXT | 项目说明。 |
| `owner_user_id` | TEXT | 项目负责人，可为空，M0.5 不做复杂组织树。 |
| `status` | TEXT NOT NULL | `active`、`disabled`、`archived`。 |
| `created_by` | TEXT | 创建人。 |
| `updated_by` | TEXT | 最近更新人。 |
| `created_at` | TEXT NOT NULL | UTC ISO 时间。 |
| `updated_at` | TEXT NOT NULL | UTC ISO 时间。 |

约束和索引：
- `UNIQUE(tenant_id, project_code)`。
- `CHECK(status IN ('active','disabled','archived'))`。
- `idx_projects_tenant_status(tenant_id, status, updated_at DESC)`。
- `idx_projects_owner(tenant_id, owner_user_id)`。

### `applications`

用途：保存项目下的业务应用。Application 是 Project 的二级归属，不独立跨项目存在。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `application_id` | TEXT PK | 稳定 ID，格式建议 `app_...`。 |
| `tenant_id` | TEXT NOT NULL | 租户隔离字段。 |
| `project_id` | TEXT NOT NULL | 所属项目。 |
| `application_code` | TEXT NOT NULL | 项目内唯一编码。 |
| `name` | TEXT NOT NULL | 应用名称。 |
| `description` | TEXT | 应用说明。 |
| `status` | TEXT NOT NULL | `active`、`disabled`、`archived`。 |
| `created_by` | TEXT | 创建人。 |
| `updated_by` | TEXT | 最近更新人。 |
| `created_at` | TEXT NOT NULL | UTC ISO 时间。 |
| `updated_at` | TEXT NOT NULL | UTC ISO 时间。 |

约束和索引：
- `UNIQUE(tenant_id, project_id, application_code)`。
- `FOREIGN KEY(project_id) REFERENCES projects(project_id)`。
- `CHECK(status IN ('active','disabled','archived'))`。
- `idx_applications_tenant_project(tenant_id, project_id, status)`。

### `project_events`

用途：记录 `project.created` 等项目域事件，M0.5 先落本地数据库，后续 Webhook 模块可消费。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `project_event_id` | TEXT PK | 事件 ID。 |
| `tenant_id` | TEXT NOT NULL | 租户隔离字段。 |
| `project_id` | TEXT | 关联项目。 |
| `application_id` | TEXT | 关联应用，可为空。 |
| `event_type` | TEXT NOT NULL | M0.5 至少支持 `project.created`、`application.created`、`virtual_key.bound`。 |
| `actor_user_id` | TEXT | 操作人。 |
| `request_id` | TEXT NOT NULL | 请求链路 ID。 |
| `trace_id` | TEXT | Trace ID。 |
| `payload_json` | TEXT NOT NULL | 脱敏事件摘要，不保存密钥或 Prompt。 |
| `created_at` | TEXT NOT NULL | UTC ISO 时间。 |

### 扩展 `virtual_keys`

新增字段：
- `project_id TEXT`：Virtual Key 绑定的项目。
- `application_id TEXT`：Virtual Key 绑定的应用，可为空。

约束：
- `project_id`、`application_id` 都必须属于同一 `tenant_id`。
- 如果设置 `application_id`，则必须同时设置 `project_id`，且应用所属项目必须等于该 `project_id`。
- SQLite 不能直接用跨表 CHECK 表达该约束，服务层和数据面鉴权查询必须重复校验。

索引：
- `idx_virtual_keys_project(tenant_id, project_id, status)`。
- `idx_virtual_keys_application(tenant_id, application_id, status)`。

### 扩展 `request_logs`

新增字段：
- `project_id TEXT`
- `application_id TEXT`

说明：
- 这是请求发生时的归属快照，来自鉴权后的 Virtual Key。
- 历史日志不随 Virtual Key 后续改绑而变动。

索引：
- `idx_request_logs_project_time(tenant_id, project_id, created_at DESC)`。
- `idx_request_logs_application_time(tenant_id, application_id, created_at DESC)`。

### 扩展 `usage_ledger`

新增字段：
- `project_id TEXT`
- `application_id TEXT`

说明：
- 用量账本必须冗余保存归属快照，用于成本聚合和预算计算。
- 历史账本不随项目/应用改名、禁用或 Virtual Key 改绑而改变归属 ID。

索引：
- `idx_usage_ledger_project_time(tenant_id, project_id, created_at DESC)`。
- `idx_usage_ledger_application_time(tenant_id, application_id, created_at DESC)`。

### 扩展预算表

需要把 `budget_rules.scope_type`、`budget_alert_events.scope_type`、`budget_evaluations.scope_type` 的允许值扩展为：
- `tenant`
- `project`
- `application`
- `virtual_key`

SQLite 旧表 CHECK 不能直接 `ALTER` 修改，migration 需要采用创建新表、拷贝数据、替换表名的方式，保留既有 tenant/virtual_key 预算记录。

预算花费读取规则：
- `tenant`：按 `tenant_id` 汇总。
- `project`：按 `usage_ledger.project_id` 汇总。
- `application`：按 `usage_ledger.application_id` 汇总。
- `virtual_key`：按 `usage_ledger.virtual_key_id` 汇总。

数据面预算适用规则：
- 每次请求加载当前租户下 active 的 `tenant` 规则。
- 如果鉴权上下文有 `project_id`，加载该项目的 active 规则。
- 如果有 `application_id`，加载该应用的 active 规则。
- 始终加载当前 `virtual_key_id` 的 active 规则。
- 多条规则命中时，只要任一规则要求 `block`，请求阻断；日志记录最严重命中。

## API设计

所有响应继续携带 `request_id`、`trace_id`，所有错误使用统一错误响应结构。

### `GET /projects`

查询当前租户项目列表。

Query：
- `status=active|disabled|archived|all`，默认 `active`。
- `search`，可选，匹配 `project_code` 或 `name`，长度上限 160。

Response：
```json
{
  "projects": [
    {
      "project_id": "prj_xxx",
      "tenant_id": "tenant_default",
      "project_code": "crm",
      "name": "CRM系统",
      "description": "客户关系管理系统",
      "owner_user_id": "usr_xxx",
      "status": "active",
      "application_count": 2,
      "virtual_key_count": 3,
      "created_at": "2026-07-11T00:00:00Z",
      "updated_at": "2026-07-11T00:00:00Z"
    }
  ],
  "request_id": "...",
  "trace_id": "..."
}
```

### `POST /projects`

创建项目。

Request：
```json
{
  "project_code": "crm",
  "name": "CRM系统",
  "description": "客户关系管理系统",
  "owner_user_id": "usr_xxx"
}
```

权限：`admin` 可写；`developer` 在 M0.5 只读，避免开发者自行创建预算归属边界。

行为：
- 校验编码、名称和负责人属于当前租户。
- 写入 `projects`。
- 写入 `project_events`，事件类型 `project.created`。
- 日志只记录项目元数据，不记录密钥或请求正文。

### `PATCH /projects/{project_id}`

更新项目名称、说明、负责人或状态。

Request：
```json
{
  "name": "CRM系统",
  "description": "生产CRM",
  "owner_user_id": "usr_xxx",
  "status": "active"
}
```

约束：
- 不支持修改 `project_code`，避免对账标识漂移。
- 禁用或归档项目不级联禁用 Virtual Key；数据面仍按 Key 状态决定是否允许调用，项目状态用于控制台治理和新绑定校验。若后续要“项目禁用即阻断调用”，应作为独立策略 PRD。

### `GET /applications`

查询应用列表。

Query：
- `project_id`，可选。
- `status=active|disabled|archived|all`，默认 `active`。
- `search`，可选。

### `POST /applications`

创建应用。

Request：
```json
{
  "project_id": "prj_xxx",
  "application_code": "crm-web",
  "name": "CRM Web",
  "description": "CRM 前台应用"
}
```

权限：`admin` 可写；`developer` 只读。

行为：
- 校验项目属于当前租户且不是 `archived`。
- 写入 `applications`。
- 写入 `project_events`，事件类型 `application.created`。

### `PATCH /applications/{application_id}`

更新应用名称、说明、状态或迁移项目。

Request：
```json
{
  "project_id": "prj_xxx",
  "name": "CRM Web",
  "description": "前台服务",
  "status": "active"
}
```

约束：
- 不支持修改 `application_code`。
- 迁移项目时必须校验目标项目属于当前租户且不是 `archived`。
- 若已有 Virtual Key 绑定该应用，迁移项目时同步校正这些 Key 的 `project_id`，并记录 `virtual_key.bound` 事件摘要；否则会破坏“应用所属项目与 Key 项目一致”的不变量。

### 扩展 `PATCH /virtual-keys/{virtual_key_id}`

新增请求字段：
```json
{
  "project_id": "prj_xxx",
  "application_id": "app_xxx"
}
```

语义：
- 字段缺失表示不修改。
- `project_id: null` 表示清除项目绑定，同时必须清除应用绑定。
- `application_id: null` 表示仅清除应用绑定，保留项目绑定。
- 设置 `application_id` 时必须设置或已有 `project_id`，且应用所属项目匹配。

响应 `VirtualKeyInfo` 新增：
- `project_id`
- `project_name`
- `application_id`
- `application_name`

### 扩展 `GET /request-logs`

新增 query：
- `project_id`
- `application_id`

过滤语义：
- 永远附加当前 `tenant_id`。
- 同时传入项目和应用时，应用必须属于项目，否则返回 `request_log.query_invalid`。
- 响应 `RequestLogInfo` 返回 `project_id`、`project_name`、`application_id`、`application_name`。

### 扩展 `GET /usage/summary` 与 `GET /usage/records`

新增 query：
- `project_id`
- `application_id`

`UsageSummaryResponse` 新增：
- `by_project`
- `by_application`

`UsageRecordInfo` 新增：
- `project_id`
- `project_name`
- `application_id`
- `application_name`

### 扩展预算 API

`PATCH /budgets/rules` 的 `scope_type` 支持 `project`、`application`。

`GET /budgets/rules` 可用 `scope_type=project|application` 过滤。

`GET /budgets/status` 新增 query：
- `project_id`
- `application_id`
- 保留 `virtual_key_id`

校验：
- `project_id` 必须属于当前租户。
- `application_id` 必须属于当前租户；若同时传 `project_id`，应用必须属于该项目。

## 前端页面/交互设计

新增控制台入口：`项目与应用`。

页面结构：
- 顶部工具条：搜索框、状态筛选、创建项目按钮。
- 左侧或上半区：项目列表，展示项目编码、名称、状态、负责人、应用数、绑定 Key 数、今日成本摘要。
- 右侧或下半区：选中项目的应用列表，展示应用编码、名称、状态、绑定 Key 数、今日成本摘要。
- 详情抽屉或表单：创建/编辑项目、创建/编辑应用。
- Virtual Key 绑定区域：在 Virtual Key 页面增加项目和应用下拉选择；在项目详情页展示已绑定 Key 的脱敏前缀。

交互约束：
- `developer` 角色可查看项目、应用、日志、用量过滤，但看不到创建/编辑按钮；后端仍必须拒绝写接口。
- `admin` 角色可创建、编辑、绑定 Key。
- 应用下拉依赖项目选择；未选择项目时应用下拉禁用。
- 绑定 Key 时只显示 Key 名称、环境和 `key_prefix`，不显示明文 Key 或 hash。
- 页面文案只描述对象和状态，不写使用教程式说明。
- UI 保持控制台工具风格，信息密度高、用于扫描和治理，不做营销式 Hero 页面。

## 错误码设计

| 错误码 | 场景 | HTTP |
| --- | --- | --- |
| `project.name_invalid` | 项目名称为空或超过上限 | 400 |
| `project.code_invalid` | 项目编码为空、过长或包含非法字符 | 400 |
| `project.code_conflict` | 当前租户项目编码重复 | 409 |
| `project.not_found` | 项目不存在或不属于当前租户 | 404 |
| `project.status_invalid` | 项目状态非法 | 400 |
| `project.update_empty` | PATCH 没有有效字段 | 400 |
| `project.owner_invalid` | 负责人不存在、不属于当前租户或已禁用 | 400 |
| `application.name_invalid` | 应用名称为空或超过上限 | 400 |
| `application.code_invalid` | 应用编码为空、过长或包含非法字符 | 400 |
| `application.code_conflict` | 同一项目下应用编码重复 | 409 |
| `application.not_found` | 应用不存在或不属于当前租户 | 404 |
| `application.project_mismatch` | 应用不属于传入项目 | 400 |
| `application.status_invalid` | 应用状态非法 | 400 |
| `application.update_empty` | PATCH 没有有效字段 | 400 |
| `virtual_key.project_binding_invalid` | Key 绑定项目/应用组合非法 | 400 |
| `budget.scope_not_found` | 项目/应用预算 scope 不存在 | 404 |

所有错误响应仍由统一中间件填充：
- `status`
- `code`
- `message`
- `request_id`
- `suggestion`

## 安全与权限约束

- 所有 Project/Application 表和事件表必须写入 `tenant_id`。
- 查询必须从认证上下文取得 `tenant_id`，禁止前端传入或覆盖。
- `admin` 可创建、更新项目/应用和绑定 Virtual Key；`developer` 可读；`viewer` 默认无项目/应用管理入口。
- 控制台隐藏按钮只是体验优化，后端依赖必须做角色校验。
- Virtual Key 绑定响应和事件只允许返回 `key_prefix`，不返回明文 Key、`key_hash` 或 Authorization。
- 数据面从 `virtual_keys` 鉴权结果恢复 `project_id`、`application_id`，不信任请求体里的项目参数。
- 日志与用量只保存项目/应用 ID 和展示名，不保存 Prompt、Response、Provider Key。
- 预算阻断必须失败关闭：预算规则读取或评估失败时仍返回统一错误，不能静默放行。
- Project/Application 禁用不自动删除历史日志和用量，保证审计追溯完整。

## 测试用例

后端集成测试：
1. 主流程：admin 创建 Project，返回 `project_id`、`request_id`，并写入 `project.created`。
2. 主流程：admin 创建 Application，应用归属到指定 Project。
3. 主流程：admin PATCH Virtual Key 绑定 Project/Application，响应只包含脱敏 Key 信息。
4. 主流程：按 `project_id` 过滤 request logs，只返回该项目日志。
5. 主流程：按 `project_id`、`application_id` 查询 usage summary，返回 `by_project`、`by_application` 聚合。
6. 主流程：创建 `project` 或 `application` scope 的预算规则，预算状态按对应 usage 归属统计。
7. 数据面：Virtual Key 鉴权后 `GatewayAuthContext` 带出项目/应用，成功请求写入 `request_logs` 和 `usage_ledger`。
8. 数据面：项目预算达到阻断阈值时，请求失败并在失败 request log 中保存预算命中摘要。

异常与权限测试：
1. developer 调用 `POST /projects` 返回权限错误，不能只靠前端隐藏按钮。
2. viewer 调用项目/应用读接口返回权限错误或认证角色错误。
3. 创建重复 `project_code` 返回 `project.code_conflict`。
4. 创建应用时 `project_id` 属于其他租户，返回 `project.not_found`。
5. Key 绑定应用与项目不匹配，返回 `application.project_mismatch` 或 `virtual_key.project_binding_invalid`。
6. request logs 同时传入不匹配的项目/应用，返回 `request_log.query_invalid`。
7. usage 查询传入不存在项目，返回 `project.not_found`。
8. 预算 scope 为不存在项目/应用，返回 `budget.scope_not_found`。

前端测试：
1. 项目与应用页面能渲染项目列表、应用列表和空状态。
2. admin 可见创建/编辑入口，developer 不可见写按钮。
3. Virtual Key 编辑表单选择项目后加载应用列表，提交 payload 包含正确归属 ID。
4. 用量和日志页面项目/应用筛选参数正确传给 API。

迁移测试：
1. `0020` up migration 能在完整 M0.5 schema 上执行。
2. 旧 Virtual Key、旧日志、旧 usage 在新增字段为空时仍可查询。
3. down migration 能移除新增表和新增字段语义，或在 SQLite 限制下重建表保留旧字段。

注释门禁：
1. 新增 Python、JS、SQL 文件必须包含中文模块职责说明。
2. 新增类、DTO、函数、路由处理函数必须有中文说明。
3. 项目/应用绑定、权限判断、预算阻断和租户过滤等关键分支必须有中文注释。
4. 编码完成后运行 `python scripts/ci/check-comments.py`。

## 代码修改计划

1. 新增 `infra/migrations/0020_projects_applications.sql` 与 `.down.sql`。
2. 新增 `control-plane-api/src/metahub_control/projects/models.py`、`service.py`、`routes.py`、`__init__.py`。
3. 在 `control-plane-api/src/metahub_control/app.py` 注册项目/应用路由。
4. 在 `auth/dependencies.py` 增加 `require_project_reader`、`require_project_admin`。
5. 扩展 Virtual Key 模型、服务和路由，支持项目/应用绑定与响应展示。
6. 扩展 request logs 模型、服务和路由，支持项目/应用过滤与展示。
7. 扩展 usage 模型、服务和路由，支持项目/应用过滤、明细展示和聚合。
8. 扩展 control-plane budgets 模型和服务，支持 project/application scope。
9. 扩展 gateway auth、request log context、usage ledger 写入，保存归属快照。
10. 扩展 gateway budget guard，加载并评估 project/application scope。
11. 新增 `console-web/src/lib/projects.js`、`features/projects/renderProjectApplicationPanel.js`，更新 shell/nav、Virtual Key、日志、用量和预算页面。
12. 新增后端集成测试 `test_projects_applications.py`，并扩展 Virtual Key、request logs、usage、budgets、gateway 相关测试。
13. 新增或更新前端 Node 测试，覆盖项目/应用页面和筛选参数。
14. 更新 README、`docs/development/directory-map.md`、`docs/testing/PRD-029-tests.md`。
15. 运行 focused 测试、全量后端 pytest、前端测试、前端 lint 和中文注释门禁。

## 两轮设计自审结论

第一轮自审：Project/Application 绑定采用 Virtual Key 上的 `project_id`、`application_id` 字段，而不是多对多绑定表。原因是当前调用链路以 Virtual Key 鉴权为唯一入口，一个 Key 同时归属多个应用会让日志、用量和预算归因产生歧义。M0.5 先保持“一把 Key 对一个项目/应用归属”的可解释模型，后续若客户需要共享 Key，再单独设计显式调用方标识或动态归属 header。

第二轮自审：预算 scope 同时支持 `project` 和 `application`，但不引入部门树。架构文档提到预算维度包含项目，本 PRD Excel 明确 Project/Application 都是核心数据对象；如果只做 Project 预算，应用级调用成本仍无法独立治理。M0.5 的应用只是项目下二级对象，不扩展复杂组织关系，符合“不做复杂部门组织树”的边界。

## 实现记录

本 PRD 已按设计落地：

- 新增 `projects`、`applications`、`project_events`，并在 `virtual_keys`、`request_logs`、`usage_ledger` 保存项目/应用归属快照。
- 控制面新增 `/projects`、`/applications`，并扩展 Virtual Key、Request Logs、Usage、Budgets 支持 `project_id` 与 `application_id`。
- 数据面鉴权后从 Virtual Key 恢复 `project_id`、`application_id`，写入 request log、usage ledger，并在预算准入中加载 tenant、project、application、virtual_key 规则。
- 控制台新增“项目与应用”导航页，Virtual Key 创建表单支持绑定 Project/Application，调用日志和用量页面支持项目/应用筛选，预算表单支持项目/应用 scope。
- 所有新增源码已补中文模块职责、函数说明和关键权限/错误/归属分支注释，并通过 `scripts/ci/check-comments.py`。

已执行验证：

```powershell
npm test
npm run lint
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src'
python -m pytest control-plane-api\tests\integration\test_projects_applications.py control-plane-api\tests\integration\test_db_migrations.py control-plane-api\tests\integration\test_console_navigation.py -q --basetemp '.tmp\pytest-prd029-final' -p no:cacheprovider
python -m pytest control-plane-api\tests -q --basetemp '.tmp\pytest-control-prd029' -p no:cacheprovider
python -m pytest gateway-data-plane\tests -q --basetemp '.tmp\pytest-gateway-prd029' -p no:cacheprovider
python scripts\ci\check-comments.py
```

结果：

- 前端 Node 单测：99 passed。
- 前端 lint：`console lint ok (72 files)`。
- PRD-029 后端 focused 集成：9 passed。
- 控制面全量 pytest：通过。
- 数据面全量 pytest：通过。
- 中文注释门禁：`comment coverage ok`。
