# PRD-082 MCP 工具注册与权限设计

状态：**已完成**（2026-08-16）。本文冻结 PRD-082 的权威范围、MCP transport、工具 Schema、
tenant-qualified 数据模型、后端 RBAC、参数 DLP、返回脱敏、高危审批、调用状态机、事件和 PAGE-114；
第 19、20 节保存两轮编码前自审，第 22、23 节保存两轮实现后自审及修正。本条不启动 PRD-083；
总体状态继续保持 `all_108_prds_complete=false`。

## 1. 权威范围与交付边界

### 1.1 交叉确认

- Excel `01_PRD总表!A83:R83` 与 `03_M2-M3扩展交付!A19:R19` 一致定义：
  `PRD-082 | M2-企业增强 | P2 | 扩展层 | MCP治理基础 | MCP工具注册与权限`。
- 用户是管理员/开发者；故事是企业未来的 Agent 工具调用也必须经过权限和审计。
- 实现范围固定为 MCP Server 注册、工具 Schema、权限、参数 DLP、返回脱敏和调用日志。
- PAGE-114 为 MCP 工具页；Excel 明示 API 是 `POST /mcp/servers` 与 `GET /mcp/tools`。
- OBJ-111..113 是 tenant-qualified `McpServer`、`McpTool`、`McpInvocation`。
- EVT-094 是 `mcp.tool_called`；TC-0163/0164 要证明主流程、Schema、无权限、异常输入、
  依赖故障、统一错误、敏感信息保护和状态追踪。
- 验收明确要求每次工具调用经过权限，高危工具可以配置审批。只交付 Server/Tool CRUD 而没有
  受控调用、DLP、脱敏和 Invocation 事实不能满足本条。
- PRD-082 设计启动前，README、PRD-081 设计/测试和目录说明均连续完成到 PRD-081，且仓库尚无
  MCP 生产模块或 PRD-082 设计文档，因此本条不是按编号猜测出的重复实现。
- PRD-082 启动基线的最高迁移为 `74/0074`、RBAC catalog revision 为 16；本条迁移固定为 `0075`，权限目录
  提升到 revision 17。

### 1.2 架构文档约束

- 系统架构设计把 MCP 治理放在扩展层，触发条件是客户把 Agent 工具调用纳入企业生产流程。
- MCP 不是 M0/M1 主链路，本条不能改变 OpenAI-compatible Chat、Provider 路由或默认上游目录。
- 默认上游继续是智谱、通义、月之暗面、文心、火山、混元、DeepSeek 官方/云服务以及客户本地
  OpenAI-compatible Endpoint。OpenRouter、硅基流动仍只是竞品与迁移兼容对象。

### 1.3 明确不做

- 不实现 Agent 计划、记忆、多工具编排、循环执行、模型自动选工具或完整 Agent 工作流。
- 不实现第三方 MCP 沙箱子网、网络 ACL、DNS/IP 重绑定防护、目标 CIDR 白名单、内网横向阻断、熔断器或
  `mcp.network_blocked`；这些属于下一条 PRD-083。由于这些边界尚未存在，PRD-082 的生产配置固定禁止
  Control 进程和普通 worker 直接连接 tenant 提交的 Endpoint。本条只交付治理控制面、持久同步意图、
  专用 internal executor 的 authorize/finalize 合同，以及明确 test-only 的字面量 loopback SDK adapter；
  生产第三方出网由 PRD-083 接管后才能开启。
- 不实现 MCP resources、prompts、sampling、roots、stdio/SSE 旧 transport、OAuth、客户端证书、
  自定义 Header 或二进制/图片/音频结果。PRD-082 只支持 Streamable HTTP、可选 Bearer Token、
  `tools/list` 和 `tools/call` 的文本/结构化 JSON 结果。
- 不把动态工具名注册成全局 RBAC permission code，不让第三方 Schema 或 description 修改固定权限目录。
- 不承诺网络 exactly-once。调用前持久化 call intent；结果未知时固定为 `outcome_unknown` 且不自动重试，
  以避免有副作用工具被重复执行。
- 不保存或输出原始 MCP 参数、原始返回、Bearer Token、Endpoint URL、DLP 命中片段、Schema description
  或第三方错误正文到日志、Audit、Webhook、DOM、截图或验收证据。

## 2. 现状约束与关键决定

1. 当前输入 DLP 只扫描 Chat user message，输出扫描器只理解 OpenAI response/tool_calls；两者不能
   冒充 MCP 参数与结果治理。PRD-082 在 Shared/Control 新增有界 JSON 投影和规则执行器，但复用安全
   regex 编译、租户规则、优先级和失败关闭原则。
2. 当前审批目标注册表只有预算和会话风险，且明确禁止在审批事务内访问网络。PRD-082 扩展固定
   `mcp_tool_invocation` target；批准事务只把 Invocation 原子推进到 queued，真实 MCP 网络调用由提交后的
   MCP scheduler 执行。
3. Excel 只列出注册和工具查询的简写 API，但 McpInvocation、返回脱敏和验收要求必然需要 invoke、
   invocation 查询及策略更新入口。第 9 节增加最小必要 API，并保持 Excel 两个接口原样可用。
4. JSON Schema 是标准引擎问题，不手写不完整 validator。运行时增加成熟 `jsonschema` 库并固定
   Draft 2020-12 profile；MCP transport 使用官方 Python SDK v1 公共 API，不手写 JSON-RPC parser。
5. 新发现的工具默认 `review_required`，不能立即调用。`mcp.policy.manage` 持有者必须显式冻结风险、
   审批、附加 permission 和返回脱敏路径后才可 active，避免 Server 注册者抢在安全复核前执行新工具。
6. 所有调用异步持久化。参数在数据库中只保存 AES-GCM 密文和不可逆摘要；原始远端结果只在 worker
   内存存在，先脱敏再加密保存。PAGE-114 只展示安全摘要，不展示参数或结果正文。
7. PRD-083 尚未提供网络沙箱。部署级 exact origin 仍不能阻止 DNS 解析到 loopback、RFC1918、link-local、
   metadata 地址或 DNS rebinding，因此不能作为“已关闭”的 SSRF 边界。PRD-082 生产网络执行固定
   `disabled`：公开 API 只持久化 Server、credential revision、operation 与 sync job，不从 Control 出网。
   独立 internal executor 必须先用服务身份领取一次性 authorize envelope，再用同一 owner/version 完成
   finalize；本条只在 dev/test 显式开关下提供仅接受字面量 `127.0.0.1`/`[::1]` 的 loopback SDK adapter。
   URL 仍禁 userinfo、query、fragment、重定向和环境代理，但这些只是输入/资源门禁，不能冒充 PRD-083。
8. 注册、刷新和凭据验证都是持久 operation。API 先提交 Server/候选 credential 与
   `mcp_server_sync_jobs` 再返回 202；网络或 executor 不可用、发现失败和凭据失败也必须形成可查询的固定事实，
   不能因同步出网失败而丢失请求。

## 3. 后端权限、身份与租户隔离

### 3.1 RBAC revision 17

新增五个固定权限：

| permission | 风险 | 默认角色 | 能力 |
|---|---|---|---|
| `mcp.read` | read | admin/developer/security_admin/ops_admin | 读取 Server、Tool 与安全 Invocation 摘要 |
| `mcp.server.manage` | critical | admin/developer/ops_admin | 注册、停用、轮换 Server 凭据和刷新工具 |
| `mcp.policy.manage` | critical | admin/security_admin | 审核工具 Schema，配置风险、审批、附加权限和脱敏路径 |
| `mcp.invoke` | write | admin/developer | 发起已审核 active 工具调用并读取本人脱敏结果 |
| `mcp.approve` | critical | admin/security_admin | 作为 MCP 目标域权限审批高危 Invocation |

- admin 继续拥有全目录。revision-16 精确默认矩阵只对上述角色按表扩展；租户自定义矩阵不自动补权。
- 每工具策略还保存 `required_permission_codes`，只能是 0..8 个当前固定目录 code。调用者必须同时拥有
  `mcp.invoke` 和全部附加权限；空集合不取消基础 `mcp.invoke`。
- 高危审批者必须同时拥有 `approval.decide` 和 `mcp.approve`。审批读取要求 `approval.read` 与
  `mcp.read`，不能只靠审批页面可见性决定权限。
- 工具策略不能要求 `mcp.approve`、`approval.decide` 或未知动态 code 作为调用权限，防止审批权被
  混同为实际执行权。

### 3.2 可信身份与 tenant owner

- 公开 API 不接受 `tenant_id`、actor、权限快照、状态、revision、lease 或事件字段。
- tenant/user/session/permission revision 只从后端 Session 恢复；每个写事务重新读取当前用户、角色和
  tenant 权限矩阵。前端隐藏按钮仅改善交互。
