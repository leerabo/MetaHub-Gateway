# PRD-048 效果-成本路由基础设计

> PRD：PRD-048  
> 里程碑：M1-企业POC  
> 优先级：P1 / 核心层  
> 状态：已完成；设计、两轮自审、实现、专项/全量回归、0037 迁移、真实三服务和三档浏览器验收均通过  
> 依赖：PRD-015、PRD-016、PRD-017、PRD-031、PRD-035、PRD-036、PRD-044、PRD-045、PRD-046、PRD-047  
> 页面：PAGE-068 路由策略页、PAGE-069 模型评分页  
> API：API-227/228/229，`GET/PATCH /model-quality-scores`  
> 数据对象：OBJ-068 `ModelQualityScore`  
> 事件：EVT-054 `model.feedback_recorded`  
> 验收：TC-0095、TC-0096

## 1. 需求理解

PRD-048 在 PRD-047 已有 `fixed`、`cost_first`、`availability_first` 三种 Alias 初始路由模式之上，新增 `quality_cost` 模式。该模式先沿用模型、Provider、健康和 KeyPool 资格过滤，再使用模型质量、请求成本和历史延迟计算可解释的综合分，解决“低质量模型只因价格便宜而被无条件选中”的问题。

本期必须完成：

1. 按当前租户和实际调用模型累计成功数、错误数、样本数与平均延迟；人工评分由管理员维护。
2. 为每个 ModelAlias 的 RoutingPolicy 配置质量、成本、延迟三项手动权重、质量门槛和最小样本数。
3. 新增 `quality_cost` 确定性路由算法，明确冷启动、低质量、缺价、混币、缺延迟和同分规则。
4. 每次动态路由冻结 Policy、统计版本、三项分量、总分和原因码，调用日志可解释。
5. 提供模型评分查询/修改 API 和 PAGE-069；扩展 PAGE-068 的路由策略编辑器。
6. 人工评分真实变化时，同事务记录审计与 `model.feedback_recorded` 事件，提交后分发 Webhook。
7. Policy Dry-Run 复用同一公式和同一冷启动规则，不产生 Provider、Usage 或统计副作用。

本期明确不做：

- 不实现自动评测模型、在线学习、滑动窗口、时间衰减、探索流量或自动调权。
- 不实现 PRD-049 的逐请求 `rating/accepted/tags` Feedback API、采纳率、满意度或日志详情反馈入口。
- 不实现 PRD-075 的 A/B 实验、流量分桶或实验统计。
- 不改变 direct model、Policy `route_to`、Provider 健康探测和 PRD-036 运行时 Fallback 的既有职责。
- 不把 OpenRouter、硅基流动登记为默认上游模型厂商。

## 2. 指标来源与归因口径

### 2.1 非流式请求

`routing_attempts` 是非流式模型质量的权威来源，一次实际主调用和一次实际备用调用分别归因。运行时必须由调用方显式给出资格；历史迁移只能使用以下固定、可重建白名单：

- `status='succeeded'` 且 `usage_status!='not_applicable'`：成功样本。
- `status='failed' AND usage_status='unavailable'`，并且 `error_category` 属于 `timeout/rate_limited/upstream_5xx`，或 `error_code='gateway.upstream_transport_failed'`：错误样本。
- `usage_status='not_applicable'`，或鉴权、配额、模型映射、请求参数、Key、Adapter 配置、能力、本地 URL/协议错误：不计入模型质量。`InvalidURL` 即使由 Adapter 包装成 `UpstreamApiError` 也不得仅凭异常类计分。
- `status='started'`：结果未知，不计入成功或错误。

失败 attempt 若命中上述固定质量码/分类却没有 `usage_status='unavailable'`，表示持久事实互相矛盾；迁移和降级重建必须失败关闭，不能静默排除或猜测。

不能从 `request_logs` 最终状态倒推模型质量，否则“主模型失败、备用模型成功”会把主失败丢失或错归给备用模型。

### 2.2 流式请求

`stream_sessions` 是流式质量来源：

- `completed`：成功样本。
- `timed_out`：错误样本。
- `upstream_failed` 只有固定码 `gateway.upstream_rate_limited`、`gateway.upstream_timeout`、`gateway.upstream_stream_timeout`、`gateway.upstream_stream_failed` 才是错误样本。
- `gateway.upstream_unavailable`、`gateway.upstream_error`、鉴权/配额/模型/请求错误、本地 `gateway.stream_session_unavailable`、`gateway.stream_processing_unavailable`、空值和未知码均保守排除，不能凭 `upstream_failed` 状态猜测质量归因。
- `client_disconnected`：客户端行为，不计入模型质量；`starting/active` 非终态不计入。

流式延迟本期使用 `completed_at - started_at` 的完整会话耗时。运行时使用同一组 UTC 时间计算；历史迁移只接受 20 位秒精度或 27 位六位微秒精度的规范 UTC `Z` 文本，以“整数 epoch 秒差乘 1,000,000，加六位微秒差，再整数除以 1,000”向下截断为毫秒。首块延迟属于后续更细的流质量指标，本期不伪造不存在的历史字段。

### 2.3 聚合策略

- M1 使用 lifetime 累计值；不做窗口、衰减、探索或自动学习。
- 调用路径必须显式传入 `quality_observation_eligible`，不能只凭 `failed/upstream_failed` 状态或异常父类猜测；只有成功、超时、429、5xx 和明确非本地网络传输失败为 true。鉴权、配额、模型映射、请求参数、本地 URL/协议、DLP、预算、KeyPool、Adapter 构造、审计闸门和客户端断开均为 false。
- attempt/session 首次进入终态时，先在事务内确认条件更新恰好命中一行，再通过共享 observation helper 原子递增 Score，最后提交；重复完成或并发清理的 `rowcount=0` 分支绝不能再次累计。
- 统计更新失败时 attempt/session 终态和 Score 增量整体回滚，由现有安全 warning 暴露，但不能覆盖已经取得的 Provider 响应；迁移和运维修复可从已持久终态重新聚合，不能从最终 request_log 猜测主备结果。
- 人工评分版本 `feedback_version` 与自动统计版本 `statistics_version` 完全分离，调用流量不能制造管理员 CAS 冲突。
- 成功率、错误率和平均延迟由整数计数派生，不在数据库中重复保存 float。
- 乱序终态提交时 `first_observed_at` 取历史与本次观察的最小值，`last_observed_at` 取最大值，`updated_at` 取已有创建/更新时间与最新观察时间的最大值；统计 trigger 禁止时间范围收缩。
- 质量归因与预算结算是两条独立账：明确在 Provider 前失败或可证明为本地 URL/协议错误的分项结算为 0；可能已经到达上游但尚无 Usage 的分项继续保留冻结估算。不得因为“不计模型质量”就一律清零预算，也不得因为异常包装为 `UpstreamApiError` 就把可证明未发出的请求计为 outstanding。

## 3. 涉及模块

