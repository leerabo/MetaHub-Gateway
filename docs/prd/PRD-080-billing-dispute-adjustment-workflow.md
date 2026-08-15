# PRD-080 账单争议与调整流程设计

状态：**已完成**（2026-08-15）。本文件冻结 PRD-080 的权威范围和实现合同；编码前两轮自审见第
16、17 节，实现后两轮自审见第 19、20 节，发现的问题均已修正且没有未关闭 P0/P1。PRD-079 未被
重复实现；本条只交付 PRD-080，继续保持 `all_108_prds_complete=false`，未启动或混入 PRD-081。

## 1. 权威范围与交付边界

### 1.1 交叉确认

- Excel `01_PRD总表!A81:R81` 与 `03_M2-M3扩展交付!A17:R17` 一致确认：
  `PRD-080 | M2-企业增强 | P2 | 扩展层 | 账单争议 | 账单争议与调整流程`。
- 用户与故事为“财务/客户成功”“作为客户，我希望对异常账单提出争议并追踪处理”。
- 实现范围固定为：争议申请、关联 `request_id` 或时间段、审核、暂缓结算、调整记录和全流程审计。
- Excel 明列 `POST /billing-disputes`、`PATCH /billing-disputes/{id}`；PAGE-112 必须能够追踪处理，
  因此在同一资源域补充 `GET /billing-disputes` 与 `GET /billing-disputes/{id}`。GET 只恢复已有事实，
  不扩展到支付、退款或发票。
- 数据对象为 tenant-qualified `BillingDispute` 和 PRD-051 已有 append-only `AdjustmentRecord`。
- canonical Webhook 仅为 `billing_dispute.created`；不自行增加 `updated/approved/refunded` 事件。
- TC-0159 要证明费用在争议期间被标记、通过后生成调整记录、全流程可审计；TC-0160 要证明金额精度、
  权限、统一错误、安全与异常状态可追踪。
- 依赖是已完成的 PRD-051 FinOps 正式账本；当前最高迁移为 `72/0072`，本条迁移固定为 `0073`。

### 1.2 明确不做

- 不调用支付渠道，不自动退款，不创建订单、支付回调、发票、应收或自动扣款对象。
- 不修改或删除原始 Usage、`finops_ledger_entries`、价格快照、PRD-079 额度桶、预留、消费或结算分摊。
- 不把账本冲正解释为额度返还；额度返还是独立业务动作，不在本条暗中执行。
- 不把 PRD-071 的 `finops_ledger_disputes` 改名、改状态或复用为工作流表。该表只表示输出安全形成的
  immutable `dispute_pending` marker；PRD-080 的客户账单争议是另一 owner 图。
- 不新增“客户成功”固定角色。系统现有六角色不变；本条使用独立权限码并默认授予 `admin`、
  `finance_admin`，客户成功人员需由租户按现有角色治理流程赋予财务管理员职责。
- 不把 OpenRouter、硅基流动登记为默认上游；本条不改变 Provider 目录和路由。

## 2. 现状约束与关键决定

1. PRD-051 的 `finops_adjustments` 和 `finops_ledger_entries` 均为 append-only，金额使用整数微元，
   账本分录保存来源 `request_id` 和价格证据。争议通过只能追加 `reversal` Adjustment/Entry，不能 UPDATE 原分录。
2. `finops_ledger_entries.status` 固定为 `posted`。暂缓结算使用独立 overlay hold，不伪造 ledger 状态，
   不影响 Gateway 继续记录 Usage，也不阻塞 PRD-079 quota settlement。
3. POST 在创建事务内物化匹配 charge、当前可冲正金额和价格证据。以后到达的 spool/replay charge 不会
   动态进入旧争议；需要覆盖迟到分录时必须创建新争议。
4. 申请默认质疑匹配分录当时的全部可冲正余额；审核人可以逐项批准全额或部分金额。批准项各生成一条
   既有类型 `reversal`，未批准项只释放 hold，不生成财务事实。
5. 争议可包含 CNY 或 USD 分录；客户端不提交 currency，服务端从冻结 item 恢复。列表和详情按币种分别
   汇总，禁止把不同币种相加，也禁止使用 JSON float。
6. 每次写入先由路由执行后端 RBAC，再在同一 `BEGIN IMMEDIATE` 事务中重读实时权限、tenant owner、
   revision 和剩余可冲正金额。前端导航、隐藏按钮、角色名和请求正文里的 tenant 均不能补权。
7. 创建、状态迁移、Audit、事件 source/outbox、hold 和 approved Adjustment/Entry 的 owner 必须可由
   数据库复合外键和 trigger 逐字段复核；不能以应用日志或最终一致性代替原子事实。

## 3. 范围、金额与时间冻结规则

### 3.1 `request_ids` 范围

- `scope_type=request_ids` 时必须提交 `request_ids`，数量 `1..100`，顺序不影响幂等摘要；重复值拒绝。
- 每个值必须是当前 tenant 的 canonical 已计费 `source_request_id`，并且恰好对应一条 `usage_charge`。
- 不存在、跨租户、零金额或已经完全冲正的目标统一使用不枚举 owner 的范围错误；整单失败，不部分创建。
- `period_start`、`period_end` 在该模式必须缺失，禁止联合范围造成不同客户端解释不一致。

### 3.2 `time_range` 范围

- `scope_type=time_range` 时必须提交 UTC `Z` 秒精度的 `period_start`、`period_end`，范围采用
  `[period_start, period_end)`；开始必须早于结束，跨度最多 31 天，结束不得晚于事务内数据库时刻。
- `request_ids` 必须缺失。查询只选择当前 tenant、`entry_type=usage_charge`、正金额、尚有可冲正余额的分录。
- 创建事务先冻结 `ledger_sequence` 高水位，再按 `created_at ASC, ledger_sequence ASC` 物化；命中 `1..100`
  条才允许创建。超过上限固定返回 scope-too-large，不静默截断。
