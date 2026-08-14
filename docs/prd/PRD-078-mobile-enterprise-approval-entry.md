# PRD-078 移动端/企微钉钉审批入口设计

状态：**已完成**（2026-08-14）。本文件已完成两轮编码前自审、实现、两轮实现后自审与修正、专项和
必要回归、0071 实迁移、Runtime、Browser、文档和提交前安全门禁。PRD-077 已完成且未重复实现；本条
只关闭 PRD-078，
`all_108_prds_complete=false`。

## 1. 权威范围与交付边界

### 1.1 权威来源

- Excel `01_PRD总表` 第 79 行与 `03_M2-M3扩展交付` 第 15 行共同确认：
  `PRD-078 | M2-企业增强 | P2 | 扩展层 | 移动审批 | 移动端/企微钉钉审批入口`。
- 角色是安全、财务、管理员；用户故事是在手机上审批预算超限与高风险操作。
- PAGE-108 是审批中心，PAGE-109 是移动卡片。
- Excel 的 `POST /approvals/{id}/approve|reject` 是被接口清单机械拆行后的简写。本设计恢复为两个真实
  动作端点 `POST /approvals/{approval_id}/approve` 与
  `POST /approvals/{approval_id}/reject`，绝不创建包含字面量 `|` 的路由。
- 数据对象为 tenant-qualified `ApprovalTask`；事件为 `approval.created`、`approval.timeout`；
  TC-0155/0156 要求超时自动拒绝、高风险后端二次验证、结果回写、审计、统一错误与敏感信息保护。

### 1.2 v1 交付

1. Control API 提供当前租户审批列表、详情、二次验证以及 approve/reject 动作。
2. 有界内部生产者入口创建审批任务，只接受本设计登记的目标类型与动作，不接受任意 URL、SQL、函数名或
   任意 JSON 回调。
3. 首批真实目标适配器：
   - `budget_block_release`：批准后释放指定租户预算规则的 active `BudgetBlock`；拒绝/超时保持阻断。
   - `session_risk_action`：批准后执行由任务冻结的高风险会话动作意图；拒绝/超时保持原风险状态。
4. PAGE-108 审批中心提供租户内筛选、任务详情、审计时间线、倒计时、批准/拒绝与二次验证。
5. PAGE-109 移动卡片是同一 Console 的窄屏、单任务视图，不是独立移动 App。它可由通知中的任务 ID 打开，
   但仍必须登录 MetaHub，并重新经过 Session、实时 RBAC、任务 owner/version 与 step-up 校验。
6. `approval.created` 与 `approval.timeout` 复用现有 Webhook Endpoint、签名、投递快照、重试、死信和恢复
   机制。事件 payload 只含卡片渲染所需的脱敏枚举、ID 与时间，不含供应商凭据或目标正文。
7. 调度器使用数据库时钟、tenant 公平租约和任务 CAS，把到期 `pending` 任务自动收敛为
   `rejected/timeout`，并原子形成 Decision 审计、事件 source 与结果回写事实。

### 1.3 明确不做

- 不开发 iOS/Android、小程序或完整移动 App；不接管企业微信、钉钉、飞书通讯录、组织架构或 OAuth 生态。
- 不实现通用工作流、任意审批链、多级会签、转交、加签、撤回、任意 URL/JSON 回调或动态函数执行。
- 不把已有 PolicySnapshot 的 `approval_status='not_required'` 改造成审批状态；既有快照不可变合同保持不变。
- 不把所有既有写 API 自动改成审批制，也不在 Gateway 高频请求热路径同步调用 Control。
- 不把 OpenRouter、硅基流动新增为默认上游。默认上游仍是智谱、通义、月之暗面、文心、火山、混元、
  DeepSeek 官方/云服务及客户本地 OpenAI-compatible Endpoint。
- 不允许外部平台回调、前端隐藏按钮、卡片中携带的字段或客户端提交的 `tenant_id` 成为授权依据。

## 2. 总体架构

```text
bounded producer                 Control Plane                    existing Webhook delivery
  internal token  ->  ApprovalTask + created source/outbox  ->  WeCom/DingTalk/Feishu endpoint
                               |                                      |
                               v                                      v
                    PAGE-108 / PAGE-109 <- safe card id/summary only
                               |
                    Session + live RBAC + task CAS
                               |
              low risk: approve/reject directly
              high risk: password re-auth -> one-use step-up ticket
                               |
       ApprovalDecision + target writeback + event source/outbox
                               |
                timeout scheduler competes on the same pending CAS
```

所有业务状态都在 Control SQLite 中按 `tenant_id` 隔离。网络投递只发生在提交后的 outbox worker 中，
不得在 `BEGIN IMMEDIATE` 内调用企微、钉钉、飞书或任意 Webhook Endpoint。

