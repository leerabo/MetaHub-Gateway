# PRD-144 平台商业账务与企业内部 FinOps 分域

状态：设计冻结，等待 G6 通过后编码。依赖 PRD-051/079/080/098/099/102/103/104/132/139，
批次边界见 `S7-platform-operations-console-domain-batch-design.md`。本条只交付 0136、平台商业账务工作台、
Workspace 订阅/发票/争议投影、Provider 成本率和企业内部 FinOps owner 迁移，不提前实现 PRD-145..148。

## 1. 产品闭环与非目标

`platform_billing` 管理 MetaHub 对客户出售的套餐目录、订阅安全投影、平台账单/发票、商业争议裁决和
双边对账；`platform_channel` 只读取 PRD-139 已关闭渠道结算的安全汇总。Workspace `owner` 和
`finance_admin` 查看本空间订阅、账单、发票和争议进度，并管理本空间 BYOK Provider 成本率、额度、预算和
内部 FinOps。没有财务授权的 `admin/developer/security_admin/legal_admin/auditor/viewer` 不读取金额，也不能
借助页面可见性、旧 Cookie 或旧 tenant API 获得财务能力。

以下三条账必须是独立的可写事实链：

1. 平台商业账：套餐版本、订阅订单、支付/退款、平台计量、Statement、Invoice 和商业调整，表示
   MetaHub 与客户之间的合同和应收事实。
2. Workspace 内部账：BYOK Provider 成本率、Usage/PriceSnapshot、FinOps Ledger、额度和预算，表示客户
   自己的成本管理，不形成 MetaHub 应收。
3. 渠道结算账：PRD-139 ChannelStatement/line 的已关闭快照，只能由渠道域产生；商业账只引用其版本和
   hash 做对账，不能改写佣金或归属。

PRD-079 的 `plans/quota_*` 不能改名为平台商品，PRD-051 的 `finops_ledger_entries` 不能复制为平台收费账，
PRD-080 的旧 tenant 内部成本争议不能伪装成客户对 MetaHub 的商业争议。平台官方 Provider 建议成本只是
版本化参考；Workspace 未显式采用时不能改变 Gateway 预算、路由或历史 PriceSnapshot。

本条不自建支付网络、税控引擎、汇率转换或渠道佣金引擎，不保存银行卡、支付密码、Provider Key/Token、
Prompt/Response/Header、客户业务正文、回调正文、发票原件、证照原件或银行资料。现有支付、退款、开票和
下载命令继续由 PRD-103/104 的隔离渠道、对象存储和短时授权执行，本条只重组 owner、投影、权限和对账。

## 2. 身份、owner 与固定权限

### 2.1 Platform、Workspace 与关联对象

`PlatformPlanCatalog`、`PlatformInvoice` 和 `BillingDomainReconciliation` 是 Platform owner，不使用
`tenant_id` 作为对象 owner，只接受有效 `PlatformSession`。Plan/Invoice 与客户的关系必须经受控关联表：
Plan 通过订阅投影关联；Invoice 通过不可变 `platform_invoice_workspace_bindings` 关联
`workspace_id + tenant_id`。PlatformInvoice 主表不能因为关联客户就降格为 tenant 可写对象。

`WorkspaceSubscriptionProjection`、`BillingDisputeCase`、`ProviderCostRate` 和
`InternalFinOpsLedger` 必须带不可变 `workspace_id + tenant_id` 复合 owner。Workspace API 从当前
Membership、WorkspaceTenantBinding、角色和权限恢复 owner，body/query/path 不接收 owner。Platform 只能
通过专用安全投影读取订阅、Invoice binding 和商业争议；不能读取 Workspace Provider 成本率、内部账分录、
预算、原始 Usage、Provider Secret 或请求正文。

PlatformInvoice 主表只保存平台 invoice ID、Statement/订单/支付安全引用、原币金额、税率版本、状态、
artifact hash/掩码、provider receipt token 和 UTC。Workspace binding 保存 invoice ID、复合 owner、
客户可见版本和 projection watermark。平台 API 无发票原件下载动作；现有 Workspace 发票接口必须先通过
binding 恢复 owner，再签发 tenant/session-bound 单次短 TTL grant。

### 2.2 0136 权限矩阵

0136 新增不可变 `platform_commercial_permission_extensions`、固定 Platform 角色映射，以及
`workspace_commercial_permission_extensions`、固定 Workspace 角色上限：

