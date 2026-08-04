# 调用日志高级查询与 SavedFilter API

本文档冻结 PRD-055 “Trace查询与过滤增强”的公开 Control API、后端权限、租户隔离、排序分页、个人 SavedFilter、统一错误码及 `log.query_executed` 事件合同。完整技术设计、两轮编码前自审及两轮实现后自审见
[PRD-055 技术设计](../prd/PRD-055-trace-query-enhancement.md)。

## 1. 能力边界

本能力在现有 `RequestLog` 只读列表上增加组合筛选、稳定排序、有界分页和个人保存筛选，不引入全文搜索引擎，也不查询 Prompt、Response、Provider 原始正文或任意 JSON 字段。

公开端点如下：

| 方法 | 路径 | 成功状态 | 用途 |
| --- | --- | ---: | --- |
| `GET` | `/request-logs` | 200 | 按 inline v1、SavedFilter 或 legacy 参数查询日志 |
| `GET` | `/request-logs/saved-filters` | 200 | 列出当前用户自己的 active SavedFilter |
| `POST` | `/request-logs/saved-filters` | 201 | 创建当前用户的 SavedFilter |
| `PATCH` | `/request-logs/saved-filters/{saved_filter_id}` | 200 | 按乐观版本更新当前用户的 SavedFilter |
| `DELETE` | `/request-logs/saved-filters/{saved_filter_id}?version=...` | 204 | 按乐观版本软删除当前用户的 SavedFilter |

所有端点使用 `Authorization: Bearer <session_token>`。后端只允许 `admin` 和 `developer`，匿名访问返回统一 401，`viewer` 返回统一 403。RBAC 必须先于 query、path 和 JSON 业务校验执行，前端隐藏入口不能代替后端权限。

`tenant_id`、`owner_user_id`、actor 和 session 只取可信认证上下文。任何客户端提交的 tenant、owner 或 actor 字段都不构成授权依据。所有成功响应设置：

```text
Cache-Control: no-store, private
Pragma: no-cache
Vary: Authorization
X-Request-ID: <本次 Control API request_id>
X-Trace-ID: <本次 Control API trace_id>
```

JSON 成功响应和统一错误响应中的 `request_id`、`trace_id` 必须与响应头一致。DELETE 204 没有响应正文，但仍保留两个链路头。

## 2. GET /request-logs

### 2.1 三种互斥查询来源

一次请求只能使用下列一种筛选来源。出现未知 query、重复单值 query 或混合来源，统一返回 `request_log.query_invalid`。

| 模式 | 筛选来源 | 允许同时出现 |
| --- | --- | --- |
| inline v1 | `filters=<Base64URL v1 JSON>` | `sort_by`、`sort_direction`、`limit`、`offset` |
| SavedFilter | `saved_filter_id=sf_<32 hex>` | 仅 `limit`、`offset` |
| legacy | 显式业务 query，或无业务筛选 | `sort_by`、`sort_direction`、`limit`、`offset` |

SavedFilter 模式不得携带 `filters`、任何 legacy 业务参数、`sort_by` 或 `sort_direction`。服务端必须按当前 `tenant_id + owner_user_id + saved_filter_id + active` 重新加载规范化筛选和排序，不能相信客户端回传的筛选副本或排序。

inline 模式不得携带任何 legacy 业务参数。legacy 模式继续兼容：

```text
request_id trace_id status model request_model resolved_model
provider_code virtual_key_id environment_id project_id application_id
snapshot_version error_code created_from created_to
latency_ms_min latency_ms_max total_tokens_min total_tokens_max
cost_amount_min cost_amount_max currency terminal_stage
```

legacy `model` 保留“匹配 request_model 或 resolved_model”的历史 OR 语义；inline v1 中的 `request_model` 和 `resolved_model` 是两个可独立组合的精确条件。

### 2.2 filters 编码

`filters` 是 UTF-8 JSON 的无填充 Base64URL 编码。Base64URL 只解决 query 传输，不提供加密。wire 最长 4096 个字符，解码后最多 3072 bytes；只允许 `A-Z a-z 0-9 _ -`，不接受 `=` padding。

