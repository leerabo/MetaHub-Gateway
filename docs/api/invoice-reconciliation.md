# Invoices, Tax Artifacts, and Reconciliation API

PRD-104 从已关账周期生成不可变结算单，锁定已验证的 BillingProfile revision，并通过合规 provider adapter
异步开票。系统不实现税控号码或签章；结算、发票产物和对账始终保留原币最小单位整数，不做跨币种覆盖。

## 凭据域

- `/billing/statements`、`/invoices*` 和 `/invoice-downloads` 只解释 active WorkspaceSession，当前职责必须为
  `owner` 或 `finance_admin`。
- `/platform/reconciliations*` 和 `/platform/invoice*reversals*` 只解释 PlatformSession。actor 必须显式具有
  `platform_billing` 或 `platform_compliance` 角色，并具有对应读权限；普通 platform owner 不可替代这两个域。
- Workspace、Platform、Support、Bearer 和其他 Cookie 不能混用。写命令要求唯一允许 Origin、对应 CSRF
  Cookie/Header、严格 JSON，并在适用接口提供 `Idempotency-Key`。
- Platform 对账只返回安全差异摘要，不能下载客户发票原件。

## 接口

| 方法 | 路径 | 请求边界 | 成功响应 |
| --- | --- | --- | --- |
| GET | `/billing/statements` | 无 query/body | 当前 Workspace 最多 100 个已发布不可变结算单 |
| POST | `/invoices` | `billing_statement_id`、固定 invoice type、`Idempotency-Key` | 锁定结算单与 BillingProfile revision 的发票申请 |
| GET | `/invoices/{invoice_id}` | 无 query/body | 发票状态、原币金额、掩码号码和安全产物摘要 |
| POST | `/invoices/{invoice_id}/artifacts/{artifact_id}/download-token` | 空 JSON | 绑定当前 WorkspaceSession 的短期单次 token |
| GET | `/invoice-downloads` | token 仅位于 `X-MetaHub-Invoice-Download-Token` Header | PDF 或 CSV 下载响应 |
| POST | `/platform/reconciliations` | `scope=all`、显式确认 | 冻结五类水位与 source hash 的异步对账 run |
| GET | `/platform/reconciliations/{run_id}/differences` | `limit=1..100`、可选 opaque cursor | run 状态与有界差异摘要 |
| POST | `/platform/invoices/{invoice_id}/reversals` | 固定 reason、显式确认、`Idempotency-Key` | 待双域审批的冲红申请 |
| POST | `/platform/invoice-reversals/{reversal_id}/approvals` | 固定 decision/reason、显式确认、`Idempotency-Key` | append-only 审批后的冲红状态 |

请求正文拒绝客户端提交 amount、tax、currency、workspace、provider invoice id 或状态。所有 JSON 响应均为
`Cache-Control: no-store, private`；下载响应额外要求 `X-Content-Type-Options: nosniff` 和受控 attachment 文件名。

## 不可变事实与异步边界

读取结算单时只为已关闭的 billing cycle 物化缺失 statement，并冻结 Meter summary set hash、source watermark、
税率和原币金额。已发布 statement 不可更新或删除；更正只能追加 credit/replacement 版本。

发票申请与 provider outbox 在一个本地事务提交，网络调用在事务外执行。provider receipt 先持久化，再追加 PDF/CSV
artifact 和状态；同 provider event 重放收敛到同一 receipt。artifact 内容 hash、大小、MIME 和 owner-bound 加密对象
定位共同校验，已发布产物不可原地替换。

## 单次下载

签发的 download token 绑定 WorkspaceSession、workspace、tenant、invoice artifact 和短 TTL，数据库只保存域分离
HMAC。消费接口拒绝 query token，并在读取对象前原子执行 `active -> used/expired`；重放、过期、跨 Session 和跨租户
请求同形失败。Console 立即使用专用 Header 消费 token，在 `finally` 中清空内存字段，不写 DOM、URL、Storage、日志
或控制器状态；下载后再次校验响应 MIME、大小和 SHA-256。

## 对账与红字冲销

ReconciliationRun 在同一事务冻结 order、payment、Meter、statement、invoice 五类水位及 source hash。worker 只在冻结
范围内比较事实；迟到事实进入后续 run。difference 为 append-only 摘要，不修改订单、支付、Meter、结算单或发票
原始事实，财务修复必须引用独立 adjustment。

冲红申请人不能审批自己的申请。账务域与合规域必须由不同 Platform actor 分别追加批准；只有两域均批准后，第二次
批准事务才创建唯一 provider reversal outbox。任一域拒绝会终止申请并恢复发票为已开具状态。

## 固定错误

业务错误使用 `invoice.request_invalid/not_found/conflict/idempotency_conflict/download_invalid/unavailable` 和
`reconciliation.request_invalid/not_found/unavailable`。凭据、Origin、CSRF、Session 与权限错误沿用
`workspace_session.*` 或 `platform.*`。错误只返回固定安全文案与关联 ID，不透传请求正文、税务资料、token、Cookie、
Session、TOTP、对象定位、路径、SQL、provider receipt 正文或私有基础设施信息。