| Permission | platform_owner | platform_billing | platform_channel | Workspace owner | finance_admin |
| --- | --- | --- | --- | --- | --- |
| `platform.commercial.plan.read` | 允许 | 允许 | 否 | 否 | 否 |
| `platform.commercial.plan.manage` | 否 | 允许 | 否 | 否 | 否 |
| `platform.commercial.subscription.read` | 允许 | 允许 | 否 | 否 | 否 |
| `platform.commercial.invoice.read` | 允许 | 允许 | 否 | 否 | 否 |
| `platform.commercial.dispute.decide` | 否 | 允许 | 否 | 否 | 否 |
| `platform.commercial.reconciliation.manage` | 否 | 允许 | 否 | 否 | 否 |
| `platform.commercial.governance.approve` | 允许 | 否 | 否 | 否 | 否 |
| `platform.channel.statement.read` | 允许 | 允许 | 允许 | 否 | 否 |
| `workspace.commercial.subscription.read` | 否 | 否 | 否 | 允许 | 允许 |
| `workspace.commercial.invoice.read` | 否 | 否 | 否 | 允许 | 允许 |
| `workspace.commercial.dispute.submit` | 否 | 否 | 否 | 允许 | 允许 |
| `workspace.provider_cost.read` | 否 | 否 | 否 | 允许 | 允许 |
| `workspace.provider_cost.manage` | 否 | 否 | 否 | 允许 | 允许 |
| `workspace.internal_finops.read` | 否 | 否 | 否 | 允许 | 允许 |
| `workspace.internal_finops.manage` | 否 | 否 | 否 | 允许 | 允许 |

`platform_owner` 通过显式 `governance.approve` 执行最终政策/紧急恢复批准，但不能以该批准替代
`platform_billing` 创建套餐候选、裁决争议或调整客户账。`platform_ops` 只能在平台总览读运行状态，
`platform_verification` 只在 PRD-102 审核投影读合同状态；`platform_customer_success` 和 `platform_growth` 只消费
PRD-140/141 的续费/单位经济聚合，均不能调用本条订阅/Invoice 明细 API。`platform_support` 无商业金额权限，
SupportGrant 也不扩展为财务授权。

`admin` 不能继承 Workspace 财务权限；只有另行授予的安全状态摘要可不含金额返回。`legal_admin` 只按
PRD-102 读取合同/Invoice 状态和掩码，不读取金额；`auditor` 只读审计中的 amount hash/币种/状态，
`viewer` 仅在另有授权时读取无金额 Usage/状态摘要。它们都没有本条六组 API 的 finance 权限。

0090 的 immutable `platform.catalog.write`、0091 的 `platform.billing.adjust`，以及 0050/0072/0073 的
`billing.finops.*`、`billing.plan.*`、`billing.dispute.*` 只作为 legacy 迁移输入；0136 target 路由和内部命令
完全忽略这些旧 code。旧表仍给 `platform_owner/admin` 的 grant 不能授权新 Plan、Dispute、ProviderCostRate 或
InternalFinOps 写入；PRD-147 卸载旧路由后再由 tombstone 审计证明这些 code 无外部可执行面。

平台商业裁决 actor 必须与任何平台人工调整 actor 分开记录；最终事务重新核验当前角色、permission revision、
case revision、source Statement/Invoice revision 和政策要求的审批。Workspace submitter 与 Platform decider
天然跨主体，不能通过同一 GlobalAccount 绑定绕过职责分离。动态撤权后下一请求立即失败。

新套餐金额/权益或官方建议成本 release 必须引用一个当前、不同 `platform_owner` 的
`platform.commercial.governance.approve` 事实；approval 绑定 candidate digest、revision 和 expiry，不能跨候选复用。
Platform billing candidate actor、governance approver 和最终 release actor 至少两人且 candidate/release 不能用
同一 Session step-up。普通 dispute decision 不复用 catalog approval，也不能把 owner approval 当商业 adjustment。

## 3. 0136 目标数据模型与唯一事实

### 3.1 PlatformPlanCatalog 与官方建议成本

0090 `saas_plan_catalogs/saas_plan_versions` 已是平台商品目录的唯一 registry，0136 直接纳入
PlatformPlanCatalog owner，不复制第二套套餐和权益 JSON。新增 `platform_plan_catalog_releases/heads`：

- release 使用稳定 `pplr_*`，引用 exact `spc_/spv_`、canonical plan/entitlement hash、币种、billing period、
  原币最小单位价格、税务/兼容版本、operator、revision、generation 和 UTC；
- published `saas_plan_versions` 与 released wrapper 均不可 UPDATE/DELETE，同 plan code 只有一个 current head；
- 发布事务锁定 draft/version、重算 schema/hash、校验 current head `If-Match`，原子写 release/head、审计、
  `plan.released` 和 outbox；同版本不能以不同金额或权益重复发布；
- 现有 `POST /platform/plans` 保留为受控 candidate/create 命令，发布/retire 只接受固定 action、version ID 和
  revision；不能提交任意 entitlement schema、脚本、价格表达式或客户端计算金额。

`platform_provider_reference_rate_releases/heads` 保存官方建议 Provider/model、币种、每千 Token 微币单位整数、
有效区间、来源版本/hash 和 release generation。它是 Platform 只读参考，不是 Workspace ProviderCostRate，
不进入 Gateway 热路径。Workspace 只能通过显式采用命令生成自己的新 rate version，并保留 source release ID；
平台后续改价不改写已采用版本或历史 PriceSnapshot。

