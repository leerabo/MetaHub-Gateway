# 批处理 Job API

本文档描述 PRD-074 的异步批处理 Scheduler 与 Worker 公开合同。Batch 只消费 PRD-072 已认证且
经 PRD-073 准入的文件；它复用 Gateway 的 Policy、DLP、Intent、限流、预算、路由、输出安全、
RequestLog、Usage 与 FinOps 链路。OpenRouter 和硅基流动仍只是竞品与迁移兼容对象，不是默认上游。

## 1. 通用安全合同

- 所有公开接口要求 Bearer Session。`tenant_id`、actor、Virtual Key、路由、价格和预算只能来自后端
  可信状态，客户端不能提交或覆盖。
- 创建要求后端实时校验 `batch.write + file.read`；读取 Job 要求 `batch.read`；Task 和结果读取要求
  `batch.read + file.read`；取消要求 `batch.read + batch.write`；人工成本对账要求
  `batch.read + billing.finops.write`。前端入口或按钮可见性不构成授权。
- 创建、结果读取、取消与人工对账在写事务中再次读取当前用户、租户和权限矩阵；撤权后的已有
  Session 不能继续执行受保护操作。
- 跨租户 Job/Task 与不存在资源同形返回 404。列表 cursor 绑定 tenant、资源类型和筛选条件，不能
  跨租户或跨筛选复用。
- 成功与错误响应都携带统一 `request_id`；公开 JSON 同时携带 `trace_id`。所有响应使用
  `Cache-Control: no-store, private`。
- 请求拒绝重复 JSON key、重复关键 Header、未知 query、未知字段、非 UTF-8、非规范 ID、超限正文
  和 operation 字段混用。
- Provider Key、Session/Internal Token、Prompt、文件正文、assistant Response、对象键、密文、grant、
  lease、内部路由和 Endpoint URL 不进入日志、错误、Webhook 或 Job/Task 列表 DTO。
- 每次真实 Provider attempt 使用独立 `request_id/trace_id`，并可关联 RequestLog、Usage、FinOps、
  Batch Task 与预算 allocation；失败请求和 outcome unknown 也必须保留可追踪的安全事实。

## 2. 创建 Job

```http
POST /batch-jobs
Authorization: Bearer <session-token>
Idempotency-Key: <canonical-uuid>
Content-Type: application/json
X-Request-ID: <request-id>
```

请求正文最多 256 KiB，固定支持 `summarize`、`translate` 和 `classify`：

```json
{
  "name": "夜间文件摘要",
  "project_id": "prj_example",
  "environment_id": "env_example",
  "model_alias_id": "alias_example",
  "operation": "summarize",
  "file_ids": ["file_00000000000000000000000000000000"],
  "priority": "normal",
  "max_duration_seconds": 300,
  "max_attempts": 2,
  "max_output_tokens": 1024,
  "output_language": "zh-CN"
}
```

Operation 字段是 exact-set：

| operation | 必需字段 | 可选字段 | 禁止字段 |
| --- | --- | --- | --- |
| `summarize` | 无 | `output_language` | `target_language`、`classification_labels` |
| `translate` | `target_language` | 无 | `output_language`、`classification_labels` |
| `classify` | `classification_labels` | 无 | `output_language`、`target_language` |

`file_ids` 为 1..100 个不重复的规范文件 ID；`priority` 为 `low/normal/high`；duration 为
30..3600 秒，attempt 为 1..3，output token 上限为 16..4096。服务在一个事务内冻结绑定、路由和价格，
校验每个文件的 tenant owner、ready extract、Token estimate 和最新权威 PRD-073 报告，并硬性执行
tenant/project/Virtual Key 预算与 tenant Job/Task 容量。任一文件或预算失败时不创建部分 Job。

同一个规范幂等键和相同请求返回原 Job；同键不同请求返回 `409 batch.idempotency_conflict`。成功首次
创建返回 201：

