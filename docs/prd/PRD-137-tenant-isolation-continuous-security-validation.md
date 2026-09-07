# PRD-137 租户隔离持续验证与安全攻防

状态：补充修复中。依赖 PRD-090..136；范围以 Excel PRD-137 为准。0129、
finding/remediation/risk acceptance 编排与 Platform Console 已实现，但 2026-09-07 源码复核确认
正式 app 未接入真实隔离 runner，创建/清理回执也未来自实际执行，不能视为完整安全验证已交付。

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

## 7. 2026-09-07 合批修复设计与未完成项

与 PRD-134..136 合批修复 schema 查询和 CAS，保留所有 lease、generation、当前权限及双人审批
判断。现有业务测试在原生数据库运行时仍使用受控 runner，验证的是编排行为，不是攻击执行。
第一轮自审：真实执行器必须使用固定场景和隔离主体，绑定待测 artifact/code/migration/contract，
不能接收请求提供的 URL、SQL、命令或生产客户 selector。缺失、失败或不可信结果仍失败关闭。
第二轮自审：当前 `_persist_execution` 自行生成 environment/seed/cleanup 摘要并写 deleted，不能
证明创建或清理确实发生。必须补齐来自受信执行器的实际回执和验证，不能仅让十个 outcome 恒真，
也不能把自动生成的 SHA256 当作签名、环境隔离或清理证明。
该缺口与原生数据库兼容均属于本批必须完成的工作。兼容测试通过后仍不得把本条标记全部完成，
更不得用它解除 G6/G8。历史受控合同测试保留，但原文“固定隔离 runner 已交付”结论已撤回。

## 8. 受信执行回执修复

服务只接受 `metahub.assurance-execution.v1` 签名 envelope。部署配置
`METAHUB_ASSURANCE_RECEIPT_TRUST_BUNDLE_PATH` 提供公钥、有效期、runner 镜像和隔离策略摘要，
每次结果提交重新读取；请求不能自带公钥。签名用途独立，绑定 assessment/owner 摘要、profile、
code/migration/contract/artifact、generation、随机租约 challenge、执行期限和完整场景 registry。
传给执行器的 payload 不包含生产 Workspace/tenant 标识，不允许以报告 owner 选择客户目标。

第一轮复核：裸 outcome、签名篡改、不同制品/owner/租约重放、已撤销公钥、未知字段、重复 JSON、
超大内容、场景乱序/缺项均拒绝。镜像与隔离策略必须同时命中部署信任，不接受响应自授信任。
第二轮复核：时间区间和 duration 一致；source/target 不同，fixture manifest 与场景资源类别逐项
一致，创建不晚于执行、清理不早于执行结束。清理失败生成 error/P0；未验签的结果不写 cleanup
journal，environment receipt 为 NULL，旧 DTO 必填摘要使用全零表示“未取得”，绝不代表实际回执。

仍未完成：真正的固定场景隔离 worker、制品与运行环境的实际度量、完整原始签名回执持久化、每阶段
短租约/撤权检查、真实 fixture 创建与 exact cleanup。当前测试里的签名和 fixture 是明确标注的
编排测试数据，不构成实际安全通过或独立审批。因此本条仍为补充修复中，G6/G8 未解除。

## 9. 真实接口探针实施设计

固定探针执行真实 GlobalAccount/WorkspaceSession 认证的 HTTP 合同。第一组接入 projects/applications
跨空间读取与写入、Membership 动态降权、旧 WorkspaceSession 重放和严格输入拒绝。每个拒绝用例
都先验证同一资源的合法访问，防止目标整体故障被误判为隔离成功；外部目标和资源标识不来自评估请求。
租约在每次请求前后检查，撤权后立即停止发送新请求；输出只包含固定检查码、HTTP 状态和摘要。

第一轮自审：不得用认证替身替代真实注册、验证码消费、登录和切换；隔离邮箱的本地投递只负责测试
账号开通，不能冒充外部身份认证或审批。固定路径中的 fixture ID 必须验证，禁止 URL/SQL/shell 输入。
第二轮自审：未实现的 cache/queue/object/metrics/late-response 场景必须返回 error；合法读取对照、
异空间写后原值验证、旧会话明确重放、降权后重新签发 WorkspaceSession 仍无写权限都必须有实际响应证明。
本阶段验证探针功能，不代表 OS/网络隔离部署、镜像度量或完整 10 场景已交付，后续仍接入同一执行链路。

2026-09-07 实施复核：探针拒绝重定向、不同来源客户端、Authorization 混用及非法 fixture ID。
每次请求与响应分块前后检查租约，整体 deadline 和单请求 10 秒墙钟上限约束挂起调用；撤销、超时或
取消后实例永久 fencing。响应上限 256 KiB，拒绝压缩后再读正文，缺失/重复资源及无效 JSON 不得穿透为
成功结果。检查记录只保留固定代码、状态和布尔结论，证据摘要不包含凭据或原始响应。

第二轮实施复核补入可观察的合法改名及回读，避免空操作 200 被当作真实可写证明；跨空间拒绝和成员
降权拒绝后均验证完整项目投影未变。真实权限中 developer 不具备 project.write，因此使用临时 admin
降为 viewer；严格输入按已存在的 422/identity.request_invalid 合同判断，不为探针修改服务端权限。
当前真实注册/验证码/登录/邀请/空间创建与切换由集成测试提供，未做鉴权替换；ASGI 进程内运行不证明
TLS、容器/VM 或网络隔离。租约回调的持久化协调、剩余五场景及全 owner registry 仍待实现。
