# PRD-075 - 模型 A/B 测试与质量评估

状态：**已完成（2026-08-10）**。编码前两轮设计审查及第 19、20、22 节实现期修订均已完成、自审并修正，
最终 P0=0、P1=0；Shared、Gateway、Control、Console、0068 正反迁移、真实 Runtime、PAGE-105 Browser、
隐私和提交前门禁已按最终源码重新通过。早于第 20、22 节修订的 Runtime/Browser 仅保留为失效历史，
不参与最终结论。本条由权威 Excel、README、系统架构、
PRD-048/049/074 设计与测试记录交叉确认后启动，不按编号猜测。PRD-074 已由提交
`a122763e77bd086daaed65888611e80baacd4bda` 完成并推送，不在本条重复实现。全部 108 条
总体目标仍在进行中：`all_108_prds_complete=false`。

## 1. 需求来源与顺序确认

### 1.1 Excel 权威行

已使用 OOXML 结构化解析逐工作表核对：

- `01_PRD总表!76`、`03_M2-M3扩展交付!12`：
  `PRD-075 | M2-企业增强 | P2 | 模型评测 | 模型 A/B 测试与质量评估`。
- 主要用户：产品、管理员。
- 用户故事：创建 A/B 实验，配置样本请求、模型 A/B、比例和反馈收集。
- 验收口径：实验流量比例可控，实验隔离和统计可信；对比成本、延迟、成功率和
  满意度/评分。
- 非目标：不做自动训练。
- `04_页面清单!106`：`PAGE-105 模型评测页`。
- `05_API接口清单!342:347`：`POST /experiments`、
  `GET /experiments/{id}/results`。
- `06_数据对象!102:103`：`ModelExperiment`、`ExperimentResult`；业务对象默认包含
  `tenant_id`。
- `07_Webhook事件!85`：`experiment.completed`。
- `08_验收用例!150:151`：`TC-0149` 主流程、`TC-0150` 边界/异常。
- `09_CodexClaude提示词!76` 与上述范围一致。

### 1.2 交叉确认

README、`docs/development/directory-map.md`、PRD-074 设计与测试记录均把 PRD-075 标为
下一条；`docs/prd` 和 `docs/testing` 尚无 PRD-075 完成记录。架构设计 7.3 要求以 Trace、
用户反馈、成本和延迟形成质量-成本事实，并明确路由演进包含 A/B。PRD-048 排除了 A/B
分桶和实验统计，PRD-049 则提供不可变反馈事实，因此本条是在既有事实链上新增在线实验，
不是重写模型质量、反馈、用量或 FinOps。

## 2. 需求理解与设计决策

“样本请求 + 可控流量比例”冻结为**在线真实流量实验**，不是让控制面接收一批离线 Prompt：

1. 实验绑定当前 tenant 的 Project、Environment、ModelAlias 和两个 concrete Model。
2. 只对公开 `POST /v1/chat/completions` 的有效 Virtual Key 流量生效；非流式和流式均可进入。
3. Embeddings、内部 Enterprise Chat、PRD-074 Batch、Policy `route_to` 和未命中作用域的调用不进入实验。
4. Gateway 在读取并通过 Policy 后，按独立 HMAC 域做采样和 A/B 分桶；命中后持久化不可变
   Assignment，再只调用该 variant 的 Model。
5. 实验 variant 禁止运行时 fallback 到另一 variant，也不使用质量/成本或 Prompt Cache 亲和
   重排，否则成功率、延迟和成本归因会被污染。
6. DLP、Intent、Session Risk、Rate Limit、Budget、Output Safety、Provider KeyPool、RequestLog、
   Usage 和 FinOps 仍完整执行。实验不能绕过任何安全或财务闸门。
7. 不保存或返回 Prompt、Response、Provider Key、Token、Endpoint、Authorization、Cookie、
   Prompt 指纹或客户正文；实验只保存本地对象 ID、分桶整数和已有事实 owner。
8. 不自动宣布赢家、不自动切换 Alias、不训练或微调模型。页面只展示后端冻结的可解释指标。

选择在线模式的依据是权威需求明确要求“流量比例”；若接受 raw Prompt 样本会新增敏感正文
存储和执行入口，却没有权威 run/upload API，也与项目默认不记录 Prompt/Response 的边界冲突。

## 3. 范围与非目标

### 3.1 本 PRD 实现

- `ModelExperiment`、不可变 Assignment、冻结 `ExperimentResult`、source event/outbox。
- `POST /experiments` 和 `GET /experiments/{id}/results`。
- PAGE-105 恢复所必需的派生只读 API：`GET /experiments`、`GET /experiments/{id}`。
- tenant/project/environment/alias 作用域，目标样本数、总流量采样率、A/B 配额和反馈窗口。
- HMAC-SHA256 确定性采样、配额约束随机分桶、并发不超样本上限。
- Gateway exact variant route、无跨 variant fallback、流式/非流式共同归因。
- RequestLog、RoutingAttempt/StreamSession、Usage、FinOps 和 RequestFeedback 的 owner-chain 统计。
- `active -> collecting_feedback -> completed` 自动收口和 `experiment.completed` 可靠投递。
- RBAC catalog revision 11、统一错误码/request_id、审计、迁移 0068、Runtime/Browser 验收。

### 3.2 明确不做

- 不接收、上传、持久化或回放 raw Prompt/Response 样本。
- 不做离线评测集、LLM-as-a-judge、人工标注平台、统计显著性推断或自动训练。
- 不做多臂老虎机、探索率自适应、动态权重、自动赢家或 Alias 自动发布。
- 不做三个以上 variant、跨 tenant 实验、跨 Project/Environment 混合统计。
- 不覆盖 Policy `route_to`，不实验 Batch/内部 Chat/Embeddings。
- 不新增暂停、取消、删除、修改或重开 API；实验创建后配置不可变并自动收口。
- 不把 OpenRouter、硅基流动加入默认上游；variant 只引用租户既有模型目录。默认上游边界仍是
  智谱、通义、月之暗面、文心、火山、混元、DeepSeek 官方/云服务及客户本地
  OpenAI-compatible Endpoint。

## 4. 权限、用户与租户边界

### 4.1 RBAC revision 11

新增两个独立权限：

| 权限 | 风险 | 默认角色 | 用途 |
| --- | --- | --- | --- |
| `experiment.read` | read | admin、developer、ops_admin | 列表、详情和权威结果 |
| `experiment.write` | critical | admin、developer、ops_admin | 创建影响在线流量的实验 |

产品人员在现有固定角色中由 developer/ops_admin 承载；租户可在产品授权上限内收窄矩阵。
运行时始终从数据库矩阵校验，不能用 Shared 默认角色集合或前端隐藏按钮补权。读取依赖
`experiment.read`；创建使用专用 composite dependency，要求 `experiment.write`、`project.read`、
`environment.read`、`routing.read`、`model.read` 全部命中。默认 admin/developer/ops_admin 已具备该组合，
自定义矩阵撤掉任一依赖即失去创建能力。认证、tenant 和权限在业务解析 path/query/body 前执行；创建
事务内再次检查同一五权限快照，读取事务内再次检查 `experiment.read`。跨 tenant、不存在和不可见对象
同形返回 404。

### 4.2 Owner 图

- Experiment、Assignment、Execution、Outcome、Settlement、Feedback Attribution、Result、Event 和 Outbox
  全部显式带 `tenant_id`。
- Project、Environment、Alias、Model A/B 必须同 tenant；Project/Environment 必须是 Alias 的
  当前合法作用域，两个 Model 必须 active、不同且支持 Chat。
- Gateway scope 只取认证 Virtual Key 的 tenant/project/environment；客户端 body/header 不能声明
  experiment、variant、model ID、tenant 或 actor。
- Assignment owner 为 `(tenant_id, experiment_id, request_id)`。Gateway 分桶时终态 RequestLog 尚未
  创建，且现有 `gateway_request_claims` 没有认证时的 Project/Environment scope，因此 0068 新增与 claim
  一一对应的不可变 `gateway_request_scope_claims`。认证事务在读取正文前同时冻结 tenant/request/trace/
  Virtual Key、project/application/environment ID 与 environment code；Assignment 必须以 deferred FK 引用该
  scope claim，不能在后续反查可变 Virtual Key。RoutingDecision 及后续 RequestLog、RoutingAttempt/
  StreamSession、Usage 和 FinOps 只能沿 claim/scope-claim/Assignment 图收口，不能把未来才出现的 RequestLog
  当父表，也不能把认证后的 Key 改绑冒充原始 scope。

## 5. 公开 API 合同

所有成功响应带 `request_id`、`trace_id`；所有错误使用全局统一结构并带 request_id。实验路由成功、
401、403、404、409、422 和 5xx 均为 `Cache-Control: no-store, private`。请求采用 exact-set，
拒绝未知字段、重复 JSON key、float/指数/NaN/Infinity 和客户端身份字段。POST 仅接受单个 UTF-8
`application/json` object，body 上限 16 KiB；GET 禁止 body，path ID 使用既有安全 ID 规范，query 重复键、
未知键、空白/编码变体均拒绝。JSON 最大深度 8、节点 128，超限与语法错误同形为 input invalid。
POST 首次与同 body 幂等重放都返回 201，三个 GET 返回 200。

### 5.1 `POST /experiments`

依赖上述五权限 composite guard 和 canonical lowercase RFC 4122 UUIDv4 `Idempotency-Key`。鉴权后、读取
body 前必须从 ASGI raw headers
证明该 Header 精确出现一次、ASCII、长度有界且 raw 值与框架 state 完全相同；重复行、逗号合并、
代理替换、空白变体和非规范 UUID 均返回固定输入错误。严格请求：

```json
{
  "name": "August quality comparison",
  "project_id": "project_...",
  "environment_id": "env_...",
  "model_alias_id": "alias_...",
  "model_a_id": "model_...",
  "model_b_id": "model_...",
  "target_sample_count": 1000,
  "traffic_sample_bps": 2500,
  "model_a_allocation_bps": 5000,
  "sampling_duration_seconds": 86400,
  "feedback_collection_enabled": true,
  "feedback_window_seconds": 86400
}
```

- `name` 为 1..120 可见字符，禁止控制字符；不进入日志、Webhook 或验收诊断。
- `target_sample_count` 为 2..100000；`traffic_sample_bps` 为 1..10000；
  `model_a_allocation_bps` 为 1..9999，B 为 `10000-A`。
- A/B 目标配额使用整数 half-up 从目标样本数计算，并要求双方至少 1；最终目标样本满额时精确
  命中配额，而不是把小样本随机偏差冒充“比例可控”。
- `sampling_duration_seconds` 为 60..604800。
- feedback 开启时 window 为 60..604800；关闭时必须为 0，结果反馈字段固定为零并显式标记关闭。
- Cost/Usage 结算宽限不由客户端控制。创建事务把部署配置
  `experiment_settlement_grace_seconds`（60..86400）冻结到 Experiment，避免配置漂移改写已开始实验。
- 创建时验证 active tenant/project/environment/alias/model/provider/endpoint owner 图和 Chat 能力；
  运行时仍逐请求重验，不能把创建时状态当永久授权。
