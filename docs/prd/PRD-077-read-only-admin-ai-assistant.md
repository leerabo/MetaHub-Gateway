# PRD-077 - 只读管理员 AI 助手

状态：**已完成**（2026-08-12）。两轮编码前设计自审、实现期复审、编码、迁移、专项与必要回归、
Runtime、PAGE-107 Browser、文档和提交前安全门禁均已完成。

总体目标仍在进行中：`all_108_prds_complete=false`。本条只处理 PRD-077，不启动或混入 PRD-078。

## 1. 需求来源与顺序确认

### 1.1 Excel 权威行

以下条目已直接从仓库内《企业级AI_Gateway_PRD开发总清单_给CodexClaude.xlsx》读取，不按编号猜测：

- `01_PRD总表` 第 78 行、`03_M2-M3扩展交付` 第 14 行：
  `PRD-077 | M2-企业增强 | P2 | 扩展层 | Admin AI助手 | 只读管理员AI助手`。
- 角色：管理员、安全、财务。
- 用户故事：管理员希望用自然语言询问成本、错误、策略冲突。
- 范围：调用统计、错误解释、成本建议、策略解释；所有查询审计；严格租户隔离。
- 禁止项：不得通过助手直接修改配置、禁用 Key、变更预算或审批高风险操作。
- 页面：`PAGE-107 | 控制台 AI 助手`。
- API：`POST /admin-assistant/query`。
- 数据对象：`AssistantQuery`、`AssistantAudit`。
- Webhook：`assistant.queried`。
- 验收：回答必须带数据来源链接；敏感数据按权限脱敏；所有查询可审计。
- 风险：越权、幻觉。
- 用例：TC-0153 主流程、TC-0154 边界与异常。

### 1.2 架构与交接交叉确认

- 架构文档 10.5 明确：管理员助手默认只读，不允许直接修改策略、禁用 Key、变更预算；
  查询必须走现有租户权限体系，所有查询写审计日志。
- Admin Assistant 属于低频 Control Plane 能力，不能进入 Gateway 高频热路径。
- README、`docs/prd/PRD-076-response-cache-optional-capability.md`、
  `docs/testing/PRD-076-tests.md` 和目录说明共同确认 PRD-076 已完成，下一条是 PRD-077。
- PRD-076 的 HEAD/`origin/main` 基线为 `4481d0e326930e6daa1875cf252a8836537532fc`，
  PRD-077 不能重复实现 PRD-073 至 PRD-076，也不能改写既有事实。

## 2. 需求理解与冻结边界

### 2.1 本期必须交付

1. 一个单轮、只读、自然语言查询入口，支持四种有界查询：
   `usage_statistics`、`error_explanation`、`cost_advice`、`policy_explanation`。
2. 每个公开事实 claim 都由当前事务内、当前 tenant 的数据库事实生成，并至少引用一个来源。
3. 每个来源由后端枚举和 allowlist 构造，链接只指向控制台内的可信页面；客户端和问题文本
   均不能提供 URL。
4. 后端先验证 `admin_assistant.query`，再按 query type 验证原始数据域权限，不能用助手权限
   绕过 RequestLog、FinOps、Usage、Policy 或 RequestExplanation 的权限边界。
5. 每个已认证调用都写 `AssistantQuery`、`AssistantAudit` 和通用不可变 AuditLog；成功、拒绝、
   只读阻断、源权限撤销和可持久化的内部失败都可追踪。
6. `assistant.queried` 使用稳定事件 ID 和可恢复 outbox；Webhook 载荷只含固定枚举与整数计数。
7. PAGE-107 提供问题输入、四个可执行示例、回答 claim、来源链接、脱敏说明和链路 ID；问题及回答
   只在当前页面内存中存在，切页、撤权、换租户和登出立即清理。
8. 迁移编号为 `0070`，RBAC catalog revision 从 12 提升到 13，并提供正向与反向迁移。
9. 本期执行模式固定为本地确定性只读引擎；每次 Query/Audit 明确记录模型调用数、Token 与模型成本均为
   0，不伪造 Usage/FinOps 分录，也不能把“未调用模型”误记成未知成本。

### 2.2 明确非范围

- 不提供创建、修改、删除、启停、发布、回滚、审批、禁用 Key、调整预算或重放请求等动作。
- 不提供工具调用、函数调用、SQL 生成/执行、浏览器自动操作或任意 URL 抓取。
- 不保存会话历史，不提供多轮记忆，不把上一轮回答拼回下一轮问题。
- 不把问题原文、问题裸 hash、回答正文、Prompt、Response、Provider Key、Virtual Key、Token、
  原始错误体或策略表达式写入数据库、日志、Webhook、AuditLog 或验收证据。
- 不新增默认 Provider，不把 OpenRouter、硅基流动改成默认上游；它们仍只作为竞品与迁移兼容对象。
- PRD-077 v1 不调用外部大模型。自然语言只用于有界意图和时间范围识别；事实选择、聚合、文案和
  建议全部由服务端确定性规则生成。这样不是降级为无来源聊天，而是从架构上消除模型生成事实、
  生成 URL、生成动作或泄漏上下文的通道。后续若引入模型润色，只能在不改变 claim/source 图的
  条件下另立 PRD，并通过现有 Gateway 记录用量、成本与失败。

### 2.3 输入与时间边界

- 请求 exact schema 只允许 `question` 一个字段，类型必须是字符串。
- JSON wire 最大 16 KiB；问题最大 2,000 Unicode code point、最大 8 KiB UTF-8。
- 拒绝 NUL、未配对 surrogate、非字符、Bidi 控制符和除换行/制表外的控制字符；内部先 NFKC，
  但不持久化规范化前后文本。
- 支持滚动窗口：最近 24 小时、7 天、30 天；默认 7 天，最大 30 天。时间以 UTC 持久化和查询，
  PAGE-107 同时展示绝对 UTC 起止时间。v1 不接受“今天/昨天/本周”等依赖用户时区的日历词，统一返回
  `admin_assistant.query_ambiguous`，避免多时区下产生不可复核语义。
- 可从问题中识别一个规范 `request_id`；同时出现多个 request_id、多个 query type 或互相冲突的
  时间范围时返回歧义错误，不替用户猜测。

## 3. 核心设计决策

### 3.1 确定性只读查询引擎

处理顺序固定如下：

1. 专用认证依赖先从 raw ASGI headers 要求唯一 canonical Authorization，再恢复可信
   Session/tenant/actor，不读取正文。重复或畸形 Authorization 无法建立可信 actor，因此只返回固定 wire
   错误，不写 tenant 业务表。
2. endpoint 使用仅认证的依赖并手工检查 base permission；不能使用“认证 + base RBAC”一体依赖让 403
   绕过领域审计。缺权时不读取问题正文，以 `query_type=unknown` 原子记录安全拒绝事实。
