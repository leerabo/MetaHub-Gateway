# PRD-081 SIEM / 安全事件格式设计

状态：**已完成**（2026-08-16）。本文冻结 PRD-081 的权威范围、0074 数据合同、API、标准安全事件、
JSON/CEF wire、聚合、限流和恢复语义；第 16、17 节保存两轮编码前自审，第 19、20 节保存两轮
实现后自审及修正。本条不重复 PRD-057 Webhook 可靠性，也不启动 PRD-082；总体状态继续保持
`all_108_prds_complete=false`。

## 1. 权威范围与交付边界

### 1.1 交叉确认

- Excel `01_PRD总表` 第 82 行与 `03_M2-M3扩展交付` 第 18 行一致确认：
  `PRD-081 | M2-企业增强 | P2 | 扩展层 | Webhook SIEM | SIEM/安全事件格式`。
- 用户为安全/运维团队；故事为“安全事件可以进入企业 SIEM”。
- 实现范围固定为 JSON 和 CEF、安全事件字段标准化、事件聚合与限流、HMAC 签名。
- PAGE-113 是现有 Webhook 设置页的 SIEM 能力，不新增第二套 Endpoint 管理页面。
- API-370/371 都指向现有 `POST /webhooks` 增加 `format`，不是新建厂商专有路由。
- OBJ-110 为 tenant-qualified `SecurityEventPayload`；EVT-093 为 `security.event_pushed`。
- TC-0161/0162 要证明 DLP、会话风险和 Provider Key 异常可按标准格式推送，且格式、限流、
  权限、统一错误、安全和异常状态可追踪。
- 依赖是已完成的 Webhook 基础/死信、DLP、会话风险和 Provider 错误事实；当前最高迁移为
  `73/0073`，本条迁移固定为 `0074`。

### 1.2 明确不做

- 不对接 Splunk、QRadar、ArcSight、Sentinel 等厂商专有 API，不保存厂商 Token。
- 不把 OpenRouter、硅基流动加入默认上游；Provider 目录和路由不在本条修改。
- 不把 Prompt、Response、DLP 命中文本、规则 pattern、Provider Key、Key prefix、Authorization、
  Endpoint Secret、签名 Header 或接收端响应正文放入事件、日志、DOM 或验收证据。
- 不删除或改写既有 `security.dlp_matched`、`security.session_high_risk` 等订阅合同；新标准事件是
  并行的 SIEM 投影，已有客户 Endpoint 保持兼容。
- 不承诺网络 exactly-once。稳定 event/delivery ID、冻结正文、source outbox 和接收端幂等共同提供
  at-least-once 可恢复语义。
- 不历史回灌 0074 前的 DLP、会话风险或 Provider 错误事实，避免升级后向新 SIEM Endpoint 突发推送
  旧事件；0074 只捕获迁移提交后的新 source。

## 2. 现状约束与关键决定

1. 现有 Endpoint 已具备 tenant owner、`webhook.manage` 后端权限、服务端生成/加密保存 HMAC Secret、
   轮换、delivery/attempt、死信和持久 replay；PRD-081 必须扩展这些表和发送内核。
2. 现有发送固定 JSON，replay 依赖已保存 `payload_json`，且恢复路径会在每次真实 attempt 前受控切换到
   Endpoint 当前 Secret 版本。仅在 Endpoint 保存 format 不足以
   保证重放，0074 必须在每条 delivery 冻结 `payload_format` 和实际 UTF-8 wire body。
3. `security.event_pushed` 是标准化聚合事件；payload 的 `source_event_type` 标识
   `security.dlp_matched`、`security.session_high_risk` 或 `security.provider_key_anomaly`。
4. 原安全事件仍可按旧订阅直接发送。SIEM Endpoint 订阅 `security.event_pushed`，JSON 与 CEF 只改变
   wire 表达，不改变逻辑 event ID、tenant、计数或时间窗口。
5. DLP 的可靠事实是 `dlp_matches`；会话风险已有不可变 `session_risk_events`；Provider Key 异常包括
   `provider_error_facts.normalized_output_error_code='gateway.upstream_auth_failed'` 和新增的本地托管密文
   `decrypt_failed` source。前者表示官方/云服务对当前租户 Provider 凭据的认证拒绝，后者表示 Gateway
   无法读取托管密文；两者都不外发 Credential ID、前缀、密文或错误正文。
6. 数据库 trigger 在上述 source 事务内建立标准 source、聚合成员和 outbox。网络只在事务提交后由
   Control scheduler 执行，不把 HTTP 延迟放入 Gateway 热路径，也不依赖进程内日志队列。
7. 聚合窗口固定 5 秒；限流按 Endpoint 独立执行，默认 60 个 `security.event_pushed` delivery/UTC 分钟，
   可在创建 Endpoint 时设置 1..600。达到上限只延迟，不丢弃、不伪造成功、不消耗 HTTP attempt。

