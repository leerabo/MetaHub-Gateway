# 租户、项目与 Virtual Key 三级预算 API

本文档对应 `PRD-052 - 租户/项目/Key 三级预算`，描述当前 Control API、Gateway
预算门禁与 `budget.scope_exceeded` Webhook 的公开开发合同。详细设计与边界见
[PRD-052 设计文档](../prd/PRD-052-tenant-project-key-three-level-budget.md)。本文不记录测试或
运行验收结论。

## 1. 能力边界

预算按 `tenant`、`project`、`virtual_key` 三层累计生效。已有 `application` 预算作为历史
兼容层继续保留，因此完整运行时顺序为：

```text
tenant -> project -> application（兼容） -> virtual_key
```

每条规则只作用于一个 `Environment`，不同 Environment 不共享额度。当前规则是
Asia/Shanghai 业务日、CNY 日预算，告警阈值可配置为 1..99，耗尽阈值固定为 100%。

| `scope_type` | `scope_id` | 作用范围 | 固定优先级 |
| --- | --- | --- | ---: |
| `tenant` | 当前登录租户 ID；创建时可省略 | 当前租户在指定 Environment 下的全部请求 | 0 |
| `project` | 当前租户 Project ID | 绑定该 Project 的请求 | 1 |
| `application` | 当前租户 Application ID | 历史兼容层；不属于 PRD-052 新增的三级范围 | 2 |
| `virtual_key` | Virtual Key 的不透明资源 ID，不是 Key 明文 | 仅该 Key 的请求 | 3 |

未绑定 Project 的 Key 只命中 `tenant + virtual_key`；服务端不会猜测 Project。Key 已绑定
Application 时，兼容链会包含其 Application 和该 Application 所属 Project。

响应中的 `budget_scope` 是值对象而不是授权凭据：

```json
{
  "budget_scope_id": "bsc_0123456789abcdef0123456789abcdef",
  "tenant_id": "tenant_example",
  "environment_id": "env_example_prod",
  "scope_type": "project",
  "scope_id": "project_example_billing",
  "scope_label": "结算项目",
  "priority": 1,
  "parent_scope_type": "tenant",
  "parent_scope_id": "tenant_example"
}
```

`budget_scope_id` 是由租户、Environment、scope 类型和资源 ID 生成的稳定不可逆引用。
调用方仍必须使用 Session 和 tenant-qualified API；不能把该 ID 当作权限令牌。

## 2. 认证、权限与通用合同

Control API 使用 Console Session Bearer Token。租户和操作者只能从 Session 恢复，query、
body 和路径参数都不能覆盖 `tenant_id` 或 actor。

| 接口 | `admin` | `developer` | `viewer` |
| --- | :---: | :---: | :---: |
| `GET /budgets` | 读 | 读 | 403 |
| `GET /budget-status` | 读 | 读 | 403 |
| `POST /budgets` | 写 | 403 | 403 |
| `PATCH /budgets` | 写 | 403 | 403 |
| `PATCH /budgets/{budget_rule_id}/mode` | 写 | 403 | 403 |

权限由后端执行。未授权调用方即使发送畸形 query 或 JSON，也先得到稳定的
`403 auth.permission_denied`，不能依赖前端隐藏入口。

所有预算响应使用 `Cache-Control: no-store`。统一中间件在响应头写入 `X-Request-ID` 和
`X-Trace-ID`；成功响应正文也包含 `request_id`、`trace_id`，错误正文使用同一调用链 ID。
客户端排障时应记录这两个 ID，不应记录 Authorization 或 Session Token。

请求示例中的 `<console-session-token>` 只是占位符；所有 `*_example` ID 和重复十六进制也只
用于说明格式，不代表真实租户、Key 或调用事实：

```http
Authorization: Bearer <console-session-token>
Content-Type: application/json
X-Request-ID: req_budget_example_01
X-Trace-ID: trc_budget_example_01
```

## 3. 严格 query 与 JSON 规则

预算路由在 RBAC 通过后执行严格 wire 解析：

