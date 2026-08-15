# 账单争议与调整 API

本文档冻结 `PRD-080 - 账单争议与调整流程` 的公开合同。接口只处理当前租户的账单争议申请、
审核、结算暂缓和 FinOps 冲正，不调用支付渠道、不自动退款，也不修改原始 Usage 或费用分录。
设计与事务边界见
[PRD-080 设计文档](../prd/PRD-080-billing-dispute-adjustment-workflow.md)。

## 1. 路由、认证与租户边界

| 方法与路径 | 后端权限 | 用途 |
| --- | --- | --- |
| `GET /billing-disputes` | `billing.dispute.read` | 读取当前租户的快照分页列表 |
| `GET /billing-disputes/{billing_dispute_id}` | `billing.dispute.read` | 读取当前争议、费用项、暂缓、冲正和时间线 |
| `POST /billing-disputes` | `billing.dispute.read` + `billing.dispute.create` | 按 request ID 或时间段创建争议 |
| `PATCH /billing-disputes/{billing_dispute_id}` | 见第 5 节 | 开始审核、批准、拒绝或取消 |

四个接口均使用 Console Session Bearer Token。权限由后端逐请求验证；写入在
`BEGIN IMMEDIATE` 事务内再次读取 Session、tenant、actor 和实时权限。前端导航、按钮可见性、
角色名或正文里的自报字段不能补权。

`tenant_id` 和 actor 只从认证上下文恢复。公开请求不接受 `tenant_id`、actor、currency、status、
FinOps adjustment type 或 outbox 字段；跨租户对象与不存在对象同形处理。

所有成功和错误响应都包含服务端生成的 `request_id` 与 `trace_id`。由于详情和写入可携带授权自由文本，
整个 `/billing-disputes` 路径均忽略客户端 `X-Request-ID/X-Trace-ID`，并设置：

```http
Cache-Control: no-store, private
```

统一错误正文使用全局 envelope。客户端必须按 `code` 和 HTTP 状态处理分支，并保留链路 ID 排障；
不能依赖错误文案解析业务状态。

## 2. 通用 wire 规则

- `POST`、`PATCH` 必须恰好携带一个 `Content-Type: application/json`；只允许可选的 UTF-8 charset。
- 正文必须是 1..32768 bytes 的严格 UTF-8 JSON object；拒绝重复 key、未知字段、非有限数、超过
  5 层或 512 个节点的结构。
- 写接口必须恰好携带一个 `Idempotency-Key`。值为 8..128 个可见 ASCII 字符，服务端只持久化
  分域 SHA-256，不在响应、日志、Webhook 或验收制品中返回原值。
- reason 和 decision reason 经 NFKC 规范化并去除首尾空白；规范值必须为 1..2000 UTF-8 bytes，
  且不含 C0/C1 控制字符。Audit 请求元数据也把这些值作为敏感碰撞项处理，不能借 `User-Agent` 持久化原文。
- 单项金额只接受正的 ASCII 普通十进制字符串，最多六位小数；不接受符号、指数、Unicode 数字、
  JSON number 或超过 `1000000000.000000` 的值。响应金额固定为六位小数文本；created Webhook 的按币种汇总
  可由多个合法 item 相加，但每个值不得超过 `item_count * 1000000000.000000`。
- 所有时间输入必须是 UTC 秒精度 `YYYY-MM-DDTHH:MM:SSZ`。

## 3. `GET /billing-disputes`

允许以下单值 query；未知、重复或空值均返回 `422 billing_dispute.query_invalid`：

| 参数 | 约束 |
| --- | --- |
| `status` | `submitted/under_review/approved/rejected/cancelled` |
| `scope_type` | `request_ids/time_range` |
| `created_from` / `created_to` | UTC 秒精度；同时存在时 start 必须早于 end |
| `limit` | `1..100`，默认 `50` |
| `cursor` | 服务端签名且 tenant/filter/双水位绑定的不透明 Base64URL 游标 |

游标只接受规范、无 padding 的 Base64URL。首次读取冻结 dispute 与 transition 双水位；后续页按冻结
状态还原 reviewer、hold 和 Adjustment link，避免翻页期间的状态迁移造成重复、漏项或跨页漂移。

响应 exact 顶层字段：

```json
{
  "billing_disputes": [],
  "limit": 50,
  "next_cursor": null,
  "has_more": false,
  "request_id": "req_control_example",
  "trace_id": "trc_control_example"
}
```

每个 summary 包含：

```text
billing_dispute_id, tenant_id, scope_type, period_start, period_end, status,
state_revision, item_count, reason_sha256, created_by, reviewer,
disputed_currency_totals, approved_currency_totals, active_hold_count,
adjustment_count, event_delivery_status, review_started_at, resolved_at,
created_at, updated_at
```

`created_by/reviewer` 只投影同租户用户的 `user_id/display_name/email`。CNY 与 USD 分开汇总，不能相加。
列表不返回 reason、decision reason、幂等摘要、原始 Usage 或 Webhook payload。

## 4. 创建与读取详情

