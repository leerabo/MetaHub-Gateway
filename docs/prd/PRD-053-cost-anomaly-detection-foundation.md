# PRD-053 - 成本异常检测基础

## 文档状态

- PRD：`PRD-053`
- 名称：成本异常检测基础
- 里程碑：`M1-企业POC`
- 优先级：`P1`
- 页面：`PAGE-077 成本异常页`、`PAGE-078 告警卡片`
- API：`GET /cost-anomalies`、`PATCH /cost-anomalies/{id}/action`
- 核心对象：`CostAnomaly`
- 事件：`cost.anomaly_detected`
- 验收：`TC-0105`、`TC-0106`
- 依赖：PRD-051 FinOps 正式账本、PRD-022 Virtual Key 限流、现有 Webhook 投递
- 当前状态：设计、两轮编码前自审、实现、测试与最终运行验收均已完成

## 1. 需求来源与结论

### 1.1 Excel 冻结需求

本设计已逐项核对以下权威位置：

- `01_PRD总表!A54:R54`
- `02_M0-M1核心开发!A54:R54`
- `04_页面清单!A78:H79`
- `05_API接口清单!A250:H255`
- `06_数据对象!A74:F74`
- `07_Webhook事件!A60:H60`
- `08_验收用例!A106:J107`
- `09_CodexClaude提示词!A54:F54`

冻结范围如下：

1. 以确定性规则检测最近 5 分钟成本相对过去 24 小时均值的异常暴涨。
2. 支持单 Virtual Key 异常和非工作时段异常。
3. 新异常形成告警，并允许管理员显式选择临时限流。
4. 提供成本异常页和告警卡片，展示建议动作与影响范围。
5. 提供 `GET /cost-anomalies` 与 `PATCH /cost-anomalies/{id}/action`。
6. 持久化 tenant-qualified `CostAnomaly`。
7. 首次检测产生 `cost.anomaly_detected`。
8. 验收必须覆盖模拟异常流量、误报、重复告警、无权限、异常输入、依赖故障、统一错误和隐私。
9. 明确不做机器学习异常检测。

API 清单把两个端点拆成 6 个路径 token；实现必须以 PRD 总表冻结的两个完整端点为准，不能误建 6 个接口。

### 1.2 架构约束

架构文档没有定义“成本异常”算法，算法细节以 Excel 和本文冻结合同为准。直接相关约束为：

- 成本、账单与异常检测属于 Control Plane/FinOps 低频异步能力，不进入 Gateway 同步模型调用热路径。
- 成本输入必须来自可追溯的正式账本，保留 tenant、Environment、Project、Virtual Key、request、trace 和价格证据链。
- 临时限流复用现有 Virtual Key RPM/TPM/并发执行器，不新增第二套热路径限流。
- 所有业务表默认带 `tenant_id`，所有管理 API 在后端恢复租户和角色。
- Webhook 复用现有签名、重试、重放与投递事实。
- 普通日志、事件和页面不得保存或显示 Provider Key、Virtual Key 明文、Authorization、Prompt 或 Response。
- OpenRouter、硅基流动仍只是竞品/迁移兼容对象；本 PRD 不改变上游厂商边界。
- M1 不引入硬件盒子、MCP 主链路、信创、复杂出境或完整事故管理平台。

### 1.3 现有能力与真实缺口

现有代码已经提供：

- `finops_ledger_entries` append-only 正式账本，以及 tenant/Environment/Virtual Key/时间部分索引。
- `usage_charge + debit + posted` 的正式 CNY 成本口径和不可变价格证据。
- `virtual_key_rate_limit_rules`、Gateway Redis 计数器与统一 429/503 错误。
- Control API Session RBAC、统一错误体、`request_id/trace_id`、审计日志。
- 可持久事件 ID、Webhook 白名单、严格 payload 校验、签名、重试和重放。
- 可组合的 Control API lifespan scheduler 模式。
- PAGE-074/075 FinOps、PAGE-076 预算页面的安全页面 owner、游标和响应式模式。

真实缺口为：

1. 没有 CostAnomaly 表、检测运行事实或去重合同。
2. 没有 5 分钟/24 小时、单 Key、非工作时段三类确定性规则。
3. 没有成本异常查询和动作 API。
4. 没有不覆盖永久限流规则的临时限流事实。
5. Gateway 只读取永久 Virtual Key 限流，不能合并有期限的治理覆盖层。
6. Webhook 白名单没有 `cost.anomaly_detected`。
7. Console 没有 PAGE-077/078、角色化导航、告警卡片和动作生命周期。
8. 没有覆盖误报、重复扫描、迟到正式分录、跨租户动作和依赖故障的测试。

## 2. 范围与非目标

### 2.1 本期范围

- 三类规则：Environment 内租户成本暴涨、Virtual Key 成本暴涨、Virtual Key 非工作时段消费。
- 固定 5 分钟桶、过去 24 小时基线、Asia/Shanghai 业务时区。
- 平台级可配置 N 倍门限、最小触发金额和工作时段。
- 完整窗口异步检测、正式账本水位、有限迟到回看、运行状态和降级可见性。
- 30 分钟同规则/同 scope 告警冷却，重复扫描幂等。
- 当前 RBAC 未提供独立财务/安全角色，读取和动作均映射为 `admin-only`；developer/viewer 后端拒绝。
- acknowledge、dismiss、apply/release temporary rate limit。
- 临时限流只适用于 Virtual Key scope；无永久限制时可新增正限制，有永久或其他临时限制时只能进一步收紧，至少一个维度必须产生实际效果。
- PAGE-077 列表/筛选/详情，PAGE-078 告警卡片。
- 首次异常事件、提交后 Webhook 投递和安全审计。
- 0043 迁移、聚焦测试、真实 Runtime/Browser 验收。

### 2.2 明确不做

- 机器学习、预测模型、季节性学习、动态阈值训练或外部 AI 判定。
- 中国法定节假日日历；v1 只按周一至周五与配置工作小时判断。
- 月度预测、预算审批、自动模型降级、永久修改 Key 限流或自动禁用 Key。
- 检测即自动止损；临时限流必须由 admin 的显式 PATCH 动作触发。
- 租户级临时限流、模型级临时限流或跨 Key 联动阻断。
- 新增财务/安全角色表；当前角色映射复用 admin/developer/viewer。
- 用户可编辑检测规则的管理 API；本期只使用有界平台配置并把配置快照写入异常事实。
- 第二套账本、第二套 Redis 计数器、同步扫描 Gateway 热路径或同步阻塞模型调用的 Webhook。
- 完整事故工单、审批流、通知编排、短信、邮件或复杂值班系统。

## 3. 检测规则冻结合同

### 3.1 权威成本输入

检测候选必须读取 `finops_ledger_entries` 中同时满足下列事实条件、且 `ledger_sequence <= frozen_watermark` 的全部行：

- `entry_type='usage_charge'`
- `direction='debit'`
- `status='posted'`
- `tenant_id` 与 `environment_id` 属于本次检测作用域
- `created_at` 落在当前窗口或其前置 24 小时基线

候选行只有同时满足以下金额证据条件时才能进入求和：

- `normalized_cny_micros` 的 Python 运行时类型严格为 `int`，不能由 `bool`、REAL、字符串或 NULL 冒充
- 单行金额位于 `0..1_000_000_000_000_000` micros
- `cny_normalization_status='normalized'`
- `price_evidence_status IN ('priced','legacy_amount_only')`

`unpriced`、CNY 不可归一、NULL、REAL、负值或越界都不能被静默忽略或当作 0；它们必须把对应 Environment 或 Virtual Key 作用域标成不完整。人工 debit/credit、gift、trial、reversal、预算 reservation 和旧 `usage_ledger.cost_cny` 不进入异常消费。