- 同一作用域最多一个 `active` 实验；`collecting_feedback` 的旧实验不再接流量，可与新 active
  实验并存，但 Assignment 的 request owner 不重叠。
- 同一 actor + Idempotency-Key + canonical body 重放返回同一 Experiment；不同 body 返回
  `experiment.idempotency_conflict`。创建、不可变配置、管理审计在同一事务。
- 原始 Idempotency-Key 必须是 canonical lowercase UUIDv4，只在当前请求内存中使用且 `repr=False`。持久化
  `idempotency_key_hash=SHA256("metahub/experiment/idempotency-key/v1<NUL>" || canonical ASCII JSON)`，
  JSON exact 字段为 tenant_id、actor_user_id、idempotency_key；唯一约束为
  `(tenant_id,created_by,idempotency_key_hash)`。`request_fingerprint` 使用 UUID UTF-8 作为 HMAC key，计算
  `HMAC-SHA256(key_uuid,"metahub/experiment/create-request/v1<NUL>" || canonical_body)`；canonical body
  是验证后的 exact JSON，不含 request_id/trace_id。UUID 具有 122 位随机熵，两种摘要均不依赖部署 Auth root，
  所以 Experiment 完成后合法轮换 Auth root 仍命中旧幂等记录；active 时仍按采样指纹合同禁止轮换。raw key、
  canonical bytes 和 digest 不进入日志、
  API、Webhook、审计或验收证据。

成功返回完整安全配置、状态、A/B 目标配额、`assigned_count=0`、`idempotent_replay` 和链路 ID，不返回
分桶 secret、HMAC fingerprint、Endpoint 或任何正文。幂等重放返回同一 Experiment ID 与首次冻结配置，
但响应 envelope 的 request_id/trace_id 始终属于本次 HTTP 调用；Experiment 独立冻结首次创建的
`operation_request_id`、`operation_trace_id`，只用于管理审计和未来完成事件的 source owner，不能被重放请求
覆盖。

### 5.2 `GET /experiments`

PAGE-105 工程派生的恢复接口，依赖 `experiment.read`。支持 exact `status`、`limit=1..100` 和
私有 HMAC cursor；cursor canonical owner 精确包含 schema version、tenant、status filter、最后
created_at/experiment ID，并以独立域 HMAC，换 tenant/filter 或篡改一律 query invalid。稳定按
`created_at DESC, experiment_id DESC`。列表只返回配置和安全进度计数，
不返回 Assignment/request ID 明细、私有 cursor 正文、分桶值或结果拼装素材。

### 5.3 `GET /experiments/{experiment_id}`

PAGE-105 工程派生的详情恢复接口，依赖 `experiment.read`。返回不可变配置、状态、计数、时间和
版本；跨 tenant/不存在同形 404。该接口不返回 Assignment 列表。

### 5.4 `GET /experiments/{experiment_id}/results`

依赖 `experiment.read`：

- `active`/`collecting_feedback` 返回同一只读 SQLite snapshot 内计算的 provisional A/B 统计，
  `is_final=false`，并返回 `facts_through_at`、pending/unresolved 数和下一状态时间。
- `completed` 只读两条不可变 Result，交叉核对 Experiment 版本/计数后返回 `is_final=true`。
- endpoint 使用单 connection、`query_only`、显式 read transaction、单次 now 和绝对 250ms deadline；
  busy、超时、owner 图异常或整数溢出固定 `experiment.statistics_unavailable`，不能返回部分统计。
- 结果不分页，也不返回单请求明细，避免页面从不完整样本自行重算。
- 结果可返回每个 arm 的安全 model/provider ID/code，但不返回 sampling key、route-config fingerprint、
  Adapter catalog hash、base URL、upstream model、bucket 或任何请求级 owner。
- 同一 Control 进程的 Experiment summary 使用独立单并发门禁并在线程中执行；门禁等待、线程调度、
  SQLite progress handler 和 DTO 构造共享路由进入时的同一绝对 deadline。HTTP waiter 超时后真实线程
  退出前不能提前释放槽位，防止遗留扫描与下一租户并发争用 SQLite。

## 6. 状态机、执行恢复与时间边界

Experiment 的公开状态保持最小且单向：

```text
active --目标样本已满或 sampling_deadline 到期--> collecting_feedback
collecting_feedback --反馈围栏关闭、执行与结算全部收口--> completed
```

- 创建时 `started_at=created_at`，`sampling_deadline=created_at+duration`。Gateway Assignment 事务和
  scheduler 都在 `BEGIN IMMEDIATE` 内重验数据库当前时间；满额或到期只允许一个 CAS 写入
  `sampling_ended_at`。到期请求先收口再绕过实验，scheduler 延迟不能扩大采样窗口。
- `feedback_deadline=sampling_ended_at+feedback_window`；关闭反馈时等于 `sampling_ended_at`。部署冻结
  `experiment_execution_max_seconds`，每条 execution 的绝对 deadline 为 Assignment time 加该上限；
  `settlement_deadline=max(feedback_deadline,sampling_ended_at+execution_max+settlement_grace)`，保证最后一条
  Assignment 也有完整执行与账务恢复时间，两臂始终获得相同反馈窗口。
- Feedback 是否进入实验不再依赖调用方在取写锁前生成的 `request_feedbacks.created_at`。反馈 INSERT
  trigger 在取得同一 SQLite 写锁后，用数据库时钟和 `feedback_fence_closed_at IS NULL` 决定是否插入
  不可变 attribution；scheduler 到期后先关闭 fence，再聚合 attribution。反馈事务若在 deadline 前取时、
  deadline 后才取得锁，将因数据库当前时间已越界而不归因，但全局 Feedback 仍照常提交。

每条 Assignment 同事务创建独立执行状态，而不是无限等待未来的 RequestLog：

```text
assigned --最终发送许可 CAS--> provider_calling --正常/异常终结--> terminal
   |                                 |
   +--租约过期恢复--> terminal       +--租约过期恢复--> terminal
```

- Assignment 事务把 execution 初始化为 `status=assigned`、`generation=1`，`lease_owner_id` 使用 Gateway
  进程启动时生成的有界稳定 owner，`lease_token` 使用每请求随机 UUID 且 `repr=False`，
  `lease_expires_at=min(database_now+execution_lease,absolute_execution_deadline)`。事务只有在能返回完整
  `ExperimentExecutionHandle(tenant/experiment/assignment/execution/request/generation/owner/token/deadline)`
  时才提交；token 不进入 RequestLogContext、日志、API、Webhook 或异常。
- Assignment 提交后立即启动 request-owned heartbeat，贯穿 capability、DLP、Intent、Rate Limit、Budget、
  Output Safety prepare、Provider Key/Adapter 构造、Attempt/Stream 创建、非流 await、流握手和整个流 body。
  renew 只按 execution/generation/owner/token/未过期 lease CAS，且不得越过 absolute deadline；renew 失败或
  deadline 到期立即取消本地等待并进入固定终结，不能继续向客户端发送新内容。
- `assigned` 表示尚无上游暴露；通过 capability、DLP、Intent、Rate Limit、Budget、Provider Key/Adapter
  构造后，Gateway 必须以 owner/generation/lease CAS 取得最终发送许可，并在调用 Adapter `send` 前立即
  重验，才能进入 `provider_calling`。发送许可失败时禁止调用 Provider。
- 请求最外层持有 execution handle，覆盖 `create_routing_attempt`、`create_stream_session` 位于内部 try 外、
  `CancelledError`、统一 ApiError 和未知异常等所有 post-assignment 出口。发送许可前的已知失败同步写
  failed/not_exposed Outcome；取消期间用有界 shield 尝试终结，无法提交时只由 lease reaper 接管。
- 正常路径只在**客户端可见逻辑终态**已经确定后，把 execution 终态和 append-only outcome 同事务提交。
  非流必须经过 Response normalization、Output Safety、RequestLog 和 Usage durability；流必须在 shielded
  body finalizer 已确定 StreamSession、RequestLog、Usage durability 与最终 SSE error 后提交。Provider 返回
  或 Attempt 标 succeeded 只产生 exposure/quality 候选事实，绝不能提前冻结 gateway success。outcome 区分
  `not_exposed|exposed|unknown`，以及 `success|failed|unresolved`，不从 Attempt/Stream 行是否存在猜测出网。
- reaper 处理的 execution 在租约和绝对期限后仍为 nonterminal，本身就证明 Gateway 没有提交客户端逻辑
  终态；因此无论 RequestLog、Attempt/Stream 和 Settlement 看起来多么完整，都不得恢复 gateway success。
  `assigned` 固定为 `abandoned_before_provider/not_exposed`，`provider_calling` 可从终态 Attempt/Stream 保留
  独立的 exposure/quality 事实，但 Gateway Outcome 只能为 failed 或 unresolved，绝不能被 Provider 成功
  抬升为成功。
- reaper 在同一事务内按 tenant/request/model/provider 以及 execution owner 完整匹配，把残留
  `request_logs=success` 改为 `failed/gateway.experiment_unavailable`，再提交唯一非成功 Outcome；改写不匹配
  时失败关闭，不能基于部分 owner 推断。recorded Settlement 和 Provider quality 可作为独立事实保留，
  但都不是客户端收到正文或 `[DONE]` 的证据。Usage spool 重放若发现同 owner Experiment Assignment 且
  RequestLog 缺失，也必须补固定 failed 日志；普通非实验请求继续保持既有 success 兼容行为。
- Gateway 以同一 absolute deadline 约束整个非流/流调用。非流或流握手在下游响应头发出前到期，关闭
  上游并返回 504 JSON `gateway.experiment_execution_timeout`；`StreamingResponse` 已返回后 HTTP 保持 200，
  body 关闭上游并发送现有 exact SSE error envelope（含固定 code、request_id、trace_id），随后结束生成器，
  不伪造 JSON 504 或 `[DONE]`。StreamSession 写 `timed_out`、RequestLog 写 failed/同 error code，实验
  Outcome 写 failed；已取得上游 2xx 握手时 exposure=exposed，但该本地 absolute cap 的 quality eligible=false。
  旧 generation 即使进程恢复也
  不能提交 outcome；每次真正发送前及每次流读取循环都校验 deadline/generation。外部 Provider 不支持
  数据库 fencing，因此超时崩溃只能保守分类为 unknown，不能宣称 exactly-once 上游调用。
- RequestLog、Attempt/Stream、Usage 和 FinOps 仍是全局审计事实，但它们缺失不再让实验永久卡死。
  到硬截止时 pending usage settlement 原子转 `unavailable`；迟到全局日志、Usage、FinOps 或 Feedback
  仍可恢复各自账本，但 completed Experiment 的 outcome、settlement、attribution、Result 和事件不改写。
- finalizer 分三段运行：第一段短写事务 claim lease，并在到期时关闭 feedback fence、reap 到期 execution、
  把到期 pending settlement 收口；第二段在租约外读取这些变化之后的 snapshot 和 `facts_version`；第三段
  短写事务重验相同 facts version/owner/generation/lease，插入 A/B 两条 Result，插入唯一 source
  Event/outbox，最后 CAS Experiment 为 `completed`。事务失败全部回滚，不形成 parent completed 与 Result
  相互等待的循环，也不拿收口前的旧快照生成结果。

## 7. 确定性采样与 Gateway 路由

