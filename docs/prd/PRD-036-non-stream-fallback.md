# PRD-036 - 非流式请求备用模型 Fallback

## 文档状态

- 阶段：M0.5-W3/4
- 优先级：P0
- 当前状态：两轮设计审核通过，编码、全量测试与本地运行态验收完成
- 测试证据：`docs/testing/PRD-036-tests.md`
- 权威输入：Excel `01_PRD总表`、`04_页面清单`、`05_API接口清单`、`06_数据对象`、`07_Webhook事件`、`08_验收用例`
- 关联对象：`FallbackPolicy`、`RoutingAttempt`
- 关联页面：PAGE-053 模型别名页、PAGE-054 日志详情
- 关联接口：`PATCH /model-aliases/{id}/fallback`
- 关联事件：`route.fallback_triggered`
- 前置依赖：PRD-015 基础路由、PRD-035 Provider 健康探测与调用前健康过滤

## 需求理解

> 当前实现扩展（2026-10-05）：本 PRD 的 M0 历史范围使用一个备用模型作为最小场景；现行运行时支持 1 个主模型和最多 31 个有序备用模型。非流式请求在白名单故障后沿冻结候选链继续尝试，最多 32 次，成功立即结束，全部失败才返回最终错误。历史条款中的“两次”描述只用于兼容回归用例，不代表当前候选上限。

1. 管理员可为 ModelAlias 配置一个主模型和一个备用模型，并启停非流式 Fallback 策略；developer 可读取配置但不能修改。
2. 非流式 Chat Completions、Embeddings 请求先调用路由计划选中的初始模型。只有该次上游调用出现超时、HTTP 429 或 HTTP 5xx 时，才按策略尝试一次备用模型。
3. 一次客户端请求最多产生两次上游调用：初始尝试一次、Fallback 尝试一次。MVP 不做同一候选重试、指数退避、递归链或多级 Fallback，防止重试风暴。
4. Fallback 复用原始客户端请求的业务内容、同一个 `request_id/trace_id`、Virtual Key、租户和治理上下文，只替换已验证的路由、Provider Key 与上游模型字段。
5. DLP、Virtual Key 限流和并发租约按一次客户端请求执行；Fallback 不重复扣限流次数。预算按本次实际可执行候选的最坏成本总和一次性准入和预留，避免备用模型更贵时绕过预算。
6. 每次上游尝试单独写 `routing_attempts`，记录顺序、角色、Provider/模型、状态、延迟、安全错误分类、是否触发 Fallback、用量与成本可得性；不保存 Prompt、Response、Authorization 或 Provider Key。
7. `request_logs` 仍是一条客户端请求一条主记录，最终 `provider/model/resolved_model` 必须指向最终响应模型；详情通过 `routing_attempts` 展示完整尝试顺序。
8. 主模型失败但备用成功时，客户端收到备用模型的正常响应；主、备都失败时返回备用模型的统一错误，同时日志保留两个错误和最终错误。
9. 流式请求完全不进入本 PRD 的请求后 Fallback。即使流式握手尚未输出首字节，本 PRD 也不切换候选；一旦输出更不允许中途切换，避免重复或拼接内容。
10. PRD-035 的调用前健康过滤与本 PRD 的调用后失败切换必须分离：若主模型在调用前已因 disabled/fresh unhealthy 被跳过，备用模型可作为初始候选，但这不算 `route.fallback_triggered`，也不会形成一次虚假的主模型调用尝试。
11. 直接请求内部模型而非 ModelAlias、别名没有备用模型、策略关闭、备用候选当前不可用，均不执行请求后 Fallback。
12. 不在 M0.5 引入任意表达式执行器、跨区域策略、多级候选链、流式断点续传或异步任务队列。

## 涉及模块

- `infra/migrations/0026_non_stream_fallback.sql`：新增 Fallback 策略、路由尝试表及调用日志汇总字段。
- `control-plane-api/src/metahub_control/model_aliases`：Fallback DTO、查询展开、专用 PATCH 服务、事务校验和审计。
- `control-plane-api/src/metahub_control/request_logs`：加载路由尝试时间线并返回日志详情。
- `gateway-data-plane/src/metahub_gateway/routing`：从单一路由升级为有界路由计划，区分调用前过滤与调用后 Fallback。
- `gateway-data-plane/src/metahub_gateway/api/openai_compatible.py`：仅在非流式路径编排最多两次上游调用。
- `gateway-data-plane/src/metahub_gateway/usage`：写入尝试关联和最终请求汇总，明确失败尝试的用量/成本可得性。
- `gateway-data-plane/src/metahub_gateway/budgets`：对路由计划执行一次最坏成本准入预留。
- `gateway-data-plane/src/metahub_gateway/routing/attempts.py`：尝试开始、完成、失败和 Fallback 触发的幂等持久化。
- `shared-sdk/python/metahub_shared/runtime/errors.py`：新增策略配置错误，复用标准上游错误码和 HTTP 状态。
- `shared-sdk/python/metahub_shared/runtime/webhooks.py`：注册 `route.fallback_triggered`。
- `shared-sdk/python/metahub_shared/runtime/webhook_delivery.py`：抽取控制面/数据面均可调用的租户级 Webhook 投递内核；控制面保留现有服务包装和响应 DTO。
- `gateway-data-plane/src/metahub_gateway/events`：提供有界、非阻塞的进程内事件队列和生命周期 worker，使 Webhook 网络 I/O 不进入 Fallback 热路径。
- `console-web/src/lib/modelAliases.js`：Fallback 专用 PATCH 客户端。
- `console-web/src/features/model-aliases`：策略配置交互。
- `console-web/src/features/request-logs`：尝试顺序与最终模型时间线。
- 控制面、数据面、前端、迁移测试和开发文档。