| 模块 | 变更 |
| --- | --- |
| `shared-sdk/python/metahub_shared/runtime/routing_policy.py` | 增加质量事实、配置、分阶段质量门槛和 `quality_cost_v1` 纯计算合同 |
| `gateway-data-plane/.../routing/models.py` | 同一租户事务冻结 Policy、Score、价格与候选解释，保存 Decision/Candidate |
| `gateway-data-plane/.../quality/observations.py` | 在调用方事务中执行 tenant-qualified 单样本增量，非流式/流式共用 |
| `gateway-data-plane/.../routing/attempts.py` | commit 前验证唯一终态跳转，显式决定真实上游样本并原子累计 |
| `gateway-data-plane/.../streaming/sessions.py` | 幂等终态、完整会话耗时和显式样本资格；排除断开/本地故障 |
| `control-plane-api/.../model_quality_scores/` | 新增 GET/PATCH DTO、严格正文解析、CAS、审计、事件和租户服务 |
| `control-plane-api/.../model_aliases/` | 扩展 RoutingPolicy DTO 和 PATCH，新增 `quality_cost` 配置 |
| `control-plane-api/.../policies/dry_run_service.py` | 使用当前统计快照复用 shared 公式，无副作用预演 |
| `control-plane-api/.../request_logs/` | 返回每条 Candidate 的质量/成本/延迟分量和版本 |
| `control-plane-api/.../auth`、`audit`、`console`、`app.py` | 后端权限、审计白名单、导航和路由注册 |
| `shared-sdk/.../errors.py`、`error_diagnosis.py`、`i18n.py`、`webhooks.py` | 统一错误、诊断、双语标签和严格事件合同 |
| `console-web/src/lib/modelQualityScores.js` | 模型评分 API client，不发送 tenant_id 或 PRD-049 字段 |
| `console-web/src/features/model-quality-scores/` | PAGE-069 评分列表、人工评分编辑与权限状态机 |
| `console-web/src/features/routing-policies/` | PAGE-068 增加 `quality_cost` 分段选项、权重和门槛 |
| `console-web/src/features/request-logs/` | 展示冻结公式版本、证据状态、分量和最终分 |
| `infra/migrations/0037_quality_cost_routing*.sql` | 新表、Model 默认 Score/字段转换 trigger、历史质量重建、硬约束表重建和可逆/拒绝回滚边界；不创建 attempt/session 自动统计 trigger |

## 4. 数据表和字段设计

### 4.1 `model_quality_scores`

每个租户内模型恰有一行，不能跨租户复用评分或统计。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `model_quality_score_id` | TEXT PK，`mqs_` + 32 lowercase hex | 评分对象 ID |
| `tenant_id` | TEXT NOT NULL | 租户边界；与 Model 使用复合外键 |
| `model_id` | TEXT NOT NULL | 实际模型 ID；`UNIQUE(tenant_id, model_id)` |
| `status` | TEXT，固定 `active` | OBJ-068 基础状态；本期不增加评分停用状态机 |
| `sample_count` | INTEGER >= 0 | 成功与错误样本总数 |
| `success_count` | INTEGER >= 0 | 成功样本数 |
| `error_count` | INTEGER >= 0 | 错误样本数 |
| `latency_sample_count` | INTEGER，`0..sample_count` | 有合法延迟的样本数 |
| `total_latency_ms` | INTEGER >= 0 | 延迟总和；平均值由此派生 |
| `manual_quality_bps` | INTEGER NULL 或 `0..10000` | 人工质量分，基点表示 0.00%-100.00% |
| `statistics_version` | INTEGER >= 0 | 每个可计数终态严格 +1 |
| `feedback_version` | INTEGER >= 1 | 仅人工评分真实变化严格 +1 |
| `first_observed_at` | TEXT NULL | 首个可计数样本时间 |
| `last_observed_at` | TEXT NULL | 最近可计数样本时间 |
| `feedback_updated_by` | TEXT NULL | 最近人工评分管理员，复合租户外键 |
| `feedback_updated_at` | TEXT NULL | 最近人工评分时间 |
| `created_at/updated_at` | TEXT NOT NULL | 创建与最近任意变化时间 |

成组约束：

- `sample_count = success_count + error_count`。
- `statistics_version = sample_count`；本期每个纳入样本只形成一次原子增量。
- 无样本时 `first_observed_at/last_observed_at` 均为空；有样本时两者均非空。
- `manual_quality_bps IS NULL` 时允许 `feedback_updated_*` 记录“清除评分”的最后操作者和时间。
- Model 新建 trigger 自动创建默认 Score 行；迁移为所有存量 Model 回填一行。
- identity、tenant、model、created_at 不可修改；删除禁止；人工 PATCH 不得写统计字段，Gateway observation helper 只允许按合法单样本增量更新。
- 字段级转换 trigger 只接受两类互斥 UPDATE：单样本统计增量，或人工评分/`feedback_version` 单步更新；禁止混合修改、计数重置、版本跳跃和直接改写 owner 字段。Score 整行不是 immutable 对象。
- 统计样本允许乱序提交，但时间边界只能扩展：`first_observed_at=MIN(old, observed_at)`、`last_observed_at=MAX(old, observed_at)`；`updated_at` 必须等于创建时间、既有更新时间和最新观察时间三者的最大值。

### 4.2 `model_quality_score_events`

只记录管理员人工评分真实变化，自动样本不会产生高频 Webhook。

| 字段 | 说明 |
| --- | --- |
| `model_quality_score_event_id` | `mqe_` + 32 lowercase hex，稳定事件 ID |
| `tenant_id/model_quality_score_id/model_id` | 复合 owner 图 |
| `actor_user_id` | 必须是同租户认证管理员 |
| `request_id/trace_id` | 链路标识；`UNIQUE(tenant_id, actor_user_id, request_id)` |
| `request_fingerprint` | 绑定 model、规范化评分和 expected version 的 SHA-256 |
| `feedback_version/statistics_version/sample_count` | 提交时冻结版本与样本数 |
| `previous_manual_quality_bps/manual_quality_bps` | 前后人工评分，可空 |
| `payload_json` | `model.feedback_recorded` 精确安全载荷，最大 8 KiB |
| `created_at` | 事件时间；整行 immutable |

事件精确 payload：

```json
{
  "trigger": "manual_score_updated",
  "request_id": "req_xxx",
  "model_quality_score_id": "mqs_xxx",
  "model_id": "model_xxx",
  "previous_manual_quality_bps": 7000,
  "manual_quality_bps": 8500,
  "feedback_version": 3,
  "statistics_version": 42,
  "sample_count": 42
}
```

载荷不得包含 Prompt、Response、请求正文、rating、accepted、tags、Provider URL/Key、Virtual Key、Token、错误正文或模型输出。

### 4.3 扩展 `model_alias_routing_policies`

新增模式 `quality_cost` 和以下 Alias 级配置：

| 字段 | 默认值 | 约束 |
| --- | --- | --- |
| `quality_weight_bps` | 5000 | `1..10000` |
| `cost_weight_bps` | 3000 | `0..10000` |
| `latency_weight_bps` | 2000 | `0..10000` |
| `quality_floor_bps` | 6000 | `1..10000` |
| `minimum_sample_count` | 20 | `1..10000` |

