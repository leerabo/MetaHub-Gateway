# PRD-069 - 轻量敏感意图分类

状态：**已完成**。两轮设计自审、实现、专项与分层回归、数据库迁移、真实 API、双视口 Browser、
性能门禁、异常/权限流程和最终运行复核均已通过。

总体目标仍在进行中：`all_108_prds_complete=false`。

## 1. 需求理解

### 1.1 权威来源与开发顺序

- Excel `01_PRD总表` 与 `03_M2-M3扩展交付` 对本条的一致定义为：
  `PRD-069 | M2-企业增强 | P2 | 扩展层 | 语义安全 | 轻量敏感意图分类 | 安全负责人`。
- 本条识别索要系统提示词、索要密钥、绕过安全策略和典型越狱等语义风险，输出
  `risk_type/risk_score`。默认检测为异步或旁路；只有高精度强规则允许同步阻断。
- Excel 中 `API-311..313` 是 `POST /security/classify-intent` 被机械拆分后的追踪编号，实际合同只有
  一条 Control API，不得误建三个接口。
- 页面为 `PAGE-096 安全规则页` 和 `PAGE-097 日志详情`；对象为
  `OBJ-093 IntentRiskResult`；事件为 `EVT-076 security.intent_detected`；
  `TC-0137/TC-0138` 分别覆盖主流程，以及误判、性能、权限、异常和不泄密。
- PRD-068 已完成且 README 已把当前开发项切换到 PRD-069，因此本条是按里程碑、优先级和 Excel
  行序交叉确认的下一条未完成 PRD，不是仅凭编号猜测。

### 1.2 架构与前序合同

- Control Plane 负责管理入口、权限、查询和安全结果展示；Gateway Data Plane 负责 Provider 前的
  同步强规则闸门和非阻塞旁路投递。Gateway 不得同步回调 Control API。
- PRD-068 已冻结当前热路径为：
  `Policy -> mask -> Routing -> DLP -> Rate Limit -> Budget -> Provider`。
  本条插入为：
  `Policy -> mask -> Routing -> DLP -> Intent strong guard / async enqueue -> Rate Limit -> Budget -> Provider`。
- PRD-024 的 DLP 已定义 Chat `role=user` 文本抽取、DLP 优先级和
  `gateway.dlp_blocked`。本条复用同一公开文本抽取函数；若 DLP 已阻断，语义分类不运行，原错误码和
  DLP 证据保持不变。
- PRD-054 的 RequestLog/Explain 是历史冻结事实。本条新增独立、可选的 `intent_risk` 详情对象，
  不把异步缺失解释成 clean，也不使用当前分类器重算历史。
- PRD-057 已提供持久 Webhook 投递基础设施。本条只新增持久 source event、共享事件白名单和精确
  payload/envelope 校验，不另造网络发送器。
- PRD-059 要求每个后端入口使用稳定 permission code，并逐请求读取当前租户数据库权限矩阵。
  前端隐藏导航和按钮只改善体验，不能替代后端校验。
- 默认上游仍是智谱、通义、文心、火山、混元、DeepSeek 官方或云服务，以及客户本地
  OpenAI-compatible Endpoint。分类器只在本地运行，不调用 OpenRouter、硅基流动或任意上游模型。

### 1.3 本期交付范围

1. 在 Shared SDK 新增确定性纯规则分类器，支持固定 taxonomy、整数基点分数、强规则子集、版本和
   有界输入；Control 与 Gateway 复用同一实现。
2. Gateway 在 DLP 之后运行强规则扫描。强规则命中时 Provider 前返回
   `gateway.intent_blocked`；未命中时通过专用有界进程内队列投递完整轻量分类，主请求不等待结果。
3. 新增 `intent_risk_results` 保存终态安全元数据，新增 `intent_risk_events` 保存
   `security.intent_detected` source event；两表都带 `tenant_id`，均不保存正文或命中片段。
4. RequestLog 新增不可回写覆盖的调度/强阻断安全字段。详情查询按
   `(tenant_id, intent_risk_result_id, source_request_id)` 关联独立结果，区分
   `disabled/pending/clean/detected/blocked/failed/skipped`。
5. 新增 `POST /security/classify-intent`，供安全负责人进行单次显式分类；接口严格 JSON、后端权限
   优先、统一错误、`request_id/trace_id` 和敏感输入短生命周期。
6. PAGE-096 新增独立 `intent-security` 导航，提供只读 taxonomy/版本状态和单次分类工作台；
   PAGE-097 扩展现有调用日志详情，展示安全元数据而不展示输入。
7. 新增独立权限 `intent_risk.classify`，默认仅授予 Admin 和 SecurityAdmin；日志详情继续使用
   `request_log.read`。
8. 补齐迁移、Shared/Control/Gateway/Console 聚焦测试、运行 API、Webhook、三服务和桌面/移动验收。

### 1.4 明确不做

- M2 不训练或微调大型安全模型，不调用外部 LLM 做分类，不下载远程规则或模型制品。
- 不新增租户自定义语义规则 CRUD。Excel 没有规则读写 API，因此 PAGE-096 不能用前端常量冒充可编辑
  运行配置。
- 不扫描模型输出、system/assistant/tool 消息、文件、图片、音频、OCR、URL 内容、Embeddings、
  Responses API 或跨请求会话累计上下文。输出安全和多模态安全属于后续 PRD。
- 不让异步旁路结果中断已经开始的流、不把旁路 detected 显示为 blocked、不为进程内队列虚构
  durable retry 或 exactly-once 消费。
- 不把正文、归一化文本、命中短语、字符位置、可字典反推的裸 hash、Prompt、Response、Key、Token、
  Endpoint 或自由异常文本写入数据库、日志、事件、URL、Storage 或前端状态对象。
- 不改写既有 `dlp-rules` 路由、DLP 优先级、Policy DSL、Provider 路由、预算或 Fallback 语义。

## 2. 涉及模块与运行时设计

### 2.1 Shared 纯分类器

新增 `shared-sdk/python/metahub_shared/intent_security/`：

- `models.py` 定义冻结的分类输入边界、`IntentClassification`、风险类型、动作、模式、原因码和版本常量。
- `classifier.py` 提供 `classify_strong_intent(texts)` 与 `classify_intent(texts)` 两个纯函数。
- 分类器不接收 `tenant_id`、数据库连接、HTTP Client 或 logger；相同文本和版本必须得到字节级一致结果。
- 输入为已由 Gateway/Control 严格提取的 `tuple[str, ...]`，只限制合计最多 20,000 code points；
  不额外发明与 DLP 不同的 64 段上限。每个非空片段至少消耗一个 code point，片段数量也被总字符上限
  自然约束。Control 超限显式拒绝，Gateway 沿用 DLP 的前 20,000 code points 有界扫描并把
  `input_truncated=true` 冻结到结果和日志，分类器内部不得无界递归、回溯或复制。
- 规范化只在内存中执行：Unicode NFKC、大小写折叠、有限空白/分隔符归一。不得把规范化文本放入
  dataclass `repr`、异常、日志或结果。
- 匹配使用有限 token/短语组合和没有嵌套量词的预编译表达式；不使用 `eval`、动态正则、租户输入规则
  或概率性采样。

### 2.2 Taxonomy、分数和决胜规则

`risk_type` 固定为：

