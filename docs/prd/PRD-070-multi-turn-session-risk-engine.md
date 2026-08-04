# PRD-070 - 多轮会话风险引擎

状态：**已完成**。编码前两轮自审、实现后两轮复核、0062、专项与必要关联回归、HMAC 轮换交叉审查、
真实三服务/API/RESP、权限、租户、异常、隐私及应用内 Browser 双视口验收均已通过；当前证据见
`docs/testing/PRD-070-tests.md`。

总体目标仍在进行中：`all_108_prds_complete=false`。

## 1. 需求理解

### 1.1 权威来源与开发顺序

- Excel `01_PRD总表` 第 71 行与 `03_M2-M3扩展交付` 第 7 行一致定义：
  `PRD-070 | M2-企业增强 | P2 | 扩展层 | 会话风险 | 多轮会话风险引擎 | 安全负责人`。
- 用户故事是连续试探的 Chat/API 会话可被识别和阻断；范围是 Redis 维护 session risk score，
  累积多轮越狱、敏感词和工具诱导风险，高风险时冻结 Virtual Key 或会话。
- 明确边界是只作用于 Chat/API session，不做员工、终端用户、IP、设备或全公司的行为画像。
- Excel API 清单把完整路径机械拆成 `API-314..321`；实际合同只有：
  `GET /security/session-risks` 和 `POST /security/session-risks/{id}/action`，不得误建八个接口。
- 页面为 `PAGE-098 高危会话报表`；对象为 `OBJ-094 SessionRiskScore`；事件为
  `EVT-077 security.session_high_risk`；`TC-0139/TC-0140` 覆盖主流程、阈值、误杀、异常、权限和安全。
- README 已把当前开发项切换到 PRD-070；PRD-069 已完成。因此本条是按里程碑、优先级和 Excel
  行序交叉确认的下一条未完成 PRD，不是仅凭编号推断。

### 1.2 架构与前序合同

- PRD-069 提供本地轻量意图分类和 Provider 前强规则；PRD-037 保证流式首块、Watchdog 和断连清理；
  PRD-066 提供服务端可信 `ChatSession`；PRD-024 提供 tenant DLP 命中事实。
- 架构文档把 Redis 定义为分钟到小时级热状态，把复杂报表放到控制面和持久存储；安全检测不得在
  SSE 首块之后再改变当前响应。会话风险必须在 Provider/首块之前完成本次准入。
- Gateway 当前没有公开 API 的跨请求 session identity。`stream_session_id` 只标识一次流式请求，
  不能冒充多轮会话；OpenAI `user` 表示终端用户而非会话，跨会话使用它会形成用户画像，也不能复用。
- 企业 Chat 的 `chat_sessions.chat_session_id` 是服务端生成且 tenant-qualified 的可信身份，但当前
  `InternalChatExecution` 尚未把它带入 Gateway 治理上下文，本条需要补齐。
- Virtual Key 的 `active/disabled` 与 ChatSession 的 `active/cleared/expired` 已有独立业务语义。
  风险冻结必须使用独立 overlay，不能覆盖这些状态，也不能被普通 Key 重新启用或页面清空操作绕过。
- OpenRouter、硅基流动仍只作为竞品和迁移兼容对象；本条不调用任意外部安全模型，默认上游边界不变。

### 1.3 本期交付范围

1. 新增 Shared 纯函数会话风险信号和固定阈值策略，复用 PRD-069 分类器，只分析本轮最后一条 user 文本。
2. 新增独立 `SessionRiskStore`，用 Redis Lua 原子完成 request 去重、累计、TTL、revision 和阈值跃迁。
3. 为企业 Chat 注入可信 ChatSession/Message scope；为公开 Chat Completions 定义必须成对出现的可选
   Session/Turn Header。任一 Header 缺失时不建立跨请求风险分数，继续沿用 DLP/PRD-069 单请求安全，
   绝不按 Virtual Key、IP、Prompt 或 OpenAI `user` 猜测会话。
4. 新增持久 `SessionRiskScore`、逐请求 observation、处置 action 和 source event；均带 `tenant_id`，
   不保存 Prompt、Response、命中片段、API session 原文或可直接枚举的裸 hash。
5. 连续风险达到告警阈值时创建 `security.session_high_risk`；达到冻结阈值时在 Provider 前自动创建 session
   overlay。已识别的 high-risk/frozen session 可由同时具备 `session_risk.read/manage` 的管理员人工冻结其
   关联 Virtual Key；无可信 session 的请求绝不建立 Key 汇总或自动冻结共享 Key。
6. 新增两个后端权限 `session_risk.read` 与 `session_risk.manage`，默认仅 Admin/SecurityAdmin 拥有。
7. 实现两条 Control API、严格分页/筛选、幂等处置、不可变审计和 PAGE-098 高危会话报表。
8. 补齐 0062 正反迁移、Shared/Control/Gateway/Console 专项、Redis 故障、并发、误杀、SSE 和真实运行验收。

### 1.4 明确不做

- 不做公司级用户画像、跨 Virtual Key 用户合并、IP/设备指纹、员工风险排名或跨租户超级管理员报表。
- 不读取或持久化 OpenAI `user` 作为 session identity；不根据 Prompt、消息历史 hash 或网络地址猜会话。
- 不做模型训练、外部 LLM 审核、文件隐藏 Prompt Injection、输出审核、MCP 主链路或真实工具执行治理。
- 不开放租户自定义权重、阈值、正则或 Lua；本期使用版本化产品规则，避免误配置造成全租户误杀。
- 不把风险冻结改写为 Virtual Key `disabled` 或 ChatSession `cleared/expired`；普通管理操作不能解除风险冻结。
- 不让异步 Webhook、报表读取或动作历史查询阻塞 Gateway；不在 SSE 已开始后中途关闭或切换 Provider。
- 不把 Redis 当审计库，也不宣称 Redis 与 SQLite/PostgreSQL 之间存在分布式事务或 exactly-once 网络投递。

## 2. 涉及模块与总体流程

### 2.1 模块边界

| 模块 | 计划职责 |
| --- | --- |
| `shared-sdk/python/metahub_shared/session_risk/` | 固定信号、权重、阈值、scope/状态模型和纯函数 |
| `gateway-data-plane/.../session_risk/` | Header/scope 解析、Redis store、准入、持久 observation、冻结和事件 |
| `gateway-data-plane/.../api/internal_chat.py` | 从已消费 grant 注入可信 ChatSession scope |
| `gateway-data-plane/.../api/openai_compatible.py` | 在 DLP/Intent/Rate/Budget/Provider 之间编排一次计分和冻结 |
| `control-plane-api/.../session_risks/` | tenant 列表、严格 action、Redis 同步、Audit 和事件派发 |
| `shared-sdk/.../rbac.py` 与 0062 | revision 7 的两个独立权限和 tenant 投影 |
| `shared-sdk/.../runtime/errors.py` | Control/Gateway 统一错误目录 |
| `shared-sdk/.../runtime/webhooks.py` | `security.session_high_risk` exact payload/envelope |
| `console-web` | PAGE-098 client、owner state、列表/详情、处置对话框和响应式样式 |

### 2.2 Gateway 顺序

冻结后的主顺序为：

```text
Auth -> Request claim -> tenant/environment/policy-lease error replay
     -> trusted session resolve -> Session/Key freeze overlay precheck
     -> payload read/validate -> Policy/mask -> Routing -> DLP
     -> one Intent path + current-turn SessionRisk assessment
     -> Redis accumulate + durable evidence
     -> Rate Limit -> Budget -> Provider / SSE
```

关键语义：

1. freeze overlay 在共同 handler 的 request claim 与既有 tenant/environment/policy-lease 错误之后、公开 API
   读取 Prompt 正文之前用可信 scope 做精确点查；已冻结公开请求不解析正文并返回固定错误。内部 Chat 必须先
   解析有界内部 body 并消费 grant 才能恢复可信 session/message，随后进入同一门禁且绝不进入 Policy/Provider。
   该检查不能只放进公开 Virtual Key 鉴权。
2. 仅在存在可信 session 时，DLP block 和 Intent strong block 才把本轮固定摘要计入会话风险；计分失败
   不能改变原安全阻断码，却必须留下固定告警，并让 Redis/DB 不一致在后续请求失败关闭。
3. DLP 已 block 时不再调用意图分类器，只提交 DLP assessment；DLP 通过且存在可信 session 时，用一次同步完整
   分类替代 PRD-069 的“强规则同步 + 轻分类异步”，不得重复分类或继续入队。
4. DLP 通过但无可信 session 时完全沿用 PRD-069：强规则同步，未命中后异步轻分类；PRD-070 不创建 score。
5. 每个可信 turn 只构造一个不可变 `TurnRiskAssessment`，一次 Lua 提交组合信号和 cap 后 delta。所有旧 pending
   先清债，再完成本轮 Redis 累计和数据库持久化。
6. session 达到自动冻结阈值或关联 Key 已被管理员人工冻结时，当前请求不进入 Rate Limit/Budget/Provider。
7. Fallback、SSE chunk、Usage 回写、Webhook 重试和内部 Provider attempt 都不得再次计分。
8. Embeddings 和非 Chat 入口保持原合同，不创建 session risk state。

### 2.3 流式边界

- 计分和冻结只发生在 SSE Response 创建之前；本轮达到冻结阈值时客户端收普通统一 JSON 错误，不开始 SSE。
- 一旦首块已发送，后续 Redis、Webhook 或报表状态不能中断流、切模型或重复计分。
- `stream_session_id` 继续只标识单次流式资源，不进入 session risk scope。
- 聚焦性能门禁分别度量 current-turn 20,000 code points 分类和本地 Redis 往返；不把 Webhook 派发计入 TTFT。

