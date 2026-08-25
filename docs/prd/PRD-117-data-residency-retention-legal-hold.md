# PRD-117 数据驻留、保留策略与法律保全

状态：已完成。设计、两轮自审、实现、迁移、专项与必要回归、真实浏览器验收和敏感审计均已通过。
依赖 PRD-089/102/106/111；范围以 Excel PRD-117 为准。

## Excel 权威合同

- API：`GET/PATCH /workspaces/{id}/data-governance`、`POST /workspaces/{id}/legal-holds`、
  `DELETE /workspaces/{id}/legal-holds/{hold_id}`、`GET /platform/data-residency/evidence`。
- 对象：`DataClassification`、`ResidencyPolicy`、`RetentionPolicy`、`LegalHold`、
  `DeletionSchedule`、`ResidencyEvidence`。
- 事件：`data.retention_changed`、`legal_hold.created`、`deletion.executed`、
  `residency.violation_detected`。
- SLO：策略判断 p95 < 10ms；到期删除调度偏差 < 24h；驻留违规检测 < 5 分钟。

## 1. 产品闭环

security_admin 为 Workspace 选择允许 residency group、数据分类和保留期；策略以签名版本快照分发给
Control、Gateway、PRD-106 job runtime、PRD-111 backup/restore 和 PRD-089 tenant-data scheduler。
legal_admin 对明确数据类别/对象范围创建 Legal Hold，hold 只冻结删除。到期 scheduler 生成 DeletionSchedule，
逐类执行并写可证明证据；platform_compliance 只读取低敏驻留/执行证据，不能读取客户正文或扩大访问。

## 2. 0109 数据与状态机

- `data_classifications`：平台内置 code、敏感级别、允许处理目的、默认 retention，版本化不可变目录。
- `residency_policies`：Workspace/tenant、allowed residency groups、primary group、cross-region mode、
  `draft|active|superseded`、revision/hash。
- `retention_policies`：Workspace + classification、retention days、deletion mode、backup grace、revision。
- `legal_holds`：Workspace、classification、受限 selector hash、reason code、legal actor、
  `active|released|expired`、revision；不存案件正文。
- `deletion_schedules`：policy/owner、due/checkpoint、batch size、hold fence、`queued|running|blocked|completed|failed`。
- `residency_evidence`：policy version、component/region/storage class、observed watermark、fixed outcome、hash/time。
- `data_governance_events/outbox`：exact schema，只含 owner ID、分类、版本、状态、计数和时间。

Down 在 active/superseded policy、hold、deletion schedule、evidence 或未投递事件存在时拒绝。内置分类仅在
没有业务引用时可撤销种子，不得静默清除 hold 或删除证明。

### 2.1 冻结表合同

- `workspace_governance_permission_extensions/workspace_governance_role_permissions`：独立承载
  Workspace Membership 职能角色权限，不把 `legal_admin/auditor` 映射后的 legacy `viewer` 当成授权来源。
  表不可变；WorkspaceSession 每次恢复 Membership 后合并权限并重算组合授权指纹。
- `platform_data_governance_permission_extensions/platform_data_governance_role_permissions`：只登记
  `platform.data_residency.read`，角色固定为 `platform_owner/platform_compliance`，由 PlatformSession 的动态
  authority union 消费。
- `data_classifications`：每个 `workspace_id + tenant_id` 固定六个内置 code：`prompt`、`response`、`file`、
  `audit`、`identity`、`billing`；记录 sensitivity、允许目的和默认保留天数。目录行不可变，新增 Workspace 在
  `workspace_regions` 建立后原子播种。
- `data_governance_fences`：每个 Workspace 一行，保存 `policy_generation`、`hold_generation`、
  `deletion_generation` 和 revision。策略、hold 与 destructive claim 均在 `BEGIN IMMEDIATE` 中 CAS 更新该行。
- `residency_policies`：版本行保存 canonical allowed group JSON、primary group、cross-region mode、有效期、
  generation/hash、actor 和 `active|superseded`。每 Workspace 只有一个 active；PATCH 新建版本并把旧版本转为
  superseded，不原地改写策略正文。
- `retention_policies`：版本行绑定 classification，保存 retention days、`hard_delete|archive_then_delete|anonymize`、
  backup grace、generation 和 `active|superseded`；同一次 PATCH 必须提供六类完整集合。
- `legal_holds`：只保存 classification、`classification|object_digest` scope、可选 selector SHA-256、固定 reason code、
  actor、fence generation、到期时间与 `active|released|expired`；没有案件名称、正文、对象 key 或原始 selector。
- `deletion_schedules`：绑定 retention policy 与 classification，保存 due、policy/hold generation、checkpoint、计数、
  manifest hash、`queued|running|blocked|completed|failed` 和 CAS revision。Job 输入只带 schedule/owner/generation。
