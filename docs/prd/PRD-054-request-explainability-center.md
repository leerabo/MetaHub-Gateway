# PRD-054 - Request Explainability Center 技术设计

## 文档状态

- PRD：`PRD-054`
- 名称：请求解释中心 / Request Explainability Center
- 里程碑：`M1-企业POC`
- 优先级：`P1`
- 页面：`PAGE-079 请求解释中心`
- API：`GET /request-explain/{request_id}`
- 核心对象：`RequestExplanation`、既有 `RoutingAttempt`、既有 `DlpMatch`
- 事件：`request.explanation_viewed`
- 验收：`TC-0107`、`TC-0108`
- 当前状态：设计、两轮编码前自审、实现、实现后两轮交叉自审、测试与真实运行验收均已完成

## 1. 需求来源与冻结结论

### 1.1 Excel 权威范围

本设计已核对：

- `01_PRD总表!A55:R55` 与 `02_M0-M1核心开发!A55:R55`
- `04_页面清单!A80:H80`
- `05_API接口清单!A256:H258`（清单把一个路径拆成 method/path token，实际接口仍只有一个）
- `06_数据对象!A10:F10`、`A13:F13`、`A75:F75`
- `07_Webhook事件!A61:H61`
- `08_验收用例!A108:J109`
- `09_CodexClaude提示词!A55:F55`

冻结需求如下：

1. 开发者需要解释一次请求为什么被路由、失败、阻断或产生某个成本。
2. 一个详情页、一次 API 响应展示鉴权、限流、预算、DLP、策略、路由候选、KeyPool、Provider 响应、usage、日志与 Webhook 状态。
3. `GET /request-explain/{request_id}` 的 path `request_id` 是被解释的 Gateway 源请求；本次 Control API 调用仍使用独立 operation `request_id/trace_id`。
4. 无权限用户不能读取；跨租户 ID 与不存在 ID 必须同形拒绝。
5. 异常输入或依赖故障返回统一错误和 operation `request_id/trace_id`，不能泄露数据库、路径、订阅地址、密钥或正文。
6. 每次成功读取都生成独立访问事实和 Audit，并以稳定事件 ID 触发
   `request.explanation_viewed`；投递失败不能丢失已提交事实，也不能让网络请求阻塞 GET。
7. 不做跨请求根因分析，不借此 PRD 引入机器学习、完整 OTel 后端或事故管理平台。

### 1.2 架构约束

架构文档第 3、4、5、9、11、12、14、15、16 章给出直接约束：

- 数据面同步链为 API Listener、Virtual Key 鉴权、限流、预算、DLP、Router、Adapter、Telemetry；解释查询属于 Control Plane/可观测层，不能进入 Gateway 同步热路径。
- 第 9 章明确 Request Explainability Center：请求概览、鉴权与权限、候选与最终路由、预算/限流/DLP/能力、上游归一错误、预估与实际成本、Webhook 成功/失败/死信。
- 请求必须记录本次使用的 snapshot version；`request_id` 贯穿日志、账单和 Trace。
- DLP 只保存规则 ID、匹配摘要哈希和长度；普通日志不得保存命中文本。
- 所有业务表必须带 `tenant_id` 或有强租户归属，所有管理 API 在后端鉴权。
- 查询应写不可变访问审计；Trace、Usage、Budget、Webhook 均按租户隔离。
- 默认在中国境内保存数据、日志、账单和审计；本 PRD 不改变上游厂商边界。

## 2. 现有实现与真实缺口

### 2.1 可复用事实

现有 `GET /request-logs/{request_id}` 已按 `tenant_id + request_id` 聚合并脱敏返回：

- `request_logs`：源请求、项目/应用/环境、策略快照、预算/DLP 摘要、状态、错误、延迟、Token 与估算成本。
- `gateway_request_claims`：认证后写入的请求占位和 snapshot lease。
- `routing_decisions`、`routing_candidates`：不可变路由决定、候选资格、健康、KeyPool 状态、价格和质量成本分量。
- `routing_attempts`、`stream_sessions`：Provider 尝试、HTTP 状态、归一错误、延迟、流终态。
- `budget_evaluations`、`budget_scope_events`：请求级预算评估和三级预算正式事件。
- `dlp_matches`：规则、动作、哈希、长度，不含命中文本。
- `usage_ledger`、`finops_ledger_entries`：归一 usage、价格快照、正式 CNY 分录和可追踪成本。
- `cache_affinity_selections`：不暴露 Credential/Prompt 指纹的 KeyPool/亲和选择摘要。
- `webhook_deliveries`、`webhook_delivery_attempts`：投递和尝试终态。
- 统一 Session RBAC、错误注册表、审计、Webhook 签名/重试及 Console owner 生命周期。

