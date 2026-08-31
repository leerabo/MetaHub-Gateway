# PRD-139 渠道伙伴、代理商与客户归属管理

状态：设计冻结，等待 G6 通过后编码。依赖 PRD-087/095/096/098/099/102..105/132/138，
批次边界见 `S7-platform-operations-console-domain-batch-design.md`。本条只交付 0131、渠道平台工作台、
独立伙伴只读入口和旧 PRD-087 事实迁移，不提前实现 PRD-140..148。

## 1. 产品闭环与非目标

`platform_channel` 管理渠道伙伴组织、固定代理等级、合同摘要、区域范围、ReferralCode、商机和
Workspace 归属申请；`platform_owner` 用独立账号审批归属，`platform_billing` 只读取可对账的
CommissionRule snapshot 和 ChannelStatement。伙伴使用全局账号进入独立 `/partner` 壳层，只能查看
自己 ChannelMembership 下已归属 Workspace 的安全摘要和已关闭账单摘要。

对外 HTTP 合同严格限定为：

- `GET/POST /platform/channel-partners`
- `POST /platform/channel-leads`
- `POST /platform/workspace-attributions`
- `POST /platform/workspace-attributions/{id}/approve`
- `GET /platform/channel-statements`
- `GET /partner/workspaces`

本条不允许伙伴进入 Workspace 控制面，不允许平台角色读取 Prompt/Response、请求日志正文、附件、
工单正文或 Provider Secret，不在企业 Workspace 保留 `partner-portal`，不把旧
`partner_customers` 自动声明为真实客户归属，也不通过任意 URL、SQL 或 schema 反射导入事实。

## 2. 身份、owner 与权限

### 2.1 伙伴组织 owner

Excel 将八类对象归为 Workspace/tenant owner。每个 ChannelPartner 因此绑定伙伴组织自己的不可变
`owner_workspace_id + owner_tenant_id`，该 owner 来自受控 WorkspaceTenantBinding；它不是任何被归属
客户 Workspace。ChannelMembership 额外绑定 GlobalAccount，Partner API 用
`GlobalSession + ChannelMembership revision` 恢复权限，绝不创建或消费 WorkspaceSession。

WorkspaceAttribution 同时保存伙伴组织复合 owner 和目标客户 `workspace_id + tenant_id` 复合 owner；
两组 FK 都必须匹配。目标 Workspace 只表示商业归属，不授权伙伴读取其业务内容。跨 owner ID、停用
Membership、过期 GlobalSession、重复 Cookie、Bearer、WorkspaceSession、PlatformSession 和
SupportSession 都统一失败关闭；对象不存在与错 owner 同形 404。

### 2.2 固定权限

0131 增加不可变 `platform_channel_permission_extensions` 和固定角色映射，并在重构后的平台权限表
白名单中显式登记：

| Permission | platform_owner | platform_channel | platform_billing |
| --- | --- | --- | --- |
| `platform.channel.read` | 允许 | 允许 | 否 |
| `platform.channel.manage` | 允许 | 允许 | 否 |
| `platform.channel.lead.register` | 允许 | 允许 | 否 |
| `platform.channel.attribution.request` | 允许 | 允许 | 否 |
| `platform.channel.attribution.approve` | 允许 | 否 | 否 |
| `platform.channel.statement.read` | 允许 | 允许 | 允许 |

现有 `platform.channel.read` 仍是平台导航基线，不重复创建。新增权限只来自固定 extension table；
`_authority` 不扫描任意 schema 表。每次请求恢复 operator status/revision、role binding 和 permission
fingerprint；申请后撤权或停用会使审批、重试和迟到响应立即失败。

ChannelMembership 只有 `partner_owner`、`partner_operations`、`partner_finance` 三个伙伴域角色；它们
不是 `platform_*` 或 Workspace RBAC 角色。当前 Excel 只开放伙伴 GET，因此三个角色都只能读取各自
归属清单，finance 才能看到裁剪后的 statement totals。成员管理由 Platform POST command 执行，伙伴
不能自助扩大成员或归属。

