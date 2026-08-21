# PRD-096 平台租户目录与生命周期运营台

状态：已完成
阶段：S2-平台运营
优先级：P0
依赖：PRD-095、现有 PRD-086/089、PRD-093/094

## 1. 目标与边界

本条让平台运营人员通过独立 PlatformSession 查看跨 Workspace 的最小运营目录，并按职责执行冻结、
恢复、风险标签和联系 Owner 通知。目录只聚合身份壳、Region、套餐占位、部署占位、生命周期、健康安全
摘要和数据导出/删除进度；不开放客户业务正文，不把平台人员写成 Workspace Membership，也不允许平台
直接编辑客户 Provider、Endpoint、Key、模型、策略、成员、Prompt、Response、文件或调用日志正文。

PRD-096 复用 PRD-095 的平台角色、权限、Session、导航和不可变审计，不新建第二套平台账号或 Cookie。
客户成功续费协同、渠道归属和增长分析分别留给 PRD-140/139/141；套餐与欠费真值由 PRD-098..104
接入，本条只返回明确的 `not_configured/not_available`，不能伪造免费套餐或健康状态。

## 2. 平台目录最小投影

目录 DTO 固定返回：

- `workspace_id`、kind、显示名称、可空规范 slug、创建时间。
- operational status/revision、Region code、residency policy version。
- member count、active session count 等有界整数；不返回成员姓名、邮箱、角色明细或 account/user ID。
- `plan_status` 和 `deployment_status` 固定枚举。真实来源尚未交付时分别为 `not_available` 与
  `not_configured`，不能从旧 Plan/Bucket 或 Provider Endpoint 推断。
- 最新平台健康投影：score band、risk level、固定 risk tags、fresh/stale/not_ready/unavailable 和评估时间；
  不返回 PRD-086 的 tenant-scoped 推荐动作、源信号数值、请求/成本/用户行为明细或 source hash。
- 数据生命周期：active/deletion_requested/deleted/conflict 与最近 export/delete 作业的固定状态摘要；不返回
  导出文件路径、下载 Token、对象正文或底层错误。
- Owner 联系状态只返回 `available/unavailable` 与最近通知状态；不返回 Owner 邮箱或成员列表。

不同平台角色通过同一列白名单再做字段裁剪。platform_support 只能看到支持排障所需状态；
platform_billing 只增加套餐/欠费枚举；platform_security 增加风险标签；platform_customer_success 可发起
Owner 通知；platform_ops 才能冻结/恢复。platform_owner 拥有这些平台权限的并集，但仍不读客户正文。

## 3. 数据模型

0088 新增：

- `workspace_operational_states`：workspace/tenant 复合 owner、status、revision、source、suspended reason、
  actor、时间；状态为 `active/suspended/deletion_pending/deleted/migration_conflict`。
- `workspace_risk_tags`：固定 tag code、status、revision、创建/撤销 actor 与时间；不保存自由文本。
- `workspace_health_projections`：对最新 PRD-086 score 的平台安全投影，只保存 score band、risk、固定 tags、
  freshness source time 和 source score ID；不复制原始信号。
- `platform_action_intents`：suspend/restore/risk/contact 的幂等/CAS 事实，绑定 Operator、workspace revision、
  request hash、固定 reason、状态和 request/trace 链。
- `workspace_owner_notification_outbox`：绑定 Workspace 和 active owner account 引用的通知意图；payload 只含
  template code、action intent ID 和 schema version，不保存邮箱或客户名称。
- `workspace_operational_events` 与 `workspace_operational_event_outbox`：生命周期/风险事件和安全投递引用。

所有同时保存 workspace_id/tenant_id 的表通过 `workspace_tenant_bindings` 复合外键证明一致；所有平台 actor
通过 active PlatformOperator 外键证明。OperationalState 是平台暂停权威，不能用 `workspaces.status` 的
`migration_conflict` 语义代替，也不能把 `tenants.status=disabled` 单独解释为可恢复暂停。

## 4. 0088 回填与兼容

回填逐 Workspace 联合 `workspaces`、binding、`tenants` 和可选 `tenant_data_states`：

- Workspace/binding/tenant 均 active，且数据生命周期不存在或为 active：operational=`active`。
- 已进入 tenant deletion requested/quiescing/deleting：`deletion_pending`。
- tenant data 已 deleted 且 tenant disabled：`deleted`，永不可通过平台 restore 复活。
- 任一 owner/binding/status 组合无法唯一解释：`migration_conflict`，目录可见安全摘要但拒绝 Session、资源写、
  suspend/restore 和自动修复。