3. 严格读取有界 JSON，禁止重复键、未知键、非 JSON、截断流和声明/实际长度不一致。
4. 在内存中规范化问题，先执行禁止动作闸门，再识别 query type、窗口和可选 request_id。
5. 在 event loop 专属 read-stage 长期连接上开启 `query_only=ON` 只读事务；入口认证已经完整校验目录、
   矩阵和授权，本事务再重读 Session/actor/role revision 与 matrix hash，并逐项查询该 query type 的
   必需 grant，冻结本次来源授权指纹。
6. 只使用参数绑定、tenant-first SQL 和有界时间窗口读取一个一致快照；任何固定查询之外的 SQL、
   用户生成列名/排序/表达式均不存在。
7. 把数据库行先映射为 typed facts，再由固定模板生成 claim、summary、recommendation 和 source。
8. 验证每个 claim/recommendation 的全部 source ID 都存在，source href 属于 allowlist，且没有
   任意未被 claim 引用的数值/资源陈述。
9. 回滚只读事务；在独立 write-stage 长期连接上开启短 `BEGIN IMMEDIATE`，再次读取 Session/actor/role
   revision 与 matrix hash、逐项查询本次必需 grant，并要求最终 authorization fingerprint 与来源快照一致。
   若此时撤权，丢弃内存回答，在独立短事务只写 `permission_denied` 的零 claim/source 拒绝审计。
10. 权限仍有效时，原子更新可信 Session `last_seen`，并写 Query、AssistantAudit、AuditLog 和 pending event
    source 后提交；该写事务持有 SQLite writer reservation，使并发矩阵更新只能发生在提交前或提交后，
    不能夹在最终检查与治理事实之间。事务失败时 `last_seen` 和四项事实一起回滚。
11. 只有审计事务成功才返回回答；审计不可用时关闭失败，不能返回未追踪的敏感聚合。

问题文本不进入模型、SQL、文件、日志或异常。未知指令只可能得到固定错误，不可能改变 query plan。

### 3.2 只读动作闸门

下列语义在意图分类前阻断：创建、修改、更新、删除、禁用、启用、发布、回滚、审批、批准、
调整预算、提高额度、降低额度、重放、执行、调用工具，以及对应英文命令。命中后：

- 不读取任何业务来源；
- 写 outcome=`read_only_blocked` 的 Query/Audit；
- 返回 `admin_assistant.read_only_violation`；
- Webhook 只披露 outcome 和零 source/claim 计数，不披露命中的词或问题片段。

禁止动作表是保守 denylist，允许查询动词表是严格 allowlist。无法唯一归类为四种读取意图的输入必须
返回 unsupported/ambiguous，不能退化成通用聊天。

### 3.3 事实 claim 与来源图

公开回答不是一段不可验证自由文本，而是以下图：

- `answer.summary`：只由 claim 模板拼接，模板只能引用已有 claim ID。
- `answer.claims[]`：稳定 `claim_id`、`claim_type`、`label`、typed `value`、`unit`、`source_ids[]`。
- `answer.recommendations[]`：稳定规则 code、固定文案、`source_ids[]`；没有足够事实时只返回
  `insufficient_evidence`，不能补全建议。
- `sources[]`：稳定 source ID、source type、控制台链接、UTC 时间范围、记录数和快照时间。
- `redactions[]`：固定 code，说明正文、凭据、原始错误和未授权域已经省略；不包含被省略值。

所有链接从下列 typed 映射构造：

| source type | href | 对应权限 |
| --- | --- | --- |
| `overview` | `/#overview` | `dashboard.read` |
| `request_logs` | `/#request-logs` | `request_log.read` |
| `request_explanations` | `/#request-explanations` | `request_explanation.read` |
| `usage` | `/#usage` | `billing.usage.read` |
| `finops_ledger` | `/#finops-ledger` | `billing.finops.read` |
| `budgets` | `/#budgets` | `billing.budget.read` |
| `cost_anomalies` | `/#cost-anomalies` | `billing.anomaly.read` |
| `model_prices` | `/#model-prices` | `model_price.read` |
| `policies` | `/#policies` | `policy.read` |
| `policy_snapshots` | `/#policy-snapshots` | `policy.read` |

后端 validator 要求 href 与 source type 完全一致、必须以 `/#` 开头、不得出现协议、主机、反斜杠、
控制字符、二次编码或 query/fragment 注入。前端还要按同一集合复核，未知 source 不渲染为链接。

### 3.4 四类查询口径

#### `usage_statistics`

- 必须权限：`admin_assistant.query` + `dashboard.read` + `billing.usage.read`。
- 来源：`overview` 只提供窗口内请求数、成功/失败与延迟的不可识别聚合；`usage` 提供
  `usage_ledger` 的 Token 与 Usage 成本。助手不得借 `dashboard.read` 返回 request_id、模型、Provider、
  Virtual Key 或错误详情，也不得把聚合来源伪装成 `request_logs` 明细权限。
- claim：请求数、成功数、失败数、成功率、输入/输出/总 Token、CNY 成本、估算记录数。
- 不返回 Virtual Key 明文、Provider credential、Prompt/Response、原始 usage JSON。

#### `error_explanation`

- 必须权限：`admin_assistant.query` + `request_log.read`。
- 有 request_id：只读取当前 tenant 的 RequestLog、ProviderErrorFact 安全投影和冻结错误目录。
- 无 request_id：在窗口内返回固定上限 5 个错误码及计数。
- 每个错误分类同时返回 shared error diagnosis 固定的安全原因和处理建议；诊断器不接收 Provider 原始
  message，不显示 raw body，也不把未知错误值拼进自由文案。
- 外租户或不存在 request_id 统一返回 `admin_assistant.source_not_found`，不能枚举归属。

#### `cost_advice`

- 必须权限：`admin_assistant.query` + `dashboard.read` + `billing.usage.read` + `billing.finops.read` +
  `billing.budget.read` + `billing.anomaly.read` + `model_price.read`。
- 来源：`overview` 的不可识别失败率聚合、`usage`、`finops_ledger`、`budgets`、`cost_anomalies` 和
  `model_prices` 的同租户安全投影。缺少任一必需权限时整次失败，不能悄悄删除成本风险域后给出片面建议。
- claim：posted CNY debit、请求数、Usage 记录数、估算占比、失败率、成本最高模型及其占比，以及生效预算、
  待处理异常和模型价格记录数。最高模型只按同 tenant 的 posted debit 聚合，不把 credit 截断成伪造的
  非负“净成本”。
- 建议只来自冻结规则，例如高失败率、高估算占比、高模型成本集中度；每条建议必须引用触发它的
  source。最高模型占 posted debit 达到 60% 才提示集中度，零成本窗口不触发；没有阈值命中时返回
  “当前证据不足以给出优化建议”，不能制造节省金额。

#### `policy_explanation`

- 必须权限：`admin_assistant.query` + `policy.read`。
- 无 request_id：读取 active Policy 数量、动作分布、当前 published snapshot 版本，以及按
  `tenant_id + priority` 聚合的同优先级候选组数量。后者只能标记为“需人工复核的优先级碰撞”，绝不能
  声称已证明 DSL 语义冲突；v1 不读取或比较策略表达式正文。