## 3. 会话身份与租户隔离

### 3.1 企业 Chat scope

- `scope_type=enterprise_chat`，`scope_key=chat_session_id`；自动 freeze target 固定 `session`。
- `chat_session_id` 与本轮 `chat_message_id` 只从已消费、owner 全图复核通过的 `chat_execution_grants` 注入
  `GatewayAuthContext`；公开 Header、请求体、历史消息或客户端 request ID 不能覆盖。
- 内部 `/internal/chat/completions` 明确拒绝两个公开 Session/Turn Header；内部身份只能来自 grant，不能制造
  Header 与 grant 的双 owner。
- 数据库触发器逐字段证明 score 的 `(tenant_id, chat_session_id, virtual_key_id)` 与
  `chat_sessions` 一致，observation 的 `chat_message_id` 与同 tenant/session 的 Message 一致；跨 tenant 同 ID
  不共享 Redis key、数据库行或事件。
- ChatSession 清空/过期不等于处置风险。清空后旧 session 自然无法执行，但高危记录和 action 历史继续可查；
  不得物理删除强安全事实。

### 3.2 公开 API session 与 turn 合同

- 公开 `POST /v1/chat/completions` 接受一对可选 Header：`X-MetaHub-Session-ID=mhs_<32 lowercase hex>` 与
  `X-MetaHub-Turn-ID=mht_<32 lowercase hex>`。两者必须同时存在或同时缺失；重复行、逗号合并值、空白、
  错误前缀或大小写漂移均返回 `gateway.session_risk_identifier_invalid`，且不得进入 Provider。
- 两个 Header 只用于 MetaHub 风险 scope/turn，不发送给 Provider，不写入 URL、日志、事件、数据库或前端。
- 使用会话风险 HMAC keyring 和不同 domain tag 分别派生 `srh_<64hex>` session key、turn digest 及
  turn-intent digest。turn-intent digest 输入为 Policy mask 后最后一条 user 文本，只用于判定同一 Turn ID 的
  同意图重放；数据库/Redis 不保存 Header、正文或无密钥 hash，digest 不进入 API、DOM、Webhook 或日志。
- 共享 HMAC helper 固定编码为 `domain_tag || uint32_be(len(part)) || part...`，拒绝空 domain、超长段和自定义对象；
  session、owner-slot、turn、turn-intent、action-idempotency、action-intent 使用互不相同的常量 domain，禁止用
  分隔符字符串拼接或跨用途复用 digest。
- scope 绑定认证得到的 `(tenant_id, virtual_key_id)`；同一 Header 在另一 tenant 或 Key 下派生不同 owner。
- 自动 freeze target 固定 `session`，只阻断相同 tenant、Virtual Key 和 session HMAC；管理员可从该已识别
  high-risk session 独立创建 Virtual Key overlay，但不能由客户端选择或自动升级。

### 3.3 Header 缺失与轮换边界

- 两个 Header 均缺失时不创建 `SessionRiskScore`、Redis key 或跨请求关联，只执行现有 DLP 和 PRD-069
  单请求安全。旧 OpenAI-compatible 调用方保持兼容，也不会被错误地当成一个共享 Key 会话。
- 只出现其中一个 Header 属于非法声明，不得静默退化；调用方必须删除两者或提供合法成对值。
- 调用方主动轮换合法 Session ID 会形成不同 API session。本期不按 Virtual Key、IP、`user`、Prompt hash 或
  设备把它们重新合并；这是“不做行为画像”的明确边界。每个 turn 的 PRD-069 强规则仍独立生效，已识别的
  high-risk session 可由管理员冻结其关联 Key；更复杂的跨 session 行为聚合不在 PRD-070 范围。
- PAGE-098 只显示“企业 Chat 会话 / API 会话”，Key 是可选人工冻结目标，不伪装成第三类会话或 score。

### 3.4 Turn 幂等与 HMAC 轮换

- 企业 Chat 以服务端 `chat_message_id` 为可信 turn identity；Control 的 `client_message_id` 重放必须复用原
  Message/Grant 合同，不能创建第二个风险 turn。
- API session 以派生后的 turn digest 为 identity。相同 Turn ID 与相同 turn-intent digest 重放返回
  `duplicate=true`，不改 score/revision/TTL；相同 Turn ID 携带不同正文返回
  `gateway.session_risk_turn_conflict`，不得进入 Provider。
- turn claim 幂等保留期固定 30 分钟；保留期内上述语义同时覆盖零分与风险 turn，过期后同 ID 可作为新 turn。
- `request_id/trace_id` 只追踪本次 HTTP attempt，不承担业务 turn 去重。数据库以
  `(tenant_id, session_risk_score_id, turn_key_digest)` 唯一约束防重，并保留首次 source request/trace。
- Turn ID 只提供风险累计幂等，不承诺缓存或重放 Provider 响应；响应丢失后的同意图重试可以重新调用 Provider，
  但绝不再次升分、刷新窗口或触发冻结。
- 配置提供 current/previous 两个带整数 version 的 HMAC key。新 cycle 使用 current；请求同时计算两个候选，
  已存在的 active/frozen cycle 继续使用其 `hmac_key_version`。previous 只有在数据库不存在任何对应
  active/frozen Chat/API score 且不存在未过期 action digest 后才能移除；缺少仍被引用的版本时运行状态 unavailable，并在
  Provider 前失败关闭。
- 移除 previous 是受控维护动作：必须在仍配置两把 key 时停止 Gateway 新写入，运行 verifier 扫描 Redis score
  hash 并清偿/拒绝所有该 version pending，再核对数据库 active/frozen score 与未过期 action digest 为 0。
  未通过 verifier 不允许删 key；这不是报表热路径，不从 Redis SCAN 生成业务页面。

## 4. 信号、分数与阈值

### 4.1 只分析当前 turn

- 从已完成 Policy mask 的 messages 逆向找到最后一条 `role=user` 文本；不重新累计客户端重复携带的历史。
- 文本最多取 20,000 code points。超长时使用有界 head/tail 各 10,000 的确定性样本，并记录
  `input_truncated=true`；这样攻击者不能只在尾部放风险短语绕过前缀扫描。
- turn-intent HMAC 使用最后一条 user message 的完整文本投影而非 head/tail 样本，避免中间文本变化被误判为
  同意图；可信 session 的该投影上限为 256 KiB UTF-8，超限固定 413 且不调用 Provider。非文本 content part
  不进入 PRD-070 文本意图，投影规则与 DLP 的字符串/`text` part 提取合同一致。
- 分类文本只在当前调用栈存在，dataclass 使用 `repr=False`；不保存文本、片段、位置、裸 hash 或异常消息。
- 企业 Chat 当前 turn 最大 8,000 字符，仍走同一纯函数，不能维护第二套分数规则。

分类编排固定如下，测试必须断言调用次数而不只断言最终状态：

| 可信 session | DLP 结果 | 意图路径 | PRD-070 计分 |
| --- | --- | --- | --- |
| 否 | block | 不调用意图分类；沿用 DLP 错误 | 不创建 score |
| 否 | allow/log_only | PRD-069 原 `sync_strong -> async_light` | 不创建 score |
| 是 | block | 不调用意图分类；沿用 DLP 错误 | 仅当前 turn DLP 信号 |
| 是 | allow/log_only | 单次同步 `classify_session_intent`；不投异步队列 | 当前 turn intent、DLP、工具信号 |

- `classify_session_intent` 是新增的一次调用合同：在同一有界规范化过程中返回“全部 user 历史的 strong guard”与
  “最后一条 user turn 的完整轻分类”。历史 strong 仍可阻断当前请求，但只有 current-turn 结果进入累计，避免
  客户端重复携带历史造成重复加分。
- 可信 session 的结果新增 `classification_mode=sync_session` 并按 PRD-069 结果/event 合同同步持久化；strong
  guard 命中时 action=block，其他结果为 observe/allow。该路径不创建 `IntentClassificationJob`，不会出现两套事实。
- 这是对 PRD-069 的窄兼容扩展：无 session 流量的异步 TTFT 合同不变；可信 session 明确承担一次本地同步轻分类。
  验收分别设置纯分类器和 Provider 前治理 P95 门禁，并验证队列入队数为 0、分类调用数恒为 1。

### 4.2 固定信号

| 信号 | 条件 | 基础 delta |
| --- | --- | ---: |
| `intent_strong` | PRD-069 当前 turn 分数 `>=9500` | 3600 |
| `intent_high` | 当前 turn 分数 `8000..9499` | 2600 |
| `intent_review` | 当前 turn 分数 `6500..7999` | 1800 |
| `dlp_block` | 当前 turn 命中 tenant DLP 且动作 block | 3000 |
| `dlp_log_only` | 当前 turn 命中 tenant DLP 且动作 log_only | 1200 |
| `tool_inducement` | 固定“工具目标 + 未授权/绕过/执行动作”组合命中 | 2400 |

- DLP `allow` 不加分；只命中历史消息的 DLP row 不加分。当前 turn 通过 `message_index` 与持久
  `dlp_matches` 精确关联，避免重复历史造成误杀。
- `tool_inducement` 只检测用户要求模型绕过授权调用函数、插件、系统命令或外部工具的强组合；安全培训、
  规则说明、否定语境和普通“如何使用工具”是固定负例。它不执行工具，也不引入 MCP。
