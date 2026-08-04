# PRD-055 - Trace查询与过滤增强技术设计

## 文档状态

- PRD：`PRD-055`
- 名称：Trace查询与过滤增强
- 里程碑：`M1-企业POC`
- 优先级：`P1`
- 页面：`PAGE-080 调用日志高级查询`
- API：`GET /request-logs?filters=`
- 核心对象：`SavedFilter`、既有 `RequestLog`
- 事件：`log.query_executed`
- 验收：`TC-0109`、`TC-0110`
- 当前状态：已完成。设计、两轮编码前自审、实现、两轮实现后自审、专项 Runtime/Browser、
  最终一次全量回归及停服静止终态审计均已完成；权威 `final-audit.json` 为 `status=passed`

## 1. 需求理解与冻结结论

### 1.1 权威需求来源

本设计已核对 Excel 中以下权威项：

- `01_PRD总表!A56:R56` 与 `02_M0-M1核心开发!A56:R56`
- `04_页面清单!A81:H81`：`PAGE-080`
- `05_API接口清单!A259:H260`：`API-258/API-259`
- `06_数据对象!A76:F76`：`OBJ-075 SavedFilter`
- `07_Webhook事件!A62:H62`：`EVT-061 log.query_executed`
- `08_验收用例!A110:J111`：`TC-0109/TC-0110`
- `09_CodexClaude提示词!A56:F56`

冻结需求如下：

1. 管理员和开发者可以按 request/trace、模型、Virtual Key、Provider、状态、错误、环境、
   项目、应用、快照版本、时间、延迟、Token 和成本组合筛选调用日志。
2. 列表支持白名单排序、稳定分页、保存筛选、复用筛选，并可从每一行跳转到 PRD-054
   请求解释中心。
3. `GET /request-logs?filters=` 是新高级查询合同；既有显式 query 参数继续兼容，不能破坏
   PRD-016 至 PRD-054 的客户端和测试。
4. 筛选准确、分页可接受且结果可复现；本 PRD 不实现全文日志搜索引擎。
5. 每次成功查询都写不可变 `log.query_executed` 源事实并进入有界异步 Webhook outbox；
   查询失败不得制造“已执行”事件。
6. 保存筛选属于当前租户、当前用户；tenant、actor、session 只能来自后端认证上下文。
7. API 成功和失败都保留统一 `request_id/trace_id`；日志、事件、DOM、URL 和 Storage 均不得
   包含 Token、Provider Key、Virtual Key 明文、Prompt、Response 或原始异常正文。

### 1.2 架构约束

- Trace Service 负责请求日志、错误归因和 Debug Center；查询属于控制面，不进入 Gateway
  同步热路径。
- Trace 热数据默认可查询 30 天，短期查询依赖关系库及复合索引；长期全文检索和归档查询
  不属于本 PRD。
- 所有管理 API 必须后端鉴权，所有查询以可信 Session `tenant_id` 为第一过滤条件。
- `request_id` 贯穿 Trace、日志、Usage 和成本；失败请求同样必须可查询和追踪。
- 默认在中国境内保存业务、日志和审计数据；本 PRD 不改变上游 Provider 边界。

## 2. 现有实现与缺口

### 2.1 可直接复用

- `request_logs` 已保存 tenant、request/trace、Virtual Key ID/安全前缀、环境、项目、应用、
  模型、Provider、状态、错误、延迟、Token、估算成本、快照、预算、DLP 和路由摘要。
- 编码前的 `GET /request-logs` 已支持 admin/developer 后端 RBAC、tenant-first 过滤、同一
  只读快照内 `COUNT + page`、固定稳定排序以及统一错误；实现后的单语句窗口计数优化见
  4.3 节和第 14 节。
- Console 已有 RequestLog list/detail owner，能在登出、换租户、换用户、降权、离页及迟到
  响应时销毁旧状态。
- PRD-054 已有 PAGE-079 一次性内存 handoff；可以复用，不需把源 request_id 放入 URL 或
  浏览器持久化存储。
- 现有 Webhook 共享合同、签名、投递、重试和 source outbox 模式可扩展
  `log.query_executed`。

### 2.2 必须补齐

1. 缺少 trace、时间、Virtual Key、延迟、Token、成本区间等高级筛选。
2. 缺少可选排序；当前只支持 `created_at DESC, request_id DESC`。
3. offset 无深分页保护，列表响应没有明确 `has_more`，页面只能用 `total` 推断。
4. 缺少 SavedFilter 表、个人所有权约束和 CRUD API。
5. 缺少不可变 `log.query_executed` 查询事实及崩溃后可恢复的事件派发。
6. 列表与 PAGE-079 已有按钮，但尚未冻结 PRD-055 的完整高级查询和保存筛选交互合同。

## 3. 范围与非目标