`estimated=1` 的 Usage 只要已成为具备可信 CNY 证据的正式 `usage_charge` 就纳入，并单独统计 `estimated_entry_count`；`legacy_amount_only` 同样计入但单独统计 `legacy_entry_count`，页面不得把二者伪装成厂商最终结算价。

聚合在 Python 中逐行使用任意精度整数完成，不能依赖可能溢出的 SQLite `SUM`。数据库、API、Webhook 和 Console 之间的累计 micros 与比率使用无前导零的 canonical 十进制字符串，前端使用 `BigInt`/十进制字符串格式化，禁止转成 JavaScript `Number` 后重新判定。

### 3.2 时间窗口、候选与水位

- 时间存储和比较使用 UTC；展示及工作时段分类使用 Asia/Shanghai。
- 5 分钟窗口按 Unix 时间整 300 秒对齐，使用左闭右开区间 `[window_start, window_end)`。
- 只评估已经闭合至少 120 秒的窗口，避免正在写入的当前桶制造误报。
- 基线为目标窗口开始前连续 24 小时，即 `[window_start-24h, window_start)`。
- 基线固定包含 288 个 5 分钟桶；没有消费的桶按 0 计入均值。这是 v1 对“过去 24 小时均值”的确定解释，不按活跃桶改变分母。
- 候选只来自最近 7 天内至少有一条当前窗口 Usage Charge 的 5 分钟桶；空消费窗口不可能触发三类规则，无需建 run。
- 每个候选的 `frozen_watermark` 是同 tenant/Environment 在 `[baseline_start, window_end)` 内的最大 `ledger_sequence`。run 不存在、相关水位增加或旧 lease 到期时才重评，其他租户/Environment 的新分录不能触发无关重算。
- 迟到分录使用新的 ledger sequence 和原业务 `created_at` 进入对应历史候选，因此服务短暂停机或 spool replay 不会因错过一次轮询而永久漏检。

生产代码统一通过模块级 `_utcnow()` 取时；内部 `run_detection_once(..., now=...)` 与 Gateway loader 的 `now` 参数只允许测试/验收直接调用，不暴露 HTTP 或环境变量覆盖，防止客户伪造时间。

### 3.3 N 倍与最小金额

Excel 没有指定 N、最小金额和配置作用域，本文冻结为：

- 默认 `N=5.000`，以整数 `spike_multiplier_millis=5000` 保存和比较。
- 默认最小触发金额 `10_000 micros = 0.010000 CNY`。
- N 与最小金额是部署级有界配置，不由请求或前端覆盖。
- 每条 CostAnomaly 保存实际 multiplier、最小金额、288 桶分母和 `detection_version='cost-anomaly-rules-v1'`。
- 比较使用 Python 任意精度整数：`current_micros * 288 * 1000 > baseline_total_micros * multiplier_millis`。
- “超过”采用严格大于；恰好等于门限不告警。
- 基线总额为 0 时不生成 spike，避免首次调用必然误报；非工作时段规则仍可独立判断。
- 当前金额低于最小触发金额时不生成异常，即使比率很高。
- `observed_ratio_millis` 使用向下取整的整数比率并以 canonical 十进制字符串持久化；它只用于解释，是否触发始终使用上面的交叉相乘式。

### 3.4 三类规则

#### 3.4.1 `tenant_spend_spike`

- 作用域为 `tenant`，`scope_id` 必须等于 `tenant_id`，金额只在同一 `tenant_id + environment_id` 内累计。
- 只有该 Environment 的当前与基线输入均完整时才评估。
- 当前完整 5 分钟金额超过该 Environment 过去 24 小时 5 分钟均值 N 倍时触发。
- severity 为 `critical`，建议动作为 `review_impacted_keys`；租户级 anomaly 不能直接创建临时限流。
- 影响范围冻结请求数、非空 Key 数、最高成本 Key 的不透明 ID 及账本时点 Project/Application 快照。

#### 3.4.2 `virtual_key_spend_spike`

- 只评估当前窗口有正式消费、且账本行能按 tenant/Environment/Virtual Key 归属的 Key。
- 每个 Key 独立判断当前与基线完整性；Key A 的未知价格不得让可信的 Key B 静默跳过。
- 每个 Key 的当前金额与该 Key 自己过去 24 小时 5 分钟均值比较。
- severity 为 `critical`，建议动作为 `apply_temporary_rate_limit`。
- Project/Application/Environment 使用账本时点快照；执行限流前再校验 Key 当前仍 active 且仍属于同 tenant/Environment。

#### 3.4.3 `virtual_key_off_hours`

- 默认工作时段为周一至周五 `09:00 <= local_time < 18:00`，时区固定 Asia/Shanghai。
- 周末全天、工作日 18:00 至次日 09:00 视为非工作时段；以 5 分钟桶起始时间分类，法定节假日不在 v1 计算。
- 只要求该 Key 当前窗口输入完整；非工作时段金额达到最小触发金额时触发，不要求非零基线。
- severity 为 `warning`，建议动作为 `apply_temporary_rate_limit`。

### 3.5 输入完整性与失败语义

每次 run 同时形成 Environment 汇总完整性和逐 Key 完整性：

- Environment current/baseline 任一可信条件失败，tenant spike 跳过。
- 某 Key current 不完整时，该 Key 的 spike 与 off-hours 均跳过；baseline 不完整时只跳过该 Key spike，current 完整的 off-hours 仍可评估。
- 其他完全可信的 Key 规则仍可产生 anomaly；run 因存在未判定作用域标记 `partial`，而不是把整个 Environment 伪装成“无异常”。
- 两层输入全部完整时 run 标记 `completed`。
- schema、SQL、时间、tenant/Environment/Key 归属或 canonical 整数合同异常时 run 标记 `failed`，只保存固定 `cost_anomaly.*` 原因码，不保存原始异常、SQL 或数据库路径。
- GET summary 返回 `healthy/degraded/idle`；`partial/failed` 都是 degraded，不能展示成“没有异常”。

检测失败不阻断 Gateway 模型调用，因为本 PRD 是异步告警能力；该 fail-open 只针对业务流量，检测运行、指标和日志必须明确 degraded。

### 3.6 精确去重、滚动冷却与迟到重评

- 精确窗口唯一键为 `tenant_id + environment_id + rule_type + scope_type + scope_id + window_start`，用于消除同一窗口重跑和并发 claim。
- 固定 30 分钟是滚动冷却，不是对齐日历桶。候选必须按 `window_start ASC` 处理；插入前在同一 `BEGIN IMMEDIATE` 中查询同 scope/rule 且 `window_start > candidate_window_start - 30m AND window_start < candidate_window_start` 的最近 anomaly，命中时只增加 run 的 `suppressed_count`。`detected_at` 是 wall-clock 处理时刻，绝不能用于历史业务窗口冷却。
- 恰好相隔 30 分钟允许新告警；10:29 与 10:30 仍只产生一条，避免对齐边界形成一分钟重复告警。
- 多实例竞争由 run lease/CAS 与上述写事务串行化；唯一约束作为最终防线。
- 水位增加可重评此前未越限的窗口，第一次跨阈值时补发 anomaly；已有 anomaly 的检测证据永不因迟到行改写。
- 冷却结束后仍持续异常可形成新 anomaly，防止长期问题永久沉默。

## 4. 模块与运行流程

### 4.1 Control 异步检测器

新增 `metahub_control.cost_anomalies`：