- 一次 turn 可有多个信号，但 `turn_delta` 上限固定 4000；单次请求绝不能直接达到冻结阈值。
- 任何 float、客户端权重、动态表达式、租户正则或远程模型分数均不进入该计算。
- Shared 返回不可变 `TurnRiskAssessment`，至少包含 turn/intent digest、三类信号计数增量、组合后整数 delta、
  input_truncated、detector/ruleset/threshold version 和 source request/trace ID；不含正文或命中片段。
- Gateway 只调用一次 `accumulate(scope, assessment)`。Lua 重新校验 delta 范围、计数、版本与 digest 格式，
  不接受逐 signal 多次写入，确保组合信号、4000 cap、turn 去重和 revision 在一个原子步骤完成。

### 4.3 状态与阈值

风险分数范围固定 `0..10000`，规则版本固定 `session-risk-ruleset-v1`：

| scope | high risk | 自动 freeze | 最少风险 turn |
| --- | ---: | ---: | ---: |
| `enterprise_chat` | `>=7000` | `>=9000` | high risk 2、freeze 3 |
| `api_session` | `>=7000` | `>=9000` | high risk 2、freeze 3 |

- 比较使用 `>=`；阈值边界必须有精确单测。
- 只有 `turn_delta>0` 才增加 `risky_turn_count`、category count 和滑动窗口；安全请求不加分、不刷新风险 TTL。
- 活跃风险窗口为自最后一个风险 turn 起 30 分钟；Redis TTL 同口径。窗口过期后旧记录保留历史展示，
  下一风险 turn 创建新 score cycle，不把过期分数带入。
- `monitoring -> high_risk -> frozen -> resolved` 是主状态机；未达到 high risk 的过期 cycle 对外为
  `expired`。session 可自动或人工进入 frozen；人工 Key overlay 也会让来源 score 保持 frozen，直到其全部
  overlay 被释放。`frozen` 不随 Redis TTL 自动解除，只能由后端处置 action 结束。
- 首次跨 high risk 阈值创建 source event；跨 freeze 阈值创建自动 action 和状态事件。并发调用只能有一个
  revision 赢得每次跃迁。
- 当前未被其他安全闸门阻断的请求若本轮跨 freeze 阈值，返回 `gateway.session_risk_frozen`；当前已被
  DLP/Intent 阻断的请求保留更早的固定错误码，但冻结 overlay 同样必须提交。

## 5. Redis 热状态设计

### 5.1 Store 协议

新增独立协议，不复用非原子的 `RedisRateLimitStore`：

- `load(scope)`：读取版本、cycle ID、分数、计数、revision、窗口和 dedupe 状态。
- `accumulate(scope, assessment)`：用单个 Lua/EVAL 和同 hash slot 的 score/turn-claim 两个 key 校验不可变
  `TurnRiskAssessment`，原子完成 turn/intent 幂等判断、组合信号累计、续期、跃迁并写入一条不含正文的待持久
  安全摘要；已有未确认摘要时拒绝继续累计。零 delta 只创建 turn claim，不创建 score/pending。
- `load_pending(scope)`：读取并 exact-validate 当前唯一待持久摘要；不存在时返回空。
- `ack_pending(scope, score_id, revision, turn_key_digest)`：数据库事务提交后以三元 CAS 清除待持久摘要；
  ACK 不得删除更新 revision 的并发状态。
- `restore(scope, durable_snapshot)`：Redis key 缺失时以数据库 snapshot 的 revision 做只增 CAS 恢复。
- `reset(scope, score_id, revision)`：人工 resolve 后只删除/终结同一 cycle，不能删除新 cycle。
- `ping()`：健康与故障注入。

测试提供 `InMemorySessionRiskStore`，生产路径使用 `RedisSessionRiskStore`。两者必须通过同一合同测试，
不能让内存实现宽松接受 Redis 会拒绝的 revision、TTL 或重复 request。

仓库新增仅用于测试/本地验收的确定性 RESP 服务，监听回环端口并实现本条使用的 PING/AUTH/SELECT/EVAL、
数组响应、超时和断连故障注入。它必须按同一 Lua 参数/返回合同执行，生产配置仍只能构造
`RedisSessionRiskStore`，不得自动退回内存实现。该服务用于完全留在 `D:\MetaHub` 沙箱内验证真实 TCP/RESP
客户端路径；测试记录必须明确标注“RESP 合同服务”，不得把它宣称为外部 Redis 产品实例。

### 5.2 Redis key 与原子字段

- 先用 HMAC 派生不透明 `owner_slot`，score key 为 `metahub:sr:v1:{<owner_slot>}:score`，turn claim key 为
  `metahub:sr:v1:{<owner_slot>}:turn:<turn_key_digest>`；Redis Cluster 花括号确保两个 key 同 slot。key 不含
  tenant、Chat ID、Prompt、邮箱、Token、Header 原文或 Provider Key。
- Hash 至少包含：`schema_version`、`score_id`、`score`、三类 category count、`risky_turn_count`、
  `state`、`revision`、`window_started_at_epoch`、`last_risk_at_epoch`、`expires_at_epoch`、
  `hmac_key_version`、`threshold_version` 以及最多一条 `pending_revision/pending_payload`。
- turn claim value 只含 schema、turn-intent digest 与 HMAC version，TTL 固定 30 分钟；同 intent duplicate 不续期，
  不同 intent conflict 也不改值。这样零分请求可幂等而不会刷新风险窗口或制造持久 score。
- `pending_payload` 是 exact-schema 的紧凑 JSON，只含 score/计数/固定枚举、服务端生成的 request/trace ID 和时间，
  不含 Prompt、Response、Header、DLP pattern、异常消息或任何 secret。解析使用 JSON API，不做字符串拆词。
- Lua 对相同 turn+intent 返回 `duplicate=true` 且不改 score/revision/TTL，相同 turn+不同 intent 返回固定 conflict；
  新风险 turn 在一个脚本内完成全部字段并生成 pending。pending 未 ACK 时 key 不过期，避免数据库故障期间安全
  债务静默消失；ACK 后恢复 30 分钟 TTL。
- Redis 返回值必须按 exact length/type 校验；未知 schema、负值、超界 score、倒退 revision 或非法状态
  统一视为 store unavailable，不能猜测修复后继续 Provider。

### 5.3 Redis 与数据库权威边界

- Redis 是活跃窗口分数和并发原子累计的权威；数据库是冻结 overlay、报表、observation、action、Audit 和
  source event 的权威。
- Gateway 每个 Chat 请求对数据库冻结 overlay 做一次 tenant-first 索引点查；这不读取复杂报表，也不调用
  Control API。现有鉴权本就访问同一运行库，本期以冻结正确性优先于省略一个点查。
- Redis key 缺失时可从未过期数据库 snapshot 恢复；数据库已 `resolved/expired` 时旧 Redis cycle 必须被
  compare-and-reset，不能自动重新冻结。
- 每次请求在新累计前先处理旧 pending：按 revision 顺序把 observation、score 跃迁、自动 action 和 source event
  放入同一数据库事务；唯一键和 revision CAS 允许“DB 已提交但 ACK 响应丢失”的安全重放。旧 pending 未提交并
  ACK 前不得累计新 turn，也不得进入 Provider。
- Redis 累计成功后，当前请求必须提交该 pending，再用 `ack_pending` 确认；数据库失败或 ACK 结果不确定均返回
  `gateway.session_risk_evidence_unavailable`。Redis 保留唯一 pending，下一请求先重放清债；不得假装事件已提交。
- 并发请求遇到 pending 时只允许执行一次有界 flush/retry；仍有 pending 或 CAS 冲突时失败关闭，不做忙等。
  Redis 在 DB 提交前丢失时当前请求已经 503 且 Provider 未调用；DB 已提交后 Redis 丢失则从数据库 revision 恢复。
- 数据库已提交后 Webhook 队列失败不能回滚 score、冻结、observation 或 action。

### 5.4 Redis 不可用

- 升级默认 `METAHUB_SESSION_RISK_ENABLED=false`，确保旧部署不会因缺少 Redis/keyring 改变现有调用合同。
  disabled 时不解析两个新 Header、不创建 score；Control 仍可查历史并返回 `runtime_status=disabled`。
- enabled 不允许在存在任何 active overlay 时切为 false；Gateway readiness/每次配置装载查询只做计数门禁，命中时
  状态 unavailable 并拒绝 Chat，直到重新启用或由 Control 合法释放 overlay，防止用配置开关绕过冻结。
- enabled 且 `service_mode=public_service` 时，Redis 缺失、认证失败、超时、协议漂移或脚本失败均在 Provider 前
  返回 `gateway.session_risk_unavailable`，不使用进程本地状态绕过多副本一致性。
- enabled 且 `service_mode=internal_only` 时，先完成数据库 active overlay 点查：已冻结仍返回 403；数据库可用且
  未冻结时允许本次请求降级继续，不累计分数，并把 RequestLog `session_risk_status=degraded`、固定原因和健康状态
  记为可追踪事实。Redis 恢复后从数据库 snapshot 继续；不得用内存伪造累计。
- 任一模式下，数据库 overlay 点查失败、Store 明确返回已累计但 evidence 未提交/可见 pending，或缺少仍被引用的
  HMAC version 都失败关闭。Redis 整体不可达时 internal_only 无法证明是否存在未持久 pending，这是 degraded-allow
  ADR 明确接受的剩余风险；必须写 degraded 事实和健康告警。DLP/Intent 已经更早阻断时保留原错误码。
- 该矩阵是 PRD-070 ADR：`internal_only` 服从架构 E.1 的可用性优先，`public_service` 服从合规优先；最终运行验收
  必须在 enabled、Redis 正常状态完成，同时对两种 service mode 做故障注入。
