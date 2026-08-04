# PRD-030 - dev/test/prod 环境隔离

## 文档状态

- PRD 阶段：M0.5-W3/4
- 优先级：P0
- 当前状态：实现与聚焦验收完成，全量回归结果见测试记录
- 依赖：PRD-029 Project/Application 模型
- 明确边界：本 PRD 不实现多地域环境，不把运行部署环境与租户业务环境混为一体

## 实现结论

- 已新增租户级 `Environment` 主数据、管理 API、审计事件和 `environment.created` Webhook 事件。
- Virtual Key、ModelAlias、BudgetRule、路由决策、调用日志、用量账本和预算评估已接入环境归因。
- Gateway 仅信任 Virtual Key 恢复的环境，禁用环境在上游调用前阻断；调试台后端拒绝环境与 Key 不匹配请求。
- 控制台已提供环境设置页，并将 Key、别名、预算和调试台的环境输入改为稳定环境 ID 选择。
- 聚焦测试 17 个、控制面全量 70 个、数据面全量 55 个、前端 103 个测试全部通过；中文注释门禁通过。

## 需求理解

本 PRD 要把现有散落在 Virtual Key 和 ModelAlias 上的 `dev/test/prod` 字符串升级为租户内可管理、可校验、可追踪的 `Environment` 业务对象。Environment 是控制面配置边界，不是服务进程的 `METAHUB_ENV` 运行配置，也不是地域或部署集群。

核心业务目标如下：

1. 管理员可以在当前租户内创建、查看和维护 dev、test、prod 环境。
2. Virtual Key、ModelAlias 和 BudgetRule 必须关联同一租户内的 Environment。
3. 同一个租户中，相同模型别名可在不同环境指向不同内部模型；同一环境内仍保持唯一。
4. Gateway 使用 Virtual Key 绑定的环境解析模型别名和预算，调用方不能通过请求体或不可信 Header 覆盖环境。
5. 调用日志和用量账本保存 `environment_id` 与环境代码快照，成功、路由失败、预算失败和调试台失败都可追踪环境。
6. 内置调试台显式选择环境，前端只展示匹配的 Key/别名；后端再次校验所选环境与 Key 绑定环境一致，防止生产 Key 在测试页误用。
7. Environment 创建时产生 `environment.created` 事件，事件只包含安全元数据。

兼容策略：

- 已有 `virtual_keys.environment`、`model_aliases.environment` 和 `routing_decisions.environment` 暂时保留为环境代码快照。
- 新增的 `environment_id` 是权威关联。写路径同时维护 ID 和代码快照；读路径优先通过 Environment 关联返回名称和状态。
- 旧客户端仅提交 `environment=dev|test|prod` 时，服务层在当前租户内解析对应 Environment；新客户端优先提交 `environment_id`。
- 同时提交 `environment_id` 和 `environment` 时，二者必须指向同一环境，否则返回统一业务错误码。

## 涉及模块

### 数据库迁移

- 新增 `environments`、`environment_events`。
- 为 `virtual_keys`、`model_aliases`、`budget_rules`、`request_logs`、`usage_ledger`、`routing_decisions` 增加 `environment_id`。
- 为 `request_logs`、`usage_ledger` 增加 `environment_code` 快照，保证历史查询无需依赖可变显示名称。
- 从现有 Virtual Key、ModelAlias 和路由记录中提取环境代码，为每个租户回填 Environment，再回填外键。
- 重建 `budget_rules`，把旧唯一约束升级为 `(tenant_id, environment_id, scope_type, scope_id)`；否则同一 scope 无法分别配置 test/prod 预算。

### 控制面 API

- 新增 `metahub_control.environments` 模块及 `/environments` 管理接口。
- Virtual Key、ModelAlias、Budget、Request Log、Usage 接口接入 Environment 关联和筛选。
- Debug Console 请求增加 `environment_id`，服务端校验 Key 与环境一致。
- 导航增加“环境设置”，管理员可写，开发者只读。

### Gateway 数据面

- Virtual Key 鉴权上下文增加 `environment_id` 和可信环境代码。
- 鉴权时联查 Environment；环境已禁用时拒绝调用。
- 路由按 `environment_id` 解析同名 ModelAlias，旧代码字段只用于兼容和日志快照。
- 预算只加载当前 Environment 的规则。
- 成功和失败请求均把环境写入 RequestLogContext、request_logs、usage_ledger 和 routing_decisions。

### Console Web