| risk_type | 含义 | 典型范围 |
| --- | --- | --- |
| `none` | 未达到检测阈值 | 普通问答、合规安全培训、开发文档讨论 |
| `credential_exfiltration` | 索要、输出、泄露密钥或凭据 | API Key、Token、Secret、密码 |
| `system_prompt_extraction` | 索要隐藏系统/开发者提示词 | 输出 system prompt、developer message |
| `policy_bypass` | 明确要求绕过或关闭安全策略 | 跳过审核、禁用过滤、绕过权限 |
| `jailbreak` | 通过角色重置或指令覆盖获得被禁止能力 | 忽略先前指令、DAN 类越狱 |

- `risk_score` 是 `0..10000` 的整数基点，响应同时返回固定 `risk_score_scale=10000`；禁止 float。
- `none` 必须为 0；非 `none` 必须大于 0。检测阈值和各规则分数属于冻结 ruleset，不接受客户端覆盖。
- 多类别同时命中时先按分数降序，再按
  `credential_exfiltration > system_prompt_extraction > policy_bypass > jailbreak` 决胜；不得依赖 dict/set
  遍历顺序。
- 强规则只包含低误阻断、高精度的“敏感目标 + 明确提取/绕过动作”组合，分数至少 9500。
- 完整轻量分类可使用更多弱信号组合，但它只产生 `observe`，不能同步阻断。安全教育、检测、治理、
  引用或防护语境可降低分数；即使发生误判也不会中断主请求。
- `detector_version` 固定代码算法版本，`ruleset_version` 固定规则目录版本；每个持久结果保存当时版本，
  页面和历史查询不得用当前版本重算。

### 2.3 Gateway 编排

对 Chat 请求，DLP 通过后执行以下一次性流程；Fallback 不重复分类：

1. 使用 DLP 导出的同一文本抽取函数读取 policy mask 后的 `role=user` 文本，不扫描其他角色或内容类型；
   抽取函数同时返回 `input_truncated`，避免只扫描前缀却在日志中宣称完整覆盖。
2. 同步调用 `classify_strong_intent`。纯分类器发生意外异常且数据库可写时创建
   `failed/sync_strong/none/classifier_failed` 结果，并把 RequestLog 调度状态冻结为 `failed_closed`；随后固定
   失败关闭为 `gateway.intent_classifier_unavailable`，不得继续 Provider。分类已完成但 result/event 持久化
   失败时改用 `gateway.intent_result_store_unavailable`；若分类失败且 failed 结果也无法持久化，结果存储错误
   优先。两类错误都只保留已完成 request claim、`failed_closed` 摘要和固定结构化告警，不能写异常文本。
3. 强规则命中时先创建 `blocked/sync_strong` 终态结果和 source event；只有事务提交成功后才抛出
   `IntentBlockedError`。该异常像 `DlpBlockedError` 一样携带不含正文的 `IntentRiskDecision`，外层统一把摘要
   应用到 `RequestLogContext`。存储失败改抛 `IntentResultStoreUnavailableError`，不得误报为已持久化 blocked；
   Rate Limit、Budget、RoutingAttempt、Provider、Usage、Cost 和 Fallback 均不得发生。
4. 未命中时构造 `IntentClassificationJob`。正文只存于进程内 job 的 `repr=False` 字段；job 仅额外携带
   tenant、request、trace、Virtual Key ID、结果 ID、版本和入队时间等可信元数据。
5. 通过 `put_nowait` 投递容量固定的专用队列。成功后 RequestLog 标记 `queued/async_light` 并继续主链路；
   队列缺失或满载时标记 `skipped` 和固定原因 `worker_unavailable/queue_full`，仍继续主链路。
6. Worker 通过 `asyncio.to_thread` 执行本地完整分类和 SQLite 事务。分类器异常但数据库仍可写时，以新的短事务
   写入 `failed` 结果；SQLite 的 begin/insert/commit 本身失败时不能假装能再次写入 `failed`，结果保持不存在，
   RequestLog 的 `queued` 事实映射为 `pending/best_effort`。两类异常都只记录异常类型和固定码，不记录异常文本
   或 job 正文。
7. detected 结果在同一事务创建 `intent_risk_events`；提交后把 canonical Webhook event 投给既有 runtime
   event dispatcher。Webhook 队列故障不能回滚安全结果，也不能改变已完成的模型响应。

Intent dispatcher 必须在 `install_runtime_event_dispatcher` 之后、Usage/Policy 等外层 scheduler 之前组合安装。
这样启动时 Webhook queue 先可用；退出时先有界排空 intent job，再由内层 event dispatcher 排空新产生的
`security.intent_detected`，不会在 Webhook worker 已停止后继续产生事件。取消或超时后必须清空 queue 引用和
未完成 job 的正文引用。

强规则失败关闭，异步旁路失败开放；这是两条不同的代码路径和明确合同，不由日志级别或前端状态隐式决定。

### 2.4 异步状态与 TTFT 合同

- 主请求只执行强规则和 `put_nowait`，不 `await` 完整分类、数据库结果写入、Webhook 或 worker drain。
- RequestLog 的 `queued` 只是“已进入当前进程内队列”，不是持久任务承诺。结果不存在时详情必须显示
  `pending`，不得显示 clean。
- `skipped` 表示队列未接收任务；`failed` 只表示 worker 已接收、分类失败且失败终态成功落库。数据库持久化
  失败时没有结果终态，详情继续显示 `pending/best_effort`。三者都不等于安全，也不能据此推断输入 clean。
- 进程退出最多有界排空；超时取消后未完成项可能永久保持 pending。本期不保存正文，因此不能在重启后重放。
  UI 和文档必须如实展示 `best_effort`，不能宣称 durable retry。
- 流式 TTFT 验收使用故意延迟的完整分类器，证明 Provider 首块在分类完成前到达；强规则扫描本身仍在
  Provider 前同步执行，并通过 20,000 字符性能基准约束。

### 2.5 Control 单次分类

- Control API 使用同一 `classify_intent` 完整规则集并等待本次结果，不调用 Gateway 或 Provider；CPU 分类和
  SQLite 事务通过 `asyncio.to_thread` 离开 ASGI event loop，避免一个 20,000 字符人工检测阻塞其他请求。
- API 本身没有模型调用，因此 `detected` 只表示“需要安全复核”，动作固定 `review`；clean 动作固定 `allow`。
- 每次结果都作为 `manual_sync` 终态持久化。该敏感写入口在共享 request-context middleware 中忽略外部
  `X-Request-ID/X-Trace-ID`，只使用服务端随机链路 ID；因此本期没有客户端幂等重放或 409 合同。若未来
  增加幂等键，必须是独立严格随机格式并只保存 domain-separated HMAC，不能把任意 Header 当持久身份。
- 分类器异常且数据库可用时保存 `failed/manual_sync/none/classifier_failed` 后返回
  `intent_risk.classifier_unavailable`；result/event 事务失败返回 `intent_risk.result_store_unavailable`，
  两类故障不能混为一个诊断事实。
- 检测到风险时结果、source event 在同一事务提交，响应只在提交后返回；Webhook 发送在提交后执行。

## 3. 数据库设计

迁移编号固定为 `0061`，文件名建议
`0061_lightweight_sensitive_intent_classification.sql`。迁移必须提供等价 `.down.sql`，并沿用当前项目的
栈顶、对象空闲、schema 精确形状、数据计数、外键和 integrity guard。

