# PRD-033 - 预算耗尽阻断与恢复

## 文档状态

- 阶段：M0.5-W3/4
- 优先级：P0
- 当前状态：已实现并通过全量自动化与本地运行门禁
- 依赖：PRD-023 预算基础、PRD-024 限流基础、PRD-030 Environment、PRD-032 用量与成本归一化
- 产品边界：MVP 只做日预算告警、硬阻断和管理员直接恢复，不做提额审批流、自动低价模型降级或复杂预算快照分发

## 需求理解

PRD-023 已实现租户、Project、Application、Virtual Key 四类日预算规则，并在数据面根据“已入账成本 + 本次请求预估成本”执行 80% 告警和 100% 硬阻断。PRD-033 需要补全阻断后的生命周期和恢复闭环：

1. 预算规则配置为 `block` 且预计成本达到 100% 时，在调用 Provider 前阻断请求并返回统一 `gateway.budget_exhausted`。
2. 首次阻断必须创建可审计的 `BudgetBlock`，重复被拒请求不能产生多个 active block 或重复 blocked 事件。
3. 管理员可通过 `PATCH /budgets/{id}/mode` 在 `block` 与 `alert_only` 间切换；也可通过现有预算编辑入口提高日额度。
4. 当规则切为仅告警、日预算提高到当前已入账成本之上、规则被停用，或新业务日开始时，active block 必须释放并记录 `budget.unblocked`。
5. 规则变更直接写权威数据库；当前 MVP 数据面每次准入读取 active 规则，因此无需等待缓存 TTL，下一次请求立即恢复，满足“一分钟内恢复”。
6. 阻断与恢复状态必须按 `tenant_id + environment_id + budget_rule_id` 隔离，并保留 request_id、trace_id、操作者、原因和金额摘要。

## 涉及模块

- `infra/migrations/0023_budget_blocks.sql`：新增阻断生命周期与事件审计表。
- `control-plane-api/src/metahub_control/budgets/`：按规则 ID 切换模式、规则变更后释放 active block、返回阻断状态。
- `gateway-data-plane/src/metahub_gateway/budgets/`：首次硬阻断幂等创建 `BudgetBlock`，新业务日/规则恢复时关闭陈旧 block。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：登记 `budget.blocked`、`budget.unblocked` 事件类型。
- `console-web/src/lib/budgets.js`：新增模式切换客户端。
- `console-web/src/features/budgets/renderBudgetPanel.js`：展示阻断提示、恢复状态和管理员快捷模式操作。
- 预算控制面、数据面、前端及迁移测试。

## 数据表/字段设计

### `budget_blocks`

每一行表示一次“阻断开始到解除”的生命周期，所有业务数据带 `tenant_id`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `budget_block_id` | TEXT PK | `bb_` 前缀唯一 ID |
| `tenant_id` | TEXT NOT NULL | 租户隔离键 |
| `environment_id` | TEXT NOT NULL | Environment 隔离键 |
| `budget_rule_id` | TEXT NOT NULL | 命中的预算规则 |
| `scope_type/scope_id` | TEXT | 阻断维度快照 |
| `budget_date` | TEXT NOT NULL | Asia/Shanghai 业务日 |
| `status` | TEXT | `active/released` |
| `blocked_cost_cny` | NUMERIC | 首次阻断时预计成本 |
| `budget_cny` | NUMERIC | 首次阻断时规则额度 |
| `blocked_request_id/blocked_trace_id` | TEXT | 首次阻断调用链 |
| `blocked_at` | TEXT | 首次阻断 UTC 时间 |
| `release_reason` | TEXT NULL | `budget_raised/mode_alert_only/rule_disabled/below_threshold/business_day_ended` |
| `released_by` | TEXT NULL | 管理员 user_id；数据面自动释放为空 |
| `release_request_id/release_trace_id` | TEXT NULL | 恢复操作调用链 |
| `released_at` | TEXT NULL | 解除 UTC 时间 |
| `created_at/updated_at` | TEXT | 审计时间 |