- 新增 current key 的 `METAHUB_SESSION_RISK_HMAC_VERSION/SECRET`，以及可选 previous 的
  `METAHUB_SESSION_RISK_PREVIOUS_HMAC_VERSION/SECRET`。version 为正整数且 current 必须更大；secret 为
  32..512 UTF-8 bytes、无空白/控制字符。previous 必须成对出现，所有 secret 使用 `exclude=True/repr=False`，
  配置摘要只显示 version 与 masked/missing，日志和错误不得包含 secret。

## 6. 数据库与迁移设计

迁移编号固定为 `0062`，文件名：
`0062_multi_turn_session_risk_engine.sql` 与等价 `.down.sql`。

### 6.1 `session_risk_scores`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `session_risk_score_id` | TEXT PK，`srs_` + 32 hex | 服务端 cycle ID |
| `tenant_id` | TEXT NOT NULL，复合 FK | 所有 owner 查询第一条件 |
| `scope_type` | `enterprise_chat/api_session` | 只允许可证明的会话 scope |
| `scope_key` | TEXT NOT NULL | Chat ID 或 HMAC 派生 session ID |
| `hmac_key_version` | INTEGER NOT NULL >=1 | scope/turn/intent/owner-slot HMAC version |
| `virtual_key_id` | TEXT NOT NULL | 同租户复合 FK |
| `chat_session_id` | TEXT nullable | 仅 enterprise_chat 必填 |
| `status` | `monitoring/high_risk/frozen/resolved/expired` | 当前持久状态 |
| `score/score_scale` | INTEGER，0..10000 / 恒 10000 | 当前 cycle 分数 |
| `risky_turn_count` | INTEGER >=1 | 去重后的风险 turn 数 |
| `jailbreak_count` | INTEGER >=0 | 意图类累计计数 |
| `sensitive_count` | INTEGER >=0 | DLP/凭据类累计计数 |
| `tool_inducement_count` | INTEGER >=0 | 工具诱导累计计数 |
| `state_revision` | INTEGER >=1 | Redis/DB CAS 版本 |
| `threshold_version` | TEXT 固定 | 阈值合同版本 |
| `detector_version/ruleset_version` | TEXT | 当前 turn 分类版本 |
| `last_source_request_id/trace_id` | TEXT | 最近风险来源链路 |
| `window_started_at/last_risk_at/window_expires_at` | UTC TEXT | 活跃窗口 |
| `first_high_risk_at/frozen_at/resolved_at` | UTC TEXT nullable | 状态时间 |
| `created_at/updated_at` | UTC TEXT | OBJ-094 最小字段及 CAS 时间 |

约束：

- 部分唯一索引 `(tenant_id, scope_type, scope_key) WHERE status IN ('monitoring','high_risk','frozen')`
  保证一个 scope 最多一个 active cycle；resolved/expired 历史不覆盖。
- enterprise_chat 要求 `scope_key=chat_session_id`，且 ChatSession 的 tenant/Virtual Key 逐字段一致；
  api_session 要求 `scope_key` 为 `srh_` + 64 hex 且 `chat_session_id IS NULL`；两类 scope 都必须引用配置 keyring
  可识别的 `hmac_key_version`。
- 状态只能按明确矩阵转换，revision 每次加一；owner/scope/created_at 不可更新。
- `frozen` 不允许基于 TTL 自动变为 resolved；只有经过权限、幂等和 Audit 的 action 可以解除。
- 索引覆盖 `(tenant_id,status,last_risk_at DESC,id DESC)`、`(tenant_id,virtual_key_id,status)` 和
  `(tenant_id,chat_session_id,status)`。

### 6.2 `session_risk_observations`

不可变逐请求证据，字段包括：

- `session_risk_observation_id`、`tenant_id`、`session_risk_score_id`；
- `turn_identity_type=chat_message/api_turn`、`turn_key_digest/turn_intent_digest`、`chat_message_id` nullable；
- `source_request_id/source_trace_id`、`intent_risk_result_id`；
- `intent_risk_type/intent_risk_score`、`dlp_action/dlp_current_turn_match_count`、
  `tool_inducement_detected`；
- `turn_delta/score_before/score_after/risky_turn_index/state_revision`；
- `input_truncated`、`detector_version/ruleset_version/threshold_version`、`created_at`。

`UNIQUE(tenant_id,session_risk_score_id,turn_key_digest)` 防止 HTTP attempt 重试重复计分；触发器校验
before/after、delta、revision、Chat Message owner 和 score 当前 owner。表不保存 DLP pattern、规则名称、
Prompt、片段、位置、API Header 或无密钥正文 hash；两个 digest 均为版本化域分隔 HMAC。

### 6.3 `session_risk_freezes`

独立 overlay 使“会话冻结”和“管理员升级冻结 Key”不覆盖资源原状态：

- `session_risk_freeze_id`、`tenant_id`、`source_session_risk_score_id`；
- `target_type=session/virtual_key`、`target_scope_type`、`target_scope_key`、`virtual_key_id`；
- `status=active/released`、`state_revision`、`created_by_type/id`、`released_by_type/id`；
- `created_source_request_id/trace_id`、`released_source_request_id/trace_id`、`created_at/released_at`。

约束：

- system 只能从达到自动阈值的 identified session 创建 `target_type=session`；用户只能从 high_risk/frozen score
  创建 session 或其关联 Virtual Key overlay，且事务内同时具备 read+manage。
- session target 必须等于来源 score 的 `(scope_type,scope_key,virtual_key_id)`；Key target 必须等于来源 score 的
  `(tenant_id,virtual_key_id)`。客户端不能提交任意 Key ID。
- 部分唯一索引 `(tenant_id,source_session_risk_score_id,target_type) WHERE status='active'` 保证同一来源/目标
  只有一个 overlay；不同 high-risk session 可以各自创建同一 Key 的 active overlay，保留独立来源与处置责任。
- Gateway 在读取正文前 tenant-first 点查“当前 session active overlay OR 当前 Key active overlay”；普通 Key
  enable、Chat clear/expire 或释放另一个 target 均不能绕过。overlay 只允许 active -> released 且 revision+1。
- score 在任一自身来源 overlay active 时保持 `frozen`；release 一个自身 target 后若仍有另一自身 active overlay
  则继续 frozen，最后一个才转 resolved。Key 只有在所有来源 overlay 都 released 后才恢复；释放一个 score 的
  Key overlay 绝不能解除另一个 score 对同 Key 的冻结。

### 6.4 `session_risk_actions`

不可变处置历史，字段包括：

- `session_risk_action_id`、`tenant_id`、`session_risk_score_id`；
- `action=high_risk/freeze_session/freeze_virtual_key/dismiss/release_session/release_virtual_key/expire`；
- `reason_code` 固定枚举；`actor_type=system/user`、`actor_id`；
- `session_risk_freeze_id` nullable、`status_before/status_after`、`score_snapshot/state_revision`；
- `source_request_id/source_trace_id`、`digest_key_version`、`idempotency_key_digest/intent_digest` nullable；
- `idempotency_expires_at` nullable、`created_at`。

自动跃迁使用 system actor 且没有客户端幂等键；人工动作使用当前 session-risk HMAC version 与独立 domain tag
保存 digest，不保存低熵幂等键、Header 或可枚举裸 SHA-256。
同 tenant+actor+idempotency 的相同意图在 24 小时保留期内返回首次 action，不同意图 409；replay 不新增
Audit、action 或事件。查找同时计算 current/previous digest；previous 仅在没有 active/frozen API score 且没有
未过期 action digest 引用后可移除。

### 6.5 `session_risk_events`

- append-only source event，事件名恒为 `security.session_high_risk`；
- 保存 `session_risk_event_id`、tenant、score、action、可选 freeze、状态、revision、分数、计数、scope/target、
  actor、source request/trace 和 occurred_at；
- `UNIQUE(tenant_id,session_risk_score_id,state_revision)` 保证一次状态跃迁最多一个事件；
- 为首次 high_risk、score 状态变化及每个 overlay create/release 创建；即使 score 因另一 overlay 仍为 frozen，
  target 变化也使用新 revision 产生一条事件。不为每个低分 observation 发送 Webhook；
- owner trigger 将事件逐字段绑定到 score/action，UPDATE/DELETE 无条件拒绝。

### 6.6 既有表兼容升级

- `intent_risk_results.classification_mode` 增加 `sync_session`；RequestLog 的 intent schedule 增加 `completed`，
  仅用于可信 session 同步完整分类。0062 必须安全重建 SQLite CHECK 表并逐列复制，旧 0061 数据保持原 rowid/值。
- Usage spool schema 从 v3 升为 v4，新增固定 session-risk 摘要；v1-v3 仍可恢复，v4 exact-key 不包含正文、Header、
  turn/intent digest。RequestLog 增加 `session_risk_status=not_applicable/disabled/clean/monitoring/high_risk/frozen/
  duplicate/degraded/failed`、可选 score ID/scope type/state，确保成功、失败和 Redis 降级请求可追踪。
- PRD-069 Webhook exact validator 增加 `sync_session`，但 payload 仍不增加正文或动态字段；企业 Chat Control
  将 `gateway.session_risk_frozen` 精确映射为 `chat.session_risk_frozen` 403，其他未知 `gateway.*` 继续折叠为
  `chat.gateway_unavailable`，禁止任意错误透传。

### 6.7 RBAC revision 7

- 目录从 revision 6 的 57 项提升到 revision 7 的 59 项：
  `session_risk.read`（read）和 `session_risk.manage`（critical）。
- 只给仍精确匹配 revision 6 产品默认矩阵的 canonical Admin/SecurityAdmin 补权；自定义角色和已修改
  SecurityAdmin 不静默扩权。Developer/Viewer/Finance/Ops 默认都没有这两个权限。