- Server/Tool/Invocation/Attempt/Event/Outbox/Scheduler 全部含 `tenant_id`，复合外键、唯一约束、
  owner trigger 和每条 SQL 的 tenant 首条件共同隔离。
- 跨租户或当前权限不可见的 Server/Tool/Invocation 统一返回同形 404，不暴露对象是否存在。
- worker 在真实网络前按 Invocation 的原始 actor 重新校验 active user、`mcp.invoke` 和冻结附加权限；
  动态撤权、禁用用户或角色矩阵损坏必须零 `tools/call` 并形成固定失败事实。

## 4. MCP transport 与工具 Schema

### 4.1 executor 边界、官方 SDK 与 transport profile

- production `executor_mode=disabled`；Control/scheduler 不解析 DNS、不建立 tenant Endpoint socket，也不把
  Bearer 交给通用 HTTP client。公开注册/refresh/invoke 只产生持久 operation/job/invocation。
- `McpExecutor.authorize` 只接受 tenant/job/invocation、冻结 Server/Tool/credential revision、一次性 lease
  owner/token 和 deadline；返回的执行材料只在受信 executor 内存存在。`finalize` 必须比较完整 owner、revision、
  intent 与 call-started CAS，迟到或重复 finalize 不能覆盖终态。生产实现留给 PRD-083，不能通过环境变量 URL
  把 internal executor 指向任意 tenant Endpoint。
- dev/test 的明确 loopback adapter 使用官方 `mcp` Python SDK v1 `ClientSession` 与 Streamable HTTP client，
  执行 initialize、`tools/list` 和 `tools/call`；依赖声明固定 `<2.0`，避免 major wire 漂移。该 adapter 在
  production profile 启动失败，且只接受字面量 `127.0.0.1`/`[::1]`，不接受 hostname 或 DNS。
- 注册只接受 absolute URL。生产只允许存储 HTTPS Endpoint，仍不出网；不支持 wildcard、suffix、客户端提交
  allowlist 或 DNS/CIDR policy。dev/test 只有部署开关开启时接受字面量 loopback HTTP。
- URL 禁止 userinfo、query、fragment、非规范端口、控制字符和超过 2048 字符；Bearer Token 独立字段传入，
  不能嵌入 URL。
- HTTP client 固定 `trust_env=False`、`follow_redirects=False`，不读取响应 cookie，不记录请求/响应 Header
  和 body。官方 SDK adapter 必须注入可测试的 bounded streaming transport，在完整 materialize 前按实际
  bytes 截止；不能先让 SDK 无界读入再检查长度。一个 operation 使用固定 10 秒 probe deadline、30 秒
  call 安全 deadline、1 MiB 总入站上限。这些只是资源硬门禁，不提供 PRD-083 的 ACL 或熔断策略。
- 一个 Server 最多 100 个工具；`tools/list` 分页总页数最多 10，重复 cursor、重复工具名、超限、
  非 MCP 响应或 SDK 不支持的 protocol version 均失败关闭。
- 注册和 refresh 先在短 `BEGIN IMMEDIATE` 事务持久化 operation、Server/candidate credential 与 sync job，
  然后由允许的 executor 在事务外做 initialize/tools-list 只读发现。成功 finalize 才冻结 Tool 事实；失败也
  原子完成 operation/job 并保留 Server 安全摘要。任何数据库事务内都不得访问网络。

### 4.2 Schema profile

- `inputSchema` 必须是 UTF-8 JSON object；可选 `outputSchema` 同 profile。每份原始 canonical JSON 最多
  65,536 bytes，深度最多 12、节点最多 1024、properties 最多 256；整个 Server 的原始加 effective Schema
  总量最多 1 MiB。
- 使用 `Draft202012Validator.check_schema` 和实际 validator；拒绝远程/本地 `$ref`、`$dynamicRef`、
  `$recursiveRef`、未知 `$schema`、非有限数、重复 key 和超限 regex。
- 根必须是 `type=object`。第三方原始 Schema 不因缺少 `additionalProperties`、`maxItems` 或 `maxLength` 而
  直接拒绝；平台保存不可变原始 canonical Schema，并确定性生成 effective Schema：所有 object 强制
  `additionalProperties=false`，缺失或过大的 array/string/object 上限分别注入/收紧到部署硬上限。调用参数
  只按 effective input Schema 验证；有 outputSchema 时，structured result 先按 effective output Schema 验证。
  `pattern` 必须先通过 Shared safe-regex 编译。
- 不执行远程 format resolver；只允许离线固定 format 白名单。未知 format、custom keyword 或可执行扩展拒绝。
- Tool name 必须为 1..128 个受控字符且同 Server 唯一；description 只保存经过字符/长度门禁的最多
  500 字符展示文本，永不进入日志、事件或证据。
- canonical Schema 使用 UTF-8、key 排序、无多余空白、禁止 NaN/Infinity；原始/effective input/output 各自
  计算 SHA-256。validator 另设 keyword 数、combinator 分支数、enum 总项/单项 bytes、regex 长度和 instance
  evaluation step budget，超限固定拒绝，防止合法 Schema 造成 CPU/内存拒绝服务。Tool policy revision 与
  四类 Schema hash 一起冻结到 Invocation 和 Approval fingerprint。

### 4.3 Schema 刷新

- refresh 按 remote tool name 对账。同 name 且 Schema/annotations 完全相同只更新时间，不推进 policy。
- 新工具创建为 `review_required`；Schema 或安全 annotations 改变时推进 `schema_revision` 和
  `policy_revision`、清除 active 状态并回到 `review_required`；已有 Invocation 继续保留旧冻结事实，
  但尚未出网者因当前 revision 漂移失败关闭。
- 远端删除的工具改为 disabled，不物理删除；Server/Tool 历史、Invocation 和事件永远保留。

## 5. 0075 数据模型

### 5.1 `mcp_servers`

| 字段 | 合同 |
|---|---|
| `mcp_server_id` | `mcps_` + 32 小写 hex，tenant-qualified 主 owner |
| `tenant_id` | 必填复合 owner |
| `name` | 1..80 字符，只在授权 UI/API 中展示 |
| `transport` | 固定 `streamable_http` |
| `endpoint_ciphertext` | AES-GCM 密文，AAD 绑定 tenant/server/kind/version |
| `endpoint_hmac/endpoint_hmac_key_version` | tenant 域不可逆排重摘要及 key version，不进入公开响应 |
| `active_credential_revision` | 指向已 probe 成功的 credential revision；注册未完成时可为空 |
| `protocol_version` | SDK initialize 协商出的受支持版本 |
| `capabilities_sha256` | canonical capabilities 摘要 |
| `status` | `pending_sync/active/sync_failed/disabled` |
| `state_revision` | CAS 修订，起始 1 |
| `created_by/updated_by` | 同租户 user 外键 |
| `created_at/updated_at` | UTC 秒 |

同 tenant 的 `endpoint_hmac` 唯一。注册/更新用 current/previous key candidates 查重，成功写事务把仍使用
previous 的 owner 受控换成 current；轮换不能让同一 Endpoint 重复注册。Bearer 不在 Server 主表：
`mcp_server_credentials` 保存 tenant/server/revision、auth type、独立密文/key version、credential HMAC/version、
`candidate/active/rejected`、probe operation 与时间。轮换先插入 candidate 并 probe；成功后才在同一 CAS 事务
原子切换 active revision，失败继续使用旧 active credential，且失败 candidate 不可被调用。Endpoint、Token 和 capabilities
原文不进入 Audit、事件、日志或 DOM；授权列表只返回 scheme、masked host 与路径存在性形成的安全 hint。

### 5.2 `mcp_tools`

| 字段 | 合同 |
|---|---|
| `mcp_tool_id` | `mcpt_` + 32 小写 hex |
| `tenant_id/mcp_server_id` | tenant-qualified Server owner |
| `remote_name/title/description` | 受控展示字段；同 Server name 唯一 |
| `input_schema_original_json/sha256` | 第三方 canonical input Schema 与摘要 |
| `input_schema_effective_json/sha256` | 平台确定性加固 input Schema 与摘要，调用验证只使用它 |
| `output_schema_original/effective_json/sha256` | 可选第三方/加固 output Schema 与各自摘要 |
| `annotations_json/annotations_sha256` | 只含 MCP 固定布尔/枚举 annotation |
| `schema_revision` | Schema 变化推进，起始 1 |
| `policy_revision` | 每次治理策略变更推进，起始 1 |
| `risk_level` | `standard/high` |
| `approval_required` | 0/1；high 必须为 1，standard 也可配置为 1 |
| `approval_expires_seconds` | 300..86400，默认 3600 |
| `required_permission_codes_json` | canonical 排序的 0..8 个固定 code |
| `result_redaction_pointers_json` | canonical 排序的 0..32 个 RFC 6901 exact pointer |
| `status` | `review_required/active/disabled` |
| `created_by/updated_by/created_at/updated_at` | 同租户 actor 与 UTC 秒 |