### 2.2 必须补齐的缺口

1. 没有 Excel 指定的独立 `GET /request-explain/{request_id}` 和 `RequestExplanation` DTO。
2. 现有详情没有显式鉴权证据，也没有把所有模块整理为有顺序、可判定完整性的决策链。
3. 现有详情只展示兼容 Usage，未同时解释正式 FinOps 分录、价格证据与预估/实际差异。
4. Webhook delivery 只有事件信封 JSON，没有 `tenant + source_request_id` 的索引化关联；
   运行时 JSON 扫描不满足性能与稳定合同，且 original/replay 有两条写入路径。
5. 查看解释没有强审计事实、持久 `request.explanation_viewed` source outbox 或崩溃恢复。
6. Console 只有调用日志列表内嵌详情，没有 PAGE-079 独立的一屏解释入口、完整状态机和双视口验收。
7. `request_logs.service` 的聚合逻辑尚无接受既有连接的公共内部入口；直接复制 SQL 会造成两个详情合同漂移。

## 3. 范围与非目标

### 3.1 本期范围

- 为当前租户的一条已持久化 Gateway 请求生成实时、只读、脱敏解释。
- admin/developer 可读；viewer 和匿名用户由后端拒绝。当前系统没有 finance/security/ops 独立角色，不能伪造角色；相关企业职责暂由 admin 承担。
- 返回固定顺序的十个决策阶段和十一个详情区块；最终 `logs_and_webhooks` 阶段拆成日志、Webhook 两个区块，每段明确 `evidence_state`。
- 解释成功读取必须写不可变 `RequestExplanation` 访问审计；同一行兼作持久 outbox 异步投递，避免重复审计事实漂移。
- 为 Webhook delivery 建立源请求关联表，并覆盖 original/replay。
- 新增 0044、共享合同、Control API、PAGE-079、专项测试和真实 Runtime/Browser 验收。

### 3.2 明确不做

- 不跨多个 request_id 聚类、推断根因或生成事故结论。
- 不返回 Prompt、Response、messages、Authorization、Virtual/Provider Key、Endpoint URL、Webhook URL/Secret、Credential ID、原始 Provider 正文或原始异常文本。
- 不把当前配置回算成历史决定；只读取请求时冻结事实，当前名称仅作为非权威显示标签。
- 不新增第二套路由、DLP、预算或账本事实，不同步扫描文件日志，不接 OTel 后端。
- 不在解释页修改策略、预算、Key、Webhook 或重放投递。
- 不为无法归属 tenant 的认证失败请求建立可枚举全局索引；无租户锚点的请求不对管理 API 开放。
- 不引入硬件盒子、MCP 主链路、信创或复杂数据出境。

## 4. 领域模型与完整性语义

### 4.1 两类 request_id

- `source_request_id`：path 中被解释的 Gateway 请求，最长 128，严格使用 `[A-Za-z0-9_.:-]`。
- `request_id`：本次 Control API operation，由统一中间件生成/恢复，出现在响应顶层、Audit 与查看事件中。
- `source_trace_id` 与 operation `trace_id` 同样分离；禁止用可能复用的 trace_id 代替源请求主键查询。

### 4.2 RequestExplanation

`RequestExplanation` 是一次“谁在何时查看了哪个请求、当时聚合完整性如何”的持久访问事实兼
Webhook source outbox，不复制整份 Trace。业务事实始终以各来源表为准，响应按一次数据库读快照聚合。

状态：

- `complete`：所有已到达的必经阶段都有可信事实，未到达阶段有可证明的 `not_applicable`。
- `partial`：legacy 记录缺少事实、异步终态尚未收敛或某区块达到有界上限；不得把缺失当作 allow/success。

区块 `evidence_state`：

- `complete`：查询成功且数据未截断。
- `partial`：存在事实但不完整或被有界截断。
- `not_recorded`：阶段按链路应存在但没有可信事实。
- `not_applicable`：由更早的确定性终态证明该阶段未执行。
- `pending`：异步事实仍可能补齐。

### 4.3 决策链顺序

1. `authentication`
2. `rate_limit`
3. `budget`
4. `dlp`
5. `policy`
6. `routing`
7. `keypool`
8. `provider`
9. `usage_and_finops`
10. `logs_and_webhooks`

每个 stage 只返回稳定枚举、reason code、时间和安全 evidence reference。成功请求不能仅凭“最终 success”倒推出每个阶段细节；只有流程可证明时才标 `allowed`，否则标 `not_recorded`。

