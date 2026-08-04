# PRD-052 - 租户/项目/Key 三级预算

## 文档状态

- PRD：`PRD-052`
- 名称：租户/项目/Key 三级预算
- 里程碑：`M1-企业POC`
- 优先级：`P1`
- 页面：`PAGE-076 预算中心`
- API：`POST /budgets`、`GET /budgets`、`PATCH /budgets`；兼容既有 `GET /budget-status` 与 `PATCH /budgets/{id}/mode`
- 核心对象：`BudgetRule`、`BudgetScope`
- 事件：`budget.scope_exceeded`
- 验收：`TC-0103`、`TC-0104`
- 依赖：PRD-051 FinOps 正式账本
- 当前状态：已完成设计、两轮编码前自审、实现、聚焦测试、0042 迁移及真实 Runtime/Browser/停服终态验收

## 1. 需求来源与结论

### 1.1 Excel 冻结需求

本设计已逐项核对以下权威行：

- `01_PRD总表!A53:Q53`
- `02_M0-M1核心开发!A53:Q53`
- `04_页面清单!A77:H77`
- `05_API接口清单!A246:H249`
- `06_数据对象!A3:F3`、`A73:F73`
- `07_Webhook事件!A59:H59`
- `08_验收用例!A104:J105`

冻结范围如下：

1. 支持 `tenant/project/virtual_key` 三层 `BudgetScope`。
2. 明确预算优先级和冲突规则，不能由前端自行推断。
3. 在预算中心聚合展示多层状态和当前有效约束。
4. 补齐 `POST/GET/PATCH /budgets`。
5. 首次越过某层上限时产生 `budget.scope_exceeded`。
6. Project A 预算耗尽只影响 Project A；租户预算耗尽影响同一 Environment 下该租户全部项目和 Key。
7. 不做部门组织树。

### 1.2 架构约束

架构文档第 8.4 节将预算维度定义为租户、部门、项目、Virtual Key、模型别名，并要求 MVP 先完成告警和硬阻断。PRD-052 只落地 Excel 冻结的租户、项目、Key 三级，不提前实现部门、模型别名预算、审批提额、自动降级或 Redis 预算快照。

架构文档同时要求：

- 数据面先鉴权，再执行预算检查，最后才允许调用 Provider。
- Usage、Budget、Webhook 均按 `tenant_id` 隔离。
- 控制面不承载高频模型流量。
- 高频热路径最终可迁移到快照/缓存；当前 SQLite 企业 POC 继续使用原子数据库事务，不在本 PRD 引入分布式基础设施。

### 1.3 现有能力与真实缺口

现有 PRD-023、PRD-029、PRD-030、PRD-033 已提供：

- `budget_rules`，支持 `tenant/project/application/virtual_key` 与 Environment 隔离。
- Gateway 按 `tenant -> project -> application -> virtual_key` 加载适用规则。
- `BEGIN IMMEDIATE` 串行“读取已用金额、判断、写 reservation”，防止并发共同突破额度。
- 80% 告警、100% `alert_only/block`、BudgetBlock 生命周期和恢复。
- admin 写、admin/developer 读、viewer 后端拒绝。
- PAGE-076 的基础规则表、状态卡和模式切换。

这些能力不能重复重写，但尚未满足 PRD-052：

1. 只有 `GET/PATCH /budgets`，缺少 create-only 的 `POST /budgets`。
2. 三级规则的 AND 关系、主因优先级和摘要规则没有形成稳定合同。
3. `GET /budget-status` 分别校验 Project 和 Key 是否存在，却未验证它们互相归属；同租户不相关组合可能被错误聚合。
4. 预算已入账金额仍直接汇总 `usage_ledger.cost_cny`，没有消费依赖 PRD-051 的正式账本。
5. 缺少 `BudgetScope` value-object DTO、适用 scope chain 和有效约束聚合。
6. 缺少可持久去重、可签名投递的 `budget.scope_exceeded` 事件。
7. 数据库只部分校验 block/reservation 的 tenant/environment/rule 归属，未全面校验 `scope_type/scope_id` 与父规则一致。
8. 现有专项测试主要证明 tenant/Key；没有完整证明“Project A 阻断而 Project B 放行”和“Tenant 同时阻断 A/B”。

## 2. 范围与非目标

### 2.1 本期范围

- 三层固定 scope：`tenant`、`project`、`virtual_key`。
- 现有 `application` scope 作为 PRD-029 兼容能力保留，不删除数据、不收窄 API、不改变既有顺序。
- 日预算、Asia/Shanghai 业务日、CNY、80% 告警、100% 耗尽。
- 固定层级冲突策略、聚合状态、限制 scope 和 scope chain。
- 正式 FinOps Usage Charge 与未结算 reservation 的一致消费口径。
- create-only POST、兼容 PATCH、后端 RBAC、租户与归属校验。
- 持久 `budget.scope_exceeded` 事实和提交后 Webhook 投递。
- PAGE-076 桌面端与移动端真实验收。