## 3. 权限、身份与租户隔离

### 3.1 RBAC revision 14 与目标域双重校验

新增两个权限并把 Shared catalog revision 从 13 提升到 14：

| code | 风险 | 默认角色 | 用途 |
| --- | --- | --- | --- |
| `approval.read` | read | admin、finance_admin、security_admin | 进入 PAGE-108/109 并读取有权目标的安全摘要 |
| `approval.decide` | critical | admin、finance_admin、security_admin | 提交 approve/reject；仍不能替代目标域写权限 |

不能只复用 `billing.budget.write` 或 `session_risk.manage`：既有目标管理员不一定就是组织指定的审批人，租户
必须能单独撤销审批入口而不同时撤销日常目标管理能力。反过来，审批权限也不能越过目标域授权。内部创建
只接受服务身份，不新增没有用户入口的 `approval.create`。

每个公开请求先从 Bearer Session 恢复可信 tenant/actor，再从当前租户数据库读取 revision-14 矩阵；列表、
详情、step-up 和决策事务均继续按目标域复核：

| target type | 读取任务还需 | approve 还需 | reject 还需 |
| --- | --- | --- | --- |
| `budget_block_release` | `billing.budget.read` | `billing.budget.write` | `billing.budget.write` |
| `session_risk_action` | `session_risk.read` | `session_risk.manage` | `session_risk.manage` |

因此 finance_admin 默认只能处理预算任务，security_admin 默认只能处理会话风险任务，admin 可处理两类；
developer、viewer、ops_admin 默认没有审批 grant。自定义矩阵实时生效，权限判断只认数据库 permission
code，不按角色名补权。PAGE-108/109 导航要求 `approval.read`；每一行还要求目标 read，每个动作还要求
`approval.decide` 与目标 write。

### 3.2 外部平台不是授权根

- v1 的企微/钉钉/飞书仅是现有签名 Webhook 的通知接收端和 MetaHub 页面入口，不提供直接批准回调。
- 卡片 payload 不携带 Bearer Token、step-up ticket、密码、Webhook Secret、签名 Secret 或可执行目标正文。
- 卡片可携带 `approval_id` 和 Console 内部入口标识；用户打开后必须在 MetaHub 登录。跨租户或无权任务与
  不存在任务使用同形 404，防止任务枚举。
- 若未来增加原生平台回调，必须另立 PRD 冻结平台签名、timestamp/nonce、防重放、可信账号映射与重新
  step-up；不得在本条实现中预留一个宽松的“万能 callback”。

## 4. ApprovalTask 状态机

### 4.1 状态与终态

- `pending`：可被用户 approve/reject 或 scheduler timeout。
- `approved`：目标的 SQLite 权威事实已与批准动作在同一事务成功；不可再变化。若目标还有 Redis 等派生
  投影，`writeback_status` 可以暂时为 pending，但持久恢复事实已经存在。
- `rejected`：拒绝或超时自动拒绝；`resolution_reason` 区分 `user_rejected` 与 `timeout`；不可再变化。

任务只允许 `pending -> approved|rejected`。approve、reject、timeout 都执行：

```sql
UPDATE approval_tasks
SET status=?, state_revision=state_revision+1, ...
WHERE tenant_id=? AND approval_task_id=?
  AND status='pending' AND state_revision=?
  AND unixepoch(expires_at)>unixepoch(?) -- 仅人工动作
```

`changes()!=1` 时重读同租户任务：已终态返回 `approval.state_conflict`；已经到期由当前请求在同一事务执行
timeout 收敛后返回 `approval.expired`。前端为一次用户意图生成稳定 `Idempotency-Key`；同 actor、同 task、
同 action、同 revision、同 reason 的精确重放返回既有决策并标记 `idempotent_replay=true`，相反决策或任一
意图字段变化返回 `approval.idempotency_conflict`，绝不把相反决策伪装成成功重放。

### 4.2 任务创建

内部入口：`POST /internal/tenants/{tenant_id}/approvals`。

- 在读取正文前要求唯一 `X-Internal-Token`，拒绝与 Bearer 混用；路径 tenant 必须存在且 active。
- exact JSON：`source_type`、`source_id`、`target_type`、`target_id`、`target_action`、
  `target_reason_code`、`expires_in_seconds`、`idempotency_key`。request/trace 来自统一 middleware，
  target version、fingerprint、risk 和 required permission 全部由服务端 registry 派生。
- TTL 300..86400 秒。`budget_block_release` 固定为 standard，`session_risk_action` 固定为 high；调用方不能
  把高风险目标降级成无需 step-up 的任务。
- `target_type/target_action` 必须命中固定 registry：
  - `budget_block_release/release`
  - `session_risk_action/freeze_session|freeze_virtual_key|dismiss|release_session|release_virtual_key`
