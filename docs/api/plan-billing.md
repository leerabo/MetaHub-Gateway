# 套餐、试用与额度 API

本文档冻结 `PRD-079 - 套餐、试用与额度类型` 当前公开合同。接口只管理当前租户的
Plan、单次试用生命周期、四类额度桶和只追加额度调整，不包含在线支付、订单、充值渠道或
PRD-080 的后续能力。设计背景见
[PRD-079 设计文档](../prd/PRD-079-plan-trial-quota-types.md)。

## 1. 路由、认证与租户边界

| 方法与路径 | 后端权限 | 用途 |
| --- | --- | --- |
| `GET /plans` | `billing.plan.read` | 读取当前租户唯一 Plan 和四类额度桶 |
| `PATCH /plans` | `billing.plan.write` | 创建或按 revision CAS 修改当前租户 Plan |
| `GET /quota-buckets` | `billing.quota.read` | 读取四类额度、分摊汇总、overage 和最近调整 |
| `POST /quota-adjustments` | `billing.quota.write` | 创建一个只追加 credit/debit 调整 |

四个接口均要求 Console Session Bearer Token。权限由后端逐请求读取实时 permission 快照；
前端菜单或按钮是否可见不构成授权。写接口进入 `BEGIN IMMEDIATE` 后还会重新认证 Session、
tenant 和对应写权限，避免已撤权或已换租户的旧页面继续提交。

`tenant_id` 和 actor 只从认证上下文或事务内 Session 恢复。所有接口都拒绝任意 query 参数；
写入正文也不接受 `tenant_id`、`plan_id`、`quota_bucket_id`、actor、currency、余额、派生状态或
结果 revision。跨租户对象不能通过客户端 ID 定位。

所有成功响应包含 `request_id` 和 `trace_id`。所有成功、认证失败、权限失败、输入失败和业务
错误响应都设置：

```http
Cache-Control: no-store, private
```

统一错误正文的字段集合固定为：

```json
{
  "code": "plan.request_invalid",
  "message": "Plan request is invalid.",
  "request_id": "req_control_...",
  "trace_id": "trc_control_...",
  "suggestion": "Use the documented exact Plan fields and current revision.",
  "diagnosis": {
    "rule_id": "unknown_error",
    "category": "unknown",
    "reason": "未命中特定错误归因规则。",
    "suggestion": "使用 request_id 查看调用日志和上游安全摘要，必要时联系平台管理员。",
    "action": "检查请求日志、Provider 状态、模型别名配置和最近的错误事件。",
    "severity": "warning",
    "retryable": false,
    "matched_by": "unknown",
    "safe_summary": "plan.request_invalid http_status=422"
  }
}
```

`diagnosis` 的具体安全诊断随错误码变化；客户端必须以 `code` 和 HTTP 状态处理业务分支，
并保留 `request_id/trace_id` 用于追踪。

## 2. 共享响应对象

### 2.1 `PlanInfo`

| 字段 | 类型与语义 |
| --- | --- |
| `plan_id` / `tenant_id` | 服务端 owner ID |
| `display_name` | NFKC 规范化并去首尾空白后的名称 |
| `base_tier` | `free` 或 `paid` |
| `effective_tier` | active 试用时为 `trial`，否则等于 `base_tier` |
| `status` | `active` 或 `suspended` |
| `trial_status` | `not_started`、`active` 或 `expired` |
| `trial_started_at` / `trial_ends_at` | UTC 时间或 `null` |
| `trial_cycle` | 从未开始为 `0`，首次开始后固定为 `1` |
| `state_revision` | Plan CAS revision，正整数 |
| `quota_generation` | 可用额度周期 generation，正整数 |
| `created_at` / `updated_at` | UTC 时间 |

### 2.2 `QuotaBucketInfo`

| 字段 | 类型与语义 |
| --- | --- |
| `quota_bucket_id` / `tenant_id` / `plan_id` | 服务端 owner ID |
| `quota_type` | `paid`、`free`、`trial` 或 `gift` |
| `granted` / `consumed` / `reserved` / `available` | 固定六位 CNY 文本；`available = granted - consumed - reserved` |
| `status` | `active`、`exhausted`、`expired` 或 `suspended` |
| `expires_at` | 仅 trial 桶可为 UTC 时间；其余类型为 `null` |
| `state_revision` | Bucket CAS revision，正整数 |
| `allocation_consumed` | 权威 `quota_usage_allocations` 的该桶累计分摊金额，固定六位 CNY 文本 |
| `allocation_count` | 该桶不可变 Allocation 行数，非负整数 |
| `created_at` / `updated_at` | UTC 时间 |

只要 Plan 存在，响应必须包含完整四桶，并严格按实际消费顺序返回：

