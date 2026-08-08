# PRD-074 - 批量任务 Scheduler 与 Worker

状态：**已完成并通过最终验收（2026-08-08）**。设计于 2026-08-07 冻结；本条从权威 Excel 七个工作表、README、架构文档、
PRD-072/073 设计与测试记录交叉确认后启动，不按编号推断。PRD-073 已完成，不在本条重复实现。
全部 108 条总体目标仍在进行中：`all_108_prds_complete=false`。

## 1. 需求来源与顺序确认

### 1.1 Excel 权威行

已使用 OOXML 结构化解析交叉核对：

- `01_PRD总表!75`、`03_M2-M3扩展交付!11`：
  `PRD-074 | M2-企业增强 | P2 | 扩展层 | 批量任务 | 批量任务Scheduler与Worker`。
- 主要用户：开发者、运营。
- 用户故事：批量总结、翻译、分类文档，不占用实时请求资源。
- 范围：`BatchJob`、队列、Worker、优先级、最大执行时长、重试、成本预估、租户并发限制。
- 非目标：不走实时 Chat 热路径，不做复杂工作流编排。
- `04_页面清单!105`：`PAGE-104 批量任务页`。
- `05_API接口清单!333:341`：`POST /batch-jobs`、`GET /batch-jobs/{id}`、
  `POST /batch-jobs/{id}/cancel`。
- `06_数据对象!100:101`：`OBJ-099 BatchJob`、`OBJ-100 BatchTask`，业务对象均带
  `tenant_id`。
- `07_Webhook事件!83:84`：`EVT-082 batch_job.created`、
  `EVT-083 batch_job.completed`。
- `08_验收用例!148:149`：`TC-0147` 主流程、`TC-0148` 资源隔离/重试/权限/异常。

### 1.2 独立交叉结论

README 明确记录 PRD-073 已收口且 PRD-074 为下一条；`docs/prd` 与 `docs/testing` 当前最高完整
记录同为 PRD-073。架构文档要求数据面、控制面、调度任务、对象存储和数据库拆分部署，并把
Batch API 标为后续能力。四类来源一致，因此 PRD-074 是当前下一条未完成项。

## 2. 需求理解

PRD-072 负责文件接收、解析、DLP 与加密存储，PRD-073 负责隐藏 Prompt Injection 检测和模型
上下文准入。PRD-074 只消费两条已建立的后端边界：它不重做解析/扫描，也不能直接解密
`FileExtract`。每个任务必须调用 PRD-073 的模型准入函数取得带不可伪造边界的文件上下文。

```text
PAGE-104 / Control API                         独立 batch-worker
          |                                          |
          | POST 创建 Job/Task/预算 hold/outbox      | 公平 claim + 90s lease
          v                                          v
   SQLite 持久队列 -------------------------->  PRD-073 文件模型准入
                                                       |
                                                       v
                                            Gateway /internal/batch
                                                       |
                                    完整 Policy/DLP/Intent/Rate/Budget/Route
                                                       |
                                                       v
                                              官方/云/本地 Provider
                                                       |
                         RequestLog/Usage/FinOps <------+--> AES-GCM 结果
```

“不占用实时请求资源”冻结为可验证工程合同，而不是描述性口号：

1. Scheduler/Worker 是独立进程，不安装进 Control API lifespan，也不进入公开实时 Chat 路由。
2. Gateway 内部 Batch 入口使用独立服务 Token、独立有界并发和实时优先 admission gate；实时入口
   永不等待 Batch gate，存在实时活动或冷却窗口时 Batch 返回固定可重试忙码，且不消费 grant。
3. Worker 有全局与每租户并发上限；租户轮转公平，单租户不能占满队列。
4. Batch 仍复用完整 Gateway 治理、Provider Adapter、RequestLog、Usage 和 FinOps，不允许直连
   Provider，也不把 OpenRouter/硅基流动作为默认上游。

## 3. 范围与非目标

### 3.1 本 PRD 实现

- PAGE-104：创建、列表、状态、任务失败原因、取消和结果下载/查看。
- 三个权威 API，以及页面可恢复性所需的列表、任务游标和显式结果读取 API。
- `BatchJob`、`BatchTask`、`BatchAttempt`、预算 hold/allocation、一次性执行 grant、source event/outbox。
- 固定 `summarize`、`translate`、`classify` 三种操作；每个 file 形成一个 task。
- 高/普通/低三个优先级；同租户内优先级排序，跨租户按公平游标轮转。
- 最大执行时长、最多三次 attempt、固定退避、取消、崩溃恢复和 outcome-unknown 终态。
- 最坏重试与 Gateway fallback 成本估算、并发原子预算占用和实时预算可见性。
- 独立 `batch-worker` 进程、Gateway 内部 Batch 路由和 Docker Compose 服务。
- `batch_job.created/completed` 持久 source/outbox，统一错误码、request_id/trace_id 和隐私审计。
- 追加迁移 `0067`，RBAC catalog revision `9 -> 10`。

### 3.2 明确不做

- 不做 DAG、步骤编排、条件分支、人工审批、定时日历、Cron 或跨 Job 依赖。
- 不允许任意 system prompt、Provider、Endpoint、API Key、温度、工具调用或路由参数进入请求。
- 不接受文件上传正文；只接受 PRD-072 当前 tenant 的 ready 文件 ID。
- 不自动触发 PRD-073 扫描；未通过权威安全报告的文件拒绝提交。
- 不做流式 Batch；内部请求固定 `stream=false`。
- 不复用实时企业 Chat Session/Message，也不改变 PAGE-101/102/103。
- 不承诺 exactly-once Provider 调用；通过“未知结果永不重试”避免无法证明安全的重复计费。

## 4. 权限、用户与租户边界

### 4.1 RBAC revision 10

新增：

| 权限 | 风险 | 默认角色 | 用途 |
| --- | --- | --- | --- |
| `batch.read` | read | admin、developer、ops_admin | 列表、详情、任务、结果 |
| `batch.write` | write | admin、developer、ops_admin | 创建和取消 |

运营是权威主要用户且必须实际处理文档，因此 revision 10 同时把 `file.read` 加入 OpsAdmin 产品上限与
新租户默认矩阵。该扩权必须通过迁移、矩阵 hash 和角色测试显式体现，不能由 Batch Worker 暗中绕过。
FinanceAdmin、SecurityAdmin、Viewer 默认不获得 Batch 权限。

