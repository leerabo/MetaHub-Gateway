# PRD-086 客户健康评分设计

状态：**已完成并验收**（2026-08-17）。本文件冻结 PRD-086 的权威范围、评分口径、
租户/RBAC、持久事实、Webhook、PAGE-118 和验收合同。两轮编码前自审均已完成并修正，随后实现、迁移、
专项测试、真实 API/Scheduler 和 Browser 验收已闭环；PRD-085 已在提交
`919df60f81f9f55c149aea1b826c7aaa4ac3cd93` 完成并推送，本条未重复实现产品激活漏斗。
总体状态始终保持 `all_108_prds_complete=false`。

## 1. 权威范围与交付边界

### 1.1 交叉确认

- Excel `01_PRD总表!A87:R87` 与 `03_M2-M3扩展交付!A23:R23` 一致确认：
  `PRD-086 | M2-企业增强 | P2 | 扩展层 | 客户健康 | 客户健康评分`。
- 用户故事为“作为客户成功，我希望提前发现续费风险”；主要用户写为“客户成功/销售”。当前系统没有
  `customer_success` 或 `sales` 固定角色，因此不能借页面名称新增隐式角色或跨租户运营权限。
- 范围固定为调用趋势、错误率、成本异常、预算配置、团队活跃和工单六类信号，输出风险标签与可解释建议动作。
- 明确不做自动续费预测模型，不训练或调用模型推断续费概率，不把规则评分包装成 AI 预测。
- PAGE-118 为“客户健康页”；API-392/393 是 Excel 对 `GET /customer-health` 的方法/路径拆分，
  本条只发布一个无业务 query 的 GET，不自行增加写 API、列表 API 或跨租户汇总 API。
- OBJ-117 为 tenant-qualified `CustomerHealthScore`；EVT-098 为 `customer.health_changed`；
  TC-0171/0172 要证明低活跃/高错误客户被标记、建议动作可解释，以及指标权重、权限、异常与敏感边界。
- Excel 依赖写为“产品埋点、日志”。PRD-085 `product_events` 与既有 `request_logs` 是主要采用/可靠性来源；
  其他四类信号只读取仓库已经完成的权威表，不创建伪造来源。

### 1.2 架构约束

- 架构文档规定所有管理 API 必须后端租户鉴权，Trace、Usage、Budget、Webhook 按租户隔离，
  Usage Ledger 能回溯 `request_id` 和 `price_schema_version`；客户健康不能绕过这些 owner。
- 控制面不承载高频模型流量；评分只读取已持久化治理事实，不进入 Gateway 热路径。
- 金额默认人民币，但本条只使用“是否存在未关闭成本异常”而不重新汇总跨币种金额，避免货币口径漂移。
- 默认上游仍是智谱、通义、月之暗面、文心、火山、混元、DeepSeek 官方/云服务和客户本地
  OpenAI-compatible Endpoint；本条不改变 Provider 目录，不把 OpenRouter、硅基流动加入默认上游。

### 1.3 明确不做

- 不做续费概率、流失预测、机器学习训练、LLM 建议生成、跨客户排行榜或销售 CRM。
- 不创建通用工单系统、客服工单 CRUD、自动外呼、邮件、短信、审批或自动修复动作。
- 不自动调高预算、停用 Key、切换模型、创建争议或改写 CostAnomaly；建议动作只读、固定、可解释。
- 不返回 Prompt、Response、Token、Provider Key、Endpoint、邮箱、用户姓名、工单原因或异常正文。
- 不按 Provider、项目、应用、Key、用户或请求展示明细；PAGE-118 只展示当前租户的聚合信号。
- 不接受客户端权重、阈值、窗口、tenant、actor、货币、include 或排序参数。

## 2. 关键设计决定

1. 评分是版本化确定性规则，不是预测模型。权重、阈值、时间窗、风险标签和动作枚举全部由代码与迁移冻结，
   响应携带 `score_version=customer-health-v1`。
2. 评分窗口以事务内 UTC 小时整点为 `window_end`，主窗口为 `[window_end-7d, window_end)`，
   对比窗口为 `[window_end-14d, window_end-7d)`；使用半开区间避免整点双计。
3. `GET /customer-health` 是纯读的 HTTP safe/idempotent 接口，只读取当前 tenant 最新已提交评分；刷新页面不扫描六源、
   不取得写锁、不物化评分，也不创建 Webhook 事件。
4. 可关闭的后台 Scheduler 每次只领取一个 tenant，在 `BEGIN IMMEDIATE` 事务中冻结 UTC 窗口、聚合全部安全计数、
   物化 append-only 评分并推进租户游标。Scheduler 使用稳定 system request/trace，不把查看页面的用户当作评分 actor。