Excel 没有给官方建议价提供写 API，而现有 `/model-prices` 是 tenant 内部成本入口，不能复用。0136 因此补充窄
作用域 `POST /platform/provider-reference-rates/{provider_id}/commands`，只接受
`create_candidate|release|retire`、已知 model/currency、微币单位整数、effective interval、candidate revision 和
governance approval ID。它不接受 Workspace、Provider Key/Endpoint、任意公式、浮点数、汇率或 Gateway 参数。

### 3.2 WorkspaceSubscriptionProjection

`workspace_subscription_projections` 每个复合 owner 最多一个 current row，消费 0090..0096 的
`workspace_subscriptions`、EntitlementSnapshot、Order、Payment、Refund、BillingCycle、Statement、
Invoice binding 和 dispute head，只保存：

- subscription/plan catalog/release/version、状态、周期、试用和 entitlement hash；
- 原币当前应付/已付/退款/开票整数摘要及 currency，不跨币种求和；
- order/payment/invoice/dispute 安全状态、固定 reason codes 和 actions；
- 每类 source watermark/hash、projection generation/revision、stale/partial 标志和 projected UTC。

该表是可重建只读投影，不能成为订阅、支付、权益、Invoice 或退款的第二个 writer。每次推进要求所有 owner、
source revision 和 watermark 单调一致；缺失支付、未知价格、对账差异或 source hash 漂移返回
`partial|reconciliation_required|unknown`，不能用 0、paid 或 active 代替未知。

### 3.3 PlatformInvoice 与 Workspace 可见绑定

`platform_invoices` 使用稳定 `pinv_*`，按 0096 Statement/InvoiceRequest/Artifact 链形成全局不可变商业发票：
source IDs、原币 subtotal/tax/total、税率/价格/合同版本、status、artifact manifest hash、号码掩码、
provider receipt token、predecessor/reversal/replacement、generation 和 UTC。原始对象引用 ciphertext 仍只由
PRD-104 invoice artifact owner 持有，不复制到平台投影。

`platform_invoice_workspace_bindings` 使用复合 Workspace owner，引用一个 platform invoice、客户可见状态、
projection revision 和 source hash。一个 platform invoice 必须恰好绑定一个 Workspace；同 source invoice
不能映射两次。平台按 invoice 主表读取，Workspace 先按 binding 过滤再读取裁剪 DTO。关闭/红字/替换只追加
新事实和链，不改原发票。

### 3.4 BillingDisputeCase 与旧内部成本争议

新 `billing_dispute_cases/items/transitions/decisions` 只表达客户对 MetaHub Statement/Invoice 的商业争议，
使用稳定 `bdc_*`、完整 Workspace owner、source Statement/Invoice/line ID、原币金额、固定 reason code、
状态 `submitted|under_review|approved|rejected|withdrawn`、revision 和 UTC。金额和 owner 均由 source 反查，
客户不能提交金额、币种、tenant、payment/provider reference 或状态；首版不接收自由文本。

item 冻结提交时的 source revision/hash 和可争议余额。Platform decision 只接受固定
`start_review|approve_credit|reject` 与固定 decision code；approve 原子追加平台商业 adjustment 和 decision，
不能修改 source Invoice/Statement/Payment。累计 credit 在锁内按同币种整数校验，不能超过可争议余额。
`billing_dispute.submitted` 只在首次提交成功时发出，`billing_dispute.decided` 只在首次批准/拒绝终态发出。

0073 `billing_disputes/items/transitions/adjustment_links` 的 source 是 Workspace 内部 FinOps，不是平台商业账。
它们迁到独立 `workspace_internal_finops_disputes` 兼容 owner，保留 `bdsp_*`、item/transition/adjustment link、
状态、reason/decision digest 和历史 reviewer；绝不进入 Platform decision queue。未终态 legacy case 仍由当前
Workspace finance 权限在内部 FinOps 工作流完成，不能由平台代审。legacy 自由文本由受控 backfill 写入
Workspace 字段级 KMS ciphertext 后清除目标明文，mapping 保存原 hash；事件、日志和证据不含正文。

### 3.5 ProviderCostRate

`workspace_provider_cost_rates/versions/heads` 绑定
`workspace_id + tenant_id + provider_id + model_id + currency`，使用稳定 `pcr_*`，保存 input/output
微币单位/千 Token 整数、effective interval、source `manual|platform_reference|legacy_model_price|legacy_model`、
source ID/hash、status `active|superseded|disabled`、revision、content hash 和 UTC。版本只追加，head 用 CAS 推进。

0022 `model_price_plans` 逐行经 tenant -> active WorkspaceTenantBinding、model -> provider owner 迁移，保留
原 `price_plan_id` mapping、版本、有效时间、币种和精确十进制定点值。没有 price plan 但 `models` 有合法价格的
current model 形成 `legacy_model` baseline；缺失/多 Workspace owner、model/provider owner 漂移、无法无损转换、
重叠 effective range 或同版本异 hash 均阻断。Gateway 切换前逐 model 对比旧/新 resolver 的 exact rate ID、
currency、amount 和 PriceSnapshot hash；缺价必须保持 unavailable，不能默认为官方建议价或 0。

