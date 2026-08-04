# RoutingPolicy 与模型质量 API

本文档描述 PRD-047 已验收的 ModelAlias 路由基线，以及 PRD-048 已验收的模型质量评分、`quality_cost`、Dry-Run 和不可变 RequestLog 解释合同。PRD-047 的最终证据见 [PRD-047 测试记录](../testing/PRD-047-tests.md)；PRD-048 的专项、全量、0037 迁移、真实三服务、浏览器、清理和停止后终态见 [PRD-048 测试记录](../testing/PRD-048-tests.md)。

设计依据见 [PRD-047 设计文档](../prd/PRD-047-cost-first-availability-first-routing.md) 和 [PRD-048 设计文档](../prd/PRD-048-quality-cost-routing-foundation.md)。

## 1. 公共约束

- Control API 使用 Bearer Session 认证。所有租户范围只来自认证上下文，查询参数、路径和请求体都不能选择 `tenant_id`。
- `GET /model-aliases` 允许 `admin`、`developer`；`viewer` 由后端返回 `403 auth.permission_denied`。
- `PATCH /model-aliases/{model_alias_id}/routing-mode` 仅允许 `admin`；`developer`、`viewer` 均由后端拒绝。前端只读或隐藏页面不是权限边界。
- `GET /model-quality-scores` 允许 `admin`、`developer`，`PATCH /model-quality-scores` 仅允许 `admin`；`viewer` 和匿名请求由后端拒绝，权限检查先于 PATCH 正文读取。
- 所有响应头携带 `X-Request-ID`、`X-Trace-ID`；成功响应体携带 `request_id`、`trace_id`，错误响应使用统一结构。
- 客户端可以发送 `X-Request-ID`。需要对一次写操作做结果未知重试时，必须原样复用第一次请求的 ID、路径和正文；不发送时由服务生成 ID，但客户端无法据此执行精确重试。
- ModelAlias、RoutingPolicy、候选、审计和事件查询均按认证 `tenant_id` 限定。其他租户的 Alias ID 在写接口上表现为 `404 model_alias.not_found`，不暴露对象是否存在。
- 响应和事件不返回 Provider Key、Endpoint、请求头、Prompt、Response、价格正文或 KeyPool 数量。

统一错误响应示例：

```json
{
  "code": "model_alias.routing_policy_version_conflict",
  "message": "Routing policy was changed by another request.",
  "request_id": "req_console_routing_policy_...",
  "trace_id": "trace_...",
  "suggestion": "Refresh the model alias and retry from the latest routing policy version.",
  "diagnosis": {
    "rule_id": "routing_policy_write_conflict",
    "category": "routing_policy_conflict",
    "reason": "路由策略已被并发修改，或同一 request_id 被用于不同配置意图。",
    "suggestion": "重新读取最新策略版本；只有完全相同的原请求才能复用 request_id。",
    "action": "基于最新版本重新确认模式并使用新的 request_id 提交。",
    "severity": "error",
    "retryable": false,
    "matched_by": "error_code",
    "safe_summary": "model_alias.routing_policy_version_conflict http_status=409"
  }
}
```

## 2. 读取 ModelAlias 与 RoutingPolicy

当前实现的读取入口是列表接口，不存在单独的 `GET /model-aliases/{id}`：

```http
GET /model-aliases?status=active&environment_id={environment_id}&environment=dev
Authorization: Bearer {session_token}
X-Request-ID: {request_id}
```

三个筛选参数均可省略：

| 参数 | 含义 |
| --- | --- |
| `status` | Alias 状态，使用现有 `active`、`disabled` 合同。 |
| `environment_id` | 当前租户内的 Environment ID；跨租户或不存在的 ID 不作为探测通道。 |
| `environment` | 兼容 Environment code，当前为 `dev`、`test`、`prod`。同时传 ID 和 code 时必须指向同一当前租户 Environment。 |

顶层响应仍为：