### 2.2 明确不做

- 部门组织树、部门预算和组织继承。
- 月/季/年预算、多币种预算、租户自定义时区。
- FinanceAdmin 新角色或跨租户财务角色；当前“财务”由 admin 权限映射承接。
- 自动切低价模型、审批提额、付款余额、发票和支付渠道。
- 子级覆盖父级、可配置优先级表达式或任意冲突 DSL。
- Redis/消息队列预算快照、PostgreSQL 行锁或分布式计数器。
- 删除现有 Application 预算。

## 3. 冲突与优先级合同

### 3.1 适用规则

Gateway 只从鉴权得到的可信上下文恢复：

- `tenant_id`
- `environment_id`
- `project_id`（可空）
- `application_id`（兼容，可空）
- `virtual_key_id`

请求正文、metadata、Header 自报的 Project 或 Scope 均不参与预算归属。

适用规则集合为：

```text
tenant(tenant_id)
+ project(auth.project_id, 若存在)
+ application(auth.application_id, 若存在，兼容)
+ virtual_key(auth.virtual_key_id)
```

每条规则还必须匹配同一 `tenant_id + environment_id` 且 `status='active'`。

### 3.2 固定冲突规则

1. 多层规则是累计 AND 门禁，不是“更具体的 scope 覆盖更宽的 scope”。
2. 任一适用规则得到 `exhausted + block`，整个请求在 Provider 前拒绝。
3. 某层 `alert_only` 只能决定该层不阻断，不能解除另一层的 `block`。
4. 严重度固定为 `block > warn > allow`。
5. 多条同严重度结果需要一个稳定主因时，顺序为 `tenant > project > application(兼容) > virtual_key`。
6. 上述顺序只决定首个阻断、request log 主摘要和页面主因，不改变“所有适用额度都必须满足”的 AND 语义。
7. 适用 active 规则的最小非负余额是请求链的 `effective_remaining_cny`。
8. 同 Environment 的租户规则影响租户内所有 Project/Key；Project 规则只匹配绑定该 Project 的 Key；Key 规则只匹配该 Key。
9. 未绑定 Project 的 Key 只命中 tenant + virtual_key；绝不猜测 Project。
10. 如果 Project、Application、Key 查询参数互相矛盾，控制面返回稳定冲突错误，不拼接一条不存在的 scope chain。

页面的 `limiting_rule` 与运行时 `governing_rule` 是两个不同概念：

- `limiting_rule` 是不带下一请求估算时，当前最先限制可用容量的规则；用于预算中心展示。
- `governing_rule` 是 Gateway 把当前请求冻结估算加入后，按固定 scope 顺序得到的首个 block；用于 402、request log 和失败诊断。
- 页面不得把 `limiting_rule` 宣称为未知下一请求必然返回的错误主因。

### 3.3 准入伪代码

```text
BEGIN IMMEDIATE
rules = load_applicable_rules(tenant, environment, project, application, key)
estimate = frozen_price_estimate(primary + possible_fallback)
for rule in tenant -> project -> application -> key:
    spend = posted_finops_usage_charge(rule) + outstanding_reservation(rule)
    if spend 包含无法归一为 CNY 的 Usage:
        ROLLBACK; fail closed
    decision = evaluate(spend + estimate, rule)
    if decision == block:
        persist idempotent block/evaluation/legacy alert/scope exceeded event
        remove reservations created earlier in this transaction
        COMMIT
        raise gateway.budget_exhausted with stable governing rule
    persist reservation and optional warn/scope exceeded facts
COMMIT
enqueue newly committed scope exceeded events
call Provider
```

## 4. FinOps 金额口径

### 4.1 正式已入账金额

PRD-052 不再分别从 `usage_ledger.cost_cny` 计算控制面和数据面预算。已入账模型消费统一来自 `finops_ledger_entries`：

```sql
entry_type='usage_charge'
AND direction='debit'
AND status='posted'
```

只累计 `normalized_cny_micros`。查询逐行读取并在 Python 中以有界整数累加，避免 SQLite `SUM` 在大数据集上溢出；仅在 API 边界以 `Decimal / 1000000` 转为 CNY。
任何比较、排序和累计都禁止 binary float；逐行还必须验证 `unpriced`、非 `normalized`、NULL 以及 Usage 上的 `not_applicable` 并失败关闭。`legacy_amount_only` 仅在已有可信 normalized CNY 时计入。

既有 `budget_rules.daily_budget_cny`、`budget_reservations.reserved_cost_cny/outstanding_cost_cny` 仍是历史 NUMERIC 字段，读取时使用同一个严格转换函数：

1. 先以 `Decimal(str(database_value))` 解析，拒绝 NaN、Infinity、负数和科学计数坏值。
2. 金额必须等于其六位小数量化值，禁止静默四舍五入历史坏数据。
3. 乘以 `1_000_000` 后必须是整数，且位于 `0..1_000_000_000_000_000` 微元范围。
4. 任一转换失败，Control 返回 `budget.status_unavailable`，Gateway 返回 `gateway.budget_unavailable`。