- producer 传入的目标 ID 始终按路径 tenant 联合查询；服务端重读目标、状态和 version，并生成
  `target_fingerprint`，不相信 producer 自报的角色、显示名、金额或风险正文。
- 同一 tenant/source_type/source_id/target_action 的 active 任务唯一；幂等键只以独立 HMAC 保存。
- 创建任务、`approval.created` source/outbox、Webhook delivery 快照和创建审计在同一事务提交。

### 4.3 目标回写

本条两个目标与 Approval 表在同一 SQLite，因此批准事务内直接调用只接受现有连接的固定适配器：

- 预算任务的 `target_id` 是 `budget_rule_id`，`source_id` 可关联触发任务的 active `budget_block_id`。批准时
  重读 `budget_rules` 与该规则的 active blocks，校验同租户 owner、rule `updated_at` 和冻结 fingerprint，
  调用 `release_active_budget_blocks(..., reason='approval_approved')`；只释放该规则的 active blocks，不会
  根据客户端字段选择其他 scope。
- 会话风险：把现有会话风险 `_execute_action` 的纯事务步骤提取为可复用适配器；重读 score owner、
  `state_revision` 和目标 action 合法性，生成既有 append-only action/event/freeze 事实。Redis 对账 intent
  作为持久 `approval_writebacks` 记录在同一事务，scheduler 在提交后执行并可恢复；在对账完成前任务公开
  `writeback_status='pending'`，但审批决策不可回滚或重复执行。
- 拒绝与超时的回写是固定 `no_effect`：目标保持原状态，但仍写一条已完成 writeback 事实，证明不是漏执行。

目标在审批期间已被其他流程修改时，人工 approve 必须失败为 `approval.target_conflict`，任务保持 pending，
允许用户刷新后重新创建新任务；scheduler timeout 仍可把任务拒绝终结。不得根据旧快照覆盖新目标事实。

## 5. 后端二次验证

审批票据、幂等键、意图、授权指纹和列表游标使用独立、分域派生的审批 HMAC 根密钥：
`METAHUB_APPROVAL_HMAC_VERSION/SECRET`，轮换窗口可同时配置
`METAHUB_APPROVAL_PREVIOUS_HMAC_VERSION/SECRET`。Secret 在 `AppConfig` 中必须
`exclude=True/repr=False`，至少 32 bytes，current/previous 版本为不同正整数且 previous 必须更小；不得
复用 Auth、Provider、Webhook、内部服务、Cache、Session Risk、Batch、File 或 Prompt 密钥。dev/test 可用
固定且明确标注为本地用途的默认值，prod 必须显式配置并通过密钥隔离校验，否则审批入口与 worker 失败关闭。
数据库只保存带 key version 的摘要；每种用途使用不同 domain，previous 仅验证保留期内旧摘要，新写入永远
使用 current。

高风险任务的二次确认必须由后端执行，不能只显示浏览器 confirm/modal。

### 5.1 获取票据

`POST /approvals/{approval_id}/step-up`，exact JSON 仅允许 `action` 与 `password`：

1. 先验证当前 Session、`approval.read/decide` 与目标域读写权限，再有界读取正文；读取 active Session、用户密码哈希、任务 owner/
   status/revision/TTL、目标 owner/version 和 authorization fingerprint 后结束只读事务。
2. 在不持有 SQLite 写锁时使用现有密码哈希验证当前用户密码，避免 PBKDF2 放大写锁占用。失败只返回
   `approval.step_up_invalid`，不区分账号、锁定、密码或任务细节；失败计数在独立短写事务按前述 owner CAS
   提交后再返回错误，密码永不进入日志、Audit、Webhook 或异常。
3. 成功后才开启短 `BEGIN IMMEDIATE`，再次重读 Session、revision-14 矩阵、任务和目标；只在全部快照仍
   一致时生成至少 256 bit 随机票据。
4. 响应明文仅返回一次；数据库只保存使用独立 domain 的 HMAC，绑定
   `tenant_id/user_id/session_id/approval_id/action/task_revision/authorization_fingerprint`。
5. TTL 120 秒、单次消费；同一 actor/task/action 同时最多一张 active 票据。签发新票据会废止旧票据。
6. 每 task/actor 5 次失败后 15 分钟内拒绝继续验证；只保存计数和时间，不保存密码或其摘要。

响应仅含 `step_up_token`、`expires_at`、`request_id`、`trace_id`，并设置
`Cache-Control: no-store, private`、`Pragma: no-cache`、`Vary: Authorization`。

### 5.2 消费票据

- high-risk approve/reject 正文必须含 `expected_revision`、`reason_code`、`step_up_token`；standard 任务禁止
  提交票据，防止客户端混淆两套合同。
