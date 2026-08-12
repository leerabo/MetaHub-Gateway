# Admin Assistant API

本文档描述 PRD-077 的只读管理员 AI 助手合同。当前版本使用本地确定性查询引擎，不调用外部模型，
不提供任何创建、修改、删除、启停、发布、回滚、审批、预算调整、Key 禁用或工具调用能力。

## 1. 通用安全合同

- 入口为 `POST /admin-assistant/query`，使用 `Authorization: Bearer <session-token>`。`tenant_id`、actor、
  Session 和权限只从后端认证上下文恢复；请求正文、Cookie 和自定义租户 Header 均不能覆盖 owner。
- 后端先校验 `admin_assistant.query`，再按查询类型校验完整来源域权限；PAGE-107 的导航或按钮可见性
  不构成授权。入口认证完整校验目录与矩阵；来源和最终事务重读 Session/角色指纹并逐项查询本次 grant。
- 请求和错误响应都携带与 `X-Request-ID`、`X-Trace-ID` 响应 Header 一致的 `request_id`、`trace_id`。
- 成功、认证错误、校验错误和依赖错误统一返回 `Cache-Control: no-store, private`、
  `Pragma: no-cache`、`Vary: Authorization`。
- 问题原文、问题裸 hash、回答正文、Prompt、Response、Provider Key、Virtual Key、Token、原始 Provider
  错误和策略表达式不进入数据库、日志、AuditLog、Webhook 或验收证据。
- 每个 HTTP attempt 独立审计，不接受 `Idempotency-Key`，也不保存会话历史或多轮上下文。建立可信 actor 后
  的客户端取消或响应写失败不会跳过 durable 终态审计。

## 2. 请求

请求必须是 UTF-8 `application/json` object，exact schema 只允许 `question` 一个字符串字段：

```http
POST /admin-assistant/query
Authorization: Bearer <session-token>
Content-Type: application/json
X-Request-ID: req_example
X-Trace-ID: trc_example
```

```json
{
  "question": "查看最近 7 天的调用统计"
}
```

完整 JSON wire 最大 16 KiB；问题最大 2,000 Unicode code point、最大 8 KiB UTF-8。重复字段、未知字段、
NUL、未配对 surrogate、Bidi 控制符、Unicode noncharacter 以及除换行和制表外的控制字符都会被拒绝。
endpoint 不接受 query 参数；拒绝重复的 Authorization、X-Request-ID、X-Trace-ID、Content-Type 或
Content-Length，以及 Content-Encoding、Transfer-Encoding、Content-Range、Idempotency-Key 和非规范
Content-Length。普通 Accept、User-Agent 等非安全语义 Header 可以透传。

`X-Request-ID`、`X-Trace-ID` 缺失时由共享 middleware 自动生成；外部值只保留安全字符并截断到 128 字符，
与 Authorization/Token 完整值或显著片段碰撞时会被内部 ID 替换。调用方不能用链路 Header 携带问题、
凭据或租户身份。

支持四类有界意图和最近 24 小时、7 天、30 天滚动窗口：

| 查询类型 | 内容 | 必需后端权限 |
| --- | --- | --- |
| `usage_statistics` | 调用量、成功/失败、平均延迟、Token 与 Usage 成本 | `admin_assistant.query`、`dashboard.read`、`billing.usage.read` |
| `error_explanation` | 固定错误分类、安全原因/建议、Top 5 或指定 `request_id` | `admin_assistant.query`、`request_log.read` |
| `cost_advice` | posted debit、模型集中度、Usage、预算、异常和价格的确定性建议 | `admin_assistant.query`、`dashboard.read`、`billing.usage.read`、`billing.finops.read`、`billing.budget.read`、`billing.anomaly.read`、`model_price.read` |
| `policy_explanation` | 生效策略、同优先级候选组、动作、已发布快照及指定请求的快照状态/版本 | `admin_assistant.query`、`policy.read` |

指定 `request_id` 的策略解释还要求 `request_explanation.read`。同一问题包含多个意图、多个 Request ID、
冲突窗口或“今天/昨天/本周”等依赖未提供时区的日历范围时返回歧义错误。命中修改、禁用、发布、回滚、
审批、预算调整、重放或工具执行语义时，在读取任何业务来源前按只读违规拒绝。

## 3. 成功响应

成功返回 200 和 exact-schema claim/source 图。下例只展示用量查询的部分 claim；实际字段集合由查询类型
决定，来源 URL 只能由服务端 allowlist 生成。