### 3.6 InternalFinOpsLedger、额度与历史 PriceSnapshot

0041 `finops_ledger_entries/adjustments`、`usage_ledger/price_snapshots` 是企业内部账唯一不可变 registry，0136 不
复制第二套 entry。`internal_finops_ledger_owner_bindings` 为每个 `fle_*`、adjustment、Usage/PriceSnapshot 增加
不可变 `workspace_id + tenant_id` owner、source hash 和 generation；新写事务必须同时解析唯一 active
WorkspaceTenantBinding 并写 binding，任一步失败整体回滚。

0072 `plans/quota_buckets/reservations/settlements/allocations/events/outbox` 继续是 Workspace 内部额度账，增加
逐对象 Workspace owner mapping 和写 guard，不映射到 PlatformPlanCatalog。历史 `pln_/qbk_/qres_/qset_`、
allocation、event/outbox、lease 和状态原值保留。企业页面后续把它归入“额度与预算”，而不是“平台套餐”。

InternalFinOpsLedger DTO 只在 WorkspaceSession 财务权限下读取绑定后的 entry、ProviderCostRate/PriceSnapshot、
额度分配、预算和 adjustment 安全摘要。平台商业 API 只能在 BillingDomainReconciliation 中读取按 Workspace、
币种、周期聚合的 hash/count/amount，不能读取 request/model/provider/Project/VirtualKey 明细。

### 3.7 BillingDomainReconciliation

0096 `reconciliation_runs/differences` 继续保存支付、订单、Meter、Statement 和 Invoice 冻结水位。0136 新增
`billing_domain_reconciliations/source_snapshots/differences` wrapper，使用稳定 `bdr_*`，同时冻结：

- Plan/Subscription/Order/Payment/Refund、Meter/Statement/Invoice/Dispute/Adjustment；
- ProviderCostRate/InternalFinOpsLedger/Quota 的聚合水位；
- PRD-139 ChannelStatement generation；
- 每个 source 的 row count、最大业务序列、canonical hash、币种分组和 owner coverage。

双边对账只比较明确可比的合同金额、平台收款/退款、Statement/Invoice 和渠道应结算额；内部 Provider 成本只
作为独立成本侧汇总，不与平台应收相减生成“利润”写回。Difference 保存安全 source refs、原币整数、类型、
状态和 adjustment reference；自动修复只重放可证明丢失的投影，不能改 source ledger、Payment、Invoice 或
ChannelStatement。相同 source snapshot 重放必须得到相同 result hash。

### 3.8 Command、事件、outbox 与 Scheduler

`commercial_accounting_commands` 保存 domain/action/object、actor、owner digest、Idempotency-Key HMAC、规范 body
hash、quoted revision、step-up/permission revision、result ID/hash 和 UTC；不保存明文键、理由正文、支付/税务
材料。`commercial_accounting_events/outbox` 只承载 Excel 五个 exact event：
`plan.released`、`subscription.changed`、`billing_dispute.submitted`、`billing_dispute.decided`、
`provider_cost_rate.changed`。payload 只含稳定 ID、owner-qualified safe ref、revision/generation、原币摘要 hash、
固定 reason/status 和 UTC。

一个 `install_commercial_accounting_scheduler` 替换以下六个 Control installer：
`install_plan_billing_scheduler`、`install_saas_billing_scheduler`、`install_platform_billing_scheduler`、
`install_payment_commerce_scheduler`、`install_invoice_reconciliation_scheduler` 和
`install_billing_dispute_scheduler`。supervisor 保留 `quota_trial/outbox`、`subscription_trial`、`meter`、
`payment/dunning/refund`、`invoice/reconciliation`、`legacy_dispute`、`projection`、`commercial_outbox` 和
`cleanup` 独立 queue code、lease、fence、watermark 和失败恢复，不把六个队列合成一个无界循环。

PRD-147 再把该 supervisor 注册到全局 SchedulerRegistry。0136 先保证每轮提交校验 lease generation/token
digest；失租旧 worker 的迟到 Payment/Invoice/effect/event/watermark 写入全部失败关闭。

## 4. 0072/0073/0090..0096/现有账本迁移

0136 使用 expand/inventory/backfill/dual-read/quiesce/cutover/contract，要求 current head 0135。迁移清单至少
覆盖 0022、0041、0072、0073、0090、0091、0094、0095、0096 和 0131 的相关表；每张表记录 row count、
canonical hash、owner coverage、最大序列、不可变引用、pending/processing outbox、lease 和 audit watermark。

### 4.1 Catalog、Subscription 与 Invoice

1. 对每个 0090 catalog/version 重算 plan/entitlement/price hash；published version 生成一个
   `legacy_reconciled` release/head wrapper，保留 `spc_/spv_`，不伪造新 `plan.released` event。
2. 每个 WorkspaceSubscription 必须唯一映射 active WorkspaceTenantBinding；逐个冻结 subscription、trial、
   entitlement、order、payment/refund、cycle/summary/statement/invoice/dispute source 水位并生成 projection。