### 3.1 `intent_risk_results`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `intent_risk_result_id` | TEXT，主键，固定 `irr_` 前缀 | 服务端生成，不接受客户端 ID |
| `tenant_id` | TEXT，NOT NULL，FK tenants | 所有查询和关联的第一 owner 条件 |
| `source_kind` | `gateway/manual` | 区分模型调用和显式分类 |
| `source_request_id` | TEXT，NOT NULL | Gateway 请求 ID 或 Control operation request ID |
| `source_trace_id` | TEXT，NOT NULL | 首次创建链路，不被重试覆盖 |
| `actor_type` | `virtual_key/user` | 可信身份类型 |
| `actor_id` | TEXT，NOT NULL | Virtual Key ID 或当前用户 ID；不对外展示 |
| `status` | `clean/detected/blocked/failed` | 只保存终态，不保存可漂移 processing |
| `classification_mode` | `sync_strong/async_light/manual_sync` | 冻结执行语义 |
| `risk_type` | 固定 taxonomy | failed/clean 必须为 none |
| `risk_score` | INTEGER，0..10000 | 整数基点 |
| `risk_score_scale` | INTEGER，恒为 10000 | 防止调用方猜测量纲 |
| `action` | `none/allow/observe/block/review` | `none` 只用于 failed；其余值必须与状态和模式一致 |
| `reason_code` | 固定枚举 | 不保存规则正文或自由错误 |
| `detector_version` | TEXT，有界 | 冻结算法版本 |
| `ruleset_version` | TEXT，有界 | 冻结规则版本 |
| `input_code_points` | INTEGER，0..20000 | 只保存长度，不保存正文 |
| `input_truncated` | INTEGER，0/1 | Gateway 是否只扫描有界前缀；manual 必须为 0 |
| `duration_ms` | INTEGER，非负有界 | 本地分类耗时，不含 Webhook |
| `error_code` | TEXT，可空 | 仅 failed 使用固定内部码 |
| `created_at/completed_at` | UTC TEXT | 终态创建和完成时间 |

约束：

- Excel `OBJ-093` 通用对象模板列出了 `updated_at`；本表保存一次写入后不可变的终态，继续增加可更新时间会
  暗示结果允许改写，因此实现明确使用 `created_at/completed_at` 表达生命周期且不设置 `updated_at`。
- `UNIQUE(tenant_id, source_kind, source_request_id)` 保证一次 Gateway/Control operation 最多一个终态；
  `classification_mode` 不能参与唯一键，否则同一 Gateway 请求可能同时拥有 strong 与 async 两条结果。
- `UNIQUE(tenant_id, intent_risk_result_id)` 支持所有 tenant-qualified 复合外键。
- 完整状态矩阵固定为：clean 只能是 `risk_type=none/risk_score=0/action=allow/error_code=NULL`；
  `detected+async_light` 只能是非 none、正分数和 `observe`；`detected+manual_sync` 只能是非 none、正分数和
  `review`；blocked 只能由 `gateway+virtual_key+sync_strong` 产生，必须为非 none、至少 9500 分和 `block`；
  failed 只能是 `risk_type=none/risk_score=0/action=none`，并携带固定 `error_code`。
- 来源矩阵固定为：gateway 只能使用 `virtual_key + sync_strong/async_light`，manual 只能使用
  `user + manual_sync`；`sync_strong` 只产生 blocked/failed，`async_light` 只产生 clean/detected/failed，
  `manual_sync` 只产生 clean/detected/failed，不产生 blocked/observe。
- Gateway 结果 owner 以 `gateway_request_claims` 为权威：`(tenant_id, source_request_id)` 必须命中 claim，
  `source_trace_id=claim.trace_id`、`actor_id IS claim.virtual_key_id`。异步结果不得外键依赖可能尚未完成最终
  upsert 的 RequestLog，也不得等待 RequestLog 后再分类。Manual 结果以 `(tenant_id, actor_id)` 复合校验 users。
- `BEFORE INSERT` 触发器验证 ID 前缀、状态矩阵和 tenant/source/actor owner；结果的
  `BEFORE UPDATE/DELETE` 无条件拒绝，保证终态不可换主或改写。
- 不存储输入 hash。唯一性只依赖服务端 request identity，避免为低熵攻击短语建立可离线枚举索引。

索引：

- `(tenant_id, source_kind, source_request_id)`：日志详情关联。
- `(tenant_id, status, completed_at DESC, intent_risk_result_id DESC)`：安全结果运维查询和事件复核。
- `(tenant_id, risk_type, completed_at DESC)`：后续安全报表预留；本期不公开列表 API。

### 3.2 `intent_risk_events`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `intent_risk_event_id` | TEXT，主键，固定 `irevt_` 前缀 | source event ID |
| `tenant_id` | TEXT，NOT NULL | 租户 owner |
| `intent_risk_result_id` | TEXT，NOT NULL | 复合 FK 到同租户结果 |
| `event_type` | 恒为 `security.intent_detected` | 不允许任意事件名 |
| `source_request_id/source_trace_id` | TEXT | 与结果逐字段一致 |
| `source_kind/classification_mode` | 固定枚举 | 与结果一致 |
| `risk_type/risk_score/risk_score_scale` | 固定安全摘要 | 与结果一致 |
| `action/reason_code` | 固定安全摘要 | 与结果一致 |
| `detector_version/ruleset_version` | TEXT | 与结果一致 |
| `input_truncated` | INTEGER，0/1 | 与结果一致，Webhook 消费方可判断覆盖范围 |
| `actor_type/actor_id` | 固定类型和内部 ID | 与 result 一致，仅用于绑定 envelope，不进入 payload |
| `created_at` | UTC TEXT | 必须等于结果 completed_at |

- 只有 `detected/blocked` 可以生成事件；`UNIQUE(tenant_id, intent_risk_event_id)` 和
  `UNIQUE(tenant_id, intent_risk_result_id)` 保证 tenant-qualified identity 以及每个结果至多一个事件。
- 事件使用 `(tenant_id, intent_risk_result_id)` 复合 FK；`BEFORE INSERT` 触发器逐字段验证 result owner、
  终态、分数、版本和时间，`BEFORE UPDATE/DELETE` 无条件拒绝。
- 表中不保存 Webhook URL、Secret、签名、正文或 matched phrase；投递事实继续归既有 Webhook 表所有。

### 3.3 `request_logs` 扩展

新增以下安全元数据列，旧行使用 `disabled` 默认值：

| 字段 | 说明 |
| --- | --- |
| `intent_risk_result_id` | 本请求预生成的结果 ID；disabled 时为空 |
| `intent_risk_schedule_status` | `disabled/queued/skipped/blocked/failed_closed` |
| `intent_risk_mode` | `async_light/sync_strong` 或空 |
| `intent_risk_type` | 仅同步 blocked 冻结类型，异步结果从独立表读取 |
| `intent_risk_score` | 仅同步 blocked 冻结整数分数 |
| `intent_risk_action` | 强规则命中为 `block`；failed_closed 为 `none`，异步调度阶段为空 |
| `intent_risk_reason_code` | 强规则、queue_full 或 worker_unavailable 等固定原因 |
| `intent_risk_detector_version` | 当次调度使用的分类器版本 |
| `intent_risk_input_truncated` | 是否仅扫描了有界前缀；旧行固定 0 |