- GET 只接受接口列出的 query 参数；未知参数、重复参数和 `tenant_id` 均返回
  `422 budget.rule_invalid`。非法 scope/status 也使用该错误码；Environment code、存在性与
  状态错误沿用下文的 `environment.*` 统一错误码。
- 写请求只接受 `application/json`，正文必须是非空 UTF-8 JSON object，最大 16 KiB。
- 任意层级重复 JSON key、未知顶层字段、过深或过多节点、控制字符、非法 UTF-8、空正文
  和非对象顶层值均被拒绝。
- body 不接受 `tenant_id`、actor、服务端派生字段或数据库字段。
- 金额最多六位小数，必须为普通十进制正数且不超过 `1000000000`；建议客户端使用 JSON
  string 发送金额，避免本地二进制浮点改写确认值。

预算规则写请求的允许字段如下：

| 字段 | 约束 |
| --- | --- |
| `scope_type` | 必填；`tenant/project/application/virtual_key` |
| `scope_id` | tenant 可省略；其他 scope 必填，必须属于当前租户 |
| `environment_id` | 推荐使用稳定 Environment ID |
| `environment` | 兼容 `dev/test/prod` code；与 `environment_id` 同传时必须一致 |
| `name` | 可选；去空白后 1..120 字符，省略时由服务端生成安全展示名 |
| `daily_budget_cny` | 新建规则必填；正数普通十进制，最多六位小数 |
| `warning_threshold_percent` | 可选整数 1..99，默认 80；布尔值不作为整数接受 |
| `exhausted_action` | `alert_only` 或 `block`，新建默认 `alert_only` |
| `status` | `active` 或 `disabled`，新建默认 `active` |

未显式提供 Environment 时，tenant、project、application 使用当前租户的默认 `dev`；
virtual_key 从该 Key 的权威绑定恢复 Environment。生产集成建议始终显式传
`environment_id`，避免依赖默认值。

## 4. 创建预算规则

### `POST /budgets`

该接口是 create-only，只允许 `admin`。同一
`tenant + environment + scope_type + scope_id` 已存在规则时返回
`409 budget.rule_conflict`，不会覆盖原规则；并发唯一键冲突也归一为同一错误码，不暴露
SQLite 文本。

```http
POST /budgets
Authorization: Bearer <console-session-token>
Content-Type: application/json
X-Request-ID: req_budget_create_01
X-Trace-ID: trc_budget_create_01

{
  "environment_id": "env_example_prod",
  "scope_type": "project",
  "scope_id": "project_example_billing",
  "name": "结算项目日预算",
  "daily_budget_cny": "500.000000",
  "warning_threshold_percent": 80,
  "exhausted_action": "block",
  "status": "active"
}
```

成功返回 HTTP 201：

```json
{
  "budget_rule": {
    "budget_rule_id": "br_0123456789abcdef0123456789abcdef",
    "tenant_id": "tenant_example",
    "environment_id": "env_example_prod",
    "environment": "prod",
    "environment_name": "生产环境",
    "scope_type": "project",
    "scope_id": "project_example_billing",
    "scope_label": "结算项目",
    "budget_scope": {
      "budget_scope_id": "bsc_0123456789abcdef0123456789abcdef",
      "tenant_id": "tenant_example",
      "environment_id": "env_example_prod",
      "scope_type": "project",
      "scope_id": "project_example_billing",
      "scope_label": "结算项目",
      "priority": 1,
      "parent_scope_type": "tenant",
      "parent_scope_id": "tenant_example"
    },
    "name": "结算项目日预算",
    "daily_budget_cny": "500",
    "warning_threshold_percent": 80,
    "exhausted_threshold_percent": 100,
    "exhausted_action": "block",
    "status": "active",
    "today_status": {
      "budget_rule_id": "br_0123456789abcdef0123456789abcdef",
      "environment_id": "env_example_prod",
      "environment": "prod",
      "environment_name": "生产环境",
      "scope_type": "project",
      "scope_id": "project_example_billing",
      "scope_label": "结算项目",
      "budget_scope": {
        "budget_scope_id": "bsc_0123456789abcdef0123456789abcdef",
        "tenant_id": "tenant_example",
        "environment_id": "env_example_prod",
        "scope_type": "project",
        "scope_id": "project_example_billing",
        "scope_label": "结算项目",
        "priority": 1,
        "parent_scope_type": "tenant",
        "parent_scope_id": "tenant_example"
      },
      "budget_date": "2026-07-27",
      "ledger_spent_cny": "0",
      "reserved_cny": "0",
      "effective_spend_cny": "0",
      "effective_remaining_cny": "500",
      "spent_cny": "0",
      "estimated_remaining_cny": "500",
      "budget_cny": "500",
      "usage_percent": 0.0,
      "status": "ok",
      "exhausted_action": "block",
      "next_action": "allow",
      "last_event_type": null,
      "block_status": null,
      "budget_block_id": null,
      "blocked_at": null
    },
    "active_block": null,
    "created_at": "2026-07-27T00:00:00Z",
    "updated_at": "2026-07-27T00:00:00Z"
  },
  "request_id": "req_budget_create_01",
  "trace_id": "trc_budget_create_01"
}
```