- 有 request_id：还必须具备 `request_explanation.read`；读取该请求已经冻结的 policy decision、
  reason code、快照状态与版本，并要求同 tenant RequestExplanation 事实存在。
- 不返回策略表达式、变量、Prompt、DLP 命中正文或 target credential。

### 3.5 脱敏与权限降级

- `admin_assistant.query` 只是进入助手的 base permission，不授予任何源数据读取能力。
- 一个 query type 缺少任一必要源权限时，后端返回统一 `auth.permission_denied`，写安全拒绝审计，
  source/claim 数均为 0；不能返回部分统计暗示被保护对象存在。
- 可选来源缺权时不读取、不计数、不返回链接。v1 的必需来源缺权则整次失败，不做不完整回答。
- 所有成功回答固定声明 `content_omitted`、`credentials_omitted`；错误回答再声明
  `raw_provider_error_omitted`。这些是固定 code，不回显内容。
- Source 页面本身继续执行自己的后端 RBAC；PAGE-107 的链接存在不能补权。

### 3.6 资源与期限

- aggregate 窗口最大 30 天，单次只运行固定 SQL，top-N 上限 5。
- middleware 入口一次冻结来源 1.2 秒、业务决策 1.5 秒和治理持久化 5 秒三条单调绝对期限；认证 admission、
  正文、tenant 排队、来源连接冷启动和来源 SQL 共用来源期限，确定性解析、答案图构造和 exact 校验不得越过
  决策期限，最终授权复核、`last_seen` 与四项治理事实事务只使用治理期限。任何 dependency、slot owner、
  worker 或重试都不得从取得资源的时刻刷新期限。
- 来源或决策预算耗尽后不再返回回答，而是使用尚未耗尽的治理期限写零 claim/source 终态；治理 write slot、
  SQLite 真写锁、progress handler、commit 或 5 秒绝对期限耗尽仍固定返回
  `admin_assistant.audit_unavailable`。已启动的 SQLite 线程或治理事务不能被遗弃：取消/超时会等待真实线程
  退出、事务回滚或提交后再释放共享连接，因此客户端完成时间可以超过 1.5 秒决策期限，但不能超过冻结的
  治理 admission/SQL 边界后再启动新事务。
- 每个 app/event loop 只有两条延迟初始化的长期 SQLite 连接：认证/来源共用一条 read-stage 连接和容量 1
  admission，最终复核/审计共用一条 write-stage 连接和容量 1 admission；二者使用 `journal_mode=WAL`、
  `synchronous=FULL`。`check_same_thread=False` 只允许受 admission 串行保护的 worker 跨线程复用，不允许
  同一连接并发执行。
- 认证、来源和最终写阶段都按剩余预算安装 SQLite progress handler 与 `busy_timeout`，并在 `finally` 清除
  handler、恢复原 timeout、回滚未完成事务和恢复 read connection 的 `query_only=OFF`，防止一次失败污染
  后续请求。
- actor 30 RPM、tenant 120 RPM 的有界进程内 limiter 防止昂贵聚合滥用；key 表只存可信 ID 和
  单调时间；过期空 key 会回收，key registry 达到固定容量后失败关闭。该 limiter 不替代数据库权限或
  tenant 条件。
- 只读事务不持有写锁；最终审计只在短 `BEGIN IMMEDIATE` 中写入，Webhook 网络调用不在事务内。
- 每个 HTTP attempt 独立执行并产生一组 Query/Audit/event；v1 不接受 `Idempotency-Key`，也不保存问题
  fingerprint 或回答，因此不能假装支持结果重放。前端在单个 owner 下禁止重复提交只是体验优化；并发
  20 个合法 attempt 可因有界 admission 得到部分 200 和部分固定 `source_unavailable` 503，但数据库必须
  得到 20 组互不重复且严格一对一的终态事实，不能丢审计或合并不同问题。
- lifespan teardown 先关闭新请求和新治理注册，再等待 read/write slot 释放，最后回滚并关闭两条连接；
  在 `closing` 前已经登记并进入 write-slot FIFO 的治理任务仍可取得长期写连接并完成，不得被 teardown
  反向改写为 `audit_unavailable`。宿主取消、客户端断连或 Browser Abort 都不能跳过已登记 attempt 的治理
  终态。后台通用 audit 投递使用响应 `BackgroundTasks`，durable `assistant.queried` source 仍在最终事务内提交。

## 4. 权限与租户隔离

### 4.1 RBAC revision 13

新增一个 read-risk 权限：

| code | category | 默认角色 |
| --- | --- | --- |
| `admin_assistant.query` | `admin_assistant` | admin、finance_admin、security_admin |

默认不授予 developer、viewer、ops_admin。管理员拥有目录全集。0070 只扩展仍精确等于 revision-12
默认矩阵的 admin、finance_admin、security_admin；除不可变 admin 外，已有定制矩阵不被迁移覆盖，需由
租户管理员显式授予新权限。角色只描述默认上限，运行时每个请求和最终审计事务都从当前 tenant 的
`roles/permissions/role_permissions` 重读，不从 Shared 默认集合回退。

### 4.2 源域权限矩阵

| query type | 必须额外权限 |
| --- | --- |
| `usage_statistics` | `dashboard.read`, `billing.usage.read` |
| `error_explanation` | `request_log.read` |
| `cost_advice` | `dashboard.read`, `billing.usage.read`, `billing.finops.read`, `billing.budget.read`, `billing.anomaly.read`, `model_price.read` |
| `policy_explanation` | `policy.read`; request-specific 再要求 `request_explanation.read` |

因此默认 finance_admin 可查询用量/成本，security_admin 可查询错误/策略，admin 可查询全部；角色名本身
不参与判定。租户自定义矩阵撤销任一权限后立即失败关闭。

### 4.3 Tenant owner 图

- tenant_id 只来自 AuthContext，不接受 path/query/body/header 中的 tenant 值。
- 所有 source SQL 的首个谓词是 `tenant_id=?`，所有外键优先使用 `(tenant_id, object_id)`。
- `AssistantQuery -> AssistantAudit -> AuditLog/outbox` 全链路 tenant 必须一致。
- target request_id 只能在当前 tenant 下解析；外租户与不存在统一 404。
- 数据库 trigger 复核 actor user/session owner、Query/Audit owner、AuditLog owner 和事件 source owner。

## 5. 数据库设计（0070）

### 5.1 `assistant_queries`

不可变的一次调用事实，不保存问题或回答：