```json
{
  "job": {
    "batch_job_id": "bjob_00000000000000000000000000000000",
    "name": "夜间文件摘要",
    "project_id": "prj_example",
    "environment_id": "env_example",
    "model_alias_id": "alias_example",
    "operation": "summarize",
    "priority": "normal",
    "max_duration_seconds": 300,
    "max_attempts": 2,
    "max_output_tokens": 1024,
    "status": "queued",
    "progress": {
      "total": 1,
      "queued": 1,
      "running": 0,
      "succeeded": 0,
      "failed": 0,
      "cancelled": 0,
      "expired": 0,
      "outcome_unknown": 0
    },
    "cost": {
      "estimated_cny": "0.001000",
      "posted_cny": "0.000000",
      "possible_cny": "0.000000",
      "reconciliation_pending": false
    },
    "deadline_at": "2026-08-08T08:05:00Z",
    "cancel_requested_at": null,
    "started_at": null,
    "completed_at": null,
    "failure_code": null,
    "created_at": "2026-08-08T08:00:00Z",
    "updated_at": "2026-08-08T08:00:00Z"
  },
  "request_id": "req_example",
  "trace_id": "trace_example"
}
```

金额都是固定精度 CNY 文本，客户端不得用二进制浮点重算。

## 3. 查询 Job

```http
GET /batch-jobs?status=running&limit=50&cursor=<opaque>
GET /batch-jobs/{batch_job_id}
Authorization: Bearer <session-token>
X-Request-ID: <request-id>
```

列表按 `created_at,batch_job_id` 倒序，`limit` 为 1..100，`status` 可省略。响应为
`jobs + next_cursor + request_id + trace_id`。详情返回与创建相同的 `job` 投影。

Job 状态为 `queued/running/completed/partial_failed/failed/cancelled/expired`。`cost.posted_cny` 是已入账
成本；`possible_cny` 只表示无法证明未收费的金额，不得冒充最终成本；存在 possible allocation 时
`reconciliation_pending=true`。

## 4. 查询 Task 与结果

```http
GET /batch-jobs/{batch_job_id}/tasks?status=succeeded&limit=50&cursor=<opaque>
GET /batch-jobs/{batch_job_id}/tasks/{batch_task_id}/result
Authorization: Bearer <session-token>
X-Request-ID: <request-id>
```

Task 列表按 `task_index,batch_task_id` 正序，状态为 `queued/leased/calling/retry_wait/succeeded/failed/
cancelled/expired/outcome_unknown`。投影只包含文件 ID、attempt 计数、固定失败码、结果可用性、Token 与
成本事实；不返回输入、输出、路由或凭据。

结果仅对 `succeeded + result_available` Task 返回：

```json
{
  "batch_job_id": "bjob_00000000000000000000000000000000",
  "batch_task_id": "btsk_00000000000000000000000000000000",
  "assistant": "用户显式请求后才解密返回的结果",
  "request_id": "req_attempt",
  "trace_id": "trace_attempt"
}
```

服务在短事务内再次验证 `batch.read + file.read`、tenant owner、result HMAC、AES-GCM owner/AAD 和
大小限制。结果不得预取或缓存；Console 切换 Job/Task、刷新、离页、注销、权限变化或非 2xx 时先清除
旧正文再终止请求。未完成返回 `batch.result_not_ready`；认证结果缺失或损坏返回
`batch.result_unavailable`；outcome unknown 返回 `batch.provider_outcome_unknown`，且绝不重调 Provider。

## 5. 取消 Job

```http
POST /batch-jobs/{batch_job_id}/cancel
Authorization: Bearer <session-token>
X-Request-ID: <request-id>
Content-Length: 0
```

请求禁止 query 和非空正文。取消为幂等状态命令：queued/retry_wait Task 在同一事务终结；leased/calling
Task 记录取消意图，Gateway grant 消费和 Worker settlement 都再次检查。已终态 Job 返回当前事实，
不回滚已发生的 Provider 调用或成本。

## 6. 人工成本对账

```http
POST /batch-jobs/{batch_job_id}/tasks/{batch_task_id}/reconciliation
Authorization: Bearer <session-token>
Idempotency-Key: <canonical-uuid>
Content-Type: application/json
X-Request-ID: <request-id>
```

确认未收费：

```json
{"decision":"confirm_not_charged","adjustment_id":null}
```