比较、排序、事件持久化和聚合都使用整数微元；仅在 API/日志边界转换为普通十进制字符串。

Scope 过滤：

- tenant：`tenant_id + environment_id + created_at window`
- project：再加 `project_id`
- application：再加 `application_id`，仅兼容现有能力
- virtual_key：再加 `virtual_key_id`

### 4.2 不计入模型消费预算的分录

以下正式分录不进入运行时消费预算：

- `manual_debit`
- `manual_credit`
- `reversal`
- `gift_credit`
- `trial_credit`

原因：这些记录表达账务调整、赠送或试用余额，不是 Provider Usage。若用它们改变热路径预算，人工贷记可能意外解封硬预算，人工借记也可能阻断没有模型调用的 Project。

### 4.3 未知金额失败关闭

当适用 scope 当日存在以下任一 Usage Charge 时，CNY 日预算无法可靠计算：

- `normalized_cny_micros IS NULL`
- `cny_normalization_status!='normalized'`
- `price_evidence_status='unpriced'`

Gateway 返回 `gateway.budget_unavailable`，且不调用 Provider；Control API 返回 `budget.status_unavailable`。不能把未知金额按 0 或免费处理。

### 4.4 Reservation

预算有效消耗为：

```text
effective_spend_cny = ledger_spent_cny + outstanding_reserved_cny
```

`budget_reservations` 的“已有 Usage 接管预留”判断改为同租户、同 request 的正式 `usage_charge` 是否存在。这样正式账本是唯一结算存在性边界，且不会把 Usage 与 Formal Entry 双计。

准入和状态聚合使用 `posted + outstanding reservation`；PRD-033 的提额恢复继续只比较正式已入账 `usage_charge`。提额释放 active block 后，下一次准入会重新把 outstanding reservation 纳入判断，不能把预计成本伪装成已发生费用。

## 5. 数据对象和数据库设计

### 5.1 `BudgetScope` value object

不新建 `budget_scopes` 主表。Tenant、Project、Virtual Key 已分别有权威主表；复制一份 scope 主数据会在 Key 改绑 Project 时产生漂移。`BudgetScope` 作为 API 与领域 value object：

| 字段 | 类型 | 说明 |
|---|---|---|
| `budget_scope_id` | string | 由 tenant/environment/scope_type/scope_id 规范化后生成的稳定不可逆标识 |
| `tenant_id` | string | 当前登录态租户 |
| `environment_id` | string | 预算环境边界 |
| `scope_type` | enum | `tenant/project/application/virtual_key`；Application 为既有兼容值 |
| `scope_id` | string | 租户、项目或 Key 的不透明 ID，不含 Key 明文 |
| `scope_label` | string | 经过服务端解析的安全展示名 |
| `priority` | integer | tenant=0、project=1、application=2、virtual_key=3 |
| `parent_scope_type` | string/null | 完整兼容链的直接父级；Key 优先为 Application，其次 Project，最后 Tenant |
| `parent_scope_id` | string/null | 对应父级不透明 ID |

`budget_scope_id` 的算法冻结为：

```text
canonical = "metahub:budget-scope:v1\\0"
          + len_utf8(tenant_id) + ":" + tenant_id
          + len_utf8(environment_id) + ":" + environment_id
          + len_utf8(scope_type) + ":" + scope_type
          + len_utf8(scope_id) + ":" + scope_id
budget_scope_id = "bsc_" + sha256(canonical_utf8).hexdigest()[0:32]
```

长度使用 UTF-8 字节数，字段保持原始权威 ID，不做 locale 大小写转换；算法没有 HMAC 密钥，不受密钥轮换影响。`budget_scope_id` 只用于稳定引用，不替代 tenant-qualified 查询，也不作为授权凭据。

### 5.2 现有 `budget_rules`

不重建、不删除既有字段。继续使用：

- `budget_rule_id`
- `tenant_id`
- `environment_id`
- `scope_type/scope_id`
- `daily_budget_cny`
- 告警/耗尽阈值、动作、状态、操作者与时间
- `UNIQUE(tenant_id, environment_id, scope_type, scope_id)`

0042 增加 INSERT/归属 UPDATE 触发器，校验：

- Environment 属于同租户。
- tenant scope 必须 `scope_id=tenant_id`。
- project scope 必须指向同租户 Project。
- virtual_key scope 必须指向同租户、同 Environment Key。
- application scope 继续校验同租户 Application，保持历史兼容。

### 5.3 新表 `budget_scope_events`

该表保存 canonical `budget.scope_exceeded` 事实，不替换既有 `budget_alert_events.event_type='budget.exhausted'`：