### 7.1 HMAC 精确合同

Shared 纯函数复用部署已有 `auth_token_secret`，使用不可替换域
`metahub/experiment/sample/v1<NUL>` 和 `metahub/experiment/variant/v1<NUL>`；`<NUL>` 精确表示单个
`0x00` 字节，不是两个字符 `\\0`，实现使用 bytes literal `b".../v1\x00"`。canonical bytes 冻结为：

```text
ASCII JSON，key 按字典序，separators=(",",":")，ensure_ascii=true
字段精确为 algorithm_version、experiment_id、request_id、tenant_id
HMAC-SHA256(key_utf8, domain || canonical_json)
bucket = int.from_bytes(digest[0:8], "big", signed=false) % 10000
```

三个 ID 只接受 1..128 位可见 ASCII、禁止空白/控制字符；secret、canonical bytes、digest 和 bucket
fingerprint 不进入日志/API/Webhook。sample bucket 小于 `traffic_sample_bps` 才候选进入实验。

第 `n` 个 Assignment 在 `BEGIN IMMEDIATE` 内保存 `assignment_sequence=n`，并按最终 A target/总 target
计算当前前缀允许的 A count floor/ceil：低于 floor 强制 A，达到 ceil 强制 B，区间内采用 HMAC 偏好。
持久原因精确为 `hmac_preferred|floor_forced|ceiling_forced`；任意前缀偏差不超过一个样本，满额精确命中
目标配额。HTTP 相同 `X-Request-ID` 重试仍由既有 gateway claim 返回 409；只承诺纯分桶函数稳定，
不宣称 HTTP 请求或 Assignment 可重放。

Experiment 保存 algorithm version 和不可逆 key fingerprint；Control/Gateway readiness 与每次 Assignment
均 constant-time 复核。存在 active 实验时部署不得轮换 Auth root；误轮换后服务固定 fail-closed，不能用
新 key 静默改变分桶。完成旧实验后才能按既有凭据轮换流程启用新 key。
fingerprint 域冻结为 `metahub/experiment/key-fingerprint/v1<NUL>`，计算结果为
`HMAC-SHA256(key_utf8, domain_bytes || canonical_ascii_json)` 的完整小写 hex，其中 `<NUL>` 同样是单个
`0x00` 字节，JSON 字段精确为 algorithm_version、experiment_id、tenant_id；它只用于内部一致性比较。

每个 arm 还冻结独立的 route-config fingerprint，域为
`metahub/experiment/route-config/v1<NUL>`，使用同一 Auth root 对 canonical ASCII JSON 做 HMAC-SHA256。
JSON exact 字段为 algorithm_version、tenant_id、model_id、model_status、provider_id、provider_code、
provider_status、provider_type、provider_auth_type、provider_base_url、upstream_model_name、canonical
capabilities、context_window、adapter catalog version/hash、provider-specific adapter spec ID 和 adapter version；
其中 base URL、upstream model 和完整 canonical bytes 只在内存中参与摘要，
不得进入 Experiment API、日志、Webhook、DOM 或验收证据。Provider Key/credential、动态健康和价格不进入
fingerprint：Key 可按既有安全轮换，健康仍逐请求检查，成本只从真实 Usage/FinOps 归因。active Experiment
已禁止 Auth root 漂移，因此 route-config fingerprint 可稳定复核；任一可执行配置变化都必须使该 arm
fail-closed，不能把不同 Provider/Endpoint 配置混入同一 Result。

### 7.2 Gateway 顺序

1. 认证事务在读取正文前同时写不可变 `gateway_request_claims` 与 `gateway_request_scope_claims`，冻结当时的
   Virtual Key project/application/environment scope；随后执行 Environment/Policy snapshot、Session Risk
   preflight、JSON exact validation。只有非 trusted、public Virtual-Key Chat 且冻结 scope 同时具有
   project/environment 才候选；认证后 Virtual Key 改绑不改变本请求 owner。
2. 复用 `validate_requested_model_authority` 后再精确解析请求模型身份。只有请求字符串命中当前 tenant/
   Environment 的 active canonical Alias，且 `model_alias_id` 等于 active Experiment 冻结值时才候选；现有
   direct concrete Model 请求明确旁路实验，继续普通 direct resolver，不能覆盖客户显式选模。Policy deny、
   `route_to`、Embeddings、Enterprise Chat 与 Batch 也均跳过实验。
3. Policy mask 后调用 Experiment route transaction。未采样继续原 `resolve_model_route_plan`；采样时同一
   `BEGIN IMMEDIATE` 写 RoutingDecision、Assignment、execution 和 usage settlement，并更新实验计数。
4. 实验事务不再调用普通 resolver。Experiment 已冻结每个 arm 的 provider ID/code 和 route-config
   fingerprint；运行时先重建同一 canonical fingerprint 并 constant-time 比对，再执行 exact 检查集合：
   concrete Model 与 Provider active、Model 支持当前 Chat/stream 形态、Provider 健康证据不过期、
   Endpoint/Adapter spec 可用，并在 Provider KeyPool 网络前门之前证明存在当前 tenant 可用 Key。Model 的
   provider/provider_model/capabilities 或 Provider 的 type/base URL/auth 等任一漂移都按 variant unavailable
   fail-closed，不能改派另一 arm。检查通过时
   直接返回冻结的 exact Model/provider/provider_code 单候选 `GatewayRoutePlan`；Assignment 唯一引用 `selected`
   RoutingDecision，reason 固定为 `experiment.variant_a|experiment.variant_b`，计划没有
   fallback、quality/cost/cache-affinity rerank。若冻结 variant 已状态/能力漂移，事务仍原子写入 `failed`
   RoutingDecision、Assignment、Execution、failed/not_exposed Outcome 和 not_exposed Settlement，再返回
   只含冻结 route owner 与固定错误码的失败 decision；调用方先绑定 RequestLogContext 后抛统一错误。
   Decision candidate summary 只含 experiment/assignment 本地 ID、variant、assignment reason 和 algorithm
   version，不含 sample/variant bucket、digest、key fingerprint、Endpoint 或请求正文。
   成功 route 或失败 decision 都必须在提交前构造完整安全 owner，返回后立即投影到 RequestLogContext；
    DTO 失败必须回滚，不能留下已提交 Assignment 却没有 model/provider owner 的半图。
   若 SQLite、schema、HMAC fingerprint 或 active owner 图不可用，Gateway 无法证明当前 scope 没有实验，
   必须在 Provider 前以 `gateway.experiment_unavailable` 失败关闭；只有权威查询证明“无 active 实验”或
   HMAC sample bucket 未命中时才允许继续普通 resolver，不能把数据库异常降级为静默旁路。
5. 返回的 execution handle 立即进入最外层生命周期 guard，再继续 capability、DLP、Intent、Rate Limit、
   Budget、Output Safety、Provider Key 与 Adapter 构造。任一上游前失败把 outcome 终结为
   failed/not_exposed；只有最终发送许可 CAS 成功才允许 Adapter send，所有阶段同时受 heartbeat/deadline。
6. Provider 返回或规范上游错误只冻结独立 exposure/quality 候选；非流在 Output Safety 与 Usage durability
   后、流在 shielded body finalizer 后，才按最终客户端逻辑状态终结 execution/outcome。Attempt/Stream 是
   验证证据而非“已出网”的替代判断。实验请求只允许 sequence-1 primary；计划固定
   `initial_role=primary`，fallback policy/route/cache-affinity 均为空，不得创建 sequence 2。
7. terminal RequestLog 写入失败发生在 Provider 之后时只能安全告警并由恢复器分类，不能倒置因果声称已
   阻止调用；只有 claim、Assignment、Attempt/Stream 和最终发送许可等前置门失败才禁止 Provider。

Variant 创建时 active、运行时不可用或能力漂移固定返回
`gateway.experiment_variant_unavailable`，并在选择 arm 的同一事务保留 Assignment、failed Decision、
failed/not_exposed Outcome、not_exposed Settlement 和尽力 RequestLog；不得偷偷改派另一 variant。
Provider 已暴露后的失败同样禁止跨 variant fallback。

Exposure 与 PRD-048 quality eligibility 是两个互不替代的轴，矩阵精确冻结为：

- 发送许可前失败，或 send 后 `NormalizedProviderError.transport_kind='local_configuration'` 且没有 HTTP
  status/evidence，一律 `not_exposed`。
- 非流返回 `NormalizedResponse`、流返回上游 2xx `ProviderStreamResponse`，或规范错误携带可信
  `ProviderErrorEvidence.upstream_http_status`，均为 `exposed`。因此 400/401/403 虽通常不计模型质量，仍可
  证明暴露；流 active 后的断连、Output Safety 阻断或本地收口失败仍为 exposed。
- send-fence 后的 timeout/network/other transport、取消、进程消失或无 HTTP status 的异常均为 `unknown`；
  ConnectTimeout/网络超时即使符合 PRD-048 quality error 白名单，也不能冒充 exposed。
- quality eligible/succeeded 只从现有 RoutingAttempt/StreamSession 终态矩阵取得，并另要求
  `exposure=exposed` 才进入本实验质量分母。不能从 exposure 推导 quality，也不能用
  `is_upstream_cost_exposure_possible` 的“可能计费”布尔替代确定 exposure。

## 8. 数据库设计（0068）

### 8.1 `model_experiments`

核心字段：`experiment_id`、`tenant_id`、name、project/environment/alias/model A/B、每个 arm 的冻结
provider ID/code 与 route-config fingerprint、target/sample/A bps、A/B target count、feedback 开关/window、
algorithm version、sampling key fingerprint、status、assigned/A/B count、`facts_version`、feedback fence、
finalization owner/generation/lease、created_by、`idempotency_key_hash`、`request_fingerprint`、
`operation_request_id`、`operation_trace_id`、execution max、started/sampling deadline/ended、feedback/settlement
deadline、completed/created/updated。`(tenant_id,created_by,idempotency_key_hash)` 唯一；operation owner 和两个摘要
创建后不可变，重放只比较 request fingerprint 并返回原 Experiment。所有整数使用 `typeof='integer'` 与范围
CHECK，时间为规范 UTC Z，owner 全为 tenant-qualified FK。partial unique 保证同 scope 最多一个 active；
配置字段创建后不可变，计数和状态只能由受约束事务单向推进。
Assignment/Outcome/Settlement transition/Feedback Attribution 和 feedback-fence close 都由数据库 trigger
把 `facts_version` 精确 +1；lease/heartbeat 不改变事实版本。counter 达 signed-64 上界时写入整体失败关闭。

### 8.2 `gateway_request_scope_claims`、`experiment_assignments` 与 RoutingDecision

`gateway_request_scope_claims` 与现有 `gateway_request_claims` 一一对应，字段为 tenant/request/trace/
Virtual Key、project/application/environment ID、environment code、created_at。认证 claim 事务必须同时插入；
表以 tenant-qualified composite FK 分别引用 parent claim、Virtual Key、Project、可选 Application 和
Environment；insert trigger 在同一 snapshot 反查 Virtual Key 当前 owner/scope、Application-Project 关系与
Environment code，并要求 trace/key 完全一致，随后禁止
UPDATE/DELETE/REPLACE。历史 claim 可以没有 scope child，但 0068 上线后的认证路径缺 child 必须在正文读取前
失败关闭；Experiment Assignment 只接受 project/environment 非空且与 Experiment 冻结 scope exact 相等的
scope claim。Virtual Key 后续改绑、禁用或删除均不能改写已冻结 owner。

