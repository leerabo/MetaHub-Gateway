# PRD-103 订单、支付、催收与退款

状态：设计完成，待编码
阶段：S3-SaaS 商业化
优先级：P0
依赖：PRD-098、PRD-099、PRD-100、PRD-102

## 1. 目标与边界

本条连接受支持支付渠道，交付订阅订单、支付意图/尝试、签名 Webhook receipt、催收和退款。系统不保存
卡号、CVV、支付密码或 provider client secret；浏览器只获得渠道 SDK 所需短期 token。支付回调可重复、
乱序和延迟，任何一次都不能重复激活权益、记账或退款。

## 2. 0095 不可变事实

- `subscription_orders`：workspace、subscription、plan version、原币金额、状态、规范 intent hash。
- `payment_intents`：order、provider、amount/currency、provider reference tokenized、state revision。
- `payment_attempts`：append-only attempt、provider status、发生时间和安全错误码。
- `payment_webhook_receipts`：provider event id、raw body object reference/hash、签名 key version、received_at、
  processing lease/status；`UNIQUE(provider,event_id)`。
- `refunds`：order/payment owner、amount/currency、reason code、状态和 idempotency hash。
- `dunning_cases/attempts`：past_due 周期、下一动作、固定模板引用，不保存自由文本。
- `commercial_effects`：receipt/order 到 entitlement、meter ledger、refund ledger 的唯一 effect key/outbox。

所有金额为原币小单位整数，refund 累计不得超过 captured。原始回调加密保存于隔离 object store，仅故障
调查受控读取；通用日志只含 receipt id/hash/provider/status。

## 3. Webhook 与 exactly-once

`POST /payments/webhooks/{provider}` 在读取业务字段前，以原始 bytes、时间戳、签名和当前/上一 key 验证；
有界 body 和时间窗失败返回固定 4xx。验证成功先持久化唯一 Receipt 再应答，处理由 lease worker 完成。

worker 读取 provider event version/occurred_at，按支付状态机处理乱序：旧事件保留 receipt 但不倒退状态；
相同 event 重放返回已有 receipt；同 ID 不同 hash 标记安全冲突。capture/refund 通过 `commercial_effects`
唯一键原子生成订单状态、Entitlement 内部命令、账务调整/outbox，网络重试不能复制效果。

## 4. 客户命令与状态

`POST /subscriptions` 和 `PATCH /subscriptions/{id}` 延续 PRD-098 WorkspaceSession owner/finance 边界；付费
变更创建 Order，不直接 active。`POST /payments/intents` 绑定当前 workspace 的 payable Order，要求幂等键。
`POST /refunds` 首版仅 Workspace owner/finance 提交申请，实际执行需 platform_billing 审核的内部状态；
客户不能提交 provider reference、amount captured 或状态。

PaymentIntent：`requires_action/processing/succeeded/failed/canceled`；Order：`pending_payment/paid/fulfilled/
past_due/canceled/refunded/partially_refunded`。Dunning 不阻塞已有请求事务，通过 scheduler/outbox 通知；
权益收缩只由冻结的 grace policy 推进，不能在通知发送失败时回滚支付事实。

## 5. HTTP 合同

| 方法与路径 | 主体 | 语义 |
| --- | --- | --- |
| `POST /subscriptions` | Workspace owner/finance | 创建订单化订阅 |
| `PATCH /subscriptions/{id}` | Workspace owner/finance | CAS 变更/取消 |
| `POST /payments/intents` | Workspace owner/finance | 幂等创建渠道意图 |
| `POST /payments/webhooks/{provider}` | provider signature | 持久 receipt，快速应答 |
| `POST /refunds` | Workspace finance | 幂等退款申请 |
| `GET /billing/orders` | Workspace finance | 订单/支付/退款安全摘要 |

回调路由不解释任何浏览器 Cookie/Bearer；业务路由拒绝 provider 签名身份。所有客户响应 no-store，不返回
原始回调、渠道 Secret、支付方法详情或内部风控信息。

## 6. 性能、恢复与测试

Webhook 持久化 p95 `<200 ms`，不等待权益/通知；订单列表 `<300 ms`。0095 仅 pristine down，代码回退
必须继续接收并持久 receipt 或明确停机，不能 2xx 丢事件。专项覆盖签名当前/上一 key、重放/篡改/乱序、
100 并发 capture/refund、超额退款、effect exactly-once、dunning、渠道超时、凭据负矩阵、PCI canary 0 命中、
PRD-098/099 回归。

## 7. 第一轮自审：回调与账务效果

结论：通过，P0/P1 已修正。

- P0：先 2xx 再落 receipt 会永久丢支付。修正为验签后 durable receipt 再应答。
- P0：按到达顺序覆盖状态会被乱序事件倒退。修正为 provider event version/occurred_at 和单调状态机。
- P0：receipt 重放可能重复权益与账务。修正为 provider event 唯一键和 commercial effect 唯一键。
- P1：业务事务同步调用渠道会放大超时。修正为 intent/worker/outbox，客户事务只写本地事实。

## 8. 第二轮自审：凭据与退款

结论：通过，P0/P1 已修正。

- P0：回调同时接受 Cookie 可形成 credential confusion。修正为 webhook 只接受 provider signature。
- P0：保存原始卡数据会扩大 PCI 范围。修正为渠道 token 与隔离加密 receipt，不保存 PAN/CVV。
- P0：退款累计竞态可超过 captured。修正为锁内按原币整数求和和唯一 effect。
- P1：通知失败回滚支付状态会使渠道与本地分叉。修正为通知独立 outbox，支付事实不可逆。

