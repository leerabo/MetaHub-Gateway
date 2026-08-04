# FinOps 正式账本 API

本文档对应 `PRD-051 - FinOps Ledger 正式账本`。完整数据模型、事务边界、
Usage 持久恢复设计和两轮编码前自审见
[PRD-051 设计文档](../prd/PRD-051-finops-ledger.md)；测试、迁移和运行证据见
`docs/testing/PRD-051-tests.md`（仅在全部验收完成后形成最终结论）。

## 1. 范围与权限

| 服务 | 方法与路径 | 身份 | 后端权限 |
| --- | --- | --- | --- |
| Control API | `GET /finops/ledger` | Console Session Bearer Token | `billing.finops.read` |
| Control API | `POST /finops/adjustments` | Console Session Bearer Token | `billing.finops.write` |

PRD-059 之后权限由数据库 permission 矩阵决定，不再按角色名写死。当前固定角色中 Admin 同时拥有读写，
FinanceAdmin 拥有读权限但没有写权限；Developer、Viewer 等缺少相应 permission 的角色由后端返回
`403 auth.permission_denied`。自定义角色只获得显式授予的能力。Console 隐藏入口只是体验约束，不能替代
后端 RBAC。

租户和操作者只从 Session 恢复。客户端不得在 query/body 中发送 `tenant_id`、actor、
direction、currency、quota、价格快照或任意 SQL 排序字段。跨租户资源与当前租户不存在资源
使用同形 404，不能形成资源枚举侧信道。

所有成功和错误响应都携带 `request_id`、`trace_id`。财务响应使用 `Cache-Control: no-store`；
Console 跨域调用需要服务端允许并暴露现有链路头，同时允许请求头 `Idempotency-Key`。

## 2. 查询正式账本

```http
GET /finops/ledger?start_date=2026-07-01&end_date=2026-07-27&entry_type=usage_charge&limit=20
Authorization: Bearer <session-token>
X-Request-ID: console_finops_ledger_...
X-Trace-ID: trc_console_finops_...
```

只允许下列查询参数；未知参数、重复参数、显式空值或非法值统一返回
`422 finops.query_invalid`。

| 参数 | 约束 |
| --- | --- |
| `start_date` / `end_date` | 严格 ISO 日期；闭区间；同时提供时最长 366 天 |
| `entry_type` | `usage_charge/manual_debit/manual_credit/reversal/gift_credit/trial_credit` |
| `direction` | `debit/credit` |
| `quota_type` | `paid/free/trial/gift` |
| `request_id` | 精确匹配来源模型调用 |
| `price_snapshot_id` | 精确匹配入账价格快照 |
| `related_entry_id` | 匹配分录自身或冲正目标 |
| `limit` | ASCII 整数，默认 20，范围 1..100 |
| `cursor` | 服务端签发的 opaque cursor，不得解析或修改 |

cursor 使用 HMAC 绑定版本、当前 tenant、规范筛选、首次读取快照水位和 keyset 位置。更换租户、
筛选或篡改 cursor 都返回同一查询错误。cursor 只应驻留页面内存，不进入 URL hash、Storage、
日志或 DOM 属性。

### 2.1 成功响应