阶段业务状态固定为 `passed/blocked/failed/pending/not_evaluated/not_recorded`；它和
`evidence_state` 是两个维度。例如 Provider 明确失败可同时是 `status=failed`、
`evidence_state=complete`，遗留成功请求缺少预算证据则是 `status=not_recorded`、
`evidence_state=not_recorded`。解释完整度不得与源请求成功/失败混为一谈。

## 5. 数据库与迁移设计（0044）

### 5.1 `request_explanations`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `request_explanation_id` | TEXT PK，`rxe_` | 单次查看对象 ID |
| `event_id` | TEXT UNIQUE，`rxevt_` | 稳定 Webhook event ID |
| `tenant_id` | TEXT NOT NULL | 只来自 Session |
| `source_request_id` | TEXT NOT NULL | 被解释请求 |
| `source_trace_id` | TEXT NOT NULL | 被解释链路 |
| `actor_user_id` | TEXT NOT NULL | 只来自 Session |
| `source_status` | `success/failed` | 源请求终态 |
| `status` | `complete/partial` | 解释完整性 |
| `evidence_version` | `request_explanation_v1` | 聚合规则版本 |
| `section_status_json` | JSON object | 固定十一个详情区块状态，不含业务正文 |
| `missing_sections_json` | JSON array | 缺失区块枚举 |
| `truncated_sections_json` | JSON array | 达到上限的区块枚举 |
| `source_created_at/source_updated_at` | UTC TEXT | 聚合水位摘要 |
| `operation_request_id/operation_trace_id` | TEXT NOT NULL | 本次查看链路 |
| `event_dispatch_status` | pending/processing/dispatched/failed | source outbox 状态 |
| `event_dispatch_attempts` | 0..10 | at-least-once 尝试数 |
| `event_next_attempt_at` | UTC TEXT nullable | 重试时间 |
| `event_claim_token/event_lease_owner_id/event_lease_expires_at` | nullable | 30 秒 lease/CAS |
| `event_last_error_code/event_dispatched_at` | nullable | 固定错误码与终态 |
| `viewed_at/created_at/updated_at` | UTC TEXT | 审计时间 |

约束：身份、源请求、完整性 JSON、operation 链路和 `viewed_at` 不可变；只允许 outbox 字段按状态机更新。触发器阻止跨 tenant owner、`REPLACE`、rowid 绕过、非法时间和非法 lease 组合。

索引：

- `(tenant_id, source_request_id, viewed_at DESC)`
- `(tenant_id, actor_user_id, viewed_at DESC)`
- partial index `(event_dispatch_status, event_next_attempt_at, created_at)`

### 5.2 `request_webhook_delivery_links`

字段：`request_webhook_delivery_link_id`、`tenant_id`、`source_request_id`、`webhook_delivery_id`、`event_id`、`event_type`、`delivery_kind`、`created_at`。

- 一条 delivery 最多关联一个源请求；状态仍从 `webhook_deliveries` 读取，避免复制可变终态。
- 索引 `(tenant_id, source_request_id, created_at DESC)`，并唯一约束 `(tenant_id, webhook_delivery_id)`。
- 插入触发器核对 link 与 delivery 的 tenant/event/type/kind 一致；表 append-only。
- 0044 只从已通过 `json_valid` 的安全信封回填：优先 `payload.source_request_id`，其次 `payload.request_id`，仅对白名单事件允许以 envelope `object_id` 作为请求 ID。未知事件不猜测。
- 0044 使用 `AFTER INSERT ON webhook_deliveries` 触发器统一生成 link，避免同时修改 Control 与
  Runtime dispatcher：original 只从完整 Webhook 信封的 `$.payload.source_request_id` 或明确
  白名单事件的 `$.payload.request_id` 提取；replay 只继承其 original link，绝不重新猜测 payload。
- 触发器在同一 delivery 事务内校验 `json_valid`、安全 ID、tenant/event/type/kind 和源
  `request_logs` owner；无可信 request ID 时不写 link。历史回填复用完全相同的提取规则。

### 5.3 既有索引补强

- 新增 `budget_scope_events(tenant_id, request_id, created_at, budget_scope_event_id)` 复合索引，
  避免解释单请求预算时依赖非租户索引或扫描租户历史。
- 新增 `usage_ledger(tenant_id, request_id, created_at, usage_ledger_id)` 复合索引；正式 FinOps
  已有 `(tenant_id, source_request_id, ...)` 索引，不重复建设。