```json
{
  "model_aliases": [],
  "request_id": "req_...",
  "trace_id": "trace_..."
}
```

每个既有 ModelAlias DTO 新增或明确以下字段：

| 字段 | 类型 | 合同 |
| --- | --- | --- |
| `routing_policy` | object 或 `null` | 当前持久 RoutingPolicy 的安全摘要。正常 0036 数据库中每个 Alias 都应有一条策略。 |
| `effective_routing_mode` | string | 有持久策略时等于 `routing_policy.routing_mode`；缺策略的旧夹具只读兼容为 `fixed`。 |
| `routing_policy_implicit` | boolean | 有持久策略为 `false`；缺策略的旧夹具为 `true`。它不是可写的虚拟 Policy。 |

`routing_policy` 字段合同：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `routing_policy_id` | string | `rpol_` 开头的持久策略 ID。 |
| `tenant_id` | string | 返回对象的租户归属，仅供当前认证租户核对，不能由客户端提交。 |
| `model_alias_id` | string | 一对一绑定的 ModelAlias ID。 |
| `routing_mode` | string | `fixed`、`cost_first`、`availability_first`、`quality_cost` 之一。 |
| `quality_cost_config` | object | 始终返回五字段休眠/执行配置；旧三模式不执行但保留，`quality_cost` 按该配置执行。 |
| `status` | string | 当前实现为 `enabled`。 |
| `version` | integer | 正整数 CAS 版本；路由语义真实变化时严格增加 1，同值不增加。 |
| `candidates` | array | 从 Alias 当前 default/fallback 关系派生的最多两条配置摘要，不是历史请求结果。 |
| `warnings` | array | 有界安全警告；当前正常响应为空数组。 |
| `created_at`、`updated_at` | datetime | RoutingPolicy 自身时间；mode-only 更新不会修改 Alias 的 `updated_at`。 |

每条配置候选只包含：

| 字段 | 说明 |
| --- | --- |
| `candidate_role` | `default` 或 `fallback`。 |
| `configured_priority` | `default=0`，`fallback=1`。 |
| `model_id`、`internal_model_id`、`display_name`、`model_status` | 当前租户 Model 的安全目录摘要。 |
| `provider_code`、`provider_name`、`provider_status` | 当前租户 Provider 的安全目录摘要。 |

读取响应中的 `candidates` 只说明“当前配置了谁”，不承诺 Gateway 在某次请求中选中谁。真实请求解释应读取 `GET /request-logs/{request_id}` 的 `routing_decision` 和 `routing_candidates`。

0036 迁移会为存量 Alias 回填 `fixed v1`，新建 Alias 也由数据库触发器原子创建 `fixed v1`。如果旧测试夹具或损坏库缺少策略行，GET 只读返回 `routing_policy=null`、`effective_routing_mode=fixed`、`routing_policy_implicit=true`；所有相关写接口均以 `503 model_alias.routing_policy_update_failed` 失败关闭，不合成虚假 ID 或版本。

## 3. 更新路由模式

### 3.1 请求 wire

```http
PATCH /model-aliases/{model_alias_id}/routing-mode
Authorization: Bearer {admin_session_token}
Content-Type: application/json
X-Request-ID: {stable_request_id}

{
  "routing_mode": "cost_first",
  "expected_version": 3
}
```

旧三模式继续使用上述精确两字段 wire。`quality_cost` 必须提交完整配置：

```json
{
  "routing_mode": "quality_cost",
  "expected_version": 3,
  "quality_cost_config": {
    "quality_weight_bps": 5000,
    "cost_weight_bps": 3000,
    "latency_weight_bps": 2000,
    "quality_floor_bps": 6000,
    "minimum_sample_count": 20
  }
}
```

服务端先完成认证和 admin RBAC，再读取正文。因此未授权请求不会通过超大或畸形正文绕过权限优先级。认证通过后的 wire 约束如下：