- 决策事务先实时复核 Session/RBAC/task/target，再常量时间比较 HMAC，检查全部绑定、未过期、未消费；
  票据消费、任务 CAS、Decision 审计、目标回写和 event source 同事务提交。
- 任一失败回滚消费；成功后 token 永远不能再次使用。Session 被撤销、用户被禁用、权限 revision 或
  authorization fingerprint 改变时，旧票据立即失效。
- 统一 middleware 对内部创建、step-up、approve、reject 的 POST 一律忽略客户端 request/trace header 并
  生成服务端 ID，防止攻击者把密码、ticket 或幂等凭据复制到响应头、访问日志或持久审计链。

## 6. 数据模型与迁移 0071

`0071_mobile_enterprise_approval_entry.sql/.down.sql` 将最高迁移提升为 `71/0071`，RBAC catalog revision
提升为 14。所有表、索引、触发器和外键均 tenant-qualified；下迁移在存在任何审批业务、决策、票据、
writeback、事件 source/outbox 或未终态 delivery 时失败关闭，不删除审计链。

### 6.1 `approval_tasks`

| 字段 | 合同 |
| --- | --- |
| `approval_task_id` | `apr_` + 32 hex，主键 |
| `tenant_id` | 必填 FK，所有查询首条件 |
| `source_type/source_id` | 固定生产者类型与不透明来源 ID |
| `target_type/target_id/target_action` | 固定 registry，不允许 URL/JSON/函数名 |
| `target_version/target_fingerprint` | 服务端创建时冻结；version 是目标域 canonical 文本，批准时 CAS 复核 |
| `risk_level` | `standard|high` |
| `status` | `pending|approved|rejected` |
| `resolution_reason` | `approved|user_rejected|timeout` 或 NULL |
| `state_revision` | 从 1 单调递增 |
| `writeback_status` | `pending|completed|failed` |
| `required_permission_code` | 服务端 registry 冻结的目标权限 |
| `created_by_type/created_by_id` | `service|user` 与可信 owner |
| `decided_by_user_id/decided_at` | 系统 timeout 的 user 为空 |
| `expires_at` | canonical UTC 秒，必须晚于 created_at |
| `source_request_id/source_trace_id` | 创建链路 |
| `decision_request_id/decision_trace_id` | 终态链路 |
| `created_at/updated_at` | canonical UTC 秒 |

唯一约束防止同一来源动作出现两个 pending 任务；trigger 禁止 tenant、source、target、risk、created owner、
created time 修改，只允许合法 CAS 终态和 writeback 进展。

### 6.2 其余表

- `approval_decisions`：append-only，保存 decision、reason_code、actor_type、actor_id、revision before/after、
  step-up 是否使用、目标回写状态、request/trace/time；不保存备注正文、密码或票据。
- `approval_step_up_challenges`：保存 ticket HMAC、绑定 owner、失败计数、expires/consumed/revoked 时间；
  trigger 禁止 owner 与摘要变更，只允许单向消费/废止。
- `approval_action_replays`：保存 24 小时幂等键 HMAC、intent HMAC 与 decision owner；不保存原始 key。
- `approval_writebacks`：固定 adapter kind、status、attempt/generation/lease owner/token、next attempt、固定
  error code；会话风险 Redis 对账还必须保存本次事务生成的 `target_fact_id` 与 `target_revision`，scheduler
  只能按 tenant/task/fact/revision 精确重建，禁止读取“最新 action/score”猜测本次批准事实。预算/拒绝/超时
  在提交事务中直接 completed，且不保存目标正文。
- `approval_events`：append-only immutable source，事件类型仅 `approval.created|approval.timeout`，
  payload 为 exact 安全 JSON。
- `approval_event_outbox`：pending/processing/delivered/failed、90 秒 lease、generation/token/owner CAS、
  5/30/300 秒退避、最多 10 次，保存 Webhook delivery snapshot marker。
- `approval_scheduler_tenants`：tenant 公平轮询与 lease；一个 claim 最多终结一条到期任务和恢复一条 writeback/
  event，避免单租户饥饿其他租户。

### 6.3 审批审计 owner

`approval_decisions` 是本条的权威审批审计，不强行把 scheduler 塞进既有只允许真实用户 actor 的 AuditLog：

- 用户 approve/reject 的 actor_type=`user`，actor_id 必须由 tenant-qualified users 外键和 insert trigger
  证明等于当前 Session 用户。
- timeout 的 actor_type=`system`、actor_id=`approval-timeout-scheduler`，trigger 只允许 timeout 使用该固定值；
  不得借用任意管理员身份。
- task、Decision、目标 SQLite 回写和 event source 由复合 owner trigger 证明属于同一 tenant、同一 revision
  与同一秒级终态事务。PAGE-108/109 的审计时间线只读该 append-only 表。