5. 每个完成小时允许追加一个来源快照；只有首个基线，或排除窗口时间与原始计数后的公开健康投影确实变化时，才与评分
   原子创建独立 EVT-098 source outbox。相同分数/风险/标签/动作/信号状态进入新小时不会误发 `health_changed`。
6. 当前仓库没有通用 SupportTicket，PRD-080 `billing_disputes` 只表达账单争议，不能冒充完整客服工单覆盖率。
   `ticket_health` 在 v1 固定为 `availability=unavailable`、`source_kind=support_ticket_unconfigured`，从有效权重分母剔除，
   并通过 `missing_signals` 与 `connect_support_ticket_source` 明示。以后接入权威 SupportTicket 必须升 `score_version`
   和 migration；不得用账单争议、审批任务、日志错误或自由文本替代。
7. PAGE-118 面向当前租户管理者。系统没有客户成功/销售固定角色，本条新增独立只读 permission，
   默认只授予 `admin` 与 `ops_admin`；不新增跨租户超级视图。

## 3. 信号来源与安全投影

### 3.1 调用趋势 `usage_trend`

- 来源：`request_logs`，按 `tenant_id + created_at` 统计当前 7 天和前 7 天请求数。
- 不读取 endpoint、model、Provider、Virtual Key、Prompt 或 error 正文。
- 当前与前期均为 0 时为 `availability=no_samples`、得分 0、rate NULL；前期为 0 且当前大于 0 时视为启动增长，
  得分/rate 均为 10000。
- 其他情况按 `current_count / previous_count` 计算整数基点并封顶 10000。
- 风险：当前为 0，或对比基点低于 5000 时产生 `usage_declining`。

### 3.2 可靠性 `reliability`

- 来源：当前窗口 `request_logs.status`；`success` 为成功，其余持久终态为失败。
- 无请求时可靠性没有样本，固定中性得分 5000，并用 `sample_status=no_samples` 明示；低活跃由独立信号处罚。
- 有请求时 `success_rate_bps=floor((success*10000 + total/2)/total)`，信号得分等于成功率。
- `error_rate_bps=10000-success_rate_bps`；当前请求至少 5 且错误率达到 500 时产生 `high_error_rate`，
  达到 1000 时触发更高风险下限；样本少于 5 不打高错误标签，避免单请求误判。

### 3.3 成本异常 `cost_anomaly_health`

- 来源：`cost_anomalies`，只按 tenant 统计仍未解决的 `status IN ('open','acknowledged')`，并区分
  `severity='critical'`；`dismissed|mitigated` 不再表示当前风险。
- 无未解决异常得分 10000；有未解决异常但无 critical 得分 4000；任一未解决 critical 得分 0。
- 有未解决异常产生 `cost_anomaly_open`；建议动作只跳转到既有成本异常页，不复制金额或证据正文。

### 3.4 预算配置 `budget_readiness`

- 来源：`budget_rules`，只统计当前 tenant `status='active'` 的规则数。
- 至少一条 active 规则得分 10000；没有得分 3000，并产生 `budget_missing`。
- 不读取预算名称、金额、scope ID 或 exhausted action，不重算预算消费。

### 3.5 团队活跃 `team_activity`

- 分母 `eligible_user_count`：`users` 中当前 tenant `status='active'` 的用户数。
- 分子 `recently_active_user_count`：上述用户中 `last_login_at` 落在 `[window_end-30d,window_end)`，或同一区间存在
  `console_page_events` 的去重用户数；同一用户只计一次。
- 分母为 0 时得分 0；否则按 `recently_active/eligible` 四舍五入计算 `0..10000` 基点。
- 分子为 0，或 eligible 至少 2 且活跃率低于 5000 时产生 `team_inactive`。
- 不返回用户 ID、邮箱、姓名、Session、navigation 或页面事件 payload。

### 3.6 工单 `ticket_health`

- v1 没有可证明为通用客服工单的权威表，因此该项固定 `availability=unavailable`、`health_status=unknown`、
  `score_bps=null`、`contribution_bps=0`，三个公开 evidence 值均为 `null`。
- 固定 `source_kind=support_ticket_unconfigured`，顶层 `missing_signals` 包含 `ticket_health`，建议动作追加
  `connect_support_ticket_source`。不把“没有数据源”解释成“零工单/满分”。
- 评分器不读取 `billing_disputes` 的 reason、decision、request/trace、金额或申请人，也不读取其他自由文本来源。

### 3.7 PRD-085 激活事实

- `product_events` 不重复计入六项权重，避免注册/接入里程碑与调用趋势双重加权。
- 它用于响应上下文：返回 7 个阶段中已到达数量和是否完成激活，帮助解释低活跃是“尚未接入”还是“接入后回落”。
- 只读取当前 tenant 的 event_type 计数；不返回 actor、source ID、request/trace 或发生时间明细。

## 4. 权重、总分与风险规则

### 4.1 固定权重

