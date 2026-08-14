# PRD-079 套餐、试用与额度类型设计

状态：**已完成**（2026-08-14）。本文先完成两轮编码前自审再进入实现；实现后的账务、恢复与隐私复核见第 15、16 节。专项、必要回归、0072 空库迁移往返、真实 Runtime、PAGE-110/111 Browser、文档和提交前安全门禁均已通过，权威结果见 `docs/testing/PRD-079-tests.md`。PRD-078 已完成且不重复实现；本条只交付 PRD-079，保持 `all_108_prds_complete=false`，不得启动或混入 PRD-080。

## 1. 权威范围与交付边界

### 1.1 权威来源

- Excel `01_PRD总表` 第 80 行和 `03_M2-M3扩展交付` 第 16 行共同确认：
  `PRD-079 | M2-企业增强 | P2 | 扩展层 | 套餐计费 | 套餐、试用与额度类型`。
- 用户是财务、销售和客户成功；现有产品只有 `admin/developer/viewer/finance_admin/security_admin/ops_admin` 六个固定角色，本条不擅自增加销售或客户成功角色，而是增加可独立授权的套餐/额度权限，默认只授予 `admin` 和仍匹配默认矩阵的 `finance_admin`。
- PAGE-110 为套餐管理，PAGE-111 为额度页。
- Excel 的接口简写为 `GET/PATCH /plans`、`POST /quota-adjustments`。PAGE-111 还需要只读数据入口，因此增加同一资源域内的 `GET /quota-buckets`；它不是在线支付或 PRD-080 调整流程。
- 数据对象为 tenant-qualified `Plan`、`QuotaBucket`；额度类型固定为 `paid/free/trial/gift`。
- Webhook 事件固定为 `quota.exhausted`、`trial.expired`。
- TC-0157/0158 要求：试用额度耗尽不能污染或扣成负数的付费额度；报表必须按四种额度类型拆分；顺序、到期、权限、异常和统一错误必须可验证。

### 1.2 本条交付

1. 每个租户最多一个当前套餐，支持免费/付费基础层级、启用/暂停、试用覆盖层及试用到期回落。
2. 每个套餐固定四个额度桶：`trial/free/gift/paid`，金额以 CNY 微元整数保存和结算。
3. 管理员通过只追加、幂等、可审计的额度调整增加或减少指定额度桶；不连接支付渠道。
4. Gateway 在 Provider 前按最坏成本进行额度预留，防止并发请求超卖；Usage/FinOps 提交时按真实成本结算。
5. 一条 Usage 仍只产生一条正式 FinOps charge；额度分摊使用独立结算头和多条分摊事实，不拆分正式 charge。
6. 试用到期由 Control scheduler 和 Gateway 当前租户准入双重收敛；已准入的在途请求仍可按原预留结算，新请求不再使用过期试用额度。
7. `quota.exhausted`、`trial.expired` 先落 source/outbox，再由 Control worker 以租约、快照和重试协议投递现有 Webhook 系统。
8. PAGE-110/111 提供套餐 CAS 编辑、四类余额、按类型消费报表、调整历史和有后端权限约束的额度调整。

### 1.3 明确不做

- 不做在线支付、支付回调、订单、发票、自动续费、退款、自动扣款或支付渠道密钥。
- 不做 PRD-080 的账单争议、结算暂停、BillingDispute、AdjustmentRecord 或自动退款。
- 不新增销售/客户成功角色，不按前端按钮、角色名、客户端 `tenant_id` 或 URL 参数补权。
- 不把 Usage 拆成多条正式 FinOps charge，不改变缓存、实验、预算、请求解释和输出安全对单一 charge owner 的依赖。
- 不把 OpenRouter、硅基流动提升为默认上游。默认上游仍为智谱、通义、月之暗面、文心、火山、混元、DeepSeek 官方/云服务和客户本地 OpenAI-compatible Endpoint。

## 2. 现状约束与关键决定

### 2.1 已有正式账本合同

- `finops_ledger_entries` 以 `UNIQUE (tenant_id, usage_ledger_id)` 冻结一条 Usage 对一条正式 charge。
- `trg_usage_ledger_finops_projection_insert_prd051` 在 Usage 事务内生成正式 charge；Gateway 随后校验来源请求、金额、价格快照和租户 owner。
- Response Cache、在线实验、预算、Request Explanation、输出安全争议和 Webhook 都引用这条 canonical charge。
- 现有 `finops_ledger_entries.quota_type` 对 Usage 固定写 `paid`，它只能继续作为 PRD-051 的历史分录分类兼容字段，不能冒充 PRD-079 的实际分摊事实。

### 2.2 冻结解决方案

```text
route + token estimate + frozen price
                |
                v
       quota admission transaction
  Plan -> ordered QuotaBuckets -> Reservation + Items
                |
           call Provider
                |
                v
 Usage INSERT -> one canonical FinOps charge
                |
                v
 quota settlement in the same Usage transaction
 Settlement -> Allocation[trial/free/gift/paid] + bucket counters
                |
      report / event source + outbox
```

