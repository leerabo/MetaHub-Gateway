# 多轮会话风险 API

本文档冻结 PRD-070 的 Gateway 会话身份扩展、Provider 前累计、Control 高危会话查询与人工处置合同。
风险引擎只保存固定分类摘要、计数、版本、HMAC 和链路 ID，不保存 Prompt、Response、Session/Turn
Header、DLP 命中片段、Provider Key、Token 或完整 API scope HMAC。

## 1. 运行配置与身份

启用多轮风险累计需要：

- `METAHUB_SESSION_RISK_ENABLED=true`
- `METAHUB_SESSION_RISK_HMAC_VERSION=<正整数>`
- `METAHUB_SESSION_RISK_HMAC_SECRET=<至少 32 字节的专用密钥>`
- `METAHUB_REDIS_URL=<生产 Redis URL>`

密钥只用于 session/turn、owner、cursor 和处置幂等的分域 HMAC，不能复用 Auth、Provider、Prompt
Template 或 Webhook secret。轮换期间可配置 previous version/secret；仍被数据库引用的版本缺失时失败关闭。

### 1.1 previous HMAC 移除预检

移除 previous version/secret 前，必须先停止所有 Gateway 新写入，同时保留 current/previous 两把密钥，
再以 Gateway 的正式环境配置运行：

```powershell
C:\Users\rabol\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe `
  scripts\preflight\prd070-hmac-rotation-verifier.py
```

`PYTHONPATH` 必须包含 `shared-sdk/python` 与 `gateway-data-plane/src`。脚本只读扫描 Redis 的 score、
pending 和 turn claim，并在同一维护窗口核对 SQLite 的 active/frozen score、未过期人工动作摘要及未知
版本。它不会删除 key、清理 pending 或改写数据库：

- 退出码 `0`：所有计数为零，可以进入人工删 previous 配置步骤；
- 退出码 `3`：仍有目标版本、未知版本或 legacy claim 引用，必须先清偿或等待 TTL 到期；
- 退出码 `2`：配置、Redis、数据库或协议不可用，无法证明安全，禁止删 key。

stdout 只输出 canonical JSON 聚合计数；stderr 只输出固定错误码。两者都不包含 Redis/数据库 URL、
Secret、tenant、Virtual Key、Session/Turn、Redis key 或 HMAC digest。未停止新写入时即使计数为零也
不能作为移除依据。

企业 Chat 的 `chat_session_id/chat_message_id` 只从一次性内部 execution grant 恢复。公开
`POST /v1/chat/completions` 可选同时发送：

```http
X-MetaHub-Session-ID: mhs_<32 lowercase hex>
X-MetaHub-Turn-ID: mht_<32 lowercase hex>
```

两个 Header 必须成对、唯一且精确匹配格式。它们不转发给 Provider，也不写入数据库、Redis、日志、
Webhook、响应或 Console。没有这两个 Header 的公开 Chat 继续使用 PRD-069 单请求安全路径，不按整把
Virtual Key 聚合风险；Embeddings 不创建 score，但仍检查管理员对关联 Key 创建的冻结 overlay。

## 2. Gateway Provider 前合同

固定顺序为：

```text
Auth -> Request claim -> environment/policy lease -> freeze preflight
     -> body/Policy mask/route -> DLP -> one Intent path
     -> SessionRisk accumulate + durable evidence -> Rate -> Budget -> Provider/SSE