3. 0096 每个 InvoiceRequest/Artifact 链生成一个 PlatformInvoice 和一个 Workspace binding；source amount、税率、
   profile revision、artifact hash、reversal/replacement 和 provider receipt 全部相等。缺 binding、多 owner、
   artifact 跨 owner 或链断裂阻断 cutover。
4. pending/processing Payment、Refund、Invoice 和 reconciliation queue 原 ID、effect key、attempt、lease、
   next-attempt 和 payload hash 保留；terminal item 不重发，processing 等原 lease 完成/过期后接管。

### 4.2 旧 Dispute、ProviderCostRate 与内部账

1. 0073 case 逐 tenant 映射 Workspace owner，完整迁入内部 FinOps dispute 兼容表；`bdsp_/bdsi_`、transition、
   hold、adjustment link、event/outbox 和 scheduler watermark 保真。它们不生成新商业 dispute 或新 Excel event。
2. 旧 reason/decision 明文在受控维护进程中读取后立即封装为 Workspace 字段级 KMS ciphertext；目标、mapping、
   审计只保存 hash/长度桶。源表在最终 hash/解密抽样核验后由一次性 scrub guard 替换为固定 tombstone 和原 hash，
   不可通过 down/rollback 恢复明文。
3. 0022 model price 与 model fallback price 无损转换为 ProviderCostRate baseline；逐条验证 effective time、currency、
   micro-unit、provider/model owner 和 price snapshot reference。历史 PriceSnapshot 继续引用原 source ID，并增加
   target mapping，不按 current rate 重算。
4. 0041/0072 的每个 ledger/quota/usage/snapshot/event/outbox 逐行建立 Workspace owner binding；tenant 缺/多 active
   binding、entry/source owner 不同、金额/币种/hash 漂移、序列缺口或 current head 回退都阻断。

### 4.3 Dual-read、切换与回滚

dual-read 逐 Workspace 比较旧/新 Plan、Subscription、Invoice、Dispute、rate resolver、FinOps/Quota 和事件输出；
Gateway 仍由旧 resolver 写 PriceSnapshot，target 只影子计算。所有 seen Workspace 必须 exact match；冷 Workspace
显式记录 `not_observed`，不能伪造通过。

quiesce 时 fence 六个 legacy installer，等待 active lease 完成/过期，短时冻结套餐发布、商业 adjustment、
dispute decision、ProviderCostRate 和内部 FinOps adjustment 写；Payment/Invoice webhook 继续 durable receipt，
但 effect worker 在 target fence 下接管。最终 reconciliation 通过后 CAS 切换 writer、projection、Gateway rate
resolver 和事件 owner，并给旧 0072/0073/model-price/FinOps owner 安装 target-aware write guard。

同一应用发布安装 Excel 新 API，并把旧 tenant `/plans`、`/model-prices`、`/billing-disputes` 及旧
`/subscriptions/current` 外部读写标记为待卸载；PRD-147 最终从 router/import/prefetch/DOM/Navigation 一并删除并
返回普通统一 404。PRD-103/104 的 `/payments/*`、`/refunds`、`/invoices*` 继续作为受控 Workspace/Provider
命令，不因页面重组被误删。

0136 down 仅在所有 release/head、reference rate、projection、PlatformInvoice/binding、new dispute、ProviderCostRate、
owner mapping、command/event/outbox/reconciliation/scheduler 水位 pristine 且没有 backfill 时允许。任一 mapping 在
首个 DDL 前阻断。空库验证 `0135 -> 0136 -> 0135 -> 0136`；down 不修改 0022/0041/0072/0073/0090..0096/0131。

生产 rollback 只切回上一 target-compatible reader/worker generation，不能解除旧 write guard、恢复旧外部 API、
重新使用 tenant reviewer 裁决平台账、把官方建议价写入 Gateway 或让旧 scheduler 与新 supervisor 并行消费。

## 5. 六组 Excel API exact 合同

所有 DTO `extra=forbid`；未知/重复 query、重复 JSON key、非规范 UTF-8、客户端 owner/amount/currency/status、
非法 cursor 和超限 body 固定 422。Platform API 只接受 PlatformSession，Workspace API 只接受
WorkspaceSession；Bearer、SupportSession、GlobalSession、ChannelMembership、Virtual Key、重复/错域 Cookie 全拒绝。
响应带 request_id/trace_id、权限指纹、`Cache-Control: no-store, private` 和适用的 quoted ETag。

写操作要求 exact Origin/CSRF、一次性 step-up、Idempotency-Key 和 quoted If-Match；create dispute 的 If-Match 绑定
source Statement/Invoice revision。幂等键绑定 actor/domain/action/owner/canonical body；同键异体 409。认证、权限和
owner 检查在读取正文/资源差异前完成，跨 owner ID 同形 404。

### 5.1 `GET /platform/plans`

