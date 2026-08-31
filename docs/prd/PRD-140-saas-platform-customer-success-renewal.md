# PRD-140 SaaS 平台客户成功、健康与续费协同

状态：已完成。实现与专项验收通过；依赖 PRD-086/095/096/097/098..105/109/132/138；批次边界见
`S7-platform-operations-console-domain-batch-design.md`。本条只交付 0132、Platform Customer Success
工作台、五个 Excel API、四个事件和旧客户健康事实迁移，不提前实现 PRD-141..148。

## 1. 产品闭环与非目标

平台客户成功人员通过现有 `platform.html#customer-success` 进入独立工作台，查看 10 万级客户目录、
激活和采用摘要、版本化健康解释、续费机会、成功计划、跟进任务和 QBR 安全摘要。支持人员只看到处理
工单所需的客户状态与健康裁剪，财务人员只看到续费日期、币种和财务状态裁剪；平台 Owner 负责批准
批量任务，不能借角色读取客户业务正文。

对外 HTTP 合同严格限定为：

- `GET /platform/customer-accounts`
- `GET /platform/customer-accounts/{id}/health`
- `POST /platform/customer-success-plans`
- `POST /platform/customer-tasks`
- `GET /platform/renewal-pipeline`

本条不增加任意联系人 CRUD、自由文本 CRM、销售预测、营销活动、Prompt/Response 搜索、请求日志正文、
工单正文、附件、内部备注或 Provider Secret 入口。联系人来源仍由 GlobalAccount、WorkspaceMembership 和
通知偏好管理；0132 只保存 owner-qualified 引用和安全偏好摘要。计划、任务和 QBR 只接受固定枚举、整数、
日期和对象引用，不提供可被误用为客户业务正文的 notes/description/custom fields。

旧企业 `customer-health` 从本条切换后不再出现在 Navigation、DOM、prefetch 或可调用 API 中；
`GET /customer-health` 从外部 router 卸载并返回普通统一 404，不返回 retired/tombstone 提示。旧代码的物理
删除和 S7 全部静态归零由 PRD-147 收口，但不可在任何 rollback 中重新挂载。

## 2. 身份、owner 与权限

### 2.1 复合客户 owner

CustomerAccountProjection 代表一个受控 Workspace 客户，不是平台自有 tenant。每个客户对象均保存不可变
`workspace_id + tenant_id`，并以复合外键绑定唯一 active `WorkspaceTenantBinding`。联系人、健康快照、
计划、任务、续费机会、QBR、命令、事件和迁移映射全部重复携带并校验同一复合 owner；禁止只靠
`workspace_id`、`tenant_id` 或一个泛化 `owner_id` 拼接对象。

平台 API 只接受有效 `PlatformSession` Cookie。WorkspaceSession、SupportSession、GlobalSession、
伙伴 ChannelMembership、Bearer、query 凭据、重复 Cookie、错 Origin 和混合域 Cookie 全部失败关闭。
PlatformSession 逐请求恢复 Operator status/revision、role binding、全部权限和权限指纹；撤权、停用或迟到
响应必须在下一请求立即失效。Platform 读取客户安全投影不创建 WorkspaceMembership，也不获得 Workspace
业务 API 能力；需要正文诊断时仍必须另走 PRD-095 SupportGrant，绝不从本模块隐式升级。

### 2.2 固定权限与字段裁剪

复用现有基础权限 `platform.customer_success.read`，0132 新增不可变
`platform_customer_success_permission_extensions` 与固定角色映射：

| Permission | owner | customer_success | support | billing |
| --- | --- | --- | --- | --- |
| `platform.customer_success.read` | 允许 | 允许 | 否 | 否 |
| `platform.customer_success.support_read` | 允许 | 否 | 允许 | 否 |
| `platform.customer_success.finance_read` | 允许 | 否 | 否 | 允许 |
| `platform.customer_success.contact.read` | 允许 | 允许 | 否 | 否 |
| `platform.customer_success.plan.manage` | 允许 | 允许 | 否 | 否 |
| `platform.customer_success.task.manage` | 允许 | 允许 | 否 | 否 |
| `platform.customer_success.renewal.read` | 允许 | 允许 | 否 | 允许 |
| `platform.customer_success.batch.approve` | 允许 | 否 | 否 | 否 |