Tool 的 Schema/name/server owner 创建后不能直接改写；只有受控 refresh 可按 CAS 推进 Schema。策略 PATCH
只能改治理字段，不能替换第三方 Schema。

`mcp_tool_role_grants` 与 `mcp_tool_user_grants` 都以 tenant/tool 为 owner，并分别复合外键到当前 tenant 的
role/user；策略激活时至少有一个显式 grantee。调用者除了 `mcp.invoke` 和所有附加 permission 外，还必须命中
本人或当前 role grant。grant 只能由 `mcp.policy.manage` 在 policy CAS 事务内修改，不能靠前端可见性代替。

### 5.3 `mcp_invocations`

| 字段组 | 合同 |
|---|---|
| owner | invocation ID、tenant/server/tool、requested_by user |
| source | 服务端 request/trace、幂等 key HMAC/version、intent HMAC |
| frozen policy | server/credential/schema/policy revision 与 fingerprints、四类 Schema hash、risk、approval、附加 permission/grant 摘要、redaction pointers |
| secret input | arguments ciphertext、encryption version、canonical byte count、tenant/domain HMAC 与 key version；无明文列或裸 SHA-256 |
| approval | nullable approval_task_id、approval deadline |
| state | `approval_pending/queued/probing/calling/succeeded/failed/blocked/rejected/outcome_unknown` |
| input DLP | 独立 status/match count/rule ID、ruleset HMAC/version 与 scanned_at |
| output DLP | 独立 status/match count/rule ID、ruleset HMAC/version 与 scanned_at；未调用时为空 |
| result | 只保存脱敏后 ciphertext、tenant/domain HMAC 与 key version、bytes、redaction count；原始结果无列 |
| tracking | probe/call attempt count、request/response bytes、duration ms、`execution_status=not_called/called/unknown` |
| usage/cost | `invocation_count=1`；not_called 为 `tool_call_count=0/cost_status=not_applicable`，called 为 `1/not_reported`，unknown 为 `NULL/unknown`，cost 永远 NULL |
| failure | 固定 error code 或 NULL，不保存第三方 message/body |
| lease | generation、owner、token、expiry、next attempt |
| time | created/queued/started/call_started/completed/updated UTC 秒 |

- `(tenant_id,requested_by,idempotency_key_hmac)` 唯一；同 key 不同 intent 返回 409，同 intent 恢复原事实。
- arguments/result 摘要只使用 tenant/domain HMAC 与版本化 current/previous key，不公开、不写日志或 Webhook，
  避免裸 SHA-256 成为低熵输入预言机。
- succeeded 必须有脱敏结果和 completed time；所有终态清空 lease；calling 后 lease 过期只能进入
  outcome_unknown，不能重新 queued。
- `mcp_tools` 提供 `UNIQUE(tenant_id,mcp_server_id,mcp_tool_id)`，Invocation 使用这三列单一复合外键；
  不能用两个独立外键让同 tenant 的 Server A 与 Server B Tool 被错误拼接。

### 5.4 `mcp_invocation_attempts`

每次 probe/call attempt 保存 tenant/invocation、attempt number、状态、lease generation、固定错误、
request/response byte count、started/call-started/completed time 和 duration。表中没有 URL、Header、参数、
Schema、结果或第三方错误。attempt append-only，只有当前 lease owner 可完成自己的非终态行。

### 5.5 Approval、Server operation、事件与 scheduler 辅助表

- 受控重建 `approval_tasks`/`approval_writebacks` 及相关 owner trigger，使 target/source 支持
  `mcp_tool_invocation/mcp`、action `invoke`、risk `high`、required permission `mcp.approve`，writeback
  adapter `mcp_invocation_enqueue`。既有预算/会话审批 rowid、字段和状态保真。
- 高危路径由服务端预生成 `approval_task_id`；Invocation 对 `(tenant_id,approval_task_id)` 使用
  `DEFERRABLE INITIALLY DEFERRED` 复合外键，并由双向 owner trigger 强制 Task target/version/fingerprint 与
  Invocation 一致。Task、Invocation 和 created decision/source 在同一事务提交，不使用 eventual approval
  outbox，因而不存在已提交 pending Invocation 没有 Task 的窗口。
- `mcp_server_operations` 保存 register/refresh/rotate/disable 的 tenant、actor、Idempotency HMAC/version、
  intent HMAC、起始 Server/credential revision、状态、固定错误和 request/trace；
  `mcp_server_sync_jobs` 保存可恢复发现/凭据 probe 的 operation owner、candidate credential、lease、attempt、
  deadline 与 finalize 坐标。API、executor 或进程失败都保留可查询事实。
- `mcp_invocation_events` 只为已提交 durable call intent 的 Invocation 生成最多一个 terminal
  `mcp.tool_called` source；输入 DLP block、审批 reject/timeout、撤权、executor disabled 等
  `tool_call_count=0` 的终态不伪造该事件，但仍由 Invocation/Attempt 追踪。字段只含安全枚举、ID、计数和时间。
- `mcp_invocation_event_outbox` 使用 pending/processing/succeeded/failed、attempt、lease generation、
  owner/token/expiry、next attempt 和冻结 subscription snapshot，提供崩溃恢复。
- `mcp_scheduler_tenants` 保存 tenant 公平游标/lease；任何 tenant 的坏任务不能形成全局队首阻塞。

## 6. 调用状态机、并发与结果未知

### 6.1 创建与 DLP preflight

1. 路由先验证 Session、`mcp.invoke` 和 Content-Type，再有界读取 exact JSON；tenant/actor 不来自正文。
2. 同一短事务重读 Tool/Server active 状态、policy/Schema revision、调用者基础与附加权限，规范化并验证
   arguments。
3. 使用当前租户 active input/both DLP 规则扫描全部字符串叶子；树深最多 12、节点最多 1024、字符串总量
   最多 65,536 code points。规则不可读或 regex 异常失败关闭且零网络。
4. 参数立即使用 MCP 独立 key 加密；Invocation、幂等、input ruleset HMAC/version 与 DLP 安全摘要同事务落地。
   block 直接写 terminal blocked 并返回 403；其 `tool_call_count=0`，不创建 `mcp.tool_called` source/outbox。
5. 无审批的调用进入 queued。需要审批时，服务端预生成 Task ID，并在同一 deferred-FK 事务创建
   approval_pending Invocation 与完整 ApprovalTask；任一边失败则整体回滚，提交后不需要补建或 bind。

### 6.2 Scheduler 和公平 lease

- scheduler 只领取持久 queued 或 pre-call probe 可重试的 Invocation。tenant lease 与 invocation lease
  都使用 `BEGIN IMMEDIATE` 和 tenant/status/generation/owner/token/未过期时间完整 CAS。
- 全局并发默认 4、单 tenant 默认 1；部署值只能从配置加载，API 不能覆盖。
- worker 解密参数后重新验证 actor 权限、Tool role/user grant、Server/credential/Tool 状态、全部冻结 revision
  与 fingerprints，并用当前 DLP 规则复扫。
  任一漂移在网络前失败关闭。
- scheduler 只能调用第 4.1 节的 executor authorize/finalize；production executor disabled 时在出网前固定
  失败关闭。test-only loopback executor 使用官方 SDK initialize 并 list target Tool，比较 remote
  original/effective input/output Schema 与 annotations hash。probe 失败最多按 5/30/300 秒重试 3 次；
  这一步没有 `tools/call`，可由过期 lease 接管。
- Schema 精确匹配后，worker 在独立短事务把状态和 Attempt CAS 为 calling/call_started，提交后才执行
  唯一一次 `tools/call`。该持久点可以保守地产生 false unknown，但不能漏掉可能已产生副作用的调用。
- calling 后发生进程崩溃、取消、deadline 或无法证明是否收到响应时进入 `outcome_unknown`，不自动重试。
  客户端用原幂等 key只能恢复该状态；新 key代表显式的新业务意图。
- MCP 明确返回 CallToolResult 时才可判断 known success 或 known tool error。`isError=true` 固定 failed；
  raw result 解析、outputSchema 校验、脱敏、output DLP 或存储失败固定 failed/blocked 且记录
  `tool_call_count=1`，不能伪造未调用。output DLP 规则不可读/不可执行固定
  `mcp.output_dlp_unavailable`、不返回结果且不保存原始结果。