按 plan/status/currency/billing period/release generation 和 keyset cursor 查询，limit <= 100。返回 catalog/head、
不可变 version、原币价格、entitlement/contract hash、官方建议成本 release 摘要、readiness 和 actions；不返回
客户订阅列表、内部 ProviderCostRate、Payment、合同/税务资料或任意客户资源明细。

### 5.2 `GET /platform/subscriptions`

只允许 status/plan/reconciliation state/UTC range/cursor/limit <= 100，先从安全 projection 分页；可选单一
`dispute_id` 模式与列表 cursor/filter 互斥，用于返回当前 case/item 的安全决策摘要。返回 Workspace
opaque customer ref、plan/release、subscription state、周期、原币商业金额、order/payment/refund/invoice/dispute
安全状态和 source watermarks；不返回 Workspace 名称搜索目录、联系人、Provider/model/request/Usage、内部成本、
支付方法、税号、银行资料、Invoice object reference 或争议自由文本。

### 5.3 `POST /platform/billing-disputes/{id}/decisions`

body 只接受 `start_review|approve_credit|reject`、固定 decision code 和 expected source revision；ID 必须是
`source_domain=platform_commercial` 的 current case。approve 在同事务锁定 case/item/credit balance、重验 actor 和
审批 policy，追加 decision、commercial adjustment、audit/event/outbox。legacy internal case、自己提交的 case、
已终态、币种/amount 漂移、超额 credit、source reconciliation_required 或迟到撤权全部失败关闭。

### 5.4 `GET /workspace/subscription`

无客户端 owner，允许可选 known period/cursor 读取 current projection。返回套餐/权益、订单/支付/退款、Statement、
Invoice、商业 dispute 进度、原币金额和服务端 actions；不返回平台目录管理、其他 Workspace、渠道结算、内部
Provider 成本明细、支付渠道 reference、税务密文或平台 decision notes。stale/partial 必须显式显示 source 状态。

### 5.5 `POST /workspace/billing-disputes`

body 只接受 source Statement/Invoice/line IDs 和固定 reason code；服务端恢复 source owner、currency、amount、
可争议余额和 revision。相同 source line 同时最多一个 active case。不能提交 request ID 列表、Provider 成本、
内部 FinOps entry、自由文本、期望 credit、客户指定 reviewer 或附件；这些输入统一拒绝。

### 5.6 `GET/PATCH /workspace/provider-cost-rates`

GET 按当前 Workspace 的 provider/model/currency/status/effective time 和 keyset cursor 查询，limit <= 100，返回 rate
version/head、微币单位整数、来源类型/安全 release ref、effective interval、Gateway adoption state 和 actions。
PATCH 只接受 `create_version|adopt_reference|disable`、当前 Provider/model ID、定点整数 rate/effective time 或已发布
reference ID；不接受 Provider Key、Endpoint、任意公式、浮点数、跨币种换算、历史 PriceSnapshot 修改或 source path。

### 5.7 官方建议成本辅助命令

`POST /platform/provider-reference-rates/{provider_id}/commands` 不是第七组 Excel 业务对象 API，而是让 Excel 已明确
要求的平台官方建议价管理可达的最小辅助命令。它复用 PlatformSession、`platform.commercial.plan.manage`、
governance approval、step-up、Idempotency-Key 和 head If-Match；成功 release 写同一 commercial command/audit，
但不产生 Excel 五事件之外的新事件。WorkspaceSession、platform_provider_engineer 和 platform_channel 全部拒绝。

## 6. 状态机、并发和事件语义

Plan release 为 `draft -> review_required -> released -> retired`，released immutable；修正必须新 version。Subscription
source 状态沿用 PRD-098/103，projection 不能反向推进 source。PlatformInvoice 沿用
`requested -> processing -> issued|failed -> reversed|replaced` 的追加链，未知 receipt 不当成功。

BillingDisputeCase 为 `submitted -> under_review -> approved|rejected`，提交者可在 under_review 前转 `withdrawn`；
Platform decision 与 Workspace withdraw 竞争时以 case revision CAS 决定唯一终态。ProviderCostRate version 只追加，
head `active -> superseded|disabled`；effective interval 重叠由 DB constraint/transaction final guard 拒绝。

事件只在真实 head/终态变化后与 source 同事务写入：plan 首次 release、subscription current state/plan/period 真实变化、
commercial dispute 首次 submit/decide、ProviderCostRate head 真实变化。draft、review、projection refresh、no-op、重放、
legacy migration 和 reconciliation read 不发 Excel event。旧 0072/0073/0090..0096 event/outbox 保留原 ID/type/payload/hash/
状态和 frozen subscription，pending 继续按原合同投递；绝不翻译、重发或双发为新事件。

## 7. Console 分域与导航责任

Platform Commercial Accounting 使用独立 Platform client/store/epoch，提供紧凑套餐 release、订阅/Invoice 队列、
商业 dispute 决策、渠道结算摘要和 reconciliation 水位。Workspace 订阅页只显示本空间套餐、账单、Invoice 和
争议进度；企业 FinOps 页面显示 ProviderCostRate、Usage/PriceSnapshot、额度/预算和内部 ledger，不出现平台套餐
发布、争议裁决、渠道佣金或跨客户列表。