- user 决策可额外发送结构化安全日志，但不改写既有 `audit_logs` canonical schema。摘要只含 status/revision、
  risk、target type、writeback status 和时间，不含金额、会话 HMAC、Endpoint、密码、Token、Prompt、
  Response 或自由备注。

## 7. API 合同

所有公开响应含 `request_id` 与 `trace_id`；所有审批响应使用 private no-store。认证/权限依赖在 query、body、
路径业务解析前执行；跨租户与不存在同形 404。

### 7.1 读取

- `GET /approvals?status=pending|approved|rejected|all&target_type=...&limit=...&cursor=...`
  - keyset 分页，默认 pending；只返回同时满足 `approval.read` 与目标域读权限的任务。
- `GET /approvals/{approval_id}`
  - 返回任务、Decision 时间线、`allowed_actions`、`requires_step_up` 与安全目标摘要；不返回 HMAC、lease、
    原始 source payload、目标正文或其他租户存在性。

### 7.2 二次验证与决策

- `POST /approvals/{approval_id}/step-up`
  - high risk only；正文 `{ "action": "approve|reject", "password": "..." }`。
- `POST /approvals/{approval_id}/approve`
- `POST /approvals/{approval_id}/reject`
  - 必须有唯一 `Idempotency-Key`。
  - standard exact body：`expected_revision`、`reason_code`。
  - high exact body：再加 `step_up_token`。
  - `reason_code` 只允许固定枚举；v1 不接收自由审批备注，避免敏感正文进入审计、日志或卡片证据。

### 7.3 内部创建

- `POST /internal/tenants/{tenant_id}/approvals`
  - 唯一 `X-Internal-Token`，与 Bearer 互斥；不在 CORS 中作为普通浏览器入口开放。
  - 返回已创建或精确幂等恢复的安全任务摘要和 request/trace ID。

## 8. 统一错误码

| code | HTTP | 含义 |
| --- | ---: | --- |
| `approval.input_invalid` | 400 | header/query/body/ID/枚举/结构非法 |
| `approval.idempotency_required` | 400 | 缺失或歧义幂等键 |
| `approval.not_found` | 404 | 不存在、跨租户或无目标域可见性 |
| `approval.state_conflict` | 409 | task revision/status 已变化 |
| `approval.idempotency_conflict` | 409 | 同 key 对应不同意图 |
| `approval.target_conflict` | 409 | 目标状态/revision/fingerprint 已漂移 |
| `approval.expired` | 409 | 人工动作到达时任务已由 timeout 收敛 |
| `approval.step_up_required` | 403 | high-risk 动作缺少票据 |
| `approval.step_up_invalid` | 403 | 密码或票据无效、过期、已消费、owner 不匹配 |
| `approval.step_up_rate_limited` | 429 | 二次验证失败次数超限 |
| `approval.capacity_full` | 429 | tenant pending task 上限达到 |
| `approval.unavailable` | 503 | 数据库/调度/事务事实不可证明 |
| `approval.writeback_unavailable` | 503 | 持久回写暂未完成，可由 scheduler 恢复 |
| `approval.event_unavailable` | 503 | 事件/outbox 恢复失败 |

错误信封沿用 Shared `ApiError`，不得拼接 SQL、数据库路径、Endpoint、供应商响应、密码、Token、Prompt、
Response、审批目标正文或外部平台签名。

## 9. Webhook 卡片事件

### 9.1 `approval.created`

exact payload v1：

```json
{
  "schema_version": 1,
  "approval_id": "apr_...",
  "status": "pending",
  "target_type": "budget_block_release",
  "target_action": "release",
  "risk_level": "standard",
  "requires_step_up": false,
  "expires_at": "2026-08-12T12:00:00Z",
  "entry": "approval-card"
}
```

### 9.2 `approval.timeout`

exact payload v1：`schema_version`、`approval_id`、`status='rejected'`、
`resolution_reason='timeout'`、`target_type`、`target_action`、`decided_at`。

只实现 Excel 冻结的 `approval.created` 与 `approval.timeout`，不额外发明 decided 事件。任何 payload 都不
包含金额、Budget 名称、会话 scope、Virtual Key、用户邮箱、密码、step-up token、Webhook Secret、
Endpoint URL、Prompt、Response 或自由文本。现有 Webhook HMAC 签名、SSRF、重定向、重试和死信合同保持不变。

## 10. PAGE-108 与 PAGE-109

### 10.1 PAGE-108 审批中心

- 导航只在后端 `/console/navigation` 返回 `approval.read` 时出现；页面加载仍调用审批 API，由后端按每条
  目标再次授权。
