# PRD-104 发票、税务资料与对账

状态：设计完成，待编码
阶段：S3-SaaS 商业化
优先级：P0
依赖：PRD-099、PRD-102、PRD-103

## 1. 目标与边界

本条生成账单结算单、管理发票申请/第三方开票产物并执行支付、订单、Meter 和发票对账。系统不自建税控
开票引擎，只通过合规 provider adapter 提交经验证资料并保存回执。税率/币种按原始事实保真，禁止将不同
币种转换后覆盖原金额。红字冲销属于高风险命令，要求两个不同 platform_billing/compliance 审批人。

## 2. 0096 数据模型

- `billing_statements`：workspace/cycle、meter summary hash、原币 subtotal/tax/total、状态和 immutable version。
- `invoice_requests`：statement、billing profile revision、类型、状态、幂等 hash。
- `invoice_artifacts`：provider invoice id tokenized、号码掩码、加密 object key/hash、KMS version、issued_at。
- `invoice_reversal_approvals`：append-only domain、operator、decision、reason code；申请人不得自批。
- `reconciliation_runs`：scope、watermarks、source hashes、状态、owner/lease/generation。
- `reconciliation_differences`：source refs、difference type、原币 integer、状态和 adjustment reference。

Statement 和 artifact 发布后不可修改；更正通过 credit/red-letter/replacement 链追加。对象下载 URL 不落库，
每次按 WorkspaceSession、workspace、artifact、expiry 签发短时 token。

## 3. 工作流

Cycle close 从 PRD-099 frozen summary 生成 Statement。`POST /invoices` 锁定 Statement 与 BillingProfile
revision，校验企业认证/资料后写 request/outbox；外部开票在事务外执行，幂等 provider reference 防重。
渠道回调先持久 receipt，再追加 artifact/status，不能原地替换 PDF。

ReconciliationRun 冻结 payment/order/meter/statement/invoice watermarks，按币种和 owner 比较。difference 只
记录 ID、类别和金额，不复制回调/税务资料。自动修复只允许显然可重放的 projection；财务差异必须人工
adjustment。红字冲销需两个不同平台 actor 的 append-only approval，第二批准原子写 provider outbox。

## 4. HTTP 合同

| 方法与路径 | 主体 | 语义 |
| --- | --- | --- |
| `GET /billing/statements` | Workspace finance | 当前 Workspace 结算单列表 |
| `POST /invoices` | Workspace owner/finance | 幂等发票申请 |
| `GET /invoices/{id}` | Workspace finance | 状态、掩码、短时下载动作 |
| `POST /platform/reconciliations` | platform_billing/compliance | 创建冻结水位对账 |
| `GET /platform/reconciliations/{id}/differences` | platform_billing/compliance | 游标差异安全摘要 |

Workspace/Platform 凭据严格分域。发票下载响应 `private/no-store/nosniff`，token 单次、短 TTL、tenant-bound；
平台对账不能下载客户发票原件。请求正文拒绝客户端金额、税额、workspace、provider invoice id 和状态。

## 5. 性能与测试

Statement 列表 `<300 ms`，10 万差异游标页 `<500 ms`；对账 run 异步且每批有界。专项覆盖 0095->0096/
fresh/down/up、币种保真、profile revision、provider 重放、artifact hash、下载跨租户/过期/重放、双审批/
自批拒绝、对账水位/迟到事实、差异不自动改账、凭据负矩阵、PDF/CSV 安全和敏感 canary。

## 6. 第一轮自审：税务与不可变事实

结论：通过，P0/P1 已修正。

- P0：自建税控号码/签章会承担不受控合规职责。修正为合规 provider adapter 和回执事实。
- P0：原地替换 Statement/PDF 会破坏审计。修正为不可变版本与 reversal/replacement 链。
- P0：跨币种汇总后覆盖原币会造成对账误差。修正为币种分组、小单位整数和原始金额保真。
- P1：开票事务等待 provider 会长事务锁表。修正为 request/outbox/receipt 异步状态机。

## 7. 第二轮自审：审批、下载与对账

结论：通过，P0/P1 已修正。

- P0：单人红字冲销可隐藏账务错误。修正为两个不同平台 actor 的 append-only 双批准。
- P0：长期对象 URL 或平台下载会泄露税务原件。修正为 Workspace tenant-bound 单次短链，平台只看摘要。
- P1：对账自动改原始账本会掩盖差异来源。修正为 difference + 显式 adjustment，不修改 source facts。
- P1：未冻结 watermark 的长对账结果不可复现。修正为 run 保存各源水位和 hash。