额度事实与预算事实相关但不同：预算仍按租户/Project/Application/Virtual Key 日上限独立约束；额度描述套餐可消费余额。两者都必须通过才可调用 Provider，任一阻断都不撤销另一领域已经提交的审计事实，但会释放本请求尚未使用的预留。

## 3. 权限、身份与租户隔离

### 3.1 RBAC revision 15

新增四个稳定权限，Shared catalog revision 从 14 提升到 15：

| permission | 风险 | 默认角色 | 用途 |
| --- | --- | --- | --- |
| `billing.plan.read` | read | admin、finance_admin | 读取 PAGE-110 套餐状态 |
| `billing.plan.write` | critical | admin、finance_admin | 创建/修改当前租户套餐与试用覆盖层 |
| `billing.quota.read` | read | admin、finance_admin | 读取 PAGE-111 额度桶、分摊报表和调整摘要 |
| `billing.quota.write` | critical | admin、finance_admin | 创建额度 credit/debit 调整 |

迁移只扩展仍精确匹配 revision-14 默认矩阵的 `admin/finance_admin`；所有自定义矩阵保持原样。后端每次请求从当前 tenant 数据库恢复权限，撤权立即生效。Console 只做入口裁剪，不承担授权。

### 3.2 owner 规则

- 公开 API 的 `tenant_id`、`actor_user_id`、request/trace identity 均由 Bearer Session 和统一 middleware 派生。
- 所有 SQL 的首要 owner 条件为 `tenant_id=?`；任何 Plan/Bucket/Reservation/Settlement/Allocation/Event 外键都使用 tenant-qualified 复合键。
- 跨租户 ID 与不存在 ID 对读取/修改接口保持同形 404；不通过错误差异枚举其他租户资源。
- Gateway 使用已认证 Virtual Key 的 tenant，不接受模型请求正文中的套餐、额度类型、余额或顺序字段。

## 4. 金额、顺序和时间口径

### 4.1 金额

- 所有套餐额度使用 CNY 微元：`1 CNY = 1_000_000 micros`。
- API 只接受普通十进制字符串，最多六位小数；禁止 JSON number、指数、正负号、NaN/Infinity 和浮点换算。
- 数据库只保存 `INTEGER` 微元，最大值与现有 FinOps 上限一致；加减前执行显式溢出检查。
- 报表和 API 输出统一为六位小数文本。

### 4.2 冻结消费顺序

顺序版本固定为 `trial_free_gift_paid_v1`：

1. `trial`
2. `free`
3. `gift`
4. `paid`

本版本每种类型只有一个当前桶，因此同类型不存在不确定排序。预留和真实结算都使用同一顺序；一笔请求可跨多个桶。试用桶耗尽后只把剩余金额继续分摊到后续桶，绝不改写、冲正或负扣 paid 桶既有余额。

### 4.3 上海业务时钟与试用边界

- 持久时间统一 UTC `Z`；页面按用户 locale 展示。到期比较由 SQLite `unixepoch` 使用数据库事务时钟完成，禁止相信浏览器时间。
- `now >= trial_ends_at` 时试用覆盖层到期：Plan 的 `trial_status` 从 `active` CAS 为 `expired`，Trial Bucket 从 `active/exhausted` 变为 `expired`，并生成唯一 `trial.expired` source/outbox。
- 到期前已经持有 reservation item 的请求保留结算权；到期只阻止新 reservation，避免 Provider 已调用后额度 owner 被回收。
- 有效层级为：active trial 时 `trial`；否则回落到 Plan 的 `base_tier=free|paid`。这是“试用到期降级”，但不会删除客户已购买的 paid 额度。

## 5. 数据模型

所有 ID 使用固定前缀和 32 位小写十六进制随机体；所有表默认包含 `tenant_id`。

### 5.1 `plans`

- `plan_id`、`tenant_id`，`UNIQUE(tenant_id)` 保证一个当前套餐。
- `display_name`：1..80 字符、无控制字符。
- `base_tier IN ('free','paid')`。
- `status IN ('active','suspended')`。
- `trial_status IN ('not_started','active','expired')`。
- `trial_started_at/trial_ends_at`：active 时都存在且 end > start；not_started 时都为空；expired 保留本周期不可变时间证据。
- `trial_cycle IN (0,1)`：0 表示从未开始，首次显式启动后固定为 1；v1 不允许已过期试用重新激活，用于 `trial.expired` 唯一事件源。
- `state_revision >= 1`：PATCH CAS。
- `quota_generation >= 1`：任何可用额度增加或新试用周期启动时递增；用于每个余额周期最多一个 `quota.exhausted` 事件。
- `created_at/updated_at`。

### 5.2 `quota_buckets`