```json
{
  "assistant_query_id": "asq_0123456789abcdef0123456789abcdef",
  "assistant_audit_id": "asa_0123456789abcdef0123456789abcdef",
  "query_type": "usage_statistics",
  "answer": {
    "summary": "基于 2 个可验证来源生成。",
    "claims": [
      {
        "claim_id": "request_count",
        "claim_type": "integer",
        "label": "调用次数",
        "value": 2,
        "unit": "requests",
        "source_ids": ["src_overview"]
      },
      {
        "claim_id": "cost_cny",
        "claim_type": "decimal",
        "label": "用量成本",
        "value": "1",
        "unit": "CNY",
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
      "record_count": 2,
      "window_started_at": "2026-08-04T00:00:00Z",
      "window_ended_at": "2026-08-11T00:00:00Z",
      "snapshot_at": "2026-08-11T00:00:01Z"
    },
    {
      "source_id": "src_usage",
      "source_type": "usage",
      "label": "用量账本",
      "href": "/#usage",
      "record_count": 1,
      "window_started_at": "2026-08-04T00:00:00Z",
      "window_ended_at": "2026-08-11T00:00:00Z",
      "snapshot_at": "2026-08-11T00:00:01Z"
    }
  ],
  "redactions": ["content_omitted", "credentials_omitted"],
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

`claim_type` 只允许 `integer/decimal/percentage/text`。整数不会超过 JavaScript 安全整数范围；金额与百分比
使用规范十进制字符串，不使用 JSON 浮点数。每个 claim 和 recommendation 至少引用一个本次响应来源，
不存在未引用来源或图外来源 ID。错误解释额外返回 `raw_provider_error_omitted` 脱敏声明。

各查询的稳定 claim/source 目录如下；`n` 从 1 开始且最多为 5，未出现的可选事实不会用空值占位：

| 查询类型 | claim ID | 允许来源 |
| --- | --- | --- |
| `usage_statistics` | `request_count`, `success_count`, `failure_count`, `success_rate`, `average_latency_ms`, `input_tokens`, `output_tokens`, `total_tokens`, `cost_cny`, `estimated_usage_count` | `overview`, `usage` |
| `error_explanation` 窗口 | `failure_count`, `error_{n}_code`, `error_{n}_count`, `error_{n}_reason`, `error_{n}_suggestion` | `request_logs` |
| `error_explanation` 单请求 | `request_status`, `error_code`, `error_reason`, `error_suggestion` | `request_logs` |
| `cost_advice` | `request_count`, `failure_rate`, `usage_record_count`, `estimated_cost_share`, `gross_debit_cost_cny`, `top_cost_model`, `top_cost_model_share`, `active_budget_count`, `open_anomaly_count`, `model_price_count` | `overview`, `usage`, `finops_ledger`, `budgets`, `cost_anomalies`, `model_prices` |
| `policy_explanation` | `active_policy_count`, `priority_collision_count`, `published_snapshot_version`, `policy_action_{n}`, `policy_action_{n}_count` | `policies`, `policy_snapshots` |
| `policy_explanation` 单请求附加 | `request_policy_decision`, `request_policy_reason_code`, `request_policy_snapshot_status`, `request_snapshot_version` | `request_explanations` |

recommendation 是 exact 三字段 `code/message/source_ids`；message 只能由 Shared 目录从 code 派生。允许 code 为
`high_failure_rate`、`high_estimated_cost_share`、`high_model_cost_concentration`、
`priority_collision_review`、`insufficient_evidence`。客户端不得执行建议，只能把它展示为有来源支撑的
人工复核提示。

来源类型与内部链接固定为：`overview`、`request_logs`、`request_explanations`、`usage`、
`finops_ledger`、`budgets`、`cost_anomalies`、`model_prices`、`policies`、`policy_snapshots`。客户端提交的
URL、问题中的 URL、外链和 `javascript:` 均不能成为来源。

## 4. 审计、用量与事件

- 每个已认证 attempt 原子更新可信 Session `last_seen`，并写入一组 tenant-qualified `AssistantQuery`、
  `AssistantAudit`、通用 AuditLog 和 pending `assistant.queried` source；回答只有在该事务提交后才返回，
  任一写入失败时整组回滚。
- `answered`、`permission_denied`、`read_only_blocked`、`unsupported`、`ambiguous`、`rate_limited` 和
  可持久化失败均可追踪。失败记录的 claim/source/redaction 计数固定为 0。
- 当前执行模式固定为 `deterministic_read_model_v1`，模型调用数、输入/输出 Token 和模型成本均为 0，
  成本状态为 `not_applicable_local`；不会伪造 Usage 或 FinOps 分录。
- `assistant.queried` 使用稳定 `asevt_` event ID、租约、CAS、退避和最多 10 次 attempt 恢复。公开 payload
  exact 七字段为 `schema_version/query_type/outcome/claim_count/source_count/redaction_count/duration_bucket`，
  不包含问题、回答、目标 Request、金额、策略身份、URL、凭据或错误正文。
- 单进程滑动窗口为每 actor 每分钟 30 次、每 tenant 每分钟 120 次；过期空 key 会回收，registry 满时
  失败关闭。middleware 入口冻结来源 1.2 秒、总决策 1.5 秒预算，认证/tenant/admission 排队、连接初始化、
  SQL 和锁等待均计入。预算耗尽不再启动新阶段并返回固定 503；已启动的 SQLite worker 或治理事务会先
  完成回滚/提交与连接清理，因此故障路径的客户端完成时间可能超过该决策预算。
- read/write admission 都是容量 1。并发过载可以得到部分 200、部分固定
  `admin_assistant.source_unavailable` 503，但每个已认证 attempt 仍必须留下独立 Query/Audit/AuditLog/event。

## 5. 统一错误码

| HTTP | code | 含义 |
| ---: | --- | --- |
| 401 | `auth.token_missing/auth.token_invalid/auth.token_expired/auth.session_revoked` | Bearer Session 无效 |
| 403 | `auth.permission_denied` | base 或任一来源域权限不足 |
| 403 | `admin_assistant.read_only_violation` | 问题请求执行写操作 |
| 404 | `admin_assistant.source_not_found` | 当前 tenant 不存在指定来源事实 |
| 422 | `admin_assistant.request_invalid` | Header、media type、JSON 或问题边界非法 |
| 422 | `admin_assistant.query_unsupported` | 无法映射到四类只读意图 |
| 422 | `admin_assistant.query_ambiguous` | 多意图、多 ID 或时间范围歧义 |
| 429 | `admin_assistant.rate_limited` | actor 或 tenant 滑动窗口配额已满 |
| 503 | `admin_assistant.source_unavailable` | 来源数据库、锁、schema 或期限不可用 |
| 503 | `admin_assistant.answer_invalid` | claim/source 图未通过 exact 校验 |
| 503 | `admin_assistant.audit_unavailable` | 不可变审计事务无法提交 |

错误使用全局统一 exact 六字段 envelope：`code`、`message`、`request_id`、`trace_id`、`suggestion`、
`diagnosis`；其中 diagnosis 也是共享固定目录生成的结构化安全分类。错误消息不拼接问题、Header、
Request ID、数据库异常、路径、Endpoint 或任何敏感材料。

所有私有响应固定 `Cache-Control: no-store, private`、`Pragma: no-cache` 和 `Vary: Authorization`；跨域请求
可由 CORS middleware 在 `Vary` 中追加 `Origin`，客户端必须按 token 与 Origin 双重隔离缓存。429 当前不返回
`Retry-After`，客户端只能按固定 `admin_assistant.rate_limited` 做有界退避，不能从错误正文推断内部窗口。

`admin_assistant.event_unavailable` 是 outbox worker 内部固定失败分类和 ErrorSpec，不由 query endpoint
直接返回给浏览器；已提交的 Query/Audit 由稳定 source 在后台恢复投递。

## 6. 部署配置

`METAHUB_ADMIN_ASSISTANT_EVENT_ENABLED=true` 默认启动 source outbox 恢复 worker；关闭仅暂停 drain，
不会跳过 Query/Audit/event 原子写入。`METAHUB_ADMIN_ASSISTANT_EVENT_POLL_INTERVAL_SECONDS=2` 控制空闲轮询，
允许 1..60 秒。这两个值是部署级配置，不能由租户请求覆盖。

每个 app/event loop 延迟创建两条长期 SQLite 连接：认证/来源共用 read-stage 连接，最终复核/持久化共用
write-stage 连接。两者使用 WAL 与 `synchronous=FULL`，只在容量 1 admission 下跨 worker 线程复用；认证、
来源和最终 SQL 的 progress handler、`busy_timeout`、事务与 `query_only` 状态都会在退出时恢复。lifespan
停止时先禁止重开并排空两个 slot，再回滚、关闭连接；宿主取消也必须等待这一清理完成。

## 7. 已验证运行基线

- PRD-077 最终 Runtime 在空 SQLite 上验证 `70/0070 -> 70/0070 -> 69/0069 -> 70/0070`，每阶段
  `integrity_check=ok`、外键违规 0；真实 Control 停服恢复后 source outbox 清空。
- 20 个并发已认证 attempt 中 9 个返回 200、11 个按来源绝对期限返回固定
  `admin_assistant.source_unavailable` 503；数据库保留 20 组唯一且一一对应的 Query/Audit/AuditLog/Event，
  证明来源过载不会绕过治理审计。
- PAGE-107 实际 Browser 验证四类查询、角色来源权限、动态撤权、后端 403、来源导航 allowlist、防篡改、
  取消/迟到/登出清理及双视口布局；外部请求、私密网络字段和敏感明文命中均为 0。
- 权威批次、SHA-256、测试分组和隐私/清理计数记录在 `docs/testing/PRD-077-tests.md`。API 合同仍保持
  `all_108_prds_complete=false`，不包含 PRD-078 的移动审批能力。