## 3. 标准 `SecurityEventPayload`

### 3.1 Webhook 信封

生产事件固定使用现有七字段信封：

| 字段 | 合同 |
|---|---|
| `event_id` | `sevt_` + 32 小写 hex；也是聚合事件的稳定幂等 ID |
| `event_type` | 固定 `security.event_pushed` |
| `timestamp` | 聚合窗口结束 UTC `Z` 秒 |
| `tenant_id` | 仅从 source owner 恢复 |
| `actor_id` | 固定 `security-event-aggregator`，不冒充原用户或 Virtual Key |
| `object_id` | 当前 `security_event_id` |
| `trace_id` | 聚合中最后一条 source 的 trace，用于定位最近事实 |

`signature` 不进入正文；它继续由 `X-MetaHub-Signature` Header 表达，并对本次实际 JSON/CEF 字节签名。

### 3.2 payload exact 字段

`payload` 必须恰好包含以下字段，不允许自由扩展：

| 字段 | 类型/取值 |
|---|---|
| `schema_version` | 整数 `1` |
| `security_event_id` | 与信封 `event_id/object_id` 相同 |
| `status` | 固定 `aggregated` |
| `category` | `data_loss_prevention/session_risk/provider_credential` |
| `source_event_type` | 三个固定 source 类型之一 |
| `severity` | `low/medium/high/critical` |
| `severity_score` | 原生整数 `0..10`，与 severity 映射一致 |
| `action` | source 对应的固定动作枚举 |
| `outcome` | `observed/blocked/detected/frozen/released/resolved/expired/rejected` |
| `resource_type` | `dlp_rule/session_risk_score/provider` |
| `resource_id` | 同租户不透明资源 ID；不允许 Credential ID 或 Key prefix |
| `event_count` | 窗口内安全发生次数，正整数 |
| `source_event_count` | 被聚合的不可变 source 行数，正整数且不大于 `event_count` |
| `first_seen_at/last_seen_at` | source 最早/最晚发生 UTC 秒 |
| `window_started_at/window_ended_at` | 5 秒半开窗口 `[start,end)` |
| `first_request_id/last_request_id` | 窗口首尾安全请求坐标；不返回列表 |
| `created_at/updated_at` | 聚合对象创建/最后纳入 source 的 UTC 秒 |

severity 映射固定如下：DLP `allow=low(3)`、`log_only=medium(5)`、`block=high(8)`；会话
`high_risk=high(8)`、冻结动作 `critical(10)`、dismiss/release/expire `medium(4)`；Provider 认证拒绝
`critical(9)`。severity 文本、分数、action、outcome 必须由数据库 source 重建，调用方不能提交。

## 4. JSON 与 CEF wire

### 4.1 JSON

- `Content-Type: application/json`。
- 使用现有 canonical JSON：UTF-8、key 排序、无多余空白、禁止 NaN/Infinity。
- delivery 的 `wire_body_text` 必须逐字等于 canonical `payload_json`。

### 4.2 CEF

- `Content-Type: text/plain; charset=utf-8`，一条 delivery 恰好一行且无 CR/LF 终止符。
- 完整 CEF UTF-8 wire 最多 16,384 bytes；超限在冻结 delivery 前失败关闭，不截断字段或拆成多行。
- Header 固定：
  `CEF:0|MetaHub|AI Gateway|1|<source_event_type>|<name>|<severity_score>|<extension>`。
- `<name>` 固定映射为 `DLP match`、`High-risk session` 或 `Provider credential anomaly`，
  不使用数据库自由文本。
- extension 按固定顺序输出：`externalId`、`rt`、`start`、`end`、`cnt`、`cat`、`act`、
  `outcome`、`cs1Label=TenantID cs1=...`、`cs2Label=ResourceType cs2=...`、
  `cs3Label=ResourceID cs3=...`、`cs4Label=TraceID cs4=...`、
  `cs5Label=FirstRequestID cs5=...`、`cs6Label=LastRequestID cs6=...`、
  `cn1Label=SourceEventCount cn1=...`。
- `rt/start/end` 固定为对应 UTC 秒的 Unix epoch milliseconds 十进制原生整数；不接受本地时区文本、
  浮点时间或不同 CEF 接收器可能产生歧义的自由日期格式。
- CEF Header 值转义 `\\` 与 `|`；extension 值转义 `\\`、`=`、CR、LF。所有字段先经过 exact
  validator 和长度/字符集限制，再格式化，防止换行或扩展字段注入。
- `webhook.test` 对 CEF Endpoint 使用固定 `Webhook test` CEF 行，只含随机 test event ID、tenant、trace，
  不伪造生产 `security.event_pushed`，也不计入安全事件限流。

### 4.3 HMAC 与 Header