Assignment 字段：ID、tenant/experiment/request/trace/Virtual Key、`routing_decision_id`、`execution_id`、
`usage_settlement_id`、
`assignment_sequence`、variant、冻结 model/provider/provider_code、sample/variant bucket、
`assignment_reason`、algorithm version、created_at。唯一 `(tenant_id,request_id)`、
`(tenant_id,experiment_id,assignment_sequence)` 和 routing decision；禁止 UPDATE/DELETE/REPLACE。

INSERT trigger 反查 active Experiment、project/environment/alias、A/B exact owner、prefix floor/ceil、不可变
`gateway_request_claims` 的 tenant/request/trace/Key，以及 `gateway_request_scope_claims` 的 project/application/
environment owner；不得重新读取当前可变 Virtual Key scope。RoutingDecision 必须在同事务先插入，
tenant/request/trace/Key/alias/model/provider/provider_code 与 Assignment 完全一致，固定 reason 和
安全 candidate summary；可执行 arm 为 selected，无效 arm 为 failed 且仅允许固定 variant-unavailable 错误。
Assignment 以 tenant-qualified FK 唯一引用它。后续 RequestLog 可从缺失进入终态，
但任意非空 scope/model/provider 必须一致。Attempt/Stream 只能绑定该 arm，非流只允许 sequence 1 primary、
无 fallback policy。

Assignment 对 Execution 和 Settlement 使用**反向 deferred closure FK**：parent 保存两个预生成且 NOT NULL 的
child ID，并分别以 `(tenant_id,assignment_id,child_id)` 引用 child 的 tenant-qualified UNIQUE key，child 同时
正常引用 Assignment；三个对象必须在同一事务插入，只有两条 child 均存在且 owner 完全一致才能 COMMIT。
这样直接 SQL、异常分支或 `INSERT OR REPLACE` 都不能提交“已消耗样本却没有 reaper/结算状态”的半图。
parent/child 两侧 immutable、rowid 与 conflict trigger 共同封住 UPDATE、DELETE、REPLACE；事务回滚同时撤销
Experiment counter 和 `facts_version` 增量。

### 8.3 `experiment_request_executions` 与 `experiment_request_outcomes`

Execution 与 Assignment 一一对应，字段含 execution/tenant/assignment/request、
`assigned|provider_calling|terminal`、generation、owner、lease、absolute execution deadline、send-fence time、
created/updated。Assignment 事务创建 generation 1，并在提交前一次性冻结 Gateway 进程级
`lease_owner_id`、请求级随机 `lease_token`、`lease_expires_at=min(database_now+execution_lease,
absolute_execution_deadline)`；只有能同时返回包含 tenant/experiment/assignment/execution/request、generation、
owner、token 和 deadline 的完整 execution handle 时才允许提交。token 只存在于 `repr=False` 的内部 handle，
不得进入日志、RequestLogContext、API、Webhook、审计或验收证据。

Assignment 提交后立即由最外层 request-owned heartbeat 持有该 handle。heartbeat 覆盖 capability、DLP、Intent、
Rate Limit、Budget、Output Safety prepare、Provider Key/Adapter 构造、Attempt/Stream 创建、非流 await、流握手和
整个流 body；每次续租都按 execution/tenant/generation/owner/token、当前状态、未过期 lease 和 absolute deadline
做单行 CAS。最终发送许可同样按该 owner 组合 CAS，把 `assigned` 线性化为 `provider_calling` 并写 send-fence；
CAS 或续租失败、lease 已过期或 deadline 到期后必须取消本地等待，禁止继续调用 Provider 或向客户端发送新内容。

Outcome 与 Execution 一一对应且 append-only，字段含 gateway `success|failed|unresolved`、exposure
`not_exposed|exposed|unknown`、固定 outcome reason/error code、可选 Attempt/Stream owner、quality
eligible/succeeded、非负 latency、recovery generation、facts time 和 created_at。Provider 返回、Attempt
成功或 Stream active 只能形成 exposure/quality 候选，不能冻结 Gateway Outcome。非流必须等 response
normalization、Output Safety、RequestLog 和 Usage durability 得出最终公开响应；流必须等 shielded body
finalizer 得出 StreamSession、RequestLog、Usage durability、最终 SSE error/断连状态，才允许在同一终结事务
先插入 Outcome、再按 generation/owner/token CAS Execution 为 terminal。Output Safety 阻断或 Usage 主库与
spool 双失败必须覆盖较早的 Provider success，不能被实验误记为 gateway success。

Execution 的 terminal trigger 要求同 owner 的 Outcome 已存在，避免循环 FK。Outcome trigger 校验枚举矩阵及
可选 Attempt/Stream 的 tenant/request/model/provider/终态；正常终结、`ApiError`、独立 `CancelledError`、未知
异常和 reaper 都必须使用最外层同一 handle。reaper outcome 只允许有效过期 generation 和两个固定恢复原因；
旧 generation 永远不能补写或覆盖 Outcome。两表不保存正文、Endpoint、凭据或原始异常。

### 8.4 `experiment_usage_settlements` 与 `experiment_feedback_attributions`

每个 Assignment 同事务创建一条 settlement：`pending|recorded|not_exposed|unavailable`，并保存可选
usage/FinOps owner、gross usage-charge CNY micros、normalization status、固定 reason 和 version。上游前
终态同事务转 not_exposed；已知无 usage 的真实失败转 unavailable；成功或暴露不明保持 pending。
正式 `finops_ledger_entries(entry_type='usage_charge')` 的 direct/spool-replay INSERT trigger 按 tenant/request/
model/provider 唯一 owner 把 pending 转 recorded。gross 口径只计原始 usage_charge debit；当前 0041/0067
schema 同时强制该类 entry 的 `quota_type='paid'`，0068 不放宽此合同。manual credit、gift/trial credit、
reversal 或后续 adjustment 均不冲减实验模型成本；只有 normalized CNY 进入成本总额，
已入账但 normalization unavailable 进入 unknown。completed 后 trigger 不改 settlement，但不阻止全局账本。

Feedback attribution 与 request_feedback 一一可选，字段含 tenant/experiment/assignment/feedback/variant/
rating/accepted、数据库归因时刻。`request_feedbacks AFTER INSERT` 在同一写锁内仅当 Assignment owner 匹配、
反馈开启、fence 未关闭，且 sampling 尚未结束或 statement 内的 database now 严格早于 feedback deadline 时
`INSERT ... SELECT`；不满足时零行，
绝不回滚合法全局 Feedback。Attribution 禁止 UPDATE/DELETE/REPLACE，结果只聚合 attribution，不再按
`request_feedbacks.created_at` 重新判断截止。

### 8.5 `experiment_results`

每个 completed Experiment 精确两行 A/B：Result/tenant/experiment/variant，以及从 Experiment arm 冻结值
复制并由 trigger 复核的 model/provider/provider_code/route-config fingerprint、assigned、
gateway success/failure/unresolved、exposed/not-exposed/unknown、quality success/failure、两类 success bps、
`latency_sample_count/total_latency_ms/average_latency_micros/p95_latency_ms`、settlement
recorded/not-exposed/unavailable、gross normalized CNY、cost known/unknown、feedback enabled/count/rating/
accepted/adoption、statistics/facts version、facts through、created_at。average micros 由整数
`total_latency_ms*1000/sample_count` half-up 得到，零样本为 null；p95 使用 latency_ms 的 nearest-rank。
Result 只在有效 finalizer 事务内 insert，禁止 UPDATE/DELETE/REPLACE；同一 arm 的全部 Assignment 必须匹配
Experiment 冻结的 model/provider/route-config owner，两臂合计必须与 Experiment 和不可变事实版本一致。
provisional 与 final 复用同一聚合函数，不从当前 Model/Provider 配置、ModelQualityScore 或价格回算历史。

### 8.6 `experiment_events` 与 `experiment_event_outbox`

完成事务插入唯一 source event 和 pending outbox。Event ID 使用 `exev_` 加 32 位小写十六进制，首次完成
事务生成后持久化；唯一 `(tenant_id,experiment_id,event_type)` 保证重试复用该事实，不在重试时生成替代 ID。
`experiment.completed` payload 为 exact-set：

```json
{
  "schema_version": 1,
  "experiment_id": "exp_...",
  "project_id": "project_...",
  "environment_id": "env_...",
  "model_alias_id": "alias_...",
  "model_a_id": "model_...",
  "model_b_id": "model_...",
  "status": "completed",
  "completion_reason": "target_reached",
  "assigned_count": 1000,
  "model_a_assigned_count": 500,
  "model_b_assigned_count": 500,
  "gateway_success_count": 970,
  "gateway_failure_count": 25,
  "unresolved_count": 5,
  "cost_unknown_count": 3,
  "feedback_count": 240,
  "facts_version": 3241,
  "facts_through_at": "2026-08-09T01:02:03Z"
}
```

`completion_reason` 只允许 `target_reached|sampling_deadline_reached`；所有计数为有界非负整数且可从
Experiment 与两条 Result 精确重建。payload 不含 name、请求明细、Prompt/Response、Endpoint/Key 或 HMAC
值。envelope 的 actor/object/source request/source trace/occurred_at 分别严格绑定 created_by、Experiment、
创建 operation owner 和 source Event created_at；pending 恢复与死信重放必须联表重建并逐字段比对。Outbox
复用 PRD-073/074 的 90 秒 lease、generation、owner、CAS、指数退避和 source 恢复；空订阅也形成已完成
投递意图。网络投递不在结果事务中执行。

### 8.7 Scheduler state

`experiment_scheduler_tenants` 保存 tenant 游标/generation，保证多 Control 副本公平扫描。Scheduler 先用
短事务 claim Experiment lease，再在租约外只读 snapshot 聚合；提交事务按 owner/generation/lease/status/
`facts_version` 和所有 fence 重验。claim 短事务先完成到期 fence/reaper/settlement 转换，再释放写锁读取
转换后的 snapshot；事实变化则放弃快照重算，过期 lease 可接管，旧 owner 不可提交。
Scheduler 还负责 execution reaper、时间状态迁移、settlement 硬截止、Result 冻结和 outbox 恢复，不调用
Provider。每次 batch 和每 tenant 工作量有界。
该表只含可重建的调度 cursor/lease，不含业务结果；无 active lease 且所有实验/外部事实为空时 down 可删除，
active lease 或任何业务事实存在时必须先由全局 down guard 阻断。

## 9. 统计口径

所有比率使用共享 arbitrary-int half-up，数据库只存整数。Gateway/quality success bps、平均延迟和平均
成本的分母为零返回 `null`；Feedback 必须保持 PRD-049 兼容：`average_rating_milli` 与
`adoption_rate_bps` 在 feedback_count 为零时返回 0，而不是 null。具体口径为：

- assigned：不可变 Assignment 数。
- gateway success/failure/unresolved：只读 append-only Outcome；success rate 分母为 success+failure，
  unresolved 单列，绝不从缺 RequestLog 推断失败。
