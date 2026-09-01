# PRD-141 SaaS 平台增长漏斗、活动与转化归因

状态：已完成。0133、Platform Growth 工作台、增长 API/导出合同、旧 Product Analytics 退役链、专项测试、
浏览器验收、敏感审计和路线图写回均已交付。依赖 PRD-085/092/096/097/099/105/107/110/134/139/140；
批次边界见 `S7-platform-operations-console-domain-batch-design.md`。本条只交付 0133、Platform Growth
工作台、Excel 五组 API、四个事件、受控导出辅助合同和旧产品分析切换，不提前实现 PRD-142..148。

## 1. 产品闭环与非目标

平台增长人员通过现有 `platform.html#growth` 进入独立工作台，查看版本化注册、激活、试用、付费和
留存漏斗，比较同期群，管理获客活动与增长实验，并查询渠道归因和单位经济安全摘要。渠道人员只读
渠道归因聚合；客户成功人员只读采用和留存聚合；平台 Owner 审批敏感导出。所有企业、伙伴、Support、
Device 和 Bearer 身份均不能进入 `/platform/growth/*`。

Excel 权威分析合同严格限定为五组 route pattern、七个 method：

- `GET /platform/growth/funnels`
- `GET /platform/growth/cohorts`
- `GET/POST /platform/growth/campaigns`
- `GET/POST /platform/growth/experiments`
- `GET /platform/growth/attribution`

Excel 同时要求 `GrowthExport`、`growth.exported`、平台 Owner 审批、删除策略和脱敏导出，却没有给出创建、
审批、状态或下载入口。现有 Platform Jobs 只有队列/详情/重试/取消和 job type 管理，通用 Workspace
审批也不能接受 PlatformSession；把导出塞进 GET query、活动 POST 或实验 POST 会破坏权限、幂等和审计。
因此 0133 仅补充第 7 节的窄作用域导出辅助路由。它们不形成通用报表系统，也不扩大上述五组分析查询。

本条不提供任意 SQL、事件浏览器、Workspace/账号枚举、Prompt/Response、请求日志正文、联系人、邮件、
IP、设备指纹、Provider Secret、原始 UTM、任意 metadata、自由分群表达式或直接 PII 导出。接入步骤仍由
PRD-097 初始化向导承接；既有 `model_experiments` 是企业 Gateway 模型路由实验，保持其 tenant owner、API、
执行和审计，不迁移、不复制，也不自动解释为 `GrowthExperiment`。

旧企业 `growth-analytics` 从本条切换后不再出现在 Workspace Navigation、DOM、prefetch 或可调用 API 中。
`GET /product-analytics/funnel` 从外部 router 卸载并返回普通统一 404，不返回 retired/tombstone 提示；
`product_analytics.read` 从 Workspace 权限目录和默认/自定义角色中精确收缩。旧静态代码物理删除由 PRD-147
收口，但任何 rollback 都不得重新挂载旧权限、route、DOM 或预取。

## 2. 身份、owner 与权限

### 2.1 来源 owner 与 PlatformSession

`ProductEvent` 继续由原 tenant 业务动作在同事务写入 `product_events`，保持不可变 `tenant_id`、`pev_*`、
source identity、发生时间和七类 exact event；0133 不复制一张“平台事件表”，也不允许 Platform API 读取
单行 ProductEvent。每个可聚合来源先通过 active `WorkspaceTenantBinding` 映射不可变
`workspace_id + tenant_id`，再校验主体、同意、来源 revision 和用途。映射缺失、重复或漂移时该来源失败关闭，
不能猜 Workspace、合并客户或落入 unknown customer。

Platform API 只接受有效 `PlatformSession` Cookie。每个请求重新恢复 Operator status/revision、固定角色、
全部扩展权限和权限指纹；WorkspaceSession、GlobalSession、SupportSession、ChannelMembership、Bearer、query
凭据、重复 Cookie、错 Origin 和混合域 Cookie 全部拒绝。读取聚合不创建 WorkspaceMembership；Platform
角色不能借增长页面进入企业 API 或读取客户正文。

### 2.2 固定权限与响应裁剪

复用现有基础权限 `platform.growth.read`，0133 新增不可变
`platform_growth_permission_extensions` 与显式角色映射：

| Permission | owner | growth | channel | customer_success |
| --- | --- | --- | --- | --- |
| `platform.growth.read` | 允许 | 允许 | 否 | 否 |
| `platform.growth.adoption_read` | 允许 | 允许 | 否 | 允许 |
| `platform.growth.channel_attribution_read` | 允许 | 允许 | 允许 | 否 |
| `platform.growth.campaign.manage` | 允许 | 允许 | 否 | 否 |
| `platform.growth.experiment.manage` | 允许 | 允许 | 否 | 否 |
| `platform.growth.export.request` | 允许 | 允许 | 否 | 否 |
| `platform.growth.export.approve` | 允许 | 否 | 否 | 否 |
| `platform.growth.export.download` | 允许 | 允许 | 否 | 否 |