- 顶部状态筛选与目标类型筛选；列表显示固定类型、动作、风险、状态、剩余时间和 writeback 状态。
- 详情展示安全目标摘要、创建/决策时间线和固定 reason code；不显示敏感正文。
- approve/reject 使用图标加明确命令文本；动作前展示确认对话框。high risk 对话框要求输入当前密码换取后端
  step-up ticket，再立即提交目标动作；密码与 ticket 只驻留本次闭包内存，不写 URL、Storage 或页面日志。
- 409 后清空 step-up 与 action owner，刷新权威任务；401/403、撤权、切租户、离页、pagehide、登出均
  abort read/action、推进 generation 并清空 DOM/内存。

### 10.2 PAGE-109 移动卡片

- 使用同一 `approvals` 导航 owner，入口由受控 URL query `approval_id` 选择单任务卡片；解析只接受
  `apr_[0-9a-f]{32}`，非法值在发请求前丢弃且不写页面事件。
- 390x844 首屏显示目标类型、风险、状态、倒计时和 approve/reject；按钮保持稳定尺寸，正文换行，无横向
  滚动、遮挡或嵌套卡片。桌面入口仍渲染为审批中心中的单任务聚焦视图。
- URL 中不放 Token、step-up ticket、密码或一次性外部凭据；任务 ID 不是授权，后端跨租户/无权同形 404。
- 卡片批准后显示已提交终态与 writeback 状态；离页立即销毁密码、ticket、任务详情与迟到回调 owner。

## 11. 调度、恢复与资源边界

- lifespan 安装 `approval` scheduler；默认短轮询，支持配置禁用、间隔、lease 与 batch size 上限。
- 每次先 tenant lease，再按 `expires_at, approval_task_id` 取一条到期 pending 任务；timeout 事务重读目标权限
  不需要用户 RBAC，但必须验证 tenant owner、task revision 和 scheduler lease。
- timeout 与用户动作竞争同一 task CAS；只有一个 winner 能创建唯一 Decision/Audit/Event/Writeback。
- event outbox 与会话风险 Redis writeback 都使用 generation/token/owner/lease CAS。过期 lease 可回收；迟到
  worker 在网络或 Redis 前复核 active lease，不能覆盖新 owner 终态。
- Webhook 网络调用在事务外；source/outbox 第十次可重试失败后原子失败关闭其 pending delivery，不能让坏
  tenant 饥饿其他 tenant。
- tenant pending 上限 1000、列表上限 100、内部 body 16 KiB、step-up body 4 KiB、决策 body 4 KiB；
  exact JSON、重复 header/key、非 UTF-8、非有限数、深度/节点、Content-Length 与分块实际长度均受限。
- 日志只记录事件名、tenant/task/actor ID、request/trace ID、固定状态/错误码和耗时桶；不记录 payload 正文。

## 12. 测试计划

### 12.1 Shared 与迁移

- RBAC revision 14、`approval.read/decide` 元数据和 admin/finance/security 默认 grant；自定义矩阵保真；
  developer/viewer/ops 无默认 grant。
- 0071 fresh/repeat、0070->0071、down/re-up；自定义矩阵保真；全部 table/index/trigger canonical hash；业务事实
  down guard；同一空库 `71 -> 71 -> 70 -> 71` 四阶段 integrity ok、FK 0。
- task/decision/challenge/replay/writeback/event/outbox/scheduler 的 tenant 复合 FK、状态 trigger、append-only、
  CAS、重复/跨租户/非法动态类型和 Audit owner。

### 12.2 Control API

- 内部创建两个目标的主流程、精确幂等与冲突、target registry、active target/revision、容量与 tenant owner。
- admin/finance/security 的列表与详情目标裁剪；developer/viewer/ops/匿名拒绝；动态撤权；跨租户同形 404。
- approve/reject/timeout 三方并发只产生一个终态、Decision 审计、writeback 和 event；过期人工请求不能赢。
- standard 与 high-risk 正文互斥；密码正确/错误/限速；ticket 全绑定、TTL、单次、撤 Session、撤权、换任务、
  换 action、换 revision、并发双消费。
- 预算批准真实释放指定 block；拒绝/timeout 不释放；目标漂移保持 pending 并返回 409。
- 会话风险批准真实写 action/freeze/event，并验证 Redis 对账成功、故障持久 pending、重启恢复与迟到 owner。
- 所有成功/失败响应含 request_id/trace_id；错误、日志、DB、event、outbox 无密码、票据、Token、Prompt、
  Response、Endpoint、供应商正文或自由备注。

### 12.3 Webhook 与 scheduler

- created/timeout exact payload、签名、冻结订阅者快照、空订阅 marker、停服恢复、退避、最大 attempt、死信、
  stale lease reclaim、owner/token/generation/attempt 篡改、坏 tenant 不饥饿其他 tenant。