| 字段 | 约束/含义 |
| --- | --- |
| `assistant_query_id` | `asq_` + 32 hex，主键 |
| `tenant_id` | tenant owner |
| `actor_user_id` / `actor_session_id` | 已认证调用者 |
| `query_type` | 四种类型或 `unknown` |
| `target_request_id` | 可选，仅 request-specific 查询 |
| `window_started_at` / `window_ended_at` | 可选 UTC，最大 30 天 |
| `outcome` | `answered/read_only_blocked/unsupported/ambiguous/permission_denied/rate_limited/failed` |
| `failure_code` | 失败时固定 allowlist code，成功为空 |
| `claim_count/source_count/redaction_count` | 非负整数，失败时 claim/source 必须为 0 |
| `query_duration_ms` | 端到端服务时间，不含前端 |
| `operation_request_id` / `operation_trace_id` | 统一链路标识 |
| `execution_mode` | 固定 `deterministic_read_model_v1` |
| `model_call_count` / `model_input_tokens` / `model_output_tokens` | 全部固定为整数 0 |
| `model_cost_cny_micros` / `model_cost_status` | 固定为 `0` / `not_applicable_local`；不写伪 Usage/FinOps |
| `created_at` | UTC，不可更新 |

索引：tenant/time、tenant/actor/time、tenant/target_request_id。禁止 UPDATE/DELETE；冲突 ID 和负 rowid
由 trigger 阻断。

### 5.2 `assistant_audits`

AssistantAudit 是 Query 的一对一不可变安全审计，同时承载 `assistant.queried` 恢复状态：

| 字段 | 约束/含义 |
| --- | --- |
| `assistant_audit_id` | `asa_` + 32 hex |
| `assistant_event_id` | `asevt_` + 32 hex，稳定 Webhook event ID |
| `tenant_id`, `assistant_query_id` | 同租户一对一 FK |
| `actor_user_id`, `actor_session_id` | 与 Query 完全一致 |
| `audit_log_id` | 同事务通用 AuditLog FK |
| `required_permissions_json` | 排序、去重、只含该 query type 精确权限集合 |
| `source_types_json` | 排序、去重、只含 typed source enum；失败为空数组 |
| `permission_revision` / `authorization_fingerprint` | 最终写事务内的实时授权版本与安全摘要 |
| `answer_schema_version` | 固定 `admin-assistant-answer-v1` |
| `outcome`, counts, duration, request/trace、模型零用量/成本字段 | 必须逐字段等于 Query |
| `event_dispatch_*` | pending/processing/dispatched/failed、attempt、lease、固定错误码 |
| `created_at`, `updated_at` | 仅 outbox 状态可更新时间 |

业务字段不可更新/删除；outbox 只允许完整 CAS 状态转换。索引按 pending due time 和 tenant/actor/time。

### 5.3 通用 AuditLog

扩展 action/object enum：

- action：`admin_assistant.queried`
- object type：`assistant_query`

after summary 只允许：`query_type`、`outcome`、`claim_count`、`source_count`、`redaction_count`、
`execution_mode`、`model_call_count`、`model_cost_status`。不允许 question、hash、answer、request target、
Token 字段、金额、来源值或任意自由文本。owner trigger 要求
同一 tenant/query/actor/request/trace 的 AssistantAudit 已存在；Query/Audit/AuditLog 使用 deferred 关系在
一个事务内提交。

Python `audit.service` 必须把 `admin_assistant.queried -> assistant_query` 加入 action/object exact 映射，
并像一次性 dry-run/config transfer 一样要求 `before={}`、after 字段集合完全相等、枚举/整数/常量关系
全部成立；不能只依赖通用“字段有变化”推导。提交后既有 `audit.recorded` 仍可尽力投递，但失败不能改变
已提交 Query 或专用 `assistant.queried` source outbox。

### 5.4 迁移守卫与回退

正向迁移：

1. 原子验证 0069、RBAC revision 12 和现有模板/租户投影无漂移。
2. 以 rowid-preserving 重建方式把 catalog 提升到 revision 13，新增一个权限并保留每个租户自定义
   role matrix；只给 admin/security_admin/finance_admin 增加默认 grant。
3. 创建 Query/Audit、索引、FK、immutable/owner/CAS trigger。
4. 重建 AuditLog allowlist 并保留全部既有 rowid、sequence 和业务引用。
5. 提交前验证 exact 对象数、权限数、revision、owner 图和 FK。

反向迁移：

1. 只要存在任意 AssistantQuery、AssistantAudit 或 `admin_assistant.queried` AuditLog 就原子失败；不能删除
   不可变审计事实、制造 export sequence/哈希链缺口，也不能只检查 pending/processing 后静默丢弃终态。
2. 仅在上述三类业务事实均为空时删除 PRD-077 空表并恢复 AuditLog 0069 allowlist/trigger/index；全部既有
   AuditLog 的 rowid、sequence、摘要与外部引用必须逐行保真。
3. 删除新权限，把目录恢复 revision 12；自定义角色原有权限矩阵逐行保真。
4. 恢复 0069 schema/trigger SQL，并验证 `69/0069`、`integrity_check=ok`、外键违规 0。

## 6. Control API

### 6.1 `POST /admin-assistant/query`

请求：

```json
{
  "question": "过去 7 天调用情况如何？"
}
```

成功响应（示意值仅说明 schema，测试不得把真实 Prompt/Response 放入证据）：

```json
{
  "assistant_query_id": "asq_...",
  "assistant_audit_id": "asa_...",
  "query_type": "usage_statistics",
  "answer": {
    "summary": "基于 2 个可验证来源生成。",
    "claims": [
      {
        "claim_id": "request_count",
        "claim_type": "integer",
        "label": "调用次数",
        "value": 120,
        "unit": "requests",
        "source_ids": ["src_overview"]
      },
      {
        "claim_id": "total_tokens",
        "claim_type": "integer",
        "label": "总 Token",
        "value": 4800,
        "unit": "tokens",
        "source_ids": ["src_usage"]
      }
    ],
    "recommendations": []
  },
  "sources": [
    {
      "source_id": "src_overview",
      "source_type": "overview",
      "label": "运行概览",
      "href": "/#overview",
      "record_count": 120,
      "window_started_at": "2026-08-04T00:00:00Z",
      "window_ended_at": "2026-08-11T00:00:00Z",
      "snapshot_at": "2026-08-11T00:00:00Z"
    },
    {
      "source_id": "src_usage",
      "source_type": "usage",
      "label": "用量账本",
      "href": "/#usage",
      "record_count": 120,
      "window_started_at": "2026-08-04T00:00:00Z",
      "window_ended_at": "2026-08-11T00:00:00Z",
      "snapshot_at": "2026-08-11T00:00:00Z"
    }
  ],
  "redactions": ["content_omitted", "credentials_omitted"],
  "request_id": "req_...",
  "trace_id": "trace_..."
}
```

公开响应 exact schema；所有响应设置：

- `Cache-Control: no-store, private`
- `Pragma: no-cache`
- `Vary: Authorization`
- 成功与错误均携带统一 request_id/trace_id；错误体继续使用 shared error envelope。

路由拒绝任何 query 参数、实体型条件 Header、重复 Authorization、重复 JSON key、未知 body 字段和
非 JSON media type。认证与 base permission 检查必须发生在读取正文前。