- stop event 在下一次 probe/call 前检查；lifespan 等待已开始同步/异步 worker 完成安全收尾，不通过取消线程
  留下后台网络或数据库写入。

### 6.3 迟到 owner

- 每次 probe、call intent 和终态写入都比较 tenant/invocation/attempt/generation/owner/token/lease。
- 旧 worker 的成功或失败不能覆盖新 owner 或 terminal。calling 的 lease 过期由 reaper 原子收敛为 unknown，
  不是重新执行。
- 事件 source/outbox 与 Invocation terminal 同事务写入；event worker 失败不能改写工具结果，重启后恢复。

## 7. 参数 DLP 与返回脱敏

### 7.1 参数 DLP

- 只扫描 JSON string 叶子，不把 key 名、整数、布尔、null 或 Schema description 当正文。
- 按租户 active `input/both` 规则和既有 priority 顺序；任一 block 规则阻断，log_only 只留下 rule ID、动作和
  match count。allow 不覆盖更高优先级 block，也不保存命中片段、offset、长度或正文 hash。
- input/output 各自冻结 canonical ruleset 的 tenant/domain HMAC 与 key version；两侧 status、match count、
  rule ID 和扫描时间独立，不能用单一 `dlp_status/stage` 覆盖另一侧事实。
- DLP block、unavailable、Schema invalid 都发生在 `tools/call` 前；Invocation 和失败请求仍可追踪。
- 参数在路由/worker 的 finally 中清空可变 bytearray 和结构引用；日志异常只使用固定 code。

### 7.2 返回投影

- 只接受 MCP text content 和 `structuredContent` JSON；未知 content type、资源链接、图片、音频、嵌入 blob、
  非有限 JSON、深度/节点/1 MiB 上限失败关闭。
- 先把 SDK 对象转为 exact plain JSON，再按三层规则处理：
  1. 内建敏感 key 只做 ASCII lower 与 `-` 到 `_` 的规范化后 exact 集合匹配，例如
     `authorization/cookie/set_cookie/access_token/refresh_token/id_token/bearer_token/api_key/provider_key/
     client_secret/password`；不做 substring 匹配，`token_count`、`secretary` 等安全字段不得被误删。命中值
     替换为固定 `"[REDACTED]"`；
  2. Tool policy 的非空 RFC 6901 exact pointer 替换存在的对应值；可选路径不存在是安全 no-op，实际替换数
     仍精确计数，非法 traversal 或非容器中间节点失败关闭；
  3. 当前 output/both DLP block 命中时不返回结果，Invocation 固定 blocked；log_only 仅记录安全计数；
     ruleset unavailable 固定 failed + `mcp.output_dlp_unavailable`。后二者均保持 `tool_call_count=1` 并发出
     可追踪的 `mcp.tool_called`，但原始或未通过 DLP 的结果绝不落盘。
- redaction 保持容器形状；被替换值统一是字符串，`redaction_count` 记录实际替换节点数。原始结果永不落盘；
  只有处理后的 canonical JSON 使用独立 AAD 加密保存。
- result API 只允许原请求者在当前仍具备基础和冻结附加权限时读取，固定 `Cache-Control: no-store`；审批者、
  Server 管理者和其他同租户开发者只能看摘要。

## 8. 高危审批

- `risk_level=high` 强制 `approval_required=1`；standard 也可由策略管理者开启审批。关闭 high 的 approval
  固定 422，不能通过直接 SQL 或前端篡改绕过。
- Approval target version/fingerprint 精确包含 tenant、Invocation、Server/credential/Tool、原请求者、四类
  Schema hash、schema/policy revision、grant/permission 摘要、arguments HMAC/version 和 approval deadline，
  不包含参数明文。
- `mcp.approve` 与 `approval.decide` 每次请求实时校验；批准事务必须比较
  `invocation.requested_by != decider.user_id`，不能错误比较 Task `created_by`。SQL trigger 同样失败关闭直接写库
  的自审，即使原调用者同时具备批准权限。
- approve 在审批事务内仅把仍为 approval_pending、版本未漂移的 Invocation CAS 为 queued，并写
  `mcp_invocation_enqueue` completed writeback；不解密参数、不连接 Server。
- reject/timeout 由 MCP reconciliation 原子把 Invocation 收敛为 rejected，写 terminal event，零网络。
- Tool/Server/Schema/policy/actor 权限在 pending 期间任一变化都会让 target conflict 或执行前失败关闭；
  旧批准不能应用到新配置。

## 9. HTTP API

所有 MCP 响应固定 `Cache-Control: no-store`，成功 DTO 含当前 `request_id/trace_id`；错误走统一
`ErrorResponse`。认证/权限先于正文，exact parser 拒绝重复 key、unknown field、NaN/Infinity、非法 UTF-8、
错误 Content-Type、数组/标量和超限 body。

| method/path | 后端权限 | 行为 |
|---|---|---|
| `GET /mcp/servers` | `mcp.read` | tenant 列表、状态/游标筛选，返回 masked endpoint hint 和 tool counts |
| `POST /mcp/servers` | `mcp.server.manage` | Idempotency-Key；先持久化 Server/credential/operation/sync job，202 |
| `PATCH /mcp/servers/{id}` | `mcp.server.manage` | Idempotency-Key + expected_revision；停启、名称或两阶段 Bearer 轮换，202/200 |
| `POST /mcp/servers/{id}/refresh-tools` | `mcp.server.manage` + `mcp.policy.manage` | Idempotency-Key + expected_revision；持久化只读发现 job，202 |
| `GET /mcp/server-operations/{id}` | `mcp.read` | 查询 register/refresh/rotate 固定状态、计数与错误，不回传 secret/URL |
| `GET /mcp/tools` | `mcp.read` | server/status/risk/approval/cursor 筛选，返回 Schema 摘要和治理策略 |
| `GET /mcp/tools/{id}` | `mcp.read` | 返回单个工具的受控 description、canonical input Schema 和治理策略 |
| `PATCH /mcp/tools/{id}` | `mcp.policy.manage` | expected_policy_revision；冻结风险、审批、grants、权限、脱敏和 active 状态 |
| `POST /mcp/tools/{id}/invoke` | `mcp.invoke` + Tool grant/附加权限 | Idempotency-Key；Schema/DLP/审批 preflight，返回 202 Invocation 摘要 |
| `GET /mcp/invocations` | `mcp.read` | 普通调用者只看本人；server/policy 管理者可看 tenant 安全摘要 |
| `GET /mcp/invocations/{id}` | `mcp.read` | 同上，永不返回参数、hash、URL、Schema 或原始结果 |
| `GET /mcp/invocations/{id}/result` | 原请求者 + 当前执行权限 | 仅 succeeded 的脱敏结果；其他状态固定冲突/不可用 |

### 9.1 注册 request

请求字段：`name`、`endpoint_url`、`auth={type,bearer_token?}`。Token 只在本次 request/允许 executor 内存存在且
永不回传。注册响应为 Server 安全摘要、operation/sync status 和 Tool 计数；发现完成前计数为 0，不能同步
出网后才写 Server。发现失败、executor disabled 或 credential invalid 仍可用 operation ID 查询固定失败事实。
Schema 正文只在单 Tool detail 端点按权限、大小和 no-store 返回；detail 明确区分 original/effective
input/output Schema。

Bearer 轮换只新增 candidate revision 和验证 operation。probe 成功后按 operation 起始 server revision、
旧 active credential revision 和 candidate revision 做原子 CAS；失败/超时保持旧 active revision，candidate
标为 rejected，绝不出现“先替换、验证失败后无可用凭据”。

### 9.2 Tool policy PATCH

请求字段：`expected_policy_revision`、`status`、`risk_level`、`approval_required`、
`approval_expires_seconds`、`required_permission_codes`、`allowed_role_ids`、`allowed_user_ids`、
`result_redaction_pointers`。至少一个可变字段；
unknown/empty/重复 permission 或 pointer、非法 RFC 6901、权限目录漂移均 422/409。

### 9.3 Invoke request/response

请求只有 `arguments` JSON object；server/tool/tenant/actor/revision/status 取路径和数据库。响应只返回
Invocation ID、状态、Tool/Server ID、risk、approval ID、分离 input/output DLP 摘要、invocation/tool-call
计数、成本状态和时间，不回显 arguments。

### 9.4 exact HTTP budgets

- `POST /mcp/servers` 与 credential `PATCH` body 最大 16,384 bytes；refresh body 最大 4,096 bytes；Tool policy
  最大 32,768 bytes；invoke HTTP body 最大 131,072 bytes，canonical arguments 最大 65,536 bytes；GET/DELETE
  不接受 body。所有上限按原始 bytes 在 JSON materialize 前执行。