金额响应使用普通十进制字符串，不保证保留客户端输入的尾随零。

## 5. 更新预算规则

### `PATCH /budgets`

该接口保留历史兼容 upsert 语义，以
`environment + scope_type + scope_id` 定位规则：存在时更新，不存在时创建。新客户端应优先
使用 POST 创建、PATCH 更新；PATCH 创建新规则时同样必须提供 `daily_budget_cny`。

更新已有规则时仍需提交 scope 身份，并至少显式提供一个可编辑字段：`name`、
`daily_budget_cny`、`warning_threshold_percent`、`exhausted_action` 或 `status`。空更新返回
`422 budget.update_empty`。

```http
PATCH /budgets
Authorization: Bearer <console-session-token>
Content-Type: application/json
X-Request-ID: req_budget_update_01
X-Trace-ID: trc_budget_update_01

{
  "environment_id": "env_example_prod",
  "scope_type": "project",
  "scope_id": "project_example_billing",
  "daily_budget_cny": "750.000000"
}
```

成功返回 HTTP 200 和与 POST 相同的 `BudgetRuleResponse`。

预算 block 是当日粘性事实，不会因人工贷记、冲正或 reservation 波动自动释放。以下显式
规则变更只影响目标规则的 active block：

- `status=disabled`：释放目标规则 block。
- `exhausted_action=alert_only`：释放目标规则 block。
- 严格提额且新额度高于该规则正式已入账 Usage Charge：释放目标规则 block；下一次准入仍会
  重新叠加 outstanding reservation。

## 6. 查询规则列表

### `GET /budgets`

允许 `admin`、`developer`。可用 query：

| 参数 | 说明 |
| --- | --- |
| `scope_type` | `tenant/project/application/virtual_key` |
| `environment_id` | 当前租户 Environment ID |
| `environment` | 兼容 `dev/test/prod` code |
| `status` | `active/disabled/all`，默认 `all` |

不传 Environment 时返回当前租户所有 Environment 的匹配规则；同传 ID 和 code 时必须指向
同一 Environment。

```http
GET /budgets?environment_id=env_example_prod&status=active
Authorization: Bearer <console-session-token>
X-Request-ID: req_budget_list_01
X-Trace-ID: trc_budget_list_01
```

成功响应包含：

- `budget_rules`：规则、`budget_scope`、今日消费拆分、active block 和时间字段。
- `environment_summaries`：每个 Environment 按四种 scope 分列的规则和状态计数。
- `request_id`、`trace_id`。

`environment_summaries` 只聚合规则数量与 `ok/warning/exhausted/block` 计数，不把 tenant、
project、application、virtual_key 金额相加。同一 Usage 可能同时命中多层规则，跨层求和会
重复计算。

今日状态的重要字段：

| 字段 | 语义 |
| --- | --- |
| `ledger_spent_cny` | 正式 FinOps 已入账模型消费 |
| `reserved_cny` | 尚未结算的请求预留 |
| `effective_spend_cny` | `ledger_spent_cny + reserved_cny` |
| `effective_remaining_cny` | `max(budget - effective_spend, 0)` |
| `spent_cny` | 兼容字段，等于 `ledger_spent_cny` |
| `estimated_remaining_cny` | 兼容字段，等于 `effective_remaining_cny` |
| `status` | `ok/threshold_reached/exhausted` |
| `next_action` | `allow/warn/block` |