0088 为后续新 Workspace 安装受约束初始化 trigger；PRD-093 创建事务也显式写 active operational state，
两者以唯一约束保证一次。已有 WorkspaceSession 签发/认证和 identity workspace 列表在 0088 schema 存在时
必须联合 operational state=`active`；suspended/deletion/deleted/conflict 均失败关闭。N/N-1 Legacy Bearer
继续受 `tenants.status` 约束。

## 5. 查询、搜索与分页

`GET /platform/workspaces` 使用服务端固定字段和 HMAC 游标分页，支持：

- exact `workspace_id` 或 organization 规范 slug 搜索；个人 label 和企业名称首版不可作为搜索键，搜索词
  不进入 cursor 或日志。
- kind、operational status、Region、risk、health freshness/risk、plan status、deployment status 筛选。
- order 固定为 `updated_at DESC, workspace_id`，limit 1..100；游标绑定规范筛选、role set、Operator revision、
  PlatformSession 和方向。篡改、跨角色或过期游标固定失败。

0088 只建立目录过滤复合索引并复用权威 `workspaces.workspace_id` 与
`organization_profiles.slug_normalized` 唯一索引，不创建第二份名称、slug 或低熵 hash 目录。首版不做
name/contains/fuzzy 搜索，避免全表扫描、字典攻击和第二份明文目录。空搜索按过滤索引扫描；10 万
Workspace `EXPLAIN QUERY PLAN` 不得出现无界业务表扫描。

列表总数使用有界 `has_more` 与下一游标，不在每页执行跨全表精确 COUNT。不存在和无权限 workspace 的详情
同形 404；主要状态、长度和时延不能用来枚举被角色裁剪的记录。

## 6. 详情与健康投影

`GET /platform/workspaces/{workspace_id}` 读取同一最小目录投影、operational revision、risk tags、最近平台
动作和 Owner 通知状态。事件时间线只来自 `workspace_operational_events` 与 PRD-095 平台审计的安全摘要，
不混入企业 audit_logs、Webhook 正文或客户成员事件。

`workspace_health_projections` 由 0088 对现有最新 PRD-086 score 回填，并通过受约束 trigger 在新
`customer_health_scores` INSERT 后刷新。投影重算固定：score 只分 `healthy/watch/at_risk/critical` band，
risk tags 只接受 PRD-086 固定枚举，超过 2 小时 stale。source row 缺失/坏 hash/未来时间时返回
`unavailable`，不得回退扫描 request_logs、usage 或用户活动。

`GET /platform/workspaces/{workspace_id}/health` 只读此平台投影。platform_support 即使持有普通
PlatformSession 也只能读这一最小摘要；需要 Workspace 诊断字段时仍必须走 PRD-095 SupportGrant/
SupportSession，不能把平台目录当成 JIT 绕过。

## 7. 冻结状态机

`POST /platform/workspaces/{workspace_id}/suspend` 要求 platform_ops、PlatformSession、Origin/CSRF、
`If-Match` operational revision、Idempotency-Key、固定 reason code 和页面内确认。事务中：

1. 重验 Operator/permission/revision、Workspace binding、tenant data lifecycle 和当前 operational revision。
2. 只允许 `active -> suspended`；deletion_pending/deleted/conflict 固定拒绝。
3. 写 `workspace_operational_states` revision+1，并把 legacy `tenants.status` 从 active CAS 为 disabled。
4. 递增 Workspace revision，撤销全部 active WorkspaceSession；GlobalSession、Membership 与客户数据保留。
5. 写 PlatformActionIntent、platform audit、workspace operational event/outbox。

冻结后新 WorkspaceSession、新 tenant 资源写和 Legacy Bearer 登录均失败。现有 Gateway 的 Virtual Key 热
路径在 PRD-101 接入平台 operational 快照前，继续依赖 tenant disabled 失败关闭；本条不得声称跨 Cell
传播已完成。目录明确展示 `control_suspended` 与 `gateway_transition_pending|not_applicable`，不伪造数据面
已停。

## 8. 恢复状态机

`POST /platform/workspaces/{workspace_id}/restore` 只允许 `suspended -> active`，同样要求 CAS、幂等、reason
和确认。事务必须证明：

- 当前 suspended 事实由平台动作产生，且对应 action/audit 链完整。
- `tenant_data_states` 仍 active，没有 deletion request、tombstone 或删除 worker lease。
- Workspace/binding/Membership owner 基础无 conflict，tenant 仍是由本次 suspend 设置的 disabled 状态。