## 数据表/字段设计

### fallback_policies

每个 ModelAlias 最多一条策略。`model_aliases.default_model_id/fallback_model_id` 继续是主备目标的唯一事实来源；策略表只保存是否启用、触发条件和固定动作，避免模型 ID 双写漂移。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `fallback_policy_id` | TEXT PK | `fbp_<uuid>` |
| `tenant_id` | TEXT NOT NULL | 租户边界 |
| `model_alias_id` | TEXT NOT NULL UNIQUE | 一别名一策略 |
| `status` | TEXT CHECK `enabled/disabled` | 是否允许请求后 Fallback |
| `version` | INTEGER NOT NULL CHECK >= 1 | 每次有效修改递增，尝试日志保存版本快照 |
| `priority` | INTEGER NOT NULL DEFAULT 100 CHECK 1..1000 | 为后续多策略预留；MVP 固定 100 |
| `condition_expr` | TEXT NOT NULL | 规范 JSON 数组，只允许 `upstream_timeout/upstream_rate_limited/upstream_5xx` |
| `action` | TEXT NOT NULL | 固定为 `{\"type\":\"use_alias_fallback\",\"max_attempts\":1}` |
| `created_by` | TEXT NULL | 创建管理员 |
| `updated_by` | TEXT NULL | 最近修改管理员 |
| `created_at` | TEXT NOT NULL | UTC |
| `updated_at` | TEXT NOT NULL | UTC |

约束与迁移：

- 外键关联 tenant、ModelAlias；INSERT/UPDATE trigger 校验策略与别名同租户。
- `condition_expr/action` 只能由服务端从枚举生成，不接受或执行用户提供的原始表达式。
- 0026 为已有 `fallback_model_id IS NOT NULL` 的别名回填 enabled 标准策略；无备用模型别名不回填。这样升级后既有主备配置获得 Excel 约定的新能力。
- 创建别名且提供备用模型时同事务创建 enabled 标准策略；清空备用模型时同事务禁用策略并递增版本。
- 现有通用 `PATCH /model-aliases/{id}` 若修改 `fallback_model_id`，必须复用同一内部写入函数：设置非空目标时保留当前策略状态；历史无策略时创建 enabled 标准策略；清空时禁用。新专用 PATCH 是控制台和公开文档的首选入口。
- 索引 `(tenant_id, status)`、唯一 `(tenant_id, model_alias_id)`；虽然 `model_alias_id` 全局唯一，租户复合唯一便于查询计划和一致性校验。