- 媒体类型必须是 `application/json`，允许标准参数，例如 `application/json; charset=utf-8`。
- 正文必须是 1 至 8192 bytes 的 UTF-8 JSON；声明长度和实际流长度都受限。
- 根节点必须是对象。旧三模式字段集合必须精确等于 `routing_mode`、`expected_version`，禁止携带 `quality_cost_config`；`quality_cost` 字段集合必须再包含完整 `quality_cost_config`。包括 `tenant_id` 在内的额外字段、缺字段和未知字段均拒绝。
- 任意层级重复 JSON key、控制字符、超过 16 层或超过 64 个扫描节点的结构均拒绝。
- `routing_mode` 必须是区分大小写的 `fixed`、`cost_first`、`availability_first`、`quality_cost` 之一，不做 trim、大小写转换或未知值回落。
- `expected_version` 必须是 JSON integer，范围为 `1..9223372036854775807`；布尔、字符串、浮点和零/负数均拒绝。
- 质量、成本、延迟三项权重分别为严格 JSON integer，质量权重范围 `1..10000`，另两项 `0..10000`，三者之和必须精确为 `10000`；质量门槛和最小样本数均为 `1..10000`。

### 3.2 成功响应

```json
{
  "routing_policy": {
    "routing_policy_id": "rpol_...",
    "tenant_id": "tenant_...",
    "model_alias_id": "alias_...",
    "routing_mode": "cost_first",
    "quality_cost_config": {
      "quality_weight_bps": 5000,
      "cost_weight_bps": 3000,
      "latency_weight_bps": 2000,
      "quality_floor_bps": 6000,
      "minimum_sample_count": 20
    },
    "status": "enabled",
    "version": 4,
    "candidates": [
      {
        "candidate_role": "default",
        "configured_priority": 0,
        "model_id": "model_...",
        "internal_model_id": "deepseek-chat",
        "display_name": "DeepSeek Chat",
        "model_status": "active",
        "provider_code": "deepseek",
        "provider_name": "DeepSeek",
        "provider_status": "active"
      }
    ],
    "warnings": [],
    "created_at": "2026-07-18T00:00:00Z",
    "updated_at": "2026-07-18T00:01:00Z"
  },
  "changed": true,
  "request_id": "req_...",
  "trace_id": "trace_..."
}
```

`changed` 语义：

- 当前版本等于 `expected_version` 且 mode 或 `quality_cost_config` 任一执行字段真实变化：返回 `changed=true`，新版本必须精确为 `expected_version + 1`，同一请求不会为 mode 和配置分别增加两次版本。
- 当前版本等于 `expected_version` 且 mode/允许提交的配置均相同：返回 `changed=false`，版本、审计和配置事件均不变化。
- CAS 在 no-op 判断之前执行。旧版本即使提交与当前相同的 mode，也返回版本冲突，不能静默成功。

### 3.3 CAS、事务和幂等顺序

一次真实变更在同一个 `BEGIN IMMEDIATE` 事务中完成以下工作：

1. 按 `tenant_id + actor_user_id + request_id` 检查已提交配置事件。
2. 按认证租户加载 Alias 和一对一 RoutingPolicy。
3. 比较 `expected_version`，再判断是否 no-op。
4. 原子更新 mode 与可选完整质量配置，并令版本严格增加 1。
5. 同事务写入 `routing_policy.updated` 审计和不可变 `routing_policy_events` 配置事实。
6. 提交后才投递 `route.policy_selected` 的 `configuration_changed` Webhook；外部投递失败不回滚已生效配置。

真实变更的幂等指纹绑定路径 Alias、规范 mode、条件性完整质量配置和 `expected_version`：

- 第一次响应丢失后，原样复用 request ID 和正文；若当前 Policy 仍是原提交后的 ID、版本和 mode，返回原策略快照，`changed=true`，并复派同一持久事件 ID。
- 同一租户、同一 actor、同一 request ID 改用不同 Alias、mode 或版本，返回 `409 model_alias.routing_policy_request_conflict`。
- 原提交之后 Policy 已继续演进，即使原样重试也返回 request conflict，避免把旧版本伪装成当前权威状态。
- no-op 不写配置事件，也不永久占用 request ID；它只是一次基于当前版本的只读成功判断。