## 3. 0131 数据模型

### 3.1 业务事实

- `channel_partners`：保留可兼容的 `par_*` ID；伙伴组织复合 owner、稳定 code/display name、
  tier、region scope、contract digest、状态、revision、创建/更新 operator 和 UTC。合同正文不入库。
- `channel_memberships`：partner owner、global account、固定 role、active/revoked、revision、邀请/撤权
  摘要和 UTC；唯一 `(channel_partner_id, account_id)`，外键同时验证 partner owner。
- `channel_referral_codes`：不可逆 code lookup digest、masked suffix、有效期、使用上限、状态和 revision；
  明文只在创建响应中出现一次，日志、DOM、Storage、事件和数据库不保存。
- `channel_registered_leads`：伙伴 owner、稳定 lead code、referral reference、region、stage、联系信息
  envelope ciphertext/lookup digest、source digest、revision 和 owner-qualified 可选候选 Workspace。
- `workspace_attributions`：伙伴 owner、目标 Workspace owner、active rule snapshot、开始/结束时间、状态和
  revision；同一目标 Workspace 同时最多一个 active attribution。
- `AttributionChange` 对应 `attribution_changes` 与 `attribution_change_approvals`：before/after owner、reason code、requester、
  required approvals、当前 generation、CAS revision 和独立审批事实。申请人与审批人、两个审批人均不同。
- `channel_commission_rule_snapshots`：合同 digest、tier/region、币种、bps、有效期和 immutable version；
  attribution 和 statement line 都引用精确 snapshot，禁止修改历史金额口径。
- `channel_statements` 与 `channel_statement_lines`：partner/target owner、UTC period、币种、整数最小单位、
  source ledger/legacy commission reference、rule snapshot、reconciliation hash、状态和 close revision。
  关闭后不可变；它证明待结算/已对账事实，不伪造实际付款。

### 3.2 迁移、命令、事件和 Scheduler

- `channel_legacy_object_mappings` 逐条保存 legacy table/ID、目标 kind/ID、source/target canonical hash、
  migration generation 和结果，不覆盖旧 ID。
- `channel_migration_cursors` 保存每张 0080 表的 rowid/业务水位、行数、hash、outbox 和审计水位；
  回填幂等重放，同一来源只能映射一次。
- `channel_action_commands` 绑定 operator/action/owner/canonical request digest；同幂等键同请求返回原结果，
  同键异体 409。所有写同时要求 quoted `If-Match`；最终审批重新恢复所有审批人的当前权限/revision。
- `channel_events` 与 `channel_event_outbox` 同事务写入四个固定事件：
  `channel.partner_changed`、`channel.lead_registered`、`workspace.attribution_changed`、
  `channel.statement_closed`。payload 只含 ID、owner、状态、revision、digest、计数和 UTC。
- `channel_scheduler_state` 使用持久 lease、generation、token digest、statement/backfill/outbox 水位和
  bounded batch；PRD-147 会把该 code 纳入统一注册表，但 0131 起迟到 worker 已必须通过 fence。

Down 仅在所有业务、成员、商机、归属、审批、规则、statement、mapping、command、event/outbox、
scheduler 和审计水位为空时执行。含 legacy backfill 的数据库必然拒绝 down；空 0130 数据库验证
`0130 -> 0131 -> 0130 -> 0131`。任何拒绝都不能改写 0080 源事实。

## 4. 旧 PRD-087 迁移与唯一事实

0131 精确盘点并迁移六类现有表：`partners`、`partner_customers`、
`partner_commission_cursors`、`commission_records`、`partner_portal_operations` 和
`partner_customer_outbox`。迁移前后分别记录行数、canonical hash、最大业务序列、待投递状态、
attempt/generation、审计链和 scheduler watermark。

- `partners.partner_id` 原值作为 ChannelPartner ID；legacy tenant 只通过固定
  WorkspaceTenantBinding 映射为伙伴组织 owner。缺失、重复或漂移 binding 阻断整条 partner 回填。