| 信号 | 权重基点 | 最高贡献基点 |
| --- | ---: | ---: |
| `usage_trend` | 2500 | 2500 |
| `reliability` | 2500 | 2500 |
| `cost_anomaly_health` | 1500 | 1500 |
| `budget_readiness` | 1000 | 1000 |
| `team_activity` | 1500 | 1500 |
| `ticket_health` | 1000 | 1000 |
| 合计 | 10000 | 10000 |

权重是公开名义权重。`availability=unavailable` 的信号不进入有效分母；v1 因 `ticket_health` 未接入，
`effective_weight_bps=9000`。`no_samples` 仍是可用信号并保留权重，数据源 SQL/类型/owner 失败则整次评分失败，
不能静默降级为 unavailable。总分使用 Python 任意精度整数：

`score_bps=floor((sum(signal_score_bps*weight_bps)+effective_weight_bps/2)/effective_weight_bps)`。

逐项贡献先取 `floor(signal_score_bps*weight_bps/effective_weight_bps)`，再把与总分的差值按余数从大到小分配
1 基点，余数相同时按上表固定顺序；因此 available 项贡献精确相加等于 `score_bps`，unavailable 项贡献恒为 0。
所有输入计数必须是 `0..922337203685477` 的 SQLite integer；该上限确保 migration trigger 复算时
`count*10000` 仍在 SQLite signed 64-bit 内。超限聚合整次失败；Python 与 SQLite 都不使用 JSON float、
数据库 REAL 或溢出乘法。

### 4.2 风险标签

- `score_bps >= 8000`：`healthy`
- `6000 <= score_bps < 8000`：`watch`
- `3000 <= score_bps < 6000`：`at_risk`
- `score_bps < 3000`：`critical`

风险 tag 是固定集合：`usage_declining`、`high_error_rate`、`cost_anomaly_open`、`budget_missing`、
`team_inactive`。排序固定为上述顺序，不能由数据库任意文本扩展；缺失信号不冒充风险 tag。

基础阈值之后再应用确定性风险下限：当前 7 天请求为 0 至少 `at_risk`；请求至少 5 且错误率达到 5% 至少
`watch`，达到 10% 至少 `at_risk`；存在未解决 critical 成本异常直接为 `critical`，其他未解决成本异常至少
`watch`。下限只能把风险调高，不能覆盖更严重的总分结果。

可用 signal 的 `health_status` 默认按同一四档 score 阈值映射；`usage_trend` 双窗口均无请求时为
`availability=no_samples,health_status=critical,score_bps=0`，`reliability` 无请求时为
`availability=no_samples,health_status=unknown,score_bps=5000`，`team_activity` 无 eligible 用户时为
`availability=no_samples,health_status=critical,score_bps=0`，`ticket_health` 为 unavailable/unknown/NULL。
离散 cost/budget 分值仍按该四档得到 health status，不允许 Console 自行解释。

### 4.3 建议动作

tag 到建议动作是一对一固定映射：

- `usage_declining -> restore_gateway_activity`
- `high_error_rate -> review_failed_requests`
- `cost_anomaly_open -> review_cost_anomalies`
- `budget_missing -> configure_tenant_budget`
- `team_inactive -> reengage_team`
- `missing_signals=ticket_health -> connect_support_ticket_source`

无 tag 且无缺失信号时返回 `maintain_current_adoption`。动作对象固定为
`{code,target_navigation_item_id}`；后端只有在当前认证权限允许目标能力时才返回非空 target，前端还必须确认 target
存在于当前后端导航快照，否则只显示建议文本。动作不预取目标 API、不执行 mutation；文案由 Console i18n 固定映射。
动作顺序先按固定 risk tag 顺序映射，再追加固定 missing signal 动作；只有两个数组均为空才返回 maintain。

目标映射固定为：`restore_gateway_activity -> growth-analytics/product_analytics.read`、
`review_failed_requests -> request-logs/request_log.read`、
`review_cost_anomalies -> cost-anomalies/billing.anomaly.read`、
`configure_tenant_budget -> budgets/billing.budget.read`、`reengage_team -> users/user.manage`；
`connect_support_ticket_source` 与 `maintain_current_adoption` target 固定 NULL。目标权限只控制导航链接，目标页面的写动作
仍由其后端 permission 独立校验。

## 5. 数据模型与迁移 0079

当前最高迁移为 `78/0078`；PRD-086 固定使用 `0079_customer_health_score.sql` 和对应 down。

### 5.1 `customer_health_scores`

append-only 评分事实：