- `X-MetaHub-Event` 直接取冻结 delivery 的 `event_type`，不再通过 `json.loads(body)` 推断。
- `X-MetaHub-Delivery` 继续使用稳定 delivery ID。
- `X-MetaHub-Signature` 保持现有 `t=<epoch>,v1=<HMAC-SHA256>` 合同，但输入必须是本次实际
  JSON/CEF UTF-8 bytes；重试和 replay 均使用 delivery 冻结的 wire body。
- 不读取或保存接收端响应正文，不记录签名、Secret、wire body 或 Endpoint URL。

## 5. Endpoint API 与 PAGE-113

### 5.1 `POST /webhooks`

在既有请求增加：

| 字段 | 规则 |
|---|---|
| `format` | 可选，`json/cef`，默认 `json` |
| `security_event_rate_limit_per_minute` | 可选原生整数 `1..600`，默认 `60` |

- `format=cef` 时 `event_types` 必须恰好为 `['security.event_pushed']`；未知、混合或空订阅固定返回
  `webhook.format_invalid`。
- JSON Endpoint 可继续订阅任意既有可订阅事件，也可订阅 `security.event_pushed`。
- 路由先完成 Session、`webhook.manage` 与 tenant 恢复，再最多读取 16 KiB 正文。只接受明确 JSON
  Content-Type、UTF-8 object、唯一 key、有限嵌套和标准 JSON 常量；Pydantic 使用 strict 类型与
  `extra='forbid'`，拒绝未知字段、bool 冒充整数、NaN/Infinity、数组/标量和超限输入。未认证/无权限的
  畸形正文仍稳定返回 401/403，不能用 422 探测业务 wire。
- tenant、Secret、Secret version、max attempts、created/updated actor 仍只能由后端决定。
- `GET /webhooks`、创建响应和 delivery 摘要增加 `format`；Endpoint 还返回限流值，delivery 返回冻结值。
- 所有响应继续携带 `request_id/trace_id`；匿名、无 `webhook.manage`、跨租户和非法正文使用既有统一错误出口。
- `POST /webhooks` 不信任客户端链路 Header，中间件生成服务端 request/trace，防止名称或 Endpoint URL
  被复制到 Header 后进入 access log、响应或持久事件；整个 `/webhooks` 与 delivery/dead-letter 响应均
  `Cache-Control: no-store`。

### 5.2 PAGE-113

- 在现有创建表单增加 JSON/CEF 模式控件和安全事件每分钟上限数字输入。
- 选择 CEF 后事件订阅自动收敛为 `security.event_pushed`，但这只是交互辅助；后端仍独立校验。
- Endpoint 列表和 delivery 行显示 `JSON/CEF` 与冻结的安全事件上限；不展示 wire body、payload、Secret、
  HMAC、rate claim、source request 列表或聚合成员。
- viewer/无权限用户不渲染受限 DOM，也不预取；权限变化继续清空一次性 Secret 和页面 owner 状态。

## 6. 0074 数据模型

### 6.1 既有 Webhook 表扩展

`webhook_endpoints` 新增：

- `payload_format TEXT NOT NULL DEFAULT 'json' CHECK IN ('json','cef')`；
- `security_event_rate_limit_per_minute INTEGER NOT NULL DEFAULT 60 CHECK 1..600`。

`webhook_deliveries` 新增：

- `payload_format`：从 Endpoint 在 intent 创建时冻结；
- `wire_body_text`：实际发送的 UTF-8 文本；JSON 必须等于 `payload_json`，CEF 必须通过固定前缀、
  单行和大小约束；
- `security_event_rate_limit_per_minute`：从 Endpoint 冻结，未来 Endpoint 修改不得改变旧 delivery。

迁移把全部历史 Endpoint/delivery 设为 JSON，并把历史 `wire_body_text=payload_json`。新 trigger 使
delivery 的事件坐标、逻辑 payload、format、wire body 和冻结限流在创建后不可改写；replay 必须复制来源
delivery 的 format/body/rate。Secret 保持既有安全协议：intent 保存当时版本，但每次真实 attempt 前在
`BEGIN IMMEDIATE` 中重读 Endpoint、CAS 更新 delivery 到当前版本，再解密当前 Secret 并签名；限流延迟
发生在该刷新之前，避免零网络等待提前消耗或固化 Secret 版本。

### 6.2 `security_event_sources`

append-only、tenant-qualified source 投影：source ID、tenant、source type/object、category、severity/score、
action/outcome、resource type/id、source request/trace、`occurrence_count`、occurred/created time。

- DLP：每条新 `dlp_matches` 产生一个 source，不复制 rule name、pattern、match hash/length 或正文。
- Session Risk：每条新 `session_risk_events` 联结同租户 action 产生一个 source，不复制 scope key/digest。
- Provider：新的 auth-failed `provider_error_facts` 与 `provider_key_anomaly_events` 的 `decrypt_failed`
  都产生 source；后者只保存 tenant、Provider/模型、安全 request/trace、失败候选计数和时间，不保存
  Credential ID、Key prefix、密文、错误投影、Provider 原文 code/type/message/body 或 Endpoint。
