# 调用反馈与采纳率 API

本文档对应 `PRD-049 - 调用反馈与采纳率记录`。完整需求、数据模型、事务边界、编码前两轮设计自审、实现后第三轮安全复审，以及第四至第七轮封账加固复审见 [PRD-049 设计文档](../prd/PRD-049-request-feedback-adoption-records.md)；专项、全量、迁移、Runtime、Browser、日志和静止数据库证据见 [PRD-049 测试记录](../testing/PRD-049-tests.md)。

## 1. 入口与认证

| 服务 | 方法与路径 | 身份 | 权限 |
| --- | --- | --- | --- |
| Control API | `POST /feedback` | Console Session Bearer Token | `admin`、`developer` |
| Control API | `GET /feedback/summary` | Console Session Bearer Token | `admin`、`developer` |
| Gateway | `POST /feedback` | OpenAI-compatible Virtual Key | active Key，且只能评价同一 Key 产生的调用 |

Gateway 不注册 `GET /feedback/summary`。报表只在控制面执行，避免聚合扫描与模型调用热路径争用资源。
`viewer` 即使直接调用 Control API 也由后端返回 `403 auth.permission_denied`；前端隐藏入口不替代权限校验。

所有响应都使用统一 `request_id`、`trace_id` 和错误结构。客户端不得发送 `tenant_id`、actor、
`model_id` 或 `provider_id`；服务端只从认证上下文与源 RequestLog 恢复这些可信字段。
POST 与 Summary 的所有响应都使用 `Cache-Control: no-store, private`，包括未捕获基础设施异常产生的
500，避免反馈事实、租户统计或错误链路被浏览器、代理或共享缓存复用。500 的 CORS Header 继续按
服务 allowlist 与 expose 配置生成，未授权 Origin 不会被反射。

### 1.1 方法、405 与 CORS

- Control API 的 `/feedback` 只允许 POST；GET/PUT/PATCH/DELETE 和不带预检头的 plain OPTIONS 返回 `405 infra.method_not_allowed`，并保留 `Allow: POST`。
- Control API 的 `/feedback/summary` 只允许 GET；POST/PUT/PATCH/DELETE 和 plain OPTIONS 返回同一 405 合同，`Allow: GET`。
- Gateway 的 `/feedback` 与 Control POST 保持相同 405 合同；Gateway 不注册 Summary，`GET /feedback/summary` 继续返回统一 `404 infra.not_found`，不能因共享 405 handler 伪装成路径存在。
- 405 正文仍使用统一 `code/message/request_id/trace_id/suggestion/diagnosis`，固定 message 为 `Request method is not allowed.`，suggestion 为 `Use a method listed in the Allow response header.`。handler 只复制框架计算出的非空 `Allow`，不透传异常正文或其他 Header。
- 合法 CORS preflight 继续返回 200，请求 method 不在 allowlist 的 preflight 保持协议级 400；只有 plain OPTIONS 进入统一 405。`Allow` 只存在于线上响应，不加入全局 `Access-Control-Expose-Headers`；显式允许、拒绝、无 Origin 和通配 Origin 仍使用服务现有精确 CORS 合同。

## 2. 创建反馈

### 2.1 请求

```http
POST /feedback HTTP/1.1
Authorization: Bearer <session-token-or-virtual-key>
Content-Type: application/json
X-Request-ID: feedback_operation_20260720_001
X-Trace-ID: trace_feedback_20260720_001

{
  "request_id": "req_source_call",
  "rating": 5,
  "accepted": true,
  "tags": ["accurate", "helpful"]
}
```

两个 `request_id` 的语义不同：

- 正文 `request_id` 是被评价的源模型调用。
- 请求头 `X-Request-ID` 是本次反馈操作的链路 ID 和幂等键；响应顶层 `request_id` 回显该值。

请求体必须精确包含四个字段：

| 字段 | 约束 |
| --- | --- |
| `request_id` | 1..128 个 ASCII 安全字符 `[A-Za-z0-9._:-]` |
| `rating` | JSON integer，范围 `1..5`；布尔值不能冒充整数 |
| `accepted` | 必填 JSON boolean；不接受 `null`、`0/1` 或字符串 |
| `tags` | 必填数组，最多 5 个，不重复，服务端按固定顺序规范化 |