规则列表、正式账本、预留、block 和 Environment 摘要在同一个只读事务快照内物化。

## 7. 查询可信预算链

### `GET /budget-status`

允许 `admin`、`developer`。query 只允许：`environment_id`、`project_id`、
`application_id`、`virtual_key_id`。

```http
GET /budget-status?environment_id=env_example_prod
Authorization: Bearer <console-session-token>
X-Request-ID: req_budget_status_01
X-Trace-ID: trc_budget_status_01
```

服务端按当前 Session 的 `tenant_id` 恢复可信归属：

1. 传 `virtual_key_id` 时，从 Key 记录恢复 Environment、Project 和 Application。
2. 同时显式传 Project/Application/Environment 时，必须与 Key 绑定完全一致。
3. 只传 Application 时，从 Application 恢复 Project；显式 Project 必须一致。
4. 只传 Project 时，必须是当前租户资源。
5. 未传 Key 和 Environment 时使用当前租户默认 `dev` Environment。

跨租户或不存在的 Project、Application、Key、Environment 返回同形
`404 budget.scope_not_found`，不暴露其他租户对象是否存在。可信绑定冲突返回
`409 budget.scope_mismatch`，不会拼接不存在的 scope chain。

成功响应结构：

```json
{
  "budget_date": "2026-07-27",
  "statuses": [],
  "aggregate": {
    "aggregate_status": "unconfigured",
    "effective_action": "allow",
    "effective_remaining_cny": null,
    "limiting_rule_id": null,
    "limiting_scope": null,
    "conflict_policy": "all_applicable_caps_deny_wins_v1",
    "scope_chain": [
      {
        "budget_scope_id": "bsc_11111111111111111111111111111111",
        "tenant_id": "tenant_example",
        "environment_id": "env_example_prod",
        "scope_type": "tenant",
        "scope_id": "tenant_example",
        "scope_label": "示例租户",
        "priority": 0,
        "parent_scope_type": null,
        "parent_scope_id": null
      }
    ]
  },
  "request_id": "req_budget_status_01",
  "trace_id": "trc_budget_status_01"
}
```

`statuses` 有值时，每项使用与规则 `today_status` 相同的数据合同。`scope_chain` 表示资源的
可信归属链，即使其中某层没有预算规则，也可以出现在链中。

## 8. 累计 AND 与 limiting 规则

多层预算是累计 AND 门禁，不是“更具体 scope 覆盖父级”：

- 任一适用规则得到 `exhausted + block`，Gateway 在 Provider 前返回
  `402 gateway.budget_exhausted`。
- 某层 `alert_only` 只表示该层不阻断，不能解除另一层 `block`。
- 严重度固定为 `block > warn > allow`；同严重度的运行时主因按
  `tenant > project > application > virtual_key` 稳定选择。
- Gateway 只使用鉴权恢复的 tenant、Environment、Project、Application 和 Key；请求正文、
  metadata 或自报 Header 不参与预算归属。

`GET /budget-status` 的 `limiting_rule_id` 表示当前纯容量最小的 active 规则。选择顺序为：

1. `effective_remaining_cny` 对应的整数微元升序；
2. scope 固定优先级升序；
3. `budget_rule_id` 升序。

`effective_remaining_cny` 是全部适用 active 规则的最小非负余额。已有 active block 只决定
`aggregate_status` 和 `effective_action`，不会抢占纯容量 limiting 结果。该 limiting 结果不含
下一次请求估算，不能当作下一请求必然返回的阻断主因；Gateway 会加入冻结估算后独立确定
运行时 governing rule。

没有适用规则时，`aggregate_status=unconfigured`、`effective_action=allow`、
`effective_remaining_cny=null`，limiting rule 为空。

## 9. FinOps 消费与 reservation 口径

预算正式已入账消费只累计 `finops_ledger_entries` 中同时满足以下条件的分录：

```text
entry_type = usage_charge
direction  = debit
status     = posted
```

