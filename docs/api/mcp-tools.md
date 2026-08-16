# MCP 工具治理 API

本文档冻结 `PRD-082 - MCP 工具注册与权限` 与 `PRD-083 - 第三方 MCP 沙箱网络 ACL` 的公开 HTTP
合同。该能力提供 MCP Server 注册、异步工具发现、工具策略、受控调用、安全结果读取，以及由独立 Sandbox
执行的 registered Endpoint-only 出网；不提供 Agent 编排、租户自定义 hostname/IP/CIDR/port allowlist 或任意
第三方网络访问。

详细数据、并发、审批、DLP 和恢复边界见
[PRD-082 设计文档](../prd/PRD-082-mcp-tool-registration-permissions.md)；Sandbox、ACL、permit、熔断与 PAGE-115
边界见 [PRD-083 设计文档](../prd/PRD-083-third-party-mcp-sandbox-network-acl.md)。

## 1. 权限、租户与响应边界

| 方法与路径 | 后端权限 | 行为 |
| --- | --- | --- |
| `GET /mcp/servers` | `mcp.read` | 当前 tenant 的 Server 安全摘要 |
| `POST /mcp/servers` | `mcp.server.manage` | 持久化注册 operation 与异步发现任务 |
| `PATCH /mcp/servers/{id}` | `mcp.server.manage` | CAS 改名、启停或候选凭据轮换 |
| `PATCH /mcp/servers/{id}/network-policy` | `mcp.read` + `mcp.network.manage` | CAS 更新 registered Endpoint-only 网络策略 |
| `POST /mcp/servers/{id}/refresh-tools` | `mcp.server.manage` + `mcp.policy.manage` | 持久化 refresh operation |
| `GET /mcp/server-operations/{id}` | `mcp.read` | 查询固定 operation 终态 |
| `GET /mcp/tools` | `mcp.read` | 工具安全摘要列表 |
| `GET /mcp/tools/{id}` | `mcp.read` | 按需读取 Schema 与策略详情 |
| `PATCH /mcp/tools/{id}` | `mcp.policy.manage` | CAS 冻结风险、审批、授权与脱敏策略 |
| `POST /mcp/tools/{id}/invoke` | `mcp.invoke` + 工具附加授权 | 创建持久 Invocation |
| `GET /mcp/invocations` | `mcp.read` | 当前 tenant 的调用安全摘要 |
| `GET /mcp/invocations/{id}` | `mcp.read` | 调用状态与安全事实 |
| `GET /mcp/invocations/{id}/result` | 原请求者 + 当前 `mcp.invoke` + 附加授权 | 只读 succeeded 的脱敏结果 |

tenant、actor、Session、权限 revision、lease、状态和 crypto owner 只从后端可信上下文恢复，公开请求不能
提交或覆盖。跨 tenant、当前用户不可见和不存在对象使用同形 404。前端隐藏入口或按钮不构成授权。

所有成功和错误响应都携带服务端生成的 `request_id`、`trace_id`，并设置：

```http
Cache-Control: no-store, private
Pragma: no-cache
Vary: Authorization
```

`/mcp` 最外层边界忽略客户端提交的 request/trace 标识；`Vary` 会与已有值安全合并且只保留一个
`Authorization`。统一错误 envelope 不包含请求正文、Endpoint、
Bearer Token、参数、Schema description、结果、DLP 命中片段、SQL 或底层异常文本。

## 2. Exact HTTP wire

- 写接口只接受恰好一个 `Content-Type: application/json`，禁止 Content/Transfer-Encoding。
- JSON 必须是严格 UTF-8 object，禁止重复 key、未知字段、NaN/Infinity 和类型强转。
- register/Server PATCH/network-policy PATCH 最大 16 KiB，refresh 最大 4 KiB，Tool policy 最大 32 KiB，
  invoke 最大 128 KiB。
- GET 请求不接受正文；无 query 的路由拒绝任意 query。
- register、Server PATCH、refresh 和 invoke 必须携带恰好一个 `Idempotency-Key`：8..128 个可见 ASCII，
  禁止逗号。相同 tenant/domain/key 只能重放同一 canonical intent。