- Gateway 主请求是 `request_logs` 的唯一写者；异步 worker 不 UPDATE RequestLog，避免与 finally upsert 竞争或
  把成功模型调用改成失败。
- 主 `record_request_log` upsert 与 spool 恢复入口都必须显式携带并冻结 intent 字段。冲突更新不得用普通
  `excluded` 覆盖安全事实：result ID 一旦非空不得改变，`queued` 不得回退 `disabled`，`blocked` 不得被
  后续成功上下文覆盖；同 request 的不同 mode、detector version 或 result ID 必须触发 integrity conflict。
- RequestLog insert/update trigger 固定组合：disabled 时其余 intent 字段全空；queued 时 result ID、mode、
  detector version 必填且允许结果尚不存在；skipped 仅允许 `queue_full/worker_unavailable`；blocked 必须关联
  同 tenant/request/result 的 blocked 结果；failed_closed 只允许 `sync_strong/none` 和
  `classifier_failed/result_store_unavailable`，result 可为空，若存在则必须是同 owner 的 failed 结果。异步结果
  可能先于 RequestLog，因此 result trigger 只查 claim。
- 详情查询按 tenant、result ID、source request 三项同时关联；结果存在时以不可变终态为准，不存在时由
  schedule marker 映射为 pending/skipped/disabled/failed。failed_closed 绝不能显示为 clean 或普通 skipped。
- `terminal_stage_v1` 和 Request Explanation v1 **完全不改**。三个语义安全 Gateway 错误映射到既有
  `dlp` 广义安全阶段，精确原因只由 RequestLogDetail 新增的 `intent_risk` 对象表达；既有
  `dlp_status/dlp_matches` 仍保持真实 DLP 值，不能伪造 DLP 命中。
- RequestLog wire 只新增 `intent_risk` 对象，不改变既有字段、筛选枚举、10 阶段/11 区块、DLP 证据或
  历史结果。

### 3.4 Usage crash spool v3

- `RequestLogContext` 新增字段后，FinOps Usage crash spool payload schema 从 v2 推进到 v3；文件 envelope
  schema 不变。
- reader 精确接受 v1/v2/v3：v1 缺少 policy mask 和 intent 字段，v2 含 policy mask、缺少 intent，v3 才
  含本条新增的安全元数据。每版使用独立 exact context field set，不能让旧 payload 通过宽松默认冒充 v3。
- writer 只生成 v3。spool 允许的 intent 字段与 RequestLog 安全摘要相同，不得加入正文、规范化文本、命中
  片段、输入 hash、job、规则表达式或异常文本。
- replay 把 v1/v2 缺失的 intent 状态恢复为 disabled，把 v3 原样恢复；未知 future version 继续失败关闭。

### 3.5 RBAC catalog revision 6

新增：

| permission | domain | 风险级别 | 默认角色 |
| --- | --- | --- | --- |
| `intent_risk.classify` | security | write | Admin、SecurityAdmin |

- catalog 从 revision 5 推进到 6，`rbac_permission_templates`、每租户 `permissions`、角色模板和矩阵 hash
  必须原子迁移。
- up 前 canonical 可扩展集合必须精确证明：Admin 的当前 `permission_revision` 为 1..5、hash 等于 revision 5
  Admin hash 且关系集完整；SecurityAdmin 的当前 revision 为 1..2、hash 等于 revision 5 SecurityAdmin hash
  且关系集完整。所有 Admin 必须进入扩展集合，否则迁移失败关闭；自定义 SecurityAdmin 不自动补权，revision、
  hash 和关系集保持不变。
- up 后 catalog 固定为 57 个 permission template、151 个 role-permission template，且每个租户有 57 个
  permissions；canonical Admin/SecurityAdmin 获得新权限。新租户使用 revision 6 catalog/hash，但所有新建角色
  的 `permission_revision` 仍按既有合同初始化为 1。
- Developer/Viewer/FinanceAdmin/OpsAdmin 默认不得访问 PAGE-096 或分类 API。
- 新租户 seed 必须直接产生 revision 6 权限目录和角色上限。

### 3.6 迁移与回滚

- up 前证明数据库精确为 0060 栈顶、RBAC revision 5、RequestLog 精确列形状和目标对象空闲。
- SQLite up 沿用 0060 已验证的 `ALTER TABLE ... ADD COLUMN ... CHECK` 逐项追加安全字段，并用新增
  `BEFORE INSERT/UPDATE` trigger 校验跨列状态；不重建当前已有 52 列、24 个显式索引和 12 个 trigger 的
  RequestLog。down 删除新增 trigger 后，才按 0060 down 的精确换表模式去除新增列并恢复 0060 形状。
- up 后验证租户数对应权限数、角色 hash、两张新表形状、RequestLog 新列、事件 owner、外键和 integrity。
- down 只允许数据库精确位于 0061 栈顶，并在首项 DDL 前一次性证明：两张业务表为空、RequestLog 全部为
  disabled；没有 endpoint 订阅 `security.intent_detected`，也没有对应 delivery/attempt/dead letter；
  `role_permission_events` 的 added/removed JSON 不含 `intent_risk.classify`；没有 canonical 合同外角色持有新权限；
  后续 schema 不引用新表/新列，且 foreign key/integrity 检查通过。任何脏证据都拒绝回退，不静默删除。
- down 把 catalog 恢复 revision 5，只回退 migration 自己扩展且仍可证明 canonical 的角色；自定义矩阵只有在
  从未持有新权限时才能保持用户状态，否则拒绝 down。canonical Admin/SecurityAdmin 的 revision 使用
  `CASE WHEN permission_revision > 1 THEN permission_revision - 1 ELSE 1 END`，不得把新租户 revision 1 减成 0。

## 4. API 设计

### 4.1 `POST /security/classify-intent`

请求头：

- `Authorization: Bearer <session-token>`
- `Content-Type: application/json`
- `X-Request-ID/X-Trace-ID` 即使存在也不会被此敏感写入口采用或回显；middleware 在任何正文读取、认证结果、
  错误响应和 access log 前生成新的内部 `req_/trc_`。

请求体精确为：

```json
{
  "text": "需要检测的一次性文本"
}
```

约束：

- 只允许一个 `text` 字段；拒绝未知字段、重复 JSON key、客户端 `tenant_id`、result ID、阈值、模式和版本。
- `text` 必须是非空字符串，去除首尾空白后至少 1 个 code point，最多 20,000 code points。
- raw body 上限 128 KiB，覆盖中文 UTF-8 和 JSON escape 膨胀；超限在 JSON 解码前拒绝。
- 接口拒绝全部 query 参数和非空多余路径语义；响应设置 `Cache-Control: no-store`。

成功响应 HTTP 200：