```json
{
  "ledger_entries": [
    {
      "finops_ledger_entry_id": "fle_0123456789abcdef0123456789abcdef",
      "tenant_id": "tenant_default",
      "entry_type": "usage_charge",
      "direction": "debit",
      "amount": "0.123456",
      "currency": "CNY",
      "normalized_cny": "0.123456",
      "cny_normalization_status": "normalized",
      "price_evidence_status": "priced",
      "price_snapshot_source": "model_price",
      "posting_source": "direct",
      "quota_type": "paid",
      "usage_ledger_id": "usg_...",
      "finops_adjustment_id": null,
      "source_request_id": "req_...",
      "source_trace_id": "trc_...",
      "operation_request_id": "req_...",
      "operation_trace_id": "trc_...",
      "price_snapshot_id": "prs_...",
      "price_schema_version": "price-v1",
      "reversal_of_entry_id": null,
      "actor": null,
      "reason": null,
      "input_tokens": 120,
      "output_tokens": 30,
      "total_tokens": 150,
      "cache_tokens": 0,
      "model_id": "model_default_chat",
      "provider_id": "provider_deepseek",
      "virtual_key_id": "vk_...",
      "environment_id": "env_...",
      "project_id": "project_...",
      "application_id": "app_...",
      "estimated": false,
      "status": "posted",
      "created_at": "2026-07-27T00:00:00Z"
    }
  ],
  "totals": {
    "by_currency": {
      "CNY": {
        "debit": "0.123456",
        "credit": "0.000000",
        "net": "0.123456"
      }
    },
    "net_cost_cny": "0.123456",
    "unnormalized_entry_count": 0,
    "entry_count": 1
  },
  "total": 1,
  "limit": 20,
  "next_cursor": null,
  "has_more": false,
  "request_id": "console_finops_ledger_...",
  "trace_id": "trc_console_finops_..."
}
```

金额始终是六位小数文本。列表和 totals 在同一只读快照内完成；服务端以整数微元和 Python
任意精度整数累计，不使用 JSON number、SQLite `TOTAL` 或二进制浮点。没有受信 FX 的 USD
分录返回 `normalized_cny=null`、`cny_normalization_status=unavailable`，不能显示成零 CNY。

`price_evidence_status` 的稳定语义：

- `priced`：入账时具有可信不可变价格快照。
- `unpriced`：入账时没有可用价格，使用明确的零价证据快照。
- `legacy_amount_only`：历史金额可信，但不能按快照单价重算。
- `not_applicable`：人工调整不适用价格证据；冲正继承目标分录状态。

响应不包含 Prompt、Response、raw/normalized Usage JSON、Provider Key、Virtual Key 明文、
Token、Endpoint、Credential/Binding/Scope ID、幂等摘要或原因摘要。

## 3. 创建调整或冲正

```http
POST /finops/adjustments
Authorization: Bearer <session-token>
Content-Type: application/json
Idempotency-Key: finops_<visible-ascii-random-value>
X-Request-ID: console_finops_adjustment_...
X-Trace-ID: trc_console_finops_...

{
  "adjustment_type": "manual_credit",
  "amount": "12.345678",
  "related_request_id": "req_source_call",
  "reason": "合同约定补偿"
}
```

请求正文必须是 1..8192 bytes 的严格 UTF-8 JSON object，拒绝重复 key、未知字段、过深/过多
节点和控制字符。`Idempotency-Key` 必须恰好出现一次，为 8..128 个可见 ASCII 字符；服务端
只保存 SHA-256 摘要，不保存原文。

| 字段 | 约束 |
| --- | --- |
| `adjustment_type` | `manual_debit/manual_credit/reversal/gift_credit/trial_credit` |
| `amount` | 必须是 JSON string；正数普通十进制，最多六位小数，最大 `1000000000.000000` |
| `reversal_of_entry_id` | 仅 `reversal` 必填；必须是当前租户内非冲正、非零分录 |
| `related_request_id` | 非冲正可选；必须对应当前租户唯一 Usage；冲正禁止自报 |
| `reason` | 去首尾空白后 1..500 UTF-8 bytes，不含 C0/C1 控制字符 |

非冲正调整固定 CNY。冲正从目标分录继承原币、额度类型、来源链和价格证据，并使用相反方向；
同一目标累计冲正不得超过目标金额，不能冲正另一条 reversal。客户端不能自报这些派生字段。

### 3.1 成功与幂等

首次提交返回 201；同 tenant、同幂等键、同规范化意图返回首次对象和 200，且
`idempotent_replay=true`。同键不同意图返回 `409 finops.idempotency_conflict`。重放不会重复
写 Adjustment、Ledger Entry、Audit 或 Webhook。