- 新租户直接物化 revision 7 目录，六个角色的 `permission_revision` 仍从 1 开始，hash 使用 revision 7 默认集合。
- down 在删除 DDL 前拒绝 active/custom 新权限关系、PRD-070 业务事实、Webhook 订阅/投递、Audit 引用、
  后续 schema 和 FK 违规；回退后精确恢复 revision 6 行、rowid、角色 revision/hash 和关系全集。

## 7. API 设计

所有接口复用共享 FastAPI 工厂，成功与错误响应均带 `request_id/trace_id` 和响应 Header；
`Cache-Control: no-store`。tenant、actor、role 只从登录 Session 恢复。

### 7.1 `GET /security/session-risks`

权限：`session_risk.read`。后端先验证 Session/实时数据库权限，再解析 query。

允许 query：

- `status=high_risk|frozen|resolved|expired`；默认 `high_risk,frozen`；
- `scope_type=enterprise_chat|api_session`；
- `virtual_key_id`；
- `from_time/to_time`：UTC 时间，采用左闭右开区间，二者同时提供时必须满足
  `from_time < to_time`；
- `limit=1..100`，默认 20；
- opaque `cursor`，绑定 tenant、规范化筛选、`last_risk_at` 和 ID，使用服务端 HMAC。

禁止客户端 tenant、actor、排序字段、offset、include_prompt 或任意未知 query。列表只返回当前租户；
跨租户同 ID 同形 404。响应包含：

```json
{
  "runtime_status": "active",
  "summary": {
    "high_risk_count": 2,
    "frozen_count": 1,
    "resolved_count": 4
  },
  "session_risks": [
    {
      "session_risk_score_id": "srs_...",
      "scope_type": "enterprise_chat",
      "scope_reference": "chs_...",
      "virtual_key_id": "040638a9-7378-57a7-87c2-6c82267f7bcb",
      "status": "frozen",
      "score": 9600,
      "score_scale": 10000,
      "risky_turn_count": 3,
      "signal_counts": {
        "jailbreak": 3,
        "sensitive": 1,
        "tool_inducement": 0
      },
      "state_revision": 4,
      "threshold_version": "session-risk-threshold-v1",
      "last_source_request_id": "req_...",
      "window_expires_at": null,
      "first_high_risk_at": "2026-08-04T04:20:00Z",
      "frozen_at": "2026-08-04T04:25:00Z",
      "updated_at": "2026-08-04T04:25:00Z",
      "freezes": [
        {
          "session_risk_freeze_id": "srf_...",
          "target_type": "session",
          "status": "active",
          "created_at": "2026-08-04T04:25:00Z"
        }
      ],
      "actions": []
    }
  ],
  "next_cursor": null,
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

- `api_session.scope_reference` 只返回 `api_` + 16 位小写十六进制的短安全显示引用，不返回 Header 原文、
  内部 `srh_` 命名空间或完整 HMAC；
  action 始终使用不透明 `session_risk_score_id`。
- 每项 action 只返回固定 action/reason/status/actor display/时间/request ID，不返回 Audit 网络元数据或幂等 hash。
- `runtime_status=disabled/degraded/unavailable/active` 只说明 Control 当前运行配置；历史数据仍可查，不能把 disabled
  显示成“没有风险”。

### 7.2 `POST /security/session-risks/{id}/action`

权限：同时要求 `session_risk.read` 与 `session_risk.manage`。读取正文前完成两个后端权限；在
`BEGIN IMMEDIATE` 内再次按 tenant 和两个 permission 复核，防止依赖完成后撤权竞态。manage 不隐含 read，
自定义 manage-only 角色固定 403。路径 ID 固定 `srs_` + 32 hex。

Headers：

- `Idempotency-Key`：8..128 位 ASCII 白名单，必填；只保存分域摘要。
- request/trace 继续使用共享规范；不得把幂等键、session Header 或原因写入链路 ID。

严格 JSON：

```json
{
  "action": "freeze_virtual_key",
  "reason_code": "confirmed_attack",
  "expected_revision": 3
}
```

动作矩阵：

| action | 允许来源 | 目标 | reason_code |
| --- | --- | --- | --- |
| `freeze_session` | high_risk | 创建当前 session overlay，score=frozen | `confirmed_attack` |
| `freeze_virtual_key` | high_risk/frozen | 创建关联 Key overlay，score=frozen | `confirmed_attack` |
| `dismiss` | high_risk 且无 active overlay | resolved | `false_positive/verified_safe` |
| `release_session` | frozen 且 session overlay active | 释放 session；按剩余 overlay 决定状态 | `false_positive/incident_contained` |
| `release_virtual_key` | frozen 且 Key overlay active | 释放 Key；按剩余 overlay 决定状态 | `false_positive/incident_contained` |

- action 名编码固定 target，客户端不能提交任意 target ID。后端只从 score 的 tenant-qualified session 与
  `virtual_key_id` 派生 overlay；Virtual Key 冻结只允许来自已识别 high-risk/frozen session 的显式人工动作。
- system 自动阈值只执行 `freeze_session`；绝不自动升级 Key。同一来源重复创建相同 target 返回固定 409 或幂等
  replay；不同 high-risk score 可以各自确认冻结同 Key，确保后续 release 不丢失独立来源。
- action 以 `expected_revision` CAS；版本或状态不符返回 409，不覆盖并发自动冻结或另一管理员处置。
- 人工动作在同一数据库事务写 score、overlay、immutable action、Audit 和 source event；Redis reset/restore
  使用 score ID/revision CAS。DB 是冻结 overlay 权威，Redis 同步失败不允许返回虚假完成；相同幂等意图可恢复重试。
- release 只解除动作名对应的一个 overlay，不改变 Virtual Key 原有 disabled 状态，不恢复已清空/过期 ChatSession，
  不解除另一 target，也不删除 observation/action/event；最后一个 overlay 释放后 score 才 resolved。
- 成功响应返回更新后的单项、刚创建/重放的 action、`idempotent_replay` 和本次 request/trace。

## 8. PAGE-098 前端设计

### 8.1 页面布局

- 新导航 `session-risks` 位于“安全与治理”，要求 `session_risk.read`；页面标题为“高危会话”。
- 顶部使用紧凑状态汇总：待复核、高危冻结、已处置、运行状态；不使用营销 Hero 或装饰性嵌套卡片。
- 主工作区是筛选条 + 可扫描列表 + 详情侧栏。桌面双列，`390x844` 改为纵向，列表和详情不产生页面级横向溢出。
- 状态使用固定标签和非单一色：高危为红色、冻结为深色高对比、已处置为绿色、依赖不可用为琥珀色。
- 列表展示 scope 类型、安全引用、分数、风险 turn、信号计数、最后时间和状态；不显示 Prompt 或匹配片段。
- 详情展示阈值版本、session/Key overlay、last request、时间线和不可变处置记录；request ID 可跳转已有调用日志页。

### 8.2 处置交互

- 只有同时具备 `session_risk.read` 与 `session_risk.manage` 的 owner 才生成处置按钮和 Dialog DOM；read-only
  或 manage-only 自定义角色都不能处置。
- freeze session/freeze Key/dismiss/release session/release Key 按状态矩阵生成固定按钮、原因菜单和确认对话框；
  不提供自由文本或任意 target 输入。
- Dialog 显示 scope、当前状态、分数和将发生的固定结果，不显示敏感输入或 Header。
- 每次动作生成内存幂等键；网络/5xx 结果不确定时保留，明确 4xx 后用户修正意图才换键。
- tenant/user/Login Session/permission revision/fingerprint/navigation/filters/generation 任一变化，立即 abort 列表或 action，
  销毁 state、cursor、选中项、Dialog 和幂等键；迟到响应不得写回新 owner。
- 401 结束当前登录；403 重新拉取权限并清空页面；前端隐藏不替代后端两层权限校验。

### 8.3 前端响应安全

- client 对根对象、item、signal_counts、action、summary 使用 exact-key 和状态矩阵校验；未知字段拒绝整包。
- 所有显示字段经固定枚举或 HTML 转义；对象 ID/时间/分数有格式和范围门禁，不能把服务端自由文本放入 DOM。
- cursor、idempotency、完整 API session HMAC、authorization fingerprint 和 Token 不进入 URL、DOM、Storage 或 console。
- 页面不预取无权限资源；直达 `#session-risks` 无权限时由后端导航解析回退且网络请求数为 0。

## 9. Webhook 与审计

### 9.1 `security.session_high_risk`

exact payload 固定为：

```json
{
  "schema_version": 1,
  "session_risk_score_id": "srs_...",
  "session_risk_action_id": "sra_...",
  "session_risk_freeze_id": "srf_...",
  "scope_type": "enterprise_chat",
  "scope_reference": "chs_...",
  "status": "frozen",
  "target_type": "session",
  "score": 9600,
  "score_scale": 10000,
  "risky_turn_count": 3,
  "jailbreak_count": 3,
  "sensitive_count": 1,
  "tool_inducement_count": 0,
  "state_revision": 4,
  "threshold_version": "session-risk-threshold-v1",
  "occurred_at": "2026-08-04T04:25:00Z"
}
```

- API session 的 `scope_reference` 使用短安全引用，不提供完整 HMAC；payload 不含 Prompt、Response、规则 pattern、
  命中 hash、Token、Virtual Key 明文、Endpoint 或异常消息。
- validator 同时绑定 event ID、tenant、actor、object=`session_risk_score_id`、source request/trace 和 occurred_at。
- 先提交 source event，再机会性进入既有 runtime dispatcher；队列满、Endpoint 失败或死信不回滚冻结和 action。