- `models.py`：公开 DTO、严格状态枚举和 canonical 金额字符串。
- `detector.py`：候选窗口、水位、逐作用域完整性、整数比较和 run CAS。
- `service.py`：列表、动作、幂等、审计和事件 outbox。
- `routes.py`：先认证/RBAC，再手工解析严格 query/body/header 并统一响应头。
- `scheduler.py`：组合 lifespan、单次 claim、outbox drain 和有界停止。

Scheduler 每轮先选一个最近 7 天内有当前消费且需要重评的 tenant/Environment/window。run 的受控状态机为：

1. `completed/partial/failed` 或新候选在 `BEGIN IMMEDIATE` 中以 `expected_revision + expected_watermark` CAS 进入 `processing`，生成不可预测 `claim_token`、`lease_owner_id` 和 `lease_expires_at`。
2. lease 未过期时其他 worker 不得领取；过期后 revision 加一并签发新 token，旧 worker 自动失权。
3. worker 在线程内新建 SQLite 连接、冻结 read watermark、完整物化输入后在 `finally` 关闭，不跨 thread/await 共享连接。
4. 完成写入必须同时匹配 tenant、run ID、revision、claim token、processing 状态和未过期 lease；CAS 失败只丢弃旧结果，不能覆盖新 revision 或累计计数。
5. 成功完成清空 lease 并写 `completed/partial/failed`、本 revision 的 anomaly/suppressed/count；重评计数按 revision 重置，不跨轮次累加。

GET 绝不触发检测或事件。API 读写也必须在 `finally` 关闭连接，并在关闭前完整物化 DTO，避免 Windows 文件句柄残留。

### 4.2 Gateway 临时限流合并

现有 `enforce_virtual_key_rate_limit` 仍是唯一执行器，只扩展 `load_virtual_key_rate_limit_rule`：

1. tenant-qualified 读取永久 active `virtual_key_rate_limit_rules`。
2. tenant-qualified 读取 `starts_at <= server_now < expires_at` 且 `status='active'` 的临时覆盖。
3. RPM/TPM/concurrency 每个字段分别取所有非空值最小值。
4. 无永久规则但有覆盖时仍执行；覆盖永远不能清空或提高已有有效限制。
5. 创建覆盖前读取永久规则与其他有效覆盖；提交值至少有一个维度严格低于当前有效值，或在原本无限制维度新增正限制，否则返回 409 `cost_anomaly.mitigation_no_effect`，不得把 anomaly 标成 mitigated。
6. 到期按 Gateway UTC 服务端时间自然失效，release 立即失效，无需恢复任务；其他 anomaly 的覆盖仍独立有效。
7. 限制值统一为 1..100,000,000，沿用 PRD-022 上限；Redis key、429/503 和 Provider 前阻断顺序不变。
8. loader 的数据库连接必须在 `finally` 关闭；有有效限制而计数存储不可用时继续 fail closed 为 `gateway.rate_limit_unavailable`。

### 4.3 检测、Webhook outbox 与动作时序

检测事务：

1. 持有有效 run claim，单一读快照加载 `<= frozen_watermark` 的正式分录和可信归属。
2. 在内存中执行有界聚合与规则判定。
3. `BEGIN IMMEDIATE` 再做 run CAS、精确唯一和滚动冷却，插入新 CostAnomaly，并把事件状态置为 `pending`。
4. commit 后由独立 outbox drainer 以 anomaly 的 event lease/CAS 领取；检测事务绝不出网。

`cost.anomaly_detected` 的 outbox 恢复合同：

- anomaly 事件状态为 `pending/processing/dispatched/failed`，每次 claim 带 token、30 秒 lease、`next_attempt_at` 和最多 10 次源派发尝试；数据库/validator 故障按 5 秒、30 秒、5 分钟上限退避，固定错误码可见。
- 公共 Webhook dispatcher 增加内部 `resume_existing_pending` 模式。先读取该 event 已有 original deliveries：success/failed 直接返回；pending 使用数据库中的 `payload_json`、`max_attempts` 和 `attempt_count + 1` 继续，不能用调用方重构正文或从 1 写冲突 attempt。
- resume 每次重读 Endpoint；active Endpoint 使用当前密文 Secret，并在下一次 attempt 前把 delivery `secret_version` CAS 到当前版本，不能保存旧 Secret 明文。Endpoint 已 disabled 时不出网，把 pending delivery 原子终结为 failed + 固定 `webhook.endpoint_disabled`。cost anomaly 仅在持有源 anomaly lease 时启用 resume；多个 Endpoint 部分成功时只续发仍 pending 的 Endpoint。
- `dispatched` 表示本次 outbox claim 内，dispatcher 读取并重新校验的 active 订阅者集合都已有唯一持久 original delivery intent，包含最终 HTTP failed；不追溯 claim 后才新增的订阅。网络成功与否继续以 Webhook delivery/attempt 为准；本次快照没有订阅 Endpoint 也可标 dispatched。
- 远端在响应前已接收而本地崩溃时可能收到同 event_id 重试，这是有 event_id 的 at-least-once 语义；不虚假承诺网络 exactly-once。

动作事务：

1. 后端先认证和 admin RBAC，再解析 raw header/body。
2. `BEGIN IMMEDIATE` 先按 tenant+actor+幂等 hash 查重，再 tenant-qualified 读取 anomaly。
3. 首次意图校验状态、Key 当前归属/状态、24 小时 mitigation eligibility 和实际收紧效果。
4. 同事务写 append-only action、anomaly 状态、必要的 override/release 和 `audit_logs`；任一步失败整体回滚。
5. commit 后才投递 `audit.recorded` 和安全成功日志；失败不得回滚已提交动作。
6. 动作不再次发送 `cost.anomaly_detected`。
## 5. 数据库与 0043 迁移

### 5.1 `cost_anomaly_detection_runs`

运行行是可恢复的调度状态，不是可任意编辑的业务配置。主要字段：

- `detection_run_id`：`car_` + 32 位小写十六进制，总长 36
- `tenant_id`、`environment_id`
- `window_start`、`window_end`、`baseline_start`、`baseline_end`
- `ledger_watermark`、`evaluation_revision`
- `status`：`processing/completed/partial/failed`
- Environment 级 current/baseline 完整性、`partial_scope_count`
- `source_entry_count`、`estimated_entry_count`、`legacy_entry_count`、`unpriced_entry_count`
- `anomaly_count`、`suppressed_count`
- `error_code`：completed 为 NULL；partial 固定 `cost_anomaly.detector_source_incomplete`；failed 只允许 `cost_anomaly.detector_source_owner_invalid`、`cost_anomaly.detector_amount_invalid`、`cost_anomaly.detector_schema_unavailable`、`cost_anomaly.detector_database_unavailable`、`cost_anomaly.detector_clock_invalid`
- `claim_token`、`lease_owner_id`、`lease_expires_at`
- `operation_request_id`、`operation_trace_id`
- `started_at`、`completed_at`、`created_at`、`updated_at`

唯一键为 `(tenant_id, environment_id, window_start)`，并提供 `(tenant_id, environment_id, detection_run_id)` 复合候选键。Environment 必须属于同 tenant；`window_end=window_start+5m`、baseline 恰为前置 24h。trigger 只允许第 4.1 节的 claim/finish/CAS 字段组合变化，拒绝 owner/window 改写、删除、REPLACE 和旧 revision 提交。

### 5.2 `cost_anomalies`

主要字段：