### 3.4 错误码

| HTTP | code | 场景 |
| --- | --- | --- |
| 401 | `auth.token_missing`、`auth.token_invalid`、`auth.token_expired`、`auth.session_revoked` | 未建立有效 Session。 |
| 403 | `auth.permission_denied` | 非 admin 调用 PATCH；viewer 调用 GET。 |
| 404 | `model_alias.not_found` | 当前认证租户内不存在该 Alias；跨租户 ID 同样返回此码。 |
| 422 | `model_alias.routing_mode_invalid` | Content-Type、UTF-8/JSON、大小/深度/节点、重复键、字段集合或 mode 非法。 |
| 422 | `model_alias.routing_policy_version_invalid` | `expected_version` 不是允许范围内的严格整数。 |
| 422 | `model_alias.quality_cost_settings_invalid` | `quality_cost` 缺完整配置、旧模式错误携带配置，或权重/门槛/样本约束不成立。 |
| 409 | `model_alias.routing_policy_version_conflict` | 当前 Policy 版本与 `expected_version` 不同。 |
| 409 | `model_alias.routing_policy_request_conflict` | 已提交 request ID 被不同意图复用，或原事件版本已不是当前权威版本。 |
| 503 | `model_alias.routing_policy_update_failed` | 缺持久 Policy、数据库/触发器、审计或配置事件事务不可安全提交。该错误可重试。 |

错误响应不会回显请求正文、SQL、数据库路径或底层异常文本，并继续携带 `request_id`、`trace_id`。

## 4. Gateway 执行合同

RoutingPolicy 只作用于命中当前租户和 Environment 的 ModelAlias 初始选路。Policy DSL `route_to` 已解析出的受控单模型和请求中的 direct internal model 均走单候选路径，不继承原 Alias RoutingPolicy，不产生 `route.policy_selected` 事件。

Alias 候选仍只来自 `default_model_id`、`fallback_model_id`，最多两条。Gateway 在一个短事务中冻结 Policy ID/版本/mode、候选 Model/Provider、健康事实，以及当前 mode 实际需要的 KeyPool、价格和 Fallback 事实，再用 shared 纯排序器得出结果并原子写 Decision/Candidate。

| mode | 资格与排序 | 不读取的事实 |
| --- | --- | --- |
| `fixed` | 保持现有 default → fallback 稳定顺序；模型/Provider 必须 active，新鲜 unhealthy Provider 被剔除，unknown/stale 健康证据 Fail Open。 | 不读 KeyPool、价格，不估算成本路由 Token。 |
| `availability_first` | 在 fixed 资格上增加 KeyPool 过滤；`unavailable` 被剔除。新鲜 healthy 优先于 unknown/stale，再按 default/fallback 稳定顺序。 | 不读价格，不估算成本路由 Token。 |
| `cost_first` | 在健康和 KeyPool 过滤后排序。只有一条合格候选时直接选择；恰有两条时才读取同一 `as_of` 价格并估算输入/输出 Token。 | 零或一条合格候选时不读价格、不估算成本路由 Token。 |
| `quality_cost` | 先做通用资格，再一次读取同租户 Score。ready 候选先过质量门槛；全 cold 按配置顺序，ready 优先于 cold。只有两条 ready-pass 且全部正权重依赖齐备时计算综合分。 | 零条通用候选不读 Score；单候选、全 cold、ready/cold、低质剔除或依赖缺失回退不做完整加权。成本权重为 0 时不估 Token/读价格，延迟权重为 0 时不要求延迟。 |

表中的“读/不读 KeyPool”只描述 PRD-047 初始排序使用的无副作用可用性快照。初始路由选定后，既有 Provider 调用阶段仍会按原合同选择和解密实际 Key；路由快照不会返回、记录或提前解密 Key。