满足后恢复 tenant active、operational revision+1、Workspace revision+1并写事件；旧 WorkspaceSession 和
Legacy Session 不复活，用户必须重新签发。任一条件不成立返回固定 lifecycle conflict，禁止“恢复”已注销、
迁移冲突或被其他安全流程禁用的租户。

并发 suspend/restore 使用 operational revision 和唯一 idempotency owner，最多一个提交。相同 key+相同
body 重放原结果；相同 key+不同 body 冲突，不重复事件或通知。

## 9. 风险标签

platform_security/platform_customer_success 可按各自固定 allowlist 添加或撤销风险标签，例如
`security_review/payment_attention/adoption_attention/compliance_hold`。安全角色不能写商业标签，客户成功
不能写 security/compliance 标签；platform_support 只读。

写入要求 If-Match risk revision、Idempotency-Key 和 reason code。标签只影响平台运营提示，不自动修改
Workspace 权限、路由、套餐、账务或数据删除状态；需要冻结必须显式走 suspend。每次变更写平台审计与
workspace risk event，禁止自由文本进入标签或事件。

## 10. 联系 Owner 通知

`POST /platform/workspaces/{workspace_id}/owner-notifications` 仅允许 platform_ops/
platform_customer_success，选择固定 template code 和 reason code。服务端在事务中解析目标 Workspace 的
active owner Membership 和 GlobalAccount 引用；若无唯一可通知 owner，固定失败且不暴露成员状态。

Outbox 不保存邮箱明文，投递 adapter 在发送时从 Identity owner 受控读取当前验证地址。通知失败不回滚已
提交 action intent，按固定错误码重试；平台页面只显示 pending/sent/failed。不得把通知入口扩展为任意
邮件、成员导出或客服查看邮箱。

## 11. HTTP 合同与权限

| 方法与路径 | 角色/权限 | 返回或写入边界 |
| --- | --- | --- |
| `GET /platform/workspaces` | ops/customer_success/support/security/billing/owner | 角色裁剪最小目录、HMAC cursor |
| `GET /platform/workspaces/{workspace_id}` | 同上 | 单 Workspace 安全运营摘要 |
| `GET /platform/workspaces/{workspace_id}/health` | 同上 | 平台 health band/tags/freshness，不返回源信号 |
| `POST .../{workspace_id}/suspend` | platform_ops/owner | active->suspended CAS；撤 Session；保留数据 |
| `POST .../{workspace_id}/restore` | platform_ops/owner | 仅恢复平台暂停，禁止复活 deletion/conflict |
| `POST .../{workspace_id}/risk-tags` | security/customer_success/owner | role-specific fixed tag allowlist |
| `DELETE .../{workspace_id}/risk-tags/{tag}` | 对应写角色 | revision CAS 撤销标签 |
| `POST .../{workspace_id}/owner-notifications` | ops/customer_success/owner | 固定模板 outbox，不返回邮箱 |
| `GET .../{workspace_id}/events` | 按角色读 | 只读平台 operational 安全时间线 |

所有写路由在 body 解析前完成 PlatformSession 权限校验，使用严格 JSON、Origin、CSRF、If-Match 和
Idempotency-Key。客户端不能提交 tenant_id、account_id、owner、状态 after 值、平台 actor 或事件 payload。

## 12. Console 交互

PRD-095 `/platform.html` 新增平台总览、租户目录、运营详情和事件时间线。目录是紧凑表格/列表，不使用
卡片瀑布；支持状态、Region、风险和健康筛选，搜索只在提交后执行，不把原词保存到 URL、Storage 或
遥测。详情为全宽工作区，生命周期与风险操作使用图标+文本命令、revision 冲突提示和页面内确认。

冻结/恢复按钮按后端 permission 投影显示，但伪造 DOM、直达 URL 和迟到响应仍由 API 拒绝。操作成功后
递增页面 generation、abort 旧详情/列表请求并重读；降权、Session 过期和 Platform Broadcast invalidation
清空当前 Workspace 摘要。390 视口把目录行转换为带明确字段标签的堆叠行，1280/1440 保持高密度表格；
文本、筛选、确认和分页不得重叠或横向溢出。

## 13. 迁移与回滚

0088 只从完整 0087 升级，回填每个 Workspace 的 operational/health 安全投影，安装新 Workspace
初始化 trigger 和 WorkspaceSession operational guard。升级后复核：每个 binding 恰有一个 state、
Workspace/tenant owner 一致、deleted 不可恢复、目录过滤索引与权威 slug 唯一索引有效、health source
引用合法、FK=0、integrity ok。

