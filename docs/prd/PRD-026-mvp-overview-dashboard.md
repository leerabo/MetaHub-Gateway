# PRD-026 - MVP总览Dashboard

状态：已完成编码、测试和文档更新。

## 需求理解

PRD-026 属于 M0-W2/P0 的仪表盘 MVP，目标是在控制台第一屏让老板、管理员和开发者一眼看到“今天是否正常、花了多少钱、哪里在失败、哪些模型和 Key 最活跃”。Excel 明确范围包括今日请求数、成功率、P95 延迟、今日成本、Top 模型、Top Virtual Key、最近错误；验收要求指标来自真实 `request_logs` 与 `usage_ledger`，刷新后数值可更新，空数据时有接入引导。

架构文档约束补充：Usage Ledger 是成本追踪和 FinOps 的事实来源；Trace、Usage、Budget、Webhook 等数据必须按租户隔离查询；控制台展示层优先中文，内部 code 保持稳定英文；金额默认人民币。M0 不做复杂趋势预测、客户健康评分、跨租户汇总、长期指标缓存或 BI 报表，本 PRD 只做当前租户“今日”总览。

## 涉及模块

- `infra/migrations/`
  - 新增 `0018_dashboard_overview.sql` 与 down 迁移。
  - 不新增指标缓存表；扩展控制台事件账本允许 `dashboard.viewed`，并补充 Dashboard 查询索引。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 新增 `dashboard.unavailable`、`dashboard.query_invalid` 错误码。
- `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py`
  - 为 Dashboard 查询失败补充诊断建议。
- `control-plane-api/src/metahub_control/auth/dependencies.py`
  - 新增 `require_dashboard_reader`，允许当前租户内已认证用户读取安全聚合指标；详细日志/用量 API 仍按原有 admin/developer 权限控制。
- `control-plane-api/src/metahub_control/dashboard/`
  - 新增 DTO、路由和服务，注册 `GET /dashboard/overview`。
- `control-plane-api/src/metahub_control/console/`
  - 允许控制台事件 `dashboard.viewed`。
  - 更新 `overview` 导航描述，使其指向真实 Dashboard，而不是仅服务状态和 MVP 边界说明。
- `console-web/src/lib/dashboard.js`
  - 新增 Dashboard API client，统一处理 `request_id`、`trace_id` 和错误结构。
- `console-web/src/features/dashboard/renderDashboardPanel.js`
  - 新增总览 Dashboard 面板。
- `console-web/src/features/developer-start/renderDeveloperStart.js`
  - 将 `overview` 内容从旧引导页替换为 Dashboard；空数据时保留接入引导。
- `console-web/src/app/main.js`
  - 登录后加载 Dashboard 状态，并在刷新周期重新读取。