- 迟到 spool charge、创建后入账或之后修正的价格不进入旧 item；详情始终展示创建时冻结证据。

### 3.3 金额

- wire 金额只接受正数 ASCII 普通十进制文本，最多六位小数；拒绝符号、指数、Unicode 数字、float、
  超过 `1,000,000,000` 单位或不能精确转换为整数微元的值。
- 单项上限不能误用于 created Webhook 的按币种汇总；每个 `currency_totals` 值允许达到
  `item_count * 1,000,000,000`，但仍须是由冻结 item 重算的正数六位小数文本。
- 每个 item 冻结原分录 `amount_micros` 和创建时 `disputable_micros`。批准金额必须大于 0，且同时不超过
  冻结值和批准事务重新计算的当前剩余可冲正值。
- approve 的调整数组按 `billing_dispute_item_id` 去歧义，最多 100 项；同 item 重复、未知、跨租户、
  currency 自报或金额超限均使整笔决定回滚。
- 返回值固定为六位小数文本，并按 `CNY`、`USD` 独立汇总；数据库、Audit 和测试均以整数微元断言。

## 4. 权限、身份与租户隔离

### 4.1 新权限目录

| 权限码 | 风险 | 用途 | 默认角色 |
|---|---|---|---|
| `billing.dispute.read` | read | 列表、详情和 PAGE-112 | `admin`、`finance_admin` |
| `billing.dispute.create` | write | 创建争议、取消本人 submitted 争议 | `admin`、`finance_admin` |
| `billing.dispute.review` | critical | 开始审核、批准或拒绝 | `admin`、`finance_admin` |

- RBAC catalog revision 从 15 提升到 16；`admin` 仍是完整不可变目录，`finance_admin` 默认增加三项。
- 其他固定角色的授权上限不增加财务争议权限，避免 developer、viewer、security 或 ops 默认看到客户账单。
- GET 依赖 `billing.dispute.read`；POST 同时依赖 read+create；PATCH 路由先要求 read 且 create/review 至少一项，
  服务层再按 action 在事务内复核精确权限。这样自定义矩阵撤掉 read 后，写接口也不能借响应取得 detail。
- `start_review/approve/reject` 要求 read+review；approve 还必须同时实时具备 `billing.finops.write`。
  `cancel` 要求 read+create 且 actor 必须是原申请人。
- 申请人不得审核自己的争议，包括 `start_review`、approve 和 reject；返回稳定 403，不提供管理员隐式例外。

### 4.2 tenant owner

- tenant 只从认证 Session 恢复；查询、正文和 path 均不接受 `tenant_id`。
- 所有业务表均包含 `tenant_id`；跨表关系使用 `(tenant_id, object_id)` 复合唯一键和复合外键。
- 跨租户 dispute、item、request、ledger、usage、hold、actor、adjustment 或 event 与不存在对象同形处理。
- 路由依赖通过后，写事务仍调用 `require_transaction_permission`/多权限等价入口，阻断依赖校验后撤权。
- 长生命周期 Console owner 绑定 tenant、user、Session、permission revision、matrix hash、授权指纹和导航项；
  任一变化立即 abort 请求、关闭 Dialog、清空 reason/decision 和 PAGE-112 DOM。

## 5. 数据模型

### 5.1 `billing_disputes`

mutable head，每次变更只允许通过 revision CAS 和状态 trigger：

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `billing_dispute_sequence` | INTEGER PK AUTOINCREMENT | 稳定列表水位 |
| `billing_dispute_id` | `bdsp_` + 32 hex | 公开资源 ID，tenant 内唯一 |
| `tenant_id` | TEXT FK | 强制租户归属 |
| `scope_type` | `request_ids/time_range` | 冻结范围类型 |
| `period_start/period_end` | nullable UTC Z | 仅 time_range 成对存在 |
| `status` | `submitted/under_review/approved/rejected/cancelled` | 当前状态 |
| `state_revision` | INTEGER >= 1 | PATCH CAS |
| `item_count` | INTEGER `1..100` | 与 item 事实精确一致 |
| `reason` / `reason_sha256` | 1..2000 UTF-8 bytes / 64 hex | 明文只留权威 DB；其他边界用摘要 |
| `created_by_user_id` | tenant user FK | 申请人 |
| `reviewer_user_id` | nullable tenant user FK | start_review actor；approve/reject 时更新为最终审核人 |
| `decision_reason` / `decision_reason_sha256` | nullable | approved/rejected/cancelled 原因；非终态为空 |
| `create_request_id/create_trace_id` | bounded TEXT | 创建链路 |
| `create_idempotency_sha256/payload_sha256` | 64 hex | 租户级创建幂等 |
| `review_started_at/resolved_at` | nullable UTC Z | 状态时间 |
| `created_at/updated_at` | UTC Z | 事务时钟 |

约束：同 tenant 的创建幂等摘要唯一；时间范围字段与 scope 互斥；approved/rejected 必须有 reviewer、
decision reason 和 resolved_at；cancelled 必须没有 reviewer、但有 decision reason 和 resolved_at；非终态不得
提前携带 decision/resolved 字段。start_review 记录当前审核人，另一个具备 review 权限且不是申请人的 actor
可以接手终态决定，head 更新为最终审核人，完整接手轨迹以 immutable transition 为准。

### 5.2 `billing_dispute_items`

创建后 immutable，逐项冻结：`billing_dispute_item_id`、tenant/dispute、`finops_ledger_entry_id`、
`usage_ledger_id`、`source_request_id`、`source_trace_id`、`ledger_sequence`、currency、原金额、
创建时可争议金额、`price_snapshot_id`、`price_schema_version`、`price_evidence_status`、created_at。
同一 dispute 不得重复 ledger entry；owner trigger 必须回查 Usage/FinOps/price 证据逐字段一致。