- source ID 由权威 source ID 的 32 hex 后缀确定性派生，`(tenant,source_type,source_object_id)` 唯一；
  producer 重试不能制造重复安全事实。
- DLP 旧表允许 nullable trace；0074 source 只接受 `[A-Za-z0-9_.:-]{1,128}` 的规范 request/trace，trace
  缺失时使用同一已规范 request ID。三类权威 source ID 必须分别满足 `dlpm_/srevt_/pef_/pkae_ + 32 hex`，
  非规范直接 SQL 不能借 trigger 生成可外发事件。

### 6.3 `security_event_aggregates` 与 members

- 聚合事件 ID 由窗口内第一条 source 派生；按 tenant、category/source type、severity/score、action/outcome、
  resource type/id、5 秒窗口与 generation 分组。
- `security_event_aggregate_members` 一条 source 只能属于一个 aggregate；聚合计数、首尾时间和首尾
  request/trace 必须能从成员反向重算。
- 每个 generation 最多 1000 个 source；达到上限后即使仍在同一 5 秒窗口也创建下一 generation，防止
  单租户突发事件形成无界 trigger 工作或超大成员重算。
- outbox 尚为 pending 时允许 trigger 仅按成员重算聚合字段；一旦 worker 领取为 processing，当前
  generation 立即冻结。同窗口迟到 source 创建下一 generation，不改写已冻结/已发送事件。
- source、member、aggregate 和 outbox 通过复合 tenant 外键及 owner trigger 约束；UPDATE/DELETE、
  `INSERT OR REPLACE` 和 rowid 旁路均拒绝。

### 6.4 `security_event_outbox`

每个 aggregate 恰好一行：tenant/event、`pending/processing/succeeded/failed`、attempt、lease generation、
owner/token/expiry、next attempt、snapshot marker、last error、创建/更新时间和成功/失败时间。

- 首次 `next_attempt_at=window_ended_at`，保证窗口闭合后才冻结 payload。
- claim 使用 `BEGIN IMMEDIATE`、tenant/event/status/generation/token/owner/未过期 lease 完整 CAS。
- worker 在同一短事务验证成员重算、构建 exact event，并冻结当时 active `security.event_pushed`
  Endpoint、delivery、format/body/rate 和 intent 时 Secret version；随后关闭事务。每次真实网络前仍按
  既有轮换合同受控刷新到当前 Secret，format/body/rate 永不随之变化。
- 崩溃后只恢复冻结 delivery，不吸收后来创建的 Endpoint。无订阅者也保存空快照并成功收敛。
- 确定性坏 source/payload 隔离为 `webhook.payload_invalid`；瞬时数据库/网络失败使用 5/30/300 秒退避，
  source 最多 10 次恢复。delivery 达到既有三次网络上限后进入死信，source 同轮 failed。

### 6.5 `webhook_security_rate_claims`

- 每个 `security.event_pushed` delivery 在首次真实网络前按 tenant + Endpoint + UTC minute 原子领取一个
  `1..frozen_limit` slot；同一 delivery 最多一条 claim。
- slot 满时 delivery 保持 pending，`next_retry_at` 指向下一 UTC minute；不创建 attempt、不调用
  `source_lease_guard`、不写失败日志。source outbox 按该时间恢复。
- slot 已领取但进程在 HTTP 前崩溃时仍占用该分钟容量，恢复复用原 claim；这是保守限流，不会扩大流量。
- claim 不含 payload、Endpoint URL、Secret、Credential 或请求正文；按 tenant/Endpoint/bucket 可审计。
- replay worker 遇到未来 `next_retry_at` 或新发生的 rate deferral 时必须保持根死信为 `replay_pending`，
  将 `replay_next_attempt_at` 精确推进到 delivery 的时间，且不增加 replay dispatch/network attempt；只有
  delivery 真正 success/failed 才能推进根死信为 replayed/dead_letter。
- rate claim 保留 30 天；scheduler 只可删除已过期且关联 delivery 已为终态的 claim，pending delivery 的
  slot 证据不可清理。

## 7. 并发、限流与恢复时序

1. Gateway/Control 在原 source 事务插入 DLP/session/provider 权威事实。本地 Key 解密失败由生产请求的
   服务端 request/trace 建立 `provider_key_anomaly_events`；直接单元调用没有可信请求坐标时不伪造 source。
2. 0074 trigger 同事务插入标准 source，纳入仍为 pending 的 5 秒 aggregate；若当前 generation 已被领取，
   创建下一 generation。