表中的 owner/customer_success/support/billing 分别指 `platform_owner`、
`platform_customer_success`、`platform_support`、`platform_billing`。平台 Owner 必须在 extension 表显式映射，
不能假设基础表中“Owner 拥有全部旧权限”的历史 INSERT 会自动覆盖未来权限。

`platform_access.service._authority` 在加入 0132 表前先完成批次要求的重构：只按固定
`_PERMISSION_EXTENSION_TABLES` tuple 顺序验证存在的表，再用固定 SQL 模板逐表加载；不扫描任意 schema，
不接受动态表名输入。权限指纹覆盖角色、Operator revision、所有固定扩展表的验证结果和权限集合。

同一 Operator 有多个角色时按权限并集裁剪，但响应仍从三个 exact DTO 片段组合，不返回“禁止字段为 null”
的宽松对象。Customer Success 可见目录、健康、联系人、计划、任务和续费运营字段；Support 只见账号状态、
工单安全计数、SLA 风险和裁剪健康因子；Billing 只见订阅、续费日期、币种、应收状态与安全 account ID，
不见联系人、采用信号、成功计划或任务。任何企业角色、伙伴身份和 Edge 主体均拒绝全部 `/platform/customer-*`。

## 3. 0132 数据模型

Excel 对象与 0132 权威表一一对应：CustomerAccountProjection 映射
`platform_customer_account_projections`，CustomerContact 映射 `platform_customer_contacts`，
CustomerHealthSnapshot 映射 `platform_customer_health_snapshots`，SuccessPlan 映射
`platform_customer_success_plans`，CustomerTask 映射 `platform_customer_tasks`，RenewalOpportunity 映射
`platform_renewal_opportunities`，QbrSummary 映射 `platform_qbr_summaries`。辅助 command/mapping/event/scheduler 表
只承担并发、迁移和恢复，不成为另一份业务真值。

### 3.1 客户目录与联系人

- `platform_customer_account_projections`：稳定 `csa_*` ID、复合客户 owner、Workspace identity source
  revision、显示壳、kind/region、operational status、activation phase/完成数、adoption band、订阅/续费状态、
  最新健康 snapshot/sequence、source watermarks/digest、projection version/revision、projected UTC。它是可重建
  投影，不成为 Workspace identity、订阅或工单的写入真值。0132 为固定 keyset 及 status/region/activation/
  adoption/health/renewal filters 建 covering 复合索引；禁止靠运行时 JSON 提取或全表排序满足目录查询。
- `platform_customer_contacts`：稳定 `cct_*` ID、复合 owner、GlobalAccount 与 active WorkspaceMembership
  引用、固定 `primary|billing|technical|security` 联系角色、status/revision、通知偏好 revision 与仅含
  category/channel/enabled 的 exact JSON。表中不复制 email、phone、display name 或消息正文；获得
  `contact.read` 的单客户 detail 请求才从当前 GlobalAccount 解析联系方式，并逐次写敏感读取审计。
- 联系人 endpoint 明文、偏好地址、抑制原因、登录状态和成员角色明细不进入目录列表、cursor、事件、日志或
  正式证据。来源 Membership 撤销、账号停用或偏好 revision 漂移会使联系人立即变为 unavailable，旧引用
  不可继续解密或显示。

### 3.2 唯一健康事实

- `platform_customer_health_snapshots` 是迁移后的唯一可写健康事实。它保留全部 PRD-086 `chs_*` ID、
  autoincrement sequence、窗口、source/projection hash、整数聚合、风险标签、operation request/trace 和 UTC，
  新增复合 Workspace owner 与 formula version。历史 `customer-health-v1` 行逐位保持，不重算、不改 ID。
- 切换后新行使用 `customer-health-platform-v2`。usage、reliability、cost anomaly、budget readiness、team
  activity 五个因子继续使用 PRD-086 冻结公式和原权重；第六个 `ticket_health` 只消费 PRD-132 的
  owner-qualified 状态、severity 和 SLA 计数，不读取 subject/message/attachment/internal note。
- v2 的 ticket 权重固定 1000，effective weight 固定 10000。任一 open P1 或 breached SLA 得 0/critical；
  任一 at-risk SLA 或至少 3 个 open P2 得 2500/at_risk；任一 waiting_platform P2 或至少 5 个未解决工单
  得 5000/watch；其余有 open 工单得 7500/watch；无 open 工单得 10000/healthy。其余五个权重仍为
  2500/2500/1500/1000/1500，按 PRD-086 最大余数法形成整数总分。PRD-132 来源缺失、类型漂移或 owner
  不一致会让整次 v2 评估失败，不按零工单或 partial 成功。
