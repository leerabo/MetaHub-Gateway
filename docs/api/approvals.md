# 审批 API

状态：**PRD-078 已完成**（2026-08-14）。公开合同、0071、真实 Runtime 与 PAGE-108/109 Browser 均已
验证；总体状态保持 `all_108_prds_complete=false`。

本文档冻结 PRD-078 的审批中心、移动卡片、后端二次验证、超时拒绝和 Webhook 通知合同。企微、钉钉、
飞书在本版本中只是既有签名 Webhook 的通知接收端；任何外部卡片点击都必须回到 MetaHub 登录页面，外部
平台身份、前端按钮和客户端 `tenant_id` 均不能替代后端授权。

## 1. 通用安全合同

- 公开入口使用 `Authorization: Bearer <session-token>`。后端先实时复核 `approval.read`；决策和 step-up
  还需 `approval.decide`，并按目标类型继续复核预算或会话风险的读写权限。
- `budget_block_release` 读取/决定分别要求 `billing.budget.read/write`；`session_risk_action` 分别要求
  `session_risk.read/manage`。跨租户、目标域无权与不存在任务使用同形 404。
- 所有审批响应包含 `request_id`、`trace_id`，并使用 `Cache-Control: no-store, private`；公开响应同时使用
  `Pragma: no-cache`、`Vary: Authorization`。认证、校验和服务异常也必须保持同一缓存边界。
- 内部创建、step-up、approve 和 reject 一律忽略客户端 `X-Request-ID/X-Trace-ID` 并生成服务端链路 ID，
  防止密码、ticket、内部 Token 或幂等键被碰撞回显。
- 密码和 step-up token 只存在一次调用栈内；数据库仅保存带 key version 的分域 HMAC。Provider Key、Token、
  Prompt、Response、目标正文、预算金额、会话 scope 和 Webhook Secret 不进入日志、事件或公开证据。

## 2. 读取任务

### `GET /approvals`

允许 query：

| 参数 | 约束 |
| --- | --- |
| `status` | `pending/approved/rejected/all`，默认 `pending` |
| `target_type` | `budget_block_release/session_risk_action`，可省略 |
| `limit` | 1..100 |
| `cursor` | 服务端签名并绑定 tenant、筛选和快照的不透明 keyset cursor |

响应为 exact schema：