- 新增环境设置页面和 API 客户端。
- Virtual Key、ModelAlias、Budget 表单改为从 Environment 列表选择。
- Debug Console 增加环境选择器，并按环境联动 Key 与别名。
- Request Log、Usage 增加环境筛选和环境展示。

### 共享契约与文档

- 新增统一错误码。
- 更新 README、目录边界、测试文档和本 PRD 实施记录。
- 执行中文注释门禁，新增定义、函数和关键分支必须有详细中文说明。

## 数据表/字段设计

### environments

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| environment_id | TEXT | PK | 环境稳定 ID |
| tenant_id | TEXT | NOT NULL, FK tenants | 租户边界 |
| code | TEXT | NOT NULL | `dev`、`test`、`prod`，租户内唯一且创建后不可修改 |
| name | TEXT | NOT NULL | 展示名称，最长 80 字符 |
| description | TEXT | NULL | 说明，最长 240 字符 |
| status | TEXT | NOT NULL | `active`、`disabled` |
| created_by | TEXT | NULL, FK users | 创建人 |
| updated_by | TEXT | NULL, FK users | 最近更新人 |
| created_at | TEXT | NOT NULL | UTC 时间 |
| updated_at | TEXT | NOT NULL | UTC 时间 |

约束和索引：

- `UNIQUE(tenant_id, code)`。
- `INDEX(tenant_id, status, code)`。
- MVP 只允许 `dev/test/prod`，不允许 region、cluster 等多地域字段。
- 禁用环境不会自动修改关联资源，但 Gateway 和新建/更新配置会拒绝使用禁用环境。

### environment_events

| 字段 | 类型 | 约束 | 说明 |
| --- | --- | --- | --- |
| event_id | TEXT | PK | 事件 ID |
| tenant_id | TEXT | NOT NULL | 租户边界 |
| environment_id | TEXT | NOT NULL | 目标环境 |
| event_type | TEXT | NOT NULL | `environment.created` 或 `environment.updated` |
| actor_id | TEXT | NULL | 操作用户 |
| request_id | TEXT | NOT NULL | 请求追踪 |
| trace_id | TEXT | NOT NULL | 链路追踪 |
| payload | TEXT | NOT NULL | JSON 安全元数据，不含 Key/Token/Prompt |
| created_at | TEXT | NOT NULL | UTC 时间 |

### 关联表变更

| 表 | 新增/调整字段 | 规则 |
| --- | --- | --- |
| virtual_keys | `environment_id TEXT` | 回填后新写入必填；必须与 `tenant_id` 和代码快照一致 |
| model_aliases | `environment_id TEXT` | 路由以 ID 为准；同租户、同 alias、同环境唯一 |
| budget_rules | `environment_id TEXT` | 每条预算规则只在一个环境内生效 |
| request_logs | `environment_id TEXT`, `environment_code TEXT` | 请求开始即可写入，失败请求不能缺失可信环境 |
| usage_ledger | `environment_id TEXT`, `environment_code TEXT` | 成本按环境可聚合，预算消费按环境过滤 |
| routing_decisions | `environment_id TEXT` | 路由成功和失败均保留环境身份 |

迁移回填：

1. 收集每个租户在 Virtual Key、ModelAlias、routing_decisions 中已出现的合法环境代码。
2. 每个租户至少创建 `dev` Environment；已有 `test/prod` 数据时同步创建对应对象。
3. 通过 `(tenant_id, code)` 回填各表 `environment_id`。
4. 为迁移后新增或种子插入的租户创建数据库触发器，自动生成 active dev Environment。
5. 旧预算规则没有环境字段，统一回填到该租户 `dev`，保持当前默认语义。
6. request_logs 和 usage_ledger 优先从 Virtual Key 反查环境；无法反查的历史记录回填为租户 `dev`。
7. SQLite 迁移阶段使用索引和服务层校验保证复合租户边界；所有新查询必须同时带 `tenant_id`。
8. 迁移 SQL 显式使用事务包裹建表、回填和重建步骤，避免中途失败留下半迁移结构。

回滚策略：

- 删除新增索引、关联字段和新表。
- 回滚前保留旧 `environment` 代码字段，因此 Virtual Key 和 ModelAlias 的旧接口仍可恢复运行。

## API 设计

所有响应继续由统一中间件携带 `request_id`、`trace_id`；请求体不接受 `tenant_id`。

### POST /environments

权限：`admin`。

请求：

```json
{
  "code": "test",
  "name": "测试环境",
  "description": "集成测试与上线前验证"
}
```