### 5.3 `billing_settlement_holds`

- 每个 item 创建一条 hold：`billing_settlement_hold_id`、tenant/dispute/item/ledger、
  `status=active/released`、created/released_at、`release_reason=approved/rejected/cancelled`、revision、
  `created_transition_id` 和 nullable `released_transition_id`。
- partial unique `(tenant_id, finops_ledger_entry_id) WHERE status='active'`，并发重叠争议整单 409。
- 只允许 `active -> released` 一次 CAS，不允许重新激活或删除；创建/释放 transition 必须属于同 dispute，
  release reason/new revision 必须与对应终态 transition 一致。
- FinOps 列表只 JOIN active hold，历史 hold 不扩行；详情从 item/hold 返回完整历史。

### 5.4 `billing_dispute_transitions`

append-only 操作事实：自增 `billing_dispute_transition_sequence`、transition ID、tenant/dispute、action、
from/to status、expected/new revision、actor、
decision reason hash、operation request/trace、tenant-scoped idempotency hash、canonical payload hash、created_at。
创建也记录 `create: NULL -> submitted`。同 tenant 幂等摘要唯一；同 dispute/new revision 唯一。

### 5.5 `billing_dispute_adjustment_links`

approved item 与既有 FinOps 事实的一一关联：link ID、tenant/dispute/item、`finops_adjustment_id`、
reversal `finops_ledger_entry_id`、amount_micros、currency、transition ID、created_at。一个 item 至多一条 link；
外键和 owner trigger 证明 Adjustment/Entry 是同 actor、同请求、同币种、同金额且 reversal 指向冻结原分录。

### 5.6 `billing_dispute_events` 与 `billing_dispute_event_outbox`

- source event immutable，保存 event/dispute/tenant、source request/trace、actor、exact payload JSON、occurred_at。
- outbox 保存 tenant/event、pending/processing/succeeded/failed、attempt、lease generation/owner/token/expiry、
  next attempt、冻结 delivery ID snapshot、last error code 和时间。token 不进入 repr、日志、响应或证据。
- source、outbox、dispute、create transition 和 Audit 在同一创建事务；外部网络只在 commit 后由 worker 调用。

### 5.7 `billing_dispute_scheduler_tenants`

- 每个出现过积压的 tenant 保存 `last_claim_sequence`、独立 `state_revision`、最后领取时间和创建/更新时间；
  全部字段均由 owner trigger 保护，不是业务 API 可写对象。
- `last_claim_sequence` 是全表唯一、单调递增的领取顺序，不是 tenant 本地累计次数。worker 按最小序列选择
  当前有到期事件的 tenant，并在 claim 同一事务取得全局下一序列；即使不同 tenant 的历史领取次数不等，
  同秒积压仍按最近最少领取顺序轮转。

## 6. 状态机、幂等与并发

```text
create -> submitted
submitted --start_review--> under_review
submitted --cancel (creator only)--> cancelled
under_review --approve--> approved
under_review --reject--> rejected
```

- 不允许 submitted 直接 approve/reject，不允许终态再次变更，不允许 under_review cancel。
- POST 与每次 PATCH 都要求恰好一个 `Idempotency-Key`：8..128 个可见 ASCII；只存域分离 SHA-256。
- POST 使用 `billing-dispute-create-v1` 域；全部 PATCH 共用 `billing-dispute-transition-v1` 域。相同原始 key
  可以分别用于一次创建和一次迁移，但不能用于两个 POST 或两个不同 PATCH 意图，避免两张表形成含糊全局唯一假设。
- POST canonical payload hash 绑定认证 actor、scope type、规范 reason，以及排序后的 request IDs 或规范 UTC 时间范围；
  PATCH canonical payload hash 绑定认证 actor、path 中的 dispute ID、action、expected revision、规范 decision reason 和
  排序后的 item/amount。两者均不含 tenant 自报值，且分别使用所属域前缀后再做 SHA-256。
- 同 tenant 同 key 同 payload 返回既有结果并标记 `idempotent_replay=true`，不重复 transition/Audit/Event/Adjustment；
  同 key 异 payload 返回 409。
- `BEGIN IMMEDIATE` 串行化同库写入；revision CAS、active hold partial unique、FinOps reversal 剩余额 guard 和
  link unique 四层阻断双重处理。approve/reject 并发只有一个终态提交。
- approve 任一 item 金额、Adjustment、Entry、link、hold、transition 或 Audit 失败时整笔回滚，不允许部分批准。
- approve 的 adjustments 必须至少一项；列出的 item 生成逐项 reversal。未列出的本争议 item 被视为本次审核未批准，
  随 dispute 进入 approved 一并 release hold，但不生成 Adjustment/link。详情和 transition 保留批准计数与 totals hash，
  不能把“未列出”误报为已冲正。

## 7. 写事务

### 7.1 POST 创建

1. 路由先同时校验 `billing.dispute.read` 与 `billing.dispute.create`，再读取单值幂等 Header 和严格 JSON。
2. `BEGIN IMMEDIATE`，事务内重读 read+create 权限和当前 tenant/actor。
3. 查同 tenant 幂等事实；完全相同则无副作用恢复，冲突则 409。
4. 冻结 ledger 高水位，按 scope 选择并逐条验证 current remaining、Usage、price 和 tenant owner。
5. 插入 dispute head、immutable items、create transition，再插入引用该 transition 的 active holds；SQLite 外键保持即时检查。
6. 插入 `billing_dispute.created` source/outbox；写入只含安全摘要的 Audit。
7. `commit_audited_transaction`；提交后只投递 `audit.recorded`，created 事件由 durable outbox 恢复。

### 7.2 PATCH start_review/reject/cancel

1. 路由先要求 read 且 create/review 至少一项权限；严格解析后开始 `BEGIN IMMEDIATE`。
2. 事务内先重读 read，再按 action 重读精确 create/review 权限；tenant+ID 读取，校验幂等、expected revision、
   状态和禁止自审。start_review 写入
   当前 reviewer；approve/reject 允许另一个合格非申请人接手，并把 head reviewer 更新为最终决策 actor。