标签白名单顺序为：

`accurate`、`relevant`、`complete`、`helpful`、`clear`、`inaccurate`、`irrelevant`、
`incomplete`、`unsafe`、`slow`。

`accurate/inaccurate`、`relevant/irrelevant`、`complete/incomplete` 三组分别互斥。接口不接受自由文本、
备注、Prompt 或 Response。

`Content-Type` 只接受 `application/json` 和可选的唯一 `charset=utf-8`。正文最大 4096 bytes，拒绝
重复 JSON key、NaN/Infinity、过深结构、未知字段和重复/被中间件改写的原始 `X-Request-ID`。
“被改写”也包括完整凭证、至少 16 字符的显著凭证片段、`Bearer<TAB>`、裸 Token 或错误认证
scheme 被安全中间件丢弃并替换为内部链路 ID；此类外部值固定返回 `feedback.input_invalid`，不得
进入响应、日志、数据库、事件或 Gateway 队列。

### 2.2 成功响应

首次创建返回 `201`；同 actor、同操作 ID、同规范化意图的恢复返回 `200`：

```json
{
  "feedback": {
    "request_feedback_id": "rfb_0123456789abcdef0123456789abcdef",
    "request_id": "req_source_call",
    "model_id": "model_default_chat",
    "rating": 5,
    "accepted": true,
    "tags": ["accurate", "helpful"],
    "actor_type": "session_user",
    "status": "recorded",
    "created_at": "2026-07-20T03:01:00Z"
  },
  "idempotent_replay": false,
  "request_id": "feedback_operation_20260720_001",
  "trace_id": "trace_feedback_20260720_001"
}
```

响应不包含 `tenant_id`、actor ID、Provider ID、Virtual Key ID、Key 明文、请求指纹或事件载荷。
一次源调用只能形成一条不可变反馈；接口不提供 PATCH 或 DELETE。

### 2.3 可反馈资格

源调用必须位于认证租户，且满足：

- `request_logs.status='success'`。
- 非流式调用没有矛盾流状态；流式调用必须 `stream_status='completed'`。
- 零 attempt 的 legacy 调用有最终 model/provider；存在路由 attempt 时必须能确认最终 succeeded attempt。
- Gateway 调用还必须满足源调用 `virtual_key_id` 与当前认证 Key 完全一致。

跨租户、跨 Key 和未知源调用统一返回 `404 feedback.request_not_found`，不披露资源真实归属。
同授权范围内但调用失败、流未完成或最终归因不完整时返回
`409 feedback.request_not_feedbackable`。

### 2.4 幂等与并发

反馈意图使用 v1 canonical JSON 和 SHA-256 指纹。客户端在响应未知时必须复用原
`X-Request-ID` 和完全相同的请求体：

- 同 actor、同操作 ID、同意图：恢复首次 Feedback/Event，`idempotent_replay=true`。
- 同 actor、同操作 ID、不同意图：`409 feedback.request_conflict`。
- 同源调用已经由另一操作创建：`409 feedback.already_exists`。

Feedback、模型反馈聚合和 `feedback.created` 持久事件在一个 SQLite 事务提交。Webhook 或 Gateway
后台队列属于提交后副作用，接收端失败不能回滚已记录反馈；重放继续使用首次持久事件 ID。

### 2.5 `feedback.created` Webhook

持久事件和对外 Webhook 共用首次 `fbe_` event ID、tenant、actor、object、trace 与创建时间。对外信封固定为：

```json
{
  "event_id": "fbe_0123456789abcdef0123456789abcdef",
  "event_type": "feedback.created",
  "timestamp": "2026-07-20T03:01:00Z",
  "tenant_id": "tenant-id",
  "actor_id": "可信 user_id 或 virtual_key_id",
  "object_id": "rfb_0123456789abcdef0123456789abcdef",
  "trace_id": "trace_feedback_20260720_001",
  "payload": {
    "request_feedback_id": "rfb_0123456789abcdef0123456789abcdef",
    "request_id": "req_source_call",
    "model_id": "model_default_chat",
    "actor_type": "session_user",
    "rating": 5,
    "accepted": true,
    "tags": ["accurate", "helpful"]
  }
}
```