响应：

```json
{
  "environment": {
    "environment_id": "...",
    "tenant_id": "...",
    "code": "test",
    "name": "测试环境",
    "description": "集成测试与上线前验证",
    "status": "active",
    "created_at": "...",
    "updated_at": "..."
  },
  "request_id": "...",
  "trace_id": "..."
}
```

### GET /environments

权限：`admin`、`developer`。

查询参数：

- `status=active|disabled|all`，默认 `active`。
- `code=dev|test|prod`，可选。

按 `dev -> test -> prod` 固定顺序返回，便于各配置页面稳定展示。

### PATCH /environments/{environment_id}

权限：`admin`。

可修改 `name`、`description`、`status`；`code` 不可修改。空 PATCH 返回业务错误。禁用环境后，控制面不允许新建或迁入 Key、ModelAlias、Budget；Gateway 已绑定 Key 的后续鉴权也会失败。

Environment 创建成功后，服务先提交 Environment 与 `environment_events` 主事务，再基于统一事件信封同步尝试投递 `environment.created` Webhook。Webhook 失败只记录 delivery/attempt 和结构化错误，不回滚已经创建的环境。

### 既有接口调整

| API | 调整 |
| --- | --- |
| GET/POST/PATCH `/virtual-keys` | 返回 `environment_id/code/name/status`；创建和更新优先接收 `environment_id`，兼容 `environment` |
| GET/POST/PATCH `/model-aliases` | 同上；列表支持 `environment_id`；唯一性按 Environment 隔离 |
| GET/PATCH `/budgets` | 规则返回并接收 `environment_id`；列表支持环境筛选 |
| GET `/budget-status` | 支持 `environment_id`；Key 查询时以后端 Key 绑定环境为准 |
| GET `/request-logs` | 支持 `environment_id` 和兼容代码筛选，列表/详情展示环境快照 |
| GET `/usage/summary`, `/usage/records` | 支持 `environment_id`，汇总增加 `by_environment` |
| POST `/debug/chat-completions` | 页面提交 `environment_id`，后端校验 Key 环境匹配，再在该环境解析 alias；旧客户端省略时从 Key 安全派生 |

### Gateway 环境解析

OpenAI-compatible API 不新增允许调用方选择环境的 Header 或请求字段。环境只能来自鉴权成功的 Virtual Key：

```text
Bearer Virtual Key
  -> 查询 virtual_keys + environments
  -> 校验 tenant、Key、Environment 均为 active
  -> 得到可信 environment_id/code
  -> 按 environment_id 解析 ModelAlias
  -> 按 environment_id 加载 BudgetRule
  -> 写入日志和用量
```

## 前端页面/交互设计

### 环境设置页

- 页面位于“配置”分组，名称为“环境设置”。
- 顶部以紧凑分段控制展示 dev、test、prod，不使用营销式大卡片。
- 每行展示代码、名称、状态、关联 Key 数、关联别名数、关联预算数和更新时间。
- 管理员可创建缺失环境、编辑名称/说明、启用或禁用。
- 开发者可查看但不能看到写按钮；后端仍通过 RBAC 拒绝写请求。
- 创建时只可选择尚未存在的标准代码，避免重复环境。
- 禁用 prod 前显示明确确认提示，但本 PRD 不实现复杂审批流。

### Virtual Key、ModelAlias、Budget

- 环境字段从自由字符串/硬编码下拉改为 Environment 列表。
- 禁用环境显示在历史记录中，但不能作为新建或迁移目标。
- 表格同时展示环境代码和名称，避免中文名称相似造成误选。

### Debug Console

- 环境选择器位于 Key 和模型别名之前。
- 切换环境后，只显示该环境 active Key 和 active ModelAlias。
- 当前选择不再属于目标环境时，自动清空并选择第一个可用项。
- 发送按钮只有在环境、Key、别名三者齐全且匹配时可用。
- 前端联动只降低误操作；后端以 `environment_id` 和 Key 记录再次校验。

### Request Log 与 Usage