- 所有 explain fan-out 查询通过 `EXPLAIN QUERY PLAN` 验证租户复合索引，禁止依赖
  `json_extract(payload_json, ...)` 运行时扫描。

### 5.4 访问审计与 down migration

- `request_explanations` 自身是 append-only 访问审计并兼作 source outbox，完整保存 tenant、actor、session、源请求、operation 链路和查看时间；不再向通用 `audit_logs` 复制第二份同义事实。
- 单条 `request_explanations` 在短写事务提交；写入失败不得返回已经聚合出的敏感解释。
- down 在存在 RequestExplanation 或 `request.explanation_viewed` delivery/attempt 时于
  DDL 前原子拒绝；纯派生的历史 link 可丢弃。down 不递归删除任何外部文件或测试证据。
- 空事实 up/down 必须逐列、索引、触发器、checksum、FK 和 `integrity_check` 恢复 0043。

## 6. API 设计

### 6.1 `GET /request-explain/{request_id}`

权限：`require_request_explanation_reader`，允许 admin/developer；anonymous 401、viewer 403。依赖先于未信任 path/query/body 业务校验执行。

输入合同：

- 不接受 query 参数、请求正文、Content-Encoding 或重复/畸形链路头。
- path 必须是 1..128 ASCII 安全 ID；不 trim、不把两个外部值折叠为同一 ID。
- 不接受 `tenant_id`、actor、include_raw、跨请求列表或动态字段选择。

成功头：

- `Cache-Control: no-store, private`
- `Pragma: no-cache`
- `Vary: Authorization`
- 统一 `X-Request-ID`、`X-Trace-ID`

响应骨架：

```json
{
  "request_explanation": {
    "request_explanation_id": "rxe_...",
    "source_request_id": "req_...",
    "source_trace_id": "trc_...",
    "status": "complete",
    "evidence_version": "request_explanation_v1",
    "captured_at": "2026-07-27T00:00:00Z",
    "summary": {},
    "decision_chain": [],
    "authentication": {},
    "rate_limit": {},
    "budget": {},
    "dlp": {},
    "policy": {},
    "routing": {},
    "keypool": {},
    "provider_response": {},
    "usage_and_finops": {},
    "logs": {},
    "webhooks": {},
    "missing_sections": [],
    "truncated_sections": [],
    "view_event_status": "pending"
  },
  "request_id": "req_control_operation",
  "trace_id": "trc_control_operation"
}
```

### 6.2 区块白名单

- 鉴权：Virtual Key 稳定 ID/前缀、Environment/Project/Application 归属、claim 时间、snapshot lease；不返回 Key hash/value。
- 预算：请求级各 scope 评估、micros/Decimal 字符串、规则/作用域/action/reason；不用 float 重算。
- DLP：rule ID/name/type/action、message index、tenant-salted hash、长度；不返回 pattern 或命中文本。
- 策略：冻结 snapshot/version/decision/action/reason/target ID；不返回 DSL、AST、上下文实际值。
- 路由：不可变 Decision/Candidate、剔除原因、质量/成本/延迟分量；不以当前配置回算。
- KeyPool：候选 `keypool_state`、CacheAffinity 安全 Selection、实际尝试是否获得可用凭据；Credential/Binding/指纹始终省略。
- Provider：attempt sequence、provider/model、status、HTTP status、归一 error/category、retryable、latency；不返回原始响应或 error summary。
- Usage/FinOps：Token、cache token、usage source、estimated、价格 snapshot/schema、正式分录 micros/CNY 证据/额度类型；不返回 `raw_usage`。
- Logs：RequestLog 安全摘要、流终态和统一 error diagnosis；不扫描文件日志。
- Webhook：事件类型、endpoint ID/name、delivery/replay 状态、次数、HTTP status、固定 error code、next retry、attempt 安全摘要；不返回 URL、payload、签名或 error summary。

## 7. 聚合、事务与事件流程

1. 后端认证恢复 tenant/actor，严格校验无 query/body 和 source ID。
2. 只读连接显式 `BEGIN` 开启一致性读事务，以 `(tenant_id, source_request_id)` 查询
   `request_logs` 锚点；不存在或跨租户均 404。
3. 使用同一连接复用“连接感知”的 RequestLog 内部聚合入口，并按固定上限加载鉴权、正式
   FinOps、Webhook link/delivery/attempt；Explain 路径不读取 feedback，禁止 helper 再开连接破坏快照。
4. 按源请求终态和事实存在性生成 stage/section 状态；所有排序均有稳定 ID 尾键。
5. 结束读事务并关闭连接，不在网络操作期间持有 SQLite 锁。
6. 新短连接 `BEGIN IMMEDIATE` 写不可变 `request_explanations` 访问审计；actor/session/tenant/source owner
   再次由 SQL/trigger 校验。写失败整体回滚，响应不返回已读出的敏感解释。