解码后的根必须是 JSON object，最多 24 个字段，必须带原生整数 `"v": 1`。未知版本、未知字段、重复 JSON key、数组、嵌套对象、NaN/Infinity、非 UTF-8 或类型不匹配全部失败关闭。

示例原文：

```json
{
  "v": 1,
  "request_id": "req_01J...",
  "trace_id": "trc_01J...",
  "status": "failed",
  "request_model": "company-default",
  "resolved_model": "deepseek-chat",
  "provider_code": "deepseek",
  "virtual_key_id": "vk_01J...",
  "environment_id": "env_01J...",
  "project_id": "prj_01J...",
  "application_id": "app_01J...",
  "snapshot_version": 7,
  "error_code": "gateway.upstream_error",
  "created_from": "2026-07-27T00:00:00Z",
  "created_to": "2026-07-28T00:00:00Z",
  "latency_ms_min": 100,
  "latency_ms_max": 5000,
  "total_tokens_min": 1,
  "total_tokens_max": 100000,
  "cost_amount_min": "0.000001",
  "cost_amount_max": "100.000000",
  "currency": "CNY",
  "terminal_stage": "provider"
}
```

字段约束：

| 字段 | 类型和约束 |
| --- | --- |
| `request_id`、`trace_id` | 1..128 个 ASCII 安全字符：字母、数字、`_`、`.`、`:`、`-` |
| 其他 ID、模型、Provider、错误码 | 1..160 个安全文本字符；拒绝控制字符和任意正文 |
| `status` | `success` 或 `failed` |
| `snapshot_version` | 1..SQLite 正整数上界 |
| `created_from`、`created_to` | UTC RFC 3339，必须满足 from < to，最大窗口 30 天 |
| latency/token min/max | 非负原生整数；下界不得大于上界 |
| cost min/max | 非负十进制字符串，最多 6 位小数；不得使用 JSON number/float |
| `currency` | `CNY` 或 `USD` |
| `terminal_stage` | `terminal_stage_v1` 枚举 |

`terminal_stage_v1` 为：

```text
completed authentication rate_limit budget dlp policy routing keypool
provider usage_and_finops logs_and_webhooks unknown
```

阶段只表示由成功终态或冻结错误码可以证明的完成/终止点；`unknown` 表示证据不足，不能解释为其他阶段均已通过。

`tenant_id`、owner、actor、role、`include_raw`、Prompt、Response、Provider Key、Virtual Key 明文、Token、Endpoint URL 均不在 v1 白名单内。

### 2.3 排序与分页

`sort_by` 白名单：

```text
created_at latency_ms total_tokens estimated_cost_amount
```

`sort_direction` 为 `asc` 或 `desc`，默认 `created_at desc`。每种排序都追加同方向的 `created_at`、`request_id` 稳定 tie-breaker；NULL 始终位于非 NULL 之后，保证相同数据快照的分页顺序可复现。

`limit` 默认 20，范围 1..100；`offset` 默认 0，范围 0..10000。inline/SavedFilter 越界返回 `request_log.pagination_invalid`；legacy 继续返回历史 `request_log.query_invalid`。本期保留 offset 兼容，不提供无界深分页或任意排序表达式。

### 2.4 成功响应

```json
{
  "request_logs": [
    {
      "request_id": "req_01J...",
      "trace_id": "trc_01J...",
      "tenant_id": "tenant_01J...",
      "virtual_key_id": "vk_01J...",
      "virtual_key_prefix": "mhv_dev_****",
      "request_model": "company-default",
      "resolved_model": "deepseek-chat",
      "provider_code": "deepseek",
      "status": "failed",
      "error_code": "gateway.upstream_error",
      "terminal_stage": "provider",
      "latency_ms": 812,
      "total_tokens": 420,
      "estimated_cost_amount": 0.012345,
      "currency": "CNY",
      "created_at": "2026-07-28T03:00:00Z"
    }
  ],
  "total": 123,
  "limit": 20,
  "offset": 0,
  "has_more": true,
  "sort_by": "created_at",
  "sort_direction": "desc",
  "query_event_id": "lqe_0123456789abcdef0123456789abcdef",
  "request_id": "req_control_01J...",
  "trace_id": "trc_control_01J..."
}
```

