# PRD-137 租户隔离持续验证与安全攻防

状态：设计冻结，尚未实现。依赖 PRD-090..136；范围以 Excel PRD-137 为准。

## Excel 权威合同

- API：`POST /platform/security/assessments`、
  `GET /platform/security/assessments/{id}`、
  `POST /platform/security/findings/{id}/remediations`。
- 对象：`SecurityAssessment`、`IsolationTestRun`、`PenTestFinding`、`Remediation`、
  `RiskAcceptance`。
- 事件：`security.assessment_failed`、`security.finding_created`、`security.finding_closed`。
- SLO：每次发布增量隔离门禁 < 30 分钟；季度完整攻防；P0 修复 < 24 小时。

## 1. 产品闭环

platform_security 选择签名、版本化 assessment profile，系统在隔离环境创建专用 tenant/workspace/account、Platform
和 Edge 测试主体，按 owner registry 验证 IDOR、写越权、缓存污染、队列 owner、对象前缀、指标标签、动态撤权、
重放、迟到响应和模糊输入。runner 只执行固定 scenario code 和 target kind，不能接收 URL、脚本、SQL、shell、
任意 header 或生产客户 selector。

失败生成不可变 finding，绑定受影响 owner/contract、最小复现 code、severity、evidence digest 和 remediation SLA；
修复通过同一 profile 新 generation 复验后关闭。与租户有关的通知只含本空间安全摘要；风险接受需要 Workspace
owner + security_admin 双批及 platform_security review，P0/P1 默认不能用于 G6 放行。

## 2. 0129 评估、发现与隔离执行

- SecurityAssessment、IsolationTestRun、PenTestFinding、Remediation 和 RiskAcceptance 全部带隔离目标的
  `workspace_id/tenant_id` 复合 owner。Assessment 保存 profile/version/signature、code/migration/contract/artifact
  digest、scope、status、requester、environment receipt 和 summary digest；平台是发起主体而不是业务 owner，
  禁止无 Workspace/tenant 的全局扫描。
- IsolationTestRun 绑定 assessment、scenario code、source/target subject matrix、owner registry version、runner image、
  seed/cleanup receipt、fixed outcome、duration 和 evidence digest；不保存凭据、请求正文或原始响应。
- PenTestFinding 绑定 assessment/run、owner kind、contract code、severity、fixed weakness code、status、SLA deadline、
  first/last seen 和 immutable evidence reference。
- Remediation 追加保存 change digest、verification assessment、outcome 和 approver；RiskAcceptance 绑定 finding、scope、
  双 Workspace approver、platform reviewer、compensating controls 和 expiry。
- runner lease、scenario receipt、cleanup journal 和 event/outbox 持久化；清理测试 fixture 不删除 assessment/finding。

Down 在任一 assessment、run、finding、remediation、acceptance、runner 水位或事件存在时拒绝。

## 3. 运行隔离、权限与 Console

platform_security 只能从 registry 发起；runner machine identity 只访问隔离环境 public contracts 和受控 fault injector，
无生产数据库、对象存储、KMS、Provider 或迁移管理权限。测试 tenant 使用显式 marker 和唯一 owner，创建前后验证目标环境，
cleanup 只删除 exact manifest 内 fixture。外部审计员只读经裁剪证据，不获得执行 token。

Platform Console 展示 profile、owner coverage、run、finding、SLA、remediation 和清理 receipt；Workspace 只展示本空间
通知与风险接受动作。POST 使用 step-up、Idempotency-Key、If-Match、no-store；动态撤权后 runner 不获得新 lease，
已租约在短 deadline 后 fencing。页面不渲染 exploit payload、凭据、内部 endpoint 或客户标识。

## 4. 第一轮自审：测试平台本身的攻击面

- P0：允许任意脚本/URL 会把安全评估变成远程执行与 SSRF 平台。修正为签名 scenario registry 和固定参数上限。
- P0：runner 使用生产平台角色会读真实客户数据。修正为隔离 machine identity、test owner manifest 和网络/资源边界。
- P0：只测列表 API 会漏队列、缓存、对象和指标侧信道。修正为版本化 owner registry 覆盖同步/异步/存储/观测面。
- P1：finding 保存原始 exploit/response 会泄漏 Secret。修正为 weakness code、hash、固定摘要和受控外部证据。

## 5. 第二轮自审：撤权、清理与证据保留

- P0：权限撤销后已有长 lease 继续攻击。修正为短 lease、每阶段恢复 permission revision、deadline 和 fencing token。
- P0：cleanup 按前缀删除可能触碰生产/其他测试数据。修正为 exact fixture manifest、owner marker、环境证明和逐项删除 receipt。
- P1：清理 fixture 同时删除 finding 会抹去失败证据。修正为运行数据和持久 assurance facts 分库/owner 生命周期。
- P1：修复者直接关闭 finding 会跳过复验。修正为只有新 assessment generation 的同 profile 通过才能关闭。

两轮均为修正后通过，无未关闭 P0/P1 设计问题。

## 6. 专项验证计划与批次边界

0128/0129 往返/down guard；scenario 签名、任意输入/生产 selector 拒绝；全 owner registry IDOR/写越权/缓存/队列/
对象/指标/动态撤权/重放/迟到响应；runner 故障/接管/fencing；finding/remediation/复验/双批接受；exact cleanup 和
静止终态；30 分钟增量门禁与 P0 SLA；Platform/Workspace 三浏览器三缩放；敏感证据审计。完整 G6 留到 PRD-138 后。