- `platform_customer_health_heads` 每客户只指向最新已验证 snapshot，保存 source sequence/hash、freshness
  watermark 与 projected UTC，取代 PRD-096 `workspace_health_projections` 的可写职责。目录只能通过 head
  读取健康摘要，禁止运行时回扫客户业务事实。

### 3.3 计划、任务、续费与 QBR

- `platform_customer_success_plans`：复合 owner、固定 objective code、起止日、目标健康/激活/adoption 基点、
  `draft|active|at_risk|completed|canceled`、revision、创建/更新 Operator 与 UTC。不得保存自由标题或正文。
- `platform_customer_tasks`：复合 owner、可选 plan/renewal 引用、固定 task type/priority/reason、assignee
  Operator、due UTC、`open|in_progress|completed|canceled`、revision、due event generation 和 UTC。任务不保存
  工单正文、客户备注或联系方式快照。
- `platform_renewal_opportunities`：复合 owner、Subscription/plan version 引用、renewal UTC、币种、仅在来源
  已存在时保存整数最小货币单位、health/adoption/payment risk codes、`observing|qualified|committed|renewed|lost|canceled`、
  formula version/revision、source digest 与 UTC。它是订阅和账务的只读运营投影，不能改写价格、发票或付款。
- `platform_qbr_summaries`：复合 owner、UTC quarter、snapshot watermarks、健康/采用/支持/续费安全整数、固定
  outcome/action code、canonical hash 和生成 UTC，append-only。没有会议纪要、客户引语、自由结论或附件。

### 3.4 命令、迁移、事件与 Scheduler

- `platform_customer_success_commands` 绑定 Operator、action、复合 owner、Idempotency-Key HMAC、canonical
  request hash、expected/result revision、状态、request/trace 和 UTC；同键同体返回原结果，同键异体 409。
- `platform_customer_task_batches` 与 `platform_customer_task_batch_approvals` 只保存最多 100 个去重 account ID
  的 canonical manifest hash、请求人、独立 platform_owner 审批人、permission revisions、generation 和结果
  计数。申请人与审批人必须不同；审批事务重新验证两人权限、全部 customer owner 和 manifest hash。
- `platform_customer_success_legacy_mappings` 逐条映射旧 table/ID 到目标 kind/ID，保存 source/target canonical
  hash 与 migration generation；`platform_customer_success_migration_cursors` 保存每个来源的行数、最大 sequence、
  source/target hash、outbox 状态计数、Webhook 投递和 scheduler 水位。
- `platform_customer_success_events` 与 `platform_customer_success_event_outbox` 同事务写四个 exact 事件；payload
  只含 ID、复合 owner、状态、revision、固定 reason、计数/hash 和 UTC。outbox 使用稳定 event ID、状态机、
  attempt、lease generation/token digest、subscription snapshot 和恢复水位，不信任 worker 传入 JSON。
- `platform_customer_success_scheduler_state` 固定 job code 为 `account_projection`、`health_evaluation`、
  `renewal_projection`、`task_due`、`qbr_snapshot`、`event_outbox`，保存 persistent lease、fencing generation、
  扫描 watermark、bounded batch、失败计数和 next run。0132 用一个
  `install_platform_customer_success_scheduler` 替换当前 `install_customer_health_scheduler` 的 supervisor 槽位，
  不增加 installer 数量；PRD-147 再把这六个内部 job 纳入统一注册表，不能届时才补持久化语义。

## 4. PRD-086/096 迁移与切换

0132 使用 expand/migrate/quiesce/cutover/contract，不能通过长期 dual-write 维持两个健康真值。

### 4.1 Expand 与盘点

1. 只接受 current head 0131，先创建目标表、固定权限、迁移 mapping/cursor、目标 worker fence 和 down guard。
2. 精确盘点 `customer_health_scores`、`customer_health_score_outbox`、
   `customer_health_scheduler_tenants`、`workspace_health_projections`，以及关联
   `customer.health_changed` Webhook subscription/delivery/dead-letter。记录逐表行数、canonical hash、最大 sequence、
   pending/leased/sent/failed、attempt/generation 和审计链水位。