- register、Server PATCH、refresh、invoke 必须且只能有一个 8..128 visible-ASCII `Idempotency-Key`；服务端只存
  tenant/domain HMAC/version。同 tenant+actor+operation+key 的相同 intent 恢复原事实，不同 intent 409。
- `/mcp` 全路径忽略客户端 `X-Request-ID/X-Trace-ID` 并生成服务端 ID，防止 Token、Endpoint、参数或结果 canary
  被复制进链路 Header；所有成功和错误状态由 shared outer boundary 强制 `no-store, private`，未捕获异常只写
  固定 event/error/ID 且 `exc_info=False`。

## 10. `mcp.tool_called`、Audit 与追踪

### 10.1 exact event payload

Webhook 七字段信封继续使用现有 HMAC/delivery/replay。payload 恰好包含：

- `schema_version=1`、`mcp_invocation_id`、`mcp_server_id`、`mcp_tool_id`；
- terminal `status`、`risk_level`、`approval_required`、`network_attempted`；
- 独立 `input_dlp_status/input_dlp_match_count` 与 `output_dlp_status/output_dlp_match_count`、`redaction_count`；
- `probe_attempt_count`、`call_attempt_count`、`duration_ms`；
- `request_bytes`、`response_bytes`、`execution_status`、`invocation_count=1`、nullable `tool_call_count`；
- `cost_status=not_applicable/not_reported/unknown`、`error_code` nullable、`completed_at`。

信封 actor 是原请求用户，object 是 Invocation，trace 是原调用 trace。事件不包含 Server/Tool 名、URL、
Schema/annotation/permission 列表、参数/结果/hash、Approval fingerprint、Token 或第三方错误。
只有 durable call intent 后的 called/unknown 终态可生成此事件；任何确定的 `tool_call_count=0` 终态没有
`mcp.tool_called` source/outbox，避免把 preflight/审批失败描述成工具已调用。

### 10.2 Audit 与 Invocation log

- Server 注册/状态/凭据轮换、operation 完成、工具 refresh/policy/grant 变更写 AuditLog；before/after 只含
  ID、status、revision 和安全计数。URL、Token、description、Schema/hash 和 redaction path 不进入 Audit。
- 高频调用以 append-only McpInvocation/Attempt/Event 为权威日志，不复制到通用 AuditLog。
- 每次 succeeded/failed/blocked/rejected/unknown 都可按 tenant、request/trace、actor、Tool、固定错误、
  duration、bytes、attempt、分离 DLP、approval、invocation count 和 tool-call 状态追踪。
- MCP Server 未提供可信计价合同，PRD-082 不伪造零成本。零网络固定 not_applicable，已知调用固定
  not_reported，结果未知固定 unknown；cost 始终 NULL，UI/事件显示对应状态。未来计价不得回写历史事实。

## 11. PAGE-114 MCP 工具页

- 在“开发者”分组新增 `mcp-tools` 导航，最低后端导航权限为 `mcp.read`。
- 页面使用 Servers、Tools、Invocations 三个 tab，不创建嵌套卡片；桌面用紧凑表格，移动用单层列表。
- Servers tab 提供注册 Dialog、状态、协议、工具计数和刷新时间；Token 输入提交后立即清空，响应和 DOM
  不保存 Token/URL 全文。
- Tools tab 显示 Tool ID、受控名称、Schema hash 短摘要、revision、risk、approval、附加权限和状态；
  用户显式打开详情 Dialog 后才按权限加载并转义展示 bounded canonical Schema/description，关闭即清空。
  `mcp.policy.manage` 用户可用独立原生 Dialog 编辑策略；Schema/description 不进入列表、Storage 或截图证据。
- Invocations tab 显示安全状态、approval、DLP、脱敏计数、attempt、耗时、bytes、成本状态和固定错误；
  不显示参数或结果。
- `mcp.server.manage`、`mcp.policy.manage` 只控制相应按钮；篡改 DOM 后端仍 403。无 `mcp.read` 不预取、
  不渲染页面 DOM。
- 页面 owner 精确包含 tenant/user/session/permission revision/fingerprint/navigation；每个 tab/list/dialog 有
  generation + AbortController。撤权、tenant/session/user 变化、离页、登出和迟到响应必须清空 Token、
  草稿、Dialog、列表、错误和结果引用。
- Dialog 覆盖焦点进入、Tab 循环、Escape、busy 禁止关闭、提交恢复；所有第三方文本 HTML escape，长 ID
  在所属列内换行。`1280x720` 与 `390x844` 均不得横向溢出、文字越界或控件重叠。

## 12. Migration 0075

### 12.1 up

1. 在首个持久 DDL 前精确验证 `74/0074`、revision-16 目录/默认矩阵、Approval/Webhook owner、
   `integrity_check=ok` 和外键 0。
2. RBAC 目录升 revision 17，增加五权限，只扩展精确 revision-16 默认角色；恢复全部不可变目录 trigger。
3. 保真重建 Approval Task/Decision/Writeback 全分支与关联 trigger，增加固定 MCP target/adapter、预生成 Task ID、
   deferred 双向 owner 和禁止自审；预算与会话风险历史 rowid、状态、索引和 hash 不变。
4. 保真扩展 `audit_logs` action/object type SQL CHECK，并用 rowid、列值、双向 `EXCEPT` 证明所有既有 Audit
   完全不变；不能只改 Python whitelist 导致数据库拒绝 MCP Audit。
5. 创建 Server、Credential、Operation/SyncJob、Tool、Role/User Grant、Invocation、Attempt、Event/Outbox、
   Scheduler 表、索引、三列 Tool owner FK、append-only、state/CAS、lease、终态和 rowid/replace owner trigger。
6. 扩展 Webhook event 目录由 Shared validator 管理；迁移不历史生成 MCP 事件。
7. 事务末尾复核 revision-17 精确目录/default matrix hash、全部新表/trigger、Approval/Audit 双向保真、
   完整性和外键后才提交。

### 12.2 down

- down 先验证精确 0075 Schema/trigger/hash。存在任何 MCP Server/Credential/Operation/SyncJob/Tool/Grant/
  Invocation/Attempt/Event/Outbox、MCP Approval/Decision/Writeback、MCP Audit/Webhook delivery、非默认新
  permission grant 或当前 owner 时原子失败关闭。
- 只有无业务事实且 revision-17 默认矩阵未定制时，删除 PRD-082 表、双向保真还原 Approval 与 Audit、
  收缩精确默认角色、删除五权限并恢复 revision 16。完成后必须 `74/0074`、integrity ok、FK 0。
- `INSERT OR REPLACE`、直接 rowid、跨租户关系、非法状态跃迁和 down 绕过都由数据库测试覆盖。

## 13. 统一错误合同

| code | HTTP | 场景 |
|---|---:|---|
| `mcp.feature_disabled` | 503 | 扩展未启用或独立密钥/worker 配置缺失 |
| `mcp.executor_disabled` | 503 | PRD-083 前生产网络 executor 固定关闭，operation/invocation 仍持久可查 |
| `mcp.request_invalid` | 422 | exact body/query/header、URL、pointer 或资源上限非法 |
| `mcp.schema_invalid` | 422 | 第三方 Schema 不满足 Draft/profile/大小合同 |
| `mcp.sync_failed` | 502 | 已持久 register/refresh operation 的发现失败 |
| `mcp.credential_invalid` | 422 | candidate credential probe 失败，旧 active credential 未改变 |
| `mcp.server_not_found` | 404 | 缺失、跨租户或不可见 Server |
| `mcp.tool_not_found` | 404 | 缺失、跨租户或不可见 Tool |
| `mcp.invocation_not_found` | 404 | 缺失、跨租户或不可见 Invocation |
| `mcp.state_conflict` | 409 | Server/Tool/Invocation revision 或状态并发漂移 |
| `mcp.idempotency_required` | 400 | 写/调用缺少唯一 Idempotency-Key |
| `mcp.idempotency_conflict` | 409 | 同 key 对应不同 intent |
| `mcp.arguments_invalid` | 422 | arguments 不满足冻结 Schema |
| `mcp.arguments_blocked` | 403 | 参数 DLP block，零工具网络 |
| `mcp.result_blocked` | 403 | 工具已调用但 output DLP block，结果不落盘/不返回 |
| `mcp.output_dlp_unavailable` | 503 | 工具已调用但 output DLP 不可用，失败关闭且无结果 |
| `mcp.approval_self_denied` | 403 | 原调用者尝试批准自己的高危 Invocation |
| `mcp.authorization_revoked` | 403 | 排队后原 actor 被撤权，零 tools/call |
| `mcp.server_unavailable` | 503 | initialize/list-tools 在 call intent 前失败 |
| `mcp.tool_failed` | 502 | Server 返回已知 `isError=true` |
| `mcp.result_invalid` | 502 | 已知结果类型、大小、Schema 或脱敏合同非法 |
| `mcp.outcome_unknown` | 503 | call intent 后无法确定第三方是否执行，禁止自动重试 |
| `mcp.unavailable` | 503 | 数据库、密文、owner、scheduler 或 outbox 无法可靠恢复 |