7. 提交后返回解释；source event 保持 `pending`。后台 worker 以 lease/CAS 领取、关闭连接后调用公共 Webhook dispatcher，再回写 dispatched 或退避。
8. 服务启动恢复 pending/过期 processing；稳定 event ID 提供 at-least-once，不宣称网络 exactly-once。

读取上限：Candidate 64、Attempt 8、Budget 16、DLP 100、Usage 16、FinOps 32、Webhook delivery 100、Webhook attempt 300。每个集合统一查询 `LIMIT + 1`，多出的唯一一行只用于判定 `truncated=true`；响应返回前 N 行，整体 status 为 partial，不额外执行无界 `COUNT`。

## 8. Webhook 事件合同

`request.explanation_viewed` 使用持久 `rxevt_`：

- envelope：`tenant_id`、可信 `actor_id`、`object_id=request_explanation_id`、operation `trace_id`。
- payload 精确字段：`schema_version`、`request_explanation_id`、`source_request_id`、`source_trace_id`、`source_status`、`explanation_status`、`missing_sections`、`truncated_sections`、`evidence_version`、`viewed_at`。
- 不发送页面筛选、Key/Provider Credential、Prompt/Response、Endpoint、成本明细或 DLP hash。
- 共享构造器校验 exact keys、ID 前缀、枚举、时间、actor/object/trace 绑定；敏感键递归门禁继续生效。

## 9. 错误码设计

| 错误码 | HTTP | 场景 |
| --- | ---: | --- |
| `request_explanation.not_found` | 404 | 当前 tenant 不存在该源请求；跨租户同形 |
| `request_explanation.request_id_invalid` | 422 | path 非安全 ASCII、空/超长/规范不一致 |
| `request_explanation.request_invalid` | 422 | query/body/header 合同非法 |
| `request_explanation.unavailable` | 503 | 数据库、schema 或聚合依赖暂不可用，可重试 |
| `request_explanation.audit_unavailable` | 503 | 访问事实/Audit 无法原子保存，失败关闭 |
| `request_explanation.integrity_failed` | 500 | 同租户 owner 图或持久 JSON 违反不变量 |
| `request_explanation.event_unavailable` | 503 | outbox 领取/回写故障；后台记录，不覆盖已提交读取响应 |

认证继续使用现有统一 401/403。所有错误由全局处理器携带 operation `request_id/trace_id`；日志只记录固定 code、tenant/user/explanation/source request ID 等安全标识，不记录 SQL/异常正文。

## 10. 安全、权限与租户隔离

- tenant/actor 只来自 Session；源请求、子表、Webhook link、Audit 每次查询/写入都带 tenant 条件。
- 跨租户与不存在返回同一 404，不通过不同延迟、计数或错误文本暴露存在性。
- viewer 即使手写 hash/API、伪造 DOM 或发送畸形参数，后端仍先 403；前端零 DOM/零预取只是补充。
- API 与页面不返回或记录 Provider Key、Token、Prompt、Response、raw usage、原始上游错误、Webhook URL/Secret、DLP 命中文本。
- 当前 Provider/模型/Key 名称如需显示，必须 tenant-qualified；不可变 ID/请求时快照是权威值，当前标签显式标注 display-only。
- `repr`、异常转换、JSON 解析、Audit/Webhook payload 均加入敏感键和长 credential 片段扫描测试。
- Console 不把 source request ID、响应、错误详情放入 URL、localStorage/sessionStorage；离页、登出、换租户、降权会 abort 并清空 DOM/内存。

## 11. PAGE-079 前端设计

新增后端导航 `request-explanations`，admin/developer 可见，viewer 不可见。页面不是营销页，也不嵌套卡片：

- 顶部紧凑 request ID 输入、查询、刷新与状态摘要。
- 单页垂直决策链，十个阶段使用稳定图标/状态色和折叠明细；默认展开异常、阻断、partial，其他阶段保持紧凑。
- 一次 API 获取全部内容，不从浏览器并发拼接十个权限不同的接口。
- Request Logs 行增加“解释”动作，把 ID 放入当前身份绑定的内存后切换页面；不写 URL/Storage。
- 状态：idle、loading、complete、partial、not-found/error。viewer/匿名没有页面 DOM；401 触发全局会话失效。
- owner 包含 tenant/user/role/session/session revision/navigation/source ID/request sequence；迟到响应不得覆盖。
- PAGE-079 本身不再额外发送通用 `console.page_viewed` 来代替业务事件；权威
  `request.explanation_viewed` 由成功 explain API 在后端原子创建，避免页面 abort/刷新导致漏审计。