### 4.2 动态授权

- 路由 dependency 先按当前 Session 数据库快照检查 `batch.read/write`，随后才解析 path/query/body。
- 创建事务内再次检查 `batch.write + file.read`；取消事务内再次检查 `batch.write`。
- Worker 不依赖已过期的浏览器 Session，但每次 attempt 前按 Job 中不可变的
  `tenant_id + submitted_by_user_id` 重读 active 用户、角色和完整矩阵，要求当前仍有
  `batch.write + file.read`。撤权后任务以 `batch.authorization_revoked` 失败，不解密文件。
- `build_file_model_context_for_batch` 只接受数据库 Job/Task owner，不接受 HTTP tenant/user，并在同一
  事务按该主体实时复核 `file.read`；它与公开 `build_file_model_context` 共用报告、Blob、AAD 和边界逻辑。
- 跨 tenant Job/File/Task/Result 与不存在资源保持同形 404。

## 5. 公开 API 合同

所有成功响应携带 `request_id`、`trace_id`；所有错误使用全局统一错误结构。公开响应均
`Cache-Control: no-store, private`。客户端不能提交 tenant、actor、Virtual Key、Provider、Endpoint、
Token、价格或预算 scope。

### 5.1 `POST /batch-jobs`

依赖 `batch.write + file.read`，要求规范 UUID `Idempotency-Key`。严格请求：

```json
{
  "name": "August documents",
  "project_id": "project_...",
  "environment_id": "env_...",
  "model_alias_id": "alias_...",
  "operation": "summarize",
  "file_ids": ["file_..."],
  "priority": "normal",
  "max_duration_seconds": 300,
  "max_attempts": 2,
  "max_output_tokens": 1024,
  "output_language": "zh-CN"
}
```

- `name` 1..120 个可见字符；禁止控制字符，日志不记录该值。
- `file_ids` 1..100、规范 ID、不得重复；一个 file 一个 task。
- `operation` 固定 `summarize|translate|classify`。
- `translate` 必须有 `target_language`；`classify` 必须有 2..20 个唯一
  `classification_labels`；其他组合 exact-set 拒绝。
- 操作参数整体使用 Batch 独立根密钥、owner-bound AES-GCM 保存，DTO/repr/log 均不显示。
- priority 为 `low|normal|high`；最高优先级仍低于任何实时请求。
- duration 30..3600 秒；attempt 1..3；output token 16..4096。
- 项目、环境、active alias 和唯一 active Project Virtual Key 复用企业 Chat 的严格绑定规则。
- 每个文件必须 ready、属于当前 tenant、具有 token estimate 且 PRD-073 权威报告为
  passed/complete/allowed；在解密文件正文前完成验证。
- 同一事务创建 Job、Tasks、预算 holds、`batch_job.created` source 和 pending outbox。
- 幂等重放必须返回完全相同 Job；相同 key 不同 canonical request 返回
  `batch.idempotency_conflict`。

### 5.2 `GET /batch-jobs`

工程必需的 PAGE-104 恢复接口。依赖 `batch.read`；支持严格 `status`、`limit=1..100`、私有 HMAC
cursor。稳定按 `created_at DESC, batch_job_id DESC`。列表不返回文件名、操作参数或结果正文。

### 5.3 `GET /batch-jobs/{batch_job_id}`

依赖 `batch.read`。返回 Job 汇总、任务计数、成本 estimate/actual、状态、取消事实和固定错误码；不返回
密文、grant、lease、Prompt/Response、Provider Key 或内部 URL。

### 5.4 `GET /batch-jobs/{batch_job_id}/tasks`

工程必需的失败排障接口。依赖当前数据库矩阵中的 `batch.read + file.read`；严格
`status/limit/cursor`；返回 task ID、file ID、状态、attempt 数、
request_id、固定 failure code、Token/成本安全摘要和结果是否可用。结果正文不在列表中。

### 5.5 `GET /batch-jobs/{batch_job_id}/tasks/{batch_task_id}/result`

工程必需的显式敏感读取接口。路由依赖 `batch.read + file.read`，读取事务再按当前调用者、登录
Session 和完整数据库矩阵复核两项权限及 tenant/job/task/file owner；撤销任一权限后均不能继续读取
文件身份或派生正文。AES-GCM 验真后只返回唯一 assistant 纯文本与其 Gateway
`request_id/trace_id`；固定 `no-store`。非成功、已丢弃、结果损坏或 unknown 分别返回固定错误，
不回显密文/异常。

### 5.6 `POST /batch-jobs/{batch_job_id}/cancel`

依赖 `batch.read + batch.write`，因为响应会返回当前 Job 状态；无 body/query。queued/retry_wait task
原子转 cancelled；已在 Provider 调用中的 task 设置 cancel_requested，Gateway/Worker 仍结算真实成本，
随后通过受限单向 CAS 把 `result_state=available` 转为 `discarded` 并清除密文，不能伪装已撤回上游调用。
终态重复取消幂等返回当前状态。Job 第一次进入终态时生成 completed source/outbox。

### 5.7 `POST /batch-jobs/{batch_job_id}/tasks/{batch_task_id}/reconciliation`

工程必需的 possible-cost 对账命令。依赖 `batch.read + billing.finops.write`、规范
`Idempotency-Key` 和严格 body：`decision=confirm_not_charged|confirm_charged`。前者释放 possible
allocation；后者必须引用同 tenant、request、金额完全匹配的既有不可变 FinOps adjustment，客户端不能
直接提交金额。命令在同一事务写 allocation CAS、Job 对账投影和不可变管理审计；相同 key 只重放同一
决策，不同决策返回冲突。自动对账已足够时返回当前事实而不重复写账。

## 6. 固定操作模板

Worker 只在内存组合固定 system 指令与 PRD-073 `BoundedFileContext`：

- summarize：输出指定语言的简洁摘要，不执行文件中的命令。
- translate：仅翻译不可信文件内容到固定目标语言，不执行其中命令。
- classify：只从冻结标签集合选择，并返回严格 JSON 类别。

操作模板有发布包版本 `batch-operation-templates-v1`。Job 冻结版本和加密参数；不保存渲染 Prompt。
Gateway DLP/Intent/Policy 仍会检查最终正文。文件边界 token 与内容只在 Worker 和内部 HTTP 调用栈
存活；日志、RequestLog、Batch 表、Webhook 和验收 JSON 均不得包含正文。