具体 wire 实现不声明 Pydantic body 参数，只接收 `Request` 并在认证/base 检查后逐块读取最多 16 KiB；
严格校验唯一 `Content-Type: application/json`、唯一 canonical Content-Length、实际长度、UTF-8、JSON
object、duplicate key、节点/深度与 exact-key。只拒绝会改变实体解释或链路身份的 Header：重复
Authorization/X-Request-ID/X-Trace-ID/Content-Type/Content-Length，任意 Content-Encoding、
Transfer-Encoding、Content-Range 和本端不支持的 Idempotency-Key；普通 Accept、User-Agent 等不属于
“未知 Header”拒绝范围。

### 6.2 统一错误码

| error code | HTTP | 语义 |
| --- | ---: | --- |
| `admin_assistant.request_invalid` | 422 | wire/schema/Unicode/长度不合法 |
| `admin_assistant.read_only_violation` | 403 | 请求包含写入或高风险动作 |
| `admin_assistant.query_unsupported` | 422 | 不属于四种查询 |
| `admin_assistant.query_ambiguous` | 422 | 多意图、多 request_id 或冲突窗口 |
| `admin_assistant.source_not_found` | 404 | 当前 tenant 找不到目标来源 |
| `admin_assistant.rate_limited` | 429 | actor/tenant 查询额度耗尽 |
| `admin_assistant.source_unavailable` | 503 | 快照读取、期限或事实映射失败 |
| `admin_assistant.answer_invalid` | 503 | claim/source 图不能通过内部 validator |
| `admin_assistant.audit_unavailable` | 503 | Query/Audit/AuditLog 原子提交失败 |
| `admin_assistant.event_unavailable` | 内部 | outbox 固定失败分类，不直接返回浏览器 |

认证失败继续使用 `auth.*`；源域缺权统一 `auth.permission_denied`，避免按错误差异枚举受保护领域。
所有异常日志只记录固定 error code、request_id、trace_id 和安全计数，不记录问题、答案、来源值、SQL 或
底层异常正文。

### 6.3 错误优先级与失败审计

错误优先级固定，不能由异常捕获顺序漂移：

1. raw Authorization 歧义；
2. Session 认证失败；
3. base permission 缺失，记 unknown/permission_denied 后返回 `auth.permission_denied`；
4. query/header/body wire 错误，记 failed/`admin_assistant.request_invalid`；
5. read-only violation；
6. unsupported/ambiguous；
7. 全部源域权限；
8. actor/tenant limiter；
9. tenant source not found、来源读取/答案校验；
10. 最终 Session/RBAC 复核；
11. Query/Audit/AuditLog/event 原子提交。

除无法建立可信 tenant/actor 的认证失败外，每个终态都写一组 Query/Audit/AuditLog/event。原始错误若可审计
但审计提交失败，公开结果提升为 `admin_assistant.audit_unavailable`，不能先返回原错误或回答。断连、
Browser Abort 和响应写失败不会回滚已提交事实；服务端建立可信 actor 后不把客户端取消当作跳过审计的
信号，并会等待已启动 worker 与最终治理事务完成。通用 `audit.recorded` 网络副作用由响应
`BackgroundTasks` 尽力投递，不阻塞 durable Query/Audit/AuditLog/event 的提交结论。

## 7. Webhook `assistant.queried`

信封继续使用标准字段：event_id、event_type、timestamp、tenant_id、actor_id、object_id、trace_id、
payload、signature。payload exact 字段：

```json
{
  "schema_version": "assistant-queried-v1",
  "query_type": "usage_statistics",
  "outcome": "answered",
  "claim_count": 7,
  "source_count": 2,
  "redaction_count": 2,
  "duration_bucket": "under_1500ms"
}
```

- 不包含 question、question hash、answer、target request_id、金额、Token 数、错误码分布、策略身份或 URL。
- stable `asevt_...` 提供 at-least-once；Webhook delivery 使用既有 event_id 去重语义。
- worker 每次只短事务 claim 一行，90 秒 lease，5/30/300 秒退避，最多 10 次；完整
  tenant/event/token/owner/attempt/lease CAS 阻止迟到 worker 覆盖。
- Control 停服后，lifespan scheduler 从 pending/stale processing 恢复；网络调用不持有数据库事务。
- commit-unknown 时按 stable event ID 和已有 delivery intent 恢复，不重新生成第二个业务事件。

## 8. PAGE-107 前端设计

### 8.1 页面组成

- 导航 ID/section：`admin-assistant`；后端导航要求 `admin_assistant.query`，放在“总览”组。
- 紧凑标题栏：`管理助手`、只读状态标识、清空图标按钮。
- 四个可执行问题建议按钮，点击只填入问题，不自动发送。
- 有界 textarea、发送按钮、取消按钮；发送中输入禁用并支持 AbortController。
- 回答区按 claim 分组展示调用/成本/错误/策略事实，不把回答做成可编辑内容。
- 来源区使用真实 `<a>`，仅渲染 exact allowlist 的 `/#...` 链接；链接文本包含 source label 和时间范围。
- 脱敏区只展示固定 redaction label；底部展示 query/audit/request ID，便于审计关联。

### 8.2 Owner、撤权与隐私

完整 owner 至少包含 tenant_id、user_id、auth session、permission revision、authorization fingerprint、
后端 active navigation item 和所需权限。任一维度变化时：

1. 先 abort 在途请求；
2. 清空 textarea、answer、source、错误和链路 ID；
3. 替换 PAGE-107 DOM；
4. 只有新的 `/auth/me` 与 `/console/navigation` 完全一致后才允许 fresh query。

问题和回答不得进入 localStorage、sessionStorage、URL、hash、console、页面访问事件或错误上报。迟到响应
必须用 generation + owner 双重校验丢弃。源链接只能在当前回答 DOM 中存在，登出后不得残留。

渲染来源时除 exact href allowlist 外，还必须把 source 对应 permission 与最新后端 navigation item
交叉验证；目标项不在当前导航快照时只显示不可点击来源标签，不能用一份已经迟到的 answer 恢复被撤销
入口。source 点击和任意 hashchange 都先同步调用 page state `leave()`/递增 generation，再交给主导航，
关闭“切页后旧响应短暂回填”的窗口。

### 8.3 响应式与可访问性

- 1280x720 使用左侧输入/右侧回答与来源；390x844 改为单列，页面不产生横向滚动。
- textarea、claim、source 使用稳定 min/max 和 overflow-wrap，最长 request ID/source label 不撑宽容器。
- 发送/取消/清空使用明确 icon 或 icon+command；未知图标带 title/aria-label。
- 回答状态 `aria-live=polite`，错误 `role=alert`；加载状态不改变固定工具栏尺寸。
- 所有服务端文本先 escape；href 通过 typed validator 后再进入 attribute。

## 9. 涉及模块与代码修改计划

### 9.1 Shared

- `shared-sdk/python/metahub_shared/admin_assistant/`：自然语言 parser、禁止动作闸门、typed claim/source、
  deterministic renderer 和内部 validator。