- `anomaly_sequence INTEGER PRIMARY KEY AUTOINCREMENT`
- `cost_anomaly_id`：`can_` + 32 位小写十六进制，总长 36
- `event_id`：`caevt_` + 32 位小写十六进制，总长 38
- `tenant_id`、`environment_id`、`detection_run_id`
- `rule_type`、`severity`、`status`：`open/acknowledged/dismissed/mitigated`
- `scope_type`、`scope_id`
- `virtual_key_id`、`top_virtual_key_id`
- 账本时点 `project_id_snapshot`、`application_id_snapshot`
- `window_start`、`window_end`、`baseline_start`、`baseline_end`
- `current_cost_micros`、`baseline_total_micros`：数据库类型为 canonical 非负十进制 TEXT，最长 40 位
- `baseline_bucket_count=288`
- `observed_ratio_millis`：off-hours 可为 NULL，其余为 canonical 非负十进制 TEXT
- `threshold_multiplier_millis`、`minimum_cost_micros`、`cooldown_minutes=30`
- `source_entry_count`、`estimated_entry_count`、`legacy_entry_count`、`affected_virtual_key_count`
- `representative_request_id`、`representative_trace_id`
- `source_ledger_sequence_min`、`source_ledger_sequence_max`
- `recommended_action`、`detection_version`
- `detected_at`、`mitigation_eligible_until=detected_at+24h`
- `event_dispatch_status`：`pending/processing/dispatched/failed`
- `event_dispatch_attempts`、`event_next_attempt_at`、`event_dispatched_at`
- `event_claim_token`、`event_lease_owner_id`、`event_lease_expires_at`、`event_last_error_code`；last error 只允许 `webhook.event_type_invalid`、`webhook.delivery_unavailable`、`cost_anomaly.detector_database_unavailable`
- `created_at`、`updated_at`

唯一约束：

- `cost_anomaly_id`
- `event_id`
- `(tenant_id, environment_id, rule_type, scope_type, scope_id, window_start)`

`tenant` scope 要求 `scope_id=tenant_id` 且 `virtual_key_id IS NULL`；Key scope 要求 `scope_id=virtual_key_id`。run、Environment 和 source ledger 必须同 tenant/Environment/window；Key/top Key 必须来自同 tenant 的源分录。Project/Application 字段是不可变账本时点快照，不在读取时用当前资源回填。代表 request 必须是同 tenant、同窗口且符合当前 scope 的 source Usage request。

除 status、事件 outbox 字段和 updated_at 的受控状态机外，检测证据全部不可变；DELETE、身份复用、`INSERT OR REPLACE`、归属 UPDATE 和跨 scope 改写均由 trigger 拒绝。

### 5.3 `cost_anomaly_actions`

append-only 字段：

- `cost_anomaly_action_id`：`caa_` + 32 位小写十六进制，总长 36
- `tenant_id`、`cost_anomaly_id`、`actor_user_id`
- `action_type`：`acknowledge/dismiss/apply_temporary_rate_limit/release_temporary_rate_limit`
- `status_before`、`status_after`
- `dismiss_reason`：仅 dismiss 必填，枚举 `false_positive/expected_activity/resolved_without_action/other`
- apply 的 `duration_minutes`、`rpm_limit`、`tpm_limit`、`concurrency_limit`
- release 的 `target_override_id`；apply action 通过 override 的 `source_action_id` 反向关联，避免循环外键
- 三个维度的 `effective_*_before`、`effective_*_after`，只保存安全整数/NULL
- `operation_request_id`、`operation_trace_id`
- `idempotency_key_sha256`、`intent_sha256`
- `created_at`

唯一键为 `(tenant_id, actor_user_id, idempotency_key_sha256)`。`intent_sha256` 对 `cost-anomaly-action-v1 + path anomaly ID + action + 规范化 reason/duration/limits` 做域隔离 canonical SHA-256；相同 key/同 intent 返回首次 immutable action 与当前 tenant-qualified anomaly/override 摘要，使用当前请求的 request_id/trace_id 并标 `idempotent_replay=true`。同 key 不同 intent 返回 409。

Action 通过复合关系绑定同 tenant anomaly/actor；release target 必须属于同 tenant、同 anomaly。任何 UPDATE/DELETE/REPLACE 都拒绝，幂等 replay 不写第二条 action、audit 或 override。

### 5.4 `temporary_rate_limit_overrides`

字段：

- `temporary_rate_limit_override_id`：`trl_` + 32 位小写十六进制，总长 36
- `tenant_id`、`virtual_key_id`
- `source_type='cost_anomaly'`、`source_id`、`source_action_id`
- `release_action_id`
- `rpm_limit`、`tpm_limit`、`concurrency_limit`
- `status`：`active/released`
- `starts_at`、`expires_at`
- `created_by`、`released_by`、`released_at`
- `operation_request_id`、`operation_trace_id`
- `created_at`、`updated_at`

约束：

- 至少一个 limit 非 NULL，所有值是 1..100,000,000 的严格整数。
- `expires_at > starts_at`，持续 5 至 1440 分钟。
- source anomaly 必须是同 tenant、同 Virtual Key 的 Key scope；source action 必须是该 anomaly 的 apply action 和同 actor。
- `release_action_id` 只能从 NULL 一次性绑定该 anomaly 的 release action，同时 status 从 active 变 released 并写可信 released_by/at。
- limit、owner、source 和 expires_at 不允许 UPDATE；过期 active 行执行语义为 expired，不能再 release 或延长。
- 多个 anomaly 可对同 Key 建立覆盖，Gateway 始终逐字段取最小值。释放一条不会修改其他覆盖或永久规则。

apply 只允许 anomaly 为 open/acknowledged、Key 当前 active 且仍在同 tenant/Environment、服务器时间未超过 `mitigation_eligible_until`。同 anomaly 已有 active 未过期覆盖、已 mitigated/dismissed、Key disabled/改绑、无实际收紧效果均返回稳定 409。release 后 anomaly 保留 mitigated 历史；同一 anomaly 不允许再次 apply，新持续异常应由冷却后的新 anomaly 表达。

### 5.5 Audit 枚举与同事务合同

0043 保真重建 `audit_logs`，在 0042 全部既有 action/object 上追加：

- `cost_anomaly.acknowledged`
- `cost_anomaly.dismissed`
- `cost_anomaly.temporary_rate_limit_applied`
- `cost_anomaly.temporary_rate_limit_released`
- object type：`cost_anomaly`

`audit/service.py` 的 `ACTION_OBJECT_TYPES` 同步一一映射到 `cost_anomaly`。`SUMMARY_FIELDS['cost_anomaly']` 只允许：`status`、`action_type`、`dismiss_reason`、`environment_id`、`scope_type`、`scope_id`、`temporary_rate_limit_override_id`、三个 limit、`expires_at`。before/after 不保存 Key 名称/前缀、幂等 hash、Prompt、Response、Token、请求正文或异常正文。

重建必须逐行保留 rowid、NULL、历史 JSON、所有既有索引和 `trg_audit_logs_owner_insert/immutable_update/immutable_delete/prd050_conflict_insert/prd050_rowid_insert`。up 前扫描依赖 audit_logs 的外部 view/trigger/FK 与未知索引；发现非白名单对象就原子拒绝，不能静默丢依赖。

非 replay 动作在同一 `BEGIN IMMEDIATE` 内插入 action、更新 anomaly/override 并调用现有 audited transaction 原语插入一条 Audit；commit 后才尽力投递 `audit.recorded`。回滚和幂等 replay 均不得留下新 Audit。

### 5.6 索引、Owner 图与回滚

0043 增加 anomaly tenant/status/sequence、rule/time、scope/time、pending event，run status/window/lease，以及 override tenant/key/status/expiry 部分索引；先用 `EXPLAIN QUERY PLAN` 证明 0042 已有 FinOps 部分索引可复用，不重复建成本源索引。

数据库第二道租户防线逐条覆盖：

- run -> tenant + Environment
- anomaly -> tenant + Environment + run + exact window；tenant scope 固定 tenant ID；Key scope 能由同 owner source ledger 证明
- representative request/trace -> 同 tenant/source window/scope
- action -> tenant + anomaly + actor；release target -> tenant + anomaly + override
- override -> tenant + current Key + source anomaly + apply action；release action/actor 同 owner