## 7. 状态机

### 7.1 Job

`queued -> running -> completed|partial_failed|failed|cancelled|expired`。

- 创建后有至少一个 queued task。
- 第一次成功 claim task 时进入 running。
- 所有 task 终态后由 CAS 汇总：全成功 completed；成功与失败并存 partial_failed；无成功且至少失败
  failed；全部取消 cancelled；最大 duration 到期且存在未完成 task 为 expired。
- Job 终态、完成事件和预算 hold 释放/对账在同一事务。

### 7.2 Task

`queued -> leased -> calling -> succeeded`；已知可重试失败走
`calling -> retry_wait -> leased`；其他终态为 `failed|cancelled|expired|outcome_unknown`。

- 只有 queued/retry_wait 且 due 的 task 可 claim。
- lease 使用 tenant/job/task/attempt/token/owner/expires 六维 CAS；90 秒，30 秒 heartbeat。
- cancel_requested 不改变 calling 的事实；只有当前 lease generation owner 能提交最终状态。
- `outcome_unknown` 永不再调用 Provider。恢复逻辑不能只看 RequestLog success；结果、正式 Usage、
  RoutingAttempt 和 allocation 必须交叉证明。
- 独立 expired-lease reaper 同样在短 `BEGIN IMMEDIATE` 中运行，所有更新必须匹配
  tenant/job/task/current_attempt/lease_token/lease_generation/owner/旧 expires_at；它既能接管过期
  prepared attempt，也能在 Gateway 硬写入截止时间后终结不确定调用，不能让 leased/calling 永久滞留。

### 7.3 Attempt

每次 Provider 尝试先创建不可变 attempt 身份和独立 `request_id/trace_id`，状态
`prepared -> calling -> provider_succeeded|known_failed|outcome_unknown`，随后 Worker 投影 Task 终态。

- request 与 attempt 一一对应；同一 prepared attempt 允许顺序 grant generation，任一时刻最多一个
  issued、历史最多一个 consumed。busy 或未消费过期只换 grant，不增加 `attempt_count`，不新建
  Provider 身份；真正进入 calling 后的重试必须使用新 attempt/request。
- Gateway 在返回 HTTP 前把已验证 assistant 结果用 Batch 独立 AES-GCM 保存到当前 attempt。
- Worker 崩溃恢复：有认证 result 则完成；有固定已知失败事实则按策略处理；RequestLog 已显示上游可能
  调用但没有认证 result/outcome 时，固定 `batch.provider_outcome_unknown`，不得第二次调用 Provider。
- reaper 按固定证据顺序裁决：`prepared + grant 未消费 + 无 routing attempt` 恢复同一 attempt；认证
  result 恢复成功（已取消/撤权/到期则结算后丢弃）；持久 `not_invoked|known_failed + retry_safe`
  才新建重试；`provider_succeeded` 无认证 result 固定 `batch.result_unavailable`；只有
  `gateway_write_deadline_at` 已过且确认 Gateway 不再允许合法写入，started/不确定调用才 CAS 为 unknown。
  Worker HTTP timeout/传输中断本身不能写 unknown，HTTP 429/5xx 也不能猜测 Provider 未执行。
  业务 4xx、安全、预算、授权、结果格式和 unknown 不重试。

## 8. 公平调度与资源隔离

- 独立 Worker 配置：全局并发默认 2、每租户默认 1、poll 1 秒、lease 90 秒、heartbeat 30 秒。
- 候选 tenant 必须有 due task、未取消/过期 Job；claim 的同一 `BEGIN IMMEDIATE` 必须同时证明数据库
  全局 active lease 数低于 global cap、该 tenant active lease 数低于 per-tenant cap，多个 Worker
  进程也不能突破上限。
- tenant 按 `last_dispatched_at ASC, tenant_id ASC` 轮转；选中后同 tenant 按
  `priority_rank DESC, next_attempt_at ASC, created_at ASC, batch_task_id ASC`。
- claim 在短 `BEGIN IMMEDIATE` 中完成并立即提交；文件解密、HTTP、加密和 Provider 调用不持 SQLite 锁。
- Worker 线程池上限固定，不按请求创建无界线程；shutdown 停止新 claim，等待当前 owner 有界收尾。
- Gateway Batch gate 默认并发 1。公开实时请求只增加实时活动计数，从不等待 batch semaphore；Batch
  仅在实时 active/waiter 为 0 且冷却时间已过时取得 slot，否则在消费 grant 前返回 429。
- 当前 PRD 的 Batch gate 只支持单 Gateway 进程；启动/部署验收必须拒绝为 Batch 启用多 Gateway
  副本。后续多副本必须先实现 Redis 分布式实时优先 gate，不能把进程内计数宣称为全局隔离。
- Virtual Key Rate Limit 增加 Batch lane 与实时保留量。对每个已配置 RPM、TPM、concurrency 维度，
  `reserve=max(1,ceil(limit*reserve_percent/100))`，默认 25%，配置范围 1..90%；Batch ceiling 为
  `limit-reserve`，任一 ceiling <= 0 即拒绝 Batch。内存实现必须在同一锁内、Redis 实现必须在单个 Lua
  中一次检查并递增 RPM/TPM/concurrency 全部维度；任一拒绝零维度计数，禁止逐键 INCR 后补偿 DECR。
  concurrency 在请求完成时由同一 owner 释放，RPM/TPM 按窗口到期。当前单 Gateway 进程使用进程内
  原子 lane；后续多副本必须同时具备 Redis 分布式 gate 与该多维脚本。
- Compose 为 `batch-worker` 设置独立进程、CPU/内存界限和只读代码；不挂 Console Secret，不开放端口。

## 9. 预算估算、占用与结算

### 9.1 最坏情况估算

每 task 输入 token 使用 PRD-072 persisted token estimate 加固定模板开销，输出使用请求上限。价格沿用
Gateway 规则：有效 PricePlan 优先、版本目录价兜底、只接受 CNY、有限非负且精度合法。对当前 alias
冻结主路由与启用 fallback 的身份及价格，单 attempt 取主调用 + 最多一次 fallback 的总额；Job hold 为所有
task 的单 attempt 最坏金额乘以 `max_attempts`。任一模型缺可信价格时返回
`batch.cost_estimate_unavailable`，不能按零元提交。