- 列表 `limit` 为 `1..100`，默认 50；cursor 是 tenant/filter 绑定的服务端签名游标。
- authentication/permission 检查先于敏感正文解析；匿名和无权用户不能借畸形正文探测合同。

## 3. Server 注册、启停与刷新

`POST /mcp/servers` 请求：

```json
{
  "name": "受控显示名称",
  "endpoint_url": "https://example.invalid/mcp",
  "auth": {
    "type": "bearer",
    "bearer_token": "仅在本次请求中提交"
  }
}
```

`auth.type` 为 `none/bearer`；只有 bearer 可以携带 `bearer_token`。Endpoint 与 Token 分域 AES-GCM 加密，
响应只返回 scheme/host/path-presence 形成的 `endpoint_hint`，永不回传 URL 或 Token。

注册先提交 `pending_sync` Server、credential candidate、幂等 operation 和 sync job，再返回 202。PRD-083
默认创建 revision 1 disabled 网络策略，sync job 固定为 `waiting_policy`，不设置 deadline、lease、attempt，
也不访问网络或产生 `mcp.network_blocked`。首次启用策略才原子恢复为 pending 并生成新 deadline。即使 Sandbox
或上游不可用，失败请求也保留可查询事实。工具发现成功后 Server 进入 active；失败进入 sync_failed。新工具
默认 review_required，必须由策略管理员明确审核后才可调用。

`PATCH /mcp/servers/{id}` 使用：

```json
{
  "expected_revision": 3,
  "status": "disabled"
}
```

请求至少包含 `name/status/auth` 之一；`status` 只接受 active/disabled。凭据轮换先建立 candidate，probe
成功后才 CAS 切换 active revision，失败保留旧 active credential。启停、update、rotate 均产生独立
operation；迟到 worker 不能覆盖新 revision 或 terminal 状态。

`POST /mcp/servers/{id}/refresh-tools` 请求只有 `expected_revision`。相同 Schema 只 touch 时间；Schema 或
安全 annotations 漂移推进 revision 并回到 review_required；远端删除变为 disabled，不物理删除历史事实。

Server 列表只允许 `status/limit/cursor`。operation 响应只含 type、status、固定 error code 和时间，不含
Endpoint、credential、capability 正文或第三方错误正文。每个 Server 额外返回当前 revision 的
`network_policy` 安全摘要；历史 revision 的 blocked/circuit 事实不能混入当前摘要。

## 4. 工具 Schema 与策略

`GET /mcp/tools` 允许 `server_id/status/risk/approval_required/limit/cursor`。列表返回 Tool/Server ID、受控
名称、Schema hash、revision、risk、approval、附加权限、授权数量、脱敏路径数量和状态，不返回完整 Schema。

`GET /mcp/tools/{id}` 按需返回 original/effective input/output Schema、受控 description、角色/用户 grant
和 RFC 6901 脱敏路径。响应仍是 no-store，调用方不能把详情写入 URL、Storage、日志或验收制品。

平台使用 `jsonschema` Draft 2020-12 离线 profile。original Schema 不变保存；effective Schema 注入有界
object/array/string 限制并用于实际参数和结构化结果验证。远程/本地 ref、未知 dialect/format、可执行扩展、
不安全 regex、重复 key、无限数值、超深/超大 Schema 均失败关闭。

`PATCH /mcp/tools/{id}` 示例：

```json
{
  "expected_policy_revision": 2,
  "status": "active",
  "risk_level": "high",
  "approval_required": true,
  "approval_expires_seconds": 1800,
  "required_permission_codes": ["request_log.read"],
  "allowed_role_ids": ["role_developer"],
  "allowed_user_ids": [],
  "result_redaction_pointers": ["/credentials/0/value"]
}
```

high 风险必须审批。附加 permission 不能包含审批权限、未知 code 或重复项；role/user grant 和基础
`mcp.invoke` 同时生效。策略 revision、Server/credential/Schema revision、grant 与脱敏事实会冻结进每个
Invocation，worker 在真正调用前仍重读当前权限和状态。