```json
{
  "intent_risk_result": {
    "intent_risk_result_id": "irr_...",
    "status": "detected",
    "classification_mode": "manual_sync",
    "risk_type": "system_prompt_extraction",
    "risk_score": 9700,
    "risk_score_scale": 10000,
    "action": "review",
    "reason_code": "explicit_system_prompt_request",
    "detector_version": "intent-rule-engine-v1",
    "ruleset_version": "intent-rules-2026-08-03-v1",
    "input_code_points": 18,
    "input_truncated": false,
    "duration_ms": 1,
    "completed_at": "2026-08-03T12:00:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

接口不返回输入、规范化文本、命中片段、位置、正则、内部 actor、tenant、Webhook 或数据库错误。

### 4.2 鉴权与解析顺序

固定执行顺序：

1. 共享 middleware 识别精确 `POST /security/classify-intent` 敏感写边界，忽略外部链路 Header 并生成内部
   operation `request_id/trace_id`，但不读取业务正文。
2. 校验 Session，并逐请求从数据库加载当前用户、当前租户和当前权限矩阵。
3. 要求 `intent_risk.classify`；401/403 必须在读取和 JSON 解析正文前返回。
4. 校验 Content-Type 和有界 raw body，再使用拒绝 duplicate key 的严格 JSON decoder。
5. 校验精确字段和文本上限，调用 Shared 分类器。
6. 在单事务写 result、可选 event；提交后构造 `Cache-Control: no-store` 响应和后台 Webhook 投递。

路由签名不得声明 Pydantic body 参数，只接收 `Request + AuthContext dependency`。依赖成功后手工有界读取；
`finally` 必须清空 bytearray、decoded dict、Pydantic/领域 payload 和正文局部引用，不能依赖下一次 GC。

后端始终从 AuthContext 取得 `tenant_id/user_id`，请求体、query、header 均不能选择其他租户。

### 4.3 `GET /request-logs/{request_id}` 扩展

既有 `request_log` 详情新增：

```json
{
  "intent_risk": {
    "intent_risk_result_id": "irr_...",
    "status": "pending",
    "classification_mode": "async_light",
    "risk_type": null,
    "risk_score": null,
    "risk_score_scale": 10000,
    "action": null,
    "reason_code": "queued",
    "detector_version": "intent-rule-engine-v1",
    "ruleset_version": null,
    "input_code_points": null,
    "input_truncated": false,
    "duration_ms": null,
    "completed_at": null
  }
}
```

- 权限仍为 `request_log.read`，tenant 仍从 Session 恢复。
- 顶层 RequestLog owner 命中后，嵌套结果还必须重新验证相同 tenant、source request 和 result ID；不满足时
  返回统一数据完整性错误，不能忽略错配后展示别租户证据。
- 老记录返回 `intent_risk.status=disabled`，不返回 null 让前端猜测。
- pending 详情允许用户显式刷新；本期不为这一个可选事实增加无界全页轮询。

### 4.4 `security.intent_detected` Webhook 合同

payload 精确字段：

```json
{
  "intent_risk_result_id": "irr_...",
  "source_request_id": "req_...",
  "source_kind": "gateway",
  "classification_mode": "async_light",
  "risk_type": "jailbreak",
  "risk_score": 8200,
  "risk_score_scale": 10000,
  "action": "observe",
  "reason_code": "explicit_instruction_override",
  "detector_version": "intent-rule-engine-v1",
  "ruleset_version": "intent-rules-2026-08-03-v1",
  "input_truncated": false,
  "completed_at": "2026-08-03T12:00:00Z"
}
```

- 共享 Webhook 校验器要求 exact keys、类型、范围、枚举、ID 前缀、时间和 event/result/source 一致。
- envelope 的 `event_id/object_id/tenant_id/trace_id/actor_id` 必须与持久 source event 一致。
- clean、failed、skipped、pending 不发送 `security.intent_detected`。
- payload 和 envelope 都不得包含 Prompt、命中片段、文本 hash、Key、Token、Endpoint 或异常文本。

## 5. 前端页面与交互设计

### 5.1 PAGE-096 安全规则页

- 新增独立导航 ID `intent-security`，归入“安全与治理”，保留既有 `dlp-rules` 及历史链接。
- 导航和页面只在后端 `/console/navigation` 返回该 active item 且当前 authorization owner 具有
  `intent_risk.classify` 时出现；不能按 `security_admin` 角色名硬编码推导。
- 页面由固定 taxonomy 标签和“单次分类”工作台组成。本期没有 GET 配置合同，因此首屏不请求或伪造
  运行时规则/版本状态；实际 detector/ruleset 版本只在一次分类结果中展示。本期没有规则编辑、启停、
  阈值调整或保存草稿按钮。
- 文本框设置有界长度、`autocomplete=off`、`spellcheck=false`，使用现有表单和错误组件；结果区域仅展示
  固定安全字段、request ID 和版本。
- 专用 API client 不发送 `X-Request-ID/X-Trace-ID`，只采用响应中的服务端链路 ID；middleware 仍必须忽略
  恶意非 Console 客户端提供的同名 Header，前端行为不能替代后端安全边界。
- 状态固定为 `idle/submitting/success/error`，并提供取消当前请求和清空安全结果的明确操作。

### 5.2 敏感输入生命周期

- 正文只存在 textarea DOM value 和当前 submit 调用栈，不写入 app state、URL、query、hash、
  localStorage、sessionStorage、console event、analytics、错误对象或测试截图标题。
- 点击提交时先读取局部变量，再立即清空 textarea；成功/失败均不得恢复原文。
- 离页、登出、401、403、用户/租户/Session/权限 owner 漂移、pagehide 和完整重载时，统一 abort 请求、清空
  textarea、结果和局部引用。
- 对迟到响应必须同时校验 generation 和完整 owner；旧用户、旧租户或已降权页面的结果不得重新进入 DOM。
- 错误区域只展示统一错误 code、固定 message 和 request ID，不拼接输入或原始 response body。

### 5.3 PAGE-097 日志详情

- 继续使用 `#request-logs` 和既有详情抽屉，不新增重复页面。
- 在 DLP 决策之后新增“语义风险”区，展示状态、模式、类型、整数分数/10000、动作、原因码、分类器/规则
  版本、结果 ID、覆盖是否截断、耗时和完成时间。
- `pending` 明确表示 best-effort 队列尚无终态；`detected` 表示旁路观察，不显示“已阻断”；只有
  `blocked/sync_strong` 显示 Provider 前阻断。
- `skipped/failed` 显示固定原因和 request ID，不展示 worker 异常；`disabled` 表示历史或非 Chat 请求未启用。
- 提供有界的“刷新详情”命令；不在页面后台无限轮询，也不改变 RequestLog 列表筛选合同。

### 5.4 权限、响应式与可访问性

- Admin/SecurityAdmin 可访问 PAGE-096；其他角色无导航、无工作台 DOM、无 classify 预取。
- Admin、Developer、SecurityAdmin 等实际拥有 `request_log.read` 的角色可在 PAGE-097 查看安全摘要；Viewer
  仍由后端拒绝日志接口。
- 403 时立即清理敏感 DOM 并重取导航；401 走全局登出销毁。
- 桌面 `1280x720` 和移动 `390x844` 均不得横向溢出、按钮遮挡、文本截断或把 textarea 内容带入截图文件名。
- 表单 label、错误区域、提交中状态、焦点恢复和键盘操作沿用现有 Console 可访问性模式。

## 6. 错误码设计