### 9.2 Job hold

- 对 tenant/project/virtual_key 所有适用 active 日预算规则各建一条 `batch_budget_holds`。
- `BEGIN IMMEDIATE` 中按上海业务日计算正式 FinOps posted + 普通 request reservation + 其他 Batch hold；
  任一规则 projected `>= daily_budget` 时整体拒绝 `batch.budget_exceeded`，零 Job/Task/hold/source；
  `alert_only` 只影响普通实时请求的耗尽动作，不能绕过 PRD-074“超预算不能提交”的硬验收。
- hold 创建时 `remaining=reserved=全额`；claim 分配时原子从 remaining 扣除 allocation reserved。Shared
  outstanding 聚合固定为普通 request reservation + `hold.remaining` + active allocation reserved +
  reconciliation possible，使后续实时请求看到已占额度；禁止再 SUM 原始 hold.reserved 导致结算后双计。
- 提交只在全部规则低于日预算时成功；接近 warning threshold 可以返回固定 warning，但不发送自由文本。
- hold 绑定上海业务日。跨日尚未执行的 allocation 不能继续使用昨日额度：Scheduler 在新 attempt 前以
  `BEGIN IMMEDIATE` 释放旧日未用 hold，按剩余 task/attempt 最坏成本重新评估并创建当日 hold；今日
  不足则 Job 终结 `batch.budget_exceeded`，绝不让昨日 hold 覆盖今日调用。

### 9.3 Attempt 与实际成本

- Worker claim attempt 时按每条 Job hold 原子创建 `batch_budget_allocations`，冻结本 attempt 的主路由 +
  fallback 路由身份和最坏成本 coverage。认证语义不变的 `GatewayAuthContext` 不承载 Batch 字段；数据库
  grant 恢复的 job/attempt/allocation/route plan 只放入 `TrustedGatewayInvocation(source=batch)`，公开
  Header/body 不能制造。
- Gateway Budget Guard 仍复核实时规则、active block、业务日、路由和当前主备估算，但以 allocation
  coverage 准入，不创建普通 `budget_reservations`，也不把 attempt estimate 再加到已计入 spend 的 Job
  hold。Policy `route_to` 或动态路由后的实际冻结 route plan 必须逐项匹配 allocation 的 Provider/模型/
  fallback owner 和金额；coverage 缺失、跨日、身份漂移、金额不等或不足都在 Provider 前失败关闭。
- RequestLogContext 持有内部 Batch allocation 结算指令。direct Usage/FinOps 事务中原子把 allocation
  转为 posted，由正式 charge 接管占用；not_invoked 或明确 Provider 前 known failure 释放当前
  allocation，但不返还已经消费的 attempt 槽；unknown 转 reconciliation/possible 并继续计入；Job
  终态只释放尚未 allocation 的 future remaining。
- Usage spool 升级为安全 schema v6，只携带 tenant/job/task/attempt/request 等规范 ID 和结算金额，不含
  Prompt/Response。direct 与 replay 共用同一 allocation settlement；`_handle_gateway_request` 只有在
  Usage 已 commit 或 v6 已 fsync 后才返回，内部 Batch 随后验证并加密保存 result。spooled Usage + result
  可完成；Usage 可恢复但 result 缺失固定 `batch.result_unavailable`，绝不重调。
- Provider outcome unknown 把 allocation 保持 `possible/reconciliation`，不释放、不重试。后台对账器按
  request_id 查到同 owner 的不可变 Usage/FinOps 后 CAS 为 posted；持久 `not_invoked` 证明则释放；其余
  只能由 5.7 的幂等后端命令解决。Job/API/Webhook 分别公开 `reconciliation_pending`、posted actual
  cost 与 possible cost，不能把 posted-only actual cost 表现成最终成本。
- hold/allocation 更新必须是非负 CAS；金额异常、缺失或超界都失败关闭，不允许截断为零。

## 10. 数据库设计（0067）

所有表默认 `tenant_id NOT NULL`，使用 tenant-qualified 复合外键和 owner trigger。主要表：

### 10.1 `batch_jobs`

`batch_job_id, tenant_id, submitted_by_user_id, idempotency_key_hash, request_fingerprint,
project_id, environment_id, model_alias_id, model_alias_snapshot, virtual_key_id,
name_ciphertext, operation_spec_ciphertext, operation_template_version, priority,
max_duration_seconds, max_attempts, max_output_tokens, task_count, status,
estimated_input_tokens, estimated_output_tokens, estimated_cost_cny,
deadline_at, cancel_requested_at, started_at, completed_at, terminal_error_code,
source_request_id, source_trace_id, created_at, updated_at`。

身份、绑定、规范请求 hash、密文和上限创建后不可改；状态只能按 7.1 单向推进。tenant +
idempotency hash 唯一。`deadline_at` 在创建事务固定为 `created_at + max_duration_seconds`，claim、grant
consume、heartbeat 与 reaper 共用，不能各自重算。公开 name 只在授权读取时解密。

### 10.2 `batch_tasks`

`batch_task_id, tenant_id, batch_job_id, file_id, task_index, priority_rank, status,
attempt_count, next_attempt_at, cancel_requested_at, lease_token, lease_owner_id,
lease_generation, lease_expires_at, current_attempt_id, terminal_request_id, terminal_trace_id,
failure_code, result_available, created_at, updated_at, completed_at`。

`tenant_id + job + task_index/file` 唯一；file owner 使用复合外键。leased/calling 必须完整 lease 四元组，
其他状态禁止遗留 lease。

### 10.3 `batch_attempts`

`batch_attempt_id, tenant_id, batch_job_id, batch_task_id, attempt_no, request_id, trace_id,
status, retry_safe, gateway_error_code, request_payload_ciphertext, request_payload_key_version,
request_payload_hmac, gateway_write_deadline_at, result_state, result_ciphertext,
result_key_version, result_hmac,
input_tokens, output_tokens, cost_cny, provider_started_at, gateway_completed_at,
created_at, updated_at`。

request/trace/attempt 全局唯一。request payload 与 result 使用不同 AES-GCM 域和 AAD，分别绑定
tenant/job/task/attempt/request/template version/key version，明文 UTF-8 wire 各有固定字节上限；HMAC
使用独立分域密钥，禁止正文裸 SHA。`result_state=none|available|discarded` 只允许单向推进；取消、撤权
或到期后可由严格 owner/CAS 从 available 转 discarded 并清空 result 密文/key version，HMAC 留作完整性
对账但不可恢复正文。attempt 行不可删除，只有冻结状态机字段可按 trigger 更新。