3. 每个 legacy tenant 必须唯一映射 active `WorkspaceTenantBinding`；缺失、重复、漂移 owner 或 PRD-096
   projection 指向非最新 score 时整条迁移失败，不猜测 Workspace，也不产生 orphan account。
4. 同时盘点现有 `customer_health.read` tenant permission、默认/自定义角色映射、Navigation 条目和 authority
   fingerprint，供切换时精确收缩；不能仅隐藏前端。

### 4.2 幂等回填

1. 所有 score 按原 sequence 回填为 target snapshot，保持 `chs_*`、窗口、v1 formula、source/projection hash、
   request/trace 和 evaluated UTC；加入从 binding 得到的 workspace owner。目标 trigger 逐列复算 v1，任一差异
   回滚该 generation。
2. PRD-096 `workspace_health_projections` 必须与每个 Workspace 最新 score 的 ID、sequence、risk、tags、hash、
   evaluated UTC 完全一致，再映射为 target head；不一致进入 migration conflict 并阻断 cutover。
3. legacy outbox 保持 `chso_*`/`evt_chs_*`、状态、attempt、generation、subscription snapshot 和时间。sent 只建
   mapping，绝不重投；failed 保持终态；pending 由新 outbox 接管；leased 必须等旧 lease 结束或由更高 fence
   generation 接管，未知结果不能当成功。
4. 每租户 scheduler 行映射为复合 owner health job 水位，保持 next evaluation、failure count、generation 和
   last completed window。account projection 同时从 Workspace identity、operational state、onboarding、subscription、
   billing、最新 health 和 PRD-132 安全计数构建，所有来源 revision/hash 一并保存。
5. 相同 migration generation 重放只验证并返回同一 mapping；同 legacy ID 指向不同 target、相同 sequence
   不同 hash、目标额外行或 outbox 状态倒退一律阻断。

### 4.3 Quiesce 与原子切换

1. 设置 legacy claim fence，停止旧 health scheduler/outbox 新 claim，等待已租约完成或过期；再次记录 row/hash/
   outbox/scheduler 水位。新写入出现时回到回填，不允许带差异切换。
2. 一个 cutover CAS 事务验证前后行数/hash、最新 head、Webhook 状态、无 legacy active lease、目标 worker
   generation 更高，然后关闭旧写 trigger，移除 PRD-096 旧 projection trigger，并为四张旧表安装不可写 guard。
3. 新 health/scheduler/outbox 成为唯一 writer。旧表只作为内部迁移历史等待 PRD-147 contract；任何组件继续
   INSERT/UPDATE legacy 表都会失败并产生固定安全告警，不能静默双写。
4. 数据 cutover CAS 成功后，同一应用发布版本才卸载外部 `/customer-health` router；发布 readiness 在旧 route
   仍挂载或新 reconciliation 未通过时失败。企业 Navigation 不再返回 `customer-health`，前端 direct hash 不渲染、
   不预取。当前 Workspace permission catalog 精确移除 `customer_health.read`，保持其他当前权限、自定义角色和
   extension 表不变，并重新计算角色/Session 指纹。
5. `/platform/customer-*` 只在 target reconciliation 全部通过后开放。切换状态与 checksum 写不可变平台审计，
   日志不记录客户字段或联系人。

### 4.4 Down 与运行回滚

0132 down 只允许从未回填、未产生客户投影/联系人/健康/计划/任务/续费/QBR/command/batch/event/outbox/cursor，
且 scheduler 为 pristine 的空数据库执行。任一 legacy backfill 都意味着存在目标业务事实，down 必须在第一个
DDL 前原子拒绝；空库验证 `0131 -> 0132 -> 0131 -> 0132`。允许的空库 down 删除 0132 extension permission 后，
所有带旧 authority fingerprint 的 PlatformSession 在下一请求失败关闭，不能继续消费已不存在的权限。

生产 rollback 是有 lease、checkpoint、补偿和审计的 job，不是 down：暂停目标写、验证 target，可回退到 target
内部兼容读取或上一稳定 target worker，但绝不能解除 legacy write guard、恢复 `customer_health.read`、重新挂载
`/customer-health`、企业 Navigation/DOM/prefetch 或旧 scheduler。若 target 不可信则 Platform 工作台显示
dependency unavailable，不能用旧跨域能力兜底。

## 5. 状态机、并发与业务规则