`object_id` 必须等于 `payload.request_feedback_id`；`actor_id` 只来自认证上下文，业务 payload 只暴露 `actor_type`，不重复传播具体身份。payload 精确七字段，不包含操作 request ID、Provider/Virtual Key 明文、Token、Prompt、Response、Endpoint、原始错误或自由文本。订阅端失败不改变已提交 Feedback/Event；幂等重派继续使用首次 event ID、trace 和 timestamp。

## 3. 反馈汇总

```http
GET /feedback/summary?model_id=model_default_chat&from_time=2026-07-01T00:00:00Z&to_time=2026-08-01T00:00:00Z&limit=20&offset=0
Authorization: Bearer <session-token>
```

查询参数只允许 `model_id`、`from_time`、`to_time`、`limit`、`offset`：

- 时间必须成对出现，格式为 canonical UTC 秒精度 `YYYY-MM-DDTHH:MM:SSZ`。
- 未传时间时使用服务端同一次 `now` 形成默认 30 天闭开窗口。
- 最大窗口 366 天；`limit` 为 `1..100`，`offset` 为 `0..10000`。
- 重复或未知参数统一返回 `422 feedback.query_invalid`。

250ms 是从统一中间件进入时刻开始计算的 endpoint 绝对 deadline，而不是路由函数自己的 250ms。
Session dependency 的数据库鉴权与剩余 `busy_timeout`、并发门禁、工作线程、单 SQLite 连接、
`query_only` 显式只读事务、SQL progress handler 和响应 DTO 都共用同一剩余预算；鉴权锁等待不能
重置或绕过时限。预算耗尽统一返回 `503 feedback.unavailable`，鉴权和查询连接都在 `finally` 关闭。
顶层 `summary`、标签计数和 `total` 覆盖完整筛选窗口；`limit/offset` 只分页 `models` 行，前端不得从
当前页重算总计。平均评分使用 milli，采纳率使用 bps，均以任意精度整数 half-up 计算。

成功响应字段固定为 `scope_type`、`from_time`、`to_time`、`summary`、`models`、`total`、`limit`、
`offset`、`request_id` 和 `trace_id`。示例：

```json
{
  "scope_type": "tenant",
  "from_time": "2026-07-01T00:00:00Z",
  "to_time": "2026-08-01T00:00:00Z",
  "summary": {
    "feedback_count": 2,
    "rating_sum": 9,
    "average_rating_milli": 4500,
    "accepted_count": 1,
    "adoption_rate_bps": 5000,
    "rating_distribution": {"1": 0, "2": 0, "3": 0, "4": 1, "5": 1},
    "tag_counts": [{"tag": "accurate", "count": 1}]
  },
  "models": [
    {
      "model_id": "model_default_chat",
      "internal_model_id": "glm-4-flash",
      "display_name": "GLM-4 Flash",
      "feedback_count": 2,
      "average_rating_milli": 4500,
      "accepted_count": 1,
      "adoption_rate_bps": 5000,
      "rating_distribution": {"1": 0, "2": 0, "3": 0, "4": 1, "5": 1}
    }
  ],
  "total": 1,
  "limit": 20,
  "offset": 0,
  "request_id": "feedback_summary_20260720_001",
  "trace_id": "trace_feedback_summary_20260720_001"
}
```

## 4. RequestLog 与模型质量扩展

- `GET /request-logs` 每行增加 `has_feedback`。
- `GET /request-logs/{request_id}` 增加 `feedback`、`feedback_eligible`、
  `feedback_ineligible_reason`；原因码稳定为 `eligible`、`already_recorded`、`request_failed`、
  `stream_incomplete`、`attribution_unavailable`，查询仍按认证 tenant 限定。
- `GET /model-quality-scores` 增加 `request_feedback_count`、`request_feedback_rating_sum`、
  `request_feedback_average_milli`、`request_feedback_accepted_count`、
  `request_feedback_adoption_rate_bps`、`request_feedback_statistics_version` 和
  `request_feedback_rating_distribution`，与 PRD-048 的 Provider 终态统计字段保持独立。