- 筛选栏增加环境选择器。
- 日志详情展示环境代码、名称和环境 ID；失败日志同样展示。
- 用量总览增加按环境聚合，不改变现有项目、应用、模型和 Provider 聚合。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `environment.not_found` | 404 | 当前租户内环境不存在 |
| `environment.code_invalid` | 422 | code 不是 dev/test/prod |
| `environment.code_conflict` | 409 | 当前租户已存在相同 code |
| `environment.name_invalid` | 422 | 名称为空或超长 |
| `environment.description_invalid` | 422 | 说明超过 240 字符 |
| `environment.status_invalid` | 422 | 状态非法 |
| `environment.update_empty` | 422 | PATCH 无可编辑字段 |
| `environment.disabled` | 409 | 创建或迁移资源时目标环境已禁用 |
| `environment.binding_mismatch` | 409 | environment_id 与兼容 code 不一致或跨租户 |
| `virtual_key.environment_mismatch` | 409 | Key 绑定环境与请求的环境不一致 |
| `model_alias.environment_mismatch` | 409 | 别名环境关联与兼容代码不一致 |
| `budget.environment_mismatch` | 409 | 预算 scope 对象与环境不一致，或环境参数冲突 |
| `debug.environment_mismatch` | 409 | 调试台选择环境与 Virtual Key 绑定环境不一致 |
| `gateway.environment_disabled` | 403 | Key 所属 Environment 已禁用 |

错误响应继续使用统一结构，不返回 SQL、堆栈、Key 明文、Provider Key、Prompt 或响应正文。

## 安全与权限约束

1. `tenant_id` 只来自登录态或 Virtual Key 鉴权上下文，不接受前端传入。
2. 所有 Environment 和关联资源查询必须同时约束 `tenant_id`，不能仅凭 `environment_id` 查询。
3. `GET /environments` 允许 admin/developer；POST/PATCH 仅 admin。
4. Environment 禁用是数据面权限边界，不能只在控制台隐藏选项。
5. Gateway 不信任请求体、query 或自定义 Header 中的环境信息，只信任 Virtual Key 绑定环境。
6. Debug Console 必须后端校验 `payload.environment_id == virtual_key.environment_id`。
7. Provider Key、Virtual Key 明文、Authorization、Prompt 和模型完整响应不得进入 Environment 事件或结构化日志。
8. Environment 事件 payload 只允许 code、name、status、changed_fields 等安全字段。
9. 失败请求必须在可获得可信 Key 上下文后写入 `request_logs`，包括环境禁用、别名不存在、预算耗尽和调试环境不匹配。
10. 用量与成本必须带 `tenant_id`、`environment_id`，预算消费只能在当前环境内聚合。
11. `environment.created` 加入共享 Webhook 事件白名单；投递失败不得破坏环境配置事务。
12. Gateway 鉴权先恢复 Key 的可信 Environment 上下文，再在进入路由前检查环境状态；这样禁用环境既不会继续调用上游，也能写入带环境的失败 request_log。

## 测试用例

### 主流程

1. 管理员创建 test 和 prod Environment，响应含 request_id/trace_id，数据库产生 `environment.created`。
2. 在 test/prod 分别创建同名 ModelAlias，指向不同内部模型，Gateway 按各自 Key 环境路由到不同模型。
3. 创建 test/prod Virtual Key，返回只展示一次明文 Key，列表不返回 hash 或明文。
4. 为 test 和 prod 创建独立预算，test 用量不计入 prod 预算。
5. Gateway 成功请求写入 request_logs、routing_decisions、usage_ledger 的 environment_id/code。
6. Request Log 和 Usage 能按环境筛选，Usage 返回 by_environment 聚合。
7. Debug Console 使用 test 环境、test Key、test alias 成功，日志与用量环境一致。

### 异常与权限

1. developer 可以 GET，POST/PATCH Environment 返回 `auth.permission_denied`。
2. viewer 访问环境 API 返回权限错误，环境导航不可见。
3. 重复 code、非法 code、空名称、空 PATCH 返回对应统一错误码。
4. 跨租户 environment_id 不能绑定 Key、Alias 或 Budget。
5. 禁用 Environment 后，新建绑定资源和 Gateway 调用均被拒绝。
6. Debug Console 传 prod Key + test environment_id 返回 `debug.environment_mismatch`，并写失败调用日志和安全事件。
7. environment_id 与兼容 `environment` code 冲突时返回 `environment.binding_mismatch`。
8. 环境路由失败、预算阻断仍写入含环境的失败日志。
9. 事件和日志扫描确认不存在 Provider Key、Virtual Key 明文、Authorization 或 Prompt。

### 迁移与回归

