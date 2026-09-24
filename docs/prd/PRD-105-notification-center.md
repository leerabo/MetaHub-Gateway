# PRD-105 通知中心、模板与偏好

状态：设计完成，待编码
阶段：S3-SaaS 商业化
优先级：P0
依赖：PRD-095、PRD-098..104

## 1. 目标与边界

本条提供 Workspace 通知中心、渠道偏好、平台模板版本和异步投递。任何订阅、支付、认证、发票或安全业务
事务只写通知 source/outbox，绝不等待邮件/短信/WebPush。模板参数使用事件类型专属 exact allowlist，正文
不得包含 Secret、Token、Cookie、TOTP、Prompt/Response、证照原件、支付方法、完整税号/银行账号或自由
文本原因。

强制安全通知不可退订；商业/产品通知可按 channel/category 关闭。关闭外部渠道不删除站内通知或业务
事实。首版 adapter 可为 email/webhook/in_app；SMS/WebPush 未配置时明确 unavailable，不伪造 sent。

## 2. 0097 数据模型

- `notification_templates`：platform-owned code/channel/locale/version/schema、subject/body、parameter allowlist
  hash、status；published 后不可变。
- `notifications`：workspace/recipient account、event/source owner、template version、safe parameters cipher/hash、
  category、read_at、created_at；source 唯一。
- `notification_preferences`：workspace/account/category/channel、enabled、revision；mandatory_security CHECK true。
- `delivery_attempts`：notification/channel/provider、attempt、status、lease/generation、safe error code、时间。
- `channel_suppressions`：recipient blind index、channel、reason code、source、期限；不保存明文地址。
- `notification_outbox`：source、template snapshot、recipient reference、状态与 retry policy；不复制联系方式。

投递时才从 Identity 受控解析已验证联系地址；地址不进入 outbox、日志或通用审计。模板渲染器无表达式、
include、反射或 HTML raw，缺失/未知参数失败为 template_invalid 并隔离该消息，不阻塞其他租户。

## 3. API 与权限

| 方法与路径 | 主体 | 语义 |
| --- | --- | --- |
| `GET /notifications` | WorkspaceSession | 当前 account 在当前 Workspace 的游标列表 |
| `PATCH /notifications/{id}/read` | WorkspaceSession | 幂等标记本人通知已读 |
| `GET /notification-preferences` | WorkspaceSession | 本人偏好与 mandatory 标记 |
| `PATCH /notification-preferences` | WorkspaceSession | If-Match 更新可选类别 |
| `GET /platform/notification-templates` | platform role | 模板版本安全列表 |
| `POST /platform/notification-templates` | platform communications/owner | 创建 draft/publish 版本 |

Workspace 通知由 recipient account + workspace 双 owner 限制，owner/admin 不能读其他成员通知。平台模板路由
只解释 PlatformSession；平台员工不能读 Workspace 收件箱。模板写要求 Origin/CSRF、If-Match、幂等和平台
审计，禁止预览真实客户参数。

## 4. 处理、重试与显示

业务 source 在同一事务创建唯一 notification outbox reference；worker 租约读取 published template snapshot，
校验参数 allowlist，生成 in_app Notification 并独立投递各外部 channel。指数退避有上限，永久失败进入
suppressed/failed；单个坏模板、收件人或 provider 不能形成全局队首阻塞。

偏好在创建 attempt 前读取并冻结 decision；mandatory security 始终投递 in_app 和已配置安全渠道。已读
使用 `read_at IS NULL` CAS，重复 PATCH 返回同一结果。列表只返回已渲染的安全正文、category、time、read，
不返回 address、source raw payload、template expression 或 provider response。

## 5. Console、性能与保留

顶栏铃铛使用熟悉图标和 tooltip，固定尺寸避免计数抖动；中心页为高密度列表、未读筛选和偏好开关，强制
安全项禁用并说明为系统策略状态。平台页为模板版本表和受控编辑器。390/1280/1440 无重叠。
列表 `<250 ms`，标记已读 `<150 ms`，业务事务新增 outbox p95 开销 `<10 ms`。通知按 policy 到期删除，
投递 attempt 保留安全摘要，业务 source 不随通知删除。

## 6. 专项测试

覆盖 0096->0097/fresh/down/up、source exactly-once、坏模板隔离、参数未知/缺失、HTML 注入、偏好并发 CAS、
mandatory 不可退订、跨 Workspace/成员、动态撤权、provider 超时/退避/抑制、队首公平、模板发布不可变、
凭据负矩阵、三视口/键盘/计数稳定、敏感 canary 与 PRD-098..104 业务不等待回归。

## 7. 第一轮自审：交易解耦与投递

结论：通过，P0/P1 已修正。

- P0：交易内同步发邮件会让 provider 故障回滚支付/发票。修正为事务 source/outbox，投递完全异步。
- P0：重放 source 会重复通知。修正为 source owner 唯一键和 delivery attempt generation。
- P0：坏模板阻塞同一队列会形成跨租户 DoS。修正为精确租约隔离失败并继续扫描。
- P1：退订删除站内事实会破坏审计。修正为偏好只控制未来外部 attempt，不删除业务/站内记录。

## 8. 第二轮自审：模板、隐私与授权

结论：通过，P0/P1 已修正。

- P0：通用模板表达式可读取任意对象字段。修正为无表达式渲染器和事件专属 exact allowlist。
- P0：outbox 保存邮箱/手机号会复制 Identity PII。修正为 recipient reference，发送时受控解析。
- P0：admin 读取成员收件箱会越过个人通知边界。修正为 account+workspace 双 owner，只有本人读取。
- P1：强制安全通知可关闭会失去安全告警。修正为数据库约束和服务双重拒绝退订。