- exposed/not-exposed/unknown：只读 Outcome 的显式 send-fence 分类。Attempt/Stream 行存在只表示建立执行
  owner，不能独立证明出网。
- quality success/failure：只计 exposure=exposed 且现有 PRD-048 终态矩阵认可的 quality eligible outcome；
  DLP、预算、Key、Adapter、URL、协议及 unknown 暴露不进入模型质量分母。
- latency：只计 quality eligible outcome 的非负 latency，返回样本、总量、half-up average micros 与
  nearest-rank p95 ms；本地预检耗时不污染模型延迟。
- cost：按 Settlement 统计 recorded/not-exposed/unavailable/pending。只累加 recorded 且 normalized 的 gross
  CNY micros；recorded 但 normalization unavailable、硬截止 unavailable 与 unknown 暴露进入
  `cost_unknown_count`，不能显示为零。平均成本只除以 cost-known exposed 请求。
- feedback：只计不可变 Attribution，返回 count、rating sum、average rating milli、accepted count 和
  adoption bps；adoption 精确为 `accepted_count/feedback_count*10000` half-up，零分母为 0，与 PRD-049
  一致。晚反馈仍在全局报表，但不会进入冻结实验。
- statistics version 精确绑定 Experiment `facts_version` 和 assignment/outcome/settlement/attribution 计数；
  不接受客户端总计，也不从当前价格、ModelQualityScore 或当前模型状态回算。

Provisional 与 final 使用同一 DTO 和聚合器；provisional 显示 execution/settlement pending。final 要求
execution 全 terminal、settlement pending=0、feedback fence closed，并记录 completion reason。所有求和先
做 signed-64 上界预检，任何溢出、owner 缺失或 facts version 漂移整体失败关闭，不能返回部分统计。

## 10. 并发、恢复与隔离

- 创建使用 `BEGIN IMMEDIATE`、scope partial unique 与 actor/idempotency fingerprint；未知提交可安全重放。
- RoutingDecision、Assignment、execution、settlement、实验计数和满额状态迁移同事务；不能先选模型再
  异步补归因。
- 所有 Assignment 后的正常/异常路径都尝试终结 execution/outcome；进程在任何指令间崩溃时由 lease reaper
  恢复或保守写 unknown，不依赖仓库并不存在的通用 RequestLog 恢复器。
- Provider 发送前的 claim/Assignment/Attempt-or-Stream/send-fence 均为 fail-closed；Provider 返回后的
  RequestLog、Attempt/Stream 或 Usage 辅助写失败只能告警、spool 或恢复，不能覆盖真实客户端结果。
- Feedback INSERT 与 scheduler close-fence 共享 SQLite 写锁顺序；deadline 前取时但 deadline 后提交的
  反馈不能穿透。FinOps direct/spool replay 与 settlement transition 同样由数据库 trigger 串行化。
- Scheduler 使用 tenant cursor、lease/generation/CAS；长聚合不持 write lock，提交时只接受相同
  facts_version。Result A/B、source Event/outbox、parent completed 在一个事务按固定无环顺序提交。
- GET results 使用单只读 snapshot；completed 后不从迟到全局事实重算。两个 tenant 即使复用相同文本 ID
  仍由每条 tenant-qualified FK/查询隔离；跨 tenant与不存在同形 404。

## 11. Webhook 与审计

- 新增 `experiment.completed` 到共享 Webhook allowlist、订阅 API、HMAC wire 和投递器。
- payload version 固定；签名覆盖 canonical bytes；重试保留同 event ID、created_at 和 payload。
- source event 的 `source_request_id/source_trace_id` 精确复用 Experiment 首次创建时分别持久化的
  `operation_request_id/operation_trace_id`，actor 固定为 created_by user，object 为 experiment ID；幂等重放
  的新 request/trace、scheduler 身份、lease owner 和 worker ID 均不得覆盖或冒充业务 source。Shared exact
  payload validator、Control pending 恢复和死信重放都必须从
  Experiment+两条 Result+Event owner 图重建并逐字段比对，不从页面 DTO 或 outbox 临时状态拼装。
- Event payload 使用 8.6 的 19 字段 exact-set；Shared validator、首次投递、pending resume 和 dead-letter
  replay 共用同一构造器。任一字段、前缀、计数和、completion reason、facts version 或 source owner 不一致
  都失败关闭，不投递近似 payload。
- Control 停机、空订阅、投递 5xx、超时和进程重启均由 source/outbox恢复。
- 创建 AuditLog 只含 experiment ID、scope ID、模型 ID、bps、target、duration、feedback 开关和 actor ID；
  不记录 name 或请求正文。Gateway Assignment 日志只含 tenant/request/experiment/variant/model 和固定码。

## 12. 错误码与隐私

Control 新增固定码：

- 422：`experiment.input_invalid`、`experiment.query_invalid`、`experiment.idempotency_required`、
  `experiment.model_invalid`
- 404：`experiment.not_found`（不存在、不可见和跨 tenant 同形）
- 409：`experiment.idempotency_conflict`、`experiment.scope_conflict`
- 503：`experiment.unavailable`、`experiment.statistics_unavailable`

Gateway 新增固定码：

- 503：`gateway.experiment_unavailable`、`gateway.experiment_variant_unavailable`
- 504：`gateway.experiment_execution_timeout`

三个 Gateway 新码同时加入错误诊断/i18n，并冻结 RequestLog 终止阶段：
`gateway.experiment_unavailable`、`gateway.experiment_variant_unavailable` 精确映射为 `routing`，
`gateway.experiment_execution_timeout` 精确映射为 `provider`。Control 的
`request_logs/query.py::TERMINAL_STAGE_SQL` 与 `request_logs/service.py::_terminal_stage` 必须逐码一致并由同一
参数化测试覆盖；只改 SQL 或只改 Python 都不允许，否则列表与详情会对同一失败返回不同阶段或降级为
`unknown`。

execution timeout 的 wire 合同按响应是否已经开始分流：下游响应头发出前返回 HTTP 504 的统一 JSON
错误 envelope，并在 body 与响应头中复用同一 request_id/trace_id；`StreamingResponse` 已开始后 HTTP 必须
保持 200，只发送一次 `event: error`，其 `data` 为 exact `{"error":{"code","message","request_id",
"trace_id"}}` 安全对象，随后关闭生成器且不发送 `[DONE]`；客户端已经断开时不再尝试发送 SSE，只提交
StreamSession、RequestLog、Usage 和实验 Outcome 的数据库终态。三条路径均禁止透传上游异常或正文。

认证/RBAC 继续使用全局固定码。底层 SQLite、URL、HTTP response body、exception/stack 不透传。
结构化日志、API、Webhook、audit、Runtime JSON、Browser JSON/PNG 和测试 fixture 均不得包含 Provider Key、
Token、Authorization、Cookie、Prompt、Response、文件正文或上游 Endpoint。验收假上游只持久化
“认证是否匹配、被调用 variant、状态/延迟/计费用量”等布尔或本地 ID，不保存请求/响应正文。

## 13. PAGE-105

- 导航 target 为 `experiments`，中文“模型评测”，位于配置组、模型评分之后；required permission
  仅 `experiment.read`。无 read 时无导航、DOM、state 或预取。
- 顶部为紧凑状态筛选和创建命令；主体为实验列表、不可变配置详情和 A/B 双列权威结果，不在浏览器
  拼接 Feedback/Usage/FinOps 或推断赢家。
- 创建对话框仅有 Project、Environment、Alias、Model A/B、目标样本、流量比例、A 比例、时长和
  feedback toggle/window；bps 使用字符串整数转换，禁止浮点乘法。
- `experiment.write+project.read+environment.read+routing.read+model.read` 全部命中才创建按钮、选项请求和
  Dialog DOM；POST 后端再次校验同一组合。网络结果未知时相同 body 复用同一 Idempotency-Key，body
  改变才换 key。
- 缺任一创建依赖时不得预取 Project/Environment/Alias/Model 选项；Shell 权限 revision/fingerprint 变化时
  同步清空草稿、私有幂等键和所有 owner，再 abort 网络。即使前端状态异常，后端 composite guard 与事务内
  快照重验仍拒绝已撤权限，前端隐藏只用于避免泄露和误操作，不构成授权。
- list/detail/results/create 使用独立 owner、generation 和 AbortController；owner 包含 tenant、Session、
  permission revision/fingerprint、导航 permission 和当前 target。离页、logout、撤权、身份变化先清理
  再 abort，迟到响应不能复活 DOM。
- Dialog 支持 Escape、Tab focus containment、关闭恢复 opener；草稿/提交中阻止 15 秒 Shell refresh
  替换 DOM。私有 cursor、幂等键、Controller/fingerprint 不进 DOM、URL、Storage 或 snapshot。
- 1280x720 和 390x844 均无横向溢出、重叠或文本裁切；移动结果使用稳定 label rows，不缩放字体。
- API client 只接受 experiment 域 exact DTO 和本地错误字典；不能透传原始 server message、diagnosis
  或 response body。401 仅在当前 owner 下清 Session；只有 `403 auth.permission_denied` 触发撤权清理，
  业务 409/422/503/504 保留为页面局部状态。

## 14. 配置与部署

- 不新增独立 secret；HMAC 复用现有部署级 Auth root 并做严格域分隔和 active key fingerprint。
- 新增 scheduler interval（1..60 秒，默认 5）、finalizer/reaper lease（30..300 秒，默认 90）、Gateway
  execution lease（30..300 秒，默认 90）、heartbeat（5..60 秒且小于 lease/3，默认 20）和 batch limit
  （1..100，默认 20）的有界配置；Gateway 只续租自身 execution，不运行 scheduler，Control 的
  scheduler/reaper 不调用 Provider。
- 新增 `experiment_execution_max_seconds`（60..86400，默认 3600）与
  `experiment_settlement_grace_seconds`（60..86400，默认 3600），创建时均冻结到 Experiment；运行中修改
  只影响新实验。长流按固定间隔续租并受 absolute deadline 限制；续租失败或 lease 丢失必须取消本地
  Provider 等待并按 exposure matrix 收口，不能继续向客户端返回未受 owner 约束的结果。硬截止仍把无法
  证明的暴露/成本保守冻结为 unknown/unavailable。
- Compose、`.env.example` 只增加非敏感数值开关；secret 保持既有掩码和生产强度门禁。
- Console/API/Gateway 使用同一 SQLite owner；多 Gateway 通过 `BEGIN IMMEDIATE` 保证样本配额。
- readiness 复核 active Experiment 的 HMAC key fingerprint；不新增 secret，也不把 fingerprint 公开。
- readiness 同时逐 arm 复核 route-config fingerprint 和 Adapter catalog/spec identity；active Experiment 存在时
  不得发布会改变 catalog hash 或该 Provider spec version 的 Adapter 制品，避免一次实验混合不同执行协议。
- Auth root 只约束 sampling 与 route-config fingerprint；active Experiment 存在时仍禁止轮换。创建幂等的
  key hash/request fingerprint 以客户端 canonical UUID 的高熵和独立域稳定计算，不依赖 Auth root；因此旧
  Experiment 在合法轮换后仍可同 body 重放，不能因 key rotation 误建第二条记录或把同 key 异 body当新请求。

## 15. 迁移与回退