### 9.2 管理审计

- 人工 freeze_session/freeze_virtual_key/dismiss/release_session/release_virtual_key 必须创建现有 `audit_logs`
  记录；action 名固定为对应的 `session_risk.*` 枚举。
- before/after 只含 score ID、scope type、状态、分数、revision、freeze ID 和 target type；不含 API session
  完整 HMAC、turn/intent digest 或幂等键。
- 自动 high-risk/freeze 由 session_risk_actions/source event 追踪，不伪造控制台用户 Audit。

## 10. 错误码设计

| code | HTTP | 场景 |
| --- | ---: | --- |
| `auth.token_missing/invalid/session_revoked` | 401 | Control 未登录或会话失效 |
| `auth.permission_denied` | 403 | 缺少 read/manage，且发生在正文前 |
| `session_risk.query_invalid` | 400 | 非法/未知筛选、cursor 或 query |
| `session_risk.not_found` | 404 | 当前 tenant 不存在该 score，跨租户同形 |
| `session_risk.payload_invalid` | 400 | 非 JSON、重复键、额外字段、非法 action/reason/revision |
| `session_risk.idempotency_invalid` | 400 | 缺失或非法幂等键 |
| `session_risk.idempotency_conflict` | 409 | 同 actor/key 不同规范化意图 |
| `session_risk.state_conflict` | 409 | 状态或 expected revision 已变化 |
| `session_risk.target_already_frozen` | 409 | 同一来源 score 已有该 target 的 active overlay |
| `session_risk.store_unavailable` | 503 | Control Redis 不可用，处置不能证明完成 |
| `session_risk.evidence_unavailable` | 503 | 数据库/Audit/source event 无法提交 |
| `gateway.session_risk_identifier_invalid` | 400 | API Session/Turn Header 未成对、非规范或重复 |
| `gateway.session_risk_turn_too_large` | 413 | 可信 session 的 current user 文本投影超过 256 KiB |
| `gateway.session_risk_turn_conflict` | 409 | 同一 API Turn ID 被用于不同 current-turn 意图 |
| `gateway.session_risk_frozen` | 403 | 当前 session/Key overlay 已冻结或本轮触发冻结 |
| `gateway.session_risk_unavailable` | 503 | enabled 时 Redis 不可用/合同损坏 |
| `gateway.session_risk_hmac_version_unavailable` | 503 | active/frozen API scope 引用的 key version 未配置 |
| `gateway.session_risk_evidence_unavailable` | 503 | 风险分数已更新但持久证据无法提交 |
| `chat.session_risk_frozen` | 403 | 企业 Chat 对 Gateway 冻结错误的精确安全映射 |

所有错误复用统一 envelope，包含本次 `request_id/trace_id`；错误 message/suggestion 是固定目录文本。
日志只记录 code、tenant、score/object ID、scope type、revision 和异常类名，不记录 Redis URL、Header、正文或异常消息。

## 11. 安全、权限与兼容性约束

1. tenant/Virtual Key 只从 Gateway auth 恢复；Control tenant/actor 只从 Session 恢复；任何客户端 tenant 字段拒绝。
2. 数据库读写以 tenant 为第一条件，复合 FK/owner trigger 同时证明 scope、Key、ChatSession、action 和 event。
3. `read/manage` 分权，写接口明确同时要求两者并在事务内复核；manage 不隐含 read，前端导航/按钮不构成授权。
4. API Session/Turn Header 使用版本化专用 HMAC 后立即覆盖原始引用；正文仅生成域分隔 turn-intent HMAC；
   原文、低熵键和 digest 均不转发、不进入日志、Webhook、DOM 或公开 API。
5. score 仅是 Chat/API 请求安全事实，不和 users、邮箱、IP、设备、部门或跨 Key 标识建立画像关系。
6. single-turn cap、最少风险 turn、30 分钟窗口和 Key 人工升级共同控制误杀；单个弱/强请求都不能冻结。
   Header 轮换不做跨 session 汇总，避免把共享 Key 变成用户画像或由单一调用者主动打挂整把 Key。
7. DLP/Intent 更早阻断继续保持原错误优先级；session risk 只补充未来冻结事实，不覆盖已有安全诊断。
8. enabled Redis 故障严格服从 public fail-closed/internal degraded-allow ADR，且 DB overlay/evidence 故障始终
   fail-closed；不以单进程内存伪造累计。disabled/degraded 必须可见，成功运行验收不得使用两者。
9. 风险冻结 overlay 独立于 Key disable 和 Chat clear；release 不能恢复其他原因已不可用的资源。
10. Webhook、日志、数据库、前端、测试证据均执行敏感扫描；合成攻击文本不得使用真实企业 Prompt 或 Key。
11. 配置 secret 使用 `exclude=True/repr=False`，摘要只返回 masked/missing，源码和 `.env.example` 只放占位符。
12. 公开 Header 是成对可选兼容扩展；不改变 OpenAI body 和 Provider payload。旧调用方不带两 Header 时不运行
    PRD-070 跨请求累计，只保留既有 DLP/PRD-069 单请求安全。

## 12. 测试用例

### 12.1 Shared 纯函数

- 三档 intent 分数、DLP block/log/allow、工具诱导、组合 cap 和整数边界精确。
- 普通工具使用、安全培训、规则说明、否定语境、单次弱命中不冻结。
- 20,000 code points、超长 head/tail、Unicode/NFKC、空/无 user、不可变 DTO 和输出不含正文。
- turn-intent HMAC 覆盖 256 KiB 边界、中段差异、文本 content parts、非文本忽略和超限 413，digest 不出现在 repr。
- enterprise/API session 的 `>=` 阈值、最少 turn、TTL、过期新 cycle、组合 assessment 与阈值版本。
- `classify_session_intent` 对历史 strong/current light 的双摘要只调用一次；DLP block 为零分类调用，普通无 session
  路径仍按 PRD-069 原同步/异步合同执行。

### 12.2 Redis store

- Redis 与 InMemory 合同：首建、组合 assessment、重复 turn、同 turn 异 intent conflict、并发同 turn、并发跨阈、
  revision、TTL、restore/reset CAS。
- pending 合同：EVAL 同步创建、pending 时拒绝新累计、DB 幂等重放、三元 ACK、ACK 丢失恢复、pending 时不超时、
  清债后恢复 TTL；pending payload exact-key 且不含敏感正文。
- 同 tenant 不同 session、同 session 不同 tenant/Key、Chat/API scope 与 current/previous HMAC version 完全隔离。
- Redis unavailable、AUTH/SELECT/协议错误、Lua error、损坏 hash、超时都返回固定 store error，不泄露 URL/密码。
- 仓库内 RESP 验收服务与生产 Store 走真实 TCP，覆盖 PING/AUTH/SELECT/EVAL/RESP 数组、半包、断连和超时；
  如环境未来已有仓库内 Redis binary，可追加真实 Redis smoke，但不得调用 WSL、下载组件或申请沙箱外权限。
- 首次 high risk/freeze 只返回一次 transition；并发不会重复 overlay/action/event。

### 12.3 迁移与数据库完整性

- 0061 -> 0062 -> 0061 -> 0062，schema、rowid、索引、trigger、角色关系、revision/hash 和数据计数精确。
- revision 7 为 59 项；只补 canonical Admin/SecurityAdmin，自定义矩阵不扩权，新租户角色 revision 为 1。
- scope/tenant/Key/ChatSession/Message 错配、非法 HMAC/version/ID、状态跳转、revision 倒退、重复 active cycle、
  重复 turn observation/event、非法 overlay target 和 immutable 行更新/删除全部被数据库拒绝。
- `sync_session/completed` 表重建保持 0061 行与 rowid；Usage spool v1-v4 恢复、RequestLog 新状态和 PRD-069
  Webhook exact validator 定向回归。
- down 的业务事实、Webhook 历史、自定义持权、Audit/后续 schema/FK guard 在首项 DDL 前失败关闭。

### 12.4 Gateway 主流程

- 企业 Chat 连续三个高风险 current turn：第二次进入 high risk、第三次冻结，第三次 Provider/Rate/Budget/Usage 为 0；
  后续同 ChatSession 固定 403，另一 Session 可用。
- 公开 API 规范 Session/Turn Header 三个不同高风险 turn 冻结该 session；同 Key 另一 Session 在 Key 未被人工
  冻结时可用。相同 Turn ID+意图重试不升分/不刷新 TTL，不同意图复用固定 409。
- 两 Header 均缺失时连续请求不创建 score/Redis key；只出现一个、重复或非法值固定 400；轮换 Session ID 不被
  偷偷合并，仍逐请求执行 DLP/PRD-069。
- 管理员从已识别 high-risk session 执行 freeze_virtual_key 后，同 Key 的无 Header、显式 Header和企业 Chat
  请求都在读取正文前被 overlay 阻断；普通 Key disable/enable 不改变该 overlay。
- 单次 strong、重复历史、四次普通对话、低分间隔超过 30 分钟均不误冻结。
- DLP block 为零意图分类调用；可信 session 的 strong/light/tool 路径每请求完整分类调用数恒为 1、异步入队为 0；
  无 session 路径保持 PRD-069 原调用/入队。Fallback/SSE chunk/Usage 不重复计分。
- 第三次非流式和流式都在 Provider/首块前阻断；未冻结流继续保持 Watchdog 和断连合同。
- Header 重复/非法/不成对、不同 tenant/Key 同 Header、客户端 `user/metadata` 伪造均按固定边界处理。

### 12.5 Gateway 异常与不泄密