表中 owner/growth/channel/customer_success 分别指 `platform_owner`、`platform_growth`、`platform_channel`、
`platform_customer_success`。Owner 权限也必须在 extension 表显式映射，不能依赖 0087 中“Owner 拥有当时全部
权限”的快照自动覆盖未来权限。申请人和审批人必须不同；同一 Owner 可以申请，但必须由另一位当前 Owner
审批。多角色按权限并集授权，独立审批约束不因并集放宽。

`platform_growth` 可见所有脱敏聚合、活动和增长实验；`platform_channel` 只能调用 attribution 的
`view=channel_summary`，DTO 不含 Workspace、账号、实验或单位经济；`platform_customer_success` 只能调用
funnels/cohorts 的 `view=adoption|retention`，且只返回达到最小样本的 band。Owner 可读安全聚合并审批导出，
但审批详情不包含制品内容。企业和伙伴角色即使字符串同名也不进入 Platform 权限解析。

`platform_access.service._authority` 在加入 0133 表前使用批次冻结的
`_PERMISSION_EXTENSION_TABLES` 固定 tuple 和固定 SQL 模板；不得扫描 schema 或拼接用户输入表名。权限指纹
覆盖角色、Operator revision、所有 extension 结果和权限集合，撤权后下一请求和任何迟到响应立即失效。

## 3. 0133 数据模型与唯一事实

Excel 对象映射如下：AcquisitionSource -> `platform_acquisition_sources`，GrowthFunnelSnapshot ->
`platform_growth_funnel_snapshots`，CohortSnapshot -> `platform_growth_cohort_snapshots`，Campaign ->
`platform_growth_campaigns`，GrowthExperiment -> `platform_growth_experiments`，ConversionAttribution ->
`platform_conversion_attributions`，GrowthExport -> `platform_growth_exports`。辅助 eligibility、bucket、command、
approval、mapping、event、outbox 和 scheduler 表只承担可复算、并发与恢复，不成为第二套来源真值。

### 3.1 ProductEvent、同意与可聚合资格

- `product_events` 与 `product_event_outbox` 原位保留，仍是七类激活事实及 `product_event.recorded` 投递的唯一
  source/outbox。所有 `pev_*`、`pevo_*`、稳定 Webhook event ID、capture mode、pending/leased/sent/failed、
  attempt、subscription snapshot、lease generation 和水位逐位保真；sent 不重投，failed 不复活，未知 leased
  结果不能当成功。
- `platform_growth_event_eligibility` 每个 source event/version 至多一行，只保存 ProductEvent 引用、复合
  Workspace owner、主体不可逆 token/key version、consent record/preference revision、固定 purpose、
  `eligible|excluded|withdrawn|erased`、固定 reason、evaluated UTC 和 canonical digest。它不复制 event type、
  actor、source ID 或正文；读取时仍回到不可变来源并重验 hash。
- 有主体的事件仅在事件发生时最新 `analytics` ConsentRecord 为 `granted`、当前 owner-qualified
  `privacy_preferences.analytics_enabled=1`、账号/Membership 有效且用途版本兼容时 eligible。历史时点无可证明
  consent、只有当前布尔值、主体映射不唯一或条款用途漂移均不得追认。
- 无 actor 的事件只有在 source business fact 能稳定链接 registration intent、GlobalAccount、Workspace owner 和
  事件时点 consent 时才可进入 v2；仅有 tenant/source code 不足以证明主体。无法证明的 legacy/backfill 行保留
  在 ProductEvent 历史和旧 v1 对账中，但不进入个体归因、实验或高基数导出。可在
  `legacy_operational_v1` 视图中仅以全平台、无维度、k 匿名聚合展示，并明确标记 consent coverage/incomplete；
  不得用当前同意反向漂白历史。
- Consent withdrawn、preference 关闭、Membership 撤销或 DSR erase 会立即阻止新消费和实验 assignment，
  将可逆主体 token crypto-shred，生成 correction generation 并 supersede 受影响的未冻结 snapshot；已达到
  k 匿名且不可反推个体的历史聚合按 retention policy 保留，不能 UPDATE 伪造原历史。

### 3.2 获客来源、漏斗与同期群

- `platform_acquisition_sources` 使用稳定 `acs_*`，保存固定 source kind
  `direct|organic|paid|partner|referral|sales|unknown`、可选 PRD-139 `channel_partner_id` /
  `channel_referral_code_id` 引用、平台自有 campaign 引用、验证状态、有效期、revision 和 canonical hash。原始
  query/UTM、落地 URL、email、IP 和 arbitrary
  metadata 不持久化；受控外部 source code 先规范化并 HMAC 后匹配目录，未知值只落 `unknown` 计数。
- 漏斗 v2 固定阶段为 `registered -> activated -> trial_started -> paid -> retained_30d`。registered/activated
  从 PRD-085 七阶段按公式版本折叠；trial/paid 只读 PRD-098..105 订阅/订单/支付终态；retained_30d 只读
  PRD-096/140 采用与健康安全投影。失败支付、取消订单和请求失败不能冒充转化。每个来源仅有一个 writable
  owner，0133 只保存引用、整数计数和 source watermark/hash。