### 3.1 本期范围

- 扩展现有列表查询，不新建第二套 RequestLog 读模型。
- 支持显式 query 与版本化 `filters` 两种互斥输入；响应统一为同一 DTO。
- 新增个人 SavedFilter CRUD、乐观版本控制、不可变查询事件和 source outbox。
- 为 tenant-first 高级筛选和四种排序补充复合索引。
- PAGE-080 提供高级筛选、排序、分页、保存/应用/重命名/删除筛选和解释中心跳转。
- 双视口运行验收覆盖主流程、空结果、错误、权限和身份切换。

### 3.2 明确不做

- 不搜索 Prompt、Response、原始 Provider 正文、文件日志或任意 JSON 正文。
- 不引入 Elasticsearch/OpenSearch、OTel 后端、跨区域检索、归档恢复或机器学习根因分析。
- 不允许自由 SQL、自由字段名、自由表达式、正则或任意排序列。
- 不共享 SavedFilter 给其他用户或跨租户；不做团队模板、订阅报表或查询告警。
- 不把筛选条件、列表结果或 request_id 写入 localStorage/sessionStorage/IndexedDB。

## 4. 高级筛选合同

### 4.1 版本化 `filters`

`filters` 使用 Base64URL 无填充编码的 UTF-8 canonical JSON。Base64URL 仅是传输编码，不是
加密；服务端仍按完整白名单校验。最大 wire 长度 4096，解码后最大 3072 bytes，JSON
必须为对象且字段数不超过 24。