- `shared-sdk/python/metahub_shared/rbac.py`：catalog revision 13、base permission 与三个默认角色。
- `shared-sdk/python/metahub_shared/runtime/errors.py`：PRD-077 固定错误目录。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：`assistant.queried` allowlist、payload validator/builder。
- `shared-sdk/python/metahub_shared/runtime/config.py`：outbox enable/poll interval 的安全配置与摘要。

### 9.2 Control

- `control-plane-api/src/metahub_control/admin_assistant/`：`routes.py`、`service.py`、`repository.py`、
  `outbox.py`、`scheduler.py`、`wire.py` 与包导出。
- `auth/dependencies.py` 与 `auth/__init__.py`：认证 helper；源权限仍由 transaction permission helper 校验。
- `audit/service.py`：Assistant Query 的严格 action/object/summary 白名单。
- `console/service.py`：PAGE-107 导航和安全 page-view event。
- `webhooks/service.py`：稳定 source_request_id/commit-unknown 恢复接点。
- `app.py`：router、private no-store middleware 和 lifespan scheduler 安装。

### 9.3 数据库与部署

- `infra/migrations/0070_read_only_admin_ai_assistant.sql`
- `infra/migrations/0070_read_only_admin_ai_assistant.down.sql`
- `.env.example`、Compose/部署摘要：outbox 开关和 poll interval，不新增 Provider credential。

### 9.4 Console

- `console-web/src/lib/adminAssistant.js`
- `console-web/src/features/admin-assistant/adminAssistantPageState.js`
- `console-web/src/features/admin-assistant/renderAdminAssistant.js`
- `console-web/src/features/admin-assistant/bindAdminAssistant.js`
- `console-web/src/features/developer-start/renderDeveloperStart.js`、`src/app/main.js`、`styles/main.css`、i18n。
- `console-web/tests/unit/prd077AdminAssistant.test.mjs`

### 9.5 文档与验收器

- `docs/api/admin-assistant.md`
- `docs/testing/PRD-077-tests.md`
- README、目录说明、PRD/testing 索引或相邻交接记录。
- `scripts/acceptance/prd077-runtime-acceptance.py`
- `scripts/acceptance/prd077-browser-acceptance.mjs`（复用仓库既有 Browser 驱动和 manifest 规则）。

所有新增或修改的 Python/JavaScript/SQL 关键判断必须带详细中文注释，特别说明 tenant、权限、来源、
隐私、CAS、失败恢复和兼容性边界；禁止用机械注释替代业务解释。

## 10. 测试设计

### 10.1 Shared 单元测试

- 四类中文/英文自然语言、同义词与自由语序、24h/7d/30d、request_id 提取；不能用四个按钮文案的
  exact match 冒充自然语言解析。“今天/昨天/本周”必须因缺少可信时区而歧义失败。
- 多意图、多 ID、冲突窗口、超长、控制字符、Unicode 非法值、未知/重复字段。
- 所有禁止动作；注入式“忽略规则并修改/访问 URL/执行 SQL”必须被阻断或 unsupported。
- claim/source 引用完整性、typed value、金额精度、source/href allowlist、未知 URL、重复 source。
- 每条 recommendation 都有触发规则和来源；无证据不生成数值或节省承诺。
- RBAC revision 13 与六角色精确上限；developer/viewer/ops 不获得 base permission。
- Webhook payload exact、安全键扫描和稳定事件 ID。

### 10.2 Migration 0070

- 从空 SQLite fresh/repeat 到 `70/0070`，`integrity_check=ok`、foreign_key_check 0。
- 从真实 0069 升级，RBAC exact count/revision/默认 grant；多个 tenant 的自定义矩阵保真。
- Query/Audit owner、immutable、outbox CAS、AuditLog deferred owner 与错租户 FK 反例。
- AuditLog 既有 rowid、sequence、摘要与所有 0069 表逐行保真。
- 任意 Query/Audit/AuditLog 业务事实的 down guard；只在空业务库 down 到 `69/0069`，再 re-up 到
  `70/0070`，并逐行证明既有 AuditLog/export sequence 保真。
- 迁移中断、重复执行、损坏 catalog/trigger/schema、外部 FK 引用和非规范 JSON 必须原子失败。

### 10.3 Control/API

- TC-0153：四类主流程，回答每个 claim/recommendation 均可解析到当前 tenant 的 source。
- admin 全部可用；finance 仅用量/成本；security 仅错误/策略；developer/viewer/ops base 403。
- base permission、每个源权限在路由后/只读事务内/最终审计事务前动态撤销，均不得返回迟到回答；撤权
  拒绝仍有零 claim/source 审计，审计失败则提升为固定 503。
- 同 request_id 的本租户/外租户/不存在边界，外租户统一 404，SQL 查询均 tenant-first。
- prohibited action、unsupported、ambiguous、rate limit、deadline、数据库 read/write/commit 故障。
- 成功、拒绝、阻断、缺权、失败 Query/Audit/AuditLog 数量和字段 exact；问题/答案零持久化。
- loop 双长期连接复用与隔离、WAL/FULL 冷启动、read/write admission、连接超时/回滚后的复用、lifespan
  teardown、宿主取消和跨 loop 回收；认证/来源/最终 progress handler 与 `busy_timeout` 不泄漏状态；已登记
  write waiter 在 `closing` 后仍先于 teardown owner 完成，新治理注册则关闭式拒绝。
- PAGE-107 入口认证 `update_last_seen=False`，普通认证仍更新；expired Session 只读入口不写状态，成功
  `last_seen` 与 Query/Audit/AuditLog/event 原子提交或回滚。
- 20 个并发 HTTP attempts 可混合 200 与固定 source 503，但精确产生 20 组唯一且一对一的
  Query/Audit/AuditLog/event，短写事务无 lost audit；本期不接受或伪装 Idempotency-Key。
- 客户端取消、响应写失败和通用 audit BackgroundTasks 不得阻塞事件循环或跳过 durable 终态。
- raw wire：匿名/撤权优先级、重复 Authorization/JSON key、未知 query/header/body、截断流、错误脱敏。
- response headers、exact schema、request_id/trace_id 和错误目录。

### 10.4 Outbox/Webhook

- pending claim、stale lease、重启恢复、退避、max attempts、坏 source 隔离、后续 tenant 不饥饿。
- stable event ID、重复 worker、迟到成功/失败、secret rotation、无 endpoint 和 commit-unknown。
- Webhook payload 不含 question/answer/request target/成本/Token/策略身份/URL；敏感键和随机 canary 扫描 0。

### 10.5 Console

- API client exact body/response/error/header，不把问题写 console 或 storage。
- 后端导航 + auth owner 双校验；base/source 权限导航可见性和来源链接可达性。
- submit/cancel/clear、四种结果、空状态、错误状态、来源跳转和固定 redaction。
- 迟到成功/错误、重复点击、切页、hash 篡改、换 tenant、动态撤权、登出时 DOM/state 清理。
- HTML/attribute 注入、`javascript:`/外链/未知 source、超长 label/request ID。
- 1280x720 与 390x844 无横向溢出、重叠、文本裁切和布局跳动。