- `platform_growth_funnel_snapshots` 按 UTC day、固定 view、可选低基数 source/channel/region/plan band、
  formula version 和 generation 保存每阶段 eligible subject/workspace count、相邻转化 basis points、收入/成本
  最小货币单位的同币种安全摘要、coverage、k 状态和来源水位。百分比只用整数 basis points 和 round-half-up；
  不跨币种相加，不接受运行时 JSON 维度或全表排序。
- `platform_growth_cohort_snapshots` 按 `signup_week|activation_week|first_paid_month|campaign`、UTC cohort bucket、
  age bucket、同一低基数维度集、eligible/retained/churned count、retention basis points、coverage、formula version、
  generation 和 source digest 保存 append-only 行。迟到来源产生 superseding generation；相同 input digest 重跑
  返回原 snapshot，不修改历史。
- 任一公开 cell 的独立 Workspace/主体数必须 `k>=20`；不足时只返回 `suppressed=true` 和 suppression code，
  不返回可通过相减恢复的小计。相邻/总计/筛选组合共同通过差分查询预算和 complementary suppression；
  platform_channel/customer_success 的阈值固定更高 `k>=50`。

### 3.3 Campaign 与 GrowthExperiment

- `platform_growth_campaigns` 使用稳定 `gca_*`，保存受控 display name、source、channel code、目标 funnel stage、
  UTC 起止、预算整数/币种、固定 status `draft|scheduled|active|paused|completed|archived`、revision 和 source
  digest。活动是平台营销配置，不写客户备注、受众名单、联系地址、任意 tag 或落地页正文。
- `platform_growth_experiments` 使用稳定 `gex_*`，只描述 SaaS 注册/激活/采用界面的增长实验：固定 hypothesis
  code、surface code、primary metric、control/variant code、整数 allocation bps、eligibility policy version、
  consent purpose/version、sample cap、UTC 窗口、guardrail、decision policy、status/revision 和 canonical hash。
  它不引用 Provider/model/routing policy，也不写自由脚本、Prompt、Response 或前端任意代码。
- `platform_growth_experiment_assignments` 是内部不可枚举事实，保存 experiment、复合 owner、可销毁主体 token、
  deterministic bucket/key version、variant、consent basis、assigned/withdrawn UTC 和 digest。分流使用服务端 HMAC
  对 `(experiment_id, account_id, workspace_id)` 取稳定 bucket；客户端参数、Cookie flag 或 campaign code 不能
  选择 variant。一个主体同实验只分配一次，退出后不得重新入组。
- 实验只能在明确 consent、已发布 surface、样本上限和完整 guardrail 下由 draft -> scheduled -> running。
  `pause|stop|decide|archive` 使用 CAS。隐私撤回、SRM、错误率/延迟/安全 guardrail、source freshness 超限会自动
  stop；安全 stop 不等待显著性。效果判断采用预注册固定 horizon 和 golden-vector 验证的统计实现，中途查看
  不可提前宣布 winner；终态仅 `winner_control|winner_variant|inconclusive|stopped_guardrail|canceled`。
- PRD-107 feature rollout 可以承接已批准 winner 的发布，但 GrowthExperiment 自身不能直接修改生产 flag；
  发布必须创建独立 rollout 命令、审批和审计。重复 decide 返回同一终态，任何 source/consent/metric digest
  改变都要求新 experiment version，不能重算成更有利结论。

### 3.4 归因、导出、事件与 Scheduler

- `platform_conversion_attributions` 使用稳定 `cat_*`，每个合格 conversion + formula version 最多一行，保存
  conversion source 引用、复合 owner、可销毁主体 token、AcquisitionSource/Campaign/Partner 引用、固定
  `last_eligible_touch_30d_v1`、lookback、整数 value/currency、consent basis、source digest 和 occurred UTC。
  touch 必须先于 conversion、同主体、同 Workspace 且在 30 日内；无合格 touch 明确归为 direct/unknown，
  不跨 Workspace、账号、币种或撤回边界拼接。
- `platform_growth_exports` 只保存 request/query canonical hash、schema/formula version、sensitivity、status、
  row/cell count、encrypted artifact digest/key reference、expiry、requester/approver revision、download count 和
  UTC；`platform_growth_export_subjects` 仅为高基数制品保存 owner-qualified keyed digest 与销毁状态，支持 DSR
  失效，绝不保存 email、账号显示信息或明文主体 ID。制品存在私有对象存储，数据库不保存 CSV/JSON 正文。
- `platform_growth_commands` 绑定 Operator、action、对象、Idempotency-Key HMAC、canonical request hash、
  expected/result revision、request/trace 和 UTC；同键同体返回原结果，同键异体 409。export approval/receipt
  使用独立表和 generation，申请人、审批人、下载人、权限 revision 与 one-shot step-up 均可核验。