```json
{
  "adjustment": {
    "finops_adjustment_id": "fadj_0123456789abcdef0123456789abcdef",
    "tenant_id": "tenant_default",
    "adjustment_type": "manual_credit",
    "amount": "12.345678",
    "currency": "CNY",
    "reversal_of_entry_id": null,
    "related_request_id": "req_source_call",
    "reason": "合同约定补偿",
    "actor_user_id": "user_admin",
    "operation_request_id": "console_finops_adjustment_...",
    "operation_trace_id": "trc_console_finops_...",
    "status": "posted",
    "created_at": "2026-07-27T00:00:00Z"
  },
  "ledger_entry": {
    "finops_ledger_entry_id": "fle_...",
    "entry_type": "manual_credit",
    "direction": "credit",
    "amount": "12.345678",
    "currency": "CNY",
    "actor": {
      "user_id": "user_admin",
      "display_name": "管理员",
      "email": "admin@example.com"
    },
    "status": "posted"
  },
  "idempotent_replay": false,
  "request_id": "console_finops_adjustment_...",
  "trace_id": "trc_console_finops_..."
}
```

示例中的 `ledger_entry` 省略了与 GET 相同的其余字段，真实响应返回完整 DTO。

Adjustment、Ledger Entry 和 `finops.adjustment_created` Audit 在同一 SQLite 写事务提交。
接口不提供 PATCH 或 DELETE；更正只能追加新分录。

## 4. `ledger.entry_created` 事件

canonical 事件名固定为 `ledger.entry_created`。架构旧名 `billing.ledger_created` 只在 Webhook
订阅输入时接受并归一为 canonical 名称；存储、列表、签名正文和投递只输出 canonical 名称，
同一分录不得双发。

```json
{
  "event_id": "flevt_0123456789abcdef0123456789abcdef",
  "event_type": "ledger.entry_created",
  "timestamp": "2026-07-27T00:00:00Z",
  "tenant_id": "tenant_default",
  "actor_id": null,
  "object_id": "fle_0123456789abcdef0123456789abcdef",
  "trace_id": "trc_source_call",
  "payload": {
    "ledger_entry_id": "fle_0123456789abcdef0123456789abcdef",
    "entry_type": "usage_charge",
    "direction": "debit",
    "amount": "0.123456",
    "currency": "CNY",
    "normalized_cny": "0.123456",
    "cny_normalization_status": "normalized",
    "quota_type": "paid",
    "source_request_id": "req_source_call",
    "price_snapshot_id": "prs_...",
    "price_schema_version": "price-v1",
    "price_snapshot_source": "model_price",
    "price_evidence_status": "priced",
    "reversal_of_entry_id": null
  }
}
```

`event_id` 与分录同事务冻结；`object_id` 必须等于 payload 的 ledger ID。人工调整的
`actor_id` 来自 Session，Usage 为 null。payload 为精确白名单，禁止 reason、Prompt、Response、
raw Usage、Key、Token、Endpoint 或内部摘要。提交后外部 Webhook 属于尽力投递；接收端故障不
回滚已确认财务事实。

## 5. Usage 持久恢复

每次 Provider Usage 必须进入主库或本地持久 spool。非流式响应只有在主库提交或 spool 原子
写入并 fsync 成功后才能确认；两者同时失败返回
`503 finops.usage_record_unavailable`。流式正文已经发送后不能改写 SSE，只记录固定 critical
错误和链路标识。

spool 使用独立域 HMAC、tenant + Usage ID 域隔离文件名、严格 schema、16 KiB 单文件上限、
exclusive-create、原子 rename、owner/权限/reparse 检查、processing claim 和 quarantine。
启动及有界周期 worker 使用原入账时刻和原价格证据重放；数据库以 tenant + Usage ID 幂等，
只有首次新建正式分录才入队一次事件。spool 文件不包含 Prompt、Response、Endpoint、Key、
Token 或人工调整原因。