| 字段 | 约束 | 说明 |
|---|---|---|
| `budget_scope_event_id` | TEXT PK | `bse_` 前缀内部事实 ID |
| `event_id` | TEXT UNIQUE | `bsevt_` 前缀稳定 Webhook ID |
| `tenant_id` | TEXT NOT NULL | 租户隔离键 |
| `environment_id` | TEXT NOT NULL | Environment 隔离键 |
| `budget_rule_id` | TEXT NOT NULL | 触发上限的规则 |
| `budget_scope_id` | TEXT NOT NULL | value object 稳定标识 |
| `scope_type/scope_id` | TEXT NOT NULL | 触发层级快照 |
| `budget_date` | TEXT NOT NULL | Asia/Shanghai 业务日 |
| `action_taken` | TEXT | `allow/block` |
| `posted_cost_micros` | INTEGER | 正式 Usage Charge 已入账微元 |
| `reserved_cost_micros` | INTEGER | 未结算 reservation 微元 |
| `estimated_request_cost_micros` | INTEGER | 当前请求冻结估算微元 |
| `projected_cost_micros` | INTEGER | 越限判定使用的总微元 |
| `budget_micros` | INTEGER | 规则额度微元 |
| `request_id/trace_id` | TEXT NOT NULL | 首次越限调用链 |
| `created_at` | TEXT NOT NULL | UTC 时间 |

约束：

- `UNIQUE(tenant_id, environment_id, budget_rule_id, budget_date)`，同一规则同一天最多一个 canonical 越限事件。
- 父规则必须与 event 的 tenant/environment/scope 完全一致。
- 五个金额字段必须是 SQLite INTEGER 且位于 `0..1_000_000_000_000_000`；`projected=posted+reserved+estimated` 由写入服务校验并由测试冻结。
- 事件只保存 ID、金额、动作和调用链，不保存 Prompt、Response、Authorization、Provider Key 或 Key 明文。
- 首次 INSERT 与后续重复命中都可在 commit 后用同一个稳定 `event_id` 机会性入队；Delivery 唯一键负责幂等。这样 worker 暂不可用或队列满时，后续请求可重投，而不会创建第二个业务事实或第二个 Delivery。

### 5.4 归属触发器补强

0042 为以下表增加 INSERT 和归属字段 UPDATE 校验：

- `budget_rules`
- `budget_alert_events`
- `budget_evaluations`
- `budget_blocks`
- `budget_reservations`
- `budget_scope_events`

子表的 `tenant_id/environment_id/budget_rule_id/scope_type/scope_id` 必须与父 BudgetRule 一致。恶意跨租户、跨 Environment、替换 scope 的 INSERT/UPDATE 统一由数据库拒绝。

### 5.5 查询索引

0042 为正式 Usage Charge 增加 tenant、project、virtual_key 和 Application 兼容 scope 的部分索引，过滤 `entry_type='usage_charge' AND direction='debit' AND status='posted'`，避免按业务日预算聚合退化为全表扫描。

### 5.6 Down 迁移

Down 先检查：

- 是否已产生 `budget_scope_events`。
- 是否已有 Endpoint 订阅或 Delivery 保存 `budget.scope_exceeded`。

存在不可逆外部事实时以稳定 guard 错误拒绝回滚；空事件环境允许删除 PRD-052 索引、触发器和新表。回滚不得删除 Application 预算或修改 0041 FinOps 账本。

## 6. API 设计

### 6.1 `POST /budgets`

- 权限：仅 admin，后端执行。
- 语义：create-only。
- 成功：HTTP 201，返回 `BudgetRuleResponse`、`request_id`、`trace_id`。
- 重复：同 `tenant + environment + scope_type + scope_id` 已有规则时返回 409 `budget.rule_conflict`，原规则不变。
- 并发：数据库 UNIQUE 冲突也必须归一为同一 409，不能泄露 SQLite 文本。
- 请求体不接受 `tenant_id`；未知字段、重复 JSON key、非 `application/json`、非法 UTF-8、超出正文上限和递归结构均由严格请求解析器拒绝，不能依赖 Pydantic 默认忽略行为。
- RBAC 依赖必须先于 query/body 解析执行。developer/viewer 即使发送畸形写请求，也先得到稳定的后端 403，解析错误不能成为权限探针。

请求示例：

```json
{
  "environment_id": "env_prod",
  "scope_type": "project",
  "scope_id": "prj_billing",
  "name": "账务项目日预算",
  "daily_budget_cny": "500.000000",
  "warning_threshold_percent": 80,
  "exhausted_action": "block",
  "status": "active"
}
```

### 6.2 `PATCH /budgets`

保留现有兼容 upsert，避免破坏 PRD-023/029/033 客户端。PAGE-076 对新 scope 优先调用 POST，对已有 scope 调用 PATCH。PATCH 的空更新继续返回 `budget.update_empty`。

### 6.3 `GET /budgets`

保留现有过滤参数和 `budget_rules`，每条规则增加：

- `budget_scope: BudgetScopeInfo`
- `today_status.ledger_spent_cny`
- `today_status.reserved_cny`
- `today_status.effective_spend_cny`