3. CAS 更新 head；终态同时单向 release 全部 active holds。
4. CAS head 后先追加 transition，再以其 ID 释放 hold，最后写 dispute Audit；无 Adjustment/ledger/Webhook status event。
5. 原子提交，随后投递不可变 AuditRecord。

### 7.3 PATCH approve

1. 同一事务实时要求 `billing.dispute.read` + `billing.dispute.review` + `billing.finops.write`，并禁止申请人自审。
2. 验证 under_review、expected revision、幂等和逐项批准数组；重新计算每个目标的 current remaining。
3. 从 FinOps 提取“使用调用方 connection、但不 commit/dispatch”的内部 adjustment writer；不得调用会自行开连接的
   公开 `create_finops_adjustment()`。
4. 每个批准 item 先追加一条 existing `reversal` AdjustmentRecord、正式 ledger entry和 FinOps Audit，暂不插 link。
5. CAS 更新 dispute 为 approved 并追加 transition；随后插入引用该 transition 的 Adjustment links，释放全部 hold，
   最后追加 dispute Audit。所有 transition 外键按可执行顺序满足 SQLite 即时检查，不依赖延迟约束。
6. 单次 commit；提交后逐条投递 Audit 和 `ledger.entry_created`。没有支付或额度副作用。

## 8. HTTP API

应用最外层响应中间件按规范化后的 `/billing-disputes` 路径前缀，为成功、认证失败、RBAC 失败、解析失败、业务错误和
未捕获异常统一设置 `Cache-Control: no-store, private`；不能只在 router 成功响应或异常处理器内补 Header。统一错误正文由
全局中间件带 `code/message/request_id/trace_id`。该路径可读写授权自由文本，整条路径均忽略客户端
`X-Request-ID/X-Trace-ID` 并生成服务端链路 ID；路由边界把未知异常收敛为固定
`503 billing_dispute.unavailable`，不得让框架向进程日志重新抛出可能携带正文的异常。
认证和 RBAC 必须先于业务 query/path/body 解析；未知/重复参数、重复 JSON key、非 UTF-8、未知字段、客户端
`tenant_id`、超深/超大正文均失败关闭。

### 8.1 `GET /billing-disputes`

允许单值 `status`、`scope_type`、`created_from`、`created_to`、`limit=1..100`、签名 `cursor`。
首次查询同时冻结最大 dispute sequence 与最大 transition sequence；cursor 绑定版本、tenant、筛选 hash、双水位
和 HMAC。列表按“不晚于 transition 水位的最新 transition”还原 status/revision/reviewer/resolved_at，再应用状态筛选；
hold 以 `created_transition_id <= 水位` 且 `released_transition_id IS NULL 或 > 水位` 还原，Adjustment link 仅计入
`transition_id <= 水位` 的事实。这样翻页期间发生 approve/reject 不会让同一快照漏项、重复、改变状态或泄露水位后的
hold/link 变化。返回 summary、按币种 disputed/approved totals、该 transition 水位下的 active hold/adjustment 计数、
event delivery status、分页信息和链路 ID；详情始终返回当前状态。

### 8.2 `GET /billing-disputes/{billing_dispute_id}`

返回 head、申请人/审核人最小身份、reason/decision reason、items、hold、Adjustment links、transitions 和安全
事件状态。item 同时返回创建时冻结的 `disputable_amount` 与按当前正式 FinOps 账本重算的
`current_disputable_amount`；批准界面和事务校验使用后者作为实时上限，不得回退使用冻结值。允许返回
request/trace、price snapshot/version 和金额；禁止返回 raw/normalized usage JSON、内部 HMAC、幂等摘要、
outbox lease/token、Webhook secret、Prompt、Response、Authorization 或 Provider Key。

### 8.3 `POST /billing-disputes`

request_ids 模式：

```json
{
  "scope_type": "request_ids",
  "request_ids": ["req_example_01"],
  "reason": "计费结果需要人工复核"
}
```

time_range 模式：

```json
{
  "scope_type": "time_range",
  "period_start": "2026-08-01T00:00:00Z",
  "period_end": "2026-08-02T00:00:00Z",
  "reason": "指定时段费用需要人工复核"
}
```

首次成功 201；完全相同幂等恢复 200。响应为完整 detail、create transition、
`idempotent_replay`、`request_id/trace_id`。

### 8.4 `PATCH /billing-disputes/{id}`

- start review：`{"action":"start_review","expected_revision":1}`。
- approve：

```json
{
  "action": "approve",
  "expected_revision": 2,
  "decision_reason": "审核确认部分费用应冲正",
  "adjustments": [
    {"billing_dispute_item_id": "bdsi_00000000000000000000000000000000", "amount": "1.000000"}
  ]
}
```

- reject/cancel：只接受 `action`、`expected_revision`、`decision_reason`。
- 首次和幂等恢复均为 200；响应返回 detail、本次 transition、生成的 Adjustment links、
  `idempotent_replay` 和链路 ID。正文不会接受 status、actor、currency、tenant 或直接 adjustment type。

## 9. 统一错误码