- `residency_evidence`：只保存 component、region、storage class、policy generation、outcome、固定 violation code、
  watermark、manifest hash 和时间；Workspace owner 复合外键强制存在。
- `data_governance_commands`：保存 Workspace、action、幂等 HMAC、request hash、result object/revision/generation；
  同 key 不同请求返回固定冲突，不保存原始 key 或请求正文。
- `data_governance_events/data_governance_event_outbox`：事件 exact schema 只含 owner、对象 ID、分类、generation、
  固定状态/计数/hash、request/trace/time；事件事实不可变，outbox 只允许有界投递状态迁移。

0109 还给 `workspace_gateway_placements` 增加 active governance generation，给 Workspace `backup_sets` 增加
residency group/generation，并注册签名 artifact 中的 `data.retention.delete` job type。PRD-089 deletion manifest 将
PRD-117 表登记为 preserve；down 只有在不存在非种子事实、job、备份引用和新 generation 时才移除这些扩展。

## 3. 策略执行与分发

策略编译为 canonical JSON + SHA-256 + generation，由 PRD-108 签名快照分发；消费者验证 Workspace owner、
version、generation、expiry 和 hash。Gateway 路由、对象存储、数据库 Cell、job placement 和 backup target
必须落在 allowed residency group；不满足时失败关闭并产生固定违规类别。LKG 仅在未过期且 owner 完整匹配时
使用，不能用默认 region 代替损坏策略。

删除 scheduler 先检查 active Legal Hold、PRD-089 export/deletion/account closure、backup grace 和当前 policy
generation，再以 checkpoint 执行。hold 创建与删除 claim 争用同一 owner fence；任一并发只允许 hold 生效或
删除终态之一，不产生“hold 已创建但数据已删”的状态。

### 3.1 PRD-108 投影合同

`config_distribution._build_manifest()` 是唯一发布入口；PRD-117 不新增签名表或签名算法。manifest 增加：

```json
{
  "data_governance": {
    "schema_version": 1,
    "policy_id": "...",
    "generation": 1,
    "sha256": "...",
    "allowed_residency_groups": ["cn-main"],
    "primary_residency_group": "cn-main",
    "cross_region_mode": "same_group",
    "valid_until": "...",
    "retention": [
      {
        "classification_code": "prompt",
        "retention_days": 30,
        "deletion_mode": "hard_delete",
        "backup_grace_days": 7,
        "revision": 1
      }
    ]
  }
}
```

数组按 code/group 排序；source hash、envelope、Ed25519/KMS signature、delta、ack、LKG 均继续走 PRD-108。
策略 apply 与签名 snapshot publication 同一事务；KMS/签名/owner projection 失败时策略不提交。策略变化计入
revocation watermark，消费者只接受 owner 完整匹配、签名/hash 正确、未过 `valid_until` 的当前 generation。

### 3.2 消费者失败矩阵

| 消费者 | active fence | 允许动作 | 失败行为与证据 |
| --- | --- | --- | --- |
| Gateway placement | target region 的 residency group 在 allowed groups；请求后落 active generation | 同组 primary/failover | `platform_gateway.residency_conflict`，写 `blocked` evidence |
| Config/Gateway runtime | Workspace/tenant/generation/hash/expiry/签名一致 | 当前签名 snapshot；未过期 owner-bound LKG | 不用默认 region；损坏/过期失败关闭 |
| Workspace backup | target group allowed；retention 使用各分类最大 retention + backup grace | 保存 policy generation/group | `infrastructure.residency_conflict`，不创建 backup |
| DR restore/failover | 每个受影响 placement 的 generation 当前且 target group allowed | approved same-group route | `dr.residency_violation`，writer/placement 均不变 |
| PRD-106 job | `data.retention.delete` 输入 owner/policy/hold generation 与 schedule 一致 | checkpoint + idempotent effect | stale/hold 返回固定 blocked/retry outcome |
| PRD-089 tenant deletion | destructive claim 与 erase 前两次检查 active hold/fence | 无 hold 时继续 | hold 生效则不删除；进入 deleting 后新 hold 冲突 |

`residency.violation_detected` 只在事务内写低敏 evidence/event；恢复扫描按 `observed_at + component + owner`
水位补写，目标检测延迟小于五分钟。

## 4. 权限、API 与 Console

- `data_governance.read/manage`：owner/security_admin 管理，auditor 只读；`legal_hold.read/manage`：
  legal_admin 管理，security_admin 只读状态；角色不能互相代持。