- 1280x720 使用摘要 + 时间线双列，390x844 改为单列；表格转 label/value 行，禁止横向溢出、文本遮挡和按钮尺寸跳动。
- 所有动态文本转义；micros/大整数以字符串/BigInt 格式化，不用 Number 回算成本或分数。

## 12. 性能、兼容与降级

- 核心锚点、各事实表和新 Webhook link 均走 `tenant + request/source` 索引；禁止 `json_extract` 全表扫描和 N+1 Endpoint 查询。
- 一次只读事务固定聚合视图；短写事务只写 access/Audit，不阻塞 Gateway 热路径。
- 缺少 optional 行是业务状态，不抛 500；缺表、锁、损坏 JSON、owner 不一致才统一失败。
- legacy 请求允许 partial；旧 `GET /request-logs/{id}` 响应保持兼容，并通过接受连接的内部 loader 复用而不是更改公开 wire。
- Webhook link 是派生索引，未知事件不关联；回放继承源关联，不能从任意 payload 字段猜 ID。
- 页面仅渲染服务端返回的固定 schema；未知枚举显示安全 fallback，不执行动态 HTML。

## 13. 测试设计

### 13.1 Shared 与迁移

1. 事件 exact payload/envelope、ID/actor/object/trace 绑定、敏感键和任意嵌套拒绝。
2. request ID 提取只接受白名单字段/事件；未知/畸形/凭证片段不建 link。
3. 0044 up/down、43/44 版本、checksum、Audit 保真、空事实回滚。
4. explanation/link owner、append-only、状态机、lease CAS、REPLACE/rowid、跨租户和 down guard。
5. 旧 delivery backfill、数据库触发器覆盖 Control/Runtime original 与 replay link、索引 query
   plan、非法/未知 payload 不误关联；dispatcher 代码无需双写。

### 13.2 Control API 与 outbox

1. admin/developer 成功；anonymous 401、viewer 403，权限先于输入解析。
2. 同租户 success/failed/预算阻断/DLP 阻断/Provider 失败/stream/fallback 的十阶段、十一区块与决策链。
3. 跨租户/不存在同形 404；非法 path、query、body、header 422。
4. legacy 缺证据返回 partial；前置阻断后的后续阶段为 not_applicable；异步未完成为 pending。
5. 候选/匹配/投递超限可见截断；金额和大整数不丢精度。
6. 数据库 lock/缺表/关闭/坏 JSON/owner 故障统一 503/500且无泄露，连接均关闭。
7. Explanation 访问审计原子写入；回滚零残留。重复 GET 产生不同查看事实，不重复源业务事实。
8. outbox pending、lease 抢占、过期恢复、失败退避、最大次数、崩溃窗口、稳定 event ID 和 pending resume。
9. Webhook delivery/attempt 只返回白名单，不含 URL/payload/error summary。

### 13.3 Console 与 Browser

1. API client URL encode、无 tenant query、统一错误和 request_id。
2. owner/abort/迟到响应/换租户/降权/登出/离页清理；source ID 不进入 URL 或 Storage。
3. 十阶段、完整/partial/error/空态、转义、micros/BigInt、折叠和焦点恢复。
4. admin/developer 可见；viewer 手写 hash 零 DOM/零网络。
5. 1280x720、390x844 的完整/partial/错误状态截图、像素非空、overflow/overlap/clipping=0。
6. Browser 网络只访问 5173/8001 与本地验收端点；Console/page/browser error 为 0，预期 4xx/5xx 单列。

### 13.4 回归与运行验收

- 先跑新增 Shared、Migration、Control、Console 单点，再跑 request logs、audit、webhooks、routing、DLP、budget、FinOps 相邻回归。
- 局部失败只复现和重跑受影响节点；候选稳定后至多执行一次所需根级全量，不反复运行小时级全量。
- `TEMP/TMP` 与所有 `--basetemp` 固定在 `D:\MetaHub\.tmp`；不提权、不使用 `C:\tmp`。
- 运行中文注释门禁、Node 示例、Console 全量/lint。
- 在开发库副本实迁移 0044，真实启动 Gateway/Control/Console/本地上游/Webhook receiver，验证 API、事件、页面、数据库与日志隐私。
- 登出全部验收 Session、停止服务、端口归零、checkpoint、integrity/FK、sidecar/spool 清空，生成独立 final audit。

## 14. 代码修改计划

