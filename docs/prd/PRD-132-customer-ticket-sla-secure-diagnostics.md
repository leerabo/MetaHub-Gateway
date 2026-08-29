# PRD-132 客户工单、SLA 与安全诊断协作

状态：已完成。依赖 PRD-095/105/109/112/125/126；范围以 Excel PRD-132 为准。

## Excel 权威合同

- API：`GET/POST /support/tickets`、`PATCH /support/tickets/{id}`、
  `POST /support/tickets/{id}/attachments`、`POST /support/tickets/{id}/escalations`。
- 对象：`SupportTicket`、`TicketMessage`、`TicketAttachment`、`SlaClock`、`Escalation`、
  `SatisfactionSurvey`。
- 事件：`support.ticket_created`、`support.sla_at_risk`、`support.ticket_resolved`。
- SLO：工单列表 p95 < 500ms；P1 告警到升级 < 5 分钟；附件扫描有超时状态。

## 1. 产品闭环

Workspace 成员以固定 category、severity、subject 和客户消息创建本空间工单，可关联 request_id、incident 或
EdgeDeployment 的安全摘要。系统启动版本化 SLA clock，分配 platform_support 队列，并在风险阈值触发持久升级
和通知。客户消息、平台内部备注、附件和诊断摘要使用四套独立可见性合同；解决后客户确认或重新打开，并可提交
一次 satisfaction survey。

附件先进入私有 quarantine，完成大小/类型、恶意内容、压缩、敏感字段和 owner 扫描后才原子发布；超时、失败和
隔离均是可见状态。关联 request_id 不自动授予 Prompt/Response、日志或 Deployment 读取；任何诊断详情仍需有效
PRD-095 SupportGrant，逐请求验证 scope、双方批准、TTL 和 revision。

## 2. 0124 数据与 SLA 状态

- ticket、message、attachment、clock、escalation、survey 全部带 Workspace/tenant 复合 owner；平台队列使用
  owner-qualified projection，不复制客户正文。
- ticket 状态固定为 `open|triaged|waiting_customer|waiting_platform|resolved|closed`，severity、category、
  assignee、revision 和 last customer/platform activity 均严格校验。
- message visibility 固定为 `customer|internal`；客户 API 永不返回 internal。正文使用独立加密用途域，事件只含
  message ID、visibility、状态和时间，不含正文。
- attachment metadata 保存 digest、media type、size、scan version/outcome、immutable object reference hash 和
  download generation；对象 URL、文件路径和内容不进普通 DTO。
- SLA clock 由追加事件计算 `running|paused_customer|paused_policy|stopped`，持久 deadline/watermark 防止重启漂移。

Down 在任一工单、消息、附件、SLA、升级、调查或事件存在时拒绝；关闭工单不删除审计事实。

## 3. 权限、JIT 与 Console

Workspace 成员只能读写本空间工单；ticket reporter、owner/admin 和显式 support permission 决定客户侧动作。
platform_support 通过独立 Platform Session 处理裁剪队列，不能复用 Workspace Cookie；内部备注只在 Platform DTO，
客户消息只按工单参与者返回。关联诊断需要独立 SupportGrant，grant 到期/撤销/权限漂移后下一请求立即失败关闭。

POST/PATCH 使用 CSRF、Idempotency-Key、If-Match、严格字段、no-store 和同形 404。Workspace Console 展示工单列表、
消息时间线、SLA、附件扫描、升级、解决/重开和满意度；Platform Console 展示裁剪队列、内部协作、SLA 风险和 JIT
入口，不显示没有 grant 的诊断字段。迟到响应通过 page epoch 丢弃，离页清除正文和对象 URL。

## 4. 第一轮自审：内容与支持权限

- P0：工单关联 request_id 后自动加载请求正文会绕过客户授权。修正为只存 owner-qualified 摘要，诊断独立 JIT。
- P0：内部备注和客户消息共用 DTO 会把内部协作泄漏给租户。修正为分表 visibility、独立 serializer 和路由。
- P0：扫描前提供附件下载会传播恶意内容。修正为 quarantine、私有扫描 worker、通过后原子发布和短 TTL 下载。
- P1：platform_support 角色本身被当作 Workspace membership。修正为双域 Session 与 SupportGrant，平台角色不扩权。

## 5. 第二轮自审：SLA 与恢复

- P0：进程内 SLA timer 在重启后丢失或重复升级。修正为追加 clock event、持久 deadline、scheduler watermark、
  lease fencing 和唯一 escalation generation。
- P0：迟到扫描把已替换附件标为可下载。修正为 attachment revision/digest 绑定，旧结果只留审计。
- P1：重复解决/重开产生多份通知。修正为 CAS 状态转换和 source event 唯一键，outbox 可幂等重放。
- P1：客户等待与平台等待暂停规则模糊会操纵 SLA。修正为版本化 policy 和固定 transition，历史 clock 不重算。

两轮均为修正后通过，无未关闭 P0/P1 设计问题。

## 6. 专项验证计划与批次边界

0123/0124 往返/down guard；ticket/message/attachment owner；客户/internal 字段隔离；匿名/跨租户/动态撤权；
附件路径穿越、压缩、恶意内容、超时和重复上传；SLA pause/resume/risk/escalation、重启接管和迟到 worker；
SupportGrant 缺失/过期/撤销；通知/outbox；列表和 5 分钟升级 SLO；真实 Workspace/Platform 浏览器、三缩放、
键盘/焦点、敏感扫描和静止终态。完整 G6 留到 PRD-138 后统一执行。

实现已交付 0124、Workspace/Platform Cookie-only API、严格 DTO、客户消息与内部备注分域加密、附件隔离扫描、
持久 SLA clock/升级、满意度和双域 Console；聚焦迁移/API、Console、必要导航权限回归和静态检查通过。真实
数据库矩阵、FLOW、浏览器、性能、安全、兼容性和 72 小时长稳仍按批次规则留到 PRD-138 后统一执行，不能由
本条专项结果替代。