### routing_attempts

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `routing_attempt_id` | TEXT PK | `rta_<uuid>` |
| `tenant_id` | TEXT NOT NULL | 租户边界 |
| `request_id` | TEXT NOT NULL | 同一客户端请求的稳定 ID |
| `trace_id` | TEXT NOT NULL | 链路 ID |
| `model_alias_id` | TEXT NULL | 直接模型请求为空 |
| `fallback_policy_id` | TEXT NULL | 请求时命中的策略 |
| `fallback_policy_version` | INTEGER NULL CHECK >= 1 | 策略版本快照 |
| `attempt_sequence` | INTEGER NOT NULL CHECK IN (1,2) | 尝试顺序 |
| `attempt_role` | TEXT CHECK `primary/fallback` | 路由计划中的角色；调用前直接选择备用时 sequence=1、role=fallback |
| `trigger_type` | TEXT CHECK `initial/fallback` | 是否由前一次失败触发 |
| `trigger_error_code` | TEXT NULL | Fallback 行记录主尝试的固定错误码 |
| `model_id` | TEXT NOT NULL | 本次实际模型 |
| `internal_model_id` | TEXT NOT NULL | 模型目录中的内部别名 |
| `provider_id` | TEXT NOT NULL | 本次 Provider |
| `provider_code` | TEXT NOT NULL | 安全展示字段 |
| `status` | TEXT CHECK `started/succeeded/failed` | 先插入 started，完成后条件更新终态 |
| `error_code` | TEXT NULL | 统一安全错误码 |
| `error_category` | TEXT NULL CHECK `timeout/rate_limited/upstream_5xx/other` | 稳定分类；非可重试错误为 other |
| `upstream_http_status` | INTEGER NULL CHECK 100..599 | Adapter 可安全提供时记录 |
| `retryable` | INTEGER NOT NULL CHECK IN (0,1) | 本次错误是否满足策略和错误白名单 |
| `latency_ms` | INTEGER NULL CHECK >= 0 | 单次上游耗时 |
| `usage_status` | TEXT CHECK `pending/recorded/unavailable/not_applicable` | 避免把未知用量误记为 0 |
| `usage_ledger_id` | TEXT NULL | 成功尝试关联最终 Usage；失败无上游 usage 时为空 |
| `estimated_cost_cny` | NUMERIC NULL CHECK >= 0 | 调用前本模型保守估算，用于解释预算；不是实际账单 |
| `created_at` | TEXT NOT NULL | started 时间 |
| `completed_at` | TEXT NULL | 终态时间 |
| `updated_at` | TEXT NOT NULL | 最近更新时间 |

约束与幂等：

- 唯一 `(tenant_id, request_id, attempt_sequence)`，同一个请求不能产生第 3 次尝试。
- 外键关联 tenant、model、provider、alias、policy；不对 `request_id` 建 `request_logs` 外键。现有 `request_logs.status` 只允许 `success/failed`，而 attempt 必须在上游调用前落库，因此二者只能通过 `(tenant_id, request_id)` 逻辑关联，不能制造不存在的 `processing` 主日志或形成插入顺序死锁。
- 每次上游调用前必须先插入 started；该行本身就是进程崩溃时可追踪的调用/错误日志。终态更新必须使用 `tenant_id + routing_attempt_id + status='started'`，重复完成不覆盖首个事实。
- request_logs 最终写入后，UPDATE trigger 校验其 `final_routing_attempt_id` 必须属于同 tenant_id、request_id；日志详情先加载 request_logs，再按同一租户和 request_id 加载 attempts，禁止仅凭 request_id 跨租户读取。
- 不存请求体、响应体、上游原始错误、Header、Key ID 或 Key 指纹。`error_code/category/http_status` 均来自 Adapter 白名单映射。
- started 超过运行时超时仍未完成表示进程终止或数据库异常，日志详情显示“结果未知”，本 PRD 不增加后台修复任务。
- 索引 `(tenant_id, request_id, attempt_sequence)`、`(tenant_id, model_alias_id, created_at DESC)`、`(tenant_id, status, created_at DESC)`。

### request_logs 扩展

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `fallback_triggered` | INTEGER NOT NULL DEFAULT 0 CHECK 0/1 | 是否发生请求后 Fallback |
| `routing_attempt_count` | INTEGER NOT NULL DEFAULT 0 CHECK 0..2 | 已创建尝试数 |
| `final_routing_attempt_id` | TEXT NULL | 最终成功或最终失败尝试 |

`provider_id/model_id/resolved_model/provider_code` 始终回写最终尝试。调用前因健康过滤直接选备用时 `fallback_triggered=0`、attempt_count=1。详情中的完整链以 `routing_attempts` 为准，汇总字段仅用于列表和快速筛选。

### usage_ledger 扩展

新增 `routing_attempt_id TEXT NULL`，成功非流式请求关联唯一成功尝试。失败尝试若 Provider 未返回可信 usage，不虚构 Token 或账单，保持 `usage_status=unavailable`、`usage_ledger_id=NULL`，并在尝试行保留调用前 `estimated_cost_cny`。最终成功模型继续写一条真实或 Adapter 明确标记为 estimated 的 Usage；主、备均失败则无 Usage 账本，但两条尝试均保留成本估算和不可得状态。

预算预留使用两候选估算成本之和。成功 Usage 入账后，现有 reservation 的 `NOT EXISTS usage_ledger` 逻辑会停止计入整笔预留；因此控制台必须把失败尝试的估算标为“预算准入估算，不等于已计费成本”，不能把它并入正式 Usage 汇总。Provider 对失败调用是否计费无法从标准 OpenAI-compatible 错误可靠判断，这是 MVP 的明确可观测边界，而不是以 0 伪装真实成本。

### 回滚

`0026_non_stream_fallback.down.sql` 先删除 trigger/index，再删除 `routing_attempts/fallback_policies`，最后按依赖反序删除 `usage_ledger.routing_attempt_id` 和 request_logs 三个字段。迁移测试覆盖 0025 数据上行、回滚和再次上行，并执行 `PRAGMA foreign_key_check`。