| code | HTTP | retryable | 场景 |
|---|---:|---:|---|
| `billing_dispute.query_invalid` | 422 | false | query/cursor 非法 |
| `billing_dispute.request_invalid` | 422 | false | Header、JSON、范围、reason、金额或字段非法 |
| `billing_dispute.not_found` | 404 | false | dispute/item 不存在或跨租户 |
| `billing_dispute.scope_not_found` | 404 | false | request/time scope 无当前租户 eligible charge |
| `billing_dispute.scope_too_large` | 422 | false | 时间或 item 数量超限 |
| `billing_dispute.hold_conflict` | 409 | false | charge 已被另一 active dispute 暂缓 |
| `billing_dispute.revision_conflict` | 409 | false | expected revision 过期 |
| `billing_dispute.transition_invalid` | 409 | false | 非法状态迁移或终态重写 |
| `billing_dispute.self_review_forbidden` | 403 | false | 申请人尝试审核自身争议 |
| `billing_dispute.adjustment_invalid` | 422 | false | approve item/金额组合非法 |
| `billing_dispute.adjustment_conflict` | 409 | false | 当前可冲正余额变化或金额超限 |
| `billing_dispute.idempotency_conflict` | 409 | false | 同 key 异意图 |
| `billing_dispute.integrity_failed` | 503 | true | 持久 owner/快照/关联损坏 |
| `billing_dispute.unavailable` | 503 | true | 数据库锁定、commit/outbox 暂不可用 |

内部 SQLite、trigger、JSON、URL、reason、幂等值和 payload 不进入 message、日志或响应。

## 10. Webhook source/outbox

### 10.1 `billing_dispute.created` 安全合同

exact payload 仅包含：

```json
{
  "schema_version": 1,
  "billing_dispute_id": "bdsp_00000000000000000000000000000000",
  "scope_type": "request_ids",
  "item_count": 1,
  "currency_totals": {"CNY": "1.000000"},
  "status": "submitted",
  "created_at": "2026-08-14T00:00:00Z"
}
```

- envelope 的 event/tenant/actor/object/trace/source request/timestamp 必须与 source row 逐字段一致。
- payload 不包含 request ID 列表、时间范围、reason、email、价格 JSON、Prompt、Response、Token 或密钥。
- Shared `runtime.webhooks` 增加 exact payload/envelope validator、订阅事件目录和敏感字段扫描；pending delivery
  恢复必须从 immutable source owner 图取私有 source request，不能使用 worker request 占位。

### 10.2 worker

- 复制 PRD-079 已验证的 snapshot/lease 协议到独立表和模块，不共享 Quota 表。网络语义明确为 at-least-once；
  每个冻结 delivery 使用稳定 delivery ID，接收端按该 ID 去重，系统不宣称无法由 HTTP 证明的传输层 exactly-once。
- claim 在每个 tenant 内按到期时间排序，再按全局单调 `last_claim_sequence` 选择最近最少领取的 tenant；租约
  90 秒，每次外呼前续租并检查 shutdown。首次外呼前停服会以完整 CAS 归还 claim，且不增加 attempt。
- 冻结创建时 delivery ID 集合；后增订阅不接收旧事件，删订阅也不能抹掉冻结 delivery。
- 可重试 source 失败按 `5s/30s/300s` 退避，最多 10 次进入 failed；Webhook delivery 已返回 terminal
  `failed` 时 source 在同轮直接收敛 failed，不能再进行不会出网的假重试。坏 source 隔离后继续其他 tenant，
  不能占住队首。
- 迟到 owner 由 tenant/outbox/status/attempt/generation/owner/token/expiry CAS 拒绝。

## 11. FinOps、Audit 与兼容性

- FinOps 内部 writer 复用现有 `_normalize_adjustment`、reversal remaining、Adjustment/Entry owner、金额和事件构造；
  公开 `/finops/adjustments` 行为及幂等域保持不变。
- dispute reversal 的内部幂等摘要使用域分离的 transition key hash + item ID，不能与公开 FinOps key 冲突。
- 每个 approved link 必须核对 reversal target、金额、currency、actor、operation request/trace 和 dispute transition。
- FinOps 列表增加可选 `billing_dispute_hold` 最小投影，只 JOIN 一条 active hold；原 PRD-071
  `output_safety_dispute` 字段和单行分页保持不变。同一 charge 同时有安全 marker 和客户 hold 仍只返回一条 ledger row。
- Audit 新增 `billing_dispute.created/review_started/approved/rejected/cancelled` 与 object type
  `billing_dispute`。summary 只含状态、revision、scope、item/adjustment 计数、币种汇总 hash 和 reason hash。
- approve 同事务还有每条既有 `finops.adjustment_created` Audit；任何 Audit 失败必须回滚所有业务事实。
- 应用日志只记录 tenant、dispute、状态、计数、request/trace 和固定 error code，不记录 reason、item ID 列表、
  幂等键、payload、价格原文、Prompt、Response、Provider Key 或 Token。

## 12. Console PAGE-112

- 在“调用与成本”增加 `billing-disputes` 导航，后端要求 `billing.dispute.read`；没有权限时无导航、DOM 或预取。
- 页面主体为紧凑工具栏、状态/范围筛选、稳定表格和无卡片嵌套的详情区；桌面和移动均保持可扫描。
- create 权限显示“提交争议”；使用 segmented control 选择 request IDs/时间段，输入 reason，提交前显示冻结范围摘要。
- review 权限且 actor 不是申请人时显示开始审核、批准、拒绝。批准 Dialog 列出 item、币种、当前可批准上限和
  金额输入；不提供退款、额度返还或支付按钮。
- Dialog 使用原生焦点语义、初始焦点、Tab 双向环、idle Escape、busy Escape 阻断和完成后焦点归还。
  关闭、失败、注销、离页、切租户或失权必须清空 reason/decision/金额和幂等 ticket。
- PAGE state 复用 PRD-079 owner/generation/Abort 模式：完整身份改变立即清 DOM；迟到 GET/PATCH 成功不得回填。
- reason/decision reason 仅在已授权 detail 中由用户显式展开后临时展示并转义；关闭、离页、失权或 owner 变化立即
  从 DOM 清除。正式 Browser 截图必须保持自由文本区域折叠，不包含 reason/decision reason；Console event 不含
  reason、金额明细、request 列表或幂等键。
- 固定尺寸约束避免 loading、badge、按钮改变布局；1280x720 和 390x844 无页面级横向溢出、文本溢出或重叠。

