# PRD-083 第三方 MCP 沙箱网络 ACL 设计

状态：两轮编码前自审均已完成并修正；编码门禁已关闭全部 P0/P1。

日期：2026-08-16

## 1. 权威范围与交付边界

### 1.1 交叉确认

本设计以工作簿、README、PRD-082 设计/测试记录和系统架构文档的交叉结果为准，而不是按编号推断：

- `01_PRD总表!A84:R84` 与 `03_M2-M3扩展交付!A20:R20`：
  `PRD-083 | M2-企业增强 | P2 | 扩展层 | MCP沙箱 | 第三方MCP沙箱网络ACL`。
- `04_页面清单!A116:H116`：`PAGE-115`，MCP 安全配置页。
- `05_API接口清单!A379:H383`：五个单元格共同组成一个接口，
  `PATCH /mcp/servers/{id}/network-policy`，不是五个接口。
- `06_数据对象!A115:F115`：`OBJ-114 McpNetworkPolicy`。
- `07_Webhook事件!A96:H96`：`EVT-095 mcp.network_blocked`。
- `08_验收用例!A166:J167`：`TC-0165/0166`。
- 权威验收语义：第三方 MCP 位于沙箱子网；只允许白名单出站；禁止访问数据库、ERP、文件服务器；
  沙箱违规访问必须阻断并审计；调用超时触发熔断并释放会话。

PRD-082 已完整交付并冻结为本项基线。0075、MCP Server/Tool/Invocation、审批、DLP、租约、
`call_intent`、`outcome_unknown`、`mcp.tool_called` 及 PAGE-114 均不得重复实现或旁路。

### 1.2 架构约束

系统架构文档要求 MCP 位于扩展层，控制面承载低频配置，调度/控制/执行职责分离，影响执行的配置必须
版本化和留痕，并且 MCP 不能进入 Gateway 模型调用热路径。因此本项只扩展 Control、MCP Scheduler、
独立 Sandbox Worker 和 Console，不修改 Chat/Provider 路由主链路。

### 1.3 明确不做

本项不承担第三方工具自身的业务逻辑安全，也不声称能阻止公网 MCP 服务在其服务端再次访问其他系统。
ACL 只约束 MetaHub Sandbox 实际发起的网络连接。

本项还明确不做：

- 不托管、下载或执行客户提供的任意二进制、脚本、容器或 stdio MCP Server。
- 不增加旧 SSE、stdio、resources、prompts、sampling、roots、OAuth、客户端证书或任意自定义 Header。
- 不实现 Agent planning、memory、多工具规划或模型自动选工具。
- 不允许租户提交代理地址、DNS Server、解析 IP、Unix socket、通配符或域名后缀规则。
- 不承诺网络 exactly-once，也不在 durable call intent 后自动重试工具调用。
- 不把 OpenRouter、硅基流动加入默认上游目录；Provider 默认上游定位保持不变。
- 不改变 `all_108_prds_complete=false`。

## 2. 现状与问题定义

PRD-082 当前只有 `disabled` 与 `test_loopback` executor mode。生产环境固定禁用 executor；测试 adapter
只允许字面量 `127.0.0.1`/`::1`，HTTP 固定 `trust_env=false`、`follow_redirects=false`，但不解析 DNS，
也没有 tenant policy、IP pinning、独立网络 namespace 或持久熔断。

现有 Scheduler 在同一进程领取 sync/invocation，读取 SQLite，解密 Endpoint/Bearer，并直接调用 executor。
如果只在 `executor.py` 中新增一个 production 分支，将产生四个不可接受的问题：

1. Control/Scheduler 可直接访问租户 Endpoint，沙箱边界不存在。
2. URL 校验与实际 connect 之间仍可能 DNS rebinding。
3. Control 与独立 Worker 可同时领取任务，disabled executor 会制造错误终态。
4. 网络阻断、熔断和策略 revision 无法与 0075 lease/CAS 状态机绑定。

因此生产能力必须通过独立 `mcp-sandbox-worker` 交付。Control 只管理策略；Scheduler 只读取业务事实、
做授权/DLP、推进持久状态并调用内部 worker；只有 Sandbox Worker 可以解析 tenant Endpoint DNS 和建立
对应 socket。

## 3. 关键设计决定

### 3.1 “第三方 MCP 放入沙箱”的解释

PRD-082 只支持远程 Streamable HTTP。PRD-083 的“沙箱”指远程 MCP 出网执行平面位于独立进程、容器、
网络 namespace 和专用子网，不表示把第三方 MCP 程序托管在 MetaHub 内。

### 3.2 三段职责

1. **Control API**：认证、后端 RBAC、tenant owner、策略 CAS、审计和公开 DTO；不解析 tenant DNS，
   不解密 Endpoint/Bearer，不建立 MCP socket。
2. **MCP Scheduler**：复用 0075 的公平 tenant lease、operation/invocation lease、审批、DLP、Schema、
   `call_intent` 和 finalize；向 Sandbox Worker 发送短期认证 envelope。生产 Compose 中 Scheduler 使用独立
   `mcp-scheduler-worker` 进程，Control API 的嵌入式 scheduler 关闭。
3. **MCP Sandbox Worker**：不挂载 SQLite、日志卷、对象存储或宿主目录；只持有 MCP keyring、内部认证密钥
   和部署级网络上限；在内存中解密 Endpoint/Bearer，执行 DNS/ACL、建立固定 IP 连接并在 deadline 后关闭
   session/socket/pool。Worker 使用独立最小 `SandboxConfig`，不加载 Control `AppConfig`，不接收数据库、Redis、
   Provider、Webhook 或对象存储配置。

### 3.3 生产角色与配置闭集

生产启动合同按进程冻结，任何跨角色变量、缺失变量或 mode 组合均失败关闭：

| 进程 | 必须配置 | 明确禁止 |
| --- | --- | --- |
| Control API | `METAHUB_MCP_SCHEDULER_ENABLED=false`、`METAHUB_MCP_EXECUTOR_MODE=disabled`、数据库、既有 Control MCP ciphertext keyring、共享非敏感 `McpNetworkPolicyCaps` | Sandbox base URL、internal token、envelope/permit/response secret；领取 MCP job；tenant DNS/socket |
| MCP Scheduler | `METAHUB_MCP_SCHEDULER_ENABLED=true`、`METAHUB_MCP_EXECUTOR_MODE=sandbox_http`、数据库、MCP ciphertext keyring、共享 caps、Sandbox internal URL/token、envelope signing secret、response current/previous key | permit secret、部署 CIDR/port ACL；`test_loopback`；直接解析或连接 tenant Endpoint；公开 HTTP API |
| MCP Sandbox Worker | 独立最小 `SandboxConfig`、`METAHUB_MCP_SANDBOX_WORKER_ENABLED=true`、MCP ciphertext keyring、共享 caps、internal token、envelope verification secret、permit secret、response current key、部署级 ACL/阶段 deadline 上限 | response previous key、Control `AppConfig`、executor mode、scheduler、数据库 URL、Redis、Provider、Webhook、对象存储和宿主目录配置 |

Sandbox Worker 不接收 worker URL；Scheduler 不接收部署 ACL CIDR/port；Control 不持有任何 Sandbox 专用 secret。
只有对应角色的闭集全部有效时才启动，任一依赖缺失均失败关闭，不回退直连或 test loopback。`test_loopback`
继续仅服务自动化测试并在 prod 被拒绝；`disabled` 只用于 Control 或明确无出网的非生产模式。

Sandbox 使用专用 `infra/docker/mcp-sandbox.Dockerfile`，镜像只复制 Shared MCP 与 Sandbox Worker 源码及其锁定依赖，
不复制 Control/Gateway、迁移、Console、验收脚本或数据库工具。Compose 不向 Sandbox 传公共 `env_file`，只逐项注入
上述 SandboxConfig 闭集。

Control、Scheduler、Sandbox 复用 Shared 的 immutable `McpNetworkPolicyCaps`：connect/probe/call max、failure
threshold max、open seconds max 按字段顺序做 canonical JSON SHA-256。Control 用该对象在 PATCH 时返回 exact 422；
Scheduler 把 caps fingerprint 纳入每个 envelope，并在 readiness 调用中要求 Worker 返回相同 fingerprint；Worker
逐请求再次匹配 fingerprint。任一进程缺失 caps、三方指纹不一致或 Worker 私有阶段上限超出 shared overall cap 均
失败关闭。

## 4. Sandbox 内部协议