列表 DTO 还可包含租户限定的环境、项目、应用、快照、策略、预算、DLP、路由尝试和反馈存在性等安全摘要。它不得包含 Provider Key、Virtual Key 明文、Session Token、Authorization、Prompt、Response、原始 Provider 正文或原始异常文本。

`total` 和当前页来自同一个只读事务快照。实现使用
`WITH matched AS MATERIALIZED`：CTE 在 tenant-first 条件和稳定排序下选择分页 rowid，
并用 `COUNT(*) OVER()` 随候选行返回匹配总数；外层只按 rowid JOIN 安全 DTO。服务端查询
`limit + 1` 行用于探测下一页，避免为展示 JOIN 再次扫描十万行租户数据。

当 offset 超过末页、window count 没有可承载总数的候选行时，服务端仅在同一个显式读事务
中回退一次 `COUNT(*)`，因此仍不会混入并发写入后的新快照。每次成功查询都必须返回新的
`query_event_id`；查询审计事实无法提交时整次 GET 失败关闭，不能先返回未审计的列表。

## 3. SavedFilter API

### 3.1 公开对象

```json
{
  "saved_filter_id": "sf_0123456789abcdef0123456789abcdef",
  "name": "近 24 小时 Provider 失败",
  "filters": {
    "v": 1,
    "status": "failed",
    "terminal_stage": "provider"
  },
  "sort_by": "created_at",
  "sort_direction": "desc",
  "version": 1,
  "created_at": "2026-07-28T03:00:00Z",
  "updated_at": "2026-07-28T03:00:00Z"
}
```

对象不返回 `tenant_id`、`owner_user_id`、状态、删除时间或内部 `filter_json`。每个用户最多 50 条 active SavedFilter；列表按 `updated_at DESC, saved_filter_id DESC` 返回。

名称 trim 后为 1..64 个 Unicode 字符，不允许控制字符；唯一性按当前 tenant、当前 owner 的 NFKC + casefold 规范化结果判断。软删除记录不复活，重新使用同名会生成新的 ID。

### 3.2 列表

`GET /request-logs/saved-filters`

```json
{
  "saved_filters": [],
  "request_id": "req_control_01J...",
  "trace_id": "trc_control_01J..."
}
```

只返回当前 tenant、当前用户自己的 active 记录。管理员也不能读取同租户其他用户的个人筛选。

### 3.3 创建

`POST /request-logs/saved-filters`

请求必须是唯一 `Content-Type: application/json`，可选唯一 `charset=utf-8`；正文最大 8192 bytes。重复 Content-Type/Content-Length、非规范长度、重复 JSON key、未知字段、非标准 JSON 常量和非 UTF-8 均返回 `saved_filter.invalid`。

```json
{
  "name": "近 24 小时 Provider 失败",
  "filters": {
    "v": 1,
    "status": "failed",
    "terminal_stage": "provider"
  },
  "sort_by": "created_at",
  "sort_direction": "desc"
}
```

四个字段均必填。filters 使用与 inline v1 相同的结构化白名单对象，不接收已 Base64URL 编码的字符串；分页永不持久化。成功返回：

```json
{
  "saved_filter": {
    "saved_filter_id": "sf_0123456789abcdef0123456789abcdef",
    "name": "近 24 小时 Provider 失败",
    "filters": {"v": 1, "status": "failed", "terminal_stage": "provider"},
    "sort_by": "created_at",
    "sort_direction": "desc",
    "version": 1,
    "created_at": "2026-07-28T03:00:00Z",
    "updated_at": "2026-07-28T03:00:00Z"
  },
  "request_id": "req_control_01J...",
  "trace_id": "trc_control_01J..."
}
```