pristine down 仅允许所有 state 仍等于确定性 backfill、没有 suspend/restore/risk/contact/action/event/
notification 事实、没有 0088 后 Workspace、没有 session 因 operational reason 撤销。任何安全或生命周期
事实存在时原子阻止。运行时回退可关闭平台目录路由，但必须继续执行 operational deny overlay，不能仅
恢复旧代码后把 suspended Workspace 当 active。

## 14. 性能、观测与隐私

- 10 万 Workspace 目录 warm p95 `<500 ms`，limit=100；单详情/health warm p95 `<300 ms`。
- suspend/restore 提交 p95 `<500 ms`；本地下一次 WorkspaceSession 请求立即失败，跨实例目标沿用 PRD-095
  `<5s` 撤权传播，但 Gateway Cell 传播留给 PRD-101/108。
- 日志、event、audit、cursor 和 outbox 不含搜索词、客户名称、Owner 邮箱、成员列表、Endpoint、Secret、
  Prompt/Response、源 health 信号或删除文件路径。
- 本条不产生模型 Usage 或成本账；目录读取和平台动作不得污染客户调用/成本事实。

## 15. 专项测试

覆盖 0087->0088/fresh/down/up、active/deletion/deleted/conflict 回填、10 万 Workspace 查询计划与 p95、
workspace/slug exact search、cursor 篡改/跨角色、五角色字段裁剪、详情同形 404、PRD-086 health 安全投影/坏源失败、
suspend/restore CAS/幂等/100 并发、Session 与 Legacy 登录撤权、禁止复活 deletion、风险标签职责分离、Owner
通知无邮箱、平台/Workspace/Support/Legacy 凭据负矩阵、CSRF/Origin、不可变事件/审计、真实浏览器筛选/
键盘/确认/动态撤权/三视口、敏感 canary 0 命中和 PRD-086/089/093/094 兼容回归。

## 16. 第一轮自审：生命周期、兼容与恢复

结论：通过，P0/P1 已修正。

- P0：直接把 `workspaces.status` 扩为 suspended 会混淆既有 migration_conflict，并要求重建被大量 FK 引用
  的表。修正为独立 WorkspaceOperationalState，WorkspaceSession 在 schema 存在时联合复核。
- P0：只看 `tenants.status=disabled` 无法区分平台暂停和数据删除，restore 可能复活已注销租户。修正为
  operational action 链 + tenant_data lifecycle 双重证明，deletion/deleted/conflict 永不可恢复。
- P0：冻结只撤 WorkspaceSession 但不处理 Legacy Bearer/Gateway，会产生双轨授权。修正为事务 CAS tenant
  disabled，WorkspaceSession 立即撤销；Gateway Cell 未接入前明确显示过渡状态，不虚报已停。
- P1：运行时回退若忽略新 state 会重新开放 suspended Workspace。修正为回退保留 deny overlay，只有
  pristine schema down 才能删除 0088 结构。
- P1：新 Workspace 若没有 state 会被查询漏掉或认证行为不确定。修正为 migration trigger 与创建服务双写
  同一唯一 state，并在事务内复核。

## 17. 第二轮自审：目录、字段与并发

结论：通过，P0/P1 已修正。

- P0：复用 tenant-scoped customer-health DTO 会把请求、成本、成员活跃和推荐动作暴露给平台。修正为独立
  allowlist 投影，只保留 band/risk/tags/freshness。
- P0：支持 name/fuzzy 搜索需要第二份明文/低熵 hash 目录或全表 LIKE，并扩大枚举面。修正为只复用权威
  workspace ID/organization slug 索引，首版明确不做 name/contains/fuzzy。
- P0：平台目录可读不等于支持访问授权。修正为目录只含运营元数据；Workspace 诊断仍要求 095
  SupportGrant/SupportSession。
- P1：精确 COUNT、裸 offset 或未绑定 cursor 在 10 万租户和降权后会退化/越权。修正为 has_more、复合
  索引和绑定 Session/filter/role revision 的 HMAC cursor。
- P1：风险标签若直接驱动冻结会把客户成功或安全提示变成隐式权限动作。修正为标签纯运营事实，冻结必须
  单独确认、授权和审计。
- P1：Owner 通知若持久化邮箱会复制 Identity PII。修正为 outbox 保存 account 引用，投递时受控解析，
  平台 DTO 只返回投递状态。

两轮修正已写回本设计，编码不得弱化这些边界。