### 4.1 内部路由

Sandbox Worker 只暴露到内部沙箱控制子网：

- `POST /internal/mcp-sandbox/discover`
- `POST /internal/mcp-sandbox/probe`
- `POST /internal/mcp-sandbox/authorize-call`
- `POST /internal/mcp-sandbox/call`
- `GET /health/live`
- `GET /health/ready`

这些不是租户公开 API，不进入 Excel API 清单或 Console client。Uvicorn access log 关闭，异常只输出固定 code、
worker request ID 和安全计数。

### 4.2 认证 envelope

每个请求采用 exact JSON envelope，包含：

- `schema_version`、`action`、`dispatch_id`、`issued_at`、`expires_at`；
- `tenant_id`、`mcp_server_id`，以及 operation 或 invocation 的一种 owner；
- `lease_generation`、`lease_owner_id`、`lease_token_hmac`；
- Server、credential、Schema、Tool policy、network policy 的 revision/fingerprint；
- Endpoint ciphertext、credential ciphertext 及对应 owner/key version；
- deadline、字节上限和 action 所需的工具/参数 ciphertext/HMAC；
- `authorize-call` 预生成但尚未持久化的 `mcp_network_call_intent_id`；
- `/call` 的已持久化 call-intent ID、授权 decision ID、permit digest 和 arguments HMAC。

Control/Scheduler 先 canonical JSON，再用独立 `METAHUB_MCP_SANDBOX_ENVELOPE_SECRET` 做 HMAC；HTTP 还要求
固定内部 bearer token。两者任一失败均返回 `mcp.sandbox_unavailable` 的内部固定结果，不回显原因。

Envelope 最长存活 10 秒。Worker 每次启动生成随机 boot ID 并将其纳入 permit；重启立即使旧 permit 失效。
Worker 维护有界 TTL replay cache；重复 `dispatch_id` 拒绝。`authorize-call` 与 `/call`
使用不同 dispatch ID；前者的 permit 绑定预生成 call-intent ID，后者必须提交该 ID 和已持久 decision。Permit 还
完整绑定 tenant/server/invocation/tool、endpoint HMAC、credential/Schema/Tool policy/network policy revision、
arguments HMAC、lease token HMAC、deadline 和 action。Envelope、permit、Endpoint、Token、arguments/result 均
不进命令行、环境变量、文件、日志或验收 JSON。这里的 Token 指 tenant Bearer；部署级内部认证 secret 只能由
编排注入环境且必须 `repr=False`，不得输出。

### 4.3 明文最小化

Scheduler 从数据库读取 ciphertext 和 owner metadata，但不解密 Endpoint/Bearer。Worker 在请求作用域内解密，
以 `repr=False` 对象持有，并在 session 关闭后释放引用。Scheduler 仍按 PRD-082 合同解密 arguments 做输入 DLP，
但发给 Worker 的是原 arguments ciphertext/owner 和 arguments HMAC；Worker 独立解密后调用。该既有控制面数据
处理不扩大 MCP 网络权限。

### 4.4 内部响应 envelope

Worker 不把 Schema、Tool metadata 或 result 作为明文 JSON 返回。每个响应固定包含 request/dispatch/action、
tenant/owner、lease、policy fingerprint、`status`、固定 `failure_code`、安全 decision/bytes/duration 计数、
`response_key_version`、`payload_nonce`、`payload_ciphertext`、`payload_hmac` 和 `response_hmac`。Worker 每次响应使用
12-byte CSPRNG nonce 和 AES-256-GCM；AAD 是上述 schema/request/dispatch/action/tenant/owner/lease/policy/status/
failure code 字段的 canonical JSON，不包含密文。`METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT` 作为 HKDF-SHA256
IKM，salt 固定为 `SHA256("metahub-mcp-sandbox-response/v1:" + response_key_version)`，分别以
`metahub/mcp/response/aead` 和 `metahub/mcp/response/hmac` info 派生两个 32-byte 域密钥。`payload_hmac` 覆盖
AAD、nonce 与 ciphertext；`response_hmac` 覆盖整个不含自身的 canonical response envelope。Scheduler 必须先验证
action、owner、dispatch、lease、deadline、key version 与两个 HMAC，再解密。

`METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION` 为正整数；Worker 只持有并使用 current secret/current version 发响应。
Scheduler 可选加载 `METAHUB_MCP_SANDBOX_RESPONSE_SECRET_PREVIOUS`，仅接受 `current_version-1`，用于覆盖轮换时尚未
超过 envelope/lease deadline、由旧 Worker 发出的在途响应。轮换顺序固定为 Scheduler 先加载 new current + old
previous，再滚动 Worker；旧 Worker 以其 old current 排空后退出，新 Worker 不接收 previous。不存在在途旧请求后
Scheduler 删除 previous。未知、跳号或回退 key version 失败关闭，AEAD 与 HMAC 不得直接复用原始 secret。

Envelope 与 permit 在本 PRD 不提供双版本窗口：Scheduler 独占 envelope signing secret，Worker 只验证 envelope；
permit secret 只存在于 Worker，Scheduler 只能持久化 permit digest，不能生成或验证 opaque permit。两类 secret
轮换采用 Scheduler/Worker 协调重启；Worker boot ID 会使重启前全部 permit 立即失效，call intent 前重新授权，
call intent 后按 `mcp.outcome_unknown` 收敛。

验签/解密失败发生在 call intent 前时映射为 `mcp.sandbox_unavailable`；发生在 call intent 后时按可能已执行收敛
为 `mcp.outcome_unknown`。响应不得被自动重放到另一个 job/invocation/lease。

## 5. 网络策略模型

### 5.1 tenant 可配置合同

公开 PATCH body 冻结为：

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

Exact 约束：

- `extra=forbid`，body 最大 16 KiB；`expected_revision>=1`。
- `status` 仅 `disabled|active`；`egress_mode` 只能是 `registered_endpoint_only`。
- 租户不能再提交 hostname、URL、IP、CIDR、port、proxy 或 DNS Server。唯一可允许目标由 Worker 从已加密的
  Server Endpoint 派生，并且只允许该 Endpoint 的 exact `(scheme, hostname, port)`；redirect 始终禁止。
- production Server Endpoint 继续由 PRD-082 限制为 HTTPS；非 prod 的 test loopback 不属于公开 policy 能力。
- connect timeout 为 1..10 秒；probe timeout 为 1..30 秒且不得超过部署级 probe deadline；call timeout 为
  1..120 秒且不得超过部署级 call deadline 和 Invocation deadline；connect timeout 不得大于 call timeout。
  租户只能收紧，不能放宽部署上限。
- failure threshold 为 1..10；open seconds 为 10..3600。

Streamable HTTP MCP Client 在本项内只需要访问其已注册 Endpoint。把第二套 hostname allowlist 暴露给租户既
没有协议必要性，又会制造 Endpoint 漂移和明文复制面，因此不支持多个 destination。私网例外只由部署运维通过
`METAHUB_MCP_SANDBOX_PRIVATE_CIDRS` 配置，且只接受 IPv4 `/32` 或 IPv6 `/128` 精确目标，不接受宽网段。
Exact Server Endpoint 仍必须命中；tenant policy 只能控制启停、
更严格的 deadline 和 breaker，不能自行开放整个 RFC1918 网段。

### 5.2 永久 deny

不论 tenant 或部署配置，以下目标始终拒绝：

- loopback、unspecified、link-local、multicast、IPv4-mapped 绕过形式；
- 云 metadata，包括 `169.254.169.254`、`100.100.100.200` 及部署维护的 metadata 集合；
- Unix socket、环境代理、PAC、`.netrc`、redirect、非 HTTP(S) transport；
- 容器 runtime socket、Kubernetes API、宿主机控制端口；
- 部署 deny CIDR 和 deny port 中的数据库、ERP、文件服务器目标。

默认 deny ports 至少覆盖常用数据库/缓存/文件协议：1433、1521、2049、2375、2376、3306、5432、5984、
6379、6443、8020、8500、9000、9042、9200、11211、27017。443 只有在 exact destination 和地址分类均通过
时才允许，不能仅因端口为 443 放行。

决策优先级固定为：永久 deny > deployment deny CIDR/port > deployment private CIDR exception 与 allowed port >
registered Endpoint exact origin。部署 allow/deny CIDR 重叠、allowed/deny port 重叠或非 canonical 配置导致 Worker
readiness 失败，不能按“后配置覆盖前配置”容错启动。