INSERT、owner UPDATE、DELETE、`INSERT OR REPLACE` 和 `UPDATE OR REPLACE` 旁路都要有迁移测试。历史 Project/Application 只作为 source ledger 快照，不因当前资源改名、禁用或改绑而漂移；临时限流则必须重新校验当前 Key 归属。

0043 down 在任何 DDL 前统计并原子拒绝以下任一事实：run、anomaly、action、override；订阅了 `cost.anomaly_detected` 的 Endpoint；该事件的 delivery 或关联 attempt；四类 cost anomaly Audit。只有完全空事实时才按子表顺序删除新对象、把 Audit 保真恢复为 0042 枚举，并验证 rowid/NULL/索引/trigger、`integrity_check` 和 `foreign_key_check`。0043 不改写、删除或重算 0041 正式分录。
## 6. API 设计

### 6.1 `GET /cost-anomalies`

权限：仅 `admin`。独立 finance/security 角色尚未实现，因此 developer/viewer 与手写请求均由后端返回 403，匿名返回 401。

route signature 不声明 FastAPI typed Query 参数；只声明 `Request` 和 `Depends(require_cost_anomaly_reader)`，先完成认证、tenant 恢复和 RBAC，再从 `request.query_params.multi_items()` 手工解析：

- `status`
- `rule_type`
- `severity`
- `environment_id`
- `virtual_key_id`
- `from_time`、`to_time`
- `limit`：1 至 100，默认 20
- `cursor`：tenant/filter/snapshot 绑定的 HMAC opaque cursor

未知字段、重复字段、客户端 `tenant_id`、非法 UTF-8/时间、倒置区间、非法枚举、bool/float limit 或 cursor 篡改统一 422。Environment/Key 必须 tenant-qualified 校验；跨租户/不存在对象和非法 ID 形状同形 404。

同一 `BEGIN DEFERRED` 快照按 `anomaly_sequence DESC` 返回：

- `cost_anomalies`：完整检测快照、安全 owner 名称/既有脱敏 Key prefix、当前 effective mitigation 状态和 append-only action history
- micros 与 ratio 均为 canonical 十进制字符串，并同时返回服务端格式化的六位 CNY 字符串；前端不重算触发结论
- `summary`：open/critical/mitigated、active override 数、detector `healthy/degraded/idle`、最近完成窗口和固定错误码
- `total`、`limit`、`has_more`、`next_cursor`
- 当前请求的 `request_id`、`trace_id`

cursor 绑定 tenant、规范化筛选、snapshot maximum sequence 和 before sequence；只进入前端内存，不写 URL、Storage 或 DOM data 属性。响应统一 `Cache-Control: no-store`。

### 6.2 `PATCH /cost-anomalies/{id}/action`

权限：仅 `admin`。route 不声明 Pydantic Body/typed Header；认证/RBAC dependency 完成后才读取 ASGI raw headers 和 body，保证畸形 query/header/body 对未授权用户仍先返回 401/403。

wire 合同：

- path ID 必须是 `can_` + 32 位小写十六进制；非法形状与当前 tenant 不存在同形 404
- `Content-Type` 只允许 `application/json` 或显式 `charset=utf-8`，其他 charset 415
- 从 `scope['headers']` 计数单一 `Idempotency-Key`，拒绝重复/逗号合并/控制字符；值匹配 `[A-Za-z0-9._:-]{8,128}`
- 正文最多 8 KiB；有 Content-Length 时先拒绝越界/重复/非法值，无长度时流式累计并在越界立即停止
- 严格 UTF-8、顶层 object、拒绝 duplicate/extra key、控制字符、NaN/Infinity/指数、过深或过多节点
- 数字只接受 JSON integer，`bool` 不得冒充 int，拒绝 float/string；客户端不得提交 tenant、actor、status、绝对 expires_at、request_id 或 trace_id

动作：

1. `{"action":"acknowledge"}`
2. `{"action":"dismiss","dismiss_reason":"false_positive"}`，reason 必须是第 5.3 节固定枚举；这就是 Excel “误报处置”的 canonical wire
3. `{"action":"apply_temporary_rate_limit","duration_minutes":30,"rpm_limit":10,"tpm_limit":null,"concurrency_limit":2}`
4. `{"action":"release_temporary_rate_limit"}`

apply 规则：

- 仅 Virtual Key scope anomaly，duration 为 5..1440 的严格整数，三个 limit 至少一个为 1..100,000,000。
- anomaly 必须 open/acknowledged 且服务器时间不晚于 `mitigation_eligible_until`；Key 必须当前 active、同 tenant/Environment。
- 同 anomaly 已有有效覆盖、已 dismissed/mitigated、已过处置期、Key disabled/改绑均返回稳定 409。
- 相对永久规则和其他有效覆盖至少一个维度实际收紧；全无效果返回 `cost_anomaly.mitigation_no_effect`，事务零副作用。

状态迁移：

- `open -> acknowledged`
- `open/acknowledged -> dismissed`
- `open/acknowledged -> mitigated`
- release 只释放该 anomaly 当前仍 active 且未过期的 override；anomaly 保留 mitigated 历史，同 anomaly 不可再次 apply
- dismissed 为终态；非法重复动作返回 409

幂等处理在首次状态校验前执行：同 tenant+actor+key 且同 canonical intent，返回首次 immutable action、当前 tenant-qualified anomaly/override 摘要和 `idempotent_replay=true`，不重复写 action/audit/override；同 key 不同 path/action/参数返回 409。成功及 replay 都使用当前请求的 request_id/trace_id，并设置 no-store。

## 7. PAGE-077/078 前端设计

### 7.1 导航与角色

后端 Console Navigation 新增 `cost-anomalies`，放在预算之后、策略之前：

- 仅 admin 返回导航项并允许预取/动作。
- developer/viewer 不返回入口；手写 hash 必须回到首个已授权页面且不发 cost anomaly 请求。
- 前端角色判断只减少无效交互，GET/PATCH 权限始终由后端 admin dependency 执行。

### 7.2 PAGE-078 告警卡片

顶部告警带使用稳定尺寸的重复卡片展示：

- severity、rule、status
- detected_at 与首次 5 分钟窗口
- 当前金额、24 小时/288 桶基线均值、N 倍或非工作时段原因
- Environment、Tenant/Virtual Key 作用域
- 受影响请求数、Key 数、账本时点 Project/Application
- 代表 request_id/trace_id 跳转
- 后端返回的 recommended action
- 临时限流 active/expired/released 和实际 effective limits

卡片只展示不透明 ID、名称和既有脱敏 Key prefix；不展示 Key 明文、Authorization、Prompt、Response、Provider 凭据、Webhook secret 或原始异常。

### 7.3 PAGE-077 列表与详情

- 状态、规则、severity、Environment 和时间筛选。
- opaque cursor 上一页/下一页。
- 选中异常详情显示不可变检测证据、影响范围、输入完整性和动作历史。
- admin 可 acknowledge、按固定 reason 标记 dismiss/误报、对可执行 Key anomaly 临时限流和 release。
- 表单依据后端 eligibility/effective status 禁用，但不能替代后端 403/409。
- action 使用内存 idempotency key；网络不确定时保留同 key，明确 4xx 未提交后才生成新 key。
- micros 用 BigInt/字符串格式化，所有错误只显示统一 code、message、request_id，不把原始 response body 拼入 DOM。
- 登出、换租户、降权、离页、pagehide 和 401 必须 abort 请求并销毁 cursor、草稿、idempotency key 和旧租户 DOM。
- 1280x720 与 390x844 无横向溢出、裁切、重叠；移动端卡片和动作表单进入单列文档流。