3. Control security scheduler 在窗口结束后领取 aggregate，重算成员并冻结逻辑 payload 与每个 Endpoint
   的 format/wire/rate/Secret version。
4. 每个 pending delivery 在出网前领取持久 rate slot。无 slot 时只延迟；有 slot 时续租 source，再对实际
   bytes 签名并投递，attempt 与 delivery 摘要同事务保存。
5. 成功、三次失败/死信、停服、超时、进程崩溃和租约接管均由 outbox CAS 收敛；迟到 owner 在出网前
   复核 lease，失权后不得继续发送。

一个 aggregate 对多个 Endpoint 串行发送时，每次真实网络前续租；停服发生在零网络前立即归还 lease，
已有网络后保持可恢复状态。任何队列、格式或 rate 故障都不能回滚原 DLP/session/provider 事实，也不能
改变 Gateway 对客户端的业务响应。反之，标准 source/member/outbox 的同事务约束失败表示安全事实无法
可靠追踪，原 source 写入必须 fail closed 并使用原领域的统一 unavailable 错误，不能只写日志后放行。

## 8. 安全与隐私

- 所有业务表默认含 `tenant_id`，跨表使用 tenant-qualified 外键或 trigger；API 不接受 tenant 字段。
- `webhook.manage` 由后端逐请求验证；前端模式切换、隐藏按钮或事件列表不构成授权。
- CEF 只使用固定字段名/标签/事件名；自由文本不能成为 Header、extension key 或日志内容。
- CEF formatter 必须先运行完整 `security.event_pushed` envelope/payload exact validator，再进行固定映射；
  不能把“已转义”当作越过事件字段、类型、owner 或敏感键校验的理由。
- source、aggregate、delivery、attempt、rate claim 和 outbox 只保存安全 ID、枚举、整数计数和 UTC 时间。
- 日志只允许 event/error code、tenant、event/delivery ID、request/trace；不得记录 payload、wire、URL、
  Secret、签名、Provider Key、Token、Prompt、Response 或底层异常正文。
- HTTP 客户端继续 `trust_env=False`、不跟随重定向、只读取响应状态，避免代理污染和响应正文泄露。

## 9. 错误合同

新增错误：

| code | HTTP | 场景 |
|---|---:|---|
| `webhook.format_invalid` | 422 | format 未知、CEF 混合订阅或 format/body 合同非法 |
| `webhook.security_rate_limit_invalid` | 422 | 创建参数不是原生整数或不在 1..600 |
| `security_event.unavailable` | 503 | source/aggregate/outbox/成员或 scheduler 状态不可可靠恢复 |

达到发送限流不作为 API 错误或 delivery failure；它是可追踪 pending 状态。其他认证、权限、Endpoint、
Secret、投递、死信和 replay 错误继续复用现有统一 code，并由中间件附加 request_id/trace_id。

## 10. 模块修改计划

1. Shared：扩展事件目录与 `security.event_pushed` exact validator；新增 JSON/CEF deterministic formatter、
   CEF escaping 与 HMAC 实际 bytes 合同；增加错误目录和测试。
2. Migration：新增 `0074_webhook_siem_security_event_format.sql/.down.sql`，扩展 Webhook 表并创建
   source/aggregate/member/outbox/rate claim、索引、trigger 和 guard。
3. Control Webhook：认证后 16 KiB exact wire、请求/响应 model、创建校验、format/body/rate 冻结、发送
   Header/Content-Type、replay 复制、pending rate gate、服务端链路 ID 和全路径 no-store。
4. Control Security Events：新增 outbox/scheduler，接入 app lifespan；仅处理持久聚合，不提供第二套管理 API。
5. Shared Runtime delivery：既有 Gateway dispatcher 也必须为普通 JSON delivery 显式写入新增 wire 字段，
   避免 0074 默认值造成旧事件失败；`security.event_pushed` 只允许 Control 持久 outbox 发布。
6. Gateway：不增加 SIEM 网络 worker；DLP/session/provider auth 事实由 0074 同事务 trigger 捕获；基础与
   Cache Affinity KeyPool 在生产 request/trace 下原子记录无 Credential 标识的 decrypt anomaly。旧事件
   派发和 Provider 选择结果保持兼容。
7. Console：PAGE-113 在现有 Webhook 表单、列表和 delivery 摘要增加 format/rate；补 owner、权限、转义和
   响应式测试。
8. Docs：README、目录说明、Webhook API、测试记录和 Excel `12_开发状态`；完成后只登记下一条，不实现。

## 11. 测试计划

### 11.1 Shared/格式

- exact payload/envelope 正反例、三 source 映射、severity/action/outcome 漂移、敏感键和大小边界。
- JSON canonical bytes；CEF 固定顺序、Header/extension 转义、CR/LF 注入、Unicode、最大长度和
  `webhook.test`。
- JSON/CEF HMAC 都对实际 bytes 验签；改变任一 byte 必须失败。