### 5.1 健康与续费投影

健康 snapshot append-only，head 只允许 source sequence 单调增加并校验复合 owner/hash。历史 v1 永不改写；v2
每个客户每个合格窗口和 source fingerprint 最多一行。首个 snapshot 或公开 health projection digest 变化时写
`customer.health_changed`，单纯重跑、projected UTC 变化或不公开计数变化不发事件。

RenewalOpportunity 只从当前 WorkspaceSubscription、published PlanVersion、支付/发票安全状态和最新健康/adoption
投影生成。无订阅明确为 `not_configured`，不得伪造 renewal 日期或金额。active/trialing/past_due 且当前 period end
进入未来 180 日窗口时产生 observing；风险或成功计划存在时可变 qualified；只有确认的续费订单/订阅状态能变
committed/renewed，付款失败不能被任务完成覆盖。每次 risk code 集合变化写 `renewal.risk_changed`，相同 digest
不重复。

QBR job 在 UTC 季度结束后的首个合格窗口为每个有当前 account projection 的客户生成至多一条
`(customer_account_id, quarter)` summary；输入固定为该季度最后 health/adoption/support/renewal watermarks，迟到来源
通过下一 generation 生成 superseding summary 引用，绝不 UPDATE 历史行。相同 source digest 重跑返回同一 QBR，
缺失来源显式标记 incomplete，不用 0 冒充健康或无工单。

### 5.2 成功计划与任务

`POST /platform/customer-success-plans` 使用 discriminated command：`create`、`activate`、`revise_targets`、
`complete`、`cancel`。create 从 draft 开始；active 只能从 draft，completed/canceled 为静止终态；complete 需要所有
required task 完成且重验最新 health/renewal source revision，随后同事务写 `success_plan.completed`。所有更新要求
quoted `If-Match` 和 Idempotency-Key，permission 撤销或 revision 冲突不提交部分状态。

`POST /platform/customer-tasks` 支持 `create_one`、`transition_one`、`prepare_batch`、`approve_batch`。单任务按固定
状态机推进；due 时间首次到达且任务仍 open/in_progress 时以 `(task_id, due_generation)` 唯一写
`customer.task_due`。修改 due 必须 CAS 增加 generation，旧 timer/outbox 不能对新 deadline 发事件。

Batch 最多 100 个去重 customer account，只允许固定 task type/priority/due，不接收 per-customer 自由 payload。
Customer Success 准备 manifest，另一个当前 active platform_owner 在 step-up 后批准；批准事务逐客户重验 owner、
权限、状态和 source revision。任一无权/冲突使整批不落业务行；依赖瞬时失败保留可安全重试 command，不产生半批。

## 6. 五个 API 的 exact 合同

所有响应带 `request_id`、`trace_id`、`Cache-Control: no-store`、CSP 与统一错误 envelope；严格 Pydantic 模型
拒绝未知字段、NaN、浮点金额、非 UTC、未来水位和超界整数。写请求额外要求 exact Origin/CSRF、HttpOnly
Platform Cookie、Idempotency-Key、quoted If-Match 与必要 step-up。

### 6.1 `GET /platform/customer-accounts`

- query allowlist 为 `limit`、`cursor`、`customer_account_id`、`workspace_id`、`slug`、`kind`、`region`、
  `operational_status`、`activation`、`adoption`、`health_risk`、`renewal_status`、`view`；未知、重复、空值和
  任意 owner override 拒绝。limit 1..100。
- directory view 固定 keyset 为 `(projection_updated_at DESC, customer_account_id ASC)`，不返回 exact global total；
  HMAC cursor 绑定规范 filter digest、PlatformSession ID、Operator/role/permission revision 和 page boundary。
- 只支持 exact ID/slug，不提供 name contains/fuzzy。`view=detail` 仅允许精确一个 `customer_account_id`，返回
  当前/最近 20 个 plan、open/最近 50 个 task、最近 8 个 QBR 和最近 50 个安全 operation summary；每个集合有
  独立 bounded count/truncated 标志，不夹带正文。`view=contact` 同样只允许精确一个 account 且要求
  `contact.read`；它解析当前联系人并写一次敏感读取审计。目录页永不批量返回 email/phone。
- Support 和 Billing 使用各自 exact projection DTO；总数、cursor 和 filter 都在权限裁剪后的可见集合上计算，
  禁止先统计全量再过滤造成侧信道。