## 8. 错误码设计

| 错误码 | HTTP | 语义 |
| --- | ---: | --- |
| `cost_anomaly.query_invalid` | 422 | query、时间或 cursor 非法 |
| `cost_anomaly.not_found` | 404 | 当前租户找不到 anomaly、Environment 或 Key，含非法 ID 形状 |
| `cost_anomaly.media_type_invalid` | 415 | action Content-Type/charset 非法 |
| `cost_anomaly.action_invalid` | 422 | header/body/action/reason/duration/limit 非法 |
| `cost_anomaly.action_conflict` | 409 | 状态迁移不允许 |
| `cost_anomaly.idempotency_conflict` | 409 | 同幂等键绑定不同 canonical intent |
| `cost_anomaly.mitigation_not_applicable` | 409 | 非 Key anomaly 请求临时限流 |
| `cost_anomaly.mitigation_active` | 409 | 同 anomaly 已有有效覆盖 |
| `cost_anomaly.mitigation_expired` | 409 | anomaly 已超过 24 小时处置期 |
| `cost_anomaly.mitigation_key_unavailable` | 409 | Key disabled、删除或已改绑 |
| `cost_anomaly.mitigation_no_effect` | 409 | 提交限制没有实际收紧任何维度 |
| `cost_anomaly.mitigation_not_active` | 409 | release 时覆盖不存在、已释放或已过期 |
| `cost_anomaly.unavailable` | 503 | Control 查询或动作数据库依赖不可用 |
| `cost_anomaly.detector_unavailable` | 503 | 内部 detector/outbox 依赖不可用，不向匿名内部调用暴露细节 |
| `auth.permission_denied` | 403 | 后端角色拒绝 |
| `gateway.rate_limit_exceeded` | 429 | 临时或永久有效限制被命中 |
| `gateway.rate_limit_unavailable` | 503 | 有效限制存在但计数存储不可用 |

所有错误继续由统一异常处理中间件返回 Header/正文一致的 `request_id` 和 `trace_id`、安全 suggestion/diagnosis，并设置 no-store；不得返回 SQL、数据库路径、Redis URL、lease/token、幂等键/hash 或原始异常。

## 9. Webhook 事件

`cost.anomaly_detected` 只在唯一 CostAnomaly 首次插入后进入 outbox。acknowledge、dismiss、apply 和 release 仅写 action/audit，不复用 detected 事件。

稳定信封：

- `event_id`：与持久 anomaly 行相同的 `caevt_` + 32 小写 hex
- `tenant_id`
- `timestamp`：持久 `detected_at`，UTC RFC3339
- `actor_id=null`：系统检测器
- `object_id`：`can_` + 32 小写 hex
- `trace_id`：检测 run 的固定 `operation_trace_id`

精确 payload：

- `anomaly_id`
- `rule_type`、`severity`、`status='open'`
- `environment_id`
- `scope_type`、`scope_id`
- `window_start`、`window_end`
- `current_cost_micros`、`baseline_total_micros`、`observed_ratio_millis`：canonical 十进制字符串，off-hours ratio 为 null
- `baseline_bucket_count=288`、`threshold_multiplier_millis`、`minimum_cost_micros`
- `source_entry_count`、`estimated_entry_count`、`affected_virtual_key_count`：0..2^53-1 JSON integer
- `recommended_action`、`detection_version`

共享 `build_webhook_event` 必须同时扩展事件白名单、可订阅集合、持久 event ID 校验、payload 精确字段/类型/上限、envelope 绑定和递归敏感键测试。payload 不含 Key 名称/前缀、request/trace 样本、Project/Application 名称、Provider/模型正文、Prompt、Response、Authorization、Token 或 secret。

outbox 的 `dispatched` 只代表 delivery intents 已可靠持久化，HTTP success/failed/retry 仍由现有 Webhook 数据对象追踪；pending intent 的 resume 和 at-least-once 边界以第 4.3 节为准。

## 10. 安全、权限与租户隔离

1. Cost anomaly reader/writer 都映射 admin；当前 finance/security 职责由 admin 承接，不提前扩展角色。匿名 401，developer/viewer 403。
2. Route 必须先执行认证/RBAC dependency，再手工读取 raw query/header/body；前端隐藏入口不构成权限控制。
3. 所有 GET/PATCH 只从 Session 获取 tenant_id；URL ID、Environment、Key、anomaly、action、override、run 全部 tenant-qualified。
4. Environment 属于 tenant；检测 Key 用 source ledger 证明，限流 Key 还要重新验证当前 Environment/active 状态。Project/Application 是账本时点快照。
5. 客户端不能提交 tenant、actor、检测数值、状态、建议动作、绝对过期时间或 operation request/trace。
6. 写路径使用 `BEGIN IMMEDIATE`，读路径使用单一 `BEGIN DEFERRED`；API、scheduler worker 和 Gateway loader 都在 `finally` 关闭各自线程内连接。
7. Action、anomaly 状态、override/release 和 Audit 同事务；Webhook/Audit dispatch 与成功日志只能在 commit 后发生，回滚零副作用。
8. action 幂等以 tenant+actor+key hash 隔离；原始 key 不落库、不入日志，intent hash 包含 schema/path/action/规范化参数。
9. anomaly 核心证据、action、override 核心字段不可变；数据库 owner/replace/delete trigger 是服务层后的第二道防线。
10. Gateway 合并覆盖时同时使用 tenant_id、virtual_key_id 和 UTC 有效期；不能跨租户或跨 Key 套用。
11. 检测和 outbox 日志只记录固定规则、canonical 金额、计数、不透明 ID、request/trace 和固定错误码。
12. 页面、Audit、Webhook 和普通日志不保存 Provider Key、Virtual Key 明文、Authorization、Cookie、Prompt、Response、Webhook secret、原始 JSON 或异常正文。
13. anomaly/action/override/run 不支持普通删除；保留期沿用财务与审计事实策略。
14. 代表 request/trace 只允许当前租户 admin 通过现有 RequestLog 权限打开。
15. Webhook 是 event_id 可去重的 at-least-once；崩溃恢复不得虚假宣称远端网络 exactly-once。

## 11. 配置与可验收时间合同

新增有界配置及 `.env.example`：

- `METAHUB_COST_ANOMALY_ENABLED`：缺失时 false，开发/生产按部署显式开启
- `METAHUB_COST_ANOMALY_POLL_INTERVAL_SECONDS=30`
- `METAHUB_COST_ANOMALY_SPIKE_MULTIPLIER_MILLIS=5000`
- `METAHUB_COST_ANOMALY_MIN_COST_MICROS=10000`
- `METAHUB_COST_ANOMALY_WORKDAY_START_HOUR=9`
- `METAHUB_COST_ANOMALY_WORKDAY_END_HOUR=18`

验证边界：poll 5..300 秒；multiplier 1001..100000；minimum 1..1,000,000,000,000 micros；hour 0..23 且 start < end。时区、5 分钟窗口、24 小时/288 桶基线、120 秒闭合、30 分钟滚动冷却、7 天回看、24 小时处置期、30 秒 lease 和 outbox 最大尝试在 v1 固定并写入检测快照或代码常量。

生产 scheduler/Gateway 使用真实 UTC；测试和验收只通过内部函数参数注入 `now`，不能由 HTTP 客户端或普通环境变量控制。Runtime 验收器在临时副本数据库上写入真实正式账本行，调用一次性 detector tick 生成事实，再通过真实 Control/Gateway HTTP 验证；不允许直接 INSERT anomaly 绕过规则。