- `quota_bucket_id`、`tenant_id`、`plan_id`、`quota_type`，`UNIQUE(tenant_id, plan_id, quota_type)`。
- `granted_micros/consumed_micros/reserved_micros` 均为非负整数，且 `consumed + reserved <= granted`。
- `status IN ('active','exhausted','expired','suspended')`。
- `expires_at` 只允许 Trial Bucket 使用，并与当前 Plan 试用周期一致；其他三类必须为 NULL。
- `state_revision >= 1`、`created_at/updated_at`。
- 创建 Plan 时原子创建四个零余额桶；不靠运行时缺省补表。

### 5.3 `quota_adjustments`

- 只追加事实：`quota_adjustment_id`、tenant/plan/bucket owner、`quota_type`、`direction IN ('credit','debit')`、正 `amount_micros`。
- 保存有界 `reason` 作为权威数据库事实，并在管理 AuditLog 中仅保存 SHA-256；轮询列表、Console 历史、日志、Webhook、错误和验收 JSON 均不返回 reason，PRD-079 不增加单条 reason 查询入口。
- 保存 `actor_user_id`、`operation_request_id/trace_id`、`expected_bucket_revision/resulting_bucket_revision`。
- 幂等只保存 `idempotency_key_sha256` 和规范 payload SHA-256；`UNIQUE(tenant_id,idempotency_key_sha256)`。
- 同一 key 的相同 intent 返回既有事实；任一字段不同返回 409，不再次改桶或写审计。

### 5.4 `quota_reservations` 与 `quota_reservation_items`

- Header：`quota_reservation_id`、tenant/plan/request/trace、`estimated_micros`、`outstanding_micros`、可空 `actual_micros`、`unallocated_micros`、order version、状态和过期时间。
- Header 状态：`active` 可直接进入 `settled|released|overage`，或在 Provider 结果仍可能计费时进入 `exposure_pending`；未决暴露到期后进入 `reconciliation_required`。除 `exposure_pending` 的迟到结算/释放外，终态不可逆；`UNIQUE(tenant_id,request_id)`。
- Item：tenant/header/bucket owner、quota type、priority、`reserved/consumed/released_micros`、状态。
- Active item 的 `consumed=released=0`；`exposure_pending` item 保留 `consumed + released < reserved` 的未决部分；终态 item 满足 `consumed + released = reserved`。
- Reservation 创建与 Bucket `reserved_micros` 增加在一个 `BEGIN IMMEDIATE` 事务完成。

### 5.5 `quota_usage_settlements` 与 `quota_usage_allocations`

- Settlement 对一条 canonical Usage/FinOps charge：tenant、plan、reservation、`usage_ledger_id`、`finops_ledger_entry_id`、request/trace、真实或保守结算金额、`exposure_outstanding_micros`、已分摊/未分摊金额、plan revision、order version。
- `amount_basis IN ('actual_cny','admission_estimate')`：价格可归一时用真实 CNY；已准入但最终价格证据不可归一时保守消费冻结预留并明确标记 estimate，绝不把未知成本当免费。
- `allocation_class IN ('paid','free','trial','gift','mixed','unallocated')`；`mixed` 表示同一 charge 分摊到多类，`unallocated` 表示无套餐或无可分摊事实。
- `status IN ('settled','overage')`；overage 的 `unallocated_micros > 0`，但 canonical Usage/FinOps 仍提交并可追踪。
- `UNIQUE(tenant_id,usage_ledger_id)`、`UNIQUE(tenant_id,finops_ledger_entry_id)` 和 `UNIQUE(tenant_id,quota_reservation_id)` 保证 spool 重放和恢复调度不重复结算。
- 数据库 trigger 把 `actual_cny` 精确绑定到 canonical FinOps 的 `normalized_cny_micros`；`admission_estimate` 只允许 FinOps 明确不可归一，且金额必须等于 `estimated - exposure`。直接 SQL 不能伪造 basis 或金额。
- Allocation 为不可变多行事实：tenant/settlement/bucket、quota type、sequence、正 amount micros；同一 settlement/bucket 唯一。

### 5.6 FinOps 兼容语义

- 不修改 `UNIQUE(tenant_id,usage_ledger_id)`，不拆分正式 charge。
- `finops_ledger_entries.quota_type` 保留为 PRD-051 历史“分录分类”字段，旧 Usage 固定 paid 的值不再作为额度消费依据。
- FinOps API 新增 `quota_allocation_class` 和 `quota_allocation_count`：Usage 从 tenant-qualified Settlement 派生；无 Settlement 为 `unallocated`；人工分录为 `not_applicable`。
- `ledger.entry_created` 在 Usage/额度事务提交后构造，并增加上述安全分摊摘要；报表只聚合 Allocation，绝不按历史 `finops_ledger_entries.quota_type` 猜测。

### 5.7 `quota_events` 与 `quota_event_outbox`