- legacy `created_by_user_id` 必须通过既有账号映射恢复 GlobalAccount，创建初始 ChannelMembership；
  无唯一账号映射时保留阻断差异，不生成伪成员。
- `partner_customers` 只是同一 legacy tenant 内 Project 映射，不足以证明另一个客户 Workspace。
  它们迁为 `legacy_unresolved` RegisteredLead 和 source mapping；只有显式 owner-qualified 解析及审批后
  才生成 WorkspaceAttribution，禁止自动归属给伙伴自身。
- `commission_records` 逐条进入不可变 statement line，保留 source ledger/record ID、币种、金额、bps、
  token 计数、estimated 和 UTC；按 partner/月份/币种形成 imported statement，canonical 总额必须相等。
- `partner_portal_operations` 映射为不可变迁移审计，保留 request/trace/hash，不复制展示名或支持正文。
- pending/leased/failed `partner_customer_outbox` 保留 event ID、payload hash、attempt、generation、
  next-attempt 和 subscription snapshot 水位；sent 只写映射，不重新投递。旧 lease 先 fence，再由新 worker
  接管，不能把未知结果当成功。

切换完成后旧六表安装 write guard 并成为历史来源；`/partners/*` 从外部 router 卸载，普通请求只得到
统一 404。企业 `partner-portal` 从 Workspace navigation 移除并停止预取。旧前端模块的物理删除、全部
直接 hash/DOM 静态归零和全 Scheduler 注册表由 PRD-147 收口；rollback 只能切内部兼容读，不能重新挂载
旧 API、企业导航或写权限。

## 5. 状态机、并发与 API 语义

`POST /platform/channel-partners` 使用严格 discriminated command，固定支持 create partner、update
catalog metadata、upsert/revoke membership 和 rotate referral code；每种 command 有独立 exact body，
未知 action/字段拒绝。GET 使用稳定 cursor 分页和固定 filter enum，不接受 tenant/workspace owner 覆盖。

Lead 以 `(partner, source digest)` 和幂等 command 双重去重。联系明文只在授权平台请求生命周期解密，
不进入事件、错误、搜索索引或证据。ReferralCode 明文提交后立即转换为 lookup digest。

初始 attribution 为 `pending_approval`，由一个与 requester 不同的 platform_owner 批准后 active；替换
现有 active attribution 属于高风险 change，要求两个不同 platform_owner 依次调用 approve，二者都不能是
requester。最终批准事务内重新检查目标 Workspace、partner、rule snapshot、所有 operator revision、
当前 active attribution 和 quoted revision；然后原子 supersede 旧归属、激活新归属、写审计/event/outbox。
reject/cancel/expired 是静止终态，旧 approval 不能跨 generation 重放。

GET `/platform/channel-statements` 按当前 platform permission 裁剪；channel 和 billing 可看金额/币种/rule
version/reconciliation，不能看客户正文或 ledger 内容。GET `/partner/workspaces` 先恢复 GlobalSession，
再按当前 ChannelMembership 查询 active attribution；响应只含 Workspace display summary、region、归属期、
健康枚举和角色允许的 statement totals。分页 total 在 owner 过滤后计算，任意 ID/offset 不能枚举其他伙伴。

所有响应带 `request_id`、`trace_id`、`Cache-Control: no-store` 和 exact DTO。平台写要求 exact
Origin/CSRF、PlatformSession、step-up、幂等键和 CAS；伙伴 GET 只接受 GlobalSession Cookie，拒绝 query
凭据和混合 Cookie。数据库/加密/依赖故障统一失败关闭并保留可接管命令或水位。

## 6. Console 与伙伴页面

Platform Channels 工作台复用现有 `platform.html#channels` 壳层，但使用独立 channel DTO/client/store；
展示伙伴目录、等级/区域/合同 digest、成员状态、ReferralCode 状态、待审批归属、statement 对账和失败恢复。
只有 capability 存在时显示动作；高风险归属展示 generation、所需/已完成审批和 revision。