| 错误码 | HTTP | 场景 | 客户端行为 |
| --- | --- | --- | --- |
| `auth.token_missing` | 401 | Session Token 缺失 | 销毁页面和敏感输入，重新登录 |
| `auth.token_invalid` / `auth.session_revoked` | 401 | Token 非法或 Session 已撤销 | 销毁页面和敏感输入，重新登录 |
| `auth.permission_denied` | 403 | 缺少 `intent_risk.classify` | 清页面并刷新导航 |
| `intent_risk.media_type_unsupported` | 415 | Content-Type 非 JSON | 使用 JSON 重试 |
| `intent_risk.payload_too_large` | 413 | raw body 超过 128 KiB | 缩短文本，不回显正文 |
| `intent_risk.payload_invalid` | 400 | 非对象、重复/未知字段、错误类型 | 修正请求结构 |
| `intent_risk.text_invalid` | 422 | 空文本或超过 20,000 code points | 修正输入长度 |
| `intent_risk.classifier_unavailable` | 503 | Control 本地分类器执行异常 | 使用新的内部 operation 有界重试 |
| `intent_risk.result_store_unavailable` | 503 | Control 结果/event 事务不可用 | 检查数据库后重新分类 |
| `intent_risk.result_integrity_failed` | 503 | 租户/来源/result 关联不一致 | 停止展示并联系管理员 |
| `gateway.intent_blocked` | 403 | Gateway 强规则同步命中 | 不调用 Provider，记录 blocked 证据 |
| `gateway.intent_classifier_unavailable` | 503 | 同步强规则执行异常 | 安全失败关闭，不调用 Provider |
| `gateway.intent_result_store_unavailable` | 503 | 同步安全结果无法持久化 | 审计失败关闭，不调用 Provider |

所有错误复用统一错误 envelope，并包含本次 `request_id/trace_id`。日志只记录 code、租户、对象 ID、链路和
异常类型，不记录正文、匹配词或原始异常消息。

## 7. 安全、权限与租户隔离

1. Control 的 tenant、actor 只从 Session 恢复；Gateway 的 tenant、Virtual Key 只从可信
   `GatewayAuthContext` 恢复。任何客户端 `tenant_id` 均不接受。
2. 所有数据库读写必须以 tenant 为第一条件；复合 FK、owner trigger 和嵌套 DTO 校验共同防止换主。
3. `intent_risk.classify` 在正文解析前校验。PAGE-096 无 DOM 不是授权；后端每次请求仍读数据库当前矩阵，
   角色降权无需重新登录即可生效。
4. 强规则同步结果持久化失败时安全失败关闭；异步队列失败只影响观察能力并 fail open。queue 未接收时
   RequestLog 留下 skipped，worker 分类异常且失败终态落库时显示 failed，SQLite 写失败或关停未完成时只显示
   pending/best_effort，不虚构已持久化证据。
5. 分类器无网络依赖、无 Provider Credential、无环境代理访问；不得把文本传给 Webhook、logger、metrics
   label 或追踪 span attribute。
6. Queue job 使用 frozen dataclass，正文 `repr=False`；异常处理只输出异常类名。shutdown 超时立即清空
   pending queue 并删除事件循环侧局部引用；CPython 无法强杀已经进入 `to_thread` 的函数，因此当前 job
   只保留到该有界函数返回，返回后释放且不能向已经停止的 Webhook dispatcher 补投事件。
7. 数据库只保存安全元数据和输入长度，不保存正文、匹配片段、位置、裸 hash 或自由异常。
8. 前端使用 `textContent`/安全 DOM API，风险字段按固定枚举渲染；服务端未知枚举不能作为 HTML 注入。
9. Webhook exact-key validator 在共享层执行，未来新增字段必须显式升级合同，不能通过 `**payload` 穿透。
10. 日志和测试证据执行敏感词扫描，但扫描器只报告文件/字段位置，测试样例使用合成攻击语句和假 Key，
    不使用真实 Provider Key 或企业 Prompt。

## 8. 性能与故障边界

- 强规则扫描限制为合计 20,000 code points；不另设与 DLP 漂移的段数限制。算法必须为有界线性扫描，
  不使用灾难性回溯正则。
- 聚焦基准在指定本地 Python runtime 上验证 20,000 字符中文/英文/分隔符攻击输入；目标 p95 不超过
  10 ms、p99 不超过 25 ms。阈值作为回归门禁记录机器和样本数，不用一次偶发冷启动判失败。
- 异步队列容量固定，默认建议 128；不允许随 tenant 或请求内容动态扩容。队列长度不得携带 tenant 作为
  高基数 metrics label。
- Worker 串行或固定小并发，避免 CPU 分类抢占 ASGI；本期优先单 worker，分类和 SQLite 写在工作线程执行。
- 流式验收注入至少 500 ms 的 async classifier 延迟，首 SSE chunk 必须在 worker 完成前到达；非流式响应
  同样不等待 worker。
- queue full、worker unavailable、worker exception、Webhook failure 和 shutdown timeout 都有固定结果语义，
  不能反压主请求或把成功请求改写为失败。
- DLP、强规则、Rate Limit、Budget 的优先级由调用顺序和 Provider/Usage 零调用断言证明，不能只靠注释。

## 9. 测试用例

### 9.1 Shared 分类器

- 中文/英文典型越狱、索要密钥、系统提示词提取、策略绕过分别得到稳定 risk_type、整数分数和版本。
- NFKC、大小写、空白和有限分隔符变体结果稳定；多类别按固定 score/precedence 决胜。
- 合规安全培训、检测规则说明、普通“系统提示”、代码中的 `API_KEY` 变量名等负例不被强规则阻断。
- `none=0`、所有分数有界、输出不含正文/片段/位置/hash；输入对象和返回对象不可变。
- 20,000 字符、65 个以上短 user message、超限和恶意回溯样例性能有界；Gateway 超限冻结 truncated，
  Control 超限拒绝。

### 9.2 迁移与数据完整性

- 0060 -> 0061 正迁移、0061 -> 0060 反迁移、再次正迁移，schema、权限数、role hash、trigger 和索引一致。
- 验证 revision 6 精确计数 57/151/每租户 57；旧租户 canonical Admin/SecurityAdmin 补权，自定义
  SecurityAdmin 不补权，新租户角色 revision 仍为 1 且使用 revision 6 hash，Developer/Viewer 无权限。
- 两张表跨租户 FK、伪造 actor、错误状态组合、重复来源、UPDATE/DELETE、事件错配全部被数据库拒绝。
- Gateway result 可在 RequestLog 最终 upsert 前按 request claim 落库；伪造 claim、trace、virtual key 与 manual
  user owner 全部被数据库拒绝。
- RequestLog 旧数据原地保留且为 disabled；主 upsert 与 spool 恢复均验证 intent 冻结/冲突语义；up/down 的
  栈顶、未知对象、脏证据和 integrity guard 失败关闭。
- down 分别注入 Webhook 订阅/投递历史、RBAC 事件、自定义角色持权、后续 schema 引用和 FK 异常，均必须在
  首项 DDL 前拒绝；canonical revision 1 回退后仍为 1。

### 9.3 Control API 主流程

- Admin/SecurityAdmin 分类 clean 与 detected，返回精确字段、request_id/trace_id、整数分数和持久结果。
- detected 同事务创建唯一 `security.intent_detected` source event，并由 Webhook exact payload 投递。
- 外部 request/trace Header 即使恰好等于攻击文本也不会进入响应、日志、result/event 或 delivery；每次调用
  使用新的内部 operation identity。