运行配置必须把 spool root 放在 Gateway 服务账号独占的本地 data root。当前实现复用
`auth_token_secret` 作为根密钥，但内容签名和文件名分别使用 FinOps 专属业务域隔离；不得把
根密钥直接用于无域前缀的正文签名。生产环境不得使用样例值或不足 32 字符的根密钥。

## 6. 错误码

| HTTP | code | 说明 |
| --- | --- | --- |
| 401 | `auth.*` | Session 缺失、无效或过期 |
| 403 | `auth.permission_denied` | 当前后端角色不是 admin |
| 404 | `finops.entry_not_found` | 当前租户看不到冲正目标；跨租户同形 |
| 404 | `finops.request_not_found` | 当前租户看不到关联 Usage；跨租户同形 |
| 409 | `finops.reversal_not_allowed` | 目标为 reversal、零金额或不可冲正 |
| 409 | `finops.reversal_exceeds_remaining` | 累计冲正超过目标剩余金额 |
| 409 | `finops.idempotency_conflict` | 同幂等键绑定了另一规范意图 |
| 422 | `finops.query_invalid` | 查询白名单、日期、枚举、limit 或 cursor 非法 |
| 422 | `finops.adjustment_invalid` | 媒体类型、正文、字段组合、reason 或幂等 Header 非法 |
| 422 | `finops.amount_invalid` | 金额格式、精度、符号或范围非法 |
| 503 | `finops.ledger_unavailable` | 账本数据库暂不可用，可按原意图重试 |
| 503 | `finops.usage_record_unavailable` | 主库和持久 spool 都未确认非流式 Usage |
| 500 | `finops.adjustment_failed` | 调整、分录或审计一致事务未完整形成 |

错误响应继续使用统一 `code/message/request_id/trace_id/suggestion/diagnosis` 结构。不得回显 SQL、
数据库或 spool 路径、幂等键、reason、请求正文、Provider 响应、Key、Token、Prompt 或 Response。

## 7. Console 页面

- PAGE-074：正式账本列表、筛选、精确汇总、opaque cursor 分页和新增调整。
- PAGE-075：分录详情、价格证据、来源 Request Logs 跳转和从目标行发起冲正。
- 两页只为 admin 挂载；登出、换租户、降权或离页必须销毁 cursor、原因草稿和幂等键。
- 金额按服务端字符串原样展示，浏览器不得用 `Number` 重算财务结果。
- reason、ID 和操作者字段必须完整 HTML 转义；敏感扩展字段不得进入 DOM、URL 或 Storage。

## 8. PRD-071 输出安全争议投影

`GET /finops/ledger` 的每条分录新增可空 `output_safety_dispute`：

```json
{
  "output_safety_scan_id": "oss_0123456789abcdef0123456789abcdef",
  "review_status": "dispute_pending",
  "reversal_eligible": true
}
```

该对象只在同 tenant 的 `usage_charge` 已关联 append-only 输出安全 dispute 时出现。Control 必须逐项复核
dispute、scan、Usage、charge 的 tenant/request/trace/ID owner、`completed + blocked` 状态和
`dispute_pending` disposition；任一不一致返回 `503 output_safety.integrity_failed`，不能静默隐藏污染事实。

FinanceAdmin 只获得这三个财务字段，不会因此获得 `request_log.read`、DLP 规则、扫描模式、暴露计数或输出
正文。`reversal_eligible=true` 仅表示正金额 charge 可进入现有人工冲正流程，不代表已经冲正；零金额 dispute
保留待复核，但资格为 false。自动输出阻断不会直接调用 `POST /finops/adjustments`。

Gateway 对存在 OutputSafetyScan 的 Chat Usage 使用 spool v5，额外只保存 scan ID、scan status、decision 和
billing disposition；无 scan 的请求继续写 v4，reader 精确接受 v1-v5。主库与 spool 双失败时返回
`finops.usage_record_unavailable`，不派发 `security.output_blocked` Webhook。完整合同见
[输出侧安全 API](output-safety.md)。