## API 设计

### PATCH /model-aliases/{model_alias_id}/fallback

- 权限：admin；developer/viewer 后端返回 403。读取模型别名列表仍沿用 admin/developer 权限。
- 不接收 `tenant_id`；租户只来自认证上下文。
- 请求体严格禁止额外字段：

```json
{
  "enabled": true,
  "fallback_model_id": "mdl_xxx",
  "trigger_conditions": [
    "upstream_timeout",
    "upstream_rate_limited",
    "upstream_5xx"
  ]
}
```

- `enabled` 必填。启用时 `fallback_model_id` 必填且必须是当前租户 active 模型，不能等于默认模型；禁用时可省略 target 以保留备用配置。显式 `fallback_model_id:null` 表示清空目标并强制 disabled。
- `trigger_conditions` 可省略并默认三个标准条件；提供时必须非空、无重复且只含白名单。为了满足 Excel 主流程，不能通过 API 创建 enabled 但没有任何触发条件的空策略。
- 修改 target、status 或 conditions 任一字段都使 version + 1；完全相同的幂等 PATCH 返回现有版本，不制造无意义版本和审计事件。
- 单事务校验别名、模型租户/状态，更新 `model_aliases.fallback_model_id` 和 `fallback_policies`；提交后写现有管理审计。
- 200 响应：

```json
{
  "model_alias": {},
  "fallback_policy": {
    "fallback_policy_id": "fbp_xxx",
    "model_alias_id": "mal_xxx",
    "status": "enabled",
    "version": 2,
    "priority": 100,
    "trigger_conditions": ["upstream_timeout", "upstream_rate_limited", "upstream_5xx"],
    "max_attempts": 1,
    "updated_at": "2026-07-13T00:00:00Z"
  },
  "request_id": "req_xxx",
  "trace_id": "req_xxx"
}
```

### GET /model-aliases

在现有 `ModelAliasInfo` 中新增可空 `fallback_policy` 摘要，不新增请求。旧数据库迁移后，有备用模型的别名均有策略；无策略时返回 null。响应不返回原始 `condition_expr/action` JSON，只返回解析后的枚举和固定 `max_attempts`。

### GET /request-logs/{request_id}

在现有详情中新增 `routing_attempts[]`，按 sequence 升序：

```json
{
  "routing_attempts": [
    {
      "routing_attempt_id": "rta_1",
      "attempt_sequence": 1,
      "attempt_role": "primary",
      "trigger_type": "initial",
      "model_id": "mdl_main",
      "resolved_model": "internal-main",
      "provider_code": "provider-a",
      "status": "failed",
      "error_code": "gateway.upstream_error",
      "error_category": "upstream_5xx",
      "upstream_http_status": 503,
      "retryable": true,
      "usage_status": "unavailable",
      "estimated_cost_cny": 0.003,
      "latency_ms": 420
    },
    {
      "routing_attempt_id": "rta_2",
      "attempt_sequence": 2,
      "attempt_role": "fallback",
      "trigger_type": "fallback",
      "trigger_error_code": "gateway.upstream_error",
      "model_id": "mdl_backup",
      "resolved_model": "internal-backup",
      "provider_code": "provider-b",
      "status": "succeeded",
      "retryable": false,
      "usage_status": "recorded",
      "latency_ms": 310
    }
  ]
}
```

权限沿用调用日志 reader（admin/developer）；跨租户 request_id 返回 404 防枚举。started 行没有 completed_at/latency/error，不推断为 failed。

## 路由计划与执行设计

### 调用前计划

1. 新增 `GatewayRoutePlan`：`initial_route`、`initial_role`、`fallback_route`、`fallback_policy_id/version/conditions`、候选拒绝摘要。
2. `resolve_model_route_plan` 只读同一事务快照中的别名、策略、主备模型、Provider 健康。直接模型计划只有 initial；ModelAlias disabled 仍失败。
3. 默认模型可用时 initial=default；策略 enabled 且备用模型可用时附带 request-time fallback。
4. 默认模型被 disabled/fresh unhealthy 过滤而备用可用时 initial=fallback、fallback_route=None，`routing_reason=alias.fallback_preflight`。这属于 PRD-035 调用前选择，不发布 Fallback 事件。
5. 默认可用但备用不可用时仍调用默认模型，计划记录备用拒绝原因；默认调用失败后不重复解析数据库、不现场等待健康恢复。
6. 路由计划是请求开始时的不可变快照。管理员在调用期间修改策略不会改变本次行为，尝试行通过 policy version 可解释。
7. 保留 `resolve_model_route` 兼容包装，供 Debug Console 等当前只支持单调用的模块取 `plan.initial_route`；该包装不自动执行 Fallback。

