# PRD-023 - Key/租户日预算与告警

状态：已实现，验证记录见 `docs/testing/PRD-023-tests.md`。

## 实现结果

- 已新增 `budget_rules`、`budget_alert_events`、`budget_evaluations` 三张租户隔离业务表，并扩展 `request_logs` 保存预算命中摘要。
- 控制面已提供 `GET /budgets`、`PATCH /budgets`、`GET /budget-status`，预算读取开放给 admin/developer，预算写入仅 admin。
- 数据面已在限流之后、Provider Adapter 调用之前执行预算检查；达到阻断策略时返回 402 `gateway.budget_exhausted`，且失败请求写入调用日志。
- 控制台已新增“预算告警”页面，并在 Token 用量概览中展示当日预算状态摘要。
- 预算事件按规则、业务日和阈值去重；日志详情可查看预算决策和评估记录。

## 需求理解

PRD-023 属于 M0-W2/P0 的预算告警基础能力，目标是在企业调用成本超过预算前给管理员/财务可见提醒，并在达到 100% 后按配置选择“仅告警”或“阻断”。预算维度包含租户日预算和 Virtual Key 日预算，阈值固定覆盖 80% 与 100%；预算状态需要能在控制台和 API 中查询，预算命中原因需要能回溯到调用日志或预算事件。

本 PRD 依赖 PRD-017 的 `usage_ledger` 成本账本和 PRD-022 的基础限流热路径。数据面在鉴权、请求校验、模型路由和限流通过后，基于当前模型价格与请求 token 估算本次 CNY 成本，再读取当天已产生的 `usage_ledger.cost_cny`，判断是否触发 80% 告警或 100% 阻断。真实成本仍由上游返回后的 Usage Ledger 记录负责；预算检查只做准入估算，不替代账本。

MVP 不做部门分账、发票、充值余额、复杂审批、自动降级到低价模型，也不做跨币种实时汇率折算。预算金额统一按 CNY 解释；USD 或其他币种成本在 PRD-017 中仍保留原始字段，但 PRD-023 的日预算只使用 `cost_cny`。

架构约束对本 PRD 的关键影响：

- 预算检查属于数据面热路径，不能记录 Prompt、Response、Authorization、Provider Key 或 Virtual Key 明文。
- 所有预算规则、预算状态、预算事件和预算评估记录必须带 `tenant_id`。
- 管理 API 必须后端校验权限，不能只靠控制台隐藏入口。
- 预算耗尽要返回统一错误码和 `request_id/trace_id`，失败请求必须进入 `request_logs`。
- 80% 和 100% 阈值事件需要去重，避免同一预算同一天重复刷屏。

## 涉及模块

- `infra/migrations/`
  - 新增 `0015_budget_rules.sql` 与 down 迁移。
  - 新增预算规则、预算告警事件、预算评估记录表。
  - 给 `request_logs` 增加预算命中摘要字段，便于日志详情快速解释阻断原因。
- `shared-sdk/python/metahub_shared/runtime/errors.py`
  - 增加预算配置、预算耗尽和预算状态不可用相关错误码。
- `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py`
  - 增加预算耗尽的可操作诊断建议。
- `control-plane-api/src/metahub_control/budgets/`
  - 新增预算规则 DTO、状态 DTO、路由和服务。
  - `GET /budgets` 返回当前租户规则与今日状态。
  - `PATCH /budgets` 按 scope 创建/更新/禁用规则。
  - `GET /budget-status` 返回当前租户或单个 Virtual Key 的今日预算状态。
- `control-plane-api/src/metahub_control/auth/dependencies.py`
  - 新增预算读写权限依赖：读给 admin/developer，写给 admin。
  - M0 还没有 finance 角色，暂把“财务”能力映射到 admin；后续 RBAC 增强再拆独立 finance role。
- `control-plane-api/src/metahub_control/app.py`
  - 注册预算路由。