环境 Provider Key 是受控的本地兼容路径，不是跨租户默认凭据。它只能在非生产环境供 active 默认租户使用，且 active Provider 的 code、数据库 Endpoint 与实际调用冻结 Route Endpoint 必须同时与运维基准精确一致。生产、非默认租户、非 active 对象或 code/Endpoint 错配均不读取进程级 Key；实际调用统一返回 `503 provider_key.unavailable`，其他租户必须使用自己的托管 Key。

两候选成本比较使用 Decimal 计算：

```text
estimated_cost =
  (estimated_input_tokens * input_price_per_1k
   + estimated_output_tokens * output_price_per_1k) / 1000
```

排序和持久解释使用同一个保留 15 位小数、`ROUND_HALF_UP` 的金额。带版本的零价是合法价格；相同成本按 default/fallback 稳定顺序。任一候选正常缺价时以 `routing.cost_fixed_fallback.price_missing` 回退固定顺序；两条有效价格币种不同则以 `routing.cost_fixed_fallback.currency_incomparable` 回退固定顺序。非法 Decimal、未知币种、半组价格或 owner 图损坏不是“缺价”，而是失败关闭的无效快照。

PRD-036 Fallback 是初始选路之后、非流式上游失败时最多一次的第二次尝试，不能与初始候选排序混淆。动态模式初始即选中 Alias fallback 时仍是第一次 `primary` attempt，`fallback_triggered=false`，不会反向重试 default；只有未被动态过滤且为 standby 的备用候选才可成为后续 runtime fallback。`quality_cost` 中低于质量门槛或 cold 的 fallback 不能在主请求失败后重新进入。

存在 active CNY Budget 时，Budget Guard 是选路后的独立保护层，只接受带版本、合法精度且币种为 CNY 的价格；缺价、非 CNY、越界或损坏价格以 `503 gateway.budget_unavailable` 在 Provider 前失败关闭。无 active Budget 规则的请求不因该保护层新增价格依赖。

Budget 准入的 `reserved_cost_cny` 始终保存主备最坏总额且终态不改写。内部 `budget_reservations.outstanding_cost_cny` 只表示还可能被上游计费但没有 Usage 的分项：主成功或 Provider 前本地失败为 0，主上游失败且备用成功为主估算，双上游失败为主备总额。该列是租户内部预算事实，不增加公开 RoutingPolicy 或 OpenAI-compatible 响应字段。Usage 写入与 outstanding 结算必须同事务；写账失败保留原总预留失败关闭。

动态模式没有合格候选时，在 Provider 前返回 `503 gateway.no_route_available`。所需事实读取失败或锁不可用返回 `gateway.routing_snapshot_unavailable`；事实枚举、价格、租户/owner 图损坏返回 `gateway.routing_snapshot_invalid`，Policy 行自身损坏可返回 `gateway.routing_policy_invalid`。动态 Decision/Candidate 原子写失败返回 `gateway.routing_decision_unavailable`；Budget 保护依赖不可审计或不可用返回 `gateway.budget_unavailable`；实际 Key 选择失败返回 `provider_key.unavailable`。这些失败均不得继续调用 Provider。`fixed` 保留原有 best-effort 决策记录和 legacy Provider/Model/Health 错误分类。

## 5. 可观测字段

真实 Alias 请求的调用日志详情可通过 `GET /request-logs/{request_id}` 读取。查询仍按认证租户限定；跨租户 request ID 返回 404。

0035 以前可能为同一租户和 request 留下多条 `routing_policy_id=null` 的历史 Decision。0036 不删除或改写这些历史行，只对 Policy 非空的新 Alias 决策建立 request 部分唯一约束。详情查询优先返回带 Policy 的权威事实；纯历史重复按 `created_at` 和 Decision ID 确定性返回最新一条，再按该 Decision ID 加载候选。因此历史兼容不会遮蔽 PRD-047 强类型候选，也不会扩大跨租户查询。