```text
trial -> free -> gift -> paid
```

该顺序同时用于 Gateway 预留与真实结算。不能按 `paid/free/trial/gift` 枚举声明顺序、数据库
排序或页面展示顺序推测消费优先级。损坏或不完整的四桶集合会失败关闭，不返回部分余额。

所有金额都是普通十进制字符串，不是 JSON number；客户端不得用二进制浮点重算财务结果。

## 3. `GET /plans`

请求不接受 query 参数。无 Plan 时返回 200，不自动创建任何事实：

```json
{
  "plan": null,
  "quota_buckets": [],
  "trial_cycle_changed": false,
  "request_id": "req_control_...",
  "trace_id": "trc_control_..."
}
```

有 Plan 时 `plan` 为完整 `PlanInfo`，`quota_buckets` 为四个完整 `QuotaBucketInfo`，且
`trial_cycle_changed` 在只读响应中始终为 `false`。顶层字段集合精确为：

```text
plan, quota_buckets, trial_cycle_changed, request_id, trace_id
```

## 4. `PATCH /plans`

请求必须使用 `application/json` 媒体类型；可带 `charset` 参数。正文必须是 1..8192 bytes 的
严格 UTF-8 JSON object，任意层重复键、未知字段、C0 控制字符、超过 8 层或超过 64 个节点均
返回 `422 plan.request_invalid`。

正文必须恰好包含以下五个字段，不能省略 `trial_ends_at`：

```http
PATCH /plans
Authorization: Bearer <session-token>
Content-Type: application/json

{
  "expected_revision": 0,
  "display_name": "Enterprise Plan",
  "base_tier": "paid",
  "status": "active",
  "trial_ends_at": "2099-08-14T12:00:00Z"
}
```

| 字段 | 精确约束 |
| --- | --- |
| `expected_revision` | 原生 JSON integer，最小 `0`；不接受 boolean、字符串或小数 |
| `display_name` | 原生 string；NFKC 后去首尾空白，长度 1..80 个字符，不含 C0 控制字符 |
| `base_tier` | `free` 或 `paid` |
| `status` | `active` 或 `suspended` |
| `trial_ends_at` | `null` 或规范 UTC 秒级文本 `YYYY-MM-DDTHH:MM:SSZ`；不接受偏移或小数秒 |

`expected_revision=0` 是 create-only 意图。当前租户已有 Plan 时返回
`409 plan.revision_conflict`；创建成功会在同一事务生成四个零余额桶，Plan 和 Bucket 的初始
revision 均为 `1`。非零 revision 必须等于事务内当前值，否则返回同一冲突错误。

试用是单周期状态机：

- `not_started` 提交未来 `trial_ends_at` 会首次启动试用并把 `trial_cycle` 设为 `1`。
- `active` 可提交新的未来时间延期；提交 `null` 会结束试用并保留原始起止证据。
- `expired` 不允许通过 PATCH 开启第二次试用；`null` 或原历史结束时间只保持终态。
- 时间是否在未来使用事务内数据库 UTC 时钟判断。请求到达时已到期的试用先持久化到期事实，
  原 PATCH 再以 `409 plan.revision_conflict` 结束，不能回滚或续期已发生的到期。

成功返回 200 和完整 `PlanResponse`，字段与 `GET /plans` 完全相同。只有本次首次启动试用时
`trial_cycle_changed=true`；普通创建、修改、延期、结束和只读均按实际状态返回。Plan 写入、
四桶治理变更和 `plan.created`/`plan.updated` Audit 在同一事务提交。

## 5. `GET /quota-buckets`

请求不接受 query 参数。无 Plan 时 exact 响应为：

```json
{
  "plan": null,
  "quota_buckets": [],
  "overage": {
    "amount": "0.000000",
    "settlement_count": 0
  },
  "recent_adjustments": [],
  "request_id": "req_control_...",
  "trace_id": "trc_control_..."
}
```

有 Plan 时顶层字段集合保持不变：

| 字段 | 精确语义 |
| --- | --- |
| `plan` | 完整 `PlanInfo` |
| `quota_buckets` | 按 `trial/free/gift/paid` 排列的四个完整 `QuotaBucketInfo` |
| `overage.amount` | `status=overage` Settlement 的 `unallocated_micros` 总和，固定六位 CNY 文本 |
| `overage.settlement_count` | 上述 Settlement 数量 |
| `recent_adjustments` | 当前租户最近 50 条调整，按创建时间和 ID 倒序 |
| `request_id` / `trace_id` | 当前读取链路标识 |

每个 `recent_adjustments` 元素字段精确为：