### 10.4 `batch_execution_grants`

保存 `grant_hash`、`grant_generation`，以及 tenant/job/task/attempt/request/trace/Virtual Key/Alias owner、
lease token/generation、issued/过期/撤销/used 时间。一个 prepared attempt 任一时刻最多一个 issued grant，
未消费 grant 可过期或撤销后顺序重发；历史最多一个 generation consumed。grant 默认 60 秒一次性消费，
内部 Token 与 grant 均不入日志、响应或证据。

### 10.5 `batch_budget_holds` 与 `batch_budget_allocations`

一 Job/规则一行：tenant/job/rule/scope/date、reserved/remaining/reconciliation CNY、状态和时间。
owner 必须与预算规则、Job、环境完全一致；金额为六位微元兼容 Decimal，不允许负数或增长。

allocation 为一 attempt/hold 一行：tenant/job/task/attempt/hold/rule/date、reserved CNY、
`execution_outcome=not_invoked|known_failed|succeeded|unknown`、
`charge_state=none|posted|possible`、状态和时间。allocation 只能从 active 当日 hold 划拨一次；
posted 与正式 Usage/FinOps request owner 必须一致，possible 不得自动释放。

### 10.6 `batch_scheduler_tenants`

每 tenant 一行公平游标：`tenant_id,last_dispatched_at,dispatch_revision,updated_at`。它不是授权事实，
只允许 claim 事务按严格 +1 更新。

### 10.7 `batch_job_events` 与 `batch_job_event_outbox`

created/completed source 只含固定安全字段：event/job/status/task counters/estimated-or-actual cost/request/
trace/actor/timestamp，不含文件名、文件 ID、操作参数、Prompt/Response、lease 或密钥。source 与 pending
outbox 由 trigger 同事务创建；90 秒租约、最多 10 次、5/30/300 秒退避、空 Endpoint 快照冻结和
tenant 隔离复用 PRD-073 可恢复 outbox 语义。`UNIQUE(tenant_id,batch_job_id,event_type)` 保证每类 source
最多一次；created actor 是提交者，取消导致的 completed actor 是取消者，Scheduler/Worker 汇总则使用
固定 system actor，不能冒充提交者。

## 11. 内部 Gateway 合同

`POST /internal/batch/chat/completions`：

- Header 仅允许独立 `X-MetaHub-Internal-Batch-Token` 与统一 request/trace；拒绝 Authorization、
  Session/Turn Header、query、redirect 和代理。
- body exact-set 只含 `grant`。Worker 在 grant 事务前构造固定 system + 单个 bounded user，并以 Batch
  AES-GCM 写入 attempt payload；Gateway 消费 grant 后按 attempt owner 解密。`model/stream/max_tokens`
  从 grant/Job 冻结事实构造，wire 不能覆盖，Prompt 不进入日志或明文持久层。
- gate 在读取正文和消费 grant 前执行；忙时 429 `batch.gateway_busy`。
- busy、网络未送达或未消费过期时，Worker 只为同一 prepared attempt 换下一 grant generation；持续忙
  超过单个 grant TTL 仍不得增加 attempt_count 或 Provider 调用次数。
- 消费 grant 的 `BEGIN IMMEDIATE` 必须重验 active tenant/user、完整当前 RBAC 矩阵中的
  `batch.write + file.read`、Job 未取消且未过 `deadline_at`、Task 当前 attempt、有效 lease
  token/generation/owner、allocation/Alias/唯一 Key owner；随后原子提交 `grant consumed + attempt
  prepared->calling + task leased->calling + attempt_count+1`。任何旧 owner、撤权、取消、过期或漂移
  统一 `batch.execution_invalid`，正文仍未解密。
- 消费后恢复可信 GatewayAuthContext、alias、allocation 和加密 payload，调用
  `_handle_gateway_request`，因此 Policy、DLP、
  Intent、Rate Limit、Budget、routing/fallback、Provider Adapter、输出安全、RequestLog、Usage、FinOps
  全部与公开请求共用。
- 成功只接受 assistant 纯文本、限定 UTF-8 字节数；先加密写 attempt，再响应 Worker。
- 内部任意未知错误折叠为 Batch 固定码，不复制 Provider body、URL、Header 或 assistant 正文。
- `_handle_gateway_request` 现有三个 nullable `trusted_*` 参数收敛为带 `source=enterprise_chat|batch`
  的 `TrustedGatewayInvocation`；每种来源执行 exact 字段校验，禁止 Chat/Batch 身份和正文半组合。
  本次同步迁移 internal Chat 和 PRD-069/070 helper 调用并删除旧参数，不保留可形成半组合的兼容 shim。

## 12. 错误码

| code | HTTP | 语义 |
| --- | ---: | --- |
| `batch.request_invalid` | 422 | 严格请求/操作参数无效 |
| `batch.idempotency_required` | 400 | 缺少规范幂等键 |
| `batch.idempotency_conflict` | 409 | 同 key 不同请求 |
| `batch.not_found` | 404 | Job 跨租户或不存在 |
| `batch.task_not_found` | 404 | Task/Result 跨 owner 或不存在 |
| `batch.binding_unavailable` | 409 | 项目/环境/Alias/唯一 Key 不可用 |
| `batch.file_not_ready` | 409 | 文件未 ready/无 token estimate |
| `batch.file_security_blocked` | 403 | PRD-073 模型准入失败 |
| `batch.cost_estimate_unavailable` | 503 | 价格/Token 事实不可证明 |
| `batch.budget_exceeded` | 402 | 任一 block 预算不足 |
| `batch.capacity_full` | 429 | tenant 待处理 Job/Task 上限 |
| `batch.gateway_busy` | 429 | 实时优先 gate 暂缓 Batch，grant 未消费 |
| `batch.execution_invalid` | 403 | 内部 grant/lease/owner 无效，不可重放 |
| `batch.authorization_revoked` | 403 | Worker 执行前主体已失权 |
| `batch.provider_outcome_unknown` | 502 | 结果不可证明，禁止重试 |
| `batch.result_not_ready` | 409 | 结果尚不可读 |
| `batch.result_unavailable` | 500 | 认证结果缺失/损坏 |
| `batch.reconciliation_conflict` | 409 | 同一对账幂等键对应不同决策 |
| `batch.worker_unavailable` | 503 | 队列/Worker/Gateway 依赖不可用 |