- `platform_growth_events` 与 `platform_growth_event_outbox` 同事务写四个 Excel exact 事件。payload 只含安全
  object ID、固定 kind/status、revision/generation、计数、formula/hash 和 UTC，不含 Workspace、账号、主体 token、
  query、活动名称、artifact URL 或财务明细。outbox 使用稳定 event ID、subscription snapshot、lease token
  digest、generation、attempt 和持久水位，不信任 worker 传入 envelope。
- `platform_growth_scheduler_state` 固定 job code：`source_eligibility`、`daily_funnel`、`cohort_projection`、
  `attribution`、`experiment_guardrail`、`export_build`、`privacy_correction`、`growth_event_outbox`、
  `product_event_outbox`。每项保存 persistent lease、fencing generation/token digest、bounded cursor、来源水位、
  next run 和失败计数。0133 用一个 `install_platform_growth_scheduler` 替换现有
  `install_product_event_scheduler` supervisor 槽位，不增加 installer 数量；PRD-147 再纳入统一注册表。

## 4. PRD-085 迁移、切换与回滚

0133 使用 expand/reconcile/quiesce/cutover/contract。ProductEvent 原位成为平台增长的受控来源，不通过长期
dual-write 建第二张 source 表。

### 4.1 Expand 与盘点

1. 只接受 current head 0132。先建立 0133 目标投影、权限、eligibility/mapping/cursor、command/event/outbox、
   scheduler fence 和 down guard，不改已有 source 行。
2. 盘点 `product_events`、`product_event_outbox`、七类 event、capture mode、逐 tenant 行数/hash、最大 UTC/ID、
   pending/leased/sent/failed、attempt/generation、subscription snapshot、Webhook delivery/dead-letter 和当前
   scheduler 活跃 lease。保存 canonical manifest，不读取 Webhook secret 或 payload 正文。
3. 每个 tenant 唯一映射 active `WorkspaceTenantBinding`；actor 必须唯一映射 GlobalAccount/Membership。无 owner、
   多 owner、event source/hash 漂移或 consent 链不完整均写固定 conflict code 并阻断相关 projection；不能因
   其他 tenant 合法而把冲突 tenant 当零数据。
4. 精确盘点 Workspace `product_analytics.read` 模板、默认/自定义角色、Navigation、旧 route/import/DOM/prefetch
   和权限指纹；同时盘点 PRD-139 channel attribution、PRD-140 adoption/retention、订阅/订单/支付与单位经济来源
   的 revision/hash 水位。

### 4.2 可重放资格与投影回填

1. 保留全部 legacy ProductEvent/outbox 原值。按 event occurred UTC 恢复当时最新 analytics ConsentRecord，
   再验证当前 privacy preference；只为可证明来源写 eligibility。无证明者保留 excluded reason，不删除 source，
   不生成主体归因或实验 assignment。
2. 原 PRD-085 七阶段逐 tenant 复算必须与旧 API 算法的 count、first/last、cumulative 和 basis points 完全一致，
   形成 `legacy_activation_v1` reconciliation；平台 v2 只在该证明后折叠 registered/activated，并明确 coverage。
3. PRD-139 referral/partner、PRD-140 adoption、billing/payment 和 ProductEvent 各自仍是唯一 writer。0133 仅保存
   stable reference、公式版本、计数和水位；任何 source hash 倒退、owner 不一致或额外 target 行阻断 generation。
4. source outbox sent/failed 原终态不变；pending 继续由新 supervisor 内同一个 product event worker 处理；leased
   等旧 lease 完成/过期后由更高 fence 接管。迁移不生成第二个 `product_event.recorded`，不更改 stable event ID。
5. 相同 migration generation 重放只校验并返回同一 mapping/snapshot；同 source ID 不同 hash、相同 bucket 不同
   formula、目标额外行、cell suppression 漂移或 consent decision 倒退均失败关闭。

### 4.3 Quiesce 与原子切换

1. fence 旧 product event scheduler 新 claim，等待活跃 lease 完成/过期，再记录 source/outbox/Webhook 水位。
   source 业务写可继续；若切换窗口出现新 ProductEvent，新 worker 先追平 cursor 后才允许 cutover。
2. cutover CAS 验证 ProductEvent 行/hash 未改、outbox 状态单调、v1 复算、v2 source coverage、consent conflicts、
   新 scheduler generation、所有公开 cell suppression 和 SLO readiness。任一差异回到 reconcile。
3. 新 growth scheduler 成为唯一 product event outbox consumer 和增长投影 writer；source trigger 仍由原业务动作
   原子写 ProductEvent。禁止为平台页面另写 event、从 Webhook 回灌或由浏览器补记转化。
4. 数据 CAS 后同一发布卸载 `/product-analytics/funnel`，移除 Workspace `growth-analytics` Navigation 和
   `product_analytics.read`，使旧 hash、DOM、prefetch、client/import 全部不可运行。Platform Growth 只在 target
   reconciliation 通过后开放；外部旧路由固定普通 404。
5. 切换 manifest、source/target hash、权限 catalog diff、route inventory 和 scheduler fence 写不可变平台审计；
   审计不包含事件主体、原始来源、query 或制品内容。

### 4.4 Down 与运行回滚

