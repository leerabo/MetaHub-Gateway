# Notification Center API

PRD-105 提供按 Workspace 与 recipient account 隔离的站内通知、渠道偏好、不可变平台模板版本和异步投递。
订阅、认证、支付、发票及安全业务事务只追加唯一 source/outbox，不等待任何外部 provider。

## 凭据域

- `/notifications` 与 `/notification-preferences` 只解释 active WorkspaceSession，并始终限定为当前
  `workspace_id + account_id`；owner/admin 不能读取其他成员收件箱。
- `/platform/notification-templates` 只解释 PlatformSession。读取要求
  `platform.notification_template.read`，创建版本要求 `platform.notification_template.write`。
- Workspace、Platform、Support、Bearer 与其他 Cookie 不能混用。写操作要求唯一允许 Origin、对应 CSRF
  Cookie/Header 和严格 JSON；偏好与模板写入必须提供当前 quoted `If-Match`。
- 平台操作员不能读取 Workspace 收件箱，Workspace 成员不能读取或发布平台模板。

## 接口

| 方法 | 路径 | 请求边界 | 成功响应 |
| --- | --- | --- | --- |
| GET | `/notifications` | `unread_only`、`limit=1..100`、可选 opaque cursor | 当前账号安全正文、类别、时间、已读状态与未读总数 |
| PATCH | `/notifications/{notification_id}/read` | 空 JSON、Workspace CSRF | `read_at IS NULL` CAS 后的同一安全投影 |
| GET | `/notification-preferences` | 无 query/body | 6 类别 x 5 渠道、mandatory/available 与 revision |
| PATCH | `/notification-preferences` | exact changes、Workspace CSRF、`If-Match` | 全部偏好和新 revision |
| GET | `/platform/notification-templates` | 无 query/body | 模板版本表与 catalog revision |
| POST | `/platform/notification-templates` | Platform CSRF、`If-Match`、`Idempotency-Key` | 新 draft/published 不可变版本 |

所有 JSON 响应均为 `Cache-Control: no-store, private`。列表不返回联系地址、source raw payload、模板表达式、
provider response、Tenant/Account owner 字段或内部基础设施信息。

## 事件合同

| 事件 | 类别 | exact 参数 |
| --- | --- | --- |
| `security.sign_in_risk` | security | `event_label`, `occurred_at` |
| `subscription.status_changed` | billing | `effective_at`, `plan_name`, `status` |
| `verification.status_changed` | verification | `status`, `updated_at` |
| `payment.status_changed` | payments | `amount`, `currency`, `status`, `updated_at` |
| `invoice.issued` | invoices | `currency`, `invoice_number_masked`, `issued_at`, `total` |
| `invoice.reversed` | invoices | `invoice_number_masked`, `reversed_at` |
| `infrastructure.backup_completed` | operations | `completed_at`, `status` |
| `gateway.health_changed` | operations | `cell_label`, `observed_at`, `status` |

参数键必须与事件 allowlist 完全相等，值为有界安全字符串。Secret、Token、Cookie、TOTP、Prompt/Response、
证照原件、支付方法、完整税号、银行账号和自由文本原因均禁止进入参数。

## 模板与投递

模板只允许 `{{name}}` 占位符；表达式、include、反射、控制字符、未配对花括号和 raw HTML 均被拒绝。每个模板
版本保存 allowlist hash 与 canonical content hash，published 版本不可更新或删除；修订只能创建下一版本。

source 事务以 `workspace + recipient + event + source owner` 唯一键追加加密参数和模板快照。worker 使用有界租约、
generation CAS、Workspace 公平领取、指数退避和最大尝试次数。单条坏模板只隔离其 outbox，不阻塞其他 Workspace。
首版 `in_app` 始终真实落库；email/webhook 只有在 adapter 与受控联系地址均可用时投递。SMS/WebPush 明确返回
unavailable，不伪造 sent。

投递准备阶段才从 Identity 或 Webhook 配置解析联系地址。地址不写入 outbox、attempt、通用审计或日志；
suppression 只保存域分离 blind index。关闭可选外部渠道不会删除站内通知或业务事实。security 类别强制启用，
客户端与数据库均拒绝退订。

## 固定错误

业务错误使用 `notification.request_invalid/not_found/revision_required/revision_conflict/idempotency_conflict/
source_conflict/template_invalid/mandatory_required/channel_disabled/channel_unavailable/recipient_unavailable/
suppressed/delivery_unavailable/unavailable`。凭据、Origin、CSRF、Session 与权限错误沿用 `workspace_session.*` 或
`platform.*`。错误只返回固定安全文案与关联 ID，不透传参数明文、地址、provider 内容或私有运行时信息。