三项权重之和必须为 10000。配置始终保存在 Policy 上，旧模式不执行这些字段；任何 mode 或配置真实变化共用同一个 `version + 1`、CAS、审计和配置事件事务。

RoutingPolicy 的数据库版本 trigger 必须覆盖 mode、三项权重、质量门槛、最小样本数和 status：任一真实 Policy 执行字段变化严格 `version + 1`。同时必须保留 PRD-047 Alias 图 trigger 的合法 `version-only +1`，因为候选和 Alias 状态存放在父表；0037 不能拦截该路径，也不能改变旧三模式的 CAS/事件语义。迁移只回填休眠默认配置，任何存量 Alias 都保持原模式和原版本，绝不自动切换到 `quality_cost`。

### 4.4 扩展 `routing_decisions`

仅 `quality_cost` 决策冻结：

- `quality_weight_bps/cost_weight_bps/latency_weight_bps`
- `quality_floor_bps/minimum_sample_count`
- `quality_as_of`
- `quality_formula_version='quality_cost_v1'`
- `quality_scoring_performed`：是否完成三项加权；冷启动或依赖缺失回退为 0

旧三模式上述字段必须全部为空；Policy ID/version/mode、价格 as_of、Token 估算继续沿用 PRD-047 语义。`quality_cost` 且成本权重为 0 时不读价格或 Token，对应价格字段保持空。

### 4.5 扩展 `routing_candidates`

每条候选增加：

- `model_quality_score_id`
- `quality_statistics_version/quality_feedback_version`
- `quality_evidence_state`：`ready/cold/not_evaluated`
- `quality_sample_count/quality_success_count/quality_error_count`
- `manual_quality_bps`
- `average_latency_ms`：规范 Decimal 文本，避免 float
- `quality_component_bps/cost_component_bps/latency_component_bps`
- `weighted_score_bps`

旧模式或在通用资格阶段已拒绝的候选为 `not_evaluated` 且质量字段成组为空。`quality_cost` 已加载质量的候选必须冻结 Score ID、两个版本和计数；只有实际执行加权时才保存三个分量与总分。

### 4.6 attempt/session 终态事务边界

不新增仅靠状态推断的统计 trigger。Gateway 的两个终态函数调用同一 `record_model_quality_observation(connection, ...)` helper：

1. 显式开启写事务，按 tenant + attempt/session ID + 非终态状态做条件更新。
2. 在 commit 前检查 `rowcount == 1`；不等于 1 时回滚，幂等同终态只返回既有结果，不累计第二次。
3. 只有调用方明确给出 `quality_observation_eligible=true` 才按 tenant + model 更新 Score；更新必须恰好命中一行。
4. 非流式成功计 success；失败只有显式资格为 true 且命中超时、429、5xx 或专用网络传输码才计 error。异常类本身不构成资格，`not_applicable`、鉴权/配额/模型/请求和本地 URL/协议错误不计。
5. 流式 completed 计 success，真实上游握手错误/timeout/read failure 使用固定可重建码计 error；`client_disconnected`、本地流处理错误和 `mark_stream_session_active` 审计故障不计。
6. 流式延迟由内存 UTC 时间差计算为非负整数毫秒；迁移使用与其一致的整数微秒差除以 1000，禁止 `julianday`/浮点 `ROUND` 造成边界漂移。
7. Score 增量和终态更新一起 commit/rollback；任何异常日志只含固定事件、tenant/request/trace/model ID 和异常类型。

`quality_observation_eligible` 只存在于函数参数和内存控制流，本期不新增可被状态误读的持久布尔列。0037 绝不创建 attempt/session 自动统计 trigger，避免与 observation helper 双计；迁移历史回填使用第 11.1 节的独立固定分类。

## 5. `quality_cost_v1` 计算合同

### 5.1 质量分

使用 20 个先验样本缓解冷启动波动：

```text
prior_bps = manual_quality_bps（存在时），否则 5000
quality_component_bps = ROUND_HALF_UP(
  (success_count * 10000 + 20 * prior_bps) / (sample_count + 20)
)
```

`manual_quality_bps` 是人工先验，不直接覆盖真实成功/失败。所有计算使用整数或 `Decimal`，禁止 float。

### 5.2 证据状态与质量门槛

- `manual_quality_bps` 非空，或 `sample_count >= minimum_sample_count`：`ready`。
- 其他情况：`cold`。
- `ready` 且 `quality_component_bps < quality_floor_bps`：先于价格读取直接拒绝。
- 恰好等于门槛视为通过。

冷启动规则：

1. 两条都 cold：不读价格/Token，按 `configured_priority, model_id` 选择，原因 `routing.quality_cost_cold_start_priority`。
2. 一条 ready 且通过门槛、一条 cold：ready 优先，原因 `routing.quality_cost_evidence_priority`，cold 不能因便宜抢占流量。
3. 已知低质候选被拒绝后只剩 cold：允许 cold 作为唯一候选，明确记录单候选原因。
4. 所有 ready 候选均低于门槛且无 cold：不调用 Provider，返回 `gateway.no_route_available`。

### 5.3 成本分

成本仍使用 PRD-047 同一请求 Token 估算和同一 `as_of` PricePlan/带版本目录价：

```text
cost_component = min_candidate_cost / candidate_cost
cost_component_bps = ROUND_HALF_UP(cost_component * 10000)
```

- 两条成本均为 0：两者均 10000。
- 仅一条为 0：零成本候选 10000，另一条 0。
- `cost_weight_bps=0`：不扫描 Token、不读价格，分量记 `not_evaluated`。
- 成本权重大于 0 但缺价或混币：不把缺失当 0、不重新分配权重，按稳定优先级回退并记录明确原因。

### 5.4 延迟分

```text
average_latency = total_latency_ms / latency_sample_count
latency_component = min_average_latency / candidate_average_latency
latency_component_bps = ROUND_HALF_UP(latency_component * 10000)
```

- 两条平均延迟均为 0：两者均 10000。
- 仅一条为 0：零延迟候选 10000，另一条 0。
- `latency_weight_bps=0`：不要求延迟证据，分量记 `not_evaluated`。
- 延迟权重大于 0 且任一参与评分候选缺少延迟：不把缺失当 0、不重分权，按稳定优先级回退并记录原因。

### 5.5 总分与稳定平局

```text
weighted_score_bps = ROUND_HALF_UP(
  (
    quality_component_bps * quality_weight_bps
    + cost_component_bps * cost_weight_bps
    + latency_component_bps * latency_weight_bps
  ) / 10000
)
```

按 `weighted_score_bps DESC, configured_priority ASC, model_id ASC` 排序。总分相同使用稳定平局原因，不依赖字典顺序、数据库默认顺序或 float 误差。

`quality_scoring_performed=1` 的唯一含义是：全部正权重分量均已计算，并为每条参与评分候选生成 `weighted_score_bps`。零权重分量必须保持 `NULL/not_evaluated`，不伪造为 0 分；单候选、冷启动优先和依赖缺失回退均为 0。

### 5.6 运行时 Fallback

