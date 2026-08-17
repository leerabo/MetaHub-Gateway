# 伙伴门户 API

本文档对应 `PRD-087 渠道伙伴与白标门户`。一个 Partner 精确归属一个现有 tenant；客户是当前
tenant 内 Project 的伙伴映射。后端始终从 Session 恢复 tenant 和 actor，调用方不能提交或覆盖
`tenant_id`。总体状态保持 `all_108_prds_complete=false`。

## 1. 通用合同

- 认证：`Authorization: Bearer <token>`；权限按下表逐请求从数据库矩阵复核。
- JSON 请求和 query 都拒绝未知字段；日期使用 `YYYY-MM-DD`，窗口最多 366 个 UTC 日。
- JSON 成功响应固定携带 `request_id`、`trace_id`；错误沿用统一 ErrorResponse 与同一 request ID。
- 所有成功和错误响应均为 `Cache-Control: no-store, private`。
- POST 创建接口要求 16..160 字节安全 `Idempotency-Key`；同键同正文返回原对象，同键异正文返回 409。
- PATCH 要求正整数 `If-Match` revision；过期 revision 返回 `partner.revision_conflict`。
- Provider Key、Token、Prompt、Response、Endpoint、数据库路径和底层异常正文不进入 API、日志或附件。

## 2. Partner 与白标

| 方法 | 路径 | 权限 | 结果 |
| --- | --- | --- | --- |
| `POST` | `/partners` | `partner_portal.configure` | 创建当前 tenant 唯一 Partner，201 |
| `GET` | `/partners/profile` | `partner_portal.read` | 返回当前 Partner 安全资料 |
| `PATCH` | `/partners/branding` | `partner_portal.read` + `partner_branding.manage` | 更新白标，200 |
| `GET` | `/partners/projects` | `partner_portal.read` + `partner_customer.manage` | 返回可映射 active Project |

POST body exact-set：

```json
{
  "partner_code": "channel-001",
  "display_name": "示例渠道伙伴",
  "portal_name": "伙伴工作台",
  "brand_primary_color": "#176B87",
  "support_text": "企业支持"
}
```

`partner_code` 为 1..80 字符规范小写业务码；颜色固定为六位大写十六进制。名称和支持信息是
有界纯文本，拒绝 NUL/CR/LF/控制字符，不支持远程 Logo、自定义域名或 HTML。

Partner JSON exact-set：`partner_id`、`partner_code`、`display_name`、`portal_name`、
`brand_primary_color`、`support_text`、`status=active`、`default_commission_rate_bps=1000`、
`revision`、`created_at`、`updated_at`。外层为 `partner`、`request_id`、`trace_id`。

PATCH body 只允许 `portal_name`、`brand_primary_color`、`support_text`；不允许修改 partner code、费率或状态。
Project item exact-set 为 `project_id`、`project_code`、`name`；列表外层为 `items`、`total`、
`request_id`、`trace_id`。

## 3. 伙伴客户

| 方法 | 路径 | 权限 | 结果 |
| --- | --- | --- | --- |
| `GET` | `/partners/customers` | `partner_portal.read` | 客户及历史用量安全聚合 |
| `POST` | `/partners/customers` | `partner_portal.read` + `partner_customer.manage` | 创建 Project 映射与 EVT-099，201 |
| `PATCH` | `/partners/customers/{partner_customer_id}` | 同上 | 改展示名或 active -> disabled |

GET query：`start_date`、`end_date`、`status=active|disabled`、`q`、`limit=1..100`、`offset>=0`。
默认最近 30 个完整 UTC 日。POST body exact-set 为 `project_id`、`customer_code`、`display_name`；
PATCH 至少包含 `display_name` 或 `status=disabled`，不接受其他状态跃迁。

客户 item exact-set：

- 身份：`partner_customer_id`、`project_id`、`customer_code`、`display_name`、`status`、`revision`。
- 分润边界：`commission_rate_bps=1000`、`linked_from_ledger_sequence`、
  `commission_cutoff_sequence`、`commission_cursor`。
- 生命周期：`created_at`、`updated_at`、`disabled_at`。
- 用量：`usage_scope=project_history`、`start_date/end_date`、成功/失败/请求计数、
  input/output/total/cache tokens、CNY/USD cost micros、estimated records。

`commission_cursor` 固定包含 `status=pending|leased|complete`、`last_scanned_ledger_sequence`、
`source_high_water_sequence`、`freshness_status=current|lagging`。客户列表外层固定为 `window`、`items`、
`total`、`limit`、`offset`、`request_id`、`trace_id`；单客户写响应外层为 `customer`、
`request_id`、`trace_id`。

用量读取被映射 Project 在日期窗口内的历史 RequestLog/Usage/FinOps 治理事实，可能包含映射前历史；
分润只从 `linked_from_ledger_sequence` 之后物化。终止映射会冻结 cutoff，但不删除历史用量或分润记录。