使用部分唯一索引保证同一租户、规则、业务日最多一个 `active` block；释放后若再次耗尽，可创建新的生命周期。外键关联 Tenant、Environment、BudgetRule 和可选 User。

### `budget_block_events`

保存 `budget.blocked/budget.unblocked` 事件事实，作为后续异步 Webhook 投递的可靠来源：

- `budget_block_event_id`、`event_id`、`tenant_id`、`environment_id`
- `budget_block_id`、`budget_rule_id`、`event_type`
- `actor_id`、`request_id`、`trace_id`
- `reason_code`、`current_cost_cny`、`budget_cny`、`created_at`

同一 block 的 blocked/unblocked 各最多一条。事件不保存 Prompt、Response、Authorization、Virtual Key 明文或 Provider Key。

### `budget_reservations`

Provider 调用前按每条命中规则原子预留本次估算成本：

- `budget_reservation_id`、`tenant_id`、`environment_id`、`budget_rule_id`
- `scope_type/scope_id`、`budget_date`、`request_id/trace_id`
- `reserved_cost_cny`、`created_at`

Gateway 使用 `BEGIN IMMEDIATE` 串行“读取 Usage/未结算预留、判断、写预留”事务，防止并发请求同时看到旧余额后集体放行。聚合时只统计尚无同 request_id Usage Ledger 的预留：Usage 成功后真实成本自然替代预留；账本暂时写失败时预留继续保守占用预算。Provider 建连前直接失败会清理预留，流中异常则写实际或估算 Usage。

### 现有字段

不修改 `budget_rules.exhausted_action` 语义；`PATCH /budgets/{id}/mode` 是受限更新入口。`usage_ledger.cost_cny` 仍是已发生成本权威口径，BudgetBlock 不参与账单计算。

## API 设计

### `PATCH /budgets/{budget_rule_id}/mode`

权限：仅 `admin`，后端通过登录态 tenant_id 查找规则，跨租户 ID 统一返回 `budget.rule_not_found`。

请求：

```json
{
  "mode": "alert_only"
}
```

`mode` 仅允许 `block/alert_only`。响应沿用统一 `BudgetRuleResponse`，并在规则 DTO 的 `active_block` 字段返回当前 active block 或 null，响应携带 request_id、trace_id。

事务语义：

1. 锁定并读取当前租户规则。
2. 更新 `exhausted_action`、`updated_by/updated_at`。
3. 若切为 `alert_only`，释放该规则全部 active block，写 `budget.unblocked` 事件。
4. 提交后记录安全结构化日志；Webhook 订阅类型白名单允许 blocked/unblocked。

### `PATCH /budgets`

保留现有 upsert 兼容接口。更新额度或状态后执行恢复判断：

- `status=disabled`：释放 active block，原因 `rule_disabled`。
- 新额度大于当前业务日已入账成本：释放 active block，原因 `budget_raised`。
- 新额度仍不高于已入账成本且模式为 block：保留 active block。

### `GET /budgets` 与 `GET /budget-status`

规则 DTO 增加可选 `active_block`；状态 DTO 增加 `block_status` 和 `budget_block_id`，用于阻断提示，不返回敏感字段。

## 前端页面/交互设计

- 复用“预算告警”页面，不新增导航和营销式页面。
- 已阻断规则使用高对比状态标记，显示阻断时间、业务日、金额与安全原因，不展示 Key 明文。
- admin 在规则行使用模式下拉菜单切换“阻断/仅告警”；切换后刷新规则和状态。
- 提额继续使用现有预算编辑表单；保存成功后显示“预算已更新，阻断状态已重新评估”。
- developer 只读查看状态，viewer 不显示入口；后端权限仍是唯一安全边界。
- Gateway 失败沿用统一错误展示，明确建议“提高预算或切换为仅告警”，并保留 request_id/trace_id。

## 错误码设计