0133 down 只允许从未写入任何 acquisition、snapshot、campaign、experiment、assignment、attribution、export、
command、approval、event/outbox、eligibility、mapping/cursor 且 scheduler pristine 的空数据库执行。任一回填或
source cursor 都是目标事实，down 在第一个 DDL 前原子拒绝；空库验证 `0132 -> 0133 -> 0132 -> 0133`。

允许的空库 down 删除 extension permission 后必须使旧 PlatformSession 指纹失效，但不能修改 0078 ProductEvent、
outbox、trigger、Webhook 或 Workspace RBAC。生产 rollback 是有 lease/checkpoint/补偿/审计的内部 job：可暂停新
projection 并恢复上一版目标 reader，绝不能重新挂载 `/product-analytics/funnel`、`product_analytics.read`、
Workspace DOM/prefetch 或第二个 product event scheduler。目标不可用时 Platform Growth 显示 dependency
unavailable，不以旧企业跨域能力兜底。

## 5. 业务口径、并发与事件语义

### 5.1 转化与聚合

唯一主体在同一 formula/window/stage 只计一次；重复 ProductEvent、source retry 或同一订单多次回调不能增加
转换人数。事件数和主体数分别保存，API 默认返回主体数。stage 必须按发生 UTC 单调满足前序才算 cumulative；
乱序事件可以在独立 stage 计数，但不越过缺口计算漏斗转化。所有除法使用整数分子/分母、round-half-up，
denominator=0 返回 null，不返回 NaN/Infinity。

日增量窗口以 UTC `[day, day+1)` 固定，迟到 7 日内进入新 generation；超过 7 日写 late correction 并只重算
受影响 bucket。查询 snapshot 不运行全表聚合。单位经济只在同币种、同 formula、达到 k 阈值时返回整数
revenue/cost/contribution；缺汇率不换算，Finance 源不可用时返回 partial + source freshness，而不是零成本。

首次 eligible conversion 写 `growth.conversion_recorded`。同 source digest 重放不发；归因模型改变产生新
generation 但不伪造第二个 conversion。事件只含 conversion ID、stage、formula、revision 和 UTC。

### 5.2 Campaign 与 Experiment 状态机

Campaign 的合法转换为 `draft -> scheduled -> active -> paused|completed -> archived`；scheduled 可在开始前回
draft，paused 可恢复 active，completed/archived 不可恢复。create/update/schedule/pause/resume/complete/archive
都用幂等键、quoted If-Match 和同事务事件；公开可观察配置 digest 改变写 `growth.campaign_changed`，仅 updated_at
变化不写。

Experiment 的合法转换为 `draft -> scheduled -> running -> paused -> running`，以及 running/paused 到
`decided|stopped|canceled`，终态只可 archive。start 时重验 consent policy、surface、allocation 总和=10000、
metric/guardrail 和 sample cap；stop/decide 事务用 scheduler fence 与 revision CAS 阻止人工决定和 guardrail
竞态。只有首次终态决定写 `growth.experiment_decided`；payload 不含效果细分或主体。

### 5.3 Export 状态机

Export 为 `requested -> policy_review -> approved -> building -> ready -> downloaded|expired|revoked|failed`；
低基数 aggregate 可由固定 policy 在同事务标记 approved_by_policy，高基数一律需独立 Owner approve。任何直接
PII、k 不足、跨用途、跨币种混合或自由字段请求在 requested 前拒绝。approve 时重新验证申请人与审批人、两人的
active revision、query hash、schema、consent coverage、retention 和当前 step-up。

builder 只消费冻结 query manifest，写加密制品后复算 row/cell count、schema/hash 和敏感扫描。ready 制品 TTL
24 小时；一次性 receipt TTL 5 分钟，只返回给原 requester，服务端仅存 HMAC。成功下载在一个事务消费 receipt、
把 download_count 从 0 改 1、写 `growth.exported` 和审计；网络结果未知时 receipt 保持已消费，不允许重放，
用户必须申请新 export。过期、撤权、withdraw/erase 命中 subject manifest 或权限指纹改变会 crypto-shred 并
转 revoked/expired，迟到 builder 受 fence 阻止发布。

## 6. Excel 五组 API exact 合同

所有模型 `extra=forbid`，未知 query/body/header、重复标量 query、数组顺序漂移和非法 UTF-8 均返回固定 422。
所有响应带 request/trace、formula/schema version、source freshness、generated UTC 和 `Cache-Control: private,
no-store`；cursor 是 HMAC 绑定 view/filter/formula/权限指纹的 opaque token，不能被改写成跨角色查询。

### 6.1 `GET /platform/growth/funnels`

允许 `view=overview|adoption|retention`、`from/to` UTC day（最多 366 日）、`bucket=day|week|month`、单一
`dimension=none|source|channel|region|plan_band`、固定 dimension value、formula version 和 cursor/limit<=200。
growth/owner 可读 overview；customer_success 只读 adoption/retention 且不能选 source/channel/单位经济。
响应为 bucket、五阶段计数/bps、coverage、suppression、同币种单位经济和 source watermarks；无 Workspace、
tenant、账号、event、campaign name 或 subject token。