### 11.2 迁移/数据库

- 空库 `74/0074 -> repeat -> 73/0073 -> 74/0074`；每阶段 `integrity_check=ok`、外键违规 0。
- 历史 Endpoint/delivery 精确转为 JSON，down/re-up 保留旧字段和 owner；不历史回灌 source。
- DLP/session/provider 三类 trigger、5 秒聚合、窗口边界、processing 后迟到 generation、成员反算和
  source 唯一；1000 个成员硬上限及同窗口下一 generation。
- tenant/owner/NULL/rowid/replace/update/delete、非法 outbox 跃迁、lease/token/generation、format/body
  不一致、跨租户 Endpoint/delivery/rate claim 全部失败关闭。

### 11.3 API/投递/恢复

- admin/developer 中实际具备 `webhook.manage` 者可创建；无权限、匿名、跨租户、撤权和非法正文返回统一错误。
- 未认证/无权限 + 畸形/超限 body 的 401/403 优先级；重复 key、未知字段、错误 Content-Type、非法 UTF-8、
  bool 整数、NaN/Infinity、数组/标量、16 KiB 边界与正文不进入错误/日志。
- JSON/CEF Endpoint 创建/list/test；CEF 混合订阅拒绝；响应携带 request_id 且不返回 Secret 密文/wire。
- 三类真实 source 聚合并分别投递 JSON/CEF；内容类型、event/delivery Header、HMAC、计数和时间一致。
- 每 Endpoint 独立限流；满额延迟、下一分钟恢复、零 attempt/零网络、claim 崩溃复用和不同 tenant/Endpoint
  不互相占用。
- replay rate deferral 保持 `replay_pending`，精确继承 delivery due time，且不会被误收敛为 failed；
  30 天 claim 清理不触碰 pending 证据。
- 接收端 2xx、非 2xx、超时、连接故障、三次失败死信、replay、Secret 轮换、Endpoint 状态变化、空订阅、
  snapshot 后新增 Endpoint、窗口/投递中崩溃和过期 lease takeover。
- replay 逐字复用来源 format/wire，即使 Endpoint 当前 format/rate 变化也不漂移；签名使用 replay 冻结 Secret。

### 11.4 Console/实际验收

- client 请求不含 tenant/Secret；format/rate 正确提交，统一错误不泄露 body。
- PAGE-113 CEF 模式切换立即禁用非 SIEM 事件并选择唯一 `security.event_pushed`；切回 JSON 恢复可编辑目录
  但不暗自订阅额外事件。覆盖 Endpoint/delivery 标签、1..600 数值边界、100 条上限、HTML 转义、权限撤销、
  owner 切换和一次性 Secret 清理；前端行为只改善交互，篡改 DOM 仍由后端拒绝。
- 真实 Control + Gateway + 本地接收器：一次 DLP、一次 session risk、一次 Provider 401，验证 JSON/CEF、
  聚合、限流、重启恢复、数据库状态和零敏感命中。
- Chrome 桌面/移动检查无重叠、无越界、无未授权 DOM/请求；隐私 canary 流与截图流分离。

## 12. 完成门禁

- 先跑 PRD-081 Shared、迁移、Webhook/outbox、Gateway source 和 Console 专项；修正后只扩展必要相邻回归。
- 最终执行空库迁移、真实 Runtime、真实 Browser、敏感信息/禁止文件/大文件/符号链接审计。
- 文档、Excel、代码和证据必须保持 `all_108_prds_complete=false`；PRD-081 提交并推送 `origin/main` 前
  不开始 PRD-082。

## 13. 待编码前确认

- [x] 权威范围、PAGE/API/OBJ/EVT/TC 已交叉确认。
- [x] 真实 DLP/session/provider source 与现有 Webhook replay 路径已追踪。
- [x] JSON/CEF、HMAC、聚合、限流和恢复合同已形成初稿。
- [x] 第一轮设计自审完成并修正。
- [x] 第二轮设计自审完成并修正。

## 14. 实现状态

Shared、0074、Control、Gateway、PAGE-113、自动化、Runtime、Browser、API/测试文档与 Excel 均已完成。
权威 Runtime 为 `.artifacts/prd081-runtime/final-20260816t`，权威 Browser 为
`.artifacts/prd081-browser/final-20260816q`；两者均保持 `all_108_prds_complete=false`。本次只登记下一条
PRD-082，不实现 MCP 能力。

## 15. 设计审查方法

第一轮聚焦数据 owner、聚合并发、窗口边界、持久 rate limit、delivery/replay wire 冻结和崩溃恢复；
第二轮聚焦 API exact wire、CEF 兼容/转义、权限、Console 生命周期、敏感信息与回归范围。每轮发现必须先
修正文档和测试计划，再关闭编码门禁。

## 16. 编码前第一轮自审：已完成并修正