`routing_decision` 新增或明确：

- `routing_policy_id`、`routing_policy_version`、`routing_mode`：该请求冻结的持久 Policy 身份。
- `price_as_of`、`estimated_input_tokens`、`estimated_output_tokens`、`token_estimate_truncated`：只有确实进入两候选成本比较时有值，其他分支为 `null`。
- `routing_policy_event_id`：成功初始选择的稳定 `rpe_` 事件 ID；失败选择为 `null`。
- `quality_weight_bps`、`cost_weight_bps`、`latency_weight_bps`、`quality_floor_bps`、`minimum_sample_count`：该请求冻结的 Alias 配置；旧三模式均为 `null`。
- `quality_as_of`、`quality_formula_version`：质量快照时点和固定公式 `quality_cost_v1`；旧模式为空。
- `quality_scoring_performed`：只有全部正权重分量已经计算并生成最终总分时为 `true`；冷启动、单候选或依赖回退为 `false`。
- `decision_status`、`decision_reason`、选中的 Model/Provider 和 `error_code`：最终初始选择摘要。

`routing_candidates` 是强类型、不可变的请求时事实，最多两条：

- 身份和排序：`routing_candidate_id`、`candidate_role`、`configured_priority`、`rank`、`status`、`reason_code`。
- 目标：`model_id`、`internal_model_id`、`model_display_name`、`provider_id`、`provider_code`、`provider_name`。
- 资格快照：`health_state`、`health_fresh`、`keypool_state`。
- 成本快照：`price_available`、`price_source`、`price_schema_version`、`currency`、`estimated_cost_amount`。
- 质量身份与版本：`model_quality_score_id`、`quality_statistics_version`、`quality_feedback_version`。
- 质量证据：`quality_evidence_state`、`quality_sample_count`、`quality_success_count`、`quality_error_count`、`manual_quality_bps`、`average_latency_ms`。
- 质量解释：`quality_component_bps`、`cost_component_bps`、`latency_component_bps`、`weighted_score_bps`。

拒绝候选的 `rank=null`、`status=rejected`；未进入某依赖分支的字段使用 `not_evaluated` 或 `null`，不能据此伪称已检查 KeyPool、价格或质量。ready/cold 候选只要已经读取 Score 就会冻结 Score ID、两个版本、计数和 `quality_component_bps`；`quality_scoring_performed=false` 只表示没有完成全部正权重分量，不能据此清空已加载的质量分。零权重分量和未执行的成本/延迟/总分保持 `null`。`estimated_cost_amount` 以 15 位小数字符串、`average_latency_ms` 以 3 位小数字符串返回，避免 JSON float 改变解释。

成功选择提交后，Gateway 以同一 `routing_policy_event_id` 排队投递 `route.policy_selected` / `request_selected`。事件只含 request/decision/Alias/Policy/选中 Model/Provider ID、候选计数、原因码和可信 Project ID；运行时 `actor_id` 固定为空，不把 Virtual Key 冒充人类操作者。队列或接收端失败不反压模型热路径，但安全日志保留 request/trace/event 关联。

配置更新也使用 `route.policy_selected`，但 `trigger=configuration_changed`，只外发 Policy 版本、变更种类和字段名摘要，不外发字段前后值。两类事件都复用现有 Webhook HMAC 信封、租户订阅和投递记录。

无论成功或失败，RequestLog、Usage、Webhook 和结构化日志均不得包含 Provider Key、Bearer/Session Token、Endpoint、Prompt、Response 或上游错误正文；最终 Usage 与成本按实际调用的 Model/Provider 归因，而不是只按 Alias 初始 default 归因。

Provider 管理日志不记录 Endpoint 或 URL hash，避免暴露客户内网拓扑。持久审计只保存规范化完整 URL 的 `base_url_hash` 变化摘要，不保存 host/path/query/fragment 明文。0036 升级会对旧 Provider Audit `before_summary_json/after_summary_json` 不可逆删除 `base_url`，但保留审计行、actor、request/trace 和时间；down 不恢复已清理 URL。