### 5.3 DNS 与防重绑定

DNS 只在 Sandbox Worker 内解析。每次 authorization：

1. 对 canonical hostname 使用绝对 DNS 名执行单次有 deadline 的 `getaddrinfo`，禁止 search suffix；限制 8 个
   最终 A/AAAA 地址和 2 秒解析 deadline。标准库不暴露可靠 CNAME 链，因此本项不虚构可验收的 CNAME 深度门禁。
2. 收集该次解析返回的完整地址集合；混合允许/禁止或超量整次阻断。NXDOMAIN、SERVFAIL、空结果和解析超时属于
   `mcp.server_unavailable` 可用性失败并计入 breaker，不伪装成 ACL 违规事件。
3. 规范化 IPv4、IPv6 和 IPv4-mapped IPv6，再应用永久 deny、部署 allow/deny 和 registered Endpoint origin。
4. 生成只在 Worker 可解密/验签的一次性 permit，绑定 host、port、解析地址集合、policy revision/fingerprint、
   dispatch/lease、expiry 和 action。
5. connect 使用 permit 中已批准 IP，HTTP URL/Host 与 TLS SNI/证书校验仍使用原 hostname；不得二次 DNS。

传输池固定 `http1=true`、`http2=false`、`max_connections=1`、`max_keepalive_connections=0`、
`keepalive_expiry=0`。禁止 origin coalescing、跨 tenant/server/policy 复用和 redirect。每次重试必须重新授权；
但 tools/call 在 call intent 后从不自动重试。

### 5.4 调用前 permit

调用流程必须在 durable call intent 前完成 `authorize-call`：

1. PRD-082 输入 DLP、授权和 Tool probe 成功。
2. Worker 解析 DNS、执行 ACL，返回 opaque permit 与随机内容的 SHA-256 digest；此步不向上游发送字节。
3. 阻断时，Invocation 以 `execution_status=not_called`、`tool_call_count=0`、
   `cost_status=not_applicable` 终结为 `blocked/mcp.network_blocked`，只生成 EVT-095。
4. 放行时，Scheduler 在同一事务绑定 permit digest、network policy revision/fingerprint 并创建 durable call intent。
5. Worker `/call` 只接受同一 lease/action 的未过期 permit，直接连接已批准 IP，不再解析 DNS。

一旦 durable call intent 已提交，Worker 超时、崩溃、连接中断或响应不确定继续按 PRD-082 收敛为
`mcp.outcome_unknown` 并保留 EVT-094，绝不能降级为“未调用”或自动重试。

## 6. 0076 数据模型

所有业务表都以 `tenant_id` 为 owner，使用复合 FK、tenant-first 索引、严格 ID/check、append-only/transition
trigger 和 UTC 秒级时间。公开请求不能提交 tenant、actor、decision、resolved IP、lease 或内部 worker 字段。

### 6.1 `mcp_network_policies`

每个 MCP Server 恰有一条 current aggregate：

- `mcp_network_policy_id`、`tenant_id`、`mcp_server_id`；
- `current_revision`、`current_fingerprint`、`status`；
- `created_at`、`updated_at`。

Aggregate 不保存 Endpoint 或第二套 destination。更新必须按 current revision/fingerprint/updated_at 做 CAS，且
current 字段必须复合引用同一 owner 的 immutable revision。

### 6.2 `mcp_network_policy_revisions`

每次策略更新追加一条不可变 revision：

- policy/tenant/server、`policy_revision`、`policy_fingerprint`；
- `status`、固定 `egress_mode=registered_endpoint_only`、`priority=1000`、
  `condition_expr=registered_endpoint_origin`、`action=allow`；
- `connect_timeout_seconds`、`probe_timeout_seconds`、`call_timeout_seconds`、`failure_threshold`、`open_seconds`；
- `updated_by`、`request_id`、`trace_id`、`created_at`。

`policy_fingerprint` 是不包含 Endpoint 的 exact 非敏感设置 canonical SHA-256；Endpoint 身份继续使用 0075 的
tenant/domain HMAC，并通过 envelope 单独绑定。0076 为所有既有 Server 建立 revision 1、disabled 的 deny-all
aggregate/revision；Server insert trigger 对新 Server 原子建立相同默认策略。迁移或注册不会自动启用生产出网。

新注册和 refresh 在 current policy 为 disabled 时不尝试网络，也不生成 blocked decision 或 EVT-095。注册事务仍
创建 0075 operation 与 sync job，但 operation 保持 `queued`，job 使用 0076 新状态 `waiting_policy`。该状态固定
`attempt_count=0`、全部 lease/next_attempt/started/completed/error 为 NULL，`deadline_at=NULL`；等待期不启动执行
deadline，也不设隐式 TTL。

所有创建 sync job 的写事务均先按 tenant/server 检查；只要已有 `waiting_policy|pending|processing` job，就在创建
新 operation 前返回 `mcp.state_conflict`。0076 insert/transition trigger 做相同复核以关闭并发竞态，因此每个 Server
最多一个非终态 sync job。首次从 disabled PATCH 为 active 的同一事务只会命中这一行，CAS 改为 `pending`、保持
attempt 0、设置 `next_attempt_at=now`，并从激活时刻按该 policy probe overall deadline 加 Shared 固定 6 秒
Worker response reserve，再加既有固定 6 秒 `SYNC_FINALIZE_RESERVE_SECONDS` 生成新的
`deadline_at`；同时记录非敏感 `waiting_job_resume_count=1`，随后在提交后唤醒 Scheduler。无等待行时计数为 0。
重复 active PATCH 不重复恢复。只有 active policy 下 Worker 对真实地址/端口作出的确定性违规 decision 才产生
EVT-095。

### 6.3 `mcp_invocation_network_bindings`

每个 Invocation 在创建事务中恰好绑定当时 current policy：tenant/invocation/server/tool、policy ID、revision、
fingerprint、status、egress mode 和 created_at。该行 append-only，并以复合 FK 同时引用 Invocation、Tool、Server
和 policy revision。Scheduler 在 probe/authorize-call 前要求 current policy 与 binding 完全一致；策略漂移时
旧 Invocation 以 `mcp.authorization_revoked` 零调用失败关闭。绑定为 disabled 时仍保留失败请求事实，执行阶段按
`policy_disabled` 生成 blocked decision/EVT-095。

### 6.4 `mcp_network_decisions`

每次实际 authorization 形成不可变事实：

- decision、tenant/server、operation 或 invocation owner（恰好一种）；
- phase：`sync|probe|call_preflight`；result：`allowed|blocked`；固定 reason code；
- policy revision/fingerprint、address_count、duration_ms、attempt_number、dispatch ID；
- permit digest（只允许 allowed/call_preflight 非空）；
- observed circuit state revision，以及 half-open owner type/id/generation（如适用）；
- request_id、trace_id、created_at。

不保存 hostname、URL、resolved IP、CIDR、DNS 错误正文、Token、参数、结果或第三方响应正文。Allowed decision
只形成追踪事实，不产生 EVT-095；blocked decision 与 EVT-095 必须同事务创建。

### 6.5 `mcp_network_call_intents`

每个 Invocation 最多一行，和 0075 `probing -> calling`、call attempt 创建在同一事务：

- 预生成 call-intent ID、tenant/invocation/server/tool；
- allowed `call_preflight` decision ID、policy revision/fingerprint、permit digest；
- endpoint/credential/Schema/Tool policy revision 或 HMAC；arguments HMAC；
- lease generation/owner/token HMAC、authorized/created time。

复合 FK 与 owner trigger 要求 decision 的 tenant、owner、phase、result、policy、lease 和 permit digest 全量一致。
表为 append-only；不得持久化 opaque permit 或解析 IP。Worker `/call` 只接受该 intent ID。任何 permit/envelope
错误发生在此行提交后都按 `mcp.outcome_unknown` + EVT-094 收敛。

### 6.6 `mcp_network_circuits`

主键为 `(tenant_id,mcp_server_id,policy_revision)`：

- state：`closed|open|half_open`；failure_count；opened_until；
- half-open owner type/id、lease generation/owner/token/expiry；last failure code；
- state revision、created/updated time。

每次 network action 冻结 observed circuit revision。结果写入必须同时命中 operation/invocation lease、policy
revision 和 circuit CAS；只有 half-open owner 可以关闭 half-open，迟到 success/failure 的 CAS 失败不得覆盖较新
状态。Circuit 更新、attempt/job/invocation 终态和对应 EVT-094/095 在同一 SQLite 事务提交。