1. `infra/migrations/0044_request_explainability_center.sql/.down.sql`。
2. Shared errors/i18n/webhooks 与严格事件构造器；delivery link 由 0044 数据库触发器统一生成，
   Runtime/Control dispatcher 不双写。
3. Control 新增 `request_explanations/{models,wire,service,routes,outbox,scheduler}.py`，注册 router/lifespan/RBAC。
4. RequestLog service 提取接受既有连接的聚合入口并补齐 `finally close`，公开旧响应不变。
5. RequestExplanation append-only、owner 与 outbox CAS/lease 数据库门禁。
6. Console 新增 `lib/requestExplanations.js`、PAGE-079 state/render/bind，接入导航和 RequestLog 跳转。
7. 新增 Shared/Control/Migration/Console/Runtime/Browser 合同测试。
8. 更新 `.env.example`、README、目录说明、API 文档、测试记录和验收证据。

## 15. 编码前自审状态

### 第一轮：数据链、完整性、事务与性能

- 状态：已完成（2026-07-27），P0/P1 已关闭。
- 审查发现与修正：
  1. 初稿要求两套 dispatcher 写 link，存在 original/replay 漏写和事务漂移；改为 0044
     `AFTER INSERT` 触发器统一生成，历史 backfill 复用同规则。
  2. `budget_scope_events` 与 `usage_ledger` 缺少适合本查询的 tenant+request 复合索引；0044
     明确补齐并以 query plan 验证。
  3. RequestLog feedback helper 会另开连接，破坏解释页单快照；改为连接感知 loader，Explain
     明确排除非验收所需 feedback。
  4. 聚合后持读锁执行写入/网络会放大 SQLite 争用；改为先结束只读事务，再用短
     `BEGIN IMMEDIATE` 原子写 Explanation 访问审计，网络仅由后台 outbox worker 执行。
  5. 初稿只有固定上限，没有冻结截断算法；所有有界集合改为稳定排序的 `LIMIT + 1`，
     禁止额外无界 `COUNT`。
  6. 补齐 source 业务状态与 evidence 完整性双维语义，异步 Trace/Usage/Webhook 可以
     `pending/partial`，不得使用当前配置回算历史事实。
- 第一轮结论：数据链、事务边界、索引和崩溃恢复方案可实施，无未关闭 P0/P1。

### 第二轮：RBAC、租户、wire、隐私与前端生命周期

- 状态：已完成（2026-07-27），P0/P1 已关闭。
- 审查发现与修正：
  1. Excel 角色描述包含尚不存在的运维/安全/财务角色；本 PRD 不伪造 PRD-059 能力，冻结
     admin/developer 可读、viewer 403，并由独立后端依赖在业务参数解析前执行。
  2. path 的源 request ID 与 operation request_id/trace_id 容易混淆；DTO、Audit、Webhook
     全部使用 `source_*` 与 operation 顶层字段分离，跨租户/不存在统一 404 防枚举。
  3. 响应字段按精确 Pydantic wire 白名单冻结，未知 query/body/header 422；所有 SQL join
     显式 tenant 条件，owner 不一致失败关闭，错误仍携带 operation request_id/trace_id。
  4. 删除 Provider 原始响应/错误正文、Endpoint/Webhook URL、raw usage、Credential/Binding/
     指纹、Prompt/Response、DLP 命中文字和策略 DSL/AST；Webhook/Audit 同样执行递归敏感键门禁。
  5. Console owner 增加 tenant/user/role/session revision/navigation/source/sequence，权限收窄、
     换租户、登出、离页均 abort+清空；viewer 手写 hash 必须零页面 DOM、零 explain 网络。
  6. 页面冻结 idle/loading/complete/partial/not-found/error，1280x720 双列、390x844 单列，
     动态文本转义，大整数与 micros 不经 Number，测试检查 overflow/overlap/clipping。
  7. 成功响应固定 `Cache-Control: no-store, private`、`Pragma: no-cache`、
     `Vary: Authorization`；source ID 不进入 URL、localStorage 或 sessionStorage。
  8. 交叉核对架构同步链时发现响应骨架漏了 `rate_limit`；已补为十个决策阶段、十一个详情区块，并在共享/Console 合同测试冻结字段集合。
- 第二轮结论：RBAC、租户、防枚举、严格 wire、隐私和前端生命周期均有可验证门禁；无未关闭
  P0/P1，编码许可已开启。

## 16. 实现后两轮交叉自审

### 第一轮：数据库状态机、事务与历史证据一致性