- `console-web/src/styles/main.css`
  - 增加 Dashboard KPI、排行、最近错误和空状态样式。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-026-tests.md`
  - 记录 API、数据来源、权限边界和测试结果。

## 数据表/字段设计

### 不新增 `dashboard_metrics` 持久表

`DashboardMetric` 在本 PRD 中定义为 API 响应 DTO，不作为数据库表。原因：M0 Dashboard 只展示当前租户今日实时聚合结果，真实事实数据已经存在于 `request_logs` 与 `usage_ledger`。新增缓存表会引入刷新一致性、回填和失效策略，超出 M0 范围。

### 源数据表

- `request_logs`
  - 使用字段：`tenant_id`、`request_id`、`trace_id`、`virtual_key_id`、`provider_code`、`request_model`、`resolved_model`、`status`、`error_code`、`latency_ms`、`created_at`。
  - 用途：今日请求数、成功率、错误率、P95 延迟、最近错误、Top 模型/Key 的请求量。
- `usage_ledger`
  - 使用字段：`tenant_id`、`request_id`、`model_id`、`resolved_model`、`virtual_key_id`、`provider_code`、`total_tokens`、`cost_cny`、`cost_usd`、`estimated`、`created_at`。
  - 用途：今日成本、Token、Top 模型/Key 的成本聚合。
- `virtual_keys`
  - 使用字段：`tenant_id`、`virtual_key_id`、`key_prefix`、`name`。
  - 用途：Top Virtual Key 只展示名称和前缀，不展示明文、哈希或密文。
- `console_page_events`
  - 扩展 `event_type` 允许 `dashboard.viewed`，记录 Dashboard 访问审计。

### 迁移：`0018_dashboard_overview.sql`

- 通过 SQLite 重建表方式扩展 `console_page_events.event_type` 约束：允许 `console.page_viewed` 与 `dashboard.viewed`。SQLite 不能直接修改 CHECK 约束，迁移必须复制旧数据、替换表并重建索引。
- 新增索引：
  - `idx_request_logs_dashboard_latency(tenant_id, created_at, latency_ms)`：支撑今日 P95 延迟计算。
  - `idx_request_logs_dashboard_model_time(tenant_id, resolved_model, created_at DESC)`：支撑 Top 模型请求量。
  - `idx_request_logs_dashboard_key_time(tenant_id, virtual_key_id, created_at DESC)`：支撑 Top Virtual Key 请求量。
- down 迁移：删除上述索引，并将 `console_page_events` 约束恢复为只允许 `console.page_viewed`；若存在 `dashboard.viewed` 事件，down 迁移需先删除或转换为 `console.page_viewed`，并在注释中说明回滚影响。

### DTO 字段

`DashboardOverviewResponse`

- `window`: `{ timezone, start_at, end_at, generated_at }`
- `metrics`: `DashboardMetric[]`
- `top_models`: `DashboardTopModel[]`
- `top_virtual_keys`: `DashboardTopVirtualKey[]`
- `recent_errors`: `DashboardRecentError[]`
- `empty_state`: `DashboardEmptyState`
- `request_id`: string
- `trace_id`: string

`DashboardMetric`

- `id`: `today_requests | success_rate | p95_latency_ms | today_cost_cny | total_tokens | error_rate`
- `label`: 中文展示名
- `value`: number
- `unit`: `count | percent | ms | cny | tokens`
- `status`: `ok | warning | critical | empty`
- `description`: 安全说明或业务解释

`DashboardTopModel`

- `model_id`: string | null
- `resolved_model`: string | null
- `provider_code`: string | null
- `request_count`: int
- `success_count`: int
- `total_tokens`: int
- `cost_cny`: float

`DashboardTopVirtualKey`

- `virtual_key_id`: string | null
- `virtual_key_name`: string | null
- `virtual_key_prefix`: string | null
- `request_count`: int
- `success_count`: int
- `total_tokens`: int
- `cost_cny`: float

`DashboardRecentError`

- `request_id`: string
- `trace_id`: string
- `error_code`: string | null
- `diagnosis_title`: string | null
- `model`: string | null
- `provider_code`: string | null
- `latency_ms`: int | null
- `created_at`: datetime

`DashboardEmptyState`

- `is_empty`: bool
- `title`: string
- `description`: string
- `recommended_actions`: list，最多 4 条，指向 Provider、模型目录、Virtual Key、调试台或调用日志。

## API设计

### `GET /dashboard/overview`

用途：读取当前租户今日 Dashboard 聚合数据。

认证：`require_dashboard_reader`。M0 允许所有已认证角色读取安全聚合；明细 API 仍由现有 request log/usage 权限限制。

查询参数：M0 不开放任意时间范围，默认按 Asia/Shanghai 今日计算。后续 M1 可增加 `date` 或 `range`。

响应示例：

```json
{
  "window": {
    "timezone": "Asia/Shanghai",
    "start_at": "2026-07-10T00:00:00+08:00",
    "end_at": "2026-07-11T00:00:00+08:00",
    "generated_at": "2026-07-10T15:30:00Z"
  },
  "metrics": [
    { "id": "today_requests", "label": "今日请求", "value": 128, "unit": "count", "status": "ok", "description": "来自当前租户 request_logs" },
    { "id": "success_rate", "label": "成功率", "value": 98.4, "unit": "percent", "status": "ok", "description": "success / total" },
    { "id": "p95_latency_ms", "label": "P95 延迟", "value": 842, "unit": "ms", "status": "warning", "description": "按今日非空 latency_ms 计算" },
    { "id": "today_cost_cny", "label": "今日成本", "value": 36.72, "unit": "cny", "status": "ok", "description": "来自 usage_ledger.cost_cny" }
  ],
  "top_models": [],
  "top_virtual_keys": [],
  "recent_errors": [],
  "empty_state": { "is_empty": false, "title": "", "description": "", "recommended_actions": [] },
  "request_id": "req_xxx",
  "trace_id": "trc_xxx"
}
```

聚合口径：

- 时间窗口：Asia/Shanghai 今日 `[00:00, 次日00:00)`，查询前转换为 UTC ISO 字符串匹配数据库 `created_at`。
- 请求数：`COUNT(*) FROM request_logs WHERE tenant_id=? AND created_at BETWEEN window`。
- 成功率：`status='success' / total`；total 为 0 时返回 0，状态 `empty`。
- 错误率：`status='failed' / total`；用于辅助卡片和最近错误状态。
- P95 延迟：只统计 `latency_ms IS NOT NULL` 的今日记录；用 `COUNT` + `ORDER BY latency_ms LIMIT 1 OFFSET ceil(n*0.95)-1` 计算，避免把全部延迟读入内存。
- 今日成本：`SUM(cost_cny)` 来自 `usage_ledger` 今日记录；空值归 0。
- Top 模型：按 `COALESCE(request_logs.resolved_model, usage_ledger.resolved_model, model_id, request_model)` 分组，最多 5 条，排序优先 `request_count DESC, cost_cny DESC`。
- Top Virtual Key：按 `virtual_key_id` 分组，最多 5 条，只返回 `key_prefix/name`，不返回明文、哈希或密文。
- 最近错误：读取今日最近 5 条 `request_logs.status='failed'`，通过 `diagnose_error(error_code)` 附带安全诊断标题。

## 前端页面/交互设计

页面：`总览Dashboard`，复用导航项 `overview`。

布局：

- 顶部标题区：显示“总览 Dashboard”、今日窗口和最后刷新时间。
- KPI 区：4 到 6 个紧凑指标块，包含今日请求、成功率、P95 延迟、今日成本、错误率、总 Token。
- 中部两列：Top 模型、Top Virtual Key。每行显示名称、请求数、成功数、Token、成本。
- 底部：最近错误列表，展示 `request_id`、错误码、模型、Provider、时间和诊断标题。
- 空状态：当 `empty_state.is_empty=true` 时展示接入引导，不展示空表格；推荐路径是注册 Provider、创建模型目录、创建 Virtual Key、进入调试台。
- 权限体验：Dashboard API 允许安全聚合读取；“查看调用详情”链接只在用户能看到调用日志导航时展示，前端不把链接当权限判断。

交互：

- 全局 15 秒刷新沿用现有 `refresh()`，Dashboard 跟随重新加载。
- API 失败时展示统一错误面板，必须显示 `request_id`。
- 文本全部 HTML 转义，特别是模型名、Provider code、Virtual Key 名称和错误诊断文案。
- 页面不展示 Prompt、Response、Authorization、Virtual Key 明文、Provider Key 或接收端响应正文。

## 错误码设计

- `dashboard.unavailable`
  - HTTP 503
  - 场景：数据库不可用、聚合查询失败或迁移缺失。
  - suggestion：检查数据库连接、迁移版本和 request_logs/usage_ledger 表。
  - retryable：true
- `dashboard.query_invalid`
  - HTTP 422
  - 场景：后续扩展查询日期/范围时参数非法；M0 保留但不主动暴露复杂查询。
  - suggestion：使用默认今日窗口或合法日期。
  - retryable：false
- 鉴权类错误继续复用 `auth.token_missing`、`auth.token_invalid`、`auth.token_expired`、`auth.permission_denied`。
- 控制台事件非法继续复用 `console.event_invalid`；数据库事件约束缺失时服务层映射为 `dashboard.unavailable` 或 `console.event_invalid`，不暴露 SQL 细节。

## 安全与权限约束

- 所有 Dashboard 查询必须从 `AuthContext.user.tenant_id` 获取租户，不接收任何请求体或查询参数里的 `tenant_id`。
- Dashboard 只返回聚合和脱敏摘要；Top Virtual Key 只返回 `virtual_key_id`、`name`、`key_prefix`，不返回明文、哈希、密文或上游 Key。
- 最近错误不返回 Prompt、Response、messages、input、Authorization、Provider Key、Virtual Key 明文或 raw upstream body。
- Dashboard API 可被所有已认证角色读取，因为它是安全聚合视图；调用日志详情、Usage 明细和管理操作仍按既有 admin/developer/admin-only 权限校验。
- 前端隐藏“查看详情”链接只做体验优化，后端 request log API 仍必须拒绝无权限用户。
- `dashboard.viewed` 事件 payload 只允许 `source`、`hash`、`viewport_class` 等安全字段，不保存模型名列表、错误详情、Prompt 或用户输入。
- 任何失败请求必须返回统一错误结构，并携带 `request_id`、`trace_id`。
- 聚合查询必须有租户和时间窗口索引，避免 Dashboard 刷新造成全表扫描。

## 测试用例

后端集成测试：

1. 主流程：为同一租户写入今日成功/失败 `request_logs` 和 `usage_ledger`，调用 `GET /dashboard/overview`，断言今日请求、成功率、错误率、P95、成本、Top 模型、Top Virtual Key、最近错误准确。
2. 租户隔离：写入另一个租户的数据，断言 Dashboard 不计入任何指标。
3. 时间窗口：写入昨天和明天边界数据，断言只统计 Asia/Shanghai 今日窗口。
4. 空状态：空租户返回 `empty_state.is_empty=true`，推荐动作存在，指标值为 0 且状态为 `empty`。
5. 权限与错误：未登录返回 `auth.token_missing`；数据库不可用返回 `dashboard.unavailable`，响应包含 `request_id`。
6. 事件：访问 Dashboard 后 `console_page_events` 可记录 `dashboard.viewed`，payload 不允许敏感字段。
7. 迁移：`0018_dashboard_overview.sql` 和 down 迁移可执行，重复迁移幂等，回滚不破坏旧 `console.page_viewed` 事件。

前端单元测试：

1. 渲染非空 Dashboard：KPI、Top 模型、Top Virtual Key、最近错误和刷新时间可见。
2. 渲染空状态：无调用时展示接入引导，不显示空表格噪音。
3. 错误状态：API 失败时显示错误码和 `request_id`。
4. 安全转义：模型名、Key 名称、错误文案中的 HTML 被转义。
5. 权限体验：无调用日志权限时不展示详情跳转，但 Dashboard 摘要仍可读。

回归命令：

```powershell
python -m pytest control-plane-api\tests\integration\test_dashboard.py control-plane-api\tests\integration\test_db_migrations.py -q
node --test console-web\tests\unit\dashboard.test.mjs
python scripts\ci\check-comments.py
node scripts\lint.mjs  # 在 console-web 目录执行
```

## 两轮设计自审结论

第一轮自审：确认 `DashboardMetric` 只作为响应 DTO，不新增缓存表；事实数据继续来自 `request_logs` 与 `usage_ledger`，避免 M0 引入刷新一致性和回填任务。

第二轮自审：确认 Dashboard 只返回安全聚合摘要，允许 admin/developer/viewer 读取；调用日志详情和用量明细仍由既有后端权限守卫限制，前端链接隐藏不能替代后端校验。

## 代码修改计划

1. 新增迁移 `0018_dashboard_overview.sql/.down.sql`，扩展 `console_page_events` 事件类型并添加 Dashboard 查询索引。
2. 在 shared runtime 增加 Dashboard 错误码和诊断信息。
3. 新增 `metahub_control/dashboard/models.py`，定义 Dashboard DTO，所有 class 和字段说明写中文注释。
4. 新增 `metahub_control/dashboard/service.py`，实现今日窗口、指标聚合、Top 模型、Top Virtual Key、最近错误和空状态；关键 SQL、P95 计算、tenant_id 过滤和敏感字段排除必须有中文注释。
5. 新增 `metahub_control/dashboard/routes.py` 并在 `app.py` 注册。
6. 在 `auth/dependencies.py` 增加 `require_dashboard_reader`。
7. 扩展 `console/models.py` 与 `console/service.py`，允许 `dashboard.viewed`，并更新 overview 导航描述。
8. 新增前端 API client 与 Dashboard 渲染模块，接入 `main.js` 和 `renderDeveloperStart.js`。
9. 增加 Dashboard CSS，使用紧凑 KPI、排行和错误列表布局，不做营销式 Hero。
10. 补后端集成测试、前端单元测试、迁移测试和注释门禁。
11. 更新 README、目录文档和 `docs/testing/PRD-026-tests.md`。

## 已定实现决策

- `dashboard.viewed` 作为独立事件类型入库，匹配 Excel 事件清单，并通过 `0018_dashboard_overview.sql` 重建 `console_page_events` CHECK 约束。
- M0 Dashboard API 允许 viewer 读取安全聚合摘要，因为 Excel 用户包含“老板”，且响应不包含明细、Prompt、密钥或跨租户数据；详细日志和 Usage 明细仍保持原权限。