### 3.4 更新

`PATCH /request-logs/saved-filters/{saved_filter_id}`

```json
{
  "version": 1,
  "name": "Provider 失败",
  "filters": {"v": 1, "status": "failed", "terminal_stage": "provider"},
  "sort_by": "latency_ms",
  "sort_direction": "desc"
}
```

`version` 必填且必须是 JSON 原生正整数；至少还要提供一个实际修改字段。成功后版本原子加一并返回完整对象。旧版本返回 `saved_filter.version_conflict`，客户端应刷新后由用户重新确认，不得静默覆盖。

### 3.5 删除

`DELETE /request-logs/saved-filters/{saved_filter_id}?version=2`

version 必须是无前导零的 ASCII 正整数。成功为 204 并执行软删除；不物理删除记录或关联查询事件。不存在、已删除、跨租户和非 owner 统一返回 `saved_filter.not_found`，不得用差异化响应枚举他人对象。

## 4. 统一错误码

| 错误码 | HTTP | 可重试 | 说明 |
| --- | ---: | --- | --- |
| `request_log.query_invalid` | 422 | 否 | 未知/重复 query、混合来源、非法 filters、非法 legacy 值 |
| `request_log.status_invalid` | 422 | 否 | legacy status 不是 success/failed |
| `request_log.sort_invalid` | 422 | 否 | 排序字段或方向不在白名单 |
| `request_log.pagination_invalid` | 422 | 否 | inline/SavedFilter 的 limit 或 offset 越界 |
| `request_log.query_unavailable` | 503 | 是 | Trace 存储或查询暂不可用 |
| `request_log.query_audit_unavailable` | 503 | 是 | 查询事实无法原子提交，列表失败关闭 |
| `request_log.query_event_unavailable` | 503 | 是 | QueryEvent source outbox 领取或回写不可用 |
| `saved_filter.not_found` | 404 | 否 | 不存在、已删除、跨租户或非 owner，同形返回 |
| `saved_filter.invalid` | 422 | 否 | ID、名称、filters、排序、version、媒体类型或 JSON 非法 |
| `saved_filter.name_conflict` | 409 | 否 | 当前 tenant + owner active 名称冲突 |
| `saved_filter.limit_exceeded` | 409 | 否 | 当前 owner 已有 50 条 active 记录 |
| `saved_filter.version_conflict` | 409 | 是 | 乐观版本过期 |
| `saved_filter.unavailable` | 503 | 是 | SavedFilter 存储不可用或可信持久记录损坏 |

认证和权限继续使用平台统一 401/403 错误码。统一错误正文只包含固定 code、面向用户的稳定 message/suggestion、`request_id`、`trace_id` 和允许的诊断摘要，不回显 query、JSON 正文、SQL、数据库路径、原始异常、Token 或凭据。

## 5. log.query_executed 事件

每次成功 `GET /request-logs` 在返回前写入不可变 `request_log_query_events` 源事实。查询失败不制造“已执行”事件。事件与列表读取使用同一数据库身份边界，actor、session、tenant 和 operation trace 均由后端绑定。

异步 source outbox 将事实转换为共享 Webhook：

```json
{
  "schema_version": 1,
  "query_event_id": "lqe_0123456789abcdef0123456789abcdef",
  "filter_version": 1,
  "filter_keys": ["created_from", "created_to", "status", "terminal_stage"],
  "filter_fingerprint": "<64 lowercase hex SHA-256>",
  "saved_filter_id": null,
  "sort_by": "created_at",
  "sort_direction": "desc",
  "limit": 20,
  "offset": 0,
  "matched_count": 123,
  "returned_count": 20,
  "has_more": true,
  "query_duration_ms": 37,
  "executed_at": "2026-07-28T03:00:00Z"
}
```