## 6. 模型质量评分 API

### 6.1 查询评分

```http
GET /model-quality-scores?model_id={model_id}&limit=100&offset=0
Authorization: Bearer {admin_or_developer_session_token}
X-Request-ID: {request_id}
```

- 查询只允许 `model_id`、`limit`、`offset`，未知或重复参数返回 `422 model_quality_score.query_invalid`。`tenant_id` 不能出现在查询中。
- `model_id` 为当前租户精确过滤；不存在和跨租户统一返回 `404 model_quality_score.not_found`，不形成资源枚举通道。
- `limit` 为 ASCII 十进制 `1..200`，默认 100；`offset` 为 `0..1000000`，默认 0。
- 响应设置 `Cache-Control: no-store`。每条 Score 只包含模型/Provider 安全目录字段、累计整数计数、派生比率、人工先验和版本，不返回 Endpoint、KeyPool、凭据、Prompt 或 Response。

响应顶层：

```json
{
  "model_quality_scores": [
    {
      "model_quality_score_id": "mqs_...",
      "tenant_id": "tenant_...",
      "model_id": "model_...",
      "internal_model_id": "deepseek-chat",
      "display_name": "DeepSeek Chat",
      "provider_code": "deepseek",
      "provider_name": "DeepSeek",
      "model_status": "active",
      "status": "active",
      "sample_count": 42,
      "success_count": 40,
      "error_count": 2,
      "latency_sample_count": 42,
      "total_latency_ms": 42000,
      "success_rate_bps": 9524,
      "error_rate_bps": 476,
      "average_latency_ms": "1000.000",
      "manual_quality_bps": 8500,
      "effective_quality_bps": 8355,
      "observation_status": "observed",
      "formula_version": "quality_cost_v1",
      "statistics_version": 42,
      "feedback_version": 3,
      "first_observed_at": "2026-07-18T00:00:00Z",
      "last_observed_at": "2026-07-18T01:00:00Z",
      "feedback_updated_by": "user_...",
      "feedback_updated_at": "2026-07-18T01:10:00Z",
      "created_at": "2026-07-18T00:00:00Z",
      "updated_at": "2026-07-18T01:10:00Z"
    }
  ],
  "total": 1,
  "limit": 100,
  "offset": 0,
  "as_of": "2026-07-18T01:11:00Z",
  "request_id": "req_...",
  "trace_id": "trace_..."
}
```

`statistics_version` 只随真实上游样本增加，`feedback_version` 只随人工评分真实变化增加，二者互不制造 CAS 冲突。`success_rate_bps` 使用整数 `ROUND_HALF_UP`，`error_rate_bps` 固定为 `10000-success_rate_bps`。`observation_status` 只表示 `observed/unobserved`，不冒充某个 Alias 的 `ready/cold` 门槛判断。

### 6.2 更新或清除人工评分

```http
PATCH /model-quality-scores
Authorization: Bearer {admin_session_token}
Content-Type: application/json
X-Request-ID: {stable_request_id}

{
  "model_id": "model_...",
  "manual_quality_bps": 8500,
  "expected_feedback_version": 2
}
```

- 请求字段必须精确为上述三项；`manual_quality_bps=null` 表示清除。`tenant_id`、actor、统计字段以及后续逐请求反馈字段 `rating/accepted/tags` 均禁止。
- 正文限制为 1 至 8192 bytes、UTF-8 JSON object、最多 16 层/64 节点；任意层重复 key、控制字符、未知字段、bool 冒充 int 和非法媒体类型均拒绝。
- `manual_quality_bps` 必须为 `null` 或严格整数 `0..10000`；`expected_feedback_version` 必须为正整数且与锁内当前版本相等。
- 同值且版本匹配返回 `changed=false`，不增加版本、不写 Audit/Event/Webhook。真实变化令 `feedback_version+1`，统计版本保持当前值，并在同一事务写 `model_quality_score.updated` 审计和 `model.feedback_recorded` 持久事件。
- 网络结果未知时只有完全相同的 tenant、actor、request ID 和请求指纹可恢复原提交结果；期间统计继续增加不破坏恢复。后续人工评分再次变化后，旧 request ID 返回 request conflict。