认证继续使用既有 401，基础/附加权限不足使用 `auth.permission_denied` 403。任何错误响应均由中间件附加
request_id/trace_id，且不得包含 URL、Tool/Server 名、Schema、参数、结果、Token、SQL 或异常正文。

## 14. 安全、隐私与兼容性

- MCP 独立 AES-GCM/HMAC current/previous keyring 不得复用 Auth、Provider、Webhook、Approval、Batch、File、Prompt、
  Response Cache 或 Session Risk Secret；prod 缺失/弱值/复用值启动或功能入口失败关闭。
- 固定配置面为 `METAHUB_MCP_ENABLED`、独立 encryption/HMAC current/previous version+key、
  `METAHUB_MCP_EXECUTOR_MODE=disabled`、test-only loopback 开关、scheduler enabled/poll/batch、global/per-tenant
  concurrency、operation/invocation lease、probe/call deadline、request/Schema/result bytes、tree/validator budgets。
  全部有保守默认值与上下界，API/tenant 不可覆盖；prod 拒绝 `test_loopback`，且不存在可绕过的 trusted-origins
  开关。
- Server Endpoint、Bearer、arguments、脱敏结果、idempotency/intent、input/output ruleset 使用不同 HKDF/HMAC
  domain 和 owner AAD；密文换 owner、tenant、kind 或 version 必须解密失败。
- 日志 allowlist 只含固定 event/error、tenant、Server/Tool/Invocation/Attempt ID、request/trace、状态和计数。
  捕获异常对象、httpx/MCP repr、URL/Header/body、Schema、description、参数或结果一律禁止。
- production Control/scheduler 对 tenant Endpoint 的 socket 建立固定为零；SDK/HTTP 依赖只存在明确 test-only
  loopback executor，错误全部折叠成固定 code。access log 不带 query，本模块本身拒绝 URL query。
- shared `fastapi_app` 把 `/mcp` 作为最外层敏感请求边界：不信任客户端链路 ID，所有状态强制 no-store，
  未捕获异常固定日志不携带异常对象/traceback。业务 router 自己设置 Header 不能替代该边界。
- response、Browser storage、DOM、截图、Runtime JSON 和提交证据不得包含 canary Token/参数/结果/Endpoint。
- 既有 Chat/Gateway/DLP/Output Safety、Approval、Webhook、Audit、RBAC 自定义矩阵和迁移保持兼容；
  MCP worker 不进入 Gateway 模型热路径。
- Provider 目录、竞品定位不变；`all_108_prds_complete=false` 在代码、文档、Excel 和证据中保持。

## 15. 模块修改计划

1. Shared：新增 MCP exact contracts/Schema profile/crypto/result redactor/event payload；扩展 RBAC revision 17、
   Webhook event validator、errors/i18n/config 与 tests。
2. 依赖：runtime requirements 增加官方 `mcp` v1 与 `jsonschema` v4 保守范围；README 安装命令不变。
3. Migration：新增 `0075_mcp_tool_registration_permissions.sql/.down.sql`，完成 RBAC、Approval/Audit 保真扩展及
   tenant-qualified MCP Credential/Operation/Job/Grant/Invocation 图和全量 guard。
4. Control：新增 `mcp_governance/{models,wire,executor,service,scheduler,outbox,routes}.py`，接入 app router
   和 lifespan；扩展 Approval target/writeback。生产 executor 固定 disabled，SDK adapter 只供 test loopback。
5. Console 后端：新增 PAGE-114 导航与服务端 required permission。
6. Console 前端：新增 `src/lib/mcpTools.js`、`src/features/mcp-tools/`、main lifecycle 和响应式样式。
7. Tests：Shared、0075、Control API/worker/approval/outbox、Console unit/browser contracts、Runtime/Browser
   acceptance。
8. Docs：新增 MCP API、测试记录，更新 README、目录说明、Excel `12_开发状态`；完成后只登记下一条，
   不实现 PRD-083。

## 16. 测试计划

### 16.1 Shared、Schema 与 crypto

- executor authorize/finalize owner 合同、production disabled 与 test loopback SDK adapter；tools/list 分页、
  重复 cursor/name、protocol/response/content type 异常。
- Draft 2020-12 正反例：unknown/remote ref、open object、无界 array/string、effective 确定性加固、optional
  outputSchema、unsafe regex、format、keyword/combinator/enum/evaluation budget、深度、节点、bytes、工具数、
  重复 key、bool-int、NaN/Infinity、非法 UTF-8。
- original/effective Schema/annotations/result 和 hash；MCP event exact payload、exact normalized 敏感 key，
  `token_count` 不误伤、错误类型和大小边界。
- Endpoint/Bearer/arguments/result 四域 AES-GCM，tenant/owner/kind/version 篡改、current/previous rotation、
  endpoint/idempotency HMAC current/previous 查重与受控 re-key、repr/model_dump/log 零明文。

### 16.2 迁移与数据库

- 空库 `75/0075 -> repeat -> 74/0074 -> 75/0075`，四阶段 integrity ok、FK 0。
- seeded 0074 的 RBAC、Approval、Audit、Webhook、业务 rowid/字段/hash 双向保真；revision-16 自定义矩阵不扩权。
- tenant 复合 FK、三列 Server/Tool 错配、deferred Approval 缺任一边 commit、自审直写、跨租户 owner、
  Server endpoint 唯一、Credential 两阶段、Operation/Job、Tool Schema/policy/grant CAS、Invocation 幂等/state、
  Attempt append-only、lease、terminal source/outbox、rowid/replace/update/delete 全部正反例。
- calling 过期只能 unknown；queued/probing 可受控恢复；旧/迟到 owner 不能覆盖新 owner或 terminal。
- 每类 MCP 事实/审批/Decision/Writeback/Audit/delivery/grant 单独存在时 down 原子阻断；清空后真实 down/re-up。

### 16.3 API、RBAC、DLP 与审批

- auth/permission 先于畸形 body；anonymous 401、无权/动态撤权 403、跨租户同形 404、query/header/body exact。
- production executor 断言零 DNS/socket；注册真实 test-only loopback MCP Server，先持久 operation/job，再发现；
  重复/幂等、注册失败可追踪、Token 加密、旧凭据保留的两阶段 rotation、Server disable/refresh、Schema 漂移
  回 review_required。
- Tool list/detail 分离；original/effective input/output Schema detail no-store/权限/大小/转义；Tool policy CAS、
  high 强制审批、role/user grant、附加 permission 交集、未知/重复 code、redaction pointer
  present/missing/非法 traversal 正反例。
- clean 参数排队；log_only 放行；block/DLP unavailable/Schema invalid 零 tools/call；排队后规则或权限变化复扫。
- 高危 Invocation 与预生成 ApprovalTask 在 deferred-FK 同事务双向绑定；缺边、owner/fingerprint 漂移回滚；
  step-up、事务/trigger 双层禁止自审、批准/拒绝/timeout、target/revision 漂移、动态撤权与重启 reconciliation。
  批准事务内不得出现 MCP/HTTP 调用。
- succeeded/known error/pre-call unavailable/call unknown/result invalid/outputSchema invalid/output DLP
  block/unavailable；同 Invocation 最多一个 call intent，迟到 worker 不覆盖终态。
- result 只供原 actor 且当前仍授权；其他同租户用户、审批者、跨租户、非成功状态均不返回正文。

### 16.4 Audit、Webhook 和追踪

- Server/Tool 变更 Audit 只含安全摘要；Invocation/Attempt 对成功、失败、blocked、rejected、unknown 可追踪。
- `mcp.tool_called` exact source/outbox、稳定 event/delivery ID、订阅冻结、失败/死信/replay、坏 source 隔离、
  Control 重启恢复；所有确定零 call 的 terminal 断言无该 source/outbox。
- not_called/called/unknown 三态下 `invocation_count=1`、`tool_call_count=0/1/NULL` 与 cost
  not_applicable/not_reported/unknown 精确对应；
  duration/bytes/attempt/error/request/trace 与数据库事实一致，失败请求不丢失。