- 两个 tenant 对相同合成文本的检测结果、事件、Webhook 订阅和 actor 始终独立，响应不暴露 tenant 字段。

### 9.4 Control 异常、权限与不泄密

- 未登录 401、Developer/Viewer/Finance/Ops 403；用 spy 证明权限拒绝发生在 `request.body/json` 之前。
- 非 JSON、超 raw body、重复 key、未知字段、客户端 tenant、空文本、错误类型和 20,001 字符使用对应统一错误。
- 分类器异常与 result/event 事务异常分别返回两个 503；分类失败终态只在数据库可用时存在。Webhook 故障发生
  在提交后且不回滚结果，所有响应和日志均不含正文或原始异常。
- 跨租户结果/event 查询与嵌套 owner 错配失败关闭。

### 9.5 Gateway 主流程与优先级

- DLP block 保持 `gateway.dlp_blocked`，语义分类调用数为 0。
- 强规则命中返回 `gateway.intent_blocked`，Provider、Rate/Budget、Attempt、Usage、Cost、Fallback 全为 0，
  RequestLog 和结果为 blocked。
- 强分类器异常与 blocked result/event 存储异常分别返回 classifier/result-store 503，均为
  `failed_closed` 且后续阶段为 0；result-store 失败不得返回 `gateway.intent_blocked`。
- 无强命中请求只 `put_nowait` 后继续 Provider；异步 clean/detected 最终可从日志详情读取。
- async detected 不改变 HTTP/SSE 成功状态、不改写 RequestLog status、不宣称 blocked，只创建一次事件。
- 每个 Gateway request 只分类一次；Fallback、Chat Demo、Prompt Template 共用同一链路且不重复。

### 9.6 Gateway 性能和故障

- delayed classifier 下流式首块和非流式响应均先于 worker 完成。
- queue full/worker unavailable 继续 Provider，并在日志详情显示 skipped；worker 分类异常且失败结果成功落库
  时显示 failed。
- 强分类器异常和同步 result-store 异常以不同错误 fail closed；异步 SQLite 持久化失败不破坏模型响应且保持
  pending/best_effort，Webhook 故障不回滚结果；日志只含固定码和异常类型。
- shutdown 有界排空和超时取消，不把 job 正文输出到 task repr、日志或异常。
- 生命周期顺序测试证明 shutdown 先排空 intent job、再排空它产生的 Webhook event；超时后 pending job
  立即释放，已进入 `to_thread` 的 current job 在底层函数返回后释放。被取消的 worker 不会复活，event
  dispatcher 不接收“停止后事件”。

### 9.7 RequestLog、Webhook 与 Console

- PAGE-097 验证 disabled/pending/clean/detected/blocked/failed/skipped 全状态和三项 owner 关联。
- Webhook 允许 `security.intent_detected`，拒绝额外/缺失/错类型/错范围/错 owner 字段和所有敏感字段。
- PAGE-096 正向分类、提交即清空、取消、统一错误、401/403、owner 漂移、迟到响应、XSS 和零 Storage。
- 无权限角色无导航、无工作台 DOM、无 classify 预取；日志权限继续由 `request_log.read` 决定。
- `1280x720`、`390x844` 验证页面、详情、焦点、滚动、错误和注销清理；URL/Storage/console/DOM 扫描无正文。

### 9.8 聚焦回归与最终门禁

- Python 聚焦：Shared intent、migration、RBAC、Control intent、Gateway intent、DLP、RequestLog、Webhook、
  Usage spool v1/v2/v3、Request Explanation v1 不变、stream、policy mask、rate/budget order。
- Console 聚焦：intentSecurity、requestLogs、permissions、consoleShell、routing/navigation、lint。
- 最终运行 `scripts/ci/check-comments.py`；新增模块、类、数据结构、常量和函数必须具备解释业务约束的中文注释。
- 专项全部通过后才运行一次必要的根级回归，不因局部修改重复从头执行无关全量测试。
- 实际运行数据库完成 61 条迁移，三服务健康 200，真实 API/Webhook/双视口验收通过后才允许标记完成。

## 10. 代码修改计划

### 10.1 Shared 与数据库

- 新增 `metahub_shared.intent_security` 纯分类器和模型；扩展统一错误目录、RBAC catalog 和 Webhook 合同。
- 新增 0061 up/down 迁移：结果、事件、RequestLog 安全字段、RBAC revision 6、owner/immutable trigger 和索引。
- 扩展 migration、RBAC、Webhook 和 Shared 分类器测试。

### 10.2 Control API

- 新增 `metahub_control.intent_security` 的 models/routes/service；路由不声明 body 参数，采用权限先行、手工
  严格正文解析、finally 清理和单事务结果/event。
- 在 app router 注册 `POST /security/classify-intent`；在 Console navigation 注册 permission-bound 页面。
- 扩展 RequestLog DTO/service 的 tenant-qualified intent 详情和既有 `dlp` 广义终止阶段映射；Request
  Explanation v1 不变。
- 新增 Control API、权限、租户、事件和不泄密测试。

### 10.3 Gateway

- 将 DLP user text extractor 改为公开复用函数，保持既有 DLP 行为和测试不变。
- 新增 `metahub_gateway.intent_security` 的 job、service、dispatcher 和生命周期安装。
- 在 `openai_compatible.py` 的 DLP 后、Rate Limit 前接入一次；扩展 `RequestLogContext`、Usage spool v3 和
  安全落库。
- 新增同步阻断、异步旁路、TTFT、队列故障、优先级、零 Provider/Usage 和不泄密测试。

### 10.4 Console 与文档

- 新增 `intentSecurity` API client、renderer、owner-bound lifecycle、导航绑定和定向单测。
- 扩展 RequestLog 详情渲染和显式刷新；新增权限/i18n 文案，不改变旧路由。
- 更新 README、directory-map、API 文档和 `docs/testing/PRD-069-tests.md`。
- 最终执行迁移、真实三服务、API/Webhook、桌面/移动 Browser、数据库完整性和敏感日志复核。

## 11. 编码前自审记录

本节必须在实现前完成两轮自审。目前状态：**两轮均已完成，全部阻断项已修正**。

### 第一轮：边界、权限、租户与兼容性

结论：**通过，2 个 blocker、7 个 major/minor 均已修正，进入第二轮**。

1. **链路 Header 可能变成正文落库旁路**：任意合法 ASCII 攻击文本可被放入外部 `X-Request-ID`，原设计又
   会把它写入 result、event、响应和日志。已把精确 POST 路径加入共享敏感写边界，middleware 在读取正文前
   忽略外部 request/trace，删除客户端幂等和 409 合同，只使用服务端随机 ID。
2. **不能静默改变 PRD-054 精确合同**：已取消 `intent_security` terminal stage 和 Explain v2 计划；
   `terminal_stage_v1`/Request Explanation v1 的筛选枚举、10 阶段和 11 区块完全不改。语义错误映射到既有
   `dlp` 广义安全阶段，精确事实只进入新的 RequestLogDetail `intent_risk`。
3. **权限不能复用 `dlp.write`**：显式分类会持久化结果和事件，但不管理 DLP 规则。已改为独立
   `intent_risk.classify`，默认只授予 Admin/SecurityAdmin，并明确 custom matrix 不自动补权。
