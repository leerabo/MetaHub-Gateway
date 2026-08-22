# Orders, Payments, Dunning, and Refunds API

PRD-103 将订阅意图物化为不可变订单，并提供支付意图、签名回调 receipt、催收和退款审核。金额均为原币
最小单位整数；系统不接收或保存卡号、CVV、支付密码、支付方法详情或 provider client secret。

## 凭据域

- `/billing/orders`、`/payments/intents`、`/refunds` 和 `/subscriptions/{subscription_id}` 只解释 active
  WorkspaceSession，当前职责必须为 `owner` 或 `finance_admin`。
- `/platform/refunds*` 只解释 PlatformSession；列表要求 `platform.billing.read`，审核要求
  `platform.billing.adjust`。
- `/payments/webhooks/{provider}` 只接受 provider HMAC。该路由拒绝 Cookie、Authorization、Origin、CSRF 和
  browser fetch metadata，先按原始 bytes 验签，再解析业务 JSON。
- Workspace、Platform、Support、Bearer 与 provider signature 不能跨域替代或混用。客户和平台写命令要求
  唯一允许 Origin、对应 CSRF Cookie/Header 和严格 JSON。

## 接口

| 方法 | 路径 | 请求边界 | 成功响应 |
| --- | --- | --- | --- |
| GET | `/billing/orders` | 无 query/body | 当前 Workspace 的订单、支付意图、退款与催收安全摘要 |
| POST | `/payments/intents` | `order_id`、固定 provider、`Idempotency-Key` | 支付意图；首次创建附一次性 `client_token`，重放返回 `null` |
| POST | `/payments/webhooks/{provider}` | 原始 JSON bytes、timestamp、key version、HMAC | durable receipt identity 与持久化状态 |
| POST | `/refunds` | 订单、最小单位金额、固定原因、显式确认、`Idempotency-Key` | `requested` 退款摘要 |
| GET | `/platform/refunds?status=...` | 必须且仅能包含固定 status | 最多 100 条退款审核摘要 |
| POST | `/platform/refunds/{refund_id}/review` | `If-Match`、固定 decision/reason、显式确认 | `processing` 或 `rejected` 退款摘要 |
| PATCH | `/subscriptions/{subscription_id}` | `If-Match`、`Idempotency-Key`、`change_plan/cancel` | 新订阅 revision 与关联订单状态 |

所有客户和平台响应均为 `Cache-Control: no-store, private`。响应不返回 provider reference、支付方法、原始回调、
签名密钥、client token hash、渠道错误正文、内部审核备注或商业 effect key。

## 一次性支付凭据

支付意图首次创建时返回短期 `client_token`，数据库只保存其 HMAC。幂等重放不再次返回 token。Console 只把
token 传给页面内 provider 消费回调，并在成功、失败或回调抛错时立即清空响应对象；token 不进入 DOM、URL、
localStorage、sessionStorage、页面控制器状态或通用日志。

## Webhook 与 Exactly-once

回调在读取业务字段前使用当前或上一版本 HMAC 密钥验证原始 bytes、timestamp 和声明 key version。通过后，原始
body 使用独立 AES-256-GCM/HKDF 域加密，AAD 绑定 provider、event 与 key version，并写入隔离 object root；数据库
只保存 object reference、hash、key version、处理 lease 和安全状态。

`UNIQUE(provider,event_id)` 收敛重放；同 ID 不同 body hash 标记安全冲突。worker 使用 provider event version 和
occurred_at 做单调状态推进，旧事件保留 receipt 但不倒退支付状态。capture、dunning recovery 和 refund ledger 通过
唯一 `commercial_effects.effect_key` 与订单/订阅状态同事务提交，网络重试不能重复激活权益、记账或退款。

## 退款与催收

退款申请只绑定当前 Workspace 已成功支付的订单，客户不能提交 captured 金额、provider reference 或状态。服务在
写锁内计算所有 `requested/processing/succeeded` 退款，累计金额不得超过 captured；Platform 审核使用强 ETag CAS。
批准只推进为 `processing`，最终 `succeeded/failed` 由已验签回调决定。

支付失败创建独立 dunning facts、固定模板引用和下一动作时间。通知 delivery 不参与支付事实事务，通知失败不能回滚
`past_due` 或后续 recovery；权益收缩只由冻结的 grace policy 推进。

## 配置

| 配置 | 约束 |
| --- | --- |
| `METAHUB_PAYMENT_WEBHOOK_KEY_VERSION` | 当前正整数签名版本 |
| `METAHUB_PAYMENT_WEBHOOK_SECRET` | 当前独立 HMAC secret，生产必填 |
| `METAHUB_PAYMENT_WEBHOOK_PREVIOUS_KEY_VERSION` / `METAHUB_PAYMENT_WEBHOOK_PREVIOUS_SECRET` | 可选上一版本，只用于轮换窗口 |
| `METAHUB_PAYMENT_RECEIPT_ENCRYPTION_SECRET` | 独立 32..512 byte receipt 加密根密钥 |
| `METAHUB_PAYMENT_RECEIPT_STORAGE_ROOT` | 隔离对象根，不得位于公开静态目录 |
| `METAHUB_PAYMENT_WEBHOOK_MAX_BODY_BYTES` | 有界 raw body 上限 |
| `METAHUB_PAYMENT_WEBHOOK_TOLERANCE_SECONDS` | 签名时间窗 |
| `METAHUB_PAYMENT_WORKER_POLL_INTERVAL_SECONDS` | receipt worker 调度间隔 |

## 固定错误

业务错误使用 `payment.request_invalid/not_found/conflict/idempotency_conflict/webhook_invalid/webhook_conflict/`
`unavailable` 和 `refund.request_invalid/not_found/amount_exceeded/idempotency_conflict/conflict`。凭据、Origin、CSRF、
Session 和权限错误沿用 `workspace_session.*` 或 `platform.*`。错误只返回固定安全文案与关联 ID，不透传请求正文、
SQL、对象引用、路径、签名值、Cookie、Session、TOTP、client token 或 provider 详情。