- 日志、Audit、event、SQLite plaintext、DOM、Storage、截图、Runtime/Browser JSON 对随机 Token、URL、参数、
  原始/脱敏结果 canary 命中为 0。

### 16.5 Console 与实际运行

- PAGE-114 三 tab、注册 Dialog、Tool policy、Invocation 摘要、HTML escape、no sensitive DOM/query/storage。
- 无权不预取/不渲染；分权限按钮；撤权、owner 变化、迟到响应、离页/登出清理；Dialog focus/Tab/Escape/busy。
- `1280x720` 与 `390x844` 实际 Chrome 无横向/文字溢出、重叠或空白页面；截图人工复核。
- Runtime 从空 SQLite 启动真 Control + loopback MCP + Webhook receiver：标准成功、高危批准、拒绝、
  DLP block、返回脱敏、known failure、call unknown、Control 停服恢复；最终 pending/lease/sidecar/owned process
  为 0，端口释放，provenance/hash 匹配当前源码。

## 17. 完成门禁

- 设计初稿必须经过两轮编码前自审并实际修正文档，之后才能改生产代码。
- 开发阶段只跑 PRD-082 Shared/0075/Control/Approval/Console 专项；完成门禁再跑必要相邻回归。
- 新增 Python 文件跑全规则 Ruff，变更 Python 新增行诊断为 0；中文注释门禁、Node lint、Git whitespace 通过。
- 空库 migration/repeat/down/re-up、真实 Runtime、真实 Browser、健康检查、敏感信息和禁止文件审计全部通过。
- 提交范围不得包含 `.env`、数据库/WAL/SHM/journal、日志、`.tmp/.artifacts`、Browser profile/cache、
  Office 锁文件、凭据、符号链接或超限生成物。
- PRD-082 完成后更新 Excel/README/docs 并提交推送 `origin/main`；只登记 PRD-083，保持
  `all_108_prds_complete=false`。

## 18. 编码前审查方法

第一轮聚焦协议/Schema、数据 owner、审批事务、调用幂等、call intent、并发、崩溃恢复和结果未知；
第二轮聚焦 API exact wire、RBAC/tenant、DLP/脱敏、SSRF 边界、PAGE owner、隐私和兼容性。
每轮发现必须先修改正文、迁移/API/测试计划，再记录关闭依据。

## 19. 编码前第一轮自审：已完成并修正

2026-08-16 按协议/Schema、owner、审批事务、幂等、call intent、用量和崩溃恢复逐项复核，发现并关闭：

1. **P0：初稿只要求 prod HTTPS，tenant 管理者仍可让 Control 请求任意 HTTPS 目标，PRD-083 完成前会留下
   SSRF/横向访问窗口。** 第一轮曾提出部署级 exact origins；第二轮证明该解法不能约束 DNS 最终 IP/rebinding，
   已按第 20.1 项替换为生产 executor 固定 disabled，而不是保留伪安全白名单。
2. **P1：高危 Invocation 先提交、再创建 ApprovalTask 存在进程崩溃后永久 pending 的窗口。** 第一轮曾设计
   durable approval outbox；第二轮确认同库事务可提供更强保证，已按第 20.4 项替换为预生成 Task ID、deferred
   复合 FK 与同事务双向绑定，不再保留 eventual orphan 窗口。
3. **P1：初稿把所有 terminal 都写成一次调用/`cost=not_reported`，会把 DLP block、拒绝和结果未知
   伪装成已调用。** 已冻结 execution not_called/called/unknown，并在第二轮进一步拆成
   `invocation_count=1`、`tool_call_count=0/1/NULL` 与 cost 三态。
4. **P1：PAGE/API 只有 Schema hash，不能直接验收工具 Schema，也无法让授权用户复核远端合同。** 新增
   `GET /mcp/tools/{id}` 和按需详情 Dialog；列表、Storage、截图仍不包含 Schema/description。
5. **P1：endpoint HMAC 未记录 key version，轮换后同一 Endpoint 可能以新摘要重复注册。** 增加 HMAC
   version、current/previous candidates 和写时 re-key；Invocation 幂等采用相同轮换原则。
6. **P1：SDK 若先无界 materialize 再检查 1 MiB，会让大小门禁失效。** transport adapter 必须在读取流时
   截止实际 bytes，并以专项测试证明，不依赖响应完成后长度判断。
7. **P2：可选 redaction pointer 缺失被定义为 conflict，会使正常可选结果字段降低可用性。** 现冻结为
   缺失 no-op、存在则替换、非法 traversal 失败关闭，redaction_count 只统计实际替换。

第一轮其余问题均已关闭；其中 SSRF 与 Approval 两项的第一版修正由第二轮主动推翻并改为更强合同。

## 20. 编码前第二轮自审：已完成并修正

2026-08-16 按 API exact wire、RBAC/tenant、DLP/脱敏、SSRF、审批、迁移、PAGE owner、隐私和兼容性逐项复核，
发现并关闭：

1. **P0：exact trusted origin 不能阻止 DNS 解析到 RFC1918/link-local/metadata 或 rebinding。** 删除该生产
   解法；production executor 固定 disabled。PRD-082 只交付治理控制面、authorize/finalize 和 test-only
   literal-loopback SDK adapter，PRD-083 才启用受沙箱/ACL 约束的第三方出网。
2. **P0：注册/refresh 同步出网后才写 Server，失败请求与凭据验证不可追踪。** 改为先原子提交 Server、
   credential candidate、幂等 operation 和 sync job，再异步发现；成功/失败/禁用均有安全 operation 事实。
3. **P0：业务 router 设置 no-store 和链路 ID 不覆盖 parser/404/405/未捕获异常。** 将 `/mcp` 纳入 shared
   FastAPI 最外层敏感边界：忽略客户端 request/trace、所有状态 no-store/private、异常日志固定且无 traceback。
4. **P0：Approval outbox 仍允许阶段性 orphan，自审若比较 `created_by` 也会绕过。** 改为预生成 Task ID、
   deferred tenant 复合 FK、双向 owner trigger 和同事务 Task/Invocation；批准事务及 SQL trigger 都比较
   Invocation `requested_by` 与 decider。
5. **P0：0075 若只扩 Python Audit whitelist 会被旧 SQL CHECK 拒绝；两个独立 FK 也允许跨 Server Tool 错配。**
   迁移现要求 Audit rowid/双向 `EXCEPT` 保真扩展、Tool 三列唯一与 Invocation 三列 FK，并覆盖 Approval
   Task/Decision/Writeback 全分支、RBAC 精确矩阵、完整 down guard 和 state/lease/event 矩阵。
6. **P1：Bearer 先替换再 probe 会让失败轮换破坏可用凭据。** 拆出版本化 credential 表；候选 probe 成功后
   才按起始 Server/active/candidate revision 原子切换，失败继续使用旧 active。
7. **P1：强迫第三方 Schema 自带 closed object/上限会拒绝正常 MCP Server，缺 outputSchema 又无法验证结果。**
   同时保存原始 canonical 与确定性 effective input/output Schema，平台注入/收紧边界并用 effective 版本验证；
   keyword/combinator/enum/regex/evaluation budgets 防止 Schema CPU DoS。
8. **P1：全局 `mcp.invoke` 加任意 permission 不能表达逐工具授权。** 新增 tenant-qualified role/user grants；
   Invocation 冻结 grant 摘要以及 Server/credential/Schema/policy revisions，每次执行重新校验。
9. **P1：单一 DLP status 会覆盖输入和输出事实，output scanner unavailable 可能泄露未审结果。** 输入/输出
   status、match、rule、ruleset HMAC/version 分列；已调用后的 output block/unavailable 不保存或返回结果，
   但保持 `tool_call_count=1` 和固定错误/事件。
10. **P1：arguments/result 裸 SHA-256 可被低熵枚举。** 改为 tenant/domain HMAC 与 key version；输入、结果、
    idempotency、intent、两侧 DLP ruleset 分域，摘要不公开、不进日志或 Webhook。
11. **P1：所有 terminal 发 `mcp.tool_called` 会把 preflight/拒绝/timeout 伪造成远端调用。** 只有 durable call
    intent 后的 called/unknown 终态生成 EVT-094；零 call 只保留 Invocation/Attempt。追踪拆成
    `invocation_count=1` 与 `tool_call_count=0/1/NULL`。
12. **P1：敏感 key substring 脱敏会误删 `token_count` 等安全用量字段。** 冻结 ASCII 规范化后的 exact key
    集合匹配；专项测试覆盖命中与不误伤。