内部 Token/grant 错误不区分缺失、过期、重放和错 owner，统一 `batch.execution_invalid`，不可重试；
部署缺 Token/key/DB 和未知内部异常统一折叠 `batch.worker_unavailable`，可在核对 request_id 后重试，
不能暴露配置值或异常正文。公开 Control 接口再折叠为上述固定域。所有错误由中间件附
request_id/trace_id；Batch Token 也必须纳入 request/trace ID 的敏感值碰撞防护。

## 13. PAGE-104

- 后端导航 ID `batch-jobs`，target `batch-jobs`，要求 `batch.read`。
- 顶部使用紧凑操作栏：创建按钮、状态筛选、刷新；不做营销 hero 或嵌套卡片。
- 创建抽屉/对话框加载当前授权文件、项目/环境/Alias，提供 operation segmented control、priority select、
  duration/attempt/token 数字控件；按钮可见性不承担授权。
- 主区为可扫描表格：名称、操作、状态、进度、优先级、估算/实际成本、创建时间、操作。
- 详情为 unframed 分栏/移动单列，任务列表 cursor 加载；错误只显示固定本地化原因和 request_id。
- 结果正文仅在用户显式打开单个成功 task 后请求，不预取所有结果。结果 ticket 精确绑定
  tenant/user/session/revision/permission fingerprint/navigation/job/task/generation/AbortController；切换
  job/task、刷新、离页、注销、owner/generation 变化或任何结果读取非 2xx 时，先同步清除旧 DOM/内存
  再中止请求。只有当前 owner 的 401 和 `auth.permission_denied` 清认证态；
  `batch.file_security_blocked` 等业务 403 不得误注销。正文只用 `textContent`，不进入 storage、URL、
  dataset、通用 renderer 或日志。
- 桌面 1280x720 与移动 390x844 无横向溢出、重叠或正文泄露；长 ID 使用受限断行/省略。

## 14. 配置与部署

新增严格、有默认兼容值的配置：

- `METAHUB_INTERNAL_BATCH_TOKEN`：敏感、exclude/repr false，生产必须独立配置，不能等于内部
  Config/Chat Token。
- `METAHUB_BATCH_ENCRYPTION_SECRET`：敏感独立根密钥，不能等于 Auth、Provider、File、Prompt、
  CacheAffinity、SessionRisk 根密钥或任一内部 Token；所有比较 constant-time。
- `METAHUB_BATCH_WORKER_ENABLED=false`、`...POLL_INTERVAL_SECONDS=1`（0.1..60）、
  `...GLOBAL_CONCURRENCY=2`（1..32）、`...TENANT_CONCURRENCY=1`（1..8）、
  `...LEASE_SECONDS=90`、`...HEARTBEAT_SECONDS=30`，并要求 heartbeat < lease/2。
- `METAHUB_BATCH_GATEWAY_CONCURRENCY=1`（固定 1）、`...REALTIME_COOLDOWN_MILLISECONDS=250`
  （0..5000）、`...REALTIME_RESERVE_PERCENT=25`（1..90）。
- `METAHUB_BATCH_MAX_PENDING_JOBS_PER_TENANT=100`、`...TASKS_PER_TENANT=10000`，创建事务按两者
  硬拒绝；单个 assistant 结果 UTF-8 固定上限 1,048,576 bytes。

生产缺 Token/加密 key 时 Control/Gateway 可继续提供其他能力，但创建/内部 Batch 失败关闭；
`service_name=batch-worker` 且 enabled 时缺 Token/key/DB 必须启动失败。Compose 增加
无端口 `batch-worker`，仅注入所需数据库、Gateway URL、Batch Token/key、文件存储；不注入 Console
Secret、管理员密码或无关 Provider 明文。

## 15. Webhook

- `batch_job.created`：Job、operation、priority、task_count、estimated_cost、actor/request/trace。
- `batch_job.completed`：Job、终态、success/failed/cancelled counters、actual_cost、actor/request/trace。
- payload exact-set，金额为规范 CNY 文本；不含 name、file/task IDs、labels/language、正文、结果、
  Provider、模型内部 ID、key、grant 或失败自由文本。
- created 与 completed 各最多一个 source；outbox/dispatch 可 at-least-once，event_id 稳定。

## 16. 迁移与回退

- 只追加 `0067_batch_scheduler_worker.sql/.down.sql`，不改 0066 checksum。
- prerequisite guard 要求 0066 applied、RBAC revision 9 和所有依赖表/trigger 形状。
- 基线精确为 62 个 permission templates、166 条角色模板关系；revision 10 精确为 64/173。两个 Batch
  权限授予 admin/developer/ops_admin 共 6 条，OpsAdmin 追加既有 `file.read` 1 条。仅捕获关系全集与
  revision-9 hash 均精确 canonical 的角色并自动扩展；自定义矩阵不得静默获权，旧 permission
  ID/rowid/时间保持不变。
- 建立 10.1 至 10.7 表、索引、owner/状态/immutable/lease/amount/outbox trigger。
- up 末尾证明表/列/索引/trigger、权限目录、角色上限、owner、`foreign_key_check=0`、
  `integrity_check=ok`，记录 0067。
- down guard 覆盖 Job/Task/Attempt/payload/hold/allocation/grant/scheduler/event/outbox、订阅
  `batch_job.*` 的 webhook endpoint/delivery/attempt/dead-letter、引用新权限的 role_permission_events、
  非 canonical Batch 权限持有者及 OpsAdmin `file.read` 持有者。scheduler cursor 惰性创建，只有其他
  Batch 事实全空时才可安全删除。down 只收缩精确 revision-10 canonical 角色并恢复 revision 9，保留
  旧 permission ID/rowid/时间；任何外部或自定义授权事实存在时固定 guard 阻断。

## 17. 测试计划

### 17.1 Shared

- revision 10 目录/角色 hash、Ops file.read/batch 权限、未知权限失败关闭。
- Batch AES-GCM owner/AAD/domain、操作 exact-set、Prompt/Response repr/log 隔离。
- Batch hold 纳入实时预算、排除当前 Job、金额损坏失败关闭。