## 5. 调用、审批与结果

`POST /mcp/tools/{id}/invoke` 请求：

```json
{
  "arguments": {
    "bounded_field": "value"
  }
}
```

参数先按 effective input Schema 验证，再执行参数 DLP。参数只保存分域密文与不可逆 HMAC；响应、Audit、
event、日志和 DOM 只出现 clean/log_only/blocked/unavailable、match count 和稳定 rule ID。

- standard 调用在 preflight 通过后进入 queued。
- high 调用在同一 deferred-FK 事务原子创建 Invocation 与 ApprovalTask，状态为 approval_pending。
- 审批者必须同时具备 `approval.decide` 和 `mcp.approve`，且不能是原请求者；批准事务不执行网络 I/O。
- 拒绝、超时、DLP block、Schema/权限失败不产生远端 call intent。
- durable call intent 后遇到连接未知、超时或完成状态无法确认时固定为 outcome_unknown，禁止自动重试。

Invocation 摘要不回显参数或结果，包含 status、risk、approval ID、输入/输出 DLP、redaction count、attempt、
bytes、duration、固定 error code 及以下追踪三态：

| execution | invocation_count | tool_call_count | cost_status |
| --- | ---: | ---: | --- |
| `not_called` | `1` | `0` | `not_applicable` |
| `called` | `1` | `1` | `not_reported` |
| `unknown` | `1` | `null` | `unknown` |

`GET /mcp/invocations` 允许 `status/tool_id/server_id/limit/cursor`。结果接口只对原请求者和当前仍完整授权的
succeeded 调用返回已经 Schema 验证、DLP 检查、exact-key 脱敏和 RFC 6901 路径替换后的 JSON；blocked、
failed、rejected、unknown 或权限撤销均不返回正文。

## 6. 网络策略

`PATCH /mcp/servers/{id}/network-policy` 不接受 query 或 `Idempotency-Key`，并在读取正文前和事务内同时复核
`mcp.read` 与 `mcp.network.manage`。请求 exact wire：

```json
{
  "expected_revision": 1,
  "status": "active",
  "egress_mode": "registered_endpoint_only",
  "connect_timeout_seconds": 5,
  "probe_timeout_seconds": 10,
  "call_timeout_seconds": 30,
  "failure_threshold": 3,
  "open_seconds": 60
}
```

`status` 只接受 `active/disabled`，`egress_mode` 固定为 `registered_endpoint_only`。三个 timeout 分别为
`1..10`、`1..30`、`1..120` 秒并受部署上限约束，且 connect timeout 不得大于 call timeout；failure
threshold 为 `1..10`，open seconds 为
`10..3600`。请求不能提交 Endpoint、hostname、IP、CIDR、port、proxy 或 DNS Server。Control 不解密
Endpoint；Sandbox 才从密文派生 exact origin，并绑定 Server/credential/policy revision、lease 与 caps
fingerprint。

成功返回 HTTP 200、`network_policy` 和服务端 `request_id/trace_id`。安全摘要只包含 policy/server ID、
status/revision、固定 egress mode、destination count、timeout/breaker、current-revision circuit state、blocked
count、最后固定 reason/time 和 updated time。active 的 destination count 为 1，disabled 为 0；不返回
hostname、Endpoint path、resolved address、CIDR、credential、permit 或第三方错误正文。

## 7. Runtime 与网络边界

生产 Control/Scheduler 不解析 tenant Endpoint DNS，也不建立对应 socket。Scheduler 只向独立 Sandbox 发送
HMAC envelope；permit secret 仅由 Sandbox 持有。Sandbox 完整检查 DNS answer set、永久 deny、部署 ACL、exact
origin 和 caps fingerprint，随后固定 IP 连接并保留 Host/SNI，不做第二次 DNS。redirect、环境代理、Unix socket、
HTTP/2 和连接复用均关闭。authorize-call 返回的一次性加密 permit 必须先于 durable call intent；call 阶段消费
permit 后不得重新解析 DNS。

