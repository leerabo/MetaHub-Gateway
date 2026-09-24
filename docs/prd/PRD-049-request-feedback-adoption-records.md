# PRD-049 调用反馈与采纳率记录设计

> 状态：已完成。编码前两轮设计自审、实现后第三轮安全复审、第四至第七轮封账加固复审、代码实现、0038/0039 正反迁移、专项与全量回归、真实三服务、Runtime、Chrome 三角色三视口、日志/隐私和停止后静止数据库验收全部通过；唯一权威成功批次为 `artifacts/prd049/20260720T181908`。

## 1. 权威需求与需求理解

### 1.1 需求来源

- Excel `01_PRD总表` 第 50 行：`PRD-049`，`M1-企业POC`，`P1`，核心层，用户反馈。
- 用户：开发者 / 业务系统。
- 范围：提供 `request_id`、`rating`、`accepted`、`tags` 的 Feedback API；控制台展示模型满意度。
- API：`POST /feedback`、`GET /feedback/summary`。
- 页面：`PAGE-070 日志详情反馈`、`PAGE-071 反馈报表`。
- 数据对象：`OBJ-069 RequestFeedback`。
- 事件：`EVT-055 feedback.created`。
- 测试：`TC-0097` 主流程；`TC-0098` 异常、权限与安全。
- 验收：可以对请求打分；评分进入模型质量统计；越权 `request_id` 不可反馈。
- 非目标：复杂 RL 训练。

系统架构第 7.2、7.3 节把质量-成本路由和反馈闭环放在 M1，并明确 `quality_score` 的来源包括用户反馈、采纳率和业务成功率。第 15.3 节同时要求所有业务表带 `tenant_id` 或可通过强外键追溯租户，管理 API 必须在后端完成租户鉴权。

### 1.2 本条交付目标

本条建立“某次已完成模型调用的一次不可变反馈”事实，并提供两个消费面：

1. 控制台管理员或开发者可以在调用日志详情中提交反馈，并查看当前租户满意度报表。
2. 业务系统可以使用产生该调用的 Virtual Key 在 Gateway 提交反馈；报表查询留在 Control API，避免聚合查询争用模型热路径资源。
3. 逐请求反馈以独立统计维度进入模型质量查询，供管理员比较模型满意度与采纳率。
4. `feedback.created` 以持久事件 ID 投递 Webhook；接收端失败不能回滚已经提交的反馈事实。

### 1.3 本期明确不做

- 不做强化学习、自动训练、Prompt 优化或自动权重学习。
- 不实现 PRD-075 的 A/B 实验、实验流量比例或实验结果归因。
- 不把逐请求反馈写入 PRD-048 的 Provider 终态 `sample_count/success_count/error_count`。
- 不复用或推进 PRD-048 的管理员人工先验 `manual_quality_bps/feedback_version`。
- 不修改 `quality_cost_v1`、当前路由排序、Dry-Run 或历史 RoutingDecision/Candidate。
- 不声称已实现项目成员 ACL；当前 RBAC 只有 admin/developer/viewer，Session 用户的授权边界是角色加当前租户。
- 不接受自由文本标签、反馈备注、Prompt 或 Response，避免新增敏感正文存储面。

## 2. 核心语义冻结

### 2.1 两类 request_id 不得混用

- 请求体 `request_id`：被评价的模型调用 ID，对应 `request_logs.request_id`。本文称“源调用 ID”。
- 请求头 `X-Request-ID`：本次 Feedback API 操作的链路 ID，也是 POST 幂等键的一部分。本文称“操作请求 ID”。
- 响应顶层 `request_id` 永远回显操作请求 ID；`feedback.request_id` 才是源调用 ID。
- 日志、事件、数据库列和测试命名必须使用 `source_request_id`、`operation_request_id` 区分内部语义，禁止都叫 `request_id` 后依赖调用栈猜测。

### 2.2 评分、采纳和标签

- `rating` 必填，必须是 JSON integer 且范围为 `1..5`；`true/false` 不能冒充整数。
- `accepted` 必填，必须是 JSON boolean；`null`、`0/1`、字符串均拒绝。本期不引入“未知是否采纳”，否则采纳率分母会产生两套口径。
- `tags` 必填但允许空数组；最多 5 个，不允许重复。
- 标签只允许以下稳定英文 code，服务端按该固定顺序规范化后再计算指纹和持久化：

| code | 中文展示 | 语义 |
| --- | --- | --- |
| `accurate` | 准确 | 事实或结果准确 |
| `relevant` | 相关 | 与业务问题相关 |
| `complete` | 完整 | 覆盖所需信息 |
| `helpful` | 有帮助 | 能支持后续业务动作 |
| `clear` | 清晰 | 表达易理解 |
| `inaccurate` | 不准确 | 存在事实或结果错误 |
| `irrelevant` | 不相关 | 偏离业务问题 |
| `incomplete` | 不完整 | 缺少关键信息 |
| `unsafe` | 不安全 | 存在安全或合规风险 |
| `slow` | 响应慢 | 业务感知延迟不可接受 |

静态标签白名单既保证跨租户报表口径一致，也阻止业务正文、个人信息或 Prompt 片段借自由文本标签进入日志、Webhook 和统计。

`accurate/inaccurate`、`relevant/irrelevant`、`complete/incomplete` 是三个互斥组，同组正反标签不能同时出现。互斥检查发生在排序与指纹计算前，避免自相矛盾的样本污染报表。

### 2.3 一次调用只允许一条反馈

- `(tenant_id, source_request_id)` 全局唯一；同一调用无论由 Session 用户还是 Virtual Key 提交，都只能形成一条事实。
- 反馈一旦创建，不提供 PATCH/DELETE，也不允许数据库 UPDATE/DELETE；数据库还必须拒绝命中任一唯一键的 `INSERT OR REPLACE`/`REPLACE`，不能依赖 SQLite 默认关闭的 recursive trigger 间接保护。
- 同 actor、同操作请求 ID、同规范化意图返回原结果并标记 `idempotent_replay=true`。
- 同 actor、同操作请求 ID、不同意图返回 `feedback.request_conflict`。
- 同源调用已经被另一操作创建时返回 `feedback.already_exists`，不做 last-write-wins。
- 这条不可变边界避免评分被覆盖后无法解释历史报表，也给后续 PRD-075 提供稳定事实来源。

### 2.4 可反馈调用

只有满足下列全部条件的源调用可以反馈：

- 源行位于当前授权租户；Virtual Key actor 还必须满足 `request_logs.virtual_key_id == actor.virtual_key_id`。
- `request_logs.status='success'`。
- 非流式调用没有 `stream_session_id`；流式调用必须 `stream_status='completed'`。
- 能确定最终成功的 `model_id` 和 `provider_id`。发生 Fallback 时，以 `final_routing_attempt_id` 对应的成功 attempt 为准，并与 RequestLog 最终快照交叉校验。

跨租户、跨 Key 和不存在的源调用统一返回 404 `feedback.request_not_found`，避免枚举。已在授权范围内但失败、流未完成或最终归因不完整的调用返回 409 `feedback.request_not_feedbackable`。

## 3. 身份、入口和权限模型

### 3.1 Control API Session 入口

- `http://<control-api>/feedback` 接受现有控制台 Session Bearer Token。
- admin、developer 可以 POST 和 GET summary。
- viewer 在后端返回 403 `auth.permission_denied`，并且认证/权限判断必须先于请求体读取。
- Session actor 使用 `actor_type='session_user'`、当前 `user_id`、角色和当前 `tenant_id`；客户端不能提交 actor 或 tenant。

### 3.2 Gateway Virtual Key 入口

- `http://<gateway>/feedback` 接受现有 OpenAI-compatible Virtual Key Bearer Token。
- POST 只能评价同租户、同 `virtual_key_id` 产生的调用。
- Virtual Key actor 使用 `actor_type='virtual_key'`、已校验的 `virtual_key_id` 和租户；明文 Key 只在内存中参与哈希匹配，禁止进入数据库、响应和日志。
- 禁用 Key、禁用租户或无效 Key 继续使用现有 `gateway.auth_*` 错误，不伪装成控制台用户。
- Gateway 不注册 `GET /feedback/summary`。日志和报表属于控制面；把时间聚合、tag 展开和模型 JOIN 放入数据面会与模型流量争用 SQLite/CPU，并扩大泄露 Key 后的可用性攻击面。

### 3.3 双入口共用的领域合同

两个 POST 入口共用同一套严格 payload 解析、规范化、指纹、事务写入和响应 DTO。入口只负责恢复可信 actor 和选择提交后 Webhook 投递器，不能各自实现一套不同的评分规则。Summary 领域查询只允许 Control Session 调用，不导出为 Gateway route。

## 4. 涉及模块

| 模块 | 设计职责 |
| --- | --- |
| `shared-sdk/python/metahub_shared/feedback/` | 双服务共享的 actor、POST DTO、严格规范化、指纹和 SQLite 写事务；Control 复用其中的汇总计算 |
| `shared-sdk/python/metahub_shared/runtime/errors.py` | Feedback 统一错误码、HTTP 状态和安全建议 |
| `shared-sdk/python/metahub_shared/runtime/fastapi_app.py` | 把不支持的方法统一转换为 405 错误合同，并保留 `Allow`、链路头、CORS 和 Feedback 禁缓存边界 |
| `shared-sdk/python/metahub_shared/db/migrate.py` | 正向、回滚、空迁移和失败路径统一在 `finally` 关闭 SQLite 连接 |
| `shared-sdk/python/metahub_shared/runtime/error_diagnosis.py` | Feedback 校验、未找到、冲突和存储故障诊断 |
| `shared-sdk/python/metahub_shared/runtime/i18n.py` | 错误码目录中英文 label |
| `shared-sdk/python/metahub_shared/runtime/webhooks.py` | 注册并精确校验 `feedback.created` 事件 |
| `control-plane-api/src/metahub_control/feedback/` | Session 路由、RBAC、严格 body/query 读取和控制面提交后投递 |
| `control-plane-api/src/metahub_control/request_logs/` | 日志详情返回现有反馈；列表返回是否已反馈 |
| `control-plane-api/src/metahub_control/model_quality_scores/` | 只读扩展独立满意度/采纳率统计，冻结 PRD-048 原字段 |
| `gateway-data-plane/src/metahub_gateway/feedback/` | 仅提供 Virtual Key POST、同 Key 范围和数据面异步事件入队，不提供报表 GET |
| `control-plane-api/src/metahub_control/console/service.py` | 后端生成 PAGE-071 导航，按角色裁剪 |
| `console-web/src/lib/requestFeedback.js` | POST/summary 白名单 API client，不发送 tenant/actor |
| `console-web/src/features/request-logs/` | PAGE-070 详情容器、现有反馈只读状态和入口位置 |
| `console-web/src/features/request-feedback/` | PAGE-070 独立表单组件和 PAGE-071 满意度/采纳率报表 |
| `console-web/src/app/main.js` | 两页 identity/tenant/navigation/generation/Abort owner 生命周期 |
| `infra/migrations/0038_request_feedback.sql/.down.sql` | 不可变事实、聚合、事件、约束、索引和不可丢事实回滚守卫 |
| `infra/migrations/0039_request_feedback_immutability_hardening.sql/.down.sql` | 在不修改已应用 0038 checksum 的前提下，补齐 Feedback/Event 及被引用 RequestLog/final Attempt 的唯一冲突 INSERT/REPLACE 防改写守卫 |