### 6.2 `GET /platform/growth/cohorts`

允许 `view=adoption|retention`、cohort kind、UTC range<=730 日、age buckets<=24、一个低基数 dimension、
formula 和 cursor。响应按 cohort/bucket 返回 eligible/retained/churned 整数、retention bps、coverage、suppression
和 superseding generation。禁止用任意字段表达式、多个维度或相减查询绕过 k 阈值。

### 6.3 `GET/POST /platform/growth/campaigns`

GET 仅 growth/owner，允许 status/source/channel、UTC range、cursor/limit<=100，返回活动安全配置、revision、
聚合 conversion/spend/outcome 和 action capabilities。POST 仅 campaign.manage，body 是 exact discriminated command
`create|update|schedule|pause|resume|complete|archive`；除 create 外必须包含 campaign ID、quoted If-Match，所有
命令要求 exact Origin/CSRF、one-shot step-up 和 Idempotency-Key。服务端忽略不了的未知字段必须拒绝。

### 6.4 `GET/POST /platform/growth/experiments`

GET 仅 growth/owner，允许 status/surface/metric、UTC range、cursor/limit<=100，返回预注册配置、样本计数、
guardrail、source freshness、decision 和 action capabilities，不返回 assignment 或主体。POST 仅
experiment.manage，exact command 为 `create|update|schedule|start|pause|resume|stop|decide|archive`；start/decide
额外要求来源水位和 consent policy 未漂移。服务端计算 bucket 和统计结果，客户端不能上传样本、效果或 winner。

### 6.5 `GET /platform/growth/attribution`

growth/owner 可用 `view=summary|campaign|channel`；channel 角色只能固定 `view=channel_summary`。允许 UTC range<=366
日、conversion stage、attribution formula、固定 source/channel/campaign ID（channel 角色不能指定 campaign）、
cursor/limit<=200。响应只含达到 k 的 source/campaign/channel 安全 ID、conversion/value/cost 整数、bps、coverage、
suppression 和水位；channel DTO 不含 campaign 配置、Workspace、plan、单位经济或其他渠道的可相减总计。

## 7. GrowthExport 辅助合同

为使 Excel 明确列出的 GrowthExport 和 `growth.exported` 可达，新增且只新增：

- `POST /platform/growth/exports`：冻结一个由第 6 节 query schema 派生的 export manifest，返回 202 和 export ID。
- `GET /platform/growth/exports/{id}`：仅 requester、具有 export.approve 的 Owner 可读最小状态；审批人看不到制品。
- `POST /platform/growth/exports/{id}/approvals`：独立 Owner exact `approve|reject`，要求 If-Match、幂等和 step-up。
- `POST /platform/growth/exports/{id}/receipts`：ready 后仅原 requester申请 5 分钟一次性下载 receipt。
- `GET /platform/growth/exports/{id}/download`：只从单个 `X-Growth-Export-Receipt` header 接收 receipt，成功即消费。

这些 route 不接受 workspace/account/subject ID、自由 query、下载 URL、query receipt、Bearer 或跨域 Cookie。
request/approve/receipt 都拒绝 body 外字段，下载返回 `Content-Disposition: attachment`、精确 MIME、`nosniff`、
`Cache-Control: private, no-store, max-age=0` 和 `Pragma: no-cache`，不重定向对象存储。低风险 policy approval 也
经过同一状态机；高基数导出不能通过拆分多个低风险请求绕过 cumulative privacy budget。

## 8. Scheduler、事件与 SLO

增长 supervisor 每轮只处理 bounded batch，所有 claim/complete/watermark 均验证持久 lease generation/token。
source eligibility 与 ProductEvent outbox 优先，daily/cohort/attribution 按来源 watermark 增量；实验 guardrail
至少每分钟运行，export/privacy correction 可恢复。失租 worker 停止提交，接管从 checkpoint 继续；停服等待当前
小批次，不清空队列或重置 cursor。

四个事件语义固定：

- `growth.conversion_recorded`：首次 eligible conversion 事实提交。
- `growth.campaign_changed`：Campaign 可观察配置或状态 revision 改变。
- `growth.experiment_decided`：首次实验终态决定提交。
- `growth.exported`：一次性 receipt 被成功原子消费，不在 requested/ready 时提前写。

SLO 以专项证据证明：聚合查询 p95 < 1 秒；已提交且 eligible 的日增量在 10 分钟内进入最新可读 snapshot；
固定 100 万 ProductEvent/业务 source fixture 在受控资源下全量重算 < 60 分钟。测量排除测试数据准备但包含
eligibility、consent、k suppression、五阶段聚合、attribution、snapshot commit 和 reconciliation；不能只测空库
或缓存命中。source 不可用时返回 bounded stale/partial 和 freshness，不超时扫描原表。

## 9. Platform Console 与旧入口归零