## 13. Migration 0073

### 13.1 up

1. preflight 要求当前 `72/0072`、RBAC revision 15/目录 hash、Audit/Webhook/FinOps/Quota owner 完整、FK=0。
2. 暂停相关 owner trigger，RBAC catalog 提升到 16，增加三权限；只扩展迁移前精确匹配默认矩阵的 admin/
   finance role，保留已自定义角色，不按角色名覆盖 tenant 决策。
3. 重建权限、角色摘要、新租户初始化和不可变目录 trigger；更新 Shared 常量 hash 与数据库终态 guard。
4. 创建八张 tenant-qualified 表、复合唯一/外键、active hold partial unique、查询索引和 owner/immutable/CAS trigger；
   scheduler 表以全局唯一领取序列和独立行 revision 固定公平水位。
5. 重建 Audit owner trigger 以允许并逐字段验证 BillingDispute 动作；扩展 Webhook 事件目录和 source recovery guard。
6. 写 `schema_migrations(version=0073)`；最终 guard 验证 schema、trigger、index、RBAC hash、FK 和 integrity 后提交。
7. repeat 不重复修改 rowid、权限 revision、角色 hash、业务事实或 trigger SQL。

### 13.2 down

- 首个 DDL 前 fail closed：存在任一 dispute/item/hold/transition/link/event/outbox、BillingDispute Audit、
  事件订阅/投递、approved adjustment link，或新权限关系偏离迁移自有默认时，固定错误并原子阻断。
- 空业务库允许删除 PRD-080 表，恢复 Audit/Webhook/RBAC revision 15、0072 新租户 trigger、目录/角色 hash 和迁移版本。
- down 后必须与权威 0072 schema、trigger SQL、index、数据、sqlite_sequence、integrity 和 FK 快照一致。

## 14. 安全、隐私与可观测性

- Provider Key、Virtual Key、Session/Authorization、Token、Prompt、Response、messages、raw usage、reason、
  decision reason 和幂等键不得明文进入日志、Webhook、Audit summary、截图清单或验收 JSON。
- reason/decision reason 只保存在 tenant-qualified 权威数据库；Audit、transition 对外摘要和事件只保存 SHA-256。
- API/HTML/日志均执行输出转义和 exact-field DTO；不得返回内部 SQL、lease token、HMAC secret 或 payload hash。
- 每个成功/失败 HTTP 响应带 request_id/trace_id；每个 dispute、transition、Adjustment、Entry、Audit、event/outbox
  可由安全 ID 和 tenant 追踪。Webhook 网络失败进入 outbox attempt/last_error_code，不回滚已提交业务事实。
- 验收证据只写固定状态、布尔、计数、版本和源码/结果 SHA-256；测试用 reason 使用无敏感固定夹具且不得写入 JSON。
- `all_108_prds_complete=false` 必须出现在设计、测试、README、目录说明、Runtime/Browser 证据与 Excel 状态。

## 15. 测试与实际验收计划

### 15.1 专项

- Shared：RBAC revision/hash、金额/时间/状态机、错误目录、Webhook exact payload/envelope 和敏感字段拒绝。
- Migration 0073：`73 -> repeat -> 72 -> 73`；四阶段 `integrity_check=ok`、外键违规 0、0072 快照全保真；
  dispute/link/事件/订阅/自定义授权存在时 down 首个 DDL 前阻断。
- Control API：request/time scope、list/detail、strict wire、金额、多币种、幂等、跨租户、RBAC、动态撤权、
  禁止自审、revision、并发 hold、approve/reject/cancel、Audit/FinOps/outbox 故障全回滚。
- FinOps：原账本 append-only、逐项 reversal、剩余额竞争、PRD-071 marker 兼容、active hold 单行投影、分页不扩行。
- Outbox：snapshot、tenant 公平、lease takeover、迟到 owner、退避/第十次终态、坏 source 隔离、重启恢复。
- Console：owner/generation/Abort、导航权限、申请/审核 Dialog、焦点、Escape、reason 清除、迟到响应和移动布局。

### 15.2 必要回归

- `control-plane-api/tests/integration/test_finops.py`、PRD-071 FinOps/Output Safety、PRD-078 动态权限、
  PRD-079 Plan/Quota/FinOps/Webhook，以及 Console FinOps/PlanBilling/navigation。
- 只有 ledger schema 或 Gateway owner 合同实际变化时运行对应 PRD-051/071/079 Gateway 回归；不重复无关全量。
- 先运行受影响专项；完成门禁再运行 Console 全量和必要 Python 回归。

### 15.3 Runtime

- 从仓库内空 SQLite 实迁移 `73/0073 -> repeat -> 72/0072 -> 73/0073`，记录 checksum/integrity/FK。
- 启动真实 Control/Gateway、loopback Provider 与 Webhook receiver，生成 canonical Usage + FinOps charge。
- 使用互不重叠的 canonical charge 集合分别实际执行 request-scope 与 time-range，再执行 start review、approve、reject；
  证明两种范围均独立命中且不因 active hold 互相干扰，并证明 active hold、逐项 Adjustment/Entry、reject 零 Adjustment、
  Audit/transition/source/outbox 数量精确。
- 验证无权 403、跨租户 404、幂等冲突 409、动态撤权 403；Control 重启后 created event 以稳定 delivery ID
  至少一次恢复，幂等接收端按稳定 delivery ID 只接受一次逻辑事件；两个 created event 的 outbox 最终均为
  succeeded delivery 事实。
- 停服后 owned 端口、进程、SQLite WAL/SHM、pending lease 和临时目录均为 0。

### 15.4 Browser