- `quality_cost` 初始选中配置 fallback 候选属于正常初始路由，不产生 `route.fallback_triggered`。
- 只有初始选中 default，且配置 fallback 为 `ready`、通过质量门槛并排为 standby 时，才能冻结 PRD-036 运行时 Fallback。
- low-quality、cold 或通用资格 rejected 的 fallback 不得在主请求失败后重新进入。

### 5.7 确定性状态矩阵与依赖顺序

所有集合先按 `configured_priority, model_id` 稳定排序；`rank` 只属于最终可执行顺序，rejected 候选必须为 `rank=NULL`。

| 通用 eligible/质量组合 | 最终状态 | 选择原因 | 依赖读取 |
| --- | --- | --- | --- |
| 0 条通用 eligible | 全部 `rejected` | 沿用模型/Provider/健康/KeyPool 固定原因；Decision 为 no-route | 不读 Score、Token、价格 |
| 1 条，cold | 该条 `selected rank=1` | `routing.quality_cost_single_eligible_candidate` | Score 一次；不读 Token/价格 |
| 1 条，ready-pass | 该条 `selected rank=1` | 同上 | Score 一次；不读 Token/价格 |
| 1 条，ready-low | 该条 `rejected rank=NULL`，no-route | `routing.candidate.quality_below_floor` | Score 一次；不读 Token/价格 |
| 2 条，均 ready-low | 两条 `rejected rank=NULL`，no-route | 同上 | Score 一次；不读 Token/价格 |
| 1 cold + 1 ready-low | cold `selected rank=1`；low rejected | 单候选原因 / low 原因 | Score 一次；不读 Token/价格 |
| 1 ready-pass + 1 ready-low | pass `selected rank=1`；low rejected | 单候选原因 / low 原因 | Score 一次；不读 Token/价格 |
| 2 条均 cold | 稳定第一条 selected、第二条 `standby rank=2` | `routing.quality_cost_cold_start_priority`；standby 用通用 standby 原因 | Score 一次；不读 Token/价格 |
| 1 ready-pass + 1 cold | ready selected；cold `standby rank=2` | `routing.quality_cost_evidence_priority` / `routing.candidate.quality_cold_start_deferred` | Score 一次；不读 Token/价格 |
| 2 条均 ready-pass | 进入依赖检查和加权 | highest/tie 或固定回退原因 | 见下方固定顺序 |

两条 ready-pass 的固定顺序：

1. 若 `latency_weight_bps>0`，先检查两条 Score 中的延迟证据；任一缺失立即按稳定优先级回退为 `routing.quality_cost_fixed_fallback.latency_missing`，不扫描请求正文、不读价格。
2. 若 `cost_weight_bps>0`，Token estimator 恰好调用一次，随后 price loader 对两条候选恰好调用一次。任一缺价优先返回 `price_missing`；价格均存在但币种不同再返回 `currency_incomparable`。
3. 对所有正权重分量计算 bps；零权重分量保持 `NULL/not_evaluated`。生成总分后才把 `quality_scoring_performed` 设为 1。
4. 总分不同时按最高分选择；相同按稳定优先级选择。selected 为 rank 1，另一条为 standby rank 2。

因此同时存在缺延迟与缺价时，延迟缺失优先且价格回调次数为 0；价格缺失优先于混币。quality loader 在至少一条通用 eligible 时只调用一次，旧三模式绝不调用它。

## 6. API 设计

### 6.1 `GET /model-quality-scores`

权限：`admin/developer` 可读；`viewer` 和匿名后端拒绝。请求体和查询参数均不能指定 tenant_id。

查询参数：

- `model_id`：可选，同租户精确过滤。
- `limit=1..200`，默认 100。
- `offset>=0`，默认 0。

未知参数、重复参数、非法枚举或越界分页返回 `model_quality_score.query_invalid`。指定不存在或跨租户 `model_id` 统一返回 404，防止资源枚举。

响应：

```json
{
  "model_quality_scores": [
    {
      "model_quality_score_id": "mqs_xxx",
      "tenant_id": "tenant_xxx",
      "model_id": "model_xxx",
      "internal_model_id": "deepseek-chat",
      "display_name": "DeepSeek Chat",
      "provider_code": "deepseek",
      "provider_name": "DeepSeek",
      "model_status": "active",
      "sample_count": 42,
      "success_count": 40,
      "error_count": 2,
      "latency_sample_count": 42,
      "total_latency_ms": 42000,
      "success_rate_bps": 9524,
      "error_rate_bps": 476,
      "average_latency_ms": "1000.000",
      "manual_quality_bps": 8500,
      "effective_quality_bps": 8355,
      "observation_status": "observed",
      "formula_version": "quality_cost_v1",
      "statistics_version": 42,
      "feedback_version": 3,
      "first_observed_at": "2026-07-18T00:00:00Z",
      "last_observed_at": "2026-07-18T01:00:00Z",
      "feedback_updated_by": "user_xxx",
      "feedback_updated_at": "2026-07-18T01:10:00Z",
      "created_at": "2026-07-18T00:00:00Z",
      "updated_at": "2026-07-18T01:10:00Z"
    }
  ],
  "total": 1,
  "limit": 100,
  "offset": 0,
  "as_of": "2026-07-18T01:11:00Z",
  "request_id": "req_xxx",
  "trace_id": "trace_xxx"
}
```

响应设置 `Cache-Control: no-store`，不返回 Endpoint、KeyPool 数量、凭据、Prompt 或 Response。

派生比率统一使用整数 `ROUND_HALF_UP`；为保证展示恒等式，`error_rate_bps` 固定等于 `10000 - success_rate_bps`，不能分别舍入后得到 10001。`observation_status` 只表示 `observed/unobserved`，不冒充任一 Alias Policy 的 `ready/cold`。

### 6.2 `PATCH /model-quality-scores`

权限：仅 `admin`。必须先认证授权，再读取最多 8 KiB 的 UTF-8 `application/json`；拒绝重复键、超深/超宽结构、未知字段和非严格类型。

请求：

```json
{
  "model_id": "model_xxx",
  "manual_quality_bps": 8500,
  "expected_feedback_version": 2
}
```

`manual_quality_bps:null` 表示清除人工评分。禁止 `tenant_id`、`actor_id`、统计字段以及 PRD-049 的 `request_id/rating/accepted/tags`。

写语义：

- 锁内按认证 tenant + model 查询，跨租户与不存在统一 404。
- `expected_feedback_version` 必须等于锁内版本；不匹配返回 409。
- 同值且版本匹配：`changed=false`，不加版本、不写 Audit/Event/Webhook。
- 真实变化：`feedback_version + 1`，自动统计版本不变；同事务写审计和 `model_quality_score_events`。
- 相同 tenant + actor + `X-Request-ID` + 指纹恢复同一已提交结果；相同 request_id 不同意图返回 409。
- 审计或事件失败整体回滚；提交后才分发 Webhook。

恢复已提交人工 mutation 时，只要求当前 `feedback_version` 仍等于事件版本；期间 `statistics_version` 增长不构成冲突，响应返回当前最新统计和同一人工评分事实。后续人工评分已经再次变化时，旧 request_id 返回 `model_quality_score.request_conflict`。