```

- freeze preflight 在公开 Chat 读取正文前执行。Session 或关联 Virtual Key 已冻结时返回 403，
  Provider、Rate、Budget、RoutingAttempt 和 Usage 均为 0。
- 有可信 session 时只执行一次同步 `sync_session` 分类；DLP block 后不再调用分类器，但仍提交当前 turn
  的 DLP assessment。没有可信 session 时保留 PRD-069 的 strong sync + light async 行为。
- 每个可信 turn 只累计一次。相同 turn 和相同意图是幂等重放；相同 turn 的不同意图返回固定 409。
- 单 turn 分数上限 4000；连续风险窗口为最后风险 turn 起 30 分钟。达到 high-risk 阈值生成事件，至少
  三个风险 turn 且达到冻结阈值才自动冻结 session。
- Redis 原子累计后必须先把 observation/score/action/event 提交 SQLite，再 ACK pending。数据库失败、
  ACK 不确定或待提交债务无法恢复时均在 Provider 前返回 503。
- `public_service` 的 Redis 故障失败关闭；`internal_only` 只有在数据库成功证明不存在 active overlay 时
  才可降级继续，并记录 `session_risk_status=degraded`，不得用进程内 Store 伪造累计。

本轮刚跨冻结阈值且没有更早安全阻断时返回 `gateway.session_risk_frozen`。若 DLP 或 Intent 已先阻断，
仍必须提交冻结证据，但保留更早的公开错误码。

## 3. GET /security/session-risks

要求后端实时权限 `session_risk.read`。tenant 只取认证 Session，所有 SQL 从 `tenant_id` 条件开始。
支持以下白名单 query：

| 参数 | 约束 |
| --- | --- |
| `status` | 逗号分隔且仅可为 `high_risk/frozen/resolved/expired` |
| `scope_type` | `enterprise_chat/api_session` |
| `virtual_key_id` | canonical 小写 UUID，且必须属于当前租户 |
| `from_time/to_time` | UTC ISO 时间，左闭右开；二者同时提供时必须满足 `from_time < to_time` |
| `limit` | 1..100，默认 20 |
| `cursor` | 服务端签名的不透明 keyset cursor，只能与原筛选和租户一起使用 |

默认只返回 `high_risk,frozen`。示例响应：

```json
{
  "runtime_status": "active",
  "summary": {
    "high_risk_count": 1,
    "frozen_count": 0,
    "resolved_count": 2
  },
  "session_risks": [{
    "session_risk_score_id": "srs_11111111111111111111111111111111",
    "scope_type": "api_session",
    "scope_reference": "api_0123456789abcdef",
    "virtual_key_id": "55555555-5555-4555-8555-555555555555",
    "status": "high_risk",
    "score": 7600,
    "score_scale": 10000,
    "risky_turn_count": 2,
    "signal_counts": {"jailbreak": 2, "sensitive": 1, "tool_inducement": 0},
    "state_revision": 3,
    "threshold_version": "session-risk-threshold-v1",
    "last_source_request_id": "req_...",
    "window_expires_at": "2026-08-04T05:00:00Z",
    "first_high_risk_at": "2026-08-04T04:20:00Z",
    "frozen_at": null,
    "updated_at": "2026-08-04T04:22:00Z",
    "freezes": [],
    "actions": []
  }],
  "next_cursor": null,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

企业 Chat 的 `scope_reference` 是服务端 `chs_` 资源 ID；API session 只返回独立 `api_` 命名空间下
16 位小写十六进制短引用，不返回内部 `srh_` HMAC。`high_risk` 必须有 `window_expires_at`；
`frozen/resolved/expired` 必须为 `null`，冻结不会因 Redis TTL 自动解除。

## 4. POST /security/session-risks/{score_id}/action

要求后端实时同时具有 `session_risk.read` 和 `session_risk.manage`。请求必须是唯一
`application/json`，并带 8..128 字节可见 ASCII `Idempotency-Key`：

```http
POST /security/session-risks/srs_11111111111111111111111111111111/action
Authorization: Bearer <session-token>
Content-Type: application/json
Idempotency-Key: sr_action_<opaque-random>

{
  "action": "freeze_session",
  "reason_code": "confirmed_attack",
  "expected_revision": 3
}
```

固定动作矩阵：

| action | reason_code | 前置状态/效果 |
| --- | --- | --- |
| `freeze_session` | `confirmed_attack` | `high_risk -> frozen`，创建 Session overlay |
| `freeze_virtual_key` | `confirmed_attack` | `high_risk/frozen -> frozen`，创建 Key overlay |
| `dismiss` | `false_positive/verified_safe` | 无 active overlay 的 `high_risk -> resolved` |
| `release_session` | `false_positive/incident_contained` | 释放本 score 的 Session overlay |
| `release_virtual_key` | `false_positive/incident_contained` | 释放本 score 的 Key overlay |

客户端不能提交 tenant、actor、target ID、Session ID、Virtual Key target 或自由原因。后端在
`BEGIN IMMEDIATE` 内重做 read/manage 权限和 CAS revision 校验，并原子写 score、freeze、append-only
action、source event 与 Audit。提交后必须把权威终态同步到 Redis；同步不确定时返回 503，调用方使用原
幂等键重试。幂等回放返回原 action 和当前最新 score，因此两者 revision 可以不同，`idempotent_replay=true`。

## 5. 权限、事件与审计

- 默认 Admin、SecurityAdmin 拥有 `session_risk.read/manage`；其他内置角色没有。自定义角色不因升级自动扩权。
- `session_risk.read` 控制 PAGE-098 导航、GET 和详情；`session_risk.manage` 不能单独读取。前端隐藏按钮只改善
  体验，GET/POST 路由和事务内权限复核才是授权边界。
- `security.session_high_risk` Webhook 只包含固定 score/action/event、scope type、状态、整数计数、版本和
  source request/trace；不包含 tenant 外部输入、完整 scope、Prompt、Response 或密钥。
- 人工动作写 `session_risk.*` Audit，摘要只含 scope type、状态、分数、revision、overlay ID 和 target type。

## 6. 统一错误

| code | HTTP | 含义 |
| --- | ---: | --- |
| `gateway.session_risk_identifier_invalid` | 400 | 公开 Session/Turn Header 缺失、重复或格式非法 |
| `gateway.session_risk_turn_too_large` | 413 | current-turn HMAC 输入超过 256 KiB |
| `gateway.session_risk_turn_conflict` | 409 | 同一 turn 使用了不同意图摘要 |
| `gateway.session_risk_frozen` | 403 | Session/Key 已冻结或本轮刚触发冻结 |
| `gateway.session_risk_unavailable` | 503 | Redis、配置或冻结预检不可用 |
| `gateway.session_risk_evidence_unavailable` | 503 | pending/数据库/ACK 证据无法确认 |
| `gateway.session_risk_hmac_version_unavailable` | 503 | 数据引用的 HMAC 版本未配置 |
| `session_risk.query_invalid` | 400 | query、cursor、时间或 UUID 非法 |
| `session_risk.payload_invalid` | 400 | 动作 JSON、Content-Type 或字段集合非法 |
| `session_risk.idempotency_invalid/conflict` | 400/409 | 幂等键非法或已绑定其他意图 |
| `session_risk.not_found` | 404 | 当前租户不可见该 score；跨租户与不存在同形 |
| `session_risk.state_conflict` | 409 | revision、状态或 overlay 已变化 |
| `session_risk.target_already_frozen` | 409 | 同一来源和 target 已有 active overlay |
| `session_risk.store_unavailable` | 503 | Control 无法证明或同步 Redis 状态 |
| `session_risk.evidence_unavailable` | 503 | 数据库证据损坏或事务不可提交 |

所有响应继续使用统一错误 envelope、`request_id/trace_id` 和 `Cache-Control: no-store`。日志只允许固定
error code、tenant/resource ID、request ID、trace ID 和异常类型，不记录 Header、幂等键、Redis URL、
SQL 文本、Prompt、Response、Token 或 Provider Key。