- 状态：已完成（2026-07-28），P0/P1 已关闭。
- 审查范围：0044 up/down、Repository 有界 SQL、Explanation 访问事实、Webhook delivery link、
  source outbox claim/finish、scheduler 生命周期及对应迁移/API/边界测试。
- 发现与修正：
  1. 原 outbox 恢复条件只领取 `attempts < 10`，若第十次网络调用后进程在 CAS 回写前退出，
     行会永久停在过期 `processing + attempts=10`。现由 claim 短事务先把一个最老的该类孤儿
     lease 终结为固定 `failed`，不发起第十一次网络调用；定点复验通过。
  2. delivery link 增加 endpoint tenant 绑定检查，避免仅凭同 tenant delivery ID 关联到错误
     Endpoint；原始 URL、payload、签名和 error summary 仍不复制进 link 或 Explain DTO。
  3. RequestLog 聚合 helper 改为复用调用方连接，Explain 的所有源事实继续位于同一 SQLite
     snapshot；读取事务先 rollback/close，访问事实再以独立 `BEGIN IMMEDIATE` 提交，网络不在事务内。
  4. 所有集合再次核对为 tenant 条件、稳定尾键和 `LIMIT + 1`，候选、attempt、DLP、Usage、
     FinOps、Webhook delivery/attempt 均不会因新增历史行形成无界读取。
- 第一轮结论：崩溃窗口、最大尝试次数、owner、单快照和连接生命周期均有代码与定点测试
  证明，无未关闭 P0/P1。

### 第二轮：API/Console 权限、隐私、兼容性与验收器真实性

- 状态：已完成（2026-07-28），P0/P1 已关闭；Runtime/Browser 的最终通过状态仍以测试记录为准。
- 审查范围：FastAPI 依赖顺序、严格 wire、统一错误、DTO exact-key、导航/偏好、PAGE-079
  owner/handoff/renderer、Runtime/Browser 验收器和敏感信息扫描。
- 发现与修正：
  1. Control 导航最初未完整注册 `request-explanations`，已冻结 admin/developer 可见、viewer
     不可见，并由后端偏好接口拒绝 viewer 手写 PAGE-079；导航相邻回归通过。
  2. Runtime fixture 曾查询不存在的 `environments.environment_code`，会把验收器自身错误误报为
     产品失败；已改为正式 `environment.code AS environment_code`，并由验收器合同测试冻结。
  3. DLP、Provider attempt 和 Webhook 投影逐字段复查，正文、URL、credential、raw error、
     Prompt/Response 均不进入响应、DOM 或证据；验收器递归禁键补入 `password`。
  4. PAGE-079 只从一次性内存 handoff 接收源 ID，地址栏固定 hash；换用户、换租户、降权、
     登出、离页和迟到响应均清空 owner，viewer 无 DOM、无 explain 预取。
  5. API 静态交叉审查确认所有源表查询均带可信 Session tenant，anonymous/viewer 的 RBAC
     先于 path/query/header/body 校验，跨租户与不存在保持同形 404，成功读取失败审计时整体失败关闭。
- 第二轮结论：实现与冻结设计一致，未发现新的未关闭 P0/P1。专项、一次根级全量及原失败
  节点定向闭环、真实 Runtime/Browser 和停服静止终态均已取得直接证据，结果以测试记录为准。

## 17. 最终验收与完成结论

- 聚焦 Python `69 passed`，outbox 第十次 lease 边界 `1 passed`；Runtime/Browser 验收器合同分别
  为 `8/8`、`9/9`，Console 全量 `414/414`，lint `139 files passed`，中文注释门禁通过。
- 根级 Python 全量实际结果为 `1227 passed / 58 failed`；58 个失败均由历史迁移测试仍把 0043
  当作栈顶造成，修正后只重跑原失败节点并取得 `58/58 passed`，两个受影响迁移文件复验为
  `67/67 passed`，没有把定向闭环虚写成第二次全量零失败。
- 权威批次 `artifacts/prd054/20260728T040841` 的 Runtime 与 Browser 均为 `status=passed`；
  0044 实迁移、数据库完整性/外键、三服务健康、双视口六张正式截图、权限/租户/异常路径、
  Session 注销、日志隐私和停服静止终态全部通过。
- pytest 临时项全程限定在 `D:/MetaHub/.tmp`，只删除 588 个链接节点并保留 588 个目标；未使用
  `C:/tmp`、未提权，最终 reparse point 为 0，清理与终态证据哈希稳定。

据此，PRD-054 已达到可运行、可验收状态并标记完成。该结论仅适用于 PRD-054，不表示全部
108 条 PRD 已完成；下一条仍按 Excel 里程碑和优先级重新核对后推进。