```json
{
  "approvals": [{
    "approval_id": "apr_0123456789abcdef0123456789abcdef",
    "target_type": "budget_block_release",
    "target_action": "release",
    "risk_level": "standard",
    "status": "pending",
    "resolution_reason": null,
    "state_revision": 1,
    "writeback_status": "pending",
    "required_permission_code": "billing.budget.write",
    "requires_step_up": false,
    "created_at": "2026-08-12T01:00:00Z",
    "expires_at": "2026-08-12T02:00:00Z",
    "decided_at": null
  }],
  "next_cursor": null,
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

列表只返回当前 actor 同时具备目标域读取权限的任务。响应不包含 source、HMAC、目标名称、金额或会话摘要。

### `GET /approvals/{approval_id}`

详情在同一个 `approval` 摘要外增加：

```json
{
  "approval": { "...": "与列表摘要相同" },
  "target_summary": {
    "target_type": "budget_block_release",
    "target_id": "br_example",
    "target_action": "release",
    "target_version": "2026-08-12T00:59:00Z"
  },
  "allowed_actions": ["approve", "reject"],
  "decisions": [],
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

终态任务的 `allowed_actions` 为空，`decisions` 含唯一 append-only 决策。决策 exact 字段为
`decision_id/decision/reason_code/actor_type/actor_id/revision_before/revision_after/step_up_used/`
`writeback_status/created_at`。timeout 使用 `actor_type=system`、
`actor_id=approval-timeout-scheduler`，不会冒充管理员。

## 3. 后端二次验证

`POST /approvals/{approval_id}/step-up` 仅用于 high-risk 会话任务。正文最大 4 KiB，exact JSON：

```json
{
  "action": "approve",
  "password": "<current-user-password>"
}
```

后端在不持有 SQLite 写锁时验证当前 Session 用户密码，然后重新读取 Session、revision-14 权限矩阵、任务和
目标版本，再签发至少 256 bit 的一次性票据。票据绑定 tenant、user、session、task、action、task revision
和 authorization fingerprint，TTL 120 秒；新票据会废止同 owner 的旧票据。

成功返回 HTTP 200：

```json
{
  "step_up_token": "<returned-once>",
  "expires_at": "2026-08-12T01:02:00Z",
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

每 task/actor 连续 5 次失败后锁定 15 分钟。错误不区分错误密码、任务 owner、Session、锁定以外的内部细节，
也不回显密码、ticket 或摘要。

## 4. 批准与拒绝

- `POST /approvals/{approval_id}/approve`
- `POST /approvals/{approval_id}/reject`

两个端点都要求唯一 `Idempotency-Key`。批准 reason 只能是 `approved_business_need`；拒绝 reason 只能是
`risk_unacceptable`、`budget_not_approved`、`no_longer_needed`。

standard 任务 exact body：

```json
{
  "expected_revision": 1,
  "reason_code": "approved_business_need"
}
```

high 任务必须额外发送 `step_up_token`；standard 任务发送该字段会被拒绝。成功返回 HTTP 200：

```json
{
  "approval": { "...": "终态审批摘要" },
  "decision": {
    "decision_id": "apd_0123456789abcdef0123456789abcdef",
    "decision": "approve",
    "reason_code": "approved_business_need",
    "actor_type": "user",
    "actor_id": "usr_example",
    "revision_before": 1,
    "revision_after": 2,
    "step_up_used": false,
    "writeback_status": "completed",
    "created_at": "2026-08-12T01:01:00Z"
  },
  "idempotent_replay": false,
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

同 actor、task、action、revision、reason 的精确幂等重放返回原决策并标记 `idempotent_replay=true`；不同
意图复用 key 返回 409。幂等回读仍先重新认证并复核实时 base + target 权限，但不再次消费已用 ticket。

approve、reject 和 timeout 竞争同一 `pending` + revision CAS，只能产生一个终态。预算批准在审批事务内
释放该规则的 active block；会话风险批准在同事务写入现有权威 action/event，并以 tenant-qualified
`target_fact_id + target_revision` 保存可恢复 Redis 对账事实，恢复器不得读取最新 action 猜测对应关系。
拒绝和 timeout 不修改目标，但会写已完成的 `no_effect` writeback 证据。

## 5. 内部创建

`POST /internal/tenants/{tenant_id}/approvals` 要求唯一 `X-Internal-Token`，并拒绝与任何 Bearer Header 混用。
认证在读取正文前完成；路径 tenant 必须存在且 active。正文最大 16 KiB，exact JSON：

```json
{
  "source_type": "budget",
  "source_id": "opaque-source-id",
  "target_type": "budget_block_release",
  "target_id": "br_example",
  "target_action": "release",
  "target_reason_code": null,
  "expires_in_seconds": 3600,
  "idempotency_key": "producer-owned-idempotency-key"
}
```

TTL 为 300..86400 秒。预算任务必须省略 `target_reason_code`；会话任务必须使用既有会话风险 reason 枚举。
服务端重读 tenant-qualified 目标并派生 risk、version、fingerprint 和 required permission，不接受任意 URL、
SQL、函数名、风险降级或客户端目标正文。新建返回 HTTP 201；精确内部幂等回读返回同一安全摘要。

## 6. Webhook 事件

只新增 `approval.created` 与 `approval.timeout`。二者复用既有 Endpoint 白名单、HMAC 签名、投递快照、租约、
重试、死信和恢复；网络调用始终发生在审批事务提交后。

`approval.created` exact payload：

```json
{
  "schema_version": 1,
  "approval_id": "apr_0123456789abcdef0123456789abcdef",
  "status": "pending",
  "target_type": "budget_block_release",
  "target_action": "release",
  "risk_level": "standard",
  "requires_step_up": false,
  "expires_at": "2026-08-12T02:00:00Z",
  "entry": "approval-card"
}
```

`approval.timeout` exact payload 为 `schema_version/approval_id/status/resolution_reason/target_type/`
`target_action/decided_at`，其中 status 固定 `rejected`、reason 固定 `timeout`。通知中的任务 ID 只是页面入口，
不是授权凭据；本版本不接受企微、钉钉或飞书直接批准回调。

## 7. 统一错误码

| HTTP | code | 含义 |
| ---: | --- | --- |
| 400 | `approval.input_invalid` | Header、query、ID、JSON 或枚举非法 |
| 400 | `approval.idempotency_required` | 缺少或歧义的 `Idempotency-Key` |
| 403 | `approval.step_up_required` | high-risk 动作缺少票据 |
| 403 | `approval.step_up_invalid` | 密码或票据无效、过期、已用或 owner 不符 |
| 404 | `approval.not_found` | 不存在、跨租户或目标域不可见 |
| 409 | `approval.state_conflict` | 状态或 revision 已变化 |
| 409 | `approval.idempotency_conflict` | 同 key 对应另一意图 |
| 409 | `approval.target_conflict` | 创建后目标版本或 fingerprint 漂移 |
| 409 | `approval.expired` | 人工动作到达时已由 timeout 收敛 |
| 429 | `approval.step_up_rate_limited` | 二次验证失败次数超限 |
| 429 | `approval.capacity_full` | tenant pending 上限 1000 |
| 503 | `approval.unavailable` | 数据库、调度或事务事实不可证明 |
| 503 | `approval.writeback_unavailable` | 派生目标对账等待恢复 |
| 503 | `approval.event_unavailable` | 事件 source/outbox 恢复失败 |

认证错误沿用 `auth.*`；权限不足使用 `auth.permission_denied`。所有错误使用统一 envelope 和 request/trace ID，
不拼接 SQL、路径、Endpoint、凭据、目标正文或供应商响应。

## 8. 部署配置

- `METAHUB_APPROVAL_HMAC_VERSION=<正整数>`
- `METAHUB_APPROVAL_HMAC_SECRET=<至少 32 bytes 的独立根密钥>`
- 可选 `METAHUB_APPROVAL_PREVIOUS_HMAC_VERSION/SECRET`，两者必须成对且 previous version 更小。

票据、创建幂等、动作幂等、意图、授权 fingerprint 和 cursor 使用不同 HMAC domain。新写始终用 current；
previous 只验证保留期内旧摘要。审批密钥不得等于 Auth、Provider、Webhook、内部服务、Cache、Session Risk、
Batch、File 或 Prompt 密钥。prod 缺失或复用时失败关闭；配置对象、摘要、日志和 API 均不输出明文。

审批 scheduler 负责 tenant 公平 timeout、会话风险 Redis writeback 和 source outbox 恢复。每次 claim 都使用
generation/token/owner/lease CAS；过期 lease 可接管，迟到 worker 不得覆盖新 owner 终态。最终运行证据和
准确配置范围见 `docs/testing/PRD-078-tests.md`；总体状态继续保持 `all_108_prds_complete=false`。