- enabled 且 Redis 缺失/中断/损坏：public_service 503 且 Provider/Usage 为 0；internal_only 在 DB overlay 可查且
  未冻结时 degraded allow 并写 RequestLog，已冻结或 DB 不可用仍阻断。disabled 保持兼容但不作为验收成功路径。
- Redis 成功 DB 失败返回 evidence 503；下一请求先重放 pending，DB 已提交但 ACK 丢失不重复 observation/action/event；
  DB 成功 Webhook queue/full/unavailable 不回滚冻结。
- DLP/Intent 已阻断时 session risk 故障不覆盖原错误；后续对账仍安全失败关闭。
- current/previous HMAC 轮换期间旧 frozen scope 持续命中；移除仍被 active/frozen row 引用的 previous version
  固定 503，不以新 secret 创建旁路 scope。
- 日志、RequestLog error、observation、action、event、Redis dump、repr 都不含 Header、Prompt、Response、Key/Token。

### 12.6 Control API、权限与租户

- Admin/SecurityAdmin GET/action；仅 read 用户看列表不能 action；manage-only 自定义角色与其他缺任一权限角色
  POST 403 且不解析正文。
- 两 tenant 同 score/header/key 同形隔离；跨租户 ID 404；cursor/filter 绑定 tenant。
- freeze_session/freeze_virtual_key/dismiss/release_session/release_virtual_key 主流程、expected revision、
  同来源 target 已冻结、重复 idempotency replay、不同 intent conflict 和网络重试。
- session/Key overlay 同时 active 时分别 release；释放一个不解除另一个，不激活 disabled Key，也不恢复
  cleared/expired ChatSession。不同 score 冻结同 Key 产生独立 overlay，Key 直到最后一个来源 release 才恢复。
- Redis/DB/Audit/Webhook 故障与提交顺序验证；所有响应含 request/trace/no-store。

### 12.7 Console 与 Browser

- strict client exact-key、筛选、cursor、action matrix、idempotency 保留、公开错误和 XSS。
- read/manage 分离；manage-only 也无处置 DOM；无权限无导航、无 DOM、无预取；撤权、tenant/Session 变化和迟到响应清理。
- PAGE-098 空态、高危、冻结、已处置、依赖不可用、详情 timeline、跳转 RequestLog 和动作 Dialog。
- 应用内 Browser `1280x720` 与 `390x844` 验证筛选、处置、焦点、滚动、零横向溢出、注销和零页面 error。
- DOM/URL/localStorage/sessionStorage/console/截图 OCR 与运行日志敏感扫描无 Header/Prompt/密码命中。

### 12.8 分层回归与运行验收

- 先运行 Shared/0062/RBAC/Control/Gateway/Console 聚焦节点，再运行 DLP、Intent、Chat、Stream、Rate、Budget、
  Webhook、Audit、RequestLog 的必要定向回归；局部修改不重复从头跑数小时根级全量。
- 聚焦全部稳定后只执行一次最终所需回归，失败只重跑原失败节点和受修复影响集合，不重复无关全量。
- 最终运行 `scripts/ci/check-comments.py`、Ruff/compileall、Node/Console test 与 lint。
- 性能门禁：20k current-turn `classify_session_intent` 本机 P95 不高于 15ms；真实本地 Redis+SQLite 的 Provider 前
  session-risk 增量 P95 不高于 50ms；无 session 的 PRD-069 队列/TTFT 定向回归不得改变。
- 开发库实际升至 62/0062，integrity check/FK 通过；生产 Redis client enabled 并连接仓库内 RESP 验收服务、
  三服务健康 200；真实 Chat/API
  连续风险、冻结、GET/action/Webhook/双视口均通过后才允许标记完成。

## 13. 代码修改计划

### 13.1 Shared 与迁移

- 新增 `metahub_shared.session_risk` 模型、工具诱导规则、计分/阈值纯函数。
- 扩展配置、错误目录、RBAC catalog revision 7 和 Webhook exact validator。
- 新增 0062 五张表（含独立 freeze overlay）、既有 intent/RequestLog CHECK 安全重建、Usage spool v4、
  owner/state/immutable trigger、索引、Audit action 和正反迁移 guard。

### 13.2 Gateway

- 扩展 `GatewayAuthContext/InternalChatExecution` 的受信 chat_session_id/chat_message_id，但不把 scope 发往 Provider。
- 新增 `session_risk` store/service/models；Redis 最小 RESP 客户端支持安全 EVAL，测试使用同合同内存实现。
- 在共同 handler 的 request claim 与 tenant/environment/policy 错误之后、读取 payload 之前执行 session/Key overlay
  点查；该位置同时覆盖公开 API 与直接构造 AuthContext 的内部 Chat，不能只改 Virtual Key 鉴权函数。
- 新增单次 `classify_session_intent` 与 `sync_session/completed` 持久合同；DLP block、strong、normal current-turn
  三条路径统一构造一次 assessment。扩展 PRD-069 Webhook、RequestLog、Usage spool 和企业 Chat 固定错误映射。

### 13.3 Control

- 新增 models/routes/service，注册 GET/POST；复用 Auth、事务内权限、Audit、cursor HMAC 和 event dispatcher。
- 新增 `session_risk.read/manage` 依赖和导航定义；Action 使用 strict body、CAS 和持久幂等。
- 列表只查询当前租户持久事实，不从 Redis 扫 key 生成报表。

### 13.4 Console 与文档

- 新增 `lib/sessionRisks.js`、feature renderer/state/binder、main 导航生命周期和 CSS。
- 扩展 permissions/i18n/navigation 单测，新增 PAGE-098 专项和 Browser 验收。
- 完成后更新 README、directory-map、`docs/api/session-risks.md` 和
  `docs/testing/PRD-070-tests.md`，记录真实命令、结果和运行证据。

## 14. 编码前自审记录

### 第一轮：边界、权限、租户与兼容性

状态：**通过（2026-08-04，修正 2 个 P0、5 个 P1、1 个 P2）**。第一版独立审查先判定不通过，主 Agent
核对现有 PRD-069/066 代码后逐项修正，再复核正文无残留旧合同：

1. **P0，共享 Key 误杀**：删除无 Header 的 `api_virtual_key` score/阈值/报表。两 Header 都缺失时不做跨请求
   累计；Key 只能由已识别 high-risk session 的 read+manage 管理员人工冻结。
2. **P0，重复分类与 TTFT**：增加逐路径表。DLP block 零分类；可信 session 只调用一次
   `classify_session_intent`，返回历史 strong guard/current-turn light 双摘要并替代异步任务；无 session 完全保留
   PRD-069 原路径。同步扩展 `sync_session/completed`、Webhook、RequestLog、Usage spool v4 和性能门禁。
3. **P1，turn 幂等**：request_id 退回 HTTP 追踪职责；企业 Chat 使用 grant 中 chat_message_id；API 必须成对
   提交 Session/Turn Header，并用域分隔 HMAC 判定同意图 replay 与异意图 conflict。
4. **P1，多信号原子性**：Store 从逐 signal 改为一次接收不可变 `TurnRiskAssessment`，Lua 同时校验三类计数、
   组合 delta、4000 cap、turn digest、版本、pending 和 revision。
5. **P1，权限蕴含错误**：POST 的路由依赖与事务内复核都明确要求 read+manage；manage-only 自定义角色不能
   解析正文或处置，前端也不生成动作 DOM。
6. **P1，HMAC 轮换旁路**：增加 current/previous versioned keyring、所有 score 的 hmac_key_version、双候选查找和
   previous 移除门禁；缺少 active/frozen 引用版本时失败关闭。
7. **P1，Redis 服务模式**：enabled 默认 false；public_service Redis 故障 fail-closed；internal_only 仅在 DB
   overlay 可查且未冻结时 degraded allow 并写 RequestLog。DB/evidence/pending 故障在两种模式都失败关闭。
8. **P2，低熵摘要**：动作字段改为 `*_digest`，统一使用 versioned session-risk secret 的独立 domain HMAC，
   不再保存可枚举裸 SHA-256，并明确摘要不进入 API、日志、DOM 或 Webhook。

附加兼容修正：冻结采用独立 `session_risk_freezes`，session/Key overlay 用 OR 点查且分别 release；不同 score
对同 Key 的冻结保留独立 overlay，只有全部来源 release 才恢复。共同门禁放在
request claim 后、payload 读取前以覆盖公开 API 和内部 Chat；企业 Chat 只精确映射新增 frozen 错误，禁止透传
任意 `gateway.*`。tenant-first FK/owner/down guard、OpenAI body/Provider payload 和 SSE 首块合同均保持不变。

### 第二轮：并发、故障、误杀与可验收性

状态：**通过（2026-08-04）**。独立后端接入点调查与主 Agent 并发/故障复核完成，修正如下：

1. **部分提交恢复**：EVAL 在同 slot 原子声明 turn、累计 assessment 并生成最多一条安全 pending；DB 以 turn/revision
   幂等提交 observation、score、overlay、action、event，三元 ACK 丢失可重放。pending 必须先清债，不能忙等或
   带债进入 Provider；Webhook 派发失败不回滚已提交安全事实。
2. **并发与去重**：Lua 先拒绝旧 pending 再创建 turn claim，避免失败尝试留下“已处理”假象；30 分钟独立 claim
   同时覆盖零分/风险 turn。并发跨阈只有一个 revision，Fallback/SSE/Usage 不再计分，当前冻结发生在首块前。
3. **冻结来源**：新增独立 freezes 表；session 与 Key target 取 OR。不同 high-risk score 可各自冻结同一 Key，
   release 一个来源不解除其他来源；普通 Key enable、Chat clear/expire 也不能改变 overlay。