共享事件信封的 `event_type` 固定为 `log.query_executed`，`object_id` 为 `query_event_id`，`tenant_id`、`actor_id` 和 `trace_id` 由源事实提供。payload 不含筛选值、SavedFilter 名称、RequestLog 内容、tenant/owner/session、Prompt、Response、Key、Token、Endpoint URL、Webhook URL/payload 或原始错误正文。

`filter_fingerprint` 是 canonical 筛选的 SHA-256，用于同一查询合同的关联，不代替访问控制，也不能用于恢复或公开筛选正文。source worker 使用 30 秒 lease、CAS 回写、最多 10 次尝试和有界退避，提供稳定 event ID 下的 at-least-once 语义；不宣称网络 exactly-once。失败只持久化固定白名单错误码。

## 6. Console 交互合同

PAGE-080 仅向后端导航确认可见的 admin/developer 展示。页面默认最近 24 小时 UTC 窗口，支持组合筛选、白名单排序、20/50/100 分页、SavedFilter 的创建/应用/更新/删除，以及从日志行跳转 PAGE-079 请求解释中心。

SavedFilter 下拉的“选择”和“应用”是两个动作，只有显式应用才改变当前筛选并把 offset 重置为 0。创建和更新必须读取提交瞬间的表单值，不能使用旧页面内存。解释跳转使用身份绑定的一次性内存 handoff，不把 source request_id 写入 URL、localStorage、sessionStorage 或 IndexedDB。

筛选、分页、详情、SavedFilter CRUD 和解释跳转都必须绑定 tenant/user/role/session/revision/navigation/sequence owner。退出登录、换租户、换用户、降权、离页或新请求启动时中止旧请求并清空旧 DOM；迟到响应必须因 owner 不一致被拒绝。

## 7. 兼容与演进

- legacy 显式 query 继续保留，避免破坏 PRD-016 至 PRD-054 客户端；新页面使用 inline v1。
- 未知 `filters.v` 失败关闭，不由旧服务猜测新语义。
- 当前只支持有界 offset 分页；游标、归档查询和全文搜索属于后续独立版本。
- 当前 SavedFilter 仅个人私有，不支持团队共享、订阅、告警或跨租户模板。
- 新增公开筛选字段、排序字段、阶段枚举或事件 payload 字段时，必须同步版本设计、数据库索引、后端白名单、前端编码器、本文档及 Runtime/Browser 验收。

## 8. PRD-071 输出安全查询扩展

PAGE-099 继续复用本接口，不新增独立安全日志 API。页面每次查询都由应用层强制
`terminal_stage=output_safety`，后端仍逐请求校验 `request_log.read` 并只使用 Session tenant。

新增四个可选 legacy 单值筛选和等价 inline v1 字段：

| 字段 | 允许值 |
| --- | --- |
| `output_safety_status` | `pending/not_configured/completed/unavailable/not_applicable` |
| `output_safety_mode` | `non_stream_full/stream_window` |
| `output_safety_exposure` | `none/partial/complete/unknown` |
| `output_safety_billing` | `not_required/dispute_pending` |

这些字段必须与 `terminal_stage=output_safety` 同时生效；未知、重复、显式空枚举或 SavedFilter 中的未知值按
`request_log.query_invalid` 失败关闭。查询事件仍只保存字段名和 canonical fingerprint，不保存枚举值原文。

列表项新增可空 `output_safety` 最小摘要；详情 `GET /request-logs/{request_id}` 新增严格
`request_log.output_safety` 证据。列表不返回规则引用，详情只返回稳定规则 ID/type、版本、非负计数、暴露和
最小 dispute，不返回 Prompt、Response、命中片段或 pattern。0063 应用后的成功 Chat 缺少应有 scan，或
scan/dispute/request owner 不一致时，详情返回 `503 output_safety.integrity_failed`，不能降级为空或 clean。

字段全集、状态矩阵、角色边界和示例见 [输出侧安全 API](output-safety.md)。PRD-071 最终运行和 Browser 证据
形成前，该扩展仍按 [PRD-071 测试记录](../testing/PRD-071-tests.md) 的“最终验收中”状态管理。