金额只使用 `normalized_cny_micros`，按 tenant、Environment、上海业务日和目标 scope 过滤。
比较、排序、累计和事件持久化均使用有界整数微元；API 边界才转换为普通十进制字符串。

以下财务分录不改变模型消费预算：`manual_debit`、`manual_credit`、`reversal`、
`gift_credit`、`trial_credit`。它们表达财务调整、冲正或赠送额度，不是 Provider Usage。

有效消耗为：

```text
effective_spend_cny = posted_usage_charge_cny + outstanding_reservation_cny
```

Gateway 在 `BEGIN IMMEDIATE` 事务中读取正式消费和 outstanding reservation、加入当前请求的
冻结价格估算、逐层判定并写 reservation。主备路由都可能计费时，准入估算覆盖主备最坏情况；
请求终态再按实际可能计费的上游尝试更新 outstanding 金额。

适用 scope 当日存在未定价、未归一 CNY、NULL 归一金额或损坏/越界金额时，系统失败关闭：

- Control 返回 `503 budget.status_unavailable`。
- Gateway 返回 `503 gateway.budget_unavailable`，且不调用 Provider。

未知金额不会按零元或免费处理。

## 10. 快捷切换耗尽模式

### `PATCH /budgets/{budget_rule_id}/mode`

仅 `admin`。规则按路径 ID 和当前 Session 的 `tenant_id` 联合查询；不存在或跨租户规则均
返回 `404 budget.rule_not_found`。

```http
PATCH /budgets/br_0123456789abcdef0123456789abcdef/mode
Authorization: Bearer <console-session-token>
Content-Type: application/json
X-Request-ID: req_budget_mode_01
X-Trace-ID: trc_budget_mode_01

{
  "mode": "alert_only"
}
```

body 只允许 `mode`，取值 `block` 或 `alert_only`；非法值返回
`422 budget.mode_invalid`。切换为 `alert_only` 只释放该规则的 active block，不释放其他
tenant、Project、Application 或 Key 规则的 block。成功返回 HTTP 200
`BudgetRuleResponse`。

## 11. `budget.scope_exceeded` Webhook

当某条规则在 Provider 前首次达到或超过当日上限时，Gateway 在预算事务中持久化 canonical
事件。事实按 `tenant + environment + budget_rule + budget_date` 去重；事务 commit 成功后才
允许进入 Webhook 投递队列。重复命中复用同一持久 `event_id`，不会创建第二条业务事实。

安全信封示例：

```json
{
  "event_id": "bsevt_0123456789abcdef0123456789abcdef",
  "event_type": "budget.scope_exceeded",
  "timestamp": "2026-07-27T00:00:00Z",
  "tenant_id": "tenant_example",
  "actor_id": null,
  "object_id": "bsc_0123456789abcdef0123456789abcdef",
  "trace_id": "trc_gateway_budget_01",
  "payload": {
    "budget_scope_event_id": "bse_0123456789abcdef0123456789abcdef",
    "budget_scope_id": "bsc_0123456789abcdef0123456789abcdef",
    "budget_rule_id": "br_0123456789abcdef0123456789abcdef",
    "environment_id": "env_example_prod",
    "scope_type": "project",
    "scope_id": "project_example_billing",
    "budget_date": "2026-07-27",
    "posted_cost_cny": "480",
    "reserved_cost_cny": "10",
    "estimated_request_cost_cny": "10",
    "projected_cost_cny": "500",
    "budget_cny": "500",
    "action_taken": "block",
    "request_id": "req_gateway_budget_01",
    "conflict_policy": "all_applicable_caps_deny_wins_v1"
  }
}
```

字段语义：

- `posted_cost_cny`：正式 posted Usage Charge。
- `reserved_cost_cny`：当时未结算 reservation。
- `estimated_request_cost_cny`：本次请求冻结估算；幂等重试已有 reservation 时为 0。
- `projected_cost_cny`：上述三项之和，且必须大于等于 `budget_cny`。
- `action_taken`：`block` 或 `allow`；`allow` 表示规则处于 `alert_only`。
- `request_id`、信封 `trace_id`：首次越限调用链。
- `actor_id=null`：运行时没有可信 Session 用户操作者。
- `object_id`：必须等于 payload 的 `budget_scope_id`。