- 真实 PAGE-112 覆盖 1280x720、390x844，至少保存桌面/移动各一张列表和审核终态非空 PNG。
- 覆盖 request/time 申请、详情、start review、部分 approve、reject、hold/Adjustment 时间线、权限撤销、注销。
- 检查焦点环、busy Escape、owner 失效、迟到响应、HTML 转义、页面/文本/控件溢出与重叠。
- 外部请求、Console error、Page error、敏感明文命中均为 0；manifest、截图和源码 provenance SHA-256 一致。

## 16. 编码前第一轮自审：已完成并修正

2026-08-15 由主 Agent 结合独立架构调查，按数据一致性、金额守恒、事务、并发、tenant owner、恢复和 down
逐项复核。发现与修正如下：

1. **P1：可变 head 仅冻结 dispute sequence 时，status 筛选会在翻页间漂移。** 已给 transition 增加自增序列，
   cursor 冻结 dispute/transition 双水位，列表按 transition as-of 状态筛选；详情仍读当前 head。
2. **P1：原表述要求所有终态 reviewer 非空，与申请人 cancel 冲突。** 已拆分 approved/rejected 与 cancelled
   约束；cancelled reviewer 必须为空，终态 reason/resolved_at 仍强制存在。
3. **P1：start_review actor 与最终审核 actor 未冻结接手语义。** 已明确任何具备 review 权限的非申请人可在
   under_review 接手，head 保存最终 reviewer，所有 actor 变化由 immutable transition 审计。
4. **P1：创建和迁移分别落表，笼统“tenant 幂等唯一”无法跨表证明。** 已增加 create/transition 两个域分离 hash；
   PATCH 全部共享 transition 域，阻断跨 dispute 重用同 key。
5. **P1：部分 approve 未说明遗漏 item 的 hold/账务终态。** 已冻结：至少批准一项；列出项逐条 reversal，遗漏项
   release hold 且不生成 Adjustment/link，transition 记录批准计数与 totals hash。
6. **P1：approve 与公开人工 reversal 可能竞争。** 保持单个 `BEGIN IMMEDIATE` 内逐项重算 current remaining，
   任何变化整单 409 且不释放 hold；已有 FinOps DB guard 再做第二层金额守恒。
7. **P1：历史 hold 直接 JOIN 会扩张 FinOps 分页。** 设计已要求只 JOIN partial-unique active hold，并测试同一
   charge 同时具有 PRD-071 marker、历史客户 dispute 和 active hold 时仍只有一条 ledger row。
8. **P1：down 若只看新权限行会被迁移自动默认关系永久阻断。** 已限定只在业务事实、订阅/投递、Audit、link
   或权限关系偏离迁移自有默认时阻断；纯默认空业务库可完整回到 0072。
9. **P2：created event 的 totals 若只信 JSON 可被 source 污染。** source owner trigger 和 worker rebuild 必须
   从 immutable items 重算 item_count/currency totals 并逐字段等值后才签名出网。

修正后没有未关闭 P0/P1；第一轮只关闭设计门禁，不代表实现完成。

## 17. 编码前第二轮自审：已完成并修正

2026-08-15 在第一轮修正基础上重新按权威范围、RBAC/自审、API 可实现性、Console 生命周期、隐私、兼容性和
验收覆盖逐项检查。发现与修正如下：

1. **P1：写接口虽在权限表要求 read，但写事务步骤只重读 create/review，动态撤销 read 后仍可能返回 detail。**
   已要求 POST、PATCH 路由和事务同时复核 read；approve 明确实时复核 read+review+finops write。
2. **P1：只在 router 或异常处理器设置 `no-store` 会漏掉认证、RBAC 或正文解析前失败。** 已固定由应用最外层
   `/billing-disputes` 前缀响应中间件覆盖所有成功与错误路径，并列入合同测试。
3. **P1：列表仅按 transition 还原 head，若直接读取当前 hold/link，双水位快照仍会泄露翻页后的批准变化。**
   已要求 hold 通过 created/released transition FK、link 通过 transition FK 在水位内重建。
4. **P1：详情允许展示 reason 与正式 Browser 截图禁止敏感自由文本之间存在验收冲突。** 已把自由文本改为授权用户
   显式展开后的临时 DOM，并要求正式截图保持折叠；清理和泄漏扫描覆盖 reason/decision reason。
5. **P1：Runtime 若让 request-scope 与 time-range 命中同一 charge，active hold 会让第二条路径只验证冲突。**
   已要求使用互不重叠 charge 集合，让两种 scope 都完成正向流程，冲突另以专项夹具验证。
6. **P1：幂等 payload 未绑定 actor、创建 scope 和 PATCH path target，tenant 内重用 key 可能误恢复其他资源。**
   已分别冻结 POST/PATCH canonical schema，绑定认证 actor；POST 覆盖完整 scope，PATCH 覆盖 dispute ID 与完整动作意图。
7. **P1：Adjustment link 与 hold 引用 transition，而原写入步骤在 transition 前插入它们，SQLite 即时 FK 不可执行。**
   已调整为先完成业务校验和 Adjustment/Entry，再 CAS head、插 transition，随后插 link/release hold；任一步失败仍整笔回滚。
8. **P1：HTTP Webhook 无法证明传输层 exactly-once。** 已改为稳定 delivery ID 的 at-least-once 恢复，接收端按 ID
   去重；验收断言一次逻辑接受和一条 succeeded delivery，而不作不可证明的网络承诺。

修正后没有未关闭 P0/P1，第二轮设计门禁关闭；此结论只授权开始 PRD-080 实现，不代表 PRD 已完成。

## 18. 实现落地

1. Shared 已发布 RBAC revision 16、错误/i18n、Webhook exact validator 及对应测试。
2. Migration 已新增 `0073_billing_dispute_adjustment_workflow.sql/.down.sql`，覆盖空库、重复、回退、重升和
   业务事实/授权漂移 guard。
3. Control 已新增 `billing_disputes/{models,routes,service,outbox,scheduler}.py`，并完成 router、lifespan
   scheduler、全路径 no-store、认证依赖、Audit owner/summary 和 stored-source Webhook 恢复接入。