```json
{
  "quota_adjustment_id": "qadj_0123456789abcdef0123456789abcdef",
  "tenant_id": "tenant_default",
  "plan_id": "pln_0123456789abcdef0123456789abcdef",
  "quota_type": "gift",
  "direction": "credit",
  "amount": "12.345678",
  "actor_user_id": "user_admin",
  "operation_request_id": "req_control_adjustment_...",
  "resulting_bucket_revision": 2,
  "created_at": "2026-08-14T00:00:00Z"
}
```

`overage` 不属于 paid 桶，不能并入其余额或分摊统计。`allocation_consumed/count` 只从真实
Allocation 聚合，不能依据 FinOps 历史 `quota_type` 猜测。

## 6. `POST /quota-adjustments`

请求必须使用与 PATCH 相同的有界 exact JSON 解析器，并且 `Idempotency-Key` 必须恰好出现
一次。Header 值必须为 8..128 个可见 ASCII 字符（码点 33..126），不能含空格或控制字符。

```http
POST /quota-adjustments
Authorization: Bearer <session-token>
Content-Type: application/json
Idempotency-Key: quota_7f79e430f75d4db7

{
  "quota_type": "gift",
  "direction": "credit",
  "amount": "12.345678",
  "reason": "Contract credit",
  "expected_bucket_revision": 1
}
```

正文必须恰好包含以下五个字段：

| 字段 | 精确约束 |
| --- | --- |
| `quota_type` | `paid`、`free`、`trial` 或 `gift` |
| `direction` | `credit` 或 `debit` |
| `amount` | 原生 JSON string；正普通 ASCII 十进制，最多六位小数，不接受符号、指数或多余前导零，最大 `1000000000.000000` |
| `reason` | 原生 string；NFKC 后去首尾空白，长度 1..500 个字符，不含 C0 控制字符 |
| `expected_bucket_revision` | 原生 JSON integer，最小 `1` |

无 Plan 或桶返回 `404 quota.not_found`。Plan 暂停返回 `409 plan.suspended`。旧 Bucket revision、
已过期 trial 调整、debit 超过 `available`、credit 超过金额上限，均返回
`409 quota.adjustment_conflict`。debit 不能侵占已消费或已预留额度。

首次提交原子更新 Bucket、写 Adjustment 和 `quota.adjustment_created` Audit，返回 201：

```json
{
  "adjustment": {
    "quota_adjustment_id": "qadj_0123456789abcdef0123456789abcdef",
    "tenant_id": "tenant_default",
    "plan_id": "pln_0123456789abcdef0123456789abcdef",
    "quota_type": "gift",
    "direction": "credit",
    "amount": "12.345678",
    "actor_user_id": "user_admin",
    "operation_request_id": "req_control_adjustment_...",
    "resulting_bucket_revision": 2,
    "created_at": "2026-08-14T00:00:00Z"
  },
  "quota_bucket": {
    "quota_bucket_id": "qbk_0123456789abcdef0123456789abcdef",
    "tenant_id": "tenant_default",
    "plan_id": "pln_0123456789abcdef0123456789abcdef",
    "quota_type": "gift",
    "granted": "12.345678",
    "consumed": "0.000000",
    "reserved": "0.000000",
    "available": "12.345678",
    "status": "active",
    "expires_at": null,
    "state_revision": 2,
    "allocation_consumed": "0.000000",
    "allocation_count": 0,
    "created_at": "2026-08-14T00:00:00Z",
    "updated_at": "2026-08-14T00:01:00Z"
  },
  "plan": {
    "plan_id": "pln_0123456789abcdef0123456789abcdef",
    "tenant_id": "tenant_default",
    "display_name": "Enterprise Plan",
    "base_tier": "paid",
    "effective_tier": "paid",
    "status": "active",
    "trial_status": "not_started",
    "trial_started_at": null,
    "trial_ends_at": null,
    "trial_cycle": 0,
    "state_revision": 2,
    "quota_generation": 2,
    "created_at": "2026-08-14T00:00:00Z",
    "updated_at": "2026-08-14T00:01:00Z"
  },
  "idempotent_replay": false,
  "request_id": "req_control_adjustment_...",
  "trace_id": "trc_control_adjustment_..."
}
```

credit 会推进 Bucket revision、Plan revision 和 `quota_generation`；debit 只推进 Bucket
revision。结果的 owner、状态、余额和 revision 全部由服务端派生。

服务端只持久化 `Idempotency-Key` 的 SHA-256。相同 tenant、相同 key、相同规范化意图返回
首次 Adjustment 和当前关联 Plan/Bucket，HTTP 200 且 `idempotent_replay=true`；不会再次改桶、
写 Audit 或生成事件。相同 key 绑定不同规范化意图返回
`409 quota.adjustment_conflict`。不同租户的 key 空间相互隔离。