| 字段 | 约束/用途 |
| --- | --- |
| `customer_health_score_sequence` | INTEGER PK AUTOINCREMENT，稳定最新顺序 |
| `customer_health_score_id` | `chs_` + 32 lower hex，公开 ID |
| `tenant_id` | 必填，所有 owner/index/FK 首列 |
| `score_version` | 固定 `customer-health-v1` |
| `window_start/window_end` | UTC Z 整秒，固定 7 天半开窗口 |
| `comparison_start/comparison_end` | 前 7 天，且 comparison_end=window_start |
| `source_fingerprint` | 窗口、来源水位、五类安全聚合和工单缺失状态的 canonical SHA-256 |
| `health_projection_sha256` | 不含窗口/原始计数的公开 score/risk/tag/action/信号状态摘要，用于判定真正 changed |
| `current_request_count/previous_request_count` | 非负且不超过 `922337203685477` |
| `successful_request_count/failed_request_count` | 和 current_request_count 精确相加 |
| `unresolved_cost_anomaly_count/critical_cost_anomaly_count` | 非负，critical 不超过 unresolved |
| `active_budget_rule_count` | 非负 |
| `eligible_user_count/recently_active_user_count` | 非负，recently_active 不超过 eligible |
| `ticket_source_status` | v1 固定 `unavailable`，不持久化伪工单计数 |
| `activation_stage_count` | `0..7` |
| `*_score_bps/*_contribution_bps` | 五项可用信号整数分数/贡献；ticket score 为 NULL、贡献为 0 |
| `effective_weight_bps` | v1 固定 9000，供响应与迁移复算 |
| `score_bps` | `0..10000`，由上述列和 v1 公式复算 |
| `risk_level` | `healthy|watch|at_risk|critical` |
| `operation_request_id/operation_trace_id` | Scheduler 按 tenant/window/source 派生的稳定 `req_chs_`/`trc_chs_` 链路 ID |
| `evaluated_at/created_at/updated_at` | UTC Z，业务列创建后不可变 |

唯一键：`(tenant_id,customer_health_score_id)`、`(tenant_id,window_end,source_fingerprint)`；
最新查询索引：`(tenant_id,customer_health_score_sequence DESC)`。表不保存 tags/actions JSON、用户名、金额、
request_id 列表或错误正文；这些公开枚举由强类型列重算。

`customer_health_score_id='chs_'+sha256(canonical source identity)[0:32]`，source identity exact-set 为 domain、tenant、
score version、window_end 与 source fingerprint；同一来源重试、进程重启和 down/re-up 得到相同 ID。`updated_at=created_at`，
业务行只能 INSERT，不能以 `INSERT OR REPLACE`、rowid 或 sequence 绕过不可变约束。

### 5.2 `customer_health_score_outbox`

- outbox 只为首个基线或 `health_projection_sha256` 相对上一评分变化的 score 创建；每个 score 最多一条。
- 状态固定 `pending|leased|sent|failed`；attempt `0..10`，generation、lease owner/token/deadline 使用完整旧值 CAS。
- 第一次 claim 在同一事务冻结 original `webhook_delivery_id` 排序去重数组；快照与 frozen_at 成对，空数组与未冻结
  NULL 不同。网络失败不回滚 score，成功、退避、十次终态和过期接管均复用稳定事件 ID。
- score trigger 禁止 UPDATE/DELETE/rowid replace；outbox 只允许状态机字段更新。所有 INSERT trigger 回查 tenant-qualified
  score/source，拒绝跨租户 owner、错误 projection hash 或自行伪造 outbox。
- 稳定 outbox/event ID 分别为 `chso_`/`evt_chs_` 加 score identity 摘要；可持久错误只允许
  `webhook.payload_invalid|webhook.delivery_unavailable|customer_health.unavailable`，不得保存异常字符串。

### 5.3 `customer_health_scheduler_tenants`

- migration 为既有 tenant 创建一条 due-now 游标，并安装 tenant INSERT trigger；游标只保存 tenant、下一次评估时间、
  lease/generation、最后成功窗口和固定错误码，不保存源正文。
- Scheduler 按 `next_evaluation_at,tenant_id` 公平领取一条。第一段短 `BEGIN IMMEDIATE` 只用完整旧值 CAS 写入
  `leased`、generation、worker/token/deadline 后提交；第二段 `BEGIN IMMEDIATE` 复核未过期租约，在同一一致快照内聚合、
  幂等插入 score/outbox 并推进游标。中途崩溃不提交半个 score，过期 lease 可由其他 worker 接管。
  首次成功后下一次对齐到下一个 UTC 小时；失败按 5/30/300 秒固定退避并只记录领域码。
- 相同 `(tenant_id,window_end,source_fingerprint)` 由唯一键幂等恢复；并发 worker 只有一个能提交。来源 fingerprint
  使用 canonical JSON exact-set 和 SHA-256，不包含 Prompt、Response、Token、Key、Endpoint、用户身份或自由文本。

### 5.4 来源快照与两类指纹

- `window_end` 取第二段事务内服务端 UTC 当前小时下界；请求主/对比窗口和 Console activity 使用 `unixepoch` 的左闭右开
  边界。未来时间、无法解析的 tenant 源时间、非终态 request status 或计数超限全部失败，不被 WHERE 静默跳过。