列表响应增加 `environment_summaries`。每个 Environment 独立按 tenant/project/virtual_key 聚合 active 规则数量及 ok/warn/exhausted/block 数；Application 兼容项单列，不混入 Excel 三级总数。未传 Environment 过滤时也不能跨 dev/test/prod 选择一个全局 limiting rule。跨层金额不能直接相加，避免同一 Usage 被 tenant/project/key 三次计入“总成本”。

兼容字段冻结如下：`spent_cny` 继续表示正式已入账金额，等于新增 `ledger_spent_cny`；`estimated_remaining_cny` 改为按 `ledger_spent + outstanding reservation` 计算，等于新增 `effective_remaining_cny`；`usage_percent` 同样使用有效消耗。新增字段使调用方能看见语义变化，旧字段不删除。

GET query 使用严格白名单，并通过 `query_params.multi_items()` 拒绝未知参数、重复参数和 `tenant_id`。列表规则、状态、预留与 Environment summary 必须在同一个只读事务快照内物化，避免一次响应混合不同提交时刻的数据。

### 6.4 `GET /budget-status`

Query 保持兼容：`environment_id`、`project_id`、`application_id`、`virtual_key_id`。

可信归属规则：

1. 传 Key 时从当前租户 Key 记录恢复 Environment、Project、Application。
2. 同时传 Project/Application 时必须与 Key 绑定一致，否则返回 `budget.scope_mismatch`。
3. 只传 Application 时从 Application 恢复 Project；显式 Project 冲突同样拒绝。
4. 跨租户或不存在对象统一 `budget.scope_not_found`，不暴露其他租户是否存在。

响应保留 `statuses`，新增：

```json
{
  "aggregate": {
    "aggregate_status": "exhausted",
    "effective_action": "block",
    "effective_remaining_cny": "0",
    "limiting_rule_id": "br_project_a",
    "limiting_scope": {
      "budget_scope_id": "bsc_xxx",
      "scope_type": "project",
      "scope_id": "prj_a",
      "priority": 1
    },
    "conflict_policy": "all_applicable_caps_deny_wins_v1",
    "scope_chain": []
  },
  "request_id": "req_xxx",
  "trace_id": "trc_xxx"
}
```

选择 limiting rule 的顺序：对全部适用 active 规则按有效剩余微元升序、固定 scope priority 和 `budget_rule_id` 排序。`limiting_rule` 与 `effective_remaining_cny` 只表达当前纯容量最小值；active block 只决定 `aggregate_status` 和 `effective_action`，不得抢占 limiting。Gateway 带请求估算后的 `governing_rule` 仍由准入循环决定，不能复用页面 limiting 结论。

没有适用规则时返回 `aggregate_status='unconfigured'`、`effective_action='allow'`、空 scope chain、空 limiting rule 和 `effective_remaining_cny=null`。

该接口同样拒绝未知/重复 query 和 `tenant_id`；权限依赖先于严格解析。归属冲突统一为 HTTP 409 `budget.scope_mismatch`，不存在或跨租户对象统一为 HTTP 404 `budget.scope_not_found`。

### 6.5 `PATCH /budgets/{id}/mode`

继续仅 admin 可用，跨租户 ID 返回 `budget.rule_not_found`。切为 `alert_only` 只释放该规则的 active block，不得释放其他 Project、Key 或 tenant block。

## 7. Webhook 事件

### 7.1 `budget.scope_exceeded`

canonical payload 只允许：

```json
{
  "budget_scope_event_id": "bse_xxx",
  "budget_scope_id": "bsc_xxx",
  "budget_rule_id": "br_xxx",
  "environment_id": "env_prod",
  "scope_type": "project",
  "scope_id": "prj_xxx",
  "budget_date": "2026-07-27",
  "posted_cost_cny": "480",
  "reserved_cost_cny": "10",
  "estimated_request_cost_cny": "10",
  "projected_cost_cny": "500",
  "budget_cny": "500",
  "action_taken": "block",
  "request_id": "req_xxx",
  "conflict_policy": "all_applicable_caps_deny_wins_v1"
}
```

信封：

- `event_id` 使用持久 `bsevt_...`。
- `tenant_id` 来自鉴权上下文。
- 运行时没有用户操作者，`actor_id=null`；request_id/trace_id 足以回查可信 Key 归属，避免把认证凭证对象误作操作者。
- `object_id` 使用 `budget_scope_id`。
- `trace_id` 使用首次越限请求 trace。

共享 Webhook 合同必须精确校验 payload 键、ID 格式、信封绑定和敏感字段。`budget.scope_exceeded` 表示 Provider 前“posted + outstanding reservation + 当前请求估算”已达到上限，不宣称全部金额均已实际消费。旧 `budget.exhausted` 日志/事实继续保留，不能把同一 canonical 事件以两个事件名对外双发。

## 8. 前端 PAGE-076

### 8.1 页面结构

页面名称从“预算告警”统一为“预算中心”，保持现有控制台壳层和紧凑运营工具风格：