| 错误码 | HTTP | 场景 |
|---|---:|---|
| `gateway.budget_exhausted` | 402 | active block 规则命中或预计成本达到 100% |
| `gateway.budget_unavailable` | 503 | 预算规则/阻断状态读取或写入失败，数据面失败关闭 |
| `budget.rule_not_found` | 404 | 规则不存在或不属于当前租户 |
| `budget.mode_invalid` | 422 | mode 非 `block/alert_only` |
| `budget.status_unavailable` | 503 | 控制面预算状态或恢复事务失败 |
| `auth.permission_denied` | 403 | developer/viewer 尝试切换模式或修改额度 |

所有错误由统一异常层补充 request_id、trace_id、suggestion；不返回 SQL、密钥或原始请求内容。

## 安全与权限约束

1. 管理 API 从登录态获取 tenant_id，路径中的 rule ID 必须再次按 tenant_id 查询。
2. Project、Application、Virtual Key scope 继续验证同租户和同 Environment 归属。
3. 数据面在 Provider 调用前执行预算 Guard；前端隐藏按钮不构成权限控制。
4. active block 创建和释放必须事务化、幂等，避免多实例并发产生重复状态或重复事件。
5. BudgetBlock、事件和 reservation 使用归属触发器验证 tenant/environment/rule 三元组，不能拼接其他租户或 Environment 的单列外键。
6. BudgetBlock 只保存 ID、金额、原因和调用链元数据，不保存 Prompt、Response、Authorization 或任何 Key 明文。
7. 预算数据库异常采用 fail-closed，失败请求仍写 request_logs 或安全错误日志；账本降级由 reservation 保守兜底。
8. Provider 自报成本不覆盖内部 PricePlan CNY 成本，预算只使用内部 PricePlan 成本与未结算 reservation。

## 测试用例

### 主流程

1. 租户/Project/Application/Virtual Key 预算达到 100% 且 mode=block 时，Provider 未被调用，返回 402 与 `gateway.budget_exhausted`。
2. 首次阻断创建一条 active BudgetBlock 与一条 `budget.blocked` 事件；并发/重复拒绝不重复创建。
3. admin 切换 `alert_only` 后 active block 释放，写 `budget.unblocked`，下一次请求立即放行。
4. admin 提高额度到已入账成本以上后释放 block，下一次请求在一分钟内放行。
5. 规则停用后释放 block；再次启用且仍耗尽时可创建新 block。
6. 跨业务日旧 block 自动释放，新业务日按新用量重新判断。
7. 控制台显示 active/released 状态，并允许 admin 快捷切换模式。
8. 并发请求只能在预算允许范围内原子预留，不能集体突破硬预算；Usage 入账后不重复计费。
9. 流式请求解析最终 usage chunk；缺失 usage、客户端断连或流中异常时写安全估算 Usage。

### 异常、权限与安全

1. developer/viewer 调用 mode API 返回后端 403。
2. 跨租户 rule ID 返回 404，不能观察其他租户规则是否存在。
3. 非法 mode、空 body 返回统一 422。
4. BudgetBlock 持久化失败时返回 `gateway.budget_unavailable`，不调用 Provider。
5. blocked/unblocked 日志与事件不包含 Virtual Key、Provider Key、Authorization、Prompt 或 Response。
6. 重复模式更新幂等：没有 active block 时不生成虚假 unblocked 事件。
7. 迁移 up/down、空库迁移、种子幂等和业务表 tenant_id 门禁通过。
8. 恶意跨租户/Environment 组合插入 block、event 或 reservation 被数据库触发器拒绝。

## 代码修改计划

1. 新增 0023 up/down 迁移和迁移测试，建立 BudgetBlock 与事件审计结构。
2. 扩展控制面 DTO、规则查询和 `PATCH /budgets/{id}/mode`；规则修改后事务内执行恢复判断。
3. 扩展数据面 Guard，幂等创建 active block、复用现有错误码并释放过期 block。
4. 登记 blocked/unblocked Webhook 类型并记录安全结构化事件。
5. 更新预算客户端、页面状态和交互事件绑定。
6. 增加控制面、数据面、前端主流程及异常/权限/安全测试。
7. 更新 README、开发与测试记录，执行编译、中文注释、前后端全量门禁和本地运行验证。