`plans`、`model-prices`、`billing-disputes` 按 Excel 拆为 Platform 与 Workspace 两套 route/DTO/client/store；
`finops-ledger` 和 `usage` 保留 Workspace，Platform 另建商业投影。两端可共享 table/pill/pagination 等纯视觉组件，
不能共享 auth client、DTO、cache、action builder 或 AbortController owner。

固定状态包括 loading、empty、partial、stale、reconciliation_required、permission revoked、conflict、dependency
unavailable、payment pending、invoice failed 和 recovery in progress。响应只有在 domain/session/Workspace/
Membership revision/navigation epoch/request generation 全匹配时更新 DOM；离页、登出、切 Workspace、撤权和迟到
200/401/403 都清理金额、cursor、step-up 和 draft。

390/1280/1440、DPR 1/1.25/1.5 下金额、币种、状态和动作无重叠/裁剪/横向溢出；键盘、焦点、读屏、直接 URL
可用。平台页面不展示 Provider/model/request 明细，Workspace 页面不展示平台客户目录或发票对象路径。

## 8. 安全、审计、恢复与 SLO

- 所有金额使用币种定义的整数最小单位或明确的微币单位费率；税率使用版本化整数 ppm/bps，禁止 binary float、
  隐式汇率和跨币种合计。每个 DTO 同时返回 currency、scale/rate unit 和 source version。
- KMS ciphertext、支付 receipt object、Invoice artifact、BillingProfile/证照、Provider Key/Token/Endpoint、
  Prompt/Response/Header、request body/error 和争议正文不进入平台订阅投影、事件、日志或测试证据。
- 审计记录 actor/domain/action/object、Workspace binding safe ref、catalog/subscription/invoice/dispute/rate/ledger/
  reconciliation revision、原币 amount hash、SoD、permission/step-up revision、outcome、request/trace/UTC；不记录 body、
  idempotency key、下载 token、对象路径、银行/税号、provider reference 或客户文本。
- DB/KMS/payment/tax provider/object store/source hash/scheduler 故障对 write/decision/release 失败关闭。Workspace 读可返回
  stale/LKG 安全投影但不得伪造 paid/issued/zero；Webhook 仍 durable receipt，Gateway 沿用已验证 rate LKG/unknown。

Excel SLO 固定为：账单/订阅查询 p95 < 500 ms；冻结 10 万 reconciliation difference 在 < 10 分钟完成；
商业 dispute 真实状态提交到 Workspace/Platform 投影传播 < 5 秒。测量包含鉴权、owner、金额复算、hash/watermark、
event/outbox 和 projection ACK，不只测空缓存 SQL。列表使用覆盖索引/keyset，无全表 COUNT 或 N+1。

## 9. 聚焦测试与批次证据

G6 通过后本条开发只运行：

- Migration：0135/0136 fresh、empty down/re-up；0022/0041/0072/0073/0090..0096/0131 全量 mapping、owner、金额、
  event/outbox/lease/watermark；任一 target fact down guard；source 金额/状态/ID/hash 不变；0073 文本 vault + scrub 是
  唯一允许的受控源变换且可由原 hash/解密抽样证明；down/rollback 不可恢复明文。
- Domain/RBAC：Platform billing/channel/owner/support 与全部 Workspace 角色矩阵；错域 Session/Cookie/Bearer；动态撤权；
  PlatformInvoice global owner + Workspace binding；platform/support 无 Provider/Usage/税务正文。
- Accounting：Plan release immutable；Subscription/Payment/Refund/Invoice/Dispute/rate 状态/CAS/幂等；跨币种；超额 credit；
  provider rate resolver/PriceSnapshot exact；内部 ledger 与平台商业 ledger 永不交叉写。
- Migration/cutover：0072 quota 保留、0073 internal dispute 不入平台队列、FinOps stable IDs、旧 event stable delivery、六
  scheduler lease/fence 接管、Webhook during quiesce、legacy route 待卸载与 target write guard。
- API/Console：六组 Excel API、现有 PRD-103/104 命令回归；两端 store/epoch/late response；金额权限；三浏览器三缩放；
  账单、10 万对账和 5 秒传播 SLO。完整 G7 性能/安全/流程/浏览器/回归在 PRD-147 后统一执行。

测试记录写入 `docs/testing/PRD-144-tests.md`，正式证据进入 `.artifacts`，`.tmp` 不作为交付证据。敏感审计覆盖
staged diff、migration/backfill 临时状态、KMS 工具输出、DB/API/events/logs/artifacts/browser/Storage；任一真实凭据、
支付/税务材料、Endpoint、客户正文或对象路径命中都阻断 PRD-144 独立提交。

## 10. 预计改动边界

