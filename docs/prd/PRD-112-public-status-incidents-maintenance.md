# PRD-112 公共状态页、事故与维护沟通

状态：实现与专项验证完成（2026-08-25）。依赖 PRD-105/109/111，范围以 Excel PRD-112 为准。

## Excel 权威合同

以下名称与目标值按 Excel 原文冻结，内部 SQL 表名可以不同，但公开 DTO、测试映射与交付证据必须逐项对应：

- API：`GET /public/status`、`GET/POST /status/subscriptions`、`GET /incidents`、`POST /platform/incidents`、
  `POST /platform/maintenance-windows`。
- 核心数据对象：`StatusComponent`、`Incident`、`IncidentUpdate`、`MaintenanceWindow`、`StatusSubscription`、`Postmortem`。
- 事件：`incident.created`、`incident.updated`、`incident.resolved`、`maintenance.scheduled`。
- 性能/SLO：`公共状态页可用性99.99%`、`p95<500ms`、`事故确认后10分钟内首条更新`。

## 1. 目标与边界

交付独立公共状态读取、订阅验证/退订、事故时间线、影响组件/区域、维护窗口和复盘发布。公共内容不得出现租户名、
Workspace/账号标识、内部拓扑、攻击细节、Provider Secret、Prompt/Response 或客户正文。租户私有事故只向当前授权成员
展示。本条不把平台内部告警自动原样公开，也不建设通用客服工单。

## 2. 独立读取面与 0104 数据

公共 `GET /public/status` 由独立 `status-page` 只读进程或对象存储静态入口提供，消费签名发布 bundle；不得实时依赖
Control 数据库。Control 不可用时继续返回最后一个未过期签名 bundle，并明确更新时间。订阅写入和平台发布仍由 Control
负责，故障时安全失败，不影响公共读取。

- `status_components`：公开 code/name、group、current state、display order 和 revision；无内部 service/endpoint。
- `incidents`：`draft|pending_security_review|published|monitoring|resolved|postmortem_published`、severity、scope、revision。
- `incident_updates`：不可变公开文案、状态、影响 component/region public code、published_at 和 operator。
- `maintenance_windows`：planned/in_progress/completed/cancelled、公开 scope、时间和 revision；维护不写成 incident。
- `status_subscriptions`：channel、加密联系地址、verification/unsubscribe token hash、public scope、状态和 expiry。
- `postmortems`：结构化公开摘要、时间线、原因类别、纠正项状态；禁止内部攻击/租户材料。
- `status_publication_bundles`：canonical JSON hash、签名/key version、generation、published_at/expiry 和对象存储 version。
- 权限 `platform.status.read/write/security_approve`：ops 发布，security 审批安全事故，owner 全部；公共读无需登录。
- down 对 published bundle、incident/update/subscription/postmortem 事实拒绝。

## 3. 发布、订阅与隔离

事故从 PRD-109 Alert/PRD-111 Run 只读取固定聚合，不自动复制内部文本。普通事故由 ops 发布；安全分类需 security
审批且发布者不能自批。每次更新生成新 bundle，KMS 签名后原子切换对象版本；旧 bundle 保留审计与回退。

状态订阅先创建 pending 记录并通过 PRD-105 通知发送一次性验证链接；验证后才接收更新。退订 token 单次、hash 保存、
不要求登录且返回同形结果。Workspace 私有状态查询使用 WorkspaceSession owner，公共 bundle 永不包含私有 incident。

通知模板只接收 component/severity/public update/time allowlist；联系地址始终加密，不进入事件、审计、DOM 或验收证据。

## 4. API 与页面

- `GET /public/status`：no-store 或短 CDN cache、签名 generation、组件/事故/维护公开投影；错误也不泄漏内部依赖。
- `GET/POST /status/subscriptions`：当前订阅查看需会话；公开 create/verify/unsubscribe 使用独立受签 intent 和同形响应。
- `GET /incidents`：WorkspaceSession 返回公共 + 当前 workspace 私有授权投影，无 tenant query。
- `POST /platform/incidents|maintenance-windows`：PlatformSession、CSRF、idempotency、revision 和 closed DTO。
- 事件 `incident.created|updated|resolved|maintenance.scheduled` 从同事务 outbox 恢复，按公开/私有 scope 分离。