```json
{
  "v": 1,
  "request_id": "req_...",
  "trace_id": "trc_...",
  "status": "failed",
  "request_model": "company-default",
  "resolved_model": "deepseek-chat",
  "provider_code": "deepseek",
  "virtual_key_id": "vk_...",
  "environment_id": "env_...",
  "project_id": "prj_...",
  "application_id": "app_...",
  "snapshot_version": 3,
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

约束：

- `v` 必须为整数 `1`，未知版本失败关闭。
- `terminal_stage` 使用冻结 `terminal_stage_v1`：`completed/authentication/rate_limit/budget/dlp/policy/routing/keypool/provider/usage_and_finops/logs_and_webhooks/unknown`。它只描述由成功终态或 error_code 可证明的完成/终止点，不声明其他阶段已经通过；无法可靠归类固定为 `unknown`。
- 不允许重复 JSON key、未知 key、数组、嵌套对象、浮点金额、NaN/Infinity 或 Unicode
  控制字符。
- ID/枚举最长 160，request/trace ID 另按 1..128 ASCII 安全字符校验。
- `created_from` 与 `created_to` 必须成对出现并满足前者小于后者，窗口最长 30 天；两者都未指定时不偷偷加 30 天条件，以兼容
  已存在的本地历史数据，但 UI 默认最近 24 小时。
- 数值区间下界不得大于上界；延迟与 Token 为非负整数，金额为最多 6 位小数的非负十进制
  字符串，并按数据库 NUMERIC 比较，不转二进制浮点。
- `tenant_id`、actor、owner、role、include_raw、prompt、response、key_prefix、provider_key、
  endpoint URL 等字段一律不在合同中。

### 4.2 兼容显式 query

- 保留现有 `status/request_id/model/provider_code/error_code/environment_id/project_id/
  application_id/snapshot_version/limit/offset`。
- 新增同名高级显式参数仅供渐进迁移；只要出现 `filters`，除 `sort_by/sort_direction/
  limit/offset` 外不得同时出现任何筛选参数，避免两套优先级产生歧义。
- `saved_filter_id` 与 `filters`、全部显式业务筛选参数互斥；后端必须使用当前
  `tenant_id + owner_user_id + saved_filter_id + active` 加载 canonical filter 和排序，
  不能相信客户端只回传的 ID。应用成功时 offset 仍由请求指定且页面固定重置为 0。
- 既有 `model` 继续同时匹配 request/resolved model；新 `filters` 可分别精确指定两者。
- 未知参数、重复单值参数、空 `filters`、非法 percent/base64/JSON 都返回
  `request_log.query_invalid`。

### 4.3 排序与分页

- `sort_by` 白名单：`created_at`、`latency_ms`、`total_tokens`、`estimated_cost_amount`。
- `sort_direction`：`desc`（默认）或 `asc`。
- 所有排序都追加 `created_at` 和 `request_id` 作为稳定 tie-breaker；主键与 tie-breaker 的
  方向一致，NULL 永远排在非 NULL 之后。
- `limit` 取 1..100，默认 20；`offset` 取 0..10000。超过上限返回
  `request_log.pagination_invalid`，不允许无界深分页拖垮控制面。
- 响应新增 `has_more`、`sort_by`、`sort_direction` 和 `query_event_id`。实现使用
  `WITH matched AS MATERIALIZED`：CTE 在 tenant-first 条件、稳定排序和 `LIMIT/OFFSET`
  下选出分页 rowid，并用 `COUNT(*) OVER()` 把匹配总数随每个候选行带回；外层只按 rowid
  JOIN 安全 DTO，查询 `limit + 1` 行用于探测下一页。这样总数与页面来自同一条 SQL、同一
  读快照，且不会为展示 JOIN 重复扫描十万行租户数据。
- 当 offset 超过末页、窗口没有可承载总数的行时，才在同一个显式读事务内执行一次
  `COUNT(*)` 回退；该低频分支仍保持 total 与页面快照一致。
- 当前 M1 保留 offset 以兼容现有 UI；游标分页留给后续超大规模/归档检索，不在本 PRD
  同时引入两套任意排序游标语义。

## 5. 数据表与迁移设计（0045）

### 5.1 `saved_filters`

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `saved_filter_id` | TEXT PK，`sf_` + 32 hex | 稳定个人筛选 ID |
| `tenant_id` | TEXT NOT NULL | 可信 Session tenant |
| `owner_user_id` | TEXT NOT NULL | 可信 Session user |
| `name` | TEXT NOT NULL，1..64 | 显示名称，禁止控制字符 |
| `name_normalized` | TEXT NOT NULL，1..64 | trim + Unicode NFKC + casefold 后唯一键 |
| `filter_version` | INTEGER，固定 1 | 过滤合同版本 |
| `filter_json` | TEXT NOT NULL | canonical JSON，不含分页和排序 |
| `sort_by` | 白名单枚举 | 可复用排序 |
| `sort_direction` | `asc/desc` | 可复用方向 |
| `version` | INTEGER >= 1 | PATCH/DELETE 乐观锁 |
| `status` | `active/deleted` | 软删除，避免 ID 复用和审计漂移 |
| `created_at/updated_at/deleted_at` | UTC TEXT | 生命周期时间 |

约束：

- FK `(tenant_id, owner_user_id)` 指向 users；`tenant_id` 直接指向 tenants。
- active 唯一索引 `(tenant_id, owner_user_id, name_normalized)`；每用户最多 50 条 active。
- 所有查询/更新使用 `tenant_id + owner_user_id + saved_filter_id`；admin 也不能读取或修改
  同租户其他用户的个人筛选。
- 插入/更新触发器重新验证 owner、状态、时间、版本、JSON 白名单；删除只允许状态机软删除，
  禁止物理 DELETE、`INSERT OR REPLACE`、rowid 绕过和 deleted 复活。

### 5.2 `request_log_query_events`

每次成功高级/兼容查询写一条不可变查询事实兼 source outbox：

| 字段 | 说明 |
| --- | --- |
| `query_event_id` / `event_id` | `lqe_` / `lqevt_` + 32 hex |
| `tenant_id/actor_user_id/actor_session_id` | 可信认证主体 |
| `filter_version/filter_fingerprint` | canonical filter 版本和 SHA-256；不保存自由输入 |
| `filter_keys_json` | 排序后的已用字段名数组，不保存值 |
| `saved_filter_id` | 可空；仅当从已验证 SavedFilter 应用时记录 |
| `sort_by/sort_direction/limit/offset` | 查询形态 |
| `matched_count/returned_count/has_more` | 查询结果摘要 |
| `query_duration_ms` | 单调时钟计算并向上取整，非负整数 |
| `operation_request_id/operation_trace_id` | 本次 Control API 链路 |
| `event_dispatch_*` | pending/processing/dispatched/failed、10 次上限、lease/CAS |
| `executed_at/created_at/updated_at` | UTC 时间 |

- 事件不保存筛选值，防止 request/trace、Virtual Key ID 或业务对象 ID 被复制到 Webhook；
  只保存字段名、不可逆指纹和聚合计数。
- 查询事实必须在同一数据库连接的短事务中写入；若事件事实无法提交，GET 失败关闭为
  `request_log.query_audit_unavailable`，不能返回未经审计的敏感列表。
- HTTP Webhook 投递异步执行，不阻塞 GET；崩溃后由 source outbox 恢复。
- `log.query_executed` payload 精确为：schema_version、query_event_id、filter_version、
  filter_keys、filter_fingerprint、saved_filter_id、sort_by、sort_direction、limit、offset、
  matched_count、returned_count、has_more、query_duration_ms、executed_at。

### 5.3 RequestLog 索引

0045 增加以下 tenant-first 索引，并用 `EXPLAIN QUERY PLAN` 验证：

- `(tenant_id, trace_id, created_at DESC, request_id DESC)`
- `(tenant_id, virtual_key_id, created_at DESC, request_id DESC)`
- `(tenant_id, provider_code, created_at DESC, request_id DESC)`
- `(tenant_id, status, created_at DESC, request_id DESC)`
- `(tenant_id, latency_ms, created_at DESC, request_id DESC)`
- `(tenant_id, total_tokens, created_at DESC, request_id DESC)`
- `(tenant_id, estimated_cost_amount, created_at DESC, request_id DESC)`
- `(tenant_id, terminal_stage_v1 CASE, created_at DESC, request_id DESC)` 表达式索引；查询 SQL 必须逐字复用同一 CASE。

不为任意字段组合建立指数级索引；时间范围与选择性等值条件由查询计划选取一个复合索引，
剩余条件在租户子集中精确过滤。

### 5.4 down migration

- 存在 active SavedFilter、查询事件或 `log.query_executed` delivery/attempt 时，down 在 DDL 前
  原子拒绝；空事实允许回滚。
- up/down 必须验证逐列、索引、触发器、FK、checksum、`integrity_check` 和前后 schema
  checkpoint；回滚不得删除数据库外文件、日志或测试证据。

## 6. API 设计

### 6.1 `GET /request-logs`

权限：复用 `require_request_log_reader`，admin/developer 可读；anonymous 401、viewer 403。
RBAC 必须先于 query 解码和业务校验。输入可选择 inline `filters`、可信 owner 加载的
`saved_filter_id` 或兼容显式筛选三种模式之一；三者互斥。

成功响应：

```json
{
  "request_logs": [],
  "total": 0,
  "limit": 20,
  "offset": 0,
  "has_more": false,
  "sort_by": "created_at",
  "sort_direction": "desc",
  "query_event_id": "lqe_...",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

成功头固定 `Cache-Control: no-store, private`、`Pragma: no-cache`、
`Vary: Authorization`，并保留统一 `X-Request-ID/X-Trace-ID`。

### 6.2 SavedFilter API

- `GET /request-logs/saved-filters`：列出当前用户 active 筛选，最多 50 条，按更新时间倒序。
- `POST /request-logs/saved-filters`：创建；body 为 name、filters 对象、sort_by/direction。
- `PATCH /request-logs/saved-filters/{id}`：更新名称/筛选/排序，必须携带当前 `version`。
- `DELETE /request-logs/saved-filters/{id}?version=`：软删除并返回 204。

所有接口复用 admin/developer 后端权限。跨租户、其他 owner、deleted 与不存在 ID 对读取/修改
均返回同形 `saved_filter.not_found`，响应和错误统一携带 operation request/trace ID。

创建/更新只接收结构化 JSON，不接收已编码 `filters` 字符串，后端使用同一个规范化器生成
canonical JSON；分页永不保存，应用 SavedFilter 后 offset 重置为 0。

## 7. 错误码设计

| 错误码 | HTTP | 语义 |
| --- | ---: | --- |
| `request_log.query_invalid` | 422 | filters/字段/区间/重复或混合输入非法 |
| `request_log.status_invalid` | 422 | legacy status 不是 success/failed |
| `request_log.sort_invalid` | 422 | 非白名单排序或方向 |
| `request_log.pagination_invalid` | 422 | inline/SavedFilter 的 limit/offset 越界或类型非法 |
| `request_log.query_unavailable` | 503 | Trace 存储、快照或安全 DTO 查询暂不可用 |
| `request_log.query_audit_unavailable` | 503 | 查询事实写入失败，整体失败关闭 |
| `request_log.query_event_unavailable` | 503 | QueryEvent source outbox 领取或回写暂不可用 |
| `saved_filter.not_found` | 404 | 不存在、跨租户、非 owner 或已删除，同形 |
| `saved_filter.invalid` | 422 | 名称、过滤器、版本或 body 非法 |
| `saved_filter.name_conflict` | 409 | 当前 owner active 名称冲突 |
| `saved_filter.limit_exceeded` | 409 | 当前 owner active 数量达到 50 |
| `saved_filter.version_conflict` | 409 | 乐观版本冲突 |
| `saved_filter.unavailable` | 503 | SavedFilter 存储或迁移不可用 |

为兼容 PRD-016 的既有客户端，legacy limit/offset 非法继续返回
`request_log.query_invalid`；inline `filters` 和可信 SavedFilter 模式返回
`request_log.pagination_invalid`。inline 中非法 status 也归入 `request_log.query_invalid`，
只有 legacy status 保留 `request_log.status_invalid`。

底层 SQLite、JSON、编码和 Webhook 异常只映射固定错误码，不返回 SQL、路径、payload、URL
或任意异常正文。

## 8. 安全、权限与租户隔离

- 所有 SQL 的第一归属条件都是可信 `context.user.tenant_id`；SavedFilter 再加
  `context.user.user_id`，不接受客户端 tenant/owner。
- 所有 JOIN 同时带 tenant 条件，不能只依赖全局 ID 唯一。
- viewer 即使手写 API 也被后端 403；前端隐藏仅是体验层。
- SavedFilter 名称是用户文本，只在页面 HTML 转义后展示，不进入 Webhook、日志或 URL。
- filter fingerprint 使用 canonical JSON 的 SHA-256；不把原始 filters、筛选值或 SavedFilter
  名称写入结构化日志。
- 查询事件 payload 经过共享敏感键递归门禁，事件信封使用持久 event_id、可信 actor、对象
  ID 和 operation trace。
- 列表只返回既有安全 RequestLog DTO；不新增 Prompt、Response、Header、Endpoint URL、
  Credential、Provider Key、Virtual Key 明文或 DLP 命中文本。
- Console 的 owner 包含 tenant/user/role/session/revision/navigation/generation；任何身份变化
  同步 abort 查询与 SavedFilter 请求并清空旧事实。

## 9. PAGE-080 前端与交互设计

- 复用现有 RequestLog 页面，不增加营销式页面或重复导航。
- 顶部使用紧凑高级筛选区：时间范围、request/trace、状态、模型、Provider、Virtual Key ID、
  环境/项目/应用、错误码、延迟/Token/成本区间；可折叠次要条件。
- 排序使用字段下拉和升/降序 segmented control；分页按钮保持固定尺寸并显示结果范围。
- SavedFilter 使用下拉菜单选择，配套保存、重命名和删除命令；二次确认只用于删除。
- 应用保存筛选立即恢复 filters/排序并回到第一页；保存成功后按服务端响应刷新菜单。
- 每行保留“详情”和图标化“解释”操作；解释操作复用 PRD-054 一次性内存 handoff 跳转
  `#request-explanations`，不在 URL/Storage 暴露源 ID。
- loading/empty/error/success 状态占用稳定区域；移动端 390x844 采用单列字段和可横向扫描的
  行式详情，不允许按钮文字溢出或控件遮挡。
- viewer 页面没有 RequestLog/SavedFilter DOM，也不发送列表、保存筛选或解释请求。

## 10. 测试用例

### 10.1 主流程

1. admin/developer 使用 `filters` 组合 trace、Provider、时间、延迟和成本，返回精确交集。
2. 四种字段、两个方向排序均稳定；相同主值使用 created_at/request_id 确定性排序。
3. 第 1/中间/末页的 total、has_more、limit/offset 正确，连续重复查询结果一致。
4. 创建、列出、应用、更新、重命名、版本冲突和软删除 SavedFilter。
5. 应用 SavedFilter 重置 offset，恢复过滤和排序但不恢复上一页位置。
6. 每次成功查询写一条 query event，持久 event_id 幂等投递；HTTP 失败不阻塞查询。
7. 从筛选结果行跳转 PAGE-079，并加载同一当前租户 request_id。

### 10.2 异常与边界

- 非法 Base64URL、重复 JSON key、未知版本/字段、超长、嵌套、控制字符、金额浮点。
- 时间倒置/超过 30 天，min > max，非法枚举、排序、limit、offset > 10000。
- `filters` 与显式筛选混用、重复 query、未知 query、空参数。
- 空结果仍写合法查询事件；数据库锁/缺表/触发器拒绝映射固定 503。
- SavedFilter 重名、超过 50 条、版本冲突、deleted 复活、物理删除、REPLACE/rowid 绕过。
- outbox claim、过期 lease、并发 worker、10 次终止、崩溃恢复和敏感 payload 拒绝。

### 10.3 权限与隔离

- anonymous 401、viewer 403，且 RBAC 先于不可信 query/body/path 校验。
- tenant A/B 使用相同 request_id、SavedFilter ID/名称仍完全隔离。
- 同租户 user A/B 不能互读、修改或删除个人 SavedFilter；admin 不例外。
- 登出、换租户、换用户、role/revision 变化、离页和迟到响应不会保留旧列表或筛选。
- API、DOM、URL、Storage、日志、事件和截图执行敏感字符串/禁键扫描。

### 10.4 性能与运行验收

- 在不少于 100000 条、至少两个 tenant 的合成 request_logs 上执行代表性组合查询；
  tenant A 查询计划必须使用 tenant-first 复合索引，不能扫描 tenant B 后再过滤。
- 本地 SQLite 验收目标：P95 查询加审计提交不高于 500 ms，单次不高于 1500 ms；该目标是
  本地可接受基线，不冒充生产容量承诺。
- 迁移 up/down/up、checksum、FK、trigger、`integrity_check`、WAL checkpoint。
- 双视口 1280x720、390x844 验证成功、空结果、错误和 SavedFilter 流程；截图非空、无横向
  页面溢出、无网络错误和 Console error。
- 执行 PRD-055 专项 Python/Node/Console、相邻 RequestLog/PRD-054 回归、中文注释门禁；最终
  仅在专项稳定后执行一次必要全量回归，不因单点修正从头反复跑全量。

## 11. 代码修改计划

1. 新增 `0045_trace_query_enhancement.sql/.down.sql`，建立 SavedFilter、QueryEvent、outbox、
   tenant-first 索引、触发器和 down guard。
2. 在 shared webhook/error 合同中登记 `log.query_executed` 和 PRD-055 错误码。
3. 扩展 request_logs models/routes/service，抽出统一 filter codec/normalizer/query builder。
4. 新增 SavedFilter CRUD 和 query event source outbox；注册应用生命周期 worker。
5. 扩展 Console client、RequestLog page memory、PAGE-080 renderer/event wiring 与样式。
6. 新增 shared/control/migration/Console/Runtime/Browser 测试和中文开发文档。
7. 更新 README、目录说明和 `docs/testing/PRD-055-tests.md`，最终记录实际命令与证据。

## 12. 编码前第一轮自审

审查范围：需求完整性、架构边界、数据模型、分页性能、事务、安全和兼容。

发现并修正：

1. 初稿考虑完全替换显式 query，会破坏现有 Console 与调用方；改为兼容两种输入，并规定
   `filters` 与显式筛选互斥，消除优先级歧义。
2. 初稿考虑任意排序游标，但 nullable 金额/延迟与四种方向会显著扩大 M1 合同；改为稳定
   offset + 10000 深分页上限 + tenant-first 索引，明确游标留给归档/超大规模阶段。
3. 查询事件若保存完整 filter_json 会复制业务标识；改为只保存字段名、SHA-256 指纹和计数。
4. SavedFilter 若仅 tenant 共享会泄露个人排障习惯；冻结为 tenant + owner 双重归属，admin
   也不隐式读取他人个人筛选。
5. 只在查询完成后写审计会把读快照保持到写锁升级；改为先完成只读快照并关闭，再开短写
   事务提交 QueryEvent；审计失败则不返回结果，避免锁升级和未经审计的数据披露。
6. 金额若转 float 会导致筛选边界漂移；冻结十进制字符串校验和数据库 NUMERIC 比较。

第一轮结论：P0/P1 已关闭；设计保持控制面范围，没有引入全文引擎、硬件、MCP 主链路或
复杂数据出境。

## 13. 编码前第二轮自审

审查范围：RBAC 优先级、租户/owner 同形、输入 wire、事件投递、前端生命周期和验收可证性。

发现并修正：

1. SavedFilter 路由可能被 `/{log_request_id}` 捕获；冻结静态路由在动态详情路由之前注册，
   并以路由集成测试证明。
2. FastAPI 默认 query 转换可能在权限依赖前返回 422；冻结入口接收原始 query，再在 RBAC
   通过后由领域规范化器解析，确保 anonymous/viewer 优先得到 401/403。
3. query event 若同步 HTTP 投递会拖慢列表；冻结“源事实提交后返回、HTTP 异步 outbox”，
   失败通过 lease/CAS 恢复且不修改不可变查询事实。
4. Deleted SavedFilter 若允许同 ID PATCH 恢复会破坏审计；冻结 deleted 不可复活、ID 不复用，
   同名可以在软删除后创建新 ID。
5. SavedFilter 名称可能进入事件/日志；冻结名称只存业务表并 HTML 转义，不进入事件 payload、
   URL 或结构化运行日志。
6. 页面已有 RequestLog owner，但 SavedFilter 请求也必须绑定同一完整身份和 generation；将
   list/mutation controller 纳入离页和身份切换清理，迟到响应不得恢复旧筛选。
7. 解释跳转若用 hash query 会把源 ID留在历史记录；明确复用 PRD-054 一次性内存 handoff，
   hash 固定为 `#request-explanations`。
8. 性能“可接受”缺少可运行阈值；补入 100000 行双租户数据、查询计划、P95 500 ms 和单次
   1500 ms 的本地验收口径，并明确不是生产容量承诺。
9. QueryEvent 原本保留 `saved_filter_id`，但 GET 未冻结可信应用语义；已补充
   `saved_filter_id` 与 inline/legacy 互斥，并要求后端按 tenant + owner + active 加载。
10. 迁移初稿只校验终态形状，未阻断 REPLACE/rowid 和非法 outbox 跃迁；0045 已要求两张表
    增加 conflict/negative-rowid 门禁，并只允许 lease/CAS 的领取、恢复、成功、退避和终止边。
11. Excel 要求“阶段过滤”，初稿遗漏该字段；现补充 `terminal_stage_v1`，只基于成功状态或现有
    不可变 error_code 推导完成/终止点，无法证明时返回 `unknown`，禁止把它解释为全阶段通过。

第二轮结论：权限、租户、兼容、性能和隐私 P0/P1 均已关闭，允许开始实现；此状态不代表
PRD 已完成，仍须通过实现后审查、专项测试、迁移和真实运行验收。

## 14. 实现结果

### 14.1 数据库、共享合同与 Control API

- `0045_trace_query_enhancement.sql/.down.sql` 已建立 `saved_filters`、
  `request_log_query_events`、tenant-first 复合索引、不可变/owner/状态机触发器和业务事实
  down guard；SavedFilter 只允许软删除，QueryEvent 作为 source outbox 使用稳定事件 ID。
- Shared 已登记完整统一错误码、中英文标签、`log.query_executed` 精确 payload/信封、
  敏感键递归门禁及 outbox 有界配置。
- Control 的 `request_logs/{query,saved_filters,outbox,scheduler,routes,models}.py` 已实现
  三种互斥查询来源、严格 filter codec、白名单排序、有界分页、个人 SavedFilter CRUD、
  乐观锁、查询事实和异步投递；认证、RBAC、tenant 和 owner 均由后端可信上下文决定。
- 查询热 SQL 已落实 materialized CTE + window count：分页 rowid、总数和稳定顺序来自同一
  SQL/快照，只有 offset 越过末页时才在同一读事务内回退计数。

### 14.2 Console 与交互

- PAGE-080 已在既有 Request Logs 页面提供高级筛选、排序、20/50/100 分页、SavedFilter
  创建/选择/显式应用/更新/软删除，以及到 PAGE-079 的一次性内存解释 handoff。
- 选择 SavedFilter 不隐式查询；显式应用才以服务端权威筛选和排序查询并回到第一页。页面
  不把筛选、源 request ID、Token、Prompt、Response 或凭据写入 URL/Storage。
- RequestLog 与 SavedFilter 请求共享 tenant/user/role/session/revision/navigation/generation
  owner；换身份、降权、登出、离页和新请求都会 abort 并拒绝迟到响应。
- PAGE-080 停留期间不执行 15 秒全页定时刷新，避免丢失未提交筛选或中断 SavedFilter 写入。

### 14.3 测试与验收器

- 已新增 Shared、Control unit/integration、0045、访问日志隐私、Runtime 合同、Console
  RequestLog/PAGE-080 与 Browser 合同测试。
- Runtime 验收器只使用批次独立数据库和回环三服务，验证十万行双租户数据、查询计划、
  筛选/排序/分页、RBAC、租户/owner、SavedFilter、QueryEvent/outbox、迁移和 Session 清理。
- Browser 验收器覆盖 1280x720 与 390x844 的 success/empty/error/SavedFilter 八种状态，
  三角色权限、布局、网络、DOM/Storage 隐私、解释 handoff 和账号/Session 清理。

## 15. 实现后第一轮自审

审查范围：查询 SQL、事务快照、0045 owner/状态机、SavedFilter 规范化、QueryEvent/outbox
和十万行性能。

发现并修正：

1. 初版列表延续“先 COUNT、再执行包含多表 JOIN 的 page”方案，在十万行租户数据上重复扫描
   展示链。现改为 `WITH matched AS MATERIALIZED` + `COUNT(*) OVER()`：先在 tenant-first
   条件下冻结分页 rowid 和 total，再按 rowid JOIN DTO；空的越界页才在同一读事务回退计数。
2. 设计表最初只写 casefold，不能阻断兼容字符形成的视觉等价重名；实现与公开合同统一为
   显示名 trim、唯一键 NFKC + casefold + trim，并同时校验规范化前后长度。
3. 列表行若在 DTO 安全映射时失败，不能先留下“成功查询”事件；现先完成全部安全 DTO 映射，
   再以短写事务提交 QueryEvent，审计提交失败整体关闭为固定 503。
4. QueryEvent 只保存字段名、指纹和结果摘要，不复制筛选值；outbox claim/lease/CAS、过期恢复、
   最多十次和稳定 event ID 均由代码、迁移触发器与专项测试交叉约束。
5. 代表性查询通过 tenant-first 索引提示和 `EXPLAIN QUERY PLAN` 拒绝全表扫描；Runtime 在
   100000 行、两个 tenant 上执行 20 次正式样本，P95 491 ms、最大 510 ms，满足本地
   P95 500 ms/单次 1500 ms 门槛，但不把该结果表述为生产容量承诺。

第一轮结论：查询一致性、数据库 owner、事务、崩溃恢复和本地性能未发现未关闭 P0/P1。

## 16. 实现后第二轮自审

审查范围：后端 RBAC、跨租户/跨 owner、防敏感信息泄露、Console 生命周期、双视口布局和
Browser 验收器真实性。

发现并修正：

1. PAGE-080 原先仍参与 15 秒全页刷新，可能覆盖用户未提交的高级筛选，并中止进行中的
   SavedFilter mutation；现对该页停用周期刷新，显式查询和身份 owner 清理逻辑保持不变。
2. Browser 布局检查最初会把闭合 `details:not([open])` 内不可见控件误判为重叠；可见性
   判定现排除闭合 details 后代，并由源码合同测试冻结，避免用放宽真实可见控件规则掩盖布局问题。
3. SavedFilter 下拉已验证“选择零查询、显式应用一次查询”，服务端按 tenant + owner + active
   重新加载权威条件；桌面和移动端均完成第二页、version 2 更新、解释 handoff 和软删除。
4. viewer 手写 `#request-logs` 会被清理，导航、受保护 DOM 和 RequestLog 预取均为零；真实
   后端仍返回 403。admin/developer 可查询，三角色登出后页面内存和 Storage 均清空。
5. 正式 Browser 证据覆盖八张非空 PNG；两档视口的横向溢出、控件/内容裁切和重叠均为零，
   blocked request、Browser error、Console error 和 viewer prefetch 也均为零。

第二轮结论：实现与冻结的权限、租户、隐私、交互和响应式合同一致，无未关闭 P0/P1。
该结论不替代最终一次全量回归和停服静止终态审计；两项独立终态证据均已完成并登记在第 17 节。

## 17. 最终验收状态

- 权威批次为 `artifacts/prd055/20260728T134456`。Runtime 证据
  `runtime-acceptance.json` 为 `status=passed`，SHA-256 为
  `8CC8F6268A11DBFD2B634A0CEE647C2E03049C280969B6D5441DCB6593014143`。
- Browser 证据 `browser-acceptance.json` 为 `status=passed`，SHA-256 为
  `967DB05A0CA8ED74D561D85798A93545DCF0132CF9D2B6CAA6E4A8196E608754`；八张正式截图、
  admin/developer/viewer、viewer hash 清理、零预取、SavedFilter 全流程、Explain handoff、
  临时用户 disabled 和 Session revoked 均已直接验证。
- 最终 Python 根级全量只执行一次，共 `1364` 项：`1363 passed / 1 failed / 0 errors / 0 skipped`。
  原失败 node 为
  `gateway-data-plane.tests.integration.test_openai_compatible_api::test_published_deny_claims_version_and_never_calls_provider`；
  该 node 使用测试级 `100 ms` 等待上限，按“局部失败不重跑整套全量”规则定向复验 `1/1 passed`。
  生产代码的 `5 ms` 配置未修改，最终 Python 状态为 `passed_with_targeted_closure`。
- Node 示例全量 `33/33 passed`，Console 全量 `433/433 passed`，Console lint `140 files passed`，
  中文注释覆盖门禁为 `passed`。
- 运行库共有 `45` 条迁移且全部为 `applied`，最高版本 `0045`，0045 loader checksum 与迁移记录一致；
  `wal_checkpoint(TRUNCATE)=[0,0,0]`、`integrity_check=ok`、外键违规为 `0`。
- 静止数据库中 `258` 条 QueryEvent 全部为 `dispatched` 且 attempts 均为 `1`，异常 actor Session 为 `0`；
  `14` 条 SavedFilter 均为合法软删除状态，生命周期异常为 `0`；PRD-055 的 `16` 条 Session 全部
  `revoked`，Browser 临时 developer/viewer 用户共 `16` 个且全部 `disabled`。
- 停服前 Gateway、Control API、Console 健康检查均为 HTTP `200`；停服后 `5173/8000/8001`
  均无监听，WAL/SHM/journal sidecar 均不存在，spool 的 pending/processing/quarantine 均为 `0`。
- 最终日志证据共扫描 `44` 个文件，密码、Bearer/Token、Provider Key、Prompt/Response 等敏感类别
  均为零命中；历史诊断日志被保留并与正式/支撑日志分类记录，`logs-final.json` 为 `status=passed`。
- pytest 清理仅删除 `D:/MetaHub/.tmp` 内 `502` 个链接节点，`502` 个目标全部保留，缺失目标、
  剩余链接节点和普通目录删除数均为 `0`；未使用 `C:/tmp`、未提权、未执行递归删除。
- `database-final.json`、`logs-final.json`、`pytest-link-cleanup.json` 和 `final-audit.json` 均已生成并
  完成稳定 SHA-256 关联；`final-audit.json` 的最终结论为 `status=passed`。

因此 PRD-055 已完成，可运行、可验收。该结论只关闭 PRD-055；`all_108_prds_complete=false`，
全部 108 条 PRD 尚未完成，下一项候选 PRD-056 尚未开始。