### 非流式执行状态机

1. 鉴权、JSON 解析、路由计划、能力校验、DLP、限流、预算准入均只执行一次。预算使用计划中实际可能调用的候选之和；预检已直接选备用时只估算一个候选。
2. 在任何 Provider Key 解密和上游网络调用前插入 sequence=1 started；写入失败时记录不含敏感信息的应用错误日志、释放治理租约/预留并返回 `gateway.routing_attempt_unavailable`，绝不发送一笔无法审计的上游请求。
3. 初始成功：完成 attempt 1 为 succeeded，写 request_logs success 和最终路由，写 Usage 并关联 attempt，返回响应。
4. 初始失败：完成 attempt 1 为 failed。仅当以下条件同时成立时继续：非流式；计划含 fallback；错误分类命中策略；错误来自 Adapter 明确的 timeout、HTTP 429 或 HTTP 5xx；尚未创建 sequence=2。
5. Fallback 触发时先原子插入 sequence=2 started；唯一约束冲突表示同 request_id 已触发，不能再次发出网络请求。插入提交后同步记录 `route.fallback_triggered` 安全结构化事件，并把同一事件放入有界非阻塞 Webhook 队列。
6. 为 fallback route 重新选择其 Provider Key并重新构建 ProviderRequest。复用原始已通过 DLP 的 payload，不复用主 Provider 的 URL、Header、Key 或上游 model 值。
7. Fallback 成功：完成 attempt 2，request_logs success/fallback_triggered=1/count=2/final=attempt2，最终 Provider/模型指向备用，Usage 关联 attempt2。
8. Fallback 失败：完成 attempt 2，request_logs failed/final=attempt2，释放失败请求预算预留，返回备用错误；attempt1 仍保留主错误。
9. 任意非可 Fallback 错误：只完成 attempt1 和 request_logs failed，释放失败请求预算预留并返回原错误。
10. request_logs/usage 的终态汇总继续采用安全容错，避免日志故障覆盖已取得的模型响应；但 sequence 1、2 的 started 持久化都是上游调用执行门。任一执行门失败都 Fail Closed，不得在没有可审计证据时调用对应模型。

### 可重试错误白名单

| 上游事实 | 统一错误码 | 分类 | 可触发 |
| --- | --- | --- | --- |
| httpx connect/read/write/pool timeout | `gateway.upstream_timeout` | `timeout` | 是 |
| HTTP 429 | `gateway.upstream_rate_limited` | `rate_limited` | 是 |
| HTTP 500..599 | `gateway.upstream_error` | `upstream_5xx` | 是 |
| DNS、连接拒绝、TLS/协议错误且无 HTTP 状态 | `gateway.upstream_unavailable` | `other` | 否，Excel 未把通用传输错误列入触发条件 |
| HTTP 400/401/403/404/409/422 | 对应 Adapter 统一码 | `other` | 否 |
| 鉴权、DLP、限流、预算、路由、Key 不可用 | 现有网关错误码 | 不产生上游 attempt 或 `other` | 否 |

`ApiError.retryable` 不能单独决定 Fallback，因为面向客户端的“可重试”语义可能包含 Provider 健康、数据库或本地治理错误。Provider 层新增 `UpstreamApiError(ApiError)`，只额外携带 `NormalizedProviderError` 中的受控 `http_status/error_category/retryable` 元数据；统一异常处理仍按父类输出既有安全响应，编排器只认上述三类，不解析 message 或异常字符串。

### 幂等与重试风暴

- 本 PRD 的“幂等”是同一网关执行链不重复创建 sequence=2，不承诺客户端使用相同业务请求重发时只调用一次上游。OpenAI-compatible 请求目前没有平台级 Idempotency-Key 合同，不能虚构跨请求去重。
- 数据库唯一键是备用网络调用前的闸门；`INSERT OR IGNORE` 后必须确认是本执行器新建，否则不再调用。
- 最大尝试数编译期与数据库双重限制为 2；策略 action 中 max_attempts 只能是 1。
- 不做主模型自身重试，不在 fallback 失败后返回主模型，不递归解析 fallback 别名。
- 主、备允许属于同一 Provider但必须是不同 model_id；每次重新选 Key。控制台提示同 Provider 对 Provider 级 5xx 的容灾价值较低，但不强制禁止客户配置。

### 事件设计

`route.fallback_triggered` 在 sequence=2 started 成功提交后产生一次，稳定 `event_id=evt_<routing_attempt_id>`，同一逻辑触发的传输重试复用该 ID。payload 仅包含：

- `request_id/trace_id`
- `model_alias_id`
- `fallback_policy_id/version`
- `from_model_id/to_model_id`
- `from_provider_code/to_provider_code`
- `trigger_error_code/error_category`
- `attempt_sequence=2`
- `triggered_at`