1. 顶部聚合状态带：当前业务日、有效动作、限制 scope、最小剩余、规则数量。
2. 三级 scope 摘要：tenant/project/virtual_key 的正常、告警、耗尽、阻断计数。
3. 适用 scope chain：按固定顺序展示每层额度、正式已入账、预留、有效消耗和剩余。
4. admin 创建/编辑表单。
5. 规则表继续展示 Environment、scope、额度、动作、今日状态和更新时间。

不使用营销式 Hero、装饰性渐变或嵌套卡片。桌面端用于密集扫描，移动端把规则行转换为稳定字段列表，不能产生横向溢出或文本重叠。

### 8.2 交互

- 新 scope 提交 `POST /budgets`。
- 已存在 scope 提交 `PATCH /budgets`。
- 409 冲突保留后端 message 与 request_id，并刷新规则以防并发创建后页面陈旧。
- admin 可切换耗尽模式；developer 只读；viewer 不渲染入口，但后端仍返回 403。
- Project 与 Key 选项只使用当前租户 API 数据；选 Key 时页面可自动对齐其 Project，但后端仍重新验证。
- 所有名称、错误和 ID 必须 HTML/属性转义；不展示完整 Virtual Key、Token、Prompt 或 Response。

## 9. 错误码

| 错误码 | HTTP | 场景 |
|---|---:|---|
| `budget.rule_conflict` | 409 | POST 重复创建同 Environment/scope 规则 |
| `budget.scope_mismatch` | 409 | Project/Application/Key 或 Environment 可信归属冲突 |
| `budget.scope_not_found` | 404 | scope 不存在或属于其他租户 |
| `budget.rule_not_found` | 404 | 路径规则不存在或跨租户 |
| `budget.rule_invalid` | 422 | scope、金额、阈值、动作或状态非法 |
| `budget.update_empty` | 422 | PATCH 没有可更新字段 |
| `budget.status_unavailable` | 503 | 正式账本存在未知 CNY Usage 或数据库聚合失败 |
| `gateway.budget_exhausted` | 402 | 任一适用 block 规则耗尽 |
| `gateway.budget_unavailable` | 503 | 数据面无法可靠计算 CNY 有效消耗，失败关闭 |
| `auth.permission_denied` | 403 | developer/viewer 写入或 viewer 读取 |

所有响应由统一异常层补充 `request_id`、`trace_id`、安全 suggestion；禁止返回 SQL、数据库路径、底层约束名称或其他租户对象信息。

POST/PATCH 请求 DTO 使用 `extra='forbid'`，但 DTO 不是唯一防线；路由必须手工有界读取 JSON object，并拒绝重复 key、未知字段、`tenant_id`、`actor` 和服务端派生字段。
GET 使用 `multi_items()` 逐项验证 query 白名单与唯一性；所有 wire-level 拒绝统一进入结构化错误响应并保留 request_id。

## 10. 安全、权限和租户隔离

1. 所有 API 从认证上下文获取 `tenant_id`，不接受客户端指定租户。
2. admin 是当前唯一预算写角色；admin/developer 可读；viewer 后端拒绝。
3. Rule、Project、Application、Key、Environment 查询均以 `tenant_id` 为首要条件。
4. Key 的 Project/Application/Environment 来自持久绑定，不信任请求正文和前端状态。
5. 数据库触发器是服务校验后的第二道归属防线，覆盖 INSERT 和归属字段 UPDATE。
6. Gateway 在 Provider 前完成全部适用规则判断；前端隐藏按钮不构成权限控制。
7. `BEGIN IMMEDIATE`、UNIQUE、幂等 reservation/block/event 共同保证并发一致性。
8. 事件只在数据库 commit 后进入异步队列；回滚或 INSERT ignored 不能输出虚假事件。
9. 日志和事件不记录 Prompt、Response、Authorization、Provider Key、Virtual Key 明文或 Endpoint 凭据。
10. Application 兼容 scope 不得在本 PRD 被删除或重新解释。
11. 租户预算的“全租户”遵循 PRD-030：作用于该预算规则所属 Environment；跨 Environment 不共享额度。
12. Control Budget 的所有公开服务无论成功、业务异常还是数据库异常，都必须在 `finally` 中关闭连接；GET 使用单一显式只读事务，POST/PATCH/mode/兼容 upsert 使用显式 `BEGIN IMMEDIATE`。
13. Audit、`budget.scope_exceeded` Webhook 和相关结构化日志只能在数据库 commit 成功后产生副作用。回滚路径不得留下已投递事件或成功日志。
14. Gateway 的 `BudgetExhaustedError` 携带本次事务内持久化且 commit 后可投递的稳定 scope event 描述；外层只在确认事务提交后调度，异常对象不得携带 Prompt、Response、Key 明文或凭证。
15. Reservation 的“已由 Usage 接管”只以同租户、同 request 的正式 `usage_charge` 是否存在为准；显式 outstanding reservation 即使已有正式分录仍需叠加，避免主失败、备成功场景低估占用。
16. active BudgetBlock 保持粘性，不能因 Manual Credit、Reversal 或 reservation 波动自动释放；管理员提额恢复只比较正式已入账 `usage_charge`，下一请求准入再叠加 outstanding reservation。