共享 Feedback 领域包不依赖 Control 或 Gateway 业务包，防止跨服务反向导入。它可以依赖 shared database/config/error/webhook 契约，但 Webhook 外部副作用仍由入口在 commit 后触发。

## 5. 数据表和字段设计

### 5.1 `request_feedbacks`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `request_feedback_id` | TEXT PK，`rfb_` + 32 lowercase hex | Feedback 对象 ID |
| `tenant_id` | TEXT NOT NULL | 强制租户边界 |
| `source_request_id` | TEXT NOT NULL，1..128 bytes，ASCII `[A-Za-z0-9._:-]` | 被评价的 RequestLog ID |
| `model_id` | TEXT NOT NULL | 最终成功模型快照 |
| `provider_id` | TEXT NOT NULL | 最终成功 Provider 快照 |
| `virtual_key_id` | TEXT NULL | 源调用 Key 快照，不是 Key 明文；历史/内部成功调用可为空，Virtual Key actor 时必须非空且等于 actor Key |
| `project_id` | TEXT NULL | 源调用项目快照 |
| `application_id` | TEXT NULL | 源调用应用快照 |
| `environment_id` | TEXT NULL | 源调用环境快照 |
| `rating` | INTEGER NOT NULL，1..5，拒绝非 integer | 评分 |
| `accepted` | INTEGER NOT NULL，0/1 | 采纳事实 |
| `tags_json` | TEXT NOT NULL | 规范化标签数组，最大 5 个，固定白名单、唯一、固定顺序 |
| `actor_type` | TEXT NOT NULL | `session_user` 或 `virtual_key` |
| `actor_user_id` | TEXT NULL | Session actor；与 Key actor 互斥 |
| `actor_virtual_key_id` | TEXT NULL | Virtual Key actor；与 user actor 互斥 |
| `operation_request_id` | TEXT NOT NULL，1..128 bytes，ASCII `[A-Za-z0-9._:-]` | POST 操作幂等键，不是源调用 ID |
| `request_fingerprint` | TEXT NOT NULL，64 lowercase hex | 规范化意图 SHA-256 |
| `feedback_event_id` | TEXT NOT NULL UNIQUE | 对应持久事件 ID |
| `trace_id` | TEXT NOT NULL，1..128 bytes，ASCII `[A-Za-z0-9._:-]` | 创建链路 Trace |
| `status` | TEXT NOT NULL，固定 `recorded` | OBJ-069 状态字段，本期只有不可变终态 |
| `created_at` | TEXT NOT NULL，UTC | 创建时间 |
| `updated_at` | TEXT NOT NULL，等于 created_at | 满足对象通用字段；不可变事实不会推进 |

约束和索引：

- `UNIQUE (tenant_id, request_feedback_id)` 和 `UNIQUE (tenant_id, feedback_event_id)` 分别为 Event 回指和 deferred Event FK 提供完全匹配的 SQLite 复合候选键；不能只依赖全局 PK/UNIQUE。
- 复合外键 `(tenant_id, source_request_id) -> request_logs(tenant_id, request_id)`。
- 复合外键 `(tenant_id, actor_user_id) -> users(tenant_id, user_id)` 和 `(tenant_id, actor_virtual_key_id) -> virtual_keys(tenant_id, virtual_key_id)`；直接复用前序迁移已有 tenant-qualified unique index。
- actor 精确 CHECK 为：`session_user` 时只有 `actor_user_id` 非空；`virtual_key` 时只有 `actor_virtual_key_id` 非空。空 actor、双 actor、actor_type 与列不匹配均在表级拒绝。
- `UNIQUE (tenant_id, source_request_id)` 保证一次调用一条反馈。
- 两个 partial unique index 分别约束 Session user 和 Virtual Key 的 `(tenant_id, actor, operation_request_id)` 幂等键。
- tenant/model/time、tenant/virtual_key/time、tenant/time 索引支撑 summary。
- INSERT trigger 重新读取 RequestLog 和最终 attempt，校验 tenant、可反馈状态及所有归因快照；Session 分支还校验 user 当前 active 且角色为 admin/developer，Virtual Key 分支校验 Key/tenant 当前 active、源调用 Key 非空且相等。不能只信任服务层 dependency。
- Feedback 的 `(tenant_id, feedback_event_id)` 外键精确引用 Event 的 `(tenant_id, feedback_event_id)`；Event 的 `(tenant_id, request_feedback_id)` 外键精确引用 Feedback 的 `(tenant_id, request_feedback_id)`，两条均为 `DEFERRABLE INITIALLY DEFERRED`。允许同一事务先插 Feedback 再插 Event，但任一方缺失时 commit 必须失败。不能只靠“提交前重读”维持事件完整性。
- UPDATE/DELETE trigger 无条件拒绝。追加 0039 的 BEFORE INSERT 冲突守卫覆盖全局 ID、tenant-qualified ID、源调用和两类 actor 幂等唯一键；即使连接保持 SQLite 默认 `recursive_triggers=0`，`INSERT OR REPLACE` 也会在删除旧行前固定失败。

### 5.2 `model_feedback_statistics`

这是可从 `request_feedbacks` 重建的 all-time 派生统计，但每次反馈必须与事实同事务推进：

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `tenant_id` | TEXT NOT NULL | 租户边界 |
| `model_id` | TEXT NOT NULL | 最终模型 |
| `feedback_count` | INTEGER >= 0 | 评分样本数 |
| `rating_sum` | INTEGER >= 0 | 评分总和 |
| `rating_1_count` ... `rating_5_count` | INTEGER >= 0 | 五档分布 |
| `accepted_count` | INTEGER >= 0 且 <= feedback_count | 已采纳数 |
| `feedback_statistics_version` | INTEGER >= 0 | 每纳入一条反馈推进 1，当前等于 feedback_count |
| `updated_at` | TEXT NOT NULL | 最近统计时间 |

主键为 `(tenant_id, model_id)`，并以 tenant-qualified 外键指向 `models`。聚合推进只由 `request_feedbacks` 的 AFTER INSERT trigger 执行，领域服务不得再手写第二套 UPSERT。`model_feedback_statistics` 的 BEFORE INSERT/UPDATE trigger 必须从不可变 `request_feedbacks` 反查同 tenant/model 的 count、sum、五档分布、accepted、version 和 `MAX(created_at)` 并逐字段验真；无条件 BEFORE DELETE trigger 拒绝删除。直接 SQL 制造 ghost count、漏计、倒退、DELETE 或错误 INSERT OR REPLACE 都会失败。这样一条 Feedback 插入、聚合推进和 deferred Event 仍在同一事务，任一失败整体回滚；down 必须先显式删除这些 trigger。

### 5.3 `request_feedback_events`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `feedback_event_id` | TEXT PK，`fbe_` + 32 lowercase hex | 持久事件 ID |
| `tenant_id` | TEXT NOT NULL | 租户边界 |
| `request_feedback_id` | TEXT NOT NULL UNIQUE | 一对一 Feedback 事实 |
| `source_request_id` | TEXT NOT NULL | 源调用 ID |
| `event_type` | TEXT 固定 `feedback.created` | EVT-055 |
| `actor_type` | TEXT | `session_user` / `virtual_key` |
| `actor_id` | TEXT NOT NULL | user_id 或 virtual_key_id，不含明文 Token |
| `operation_request_id` | TEXT NOT NULL | 反馈操作链路 |
| `trace_id` | TEXT NOT NULL | 事件 Trace |
| `payload_json` | TEXT NOT NULL，精确字段、<=4096 bytes | Webhook 安全 payload |
| `created_at` | TEXT NOT NULL | 与反馈创建时间一致 |

除全局 `feedback_event_id` PK 和 `request_feedback_id` UNIQUE 外，Event 还声明 `UNIQUE (tenant_id, feedback_event_id)`、`UNIQUE (tenant_id, request_feedback_id)`，作为双向 deferred FK 的父侧候选键。迁移测试通过 `PRAGMA foreign_key_list` 核对列顺序，并真实执行双行 commit 与两个单边 commit，而不是只检查建表 SQL 文本。

`payload_json` 精确为：

```json
{
  "request_feedback_id": "rfb_...",
  "request_id": "被评价的源调用 ID",
  "model_id": "最终模型 ID",
  "actor_type": "session_user",
  "rating": 5,
  "accepted": true,
  "tags": ["accurate", "helpful"]
}
```

`payload_json` 和 Webhook payload 不包含 operation request ID、actor ID、Prompt、Response、Token、Key、Provider Endpoint、原始错误或自由文本；持久 Event 行仍保存 operation request ID，用于幂等图校验但不向接收端扩散。`actor_type` 只区分 user/key，不暴露身份值。Webhook envelope 精确绑定 `event_id=feedback_event_id`、`object_id=request_feedback_id`、`actor_id` 为对应 user_id/key_id、tenant、trace 和 created_at；表级 graph/payload/immutable trigger 逐字段核对 tenant、actor、源调用、评分、采纳、标签和时间。双向 deferred 外键保证 Feedback 与 Event 必须成对提交。0039 还按 Event 全局 ID 和 Feedback 全局 ID 拒绝 INSERT 唯一冲突，阻止 `REPLACE` 绕过 DELETE trigger 后改写事件链。