不含 Prompt、Response、Key、Endpoint、Header、上游原始错误正文。结构化应用日志与 Webhook 使用同一安全 payload。

数据面不能在 Fallback 热路径同步执行现有最多三次的 HTTP 投递，否则一个故障 Webhook 会直接延迟容灾。Gateway lifespan 启动一个有界进程内队列 worker；触发路径只做 `put_nowait`，队列满或 worker 不可用时写安全告警并继续备用调用。worker 通过 `asyncio.to_thread` 调用 shared SDK 投递内核，沿用 Endpoint 的最多三次、数据库 delivery/attempt 和 best-effort 语义。shutdown 停止接收后有界排空，超时则取消等待；没有 outbox，因此进程崩溃仍可能丢事件，这是与现有 best-effort 合同一致的明确边界。为避免数据面依赖控制面包，把通用投递内核下沉 shared SDK，控制面的配置管理/DTO 保持原模块边界。

## 前端页面/交互设计

### PAGE-053 模型别名页

- 沿用当前紧凑模型别名列表，不新增营销式卡片。每行主模型、备用模型下方显示策略状态、触发条件和版本。
- admin 显示“配置 Fallback”图标按钮及 tooltip；developer 只读；viewer 不进入模型目录。按钮隐藏只改善体验，后端专用 PATCH 仍强制 admin。
- 点击后打开单层对话框：启用开关、备用模型选择器、三个触发条件复选框。最大尝试次数显示固定“1 次”只读信息，不提供可制造重试风暴的数值输入。
- 备用模型选择器排除当前主模型，展示模型名、Provider 和状态；disabled 模型不允许提交。
- 保存期间只锁定该别名；成功后合并响应并显示 request_id；失败显示统一错误码和 request_id，不回显底层异常。
- 禁用策略默认保留备用模型，便于短期停用后恢复；“清除备用模型”是独立复选确认，提交 null 并自动禁用。
- 同 Provider 的主备组合显示非阻断警告“Provider 级故障时可能同时受影响”，不禁止保存。

### PAGE-054 日志详情

- 在路由决策与 Usage 之间新增“上游尝试”时间线，按顺序展示主/备标签、模型、Provider、耗时、结果、固定错误码和成本状态。
- Fallback 成功时详情头部显示“已切换备用模型”，请求日志主模型栏仍显示客户端请求的 alias，最终模型栏显示实际备用模型。
- 失败尝试没有可信 usage 时显示“上游未返回用量”，估算成本明确标注“预算准入估算”，不得显示为正式已计费成本。
- 调用前因健康过滤直接选择备用时只显示一条“初始候选：备用”，不显示主模型失败或 Fallback 已触发。
- started 未完成显示“结果未知”，避免伪造失败原因。
- 1440 宽时间线横向字段对齐；1100/720/390 下改为单列条目。长 request/model/provider ID 使用 `overflow-wrap:anywhere`，无横向溢出。
- 状态同时使用文字和现有图标/颜色；图标按钮有 tooltip，键盘可聚焦。

## 错误码设计

| 错误码 | HTTP | 场景 |
| --- | --- | --- |
| `model_alias.fallback_invalid` | 422 | 主备相同、启用无备用、conditions 空/重复/非法 |
| `model_alias.fallback_model_not_found` | 404 | 备用模型不存在或不属于当前租户，防跨租户枚举 |
| `model_alias.fallback_model_disabled` | 409 | 启用策略时备用模型不是 active |
| `model_alias.fallback_update_unavailable` | 503 | 策略事务读写失败 |
| `gateway.routing_attempt_unavailable` | 503 | 初始尝试执行门无法持久化，未调用上游 |
| `gateway.fallback_unavailable` | 503 | Fallback 执行门/attempt 无法持久化，禁止无审计调用备用 |
| `gateway.upstream_timeout` | 504 | 上游超时，可按策略触发 |
| `gateway.upstream_rate_limited` | 429 | 上游 429，可按策略触发 |
| `gateway.upstream_error` | 502 | 上游 5xx，可按策略触发 |
| `auth.permission_denied` | 403 | developer/viewer 修改策略 |

别名不存在或跨租户继续复用 `model_alias.not_found` 404。所有 API 错误通过统一处理器携带 `request_id/trace_id`。主备都失败时对客户端返回备用尝试的错误码；详情仍展示两个错误。

## 安全与权限约束