逐请求反馈不修改 PRD-048 的 Provider 终态样本、人工先验、`statistics_version`、
`feedback_version`、`effective_quality_bps` 或 `quality_cost_v1` 路由结果。

## 5. 错误码

| HTTP | code | 说明 |
| --- | --- | --- |
| 401 | `auth.*` / `gateway.auth_*` | Session 或 Virtual Key 缺失、无效、过期或禁用 |
| 403 | `auth.permission_denied` | Control API 角色无权限 |
| 405 | `infra.method_not_allowed` | 路径存在但方法不受支持；保留非空 `Allow` |
| 404 | `feedback.request_not_found` | 当前租户/Key 范围内不可见；跨范围同形返回 |
| 409 | `feedback.request_not_feedbackable` | 已授权源调用不满足成功终态或最终归因 |
| 409 | `feedback.already_exists` | 源调用已经有不可变反馈 |
| 409 | `feedback.request_conflict` | 操作 ID 已绑定另一意图 |
| 422 | `feedback.input_invalid` | 正文、媒体类型、操作 ID 或 wire 合同非法 |
| 422 | `feedback.query_invalid` | Summary 参数、时间窗口或分页非法 |
| 503 | `feedback.unavailable` | 数据库连接、锁或 deadline 暂不可用；POST 复用原操作 ID 和同一正文重试，Summary GET 可直接重试 |
| 500 | `feedback.record_failed` | Feedback/Aggregate/Event 一致事务未能完整形成 |
| 500 | `infra.unhandled` | 路由、认证或数据库依赖的未捕获异常；只返回通用安全错误 |

错误响应继续使用统一 `code/message/request_id/trace_id/suggestion/diagnosis` 结构，不回显 SQL、
数据库路径、Key、Token、Prompt、Response、请求正文或接收端响应正文。

## 6. 数据与安全边界

- `request_feedbacks`、`model_feedback_statistics`、`request_feedback_events` 全部带 `tenant_id`。
- tenant-qualified 复合外键、actor graph、源 RequestLog/final Attempt graph 和双向 deferred FK
  防止跨租户错绑或缺少 Event 的半事实提交。
- Feedback/Event 不可更新或删除；派生聚合由原始事实逐字段验真，直接 INSERT/UPDATE/REPLACE/DELETE
  均失败关闭。
- 反馈形成后锁定 RequestLog 与 final Attempt 的身份、终态和归因字段；Token、Usage、成本与延迟补账
  仍可继续。
- 日志只记录安全对象 ID、actor 类型和幂等状态，不记录 rating、accepted、tags、Key、Token、Prompt
  或 Response。
- 0038 down 仅在 Feedback、Event、Aggregate、`feedback.created` 订阅、对应 delivery 及其关联
  `webhook_delivery_attempts` 全部不存在时允许执行，避免不可逆丢失业务事实。
- 0039 不修改 0038 表或 checksum，只追加六个冲突目标守卫：Feedback/Event 的 BEFORE INSERT、RequestLog 与 final Attempt 的 BEFORE INSERT/UPDATE。十三个参数化分支覆盖全部非冗余唯一冲突，默认 `recursive_triggers=0` 和显式开启两种连接语义均拒绝覆盖旧事实。
- RequestLog INSERT 守卫只在 11 个既有保护字段漂移时拒绝；字段相同的生产 `ON CONFLICT DO UPDATE`、当前行自身更新和后续 Usage/Token/成本补账继续允许。UPDATE 守卫以数据库目标行反查 Feedback，并用 `rowid` 排除当前 OLD 行。
- 0039 down 在六类业务事实任一存在时先于 DROP 原子失败；空事实只移除六个 0039 trigger，保留完整 0038。迁移入口在正常、幂等和异常路径都回滚/提交后关闭连接。
- PRD-049 最终数据库基线为 39 条迁移、最高 `0039`，checksum 为 `4f94e14f916aded4c142c2098fa5e9ee34b79e84879d8aff720d59c54fd6335e`。