- 新增 `0068_model_ab_testing_quality_evaluation.sql` 与 `.down.sql`。
- up 精确要求 67 条、最高 0067、RBAC revision 10 和冻结表/trigger 形状；升级到 68/0068 与
  catalog revision 11。只扩展完全匹配 canonical revision-10 hash/关系集的角色，自定义矩阵不静默获权。
- 新增两项权限后目录为 66 项，canonical 默认关系为 179 条（revision 10 的 64/173 加
  admin/developer/ops_admin 各两项）；revision-11 canonical matrix SHA-256 分别冻结为 admin
  `33fe73e93c4b09ad6cb33367484de80652388fa03d390dd4e5c35fef066e0148`、developer
  `71313d68fd5b18c754ab9927743ef6a772f42ae4b8e7167e81222f1a788f48cd`、ops_admin
  `0f024ad0b30f6f44d26d92156c12506b4348b6412201cf9212b6e7183a313f1a`。迁移以完整 hash/关系集证明，
  不只检查计数。
- 0068 up 必须先识别并删除 0067 中嵌入 revision-10 目录、关系和角色 hash 的
  `trg_tenants_rbac_initialize_prd059`、role owner/matrix、permission/template owner 与 immutability trigger，再以
  revision 11 的 66/179 exact-set 和新 canonical hash 原样重建。迁移后新建 tenant 必须一次性得到 66 条
  Permission 和 revision-11 canonical 角色矩阵；down 在收缩既有 tenant 后恢复 0067 的 initializer、role
  insert/matrix 和目录 trigger，使 down 后新建 tenant 精确得到 revision 10，而不是留下混合版本 initializer。
- up 后证明所有表/列/index/trigger、角色矩阵、source/outbox 覆盖、`integrity_check=ok` 和 FK 0。
- down 在任何 scope claim、Experiment、Assignment、Execution、Outcome、Settlement、Feedback Attribution、
  Result、Event、Outbox、实验 AuditLog/role-permission event、含三个实验错误码的 RequestLog，或
  `webhook_endpoints.event_types_json` 订阅 `experiment.completed` 时先失败；同样逐级 guard
  `webhook_deliveries(event_type='experiment.completed')`、其 `webhook_delivery_attempts` 和
  `webhook_dead_letter_events`。这些都是 0068 外部或客户事实，不能因删除实验表而遗失。
- `experiment_scheduler_tenants` 只保存可重建的内部 next-run/lease cursor，不是客户事实；down guard 先证明
  没有 active lease 且其他业务/外部事实均为零，再显式删除 cursor。它不能被含糊地同时当阻断事实和可删状态。
  全部 guard 通过后才精确恢复 revision 10/0067 与旧矩阵 hash。
- down 先移除挂在 request_feedbacks、FinOps、RequestLog、Attempt/Stream、claim/Decision 等既有表上的
  0068 trigger，再按 outbox/event/result/attribution/settlement/outcome/execution/assignment/experiment/
  request-scope-claim 的反向 FK 顺序收缩，并在 RBAC 收缩后恢复 revision-10 initializer 与矩阵 trigger；任何
  guard 失败都必须保留完整 0068，不得留下半套 catalog、孤儿 trigger 或混合版本新租户初始化器。
- up/down 都在各自单个 `BEGIN IMMEDIATE` 中执行；up guard 精确验证 revision-10 模板/关系 hash 和既有
  schema 形状，down guard 在任何 DDL 前执行。只记录 canonical revision-10 hash 且关系 exact-set 的
  admin/developer/ops_admin role ID，才新增六条默认关系；自定义矩阵只获得 tenant 级 Permission catalog
  行而不获 grant。down 只撤销迁移实际扩展且之后未漂移的 role，任一权限管理审计或矩阵/hash 漂移均整体
  失败，迁移器 rollback 后仍保持完整 0068。
- 空 SQLite 必须真实上行到 68/0068，重复 apply 幂等；空事实 0068 down -> 0067 -> up 0068 可恢复。

## 16. 测试与验收计划

### 16.1 Shared / Migration

- HMAC domain、ASCII canonical JSON、digest 前 8 字节 big-endian、bucket 边界、纯函数重复稳定、不同
  tenant/experiment/request 隔离、route-config fingerprint 的 exact 字段/敏感值零暴露、HTTP 重复 request ID
  409、exact quota 和三种 assignment reason。
- arbitrary-int half-up、Gateway nullable 零分母、PRD-049 Feedback 零分母为 0、CNY micros、feedback
  milli、整数溢出。
- RBAC revision 11、角色上限/默认矩阵、动态撤权、旧 revision fail-closed；up 后创建新 tenant 得到 66/179，
  down 后创建新 tenant 恢复 64/173。具名覆盖自定义 developer/ops_admin 在 up 不获新 grant 且原
  permission revision/hash 不漂移，以及其后矩阵或权限审计漂移使 down 在 DDL 前原子失败。
- 0068 fresh/up/down/idempotency/checksum/形状/trigger/REPLACE/UPDATE OR REPLACE/owner graph/down guard；
  Assignment 缺 Execution 或 Settlement 任一 child 时 COMMIT 必须失败且 counter/facts_version 回滚。
- down 分别注入 experiment.completed endpoint subscription、delivery、attempt、dead-letter、实验 RequestLog、
  AuditLog/permission event 和 active scheduler lease，逐项证明完整 0068 原子保留；仅可重建且无 active lease 的
  scheduler cursor 在其余事实为空时允许删除。

### 16.2 Control / Scheduler

- 401/403、admin/developer/ops/viewer、跨 tenant 同形 404、body/query/header exact-set。
- 创建模型/scope/比例/时间/feedback 正反例、幂等重放/冲突、active scope 并发唯一；同一 UUID 在 Auth root
  合法轮换后仍重放原 Experiment，不同 body 仍冲突，operation source request/trace 始终保持首次值。
- list/detail cursor 私有性；provisional 单 snapshot；completed 冻结后迟到 feedback 不漂移。
- time/target 两条收口、execution 三态与租约续期、Assignment/Attempt/send/RequestLog/Usage 各崩溃点、
  FinOps spool 等待、硬结算截止与 unknown cost、双 scheduler lease/CAS、旧 generation 拒绝。
- Feedback 在 deadline 前取时后阻塞、finalizer 先关 fence、deadline 后提交的 barrier 竞态；全局 Feedback
  成功但 attribution 不增长，完成结果保持不变。
- source/result/outbox 原子、空订阅、重试、停机恢复、Webhook 签名和审计隐私。

### 16.3 Gateway

- sample bps 1/10000 边界、纯 HMAC 输入重复稳定、A/B exact target、floor/ceiling forced reason、并发不超配额。
- tenant/project/environment/alias 隔离；Policy route_to、Embeddings、internal Chat、Batch 明确跳过。
- 认证事务 scope claim 与可变 Virtual Key 改绑竞态、variant exact route、Assignment/Decision/execution/
  settlement deferred closure、Model provider/capability 与 Provider type/base URL/auth 漂移失败、无跨 variant
  fallback、流/非流 owner 一致、发送许可前后取消与进程崩溃的 exposure 分类。
- DLP/Intent/Rate/Budget/Output Safety/RoutingAttempt/Stream/Usage/FinOps 正反链路和所有失败可追踪；
  本地 Key/Adapter/URL/协议失败不得误标为 upstream observation。
- Provider Key/Token/Prompt/Response/Endpoint 在日志、DB、异常和验收证据零命中。

### 16.4 Console / Runtime / Browser

- API DTO exact projection、错误映射、owner/generation/abort、草稿与模糊提交幂等、撤权零预取。
- PAGE-105 desktop/mobile、Dialog focus、列表恢复、active -> collecting -> completed、结果冻结。
- 真实隔离 SQLite、Control/Gateway/Console、两个回环假 Provider 和 Webhook；生成安全合成请求直至
  A/B 配额，注入成功/失败/延迟/用量/反馈并逐项核对 API/DB/UI。
- fresh 68/0068、正反迁移、`integrity_check=ok`、FK 0；停止服务后端口/WAL/SHM/Session 清理。
- 专项先行；完成门禁再跑受影响相邻回归和必要全量。证据始终保持
  `all_108_prds_complete=false`。

## 17. 代码修改计划

1. Shared：experiment HMAC/幂等/route-config fingerprint/统计/Webhook 合同、RBAC revision 11、错误目录。
2. Migration：0068 up/down、request scope claim、Experiment/Assignment/Execution/Outcome/Settlement/Attribution/
   Result/Event 表、deferred closure FK、index/trigger、RBAC initializer/角色矩阵和迁移测试。
3. Control：`experiments` models/routes/service/scheduler/reaper/outbox、app/navigation。
4. Gateway：Assignment/Decision 原子 claim、exact route、execution lease/send fence、Outcome/Settlement 收口、
   无 fallback 与 owner 保护。
5. Console：PAGE-105 client/state/render/bind/styles/i18n/navigation/tests。
6. Compose/env/API/directory-map/README/testing/Runtime/Browser 验收器。
7. 先跑受影响专项；设计、实现和验收收口后再执行敏感/禁止文件审计、提交并推送 `origin/main`。

## 18. 编码前两轮自审

### 18.1 第一轮：真实写入顺序、恢复活性与线性化

状态：**已完成并修正（2026-08-08）**。本轮按真实 `_handle_gateway_request`、
`gateway_request_claims`、Attempt/Stream 创建点、Usage/FinOps 事务和 Feedback 写锁顺序推翻原“已冻结”结论，
并写回以下修正：

1. Assignment 的父事实改为已提交 gateway claim；RequestLog 可能永远缺失，不能作为前置 FK 或唯一恢复源。
2. Assignment 同事务创建带 lease/generation 的 execution；reaper 区分发送许可前 abandoned 与发送后
   outcome unknown，硬截止不再永久等待，也不伪造失败。
3. Attempt/Stream 早于真正网络发送，不能作为 Provider-called 事实；新增显式 send-fence/exposure matrix，
   质量分母只认现有 PRD-048 终态资格。
4. Assignment 与 Experiment RoutingDecision 同事务唯一互链；exact route 在提交前完整构造并立即绑定
   RequestLogContext，禁止 resolver 失败留下无 model/provider owner 的半图。
5. Usage settlement 从 Assignment 起即存在；正式 usage_charge direct/spool replay 原子推进 recorded，硬截止
   把剩余 pending 变 unavailable，gross charge 不受 credit/reversal 改写。
6. Feedback cutoff 不再按锁前生成的 created_at；INSERT trigger 与 scheduler close-fence 共享写锁，并用
   database now 决定 attribution，关闭后全局反馈仍成功但实验结果不漂移。
7. HMAC 精确冻结 ASCII canonical JSON、digest 前 8 字节 big-endian；HTTP request ID 重试仍为 claim 409。
8. 最终事务按 Result A/B、Event/outbox、parent completed 的无环顺序提交，并用 facts_version/lease CAS
   防止长聚合快照漂移。
9. finalizer 必须先在短事务关闭 feedback fence、reap execution 和收口 settlement，再读取新 snapshot；
   不能用收口前的旧聚合生成 Result。持续流另有绝对 execution deadline，避免合法续租永久阻塞完成。