1. 策略写操作仅 admin；读取别名和调用日志沿用 admin/developer。前端隐藏控件不替代 FastAPI 依赖与服务层 tenant 条件。
2. 所有新表强制 `tenant_id`，查询和更新先以 tenant_id 过滤；跨租户对象统一按不存在处理。
3. 不接受原始条件表达式、SQL、URL、Header 或 Key；条件由固定枚举编译为规范 JSON，运行时不使用 `eval`。
4. Provider Key 每次尝试独立选择并仅在构建出站请求时解密；attempt、request log、Webhook、结构化日志、错误响应和前端状态不得出现明文、密文、Authorization 或完整指纹。
5. Fallback 使用请求开始时的不可变、安全路由计划，不在失败后使用客户端字段重新解析 Provider URL 或模型，避免二次输入注入。
6. DLP 只检查一次原始 payload，Fallback 只能复用已通过检查的 payload；不得在日志中保存该 payload。
7. 限流一次申请、finally 一次释放并发租约。Fallback 不重复消耗 RPM/TPM，但上游调用次数通过 routing_attempts 可观测。
8. 预算在任何上游调用前按最坏两次调用保守预留；预算拒绝不产生 routing_attempt。主备均失败或首个非可重试失败时释放预留。
9. 每次上游尝试都必须先持久化 started。数据库故障时 Fail Closed，不允许为了可用性跳过审计与风暴闸门；终态 request_logs/usage 故障不能改变已经获得的 Provider 响应。
10. Adapter 错误正文只用于内存内映射，不进入数据表/API/Webhook；HTTP 状态和固定代码足以诊断。
11. Webhook 事件 ID 稳定，接收端可幂等；有界进程内队列和投递是 best-effort，不承诺 outbox 级至少一次，队列满不得反压 Fallback 热路径。
12. 流式请求不读取或执行 request-time fallback，不存在首字节后切换分支。

## 测试用例

### 主流程

1. admin 为别名配置主、备和三个条件，专用 PATCH 返回策略版本；GET 别名展开相同配置。
2. 主模型 HTTP 503、备用成功：上游调用顺序严格为主后备；客户端得到备用响应；request_logs 成功且最终模型/Provider 为备用；attempt 1 failed、attempt 2 succeeded；Usage 关联 attempt 2。
3. 主模型超时、备用成功；主模型 429、备用成功，分别记录 timeout/rate_limited 与 trigger code。
4. 主模型成功时只调用一次，不创建 attempt 2、不发布事件。
5. 主模型在调用前 fresh unhealthy，备用作为初始候选成功：只有 sequence 1 role=fallback、trigger_type=initial，`fallback_triggered=0`。
6. 主、备均失败：客户端返回备用统一错误；request_logs failed/final attempt2；详情按顺序显示两个安全错误。
7. Chat Completions 与 Embeddings 非流式分别覆盖 5xx Fallback。

### 边界、风暴与预算

8. stream=true 的主模型 5xx、握手失败和首字节后断流均不调用备用；流式既有清理/Usage 行为不回归。
9. 主模型 400/401/403/404、连接拒绝/TLS、Key 不可用、DLP、平台限流、预算阻断均不触发 Fallback。
10. 策略仅启用 timeout 时，429/5xx 不触发；conditions 顺序归一化且重复/空值被拒绝。
11. 并发或重复进入同一 request_id 的 sequence 2 插入只有一个执行器成功，备用上游最多收到一次；永远无法创建 sequence 3。
12. 主、备同 Provider不同模型可执行且每次重新选 Key；主备相同 model_id 被拒绝。
13. 预算估算为主、备成本之和；备用更贵且总和超预算时在主调用前阻断；主模型预检被过滤时只估算实际备用候选。
14. Fallback 不重复 DLP、不重复 RPM/TPM 扣减；并发租约只释放一次。
15. 进程在 attempt started 后终止，专用 attempt 查询事实保留为 started；已有 request_logs 的详情显示结果未知且不伪造用量/成本。数据库无法创建 attempt1 时不发主请求并返回 routing_attempt_unavailable；无法创建 attempt2 时不发备用请求并返回 fallback_unavailable。
16. 主失败没有 usage 时 attempt usage_status=unavailable、估算成本有明确标签；备用成功 Usage 与 request_logs 成本仍按最终真实/Adapter 估算值写入。

### 权限、租户与脱敏

17. admin 可 PATCH；developer/viewer 403；未登录 401。developer 可读模型别名和日志尝试。
18. 使用其他租户 alias/model/request_id 返回 404，数据库直接插入跨租户 policy/attempt 被 trigger 拒绝。
19. Provider Key、Authorization、Prompt、Response 和恶意上游错误正文不会出现在数据库、API、Webhook、应用日志或前端 DOM；测试使用 canary 全链扫描。
20. Webhook 只在 attempt2 started 首次提交后产生一次稳定 event_id；主预检跳过、主成功、策略关闭和非可重试失败都不产生。
21. Webhook Endpoint 超时/失败、队列满和 shutdown 排空超时不会阻止或延迟备用模型调用，不会递归产生事件；payload 不含敏感字段。

### 迁移、兼容与前端