- Event 保存 event/tenant/plan/bucket owner、固定 event type、generation、request/trace、system/user actor、安全 `payload_json` 和发生时间。
- `quota.exhausted` 对 `(tenant, plan, quota_generation)` 唯一；credit 或新试用周期提高 generation 后可再次产生。
- `trial.expired` 对 `(tenant, plan, trial_cycle)` 唯一。
- Outbox 保存 pending/processing/dispatched/failed、attempt、owner/token/generation/lease、next attempt、delivery snapshot、稳定 error code 和时间；无法重建的持久 source 通过精确租约固定为 `webhook.payload_invalid`，同一 drain 继续扫描后续租户，不能形成全局队首饥饿。
- 业务事务只写 source/outbox；任何网络请求都在 commit 后由 worker 执行。

## 6. 状态机与事务

### 6.1 Plan PATCH

1. 先完成 Session 与 `billing.plan.write` 实时校验，再严格读取正文。
2. `BEGIN IMMEDIATE` 后重新验证权限指纹、tenant 和 expected revision。
3. expected revision 0 只允许创建；已有 Plan 则 409。更新必须精确命中当前 revision。
4. 首次启动试用要求当前为 `not_started` 且 `trial_ends_at` 在未来；把 trial cycle 固定为 1并递增 quota generation。已过期试用不能重新激活，避免重置 Trial Bucket 时破坏历史 Adjustment/Allocation。
5. 手工结束或事务时钟到期时进入 expired；已有 reservation 仍可结算。
6. 用户发起的 Plan PATCH 将 Plan、四桶、AuditLog 和必要 event source/outbox 原子提交；Control scheduler 与 Gateway guard 的系统到期没有用户 actor，以不可变 `trial.expired` source/outbox 作为系统审计事实，不伪造用户 AuditLog。

### 6.2 额度调整

1. 先校验 `billing.quota.write`，正文不得携带 tenant/actor/currency/granted/consumed/reserved/result revision。
2. `BEGIN IMMEDIATE` 后按 tenant + bucket owner 读取并复核实时权限。
3. credit 增加 granted，恢复非过期桶为 active，并递增 Plan quota generation；Trial credit 只允许当前 active 试用。
4. debit 只减少尚未消费、尚未预留的可用额；不足返回 409，不触碰 consumed/paid 等其他桶。
5. 调整事实、Bucket CAS、AuditLog 和必要事件原子提交；提交后才记录不含 reason 的结构化摘要。

### 6.3 Gateway 准入预留

1. Policy、路由、价格和 Budget Guard 仍先执行；Quota Guard 使用同一 token estimate、price-as-of 和主备最坏价格，不重新解释 Prompt。
2. 无 Plan 时保持现有租户向后兼容并跳过额度约束；Plan suspended 返回固定阻断错误。
3. active Plan 但缺少可信 CNY 价格时返回 `gateway.quota_unavailable`，不能用 0 预留。
4. `BEGIN IMMEDIATE`，先收敛当前 Plan 到期状态，再计算每桶 `granted-consumed-reserved`。
5. 按 `trial/free/gift/paid` 分摊最坏估算；全部覆盖才写 Header/Items 和 Bucket reserved。
6. 不足时不留下部分 reservation；提交唯一 `quota.exhausted` source/outbox 后抛 402。
7. Budget 已经创建的本请求预留由外层失败终态以 0 结算释放；Quota 阻断不能留下预算占用。

### 6.4 Usage/FinOps 结算

1. Usage INSERT 和 canonical FinOps charge 仍由现有事务形成。
2. 按 tenant/request 读取 Reservation，逐项验证 plan/bucket owner、状态、顺序、request/trace 和 charge owner。
3. 真实 CNY 小于预留：从首优先 item 消费，后续和尾部余量释放。
4. 真实 CNY 大于预留：先消费预留，再在当前事务按相同顺序使用剩余可用额；仍不足则写 overage/unallocated，不回滚已经真实发生的 Usage/FinOps。
5. Fallback 成功时 canonical actual 先按 `trial/free/gift/paid` 消费，主路由仍可能计费的金额以 `exposure_outstanding_micros` 独立保留；它不伪装成 charge，也不能抢占 actual 的优先桶。
6. 更新 Bucket consumed/reserved、Reservation/Items 终态，插入 Settlement/Allocations；任一 owner 或计数不一致则整个 Usage 事务回滚并进入既有安全 spool。
7. Usage spool v7 只新增规范 `quota_reservation_outstanding_cny`；v1-v6 exact-set 保持可恢复，v7+Batch 仍强制完整 Output Safety/Batch owner/estimate，v8 失败关闭。
8. spool replay 先检查唯一 Settlement；完全相同 owner、canonical charge、basis、amount 和 exposure 返回既有事实，冲突失败关闭。
9. commit 后的 FinOps event 从 Settlement 派生 allocation class；日志只记录 ID、类别、金额状态和 request/trace。