交付 `status.html` 公共状态页和 Platform `status-incidents` 工作台。公共页首屏直接显示整体状态、组件、当前事故与维护，
不做营销 Hero；支持键盘、读屏、390/1280/1440，无页面级横向溢出。

## 5. 测试与 SLO

专项覆盖 Control 停服公共读、bundle 签名/过期、事故审批、维护区分、私有 scope、订阅验证/退订/重放、通知失败恢复、
动态撤权、跨租户、对象存储/KMS 故障、迁移往返和敏感扫描。门禁：公共状态页 99.99%、p95<500ms；事故确认后
10 分钟内首条更新。

## 6. 第一轮自审：独立性与公开投影

- P0：把 `/public/status` 放在 Control 进程无法满足 Control 故障仍可用。修正为独立只读进程/静态 bundle。
- P0：内部 Alert 文本自动公开会泄露租户和攻击细节。修正为结构化人工发布 DTO 和安全审批。
- P0：公共 bundle 混入私有 incident 会跨租户泄漏。修正为物理独立 public projection，私有查询只走 WorkspaceSession。
- P1：对象存储更新非原子会返回半 bundle。修正为写新版本、验签、原子切换 generation。
- P1：内部 component code 暴露拓扑。修正为独立 public code/name 目录。

## 7. 第二轮自审：订阅、维护与恢复

- P0：未验证地址直接订阅会被用于骚扰。修正为 pending + 一次性验证 intent。
- P1：退订要求登录会阻断收件人。修正为高熵一次性 token hash 和同形公开响应。
- P1：维护窗口自动生成 incident 会污染可用性事实。修正为独立状态机和显示语义。
- P1：KMS/对象存储失败时覆盖旧 bundle 会让状态页空白。修正为只在新 bundle 完整验签后切 head。
- P1：发布者自批安全事故。修正为不同 operator 的 security approval，执行时重验权限。

## 8. 批次交叉审查

- 第一轮（范围/owner/权限）：公共 projection 与 Workspace 私有 incident 物理分离；公共读取无会话，私有读取以当前
  WorkspaceSession 复合 owner 授权，内部 Alert/DR 文本不会自动进入公共内容。
- 第二轮（迁移/依赖/失败策略）：0104 对 published bundle/incident/update/subscription/postmortem 执行 down guard；独立读取面
  只消费原子切换的签名 bundle，Control/KMS/对象存储发布故障保留最后有效版本，不使公共读随 Control 一起中断。

## 9. 实施证据

- 数据与回滚：`0104_public_status_incidents.sql` / `.down.sql` 已交付组件、事故、更新、维护、订阅、复盘、发布 bundle、命令和 outbox；迁移往返、fresh schema、事实 down guard、完整性及外键专项通过。
- 独立读取：`metahub_status` 只读容器仅消费只读 volume 中的 canonical 签名 envelope，不继承 Control 共享 env、数据库或认证配置，并严格验证 generation、摘要、签名、expiry 和公开 DTO；Control 数据库不可用不影响已缓存未过期版本。
- 可用性续签：Control 启动后幂等生成 `system` 初始全正常 bundle；未临期不重发，TTL 剩余三分之一时生成新 generation。人工事故或维护发布记录为 `operator`，系统续签不会伪装成人工操作。
- 发布恢复：新对象写入、fsync 和验签完成后才原子替换 `head.json`；失败保留旧 head。业务事实先提交，调用方以相同幂等键重放后只补发布，不重复 incident/maintenance/update。
- API 与隐私：Control 路由实施 Platform/Workspace Cookie 域、CSRF、closed DTO、revision CAS、幂等键、动态权限复核、复合 owner 与公开/私有投影分离；联系人加密、blind index 和 hash-only token 已专项验证。
- 页面：已交付 `status.html` 公共页及 Platform `status-incidents` 工作台，包含整体状态、组件、事故/更新/复盘、维护、订阅以及权限裁剪；390/1280/1440 响应式合同和无浏览器持久存储由前端专项覆盖。
- 结果：PRD-112 Migration `3 passed`，API/发布 `7 passed`，Deployment Compose/脚本 `9 passed`，Console PRD-109..112 `21 passed`，Console lint `351 files`，Ruff 与 compileall 通过；详见 `docs/testing/PRD-112-tests.md`。Docker CLI 在当前主机不可用，Compose `config --quiet` 未执行并如实记录，批次级浏览器/性能/安全门禁保留到 PRD-113 后统一执行。