逐请求反馈可能接近调用量，不写入面向管理变更的 `audit_logs`，避免高频业务事实淹没管理员审计，也不能给 Virtual Key 伪造 user actor。`request_feedbacks + request_feedback_events` 是两类 actor 共用的权威审计链；Session 和 Virtual Key 使用同一事件合同。

### 5.4 与 PRD-048 的统计隔离

`model_quality_scores` 表不新增逐请求反馈写路径。API 只通过 `(tenant_id, model_id)` LEFT JOIN `model_feedback_statistics`，增加以下只读字段：

- `request_feedback_count`
- `request_feedback_rating_sum`
- `request_feedback_average_milli`，范围 0 或 1000..5000
- `request_feedback_accepted_count`
- `request_feedback_adoption_rate_bps`，范围 0..10000
- `request_feedback_statistics_version`
- `request_feedback_rating_distribution`

`model_feedback_statistics` 只表示 tenant + model 的 all-time 派生统计，专供 ModelQuality LEFT JOIN。带时间窗口的 Feedback Summary 必须从 `request_feedbacks` 先按 tenant 与 `created_at` 做 SQL scope，再 group；不能拿 all-time 表回答窗口查询。`sample_count/success_count/error_count/latency_sample_count/statistics_version/manual_quality_bps/feedback_version/effective_quality_bps` 必须在 PRD-049 前后完全一致。现有 Gateway observation helper 不读取或写入 `request_feedbacks`。

`feedback.created` 与 PRD-048 的 `model.feedback_recorded` 是两个独立事件：前者表达逐请求用户/业务反馈，后者只表达 admin 修改模型级人工先验。订阅目录、payload 校验、持久 ID 前缀和测试不得合并。

## 6. API 设计

### 6.1 `POST /feedback`

请求头：

- `Authorization: Bearer ...`：Control 为 Session Token，Gateway 为 Virtual Key。
- `X-Request-ID`：操作链路/幂等 ID；中间件缺省生成，但调用方要在丢响应重试时复用原值。
- `X-Trace-ID`：可选，沿用统一链路规则。
- `Content-Type: application/json`。

`Content-Type` 只接受 `application/json`，允许大小写无关的 media type 和合法 `charset=utf-8` 参数；拒绝 `application/*+json` 及其他类型。源调用 ID、操作请求 ID 和 trace ID 只接受上述 ASCII 安全字符集，避免业务正文、换行或日志控制字符借链路字段扩散。

现有统一中间件会把非法 `X-Request-ID` 字符替换为 `_` 并截断，因此 Feedback 不能直接把规范化后的 state 值当作外部幂等键。认证/RBAC 通过后、读取 body 前，路由必须从 ASGI raw headers 计数并校验：没有该 header 时使用 state 生成值；恰好一个时要求原始 bytes 为 ASCII、长度 1..128、只含安全字符且解码值与 state 完全一致；重复、替换或截断一律返回 `feedback.input_invalid`。这可防止 `a?` 与 `a!` 归一到同一幂等键。

请求体必须精确包含四个字段，不接受 `tenant_id`、actor、模型、Provider、统计值、未知字段或重复 JSON key：

```json
{
  "request_id": "req_source_call",
  "rating": 5,
  "accepted": true,
  "tags": ["accurate", "helpful"]
}
```

规范化意图指纹合同固定为 v1：

1. 先按固定标签顺序排序，并拒绝重复、未知和互斥标签。
2. 按固定字段顺序生成无空白 ASCII JSON：`{"schema_version":1,"source_request_id":"...","rating":5,"accepted":true,"tags":[...]}`。
3. 使用 Python 等价参数 `ensure_ascii=True, separators=(",", ":"), sort_keys=False` 取 UTF-8 bytes，再计算 lowercase SHA-256。
4. 指纹不包含 tenant、actor、operation request ID、trace、模型、Provider 或时间；这些值分别由幂等 scope、源事实和事务恢复。
5. Shared/Control/Gateway 使用同一函数，并以 golden vector 固定跨入口结果；未来改变字段或规范化规则必须新建 schema version，不能静默改变 v1。

Golden vector：上方示例规范化字节必须精确为 `{"schema_version":1,"source_request_id":"req_source_call","rating":5,"accepted":true,"tags":["accurate","helpful"]}`，SHA-256 必须为 `1baf6cd515497feffb6ce568f0574a66d3677ef17004bb95c0b3ea56131a2b6d`。

首次创建返回 HTTP 201；同操作同意图重放返回 HTTP 200。顶层 `request_id/trace_id` 始终属于本次 HTTP 请求；重放时不会回显首次 trace。持久 Event 继续保留首次创建 trace/time，并以它重派 Webhook：

```json
{
  "feedback": {
    "request_feedback_id": "rfb_...",
    "request_id": "req_source_call",
    "model_id": "model_...",
    "rating": 5,
    "accepted": true,
    "tags": ["accurate", "helpful"],
    "actor_type": "session_user",
    "status": "recorded",
    "created_at": "2026-07-19T12:00:00Z"
  },
  "idempotent_replay": false,
  "request_id": "feedback_operation_id",
  "trace_id": "trace_..."
}
```

响应使用 `Cache-Control: no-store, private`。不返回 tenant_id、actor ID、Provider ID、Virtual Key ID、指纹或内部事件 payload。

### 6.2 `GET /feedback/summary`（仅 Control API）

允许的 query：

- `model_id`：可选稳定模型 ID。
- `from_time`、`to_time`：必须成对出现；未提供时使用服务端当前 UTC 之前 30 天，闭开区间 `[from_time, to_time)`；最大跨度 366 天。
- `limit`：默认 20，范围 1..100；按模型行分页。
- `offset`：默认 0，上限 10,000。

该接口只接受 admin/developer Session。认证与 RBAC dependency 必须在手工 query 解析和 Summary 领域查询连接前完成；认证自身仍可使用现有 Session 数据库连接，匿名或 viewer 即使提交畸形 query 仍稳定返回 401/403。任何未知 query、重复 query、`tenant_id`、actor、`virtual_key_id` 都返回 422。SQL 必须先按当前 Session tenant 和 `request_feedbacks.created_at` 窗口过滤，再聚合、JOIN 模型和分页；禁止先查全租户或使用 all-time aggregate 代答。

时间参数只接受秒精度 canonical UTC `YYYY-MM-DDTHH:MM:SSZ`；拒绝 offset、fraction 和非规范文本，避免 SQLite TEXT 比较产生时区/格式歧义。查询在一个 connection 中设置 `query_only`，捕获一次 `now`/默认窗口并开启显式只读 `BEGIN`；总计、分页模型、tag counts 和 total 全部读取同一 SQLite snapshot。250ms 是从统一中间件进入时刻开始计算的整个 endpoint 单一 monotonic deadline；Session 鉴权连接按剩余预算设置 `busy_timeout` 并在 `finally` 关闭，并发门禁、工作线程、所有 statement 的 progress handler 和 DTO 共用同一绝对 deadline，任何阶段都不能重置预算。超时映射 `feedback.unavailable`，并在 finally rollback、clear handler、close。专项测试固定 `EXPLAIN QUERY PLAN` 命中 tenant/time 索引，并覆盖真实写锁无法通过鉴权等待绕过 endpoint budget、并发 POST 时各响应分段仍来自同一快照、summary 不阻塞模型调用。

模型行固定按 `feedback_count DESC, model_id ASC` 排序，`total` 表示窗口内有反馈的模型总数；总计和 `tag_counts` 始终覆盖完整过滤窗口而不是当前页。`tag_counts` 按第 2.2 节标签白名单顺序返回，禁止依赖数据库未定义顺序。

响应示意：

```json
{
  "scope_type": "tenant",
  "from_time": "2026-06-19T12:00:00Z",
  "to_time": "2026-07-19T12:00:00Z",
  "summary": {
    "feedback_count": 12,
    "rating_sum": 51,
    "average_rating_milli": 4250,
    "accepted_count": 9,
    "adoption_rate_bps": 7500,
    "rating_distribution": {"1": 0, "2": 1, "3": 2, "4": 2, "5": 7},
    "tag_counts": [{"tag": "accurate", "count": 8}]
  },
  "models": [
    {
      "model_id": "model_...",
      "internal_model_id": "glm-4-flash",
      "display_name": "GLM-4 Flash",
      "feedback_count": 12,
      "average_rating_milli": 4250,
      "accepted_count": 9,
      "adoption_rate_bps": 7500,
      "rating_distribution": {"1": 0, "2": 1, "3": 2, "4": 2, "5": 7}
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0,
  "request_id": "summary_operation_id",
  "trace_id": "trace_..."
}
```

平均评分使用 integer milli、采纳率使用 basis points，均由 shared Python arbitrary-int 在读取原始 count/sum 后执行 integer half-up：`floor((2*numerator*scale + denominator)/(2*denominator))` 的等价实现，零样本固定为 0。禁止在 SQLite 中先乘 scale 造成 64 位溢出，也禁止数据库 float 或 JavaScript 浮点重新决定权威值。

### 6.3 RequestLog 扩展

- 列表项新增 `has_feedback: bool`，按当前租户 LEFT JOIN/EXISTS，不返回评分正文。
- 详情新增 `feedback: RequestFeedbackInfo | null`、`feedback_eligible: bool` 和稳定 `feedback_ineligible_reason`。服务端按同一领域规则给出 `eligible/already_recorded/request_failed/stream_incomplete/attribution_unavailable`，供 PAGE-070 显示或初始化表单；前端不能根据 status/stream 字段自行猜测。
- 归因状态机必须由详情与 POST 共用：`routing_attempt_count=0 AND final_routing_attempt_id IS NULL` 才允许 legacy/non-attempt 成功行使用 RequestLog 自身非空 model/provider；`routing_attempt_count IN (1,2)` 时必须有同 tenant/request、status=`succeeded` 的 final attempt并使用其 model/provider；其余组合一律 `attribution_unavailable`。不能因 final ID 恰好为空就宽松回退。
- 详情反馈不改变 RequestLog、RoutingDecision 或 Candidate；历史路由解释继续只读冻结事实。

