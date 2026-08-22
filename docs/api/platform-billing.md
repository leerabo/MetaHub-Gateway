# Platform Metering and Billing API

PRD-099 从 canonical Usage 事实异步生成 MetaHub SaaS 平台计量、周期配额与不可变账单摘要。本领域与
PRD-051/079 的 Workspace BYOK 成本和配额账本完全分离，不共享 ledger、调整表或权限。

## 凭据域

- `/billing/*` 只解释 active WorkspaceSession，成员职责必须为 `owner` 或 `finance_admin`。请求不得携带
  Authorization、PlatformSession 或 SupportSession。
- `/platform/billing/*` 只解释 PlatformSession。读取要求 `platform.billing.read`，调整要求
  `platform.billing.adjust`；WorkspaceSession、SupportSession 与 Bearer 均不能替代。
- Platform 写请求必须来自允许的唯一 Origin，并携带 Platform CSRF Cookie 对应的 `X-CSRF-Token`。
- 所有认证响应均为 `Cache-Control: no-store, private`。计量 DTO 不包含 tenant、账号、邮箱、Prompt、
  Response、Cookie、Token 或 Provider Secret。

## 接口

| 方法 | 路径 | 请求边界 | 结果 |
| --- | --- | --- | --- |
| GET | `/billing/summary` | Workspace finance；可选唯一 `cycle_id` | 当前或指定周期与四个 meter 聚合 |
| GET | `/billing/usage` | Workspace finance；可选唯一 `cycle_id` | 与 summary 相同的安全 meter 投影 |
| GET | `/billing/reconciliation-export` | Workspace finance；必需唯一 `cycle_id` | 固定列 UTF-8 BOM CSV |
| GET | `/platform/billing/summary` | Platform billing read；`status/limit/cursor` | 跨 Workspace 周期列表与主体绑定游标 |
| POST | `/platform/billing/adjustments` | Platform billing adjust、CSRF、幂等键、严格 JSON | 追加贷记或借记 |

`cycle_id` 必须是 `pbc_` 加 32 位小写十六进制字符。Platform `status` 只允许 `open`、`closed`、
`reconciliation_required`；`limit` 为 1..100。游标绑定 operator ID、operator revision、status 筛选和最后
排序位置，不能跨主体、撤权或筛选条件复用。

## Meter 与周期

每个周期固定返回四个 meter：

| Meter | Unit | Billing mode | 配额和金额语义 |
| --- | --- | --- | --- |
| `api_requests` | `request` | `subscription_base` | 分配当期套餐月请求权益，显示超额与订阅基础金额 |
| `input_tokens` | `token` | `informational` | 只显示结构化用量，当前无价格不表示免费 |
| `output_tokens` | `token` | `informational` | 只显示结构化用量，当前无价格不表示免费 |
| `total_tokens` | `token` | `informational` | 必须等于输入与输出 Token 之和 |

金额为两位小数的十进制字符串，存储使用币种小单位整数。`open` 周期从 live reading 聚合；`closed` 摘要
不可更新或删除。已关账周期收到迟到 Usage 时追加 `late_usage` adjustment，并把周期推进为
`reconciliation_required`，不改写原摘要。

## Platform 调整

请求正文固定为：

```json
{
  "workspace_id": "wsp_00000000000000000000000000000000",
  "billing_cycle_id": "pbc_00000000000000000000000000000000",
  "meter_code": "api_requests",
  "reason_code": "manual_credit",
  "quantity_delta": -100,
  "amount_delta": "-1.00",
  "expected_summary_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
}
```

`manual_credit` 的数量和金额必须小于等于零，`manual_debit` 必须大于等于零，且两者不能同时为零。
`expected_summary_sha256` 是冻结摘要 CAS；摘要不存在或 hash 漂移时失败。`Idempotency-Key` 为 16..128
字符，首字符为字母或数字，其余只允许字母、数字、`.`、`_`、`:`、`-`。同 key 同规范请求返回原调整，
同 key 不同请求返回冲突。成功只追加 adjustment 与不可变平台审计，不能修改 Usage、Reading 或 summary。

## CSV 导出

导出固定列为 `cycle_id/period_start/period_end/meter/unit/usage/allocation/overage/`
`adjustment_quantity/currency/effective_amount/status`。响应满足：

- `Content-Type: text/csv; charset=utf-8`，正文包含 UTF-8 BOM；
- 固定 ASCII 文件名 `metahub-billing-<cycle-fragment>.csv`；
- `Cache-Control: no-store, private` 与 `X-Content-Type-Options: nosniff`；
- 以 `= + - @` 开头的单元格增加单引号，控制字符替换为空格；
- 只导出当前 Workspace 的已知周期，不生成长期下载 URL。

## 错误

- `platform_metering.source_invalid/owner_invalid/meter_unknown/checkpoint_conflict/`
  `reconciliation_required/unavailable`：投影源、owner、meter、水位或运行依赖异常。
- `platform_billing.request_invalid/not_found/revision_conflict/idempotency_conflict/`
  `reconciliation_required/export_too_large/unavailable`：查询、CAS、幂等、对账或导出异常。
- `workspace_session.*`、`platform.*`：凭据域、Origin、CSRF、Session 或权限失败。

错误只返回固定安全文案和关联 ID，不暴露 SQL、表名、owner hash、Cookie、TOTP 或调用正文。