13. **P1：各写 API body、refresh 幂等和 operation CAS 未冻结。** 已固定逐端点原始/canonical byte 上限，
    register/Server PATCH/refresh/invoke 单一 Idempotency-Key、tenant/domain HMAC 以及起始 revision CAS。
14. **P1：运行配置未形成可验收闭集。** 已冻结 feature/keyring/executor/scheduler/concurrency/lease/deadline/
    size/validator 配置及上下界，tenant/API 不得覆盖，prod 拒绝 test loopback。

两轮审查无未关闭 P0/P1，正文、迁移/API/测试计划均已同步修正；设计冻结，编码门禁现已关闭。

## 21. 实现落地

1. Shared 新增 `metahub_shared.mcp` contracts、Draft 2020-12 Schema profile、分域 HMAC/AES-GCM、参数 DLP
   投影、结果脱敏与 `mcp.tool_called` exact payload；RBAC catalog 提升到 revision 17，固定五项 MCP 权限。
2. `0075_mcp_tool_registration_permissions.sql/.down.sql` 已创建 tenant-qualified Server、Credential、
   Operation、SyncJob、Tool、Grant、Invocation、Attempt、Approval 绑定、event/outbox 与 scheduler 表，
   并以 owner、复合外键、append-only、CAS、lease、Audit 保真和 down guard 约束状态。
3. Control 新增 `mcp_governance/{models,wire,executor,service,scheduler,outbox,routes}.py`，接入认证后 exact wire、
   Server 注册/刷新/停用/启用、Tool Schema/policy、异步 invoke/result、非自审高危审批、公平 scheduler、
   事件 outbox 和 lifespan 恢复；后端实时 RBAC、tenant owner、统一错误/request_id 与 no-store 是唯一边界。
4. 生产 executor 固定 `disabled`；公开 API 只持久化操作和调用事实。正式 Runtime 仅以 test-only
   literal-loopback 官方 SDK adapter 验证 Streamable HTTP，不交付 PRD-083 的沙箱、网络 ACL 或生产出网。
5. Console PAGE-114 已落地 Servers、Tools、Invocations 三个 tab、原生注册 Dialog、按需 Schema Dialog、
   Tool policy 与安全 Invocation/审批摘要；前端不保存或展示 Token、Endpoint、原始参数或结果正文。
6. 自动化、Runtime、Browser、MCP API、测试记录、README 与目录说明已同步。正式 Runtime 为
   `.artifacts/prd082-runtime/prd082-final-runtime-7`，JSON SHA-256 为
   `15457991d5e1fb5c1b1acc55530d53acd68e74f9fb53bc5a7c3f3c0633b21c21`；正式 Browser 为
   `.artifacts/prd082-browser/prd082-page114-20260816-r8`，JSON/manifest SHA-256 分别为
   `7c9a965af27968769f8294c14a569bb152e27998d4b083f300f51625511360eb`、
   `e387ef1cf76b53b2a886751899ab919b9ea5fd90ca44beb9ec74e953f4eb411c`。

## 22. 实现后第一轮自审：已完成并修正

2026-08-16 按生产网络边界、迁移往返、审批原子性、DLP/脱敏、调用状态机和可追踪性复核，关闭以下问题：

1. **P0：治理实现可能被误解为已允许生产第三方 MCP 出网。** 配置和启动门禁现固定 production executor
   为 `disabled`；Runtime 只允许显式 test profile 下的字面量 loopback adapter，并直接证明生产模式零网络。
2. **P1：只校验 up migration 会遗漏 down 文件漂移。** Runtime 与 Browser provenance 均纳入 up/down SHA；
   空库实际完成 `75/0075 -> 75/0075 -> 74/0074 -> 75/0075`，四阶段 integrity ok、FK 0。
3. **P1：高危调用若只检查最终状态，不能证明非自审、step-up 和 Approval/Invocation 原子绑定。** Runtime
   现直接验证非自审批准、step-up 消耗、绑定数量和高危成功；审批事务不执行网络。
4. **P1：DLP block、输出 block、已知失败和结果未知容易被合并成“调用成功”。** Runtime 分别验证零远端
   调用 input block、output block、`mcp.tool_failed`、`mcp.outcome_unknown`、脱敏成功和 log-only 成功，
   并保留 Invocation/Attempt、失败请求、成本状态和 event/outbox 事实。
5. **P1：原始参数或低熵结果可能经响应、SQLite 或验收制品泄露。** 响应不回显随机 arguments，数据只保存
   分域密文/摘要和脱敏结果；Runtime 对 9 个受控文件的敏感明文命中为 0。

最终专项合集 `146/146`、Scheduler `30/30`、Runtime contracts `36/36`、Browser harness `8/8`、Console
`846/846`、Ruff 与 compile 均通过；分组存在覆盖，不相加冒充一次
根级全量。第一轮正式范围无未关闭 P0/P1。

## 23. 实现后第二轮自审：已完成并修正

第二轮聚焦 PAGE-114 操作闭环、响应式布局、网络/进程 owner、证据发布与隐私边界：

1. **P1：Server disable/enable 若只验证请求返回，不能证明页面和数据库状态闭环。** Browser 现实际执行
   register、refresh、disable、enable；数据库精确保留四个 operation 和
   `mcp.server_registered/disabled/enabled`、`mcp.tool_policy_updated` 四条管理 Audit。
2. **P1：Tool 列表只有 hash 不能证明 Schema 可复核，移动端表单也可能退化为非原生浮层。** 桌面按需打开
   Schema Dialog 并关闭清空；移动端使用原生注册 Dialog，三个 tab 均以真实页面操作验证。
3. **P1：Browser 成功不能建立在外网、敏感 query 或残留服务上。** 正式批次外部请求、禁止 query、
   Console/Page error 均为 0；Browser、Console、Control owner 均停止，SQLite sidecar 为 0。
4. **P1：截图存在不代表布局可用。** `1280x720` 与 `390x844` 四张 PNG 已目视复核为非空、内容完整；
   全局横向 overflow、文字 overflow 和 overlap 均为 0，Console warning/error 为 0。
5. **P1：验收制品若直接覆盖 final，失败轮可能留下半发布证据。** harness 以 owner marker、staging 和原子
   rename 发布白名单；正式 Browser JSON、四图与 manifest 逐文件 SHA-256 匹配，work/staging 共 11 个受控
   文件敏感明文命中为 0。

正式 Browser JSON 的自动化撤权与 viewer 断言已由人工追加验收再次确认：会话过期时权限矩阵失败关闭；
动态撤权后 PAGE-114、导航和 root 状态清除并回到概览；viewer 无入口，直达 `#mcp-tools` 仍不渲染
PAGE-114。正式批次的端口、进程、数据库、sidecar、work/staging/driver 和临时凭据最终均为 0；权威
r8 证据只在完整校验后原子发布。第二轮正式范围无未关闭 P0/P1；PRD-083 仅登记、未启动。

## 24. 最终独立深审：已完成并修正

提交前对当前实现、状态机、前端 owner 和验收器再做独立交叉审查，关闭以下问题：

1. **P2：object Schema 可只在 `oneOf/anyOf/allOf` 分支声明字段，从而绕过父层字段承载约束。** profile
   现拒绝无本地字段承载能力却依赖组合分支声明字段的 object；父层已声明字段的合法组合仍可用。
2. **P1：Scheduler 允许第四次 probe，但 EVT-094 payload 上限仍为 3。** 事件合同已对齐为 4；真实 SQLite
   回归按 5/30/300 秒完成前三次 unavailable、第四次成功、单次 call intent 与 outcome unknown，并断言
   4 条 probe attempt、1 条 call attempt、事件和 pending outbox。
3. **P1：PAGE-114 在首个 hashchange/pagehide、空/非法 hash、响应体读取 AbortError 和迟到旧 401/403 下
   可能保留或影响新 owner。** 页面现同步清理私有 state，owner 要求精确 hash；所有 mutation/detail 先校验
   当前 ticket，`fetch` 与 `response.json()` 阶段的 AbortError 都原样传播。
4. **P1：验收发布在 owner marker、写盘、扫描或 rename 后失败时可能留下无 owner staging/final，且证据验证与
   provenance 不足以冻结真实执行路径。** Runtime/Browser/driver 均改为 marker-first 可回滚、final 失败
   owner-safe 清理；evidence 使用 exact typed facts，provenance 覆盖动态 helper、Schema/event/outbox 与
   Console main，并在发布前后重复比对。故障注入 Python `44/44`、Node `6/6` 通过。

深审修复后重新生成 Runtime r7 与 Browser r8，原 r6/r7 仅为历史批次，不再作为当前源码权威证据。
最终范围无未关闭 P0/P1；`all_108_prds_complete=false`，PRD-083 仅登记、未启动。