1. 从 0020 升级后，现有 dev/test/prod Key 和别名均有正确 environment_id。
2. 旧预算规则回填到 dev；旧日志和用量可按 dev 查询。
3. 0021 回滚后旧字符串环境字段仍存在，0020 功能可恢复。
4. 既有 Virtual Key、ModelAlias、Budget、Request Log、Usage、Debug Console 测试继续通过。
5. 控制面、Gateway 全量测试、Console 单测/lint 和中文注释门禁全部通过。

## 代码修改计划

1. 新增 `0021_environment_isolation.sql` 与回滚迁移，完成表、字段、索引和历史回填。
2. 新增控制面 `environments` models/service/routes，注册 Router 和 RBAC 依赖。
3. 扩展统一错误码和环境解析公共辅助逻辑，避免 Key、Alias、Budget 各自重复跨租户校验。
4. 改造 Virtual Key、ModelAlias、Budget 的 DTO、服务查询和写入逻辑，保留旧 code 兼容。
5. 改造 Gateway 鉴权、路由、预算、RequestLogContext 和用量写入，使环境 ID 贯穿成功/失败链路。
6. 改造 Request Log、Usage 和 Debug Console API，增加环境筛选、聚合和后端匹配校验。
7. 新增 Console Environment 客户端和页面；联动 Key、Alias、Budget、Debug、Log、Usage。
8. 增加 PRD-030 控制面/Gateway 集成测试和 Console 单元测试，扩展迁移与导航测试。
9. 更新 README、目录边界和测试文档，执行格式、lint、全量测试及中文注释门禁。

## 第一轮设计审查：数据一致性与迁移可恢复性

审查结论：通过，以下问题已在设计中修正。

1. 原设计只描述给 `budget_rules` 加字段，遗漏旧唯一约束会阻止同一 scope 在多个环境建立预算。已改为重建预算表并使用 `(tenant_id, environment_id, scope_type, scope_id)` 唯一约束。
2. Environment 是权威 ID，但现有 Key/别名和路由依赖代码字符串。保留代码快照并双写，回滚时仍可恢复 0020 行为；代码不可修改，避免 ID 与快照长期漂移。
3. 历史租户不一定已有 test/prod 数据。迁移只创建实际出现的标准环境并保证 dev 存在，既不凭空扩大配置，又能兼容旧数据。
4. 迁移包含重建表和多表回填，必须由显式事务包裹；任何一步失败都不能留下部分字段。
5. Excel 标注 `environment.created` 为事件/Webhook。仅写 `environment_events` 不足，已增加共享事件白名单和投递设计；投递放在主事务提交后，避免外部网络故障回滚配置。
6. 同名别名的隔离由不可变环境代码旧唯一约束与新增 Environment ID 唯一索引共同保证，迁移和兼容期间均不会出现同环境重复别名。

## 第二轮设计审查：权限、失败闭环与 MVP 边界

审查结论：通过，可以进入编码。审查确认如下：

1. 控制台导航只用于改善体验，真正写权限由 `require_environment_admin` 在后端限制为 admin；developer 只有读权限，viewer 无环境 API 权限。
2. Gateway 不开放环境覆盖 Header 或请求体字段。业务调用环境始终来自 Virtual Key，避免调用方拿 prod Key 后伪装成 test 环境绕过预算或别名。
3. 环境禁用判断不能直接在鉴权函数中丢失上下文。实现时先构造可信 `GatewayAuthContext`，再在路由前拒绝禁用环境，使失败请求可写入含 tenant、Key、environment 的 request_logs。
4. Debug Console 前端过滤不足以构成安全边界。后端必须比较所选 environment_id 与 Key 记录；不匹配时不做路由、不调用 Provider，并写失败日志与调试安全事件。
5. 为兼容既有调试 API，`environment_id` 可省略并从 Key 派生；新版页面始终显式提交，因此“prod Key + test 页面”仍会被后端识别并拒绝。
6. Tenant/Project/Application 预算允许在每个环境各有一条规则；Virtual Key scope 的环境必须与该 Key 绑定环境一致。Project/Application 本 PRD 不新增环境外键，避免超出 Excel 明确范围。
7. Environment 禁用不会级联禁用 Key、Alias 或 Budget，也不引入审批、地域、集群、配置发布和复杂动态配置中心，保持 M0.5 范围可控。
8. 事件投递采用现有同步最小闭环，不在本 PRD 引入消息队列、死信桶或后台调度器。
9. 所有新增日志、事件、DTO 和测试均只使用脱敏 Key 前缀或资源 ID；Provider Key、Virtual Key 明文、Authorization、Prompt 和响应正文不进入持久化或结构化日志。