### 10.6 Runtime 与 Browser 验收

Runtime 必须使用一次性 `.tmp` 数据库和随机 canary，实际启动 Control：

1. fresh/repeat/down/re-up migration，四阶段 integrity/FK 均通过。
2. 两个 tenant、admin/finance/security/developer，真实写入 RequestLog/Usage/FinOps/Policy/Snapshot 安全事实。
3. 实际 HTTP 执行四类查询、越权、跨租户、只读阻断、失败和并发。
4. 停止 Control 后制造 pending source，再由新 Control lifespan scheduler 恢复 `assistant.queried`。
5. 验证 Query/Audit/AuditLog/outbox 一一对应，pending/processing/quarantine 为 0。
6. 扫描数据库、日志和 manifest：Provider Key、Token、问题、回答、Prompt、Response、`.env` 内容和
   随机 canary 明文命中为 0。Browser 只在内存中断言活动回答 DOM；不把含问题/回答的截图保存为验收
   artifact，持久 manifest 只保存清空后页面的像素/布局摘要与文件 SHA-256。

Browser 必须使用实际 PAGE-107、桌面和移动视口，覆盖 admin 主流程、finance/security 权限边界、来源
链接、禁止动作、动态撤权、迟到响应和登出清理；检查非空像素、横向溢出、重叠、异常 Console、外部请求
和 manifest SHA-256。验收结束后清理自建进程，不留下浏览器、server、SQLite、日志或临时 credential。

## 11. 实现步骤

1. 完成本文两轮设计自审并落盘修正。
2. 先实现 Shared parser/claim/source/RBAC/error/webhook 合同及单测。
3. 实现 0070 up/down 与迁移专项。
4. 实现 Control repository/service/wire/route/audit/outbox/scheduler 及专项。
5. 实现 PAGE-107 API/state/render/bind/owner 清理和 Console 专项。
6. 更新 API、README、目录说明和测试记录。
7. 运行受影响专项，再运行必要相邻回归、中文注释门禁和 lint；不重复无关全量。
8. 从空库执行真实迁移、服务健康、Runtime 和双视口 Browser 验收。
9. 执行敏感信息与禁止文件审计；只显式暂存 PRD-077 文件，提交并推送 `origin/main`。
10. 推送成功后才交叉确认下一条 PRD，且继续保持 `all_108_prds_complete=false`。

## 12. 编码前自审记录

### 12.1 第一轮自审

结论：已完成并修正，未开始编码。

1. **源域越权已修正**：初稿让 `usage_statistics`/`cost_advice` 读取 RequestLog 聚合，却没有要求
   `request_log.read`。这会使 finance_admin 借助手获得日志域事实。现改为把不可识别请求聚合明确归入
   `dashboard.read -> overview`，Usage/FinOps/预算/异常/价格各要求自身权限；缺一项整次失败。
2. **来源图已补齐**：新增 overview、budgets、cost_anomalies、model_prices 的 typed source/href/permission
   三元映射。所有 claim/recommendation 仍须引用本次 tenant 快照中的来源，模型、客户端和问题文本均不能
   生成 URL。
3. **策略冲突口径已收紧**：只公开同租户、同优先级候选组这一可验证事实，并明确它不是 DSL 语义冲突
   证明；不读取或返回策略表达式。request-specific 解释继续要求 `request_explanation.read`。
4. **本地执行追踪已补齐**：每次 Query/Audit 固定记录模型调用、输入/输出 Token、模型成本均为 0 和
   `not_applicable_local`；不制造零金额 Usage/FinOps 分录，失败仍由 outcome/failure_code 追踪。
5. **只读与租户边界复核通过**：源连接强制 `query_only=ON`，固定 tenant-first 参数 SQL、30 天窗口、
   top-N=5；tenant 只来自 AuthContext，Query/Audit/AuditLog/event 与 actor owner 使用复合关系。
6. **回退破坏审计链已修正**：初稿计划删除 PRD-077 AuditLog，与 PRD-058 不可变/export sequence 冲突。
   现改为任意业务事实存在即拒绝 down，只允许空业务库恢复 0069，并逐行保真既有审计。
7. **隐私证据矛盾已修正**：含活动问题/回答的截图不能成为验收 artifact；Browser 运行时只做内存断言，
   持久 manifest 仅记录清空后布局摘要和哈希。

第一轮未发现需要扩大到写操作、外部模型、任意 SQL/URL 或新默认 Provider 的理由；这些边界继续失败关闭。

### 12.2 第二轮自审

结论：已完成并修正，可以进入编码。

1. **认证与审计次序已闭合**：路由不能使用会在 endpoint 前直接 403 的复合依赖；改为 raw Header 唯一性
   校验 + 仅认证依赖，再在正文前手工检查 base 权限，使可信 base-denied 调用也能写零正文审计。
2. **撤权 TOCTOU 已闭合**：来源只读事务和最终 `BEGIN IMMEDIATE` 都重读 Session 与完整权限；最终
   writer reservation 把权限检查与持久审计原子绑定。最终撤权丢弃回答，并用独立短事务写拒绝事实。
3. **错误优先级已冻结**：wire、认证、base、只读、意图、源权限、限流、来源、答案、最终授权、审计按
   固定顺序处理；能建立 tenant/actor 的失败全部审计，审计自身失败一律提升为固定 503。
4. **并发语义已澄清**：问题/回答均不持久化，因此本期不能安全重放幂等结果。每个 HTTP attempt 独立
   一对一审计，20 并发应有 20 组事实；前端防连点不能替代后端一致性。
5. **RBAC 迁移兼容已修正**：仅默认矩阵角色获得新 grant，自定义 finance/security 矩阵不被覆盖；up/down
   必须保真 permission revision、matrix SHA、rowid 和所有旧关系，新租户初始化使用 revision-13 默认值。
6. **AuditLog 兼容已冻结**：新增 action/object/summary 必须 exact 校验，并保留 PRD-058 export sequence、
   全部 0069 index/trigger 与 rowid；任意本功能业务事实存在时 down 失败。
7. **HTTP 兼容已修正**：“未知 Header”收窄为会改变实体解释/链路身份的集合；手工有界 parser 复用仓库
   raw ASGI header 和 duplicate-key 模式，不让 FastAPI 在认证前解析正文。
8. **前端 owner 竞态已闭合**：generation + tenant/user/session/revision/fingerprint/navigation 全量比较；
   hash/source 点击先清状态，来源还要与最新后端导航相交，撤权后不恢复入口。
9. **隐私与 Provider 边界复核通过**：问题/回答只在当前 HTTP 与活动 DOM 内存；DB、日志、Webhook、
   AuditLog、URL、Storage、持久截图和 manifest 均不含正文。功能不调用 Provider，不改变默认上游，零模型
   用量/成本以常量事实追踪；Webhook 网络调用仍在事务外。

第二轮未留下阻止编码的开放决策。后续实现若偏离任一冻结边界，必须先回到本文补充自审，不能用测试
绿灯替代设计变更记录。

