# 模型实验 API

本文档描述 PRD-075 在线模型 A/B 测试与质量评估合同。实验只作用于公开
`POST /v1/chat/completions` 的真实 Virtual Key 流量，不接收或保存离线 Prompt/Response，
不自动训练、不宣布赢家，也不自动切换 Model Alias。

## 1. 通用安全合同

- 所有 Control 接口要求 Bearer Session，并由后端读取当前用户、`tenant_id` 和数据库权限矩阵。
- 列表、详情和结果要求 `experiment.read`；创建同时要求 `experiment.write`、`project.read`、
  `environment.read`、`routing.read` 和 `model.read`。前端入口或按钮可见性不构成授权。
- 创建事务会再次校验五项权限以及 Project、Environment、Alias、Model 和 Provider 的 tenant owner 图；
  跨租户、不可见和不存在的实验同形返回 404。
- 成功 JSON 携带 `request_id`、`trace_id`；错误使用全局统一 envelope 并携带 `request_id`。
  所有实验响应使用 `Cache-Control: no-store, private`。
- POST 只接受单个 UTF-8 `application/json` object，正文最多 16 KiB；拒绝重复 JSON key、未知字段、
  float/指数/NaN/Infinity、重复 Header、未知 query、GET 正文和非规范资源 ID。
- Provider Key、Token、Authorization、Cookie、Prompt、Response、Endpoint、HMAC 输入或摘要、
  私有 cursor 和请求级 Assignment 明细不得出现在 API、日志、Webhook 或页面 DOM 中。

## 2. 创建实验

```http
POST /experiments
Authorization: Bearer <session-token>
Idempotency-Key: <canonical-lowercase-uuid-v4>
Content-Type: application/json
X-Request-ID: <request-id>
```

请求 exact-set：

```json
{
  "name": "August quality comparison",
  "project_id": "prj_example",
  "environment_id": "env_example",
  "model_alias_id": "alias_example",
  "model_a_id": "model_a_example",
  "model_b_id": "model_b_example",
  "target_sample_count": 1000,
  "traffic_sample_bps": 2500,
  "model_a_allocation_bps": 5000,
  "sampling_duration_seconds": 86400,
  "feedback_collection_enabled": true,
  "feedback_window_seconds": 86400
}
```

字段约束：

| 字段 | 约束 |
| --- | --- |
| `name` | 1..120 个可见字符，不允许控制字符；不进入日志或 Webhook |
| 五个 owner ID | 1..128 位安全资源 ID；必须属于当前 tenant，A/B Model 必须不同 |
| `target_sample_count` | 2..100000，A/B 最终配额均至少为 1 |
| `traffic_sample_bps` | 1..10000；10000 表示所有符合条件的流量参与采样 |
| `model_a_allocation_bps` | 1..9999；B 比例为 `10000-A` |
| `sampling_duration_seconds` | 60..604800 |
| feedback | 开启时 window 为 60..604800；关闭时 window 必须为 0 |

同一 tenant、Project、Environment 和 Alias 最多存在一个 `active` 实验。实验创建后配置不可变；
同一 actor、幂等键和同一规范请求返回原 Experiment，异体请求返回
`experiment.idempotency_conflict`。首次创建和幂等重放均返回 201，本次 HTTP envelope 始终使用本次
`request_id/trace_id`，不会覆盖首次创建的审计 source owner。

响应顶层固定包含 `experiment`、`idempotent_replay`、`request_id` 和 `trace_id`；其中 `experiment`
包含安全配置、A/B 冻结 Model/Provider owner、目标配额、当前计数、状态和时间。响应不返回分桶 secret、
bucket、route-config fingerprint 或 Endpoint。

## 3. 查询实验

```http
GET /experiments?status=active&limit=50&cursor=<opaque>
GET /experiments/{experiment_id}
Authorization: Bearer <session-token>
X-Request-ID: <request-id>
```

列表支持可选 `status=active|collecting_feedback|completed`、`limit=1..100` 和私有 cursor，按
`created_at DESC, experiment_id DESC` 稳定排序。cursor 绑定 tenant 和筛选条件，篡改、跨 tenant 或
换筛选复用均返回 `experiment.query_invalid`。

列表与详情返回同一 `ExperimentInfo` 投影：

- `experiment_id`、name、Project、Environment 和 Alias ID；
- `model_a/model_b` 的 variant、Model/Provider ID/code、allocation bps、目标数和已分配数；
- 总目标、流量 bps、采样时长、feedback 配置和 algorithm version；
- `active|collecting_feedback|completed` 状态、`facts_version` 和状态时间。

接口不返回 Assignment/request ID、bucket、私有 cursor 正文或用于客户端重算结果的请求级事实。

## 4. 查询权威结果

```http
GET /experiments/{experiment_id}/results
Authorization: Bearer <session-token>
X-Request-ID: <request-id>
```

`active` 和 `collecting_feedback` 返回单个只读 SQLite snapshot 计算的 provisional 结果，
`is_final=false`；`completed` 只返回数据库内两条不可变冻结结果，`is_final=true`。响应固定包含：

- Experiment 状态、completion reason、`facts_version/facts_through_at/next_state_at`；
- pending execution 与 settlement 数；
- A/B 两臂的 assignment、Gateway success/failure/unresolved；
- exposed/not-exposed/unknown 和独立的 quality success/failure；
- 延迟样本数、总延迟、整数 half-up 平均微秒和 nearest-rank p95 毫秒；
- settlement 状态、gross CNY micros、成本已知/未知数和平均成本；
- feedback count、rating、accepted count 和 adoption bps。