10. runtime variant 漂移不能在“必须有 selected route”和“必须计入 arm 失败”之间二选一；失败事务现冻结
    failed Decision、Assignment、terminal failed/not-exposed Outcome 与 Settlement，再安全返回统一错误。
11. 只允许带 project/environment 的 public Virtual Key 进入，active 实验期间 Auth root fingerprint 不得漂移；
    tenant-qualified owner、RBAC、RequestLog 阶段映射和默认上游边界均未被实验旁路。

第一轮逐段一致性复读、字段/枚举对照和反例测试清单已核验通过；其结论随后进入第二轮独立复审，第二轮
明确通过前没有开始迁移或业务编码。

### 18.2 第二轮：权限、迁移、Console、兼容性与隐私

状态：**已完成并修正，设计冻结（2026-08-09）**。第二轮从动态 RBAC 与事务内撤权、tenant owner 图、0068 up/down
和自定义矩阵、旧 PRD 路由/Batch/Chat 兼容、PAGE-105 恢复与异步 owner、Webhook source/outbox、双视口
可访问性，以及日志/DB/API/证据零泄露逐项反证，已关闭以下问题：

1. 创建权限冻结为五权限 composite guard，事务内重验；Console 缺任一依赖时不创建 Dialog、不预取选项，
   permission owner 漂移先清理再 abort，后端不依赖前端隐藏授权。
2. RBAC 基线由真实 0067 fresh DB 复核为 revision 10、64 permissions、173 canonical relations；0068 目标
   为 11、66/179，只扩展 exact canonical role，自定义或迁移后漂移矩阵 fail-closed。
3. 0068 down 在 DDL 前完成事实、审计、矩阵和 owner guard，先删既有表上的 0068 trigger，再反向删除新
   owner 图；所有步骤同一写事务，失败不会留下半迁移。
4. sampling、variant、sampling-key fingerprint 三个既有 HMAC 域与新增 route-config fingerprint 域均冻结为
   单个 `0x00` 终止字节，修正双反斜杠歧义；bucket 只取 digest 前 8 字节 big-endian，并要求固定 golden
   vectors。创建幂等另以 canonical UUID 高熵和独立域计算，不受 Auth root 合法轮换影响。
5. FinOps 复核当前 usage_charge 只允许 paid quota；实验只累计 gross usage charge，不把 credit、reversal
   或 adjustment 误作负模型成本，也不把 unavailable 成本显示为零。
6. Feedback adoption 继续以 feedback_count 为分母并在零分母返回 0，保持 PRD-049 wire 兼容；反馈截止只
   依赖取得写锁后的 database now 与 fence，不信任锁前 created_at。
7. `experiment.completed` payload、event ID 前缀、completion reason、计数和 source owner 已冻结 exact-set；
   首投、pending resume 与 dead-letter replay 必须由同一 owner 图重建验证。
8. Gateway 无法权威判断 active scope、fingerprint 或 owner 图时在 Provider 前失败关闭；Policy route_to、
   Embeddings、Batch、Enterprise Chat 和其他 trusted invocation 仍明确旁路，不改变旧 PRD 合同。
9. Result 延迟单位、nullable 指标、feedback enabled/zero 语义、facts version 与 250ms read deadline 已逐字段
   对齐；provisional/final 不再可能使用不同分母或从当前价格回算历史。
10. Provider Key、Token、Prompt、Response、Endpoint、HMAC 输入/摘要和私有 cursor/idempotency owner 均不
    进入日志、API、Webhook、DOM、截图或验收 JSON；Runtime 假上游也只保留布尔与安全本地 ID。
11. Exposure 不再由 PRD-048 quality eligibility 推导：有可信 HTTP 响应、成功响应或 active stream 才是
    exposed；发送许可后无响应的 timeout/network/cancel/crash 为 unknown；发送许可前或明确 local
    configuration 为 not_exposed。400/401/403 可证明 exposed 但通常不进入质量分母，ConnectTimeout 即使
    quality eligible 也仍是 unknown。
12. Gateway Outcome 不在 Provider 返回时冻结：非流必须等 Output Safety、RequestLog 和 Usage durability，
    流必须等 shielded body finalizer；output blocked 或 Usage 主库与 spool 双失败可以把较早 Provider success
    收口为最终 gateway failure。
13. 实验只匹配当前环境解析出的 exact canonical Alias；`validate_requested_model_authority` 接受的 direct
    concrete Model 明确旁路实验并继续原 resolver，不能覆盖客户显式选模。
14. generation-1 owner/token/lease、request-owned heartbeat 和最外层 execution handle 已补到状态机与表合同，
    覆盖全部 post-assignment 路径；renew/send-fence CAS 失败或 absolute deadline 到期必须停止 Provider 等待和
    客户端发送，`ApiError`、独立 `CancelledError`、未知异常及 Attempt/Stream 创建失败均由同一 handle 终结。
15. execution timeout 已区分响应头前 504 JSON、SSE 开始后的 HTTP 200 + 单一标准 error event，以及客户端
    已断开后只写数据库终态；两个 unavailable 错误映射 routing、timeout 映射 provider，SQL/Python 必须一致。
16. 创建幂等现在具名持久化 actor-scoped `idempotency_key_hash`、`request_fingerprint`、首次
    operation request/trace 和唯一约束；key/request 摘要不依赖 Auth root，轮换后重放仍命中旧 Experiment，
    Webhook source 不能被重放调用覆盖。
17. 现有 gateway claim 不含可证明的 Project/Environment，新增认证事务内一一 scope claim；Assignment 以
    deferred FK 绑定该不可变 scope，Virtual Key 在认证后改绑不能改变请求归属。
18. Experiment 每个 arm 冻结 provider ID/code 和敏感值不外泄的 route-config fingerprint，Result 复制同一
    owner；Model provider/model/capability 或 Provider type/base URL/auth 漂移必须 fail-closed，不能把不同
    执行配置聚合为一个 arm。
19. Assignment 以两个 NOT NULL child ID 和反向 deferred closure FK 强制 Execution/Settlement 同事务闭合；
    缺任一 child 时 COMMIT、样本 counter 和 facts version 必须整体回滚，不能只靠应用层约定。
20. 0068 up/down 明确替换和恢复 tenant RBAC initializer、role insert/matrix 与目录 trigger；up 后及 down 后
    新建 tenant 分别得到 exact revision 11 与 revision 10，自定义 developer/ops_admin 不被静默扩权。
21. down guard 已逐类列出 experiment.completed endpoint subscription、delivery/attempt/dead-letter，以及实验
    RequestLog、AuditLog/权限事件；scheduler cursor 被明确为仅在无 active lease 且无业务事实时可删的内部状态。

上述十一项已写回正文，并按真实 Gateway、0067 schema/RBAC initializer、可变 Virtual Key/Model/Provider、
Webhook delivery 图和迁移器事务语义重新逐条复读。最终结论为 P0=0、P1=0，两轮设计审查明确通过，设计冻结，
现在允许按第 17 节开始编码。任何实现若改变上述合同必须先回到设计审查，不能以测试通过替代设计变更记录。

## 19. 实际运行验收修订：统计查询期限与调度包络

状态：**两轮修订自审已完成并修正，修订设计冻结（2026-08-09）**。PAGE-105 在完整 Control 进程中连续
返回 `experiment.statistics_unavailable` 后停止完成验收；以下修订不是放宽 SQLite 聚合，而是修正原设计把
worker 尚未获得 CPU 的调度时间错误计入数据库读取期限的问题。

实测证据：同一完成实验的 A/B 聚合约 0.6ms，响应模型约 0.1ms；独立进程内含事务 RBAC 的完整同步读取为
121-183ms。完整 Control 同时运行多个 Scheduler 时，worker 可能超过 250ms 才开始执行，导致尚未读取任何
实验事实就确定性 503。详情 API 同时仍可在约 1.2s 返回，证明数据库 owner 图并未损坏。

修订后的冻结合同：

1. SQLite `query_only` read transaction 从专用 worker **实际开始执行**时取得单调时钟，并继续使用绝对
   250ms progress deadline；busy、整数溢出、owner/facts 不一致仍整体 503，不返回部分结果。
2. HTTP 调度包络固定为 2 秒，只覆盖专用线程获得 CPU、250ms 数据库读取和结果交回事件循环；它不改变
   SQLite deadline。超过包络仍返回 `experiment.statistics_unavailable`。
3. 统计 lane 使用独立单线程执行器和非阻塞进程锁。已有读取时新请求立即 503，不排队、不刷新预算，也不按
   tenant 建无界 worker；worker 的 concurrent-future 回调直接释放锁，不依赖繁忙事件循环。
4. 路由依赖先完整重算 66 项租户 RBAC 目录。结果 read transaction 再以一条 owner 查询复核 Session、
   tenant/user/role、`experiment.read` relation、role revision/matrix hash 和刚认证的 authorization fingerprint；
   撤权、换角色、Session 失效或 tenant 漂移仍在读取实验事实前失败关闭，不重复扫描完整目录。

### 19.1 修订第一轮自审：并发、期限和资源所有权

状态：**已完成并修正（2026-08-09），P0=0、P1=0**。

1. 反证多个请求同时到达：非阻塞锁只允许一个 worker，失败请求不会进入 executor queue；不存在延后启动后
   获得新 250ms 的积压任务。
2. 反证 HTTP waiter 超时或客户端取消：线程不能被强制取消，锁由 concurrent future 在真实退出后释放；
   期间后续请求固定 503，不能与遗留 SQLite 读取重叠。
3. 反证事件循环或默认线程池饱和：统计执行器不与 Scheduler 共池，SQLite deadline 在 worker 内生成；
   2 秒包络只吸收调度抖动，查询本身仍被 progress handler 限制为 250ms。
4. 反证 executor 关闭、submit 失败和回调异常：提交失败先释放锁并返回稳定错误码；回调只消费异常，不记录
   SQL、路径、Token 或租户事实。

### 19.2 修订第二轮自审：权限、租户、兼容性和证据

状态：**已完成并修正，修订设计冻结（2026-08-09），P0=0、P1=0**。

1. 快速事务复核不是信任 Token claim：完整目录校验仍由同一 HTTP 请求的后端 dependency 完成；事务查询
   使用可信 AuthContext owner，客户端不能提交 tenant/user/role/permission 参数。
2. RolePermission trigger 原子推进 revision/matrix hash；事务内 relation 或 fingerprint 任一不匹配均 403，
   非法 role/hash 或数据库错误均 503。跨租户与不存在仍由 tenant-qualified Experiment 查询保持同形 404。
3. wire contract、request_id、结果字段、provisional/final 分母和 `Cache-Control` 不变；只新增内部调度边界，
   不影响旧 PRD、Gateway、Batch、Chat 或 Provider 默认范围。
4. 测试必须覆盖默认线程池不可用、worker 超时后锁仍持有、worker 退出释放、路由后换角色、250ms worker
   deadline 和 2 秒响应包络；Browser 必须实际显示两张最终 A/B 结果卡后才可关闭该修订。

两轮修订均已把并发、权限、tenant、回退和兼容性问题写回本节；现在才允许按该冻结合同修改最终期限实现。

## 20. 终态一致性与验收可信性修订