### 12.3 实现期对齐复审

结论：并发、deadline 和生命周期实现经过两轮只读复审后已按实际约束回写本文；最终运行门禁已通过。

1. **连接所有权已收敛**：每请求独占连接改为每 loop 两条长期连接，read/write 各容量 1；WAL/FULL、
   progress handler、busy timeout、query_only、rollback 和 teardown 都由明确 owner 管理。
2. **认证与最终原子性已修正**：入口认证保持只读，不刷新或持久化 expired Session；可信 `last_seen` 只在
   最终四事实事务更新，撤权或提交失败一起回滚。
3. **取消与线程清理已修正**：已认证客户端取消仍等待 worker 和治理终态；lifespan 被取消也先排空 slot、
   关闭连接，再向宿主传播取消。绝对期限是阶段 admission/SQL 决策预算，不宣称能强杀已启动线程。
4. **并发验收语义已修正**：单连接 admission 下 20 并发允许受控 503，但必须留下 20 组完整唯一审计事实；
   测试同时固定连接复用、冷 WAL、last_seen、BackgroundTasks 和 teardown。
5. **有界 limiter 已补齐**：actor/tenant 空 key 过期回收，registry 达到固定容量时失败关闭，避免长期身份
   churn 造成进程内存无界增长。

### 12.4 实现期并发缺口第一轮自审

结论：重负载证据推翻了“1.5 秒总期限足以同时覆盖决策和治理”的实现假设，必须先修订期限所有权再继续
最终门禁。

1. **失败证据已定位**：20 个并发 attempt 只形成 13 组治理图，其中 10 个 answered、3 个
   `source_unavailable`；另外 7 个在单 write slot 前耗尽 1.5 秒期限并被提升为
   `audit_unavailable`。数据库完整性和事务原子性正常，缺口是治理 admission 没有独立时间预算。
2. **三阶段边界已冻结**：middleware 同一入口时钟派生 source `started+1.2`、decision `started+1.5`、
   governance `started+5.0`。source/decision 的吞吐保护不放宽；5 秒只供最终授权复核、`last_seen` 和完整
   Query/Audit/AuditLog/event 持久化使用。
3. **响应与审计顺序不变**：成功回答或可持久化业务错误仍须等待治理事务提交后才能返回；不得把持久化改成
   响应后的后台任务，也不得在 5 秒耗尽后迟到提交。
4. **真实存储故障仍关闭式**：SQLite write lock、progress interruption、commit/rollback 异常或治理期限耗尽
   均返回固定 `admin_assistant.audit_unavailable`，且四表不得留下部分事实。

第一轮修正不改变 API schema、数据库 schema、RBAC、tenant 条件、Provider 边界或隐私合同。

### 12.5 实现期并发缺口第二轮自审

结论：第一轮的独立治理期限可实现，但必须补齐 registration 与 teardown 的竞态约束；修正后允许进入编码。

1. **deadline 不刷新**：治理期限和前两条期限一起在 middleware 入口冻结。write slot 获取、写连接初始化、
   `busy_timeout`、progress handler、最终权限复核和递归撤权终态都只接收同一绝对 governance deadline，
   取得 slot 后禁止重新计算 5 秒。
2. **排队所有权已闭合**：新增专用 persistence worker，在检查 runtime 可接收新工作后立即进入既有
   `CapacityLimiter(1)` FIFO；它复用既有 shield、线程退出和 slot 释放语义，不增加无界线程、第二写连接或
   自制队列。延迟写测试必须让 slot 持有超过 1.5 秒、在 5 秒前释放并证明完整事实仍提交。
3. **teardown 竞态已闭合**：`closing` 阻止新 persistence worker 登记，但不能让已在 FIFO 中等待的 worker
   因写连接守卫再次失败。teardown 自身排在既有 write waiter 后取得 slot，随后设置 `closed` 并关闭连接；
   `closed` 后任何 worker 都固定拒绝。宿主取消仍需等待已启动线程真实退出。
4. **并发与故障测试已冻结**：20 并发用例给每个短写事务注入确定性延迟，使总排队超过 1.5 秒，并精确断言
   20 组唯一一对一事实；write-slot/DB-lock 用例显式缩短 governance deadline，分别证明未启动线程和无部分
   事务。另加 teardown 与已登记 write waiter 的顺序测试。
5. **兼容性复核通过**：错误码、HTTP 状态、前端交互、0070 正反迁移和 outbox schema 均不变；来源失败仍可
   是部分 503，区别只在这些已认证 attempt 不再因治理排队丢失审计。功能继续零模型调用、零 Token/模型
   成本，不接触默认上游或迁移兼容 Provider。

## 13. 当前门禁

- [x] 权威顺序与范围交叉确认。
- [x] 初版设计落盘。
- [x] 第一轮设计自审并修正。
- [x] 第二轮设计自审并修正。
- [x] 编码、专项测试、迁移、文档、Runtime、Browser 与提交前审计。

## 14. 最终验收记录

- Runtime 权威批次为 `.artifacts/prd077-runtime/prd077-final-20260812-04/`。从空 SQLite 实际完成
  fresh/repeat/down/re-up：`70/0070 -> 70/0070 -> 69/0069 -> 70/0070`，四阶段
  `integrity_check=ok`、`foreign_key_check=0`；`runtime-acceptance.json` SHA-256 为
  `c4903b6c542051164d8ce9298b61581620423ac9782141e9d9a1340be9993f4b`。
- Runtime 实际启动 Control，覆盖四类查询、admin/finance/security/developer、第二租户、只读阻断、
  来源故障和 20 并发；并发结果为 9 个 200、11 个固定 503，20 组 Query/Audit/AuditLog/Event 均唯一且
  一一对应。停服前 33 个 pending source 由重启 scheduler 恢复，最终 pending/processing、failed 和
  forbidden/invalid Webhook 均为 0；owned process、SQLite sidecar 和敏感明文命中均为 0。
- Browser 权威批次为 `.artifacts/prd077-browser/prd077-browser-final-20260812-05/`。实际 PAGE-107 覆盖
  四类回答、finance/security 允许与拒绝、动态撤权、后端 403、来源链接精确导航与两类篡改拒绝、取消、
  迟到结果和登出清理；6 次助手 200、3 次助手 403，外部请求、私密 URL/Header、非预期 Console error
  和进程明文命中均为 0。
- `1280x720` 与 `390x844` 清空态截图已人工复核，均无敏感正文、横向溢出、文本溢出、控件重叠或空白
  像素问题。`sha256-manifest.json` 精确声明 4 个 payload 文件，文件名、字节数和 SHA-256 独立重算全部
  匹配；manifest 自身 SHA-256 为
  `d347474897d3c2eeceb2fc4af73b115758a83a94101819c1757ac9b81e2b55c0`。
- 完整专项、必要回归、静态门禁、中文注释、敏感信息、禁止文件、端口和 owner 进程审计见
  `docs/testing/PRD-077-tests.md`。本条完成不代表总体完成，`all_108_prds_complete=false`。