- 网络调用不在事务中；超时提交后 outbox 丢失/重启仍恢复；第十次失败终结 source 与 pending delivery。
- scheduler 多实例 tenant lease、数据库时钟、用户动作竞态、重复 tick、停服、取消和 lifespan teardown。

### 12.4 Console 与 Browser

- 后端导航 + API 双授权；三目标角色、撤权、401/403/404/409/429/503；无权限时无 DOM/预取。
- PAGE-108 筛选、seek 分页、详情、确认对话框、step-up、决策、倒计时、writeback 状态。
- PAGE-109 受控 task ID、直接打开、登录后恢复、移动主流程；URL/Storage/DOM/console/network 不含密码/ticket。
- owner/generation/Abort：切任务、切筛选、离页、切租户、撤权、迟到 200/401、重复点击、登出/pagehide。
- 实际 `1280x720` 与 `390x844` Chrome：非空像素、无横向溢出、文本溢出、重叠，外部请求只命中受控
  loopback；截图不包含密码、ticket 或敏感目标正文。

### 12.5 必要回归与最终门禁

- 受影响 Python/Console 专项先运行；完成门禁再运行 Auth、RBAC、Audit、Webhook、Budget、SessionRisk、
  Console navigation 与 Console 全量等必要回归，不反复运行无关根级全量。
- Ruff/format、`py_compile`、Console lint、中文注释门禁、`git diff --check`、迁移编号/正反对完整性。
- 实际 Runtime：空库迁移、Control 启动、两目标主流程、并发 timeout、outbox/Redis 恢复、三角色、统一错误、
  owner 进程/端口/SQLite sidecar 清理。
- 提交候选和证据执行敏感信息、多编码 canary、禁止 DB/WAL/SHM/journal/log/profile/cache/大文件审计。
- 更新 README、目录说明、API 文档与 `docs/testing/PRD-078-tests.md` 后提交并推送 `origin/main`；只关闭
  PRD-078，不启动 PRD-079，保持 `all_108_prds_complete=false`。

## 13. 代码变更计划

- Shared：审批合同/票据 HMAC、Webhook 事件白名单、RBAC revision 14、统一错误与配置。
- Migration：`0071_mobile_enterprise_approval_entry.sql/.down.sql`。
- Control：`approvals/{models,wire,service,routes,scheduler,outbox,targets}.py`，auth dependency、app 装配、
  Audit/Webhook owner 扩展及会话风险事务适配器。
- Console：`src/lib/approvals.js`、`features/approvals/*`、主 app/shell/navigation/style 装配及专项测试。
- Docs/acceptance：审批 API、测试记录、Runtime/Browser 验收器与合同测试、README 和目录说明。

## 14. 编码前两轮自审

### 第一轮：安全、租户、并发与隐私

状态：**已完成并修正**（2026-08-12）。

发现并关闭的问题：

1. 初审先质疑了 `approval.read/decide/create` 的双矩阵风险；删除了确实没有用户入口的 `approval.create`，
   并要求审批权限与目标域权限双重实时复核，不能相互替代。是否保留 read/decide 留待第二轮按产品授权语义复核。
2. 删除 producer 自报 risk、target revision、request/trace 的入口；risk、version、fingerprint 与 required
   permission 统一由固定 registry 和目标事实派生。
3. 修正预算目标语义为 tenant-qualified rule + active block fingerprint，批准只释放冻结规则，不再同时声称
   “单 block”与调用释放整条 rule。
4. 明确 exact idempotent replay 可回读既有决策，相反 action/reason/revision 必须 409，关闭初稿中的语义冲突。
5. 把 PBKDF2 移出 SQLite 写锁；失败计数使用独立短事务，成功签票前重新复核全部 owner，防止验证期间撤权。
6. 所有含密码、ticket 或幂等凭据的 POST 强制服务端 request/trace ID，关闭凭据碰撞回显。
7. timeout 使用 append-only ApprovalDecision 的固定 system actor，不重写 AuditLog 或冒充管理员。
8. 删除超出 Excel 的 `approval.decided` Webhook，只保留 created/timeout exact payload，缩小公开数据面。

### 第二轮：可实现性、范围、迁移、测试与验收

状态：**已完成并修正，设计冻结**（2026-08-12）。

发现并关闭的问题：

1. 复核自定义角色后确认仅用目标权限会把日常目标管理员自动升级为审批人，无法单独撤权；因此恢复
   `approval.read/decide` 和 revision 14，但仍删除无用户入口的 `approval.create`，并冻结 base + target
   双重后端 RBAC。
2. `approval_step_up_challenges` 只能保存成功票据，不能承担失败限速；新增 tenant/task/user-qualified
   `approval_step_up_guards`，只保存失败计数、锁定时间和单调更新时间。