- request 来源 exact 聚合：current/previous 总数、current success/failed；`status='success'` 才成功，`failed` 才失败。
- current-state 来源在同一事务 `evaluated_at` 冻结：unresolved/critical CostAnomaly 数、active BudgetRule 数、
  eligible/recently-active User 数；activation 只取七类 ProductEvent 是否到达并汇总 `0..7`。
- `source_fingerprint` canonical exact-set 固定为 domain、tenant、score version、四个窗口边界、上述全部计数、
  `ticket_source_status=unavailable` 和 activation stage count；不含 evaluated_at，故相同窗口/聚合重试严格幂等。
- `health_projection_sha256` exact-set 固定为 score version、五项 score/contribution/availability/health status、
  ticket unavailable、effective weight、总分、最终风险、固定 risk tags、missing signals 和 action codes；明确排除窗口、
  原始计数、activation、request/trace、用户相关 target 和文案，避免每小时或不同读取角色造成假 changed。
- canonical JSON 使用 ASCII、排序字段、无空白与整数/NULL 原生类型；Python 计算后，migration trigger 使用同一字段序列和
  `metahub_sha256(json_array(...))` 复核。任何 hash/ID/公式不一致都让整个评估事务回滚。

### 5.5 RBAC revision 21

新增 `customer_health.read`，risk=`read`，默认授予 `admin`、`ops_admin`。迁移只扩展精确匹配 revision-20
默认矩阵的固定角色；自定义角色不被静默补权。Shared catalog、模板、tenant permission、matrix hash、
新租户初始化 trigger 和 Console fingerprint 同步升至 revision 21。

### 5.6 down 保护

- 空库和仅 seed/RBAC 数据允许 `79/0079 -> 78/0078 -> 79/0079`。
- 任一 `customer_health_scores`、score outbox、关联 Webhook delivery/attempt/dead-letter、订阅、active lease 或 revision-21 drift
  存在时，down 在首个 DDL 前原子阻断，不能丢弃客户健康历史或留下半降级目录。
- 没有 score/outbox/订阅/投递且 scheduler 仅含 migration/tenant trigger 自动生成的 pristine 游标时允许原子删除游标并回退；
  pristine 必须是 generation=0、无 lease/错误/成功窗口且 `next_evaluation_at=created_at`，任一漂移均阻断。
- 允许 down 时恢复 revision 20 的精确模板、permission 行、角色矩阵 hash 和 trigger；up/down 均做最终 hash guard。

## 6. EVT-098 `customer.health_changed`

### 6.1 source 与恢复

- 首个评分基线，或公开健康投影相对上一评分真正变化时，score 与独立 source outbox 同事务提交；普通小时快照不建 outbox。
- worker 每次领取一条到期 source，第一次 claim 在同一事务冻结 original delivery 快照，之后重启只复用快照。
- 公开事件 ID 从 score ID 稳定派生；每次 Endpoint 网络尝试前续租；成功、退避和十次失败使用完整 CAS。
- Webhook dispatcher 失败不记录异常正文；日志只保留固定 `customer_health.unavailable` 或 Webhook 错误码。
- pending delivery 恢复必须按 tenant + score ID 重建 exact payload，不信任 outbox JSON 或 worker 传入 request_id。
- 首次 claim 冻结为空数组后也不得重新查询后加订阅；冻结后删除/新增 Endpoint 不改变 original delivery 集合。
  lease 过期、停服信号和续租失败都禁止开始下一次网络尝试，旧 worker 不得标记新 generation 的结果。

### 6.2 exact payload

```json
{
  "schema_version": "1.0",
  "customer_health_score_id": "chs_...",
  "score_version": "customer-health-v1",
  "score_bps": 4200,
  "risk_level": "at_risk",
  "risk_tags": ["usage_declining", "high_error_rate"],
  "missing_signals": ["ticket_health"],
  "window_end": "2026-08-17T08:00:00Z"
}
```

信封 `tenant_id`、`object_id=customer_health_score_id`、`trace_id` 与 source 一致；actor 固定为
`system:customer-health-scheduler`，payload 不含读取者、request_id、计数明细或建议文案。Shared exact validator 拒绝
额外字段、未知/错序 tag、错序 missing signal、越界 score 和未知 risk；Control 从 tenant-qualified score 重建 payload，
并复核 projection hash，不能信任 outbox JSON 或 worker 传参。

## 7. 公开 API

### 7.1 `GET /customer-health`

- 无 query/body；路由先执行 `require_customer_health_reader`，tenant 只从 Session 恢复。
- 成功返回 `Cache-Control: no-store, private`、统一 `X-Request-ID/X-Trace-ID`，头体必须一致。
- 未知 query 返回 `customer_health.request_invalid` 422；数据库或评分不变量失败返回
  `customer_health.unavailable` 503；尚无已提交评分返回 `customer_health.not_ready` 503；匿名/无权继续使用统一 401/403。