## 第一轮设计自审

结论：发现三处状态语义缺口，修订后通过。

1. 仅在每次请求重新比较 projected cost 会导致“大请求触发阻断后，小请求又被放行”；已明确当日 active BudgetBlock 是硬阻断事实，Guard 在成本估算前优先拒绝，直到管理员恢复或业务日结束。
2. 首次被拒请求的 projected cost 不会进入 Usage Ledger，恢复判断不能拿该预计值冒充已发生成本；提额时只比较可信的 `usage_ledger.cost_cny`，释放后若下一请求仍会超额，可按新额度重新创建 block。
3. 在数据面同步投递 Webhook 会放大阻断请求延迟并违反热路径约束；已将 `budget_block_events` 定义为可靠事件事实，并记录同名结构化事件、加入订阅白名单。MVP 不在阻断热路径同步 HTTP 投递，后续异步事件投递器消费该表，不影响本 PRD 状态一致性。
4. active block 必须先按当前 `budget_date` 查询；进入新业务日时事务内释放旧 block，再按当日 Usage 重新评估，避免跨日误阻断。

## 第二轮设计自审

结论：通过，可以编码。

1. 数据模型具备 tenant_id、environment_id、rule ID、scope 快照和完整开始/释放调用链，能够支持租户隔离与审计。
2. 部分唯一索引加 `INSERT OR IGNORE` 保证并发首次阻断幂等；blocked/unblocked 事件唯一约束阻止重复通知事实。
3. mode API 只能更新动作，不接受 tenant_id、scope 或金额，跨租户规则统一 404；提额仍复用现有经过 scope 归属校验的接口。
4. 恢复事务同时更新规则、关闭 active block 和写 unblocked 事件，不会出现“规则已放行但控制台仍显示 active”的中间状态。
5. 数据面读取权威数据库且没有预算缓存，恢复在提交后的下一请求生效，明确满足 1 分钟要求；未来引入快照时必须保留版本失效机制。
6. 方案没有引入审批流、Redis 复杂快照、自动降级、硬件、MCP 或复杂出境能力，符合 M0.5 边界。

## 实现结果

1. 新增 0023 up/down 迁移，建立 `budget_blocks`、`budget_block_events`、`budget_reservations`、active 部分唯一索引、归属触发器及查询索引。
2. 新增共享预算阻断生命周期模块，控制面和数据面使用同一套幂等创建、释放和事件写入逻辑。
3. 数据面首次达到 100% 时创建 active block；后续请求即使预计成本降低也继续阻断，每次失败仍写独立预算评估和 request_logs。
4. 新增 `PATCH /budgets/{budget_rule_id}/mode`，仅 admin 可切换 `block/alert_only`；跨租户规则与不存在规则统一 404。
5. 原 `/budgets` 提额、切换仅告警或停用规则时，会在同一事务释放 active block；下一 Gateway 请求无需重启即可恢复。
6. 新业务日首次请求自动释放旧 block；防御性处理手工改库造成的 alert_only 残留 block。
7. 控制台展示 active block、首次阻断时间和生命周期 ID，admin 可在规则行切换模式，developer 只读、viewer 隐藏。
8. blocked/unblocked 事件事实、结构化日志和 Webhook 类型白名单均不包含 Prompt、Response、Authorization 或任何 Key 明文。
9. 数据面使用 `BEGIN IMMEDIATE` 和持久 reservation 原子串行预算准入；账本写失败仍保守计入，Provider 前失败则清理预留。
10. 流式 SSE 增量解析 Provider usage，缺失时只按请求/输出字符计数估算，正常、断连和流中异常均进入 Usage Ledger。

测试结果详见 `docs/testing/PRD-033-tests.md`。