2026-08-15 按 source owner、聚合边界、delivery/replay、持久限流与崩溃恢复逐项复核，发现并关闭：

1. **P1：初稿把 Secret version 与 wire 一并描述为永久冻结，违背现有每次 attempt 前切换当前 Secret 的
   轮换合同。** 已拆分边界：format/body/rate 永久冻结；Secret 在 rate gate 通过后、真实网络前受控刷新，
   因而 Endpoint 轮换继续生效且零网络延迟不提前改变版本。
2. **P1：现有 replay worker 会把 rate-limited pending 当作失败并把根死信退回 dead_letter。** 已增加
   `replay_pending` 延迟分支，继承 delivery `next_retry_at`，零 attempt/零网络且仅真实终态推进根状态。
3. **P1：5 秒窗口内成员数量没有硬上限，单租户突发可造成无界重算。** 每 generation 固定最多 1000 个
   source，满额在同窗口创建下一 generation，继续由 Endpoint rate limit 平滑出站。
4. **P1：仅把上游 401 视为 Key 异常会漏掉本地托管密文损坏。** 新增不含 Credential 标识的
   `provider_key_anomaly_events`，生产 request/trace 下由基础与亲和 KeyPool 同事务记录 decrypt failure；
   与 auth-failed Provider fact 共同覆盖验收。
5. **P1：CEF `rt/start/end` 未指定编码，会让 SIEM 解析器按本地时间或自由文本分歧。** 已固定 Unix epoch
   milliseconds，并写明 label/value 的实际 CEF key 顺序。
6. **P2：rate claim 没有留存边界。** 固定保留 30 天，仅在关联 delivery 终态后清理；pending 证据不删。
7. **P1：0074 新增 wire 非空约束后，既有 Gateway runtime INSERT 若依赖默认值会破坏旧事件。** 修改计划
   已要求 Shared runtime 对普通 JSON delivery 显式写 format/body/rate，并禁止从内存队列发布新 SIEM 事件。

修正后第一轮无未关闭 P0/P1；进入第二轮 API、CEF、权限、Console 与隐私审查。

## 17. 编码前第二轮自审

2026-08-15 按 API exact wire、CEF 兼容/注入、权限顺序、Console owner 和隐私边界复核，发现并关闭：

1. **P1：既有 `POST /webhooks` 由框架自动解析，无法证明认证先于正文，也未拒绝重复 key、未知字段、
   非标准 JSON 或超限正文。** 已冻结认证后 16 KiB 流式读取、唯一 key、strict/forbid 和固定 401/403
   优先级；OpenAPI 仍由同一 Pydantic schema 生成。
2. **P1：Endpoint 名称/URL 可能被客户端复制进 request/trace Header，再由通用中间件规范化后进入日志。**
   写路径改用服务端链路 ID，且整个 Webhook 管理/投递响应固定 no-store。
3. **P1：初稿只有“CEF 大小约束”而没有数值，无法形成数据库、formatter 和测试的共同边界。** 已固定
   16,384 UTF-8 bytes、单行、零截断；formatter 必须先 exact validate 再转义。
4. **P1：旧 DLP trace 可空、旧表 ID 约束弱，直接 SQL 可能生成无法安全写 CEF 的 source。** 已固定
   request/trace ASCII 规则、null trace 回退和四类 source ID 前缀/32 hex；非法 owner 事务失败关闭。
5. **P1：Console 只在 submit 时改写 event list 会让屏幕状态与实际请求不一致。** PAGE-113 必须在模式
   change 时立即收敛可见/可编辑选项，切回 JSON 不静默扩大订阅；后端 exact 校验仍是唯一权限/合同边界。
6. **P2：CEF test、生产事件和 replay 的格式关系容易混淆。** 已明确 test 使用固定非生产 CEF 行且不计入
   security rate；生产/replay 使用冻结逻辑 event 与 wire，Headers 直接取 delivery event type。

第二轮无未关闭 P0/P1。编码门禁关闭，后续实现若改变 payload、CEF 顺序、窗口、限流或恢复语义，必须先
回到本文更新并重新审查。

## 18. 实现落地

1. Shared 新增 `runtime/security_events.py`，冻结 exact 安全事件、三类 source 映射、JSON/CEF 字节、
   CEF escaping/大小和实际 HMAC 输入；事件目录、统一错误、i18n、FastAPI 缓存边界与旧 JSON delivery
   同步扩展。
2. `0074_webhook_siem_security_event_format.sql/.down.sql` 已扩展 Endpoint/delivery，并新增
   tenant-qualified source、aggregate/member、outbox、rate claim、Provider decrypt anomaly 与全量 owner、
   append-only、CAS、wire freeze 和 down fail-closed guard。