成功响应包含 `model_quality_score`、`changed`、`request_id`、`trace_id`。主要错误如下：

| HTTP | code | 场景 |
| --- | --- | --- |
| 403 | `auth.permission_denied` | developer/viewer 尝试 PATCH，或 viewer 尝试 GET。 |
| 404 | `model_quality_score.not_found` | 当前认证租户不存在指定模型/Score；跨租户同样返回。 |
| 409 | `model_quality_score.version_conflict` | 锁内反馈版本与 expected 不一致。 |
| 409 | `model_quality_score.request_conflict` | 同 request ID 绑定不同意图，或旧结果已被后续人工版本取代。 |
| 422 | `model_quality_score.query_invalid` | GET 查询未知、重复或越界。 |
| 422 | `model_quality_score.input_invalid` | PATCH wire、字段集合或评分非法。 |
| 422 | `model_quality_score.version_invalid` | expected version 类型或范围非法。 |
| 503 | `model_quality_score.unavailable`、`model_quality_score.update_failed` | 读取依赖不可用，或评分/审计/事件事务无法安全提交。 |

## 7. quality_cost 计算与 Dry-Run 合同

质量分使用 20 个先验样本，全部计算使用整数或 Decimal：

```text
prior_bps = manual_quality_bps（存在时），否则 5000
quality_component_bps = ROUND_HALF_UP(
  (success_count * 10000 + 20 * prior_bps) / (sample_count + 20)
)

weighted_score_bps = ROUND_HALF_UP(
  (quality_component_bps * quality_weight_bps
   + cost_component_bps * cost_weight_bps
   + latency_component_bps * latency_weight_bps) / 10000
)
```

`manual_quality_bps` 是先验，不覆盖真实成功/错误样本。人工评分存在或样本数达到 Alias 的 `minimum_sample_count` 时证据为 `ready`，否则为 `cold`；ready 且质量分低于 `quality_floor_bps` 的候选先于价格读取被拒绝。两条均 cold 时按配置稳定顺序，一条 ready-pass 与一条 cold 时 ready 优先。只有两条 ready-pass 且所有正权重依赖齐备时执行完整加权；同分按 `configured_priority, model_id` 稳定破局。

`POST /policies/dry-run` 的请求 wire、admin-only RBAC、Prompt 内存生命周期和既有 explanation/audit/`policy.dry_run_executed` 合同保持 PRD-046 不变。路由结果新增以下只读字段：

- route level：`quality_as_of`、`quality_formula_version`、`quality_scoring_performed`、三项权重、质量门槛、最小样本数；
- candidate level：Score ID、统计/反馈版本、证据状态、样本/成功/错误计数、人工评分、平均延迟、三项分量和最终分。

Dry-Run 在同一只读快照读取当前 Score/Policy/按需价格并复用 shared 公式。它不调用 Provider，不修改 Score，不写 RoutingDecision/Candidate/Usage，不发布 `route.policy_selected` 或 `model.feedback_recorded`。坏事实或数据库依赖失败继续映射为 `policy.dry_run_unavailable`，不能伪装成 Gateway 热路径错误。

旧三模式的 route-level 质量配置、公式和时点均为 `null`，`quality_scoring_performed=false`；Candidate 的 `quality_evidence_state=not_evaluated`，其余质量字段为空。`quality_cost` 的通用 rejected 候选也保持 `not_evaluated`。RequestLog 和 Dry-Run 都只展示各自快照，不使用当前 Score 回算已经发生的请求。