- 服务只按 Session tenant 读取最新 `customer_health_scores` 与 Scheduler 安全状态，不扫描来源、不写数据库。
  `window_end` 不得晚于当前服务端 UTC 小时；相差不超过 2 小时为 `fresh`，否则保留最后可信快照并返回 `stale`；
  损坏行不降级。最新行按 tenant + sequence DESC 选择，跨租户同 ID 与不存在对象同形失败。

响应顶层固定字段：

```json
{
  "tenant_id": "tenant_...",
  "customer_health_score_id": "chs_...",
  "score_version": "customer-health-v1",
  "score_bps": 4200,
  "risk_level": "at_risk",
  "risk_tags": ["usage_declining", "high_error_rate"],
  "missing_signals": ["ticket_health"],
  "effective_weight_bps": 9000,
  "freshness_status": "fresh",
  "recommended_actions": [
    {"code": "restore_gateway_activity", "target_navigation_item_id": "growth-analytics"},
    {"code": "review_failed_requests", "target_navigation_item_id": "request-logs"},
    {"code": "connect_support_ticket_source", "target_navigation_item_id": null}
  ],
  "window": {
    "start": "2026-08-10T08:00:00Z",
    "end": "2026-08-17T08:00:00Z",
    "comparison_start": "2026-08-03T08:00:00Z",
    "comparison_end": "2026-08-10T08:00:00Z",
    "timezone": "UTC"
  },
  "signals": [],
  "activation": {"completed_stage_count": 4, "total_stage_count": 7, "activated": false},
  "evaluated_at": "2026-08-17T08:00:00Z",
  "generated_at": "2026-08-17T08:03:00Z",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

`signals` 必须恰好按 `usage_trend,reliability,cost_anomaly_health,budget_readiness,team_activity,ticket_health`
返回六项。每项 exact-set 为 `code`、`weight_bps`、`availability`、`health_status`、`score_bps`、
`contribution_bps`、`current_value`、`comparison_value`、`rate_bps`、`source_kind`；不返回任意说明文本。

| signal | availability/source | current / comparison / rate |
| --- | --- | --- |
| usage | `available|no_samples/request_log_aggregate` | 当前请求数 / 前期请求数 / 趋势比率 bps；双零时 rate NULL |
| reliability | `available|no_samples/request_log_aggregate` | 成功数 / 当前请求数 / 成功率 bps；无样本时 rate NULL |
| cost | `available/cost_anomaly_aggregate` | 未解决数 / 其中 critical 数 / NULL |
| budget | `available/budget_rule_aggregate` | active 规则数 / NULL / NULL |
| team | `available|no_samples/user_activity_aggregate` | 近期活跃数 / eligible 数 / 活跃率 bps；无用户时 rate NULL |
| ticket | `unavailable/support_ticket_unconfigured` | NULL / NULL / NULL，score NULL、贡献 0 |

`health_status` 冻结为 `healthy|watch|at_risk|critical|unknown`。所有非空计数为有界整数；权重、贡献、总分、
tag/action/missing signal、UTC 窗口和头体链路都由严格 DTO 与客户端逐项复算。

服务端 Pydantic 模型与客户端 validator 都使用 exact-set/`extra=forbid`，并冻结 ID 正则、六项顺序、各 signal 的
nullable 矩阵、action code/target 配对、`risk_level` 下限和 `activation.completed_stage_count<=7`；时间必须是 UTC Z
整秒，窗口精确 7d/7d 且 `comparison_end=start`。成功、401、403、422、503 都由最外层 middleware 统一 no-store 和头体链路。

## 8. PAGE-118 Console

- 后端导航项 `customer-health`，target section 同名，required permission 必须精确为 `customer_health.read`。
- owner 绑定 tenant、user、role、Session ID/revision、permission revision、完整 permission set、fingerprint、
  后端导航和全局 refresh generation；任一漂移先清 DOM 再 Abort。
- 未授权角色不渲染导航/DOM，不预取 API；当前 403 清空并刷新导航，当前 401 清 Session；迟到 200/401/403
  静默丢弃，同 owner 连续刷新也只能由最新 ticket 提交。
- 页面采用紧凑工作台布局：标题+刷新图标、总分/风险摘要、固定风险标签、六行信号贡献、建议动作和激活上下文。
- 桌面以稳定六列表格展示信号；390px 变为 label/value 行，不横向滚动；固定维度避免 loading/错误/数据切换位移。
- 所有动态值先经过 strict client validator，再 HTML/attribute 转义；成功页不显示原始 request_id，错误只显示 code/request_id。
- 页面完整覆盖 loading、ready、`no_samples` 新租户、`stale` 和整页 error；不自动周期刷新。建议动作只有后端 target
  非空且当前导航快照也包含该 target 时可点击，否则显示为不可点击文本并且不得预取目标 API。
- `customerHealthPageState` 是独立实例，不能复用 PAGE-117 的 controller。离页、租户/用户/Session/role/permission
  revision/fingerprint/navigation/global generation 任一变化都先推进本页 generation、清 DOM，再 abort；即使旧 fetch
  忽略 AbortSignal，旧 200/401/403/503 也不能影响新 owner 或新 Session。

## 9. 隐私、日志与追踪

- 评分事实、source outbox、Webhook、API、Console、日志和正式证据均不得包含 Provider Key、Virtual Key、
  Token、Prompt、Response、Endpoint、用户名、邮箱、工单原因、异常正文或请求 ID 列表。
- 失败请求继续在 `request_logs` 可追踪，Usage/成本继续由既有 ledger/FinOps 事实追踪；本条只读聚合，不复制账本。
- scheduler 日志禁止 `exc_info` 和异常字符串，只记录固定 event/error code；API 使用统一 ApiError 和 request_id/trace_id。
- score 行保留安全聚合计数、窗口、版本和 source fingerprint，可解释每项贡献并支持历史复核。

## 10. 测试与实际验收计划

### 10.1 Migration / Shared

- 空 SQLite：`79/0079 -> 79/0079 -> 78/0078 -> 79/0079`，每阶段 `integrity_check=ok`、FK 违规 0。
- revision 21、permission/template/matrix hash、新旧 tenant、默认与自定义角色扩展/回退精确。
- score owner、不可变、来源指纹、整数重归一化/最大余数贡献、scheduler/outbox 状态机、down 依赖和 drift 原子阻断。
- Shared `customer.health_changed` exact payload/envelope，敏感/额外字段、阈值和数组排序全部拒绝。

### 10.2 Control API

- 五类可用信号与工单 unavailable 边界、整数舍入/重归一化、总分/风险阈值与下限、固定 tag/action 顺序和 v1 复算。
- TC-0171：低活跃租户、至少 5 次请求且高错误率租户被稳定标记，建议动作可由 signal 贡献解释。
- tenant 隔离、同形跨租户、动态撤权、匿名、未知 query、not-ready/stale、数据库故障和重复纯读 GET。
- Scheduler 公平轮转、并发物化唯一、相同来源幂等、迟到来源、失败保留最后可信快照；首个基线发事件，
  新小时但投影不变不发事件，分数/风险/tag/action/信号状态变化才发事件。
- source snapshot、租约续期、过期接管、失败退避、十次终态、pending 恢复和 Webhook 重启幂等。

### 10.3 Console / Browser

- strict client：exact-set、头体链路、no-store、tenant、公式、时间、tag/action/source_kind、错误信封。
- owner/state/render：未授权零 DOM/零预取，当前与迟到 403/401，连续刷新迟到成功/失败，新旧租户计数可区分，
  action target 双 RBAC，全部字段转义。
- 真实 Browser：1280x720 与 390x844；至少四张 PNG 覆盖健康、高错误/高风险、新租户/no_samples 和租户隔离。
- 检查 overlap、横向溢出、页面/Console error、外部请求、敏感 canary、固定请求数和 owner 清理。

### 10.4 Runtime

- 从空库迁移，启动真实 Control/Console、本地 Webhook receiver，创建双租户安全聚合事实。
- 验证低活跃/高错误、成本异常、预算、团队、工单源未接入重归一化、PRD-085 激活上下文、event delivery、失败重试。
- 证明 request/usage/cost/失败可追踪，日志/DB/JSON/PNG 敏感命中 0，端口/进程/SQLite sidecar/临时目录归零。

## 11. 预计改动边界

- `infra/migrations/0079_customer_health_score.sql` 与 `.down.sql`
- `shared-sdk/python/metahub_shared/rbac.py`、Webhook exact validator、统一错误目录及专项测试
- `control-plane-api/src/metahub_control/customer_health/`、auth dependency、app/router/navigation 和专项测试
- `console-web/src/lib/customerHealth.js`、`features/customer-health/`、主壳/i18n/CSS/专项测试
- `scripts/acceptance/prd086-*.py|mjs` 与合同测试
- `docs/api/customer-health.md`、README、directory-map、PRD 测试记录和 Excel

不改 Gateway 热路径，不重构既有 PRD-085，不新增 Provider、CRM 或通用工单模块。

## 12. 编码前第一轮自审

状态：**已完成并修正（2026-08-17）**。

1. P1：初稿把 GET 变成六源扫描、写事务、物化与 Webhook 触发，查看页面会决定业务事实并占用 writer reservation。
   已改为 Scheduler 周期物化、GET 纯读；读取者不再成为事件 actor。
2. P1：初稿把 `billing_disputes` 当通用工单，零账单争议会被误算成零客服工单满分。已改为工单源未接入、
   明示 missing signal、有效权重 9000 重归一化，禁止以账单争议或文本来源冒充。
3. P1：初稿每个新小时都创建 `health_changed`，即使公开健康完全相同。已拆分 score/outbox，只有首个基线或
   `health_projection_sha256` 变化才发 EVT-098。
4. P1：初稿逐项独立四舍五入不能保证贡献之和等于总分。已冻结总分一次舍入、最大余数分配和固定 tie-break。
5. P1：初稿团队列名把分子/分母写成 active/enabled，语义倒置。已改为 eligible/recently_active，并冻结 30 日窗口。
6. P1：初稿风险只看总分，零调用或高错误仍可能显示 healthy。已增加零调用、5%/10% 错误率与成本异常风险下限。
7. P1：初稿建议动作只是字符串，可能让 ops 用户看到无权目标。已改为后端权限 + 当前导航快照双门禁的 action 对象。
8. P2：初稿未区分无样本、缺源、源故障和陈旧快照。已冻结 `no_samples`、`unavailable`、not-ready、stale；
   除权威工单源未接入外，任何源读取/类型/owner 故障均整次失败，不能按零或 partial 成功。
9. P2：成本异常只统计 open 会把 acknowledged 当作已解决。已改为 open/acknowledged 未解决集合。
10. P2：down 只笼统阻断评分，未覆盖独立 outbox、订阅、投递与非 pristine scheduler 游标。已补齐首个 DDL 前原子门禁。

## 13. 编码前第二轮自审

状态：**已完成并修正（2026-08-17）**。

1. P1：初稿允许 64 位最大计数并要求 SQLite 复算 `count*10000`，会溢出成 REAL。已把安全计数上限冻结为
   `922337203685477`，Python/trigger 共享 exact 整数公式，超限失败关闭。
2. P1：初稿只有宽泛 source fingerprint，无法证明重试、迟到数据与事件 changed 判定。已分别冻结
   `source_fingerprint` 和 `health_projection_sha256` exact-set，后者排除窗口、计数、activation 与用户特定导航 target。
3. P1：初稿把 scheduler claim 与长聚合写成一个未区分事务，租约语义不可实现。已改为短 claim CAS + 一致快照
   evaluate/commit 两段事务，冻结 generation/token/deadline、过期接管和失败退避。
4. P1：初稿没有冻结 stable score/outbox/event/system request/trace ID。已按 tenant/window/source identity 派生，
   同窗口同聚合在并发、重启和 down/re-up 后保持同一身份。
5. P1：初稿未处理 trigger 公式/hash 复核、rowid/replace 与贡献精确相加。已冻结 canonical JSON、SHA-256、
   safe-count、最大余数顺序和 INSERT-only owner 门禁。
6. P1：建议动作示例错误使用不存在的 `product-analytics` navigation ID。已核对后端目录改为 `growth-analytics`，
   并冻结五个真实 target/permission 配对与两个 NULL target。
7. P1：Webhook 恢复未明确冻结空订阅、停服、续租失败和旧 generation。已补齐 original snapshot、每次网络前续租、
   旧 worker 禁止写回、十次终态及不信任 outbox JSON 的恢复合同。
8. P1：API strict DTO 未冻结 signal nullable/health 状态、action target 配对、未来窗口与 activation 边界。已补齐
   服务端/客户端 exact-set、六项顺序、UTC Z 整秒、风险下限、最新 tenant sequence 和头体/no-store 全路径合同。
9. P1：页面只写了迟到 200/401，遗漏旧 403/503、同 owner 连续刷新和旧 401 登出新 Session。已要求独立 PAGE-118
   controller 和完整 owner/ticket/generation 校验，任何旧结果都不得改变新 owner。
10. P2：初稿没有精确定义 request 终态、current-state cutoff、坏时间和 fingerprint 是否含 evaluated_at。已冻结
    success/failed、事务内 evaluated_at、时间失败关闭与 fingerprint 排除 evaluated_at，避免静默漏行和重试失去幂等。
11. P2：stale 只写年龄，没有拒绝未来窗口或保证最后可信快照。已增加未来失败、2 小时 freshness 和 scheduler 失败
    保留旧 score 的合同。
12. P2：Browser/Runtime 证据必须可判定。正式验收固定双视口、四类状态、请求数、loopback-only、source provenance、
    artifact 白名单/SHA-256、敏感 canary 0，以及端口、进程、SQLite sidecar 和临时目录归零。

两轮自审问题均已在本文件修正，至此允许开始 PRD-086 编码；PRD-087 仍不得开始。

## 14. 全局状态

PRD-086 完成也不代表 108 条 PRD 全部完成；设计、代码、测试、证据、Excel、提交和推送全过程均保持
`all_108_prds_complete=false`。下一条只在 README、测试记录和 Excel 中交叉登记，本设计文件不扩展 PRD-087 范围。