## 11. 测试计划

### 11.1 迁移与数据库

1. 从 0041 空事实升级 0042，表、索引、触发器完整，重复迁移幂等。
2. 带 tenant/project/key/application 规则、block、reservation、alert/evaluation 的 0041 数据升级后逐行保持。
3. 同租户跨 Environment Key、跨租户 Project/Key、scope 与父规则不一致的 INSERT/UPDATE 被触发器拒绝。
4. `budget_scope_events` 同规则/业务日去重，父规则归属不一致被拒绝。
5. 空事件环境 down/up 往返后 `PRAGMA foreign_key_check` 为零。
6. 已产生 scope event/subscription/delivery 时 down 明确拒绝，不静默丢外部事实。
7. 0041 FinOps 表、触发器和历史正式分录哈希/计数不被 0042 改写。

### 11.2 Control API 主流程

1. admin 分别 POST tenant/project/key，HTTP 201，响应含 scope DTO、request_id、trace_id。
2. GET 返回三级规则、正式账本/预留拆分和 scope summary。
3. PATCH 更新已有规则，POST 同 scope 返回 409 且原规则不变。
4. `GET /budget-status?virtual_key_id=key_a` 自动返回 tenant + Project A + Key A chain。
5. 只传 Application 自动恢复 Project；Application 兼容规则仍可读。
6. 聚合 limiting scope、最小余额和 stable priority 正确。

### 11.3 异常与权限

1. developer GET 成功、POST/PATCH 403；viewer GET/POST/PATCH 均 403。
2. 其他租户 Project/Key/Rule 返回 404，不暴露存在性。
3. Key A + Project B、Key Environment 与 query Environment 冲突返回 409。
4. 非法 scope、金额、阈值、动作、空 PATCH 返回统一错误码。
5. 409/403/404/422/503 的 Header 和正文 request_id 一致。
6. 正式 Usage Charge 未归一 CNY或明确 unpriced 时 Control 返回 503，Gateway 不调用 Provider。

### 11.4 Gateway 层级与并发

1. Project A block 耗尽：Key A 返回 402、Provider 0 次；Project B Key 返回成功。
2. tenant block 耗尽：Project A/B 的 Key 均 402，governing rule 都是 tenant。
3. Key A block 耗尽：同 Project Key B 和其他 Project Key 均成功。
4. tenant alert_only + project block：Project 仍阻断。
5. tenant block + key alert_only：Tenant 仍阻断。
6. project alert_only + key block：Key 阻断，其他 Key 放行。
7. 前序 tenant/project reservation 已创建而后序 key 阻断时，本请求新预留全部删除。
8. 同 Project 两个并发请求竞争最后额度时最多一个准入；不同 Project 不互相消耗项目额度。
9. Manual adjustment、credit、reversal、gift/trial 不改变模型消费预算；Usage Charge 只计一次。
10. 上海零点、提额、停用、切 alert_only 只释放目标规则 block，不释放其他 scope。

### 11.5 事件和隐私

1. 首次 100% 越限创建一条 `budget_scope_events` 和一个 Webhook 事件。
2. 重复请求不创建第二条事实或 Delivery。
3. commit 失败不记录/投递事件，不调用 Provider。
4. payload、签名正文、结构化日志和验收证据零命中 Prompt、Response、Authorization、Provider Key、Token、完整 Virtual Key。

### 11.6 Console

1. admin 显示 PAGE-076 聚合状态、三级摘要、scope chain、POST/PATCH 表单和模式操作。
2. developer 只读；viewer 不渲染入口。
3. 恶意名称、错误和 ID 被转义。
4. 1280x720 与 390x844 无重叠、截断、横向溢出；长中文和最长 ID 保持可读。
5. 网络证据覆盖 POST/GET/PATCH、201/402/403/404/409，并核对 request_id。

## 12. 代码修改计划

1. 新增 `0042_three_level_budgets.sql/.down.sql`，建立 scope event、FinOps scope 索引和归属触发器。
2. 新增共享 BudgetScope/FinOps spend helper，统一整数微元、unknown fail-closed 和 priority；`enforce_budget_guard`、`load_policy_budget_remaining` 与 Control 状态必须全部复用，禁止 Policy 继续读取旧 Usage 口径。
3. 更新 reservation 结算存在性，从 Usage 表切换到正式 Usage Charge。
4. 扩展 Webhook 白名单、严格 payload/envelope 合同和单元测试。
5. 扩展 Control Budget DTO、POST 路由、create-only 服务、可信 scope resolver、聚合响应和错误码。
6. 在 Gateway 复用共享 spend helper，持久 canonical scope event，并把 commit 后事件交给现有 runtime dispatcher。
7. 更新 PAGE-076 客户端、状态聚合、三级展示、POST/PATCH 选择和响应式 CSS。
8. 新增独立 PRD-052 迁移、Control、Gateway、Shared 和 Console 测试；只补必要的既有回归断言。
9. 更新 README、目录说明、`docs/testing/PRD-052-tests.md` 和实际验收脚本/证据。