Gateway success rate 只以 success+failure 为分母，unresolved 单列；质量只统计可信 exposed 且符合
既有终态矩阵的请求。成本只累计正式 `usage_charge` 的 gross normalized CNY，credit/reversal 不冲减；
未知成本不能显示为零。feedback 零分母时平均评分和 adoption 保持 PRD-049 兼容值 0。

统计超时、数据库 busy、owner 图异常、整数溢出或 facts 漂移时整体返回
`experiment.statistics_unavailable`，不返回部分结果。完成后迟到的全局 Usage、FinOps 或 Feedback 不会
改写 ExperimentResult。

## 5. Gateway 流量语义

- 只有认证 Virtual Key 冻结出的 tenant/Project/Environment scope、当前环境 exact canonical Alias、
  公开 Chat 请求才候选进入实验。direct concrete Model、Policy `route_to`、Embeddings、Enterprise Chat
  和 Batch 明确旁路。
- Gateway 使用独立 HMAC 域做采样和 A/B 分桶，并在同一写事务创建 RoutingDecision、Assignment、
  Execution 和 Usage Settlement。整数前缀约束保证满样本时精确命中 A/B 目标配额。
- 命中 arm 后只允许冻结的 exact Model/Provider route；运行时配置、能力、Provider 或 Adapter 漂移会
  fail-closed，绝不改派另一 arm，也不启用质量、成本、fallback 或 cache-affinity 重排。
- DLP、Intent、Session Risk、限流、预算、Output Safety、Provider KeyPool、RequestLog、Usage 和 FinOps
  均继续执行。Provider 调用前由 execution owner/generation/lease CAS 取得发送许可。
- Provider 成败、Gateway 成败和 exposure 是独立事实。非流必须等 Output Safety、RequestLog 与 Usage
  durability；流必须等 shielded body finalizer 后才冻结最终 Outcome。
- 实验流的 timeout、协议不完整和通用异常都不得在 finalizer 前抢先发送 SSE error。Session、RequestLog、
  Usage 或 Outcome 的持久化降级可以覆盖最终公开错误码；客户端只会在 shielded 收口后收到至多一个安全
  `event: error`，不能先收到上游错误、再收到相反的持久化错误。普通流保留历史即时错误时序，但 finalizer
  会识别已发送状态，不会因后续持久化失败追加第二个终态事件。
- Provider stream profile 会冻结可空 `termination_marker`。OpenAI SSE profile 的 marker 为 `[DONE]`，只有
  观察到完整终止事件后才允许按逻辑成功收口；正文后 clean EOF 但缺少 marker 固定按上游流不完整失败关闭，
  Session、RequestLog、Outcome 和客户端安全 error event 使用同一终态。
- Generic/custom profile 的 `termination_marker=NULL`，clean EOF 继续兼容为合法结束。未启用 Output Safety 且
  不属于实验的 raw passthrough 使用有界、跨网络块的 marker observer；observer 不保存正文、不重分块、不延迟
  或改写上游字节，也不把普通字符串子串误认成完整终止事件。
- 上游 2xx 已建立但 active StreamSession 审计失败时，关闭上游响应、终结 starting Session 和释放 Cache
  Affinity Selection 由独立 shielded cleanup 一次收敛；调用方取消只能在清理完成后传播，不能遗留连接或租约。
- execution absolute deadline 在响应头前返回统一 504；SSE 已开始时保持 HTTP 200、发送一个安全
  `event: error` 后结束且不发送 `[DONE]`；客户端已断开时只提交数据库终态。

## 6. 状态、恢复与 Webhook

状态只允许单向推进：

```text
active -> collecting_feedback -> completed
```

达到样本目标或采样截止后停止新 Assignment；feedback fence、execution reaper 和 settlement 硬截止全部
收口后，finalizer 在一个事务中冻结 A/B Result、写 source event/outbox 并标记 completed。租约、generation
和 facts-version CAS 防止旧 Scheduler/Gateway owner 覆盖新事实。

完成事件为 `experiment.completed`。payload 只包含 Experiment/scope/model 安全 ID、completion reason、
汇总计数、facts version 和 facts time；不包含 name、请求明细或任何敏感正文。首次投递、pending 恢复和
dead-letter replay 均从同一不可变 owner 图重建并验证。

## 7. 固定错误码

| HTTP | code | 含义 |
| ---: | --- | --- |
| 422 | `experiment.input_invalid` | body、Header、path 或编码不符合 exact 合同 |
| 422 | `experiment.query_invalid` | query/cursor 非法或不匹配当前 owner |
| 422 | `experiment.idempotency_required` | 缺少规范 Idempotency-Key |
| 422 | `experiment.model_invalid` | Model/Provider/能力不满足创建合同 |
| 404 | `experiment.not_found` | 不存在、不可见或跨 tenant 的同形结果 |
| 409 | `experiment.idempotency_conflict` | 同幂等键对应不同请求 |
| 409 | `experiment.scope_conflict` | 同 scope 已有 active 实验 |
| 503 | `experiment.unavailable` | Control 无法证明安全 owner/配置状态 |
| 503 | `experiment.statistics_unavailable` | 统计快照无法完整且可信地生成 |
| 503 | `gateway.experiment_unavailable` | Gateway 无法权威判断实验 scope/owner |
| 503 | `gateway.experiment_variant_unavailable` | 冻结 arm 运行时不可执行 |
| 504 | `gateway.experiment_execution_timeout` | 实验 execution 达到绝对截止 |

认证和权限错误继续使用全局 `auth.*` 合同。公开错误不得透传 SQLite、URL、上游响应正文、异常堆栈或
Provider 凭据。