### 4.1 `POST /billing-disputes`

按 request ID 创建：

```http
POST /billing-disputes
Authorization: Bearer <session-token>
Content-Type: application/json
Idempotency-Key: billing-dispute-create-example

{
  "scope_type": "request_ids",
  "request_ids": ["req_example_01"],
  "reason": "计费结果需要人工复核"
}
```

`request_ids` 必须为 1..100 个不重复的当前租户 canonical 已计费请求；顺序不影响幂等意图。
该模式不接受 `period_start/period_end`。

按时间段创建：

```json
{
  "scope_type": "time_range",
  "period_start": "2026-08-01T00:00:00Z",
  "period_end": "2026-08-02T00:00:00Z",
  "reason": "指定时段费用需要人工复核"
}
```

时间范围采用 `[period_start, period_end)`，最长 31 天，end 不得晚于事务内数据库时钟；该模式不接受
`request_ids`。服务端冻结创建时已存在且仍有可冲正余额的 1..100 条 charge，之后到达的费用不会进入旧争议。

首次创建返回 201；同 tenant、同 key、同规范意图返回既有结果和 200，且
`idempotent_replay=true`。同 key 异意图返回 409。创建事务原子写入 dispute、items、active holds、
create transition、Audit、`billing_dispute.created` source 与 outbox；网络投递发生在提交后。

首次和幂等重放的响应 exact 顶层字段均为：

```text
dispute, transition, adjustment_links, idempotent_replay, request_id, trace_id
```

首次创建为 `201/idempotent_replay=false`，重放为 `200/idempotent_replay=true`。创建响应的顶层
`adjustment_links=[]`，`transition` 与 `dispute.transitions[0]` 是同一 create 事实，固定为
`action=create`、`from_status=null`、`to_status=submitted`、`expected_revision=0`、`new_revision=1`。
顶层 `request_id/trace_id` 始终属于本次 HTTP 请求；幂等重放时，`dispute.create_request_id/create_trace_id`
及 create transition 的 `operation_request_id/operation_trace_id` 仍保留首次创建链路。

### 4.2 `GET /billing-disputes/{billing_dispute_id}`

详情顶层字段固定为 `dispute/request_id/trace_id`。`dispute` 包含：

```text
summary, reason, decision_reason, create_request_id, create_trace_id,
items, holds, adjustment_links, transitions, event
```

每个 item 的 exact 字段为：

```text
billing_dispute_item_id, finops_ledger_entry_id, usage_ledger_id,
source_request_id, source_trace_id, ledger_sequence, currency, amount,
disputable_amount, current_disputable_amount, price_snapshot_id,
price_schema_version, price_snapshot_source, price_evidence_status,
created_at, hold, adjustment_link
```

`amount` 是原始 usage charge 金额；`disputable_amount` 是创建时冻结值；
`current_disputable_amount` 是按当前正式 FinOps 账本重新计算的批准上限。`price_evidence_status` 是定价证据
状态，不是价格正文。item 内的 `hold` 必须与顶层 `holds` 中同 ID 对象相等；`adjustment_link` 未批准时为
null，批准后必须与顶层 `adjustment_links` 中同 ID 对象相等。

hold exact 字段：

```text
billing_settlement_hold_id, billing_dispute_item_id, finops_ledger_entry_id,
status, release_reason, state_revision, created_transition_id,
released_transition_id, created_at, released_at, updated_at
```

Adjustment link exact 字段：

```text
billing_dispute_adjustment_link_id, billing_dispute_item_id,
finops_adjustment_id, reversal_finops_ledger_entry_id, amount, currency,
billing_dispute_transition_id, created_at
```

transition exact 字段：

```text
billing_dispute_transition_id, action, from_status, to_status,
expected_revision, new_revision, actor_user_id, decision_reason_sha256,
approved_item_count, approved_totals_sha256, operation_request_id,
operation_trace_id, created_at
```

event exact 字段：

```text
billing_dispute_event_id, event_type, delivery_status, attempt_count,
last_error_code, occurred_at, succeeded_at
```

`summary.event_delivery_status` 是列表/摘要投影，`event.delivery_status` 是详情中的事件对象字段；两者位置
不同且不能改名或合并。

详情可以返回申请和决定理由，但禁止返回 raw/normalized Usage JSON、幂等摘要、租约 owner/token、
Webhook Secret、Authorization、Provider Key、Prompt 或 Response。

## 5. `PATCH /billing-disputes/{billing_dispute_id}`

状态机固定为：

```text
submitted -> under_review -> approved
                         \-> rejected
submitted -> cancelled
```

所有动作要求 `billing.dispute.read`。`start_review/approve/reject` 还要求
`billing.dispute.review`，申请人不能审核本人争议；`approve` 额外实时要求
`billing.finops.write`。`cancel` 要求 `billing.dispute.create` 且只允许原申请人执行。

开始审核：

```json
{"action":"start_review","expected_revision":1}
```

批准至少一个 item：