新增独立 `partner.html` 壳层，首屏直接加载 `/partner/workspaces`，没有 Workspace switcher、tenant API client、
Platform 导航或 SupportGrant 能力。页面有 loading/empty/stale/revoked/dependency unavailable 状态；离页、
注销、撤权和迟到响应都会 abort/epoch 清理。Referral 明文、step-up、Cookie、联系密文和内部摘要不进入
URL、DOM、Storage 或日志。

企业 Workspace 从本条起不返回 `partner-portal`，也不发起 `/partners/*` 请求。PRD-147 统一删除源码导入、
route switch 和旧 DOM；G7 对 Navigation/DOM/prefetch/API 做最终零命中审计。

## 7. 聚焦测试与批次证据

本条开发期间只运行：0131 空库往返、legacy 六表回填/hash/down guard、复合 owner/FK；六路由成功/匿名/
错域/未知字段/幂等/CAS/动态撤权；ChannelMembership 枚举隔离；lead 去重；初始及双审批归属；statement
金额复算；outbox/lease/fence 接管；Platform/Partner strict DTO/store 和企业导航不再返回 partner portal。

不在本条重复全套数据库、三浏览器、性能或安全矩阵。PRD-147 后的 G7 统一验证渠道目录 p95 < 500ms、
归属查询 p95 < 300ms、月结 1 万 Workspace < 30 分钟，三浏览器三缩放、真实伙伴/平台流程、旧入口四层
归零、代理/断网/N/N-1、动态撤权、敏感审计和静止终态。本条测试结论独立写入
`docs/testing/PRD-139-tests.md`，不与后续 PRD 合并。

## 8. 两轮设计自审

### 第一轮：身份、owner 与 legacy 语义

- P0：让伙伴复用 WorkspaceSession 会获得客户控制面能力。修正为 GlobalSession + 当前
  ChannelMembership，partner page 没有 Workspace client 或 switcher。
- P0：把 legacy `partner_customers` 直接迁为 WorkspaceAttribution 会把同 tenant Project 错认成独立
  客户。修正为 `legacy_unresolved` lead；只有 owner-qualified 显式解析和审批才能形成归属。
- P0：只保存 partner_id 而不保存伙伴组织和目标客户两组复合 owner，会允许跨域拼接。修正为所有子事实
  带伙伴 owner，attribution/statement line 额外带目标 Workspace owner，并用复合 FK/trigger 校验。
- P0：复制旧 commission 后继续让旧 worker 写会形成双事实。修正为 source ID 唯一映射、旧 lease fence、
  write guard 和新 statement line 唯一写入；sent outbox 永不重放。
- P1：保存合同/推荐码/联系人明文会扩大平台泄漏面。修正为合同 digest、推荐码 lookup digest 和联系人
  envelope ciphertext；事件、日志和证据只含安全摘要。

第一轮修正后无未关闭 P0/P1。

### 第二轮：审批、撤权、回滚与可对账性

- P0：一个 platform_channel 自己申请并批准归属会让客户归属不可控。修正为初始归属至少独立 owner
  批准，替换归属要求两个独立 owner；申请人、两审批人互异，最终事务重验全部 revision/permission。
- P0：旧 route 返回 retired 响应仍不满足 G7 API 归零。修正为外部 router 卸载和普通 404；内部
  tombstone/mapping 才保存迁移历史，rollback 不能重新挂载。
- P0：statement 只比汇总金额会掩盖行级丢失或重复。修正为逐 source record 唯一映射、整数金额、币种、
  rule snapshot、行数/canonical hash 和聚合总额同时匹配。
- P1：partner total 在 owner 过滤前计算会泄漏其他渠道规模。修正为先按 current membership/attribution
  过滤，再计算 cursor 和 total；错误 owner 与不存在同形。
- P1：permission 表继续复制条件分支会漏掉新增权限或加载未知表。修正为固定 allowlist tuple + 固定 SQL
  模板，0131 权限表显式登记并纳入 authority fingerprint。

第二轮修正后无未关闭 P0/P1。G6 未通过前，本设计不授权创建 0131 或业务代码。