22. 0025 数据升级：有 fallback_model_id 的别名回填 enabled 标准策略，无 fallback 的不回填；up/down/up 和 foreign_key_check 通过。
23. 现有通用 create/patch alias 与专用 PATCH 使用同一策略写入语义；旧客户端不需要发送新字段也能继续工作。
24. `resolve_model_route` 兼容调用只返回 initial route，不意外执行网络 Fallback；Debug Console 既有测试不回归。
25. 模型别名页 admin 可配置、developer 只读；保存成功/统一错误/request_id、同 Provider 警告和清除确认正确。
26. 日志详情正确展示一条、两条、预检备用、双失败、started 未完成和 usage unavailable；1440/1100/720/390 无重叠或横向溢出。

## 代码修改计划

1. 新增 0026 up/down 迁移和迁移测试，先建立策略/尝试/汇总的数据库事实与跨租户约束。
2. 扩展统一错误与 Webhook 事件注册；把不依赖控制面 DTO 的投递内核下沉 shared SDK，保持现有控制面测试兼容。
3. 在模型别名控制面增加 Fallback DTO、专用 PATCH、事务服务、列表展开、管理审计和权限测试。
4. 将路由解析重构为 `GatewayRoutePlan`，保留单路由兼容包装；增加策略快照、调用前健康过滤和候选拒绝测试。
5. 新增 routing attempt 持久化服务和 request_logs/usage 关联，扩展日志详情 API。
6. 修改预算准入接受有界候选并按估算成本总和预留，覆盖更贵备用模型和预检过滤场景。
7. 重构非流式调用为最多两次的显式状态机；流式路径保持独立；增加 Adapter 受控 HTTP 状态/错误分类元数据。
8. 增加 Fallback 事件结构化日志和 best-effort Webhook 投递，验证稳定 event_id 与脱敏。
9. 完成模型别名策略对话框、日志尝试时间线、API 客户端、样式及前端单元测试。
10. 更新 `docs/testing/PRD-036-tests.md`、README/开发进度，执行迁移、后端、数据面、前端、编译、Lint、中文注释门禁和本地服务烟测。
11. 使用 Playwright 在 1440/1100/720/390 验证模型别名页与日志详情，不允许重叠、截断、横向溢出或权限状态错位。

## 设计审核记录

### 第一轮：需求、边界与一致性审核

- 结论：通过，已按发现项修订。
- 发现 1：原初稿要求先写 `request_logs(status=processing)`，但 0001 表约束只允许 `success/failed`。已删除虚构状态，由 `routing_attempts.started` 承担在途事实。
- 发现 2：attempt 必须先于上游调用，而 request_logs 当前在调用终态写入，二者不能建立 request_logs 前置外键。已改为 `(tenant_id, request_id)` 逻辑关联，并用 final attempt trigger 校验最终引用。
- 发现 3：已有 `fallback_model_id` 与新策略表可能双写漂移。已明确模型目标只存 model_aliases，policy action 固定引用 alias fallback，所有旧/新写入口复用同一事务函数。
- 发现 4：PRD-035 的预检备用与 PRD-036 的请求后 Fallback 容易混淆。已固定预检备用只有一条 sequence=1/initial，既不发布事件也不设置 fallback_triggered。
- 发现 5：Excel 只列超时、5xx、限流。已明确通用连接/TLS/DNS unavailable 虽对客户端可重试，但不扩大为本 PRD 的 Fallback 条件。

### 第二轮：失败模式、安全与可测试性审核

- 结论：通过，已按发现项修订。
- 发现 1：原初稿只把 sequence=2 当执行门，sequence=1 日志失败后仍可能产生无法审计的调用。已要求每次网络调用前必须成功写 started，分别返回统一执行门错误。
- 发现 2：现有 Webhook 是同步最多三次 HTTP 投递，若直接复用会阻塞备用调用。已改为 Gateway lifespan 管理的有界非阻塞队列，触发路径只入队，投递失败和队列满均不反压。
- 发现 3：仅凭通用 `ApiError.retryable` 会错误扩大条件。已设计安全 `UpstreamApiError` 元数据，编排器只认 Adapter 白名单分类和状态。
- 发现 4：失败 Provider 通常不返回可信 usage，写 0 会造成错误账单事实。已保留 `usage_status=unavailable` 和明确预算估算，不把估算并入正式 Usage 汇总。
- 发现 5：同 request_id 并发触发和进程崩溃可能制造重试风暴。已用 sequence 唯一键作为调用前闸门，固定最大两次，并明确跨客户端请求不承诺业务幂等。
- 发现 6：流式握手失败是否可切换存在解释空间。Excel scope 明确非流式，因此本设计对 stream=true 全路径禁用 request-time Fallback，避免实现隐式扩边。