## 7. 事务、幂等和并发顺序

POST 使用独立 SQLite connection、领域级 `PRAGMA busy_timeout=250` 和 `BEGIN IMMEDIATE`，严格按以下顺序。250ms 内拿不到写锁时回滚并返回 `feedback.unavailable`，避免 Feedback 与 Gateway RequestLog/Usage 写事务争用时长时间拖慢模型流量：

1. 路由 dependency 完成 Session RBAC 或 Virtual Key 鉴权；未授权时不读 body。
2. 校验 raw `X-Request-ID` 唯一且未被中间件替换/截断，再严格解析、规范化 body，计算不含 tenant/actor 明文 Token 的 canonical fingerprint。
3. 开启写事务，先按 tenant + actor + operation request ID 查询历史操作。
4. 命中同指纹时读取并返回已有 Feedback/Event；命中异指纹返回 `feedback.request_conflict`。
5. tenant-qualified 查询源 RequestLog；Virtual Key 查询额外带 actor Key。未命中统一 404。
6. 校验 success/stream/final attempt，并冻结最终归因；不满足返回 409。
7. 检查源调用唯一反馈；已有返回 `feedback.already_exists`。
8. INSERT `request_feedbacks`，由 AFTER INSERT trigger 原子推进并验真 `model_feedback_statistics`，再 INSERT `request_feedback_events`。
9. 在提交前重读并校验三者一致；任一步失败 rollback 并映射统一错误。
10. commit 后从持久 Event 构造 `feedback.created`。Control 使用现有控制面投递器；Gateway 进入既有有界异步 worker。投递失败只记录安全分类，不能改变 API 成功结果。

同意图幂等重放不创建第二个 Event，但会使用原 `feedback_event_id` 再次触发提交后投递，从而恢复“数据库已提交、进程在首次投递前退出”的窗口；delivery 以稳定 event ID 去重，已经形成的投递不会重复发送。Gateway 队列是有界、非持久、best-effort：worker 未启动或 queue full 时记录不含 payload 的固定 warning 并仍返回成功；本期不声称具备 PRD-057 的持久队列接管。

并发请求由写锁、源调用 unique 和 actor 幂等 partial unique 共同裁决。捕获 `sqlite3.IntegrityError` 后先 rollback/close，再用新连接按固定优先级分类：先查 tenant + actor + operation key，同指纹为 replay、异指纹为 request_conflict；未命中再查 tenant + source，命中为 already_exists；两者均未命中则为 `feedback.record_failed`。busy/locked 单独映射 `feedback.unavailable`；随机 Feedback/Event PK 碰撞、graph/payload/aggregate trigger 或未知约束错误不得伪装成业务冲突，也不能把 SQL 文本回给客户端。

0038 为已有 Feedback 的父事实增加后向不可改写 trigger：

- `request_logs` 在存在 Feedback 时，禁止 tenant/request/key/status/model/provider/project/application/environment/stream/final-attempt/routing-attempt-count 发生真实变化，并禁止 DELETE；仍允许 input/output/total tokens、cost、usage、updated_at 等不影响反馈归因的补账字段更新。
- 被某条 Feedback 的源 RequestLog 引用为 final 的 `routing_attempts`，禁止 tenant/request/id/status/model/provider 变化或 DELETE；usage_status、usage ledger、latency 等不改变反馈快照的后续结算仍按既有合同处理。

父侧 trigger 只保护已形成 Feedback 的行，不能阻断其他进行中请求的正常终态推进。

## 8. 前端页面和交互设计

### 8.1 PAGE-070 日志详情反馈

- 复用现有调用日志详情，不新建第二个日志列表。反馈区放在基础摘要之后、路由/用量长证据之前，保证移动端可达；列表命令显示“查看并反馈”。
- 只有后端详情 `feedback_eligible=true` 且 `feedback=null` 时显示可编辑区，前端不重复实现可反馈资格。
- `rating` 使用 1..5 的可访问单选分段控件；`accepted` 使用“已采纳/未采纳”二选一分段 radio 且初始无默认值，避免未操作的 checkbox 被误当作明确 false；tags 使用固定多选 checkbox，最多 5 个。
- 提交按钮是明确命令；请求中禁用表单并设置 `aria-busy`。错误用现有 live region 显示安全错误码和操作 request_id。
- 已有反馈显示只读评分、采纳、标签和时间，不提供覆盖/删除按钮。
- 列表 `has_feedback` 只显示简洁状态，详情才读取反馈内容。

### 8.2 PAGE-071 反馈报表

- 新导航 ID：`feedback-summary`，标签“反馈报表”，放在“调用与成本”分组、排序 94；后端只向 admin/developer 返回，viewer 无导航、DOM 和预取。
- 顶部提供模型与时间范围筛选、刷新命令；正文展示总样本、平均评分、采纳率、评分分布和按模型表格。
- 页面是全宽工作区，不使用卡片套卡片。长模型名和标签允许换行，数值列保持稳定宽度。
- 页面明确使用响应 `scope_type=tenant` 和 `from_time/to_time` 的后端口径，不从分页模型行重新计算总平均或采纳率。零样本、加载、错误和成功状态均原位显示；错误不回显 URL、Token、请求正文或 SQL。

### 8.3 异步 owner 和敏感状态生命周期

RequestLog list/detail、Feedback POST、Feedback summary GET 各自拥有 AbortController 和 generation。有效 owner 至少绑定：

- tenant_id
- user_id/email
- role
- session_id/revision
- active navigation
- 源调用 ID（POST）
- detail/form generation（POST）
- operation request ID 与规范化表单意图（POST retry）

15 秒读取刷新不得取消正在进行的 POST，也不得覆盖 dirty 草稿。登出、租户/用户/角色/Session 变化、离开页面或切换详情时，才先递增 mutation generation 并 abort，再清除草稿、标签、错误、成功反馈和 operation request ID。迟到的 200/201/401/错误响应都不得重建 DOM、退出新 Session 或覆盖新租户状态。反馈数据、标签、Token 和 request ID 不写 localStorage/sessionStorage/URL。

### 8.4 响应式与可访问性

- 验收视口至少为 1280x720、900x800、390x844。
- 表格在窄屏转换为带 `data-label` 的单列记录；fieldset、长 ID 和标签使用 `min-width:0` 与 `overflow-wrap:anywhere`，根文档不产生横向溢出。
- 分段评分有 `fieldset/legend` 和明确选中状态；所有 checkbox 有 label；加载/错误/成功使用 `role=status/alert`。
- 键盘可以完成评分、采纳、标签、提交、筛选和刷新；焦点在提交后回到反馈结果区。
- 所有模型名、标签、错误码和 request_id 通过既有 `escapeHtml` 输出，覆盖 XSS 测试。

## 9. 错误码设计

| HTTP | code | 场景 |
| --- | --- | --- |
| 401 | 现有 `auth.*` / `gateway.auth_*` | 缺失、无效、过期或禁用身份；Gateway 仅适用于 POST |
| 403 | `auth.permission_denied` | viewer 调用 Control Feedback API |
| 405 | `infra.method_not_allowed` | 路径存在但 HTTP method 不受支持；保留安全的 `Allow` 响应头 |
| 404 | `feedback.request_not_found` | 不存在、跨租户或 Virtual Key 跨 Key 源调用，统一防枚举 |
| 409 | `feedback.request_not_feedbackable` | 同授权范围内调用失败、流未完成或最终归因不完整 |
| 409 | `feedback.already_exists` | 源调用已有另一条反馈 |
| 409 | `feedback.request_conflict` | 同 actor + 操作 request_id 的规范化意图不同 |
| 422 | `feedback.input_invalid` | body 类型、重复键、未知字段、rating/accepted/tags 不合法 |
| 422 | `feedback.query_invalid` | summary 未知/重复/越界 query 或时间范围不合法 |
| 503 | `feedback.unavailable` | 数据库、迁移或读取依赖不可用 |
| 500 | `feedback.record_failed` | 事务内不可分类一致性故障，返回安全建议且不泄露 SQL |
| 500 | `infra.unhandled` | 路由、认证或数据库依赖抛出未捕获异常；只返回统一通用错误，不回显异常正文 |

所有错误走统一 `code/message/request_id/trace_id/suggestion/diagnosis`，并进入 `/error-codes` 中英文目录。

## 10. 安全与隐私约束

- tenant_id、actor、模型、Provider、Virtual Key 和归属快照全部由鉴权与 RequestLog 恢复，客户端不能覆盖。
- 权限 dependency 在手工 body 解析前执行；viewer 的畸形 body 仍返回 403，而不是借校验差异探测合同。
- POST body 上限 4096 bytes；JSON 最大深度 3；拒绝重复 key、NaN/Infinity、控制字符和未知字段。
- query 采用精确白名单并拒绝重复参数，不接受 tenant_id 或 virtual_key_id。
- 日志只记录 event code、tenant、actor 类型、对象 ID、操作 request_id/trace_id 和固定失败分类；不记录 Authorization、Token、Prompt、Response、tags 或请求体。
- Webhook payload 只允许精确静态字段；通用递归敏感键检查仍作为第二道防线。
- Virtual Key 的 `key_prefix` 也不进入 Feedback 响应、事件或日志。
- CORS 继续使用服务应用工厂；两个入口不得绕开统一 request_id、trace_id、错误处理和结构化日志中间件。404、405、422 和 500 都必须使用统一 JSON 错误体；405 仅透传框架计算出的 `Allow`，不能复制异常正文或任意 Header。
- 安全中间件在认证失败响应前丢弃完整凭证、至少 16 字符的显著凭证片段、`Bearer<TAB>`、裸 Token 和错误 scheme，并生成内部链路 ID；Feedback raw/state 门禁再拒绝被替换的外部 `X-Request-ID`，秘密不得进入响应、日志、数据库、事件或队列。
- Control/Gateway POST 和 Control Summary 的全部响应统一设置 `Cache-Control: no-store, private`，包括成功、认证失败、权限失败、校验错误和未捕获基础设施异常；普通响应由统一中间件覆盖，最外层 `ServerErrorMiddleware` 生成的 500 由异常处理器显式复用同一规则。带 Origin 的 500 还必须按应用现有 allowlist/CORS expose 合同返回 Header，不能反射未授权 Origin。Summary 仍按 Session tenant/time scope 在 SQL WHERE 中过滤、受 endpoint-wide 250ms deadline 保护，Gateway 不提供 summary。