响应包含 `model_quality_score`、`changed`、`request_id`、`trace_id`。

### 6.3 扩展 `PATCH /model-aliases/{model_alias_id}/routing-mode`

旧三模式保持原有两字段请求兼容：

```json
{"routing_mode":"cost_first","expected_version":4}
```

`quality_cost` 必须携带完整配置：

```json
{
  "routing_mode": "quality_cost",
  "expected_version": 4,
  "quality_cost_config": {
    "quality_weight_bps": 5000,
    "cost_weight_bps": 3000,
    "latency_weight_bps": 2000,
    "quality_floor_bps": 6000,
    "minimum_sample_count": 20
  }
}
```

- 旧模式禁止携带 `quality_cost_config`；切回旧模式保留数据库中的上次配置但不执行。
- `quality_cost` 模式下配置必须完整，不能靠服务端合并缺字段后改变管理员意图。
- mode 和配置任一真实变化只推进一个 Policy 版本；同值 no-op。
- Policy 响应始终返回当前 `quality_cost_config`，供再次启用时显式确认。

### 6.4 RoutingPolicy 配置事件精确合同

0037 为 `routing_policy_events` 增加 `payload_schema_version`：存量事件回填 1，新服务只允许写 2。数据库可读取 v1 历史，但禁止 0037 代码新写 v1。

v2 `configuration_changed` payload 精确字段为：

```json
{
  "trigger": "configuration_changed",
  "request_id": "req_xxx",
  "model_alias_id": "ma_xxx",
  "routing_policy_id": "rpol_xxx",
  "routing_policy_version": 5,
  "configuration_schema_version": 2,
  "change_kinds": ["mode", "quality_cost"],
  "changed_routing_fields": ["routing_mode", "quality_cost_config"],
  "previous_mode": "cost_first",
  "routing_mode": "quality_cost",
  "quality_cost_config": {
    "quality_weight_bps": 5000,
    "cost_weight_bps": 3000,
    "latency_weight_bps": 2000,
    "quality_floor_bps": 6000,
    "minimum_sample_count": 20
  }
}
```

固定顺序：

- `change_kinds`：`created, mode, quality_cost, candidates, alias_state`。
- `changed_routing_fields`：`alias, environment, environment_id, status, default_model_id, fallback_model_id, routing_mode, quality_cost_config`。
- 创建事件仍只能是 `change_kinds=[created]`，previous_mode 为空，八个初始路由字段全部列入 changed fields，并携带 fixed + 默认休眠配置。
- 质量配置任一字段真实变化只产生一个逻辑 `quality_cost_config` changed field；mode 与配置同时变化时按上述顺序同时列出。
- Alias 图变化造成 Policy 合法 version-only +1 时，v2 事件携带当前配置快照，但 `change_kinds/changed_fields` 仍只描述 Alias/candidate 的真实变化。

运行时 `request_selected` payload 字段集合保持 PRD-047 完全不变，只把 `routing_mode` 白名单扩展为 `quality_cost` 并允许新增稳定原因码；权重、分量和统计不进入 Webhook，完整解释只保存在 tenant-qualified Decision/Candidate。v1/v2 配置事件均继续使用 `rpe_` 稳定 ID 和既有 actor/object/trace 信封绑定。

### 6.5 Dry-Run 与 RequestLog 响应合同

`RoutePreviewResult` / Policy Dry-Run 路由结果新增：

- `quality_as_of`
- `quality_formula_version`
- `quality_scoring_performed`
- `quality_weight_bps/cost_weight_bps/latency_weight_bps`
- `quality_floor_bps/minimum_sample_count`

`RouteCandidatePreview`、`RequestLogRoutingCandidate` 共用以下新增字段：

- `model_quality_score_id`
- `quality_statistics_version/quality_feedback_version`
- `quality_evidence_state`
- `quality_sample_count/quality_success_count/quality_error_count`
- `manual_quality_bps/average_latency_ms`
- `quality_component_bps/cost_component_bps/latency_component_bps`
- `weighted_score_bps`

成组规则：

1. 旧三模式 route-level 质量配置/as_of/formula 全为空、`quality_scoring_performed=false`；Candidate state 固定 `not_evaluated`，其余质量字段全为空。
2. `quality_cost` 在至少一条通用 eligible 时，route-level 配置、formula 和 as_of 非空；通用阶段 rejected Candidate 仍为 `not_evaluated`，已加载 Candidate 必须冻结 Score ID、两个版本、计数和质量分。
3. 只有全部正权重分量完成时 `weighted_score_bps` 非空且 `quality_scoring_performed=true`；零权重分量和未执行分量保持空。
4. `status/rank/reason_code` 严格服从第 5.7 节，不能由前端重排。
5. RequestLog 只读取不可变 Candidate/Decision 行，绝不 JOIN 当前 Score 后回算历史。

Dry-Run 在同一只读快照读取当前 Score/Policy/价格并复用 shared 算法；坏事实或 DB 失败继续映射 `policy.dry_run_unavailable`，不是 Gateway 错误。质量预演不写 Score、Routing Decision/Candidate、Usage，也不发布 `route.policy_selected` 或 `model.feedback_recorded`；PRD-046 已有 explanation/audit/`policy.dry_run_executed` 事件及 Webhook 语义保持不变。

## 7. 前端页面和交互设计

### 7.1 PAGE-068 路由策略页

- 模式使用四段 segmented control：固定、成本优先、可用性优先、质量成本。
- 选择质量成本后显示三项整数百分比输入/步进控件、质量门槛和最小样本数；前端把百分比精确换算为 bps，不使用浮点累计判断。
- 三权重实时显示合计，只有恰好 100% 且质量权重大于 0 才允许保存；后端仍重复校验。
- 复用现有 tenant/user/role/session/navigation 身份快照、operation/refresh 双代次、稳定 request_id、CAS 冲突显式 rebase 和迟到响应门禁。
- developer 只读；viewer 无导航、无 DOM、无 GET/PATCH 预取。

### 7.2 PAGE-069 模型评分页

- 导航 `model-quality-scores` 紧邻路由策略；admin/developer 可见，viewer 不可见。
- 展示模型、Provider、成功率、错误率、平均延迟、人工评分、有效质量分、证据状态和样本数。
- admin 可编辑/清除人工评分；developer 只读。
- 每行独立 draft/ticket，ticket 绑定 tenant、用户、角色、Session、导航、model、feedback version、draft 和稳定 request_id。
- 15 秒刷新不覆盖 dirty/busy 行；迟到 200/401/409、离页、登出、身份变化和 `pagehide` 均 abort/拒绝提交旧结果。
- 网络结果未知的重试必须复用原 request_id；明确 409 后只能重新加载并由用户确认。
- 所有服务端文本 HTML 转义；不在 DOM、URL、storage 或日志保存 Prompt、Response、Key 或评分请求正文。

### 7.3 响应式与可访问性