```json
{
  "action": "approve",
  "expected_revision": 2,
  "decision_reason": "审核确认部分费用应冲正",
  "adjustments": [
    {
      "billing_dispute_item_id": "bdsi_00000000000000000000000000000000",
      "amount": "1.000000"
    }
  ]
}
```

每个批准金额必须同时不超过 item 的冻结值和事务内重新计算的当前余额。服务端为列出的 item 各追加一条
既有 FinOps reversal Adjustment/ledger entry；未列出的 item 只释放 hold，不产生冲正。任一金额、权限、
Audit、FinOps 或 owner 校验失败会回滚整笔决定。

拒绝或取消只接受 `action/expected_revision/decision_reason`；开始审核只接受
`action/expected_revision`。终态不可重写，旧 revision 返回 409。

成功响应 exact 顶层字段：

```text
dispute, transition, adjustment_links, idempotent_replay, request_id, trace_id
```

首次与精确幂等恢复均返回 200。恢复不会重复写 transition、Audit、hold release、Adjustment 或 ledger。

## 6. 暂缓、FinOps 与审计

- 每个 item 在创建事务生成一条 active settlement hold；同一 charge 同时只能属于一个 active dispute。
- hold 是结算 overlay，不修改原 `finops_ledger_entries.status`，也不阻止 Gateway 继续记录后续 Usage。
- approve/reject/cancel 都单向释放全部 hold。只有 approve 的列出项生成 append-only reversal 和
  `billing_dispute_adjustment_links`。
- create/start_review/approve/reject/cancel 各有一条 immutable transition 和一条安全 Audit；Audit 只保存
  reason hash、计数和金额汇总 hash，不保存自由文本，`User-Agent` 也经过 reason/decision reason 碰撞过滤。
- 用量、原始费用、冲正、失败请求、request_id 与 trace_id 保持可追踪；原始 Usage 和 charge 不被覆盖或删除。

## 7. Webhook 与恢复

只新增 canonical `billing_dispute.created`。exact payload 为：

```json
{
  "schema_version": 1,
  "billing_dispute_id": "bdsp_00000000000000000000000000000000",
  "scope_type": "request_ids",
  "item_count": 1,
  "currency_totals": {"CNY": "1.000000"},
  "status": "submitted",
  "created_at": "2026-08-15T00:00:00Z"
}
```

payload 不包含 request ID 列表、时间范围、reason、email、价格正文或凭据。创建事务冻结 delivery ID
集合；之后新增订阅不追溯旧事件。worker 使用 90 秒租约、完整 generation/owner/token/expiry CAS 和
持久 tenant 公平水位，网络语义为稳定 event/delivery ID 的 at-least-once。失败按 5/30/300 秒退避，
最多 10 次；零网络调用的协作停服会归还 claim 且不消耗 attempt。Webhook delivery 失败不会把 source
outbox 误标为成功；冻结 delivery 已经 terminal failed 时，source 必须在同轮收敛为 failed，不能继续执行
零网络的 source 假重试。坏 source 会被隔离后继续处理其他 tenant。

## 8. 错误码

| HTTP | code | 场景 |
| ---: | --- | --- |
| 401 | `auth.*` | Session 缺失、无效、过期或已撤销 |
| 403 | `auth.permission_denied` | 当前实时权限不足 |
| 403 | `billing_dispute.self_review_forbidden` | 申请人尝试审核本人争议 |
| 404 | `billing_dispute.not_found` | dispute/item 不存在或跨租户 |
| 404 | `billing_dispute.scope_not_found` | 范围内没有 eligible charge |
| 409 | `billing_dispute.hold_conflict` | charge 已有 active dispute hold |
| 409 | `billing_dispute.revision_conflict` | expected revision 过期 |
| 409 | `billing_dispute.transition_invalid` | 非法状态迁移或终态重写 |
| 409 | `billing_dispute.adjustment_conflict` | 当前可冲正余额变化或金额超限 |
| 409 | `billing_dispute.idempotency_conflict` | 同 key 对应另一规范意图 |
| 422 | `billing_dispute.query_invalid` | query 或 cursor 非法 |
| 422 | `billing_dispute.request_invalid` | Header、JSON、范围、reason 或字段非法 |
| 422 | `billing_dispute.scope_too_large` | 时间或 item 数量超限 |
| 422 | `billing_dispute.adjustment_invalid` | approve item/金额组合非法 |
| 503 | `billing_dispute.integrity_failed` | 持久 owner、快照或关联损坏 |
| 503 | `billing_dispute.unavailable` | 数据库、Audit、FinOps、outbox 或未知内部路径暂不可用 |

未知内部异常在专用路由边界收敛为上述固定 503，不能把异常正文或 traceback 重新抛给进程访问日志。
错误正文、结构化日志和 Audit `User-Agent` 不得包含 SQL、数据库 URL、reason、幂等键、payload、Endpoint、
Provider Key、Token、Prompt 或 Response。测试范围与最终运行证据见
[PRD-080 测试记录](../testing/PRD-080-tests.md)。总体状态继续保持
`all_108_prds_complete=false`。