## 11. 数据库迁移与回滚

### 11.1 正向迁移 `0038_request_feedback.sql`

1. 脚本固定以 `PRAGMA foreign_keys=ON; BEGIN IMMEDIATE;` 开始；PRAGMA 必须在事务前，兼容迁移器 `executescript`。
2. 断言并复用 0033 的 `uq_users_tenant_user_prd044`、`uq_models_tenant_model_prd044` 和 0034 的 `uq_virtual_keys_tenant_key_prd045`；0038 不创建同义重复索引，也不在 down 删除前序 PRD 所有的索引。
3. 创建 `request_feedbacks`、`model_feedback_statistics`、`request_feedback_events`。
4. 创建查询、幂等和归属索引。
5. 创建 graph、payload、聚合验真、parent-side 和 immutable trigger。
6. 用事务内 TEMP guard 表/trigger 检查 `pragma_foreign_key_check` 与 `pragma_integrity_check`；异常通过 trigger `RAISE(ABORT, 'prd049_up_integrity_failed')` 中止，最后 `COMMIT`。

新表为空，不对历史 RequestLog 猜测或生成反馈，也不回填评分。单独完成 0038 阶段时 schema 最高版本为 0038；PRD-049 追加不可变加固后的最终基线为 39 条迁移、最高 0039。

### 11.2 回滚 `0038_request_feedback.down.sql`

- 脚本固定以 `PRAGMA foreign_keys=OFF; BEGIN IMMEDIATE;` 开始；关闭必须发生在事务前，目的是允许空表循环 FK 按确定顺序拆除。
- 第一项 DDL 创建 TEMP guard 表和 TEMP BEFORE INSERT trigger。把 Feedback/Event/nonzero Aggregate、`webhook_deliveries.event_type='feedback.created'`、这些 delivery 关联的 `webhook_delivery_attempts`，以及 `json_each(webhook_endpoints.event_types_json)` 中的订阅总数 INSERT guard；非零时固定 `RAISE(ABORT, 'prd049_down_business_facts_present')`，禁止静默丢业务反馈或留下旧代码不能理解的订阅。
- guard 通过后，先 drop parent-side 与 Aggregate DELETE 等 0038 trigger，再按 event -> aggregate -> feedback 顺序删除其余 trigger、index、table；前序 PRD 的 tenant-qualified unique index保持不变。
- 用第二个 TEMP guard 对 `pragma_foreign_key_check` 与 `pragma_integrity_check` 的非零结果固定 `RAISE(ABORT, 'prd049_down_integrity_failed')`，drop TEMP guard 后 `COMMIT; PRAGMA foreign_keys=ON;`。
- 任何 guard 失败都由迁移器 rollback 并关闭连接；不能依赖脚本尾部重新开启 PRAGMA 后再报告成功。

测试必须覆盖 `0037 -> 0038 -> 0037` 的原迁移可逆性，以及追加加固后的 `0037 -> 0038 -> 0039 -> 0038 -> 0037` 空库逐版本回滚；带任一反馈事实时 0039 和 0038 都不可降级。完整链为 `0001..0039`，重复 up 必须幂等跳过，并执行迁移后真实读写。

### 11.3 追加加固迁移 `0039_request_feedback_immutability_hardening.sql`

1. 0038 已在开发库和验收库应用，禁止修改其 checksum；加固必须使用新的 0039，而不是原地改写历史 SQL。
2. 正向脚本固定按 `PRAGMA foreign_keys=ON -> BEGIN IMMEDIATE -> TEMP prerequisite guard -> 创建六个持久 trigger -> TEMP FK/integrity guard -> COMMIT` 执行。先精确断言 0038 已 applied、三张表及八个 immutable/parent trigger 均存在；缺失固定 `prd049_hardening_prerequisite_missing`，完整性失败固定 `prd049_hardening_up_integrity_failed`，任一异常回滚全部 0039 DDL。
3. 对 `request_feedbacks` 覆盖五类非冗余唯一冲突：全局 Feedback PK、全局 Event ID、tenant/source、Session actor operation partial key、Virtual Key actor operation partial key；tenant-qualified Feedback/Event ID 候选键与全局键逻辑冗余，迁移测试通过 schema introspection 锁定但不重复计数。对 `request_feedback_events` 覆盖 Event PK 和全局 Feedback ID 两支。BEFORE INSERT 在 REPLACE 删除旧行前固定抛原有 immutable 错误。
4. 对已被 Feedback 引用的 `request_logs` 复合主键增加条件 INSERT 冲突守卫：只有 Key、终态、模型、Provider、项目、应用、环境、流状态、final attempt 或 attempt count 等 0038 已保护字段与当前行不同才拒绝；受保护字段相同的既有 `ON CONFLICT DO UPDATE` 仍可按现有字段集更新 RequestLog；Usage/Token/成本继续由后续直接 UPDATE 补账。对 final `routing_attempts` 的全局 PK 和 tenant/request/sequence 两类 INSERT 冲突一律拒绝，避免父快照通过 `INSERT OR REPLACE` 漂移。
5. INSERT 守卫不能覆盖 `UPDATE OR REPLACE`：攻击者可从未被 Feedback 引用的源 RequestLog/Attempt 出发，把唯一键改成受保护目标键；默认 `recursive_triggers=0` 会隐式删除目标，而 0038 的 UPDATE trigger 只检查未引用的 OLD 源行。0039 因此另加两个 BEFORE UPDATE 冲突目标守卫：RequestLog 覆盖 tenant/request 复合主键，Attempt 覆盖全局 PK 与 tenant/request/sequence 两类唯一键。predicate 必须从数据库现有目标行反查 Feedback，排除当前 OLD 行本身；只阻断会删除受保护目标的冲突，不影响普通更新、当前受保护行的非归因字段补账或无冲突唯一键变更。
6. 不全局开启 `recursive_triggers`：既有迁移含多组同表/跨表 trigger，全局改变连接 PRAGMA 会扩大兼容面；显式冲突守卫在 SQLite 默认 0 和调用方自定义 PRAGMA 下都可审计。
7. down 固定按 `PRAGMA foreign_keys=ON -> BEGIN IMMEDIATE -> TEMP business-fact guard -> DROP 六个 0039 trigger -> TEMP FK/integrity guard -> COMMIT` 执行。business guard 完整复用 0038 六类口径：Feedback、Event、Aggregate、`feedback.created` subscription、delivery 和关联 delivery attempt；任一存在固定 `prd049_down_business_facts_present`，六个 trigger、数据和 `schema_migrations.0039=applied` 必须原样。空事实只移除 0039 trigger，不删除 0038 表、索引或原 trigger；完整性失败固定 `prd049_hardening_down_integrity_failed`。迁移器在任何返回或异常路径 rollback 后关闭连接，TEMP 对象也随事务/连接清理。

## 12. 测试用例

### 12.1 Shared 单元测试

- payload 严格类型：rating 1/5、0/6、bool/float/string；accepted bool 与 null/0/1；tags 空/边界/重复/未知/乱序/三个互斥组。
- 规范化后标签顺序、v1 canonical JSON、golden fingerprint 和跨入口同合同。
- half-up average milli、adoption bps、五档分布、零样本和大整数。
- `feedback.created` 精确 payload/envelope、持久 ID、敏感键、Prompt/Response/Token/Key 拒绝。

### 12.2 Control API 集成测试

- admin/developer POST 201，详情可读，summary 与 ModelQuality 扩展一致。
- viewer 403 且权限先于 malformed/oversized body。
- 跨租户、随机 ID 同形 404；同租户失败/active stream 409。
- Fallback 使用最终成功模型归因。
- 同 operation 同意图 200 replay；异意图 request_conflict；同 target 新 operation already_exists；并发唯一；IntegrityError 固定重分类优先级和未知约束兜底。
- unknown/duplicate JSON key、tenant/actor 注入、控制字符、超限 body/query、重复/替换/截断 raw `X-Request-ID` 全部统一错误。
- Control `/feedback` 的 GET/PUT/PATCH/DELETE 固定 405 `infra.method_not_allowed` 且 `Allow: POST`；`/feedback/summary` 的 POST/PATCH/DELETE 固定 405 且 `Allow: GET`。body/header 的 request_id/trace_id 一致，全部保留 CORS 和 `no-store, private`。
- 事务内 Feedback/Aggregate/Event 一致；故障注入全部 rollback；提交后 Webhook 失败仍成功。
- RequestLog list/detail tenant 隔离、后端 eligibility/reason 和 feedback 扩展；ModelQuality all-time 反馈维度正确且原 PRD-048 字段/版本前后不变。
- Summary 匿名/viewer 畸形 query 仍先返回 401/403；canonical Z 时间、单连接只读 snapshot、默认 30 天、闭开边界、最大 366 天、tenant/time 先 scope、Python arbitrary-int half-up、EXPLAIN 索引、整个 endpoint 250ms budget 和并发资源门禁。

### 12.3 Gateway 集成测试

- active Virtual Key 可以评价自己的成功调用；Gateway 不存在 summary route。
- Gateway `/feedback` 的 GET/PUT/PATCH/DELETE 同样固定统一 405、`Allow: POST`、链路头和禁缓存响应，不能退回框架 `detail`；Gateway `/feedback/summary` 继续是统一 404 `infra.not_found`，不能因 405 handler 误报路径存在。
- 405 CORS 矩阵覆盖显式允许 Origin（精确 ACAO、`Vary: Origin`、Expose 含链路头）、拒绝 Origin（无 ACAO 且不反射）、无 Origin（无 CORS 头）和通配 Origin（ACAO `*`、无 Vary）。`Allow` 只保留在线上响应，不加入全局 Expose Headers，避免扩大既有精确 CORS 合同。
- 合法 POST CORS preflight 继续由 `CORSMiddleware` 返回 200；请求未允许 method 的 preflight 保持 CORS 协议级 400；不带 preflight headers 的 plain OPTIONS 进入统一 405。405 handler 不接管或伪装预检响应。
- 同 tenant 另一 Key、跨 tenant、随机 ID 统一 404。
- invalid/disabled Key 使用既有 gateway auth 错误，明文 Key 不入日志。
- Virtual Key 入口与 Control 入口竞争同一源调用时只形成一条事实。
- runtime event 入队不阻塞响应；队列/接收端失败不回滚。
- worker unavailable、queue full 和同意图 replay 重派持久 event ID 的 best-effort 边界可验证，且不宣称持久接管。