### 6.5 失败、取消与不确定结果

- 未调用 Provider：把本请求 Quota/Budget reservation outstanding 更新为 0并释放。
- Provider 结果确定失败且不计费：释放为 0。
- Provider outcome unknown 或 Usage 已进入 spool：把 Reservation 缩小为仍可能计费的主/备冻结估算并进入 `exposure_pending`，等待 canonical Usage replay；风险估算不写入正式 charge。
- Fallback 成功但主路由结果仍可能计费：备用实际费用形成唯一 canonical charge，主路由风险作为独立 exposure 保留；实际费用超过 admission 中可消费部分时继续补扣当前余额，仍不足写可追踪 unallocated，不能永久卡住 spool。
- Scheduler 同时扫描 `active/exposure_pending`。有 canonical charge、无 Settlement 时按 charge 补结算；已有 Settlement 的未决 exposure 到期后释放并标记 `reconciliation_required`；`request_log.failed` 后若已有 routing attempt，仍按结果未知处理，不能误称零费用。
- 超期且无 charge 的明确 Provider 前失败可释放；其他未知结果释放 Bucket 占用并留下 `reconciliation_required`，未来迟到 replay 形成可追踪 overage，而不是静默扣错桶。
- Response Cache 命中若正式 charge 为零，则释放全部预留并写零额 Settlement 摘要；不把未定价误称为免费。
- Batch 通过同一内部 Gateway 入口，以冻结批任务价格估算预留；不修改 PRD-074 的 Batch budget allocation owner。

## 7. HTTP API

所有响应带 `request_id/trace_id`，认证响应设置 `Cache-Control: no-store, private`；错误使用统一 ErrorResponse。

### 7.1 `GET /plans`

- 权限：`billing.plan.read`。
- 无客户端 tenant 参数。
- 返回当前 Plan、`effective_tier`、四类安全余额摘要和 revision；无 Plan 返回 `plan: null`，不自动创建。

### 7.2 `PATCH /plans`

- 权限：`billing.plan.write`。
- exact JSON：`expected_revision`、`display_name`、`base_tier`、`status`、`trial_ends_at`。
- `expected_revision=0` 创建；其余为 CAS 更新。未知/重复字段、非规范时间、过期 future 值和客户端 owner 字段固定 422。
- 返回 Plan、四桶摘要、是否发生试用周期切换和 request/trace。

### 7.3 `GET /quota-buckets`

- 权限：`billing.quota.read`。
- 返回固定四桶、按 paid/free/trial/gift 的 all-time consumption、allocation count、当前余额、overage/unallocated 独立摘要，以及最近 50 条脱敏调整摘要。
- reason 只留在权威数据库并以摘要进入 AuditLog；本条没有单条 reason 查询接口，额度页和轮询列表均不返回 reason，避免后台刷新复制业务正文。

### 7.4 `POST /quota-adjustments`

- 权限：`billing.quota.write`；要求唯一可见 ASCII `Idempotency-Key`。
- exact JSON：`quota_type`、`direction`、`amount`、`reason`、`expected_bucket_revision`。
- tenant、actor、currency、Plan ID、Bucket ID 和结果 revision 均由后端派生。
- 首次 201；完全相同幂等恢复 200；key intent 冲突 409。

## 8. 统一错误与观测

新增稳定错误码：

| code | HTTP | 场景 |
| --- | ---: | --- |
| `plan.request_invalid` | 422 | Plan 字段、时间或 revision 非法 |
| `plan.not_found` | 404 | 当前租户没有目标 Plan |
| `plan.revision_conflict` | 409 | CAS 漂移 |
| `plan.suspended` | 409 | 管理 API 对暂停 Plan 的冲突操作 |
| `plan.unavailable` | 503 | Plan 存储/审计不可用 |
| `quota.request_invalid` | 422 | Quota 查询/调整合同非法 |
| `quota.adjustment_conflict` | 409 | debit 超过可用额、Trial 已过期或幂等 intent 冲突 |
| `quota.not_found` | 404 | 当前租户无对应桶 |
| `quota.unavailable` | 503 | Control 额度服务不可用 |
| `gateway.plan_suspended` | 402 | active 套餐治理明确暂停调用 |
| `gateway.quota_exhausted` | 402 | 最坏估算无法由当前四桶完全覆盖 |
| `gateway.quota_unavailable` | 503 | 价格、表结构或结算 owner 不可信，失败关闭 |

成功、阻断、过期、调整、结算、overage、outbox retry 都记录 tenant/object/request/trace/error code。禁止记录 Provider Key、Token、Prompt、Response、Authorization、Webhook Secret、完整 reason、幂等键、SQL、数据库 URL 或底层异常正文。

## 9. Webhook source/outbox

### 9.1 `quota.exhausted`