### 17.2 Control API / Worker

- 创建/幂等/绑定/file owner/PRD-073 准入/价格/预算/容量/权限撤销。
- Job/Task 列表 cursor、跨租户同形 404、结果 no-store、cancel 竞态。
- 公平 tenant 轮转、priority、全局/tenant 并发、90 秒 claim/heartbeat/CAS、重试矩阵、duration。
- 崩溃点：claim 后、grant 前、Provider 前、Provider 后 result 前、result 后 Worker commit 前。
- unknown 不重试；已持久结果恢复；每 attempt 独立 request_id 且 RequestLog/Usage/FinOps 可关联。
- created/completed source/outbox 同事务，停服后恢复、空订阅冻结、退避、坏 source 隔离。

### 17.3 Gateway

- 内部 Token/grant exact owner、过期/重放、Authorization/query/header 拒绝。
- gate 在正文/grant 前拒绝，实时请求从不等待；Batch 并发有界。
- 内部请求复用 Policy/DLP/Intent/Rate/Budget/route/fallback/output safety。
- assistant 结果先加密后响应；错误与日志不含 request/response/token/key。
- Job hold 与普通 reservation/Usage 的不双计、实时可见和 unknown reconciliation。

### 17.4 Migration / Console / Runtime

- fresh DB `67/0067`、二次 apply 幂等、down 空库成功/有事实阻断、历史 RBAC 升级。
- PAGE-104 strict client、精确 result ticket、权限错误分类、迟到响应丢弃、移动/桌面布局、DOM 隐私、lint。
- 三服务 + 独立 Worker 真实运行：提交、实时活动时 Batch defer、Worker 恢复、retry、cancel、预算拒绝、
  结果读取、RequestLog/Usage/FinOps、两个 Webhook、进程与临时资源清理。运行脚本、进程、数据库、
  日志、stub 和 artifacts 全部位于 `C:\MetaHub`；本地 OpenAI-compatible Provider/Webhook stub 不记录
  body。验收覆盖 busy 超过 grant TTL、撤权、Worker/Gateway 崩溃、迟到结果、unknown 单次调用、预算
  对账、fresh `67/0067`；Playwright 实测 1280x720 与 390x844 截图、零预取和 DOM 清除。
- 日志/数据库/证据扫描 Provider Key、Token、Prompt、文件正文、assistant Response canary 命中为 0。

## 18. 代码修改计划

1. Shared：RBAC revision 10、Batch 加密/合同、预算 hold 聚合、配置和错误目录。
2. Migration：0067 up/down、角色矩阵、Batch 表/trigger/outbox。
3. Control：`batch_jobs` models/routes/service、内部 actor 授权、文件准入适配、outbox。
4. Worker：公平 claim、lease/heartbeat、operation render、grant、Gateway、结果/重试/汇总。
5. Gateway：internal Batch token/grant、实时优先 gate、可信上下文、预算排除与结果持久化。
6. Console：PAGE-104 client/state/render/event wiring/styles/navigation。
7. Compose/env/API/目录/README/testing/验收器。
8. 先跑受影响专项；收口再跑必要相邻回归、fresh migration、真实 Runtime/Browser 与安全审计。

## 19. 编码前自审

### 19.1 第一轮：预算、Gateway 与崩溃恢复

状态：**已完成并修正（2026-08-07）**。

本轮由独立审查者按现有 Budget Guard、RequestLog/Usage/FinOps 事务、RoutingAttempt、Rate Limit、
内部 Chat grant 和 Compose 边界逐项复核，发现并修正：

1. **Job hold 不能伪装成 request reservation**：普通 reservation 由同 request 的正式 Usage 接管；
   用 Job request ID 承载整 Job 会让任一 attempt 错误释放全部额度。新增独立 hold/allocation，Gateway
   使用 allocation coverage，Usage 事务按 attempt 结算。
2. **自身 hold 排除 + 新建普通 reservation 仍会制造双重语义**：冻结为 Batch attempt 不创建普通
   reservation；Budget Guard 复核 allocation 与当前主备估算，其他实时/Job 占用照常计入。
3. **`alert_only` 会违背权威验收**：PRD 明确“超预算不能提交”，故 Batch 创建对所有适用规则硬拒绝，
   不沿用普通实时请求的 alert-only 放行语义。
4. **业务日跨越未定义**：hold 绑定上海日；新日期执行前释放旧日未用额度并按剩余最坏成本重新准入。
5. **仅看 RequestLog success 不能恢复 Response**：成功但认证 result 缺失固定
   `batch.result_unavailable`，禁止重调；started/timeout/传输中断按 outcome unknown。
6. **按 HTTP 状态猜 retry-safe 不可靠**：只有 Gateway 持久化的 execution outcome、charge state 和
   routing attempt 共同证明未调用/已知失败时才允许新 attempt。
7. **Batch 低阈值若先 INCR 会反而吃掉实时额度**：Rate Limit 增加原子 try-increment lane；拒绝零计数，
   并保留实时容量。多副本要求 Redis 原子实现。
8. **直接扩展三个 nullable trusted 参数会混用身份**：引入带来源枚举的严格 invocation 对象。
9. **正文 wire 不必要地扩大暴露面**：Worker 只持久化 owner-bound 密文，内部 wire 仅发送 grant，
   Gateway 解密后进入既有全治理链。

第一轮修正后，预算占用、实际结算和 unknown 对账分别有独立事实，实时预算不会被 Batch 双计或静默
释放；Provider outcome 无法证明时以可追踪失败终结，而不是重复调用。

### 19.2 第二轮：tenant owner、lease、预算与页面隐私

状态：**已完成并修正（2026-08-07）**。第二位独立审查者逐项核对当前 Chat grant、文件权限、RBAC
迁移、outbox 和 Console 生命周期；预算/Gateway 审查者又对照 Rate Limit、Usage spool 和路由链复核。
发现并修正：