4. FinOps 已提取调用方事务内 adjustment writer，增加 active billing hold 安全投影，公开接口和 PRD-071
   单行 read model 保持兼容。
5. Console 已新增 `lib/billingDisputes.js` 与 `features/billing-disputes/` owner/state/render/bind，完成 PAGE-112
   导航、权限、i18n、响应式样式、原生 Dialog、焦点和生命周期接入。
6. Runtime 与 Browser harness/driver/合同测试已完成；权威制品只包含安全状态、计数和 SHA-256。
7. API、测试记录、README、目录说明和 Excel `12_开发状态` 已同步，且始终保持
   `all_108_prds_complete=false`。
8. 完成门禁只登记下一项 PRD-081，不在本条提交中实现 SIEM 能力。

## 19. 实现后第一轮自审：已完成并修正

2026-08-15 按迁移/数据库 owner、账务守恒、并发、公平调度、Webhook 恢复和真实 Runtime 崩溃边界复核，
发现并关闭以下问题：

1. **P1：按 tenant 本地 revision 排序会让不同历史深度的 tenant 失去长期公平。** scheduler 改为全表唯一、
   单调递增的 `last_claim_sequence`；claim 与水位更新保持同一事务，并补充历史 revision 不等的轮转测试。
2. **P1：冻结 delivery 已进入 terminal failed 时，source 仍按退避做零网络“重试”。** worker 现在同轮把
   source 收敛为 failed，不增加虚假网络 attempt，也不会阻塞其他 tenant。
3. **P1：原 Runtime 崩溃注入可能终止 launcher 而非当前 Control owner，恢复证据不足。** 改为 Event gate
   阻塞第二次真实请求，再精确终止当前 Control 完整进程树；中途严格断言一次失败 attempt、pending delivery、
   过期租约接管和零残留 lease。
4. **P1：Runtime 只比较 event 摘要，不能证明接收端幂等坐标稳定。** 验收现直接比较数据库冻结的 delivery ID
   与跨重启 `X-MetaHub-Delivery`，同时断言 4 次网络投递、2 个逻辑事件和 2 次重复被接收端去重。
5. **P2：相邻 FinOps read model 可能因 active hold JOIN 扩行。** 必要回归确认 PRD-071 marker、历史 hold 与
   当前 hold 并存仍只返回一条 ledger row，原分录保持 append-only。

修正后 PRD-080 Python 专项、FinOps/Webhook/scheduler 必要回归和权威 Runtime 均通过；第一轮无未关闭 P0/P1。

## 20. 实现后第二轮自审：已完成并修正

第二轮聚焦 HTTP exact wire、Console owner/隐私、Browser 进程清理、旧 PRD 当前迁移兼容和文档证据：

1. **P1：portable Chrome launcher 已退出时，清理 facade 继续等待一次性 launcher close，会把真实 stdio EOF
   误报为超时。** PRD-080 facade 改为直接观察真实 browser 继承的 stdout/stderr；两个流均结束才报告排空，
   force fallback 始终只使用 CDP 返回的 browser PID。
2. **P1：正常关闭已结束 browser 后，fallback 仍可能对已退出 PID 执行 taskkill。** 清理增加两轮正常排空等待，
   仅在精确 owner 仍未退出时强制终止；最终四个固定端口均释放，本轮无活动残留进程。
3. **P1：登录等待先等 PAGE-112 DOM、后检查 login 200，网络失败会被误报为 DOM timeout。** Browser 驱动
   在提交前固定 `#billing-disputes`，先验证登录网络状态，再分别等待已认证 Shell 和 PAGE-112 root；合同测试
   锁定该顺序，真实 Chromium final 随后通过。
4. **P1：PRD-078/079 Control fixture 把“本 PRD 迁移已包含”误写为“它仍是最新迁移”。** fixture 现从空库
   执行完整 0073 链并分别确认 0071/0072 已应用；30 个旧业务用例在 RBAC revision 16 上通过，未放宽
   `matrix_integrity_failed` 的失败关闭语义。
5. **P2：API 文档只在 PATCH 小节列出 mutation exact 顶层，且详情子对象字段不完整。** 已补 POST 201/200
   exact 响应、重放链路语义和 item/hold/link/transition/event 全字段，文档与 Pydantic model/集合等值测试一致。
6. **P1：应用缓存中间件没有覆盖账单争议，未捕获异常响应可能缺少私有 no-store。** 已把整个路径前缀加入共享
   cache boundary，并以认证、校验、业务错误和未知异常合同锁定。
7. **P1：合法零金额 Usage charge 在余额辅助函数中被整数校验误判为数据库损坏。** 已先按非负金额恢复，零金额
   明确返回不枚举 owner 的 `404 billing_dispute.scope_not_found`，且不创建任何争议事实。
8. **P1：reason/decision reason 可由客户端复制到 `User-Agent` 或链路 Header，继而进入 Audit、响应、事件或日志。**
   写路由现把自由文本加入 Audit 请求元数据的敏感碰撞过滤；整个账单争议路径忽略客户端链路 ID，统一生成服务端 ID。
9. **P1：未知路由异常可能被 Starlette 重新抛给 Uvicorn 并记录异常正文。** 专用 `BillingDisputeRoute` 现只向外抛
   固定 `billing_dispute.unavailable`，共享错误出口保持统一 code、request_id/trace_id 和私有缓存头。
10. **P1：created Webhook 的多 item 汇总复用了单 item 十亿上限，合法大额汇总会被拒绝。** validator 现按
    `item_count * 1,000,000,000` 限制每个币种汇总，并继续要求 exact 字段、六位小数和冻结事实重算。

最终 Console 全量、lint、Browser final、迁移/Runtime、provenance、截图人工复核和敏感信息门禁均通过；
第二轮无未关闭 P0/P1。PRD-081 只作为下一条权威未完成项登记。