### 6.2 `GET /platform/customer-accounts/{id}/health`

`id` 固定为 `csa_*`。Customer Success/Owner 获得 v1/v2 formula、相邻窗口、score/risk、六个安全因子、固定
risk/action codes、freshness 与 source watermark；Support 只获得 risk、SLA/ticket 安全因子、评估时间和
固定建议，不获得用量、成本、预算、成员活动计数；Billing 无 health detail 权限。非法 ID 与不存在同形 404，
snapshot/hash/future time/source owner 任一不一致固定 503，不返回部分可信对象。

### 6.3 两个 POST

计划与任务 endpoint 只接受第 5 节冻结的 discriminated command。对象 ID、owner、operator、permission、request
hash、expected revision、source watermark、event/outbox 和平台审计在一个事务内提交。数据库/KMS/权限/依赖
错误失败关闭；同幂等键同体返回原 exact 结果，同键异体 409；终态重放不能新建 event。

### 6.4 `GET /platform/renewal-pipeline`

query allowlist 为 `limit`、`cursor`、`status`、`risk`、`renewal_from`、`renewal_to`、`region`、
`customer_account_id`。keyset 固定 `(renewal_at ASC, renewal_opportunity_id ASC)`，cursor 同样绑定当前权限指纹。
Customer Success 看客户安全壳、日期、risk codes、计划/任务进度；Billing 看订阅/plan version、币种、来源已存在
的整数金额、invoice/payment 状态，不看联系人/健康因子/任务；Owner 看两者并集但仍无正文。没有来源金额返回
明确 `amount_status=not_available`，不能用 0 冒充免费或已结清。

## 7. Scheduler、事件与 SLO

- account projection 消费 owner-qualified source revision/event，最迟 5 分钟追平；每轮 bounded 500，逐客户
  canonical hash 相同时不写 revision。全量 reconciliation 分片执行，不能阻塞请求线程。
- health worker 每分钟 claim 到期客户，在合格整点窗口后 5 分钟内完成 v2 snapshot/head；来源故障保留最后可信
  head 并标 stale/unavailable，不把失败水位当完成。旧 PRD-086 worker fence 后永不写回。
- renewal worker 每分钟消费订阅、订单、发票和 health/adoption source watermark；相同 source digest 幂等，状态
  只按权威事实推进。task worker 的 due scan/claim 周期不超过 15 秒，包含数据库争用后的 p99 deadline drift
  必须小于 60 秒。
- event worker 按 stable event ID 构造 exact payload并验证 source row/hash；网络失败只推进 outbox 状态，绝不
  回滚业务事实。过期 lease 由更高 generation 接管，旧 worker 完成结果因 fence 不匹配被拒绝。
- 10 万 account directory 在生产等价索引、100 并发和噪声邻居下 p95 < 500ms；健康投影 source-to-head
  freshness < 5 分钟；续费任务到期偏差 < 1 分钟。G7 同时记录 p50/p95/p99、错误率、CPU、内存、数据库锁和
  邻居延迟，不能只报告单次最佳值。

四个事件 exact payload 均包含 `event_id/schema_version/occurred_at/workspace_id/tenant_id/customer_account_id`
及各自 object ID、status/revision/reason codes/source digest；不得包含联系人、计划自由文案、工单正文、金额以外
财务明细或任何客户业务正文。事件 schema 分别固定为：

- `customer.health_changed`
- `customer.task_due`
- `renewal.risk_changed`
- `success_plan.completed`

## 8. Platform Console

复用现有 `customer-success` Platform 导航壳，但新增独立 customer-success DTO/client/store/controller，不复用
企业 `customerHealthPageState`，也不把现有 `/platform/workspaces` 宽对象直接当客户成功模型。工作台采用可扫描
的目录表、详情面板和 Health/Plan/Task/Renewal/QBR tabs；单客户 `view=detail` 供 Plan/Task/QBR tabs 读取，
Health tab 调用独立 health API，Renewal tab 用 account filter 调用 pipeline。联系人只在显式 `view=contact` 中按
权限显示，离页、切客户、撤权和注销立即清空。