安全 payload 精确包含：`schema_version`、`plan_id`、`quota_generation`、`required_cny`、`available_cny`、`reason_code`、`effective_tier`。不包含桶内部计数、请求正文、模型、Provider 或凭据。

### 9.2 `trial.expired`

安全 payload 精确包含：`schema_version`、`plan_id`、`trial_cycle`、`expired_at`、`effective_tier`。不包含用户正文、支付信息或历史调整原因。

### 9.3 恢复合同

- Worker 以 owner + token + generation + lease expiry CAS 领取；网络调用前冻结当前订阅 delivery snapshot。
- 成功标记 dispatched；可重试失败按 5/30/300 秒退避，最多 10 次；payload invalid 直接 failed。
- shutdown 不取消已经取得网络租约的有界投递；下一 owner 只能在 lease 到期后恢复。
- Control downtime 时 source/outbox 保留；恢复后真实 lifespan 自动 sweep/dispatch。

## 10. Console PAGE-110/111

### 10.1 PAGE-110 套餐管理

- 后端导航要求 `billing.plan.read`；保存按钮还要求当前 auth snapshot 有 `billing.plan.write`，但 PATCH 仍由后端再次授权。
- 显示基础层级、有效层级、状态、试用结束、revision 和四类余额摘要。
- 使用 native select、datetime-local、状态 segmented control 和明确保存命令；确认对话框展示无敏感摘要。
- PATCH 进行中禁止重复提交/离页重放；撤权、切租户、切页面、登出和迟到响应都清除草稿与 owner。

### 10.2 PAGE-111 额度页

- 后端导航要求 `billing.quota.read`；调整命令还需 `billing.quota.write`。
- 四类余额用紧凑表格和类型色标展示 granted/consumed/reserved/available；报表明确分开四类，overage/unallocated 作为独立告警行，不能塞入 paid。
- 调整使用 modal，类型/方向为选择器，金额为精确文本输入；reason 和幂等键只存在于当前 dialog owner 内存。
- 页面 state 绑定 tenant/session/permission revision/navigation；后台全壳刷新不能覆盖 busy dialog，迟到 200/401 不能恢复旧租户数据。

### 10.3 适配性

- 桌面和 `390x844` 移动视口均不得出现页面级横向溢出、文字截断、控件重叠或 nested card。
- 金额表格在窄屏切换为定义列表，不缩放字体随视口变化。
- 使用现有图标系统；不以隐藏按钮作为安全边界。

## 11. Migration 0072

文件：`infra/migrations/0072_plan_trial_quota_types.sql/.down.sql`。

### 11.1 Up

1. 首个持久 DDL 前验证 71/0071、revision-14 目录哈希、默认矩阵哈希、关键 FinOps/Usage trigger、数据库 integrity 和 FK。
2. 精确保留 rowid/permission id/time/custom grants，把 RBAC 目录提升到 15并增加四项权限；只扩展精确默认 admin/finance_admin。
3. 创建 Plan、Bucket、Adjustment、Reservation/Item、Settlement/Allocation、Event/Outbox 表、复合唯一索引及 owner/append-only/CAS trigger。
4. 不回填历史 Plan 或额度；历史 Usage 没有 Allocation，查询明确显示 unallocated。
5. 末尾再次验证表、索引、trigger、RBAC 目录/矩阵、integrity/FK 后提交。

### 11.2 Down

- 首个持久 DDL 前验证 72/0072 和 revision-15 精确基线。
- 若存在任何 Plan/Quota 业务事实、active reservation、Settlement/Allocation、Event/Outbox、针对新事件的订阅/投递、或新权限的自定义 grant，则失败关闭，绝不静默删业务数据或缩权。
- 空事实场景删除 0072 专属表/trigger/index，移除四项默认 grant，精确保留其他自定义矩阵并恢复 revision 14。
- 末尾验证 71/0071、RBAC、integrity/FK；支持 fresh -> repeat -> down -> re-up。

## 12. 测试与真实验收

### 12.1 专项

- Shared：revision-15 目录/矩阵哈希、金额/ID/事件 payload、FinOps allocation class 合同。
- Migration：fresh/repeat/down/re-up、0071 升级、rowid/schema/custom matrix 保真、down guard、trigger/owner/append-only、integrity/FK。
- Control：四权限、tenant 隔离、Plan create/CAS/试用周期、四桶、调整幂等、debit 边界、Audit/Event/Outbox、统一错误和隐私。
- Gateway：无 Plan 兼容、暂停、缺价、严格顺序、多桶分摊、并发预留、trial expiry race、paid 不受污染、真实/估算结算、overage、失败/取消/outcome unknown、spool replay、Cache、Batch。
- Console：API 白名单、owner/generation/Abort、撤权/切租户/离页/登出、Dialog busy/焦点、桌面/移动渲染。

### 12.2 必要回归

