# PRD-111 多区域高可用、备份与灾难恢复

状态修订（2026-09-10）：原“已完成并通过专项验证（2026-08-24）”结论被实际执行链审查否定。
当前 handler 仅检查备份标识，未实际恢复；整体完成与 RPO/RTO 均未证实。正在按
`S3-actual-restore-execution-20260910.md` 补齐，历史设计与验收记录保留，不作为真实容灾通过证据。
依赖 PRD-106..110，范围以 Excel PRD-111 为准。

## Excel 权威合同

以下名称与目标值按 Excel 原文冻结，内部 SQL 表名可以不同，但公开 DTO、测试映射与交付证据必须逐项对应：

- API：`GET /platform/dr/status`、`POST /platform/dr/drills`、`POST /platform/regions/{id}/failovers`、
  `GET /platform/backups`。
- 核心数据对象：`Region`、`FailoverPlan`、`DisasterRecoveryRun`、`RecoveryPoint`、`RestoreEvidence`。
- 事件：`dr.drill_started`、`region.failover_started`、`region.failover_completed`、`dr.objective_missed`。
- 性能/SLO：`Control RPO<=15分钟/RTO<=2小时`、`Gateway区域切换RTO<=10分钟`。

## 1. 目标与边界

交付 Control 多可用区/区域故障切换合同、数据库恢复、对象副本、KMS 恢复、Gateway 路由切换、数据驻留硬约束、
灾备演练和恢复证据。自动化只能执行已审批、预定义且可围栏的计划；不接受任意基础设施命令、脚本、地址或凭据。
本条不改变 PRD-117 的租户驻留策略产品模型，只消费当前权威 region/placement 约束。

## 2. 0103 数据模型

- `platform_regions`：稳定 code、residency group、status、failover epoch、revision；不保存云账号/内部 endpoint。
- `failover_plans`：source/target、允许 residency、组件清单、RPO/RTO、precheck/stop 条件、签名 manifest 和不可变版本。
- `failover_approvals`：两名不同 operator；至少一名 ops、一名 security/owner，执行时重验权限和计划 hash。
- `disaster_recovery_runs`：`planned|prechecking|approved|running|verifying|succeeded|failed|aborted|rollback_required`，
  绑定 PRD-106 job、fencing epoch、request/trace 和 revision。
- `recovery_points`：数据库/object/config/outbox/ledger 各水位、manifest hash、KMS key version 和 created_at；无对象路径/Secret。
- `restore_evidence`：run/component、before/after watermark、integrity/hash、RPO/RTO outcome；Workspace 投影必须复合 owner。
- 权限 `platform.dr.read/execute/approve/security_review`：owner 全部，ops 读/执行，security 读/审批；其他角色拒绝。
- down 对 region epoch、plan/run/recovery/evidence 或 active lease 事实拒绝。

## 3. 围栏、驻留与恢复协议

Failover 先冻结计划版本并验证：目标 region 健康、residency group 相同、恢复点/KMS 可用、配置 head 与 outbox/ledger 水位
可证明。执行通过数据库单调 `failover_epoch` 和 region writer lease 围栏；旧 region 无法续租或提交新 head，检测双 writer
立即 abort，禁止“最后写入者获胜”。

PRD-106 job checkpoint 固定为 precheck、quiesce、restore、route、verify、complete；每步幂等且可从权威水位恢复。
网络/云 adapter 调用不持有 DB 事务。回切创建新 run/epoch，不逆向修改旧 run。

数据驻留为硬约束：没有同 residency 目标时自动切换不得发生，状态保持 degraded 并升级人工处置。备份 envelope 加密，
KMS 恢复只验证 key version/解密哨兵，不输出 key。恢复后必须核对数据库完整性、账本守恒、outbox 无丢失、PRD-108
配置 generation/hash 和 Gateway placement。

## 4. API 与事件

- `GET /platform/dr/status`：region、计划 readiness、最近演练和 RPO/RTO 聚合；不返回内部拓扑/路径。
- `POST /platform/dr/drills`：选择预定义 plan/version，CSRF/idempotency/双审批；演练目标隔离且禁止指向生产 writer。
- `POST /platform/regions/{id}/failovers`：高风险命令，只接受 plan/version/reason/expected epoch。
- `GET /platform/backups`：复用 PRD-100 备份事实的安全 recovery-point 投影。
- 事件 `dr.drill_started|region.failover_started|region.failover_completed|dr.objective_missed` 只含 plan/run/region code、
  objective/outcome 和 request/trace。

## 5. Console 与验收

Platform `disaster-recovery` 页面显示 region 状态、计划版本、审批、run checkpoint、恢复点和目标达成；执行对话框明确
source/target/residency/stop 条件，busy 状态不能关闭或重复提交。security 只有审批，ops 不能伪造第二票。

专项覆盖单区故障、DB/object/KMS 恢复、旧 writer fencing、脑裂、驻留拒绝、checkpoint 接管、回切、账本/outbox/config
水位、权限漂移、迁移往返和浏览器。门禁：Control RPO<=15 分钟、RTO<=2 小时；Gateway region RTO<=10 分钟。

## 6. 第一轮自审：脑裂与驻留

- P0：DNS 切换而无 writer epoch 会产生脑裂。修正为数据库单调 epoch、writer lease 和每次提交 fence。
- P0：自动选择最近 region 可能违反驻留。修正为 residency group 硬过滤；无合规目标则不切换。
- P0：API 接收脚本/endpoint 会形成基础设施注入。修正为签名预定义 plan 和 adapter allowlist。
- P1：旧 region 迟到成功覆盖新状态。修正为 run/epoch/generation CAS，迟到结果只记录 stale outcome。
- P1：恢复证据包含对象路径/KMS 标识会泄漏基础设施。修正为稳定 public code、hash 和 key version。

## 7. 第二轮自审：恢复完整性与审批

- P0：只验证数据库健康不足以证明财务/配置一致。修正为账本守恒、outbox、snapshot head 和 placement 联合水位。
- P0：同一 ops 双击执行可绕过双人控制。修正为不同 operator、不同角色类别且执行时重验。
- P1：长事务包住云恢复会阻塞控制面。修正为 PRD-106 分阶段 checkpoint 与短事务。
- P1：失败自动回切可能二次破坏。修正为 stop 条件进入 `rollback_required`，回切是新审批 run。
- P1：down 删除 recovery evidence 会失去季度证明。修正为任何 DR 事实存在即拒绝回退。

## 8. 批次交叉审查

- 第一轮（范围/owner/权限）：Region/plan/run 为平台事实，Workspace 状态仅是复合 owner 投影；ops 执行与 security/owner 审批
  角色分离，API 只接受签名预定义 plan，不接受脚本、endpoint、地址或凭据。
- 第二轮（迁移/依赖/失败策略）：0103 对 epoch/plan/run/recovery/evidence/active lease 执行 down guard；单调 writer epoch 阻断
  脑裂，residency 不兼容时保持 degraded 而不隐式跨区；DR job 按 checkpoint 恢复，失败回切必须新建审批 run。