## 13. 聚焦验证策略

本 PRD 不因局部失败反复执行仓库根级全量 pytest，也不重跑 PRD-051 浏览器批次。测试全部在 `D:\MetaHub` 沙箱内运行，pytest 固定：

```powershell
$env:TEMP='D:\MetaHub\.tmp'
$env:TMP='D:\MetaHub\.tmp'
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src;examples\python\src'
```

验证顺序：

1. 新增 PRD-052 迁移、Control、Gateway、Shared、Console 专项。
2. 受影响旧预算、Project/Application、Webhook、错误码测试。
3. Console 预算单测与 lint。
4. `scripts/ci/check-comments.py`。
5. 开发库 0042 实迁移和真实服务/API/页面验收。

只有聚焦闭环出现跨模块合同问题时才扩大到对应模块测试；不因单个失败从头执行无关全量套件。

## 14. 编码前自审记录

### 第一轮自审

- 状态：2026-07-27 已完成并修订。
- 重点：需求边界、FinOps 口径、冲突算法、并发事务、兼容性。
- 发现并修正：事件 NUMERIC 与整数微元合同冲突；limiting/governing 概念混用；提额恢复误纳 reservation；事件首次入队失败无重投；scope ID 算法未冻结；Application parent 歧义；`budget.update_empty` 状态码漂移；Policy 余额遗漏；runtime actor 语义错误；跨 Environment 汇总；projected 越限语义不清。
- 结论：所有 P0/P1 已进入修订稿，第二轮需重新核对实际可实现性和安全边界。

### 第二轮自审

- 状态：2026-07-27 已完成并修订。
- 重点：RBAC、tenant/Environment/Project/Key 归属、错误码、事件隐私、事务与连接生命周期、迁移可逆性和测试可验收性。
- 发现并修正：Budget query/body 可被默认解析器静默接受未知或重复字段；畸形请求可能先于 RBAC 暴露校验差异；Control 公开服务缺少统一连接关闭约束；GET 多次查询可能跨快照；写操作事务模式未全部冻结；Gateway 事件/Audit/日志可能在 commit 前产生副作用；预算耗尽异常缺少稳定待投递事件载体；reservation 仍可能以旧 `usage_ledger` 判断已结算；提额恢复与 reservation/账务贷记边界不清；`budget.scope_mismatch` HTTP 状态未逐处冻结；SQLite 聚合存在整数溢出风险。
- 补充交叉自审：发现并消除 API 小节的 active-block-first 与通用冲突规则“最小非负余额”之间的矛盾；现冻结 `limiting_rule/effective_remaining_cny` 为纯容量语义，active block 只影响聚合状态和动作，不抢占 limiting。
- 修订结论：严格 wire 解析与 RBAC 顺序、只读/写事务、`finally` 关闭连接、commit 后副作用、逐行整数累计、正式 Usage Charge 结算边界、sticky block 和 409 scope mismatch 均已写入冻结合同。
- 范围复核：未引入 CAS/version、Redis、FinanceAdmin、部门预算或任意优先级 DSL，避免超出 PRD-052 与现有兼容面。
- 结论：第二轮无未关闭 P0/P1 问题，允许进入编码；实现若无法满足任一安全或兼容合同，必须先暂停并回写设计。

## 15. 实现与最终验收结论

- 实现已按冻结合同落地，未删除 Application 兼容 scope，也未引入部门预算、FinanceAdmin、Redis 或任意优先级 DSL。
- 0042 已在 Runtime 库与开发库实际应用；两库均为 42 条迁移、最高 `0042`、`integrity_check=ok`、外键违规 0。
- 真实 Runtime 已验证三级规则、正式 FinOps 消费、重复 POST 409、developer/viewer 后端 403、Project 预算 402 和 Provider attempt 为 0。
- PAGE-076 已在 1280x720 与 390x844 两档真实浏览器完成 6 个页面状态验收，无横向溢出、裁切、重叠、浏览器错误或被阻断请求。
- 停服后 5173/8000/8001 监听均为 0，本批次 7 个 Session 全部 revoked，WAL/SHM/journal 均不存在；日志隐私、数据库与证据哈希终态审计通过。
- 测试口径与直接执行结果见 [PRD-052 测试记录](../testing/PRD-052-tests.md)，权威运行证据见 [runtime-evidence.json](../../artifacts/prd052/20260727T112603/runtime-evidence.json)、[browser-evidence.json](../../artifacts/prd052/20260727T112603/browser-evidence.json) 与 [final-audit.json](../../artifacts/prd052/20260727T112603/final-audit.json)。

结论：PRD-052 当前无已知 P0/P1 缺陷，可以标记为已完成；该结论不代表全部 108 条 PRD 已完成。Excel 核对后的下一条未完成项为 `PRD-053 成本异常检测基础`。
