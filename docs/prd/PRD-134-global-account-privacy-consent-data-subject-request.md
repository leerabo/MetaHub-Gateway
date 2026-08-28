# PRD-134 全局账号隐私、同意与数据主体请求

状态：设计冻结，尚未实现。依赖 PRD-089/091/092/105/117；范围以 Excel PRD-134 为准。

## Excel 权威合同

- API：`GET/PATCH /identity/privacy-preferences`、`POST /identity/data-subject-requests`、
  `GET /identity/data-subject-requests/{id}`、`GET /platform/privacy/queue`。
- 对象：`ConsentRecord`、`PrivacyPreference`、`DataSubjectRequest`、`IdentityExport`、
  `ErasureDecision`、`RetentionException`。
- 事件：`privacy.consent_changed`、`privacy.request_received`、`privacy.request_completed`。
- SLO：请求状态查询 p95 < 500ms；法定 SLA 前预警；导出大任务异步可恢复。

## 1. 产品闭环

已认证全局账号查看全局条款版本，并在当前 Workspace Session 下管理本空间偏好或提交 `export|rectify|erase`
数据主体请求。Control 构建当前 `workspace_id/tenant_id/account_id` 限定的数据图，将任务分发给 identity、
Membership、audit、billing、support、Edge 和 data governance 适配器；每个 owner 只返回该主体在当前 Workspace
可导出的字段或固定删除决定，其他 Workspace 一律不进入该请求。用户查看进度，完成后通过短 TTL 一次性下载
加密 IdentityExport。

企业管理员只能移除 Membership 或响应本 Workspace 的协调任务，不能代表用户删除全局账号。platform_privacy 只看
最小处理队列；legal_admin 通过 PRD-117 Legal Hold/retention 产生可解释例外。保留事实不出现在导出正文，且保全不
扩大读取权限。

## 2. 0126 数据与 DSR 编排

- ConsentRecord 是平台全局账号事实，追加保存 subject、purpose、terms version、locale、source kind、decision、time 和
  evidence digest，不带 tenant_id。PrivacyPreference 带 Workspace/tenant 复合 owner，保存当前 CAS projection。
- DataSubjectRequest 带 `workspace_id/tenant_id/account_id` 复合 owner，使用
  `received|identity_verified|collecting|reviewing|ready|completed|partially_denied|failed|cancelled`，并保存 request kind、
  deadline、revision 和 verification generation。
- IdentityExport、ErasureDecision 和 RetentionException 都继承同一 Workspace/tenant owner。Export 只保存 encrypted
  artifact digest、schema version、owner manifest、size、expires 和 one-time receipt；Decision 按 domain 保存
  `erase|anonymize|retain|not_found`、固定 legal code、hold/retention generation 和 executor receipt。
- DSR job 使用 owner adapter registry、持久 checkpoint、lease fencing 和幂等 source identity；事件只含请求状态摘要。

Down 在任一 consent、preference、DSR、export、decision、exception 或事件存在时拒绝。

## 3. 授权、导出与页面

Identity API 同时恢复当前 global session 与 Workspace Session，隐私请求 owner 必须与二者及 Membership 一致；
敏感动作要求近期 MFA/step-up，不能传 account_id/workspace_id 选择他人或其他 Workspace。
Platform privacy queue 使用独立 Platform Session 和 `platform_privacy` 权限，只返回 pseudonymous request code、deadline、
domain progress 和 fixed legal outcome。Workspace owner 只从协调命令处理本空间 Membership，不可读取完整数据图。

导出适配器使用显式字段白名单，排除其他账号、其他 Workspace 客户数据、Provider Key、Token、Prompt/Response、
平台内部备注和安全 Secret。Identity 页面显示同意、请求类型、状态、分域进度、例外解释和一次性下载；Platform 页面
显示工作队列和审查动作。响应 no-store，下载后清除 URL/密钥，迟到响应不恢复已清除值。

## 4. 第一轮自审：主体与跨 Workspace 边界

- P0：Workspace owner 代表成员删除全局账号会跨越主体权利。修正为本人全局身份验证，企业只移除 Membership。
- P0：导出按 email 查询会合并同名/旧地址并泄漏其他主体。修正为稳定 global account ID 和 owner-qualified links。
- P0：platform_privacy 队列返回完整身份和正文会扩大平台可见性。修正为 pseudonymous code、最小进度和按需短授权。
- P1：同意只保存当前布尔值无法证明条款版本。修正为追加 ConsentRecord + 当前 projection。

## 5. 第二轮自审：保全、删除与恢复

- P0：删除先执行后检查 Legal Hold 无法恢复。修正为先生成 owner decision，并在每个原子执行前重验 hold/retention generation。
- P0：一个 owner worker 失败导致重跑已删除域。修正为 per-owner receipt、稳定 request identity 和 checkpoint resume。
- P1：导出完成后长期有效 URL 会泄漏。修正为加密制品、短 TTL、单次 receipt、no-store 和定时销毁。
- P1：请求取消覆盖已提交删除事实会伪造回退。修正为取消只阻止未开始 owner，已执行决定不可变记录。

两轮均为修正后通过，无未关闭 P0/P1 设计问题。

## 6. 专项验证计划与批次边界

0125/0126 往返/down guard；本人/匿名/他人/Workspace admin/Platform 负矩阵；同意版本与 CAS；跨 Workspace 数据图；
export 字段、大小、加密、一次下载和超时；erase/anonymize/retain、Legal Hold、retention generation、部分拒绝；
worker 崩溃/接管/取消/迟到响应；法定 deadline 告警、p95；Identity/Platform 三浏览器三缩放；敏感审计与静止终态。
完整 G6 留到 PRD-138 后统一执行。