3. Control 新增 `security_events/{outbox,scheduler}.py` 并接入 lifespan；Webhook 创建走认证后的 16 KiB
   exact JSON 解析，发送/replay 使用冻结 format/wire/rate，真实网络前再刷新当前 Secret 版本。
4. Gateway 基础与 Cache Affinity KeyPool 只在可信 request/trace 下持久化无 Credential 标识的 decrypt
   anomaly；上游认证失败由既有 Provider fact 事务投影。Provider Key、错误正文和 Endpoint 不进入事件。
5. Console PAGE-113 在原 Webhook 页面增加 JSON/CEF segmented control、1..600 限流输入和冻结投递摘要；
   CEF 模式立即收敛订阅，但后端 `webhook.manage` 与 exact wire 校验仍是唯一授权/合同边界。
6. Runtime/Browser harness、合同测试与隐私门禁已落地；API、测试记录、README、目录说明和 Excel
   `12_开发状态` 已同步。

## 19. 实现后第一轮自审：已完成并修正

2026-08-15 至 2026-08-16 按数据库事实、聚合/限流并发、Webhook 恢复、Gateway 热路径与实际 Runtime
逐项复核，发现并关闭：

1. **P1：Runtime 验收把正式账本表误写成不存在的 `charge_ledger`。** 已改为权威
   `finops_ledger_entries`，并直接证明一个成功 Usage/charge 与七个失败 RequestLog 可追踪。
2. **P1：scheduler 最后一次业务提交可能晚于验收器最终查询，产生偶发 pending 误报。** 最终数据库检查
   增加有界静止等待，只有 outbox/delivery/lease 全部收敛后才发布证据。
3. **P1：rate deferral 后 replay source 可能被旧恢复分支误收敛为 failed。** worker 现保留
   `replay_pending`，延迟期间 attempt/network 均为 0，只在真实终态推进根死信。
4. **P1：损坏 aggregate/member 若在 claim 前验证，会让坏队首永久阻塞其他租户。** worker 必须先公平
   claim，再由当前且未过期 owner 完整 CAS 验证并固定隔离，随后继续领取下一 source。
5. **P1：Gateway decrypt failure 原路径可能只记日志，无法形成可靠 SIEM source。** 基础与亲和 KeyPool
   在调用方事务内写最小 anomaly fact；日志和事件均不含 Credential ID、Key prefix、密文或异常正文。

修正后 Shared/Control `34/34`、Gateway `36/36`、Runtime/Browser 合同 `9/9` 通过；权威 Runtime
`final-20260816t` 从空库完成 `74/0074 -> 74/0074 -> 73/0073 -> 74/0074`，四阶段 integrity=ok、FK=0，
三类真实 source、JSON/CEF、HMAC、聚合、独立限流和 Control 重启恢复均通过。第一轮无未关闭 P0/P1。

## 20. 实现后第二轮自审：已完成并修正

第二轮聚焦 PAGE-113、Browser 网络/进程边界、证据 provenance、SQLite 隐私与提交范围：

1. **P1：Webhook 表格强制单行省略会让真实桌面长事件名越出列宽。** 改为列内安全换行；桌面/移动
   text overflow、horizontal overflow 和 overlap 重新实测均为 0。
2. **P1：Browser 网络白名单遗漏 Control 依赖的 loopback `:8000` 健康检查。** 只补固定本机 origin；
   外网、携带敏感 query、用户信息 origin 和重定向仍全部阻断。
3. **P1：Windows Chrome 清理只观察 taskkill 返回，不能证明真实 browser PID 与继承双流已结束。** 清理
   现要求精确 PID 消失并等待 stdout/stderr EOF；失败轮 owner 目录也只在边界和标记验证后删除。
4. **P1：最终 SQLite 隐私收尾依赖 `VACUUM`，活动连接竞态会使证据发布失败。** 改为
   `secure_delete + 同字节覆盖 + quiesce`，再验证 work/staging 明文命中和 sidecar 均为 0。
5. **P1：重新格式化验收器后旧证据 provenance 不再匹配。** 未改写旧 JSON；以当前源码重新执行 Runtime
   `final-20260816t` 与 Browser `final-20260816q`，源码哈希、JSON/manifest、截图和当前工作树重新匹配。
6. **P2：CEF/JSON Endpoint 元数据若继续单独依赖前端显示，无法证明后端冻结。** Browser 现在同时读取
   Endpoint 与 delivery 摘要，并由数据库确认 JSON `600/min`、CEF `1/min`、两次真实成功投递和验签错误 0。

最终 Console `822/822`、Node Browser 合同 `5/5`、Console lint `254 files`、新增 Python 全规则 Ruff、
Git whitespace 和提交前敏感/禁止文件门禁均通过。四张新 PNG 已人工复核为非空、内容完整且无控件/文字
重叠；外部请求、Console/Page error、DOM/缓存/日志/截图敏感明文命中均为 0。第二轮无未关闭 P0/P1。