Gateway 实际 429/恢复验收优先使用已配置本地 Redis；无 Redis 的隔离测试服务器可在创建 app 后、监听端口前向 `app.state.rate_limit_store` 注入仓库现有的受控内存实现。真实运行用 release 立即证明恢复，expiry 自动恢复用同一 loader/执行器和注入时钟的集成测试证明，避免等待 5 分钟。Webhook 使用仅绑定 `127.0.0.1` 的临时 receiver，验收结束关闭 receiver、服务和 Session。全部临时文件仍限定 `D:\MetaHub\.tmp`。
## 12. 测试计划

### 12.1 纯规则、金额与时间

1. tenant 当前桶严格超过 24h/288 均值 5 倍时触发；等于时不触发。
2. Key A spike 只产生 Key A anomaly；Key A unknown 不阻断可信 Key B。
3. 非工作时段、周末、09:00、18:00、午夜边界按 Asia/Shanghai 和桶起始时间正确。
4. 基线 0 不触发 spike；off-hours 仍可触发。
5. 小于 0.01 CNY 不触发，恰好最小金额的 off-hours 触发。
6. manual adjustment、credit、reversal、reservation 不计入。
7. priced/legacy/estimated 正式 Usage 正确计入并分别计数。
8. current/baseline 的 unpriced、USD unavailable、NULL、REAL、bool、负值和越界按作用域形成 partial/failed，不伪造 0。
9. 超过 SQLite/JavaScript 安全范围的累计值由 Python int + canonical TEXT 保真，Webhook/Console 不用浮点重算。
10. 候选闭合 120 秒、最近 7 天和 `[baseline_start, window_start)` 半开边界正确。

### 12.2 调度、CAS、冷却与事件

1. 同 watermark 重跑直接幂等，相关 watermark 增加才 revision 重评。
2. 无关 tenant/Environment 新分录不重评；迟到 current/baseline 分录能触发受影响历史窗口。
3. lease 未过期拒绝第二 worker；lease 到期接管签发新 revision/token，旧 worker finish CAS 为 no-op。
4. 候选按 window_start 升序；同 window 并发只有一个事实；冷却查询只比较业务 window_start，10:29/10:30 不重复，恰好 30 分钟允许新告警，迟到历史回放不受 wall-clock detected_at 污染。
5. 首次未越限、迟到后越限只补发一个 anomaly；已有证据不漂移。
6. commit 失败零 outbox/网络；首次 anomaly 只有一个 event_id。
7. delivery intent commit 后、HTTP 前崩溃可用持久 payload/max_attempts 从 attempt_count+1 resume；Secret 轮换 CAS 到当前版本，disabled Endpoint 安全终结，多个 Endpoint 部分成功只续发 pending。
8. source event lease 并发、订阅者 claim 快照、退避、最大 10 次和 failed/degraded 状态正确；claim 后新增订阅不追溯旧事件。
9. 远端不确定重试保持同 event_id/delivery ID；不产生第二 original delivery。
10. payload/envelope 精确字段、canonical string、ID 长度和递归敏感键门禁通过；PATCH 不发 detected。

### 12.3 迁移、Audit 与数据库

1. 0042 空事实升级 0043，表、索引、trigger、Audit 枚举完整，重复迁移幂等。
2. 0042 FinOps、预算、限流、Webhook 和 Audit 逐行/rowid/NULL 保持，未知依赖对象使 up 原子拒绝。
3. run/anomaly/action/override 的跨租户、跨 Environment、跨 Key/actor/source INSERT 和 owner UPDATE 被拒绝。
4. tenant/key scope、representative request/window/source ledger 关系强制成立。
5. anomaly 核心证据、action、override limit/expiry 不可改写或删除；INSERT/UPDATE OR REPLACE 旁路拒绝。
6. active -> released 合法；重复 release、过期 release、延长 expires_at、错误 release action 拒绝。
7. 四类 action 各生成一条同事务 Audit，summary 只含白名单；回滚/replay 零新增 Audit。
8. up/down 空事实往返恢复 0042 Audit schema，`integrity_check=ok`、`foreign_key_check=0`。
9. run/anomaly/action/override、订阅、delivery、attempt 或 cost Audit 任一存在时 down 在 DDL 前原子拒绝。
10. 0041 FinOps 正式分录数量、关键 hash 和 0042 预算事实不变；FinOps 查询计划复用 0042 部分索引。

### 12.4 Control API 主流程、异常与权限

1. admin GET 成功；anonymous 401，developer/viewer GET 403。
2. admin acknowledge、四种 dismiss reason、apply、release 成功；误报处置可追踪。
3. developer/viewer PATCH 畸形 query/header/body 仍先 403；anonymous 仍先 401。
4. GET 筛选、snapshot cursor、total/no-store、Header/正文 request_id/trace_id 正确。
5. 跨租户/不存在/非法形状 anomaly、Environment、Key 同形 404，列表零串租。
6. 同 idempotency/path/intent replay 返回首次 action+当前状态且零副作用；同 key 不同 anomaly/action/参数 409。
7. 严格测试重复 raw header、重复 JSON key、extra、超 8KiB、有/无 Content-Length、非法 charset/UTF-8、bool/float/string/指数/NaN、控制字符和深度/节点上限。
8. 非 Key apply、过期 anomaly、Key disabled/改绑、已有覆盖、全无收紧效果、dismissed/mitigated 冲突返回固定 409。
9. release 不存在/已释放/已过期覆盖 409；其他 anomaly 覆盖保持有效。
10. 数据库 lock/schema/close 异常统一 503，不泄漏 SQL、path、hash、lease 或 raw exception；连接可重新打开迁移。

### 12.5 Gateway 临时限流

1. 只有 override 无永久规则时也执行 RPM/TPM/concurrency。
2. 永久 100 RPM + 临时 10 RPM 的有效值为 10；提交 200 且其他维度无收紧在 Control 被 409 拒绝。
3. 多个 active override 每字段取最小值；release 一条仍保留其他最严格值。
4. 其他 tenant 同形 Key、同 tenant 其他 Key、不同 Environment 不受影响。
5. 到期后无需重启自动恢复永久限制；release 立即恢复，mitigated 历史保留。
6. disabled/改绑 Key 不能新建覆盖；已有覆盖只按其不可变 tenant+Key 和有效期执行。
7. 计数存储不可用且存在有效规则返回 503，不静默放行。
8. 命中返回 429，Provider attempt 为 0，失败 request/trace 可追踪且日志无敏感值。
9. loader 每次成功/失败都关闭 SQLite 连接，Windows 下数据库可立即迁移/删除临时副本。

### 12.6 Console

1. admin 显示 PAGE-077/078 和动作；developer/viewer 无导航、手写 hash 不预取。
2. 卡片显示规则、不可变证据、建议动作、影响范围、input quality 和 effective mitigation。
3. dismiss reason 表达误报；无实际收紧、过期、Key disabled 等 409 能恢复表单而不伪造成功。
4. canonical micros 保真格式化，恶意名称/ID 转义，统一错误 code/request_id 可见。
5. action owner、same-key retry、stale response、401/离页/换租户/降权清理正确。
6. cursor、action 草稿和幂等键不进入 URL、Storage 或 DOM data。
7. 1280x720 与 390x844 无重叠、截断、横向溢出，按钮/输入文字不超出容器。

### 12.7 真实 Runtime/Browser