`reason` 明文只保存在权威 `quota_adjustments` 事实表。Audit 的安全摘要只保存
`reason_sha256`，结构化日志、错误、Webhook、`GET /quota-buckets` 和 POST 响应都不返回原因。
当前公开合同不存在 `GET /quota-adjustments/{id}` 或其他单条 reason 读取接口；客户端不能假设
提交后可通过 API 回读原因。

## 7. 错误码

| HTTP | code | 场景 |
| ---: | --- | --- |
| 401 | `auth.*` | Session 缺失、无效、过期或已撤销 |
| 403 | `auth.permission_denied` | 当前实时 permission 快照缺少路由要求的能力 |
| 404 | `plan.not_found` | 非零 revision 更新时当前租户没有 Plan |
| 404 | `quota.not_found` | 当前租户没有 Plan 或完整目标 Bucket |
| 409 | `plan.revision_conflict` | create-only 冲突、Plan CAS 漂移或请求前试用已到期 |
| 409 | `plan.suspended` | 暂停 Plan 上尝试额度调整 |
| 409 | `quota.adjustment_conflict` | Bucket CAS、余额、Trial 状态、上限或幂等意图冲突 |
| 422 | `plan.request_invalid` | Plan query、媒体类型、JSON、字段、枚举、名称或时间非法 |
| 422 | `quota.request_invalid` | Quota query、媒体类型、JSON、字段、金额、reason、revision 或幂等 Header 非法 |
| 503 | `plan.unavailable` | Plan 数据库、持久事实或 Audit 无法可信读写 |
| 503 | `quota.unavailable` | Quota 数据库、持久事实、调度或 Audit 无法可信读写 |

错误正文不得回显 tenant 外资源、余额、SQL、数据库 URL、幂等键或摘要、reason、请求正文、
Provider 响应、Provider Key、Token、Prompt 或 Response。

## 8. Webhook 与审计事件

### 8.1 `quota.exhausted`

一次额度 generation 内，Gateway 最坏费用估算无法由完整四桶覆盖时，业务事务写入唯一 source
event 和 outbox。对外信封字段及 payload 均为 exact 白名单：

```json
{
  "event_id": "qevt_0123456789abcdef0123456789abcdef",
  "event_type": "quota.exhausted",
  "timestamp": "2026-08-14T00:00:00Z",
  "tenant_id": "tenant_default",
  "actor_id": "quota-admission",
  "object_id": "pln_0123456789abcdef0123456789abcdef",
  "trace_id": "trc_gateway_...",
  "payload": {
    "schema_version": 1,
    "plan_id": "pln_0123456789abcdef0123456789abcdef",
    "quota_generation": 2,
    "required_cny": "2.000000",
    "available_cny": "1.000000",
    "reason_code": "quota_exhausted",
    "effective_tier": "paid"
  }
}
```

`required_cny` 必须为正，`available_cny <= required_cny`；两者均为固定六位 CNY 文本。
事件按 `(tenant, plan, quota_generation)` 唯一。credit 或首次启动试用推进 generation 后，新的
额度周期才可再次产生事件。

### 8.2 `trial.expired`

试用由请求前到期守卫或后台 scheduler 收敛时，写入：

```json
{
  "event_id": "qevt_abcdef0123456789abcdef0123456789",
  "event_type": "trial.expired",
  "timestamp": "2026-08-14T00:00:00Z",
  "tenant_id": "tenant_default",
  "actor_id": "quota-trial-scheduler",
  "object_id": "pln_0123456789abcdef0123456789abcdef",
  "trace_id": "trc_trial_expiry_...",
  "payload": {
    "schema_version": 1,
    "plan_id": "pln_0123456789abcdef0123456789abcdef",
    "trial_cycle": 1,
    "expired_at": "2026-08-14T00:00:00Z",
    "effective_tier": "paid"
  }
}
```

请求内到期守卫的 actor 为 `quota-expiry-guard`，scheduler 为
`quota-trial-scheduler`；`expired_at` 必须与信封 `timestamp` 同秒。事件按
`(tenant, plan, trial_cycle)` 唯一。

两类 payload 都不包含 Bucket 内部行、Reservation、Allocation、调整 reason、支付信息、模型、
Provider、Endpoint、凭据、Prompt 或 Response。source request ID 只用于内部权威行和信封一致性
校验，不属于对外 JSON 字段。source/outbox 先随业务事务提交，再由有租约的 worker 投递；网络
故障不会回滚 Plan、试用到期或额度阻断事实。

Plan 创建/更新和额度调整分别形成 `plan.created`、`plan.updated`、
`quota.adjustment_created` Audit。Audit 只保存当前租户 owner、版本、状态、精确金额和必要摘要；
额度调整原因只以 SHA-256 出现在 Audit 摘要，幂等键及其摘要不进入日志或对外事件。