状态：**两轮修订自审已完成并修正，修订设计冻结（2026-08-10）**。实现后独立审查发现旧设计仍允许
后台 SQLite 线程在调用方取消后提交 success，也允许 reaper 从未提交 Outcome 的 execution 恢复 success；
旧 Runtime/Browser 证据又早于随后的源码与迁移改动。以下合同先于最终整改冻结，旧证据全部失效。

### 20.1 第一轮：取消、CAS 与客户端终态线性化

状态：**已完成并修正（2026-08-10），P0=0、P1=0**。

1. `asyncio.to_thread` 一旦启动不能靠取消等待者停止。Guard 必须从创建时就在独立 Task 内运行 Outcome CAS，
   外层只 shield 该 Task；观察到取消后仍等待线程确定收敛，再传播原取消。CAS 提交结果必须由独立 Task
   原子发布为 `terminal_committed` 与不可变 `terminal_intent`，不能出现数据库已提交而 Guard 仍称未终结。
2. success CAS 启动前已经存在取消时，不得启动 success 提交；外层先形成固定 failed RequestLog，再以
   `cancelled` 意图终结。CAS 已启动或已提交后到达的取消仍传播给 ASGI，但不得把已提交终态的 RequestLog
   改写成相反状态。CAS 未命中才允许用同一 owner 尝试一次固定失败终态。
3. 流式 body finalizer 使用共享 cancellation signal。cleanup 独立 Task 在 Outcome CAS 前重验该信号；边界前
   取消提交 failed/cancelled 且不发送 `[DONE]`，边界后取消保留已经提交的终态。Session、RequestLog、Usage、
   Outcome 与客户端最后帧共用这一线性化顺序，不以“cleanup 最终运行过”代替客户端交付事实。
4. 反证外部取消、Guard deadline 取消、重复取消和 finalize 线程阻塞：只消费 Guard 自己增加的一次取消计数；
   外部取消不能被转换成 HTTP 成功或静默吞掉，heartbeat/finalizer 也不得遗留后台任务。

### 20.2 第二轮：恢复、隐私、租户与验收反例

状态：**已完成并修正（2026-08-10），P0=0、P1=0**。

1. expired nonterminal execution 没有 Outcome CAS，故 reaper 永不恢复 Gateway success；RequestLog 修正使用
   tenant/request/model/provider 与 execution owner 全组合，跨租户、跨模型或旧 generation 均不能命中。
   Provider exposure/quality 与 recorded cost 独立保留，不改变客户端 Gateway 失败事实。
2. Usage spool 缺失 RequestLog 时先按 tenant/request/model/provider 检测 Assignment。实验请求补固定
   `failed/gateway.experiment_unavailable`，普通请求保持历史 success 兼容；spool -> reaper -> outbox 联合反例
   必须证明最终事件不会发布虚假成功。
3. Runtime CLI 的未知参数和路径不得由 argparse 原样输出；统一映射为固定 `config.arguments/invalid`。
   owner、Provider/Webhook stub、端口和日志句柄必须 best-effort 全部清理，记录首个固定清理错误后才失败，
   任何失败批次都不得发布为 final 证据；SQLite `-wal/-shm/-journal` 均属于未清理的私有数据。
4. 测试新增真实 `traffic_sample_bps=1/9999` 命中与旁路、真实 Feedback API 写入/attribution/facts fence/
   晚反馈全局成功与 final 冻结、两个独立 Control 实例同 scope 并发创建恰好一个 201 和一个 409，以及
   finalize cancel、CAS/RequestLog 双失败、spool/reaper/outbox 联合恢复反例。
5. Runtime 与 PAGE-105 Browser 必须在最终源码和 0068 冻结后重新运行。Browser 必须验证创建成功、同 body
   同 key 重放、改 body 换 key、撤权 403，以及桌面/移动视口；任何较早证据不得拼接或引用。

两轮修订已把取消边界、后端权限、tenant owner、恢复保守性、普通请求兼容和证据隐私逐项反证并写回正文。
最终整改只能按本节合同实施；专项、迁移、Runtime 与 Browser 全部重新通过前不得恢复“已完成”状态。

## 21. 最终实现与验收结论

状态：**已重新验收并完成（2026-08-10）**。旧 Runtime `prd075-final-20260809T193006Z`、
`prd075-final-20260809T232611Z` 与旧 PAGE-105
Browser 批次早于第 20、22 节修订，保持失效且不参与最终证据。最终结论只引用以下重新生成批次：

1. 最终受影响回归：Shared/Provider `180 passed`、Gateway `204 passed`、Control/0068 `120 passed`、
   Runtime 验收器 `21 passed`；Console `723 passed`、lint `225 files passed`。各集合可能重叠，不相加冒充
   一次根级全量。
2. Runtime `prd075-final-20260810T011951Z` 的 JSON SHA-256 为
   `a57fc35bfab6f5ab8e8f0873f02cce7f2e60ee7fc3d84d6f63674c5c903ac845`。同一空 SQLite 完成 fresh/repeat
   `68/0068`、down `67/0067`、re-up `68/0068`；四阶段 `integrity_check=ok`、外键违规 0，RBAC revision 11、
   permission 66。Viewer 403、跨租户 404、A/B 各四次调用、成本/失败/恢复/Webhook、outbox pending 0
   均通过；145 个受控文件敏感明文命中 0，SQLite sidecar 0。
3. Browser `prd075-final-20260809T234829Z` 的主 JSON SHA-256 为
   `82379395d845649fa5d54504311c1862aa81974b47000b58ca9c2f8e730cc092`。应用内 Chromium 在
   `1280x720` 与 `390x844` 显示两张 facts v7 冻结结果卡；UI 点击创建与独立 API helper 证据分离，后者验证
   201 幂等重放、409 异体冲突和新 key 新 owner。动态撤销 `experiment.read` 后后端 403 且 request_id 与
   响应头一致，导航、Panel、DOM 全部清空；13 项 manifest、11 张非空 PNG、布局和隐私门禁均通过。
4. Runtime、Browser fixture 和临时凭据均已停止或删除，固定端口监听、WAL/SHM/journal 与私有 Browser
   state 均为 0。PRD-075 的设计、实现、迁移、测试、文档和实际运行验收至此闭环。

该完成状态只关闭 PRD-075，不启动后续 PRD；`all_108_prds_complete=false` 保持不变。

## 22. 流终止与历史请求兼容性修订

状态：**两轮修订自审已完成并修正，修订设计冻结（2026-08-10）**。终态整改后的独立反例审查继续发现：
OpenAI SSE 在未收到冻结终止标记时仍可按 success 收口，active 审计失败后的上游关闭可被取消中断，Guard 已
提交后没有拒绝相反终态；同时 0068 的 scope claim 写入误伤缺少完整 Project/Environment 投影的既有普通请求，
Reaper 也可能在残缺 success RequestLog 存在时提交 unresolved Outcome。以下合同先冻结再实施，仍不得复用旧
Runtime/Browser 证据。

### 22.1 第一轮：流协议、取消与唯一终态

状态：**已完成并修正（2026-08-10），P0=0、P1=0**。

1. `ProviderRequest` 必须冻结 Adapter Stream Profile 的可空终止标记。标记为 `[DONE]` 的 OpenAI SSE 只有
   实际观察到完整标记后才具备客户端逻辑成功候选；clean EOF 但缺标记固定按上游流不完整失败，Session、
   RequestLog、Outcome 和 SSE error 必须一致。Generic/custom profile 的标记为 `NULL`，其 clean EOF 仍是合法
   终止，不能为了修复 OpenAI 路径破坏客户本地兼容端点。未启用 Output Safety 且不属于实验的 OpenAI raw
   passthrough 必须用独立、有界且不保存正文的 SSE marker observer 跨网络块识别完整事件；observer 不得重分块、
   延迟或改写原始响应，也不能把块内字符串子串误认成终止事件。
2. 上游 2xx 已建立而 `mark_stream_session_active` 失败时，关闭响应、终结 starting Session 和释放 Cache
   Affinity Selection 必须组成一个独立 shielded 清理任务。清理期间到达的 `Task.cancel()` 只在全部清理完成后
   原样传播，不能遗留连接、starting Session 或选择租约。
3. Guard 的唯一 Outcome 既要求数据库 CAS 唯一，也要求内存终态意图唯一。已经提交后只允许相同 intent 幂等
   返回；success 与 failed/cancelled 等相反 intent 必须抛固定编程冲突，不能错误返回 `True`。
4. 反证空 OpenAI 流、正文后直接 EOF、Generic EOF、清理中一次/重复取消和 CAS 后相反 intent；所有后台
   finalizer、heartbeat、连接和并发租约必须归零，错误与日志只包含固定 code、request_id、trace_id 和 tenant_id。

### 22.2 第二轮：scope claim、租户 owner 与恢复兼容

状态：**已完成并修正（2026-08-10），P0=0、P1=0**。

1. `gateway_request_claims` 仍覆盖所有已认证请求；0068 新增的 `gateway_request_scope_claims` 只在上下文同时
   具备 Virtual Key、Project、Environment ID 与规范 environment code 时写入。缺少完整 scope 的旧测试、内部
   调用或历史兼容 Key 继续获得普通 claim，但没有 scope claim，因此不可能进入实验 Assignment。
2. 完整公开 Virtual Key 路径仍在读取正文和 Provider 前冻结 tenant/project/application/environment；条件写入
   不是从数据库补猜可变 Key 关系，也不能允许客户端 body/header 声明 scope。Experiment repository 缺 scope
   claim 时只能旁路，不能降级为跨 owner 匹配。
3. Reaper 只可把 tenant/request/model/provider 全匹配的残留 success RequestLog 降为固定实验失败。若同
   tenant/request 仍存在 owner 缺失或冲突的 success，必须在写 Outcome 前抛稳定完整性冲突并由调用事务回滚；
   不能一边保留客户端 success，一边完成 unresolved/failed 实验事实。跨 tenant 同 request 不参与判断。
4. 回归必须覆盖 PRD-069 缺 scope 普通 claim、完整 PRD-075 scope claim、NULL model/provider 残留 success、
   owner 完整降级、普通 spool replay、实验 spool/reaper/outbox 联合恢复和 0068 FK/integrity。上述修订不改变
   Batch、Enterprise Chat、Embeddings、Policy `route_to` 或非实验 RequestLog 的历史成功语义。

两轮修订已经逐项核对并发线性化、Provider profile 兼容、tenant owner、历史调用和失败恢复。提交前独立反例
复核又实际复现了实验流 timeout/通用异常先发送上游 error、持久化降级后再发送第二个 error 的 P1；实现已改为
实验流统一在 shielded finalizer 后只发送最终错误码，并新增 timeout+RequestLog 失败、缺 `[DONE]`+Usage 丢失
两条组合反例。后续兼容复核还关闭了普通流即时 error 后因持久化失败重复发送的 P2：普通流保持历史即时错误，
但以已发送标记阻止 finalizer 追加第二个终态事件。修复后的完整专项、必要相邻回归、0068 正反迁移和最终 Runtime
均重新执行；
PAGE-105 Browser 所覆盖的 Control/Console/API helper 代码未受该 Gateway-only 修复影响，其最终批次继续有效。
最终批次和结果登记在第 21 节及测试记录。`all_108_prds_complete=false` 保持不变。