固定状态包括 loading、empty、partial、stale、permission revoked、revision conflict、dependency unavailable、
batch awaiting approval、batch rejected 和 completed history。每个请求绑定 PlatformSession/Operator/role/
permission fingerprint、navigation epoch、page generation 和 request ticket；离页 abort，迟到 200/401/403/409/503
均不能覆盖新客户或注销新 Session。403 清内容并刷新导航，401 清 Session；旧 Cookie、联系人和 task manifest
不进入 URL、Storage、history、analytics 或 Console。

390/1280/1440、DPR 1/1.25/1.5 下表格/详情不重叠、不裁剪、不横向溢出；键盘、焦点、读屏状态与 modal/drawer
关闭恢复完整。真实 Chromium/Edge/Firefox 验收要求外部请求、Console error、Page error、未处理 rejection 为 0。

## 9. 隐私、审计与失败恢复

- Platform 投影只保存白名单 ID、枚举、整数、UTC、摘要和 owner 引用。Provider Key、Virtual Key、Token、
  Prompt、Response、请求正文、工单 subject/message/internal note、附件、客户文件和自由 CRM 文本均禁止。
- 联系方式只在授权单客户响应生命周期从 GlobalAccount 解析；普通平台审计记录 contact ID、actor、purpose code、
  owner、request/trace 和结果，不记录 email/phone/display name。KMS 或 source revision 不可用时拒绝显示。
- 搜索词、cursor、Idempotency-Key、CSRF、Cookie、lease token、SupportGrant 与联系人不进入日志。持久错误只允许
  fixed code；禁止异常字符串、SQL、文件路径和 `exc_info`。正式证据只含合成 canary 的扫描结论。
- 所有 mutation 同事务写不可变 `platform_audit_logs`、command、event/outbox；敏感 read 写独立 purpose audit。
  失败保留可接管 command/watermark，不生成“已完成”假记录。静止终态必须复核数据库、API、DOM、worker 和日志。

## 10. 聚焦测试与批次证据

本条编码期间只运行 0132、customer-success 模块、Platform authority/navigation、PRD-086/096 迁移兼容、
PRD-132 安全聚合以及受影响 Console 的单元/API 冒烟：

- 空库 0131/0132 往返；legacy 四表、Webhook/outbox/scheduler、PRD-096 projection 全量 row/hash/watermark 回填；
  owner 缺失/重复、projection 漂移、重放、活跃 lease、额外目标行、down guard 和 write guard。
- v1 逐位兼容与 v2 六因子整数复算；source 故障、future/stale、head 单调、health event 去重、旧 worker fence。
- 四角色 permission/字段矩阵；匿名、企业、伙伴、SupportSession、Bearer、混合 Cookie、动态撤权、迟到响应、
  同形 404、unknown query/body、cursor 篡改与权限侧信道。
- plan/task/renewal 状态机、幂等/CAS、终态、100 项 batch 上限、独立审批、权限漂移、原子失败、due generation、
  event/outbox 接管和重启恢复。
- exact/contact detail、无批量 PII、KMS/source revision 失败、日志/事件/证据敏感 canary；企业旧 Navigation/DOM/
  prefetch/API 归零和 `/customer-health` 普通 404。

PRD-140 专项结论独立写入 `docs/testing/PRD-140-tests.md`，不与后续 PRD 合并。PRD-147 提交后 G7 统一执行
0130..0139 数据库矩阵、跨域集成/FLOW、三浏览器三缩放、10 万目录性能、5 分钟 freshness、1 分钟 deadline、
长稳、断网/N/N-1、动态撤权、安全和敏感审计；本条专项通过不能替代 G7。

## 11. 预计改动边界

- `infra/migrations/0132_platform_customer_success_renewal.sql` 及 down。
- `control-plane-api/src/metahub_control/platform_customer_success/` 的 models/routes/service/projection/
  scheduler/outbox/permissions，以及 app、platform_access authority/navigation/workspace projection reader、
  customer_health legacy router。新 scheduler 原位替换旧 customer-health installer，不增加 supervisor installer 数量。
- `console-web/src/lib/platformCustomerSuccess.js`、`features/platform-customer-success/`、Platform page wiring/i18n/style，
  并停止企业 customer-health route/render/prefetch；PRD-147 再物理删除全部 legacy import/source。
- migration、API、scheduler、permission、Console 专项测试与 `docs/testing/PRD-140-tests.md`。

不修改 PRD-141 增长、PRD-142 品牌、PRD-143 Provider 工程、PRD-144 商业账、PRD-145 页面隔离、
PRD-146 最终 Workspace 导航或 PRD-147 统一注册表的业务范围。