- PRD-051 FinOps trigger、spool 和 `ledger.entry_created`。
- PRD-052/074 Budget reservation 与 Batch allocation。
- PRD-075 Experiment settlement。
- PRD-076 Response Cache charge/settlement。
- Auth/RBAC、Console navigation 和 Webhook delivery snapshot。

### 12.3 实际迁移、Runtime 与 Browser

- 从空 SQLite 实际执行到 `72/0072`，再 repeat/down/re-up；每阶段 `integrity_check=ok`、FK 违规 0。
- Runtime 使用真实 Control/Gateway/Console：创建 Plan、试用/免费/赠送/付费额度，发起跨桶请求，验证 trial 到期回落、并发只放行可覆盖请求、spool/outbox 恢复、三角色权限、统一 request_id 和进程/端口/sidecar 清理。
- Browser 在桌面和移动视口实际操作 PAGE-110/111，检查权限裁剪、Plan CAS、Quota 调整、报表四分类、Dialog/focus、撤权/登出和零外部请求/Console error/布局溢出。
- 证据只白名单脱敏 JSON、PNG 和 SHA-256 manifest；不得包含数据库、WAL/SHM/journal、日志、Browser profile、Prompt/Response/Token/Key 或失败诊断批次。

## 13. 编码前第一轮自审：数据一致性、安全与恢复

状态：**已完成并修正**（2026-08-14）。

发现并纳入设计的修正：

1. 原始设想若按额度类型拆正式 FinOps charge，会破坏 Usage 唯一 owner、Response Cache、Experiment、Budget 和 Request Explanation；现冻结为一条 canonical charge + Settlement + Allocations。
2. 只在 Usage 后扣余额会让并发请求同时看到同一余额；现增加 `BEGIN IMMEDIATE` admission reservation 和四桶计数不变量。
3. 直接在到期时回收 Trial reservation 会让已调用 Provider 的请求失去结算 owner；现冻结“到期阻止新预留、既有预留继续结算”。
4. 只按 `finops_ledger_entries.quota_type` 报表会把跨桶请求错误归类 paid；现把它明确降为历史分录分类，新增 authoritative allocation class，报表只聚合 Allocation。
5. 缺价、USD 未归一、Cache 零额、Fallback、Batch 和 spool replay 原设计没有统一口径；现冻结同一 token/price-as-of 最坏估算、真实 CNY 优先、不可归一时保守消费 admission estimate，并保留 amount basis。
6. 同步投递事件会在写锁内调用网络并使业务终态受外部 Endpoint 影响；现增加 source/outbox、delivery snapshot、lease CAS 和恢复 worker。
7. 复用 `billing.finops.write` 无法单独撤销套餐或额度管理；现增加 plan/quota 四个独立权限并保持后端实时 RBAC。
8. 真实金额超过预留时若回滚 Usage 会造成 Provider 已成功但成本不可追踪；现允许同事务补用可用桶，仍不足写 overage/unallocated，同时保留 canonical charge。

## 14. 编码前第二轮自审：范围、可实现性、迁移与验收

状态：**已完成并修正，设计冻结**（2026-08-14）。

发现并纳入设计的修正：

1. 对所有历史租户自动创建零余额 Plan 会在上线后立即阻断流量；现规定“无 Plan 保持兼容并跳过，显式创建 active Plan 后才启用额度治理”。
2. 只依赖定时任务处理 Trial 到期会在 Control downtime 时让新请求继续使用过期额度；现由 scheduler 和 Gateway 当前租户 Guard 双重收敛同一 CAS。
3. 只保存 Reservation header 无法证明跨桶顺序，也无法精确释放尾部预留；现增加 item 行及 consumed/released 守恒。
4. 失败路径直接释放全部预留会低估 Provider outcome unknown；现复用调用状态机的主/备 outstanding 金额，spool replay 后再完成结算。
5. 只返回四类汇总会把历史未分配和异常 overage 偷塞入 paid；现把四类报表与 unallocated/overage 独立展示。
6. 简单 down 删除新表会丢失套餐/额度/事件事实或自定义 grant；现冻结业务事实、新事件订阅/投递和自定义新权限 grant 的 down fail-closed guard。
7. Excel 没列 PAGE-111 的 GET 路径，但页面不能从 PATCH/POST 猜余额；现增加同资源域的 `GET /quota-buckets`，不扩大到支付或 PRD-080。
8. Console 若沿用全壳定时刷新会销毁金额/reason dialog 或接受迟到跨租户响应；现要求独立 owner/generation/Abort 和 busy dialog 局部刷新合同。
9. 单一 Trial Bucket 与“重复开启试用并重置余额”无法同时保留历史守恒；PRD 未要求重复试用，现冻结 v1 每租户只允许一次试用周期，expired 不得重新激活。

两轮修正后，本设计的数据模型、API、状态机、顺序、权限、迁移、事件、测试与验收合同已冻结。下一步才允许修改 Shared、Migration、Control、Gateway 和 Console 生产代码。