1. 任务/结果读取与有状态取消补齐 `file.read`/`batch.read`，撤权后不能保留文件派生结果访问。
2. grant 绑定 lease token/generation，消费事务实时复核 actor、RBAC、取消、deadline 和完整 owner 图。
3. 增加 expired-lease reaper 与 Gateway 写入硬截止；传输中断不再直接判 unknown。
4. prepared attempt 支持顺序 grant generation；busy/未消费过期不计 Provider attempt。
5. attempt 补齐独立域 AES-GCM request payload、key version、AAD、大小上限和分域 HMAC。
6. Job 持久 deadline；取消/撤权/到期结果通过单向 discard CAS 真正清除密文但保留账务事实。
7. Worker claim 在数据库同时执行全局/tenant 并发门禁；本 PRD 对 Batch 明确只支持单 Gateway 进程。
8. RBAC 迁移冻结 62/166 -> 64/173，只扩展精确 canonical 角色，自定义矩阵不静默获权。
9. down guard 覆盖 allocations、scheduler、Webhook 外部事实和授权事件，不能破坏引用或客户事实。
10. possible allocation 增加自动与人工幂等对账状态机，API/Webhook 不再把 posted cost 冒充最终值。
11. PAGE-104 使用精确 result ticket、错误码分类、先清理后中止和双视口可执行验收。
12. RPM/TPM/concurrency 改为一次原子多维准入，并冻结 25% 且每维至少 1 的实时保留算法。
13. hold remaining、active allocation、posted 接管与 possible 的金额生命周期改为不重不漏的聚合合同。
14. Usage spool 升级安全 v6，direct/replay 共用 allocation 结算且都禁止保存 Prompt/Response。
15. Policy 动态路由必须逐项匹配冻结 route plan，Batch coverage 进入 trusted invocation 而非认证对象。
16. 补齐 worker/gate/capacity/结果字节上限默认值、密钥域隔离和内部固定错误折叠。

Outbox 的不可变 source、同事务 pending intent、稳定 event ID、空订阅快照和 90 秒 CAS 语义通过本轮
复核；另补 source 唯一约束和 completed actor 归属。两轮发现均已写回正文并复核无冲突，编码基线冻结。

## 20. 实现结果与实现后复核

状态：**已完成并通过最终验收（2026-08-08）**。本节记录编码后按真实数据库、真实进程和真实页面
重新检查得到的修正，不改写第 19 节已经冻结的编码前两轮自审。

### 20.1 第一轮实现后复核：状态机、租约与恢复

第一轮按 Scheduler、Worker、Gateway、预算和 Webhook 的持久 owner 链逐项复核并修正：

1. 同秒 tenant 公平调度必须同时使用 dispatch revision，连续领取稳定轮转，不能退化为 tenant ID 排序。
2. Reaper 只有在 Provider outcome 无法证明时才能写 `outcome_unknown`；明确不可重试失败必须保留原错误，
   无 Attempt 的过期 lease 首次恢复也必须先尊重取消和 Job deadline。
3. 独立 Worker 只持有数据库、Gateway Batch Token/加密密钥和文件存储能力，不持有 Webhook 或 Provider
   Secret；Webhook 由 Control lifespan scheduler 从同事务 source/outbox 恢复。
4. Webhook pending resume 必须从同 tenant 的不可变 `batch_job_events` 恢复并严格比对
   `source_request_id`、event/type/job/actor/trace/timestamp/canonical payload，身份漂移固定失败关闭。
5. Gateway 统一错误解析同时接受顶层稳定 `code`，真实 `429 batch.gateway_busy` 不得被 Worker 误判为
   不可恢复拒绝。

### 20.2 第二轮实现后复核：跨进程合同与真实 Runtime

第二轮以空库 `67/0067`、真实 Control/Gateway/Worker、固定回环 Provider/Webhook 和 PAGE-104 浏览器执行
验收，关闭了两个只有跨进程运行才暴露的问题：

1. Control 从 SQLite 读取的单价曾冻结为 `"0.1"/"0.2"`，Gateway 候选模型却按另一精度规范化后做
   exact comparison，导致有效 grant 返回 `403 batch.execution_invalid`。现由 Shared 提供唯一
   `canonical_batch_price`，沿用 Gateway 的 12 位价格精度上限并输出固定 12 位；Control 冻结与 Gateway
   校验共同复用，真实 Control 生成的 route plan 直接进入 Gateway validator 的回归已通过。
2. Runtime 验收器曾读取不存在的 `usage_ledger.trace_id`。产品实际通过
   `usage_ledger.routing_attempt_id -> routing_attempts.trace_id` 保持链路；验收器现按真实 owner 关系 JOIN，
   并以刻意不含 `trace_id` 列的 SQLite 合同测试冻结该路径。
3. 上述 JOIN 最初只验证 ID 与 trace 归属，没有同时冻结被关联路由的终态。验收器现进一步要求
   `routing_attempts.status=succeeded` 且 `usage_status=recorded`；正例和两类坏终态合同均已通过，并在补强后
   重新执行完整 Runtime，避免旧证据对损坏或未完成的账务链误报通过。

### 20.3 最终可运行结论

- Shared Batch 合同、RBAC revision 10、0067、Control Batch API、独立 Worker、Gateway internal Batch、
  预算 hold/allocation、Usage/FinOps、Webhook source/outbox、Compose 和 PAGE-104 已落地。
- 正式 Runtime 证据位于
  `.artifacts/prd074-runtime/prd074_20260808T105000Z_runtime_final2/`：`status=passed`、`67/0067`、
  `integrity_check=ok`、外键违规 0；实时优先、grant 轮换、Worker 过期 lease 恢复、取消、撤权、
  RequestLog/路由/Usage/FinOps/成本、Control 停机后的 outbox 恢复和全部自建进程清理均通过。
- Runtime 扫描 23 个数据库/对象/日志文件、14 个服务日志，Provider Key、Token、Prompt、文件正文和
  assistant Response 三编码明文命中均为 0；证据保持 `all_108_prds_complete=false`。
- PAGE-104 证据位于 `.artifacts/prd074-browser/prd074_20260808T094300Z_page104/`；Chromium
  `1280x720`、`390x844` 列表和移动创建弹窗均为横向溢出 0、重叠 0、文本溢出 0、非空截图，结果
  清理、迟到响应隔离和未授权入口裁剪均通过。

Excel 总表、M2/M3 交付、PAGE-105、API、数据对象、Webhook 和 TC-0149/0150 交叉确认：PRD-074
完成后的下一条权威未完成项是
`PRD-075 | M2-企业增强 | P2 | 模型评测 | 模型 A/B 测试与质量评估`。全部 108 条总体目标仍在进行，
`all_108_prds_complete=false`。