确认已收费必须引用同 tenant、同 attempt request 和等额 FinOps adjustment：

```json
{"decision":"confirm_charged","adjustment_id":"fadj_00000000000000000000000000000000"}
```

对账是幂等且不可变的管理事实。服务不会凭 HTTP 状态猜测收费，也不会把 possible 金额自动写成 posted。

## 7. 调度、重试与恢复语义

- Scheduler 在数据库中按 tenant 轮转、priority 和 FIFO claim，同时执行全局与 tenant 并发门禁；租约、
  generation 和 owner 都由 CAS 保护。
- Worker 在 Provider 调用前消费一次性内部 grant，并再次校验 active tenant/user、当前完整 RBAC、取消、
  deadline、lease generation、冻结绑定和预算 allocation。grant 过期但未消费只换 generation，不增加
  Provider attempt。
- Gateway 实时请求永不等待 Batch；实时活动期间 Batch 返回 `batch.gateway_busy`，Worker 有界退避并
  刷新同一 prepared attempt。Batch 限流保留至少 25% 实时容量，拒绝不计数。
- 只有 Gateway 持久事实共同证明 `not_invoked` 或 `known_failed + retry_safe` 才能新建 attempt。调用结果
  不可证明时固定为 `outcome_unknown`，禁止重试。
- Usage 已安全落入 fsync spool 时，Worker/reaper 等待 replay 完成预算结算后再终结；认证结果缺失时以
  `batch.result_unavailable` 结束，不重复调用 Provider。
- Worker、Gateway 或 Control 停服后，过期租约、Usage spool 和 Batch Webhook source/outbox 均从数据库
  恢复；恢复路径与正常路径使用同一租户、幂等和 CAS 约束。

## 8. Webhook

PRD-074 增加：

- `batch_job.created`：精确包含 schema version、Job ID、operation、priority、task count、estimated CNY、
  actor、request/trace 元数据。
- `batch_job.completed`：精确包含 schema version、Job ID、终态、成功/失败/取消计数、posted/possible CNY、
  reconciliation 状态、actor、request/trace 元数据。

事件不包含 Job name、文件/Task ID、operation 参数、Prompt、结果、Provider、模型内部 ID、Key、grant、
Secret、URL 或自由错误文本。source 与 outbox 在业务事务中原子创建，Endpoint 集合包括空集合也会冻结；
稳定 event ID 提供 at-least-once，失败采用有限退避和死信语义。

## 9. 错误码

| code | HTTP | 含义 |
| --- | ---: | --- |
| `batch.request_invalid` | 422 | 严格请求、path、query、Header 或正文无效 |
| `batch.idempotency_required` | 400 | 缺少唯一规范幂等键 |
| `batch.idempotency_conflict` | 409 | 同一键绑定不同创建请求 |
| `batch.not_found` | 404 | Job 跨租户或不存在 |
| `batch.task_not_found` | 404 | Task/Result 跨 owner 或不存在 |
| `batch.binding_unavailable` | 409 | Project、Environment、Alias 或唯一 Key 不可用 |
| `batch.file_not_ready` | 409 | 文件或 Token estimate 未 ready |
| `batch.file_security_blocked` | 403 | PRD-073 模型准入失败 |
| `batch.cost_estimate_unavailable` | 503 | 价格或成本事实不可证明 |
| `batch.budget_exceeded` | 402 | 任一适用预算不足 |
| `batch.capacity_full` | 429 | tenant pending Job/Task 达上限 |
| `batch.provider_outcome_unknown` | 502 | Provider 结果不可证明，禁止重试 |
| `batch.result_not_ready` | 409 | Task 尚无可读结果 |
| `batch.result_unavailable` | 500 | 认证结果缺失或损坏 |
| `batch.reconciliation_conflict` | 409 | 对账幂等键、金额或 adjustment 不一致 |
| `batch.worker_unavailable` | 503 | Batch 依赖、配置或安全持久化不可用 |

认证失败、权限不足和服务级错误继续使用统一平台错误信封；所有错误都有 `request_id`，且不得复制底层
异常正文或敏感请求字段。