Platform Growth 页面使用独立 `platformGrowth` client/store/AbortController/navigation epoch，不能复用 Workspace
`productAnalyticsPageState`、Cookie、DTO 或缓存。页面包含紧凑的漏斗、同期群、归因、活动、实验和导出任务视图，
固定 loading、empty、partial、suppressed、stale、permission revoked、conflict、dependency unavailable、approval
pending、export expired/revoked 状态。写按钮只按 API action capability 启用，撤权/离页/切 role 后清空数据、
step-up、receipt 和迟到响应。

390/1280/1440、DPR 1/1.25/1.5 下无重叠、裁剪或横向溢出；表格小屏转为可扫描行，不缩放字体。键盘、焦点、
读屏、错误摘要、图表等价文本、直接 URL 和浏览器后退均可用。页面不显示“功能说明/快捷键”宣传文案，导出
按钮不把 query 或 receipt 写入 DOM、URL、localStorage、日志或截图证据。

PRD-141 切换同时使 Workspace 侧 `growth-analytics` 不下发、不 import、不 render、不 prefetch；
`renderDeveloperStart` 不能再内嵌旧组件，`customerHealth` 的 restore target 也不能指向已退休入口。PRD-147 做物理
删除和全批次静态归零，但 PRD-141 起真实运行已经 ordinary 404，不能留到批次末才关闭跨域能力。

## 10. 隐私、审计与失败恢复

- 所有聚合、实验、归因和 export 在 source consumption、assignment、guardrail、build、receipt 五处重验
  consent/privacy revision；撤回不是仅隐藏 UI。PRD-134 DSR adapter 能定位资格、assignment、attribution 和未过期 export，执行
  exclude/anonymize/crypto-shred，并记录固定 owner receipt。
- ProductEvent、eligibility、snapshot、API、event/outbox、artifact、日志、测试 fixture 和正式证据均扫描敏感键和值。
  禁止项包括 email/phone/IP/UA、Token/Cookie/Authorization、Prompt/Response、原始 source/UTM、URL、内部备注、
  account/workspace display、subject token、下载 receipt 和加密 key。
- 普通 aggregate 最长保留 25 个月，assignment/attribution 可逆主体 link 最长 90 日，ready artifact 最长 24 小时，
  receipt 5 分钟；Legal Hold 只能保全不可变审计/hash，不能延长制品 URL、扩大读取或阻止 consent 停止处理。
- 审计记录 operator、action、object、permission/step-up revision、request digest、before/after revision、approval、
  outcome、request/trace 和 UTC；不记录 body、query 明文、artifact、主体或错误异常字符串。
- 数据库、KMS、对象存储、source 或 worker 故障均失败关闭。命令事务不产生部分 Campaign/Experiment/Event；
  builder 未知结果不能 ready；download 未知结果不能重复 receipt；correction 失败时标记 affected snapshot stale 并
  阻止 export，而不是继续提供可能含已撤回主体的制品。

## 11. 聚焦测试与批次证据

G6 通过后 PRD-141 开发阶段只执行受影响测试：

- Migration：0132/0133 fresh、空库 down/re-up、任一目标/eligibility/cursor/export/event/outbox/scheduler 事实阻断、
  ProductEvent/outbox 逐行 hash 与状态保真、v1 漏斗复算、权限精确收缩、FK/integrity。
- API/RBAC：四个 Platform 角色 exact 正/负矩阵，所有企业/伙伴/Support/Global/Bearer 拒绝，动态撤权、错域
  Cookie、Origin/CSRF、step-up、幂等、If-Match、未知字段/query、cursor 权限绑定和 ordinary 404。
- Aggregate：七类 source、乱序/重复、五阶段折叠、迟到 correction、basis points、同币种单位经济、k/complementary
  suppression、channel/customer-success field clipping、source stale/partial 和可复算 hash。
- Consent/DSR：事件时点 granted/denied/withdrawn、当前 preference、无 actor/无历史证明、Membership 撤销、
  experiment opt-out、assignment 稳定性、erase/crypto-shred、导出失效与迟到 builder fence。
- Campaign/Experiment：完整状态机、CAS 竞争、固定 allocation、SRM/guardrail stop、固定 horizon、重复 decide、
  与 tenant model experiments 隔离、winner 不能直接改 feature flag。
- Export：aggregate/high-cardinality/PII 拒绝、独立审批、同人拒绝、权限漂移、build 接管、artifact hash/schema/
  敏感扫描、24h/5m TTL、单次 receipt、下载未知结果、no-store 和 `growth.exported` 精确一次。
- Scheduler/Event：替换 installer 不增加 supervisor 数量、九 job lease/fence/checkpoint、product outbox 水位、
  四事件 exact payload、稳定 ID、崩溃接管和迟到 worker 拒绝。
- Console：strict client、角色裁剪、epoch/Abort/迟到 200/401/403、旧 Workspace DOM/prefetch/API 归零；真实浏览器
  和完整性能/安全/回归留到 PRD-147 后统一 G7。

测试记录写 `docs/testing/PRD-141-tests.md`，正式 JSON/截图/manifest/SHA-256 只进入
`.artifacts/product-qa` 或对应 PRD artifact 目录，`.tmp` 不作为交付证据。敏感审计必须同时覆盖 staged diff、
测试输出、API body/header allowlist 和生成制品；任何明文命中阻断独立提交。