4. **分类恢复取舍**：审查比较了“沿用 async light、下次请求生效”和“可信 session 同步一次完整分类”。异步任务
   在进程崩溃后无法在不持久 Prompt 的前提下重建，可能留下不可清偿风险债务，因此选择后者；已把
   sync_session/completed、0062 表重建、Webhook、RequestLog、spool v4 与 15/50ms P95 门禁全部纳入范围。
5. **轮换与低熵摘要**：所有 Chat/API score 都冻结 hmac_key_version；previous 移除前必须停写、Redis pending
   verifier、DB active/frozen 与 24 小时动作摘要三重归零。HMAC 统一长度前缀编码和独立 domain，消除拼接碰撞。
6. **故障模式**：public Redis 故障 fail-closed；internal 只在 DB overlay 可证明未冻结时 degraded allow并留
   RequestLog/健康事实。DB、可见 pending、evidence、缺失 HMAC version 始终失败关闭；active overlay 禁止关功能。
7. **误杀边界**：无 Header 不累计、单 turn cap 4000、至少三风险 turn 才自动冻 session、30 分钟窗口、固定负例；
   Virtual Key 只能从 identified high-risk session 人工冻结，未引入 IP/user/设备或跨 Key 行为画像。
8. **沙箱验收**：当前 `D:\MetaHub` 内无 Redis binary，PATH 无 Docker/Podman。测试方案不调用 WSL、不下载、不提权；
   仓库内 RESP/EVAL 服务通过生产 Store 验证 TCP/AUTH/SELECT/EVAL/数组/故障路径，并在证据中如实标注，不冒充
   外部 Redis 产品。迁移、三服务、真实 Chat/API、Webhook 和双视口仍必须实际运行。

第二轮确认每个结论均有对应专项、异常/权限/并发测试和运行证据位置；两轮阻断项全部闭合，允许开始编码。

### 编码中兼容性修订复核：Virtual Key 既有 ID 合同

2026-08-04 编码核对开发库、`virtual_keys` 既有表和前序 API 后确认：项目现存
`virtual_key_id` 是 canonical 小写 UUID，不是新资源常用的 `vk_<32hex>`。本 PRD 只引用既有
Virtual Key，禁止为了页面筛选或风险关联重写资源 ID；上方响应示例已改为真实 UUID 形状。

第一轮兼容性复核：**通过**。Control query、数据库外键和 Console exact validator 均使用项目既有
UUID 合同；tenant-qualified owner 查询仍先校验当前租户是否拥有该 Key，非法、其他租户和不存在值
继续统一为 404，未放宽租户隔离。

第二轮安全复核：**通过**。UUID 只作为既有不透明资源 ID 展示和筛选，不参与 session 猜测；API session
scope、turn 与 owner-slot 仍使用独立 HMAC。修订不引入 Prompt/Header/Token 日志，不改变动作只能从
tenant-qualified score 派生目标的约束，并新增真实 UUID 的 Control/Console 合同测试。

### 编码中状态机合同复核：窗口终点 nullable

2026-08-04 联调动作事务时确认：0062 的状态 CHECK 规定 `high_risk` 保留
`window_expires_at`，而 `frozen/resolved` 必须清空该字段，因为冻结不能靠 TTL 自动解封，已处置
cycle 也不再参与活跃窗口恢复。原响应示例把 frozen 与非空终点同时展示，与数据库约束冲突。

第一轮边界复核：**通过**。公开 DTO 将 `window_expires_at` 改为 nullable，并按状态强校验；只有
`high_risk` 允许非空，`frozen/resolved/expired` 均要求空。该修订不改变列表排序、cursor 或历史时间线。

第二轮兼容复核：**通过**。Console exact validator 同步接受 nullable 并校验状态组合，旧的合法
high-risk 响应保持不变；不会把缺失字段与显式 `null` 混同，也不会允许 frozen cycle 被 TTL 恢复。

## 15. 实现后复核与验收状态

### 15.1 实现后第一轮自审：边界、权限与租户

状态：**通过（2026-08-04）**。

1. Shared 只输出固定 taxonomy、整数分数、阈值、HMAC 摘要和安全枚举；Prompt、Response、API
   Session/Turn Header 原文不进入持久层、日志、事件或前端。
2. Gateway 只对可信企业 Chat scope 或严格成对的公开 API Header 建立累计；冻结预检位于正文读取和
   Provider 路由之前。既有 DLP/Intent 错误优先级保持不变，风险累计失败不会把原安全错误改写成成功。
3. Control 的 GET 要求 `session_risk.read`，POST 同时要求 `session_risk.read/manage`；路由依赖和
   `BEGIN IMMEDIATE` 事务内均复核当前 tenant 权限。跨租户 score 统一不可见，处置目标只能从当前
   tenant 的 score 派生。
4. session/Virtual Key 冻结使用独立 overlay；普通 Key 启用、Chat 清空/过期或释放另一来源都不能绕过
   当前冻结。动作使用 revision CAS、持久幂等和安全审计投影。
5. 0062 的业务表、索引、复合外键和触发器均 tenant-first；down migration 在业务事实、自定义授权或
   未知依赖存在时原子拒绝，不静默丢数据。

### 15.2 实现后第二轮自审：并发、故障与兼容性

状态：**通过（2026-08-04）**。

1. Redis Lua 对 turn claim、累计、阈值跨越、pending 和 revision 做单次原子更新；ACK 丢失、重复 turn、
   异意图重放、过期 cycle 和旧 revision 均有定向测试。
2. Redis RESP 客户端以真实 TCP 合同覆盖 PING、AUTH、SELECT、EVAL、RESP 数组、半包、断连和超时；
   全程只使用仓库内验收服务，不下载、不调用 WSL/Docker，也不把它描述为外部 Redis 产品验收。
3. Usage crash spool 升至 v4，reader 明确兼容 v1-v4 并拒绝未知 v5；180 节点受影响回归暴露的 6 个
   共同漂移节点已定向修复，原失败及直接受影响集合 `8/8`、后续专项 `10/10` 通过，未重复运行整组。
4. 真实三轮风险分数为 `3600 -> 7200 -> 10000`，随后冻结预检返回
   `gateway.session_risk_frozen`；8 条被治理请求均未产生 RoutingAttempt 或 Usage。
5. 五类人工处置、幂等回放、幂等冲突、旧 revision、Viewer 403 和跨租户 404 均经真实 Control API
   验证；所有分支返回统一错误结构、`request_id/trace_id` 和 `Cache-Control: no-store`。
6. 开发库已升至 `62/0062`，`integrity_check=ok`、外键违规 0；临时 Provider 健康状态已恢复，第二租户
   验收资源已禁用，数据库与运行日志敏感明文扫描为 0。

### 15.3 HMAC 轮换交叉审查

状态：**通过（2026-08-04）**。

1. previous 移除 verifier 同时核对 Redis score/pending/turn claim、数据库 active/frozen score、
   24 小时内人工动作 digest 和未知版本；运维脚本要求先停 Gateway 新写入，不会自动删 key 或改数据。
2. 纯内存扫描覆盖 SCAN 多页、重复 key、自然过期、unknown version、legacy claim、游标循环及协议污染；
   报告只含计数，不暴露 URL、Secret、tenant、Session/Turn、Redis key 或 digest。
3. 第一轮交叉审查修正未知 Redis Hash `schema_version` 未失败关闭；第二轮修正 Store 构造阶段异常可能
   逃逸 CLI 包装的问题，并把数据库父目录访问触发的 `OSError/PermissionError` 统一转换为固定的
   verifier unavailable。最终新增扫描专项 `13 passed`、相关 Store/HMAC/RESP 回归 `35 passed`、
   数据库文件系统故障节点另行 `1 passed`、CLI 专项 `3 passed`；固定退出码与无 traceback/Secret
   泄漏均有测试。由于文件系统故障节点是随后单独执行，结果如实记录为 `35 passed + 1 passed`。

### 15.4 当前完成门禁

- 已通过：聚焦 Python `86 passed`、性能 `1 passed`、迁移 `6 passed`、PAGE-098 单元/合同
  `11 passed`、Console lint `201 files`、Ruff、compileall 和中文注释门禁。
- 已通过：真实 Gateway/Control/Console 健康检查、连续风险累计、冻结、五类处置、权限、租户、异常、
  并发、迁移和隐私验收。权威运行证据位于
  `.artifacts/prd070-runtime/run-20260804T091013/`。
- 已通过：筛选栏按容器自动换行的 CSS 修订及 PAGE-098 `11/11` 单元/合同测试。
- 已通过：应用内 Browser `1280x720` 与 `390x844` 双视口真实页面验收。默认高风险列表、已处置筛选
  返回 2 项、详情、2 个冻结 overlay、5 条动作时间线和双视口零横向溢出均已核对；桌面筛选栏曾出现
  容器溢出，已改为 `repeat(auto-fit, minmax(180px, 1fr))` 并重新验收。最终恢复检查重新打开
  `http://localhost:5173/#session-risks` 并登录，确认高风险标题 2 个、运行状态徽标 2 个、筛选按钮 1 个、
  登录页 0 个。文本证据位于 `.artifacts/prd070-browser/in-app-browser-acceptance.txt`。
- 并行 Edge extension DOM 读取超时记录保留在
  `.artifacts/prd070-browser/browser-control-failure.txt`，仅作为非权威诊断，不覆盖已经完成的应用内
  Browser 验收；该诊断未生成截图，文档也不声称存在截图制品。

设计、实现、自动化、文档、迁移与实际页面/运行验收均已闭合，PRD-070 已完成；总体目标仍为
`all_108_prds_complete=false`。下一项为已从 Excel 交叉确认、尚未开始设计的 PRD-071。