- 1280 桌面使用全宽紧凑表格；900 使用标签化网格；390 使用单列行布局。
- 不嵌套卡片，不制造页面级浮动卡片；输入和按钮使用稳定高度，长模型名允许换行。
- 必须验证无横向滚动、遮挡、内容重叠或不可达控件；图标按钮使用现有图标库并提供 tooltip/可访问名称。
- 保存后焦点回到对应行反馈区；错误提示使用统一 error code、request_id 和可操作建议。

## 8. 错误码设计

| 错误码 | HTTP | 场景 |
| --- | ---: | --- |
| `model_quality_score.query_invalid` | 422 | 非法/重复/未知查询参数 |
| `model_quality_score.input_invalid` | 422 | PATCH wire、字段或评分非法 |
| `model_quality_score.version_invalid` | 422 | expected version 类型/范围非法 |
| `model_quality_score.not_found` | 404 | 当前租户不存在指定模型/评分 |
| `model_quality_score.version_conflict` | 409 | 锁内人工评分版本不匹配 |
| `model_quality_score.request_conflict` | 409 | 同 request_id 不同意图或旧结果失效 |
| `model_quality_score.unavailable` | 503 | 查询依赖暂不可用 |
| `model_quality_score.update_failed` | 503 | 评分、审计或事件事务失败 |
| `model_alias.quality_cost_settings_invalid` | 422 | 权重、门槛或样本配置非法 |
| `gateway.routing_snapshot_invalid` | 503 | 质量/价格/Policy 持久事实损坏 |
| `gateway.routing_snapshot_unavailable` | 503 | 同一快照无法读取 |
| `gateway.routing_decision_unavailable` | 503 | 动态解释事实无法提交 |
| `gateway.no_route_available` | 503 | 所有候选均不可用或已知低质 |

所有错误继续使用统一 `code/message/request_id/trace_id/suggestion/diagnosis` 响应，不能返回 SQL、文件路径、异常正文或敏感字段。

## 9. 稳定原因码

新增原因码至少包括：

- `routing.quality_cost_highest_score`
- `routing.quality_cost_score_tie_stable_priority`
- `routing.quality_cost_cold_start_priority`
- `routing.quality_cost_evidence_priority`
- `routing.quality_cost_fixed_fallback.price_missing`
- `routing.quality_cost_fixed_fallback.currency_incomparable`
- `routing.quality_cost_fixed_fallback.latency_missing`
- `routing.quality_cost_single_eligible_candidate`
- `routing.candidate.quality_below_floor`
- `routing.candidate.quality_cold_start_deferred`

原因码是 shared、Gateway、数据库、Dry-Run、RequestLog 和 Console 的共同机器合同，禁止动态拼接模型名、URL 或错误正文。

## 10. 安全与权限约束

1. 所有 Score、Policy、Event、Decision、Candidate、Audit 查询和外键均带 tenant_id；API 不接受客户端 tenant_id。
2. GET 后端仅 admin/developer；PATCH 后端仅 admin。前端隐藏按钮只是体验优化，不能替代依赖和服务层校验。
3. PATCH 必须先完成认证/授权再读取正文，避免未授权调用者用大正文消耗资源或观察解析差异。
4. Provider Key、Virtual Key、Token、Prompt、Response、Endpoint、Header、Cookie 和原始上游异常不得进入表、事件、审计、日志或 DOM。
5. 自动统计只消费固定状态和整数延迟，不保存错误正文、输入输出或内容摘要。
6. shared 算法无 DB、网络、日志或凭据副作用；Gateway 与 Dry-Run 必须复用同一实现。
7. 动态路由解释事实写失败时停在 Provider 前；fixed 继续保持 PRD-047 已验收的 best-effort 兼容语义。
8. 价格和质量损坏不能伪装成正常缺失；未知模式、枚举、版本或半组字段必须失败关闭。
9. Webhook 使用精确 payload、稳定 `mqe_` event_id 和同租户 actor/object/trace 信封绑定。
10. direct model 和 Policy `route_to` 不读取 Score、不写质量候选、不发布质量选择事件。

## 11. 数据库迁移与回滚

### 11.1 正向迁移 `0037_quality_cost_routing.sql`

1. `PRAGMA foreign_keys=OFF` + `BEGIN IMMEDIATE`，在结构替换前运行 owner/历史一致性 preflight。
2. 历史 attempt 回填固定为：`succeeded AND usage_status!='not_applicable'` 计成功；`failed AND usage_status='unavailable'` 还必须命中 `timeout/rate_limited/upstream_5xx` 分类或 `gateway.upstream_transport_failed` 专用码才计错误。质量分类与非 `unavailable` 同时出现属于矛盾历史，preflight 原子中止。
3. 历史 stream 回填固定为：`completed` 计成功，`timed_out` 计错误；`upstream_failed` 只有 `gateway.upstream_rate_limited/gateway.upstream_timeout/gateway.upstream_stream_timeout/gateway.upstream_stream_failed` 四个固定码计错误。旧歧义码、账号/Key/请求、本地/空值/未知码全部保守排除。
4. 对所有将纳入的 attempt，`latency_ms/completed_at` 必须非空且延迟非负；attempt 时间可接收 SQLite 能规范化的合法 offset，但结果必须是年份 1..9999 的 20 位 UTC `Z`。stream 时间只接受年份 1..9999 的 20/27 位规范 UTC `Z`，延迟按整数 epoch 秒和六位微秒差向下截断到毫秒。任一坏终态、owner 缺失、负时间差、计数或 `SUM(latency)` 超过 SQLite signed 64-bit 上限都由 preflight 原子中止，不静默跳过、浮点舍入或饱和。
5. 创建、回填 `model_quality_scores`：使用临时聚合表按 tenant + model 合并两类来源，`first/last_observed_at` 取可计数终态时间的最小/最大值，`updated_at=MAX(model.created_at,last_observed_at)`；回填行数必须等于 Model 数，计数/延迟与源查询 manifest 必须一致。
6. 只创建 Model 自动 Score trigger，以及 Score owner/identity/delete/字段级转换 trigger；明确不创建 attempt/session 自动统计 trigger。运行时统计只由显式 observation helper 完成。
7. 创建 `model_quality_score_events` 及精确 payload、owner、幂等、immutable 约束。
8. 原子重建 `model_alias_routing_policies`，旧行只回填默认休眠配置，mode/version/actor/time 完全不变。
9. 原子重建 `routing_decisions`、`routing_candidates`、`routing_policy_events`、`audit_logs`，逐列复制旧历史并用计数 guard 验证零丢失；事件 v1 原文不得重写。
10. 重建全部复合 FK、owner/graph/version/字段转换/immutable trigger 与索引。
11. 最终 `foreign_key_check` 和 `integrity_check` 必须为零/ok 后提交。

不能修改已经发布的 0036 文件。

### 11.2 回滚 `0037_quality_cost_routing.down.sql`