## 4. 分润报告

`GET /partners/commission` 需要 `partner_portal.read`。query 为 `start_date`、`end_date`、
`customer_id`、`limit=1..100`、`offset>=0`；JSON 顶层 exact-set 为：

`window`、`items`、`totals`、`cursor`、`total`、`limit`、`offset`、`request_id`、`trace_id`。

每条 item 固定包含 record/customer 安全 ID、客户 code/name 快照、currency、amount/commission micros、
1000 bps 费率、token 计数、estimated、`status=accrued`、`settlement_status=not_applicable`、
source request/trace 与发生时间。`totals` 按 CNY/USD 分别返回 request/token/amount/commission/estimated，
禁止跨币种相加。`cursor` 暴露 materialized/source high-water 与 current/lagging，不把滞后数据冒充结算结果。

`GET /partners/commission?format=csv` 额外要求 `partner_commission.export`，拒绝 `limit/offset`。CSV：

- UTF-8 BOM、`text/csv; charset=utf-8`、attachment、`nosniff`、request/trace headers。
- 固定列为 period day、客户 code/name、currency、request/token/amount/commission/estimated。
- 按 UTC 日 + Customer + currency 聚合；CNY/USD 分开。
- 文本拒绝 NUL/CR/LF，对 `= + - @` 前缀加单引号，防止表格公式注入。
- 最多 10,000 行、8 MiB；超限在发送任何附件字节和写成功操作前返回 413。

分润仅是 append-only ledger 的非结算物化，不提供支付、提现、税务、汇率换算或复杂结算。

## 5. 交付包

`GET /partners/delivery-package` 需要 `partner_portal.read` + `partner_delivery.download`，返回
`application/json; charset=utf-8` attachment。正文为：

```json
{
  "package": {
    "schema": "metahub.partner-delivery/v1",
    "partner_code": "channel-001",
    "branding": {},
    "customers": [],
    "api_paths": {
      "chat_completions": "/v1/chat/completions",
      "responses": "/v1/responses"
    },
    "documentation_path": "/docs/api/openai-compatible",
    "generated_at": "2026-08-17T12:34:56Z",
    "request_id": "req_...",
    "trace_id": "trc_..."
  },
  "manifest_sha256": "..."
}
```

manifest hash 只计算不含 hash 字段的 canonical `package` 对象。包内不含 Project ID、Provider/Virtual Key、
Endpoint、Token、Prompt、Response、用量或分润明细；写入 append-only 下载操作后才允许返回完整附件。

## 6. EVT-099 与 Scheduler

创建客户时，同一事务写入 `partner.customer_created` source outbox。payload 七字段 exact-set 为
`schema_version=1.0`、`partner_id`、`partner_customer_id`、`customer_code`、`project_id`、
`status=active`、`commission_rate_bps=1000`；不包含展示名、branding、金额或正文。

恢复 worker 按 tenant/customer/event 回读 PartnerCustomer 与原 actor/request/trace，重建 canonical event；
订阅快照首次冻结后即使为空也不再追溯后来订阅。投递采用短租约、generation/token CAS、续租、固定退避和
十次失败终态。

分润 Scheduler 在 Gateway 热路径之外按 tenant/customer 公平 claim。每次冻结 ledger high-water/cutoff，
最多处理 200 条匹配分录；CommissionRecord exactly-once，失败不能跳过坏源推进 cursor。用量、成本与失败请求
继续由 RequestLog、Usage 和 FinOps 权威链路追踪。

## 7. 错误码

| HTTP | code | 场景 |
| ---: | --- | --- |
| 422 | `partner.request_invalid` | 未知/非法 body、query、header、日期或文本 |
| 404 | `partner.not_configured` | 当前 tenant 没有 Partner |
| 409 | `partner.already_configured` | 已配置或 Partner 创建幂等冲突 |
| 409 | `partner.revision_conflict` | If-Match 过期 |
| 404 | `partner.project_not_found` | Project 不存在、跨租户、disabled 或已映射 |
| 404 | `partner.customer_not_found` | 客户不存在、跨 tenant/Partner 或非法 ID |
| 409 | `partner.customer_conflict` | customer code、Project 或重试标识冲突 |
| 503 | `partner.commission_unavailable` | 分润 source/cursor/owner 不一致 |
| 413 | `partner.export_too_large` | CSV 行数或字节超限 |
| 503 | `partner.export_unavailable` | CSV 或交付包无法安全生成 |
| 503 | `partner.event_unavailable` | EVT-099 无法恢复或投递 |
| 503 | `partner.unavailable` | 固定内部兜底 |

401/403 沿用 `auth.*` 统一错误。所有错误只返回固定公开文本和 request ID，不回显提交值或异常正文。