- `gateway-data-plane/src/metahub_gateway/budgets/`
  - 新增预算规则加载、日窗口计算、成本估算、阈值判断、告警去重和评估记录逻辑。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`
  - 在限流通过后、Provider Adapter 调用前执行预算检查。
  - 预算阻断时写失败 `request_logs`，错误码为 `gateway.budget_exhausted`。
- `gateway-data-plane/src/metahub_gateway/usage/ledger.py`
  - 复用现有成本估算思想，必要时抽取安全的价格估算 helper，避免预算与账本价格口径分叉。
- `control-plane-api/src/metahub_control/request_logs/`
  - 日志列表/详情增加预算状态摘要字段。
- `console-web/src/lib/budgets.js`
  - 新增预算规则与预算状态 API client。
- `console-web/src/features/budgets/renderBudgetPanel.js`
  - 新增预算设置页和今日状态展示。
- `console-web/src/features/usage/renderUsagePanel.js`
  - 在用量概览中展示预算状态入口或摘要。
- `console-web/src/app/main.js`
  - 加载预算状态、绑定预算设置表单。
- `console-web/src/features/developer-start/renderDeveloperStart.js`
  - 在统一控制台导航中挂载预算设置页。
- `README.md`、`docs/development/directory-map.md`、`docs/testing/PRD-023-tests.md`
  - 更新功能说明、目录边界、测试记录。

## 数据表/字段设计

### 新表：`budget_rules`

用途：保存当前租户下的租户级或 Virtual Key 级日预算配置。M0 每个 `(tenant_id, scope_type, scope_id)` 只保留一条规则。

字段：

- `budget_rule_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'virtual_key'))`
- `scope_id TEXT NOT NULL`
  - `scope_type='tenant'` 时等于 `tenant_id`。
  - `scope_type='virtual_key'` 时等于 `virtual_key_id`。
- `name TEXT NOT NULL`
- `daily_budget_cny NUMERIC NOT NULL CHECK (daily_budget_cny > 0)`
- `warning_threshold_percent INTEGER NOT NULL DEFAULT 80 CHECK (warning_threshold_percent > 0 AND warning_threshold_percent < 100)`
- `exhausted_threshold_percent INTEGER NOT NULL DEFAULT 100 CHECK (exhausted_threshold_percent = 100)`
- `exhausted_action TEXT NOT NULL DEFAULT 'alert_only' CHECK (exhausted_action IN ('alert_only', 'block'))`
- `status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled'))`
- `created_by TEXT`
- `updated_by TEXT`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`

约束与索引：

- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `UNIQUE (tenant_id, scope_type, scope_id)`
- `idx_budget_rules_tenant_scope`
- `idx_budget_rules_status`

设计理由：

- `scope_id` 非空可以避免 SQLite 对 `NULL` unique 的特殊行为，租户预算统一把 `scope_id` 写成 `tenant_id`。
- 80% 阈值保留为字段，M0 默认固定为 80；后续可以放宽配置，不需要改表。
- 100% 阈值固定为 100，策略只在 `alert_only` 与 `block` 之间切换。

### 新表：`budget_alert_events`

用途：记录每天每条预算规则的阈值事件，并通过唯一约束实现重复告警去重。

字段：

- `budget_alert_event_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `budget_rule_id TEXT NOT NULL`
- `scope_type TEXT NOT NULL`
- `scope_id TEXT NOT NULL`
- `budget_date TEXT NOT NULL`
  - 使用 Asia/Shanghai 业务日，格式 `YYYY-MM-DD`。
- `threshold_percent INTEGER NOT NULL`
- `event_type TEXT NOT NULL CHECK (event_type IN ('budget.threshold_reached', 'budget.exhausted'))`
- `current_cost_cny NUMERIC NOT NULL DEFAULT 0`
- `projected_cost_cny NUMERIC NOT NULL DEFAULT 0`
- `budget_cny NUMERIC NOT NULL`
- `action_taken TEXT NOT NULL CHECK (action_taken IN ('warn', 'allow', 'block'))`
- `request_id TEXT`
- `trace_id TEXT`
- `created_at TEXT NOT NULL`

约束与索引：

- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id)`
- `UNIQUE (tenant_id, budget_rule_id, budget_date, threshold_percent)`
- `idx_budget_alert_events_tenant_date`
- `idx_budget_alert_events_request_id`

设计理由：

- 同一预算、同一业务日、同一阈值只产生一次事件，避免后续每个请求都重复告警。
- `request_id/trace_id` 指向首次触发阈值的请求，便于从事件跳回调用日志。

### 新表：`budget_evaluations`

用途：记录预算检查命中的请求级解释，满足“日志可查预算命中原因”。M0 只在达到 80% 或 100% 时写入，不对每个正常请求写全量评估，避免热路径写放大。

字段：

- `budget_evaluation_id TEXT PRIMARY KEY`
- `tenant_id TEXT NOT NULL`
- `request_id TEXT NOT NULL`
- `trace_id TEXT NOT NULL`
- `virtual_key_id TEXT`
- `budget_rule_id TEXT NOT NULL`
- `scope_type TEXT NOT NULL`
- `scope_id TEXT NOT NULL`
- `budget_date TEXT NOT NULL`
- `spent_before_cny NUMERIC NOT NULL DEFAULT 0`
- `estimated_request_cost_cny NUMERIC NOT NULL DEFAULT 0`
- `projected_cost_cny NUMERIC NOT NULL DEFAULT 0`
- `budget_cny NUMERIC NOT NULL`
- `threshold_percent INTEGER NOT NULL`
- `budget_status TEXT NOT NULL CHECK (budget_status IN ('threshold_reached', 'exhausted'))`
- `action_taken TEXT NOT NULL CHECK (action_taken IN ('warn', 'allow', 'block'))`
- `reason_code TEXT NOT NULL`
- `created_at TEXT NOT NULL`

约束与索引：

- `FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)`
- `FOREIGN KEY (budget_rule_id) REFERENCES budget_rules(budget_rule_id)`
- `idx_budget_evaluations_request_id`
- `idx_budget_evaluations_tenant_date`

设计理由：

- 同一个请求可能同时命中租户预算和 Virtual Key 预算，所以评估记录单独成表。
- 正常未触发阈值的请求不落评估表，避免 M0 SQLite 热路径过重。

### `request_logs` 增补字段

用于列表和详情快速展示预算阻断原因：

- `budget_status TEXT`
  - `NULL`、`threshold_reached`、`exhausted`
- `budget_rule_id TEXT`
- `budget_scope_type TEXT`
- `budget_scope_id TEXT`
- `budget_action TEXT`
  - `warn`、`allow`、`block`
- `budget_reason_code TEXT`
- `budget_current_cost_cny NUMERIC`
- `budget_limit_cny NUMERIC`

设计理由：

- `request_logs` 是控制台调用排障主入口，预算阻断必须能不跳表也展示摘要。
- 详细多规则命中仍通过 `budget_evaluations` 查询。

### 日窗口口径

- `budget_date` 使用中国企业默认业务日：Asia/Shanghai。
- 聚合 `usage_ledger.created_at` 时，把 `budget_date` 转成 UTC 起止边界：
  - `start_utc = budget_date 00:00:00 Asia/Shanghai`
  - `end_utc = next_day 00:00:00 Asia/Shanghai`
- M0 不做租户自定义时区；后续可从租户配置扩展。

## API设计

### GET /budgets

用途：读取当前租户的预算规则与今日状态。

权限：admin/developer 可读；viewer 拒绝。M0 暂无 finance 角色，后续 RBAC 增强再映射。

Query：

- `scope_type` 可选：`tenant` 或 `virtual_key`
- `status` 可选：`active`、`disabled`、`all`

响应：

```json
{
  "budget_rules": [
    {
      "budget_rule_id": "br_xxx",
      "tenant_id": "tenant_default",
      "scope_type": "tenant",
      "scope_id": "tenant_default",
      "scope_label": "默认租户",
      "name": "租户日预算",
      "daily_budget_cny": "100.00",
      "warning_threshold_percent": 80,
      "exhausted_threshold_percent": 100,
      "exhausted_action": "block",
      "status": "active",
      "today_status": {
        "budget_date": "2026-07-09",
        "spent_cny": "72.40",
        "estimated_remaining_cny": "27.60",
        "usage_percent": 72.4,
        "status": "ok",
        "last_event_type": null
      },
      "created_at": "2026-07-09T10:00:00Z",
      "updated_at": "2026-07-09T10:00:00Z"
    }
  ],
  "request_id": "req_xxx",
  "trace_id": "trc_xxx"
}
```

### PATCH /budgets

用途：按 scope 创建或更新一条预算规则。请求不接收 `tenant_id`，后端从登录态恢复租户。

权限：admin 可写；developer/viewer 拒绝。

请求体：

```json
{
  "scope_type": "virtual_key",
  "scope_id": "vk_xxx",
  "name": "研发测试 Key 日预算",
  "daily_budget_cny": "50.00",
  "warning_threshold_percent": 80,
  "exhausted_action": "block",
  "status": "active"
}
```

字段规则：

- `scope_type` 必填，只允许 `tenant`、`virtual_key`。
- `scope_type='tenant'` 时 `scope_id` 可省略或传当前 `tenant_id`，服务端统一写入 `tenant_id`。
- `scope_type='virtual_key'` 时 `scope_id` 必须是当前租户下存在的 Virtual Key。
- `daily_budget_cny` 必须是大于 0 的十进制金额，最多 6 位小数。
- `warning_threshold_percent` M0 默认为 80，允许 1-99。
- `exhausted_action` 只允许 `alert_only` 或 `block`。
- `status` 只允许 `active` 或 `disabled`。

响应：同 `GET /budgets` 中的单条规则结构。

### GET /budget-status

用途：读取当前租户的今日预算状态，可按 Virtual Key 查询。

权限：admin/developer 可读；viewer 拒绝。

Query：

- `virtual_key_id` 可选：传入后返回租户级 + 该 Key 级预算状态。

响应：

```json
{
  "budget_date": "2026-07-09",
  "statuses": [
    {
      "budget_rule_id": "br_tenant",
      "scope_type": "tenant",
      "scope_id": "tenant_default",
      "scope_label": "默认租户",
      "spent_cny": "80.01",
      "budget_cny": "100.00",
      "usage_percent": 80.01,
      "status": "threshold_reached",
      "exhausted_action": "block",
      "next_action": "warn"
    }
  ],
  "request_id": "req_xxx",
  "trace_id": "trc_xxx"
}
```

### 数据面错误响应

当预算达到 100% 且 `exhausted_action='block'` 时，OpenAI-compatible API 返回：

```json
{
  "code": "gateway.budget_exhausted",
  "message": "Daily budget has been exhausted.",
  "request_id": "req_xxx",
  "trace_id": "trc_xxx",
  "suggestion": "Review budget settings or increase the daily budget before retrying.",
  "diagnosis": {
    "reason": "budget_exhausted",
    "retryable": false
  }
}
```

HTTP 状态：`402 Payment Required`。如果共享错误处理器当前不支持 402，则本 PRD 同步扩展错误码状态映射。

## 前端页面/交互设计

### 预算设置页

入口：

- 控制台侧边栏新增“预算设置”，位于“用量与成本”附近。
- 用量概览页顶部显示今日预算状态摘要，并提供跳转到预算设置页的入口。

可见性：

- admin：可查看、创建/更新、禁用规则。
- developer：只读，可查看预算状态与阻断原因。
- viewer：不显示入口；后端仍必须拒绝 API。

布局：

- 顶部为今日状态条：租户预算、Key 预算、用量百分比、剩余额度、策略。
- 下方为租户预算设置表单：
  - 日预算金额 CNY 输入框。
  - 80% 阈值数字输入，默认 80。
  - 100% 后动作选择：仅告警 / 阻断。
  - 状态开关：启用 / 禁用。
- 下方为 Virtual Key 预算表：
  - 每行展示 Key 名称、前缀、今日成本、预算、状态、策略。
  - admin 可编辑金额、阈值、策略和状态。
  - developer 只读。

交互：

- 保存后重新加载 `GET /budgets` 和 `GET /budget-status`。
- 错误卡片展示统一错误消息、`request_id` 和建议。
- 金额输入只允许非负小数；前端做基础提示，后端做最终校验。
- 所有展示字段转义，不渲染用户输入为 HTML。

### 用量概览联动

- `renderUsagePanel` 顶部增加预算摘要：
  - `ok`：显示“今日预算正常”。
  - `threshold_reached`：显示“已达到 80% 告警阈值”。
  - `exhausted`：显示“预算已耗尽，策略：仅告警/阻断”。
- 用量明细或调用日志中出现 `gateway.budget_exhausted` 时，错误诊断提示跳转预算设置页。

## 错误码设计

### 控制面

- `budget.rule_invalid`
  - HTTP 400
  - 预算金额、阈值、策略、状态或 scope_type 不合法。
- `budget.scope_not_found`
  - HTTP 404
  - 指定 Virtual Key 不属于当前租户或不存在。
- `budget.update_empty`
  - HTTP 400
  - PATCH 请求体缺少可更新字段。
- `budget.status_unavailable`
  - HTTP 503
  - 预算状态聚合失败，例如数据库不可用。

### 数据面

- `gateway.budget_exhausted`
  - HTTP 402
  - 已配置预算规则且达到 100%，策略为阻断。
- `gateway.budget_unavailable`
  - HTTP 503
  - 预算规则或状态读取失败，无法确认是否可放行。M0 采用 fail closed，避免治理保护静默失效。

### 事件

- `budget.threshold_reached`
  - 达到或超过 warning threshold，例如 80%。
- `budget.exhausted`
  - 达到或超过 100%。

事件 payload 只包含安全元数据：

- `tenant_id`
- `budget_rule_id`
- `scope_type`
- `scope_id`
- `budget_date`
- `threshold_percent`
- `spent_before_cny`
- `estimated_request_cost_cny`
- `projected_cost_cny`
- `budget_cny`
- `action_taken`
- `request_id`
- `trace_id`

不包含 Prompt、Response、Authorization、Provider Key、Virtual Key 明文。

## 安全与权限约束

- 所有预算表默认包含 `tenant_id`，所有查询第一过滤条件都是当前登录态或鉴权解析出的 `tenant_id`。
- `PATCH /budgets` 只允许 admin；M0 未实现 finance role，不能临时把 viewer 或 developer 当财务写权限。
- `GET /budgets` 与 `GET /budget-status` 允许 admin/developer；viewer 后端拒绝。
- 请求体不接受 `tenant_id`，即使传入也忽略或拒绝，避免跨租户写预算。
- Virtual Key 预算只能绑定当前租户下的 Key；后端必须通过 `tenant_id + virtual_key_id` 校验存在性。
- 数据面预算检查只读取安全元数据和 token/成本估算，不记录请求内容。
- 预算事件和评估记录不保存明文 Key，不保存 Prompt/Response。
- 预算阻断必须写入 `request_logs`，保留 `request_id/trace_id/error_code/budget_reason_code`。
- 预算规则读取失败采用 fail closed，返回 `gateway.budget_unavailable`，避免预算保护静默失效。
- 80%/100% 告警事件通过唯一约束去重，避免刷屏与重复 Webhook。

## 测试用例

### 控制面

1. 主流程：admin 创建租户级日预算，`GET /budgets` 返回规则和今日状态。
2. 主流程：admin 创建 Virtual Key 日预算，`GET /budget-status?virtual_key_id=` 返回租户 + Key 两类状态。
3. 权限：developer 可读不可写；viewer 读写都被后端拒绝。
4. 异常：非法金额、非法 scope_type、非法 exhausted_action 返回 `budget.rule_invalid`。
5. 异常：跨租户或不存在的 Virtual Key 返回 `budget.scope_not_found`。
6. 安全：请求体传入 tenant_id 不会改变规则租户归属。

### 数据面

1. 主流程：当今日成本达到 80% 后，下一次请求允许通过，写入 `budget.threshold_reached` 事件和预算评估记录。
2. 阻断：当今日成本 + 估算请求成本达到 100%，且策略为 `block`，返回 402 `gateway.budget_exhausted`，不调用上游。
3. 仅告警：当达到 100%，但策略为 `alert_only`，请求继续调用上游，并只写 `budget.exhausted` 事件。
4. 去重：同一规则同一天重复命中 80% 或 100%，`budget_alert_events` 只保留一条。
5. Virtual Key 隔离：Key A 达到预算不会阻断 Key B，租户级预算仍会影响全部 Key。
6. 日窗口边界：Asia/Shanghai 零点后重新计算今日成本，不沿用前一天告警。
7. 故障：预算规则读取失败返回 `gateway.budget_unavailable`，并写失败 `request_logs`。
8. 安全：日志、事件和响应中不出现 Prompt、Provider Key、Authorization 或 Virtual Key 明文。

### 前端

1. admin 可看到预算设置表单并保存租户级/Key 级预算。
2. developer 只能查看状态，保存按钮不可用。
3. viewer 不渲染预算入口。
4. 错误状态展示统一错误消息、建议和 `request_id`。
5. 恶意名称、Key 描述、错误消息会被转义，不产生 HTML 注入。

### 回归

1. `scripts/ci/check-comments.py` 必须通过。
2. PRD-023 后端聚焦测试、迁移测试、前端单测、控制台 lint 必须通过。
3. 后端全量 `pytest -q` 必须通过。

## 代码修改计划

1. 迁移：
   - 新增 `0015_budget_rules.sql`、`0015_budget_rules.down.sql`。
   - 更新迁移测试期望版本和新表/字段断言。
2. 共享错误：
   - 增加预算控制面和数据面错误码。
   - 增加预算耗尽诊断。
3. 控制面权限：
   - 新增 `require_budget_reader`、`require_budget_writer`。
4. 控制面预算模块：
   - 新增 `budgets/models.py`。
   - 新增 `budgets/service.py`。
   - 新增 `budgets/routes.py`。
   - 注册到 `app.py`。
5. 数据面预算模块：
   - 新增 `metahub_gateway/budgets/models.py`。
   - 新增 `metahub_gateway/budgets/service.py`。
   - 复用或抽取 token 与成本估算 helper。
   - 在 `openai_compatible.py` 上游调用前接入预算检查。
6. 调用日志：
   - `RequestLogContext` 或 `record_request_log` 支持预算摘要字段。
   - 控制面 request log DTO/查询/渲染增加预算信息。
7. 前端：
   - 新增 `console-web/src/lib/budgets.js`。
   - 新增 `console-web/src/features/budgets/renderBudgetPanel.js`。
   - 更新 `renderDeveloperStart.js`、`main.js`、导航和 CSS。
   - 用量概览展示预算摘要。
8. 测试：
   - `control-plane-api/tests/integration/test_budgets.py`。
   - `gateway-data-plane/tests/unit/test_budget_service.py`。
   - `gateway-data-plane/tests/integration/test_budget_guard.py`。
   - `console-web/tests/unit/budgets.test.mjs`。
   - 更新迁移测试和 request log 测试。
9. 文档：
   - 更新 README 当前完成列表、预算功能说明和测试命令。
   - 更新 `docs/development/directory-map.md`。
   - 新增 `docs/testing/PRD-023-tests.md`。

## 自检一

- 是否只做 M0 预算告警和阻断：是，不做部门分账、发票、余额、审批或自动降级。
- 是否所有业务数据带 tenant_id：是，三张新表与 request log 预算字段均按租户写入和查询。
- 是否不泄露密钥与 Prompt：是，预算检查只保存 scope、成本、阈值、request_id 和 trace_id。
- 是否后端权限覆盖：是，预算读写都设计后端依赖，viewer 不依赖前端隐藏。
- 是否失败请求可追踪：是，预算阻断和预算不可用都写 `request_logs`。

## 自检二

- 最大实现风险：预算检查需要估算请求成本，必须和 PRD-017 账本价格口径保持一致，避免“预算按一个价格挡、账本按另一个价格记”。
- 最大性能风险：每次请求聚合当天 Usage。M0 可以接受 SQLite 聚合；后续应把预算快照放 Redis 或预聚合表。
- 最大一致性风险：预算先估算、后真实账本，流式请求真实 usage 可能滞后。M0 以准入保护为主，真实成本仍以 usage_ledger 为准。
- 最大体验风险：规则和状态在同页太密。实现时预算状态放顶部摘要，规则表保持紧凑、可扫描。