3. 冻结决策 reason code：批准仅 `approved_business_need`；拒绝仅 `risk_unacceptable|budget_not_approved|
   no_longer_needed`。session-risk 的 `target_reason_code` 继续使用既有四枚举且仅内部创建可提交；预算任务
   必须省略该字段。
4. 明确幂等回读顺序：仍先认证并实时复核 base/target RBAC，再按 actor + key HMAC 找既有 replay；精确
   intent 可回读，不要求再次消费已使用的 step-up ticket；不同意图固定 409。
5. `approval.created/timeout` 只是供应商无关的签名卡片信封，企微/钉钉/飞书按其 Endpoint 侧适配；Control
   不猜测厂商专有卡片 JSON，也不保存厂商 access token。这样满足“可接”而不扩成三套应用生态。
6. 会话风险的 SQLite 权威 action 与批准同事务；Redis 仅是可恢复派生投影。API 返回已批准任务及
   `writeback_status=pending`，scheduler CAS 对账后变 completed；达到上限则 failed 并保留固定错误供追踪。
7. PAGE-109 使用 hash 路由内的受控 `approval_id` 查询参数，不在服务端模板或外部 URL 中放凭据；登录、
   切身份和离页必须清除该选择以及 password/ticket 内存。
8. 迁移不重建既有 AuditLog；0071 只更新 revision-14 权限投影并新增审批表/trigger，down 在审批事实、
   订阅、delivery 或自定义新 grant 存在时于首个持久 DDL 前失败关闭。
9. 补齐独立、可轮换的 `METAHUB_APPROVAL_HMAC_*` 配置；禁止借用其他密钥，并把生产缺失配置固定为
   失败关闭。同步修正 `approval.created` 示例：预算释放固定是 standard/无需 step-up，高风险只属于会话动作。

## 15. 实现后两轮自审与完成结论

### 第一轮：后端安全、事务与恢复边界

状态：**已完成并修正**（2026-08-14）。

1. 逐入口复核 tenant owner、Session、`approval.read/decide` 与目标域权限，确认列表、详情、step-up、
   approve/reject 均以后端实时矩阵为准；跨租户和目标域不可见保持同形 404，Console 裁剪不参与授权。
2. 逐事务复核 create、decision、timeout、target writeback、Decision、AuditLog 与 source/outbox，确认业务
   事实原子提交；Webhook 网络与 Redis 派生写回均在提交后执行，并由完整 owner/token/generation/lease
   CAS 恢复，迟到 worker 不能覆盖新 owner。
3. 修正登录、刷新与登出的异步 owner 边界：旧登录成功/失败、旧 current-user 响应和后台刷新不能恢复已
   清理的 Session、Storage 或审批 DOM；健康探测 timeout 等待真实请求收敛后才决定是否重试，禁止并行
   重复探测和悬挂请求。
4. 复核密码、step-up ticket、内部 Token、幂等键和审批 HMAC：明文只在单次调用栈存在，日志、Audit、
   Webhook、截图和验收 JSON 均不持久化；所有敏感 POST 继续由服务端生成 request/trace ID。

### 第二轮：Console 生命周期、迁移与验收可信度

状态：**已完成并修正**（2026-08-14）。

1. 修正 Browser 验收中迟到拒绝的完成判定：旧隐藏 overview 不再能冒充新 refresh 完成；释放暂停的
   step-up 前，除精确暂停句柄外的普通网络 owner 必须归零，同时兼容 Network 已终态清除而 Fetch 句柄
   仍受控暂停的合法 CDP 顺序，并继续拒绝半清理、错 owner 与额外取消。
2. 复核 PAGE-108/109 的 owner/generation/Abort、动态撤权、切租户、切任务、离页、登出和 busy Dialog；
   密码、ticket 与任务详情在所有失主路径清空，迟到 200/401 不能重绘，焦点只归还仍归属当前页面的触发器。
3. 0071 已从空 SQLite 实际完成 fresh/repeat/down/re-up：
   `71/0071 -> 71/0071 -> 70/0070 -> 71/0071`；四阶段 `integrity_check=ok`、外键违规 0，
   自定义矩阵、既有 rowid/schema 和 down 失败关闭合同由专项覆盖。
4. Runtime `prd078-runtime-20260814-08` 与 Browser `prd078-browser-final-20260814-36` 均绑定冻结源码 SHA-256；
   证据采用原子 staging/publish、精确库存、隐私扫描和 owner-safe cleanup。三张截图已经人工复核，无空白、
   横向溢出、文本溢出、控件重叠或敏感正文。

最终结果与命令见 `docs/testing/PRD-078-tests.md`。PRD-078 已完成；下一条仅交叉确认为 PRD-079，本次不
实现，`all_108_prds_complete=false`。