## 12. 预计改动边界

- `infra/migrations/0133_platform_growth_funnel_campaign_attribution.sql` 与 `.down.sql`
- `control-plane-api/src/metahub_control/platform_growth/`
- `control-plane-api/src/metahub_control/product_analytics/` 的 source/outbox owner 迁接，以及 `app.py`
- `control-plane-api/src/metahub_control/platform_access/`、`auth/`、`console/`
- PRD-134 privacy owner adapter、共享 error/webhook exact catalog
- `console-web/src/lib/platformGrowth.js`、`src/features/platform-growth/`、Platform renderer/store
- Workspace 旧 product analytics route/client/import/DOM/prefetch 的运行时卸载
- 对应 migration、Control、Console、privacy、scheduler 和 focused performance 测试
- `docs/testing/PRD-141-tests.md`、目录说明与批次证据索引

本条不修改 0078/0126 已应用 migration，不迁移或删除 tenant model experiments，不实现 PRD-142 品牌、PRD-143
Provider 工程、PRD-144 商业账、PRD-145 页面分域、PRD-146 Workspace 导航合同或 PRD-147 统一 scheduler 注册表。

## 13. 两轮编码前自审

### 第一轮：来源唯一性、同意与实验边界

发现并修正：

1. P0：初稿准备把 ProductEvent 复制到 platform growth event 表，会形成第二套 source、破坏 `pev_*`/outbox
   水位和 Webhook 稳定 ID。修正为 `product_events/product_event_outbox` 原位保留唯一事实，0133 只写 eligibility、
   aggregation 和 attribution reference；新 supervisor 接管同一 outbox worker。
2. P0：初稿用当前 `analytics_enabled` 追认全部 PRD-085 历史，会把事后 consent 当成事件时点 consent。修正为
   必须有 occurred_at 前最新 granted ConsentRecord 且当前 preference 仍有效；无历史证明只保留
   `legacy_operational_v1` 不可识别聚合，禁止个体归因、实验和高基数导出。
3. P0：初稿把既有 `model_experiments` 当 GrowthExperiment 来源，会让平台增长角色控制企业 Gateway 路由。
   修正为两个对象、owner、权限、API 和事件完全隔离；增长 winner 只能申请 PRD-107 rollout，不能直改生产。
4. P1：初稿按 tenant_id 聚合但未要求 WorkspaceTenantBinding 唯一，可能把迁移/重建 tenant 合并为同客户。
   修正为所有资格、归因和 source adapter 使用不可变 workspace_id + tenant_id 复合 owner，映射冲突失败关闭。
5. P1：初稿只在导出时检查 consent，running experiment 在撤回后仍会分流。修正为 source consumption、assignment、
   guardrail、build 和 receipt 五处重验，withdraw 立即退出且不能重新入组。

结论：第一轮 P0=0（3 项已关闭），P1=0（2 项已关闭）。继续第二轮审查。

### 第二轮：导出可达性、差分泄漏、切换与 SLO

发现并修正：

1. P0：Excel 定义 GrowthExport/`growth.exported` 但没有 request/download API；复用 Platform Jobs 或把 format=csv
   放进 GET 都无法表达 Platform 身份、独立审批、一次性下载和状态恢复。修正为第 7 节五个窄作用域 route，
   并冻结其非通用、无自由 query、no-store、短 TTL 和 one-use 合同。
2. P0：单 cell `k>=20` 仍可通过相邻范围相减恢复小群体。修正为 query fingerprint privacy budget、
   complementary suppression、角色更高阈值和 cursor/filter 绑定；禁止任意多维组合。
3. P0：只隐藏 Workspace 导航会保留 `/product-analytics/funnel`、DeveloperStart 内嵌组件和 restore target。
   修正为 PRD-141 数据 cutover 同发布卸载旧 permission/router/DOM/prefetch/import 并返回普通 404；PRD-147
   只负责物理删除和全批次静态证明，rollback 永不复活旧入口。
4. P1：增加 growth scheduler installer 会使现有 product event outbox 双 worker 竞争并扩大 39+2 注册项。
   修正为替换 `install_product_event_scheduler` 原槽位，一个 supervisor 内保留同一 worker 与水位，PRD-147
   再统一注册。
5. P1：只测缓存 query p95 和空库重算不能证明 Excel SLO。修正为 100 万固定 fixture 包含 consent、eligibility、
   suppression、五阶段、attribution、commit/reconcile；增量从 eligible source commit 到可读 snapshot 端到端测量。
6. P1：`growth.exported` 在制品 ready 时写会把未下载/已撤销制品记成已导出。修正为成功原子消费一次性
   receipt 时才写，未知网络结果不重放 receipt。

结论：第二轮 P0=0（3 项已关闭），P1=0（3 项已关闭）。两轮自审后无未关闭 P0/P1；G6 已通过，
本设计授权创建 0133 并进入 PRD-141 连续编码。