ACL deny 是确定性安全拒绝，不增加熔断失败计数，也不重试。NXDOMAIN/SERVFAIL/DNS timeout、连接/TLS/headers/
body idle/overall timeout、Sandbox unavailable 等可用性失败按固定分类计数。成功关闭 circuit；达到阈值打开；
到期后只允许一个 half-open probe。不同 tenant、Server 或 policy revision 绝不共享熔断状态。

### 6.7 `mcp_network_events`

EVT-095 source 为 append-only：event/decision/tenant/server、operation 或 invocation、phase、reason、
policy revision、address_count、attempt、request/trace、occurred_at。复合 FK/trigger 要求 source 只引用 blocked
decision；Invocation owner 还必须不存在 call intent 且其终态为 `execution_status=not_called/tool_call_count=0`。
创建 source 的同一事务冻结现有 Webhook delivery snapshot，并创建下述 source outbox；stable event ID 提供
at-least-once 投递，不宣称网络 exactly-once。

### 6.8 `mcp_network_event_outbox`

EVT-095 使用独立 durable source outbox，不能假设通用 Webhook replay scheduler 会替代 source owner：

- outbox/event/tenant owner、status、attempt count、lease generation/owner/token/expiry；
- next attempt、subscription snapshot JSON/frozen time、fixed error、delivered/created/updated time；
- tenant/event 复合 FK 与唯一约束，完整 pending/processing/succeeded/failed transition trigger。

MCP outbox loop 在同一 tenant-fair batch 中轮询 EVT-094 与 EVT-095。EVT-095 claim、snapshot 验证、每次真实
Endpoint dispatch 前的 lease/token 轮换、过期恢复、十次 dead-letter 和迟到 owner CAS 与 PRD-082 outbox 同级；
payload/source 任一不一致失败关闭且不记录正文。

### 6.9 RBAC revision 18

新增独立权限 `mcp.network.manage`，描述为“管理当前租户 MCP Server 的出站网络策略与熔断边界”。默认授予：

- admin：授予；
- security_admin：授予；
- ops_admin：授予；
- developer、finance_admin、viewer：不授予。

只有仍精确匹配 revision 17 默认矩阵的角色才自动扩展；自定义矩阵保持不变。公开 PATCH 在读取 body 前要求
`mcp.read AND mcp.network.manage`，事务内再次读取实时矩阵。它不隐含 `mcp.server.manage` 或
`mcp.policy.manage`，反向也不隐含网络权限；只有 manage 而没有 read 的自定义角色不能调用该 API。

### 6.10 Audit exact schema

策略更新与 Audit 在同一 SQLite 事务提交。Audit 固定 `action=mcp.network_policy_updated`、
`object_type=mcp_network_policy`、`object_id=mcp_network_policy_id`。`before` 与 `after` 只允许：
`mcp_server_id`、`status`、`revision`、`egress_mode`、`destination_count`、`connect_timeout_seconds`、
`probe_timeout_seconds`、`call_timeout_seconds`、`failure_threshold`、`open_seconds`、
`waiting_job_resume_count`；其中 before 的 resume count 固定 0，after 为本事务实际 CAS 恢复数。

Audit 禁止 Endpoint、host、IP、CIDR、credential、permit、secret、第三方错误和任意自由文本。Python action/object/
field whitelist、SQL CHECK/owner trigger 与策略事务必须使用同一 exact 常量集合；缺少 Audit、字段漂移或 owner 不同
时整笔 PATCH 回滚。

## 7. 公开 API

### 7.1 `PATCH /mcp/servers/{id}/network-policy`

- dependency 在读取 body 前同时校验 `mcp.read AND mcp.network.manage`；不接受 query 或 Idempotency-Key。
- path ID、body 和 16 KiB exact budget 使用现有 `McpRoute/read_body/parse_json/no_store`。
- 跨 tenant 与不存在统一 `mcp.server_not_found` 404，不能泄露资源存在性。
- 事务内重新校验权限和 tenant owner；按 `expected_revision` CAS。
- body 不含 destination；Control 无需解密 Endpoint。Worker 执行时才从 ciphertext 派生唯一 registered Endpoint
  origin，并把 0075 endpoint HMAC、Server revision 与 policy revision/fingerprint 绑定到 authorization。
- 成功固定 HTTP 200；所有响应 `Cache-Control: no-store, private`。

成功 response exact wire：