## 12. 两轮编码前自审

### 第一轮：事实唯一性、owner 与隐私

- P0：只迁最新 score 会丢历史审计、source hash 和已投递/待投递状态。修正为迁移全部 score sequence、
  score outbox、Webhook 水位和 scheduler 行，保持稳定 ID/attempt/generation。
- P0：忽略 PRD-096 `workspace_health_projections` 会制造第三套健康摘要。修正为把它纳入盘点并逐 Workspace
  与最新 score 对账，目标 head 是唯一可写摘要。
- P0：只给健康 snapshot 加 tenant_id 无法证明对应客户 Workspace。修正为所有客户对象带不可变
  workspace/tenant 复合 owner 和复合 FK，缺失/漂移 binding 阻断整个客户迁移。
- P0：让 platform_support 因角色直接读联系人或工单正文会绕过 JIT。修正为独立 support_read exact DTO，
  只含安全计数；联系人只对 contact.read 单客户 detail 开放，正文仍要求 SupportGrant。
- P1：在 CustomerContact 复制 email/phone 会扩大 PII 持久面。修正为只保存 account/membership 引用和偏好布尔
  摘要，响应时按当前 revision 解析并审计，目录不批量返回 endpoint。
- P1：SuccessPlan/QBR 支持自由文本会把平台投影变成客户正文仓库。修正为固定 objective/outcome/action/reason、
  整数目标和日期，不提供 notes/description/custom fields。
- P1：五个 Excel API 中没有独立 plan/task/QBR GET，若只实现两个 POST，详情页无法恢复已完成事实。修正为
  单客户 `GET /platform/customer-accounts?view=detail` 返回四个严格有界集合，联系人仍走独立敏感 detail view。

第一轮修正后无未关闭 P0/P1。

### 第二轮：切换、并发、权限与 SLO

- P0：先开放新 API 再核对 legacy lease 会产生双 writer。修正为 quiesce、二次 hash/watermark、无 active lease、
  更高 fence generation 和一个 cutover CAS 后才挂载 Platform API。
- P0：rollback 重新挂载 `/customer-health` 会恢复已撤销的跨域能力。修正为 rollback 只切 target 内部兼容读，
  永不恢复旧 permission、Navigation、DOM、prefetch、API 或 scheduler。
- P0：批量任务逐条提交会留下半批并允许申请人自批。修正为最多 100 项 canonical manifest、独立 Owner step-up
  审批、事务内全量重验和全有或全无落库。
- P1：只按 role 裁剪但 cursor/total 在裁剪前计算会泄漏客户规模。修正为权限集合驱动 exact DTO，filter、count
  和 cursor 全部在可见投影上生成并绑定权限指纹。
- P1：任务 timer 不绑定 deadline revision 会对已改期任务发旧事件。修正为 due generation、唯一 event key、
  CAS 和旧 worker fence。
- P1：PRD-086 v1 的 ticket unavailable 若直接沿用，会浪费已交付 PRD-132 且无法解释支持风险。修正为历史 v1
  不重算，新 v2 只消费 owner-qualified 安全计数并冻结 1000 权重和五档规则。
- P1：QbrSummary 只有表而没有生成/重放合同会成为永远为空的名义对象。修正为同一 scheduler 内的季度 job、
  account-quarter 唯一键、source watermark/digest 和 append-only superseding 语义。
- P1：为 PRD-140 新增独立 installer 会把批次审计确认的 supervisor 数量再扩大。修正为用 customer-success
  installer 原位替换旧 customer-health installer，六个内部 job 共享持久状态但各自独立 lease/fence/watermark。
- P1：仅写“5 分钟 freshness/1 分钟 drift”没有可执行 worker 合同。修正为 1 分钟 projection/health/renewal
  claim、15 秒 task scan、bounded batch、持久 watermark/lease/fence 和 G7 p95/p99/资源证据。
- P1：继续按条件分支加载每张平台权限表会漏掉 0132 或未来 S7 权限。修正为固定 allowlist tuple 顺序、固定 SQL
  模板、表内容验证和 authority fingerprint；禁止 schema 扫描或用户输入表名。

第二轮修正后无未关闭 P0/P1。G6 已通过；0132 与 PRD-140 实现、专项验收和敏感审计均已完成，下一连续编码项为 PRD-141。