- 本迁移是“条件可逆”：自动统计只有在能够从 attempt/session 完整重建且与每个 `(tenant_id, model_id)` Score 全字段 manifest 一致时才是可删除派生数据；默认休眠 Policy 配置也只有从未改变时没有执行语义。
- down 在任何正式表变更前先执行自动统计可重建 guard：复用正向迁移的固定分类、时间与 64-bit preflight，逐模型比对 status、全部计数、延迟、statistics version、观察时间、created/updated 时间以及行集合。缺源、坏源、缺行、多行或无源增量统一以 `prd048_down_automatic_statistics_unrebuildable` 原子中止。
- 自动统计 guard 通过后执行业务事实 guard：当前 Policy 为 `quality_cost`；fixed/旧模式下任一非默认休眠配置；任一 quality_cost Decision/Candidate；任一 v2 Event 的 quality_cost mode/change/field；v2 created 历史中的非默认质量配置；任一人工评分，或评分虽已清空但 feedback version/actor/time 仍证明曾变更；任一 `model_quality_score_events`、质量 Audit，及任一 `model.feedback_recorded` endpoint/delivery/attempt。
- 任一业务谓词非零时，在任何 DROP、DELETE、UPDATE 或正式表重建前用固定 SQLite 错误 `prd048_down_business_facts_present` 中止。失败后 schema/data manifest 与迁移前完全一致；WAL checkpoint 后主库 SHA 也必须一致。
- 即使管理员已把当前 mode 切回旧模式，只要历史质量 Decision/Event/反馈仍存在就继续阻断；不得改写成 fixed/cost_first、删除订阅或裁剪审计。
- 默认 Score 行、纯自动统计和从未执行/变更过的默认休眠配置不能命中 guard，否则空业务往返无法成立。
- guard 为零时，down 原子恢复 0036 表形、索引和 trigger；0036→0037→0036→0037 必须通过。
- 运行库正式升级前必须备份；迁移验收使用副本，不直接拿开发库做破坏性往返。

## 12. 测试用例

### 12.1 Shared 单元测试

- 默认/自定义权重、权重和、质量权重为零、门槛边界、版本/枚举损坏。
- 人工先验、有/无样本、门槛等值、低于门槛、全部低质。
- 全 cold、单 cold、ready 优先、低质 + cold、单候选。
- 零价、同价、价格差、缺价、混币、极端 Decimal、成本权重为零的回调次数。
- 零延迟、同延迟、缺延迟、延迟权重为零。
- 综合分、同分稳定破局、输入顺序无关、无 float。
- 旧三模式 golden regression：结果、原因码、事件 payload 和依赖回调次数保持 PRD-047 合同；fixed 不读 KeyPool/Score/价格/Token，availability 只读健康/KeyPool，cost_first 仍只在两条合格候选时读价格/Token。

### 12.2 Control API 集成测试

- GET 全量/过滤/分页、派生指标、no-store、统一 request_id/trace_id。
- admin/developer 可读、viewer/匿名拒绝、跨租户防枚举、拒绝 tenant_id/未知/重复参数。
- PATCH 设置、清除、同值 no-op、版本非法/冲突、双连接 CAS。
- 权限先于 body；非 JSON、重复键、超限、深层、未知字段、bool 冒充 int、PRD-049 字段拒绝。
- 相同 request_id 同意图恢复、不同意图冲突、当前版本继续演进后的旧重试冲突。
- 审计或事件故障完整回滚；Webhook 精确 payload、签名、actor/object/trace、敏感字段拒绝。
- RoutingPolicy `quality_cost` 创建/更新/切回、完整配置、权重和、单版本推进和旧请求兼容。
- RoutingPolicy event v1 历史读取、v2 精确字段/顺序/schema version、Alias version-only bump 和 runtime payload 不增字段。
- Dry-Run 与 Gateway 同事实 parity，且无 Provider/Usage/统计副作用。
- Dry-Run `policy.dry_run_unavailable` 映射、既有 PRD-046 explanation/audit/event 不变，以及不新增 route/feedback Webhook。
- RequestLog 候选强类型字段、成组 NULL、旧模式空字段、只读冻结事实和跨租户隔离。

### 12.3 Gateway 集成测试

- 非流式主失败/备用成功分别统计；显式 observation flag、固定质量白名单、`not_applicable` 本地失败排除；ConnectError 与 InvalidURL 的公开错误保持兼容，但持久质量事实不同。重复/并发终态不重复统计，rowcount 在 commit 前检查。
- 预算分项与质量资格独立断言：可证明为本地 InvalidURL/协议错误的主或备用分项 outstanding 为 0，可能已经到达上游但没有 Usage 的 ConnectError/HTTP 错误保留冻结估算。
- 流式 completed/timed_out/固定上游码统计，client_disconnected、鉴权/配额/请求错误、active 审计闸门、本地处理和本地会话故障排除；旧歧义码不能由新请求写入。
- 高质量较贵模型击败低质量便宜模型；两条低质零 Provider 调用。
- 全 cold 不读取价格/Token；单 ready 不让 cold 因便宜抢占。
- 缺价/混币/缺延迟稳定回退；低质门槛仍先执行。
- 质量 fallback 不合格时不进入 PRD-036 运行时 Fallback。
- Decision/Candidate 原子事实、事件、失败关闭、request_id 唯一与敏感日志扫描。
- direct model、route_to 和旧三模式不受影响。

### 12.4 Console 单元与浏览器测试

- API 精确 path/body/Auth/X-Request-ID，无 tenant_id/PRD-049 字段。
- PAGE-068 权重、门槛、CAS、dirty/refresh、409 rebase、迟到 200/401/409。
- PAGE-069 admin 编辑、developer 只读、viewer 无 DOM/网络预取、直达 hash 回退。
- 身份变化、离页、登出、pagehide、Abort、结果未知重试和定时刷新。
- XSS/长模型名/错误 request_id 转义；1280/900/390 无滚动、遮挡或重叠。
- RequestLog 展示公式版本、证据、三分量和最终分。

### 12.5 迁移、回归与真实运行验收

- 新库 0037、0036→0037、固定白名单历史精确回填、坏时间/owner/矛盾资格/溢出 preflight、空业务事实往返、自动统计不可重建固定错误、业务事实固定错误、失败前后 schema/data/hash 不变、FK/integrity、历史行/字段 manifest 保真。
- 中文注释检查、Python compile、Node check、Console lint、专项测试和全量 Python/Node/Console 回归。
- 真实三服务使用两个本地上游构造“高质量较贵”和“低质量便宜”，证明选择前者，并在日志详情验证冻结分量。
- 真实 admin PATCH、developer GET、viewer 403/无预取；`model.feedback_recorded` Webhook HMAC 验签。
- 1280、900、390 的 PAGE-068/PAGE-069 和调用日志截图、PNG 非空像素、无横向溢出。
- 三服务健康 200；JSON 日志逐行解析；Key/Token/Prompt/Response/Endpoint/密码/Bearer/JWT/Traceback/ERROR 敏感扫描。
- 精确清理本次创建资源；数据库无 WAL/SHM，端口 8000/8001/5173 最终无监听。

## 13. 代码修改计划