### 12.4 迁移和数据库测试

- 表、双方复合候选键/deferred FK/foreign_key_list、partial unique、标签 trigger、graph trigger、Feedback AFTER INSERT 聚合、聚合反查验真/DELETE/REPLACE 和 immutable trigger。
- 在真实默认 `recursive_triggers=0` 下参数化覆盖十三个唯一冲突分支：`INSERT OR REPLACE` 的 Feedback 五支、Event 两支、RequestLog 一支、final Attempt 两支，以及从未引用源行发起的 `UPDATE OR REPLACE` RequestLog 一支、final Attempt 两支。每支只制造目标候选键冲突，断言固定 immutable 错误、事务回滚、原 Feedback/Event/Aggregate/parent 全字段不变；tenant-qualified 冗余候选键和受双向一对一图约束的 Event 两支 predicate 另做 trigger SQL introspection，不能依赖多个 BEFORE trigger 的执行顺序。
- 租户/actor 反例证明：另一 tenant 可使用相同 source，另一 actor 可使用相同 operation；跨 tenant 伪造全局 Feedback/Event/Attempt ID 仍按已有冲突行阻断并由领域层归一为 `feedback.record_failed`，不能泄露原租户。Attempt PK 分支改变 NEW tenant/request/sequence，复合键分支改变 NEW attempt ID，锁定守卫锚点。
- 直接 SQL 伪造 tenant/actor/归因/tags/event、只插 Feedback、只插 Event、ghost aggregate、漏计/错计 aggregate、DELETE/INSERT OR REPLACE aggregate、更新或删除必须失败。
- 已反馈 RequestLog/final Attempt 的归因字段更新、DELETE、会漂移受保护字段的唯一冲突 INSERT，以及从未引用源行覆盖受保护目标的唯一冲突 UPDATE 均失败；RequestLog 受保护字段相同的生产 `ON CONFLICT DO UPDATE` 仍可执行其现有字段更新；后续直接 UPDATE 补齐 Usage/Token/成本的兼容路径也继续成功。
- 0039 空事实 down 后保留完整 0038，随后 0038 down 回到 0037；反馈、事件、aggregate、`feedback.created` subscription、对应 delivery 或其关联 `webhook_delivery_attempts` 任一存在时，0039 首次 down 即原子失败，六个 trigger、事实和 migration 状态原样。
- 正向迁移、空迁移、成功回滚、缺少 down 和 guard 失败路径都验证连接已关闭。
- `foreign_key_check=0`、`integrity_check=ok`。

### 12.5 Console 单元与浏览器测试

- PAGE-070 成功、错误、已反馈、不可反馈、标签上限、重复提交、XSS escape。
- POST owner 绑定详情/form/operation；离页、切详情、登出、角色/租户/Session 切换和迟到 200/401。
- PAGE-071 零样本、筛选、分页、刷新、错误、长模型名/标签和整数显示。
- viewer 无 nav/DOM/预取；手写 API 仍 403。
- 1280x720、900x800、390x844 无重叠、横向溢出或不可达控件；键盘与 live region 可用。
- storage/URL/DOM 检查确认无 Token、Prompt、Response、Key 明文和上一 owner 数据。

### 12.6 全量回归与真实运行

- Python 全量、Node 示例、Console 全量、Console lint、中文注释门禁全部通过。
- 对独立开发数据库实际应用至 0039，连续两次迁移均跳过 39 条，启动 Gateway/Control/Console，三个健康检查 HTTP 200。
- 使用真实 admin/developer/viewer Session 和两把 Virtual Key 验证主流程、权限、防枚举、幂等、统计和 Webhook。
- 浏览器实际操作 PAGE-070/071 并保存三视口截图、DOM/像素和 storage 证据。
- 停止服务后重新检查数据库版本、事实行、统计、事件、Webhook delivery、隐私扫描和端口清理。

## 13. 代码修改计划

1. 新增 0038 正反迁移和迁移测试；封账终审后追加 0039 防 REPLACE 加固迁移，不修改已应用 0038 checksum。
2. 新增 shared Feedback 领域包、错误码、诊断、i18n 和 Webhook 精确合同。
3. 新增 Control Session 路由和 Gateway Virtual Key 路由，注册到两个 app。
4. 扩展 RequestLog 与 ModelQuality 只读响应，不改变 PRD-048 写入/路由合同。
5. 新增/修改 Control、Gateway、shared 专项测试。
6. 新增 Console API client、报表 feature，扩展日志详情反馈和 app owner 生命周期。
7. 新增 Console 单元测试和响应式样式。
8. 更新 README、目录说明、API 文档、错误码文档和 `docs/testing/PRD-049-tests.md`。
9. 执行专项、全量、迁移、真实三服务、真实 API、三视口浏览器和停止后终验。

## 14. 编码前两轮设计自审记录

### 第一轮：边界、安全、权限和租户隔离

已执行主 Agent 自审，发现并修订：

1. 初稿只要求服务层同时写 Feedback/Event，直接 SQL 仍可能留下单边事实。已改为 tenant-qualified 双向 deferred 外键，加逐字段 graph trigger，并新增单边提交数据库测试。
2. 初稿只限制 request_id 长度，未限制字符集。已冻结源调用 ID、操作请求 ID、trace ID 的 ASCII 安全字符集，拒绝换行、控制字符和业务正文进入链路字段。
3. 初稿未说明 Virtual Key 无法写 user audit 的处理。已明确不扩展高频管理审计，以不可变 Feedback/Event 作为两类 actor 的统一权威审计链，禁止伪造 user actor。
4. 独立审查发现 Gateway summary 会让报表聚合争用模型热路径。已移除 Gateway GET，仅保留同 Key POST；Summary 回到 Control，并增加认证前置、默认/最大窗口、offset、statement timeout 和 EXPLAIN 资源门禁。
5. 初稿 actor 约束不足。已冻结 exact-one CHECK、active role/Key graph trigger、nullable source Key 的 Session 兼容口径和后端 `feedback_eligible/reason`。
6. 初稿 Webhook envelope 无法区分 actor。已在安全 payload 增加 actor_type，冻结 event/object/actor/trace 图绑定，并明确不可变 Event 取代双 actor 管理审计。
7. 初稿未冻结幂等指纹机器合同。已增加 v1 canonical JSON、字段顺序、编码参数、排除字段、golden vector 和版本升级规则。
8. 初稿未区分 all-time aggregate 与窗口 Summary。已明确 ModelQuality 只读 all-time 派生表，Summary 从原始事实按 tenant/time 先 scope 后 group。
9. 初稿未覆盖提交后崩溃和 queue full。已冻结 replay 复用持久 event ID 再投递、delivery 去重、Gateway best-effort 非持久边界，并保持外部失败不回滚。
10. 初稿未覆盖 Webhook delivery 的 down 风险。已把 `feedback.created` delivery 及其关联 `webhook_delivery_attempts` 加入不可降级事实守卫。
11. Console 调查发现移动端反馈入口和 15 秒刷新边界不够精确。已固定反馈区位置、独立 mutation generation、dirty 草稿和读取刷新不得取消写请求。

本轮初审因 Gateway 报表边界等问题未通过；完成上述修订并逐项复核后通过。双 POST 入口均从可信认证上下文恢复 tenant/actor；viewer 后端拒绝；跨租户/跨 Key 同形 404；报表只在 Control；数据库、API、事件、日志和 Console 生命周期的租户/敏感边界已形成闭环。进入第二轮前仍需独立复核兼容性和迁移可执行性。

### 第二轮：兼容性、迁移、并发和可验收性

已执行主 Agent 与独立 Agent 的兼容性审查，初审未通过；发现并修订：

1. 双向 deferred FK 只有全局 PK/UNIQUE，SQLite 实际 DML 会出现 foreign key mismatch。已为两侧明确增加完全匹配的 tenant-qualified composite candidate key、列级 FK 对应和真实 commit/foreign_key_list 测试。
2. 初稿 migration 只描述 guard 意图，没有冻结 `executescript` 可执行的 PRAGMA/transaction/RAISE 顺序。已明确 up/down 的事务前 PRAGMA、TEMP table + TEMP trigger 固定异常、循环表拆除和双完整性 guard。
3. 聚合表虽校验 INSERT/UPDATE，仍可直接 DELETE。已增加无条件 DELETE trigger、REPLACE/漏计测试和 down 先拆 trigger 的顺序。
4. 统一中间件会替换/截断外部 request ID，可能制造幂等碰撞。已要求鉴权后、body 前核对 ASGI raw header 数量、ASCII、长度和 raw==state，并覆盖重复/碰撞测试。
5. Summary 的多个查询原可跨不同 SQLite snapshot。已冻结单 connection、query_only、显式只读事务、单次 now/window、canonical Z 时间和整个 endpoint 共用的 250ms deadline。
6. Feedback 创建后父 RequestLog/Attempt 仍可更新归因字段。已设计 parent-side update/delete trigger，仅锁定反馈相关字段并保留 Usage/Token/成本补账。
7. 初稿并发 IntegrityError 没有未知约束兜底。已固定 rollback 后新连接按 operation、source、record_failed 的优先级分类，并把 busy/locked 独立映射 unavailable。
8. legacy 归因回退过宽。已固定只有 attempt_count=0 且 final ID 为空才使用 RequestLog；存在 attempt 的不完整组合失败关闭，详情与 POST 共用同函数。
9. 发现前序 tenant-qualified unique index 已由 0033/0034 建立。0038 改为断言复用，down 不删除前序索引。
10. 补齐 `application/json` media type、replay 使用本次响应 trace/持久 Event 保留首次 trace、Python arbitrary-int half-up、Feedback 写锁 250ms 和报表确定排序等机器合同。