```json
{
  "network_policy": {
    "mcp_network_policy_id": "mcpnp_...",
    "mcp_server_id": "mcps_...",
    "status": "active",
    "revision": 2,
    "egress_mode": "registered_endpoint_only",
    "destination_count": 1,
    "connect_timeout_seconds": 5,
    "probe_timeout_seconds": 10,
    "call_timeout_seconds": 30,
    "failure_threshold": 3,
    "open_seconds": 60,
    "circuit_state": "not_initialized",
    "blocked_count": 0,
    "last_block_reason": null,
    "last_blocked_at": null,
    "updated_at": "2026-08-16T00:00:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

IDs、枚举与整数均非空；`last_block_reason`/`last_blocked_at` 同时为空或分别为固定 reason/RFC 3339 UTC 时间；
`updated_at` 为 RFC 3339 UTC。`destination_count` 在 active 为 1、disabled 为 0。DTO 使用 `extra=forbid`，不包含
hostname、完整 Endpoint path、resolved IP、CIDR、credential 或第三方错误。

安全摘要只聚合 current `network_policy.revision`：`blocked_count` 是该 revision 的 blocked decision 数，
`last_block_reason/last_blocked_at` 是其中按 `(created_at,decision_id)` 最后一条；`circuit_state` 只读取同 tenant/
server/revision circuit 行，不存在时为 `not_initialized`。每次 policy 更新产生新 revision，其摘要固定从
`not_initialized/0/null/null` 开始；旧 revision 的 decision/circuit 事实继续保留，但绝不混入当前摘要。

### 7.2 既有 Server 读取

`GET /mcp/servers` 的每个 Server 增加 network policy 摘要，使 PAGE-115 不新增未登记的读取接口。具有
`mcp.read` 的用户在每个 Server 上获得名为 `network_policy` 的嵌套对象，其字段、类型与上述安全 DTO 完全相同：
policy/server ID、status/revision/egress mode/destination count、三个 timeout、failure threshold、open seconds、
circuit state、blocked count、last block reason/time 与 updated time。只有同时具有 `mcp.network.manage` 的用户
可编辑启停、deadline 和 breaker。没有 hostname 编辑或读取合同；后端仍按公开 DTO 决定返回字段，不能靠前端
隐藏敏感内容。

### 7.3 错误合同

新增稳定错误：

- `mcp.network_policy_invalid`：422，策略正文不满足 exact 合同；
- `mcp.network_blocked`：403，ACL 明确阻断且确定未执行工具；
- `mcp.sandbox_unavailable`：503，内部 worker/认证/permit 不可用；公开 operation/invocation 只持久固定 code；
- circuit open 继续使用 `mcp.server_unavailable`，避免把可用性故障伪装成 ACL 违规。

`expected_revision` 过期固定使用既有 `mcp.state_conflict` 409；malformed JSON、duplicate key、非 object、超出
16 KiB 或字段类型错误固定使用既有 `mcp.request_invalid`。合法 JSON 但违反字段枚举/范围，或超过部署级 timeout/
breaker 上限，使用 `mcp.network_policy_invalid` 422。所有错误 response 均为既有统一 envelope 并含服务端
`request_id`，不得把解析器、DNS、socket、TLS、proxy、SDK 或第三方错误正文回显。

所有 API 错误继续使用统一 error body 和服务端 `request_id`。DNS/socket/TLS/proxy/SDK 异常原文统一折叠，
不得进入响应、日志、Audit、Webhook 或验收证据。

## 8. Scheduler、状态机与策略变更

### 8.1 Sync/refresh

新注册或 refresh 在 disabled policy 下进入 `waiting_policy`，Scheduler 不得领取，也不得把默认 deny 解释为安全
违规。首次启用事务将其恢复为 pending 后，Scheduler 才读取 active policy 和 ciphertext、检查 circuit 并构造
discover envelope。Worker 在同一请求内授权并固定连接。真实 ACL blocked 时 job/operation 使用
`mcp.network_blocked` 终态且不重试；
可用性失败继续受既有最多尝试次数和 circuit 约束。迟到结果必须命中 job 全量 lease CAS。

### 8.2 Invocation

保留顺序：领取 -> 实时 RBAC/Tool grant/审批 -> Invocation policy binding 对比 -> 输入 DLP -> probe ->
预生成 call-intent ID -> authorize-call -> 同事务写 network call intent/0075 call intent -> call -> 输出 DLP/脱敏
-> terminal/outbox。任何旧 policy revision、旧 lease、旧 decision 或旧 permit 均不能 finalize。

策略在 invocation 尚未 authorize-call 时变化，旧 invocation 失败关闭为 `mcp.authorization_revoked`；已经绑定
call intent 的调用按冻结 revision 收敛，不通过策略更新强行伪造“未调用”。

### 8.3 超时与释放

Worker 分开限制 DNS、connect、TLS、headers、body idle 和 overall deadline。打开第一个网络资源前，以非敏感
dispatch owner 在 session registry 原子登记 session/connection/owned task；正常路径通过 `async with` 与
`finally` 关闭 MCP ClientSession、stream、HTTP client、transport、pool 和 socket。

overall timeout、取消或 shutdown 时进入 `asyncio.shield` 保护的 bounded cleanup，使用独立部署级 cleanup deadline
（默认 2 秒，不得由 tenant 放宽）。`terminate_on_close=true` 的协议终止只能复用原 pinned origin、IP、Host/SNI
和现有 transport；不得重新 DNS、redirect 或连接其他目标。graceful close 超过 cleanup deadline 后必须取消并有界
await owned task，强制关闭 transport/socket/pool，最后在不可取消的 `finally` 中原子注销 registry；清理失败只记
固定 code 和计数。MCP session ID 及所有响应 header 按 credential 处理，不进入日志、exception、registry snapshot
或验收证据。

Runtime 验收必须证明 deadline 后 active session/connection/owned task 均为 0，而不是只检查 API 状态。

## 9. `mcp.network_blocked` 事件

Exact payload 冻结为：

```json
{
  "schema_version": 1,
  "mcp_server_id": "mcps_...",
  "operation_id": null,
  "mcp_invocation_id": "mcpi_...",
  "phase": "call_preflight",
  "reason_code": "address_denied",
  "policy_revision": 2,
  "address_count": 1,
  "attempt_number": 1,
  "tool_call_count": 0,
  "cost_status": "not_applicable",
  "occurred_at": "2026-08-16T00:00:00Z"
}
```

operation ID 与 invocation ID 恰好一个非空。reason 只允许：`policy_disabled`、`address_denied`、
`mixed_dns_answers`、`port_denied`、`redirect_denied`。NXDOMAIN/SERVFAIL/timeout 是可用性失败，不进入 EVT-095；
permit/envelope invalid 是内部完整性失败，也不进入 EVT-095。Webhook 不披露具体 host/IP/CIDR。Allowed decision
不发 EVT-095；Invocation call_preflight block 固定 `tool_call_count=0`；sync/probe 不制造 `mcp.tool_called`。
durable call intent 一旦存在，只能按 EVT-094 语义收敛，数据库 trigger 禁止二者双发。

## 10. PAGE-115 MCP 安全配置页

PAGE-115 复用 PAGE-114 的 MCP 页面 owner、数据加载、request epoch 和清理逻辑，以新的“安全配置”tab 呈现，
不建立嵌套 card 或独立敏感 Storage。

页面包含：

- Server 选择、policy status/revision、destination 数量、circuit 状态、阻断总数和最近安全 reason；
- 仅 `mcp.network.manage` 可见的编辑按钮与原生 Dialog；
- 固定只读说明“仅已注册 Server Endpoint”，不显示或输入 hostname、port、IP 或 CIDR；
- timeout、failure threshold、open seconds 使用有界 number/stepper；
- status 使用 segmented control；保存时携带当前 revision，409 后刷新并保留未提交草稿提示。

只有 `mcp.read` 时页面为只读；无 `mcp.read` 时导航和直达 hash 都不渲染；无 network manage 时不能打开 Dialog，
但后端依旧逐请求拒绝 PATCH。loading、empty、error、stale、saving、conflict、success、permission revoked 状态完整。

页面、DOM、Storage、截图和浏览器验收 manifest 禁止包含完整 Endpoint path、resolved IP、部署 CIDR、Token、
arguments、result、Schema 正文或第三方错误。验收只使用保留示例域名和固定安全枚举。

## 11. Compose 与运行隔离

新增 `mcp-scheduler-worker` 和 `mcp-sandbox-worker`：

- Scheduler 挂载 SQLite/log 所需卷，但不配置 tenant Endpoint executor；只连接 core 与 sandbox-control。
- Sandbox 不挂载 SQLite、log 或宿主目录；`read_only: true`、`tmpfs /tmp`、`cap_drop: ALL`、
  `security_opt: no-new-privileges:true`、非 root user、pids/CPU/memory 限制。
- Sandbox 只连接 `mcp-sandbox-control` 与 `mcp-sandbox-egress`；数据库、Redis、MinIO、Control/Gateway 服务名均不在
  egress 网络上。内部端口不映射宿主机。
- Control Compose 明确关闭嵌入式 MCP scheduler，防止双 worker 抢 lease。
- Sandbox readiness 在 keyring、envelope auth、ACL compiler 和 resolver 未就绪时返回失败。
- Sandbox 使用专用 `infra/docker/mcp-sandbox.Dockerfile` 且不引用公共 `env_file`；镜像内容与注入变量必须通过
  构建测试证明不含 Control/Gateway/迁移/数据库客户端和 SandboxConfig 禁止项。

Docker bridge 不是 tenant ACL 的唯一实现；应用层 exact policy、DNS 全地址校验、IP pinning 和永久 deny 仍是
必要边界。部署应在生产环境再以主机 firewall/eBPF/egress gateway 限制 sandbox-egress；本仓库 Compose 提供
最小可运行隔离和明确配置接口，不虚构云防火墙已存在。

## 12. 配置合同

新增或调整的部署配置：

- `METAHUB_MCP_EXECUTOR_MODE=disabled|test_loopback|sandbox_http`（仅 Control/Scheduler AppConfig）
- `METAHUB_MCP_SANDBOX_BASE_URL`（Scheduler only，固定内部 URL）
- `METAHUB_MCP_SANDBOX_INTERNAL_TOKEN`
- `METAHUB_MCP_SANDBOX_ENVELOPE_SECRET`
- `METAHUB_MCP_SANDBOX_PERMIT_SECRET`
- `METAHUB_MCP_SANDBOX_RESPONSE_KEY_VERSION`
- `METAHUB_MCP_SANDBOX_RESPONSE_SECRET_CURRENT`
- `METAHUB_MCP_SANDBOX_RESPONSE_SECRET_PREVIOUS`（仅轮换窗口可选）
- `METAHUB_MCP_NETWORK_CONNECT_TIMEOUT_MAX_SECONDS`（三角色共享，1..10，默认 5）
- `METAHUB_MCP_NETWORK_PROBE_TIMEOUT_MAX_SECONDS`（三角色共享，1..30，默认 10）
- `METAHUB_MCP_NETWORK_CALL_TIMEOUT_MAX_SECONDS`（三角色共享，1..120，默认 30）
- `METAHUB_MCP_NETWORK_FAILURE_THRESHOLD_MAX`（三角色共享，1..10，默认 10）
- `METAHUB_MCP_NETWORK_OPEN_SECONDS_MAX`（三角色共享，10..3600，默认 3600）
- `METAHUB_MCP_SANDBOX_PRIVATE_CIDRS`（部署级，默认空）
- `METAHUB_MCP_SANDBOX_DENY_CIDRS`
- `METAHUB_MCP_SANDBOX_ALLOWED_PORTS`（默认 443）
- `METAHUB_MCP_SANDBOX_DENY_PORTS`
- `METAHUB_MCP_SANDBOX_DNS_TIMEOUT_SECONDS`（1..5，默认 2）
- `METAHUB_MCP_SANDBOX_TLS_TIMEOUT_SECONDS`（1..10，默认 5）
- `METAHUB_MCP_SANDBOX_HEADERS_TIMEOUT_SECONDS`（1..30，默认 10）
- `METAHUB_MCP_SANDBOX_BODY_IDLE_TIMEOUT_SECONDS`（1..30，默认 10）
- `METAHUB_MCP_SANDBOX_MAX_DNS_ANSWERS`（1..16，默认 8）
- `METAHUB_MCP_SANDBOX_MAX_ACTIVE_SESSIONS`（1..64）
- `METAHUB_MCP_SANDBOX_CLEANUP_TIMEOUT_SECONDS`（1..5，默认 2）

Scheduler secrets 在 `AppConfig`、Worker secrets 在独立 `SandboxConfig` 中均 `repr=False`，生产不得使用 dev
默认值。未知 `METAHUB_MCP_*` 继续失败，URL 不允许 userinfo/query/fragment，三角色的闭集组合必须按 3.3 验证。

Sandbox sync 的 durable job deadline 固定为 `probe + Worker response reserve 6 秒 + DB finalize reserve 6 秒`；
Scheduler 签名 dispatch deadline 只扣除 DB finalize reserve，得到 `probe + Worker response reserve`。Worker 从
收到请求即从签名 deadline 扣除完整固定 6 秒 response reserve，再启动 probe/call overall deadline；DNS、connect、
TLS、headers 和每次 body idle wait 都使用 `min(对应部署阶段上限, overall remaining)`，不能串联后重新获得完整预算。
即使部署把 cleanup 配为 1 秒，也不得把未使用的 reserve 扩给上游；预算不足时在网络前固定映射 `overall_timeout`。
tenant 的 connect/probe/call 值还分别
不得超过 shared connect/probe/call cap，failure threshold/open seconds 也不得超过 shared cap。readiness 要求
DNS/connect/TLS/headers/body-idle 上限均不大于 probe 与 call overall cap 中较小者，所有值 canonical 且 cleanup
deadline 独立；Scheduler operation lease 必须严格大于 shared probe cap + 固定 6 秒 response reserve + 固定 6 秒
finalize reserve，invocation
lease 必须严格大于 shared call cap。任何关系不成立均失败关闭。

Worker 的 action timeout 只覆盖 transport/session 进入、initialize、list/call 和结果完整物化；MCP context 退出使用
独立的 `cleanup_timeout_seconds`，不能继续占用 action 预算。结果尚未完整物化时发生超时仍固定映射
`overall_timeout`；结果已经完成 canonical 校验和内存物化后，单纯的协议关闭超时不得把已知结果降级为未知。
此时由 response reserve 内的 bounded cleanup 和外层 registry 对 pinned transport/socket/task 强制收口，取消信号仍
必须传播，非超时关闭错误不得被吞掉。

## 13. Migration 0076

### 13.1 Up

1. 验证最新 applied version 为 0075、RBAC revision 17 exact hash、0075 表/trigger/外键/完整性。升级手册要求先停
   MCP Scheduler 并排空工作；preflight 要求 operation 无 `queued|processing`、sync job 无 `pending|processing`、
   invocation 无 `approval_pending|queued|probing|calling`。任一非终态存在时以固定
   `prd083.mcp_drain_required` 原子失败，不创建默认 policy、不发 Audit/Webhook，也不修改任何既有行。
2. RBAC 提升 revision 18，只扩展仍匹配默认 revision 17 的 admin/security_admin/ops_admin。
3. 保留 rowid、全部既有行、复合 FK、索引和历史语义，重建 `mcp_server_operations`、
   `mcp_server_sync_jobs`、`mcp_invocations`、`mcp_invocation_attempts` 及对应 0075 transition/owner trigger，扩展
   sync job 的 `waiting_policy` 非终态、`mcp.network_blocked`/`mcp.sandbox_unavailable` error code 和 not-called
   network-blocked 终态；`deadline_at` 只允许 `waiting_policy` 为 NULL，waiting insert/transition trigger 保证同
   tenant/server 只有一个非终态 job，`waiting_policy -> pending` 必须原子写新 deadline/next_attempt 且 attempt 仍为
   0。不得只改 Python。既有 0075 行的 rowid 与全部列值必须逐行相等。
4. 创建八张 network 表、索引、复合 FK、owner/transition/append-only trigger，包括独立
   `mcp_network_event_outbox`；对象清单和 schema hash 固定测试。
5. 为既有 Server 建立 deny-all revision 1 aggregate/revision，为既有 Invocation 建立相同 disabled binding；创建
   Server/Invocation insert owner trigger。迁移生成的默认行必须可与真实 policy update 区分。
6. 保真重建 Audit action whitelist/owner trigger，加入 `mcp.network_policy_updated`。
7. 扩展 Webhook event type 合同，使 endpoint subscription 和 delivery 接受 `mcp.network_blocked`。
8. 终态 guard 校验 schema hash/对象数、租户复合 FK、RBAC、既有行双向 `EXCEPT`、完整性和外键违规为 0。

### 13.2 Down

Down 只有在以下均成立时才允许：

- 所有 policy 仍为 revision 1 disabled 默认规则，Invocation binding 只引用该默认 revision；
- decision、event、event outbox、call intent、circuit 业务事实均为空，且无 `waiting_policy` sync job；
- 无 `mcp.network_blocked` Webhook subscription/delivery，无 network policy Audit；
- revision 18 默认扩展角色可精确收缩，自定义矩阵没有引用新权限。

Guard 通过后删除默认 policy/binding 与八张 network 表，并保留 rowid/全部既有业务行精确还原四张 0075 状态表、
Audit/Webhook/RBAC revision 17；任一 PRD-083 业务事实存在时整个事务原子失败。迁移测试除空库外，还要构造
0075 Server/Operation/Job/Invocation/Attempt 的全部合法状态、错误与 owner 历史事实，并用 rowid 比较和双向
`EXCEPT` 证明 up/down 保真。空库门禁执行
`76/0076 -> 76/0076 -> 75/0075 -> 76/0076`，四阶段均要求 `integrity_check=ok`、foreign key violation 0。

## 14. 测试计划

### 14.1 Shared 与 ACL

- exact policy canonicalization/fingerprint、extra 字段、固定 egress mode 和超时/breaker 边界；
- IPv4/IPv6/mapped IPv6、global/private/CGNAT/reserved/documentation/metadata 分类；
- mixed DNS、无结果、超时、超量、绝对查询名和全地址 fail closed；
- pinned connect 保留 Host/SNI/证书且不二次 DNS；proxy/redirect/Unix socket/HTTP2 禁止；
- envelope/permit HMAC、expiry、action/lease/policy binding、replay cache；
- `mcp.network_blocked` exact payload/envelope 和敏感字段递归拒绝。

### 14.2 数据库

- fresh/repeat/down/re-up、0075 baseline drift、RBAC exact hash；
- 多个同 Server legacy pending/processing job 与各类非终态 invocation 均使 0076 原子拒绝；排空后迁移成功，
  migration 生成的 disabled policy 不产生 blocked decision/EVT-095；
- tenant owner、复合 FK、跨 tenant 伪造、append-only、policy CAS；
- default deny aggregate/revision、`waiting_policy -> pending` 原子恢复、Invocation binding、Server/Invocation
  insert/delete、decision/event/call intent owner graph；
- circuit closed/open/half-open lease CAS、过期 owner、不同 tenant/server/revision 隔离；
- Audit exact whitelist/Webhook source/outbox 原子性、down guard、八张表、rowid/历史保真。

### 14.3 API 与 Scheduler

- body 前双权限 RBAC、事务内撤权、跨 tenant 404、统一 request_id/no-store；
- exact PATCH/list DTO、policy active/disabled、registered Endpoint only、revision conflict、malformed/duplicate/oversize、
  部署上限冲突和敏感异常折叠；
- disabled 注册/refresh 等待且无 EVT-095，首次启用只恢复有效 job 并精确审计恢复数；
- blocked sync 不重试、blocked invocation 为零 call/零 cost/仅 EVT-095；
- permit 必须先于 call intent，call intent 后超时为 outcome_unknown/EVT-094；
- policy 漂移、lease 过期、迟到 Worker、重复 dispatch、worker crash/restart；
- circuit threshold/open/half-open/success close，tenant fairness 和并发上限；
- response current/previous key rotation、HKDF 域分离、nonce/AAD、篡改与重放；
- session/stream/socket/task 在 DNS/connect/TLS/header/body/overall timeout 后经 bounded shield cleanup 全部释放。

### 14.4 Console 与运行验收

- PAGE-115 admin/security/ops 可编辑，reader 只读，无 read 不可见；
- loading/empty/error/conflict/revoked/success、移动 390x844 和桌面 1280x720；
- 不横向溢出，不在 DOM/Storage/console/network log/截图写敏感字段；
- 真实 Runtime 启动 Control、独立 Scheduler、Sandbox Worker 和临时 MCP Server；
- 三角色配置矩阵失败关闭，Sandbox 专用镜像和 Compose 不携带数据库或 Control 配置；
- 证明一个部署批准的精确测试目标可 discovery/invoke，一个 DB/loopback/metadata/混合 DNS 目标被阻断；
- 允许路径使用专用静态测试子网和固定容器 IP，`/32` 或 `/128` 例外只注入验收进程环境；manifest 仅保存配置摘要，
  不保存 IP、CIDR 或 Endpoint，也不依赖公网和动态 Compose 地址；
- 超时后 session/connection/task 为 0，breaker 打开，调用/成本/失败事实可追踪；
- 进程、端口、临时数据库、临时凭据和 sidecar 全部清理。

## 15. 文档与验收证据

完成时更新 README、MCP API、Compose 部署、目录说明、PRD-083 测试记录和 Excel 对应行。证据只保存：

- commit/migration/version/hash、测试计数、HTTP status、固定 ID、revision、枚举和整数计数；
- 安全示例域名或不可逆摘要；
- 不保存 Provider Key、Token、Prompt、Response、Endpoint、resolved IP、CIDR、参数/结果正文、第三方错误正文。

敏感信息审计同时覆盖 tracked/untracked、patch、Runtime/Browser JSON、截图 OCR/DOM 摘要和进程命令行；禁止提交
`.env`、数据库、日志、密钥、Token、真实 Endpoint、Office 临时文件、cache、coverage 和构建产物。

## 16. 模块修改计划

- Shared：`metahub_shared.mcp.network`、events/webhook/error/i18n/RBAC/config。
- Control：network policy DTO/route/service、scheduler sandbox client、circuit/decision/event helper。
- Sandbox：独立 app/main、ACL resolver、pinned async transport、envelope/permit validator、session registry。
- Migration：`0076_mcp_sandbox_network_acl.sql/.down.sql`。
- Console：PAGE-115 state/render/bind/client/navigation/tests。
- Infra/docs/acceptance：Compose、env example、API、testing、runtime/browser harness、目录说明、Excel。

## 17. 完成门禁

编码前必须先满足：

- [x] 本设计第一轮自审完成，所有 P0/P1 已修正文档并记录。
- [x] 修正后由独立视角完成第二轮自审，所有 P0/P1 已修正文档并记录。
- [x] 无未决的 trust boundary、RBAC、permit、event 或 migration 决策。

最终交付必须满足：

- [ ] 0076 正反迁移和空库完整性通过。
- [ ] Shared/Control/Sandbox/Console 专项和必要回归通过。
- [ ] 真实允许、阻断、超时熔断、会话释放和事件投递验收通过。
- [ ] Runtime/Browser 证据通过敏感信息审计且资源清理完成。
- [ ] README、API、testing、directory-map、Excel 一致。
- [ ] `all_108_prds_complete=false`。
- [ ] 工作树只含 PRD-083 文件，提交并推送 `origin/main`。

## 18. 编码前审查方法

第一轮聚焦信任边界、permit/call intent、DNS/ACL、数据 owner、熔断、事件正交和 migration；第二轮聚焦
公开 API exact wire、RBAC、Worker 配置、超时释放、PAGE-115、隐私、兼容性和可验收性。每轮发现先修改正文，
再在下方记录问题与关闭依据；两轮结束前不得编码。

## 19. 编码前第一轮自审：已完成并修正

2026-08-16 按信任边界、permit/call intent、DNS/ACL、tenant owner、熔断、事件正交和迁移执行第一轮复核，
发现并关闭：

1. **P0：初稿让 Control 一边承诺不解密 Endpoint，一边在 PATCH 事务中解密并比较 origin。** 已删除租户可提交
   的第二套 destination；policy 固定 `registered_endpoint_only`，Control 不解密 Endpoint，Worker 执行时才派生
   exact origin，并绑定既有 endpoint HMAC。
2. **P0：初稿把 allowlist hostname 明文存入 policy，扩大了 Endpoint 明文面。** 已删除 hostname/IP/CIDR/port
   的公开输入、存储和读取；PAGE-115 只显示 derived destination count 和 masked 既有 Server hint。
3. **P0：只有 current policy 行，无法验证旧 Invocation 冻结 revision。** 已拆为 current aggregate + immutable
   revisions，并增加 Invocation 创建事务中的 append-only binding；旧 revision 由复合 FK 可验证。
4. **P0：初稿只新增 network 表，但 0075 的 operation/job/invocation/attempt CHECK 与 trigger 均拒绝新错误码，
   blocked 状态也只接受 DLP。** 已要求 0076/Down 保留 rowid 和全部历史事实重建四张状态表及 trigger，测试既有
   0075 业务行双向 `EXCEPT`，不是只测空库。
5. **P0：permit 未完整证明对应已提交的 durable call intent。** 现要求 Scheduler 预生成 call-intent ID，
   authorize permit 全量绑定 owner/revision/HMAC/lease/deadline；同一事务写 network intent、0075 calling 和 call
   attempt；Worker `/call` 只接受该持久 ID。
6. **P1：decision 与 call intent、half-open circuit owner graph 不完整。** 已增加 allowed call-preflight decision
   的复合 FK、permit/lease 全量一致约束和 half-open owner type/id；迟到结果必须命中 job/invocation lease、policy
   revision 与 circuit revision 三重 CAS。
7. **P1：初稿虚构标准 `getaddrinfo` 可验收完整 CNAME 深度，并把 NXDOMAIN 当安全违规。** 已冻结可实现的绝对名
   单次解析/全地址集合/答案和 deadline 门禁；NXDOMAIN/SERVFAIL/timeout 改为可用性失败并计入 breaker，只有
   mixed/denied address 等确定性 ACL 拒绝产生 EVT-095。
8. **P1：EVT-095 示例与 reason 枚举不一致，`permit_invalid` 还会在 call intent 后伪造零调用。** 已统一固定枚举，
   删除 permit invalid；allowed decision 不发事件，call intent 后只按 EVT-094 收敛，并用 trigger 禁止双发。
9. **P1：内部请求有 HMAC，但 Schema/result 响应的保密、完整性和重放边界未冻结。** 已增加 exact response
   envelope、独立 response HMAC、调用级 AEAD 和 owner/dispatch/lease/policy 绑定；验签失败按 intent 前后分别映射。

第一轮无遗留 P0/P1，正文、数据模型、API、迁移和测试计划已同步修正，并已由第二轮复核重新检查。

## 20. 编码前第二轮自审：已完成并修正

2026-08-16 从公开 API exact wire、RBAC、Worker 配置、超时释放、事件投递、隐私、迁移兼容和可验收性独立复核，
发现并关闭：

1. **P0：Sandbox 仍复用通用 AppConfig/镜像，三角色 mode 与 secret 边界不闭合。** 已冻结 Control、Scheduler、
   Sandbox exact 配置矩阵；Sandbox 改用独立最小 `SandboxConfig` 和专用 Dockerfile，不接收数据库、Control 或
   公共 env_file。
2. **P0：EVT-095 只有 source，没有可恢复的首次投递 owner。** 已增加第八张
   `mcp_network_event_outbox`，冻结 tenant-fair claim/lease、snapshot、重试、过期恢复、dead-letter 和迟到 CAS。
3. **P1：默认 disabled policy 与注册立即 sync 形成竞态并伪造违规。** 已增加 `waiting_policy` 非执行态；disabled
   注册/refresh 不调用网络、不发 EVT-095，首次启用事务原子恢复有效 job 并审计恢复数。
4. **P1：PATCH/list DTO、409 与 malformed/oversize 错误未 exact 冻结。** 已冻结完整 request/HTTP 200 response、
   list 嵌套摘要、字段类型/空值/时间、`mcp.state_conflict` 409、`mcp.request_invalid` 与 422 分界。
5. **P1：公开 PATCH 与 PAGE-115 的读取/管理权限不一致。** 路由 dependency 和事务内复核现均要求
   `mcp.read AND mcp.network.manage`，自定义角色只有 manage 仍被拒绝。
6. **P1：普通 finally 不能证明取消后 session 真正归零。** 已冻结独立 cleanup deadline、shielded bounded cleanup、
   pinned-origin termination、强制 transport/socket/task 关闭与 registry 原子注销；session ID/header 按 credential
   处理。
7. **P1：内部响应 key rotation 与 Audit 仍不是 exact contract。** 已冻结 response key version、current/previous
   secret、HKDF-SHA256 双域、12-byte nonce/AAD/rotation；Audit 的 action/object/before/after 安全字段及 Python/SQL/
   事务一致性也已固定。
8. **P0：`waiting_policy` 沿用创建时 deadline 会在策略启用前过期并永久卡住 registration。** waiting job 现固定
   deadline/next attempt/lease 为空、attempt 为 0；首次启用时才从当前时间生成执行 deadline。所有 sync 写事务及
   trigger 同时保证每 Server 最多一个非终态 job，消除多 job 并发恢复。
9. **P1：Scheduler 持 permit secret，且 response previous key 错配给 Worker。** permit secret 现只存在于 Worker；
   Scheduler 只持 permit digest。response 轮换固定为 Scheduler current/previous、Worker current，并冻结滚动顺序；
   envelope/permit 不做隐式双版本，协调重启后由 boot ID 作废旧 permit。
10. **P1：最小 SandboxConfig 没有其声称执行的阶段/overall deadline 上限。** 已补 DNS/connect/TLS/headers/body
    idle、probe/call overall 与 cleanup 的 exact 配置、剩余预算算法、组合约束和 readiness 失败条件。
11. **P1：PAGE-115 安全摘要的历史聚合范围不确定。** 已固定所有 count/last/circuit 只取 current policy revision；
    新 revision 从 `not_initialized/0/null/null` 开始，历史事实保留但不混入当前 DTO。
12. **P1：Control 不持有 Worker 上限，却承诺超限 PATCH 返回 422。** 已增加三角色共享、非敏感、带 canonical
    fingerprint 的 `McpNetworkPolicyCaps`；Control 校验 422，Scheduler readiness/envelope 与 Worker 逐请求均校验
    同一指纹，并纳入 breaker caps。
13. **P1：合法 0075 库可能有多个非终态 job，与新单 job 不变量冲突。** 0076 preflight 现要求停 Scheduler 并排空
    全部 MCP 非终态 operation/job/invocation；否则以固定 migration error 原子失败，不生成默认策略或事件。终态
    0075 事实仍保持 rowid 和全部列值不变。
14. **P2：waiting job 激活时的 finalize reserve 未冻结。** 明确分离 Shared 固定 6 秒 Worker response reserve 与既有
    固定 6 秒 DB finalize reserve，并要求 operation lease 严格覆盖 shared probe cap + 两段 reserve，配置和边界
    测试同步验证。

第二轮修订后再次逐项交叉检查，未遗留 P0/P1；编码门禁通过。后续实现不得弱化上述合同。

## 21. 实现后第一轮深审：已完成并修正

2026-08-16 对 Scheduler 的授权时序、permit/call intent、熔断 CAS、租户 owner 与失败终态执行实现后复核，
发现并关闭：

1. **P1：authorize 与 call 之间的权限漂移可能绕过实时 RBAC。** call-intent 事务现在重新校验用户、角色、
   Tool、Server、Credential、Schema、Tool Policy 与 Network Policy；撤销 grant 后固定收敛为
   `mcp.authorization_revoked`，`execution_status=not_called`、`cost_status=not_applicable`，且不写
   call-preflight decision、call intent 或 call attempt。
2. **P1：permit 对 durable intent、Tool、Credential、Schema 和 Tool Policy 的绑定不完整。** Scheduler 预生成
   network decision/intent ID，authorize-call 与 call 全量绑定 owner、revision、fingerprint、HMAC、permit digest
   和冻结 deadline；Tool Policy fingerprint 取 Invocation 冻结值，不读取不存在或可漂移的 Tool 列。
3. **P1：authorize 异常路径复用已关闭 probe fence，可能把 Invocation 留在 `probing`。** 异常收敛改用当前
   Invocation owner/fence，并由竞态测试证明终态可恢复。
4. **P1：`auth_type=none` 仍可能携带 Credential 标识或密文摘要。** 无认证路径现固定 Credential ID 为空，
   permit 仅绑定空值摘要；bearer 路径继续要求完整 owner 与 ciphertext digest。
5. **P1：closed circuit 的迟到 fence 可能覆盖新 revision 聚合。** 非 half-open 完成只聚合当前 circuit revision；
   half-open 仍要求唯一 owner，过期 owner 通过两次 CAS 接管。并发 stale-fence、grant race 和恢复测试均通过。

修正后重跑完整 PRD-083 Scheduler 文件及相邻 PRD-082 MCP Control/Scheduler 回归，无未关闭 P0/P1。

## 22. 实现后第二轮深审：已完成并修正

2026-08-16 从 Worker 最小权限、DNS/transport、固定错误分类、加密响应、资源释放和共享 permit 合同再次复核，
发现并关闭：

1. **P2：DNS 超量拒绝暴露了超过部署上限的实际答案计数。** 对外 address count 现截断到已配置上限，
   仍按 `address_denied` 失败关闭。
2. **P1：SandboxConfig 未拒绝跨角色的未知 `METAHUB_*`。** Worker 现在只接受显式 allowlist，数据库、
   Control、Gateway、Scheduler 或公共 env_file 配置均使启动失败。
3. **P1：registry cleanup 的 graceful task 生命周期不完整。** cleanup 显式创建、取消并等待 graceful task，
   随后执行有界 force close 和原子注销；取消、超时与 ExceptionGroup 路径均验证连接、task、session 归零。
4. **P1：网络异常分类可能携带异常文本或把阶段混为一类。** Worker 只产生固定
   `dns_unavailable`、`connect_unavailable`、`tls_unavailable`、`headers_timeout`、`body_timeout`、
   `overall_timeout`；分类经加密响应传给 Scheduler，持久化只保存固定枚举，不保存 Endpoint、Header、正文或
   第三方错误文本。ASGI 异常继续返回统一 `mcp.sandbox_unavailable` 和 request_id。
5. **P1：permit 解析只检查 required subset，连接目标字段未纳入精确合同。** permit 现要求唯一精确字段集，
   明确包含 boot ID、`scheme/hostname/port/pinned_ip/address_count`；只允许 HTTPS、1..65535 端口、规范 IP、
   1..16 地址计数，并拒绝未知字段、控制字符、IPv4-mapped IPv6 与所有目标篡改。
6. **P1：成功 permit 没有 migrated DB 的 call-intent commit 覆盖。** 真实 Runtime 发现 sandbox 与 legacy
   `probing -> calling` 都把 Invocation `request_bytes` 重写为参数长度，与 0075/0076 trigger 冲突并把合法调用
   收敛为 `mcp.unavailable`。两条转换现均保持 Invocation request_bytes 不变；真实 Sandbox call HTTP 字节只在
   terminal finalize 写入，call attempt 仍保存非空请求字节。新增 migrated success 测试逐项验证 calling、allowed
   decision、permit-bound durable intent、processing call attempt、integrity 与 FK。
7. **P1：Worker 把签名 deadline 全部交给上游动作。** Shared 现固定 6 秒 response reserve；durable job deadline
   为 probe + Worker response reserve + DB finalize reserve，Scheduler envelope deadline 为 probe + Worker
   response reserve。Worker 在 DNS/网络前固定扣除完整 reserve，不把未使用 cleanup 时间扩给上游。
8. **P1：MCP context close 超时覆盖已物化结果。** initialize/list/call 和结果物化受 action timeout 约束，context
   exit 单独使用 deployment cleanup deadline。裸 TimeoutError 和纯 timeout ExceptionGroup 不覆盖已知结果；
   混合异常、取消和结果完成前 timeout 仍失败关闭，pinned transport 继续由 registry 强制收口。
9. **P1：EVT-095 pending Webhook 恢复缺少构造期 request_id。** `mcp.network_blocked` 公开信封不携带 request_id；
   恢复现只从同 tenant 不可变 `mcp_network_events` 取回 source request 身份，owner、event 或信封漂移均在 HTTP
   前固定失败。真实独立 Scheduler 已完成 source outbox 重试和签名 receiver 验收。
10. **P2：Runtime 最终 readiness 读取不存在的 `active_tasks`。** Runner 已改为 Worker 正式合同字段
    `owned_tasks`，并由源码合同固定 session/connection/task 三项归零，避免把字段漂移冒充资源泄漏。

修正后 Shared network `9/9`、Worker `28/28`、PRD-083 Scheduler `20/20`、0076/Compose `17/17`、
相邻 MCP Control/Scheduler `35/35`、Console 受影响专项 `33/33`、验收合同 `62/62` 与 lint/Ruff 均通过；
未发现未关闭 P0/P1。最终 Runtime、Browser、文档、Excel 和提交前审计结果记录在测试文档中。
