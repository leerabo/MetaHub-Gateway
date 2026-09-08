# PRD-135 等保、个保与企业审计证据中心

状态：已完成。依赖 PRD-034/058/081/109/117/134；范围以 Excel PRD-135 为准。

## Excel 权威合同

- API：`GET /platform/compliance/controls`、`POST /platform/compliance/evidence`、
  `GET /workspaces/{id}/compliance-reports`、`POST /platform/compliance/exceptions`。
- 对象：`ComplianceControl`、`ControlMapping`、`ComplianceEvidence`、`ComplianceException`、
  `RemediationTask`、`ComplianceReport`。
- 事件：`compliance.evidence_expiring`、`compliance.control_failed`、
  `compliance.exception_approved`。
- SLO：报告生成 < 10 分钟；过期检测 < 24 小时；关键控制失败 < 5 分钟告警。

## 1. 产品闭环

platform_compliance 维护版本化控制项库和等保、个保、数据安全法映射，指定 owner、适用范围、证据类型、采集器、
有效期和验收规则。受信采集器提交 digest-bound 结构化证据，Control 验证来源、schema、owner、时间和 freshness，
计算 fixed outcome；失败自动创建 remediation，例外经不同账号双人审批并到期。

Workspace security_admin/auditor 只读取本空间裁剪报告和整改状态；报告明确区分 `implemented|partially_implemented|
not_implemented|not_applicable|exception_active|evidence_stale`，不把控制证据等同认证证书，也不声明未取得的认证。
敏感证据正文保留在分级对象域，普通报告只含摘要、hash、有效期和可解释结论。

## 2. 0127 数据与证据状态

- ComplianceControl/ControlMapping 为平台全局不可变版本，保存 framework/code/version、responsibility、required
  evidence kinds 和 evaluation rule digest；新版本不覆盖历史报告。
- ComplianceEvidence、ComplianceException、RemediationTask 和 ComplianceReport 全部带 Workspace/tenant 复合 owner；
  Evidence 保存 control version、collector identity/version、artifact digest、classification、captured/valid-until、
  schema/outcome 和 immutable receipt。平台级控制证据也必须关联专用受控 Workspace，不能创建无 owner 证据。
- ComplianceException 绑定 control/evidence scope、fixed reason、compensating controls、requester/approver、
  starts/expires、review generation；申请人不能自批。
- RemediationTask 状态为 `open|in_progress|blocked|verified|closed`，绑定失败 control generation 和 owner；关闭需新证据。
- ComplianceReport 绑定 Workspace、control set version、evidence high-watermark、生成器版本、summary digest 和 expiry；
  证据变化使旧报告 stale，不原地改写。

Down 在任一非种子控制版本、证据、例外、整改、报告或事件存在时拒绝。

## 3. 授权、采集与 Console

platform_compliance 管理控制、证据和报告；platform_security 可读安全控制摘要但不能自批自身例外；Workspace
security_admin/auditor 只读本空间报告。跨 Workspace 同形 404；平台证据与租户证据分域，SupportGrant 不自动授予
高分类证据正文。采集器使用 machine identity、固定 audience/scope、签名 envelope 和 replay guard，禁止任意文件路径、
URL、命令或自由 JSON。

Platform Console 展示控制覆盖、freshness、失败、例外、整改和证据来源；Workspace Console 展示本空间报告、差距和
恢复动作。认证/法规文案来自版本化受审模板，页面不得把 `control_passed` 渲染为“已认证”。所有下载短 TTL、no-store、
逐次审计，离页清理对象地址。

## 4. 第一轮自审：证据可信度与措辞

- P0：人工上传 PDF 即将控制标为通过无法证明来源。修正为受信 collector identity、schema、digest、freshness 和 review。
- P0：控制通过自动显示“已通过等保认证”会产生虚假合规声明。修正为事实 outcome 与认证状态分离，认证只能引用
  独立已验证证书事实。
- P0：平台报告 join 全租户证据会泄漏客户安全状态。修正为 owner-qualified projection 和 Workspace-specific report。
- P1：高分类证据 URL 出现在报告会绕过授权。修正为摘要引用，正文按分类短授权下载。

## 5. 第二轮自审：时效、例外与整改