- `infra/migrations/0136_platform_commercial_accounting_domain.sql` 与 `.down.sql`
- `control-plane-api/src/metahub_control/commercial_accounting/`
- 现有 `saas_billing/platform_billing/payment_commerce/invoice_reconciliation/billing_disputes/plan_billing` 迁接层
- `platform_access` 固定 extension allowlist、Workspace commercial permission loader 和权限指纹
- Gateway ProviderCostRate resolver、PriceSnapshot/InternalFinOps owner binding 与 rate LKG
- `console-web/src/lib/platformCommercialAccounting.js`、Platform 商业账页面、Workspace subscription/FinOps 分域
- 旧 plan/model-price/dispute/subscription-current route/client/import/prefetch 的待卸载标记
- 对应 migration/Control/Gateway/Console/performance/security 测试与 `docs/testing/PRD-144-tests.md`

本条不修改已应用 0022/0041/0072/0073/0090..0096/0131 migration，不改变默认 Provider/上游，不读取客户
Provider Secret，不实现 PRD-145 页面共享分域、PRD-146 导航合同或 PRD-147 全局 tombstone/SchedulerRegistry。

## 11. 两轮编码前自审

### 第一轮：三套账、legacy 语义与 owner

发现并修正：

1. P0：把 0072 tenant `plans` 迁成 PlatformPlanCatalog 会把企业 BYOK 额度伪装成 MetaHub 商品。修正为 0090
   SaaS catalog 是唯一平台 registry；0072 完整保留为 Workspace 额度账并增加复合 owner mapping。
2. P0：把 0073 内部 FinOps dispute 放进平台商业裁决队列会让平台员工改客户内部成本账。修正为 legacy case
   迁到 InternalFinOpsLedger 子域；新 BillingDisputeCase 只引用平台 Statement/Invoice。
3. P0：把官方建议成本直接作为 Gateway rate 会在平台发布时改变客户预算和历史成本。修正为 global reference
   release 与 Workspace ProviderCostRate 分表，只有显式采用生成新 Workspace version。
4. P0：让 PlatformInvoice 直接以 tenant 为 owner 与 Excel 全局对象冲突，也可能让 Workspace 写平台发票。
   修正为无 tenant 的 PlatformInvoice 主表 + 唯一 Workspace binding；Workspace 只能读 binding 投影/短时原件。
5. P1：复制 0041 FinOps entries 到新表会产生双 writer 和重复调整。修正为保留唯一 entry registry，增加不可变
   Workspace owner binding；平台只读聚合 hash/count/amount。
6. P1：让 SubscriptionProjection 接受状态命令会与 0090..0096 source 竞争。修正为严格只读、单调 watermark 投影，
   所有命令仍写 source transaction。

结论：第一轮 P0=0（4 项已关闭），P1=0（2 项已关闭）。继续第二轮审查。

### 第二轮：命令可达性、事件、Scheduler 与对账真实性

发现并修正：

1. P0：Excel 只有 Plan GET，但 platform_billing 被要求管理/发布套餐，若删除现有 POST 会让 `plan.released`
   不可达。修正为保留 0090 `POST /platform/plans` 的受控 candidate/action 合同，并让 0136 release wrapper 同事务
   产生 head/event；不新增通用脚本或任意定价入口。
2. P0：Excel 同时要求平台管理官方建议价，却没有对应写 API；复用 tenant `/model-prices` 会跨 owner。修正为窄
   `provider-reference-rates/{provider_id}/commands`，只管理 global reference release，绝不进入 Gateway 或 Workspace
   rate head。
3. P0：六个旧 scheduler 与新 projection worker 并行会重复 Payment/Invoice/effect/event。修正为一个 commercial
   supervisor 替换六个 installer，保留独立 queue/lease/watermark，并对每次提交做 fence 校验。
4. P0：把旧 event 翻译成五个 Excel event 会让客户收到重复账务通知。修正为 legacy ID/type/payload/状态原样续投；
   新 event 只由 target 真实变化产生，migration/no-op 不发。
5. P0：争议接受自由文本、客户端金额或 request ID 会复制业务正文并允许客户操纵 credit。修正为固定 reason、
   Statement/Invoice line 引用和服务端金额；旧文本 KMS 迁移后不进入新队列。
6. P1：只比较 reconciliation 行数无法发现币种、owner 或金额漂移。修正为冻结多源 watermark、逐币种整数、
   canonical hash、owner coverage、outbox/audit/scheduler 水位并复算相同 result hash。
7. P1：只从侧栏隐藏旧 plan/model-price/dispute 会保留直接 URL、DOM、prefetch 和 API。修正为本条安装 target
   write guard/待卸载标记，PRD-147 统一 router/import/DOM/prefetch/Navigation ordinary 404 归零。
8. P1：把 CNY/USD 汇总为一个“成本”会制造隐式汇率。修正为所有 rate/ledger/invoice/difference 按原币独立，
   无显式版本化 FX fact 时禁止跨币种相减或排序。

结论：第二轮 P0=0（5 项已关闭），P1=0（3 项已关闭）。两轮自审后无未关闭 P0/P1；G6 未通过前，
本设计不授权创建 0136、修改运行代码、切换 Gateway rate resolver 或启动 S7 migration。