## 15. 实现后第一轮自审：账务守恒、并发与恢复

状态：**已完成并修正**（2026-08-14）。

发现并关闭的问题：

1. 最初失败收口会把 Provider 风险估算当作 canonical charge；现由 `finalize_request_quota_failure` 只释放或缩小 exposure，正式费用仍只来自 Usage/FinOps。
2. Fallback 成功时 admission estimate 已包含主备最坏费用，若再次把主路由风险叠加到 actual 会重复占用；现 Settlement 分开保存 actual 与 exposure，且 `actual=None` 只消费 `estimated-exposure`。
3. actual 超过 admission 中可消费部分时，若只消费原 Reservation 会让真实 Usage 永久卡 spool；现先消费预留，再补扣当前可用桶，仍不足写显式 unallocated/overage。
4. 多桶缩容后的空终态 item 仍参与 CAS 会造成迟到结算冲突；现跳过无剩余 item，只修改仍持有 owner 的 `active/exposure_pending` 行。
5. 恢复任务原先只扫描 active，未决 exposure 会永久占额；现同时扫描 `active/exposure_pending`，区分已有 Settlement、仅有 canonical charge、Provider 前失败和已有 routing attempt 的未知结果。
6. Usage spool 需要持久化 exposure，同时不能破坏 v1-v6 或放松 Batch/Output Safety；现冻结 v7 exact-set、规范微元无损校验及 v7+Batch 完整证据，未来 v8 失败关闭。
7. Settlement trigger 原先只绑定 owner，直接 SQL 仍可伪造金额 basis；现把 `actual_cny` 绑定 canonical normalized CNY，把 `admission_estimate` 绑定 unavailable/NULL 与 `estimated-exposure`，并增加一 Reservation 一 Settlement 守卫和直接 SQL 负测。
8. 无法重建的 outbox source 原先会持续占据全局队首；现以 tenant/owner/token/generation 两步 CAS 隔离为 `webhook.payload_invalid`，同一次 drain 继续处理后续租户。

残余边界：Control scheduler 对脱离服务直接破坏的 Plan/Trial Bucket 图统一返回 `plan.unavailable`，不猜测或自动暂停租户；正常写路径由 Plan 创建事务、复合外键和不可删除 trigger 保证四桶完整。此类物理/人工数据库破坏需要运维修复，不在 PRD-079 内发明数据修复策略。

## 16. 实现后第二轮自审：权限、隐私、范围与兼容

状态：**已完成并修正**（2026-08-14）。

发现并关闭的问题：

1. 设计一度承诺授权页面单条读取 adjustment reason，但实现没有该 API，且轮询复制自由文本扩大隐私面；现冻结 reason 仅存权威表、AuditLog 只存摘要，Console/API/证据均不返回正文。
2. 设计把所有试用到期都写成用户 AuditLog，但 scheduler/Gateway 没有合法用户 actor；现区分用户 PATCH 的 AuditLog 与系统 `trial.expired` source/outbox，禁止伪造用户身份。
3. FinOps DTO 与 `ledger.entry_created` 已增加 `quota_allocation_class/count`，既有 PRD-051 文档也必须同步，不能只写新 Plan API 文档。
4. PRD-075 回归仍硬编码 0068 是最新迁移；现验证 0068 仍被应用并把 0072 作为当前头，真实 A/B Gateway 主流程继续通过。
5. PRD-069 无数据库阶段顺序夹具未隔离后来新增的 Cache/Quota 闸门；现显式替换两者，完整意图安全回归恢复通过，不修改生产阶段顺序。
6. 四项权限由依赖层和事务内实时权限指纹双重校验；Console 的按钮/导航裁剪只负责体验，跨租户 ID、不存在对象、撤权和迟到响应均不能绕过后端。
7. 日志、Webhook 和验收证据只保留安全 ID、状态、类别和有界金额；Provider Key、Token、Authorization、Prompt、Response、reason 与幂等键明文均不进入持久证据。
8. 本条没有支付、争议、退款或结算暂停实现；OpenRouter/硅基流动仍只是竞品与迁移兼容对象。下一项只能在本条提交推送并确认干净后另行启动，`all_108_prds_complete=false`。

## 17. 代码变更计划

- Shared：RBAC revision 15、统一错误、Quota/FinOps/Webhook 合同和运行时 reservation/settlement helper。
- Migration：`0072_plan_trial_quota_types.sql/.down.sql`。
- Control：`plan_billing` models/routes/service/scheduler/outbox，App 路由和 lifespan 装配。
- Gateway：Quota Guard、请求失败 finalizer、Usage/FinOps 同事务 settlement、spool replay idempotency。
- Console：Plan/Quota API client、PAGE-110/111 state/render/bind、导航、i18n 和样式。
- Tests/acceptance/docs：专项、必要回归、迁移、Runtime、Browser、README、目录说明、API、testing 和 Excel 状态。