- `platform.data_residency.read` 只给 platform_compliance/owner，返回 component/region/policy/outcome/watermark，
  不含 Prompt、Response、文件路径、对象 key、账号、Provider Key 或租户业务明细。
- PATCH 使用 If-Match、simulation 结果和 confirmed；跨区模式变更需要明确 reason，不能隐式迁移存量数据。
- Workspace Data Governance 页面显示驻留、分类、保留、hold 和删除终态；Platform 页面只显示合规证据。

### 4.1 角色矩阵

| Membership / Platform role | data_governance.read | data_governance.manage | legal_hold.read | legal_hold.manage | platform.data_residency.read |
| --- | --- | --- | --- | --- | --- |
| owner | 是 | 是 | 是 | 是 | - |
| security_admin | 是 | 是 | 是 | 否 | - |
| legal_admin | 是 | 否 | 是 | 是 | - |
| auditor | 是 | 否 | 是 | 否 | - |
| admin/developer/finance_admin/viewer/service_account | 否 | 否 | 否 | 否 | - |
| platform_owner/platform_compliance | - | - | - | - | 是 |

角色与权限必须同时满足；Platform、Support、Bearer、Global Cookie 不能调用 Workspace 治理 API，Workspace/Global/
Support Cookie 不能调用 evidence API。Membership revision、WorkspaceSession legacy matrix 和组合治理指纹逐请求恢复。

### 4.2 冻结 HTTP DTO

- `GET /workspaces/{id}/data-governance`：无 query/body；返回 active residency policy、六类 classification + active
  retention、当前 active/released hold 摘要、最近 deletion schedule 终态、fence revision、request/trace；ETag
  为 active policy revision，全部响应 `no-store, private`。
- `PATCH /workspaces/{id}/data-governance`：必须 `If-Match`、`Idempotency-Key`，body 仅允许
  `allowed_residency_groups[]`、`primary_residency_group`、`cross_region_mode`、六类完整 retention、固定
  `reason_code`、`confirmed`。`confirmed=false` 只返回 candidate hash、migration-required components 与下一
  generation，不写库；`confirmed=true` 重算同一 simulation 后原子 apply/publish。跨组变化只阻止新违规并返回
  migration required，不搬迁存量。
- `POST /workspaces/{id}/legal-holds`：必须 `Idempotency-Key`，body 仅允许 classification code、
  `classification|object_digest` scope、object digest（仅 object scope）、固定 reason、可选 expires_at、confirmed。
  返回创建后的低敏 hold；同 owner/scope active hold 幂等。
- `DELETE /workspaces/{id}/legal-holds/{hold_id}`：必须 `If-Match`、`Idempotency-Key`，body 仅允许固定
  release reason 与 confirmed；CAS 转为 released，不物理删除。
- `GET /platform/data-residency/evidence`：只允许 bounded `workspace_id/component/outcome/cursor/limit`；返回固定
  component/region/storage class/policy generation/outcome/violation/watermark/hash/time，不返回客户正文或路径。

## 5. 第一轮自审：策略权威与访问边界

- P0：只在 Console 保存驻留选择不会约束 Gateway/backup。修正为签名策略快照和所有消费者 owner 校验。
- P0：Legal Hold 被当成读取授权会形成数据后门。修正为 hold 只参与 deletion predicate，绝不进入 read ACL。
- P0：跨区审批只记日志不能约束路由。修正为 active policy generation 进入 placement 与 restore guard。
- P1：策略损坏回退默认 region 可能违规。修正为 fail closed 或仍有效的 owner-bound LKG。
- P1：Platform evidence 含对象路径会泄漏客户资产。修正为低基数 component/region/outcome/hash。

## 6. 第二轮自审：删除竞态与可证明性

- P0：hold 与 scheduler 并发可能先删后保全。修正为同一 generation fence 和 BEGIN IMMEDIATE/CAS。
- P0：到期删除只改状态不能证明执行。修正为每类 checkpoint、count、watermark、manifest hash 和终态证据。
- P1：备份保留与主存储策略独立会留下过期副本。修正为 backup grace/expiry 纳入同一 policy projection。
- P1：策略更新自动跨区搬迁风险过大。修正为本 PRD 只阻止新违规并生成 migration-required，不执行搬迁。
- P1：hold reason 自由文本进入事件。修正为固定 reason code，案件正文留在外部法务系统。

## 7. 专项验证计划

0108/0109 fresh/往返/down guard；策略 canonical/hash/generation；Gateway/job/backup/restore/tenant-data 消费；
hold/delete 并发；PRD-089 导出注销协调；LKG/损坏/过期；跨 Workspace/平台字段裁剪；SLO 与 scheduler 偏差；
真实 Console 三浏览器、三档缩放；对象路径、正文和凭据敏感扫描。