独立 Agent 已对原始 findings 做定向闭环复核：3 个 P0、5 个 P1 及 P2 均实质关闭；最后一处 statement/endpoint budget 文案也已统一。本轮结论为通过，可以进入编码，但不得把设计通过等同于 PRD 完成。

## 15. 实现后第三轮安全与兼容复审

状态：2026-07-20 已完成并修订，通过。本轮只核对真实实现、安全边界和回归兼容性，不能替代最终 Runtime、Browser 与静止终态验收。

1. 链路 ID 原实现只拒绝少数 Token 形态，完整凭证或长片段仍可能借 `X-Request-ID` 进入错误响应、结构化日志或持久事实。安全中间件现统一丢弃完整 Token、长前缀/中段/后缀、带装饰字符的至少 16 字符片段、`Bearer<TAB>`、裸 Token 和错误 scheme；Feedback raw/state 门禁把被替换的外部值固定映射为 `feedback.input_invalid`，并以响应、日志、数据库、事件和队列零命中回归锁定。
2. Summary 原先从路由或 dependency 后才开始计时，真实 SQLite 写锁可以先耗尽鉴权时间再获得新的 250ms。实现改为中间件用 `time.monotonic()` 记录绝对 deadline；Session 鉴权连接按剩余预算设置 `busy_timeout` 并始终在 `finally` 关闭，鉴权、并发门禁、worker、只读事务、progress handler 和 DTO 共用同一剩余预算，真实 `BEGIN IMMEDIATE` 锁场景也不能绕过 endpoint 时限。
3. POST 与 Summary 曾短暂复用错误的权限 dependency 方向。最终路由固定为 POST 使用 `require_feedback_user`，Summary 使用 `require_feedback_summary_user`；viewer 在正文/query 解析前 403，Summary 的鉴权与查询才受同一绝对 deadline 约束。
4. 双入口并发、未知 SQLite 约束和提交后副作用经独立复核：Control/Gateway 竞争同一源调用只提交一个不可变事实；锁与未知一致性错误使用固定分类；Gateway queue 或 Control Webhook 失败不回滚已提交 Feedback/Event，且日志不保存 payload 或凭据。
5. Browser 验收器在实现审查中补齐三角色后端探针、viewer 零 DOM/预取、截图仅发生于成功登录后的业务页、UI/API logout 与数据库精确兜底、异常固定投影及最终 JSON 再次敏感扫描。验收脚本不会把 URL、Authorization、Cookie、响应正文、底层异常 message 或 stack 写入证据。
6. 冻结代码全量回归暴露了 PRD-048 测试对固定自然日和“0037 永远是最高迁移”的隐含假设。测试夹具已改为基于 Score 当前 `updated_at` 构造未来 UTC 秒级事实，历史迁移回归则显式先回滚 0038 并要求重新上行包含 0038；原有乱序时间、版本单步、down guard 和历史保真断言均保留，没有通过放宽生产约束换取通过。
7. 首轮真实 Browser 在 viewer Summary 403 后安全失败，证明路由内设置缓存头不能覆盖认证/RBAC dependency 的提前返回。最终实现把 `/feedback` 及子路径的 `no-store, private` 提升到统一中间件，Control/Gateway 的 401/403/404/409/422 与成功响应共享同一缓存边界，并保留 Browser 对真实 403 header 的强断言。
8. 第二次独立终审继续发现 Starlette 的最外层 `ServerErrorMiddleware` 会让未捕获异常 500 绕过 request-context 与内层 CORS 中间件，导致 Feedback 500 缺禁缓存和跨域读取合同。最终实现抽出敏感路径缓存规则供普通响应与异常处理器复用，并按实际 Starlette 1.3.1 语义补 allowed Origin、`Vary: Origin` 和 expose headers；Control/Gateway 参数化测试同时覆盖显式允许、拒绝、无 Origin、服务专属 expose 和通配 Origin，未授权来源不会被反射。

复核结论：当时对链路凭证、绝对 deadline、后端 RBAC/租户、并发唯一、迁移兼容、500 安全响应头和浏览器证据边界未发现未解决 P0/P1；该结论已被第四轮 REPLACE/405 反例推翻，不再作为封账依据。PRD 状态仍须等待加固后冻结代码全量、真实三服务、Runtime、Browser、日志/数据库隐私和停止后静止终态全部通过后再更新。

## 16. 封账前设计增补自审

### 第四轮：封账终审的不可变、统一错误与资源关闭增补

本轮由独立 Agent 与主 Agent 对冻结实现做反例审查，初审未通过，并完成以下设计修订：

1. SQLite 的 `INSERT OR REPLACE` 会先删除唯一冲突行；默认 `recursive_triggers=0` 时不会触发既有 DELETE trigger。已在隔离验收库实证 Feedback/Event 可成对替换并提交，评分、trace 和 aggregate 随之改变，因此仅有 UPDATE/DELETE trigger 不构成不可变闭环。
2. 相同绕过也影响已反馈 RequestLog 和 final Attempt 的 parent DELETE trigger。设计从两张事实表扩大为四类唯一冲突守卫，阻断相邻父快照漂移，不只修复最先发现的表面用例。
3. 框架默认 405 仅返回 `detail`。已新增共享 `infra.method_not_allowed` 合同，要求 Control/Gateway 的 Feedback 不支持方法带统一错误体、链路头、`Allow`、CORS 和禁缓存。
4. 迁移器原实现没有在全部 success/empty/error return path 显式关闭连接；down guard 又会在事务前关闭 foreign key。设计改为 `apply_migrations` 与 `rollback_last_migration` 均由最外层 `finally` 关闭连接，并增加真实关闭断言。
5. 0038 已进入开发与验收数据库，原地编辑会触发 checksum 变化并破坏升级兼容。设计采用追加 0039，仅新增 trigger 和 guard，不改 0038 或业务数据。

本轮在补齐上述机器合同前不通过；完成反例复现、影响面扩展和迁移版本决策后，进入第五轮兼容性复核。

### 第五轮：加固兼容性、租户权限与可验收性复核

1. 四个 BEFORE INSERT 守卫全部以现有行和 tenant-qualified 关系判断，不接受客户端 tenant/actor，也不改变正常新建、后端 RBAC、防枚举或幂等重分类；同 operation 并发仍由 IntegrityError 后的新连接按既有优先级恢复。
2. 0039 不修改表列、历史行或 0038 checksum；up 使用精确先验与完整性 guard，down 复用六类业务事实 guard，空库逐版本回滚可恢复 0038/0037。未采用全局 `recursive_triggers=ON`，避免改变既有 trigger 递归语义；RequestLog 条件守卫保留受保护字段相同的生产 upsert，并由独立直接 UPDATE 测试覆盖 Usage/Token/成本补账兼容性。
3. 405 处理只白名单复制 Starlette 计算出的 `Allow`，其余正文统一来自错误目录；普通中间件继续补 request_id/trace_id、CORS 和 Feedback cache-control，不引入新的认证或租户读取。`Allow` 不加入全局 CORS expose；合法/非法 preflight 继续由 `CORSMiddleware` 使用原 200/400 协议响应，plain OPTIONS 才进入统一 405。
4. 连接关闭调整覆盖 apply 成功、全部跳过、checksum/目录错误、无迁移回滚、缺少 down、down SQL/commit 失败和正常回滚；关闭发生在返回或重新抛出之后的 `finally`，不改变事务 rollback/commit 顺序。
5. 验收计划已增加默认 PRAGMA 下的真实 REPLACE 反例、Control/Gateway 405、39 迁移正反/幂等、迁移连接关闭、Python 全量以及重建 Runtime/Browser 静止终态；旧的 38 迁移与先前运行证据不再作为最终封账依据。

第五轮结论：当时的四个 INSERT 冲突守卫仍遗漏 `UPDATE OR REPLACE` 目标删除旁路，因此该结论已被第六轮反例推翻，不能作为编码放行依据。

### 第六轮：`UPDATE OR REPLACE` 冲突目标反例审查

1. 主 Agent 与独立审查 Agent 从 0038 实际 trigger predicate 交叉发现：RequestLog/Attempt 的 UPDATE 守卫只以 `OLD` 是否已被 Feedback 引用为条件。攻击者可从另一条未引用源行执行 `UPDATE OR REPLACE`，把唯一键改为受保护目标键；SQLite 会隐式删除目标，默认 `recursive_triggers=0` 又不执行目标 DELETE trigger，而 BEFORE INSERT 守卫完全不参与 UPDATE 语句。
2. 该旁路分别影响 RequestLog tenant/request 复合主键、Attempt 全局 PK 与 tenant/request/sequence 复合唯一键，共新增三条攻击分支。Feedback/Event 自身的 BEFORE UPDATE 无条件拒绝，Aggregate 的 INSERT/UPDATE 反查验真已经覆盖，不需要增加同类 UPDATE trigger。
3. 设计把 0039 从四个扩为六个持久 trigger：保留四个 BEFORE INSERT 守卫，新增 RequestLog 与 Attempt 两个 BEFORE UPDATE 冲突目标守卫。新增 predicate 必须查询即将因唯一冲突被删除的数据库目标行，并用 rowid 排除当前 `OLD` 行；不能只比较 NEW，也不能重复阻断当前行自己的普通 UPDATE。
4. 迁移 up/down、失败原子性、Runtime/Browser schema 核验和测试矩阵全部同步为六个 trigger、十三个冲突分支。未完成这些机器合同前，第六轮不通过，编码保持暂停。

### 第七轮：UPDATE 加固兼容性、租户与回滚复核