该事件表示“正式入账 + 未结算预留 + 当前请求估算”已达到上限，不表示所有金额均已实际
消费。`budget.exhausted` 旧内部事实继续用于兼容诊断，但同一 canonical scope 事件不会用两个
事件名对外双发。

Webhook 精确拒绝额外 payload 字段和敏感键。事件、签名正文、Delivery 和结构化日志均不得
包含 Authorization、Session Token、Provider Key、Virtual Key 明文、Prompt、Response、
请求正文或上游原始响应。

## 12. 错误码

| HTTP | 错误码 | 场景 |
| ---: | --- | --- |
| 402 | `gateway.budget_exhausted` | 任一适用 `block` 规则耗尽，Provider 前拒绝 |
| 403 | `auth.permission_denied` | developer/viewer 写入或 viewer 读取 |
| 404 | `budget.scope_not_found` | Project/Application/Key scope 不存在或不属于当前租户；状态查询中的 Environment 不可见 |
| 404 | `environment.not_found` | 规则列表或规则写入指定的 Environment 不存在或不属于当前租户 |
| 404 | `budget.rule_not_found` | mode 路径规则不存在或属于其他租户 |
| 409 | `budget.rule_conflict` | POST 重复创建同 Environment/scope 规则 |
| 409 | `budget.scope_mismatch` | Project/Application/Key/Environment 可信归属冲突 |
| 409 | `environment.disabled` | 在已停用 Environment 创建或更新预算规则 |
| 422 | `budget.rule_invalid` | query、严格 JSON、scope、金额、阈值、动作或状态非法 |
| 422 | `budget.update_empty` | PATCH 没有可编辑字段 |
| 422 | `budget.mode_invalid` | mode body 非法 |
| 422 | `environment.code_invalid` | 兼容 Environment code 不是 `dev/test/prod` |
| 503 | `budget.status_unavailable` | Control 无法可靠读取正式账本、金额或数据库快照 |
| 503 | `gateway.budget_unavailable` | Gateway 无法可靠完成预算门禁，失败关闭 |

统一错误正文包含 `code`、`message`、`request_id`、`trace_id`、`suggestion` 和安全
`diagnosis`。底层 SQL、数据库路径、触发器名称、其他租户对象、密钥和请求/响应正文不会进入
公开错误。

```json
{
  "code": "budget.rule_conflict",
  "message": "Budget rule already exists for this scope.",
  "request_id": "req_budget_create_02",
  "trace_id": "trc_budget_create_02",
  "suggestion": "Refresh the budget list and update the existing rule instead.",
  "diagnosis": {
    "rule_id": "unknown_error",
    "category": "unknown",
    "reason": "未命中特定错误归因规则。",
    "suggestion": "使用 request_id 查看调用日志和上游安全摘要，必要时联系平台管理员。",
    "action": "检查请求日志、Provider 状态、模型别名配置和最近的错误事件。",
    "severity": "warning",
    "retryable": false,
    "matched_by": "unknown",
    "safe_summary": "budget.rule_conflict http_status=409"
  }
}
```

`diagnosis` 的具体本地化文案由统一诊断注册表生成；客户端应以稳定 `code` 和 HTTP 状态做
程序分支，以 `request_id`、`trace_id` 做排障关联。

## 13. 数据库与集成注意事项

迁移 `0042_three_level_budgets.sql` 依赖已应用的 0041 FinOps 账本，主要增加：

- 不可变 `budget_scope_events`，金额以 INTEGER 微元保存并校验加法关系；
- posted Usage Charge 的 tenant/project/application/virtual_key 部分索引；
- `budget_rules`、alert、evaluation、block、reservation、scope event 的租户、Environment、
  Rule 与 scope 一致性触发器；
- 规则 ID 与 canonical scope event 的不可变约束。

数据库约束是服务层 tenant-qualified 校验后的第二道防线，不替代 Session、RBAC 和可信归属
解析。应用侧不得捕获约束错误后降级为无预算或跨租户重试，也不得把 SQLite 原始错误文本
返回给客户端。