- P0：进程内过期 timer 重启后不告警。修正为持久 deadline/watermark、lease fencing 和幂等 catch-up。
- P0：例外只绑定 control code 会放行后续更严格版本。修正为 control version、scope、evidence generation 和 expiry 全绑定。
- P1：新证据覆盖旧证据会失去审计链。修正为追加 evidence，current projection 只指向最新有效 generation。
- P1：整改关闭不要求新验证会伪造恢复。修正为 verified 状态必须引用更新后通过的 evidence receipt。

两轮均为修正后通过，无未关闭 P0/P1 设计问题。

## 6. 专项验证计划与批次边界

0126/0127 往返/down guard；控制版本/映射、采集器签名/replay、证据分类/owner/freshness；虚假认证文案负测试；
跨租户报告；例外非自批/到期/撤权；整改闭环；collector/DB/object store/outbox 故障与恢复；10 分钟报告、24 小时过期、
5 分钟告警；Platform/Workspace 三浏览器三缩放；敏感扫描和静止终态。完整 G6 留到 PRD-138 后。

## 7. 实施结论

0127 已交付版本化控制与法规映射、owner-qualified 证据/例外/整改/报告、不可变事件与 outbox、重放保护、
持久 scheduler lease/fencing，以及 `compliance_collector` 机器身份模板和 `compliance.collect` 封闭 scope。
Control API 只接受受信机器身份提交的签名结构化 envelope，不接收任意路径、URL、命令或自由 JSON；
Workspace 与 Platform Console 只显示控制事实、证据 freshness、差距、例外和整改，不生成外部认证声明。

第一轮实施自审复核 owner、证据来源、字段投影和措辞边界。迁移同时扩展 machine audit 闭集、动态权限目录和
tenant deletion manifest；报告 DTO 排除证据正文、对象地址、本地路径和 collector 密钥材料；Platform/Workspace
查询均重新验证复合 owner 和当前权限。修正后无未关闭 P0/P1。

第二轮实施自审复核 replay、幂等、例外非自批、freshness、scheduler 接管与历史热路径。证据 envelope 将
route、owner、control version、collector identity/version、artifact digest、captured/valid-until 和 outcome
共同纳入签名与 replay guard；失败自动生成整改，例外绑定固定 generation 并要求不同账号审批。复核发现将
所有机器路由切到完整扩展权限快照会改变既有 provider/observability 热路径，已恢复其轻量授权探针，仅
`compliance.collect` 使用完整扩展权限校验。修正后无未关闭 P0/P1，两轮实施自审均通过。

专项结果、敏感审计与批次门禁边界见 `docs/testing/PRD-135-tests.md`；下一权威项为 PRD-136。

## 8. 2026-09-07 合批原生验证设计

与 PRD-134/136/137 合批复用基线与运行时构造，保留受信 collector、机器 OAuth、证据 envelope、
报告 owner、双人例外和 freshness 调度全流程。fixture 核验所需 0127 和迁移幂等，不固定旧 head。
第一轮自审：不能用伪造机器身份替代已有真实 OAuth 用例；签名、scope、replay 与 owner 校验保持
原样，合规 outcome 不能扩大成外部认证声明。
第二轮自审：同一证据重放、不同证据 generation、例外独立审批与过期必须在真实数据库中运行；
失败和重试分别保留，不把发布/隔离安全的测试结果混入本条。独立记录、审计与提交。

原生实测修复：机器认证的 SQL `grant` 别名改为 `token_grant`；例外水位从 SQLite `rowid`
改为当前 owner 的 `SUM(revision)`，批准和过期均递增、不可删除约束保持；时间比较统一为入库毫秒精度。
第一轮复核保持真实 OAuth、动态权限、凭据轮换及 IP/mTLS 约束原样；第二轮验证不同 owner 不改变
本空间水位，新增/批准/过期逐次失效，使用 WITHOUT ROWID 表检测物理行号依赖不会复发。

## 9. 2026-09-08 调度异常清理

第一轮自审：完整注册调度下的外层 lease fence 可在 _release_lease 的 commit 抛错；原函数
吞掉异常后跳过 close，SQLite 事务与写锁会残留。独立副本已记录该函数持锁超过 8 秒。将连接
归还置于 finally，异常时先尽力 rollback，不绕过 fence，也不把释放失败写成释放成功。

第二轮自审：连接创建失败必须仍为 best-effort；rollback 自身失败也不能跳过 close。使用
真实 SQLite 事务和生产 database_commit_guard 制造提交拒绝，检查句柄关闭、业务行不变与
第二连接可立即取得写锁；正常 CAS 释放及 owner 不匹配仍按原逻辑运行。独立归属本 PRD。