1. 在开发库备份副本从 0042 实际升级 0043，校验 migration count、最高版本、integrity/FK；不直接污染唯一开发库证据。
2. 启动 Gateway、Control API、Console 和本地 Webhook receiver，健康检查均 200。
3. 构造可追溯 baseline/current 正式 Usage Charge，通过一次性 detector tick 生成 tenant/key/off-hours anomaly，不直接 INSERT anomaly。
4. admin GET/PATCH 实际成功；developer/viewer 真实 403、跨租户真实 404、统一 request_id/trace_id/no-store 正确。
5. admin apply 临时 RPM，Gateway 后续请求真实 429 且 Provider attempt 0；release 后真实恢复。expiry 由同执行器注入时钟集成证明。
6. `cost.anomaly_detected` 签名投递、pending resume、去重和安全 payload 通过本地 receiver 验证。
7. PAGE-077/078 在桌面与移动端真实浏览器验收，并检查控制台错误、横向溢出和旧租户 DOM。
8. 登出并注销全部 Session，停止 receiver/三服务，端口归零。
9. Runtime 数据库 checkpoint、WAL/SHM、日志错误/敏感值、迁移/事实 hash 做静止终态审计。

## 13. 代码修改计划

1. 新增 `0043_cost_anomaly_detection.sql/.down.sql`，包含四表、owner/immutability/CAS、Audit 保真重建与完整 down guard。
2. 在 shared config/errors/i18n/webhooks 增加有界配置、统一错误、精确事件合同和 pending delivery resume。
3. 新增 Control `cost_anomalies` models/detector/service/routes/scheduler，并在 app 注册 router/lifespan。
4. 新增 admin-only `require_cost_anomaly_reader/writer`；routes 使用 raw wire parser 保证 RBAC-before-body/query。
5. 扩展 Audit action/object/summary 白名单，动作同事务、提交后 dispatch。
6. 扩展 Gateway RateLimitRule loader 合并有效 override，并修复 loader 所有路径连接关闭。
7. 新增 Console API client、页面 owner/state、PAGE-077/078 renderer、admin-only 导航和响应式交互。
8. 新增 Shared/Control/Gateway/迁移/Console 专项及必要相邻回归。
9. 更新 API 文档、README、目录说明、测试记录与 Runtime/Browser 验收器。

## 14. 聚焦验证策略

所有测试只在 `D:\MetaHub` 沙箱内运行：

```powershell
$env:TEMP='D:\MetaHub\.tmp'
$env:TMP='D:\MetaHub\.tmp'
$env:PYTHONPATH='shared-sdk\python;control-plane-api\src;gateway-data-plane\src;examples\python\src'
```

执行顺序：

1. 纯规则和 0043 migration 单点。
2. Shared Webhook resume/错误合同与 Control API/scheduler 专项。
3. Gateway 临时限流专项。
4. Console API/state/render/lifecycle 专项与 lint。
5. 只扩展到受影响的 FinOps、预算、限流、Webhook、auth、audit、console 相邻回归。
6. 候选稳定后只执行一次所需全量回归，不因单个局部问题反复从头运行。
7. 中文注释门禁、开发库副本实迁移、真实 Runtime/Browser 和停服终态。

每个 pytest 批次使用独立 `D:\MetaHub\.tmp\prd053-*` basetemp；不使用 `C:\tmp`，不申请沙箱外权限。局部失败先复现最小节点，修复后只跑直接节点及受影响合同。

## 15. 编码前自审记录

### 第一轮：算法、账本、边界与兼容性

- 状态：2026-07-27 已完成，无未关闭 P0/P1。
- 发现并修正正式金额条件遗漏 `price_evidence_status`，明确 unpriced/unknown 不得按 0；完整性从 Environment 粗粒度改为 Environment + 逐 Key，避免坏 Key 静默屏蔽可信 Key。
- 发现对齐 30 分钟桶会在 10:29/10:30 重复告警，改为 exact-window 唯一 + `BEGIN IMMEDIATE` 内滚动 30 分钟查询。
- 冻结最近 7 天有消费候选、相关 25 小时 watermark、迟到分录重评和 run revision/claim token CAS，旧 worker 不得覆盖新结果。
- 累计金额改为 Python 任意精度 + canonical TEXT，避免 SQLite SUM 和 JavaScript Number 溢出。
- 解除 action/override 潜在循环外键：apply action 先写、override 反向引用；release action 才保存 target override。
- 临时限流改为“至少一个维度实际收紧”，补齐无永久规则、多 overlay、Key disabled/改绑、24 小时 eligibility、release 后不可复用同 anomaly。
- 冻结内部可注入时钟和一次性 detector tick，使 120 秒闭合、5 分钟 TTL 与真实 Runtime 都可稳定验收而不直写 anomaly。

### 第二轮：权限、租户、事务、错误与隐私

- 状态：2026-07-27 已完成，无未关闭 P0/P1。
- 依据 Excel “财务/安全”和现有 PRD-051/Audit 映射，将 reader/writer 均收紧为 admin-only；不提前伪造 finance/security 角色。
- 冻结 route 无 typed Query/Body/Header、先 Depends 后 raw wire 解析，补齐重复 Header、8 KiB 流式上限、strict integer/UTF-8/JSON 和非法 path 同形 404。
- 幂等 fingerprint 加入 schema + path anomaly ID + action + 规范化参数，并明确 replay 返回首次 immutable action + 当前 tenant-qualified 状态，避免跨 anomaly 错放响应。
- 逐条冻结 run/anomaly/action/override、Environment/Key/request/source ledger 的 owner 图，以及 INSERT/UPDATE/REPLACE/DELETE 数据库旁路。
- 补齐四类 Audit action、object/summary 白名单、0043 Audit 保真重建和 down 前置 guard，确保 action/anomaly/override/Audit 同事务可实际执行。
- 发现公共 Webhook dispatcher 对“intent commit 后、HTTP 前崩溃”的 pending 行不会续发；补充 source outbox lease/CAS、pending 从 attempt_count+1 resume、部分 Endpoint 和 at-least-once 合同。
- 明确 micros/ratio Webhook wire、ID 精确长度、detected_at、event 状态/退避/上限和敏感键集合。
- 把 detection run、Webhook attempt、cost Audit 纳入 down guard；Gateway loader/API/scheduler 连接关闭和 Windows 文件句柄测试纳入验收。
- 编码门禁结论：两轮自审列出的 P0/P1 均已在正文形成可测试合同，允许进入实现。

## 16. 实现与验收结论

- 2026-07-27 已完成 0043、确定性 detector、admin-only GET/PATCH、source outbox、Gateway 临时限流、PAGE-077/078 及相关文档；tenant/actor 始终从认证上下文恢复，后端 RBAC、统一错误和 `request_id/trace_id` 已由专项与真实运行验证。
- Python 根级全量实际结果为 `1178 passed / 30 failed`；30 个失败均定位为 Windows 长 basetemp 或 PRD-052/新增迁移后的历史测试合同，未重跑一小时全量，随后用 `34/34` 受影响测试完成原失败闭环。Node `33/33`、Console `401/401`、lint `134 files`、中文注释门禁均通过。
- [Runtime 证据](../../artifacts/prd053/20260727T183515/runtime-acceptance.json) 与 [Browser 证据](../../artifacts/prd053/20260727T183515/browser-evidence.json) 均为 `status=passed`；双视口五状态共 10 张正式 PNG。独立 Runtime 库为 43/0043，原开发库按接手约束保持 42/0042。
- [最终审计](../../artifacts/prd053/20260727T183515/final-audit.json) 为 `status=passed`：Session `44/44` revoked、三端口关闭、两库 checkpoint `[0,0,0]`、integrity `ok`、外键违规 0、sidecar 和 spool 均为空；`.tmp` 的 483 个链接节点删除后 483 个目标全部保留。
- 完整测试分级、诊断证据排除和哈希见 [PRD-053 测试记录](../testing/PRD-053-tests.md)。该结论仅表示 PRD-053 完成，全部 108 条 PRD 仍在进行。