PRD-082 的 test-only loopback adapter 只用于既有测试兼容，不是生产执行路径。生产 `executor_mode` 必须为
`sandbox_http`，Control、Scheduler、Sandbox 使用彼此独立的进程角色与最小配置；Control 不持 Sandbox secret，
Scheduler 不持 permit secret，Sandbox 不持数据库或 MCP 加密主密钥。

单 Server 最多 100 个工具、最多 10 页发现、总响应最大 2 MiB；probe/call 使用部署固定 deadline。网络在
数据库事务外执行，公平 lease 在 durable claim 后释放；过期 owner 只能由新 generation CAS 接管。

## 8. 事件、审计与稳定错误

只有已经形成 durable call intent 的 called/unknown 终态生成 `mcp.tool_called`。事件包含 tenant、
Invocation/Server/Tool、状态、attempt、bytes、duration、DLP 摘要、redaction count、execution/count/cost
三态和 request/trace；不含 Endpoint、Token、参数、Schema、结果或第三方错误正文。source/outbox 使用稳定
event/delivery ID、tenant 公平 lease、失败重试、dead-letter 与重启恢复。

确定未形成 call intent 的 ACL 阻断生成 `mcp.network_blocked`，使用独立 source/outbox。payload 只包含
Server/operation 或 Invocation ID、policy revision、阶段、固定 reason、address count 与 request/trace；不含
Endpoint、hostname、IP、CIDR、permit、参数或结果。数据库 trigger 保证同一 Invocation 不能同时产生
`mcp.network_blocked` 与 `mcp.tool_called`。

| code | HTTP | 场景 |
| --- | ---: | --- |
| `mcp.feature_disabled` | 503 | 部署未启用 MCP 治理 |
| `mcp.executor_disabled` | 503 | 当前部署禁止 MCP 网络执行 |
| `mcp.network_policy_invalid` | 422 | 网络策略 exact body、范围或部署上限不成立 |
| `mcp.network_blocked` | 403 | ACL 明确阻断且确定未执行工具 |
| `mcp.sandbox_unavailable` | 503 | Sandbox、内部认证、响应或 permit 不可用 |
| `mcp.request_invalid` | 422 | exact wire、字段、类型或资源边界不成立 |
| `mcp.schema_invalid` | 422 | Schema 不满足有界 profile |
| `mcp.sync_failed` | 502 | 工具发现已形成固定失败 |
| `mcp.credential_invalid` | 422 | 候选凭据 probe 失败 |
| `mcp.server_not_found` / `mcp.tool_not_found` / `mcp.invocation_not_found` | 404 | 不存在、跨 tenant 或不可见 |
| `mcp.state_conflict` | 409 | revision、owner 或状态发生并发漂移 |
| `mcp.idempotency_required` | 400 | 缺少或非法 Idempotency-Key |
| `mcp.idempotency_conflict` | 409 | key 已绑定另一 intent |
| `mcp.arguments_invalid` | 422 | 参数不满足 effective Schema |
| `mcp.arguments_blocked` / `mcp.result_blocked` | 403 | 输入或输出 DLP 阻断 |
| `mcp.output_dlp_unavailable` | 503 | 已调用结果不能安全扫描 |
| `mcp.approval_self_denied` / `mcp.authorization_revoked` | 403 | 自审或实时授权失败 |
| `mcp.server_unavailable` | 503 | call intent 前 Server 不可用 |
| `mcp.tool_failed` / `mcp.result_invalid` | 502 | 已知工具失败或结果合同失败 |
| `mcp.outcome_unknown` | 503 | 远端副作用无法安全确认 |
| `mcp.unavailable` | 503 | 存储、keyring、scheduler 或 outbox 不可用 |

调用方必须按 HTTP 状态和 `code` 分支，不得解析 message 或 suggestion。`mcp.outcome_unknown` 明确要求先
对账远端副作用，不能用相同或新 idempotency key 自动重试。