1. 先实现 shared 强类型纯计算及单元测试，冻结公式和原因码。
2. 编写 0037 正反迁移及迁移测试，先证明表约束、历史统计回填和历史保真。
3. 实现 Control `model_quality_scores` API、错误/权限/审计/Webhook 和集成测试。
4. 扩展 RoutingPolicy 配置 PATCH、DTO、配置事件和旧客户端兼容测试。
5. 接入 Gateway 同事务质量快照、Decision/Candidate 和运行时 Fallback 门禁。
6. 接入 Policy Dry-Run parity、RequestLog DTO/查询。
7. 实现 PAGE-068/PAGE-069/RequestLog 前端和异步状态测试。
8. 更新 API、README、目录说明和测试记录。
9. 运行专项、全量、迁移副本、真实三服务和三档浏览器验收；只有全部通过才把 PRD-048 标记完成。

## 14. 两轮设计自审记录

### 第一轮：边界、安全、权限与租户审查

状态：2026-07-18 已完成并修订，通过。

发现与修订：

1. 原设计按终态 trigger 自动累计，无法区分真实上游失败与本地流审计失败。已改为调用方显式 `quality_observation_eligible`，两个终态路径复用同一 tenant-qualified helper。
2. 终态函数原实现 commit 后才检查 `rowcount`，不适合作为质量 exactly-once 边界。已要求在 commit 前确认唯一状态跳转，重复/并发清理不得二次累计。
3. 人工评分与自动统计会独立改变决策。已确认 Decision/Candidate 必须冻结 Policy version、Score ID、statistics/feedback version、formula、分量和总分，历史日志禁止用当前 Score 回算。
4. Policy 新字段若不进入数据库版本 trigger 会绕过 CAS。已补充全部执行字段真实变化严格单次 `version + 1`，存量 Alias 只回填休眠配置且不切模式。
5. PAGE-069 若使用固定 20 样本声称 ready/cold，会与 Alias 专属门槛冲突。已删除全局 evidence filter，页面只展示 observed/unobserved；路由候选才冻结 Policy-specific `ready/cold`。
6. down 若裁剪 feedback 订阅、事件或审计会丢失用户事实。已改为检测人工事实、订阅和投递后失败关闭。

第一轮复核后，租户、RBAC、敏感字段和事务边界没有未解决阻断项。

### 第二轮：兼容性、数据迁移、算法与可验收性审查

状态：2026-07-18 已完成、修订并复核通过。

初审发现与修订：

1. 迁移章节残留 attempt/session 自动统计 trigger，会与 helper 双计。已明确 0037 绝不创建该 trigger，只做历史聚合和 Score 字段转换约束。
2. 0/1/2 候选、cold/ready-low 组合、rank/status、依赖顺序和回调次数不完整。已新增完整状态矩阵，固定延迟缺失优先于价格、缺价优先于混币，并定义正权重分量语义。
3. 0036 RoutingPolicy/Event 硬合同未闭合。已定义 v1 历史 + v2 新事件、schema version、精确 payload、数组顺序、Alias version-only bump 和 runtime payload 不增字段。
4. 历史库没有内存 observation flag。已定义 attempt/stream 精确 SQL 分类、流本地错误排除、坏时间/owner/64-bit 溢出 preflight 和回填 manifest。
5. Dry-Run/RequestLog 缺少 DTO/NULL 合同。已逐字段冻结 route/candidate 输出、错误映射和无新增副作用边界。
6. down 明确为条件可逆：真实质量事实在任何结构修改前固定错误中止，默认休眠配置和可重建统计允许空业务往返。

复核结论：上一轮五个阻断项均已关闭；旧三模式、迁移回填、事件 schema、Dry-Run/RequestLog 和条件回滚边界均已形成可实现、可测试合同，允许进入编码阶段。

## 15. 实现后第三轮安全与兼容复审

状态：2026-07-19 已完成并修订，通过。本轮不改变前两轮冻结的产品范围，只核对真实实现、迁移和验收器是否完整兑现合同。

发现与修订：

1. 质量失败原先可能把认证、配额、模型/请求错误和本地协议错误混入上游质量统计。实现已收敛到 timeout、429、5xx 和明确网络 transport；本地 `InvalidURL`、不支持协议及本地处理错误不累计质量，流式路径只接受冻结的精确错误白名单。
2. “是否可能已计费”和“是否计入质量”不能共用一个判断。预算分项现独立处理：可证明在 Provider 前失败的本地错误 outstanding 为 0；HTTP、timeout、ConnectError 等可能到达上游的无 Usage 失败仍保留冻结估算。
3. 流式 close/finish 异常原先可能跳过 Session、RequestLog、Usage 或租约清理。终态收口改为逐项失败隔离并保持事务边界，任何清理异常都不能掩盖后续持久化和释放动作。
4. Provider 返回的任意 `error.type/code` 不再原样进入日志，只记录固定枚举；新增内部错误码仍通过统一错误体携带 request_id/trace_id，Prompt、Response、Token、Key 和原始上游异常不落日志。
5. 0037 down guard 扩展到自动统计完整 manifest、历史 v2 created 非默认配置、fixed 休眠配置、清除后的 feedback 元数据、质量 Decision/Event/Audit 和 Webhook 事实。自动统计不可重建与业务事实分别用固定错误原子阻断，失败前后 schema/data/hash 保持不变。
6. 浏览器终验修正了 ARIA 可访问名称与 `innerText` 的混淆、受控 403/422 Chrome 提示的窄窗口分类、既有 ModelAlias 与 PRD-048 敏感质量事实的 no-store 合同边界，以及 900px Dashboard 四列最小宽度溢出。最终仍保持未知 console/network 错误失败关闭，viewer 四个受限入口零目标 API 预取。

复核结论：租户隔离、后端 RBAC、统一错误码/request_id、统计 exactly-once、预算冻结、日志脱敏、迁移条件回滚、旧三模式和真实浏览器兼容性均无未解决阻断项。

## 16. 最终验收结论

最终成功批次为 `artifacts/prd048/20260719T200349`。Python 根级全量 `680/680`、Python 示例 `38/38`、Node 示例 `33/33`、Console `261/261`、Console lint `110 files`、Ruff F 规则、compileall 和中文注释门禁均通过。

真实运行证明高质量但更贵的模型胜出、全部低质时 Provider/Attempt 均为 0、三角色后端权限与跨租户防枚举成立、六条 Webhook HMAC 有效且 RequestLog 冻结 `quality_cost_v1` 解释事实。浏览器以 admin 9 张、developer 9 张、viewer 3 张覆盖 1280/900/390 三档视口，21 张 PNG 全部非空且无横向溢出、未知浏览器错误、外部请求或敏感信息命中。

资源清理首轮验证通过，第二轮全部资源变更计数为 0。服务停止后数据库 SHA-256 为 `39eaf358f537aa4c24fa2b061a821e47fb1238b5a6dd651174e182ee3be66ce3`，37 条迁移、最高 0037、完整性 `ok`、外键违规 0，WAL/SHM 和 8000/8001/5173 监听均为 0。完整命令、checksum、manifest、日志和截图证据见 [PRD-048 测试记录](../testing/PRD-048-tests.md)。