4. **DLP 优先级和抽取兼容必须保持**：DLP block 时分类调用数为 0；分类只位于 DLP 后、Rate Limit 前。
   删除与 DLP 不一致的 64 段上限，65 个以上短消息仍按 20,000 总字符处理；`input_truncated` 贯穿所有事实。
5. **一次请求只能有一个结果**：唯一键删除 `classification_mode`，固定为
   `(tenant_id, source_kind, source_request_id)`；event 增加 tenant-qualified result 唯一键。
6. **多态 owner 必须由数据库证明**：冻结 `gateway -> virtual_key -> sync_strong/async_light`、
   `manual -> user -> manual_sync`，逐字段绑定 claim/user、trace、actor、result 和 event envelope。
7. **Usage crash spool 不能丢新证据**：新增 payload v3，writer 只写 v3，reader 分别使用 v1/v2/v3 exact-set；
   历史缺失映射 disabled，未知版本失败关闭，spool 不含正文或匹配内容。
8. **分类与存储失败不能共用错误码**：拆分 classifier 和 result-store 503；blocked 只有 result/event 事务提交后
   才成立，存储失败优先返回 result-store 错误。`IntentBlockedError` 只携带安全 decision。
9. **HTTP、清理和迁移边界不足**：路由不声明 Pydantic body，权限后手工读取并 finally 清理正文引用；POST
   拒绝 query、响应 no-store。up 沿用 ADD COLUMN + BEFORE trigger，down 完整保留旧封印；RBAC/down 跨模块
   门禁和 canonical revision/hash/关系集均已精确冻结。

### 第二轮：异步语义、性能、故障与可验收性

结论：**通过，修正 8 项后允许进入实现**。

1. **状态矩阵与运行路径存在矛盾**：`sync_strong` 原文字段一度禁止 failed，但同步异常又要求失败结果。
   已冻结为 strong 只产生 blocked/failed，async 和 manual 只产生 clean/detected/failed；failed action 固定 none。
2. **RequestLog 缺少失败关闭调度态**：新增 `failed_closed`，与 skipped/pending 分离；classifier/result-store 原因
   分列。详情无结果时也显示 failed，不能把安全失败误报为 clean 或普通旁路丢弃。
3. **blocked 必须晚于审计提交**：`gateway.intent_blocked` 仅在 result/event 单事务 commit 后返回；存储失败返回
   独立 result-store 503。异常只携带固定 decision，外层再写 RequestLog，不携带正文。
4. **Control 同步语义不能阻塞 ASGI**：人工调用仍等待本次结果，但分类和 SQLite 事务通过
   `asyncio.to_thread` 执行；权限依赖先行，路由 finally 显式释放 raw/decoded/text 引用。
5. **worker 与 Webhook shutdown 顺序必须可证明**：Intent dispatcher 安装在 event dispatcher 之外、其他
   scheduler 之内；退出先排空分类 job，再排空新事件。超时取消立即清 pending queue；已运行线程不能被
   Python 强杀，只在函数返回后释放 current job，且迟到返回不得重新投递事件。
6. **spool 版本不能宽松兼容**：writer 只写 v3，reader 为 v1/v2/v3 使用三套 exact field set；历史缺失映射
   disabled，未知版本拒绝，恢复不会覆盖已冻结 intent identity。
7. **外部链路 ID 与前端生命周期仍可能泄密**：专用 Console client 不发送 request/trace Header，middleware
   对任意客户端仍强制内部 ID；textarea 提交即清、owner 漂移/401/403/离页统一 abort 和销毁。
8. **验收必须证明“没有等待”而非只测成功**：加入 delayed classifier 的 SSE/非流式先返回、20k 性能、65+
   消息、queue full、worker/SQLite/Webhook/shutdown、DLP 优先、Provider/Usage 零调用、spool 三版本和
   Request Explanation v1 exact-set 回归。

## 12. 实现与验收闭环（已完成）

### 12.1 实现一致性

- 最终实现保持设计冻结顺序：`Policy -> mask -> Routing -> DLP -> Intent -> Rate Limit -> Budget -> Provider`。
  DLP 阻断不调用分类器；强语义规则只在 result/event 同事务提交后阻断；异步轻分类不等待完整分类结果，
  不改变已经成功返回的 HTTP/SSE 结果。
- Control 的 `POST /security/classify-intent` 先执行 Session/RBAC，再读取严格 JSON 正文；tenant、actor、
  request/trace ID 均由服务端可信上下文恢复，响应固定 `Cache-Control: no-store`。
- 0061 已把结果、source event、RequestLog 九字段、RBAC revision 6、tenant-qualified owner 和不可变约束
  落入开发库；Usage crash spool writer 只写 v3，reader 对 v1/v2/v3 分别执行 exact-set 校验。
- PAGE-096 只保留提交后的安全分类摘要，textarea 提交即清空；PAGE-097 使用独立七状态 DTO，不复用
  PAGE-096 的 `manual_sync/review` 响应投影，因此可如实展示 `async_light/observe` 历史事实。

### 12.2 已完成真实验收

- Control 真实验证 clean、detected、匿名 401、Viewer 403、统一错误、request/trace ID、`no-store`，并确认
  401/403 不创建 result/event；detected 只提交 `security.intent_detected` source event，不虚构外部
  Webhook delivery 成功。
- Gateway 真实验证 clean 与 detected 继续 Provider、强规则在 Provider 前返回
  `gateway.intent_blocked`；blocked 请求的 RoutingAttempt、Usage 和 Provider 调用均为 0，运行配置已恢复。
- PAGE-096 已在 `1280x720` 与 `390x844` 验证分类、立即清空正文、清空结果、Viewer 零导航/零表单和注销
  清理。PAGE-097 已真实读取 clean、`detected/7400/observe`、`blocked/9900/block` 三条结果；两种视口均无
  页面级横向溢出，页面 Console error 为 0。
- 开发库当前为 `61/0061`，`integrity_check=ok`、外键违规 0；运行表结构没有 Prompt/Response/正文列，
  `.logs` 对本次敏感短语和本地管理员密码扫描命中 0。Gateway、Control API、Console 健康检查均为 200。

### 12.3 最终回归与性能闭环

- 唯一一次根级 Python 全量为 `2146 passed / 4 failed`，耗时 `14514.48s`。四个失败均为历史测试仍写死
  `0060/60` 或未排除 revision 6 权限；只修正对应测试合同后，原四个精确 node ID 定向 `4/4 passed`，
  未启动第二次全量。
- 新增性能、Webhook queue unavailable/full 和 shutdown timeout 四个聚焦节点均通过。性能节点对中文、
  英文和分隔符三类 20,000 code points 输入分别预热 10 次、采样 100 次；最终所有 strong/light P95
  均小于 10ms、P99 均小于 25ms。
- 性能闭环保留 NFKC 和分隔符兼容语义，只为已经满足 NFKC、纯字母数字的正文增加标准库快路径；修改后
  Shared 分类合同 `21 passed`，相关 Shared + 性能组合 `22 passed`，Ruff、compileall 和中文注释门禁通过。
- 最终 Gateway、Control API、Console 为 HTTP `200/200/200`；开发库为 `61/0061`、
  `integrity_check=ok`、外键违规 0；39 个运行日志文件的验收敏感短语和管理员密码命中 0。Browser 已重新
  显示非空的“MetaHub 控制台登录”页面。详细命令、分位数和 request ID 见测试记录。