1. 对生产 RequestLog `INSERT ... ON CONFLICT DO UPDATE`，BEFORE INSERT 条件守卫仅在 11 个既有保护字段漂移时拒绝；进入 UPDATE 分支后，冲突目标就是当前 `OLD` 行，新增 UPDATE 守卫通过 rowid 排除自冲突，最后仍由 0038 字段级 UPDATE 守卫裁决。保护字段相同的现有写法和非归因补账不受影响。
2. 普通 UPDATE 若没有另一行占用 NEW 唯一键，新增目标查询为空；未被反馈引用的行之间仍按原约束更新。攻击分支即使改变 NEW tenant、request、attempt ID 或 sequence，守卫也从数据库目标行反查其 final-attribution/Feedback 关系，不信任 NEW tenant 或调用方身份。
3. 两个新增 trigger 不读取 API tenant、Session、Key 或敏感正文，只保护已持久化租户图；跨租户复用全局 Attempt ID 仍命中目标，错误由数据库固定 immutable 文本和领域统一错误收敛，不能披露目标租户。
4. 0039 down 在任何一类业务事实存在时先于 DROP 失败，六个 trigger 和 migration 状态原样；空事实才精确删除六个 trigger。缺任一 trigger、FK/integrity 异常或 SQL 错误均在显式事务中回滚并由迁移入口关闭连接。
5. 验收增加三条真实 `UPDATE OR REPLACE` 反例、合法 upsert/普通 UPDATE 对照、默认与开启 recursive trigger 两种连接语义的可审计断言，并要求 Runtime/Browser 在消费数据库前重新核对六个 trigger。

第七轮结论：十三个唯一冲突分支、正常生产 upsert、直接补账、租户图、0039 正反迁移和统一 405/连接关闭边界均已形成可执行机器合同，可以恢复加固编码；仍不得在冻结代码专项、全量、真实三服务、Runtime、Browser、隐私扫描和停服数据库验收全部通过前标记 PRD-049 完成。

## 17. 最终实现与验收结论

最终权威批次为 `artifacts/prd049/20260720T181908`。冻结代码通过 Python 根级 `783/783`、PRD-049 迁移专项 `33/33`、全迁移 `56/56`、Runtime 验收器 `9/9`、Node 示例 `33/33`、Console 修复后 `287/287`、Console lint `114 files`、Browser 验收器 `16/16`，Ruff、compileall 和中文注释门禁均通过。PAGE-071 最终修复了 `waitForResponse` 已返回但旧 Summary DOM 尚未收敛的竞态；验收器现等待当前响应数据进入页面，反向单测证明旧 DOM 不得被误判为成功。

迁移从未改动的 0038 开发库副本只应用 0039，随后连续两次 `applied=[] / skipped=39`。最终 0039 checksum 为 `4f94e14f916aded4c142c2098fa5e9ee34b79e84879d8aff720d59c54fd6335e`，六个加固 trigger 完整存在。十三个 INSERT/UPDATE OR REPLACE 冲突分支、合法 RequestLog upsert、直接 Usage/Token/成本补账、up/down 原子失败、六类事实 guard 和迁移连接关闭均由真实 SQLite 测试覆盖。

Runtime `status=passed`：Session 与 Virtual Key 两条主流程形成 2 条 Feedback/Event/delivery，Summary 为 `2 / 4500 milli / 5000 bps`；admin/developer/viewer 后端 RBAC、跨租户/跨 Key 防枚举、统一 405/`Allow`、`feedback.created` HMAC、PRD-048 统计隔离和数据库/日志隐私全部通过。最终 Browser `status=passed`，admin、developer、viewer 覆盖 1280x720、900x800、390x844 三档共 15 张非空 PNG，cleanup 通过且 stderr 为 0；`browser-result.json` 与 `browser.stdout.json` 字节一致，SHA-256 为 `3743a40603457cd303b66be8dd0959abf6945efee9e5396afaabe4f70a23a2e9`。人工视觉复核的空白、横向溢出、控件裁切、非连贯重叠和文字遮挡五类发现均为 0。

停止服务后日志审计得到 909 行结构化 JSON、809 行 access、8 行生命周期和 26 行受控 4xx warning；5xx、ERROR、未知异常和敏感命中均为 0。最终数据库 `wal_checkpoint(TRUNCATE)=(0,0,0)`，39 条迁移、最高 0039、六个 trigger、`integrity_check=ok`、FK 违规 0；保留两轮 Runtime 的 4 条 Feedback、4 条 Event、Aggregate count 合计 4 和 4 条 delivery，临时用户/Session 为 0，WAL/SHM 与三个服务端口监听均为 0。最终数据库 SHA-256 为 `80578e85c35b8548aaca2d4103ec0d416d5ef6f022566ccb31b85e621ab28d9f`。完整命令、作废批次、hash、日志和截图证据见 [PRD-049 测试记录](../testing/PRD-049-tests.md)。

PRD-049 的设计、实现、租户隔离、后端权限、统一错误码/request_id、不可变反馈闭环、迁移、专项/全量、真实运行、Browser、隐私和静止终态全部通过，可以标记完成。按 Excel `01_PRD总表` 重新识别出的下一条未完成项是 `PRD-050 - Prompt Cache 感知粘性路由`（M1-企业POC / P1）；总体 108 条 PRD 目标仍在进行中。

## 18. 2026-09-08 多后台队列下的汇总执行隔离

完整本地运行配置中，反馈回放/日志读取成功，但 Summary 6/8 次 unavailable。运行时
通过 asyncio.to_thread 使用默认线程池，与 63 个后台队列竞争；现有 PRD-075 实验统计
已用专用单线程规避同类排队。新增确定性饱和反证后，复用此模式为 Summary 提供惰性
专用进程内单线程；原有每 app Semaphore、250ms 绝对 deadline、数据库 progress handler、
独立 worker 连接和末端重新授权不变。此设计只证明可避免无关线程池排队，不预先认定
它是本地所有超时的原因。

第一轮自审：run_in_executor 必须显式 copy_context，不能丢失请求上下文、数据库 scope
和提交 guard；每次提交独立复制，不从前一工作项恢复租户/权限。执行器不存储凭据。
在取得有界槽位后才提交；运行中的 worker 仍 shield，超时/取消后真正退出才归还槽位。

第二轮自审：专用池按现有实验统计模式进程级惰性创建线程，不增加后台工作者数量或
放宽数据库/Summary deadline。测试覆盖默认池完全占满时空操作成功、上下文隔离、
超时与外部取消保留槽位、恢复以及提交失败仍归还槽位。实测服务仍失败就保留失败，
不得用独立测试或重启后零计数替代本地整体验收，更不替代 G6/G7 和 72 小时。

## 19. 2026-09-09 汇总冷启动与权限阶段诊断

本轮仅定位，不修改生产授权、路由或预算。独立 ASGI 进程使用真实本地 Workspace
Cookie 和运行库，但不启动 lifespan；正常 Control 进程继续运行全部后台队列。
因此阶段计时不等于真实服务器的 HTTP 延迟，也不能用来认证 SLA。

第一轮自审：记录函数名称、计数、耗时和错误类型，不保存 SQL、参数、Cookie 或正文。
每次请求等待 Summary worker 真正退出后才冻结结果；退出时关闭执行器、连接池并仅
注销自己创建的会话。cProfile 会改变时序，必须保留无 profiler 对照和失败样本。

第二轮自审：权限目录 JSON 聚合、FastAPI 路由准备以及 SQLite 连接准备均限诊断进程。
路由准备依赖本机 FastAPI 0.141.1 的 iter_route_contexts，不能直接成为支持 >=0.115
的生产依赖。任何后续优化仍须逐请求完整验权、在末端新快照复核撤权、保持中间件
起算的 250ms 绝对预算和有界并发；不缓存跨请求权限结论，不删除失败计量事实。

结果不支持直接发布上述原型：路由与连接准备后的无 cProfile 对照仍为
200/503/200/503。继续分离框架冷启动、完整权限读取和后台争用，不将准备成功等同于
业务成功。具体样本和限制见本条测试记录第 13 节。

## 20. 2026-09-09 幂等反馈写入尾延迟分段诊断

PRD-146 导航验证中的真实 POST 全部恢复同一反馈，但耗时 1812..4360ms。继续与
PRD-147 运行时批次联合定位，扩展既有诊断工具，不改生产实现或请求预算。

第一轮自审：诊断先确认既有固定操作和原反馈存在、无活动 Webhook 接收者；只从
数据库内存恢复同一请求意图，再经真实 Cookie、Origin、CSRF 及生产 ASGI 路由重放。
不新增模型调用、反馈或接收端，不打印原意图、SQL、Cookie 和身份值。

第二轮自审：计时覆盖完整权限、BEGIN、事务内 verify、创建/恢复和提交后 Webhook。
关闭 cProfile 的对照仍保留嵌套计时开销，不冒充线上 HTTP SLA；事实前后哈希一致与
幂等同一性分别验证，最终只注销自建会话。若定位到锁或权限开销，后续修复仍须保持
新鲜授权、不可变事实、并发唯一性与提交 guard，不根据这次诊断预先删掉安全复核。

### 实施前复核：独立连接的有界暖复用

分段证据显示每个 POST 新开三个物理 SQLite 连接，首次 SELECT 分别约 291..305ms；
创建后的零接收者派发仍重复支付首次 schema 解析成本。当前 GET 使用 request scope，
POST 刻意保持独立连接 ownership，因此不能直接把写请求改为共享一个 facade。

第一轮：增加显式的独立连接池 scope，仅本条反馈写鉴权和 worker 使用。每次 connect
获得独占 facade，close 立即 rollback/reset/归还，并使旧 facade 失效；同时打开的连接
不可复用。池继续采用已有 16 句柄上限，不缓存数据、权限或事务快照，不改变默认连接行为。
SQLite 内存库与原生 PostgreSQL 路径保持原逻辑；任何外层事务不因新 scope 提交或关闭。

第二轮：鉴权和业务 worker 分别建立 scope，线程池上下文复制继续承载 commit guard；
客户端取消后由真正 worker 的 finally 归还，不把独立连接交给 HTTP teardown。保留每次
完整鉴权、Origin/CSRF、同意图幂等、事务末端复核、唯一约束、250ms 写锁和一秒只读恢复。
测试先证明物理复用、并行隔离、状态重置、异常与旧句柄失效；再跑已有 Workspace/Legacy
API、撤权、冲突和读汇总回归。冷解析优化不等同于后台租约故障已修复。

补充复核：SQLite 首轮 API 11 项中 1 项汇总断言失败，持久库已有一条反馈。默认窗口
按秒截断且上界排除，旧断言依赖请求自然跨秒；不修改产品时间语义。测试先按实际返回
窗口核对默认统计，再显式验证创建时刻作为上界排除、作为下界纳入和同窗口跨租户隔离。
另补真实 asyncio 取消测试，确认 HTTP 取消期间 worker 仍独占连接，退出才 rollback/归还。
