# PRD-046 - 策略模拟与单请求预演

状态：已完成。设计、两轮自审、实现、专项与全量回归、0035、真实三服务/API、结果未知补偿、敏感扫描和三档浏览器验收均已通过。

## 1. 需求来源与顺序确认

- Excel `01_PRD总表` 与 `02_M0-M1核心开发` 均将本条定义为 `PRD-046`、`M1-企业POC`、`P1`、核心层，模块为“策略 Dry-Run”，名称为“策略模拟与单请求预演”。
- README、`docs/prd`、`docs/testing` 与当前运行证据连续完成到 PRD-045；PRD-045 当前验收脚本已由批次 `20260718T121929` 实际运行通过，因此 PRD-046 是按 `M0-W1 -> M0-W2 -> M0.5 -> M1 -> M2 -> M3` 识别出的下一条未完成 PRD。
- 页面为 `PAGE-066 - 策略模拟器`；接口为 `API-220/221/222` 汇总后的唯一业务入口 `POST /policies/dry-run`；数据对象为 `OBJ-065 - PolicyExplanation`；事件为 `EVT-052 - policy.dry_run_executed`。
- `TC-0091` 要求输入 Prompt、模型、项目和数据等级，返回策略决策树、命中规则、动作、路由结果和预算结果，且不产生真实调用成本。
- `TC-0092` 要求不回放真实历史流量，覆盖无权限、异常输入和依赖故障；所有错误使用统一错误码并携带 request_id，不泄露敏感输入。

## 2. 需求理解

### 2.1 交付目标

管理员在策略上线前，用一条当前构造的请求上下文预演租户内 `active` Policy 候选集。预演必须回答：

1. 哪些 Policy 按 `priority, policy_id` 被检查；每条规则是否命中；首个决定性动作是什么。
2. 该动作若进入当前 Gateway 主流程，会继续、拒绝、要求不支持的 mask，还是改路由到指定模型。
3. 在选定 Environment 和 Project 下，请求模型会解析到哪个租户内 Model/Provider；哪些候选因禁用或新鲜 unhealthy 证据被剔除。
4. 把 Prompt 仅作为一条内存中的 `role=user` 文本执行基础 DLP 时，会命中哪些规则及动作。
5. 按当前上海业务日、Usage、未结算 reservation、模型价格和最坏 Fallback 成本估算，tenant/project 预算会放行、告警还是阻断。
6. 当前 active candidate 是否能由 PRD-045 发布器和现有 Gateway 执行；若不能，必须明确显示兼容性原因，不能把 what-if 结果描述成线上精确行为。

### 2.2 为什么增加 `environment_id`

Excel 摘要没有列出 Environment，但现有模型别名、预算规则、Provider 健康与 Gateway 可信上下文均依赖 Environment。静默默认 `dev` 会让路由和预算结果看似完整却不可信，因此请求增加必选 `environment_id`：

- `environment_id` 必须属于认证 tenant 且为 active。
- `project_id` 必须属于同一 tenant 且为 active；Project 当前不是 Environment 子对象，因此两者独立校验。
- `model` 按所选 Environment 先解析 ModelAlias，再兼容租户内 direct internal model。
- 页面必须显式选择 Environment；不自动选择列表第一项。

### 2.3 模拟来源与 PRD-045 兼容性边界

本条默认模拟当前 `active` candidate，而不是已发布 snapshot：用户故事明确是“策略上线前模拟”。同时必须展示当前已发布版本作为对照，但不能用 published snapshot 替代 candidate。

PRD-045 当前发布器明确拒绝：

- active Policy 使用 `mask`：`policy_snapshot.action_unsupported`；
- active Policy 引用 `data_class`：`policy_snapshot.variable_unsupported`。

因此 Dry-Run 允许把管理员输入的 `data_class` 作为 what-if 上下文，并允许解释 `mask` 命中，但响应必须固定包含：

- `policy_source="active_candidate"`；
- `simulation_only=true`；
- `runtime_compatible`；
- `runtime_incompatibility_codes`；
- `gateway_parity="partial"` 与 `coverage_complete=false`；
- `published_snapshot_version`（可以为空）。

兼容性按整个 active candidate 计算，而不是只看最终命中规则。只要候选集中存在 PRD-045 无法发布的 `mask` 或 `data_class`，`runtime_compatible=false`。该字段只表示“候选是否满足当前 PRD-045 发布器/执行器合同”，不表示本次模拟与某个真实 Virtual Key 请求完全等价。由于 Excel 没有提供可信 Virtual Key/Application，且本期不读取 KeyPool 凭据，`gateway_parity` 固定为 `partial`、`coverage_complete=false`。命中 `mask` 时不发明脱敏算法，路由/DLP/预算阶段固定为 `not_evaluated`。

### 2.4 明确非目标

- 不读取或回放 `request_logs`、历史 Prompt、Response、Trace、历史 request_id 或历史 snapshot 流量。
- 不调用 Provider Adapter、KeyPool、客户 Endpoint、OpenRouter 或硅基流动；后两者仍只是竞品/迁移兼容对象，不是默认上游。
- 不模拟真实 Virtual Key、Application、RPM/TPM/并发限流；响应必须列出未覆盖 scope，不能宣称与某个真实 Key 完全一致。
- 不创建 `gateway_request_claims`、request log、Usage、price snapshot、routing decision/attempt、DLP match、预算 evaluation/reservation/block/alert。
- 不实现复杂语义 DLP、历史流量回放、双跑、灰度、MCP 主链路、硬件盒子、信创或复杂数据出境。

## 3. 架构约束映射

- 系统架构 5.1：Dry-Run 属于低频 Control Plane Route/Policy Service，不进入高频模型流量入口。
- 系统架构 5.3：线上数据面只消费已发布快照；本页面的 active candidate 结果不能隐式改变 Gateway。
- 系统架构 9.3：解释需覆盖候选模型、剔除原因、最终选择、预算、DLP 和策略动作。
- 系统架构 14.2/14.3：管理 API 与 Webhook 使用稳定英文 code/type，事件只发送安全摘要。
- 系统架构多租户约束：所有管理 API 在后端做租户鉴权；Trace、Usage、Budget、Webhook 均按 tenant 隔离。
- 所有 API 成功/失败响应和响应头继续携带 `request_id`、`trace_id`；敏感正文不进日志、审计、事件或持久对象。

## 4. 涉及模块

### 4.1 Shared SDK

- `metahub_shared.runtime.policy_dsl`：复用唯一受限 DSL 编译器与 `evaluate_policy_set`，不实现第二套表达式引擎。
- 新增治理预演纯计算结构/函数：规范化路由候选、DLP 规则和预算规则后做确定性选择；函数不接收数据库连接，不写日志，不做网络调用。
- `errors.py`、`error_diagnosis.py`：登记 Dry-Run 统一错误与固定诊断。
- `webhooks.py`：加入 `policy.dry_run_executed` 白名单及精确 payload 校验器。

### 4.2 Control Plane API

- `policies/models.py`：增加严格请求 DTO 和强类型 `PolicyExplanation` 响应 DTO；不得用无约束 `dict[str, Any]` 作为正式合同。
- `policies/routes.py`：增加 admin-only `POST /policies/dry-run`，先做后端认证/限额，再使用有界 JSON 读取器解析正文。
- 新增 `policies/dry_run_service.py`：一致性只读事务、候选重编译、Policy/DLP/Route/Budget 预演、安全摘要入库、审计与事件投递。
- `audit/service.py`：增加 `policy.dry_run_executed -> policy_explanation` 动作/对象和安全摘要字段。
- `console/service.py`：增加 admin-only `policy-simulator` 导航。
- `app.py`：沿用现有 policies router，不新增绕过共享中间件的应用。

### 4.3 Gateway Data Plane

- 不增加 Dry-Run 路由，不让控制台请求进入 Gateway。
- 将现有路由/DLP/预算中可复用的确定性判断收敛为纯计算核心或增加严格 parity 测试；真实 Gateway wrapper 继续负责 claim、日志、Usage、reservation 和治理事件。
- 禁止 Dry-Run 调用 `resolve_model_route*`、`resolve_policy_target_route`、`enforce_dlp_guard`、`enforce_budget_guard`、`authenticate_virtual_key` 或 Adapter/KeyPool，因为这些入口存在数据库/网络副作用。

### 4.4 Console Web

- 新增 `src/lib/policyDryRun.js`：只负责有界请求、AbortSignal、统一错误解析，不缓存请求正文。
- 新增 `src/features/policy-dry-run/renderPolicyDryRunPanel.js`：表单、决策树、命中规则、路由、DLP、预算和兼容性展示。
- `src/app/main.js`：admin-only 页面生命周期、身份 revision、离页/登出/401/降权清理、AbortController。
- `src/styles/main.css`：1280、900、390 三档无横向溢出的工作型布局。

### 4.5 数据库与文档

- `infra/migrations/0035_policy_dry_run.sql` 与 `.down.sql`。
- `control-plane-api/tests/integration/test_policy_dry_run.py`、迁移/错误/Webhook/审计/导航回归。
- shared 与 Console 单元测试、真实 API/浏览器验收脚本。
- 更新 README、目录说明、API 文档和 `docs/testing/PRD-046-tests.md`。

## 5. 数据表和字段设计

### 5.1 `policy_explanations`

本表只保存不可变、安全、可审计摘要；完整规则树、比较节点、预算余额和 Prompt 只存在于当前响应内存中。

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `policy_explanation_id` | TEXT PK，`pexp_` + 32 hex | PolicyExplanation 稳定对象 ID。 |
| `tenant_id` | TEXT NOT NULL | 认证上下文注入；复合 FK/索引的第一列。 |
| `actor_user_id` | TEXT NOT NULL | 实际执行管理员；与 tenant 组成复合 FK。 |
| `request_id` | TEXT NOT NULL | 与 API、审计、事件关联；不由请求体提供。 |
| `trace_id` | TEXT NOT NULL | 共享追踪 ID。 |
| `status` | TEXT CHECK=`completed` | 接口返回的模拟结果已完成；模拟得到 deny/block 仍是 completed。 |
| `version` | INTEGER CHECK=1 | 安全摘要 schema 版本，不等于 Policy 版本。 |
| `policy_source` | TEXT CHECK=`active_candidate` | 防止后续误把候选解释成已发布快照。 |
| `policy_set_fingerprint` | TEXT，64 位小写 SHA-256 | tenant-bound active candidate 安全指纹，不含 DSL 原文。 |
| `published_snapshot_version` | INTEGER NULL，>=1 | 读取时当前 published 版本对照。 |
| `decision` | allow/deny/mask/log_only/route_to | Policy 纯求值决定。 |
| `reason_code` | TEXT NOT NULL | 稳定 Policy 原因码。 |
| `matched_policy_id` | TEXT NULL | 决定性规则 ID；无命中时为空。 |
| `matched_priority` | INTEGER NULL | Excel 要求可见的优先级摘要。 |
| `matched_action` | TEXT NULL | 决定性动作摘要。 |
| `route_status` | selected/unavailable/not_evaluated | 不保存 base_url、auth_type 或凭据。 |
| `dlp_status` | clean/matched/blocked/not_evaluated | 不保存 pattern、命中文本或命中 hash。 |
| `budget_status` | no_rules/within_limit/threshold_reached/exhausted/not_evaluated | 不持久化具体余额。 |
| `would_error_code` | TEXT NULL | 模拟 Gateway 将返回的统一错误码。 |
| `checked_policy_count` | INTEGER，0..200 | 已检查 Policy 数。 |
| `runtime_compatible` | INTEGER CHECK 0/1 | 当前 PRD-045 发布/执行兼容性。 |
| `coverage_complete` | INTEGER CHECK 0/1 | 本次是否覆盖真实 Gateway 全部上下文；本期固定为 0。 |
| `explanation_truncated` | INTEGER CHECK 0/1 | 响应解释是否因上限截断。 |
| `created_at` / `updated_at` | TEXT NOT NULL 且相等 | Excel 对象字段保留；对象不可变，因此时间相同。 |

约束与索引：

- `UNIQUE(tenant_id, policy_explanation_id)`；
- `UNIQUE(tenant_id, actor_user_id, request_id)`，重复 request_id 不返回旧完整结果，固定 409；
- `(tenant_id, created_at DESC)`、`(tenant_id, actor_user_id, created_at DESC)`；
- tenant、actor 使用 tenant-qualified FK；matched Policy 只保存安全定位值，不因后续状态变化改写历史。
- UPDATE/DELETE 触发器固定拒绝普通修改；未来保留期清理由独立合规迁移处理，不提供本 PRD 删除 API。

### 5.2 `policy_events` 扩展

- `event_type` CHECK 增加 `policy.dry_run_executed`。
- `policy_id` 写决定性 matched Policy；无命中为 NULL。
- `policy_events.reason_code` 固定为事件事实码 `policy.dry_run_executed`；模拟引擎的 `policy.rule_matched` 等原因只进入下述 `decision_reason_code`，不能混用事件列语义。
- payload 字段集合必须精确为：
  - `policy_explanation_id`
  - `request_id`
  - `policy_set_fingerprint`
  - `decision`
  - `decision_reason_code`
  - `matched_policy_id`
  - `route_status`
  - `dlp_status`
  - `budget_status`
  - `would_error_code`
  - `checked_policy_count`
  - `runtime_compatible`
  - `coverage_complete`
- 禁止 Prompt、Prompt hash/fingerprint、Prompt 长度、data_class、模型/项目输入、DSL、AST、比较 actual/expected、预算余额、Provider URL、Key/Token。
- 持久 `policy_event_id` 派生稳定 Webhook `event_id`，同租户签名和投递。

### 5.3 `audit_logs` 扩展

- action：`policy.dry_run_executed`；object_type：`policy_explanation`。
- before 固定为空；after 只允许 explanation ID、candidate fingerprint、decision/decision reason、matched Policy/priority/action、三个阶段状态、would_error_code、checked count、runtime compatibility/coverage complete。
- `changed_fields` 使用固定安全字段列表；不序列化请求 DTO 或完整响应。
- 增加部分唯一索引 `(tenant_id, actor_user_id, request_id) WHERE action='policy.dry_run_executed'`，使 explanation/event/audit 三类事实都具备相同的 exactly-once 键。

### 5.4 0035 up/down

Up：

1. 创建 `policy_explanations`、复合索引和不可变触发器。
2. 无损重建 `policy_events`：同时扩展 event_type CHECK 与 event/reason 语义 CHECK，dry-run 分支要求 `reason_code='policy.dry_run_executed'`；保留所有旧行、索引、created/validation 部分唯一约束与 owner/不可变触发器，并新增 dry-run request 部分唯一索引。
3. 无损重建 `audit_logs`，仅扩展 action/object_type CHECK，保留 0034 全部字段、索引和旧事实。
4. 迁移结束执行 foreign key/integrity 测试；任何中间复制数量不一致必须回滚。

Down：

1. 删除 `policy.dry_run_executed` 的 Webhook attempts/deliveries；从 Endpoint 订阅 JSON 中精确移除此事件，不能删除其他订阅。
2. 不直接 DELETE 受不可变触发器保护的 audit/policy event。分别创建 0034 旧结构临时表，以 `action/event_type != 'policy.dry_run_executed'` 结构化过滤复制，核对行数后 drop/rename 并恢复 0034 全部索引和 owner/immutable 触发器；非 PRD-046 行逐字段保真。
3. 删除 explanation 不可变触发器、索引和表。
4. 覆盖 `0034 -> 0035 -> down -> up`、空库全迁移、历史行保真、索引/触发器、FK 与 integrity。

## 6. API 设计

### 6.1 请求读取边界

路由先执行 `require_policy_admin`，再流式读取最多 256 KiB JSON。该上限能够覆盖 20,000 个 Unicode code point 在 UTF-8 或 JSON surrogate escape 下的合法最坏编码，同时仍限制无界正文：

- Content-Type 必须为 `application/json`；
- 超过上限立即停止读取并返回 413；
- JSON 顶层必须是 object，拒绝重复 key、NaN/Infinity 和尾随内容；
- DTO `extra="forbid"`，因此 `tenant_id`、actor、策略列表、AST/hash、历史 request_id、snapshot_version、budget_remaining、Provider/Key 等均被拒绝；
- 框架/领域错误响应不包含 Pydantic 的原始 input。

### 6.2 `POST /policies/dry-run`

请求 DTO：

```json
{
  "prompt": "用于策略预演的当前输入",
  "model": "chat-default",
  "project_id": "prj_...",
  "environment_id": "env_...",
  "data_class": "confidential",
  "max_output_tokens": 512
}
```

字段约束：

| 字段 | 约束 |
| --- | --- |
| `prompt` | strict string，1..20,000 code point；不做 Unicode 归一化，拒绝 NUL 与非必要 C0/C1 控制字符；仅在当前函数栈参与 DLP/长度估算。 |
| `model` | strict string，trim 后 1..128；按当前租户和 Environment 解析。 |
| `project_id` | strict string，1..128；必须属于当前租户且 active。 |
| `environment_id` | strict string，1..128；必须属于当前租户且 active。 |
| `data_class` | strict string，1..64；管理员 what-if 输入，不宣称是 Gateway 可信分类来源。 |
| `max_output_tokens` | 可选 strict integer，1..131072；只用于预算预留估算。 |

成功固定 HTTP 200。Policy deny、DLP block、预算 block、路由 unavailable 或策略超时是“模拟出的业务结果”，放在 `would_error_code`，不能把 Dry-Run API 本身变成 403/429/503。

响应顶层：

```json
{
  "policy_explanation": {
    "policy_explanation_id": "pexp_...",
    "tenant_id": "tenant_default",
    "status": "completed",
    "version": 1,
    "policy_source": "active_candidate",
    "policy_set_fingerprint": "...",
    "published_snapshot_version": 10,
    "simulation_only": true,
    "runtime_compatible": false,
    "runtime_incompatibility_codes": ["policy_snapshot.variable_unsupported"],
    "gateway_parity": "partial",
    "coverage_complete": false,
    "provider_called": false,
    "usage_recorded": false,
    "cost_recorded": false,
    "outcome": "would_allow",
    "would_error_code": null,
    "decision_tree": {"nodes": []},
    "policy": {},
    "route": {},
    "dlp": {},
    "budget": {},
    "coverage": {},
    "input_summary": {
      "prompt_redacted": true,
      "prompt_codepoints": 12,
      "estimated_input_tokens": 3
    },
    "created_at": "2026-07-18T12:00:00Z",
    "updated_at": "2026-07-18T12:00:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

### 6.3 强类型解释对象

`decision_tree.nodes` 使用扁平、可验证的树节点：`node_id`、`parent_id`、`order`、`node_type`、`status`、`reason_code`、可选 rule/policy ID、priority、action、matched。禁止任意 payload 字典；节点数硬上限 256。

`policy`：

- decision/reason/target model；
- `policy_reason`、`policy_checks`、`log_only_matches`；
- 每条显示 Policy/Rule/Action ID、priority、condition_hash、action、matched、match_order；
- comparison 只返回 variable/operator/value_type/expected/matched，不返回 `actual`，管理员已知自己的输入，后端无需回显；
- 复用 shared 的 200 Policy、64 comparison、20 log_only 与统一 deadline 上限。

`route`：

- status、requested model、alias/direct/policy route reason；
- selected model ID、internal model ID、Provider ID/code；
- 有界 candidates 与 rejected reason；
- 可选运行时 fallback 候选及最坏预算是否包含它；
- Chat capability 严格按当前 Gateway 的“先完成配置路由选择、再校验已选模型能力”顺序执行；已选模型不支持 Chat 时使用现有 `gateway.model_not_found` what-if 错误，不能预先改选 fallback，也不能继续 DLP/预算；
- `provider_credential_status="not_evaluated"`，`selected` 只表示租户内模型/Provider/健康配置路由已选定，不保证 KeyPool 中存在可用凭据；
- 禁止 base_url、upstream model name、auth_type、KeyPool、凭据或 Provider 原始健康错误。

`dlp`：

- clean/matched/blocked/not_evaluated；
- 命中规则只返回 rule ID、priority、action；
- match count、scan truncated；
- 禁止 rule name、pattern、命中文本、message index 和命中 hash。

`budget`：

- no_rules/within_limit/threshold_reached/exhausted/not_evaluated；
- estimated input/output tokens、CNY estimate、价格可用性；
- tenant/project 每条适用规则的 rule ID、scope type、spent/reserved/projected/limit、status/action/reason；金额均为固定 Decimal string；
- active block 按当前业务日生效；alert_only 的残留 block 按真实 Guard 语义忽略但不修改数据库；
- `uncovered_scopes=["application","virtual_key"]`，明确本次没有可信 Key/Application 上下文；
- 如果 alias runtime fallback 可用，预算使用主备最坏估算；stream 不在本 PRD 输入中。

`coverage` 明确已模拟 policy、模型/Provider 配置路由、Chat capability、DLP、tenant+project budget；未模拟 Virtual Key/Application budget、RPM/TPM/并发限流、Provider credential/KeyPool 可用性和真实 Provider。`runtime_compatible=true` 与 `coverage_complete=false` 可以同时出现，页面必须分别展示，不能合并成一个“可上线”状态。

### 6.4 决策顺序

与当前 Gateway 保持：

1. 校验 tenant/environment/project/model 权威性；
2. 读取适用 tenant/project 预算余额，供引用 `budget_remaining` 的 Policy 使用；
3. 纯求值 active candidate；
4. deny/context missing/timeout 立即短路；mask 标记 simulation incompatible 并短路；
5. allow/default 使用请求模型，route_to 使用 Policy 目标，执行只读路由选择；
6. 路由不可用短路；
7. 对内存 Prompt 执行只读 DLP；block 短路；
8. 计算 tenant/project 预算；block 形成 would_error；
9. 生成 explanation、安全事件与审计；绝不调用 Provider。

每个未执行阶段都显式返回 `not_evaluated` 和固定 reason，不能用空对象让前端猜测。

## 7. 一致性、纯计算与事务设计

### 7.1 只读模拟事务

使用独立连接：

1. `PRAGMA query_only=ON`；
2. `BEGIN`；
3. 在同一快照中读取 tenant、environment、project、active Policy 图、published head、模型/Provider/健康、DLP、价格、Usage、reservation、budget block/rules；
4. 所有 SQL 都显式包含认证 `tenant_id`，所有 JOIN 使用 tenant-qualified 条件；
5. 纯函数完成编译与决策；
6. `ROLLBACK` 并关闭连接。

数据库层的 query_only 是“无真实成本”保护，不只依赖代码约定。并发修改时，解释只能完整对应事务开始前或后的一个数据库快照，不能混合两套配置。

### 7.2 安全事实写事务

读事务关闭后使用第二连接 `BEGIN IMMEDIATE`：

- 取得写锁后先按 `(tenant_id, actor_user_id, request_id)` 再查 explanation/event/audit；这一步处理两个并发请求都在读事务看到“未存在”的竞争，不能只依赖捕获唯一约束异常；
- 插入一条 `policy_explanations` 安全摘要；
- 插入一条 `policy.dry_run_executed` policy event；
- 插入一条 `policy.dry_run_executed` audit；
- 写入后从数据库权威回读三行，逐字段核对 tenant、actor、request/trace、fingerprint、decision、阶段状态和 object ID，并使用持久行的 ID/created_at/updated_at 构造最终响应；三者任一缺失或不一致全部回滚并返回 `policy.dry_run_unavailable`。
- 三者同事务提交，任一失败全部回滚。已存在分支核对安全事实并在事务外重派稳定事件后返回 `policy.dry_run_request_id_reused`，其他错误返回 `policy.dry_run_unavailable`。

提交后分别尽力投递 `audit.recorded` 和稳定 `policy.dry_run_executed` Webhook。Webhook 失败不能回滚已完成解释，也不能改变 HTTP 200；日志只保留固定 event/object/request/error code。若同 tenant+actor 的 request_id 再次到达，服务先从持久 explanation/event/audit 安全摘要核对事实并重派相同 event_id，再返回 409；它不返回无法重建的完整旧解释。没有客户端重试时的持久后台 outbox 仍属于 PRD-057，不能在本条伪称已交付。

### 7.3 候选指纹

active Policy 每条都从持久 `condition_expr` 重新调用 shared 编译器，并核对持久 AST/hash/variables；规范条目仅含 tenant-bound schema、Policy/Rule/Action ID、Policy version/priority、condition_hash、action type、target model ID。排序后 canonical JSON 做 SHA-256：

- 不含名称、描述、DSL/AST 正文、Prompt 或输入上下文；
- 同一 tenant 同一候选稳定；
- tenant relabel、Policy 版本/优先级/动作/条件/目标变化均改变指纹；
- 图不完整或持久 hash 不一致按 `policy.dry_run_unavailable` 失败关闭。

## 8. 安全与权限约束

### 8.1 RBAC

- 当前 RBAC 没有独立 security 角色；Excel 的“管理员/安全”在本期映射为后端 admin-only `require_policy_admin`。
- developer/viewer 手写请求固定 403 `auth.permission_denied`；匿名/坏 Token 固定 401。
- 前端隐藏导航和按钮只是体验层，测试必须直接攻击后端入口。

### 8.2 租户隔离与防枚举

- 请求体不接受 tenant；tenant 只来自 Session/Bearer AuthContext。
- Environment、Project、Model、Policy target、DLP、预算、Usage、reservation、解释、事件、审计均按 tenant 查询或复合关联。
- 跨租户 ID 与不存在/不可用上下文统一 `policy.dry_run_context_not_found`，不泄露对象存在性。
- 响应中的 `tenant_id` 仅回显已认证 tenant；不返回其他租户候选或 rejected 细节。

### 8.3 Prompt 与敏感信息

Prompt 仅存在于：HTTPS 请求正文、后端当前函数栈、DLP 正则匹配和字符计数。它不得进入：

- API 响应、统一错误、异常 message、日志 extra、审计、policy event、Webhook；
- `policy_explanations` 或任何业务表，包括 Prompt hash/fingerprint；
- URL/query/history、localStorage、sessionStorage、IndexedDB、analytics、console；
- 页面验收截图和浏览器持久状态。

前端在请求 settle 后立即清空 textarea 和局部 payload 引用；离页、登出、401、身份/角色变化、Abort 时再次清空。后端不返回所谓“脱敏预览”，只返回长度和 `prompt_redacted=true`。

Provider Key、Virtual Key、Token、Provider URL、DSL、AST、DLP pattern/命中文本同样不进入日志/事件/响应非授权字段。

### 8.4 资源和滥用上限

- body 256 KiB、Prompt 20,000 字符、Policy 200、DLP rule 100、route candidate 2、comparison 64、tree node 256、budget rule 2（tenant/project 各一条）。
- 复用 `METAHUB_POLICY_EVALUATION_TIMEOUT_MS` 1..100 ms；超时产生 completed explanation + would-error，而非继续路由。
- 增加进程内有界 limiter：每 actor 30 RPM、每 tenant 120 RPM，状态不含 Prompt，周期清理且 key 数有硬上限。它是本地 CPU 防护，不宣称替代未来分布式限流。
- 429 也必须携带 request_id/trace_id，且不写 dry-run 成功事件。

## 9. 统一错误码

| code | HTTP | retryable | 场景 |
| --- | ---: | --- | --- |
| `policy.dry_run_payload_invalid` | 422 | 否 | JSON/字段/类型/控制字符/范围/未知字段非法；不回显 input。 |
| `policy.dry_run_payload_too_large` | 413 | 否 | body 超过 256 KiB。 |
| `policy.dry_run_context_not_found` | 404 | 否 | Environment/Project/Model 不属于当前 tenant、不可用或不存在；防枚举同码。 |
| `policy.dry_run_request_id_reused` | 409 | 否 | 同 tenant+actor 重复完成同一 request_id；不返回旧完整解释。 |
| `policy.dry_run_rate_limited` | 429 | 是 | actor/tenant CPU 防护限额。 |
| `policy.dry_run_unavailable` | 503 | 是 | Policy 图、数据库、一致性读取或安全事实事务不可用。 |

Policy/DLP/Budget/Route 模拟出来的错误继续使用既有 Gateway code 放在 `would_error_code`，例如 `gateway.policy_denied`、`gateway.policy_context_missing`、`gateway.policy_evaluation_timeout`、`gateway.policy_action_unsupported`、`gateway.policy_route_unavailable`、`gateway.dlp_blocked`、`gateway.dlp_unavailable`、`gateway.budget_exhausted`、`gateway.budget_unavailable`。

所有 HTTP 错误和成功响应均由共享中间件添加 body/header request_id、trace_id、`Cache-Control: no-store`。

## 10. 前端页面和交互设计

### 10.1 页面结构

- admin-only 导航“策略模拟器”，直接进入可用工具，不做营销/说明页。
- 顶部紧凑表单：Environment select、Project select、Model input/select、数据等级、最大输出 Token、Prompt textarea；运行和清空为明确命令。
- 结果区为未嵌套卡片的全宽分区：结果状态、发布兼容性、决策树、规则表、路由、DLP、预算。
- 规则表显示 ID、优先级、动作、命中、顺序；移动端转为有界定义列表，不横向滚动。
- 路由候选用状态/原因展示，不显示 Endpoint URL 或认证类型；预算金额右对齐且保留固定小数。
- 1280 桌面双列表单/结果摘要，900 单列，390 全单列；所有固定控件设置稳定尺寸，动态结果不能挤压工具栏。

### 10.2 状态机

- `idle -> loading_context -> ready -> submitting -> success/error`；每次身份 revision 或页面 revision 变化使旧响应失效。
- 切换 Environment 时清空 Project/Model 选择和旧结果；不能保留跨环境旧候选。
- submit 时禁用重复提交并创建 AbortController；请求 settle 后立刻清空 Prompt，无论成功或失败。
- 401 走统一登出；403 回退 overview；离页/降权/登出 abort 请求并清空 Prompt、结果和控制器。
- 所有用户/API 文本使用 escape/textContent；不使用 `innerHTML` 拼接未转义值。

### 10.3 可访问与隐私验收

- label/for、fieldset/legend、表格表头、`aria-live` 状态、焦点返回和键盘操作完整。
- 不把使用说明、快捷键或设计描述作为可见文案；只展示业务字段、状态和错误。
- 浏览器验收使用唯一 Prompt sentinel，提交后断言 DOM、URL、storage、console、response、event/audit/webhook 和截图均不含 sentinel。

## 11. 测试用例

### 11.1 Shared 纯计算

1. Policy 稳定 priority+ID 顺序、log_only 后继续、默认 allow、deny/mask/route_to、context missing、timeout fail closed。
2. comparison 响应删除 actual，规则 ID/priority/action/matched 保真；200/64/20/256 上限与截断标志。
3. route alias default/fallback、direct、policy target、disabled model/provider、新鲜 unhealthy、过期健康 Fail Open、跨 tenant 不进入候选。
4. DLP clean/log_only/block、关键词/安全正则、20,000 字符边界；结果不含 pattern/命中文本/hash。
5. token 估算、CNY/非 CNY/缺价格、runtime fallback 双候选、Decimal 精度。
6. budget 无规则、tenant/project、最严结果、上海业务日、Usage+outstanding reservation、active block、alert_only 残留 block。

### 11.2 API 主流程 `TC-0091`

1. admin 输入完整上下文，返回 200、PolicyExplanation、request_id/trace_id/no-store。
2. active candidate 规则树显示 rule ID、priority、action；candidate fingerprint 稳定且 published version 只作对照。
3. allow、deny、mask、route_to、default allow、log_only 分别得到准确短路路径。
4. data_class/mask 可模拟但 `runtime_compatible=false` 并返回 PRD-045 兼容码。
5. 路由、DLP、预算结果与同一数据库快照一致；Provider fake 调用数严格为 0。
6. explanation/policy event/audit 的 object/request/fingerprint/decision 一致；Webhook 签名、租户和精确 payload 通过。

### 11.3 异常/权限/安全 `TC-0092`

1. 匿名/坏 Token 401，developer/viewer 403，admin 200；禁用用户、撤销 Session 拒绝。
2. 跨租户 Environment/Project/Model/Policy target 与随机不存在 ID 返回同码同结构。
3. bool/float/list/object 类型混淆、未知 tenant/预算/策略字段、重复 JSON key、NUL、控制字符、各长度边界、256 KiB body；20,000 个中文/astral code point 的合法编码不会被 body 上限误拒绝。
4. candidate 图不完整、hash/AST/variables 不一致、损坏事件/audit、数据库锁/commit 失败统一 503，事务零残留。
5. Policy deny 后 route/DLP/budget 均 not_evaluated；route unavailable 后 DLP/budget 不执行；DLP block 后 budget 不执行。
6. Webhook 无订阅、有订阅、签名、失败重试、敏感嵌套字段整体拒绝；投递故障不改变已完成响应。
7. 两个真实 SQLite 连接并发复用 request_id：写锁内复查后只有一组 explanation/event/audit；另一请求重派相同 event_id 后 409，不依赖唯一约束异常文本。
8. 模拟 commit 后、首次 dispatch 前退出，再用同 request_id 重试；安全事实不重复且稳定 dry-run/audit event 均可重派。
9. actor/tenant limiter、并发身份切换、离页 Abort、旧响应不可回写。

### 11.4 “不产生真实调用成本”强断言

对 allow、deny、DLP block、budget block、route unavailable 五类结果，在 Dry-Run 前后逐表比对以下均零增量：

- `gateway_request_claims`
- `request_logs`
- `usage_ledger`
- `price_snapshots`
- `routing_decisions`
- `routing_attempts`
- `dlp_matches`
- `budget_evaluations`
- `budget_reservations`
- `budget_blocks`
- `budget_block_events`
- `budget_alert_events`
- Provider/Provider credential/Virtual Key 的 last_used/health/error 字段

允许增量仅为 `policy_explanations`、`policy_events`、`audit_logs`，以及有订阅时的 `webhook_deliveries/attempts` 和对应 Endpoint 的投递状态时间戳。Provider mock 和 Adapter 方法调用计数必须为 0；运行验收显式关闭 Provider health 与 Usage export 后台调度，避免无关周期任务污染零增量证据。

### 11.5 迁移、回归和运行验收

- 0035 正向、down、up；空库 35 条迁移；0034 历史 policy/audit/webhook/request/usage 行逐字段保真；FK/integrity/trigger/index。
- PRD-044 DSL、PRD-045 snapshot、预算、DLP、路由、Webhook、审计、导航、错误目录专项回归。
- Python 根级、Node 示例、Console 全量、Console lint、中文注释检查、compile/node-check。
- 受控数据库副本应用 0035，启动 Control/Gateway/Console/fake upstream，真实 API 验证主/异常/RBAC/租户/零成本/清理。
- 真实 Chrome 1280x720、900x800、390x844：表单、决策树、兼容性、权限、无溢出、Prompt sentinel 不出现在截图/DOM/storage/console。
- 成功批次日志扫描密码、Token、Provider/Virtual Key、Authorization/Bearer、Prompt sentinel、DSL/AST、Traceback、真实 ERROR 与 5xx。

## 12. 代码修改计划

1. 先补 shared 纯预演结构、错误码与 Webhook 精确合同及单元测试。
2. 编写 0035 up/down 与迁移测试，确保旧数据保真后再接服务。
3. 增加严格 DTO、有界 JSON 读取、admin 路由和进程内限额。
4. 实现 `dry_run_service` 的 query-only 一致性读取、候选重编译、四阶段短路与强类型响应。
5. 实现 explanation/event/audit 同事务及提交后稳定 Webhook；补故障注入。
6. 增加导航、前端 API client、页面状态机、Prompt 生命周期清理和响应式样式。
7. 完成专项、迁移、权限、租户、零成本和前端测试。
8. 更新 README、目录说明、API/开发文档和测试记录。
9. 执行全量回归、运行库副本迁移、真实 API、浏览器三档、日志/数据库敏感扫描与资源清理。

## 13. 第一轮设计自审

状态：通过。第一轮由主 Agent 结合两项前置独立安全/代码调查，按 Excel、架构、PRD-044/045 和当前 Gateway 热路径逐项审查；初审结论为“不通过”，完成下列修订后复核通过：

1. 原 32 KiB body 无法容纳 20,000 个中文或 surrogate escape 字符；改为 256 KiB wire 上限，同时保留 20,000 code point 业务上限和有界流式读取。
2. 原 `runtime_compatible` 容易被误解为完整线上等价；拆分为 PRD-045 发布兼容性与 `gateway_parity/coverage_complete`，本期明确缺少 Virtual Key/Application/限流/credential 上下文。
3. 原 route `selected` 没有充分说明 KeyPool 未检查；增加 `provider_credential_status=not_evaluated`、Chat capability 语义及页面禁止把配置路由描述成可调用保证。
4. 原提交后 Webhook 存在进程退出窗口且重复 request_id 只返回冲突；增加从已持久 explanation/event/audit 核对并重派稳定 event_id 后再 409 的恢复路径，同时诚实保留 PRD-057 后台 outbox 边界。
5. 再次确认所有读取只使用 query-only 同一连接，禁止调用带写副作用的 route/DLP/budget/auth/adapter 入口；安全事实使用第二写事务。
6. 再次确认 Prompt 不持久化也不做低熵 hash，comparison 删除 actual，事件使用精确字段集合，admin-only 与 tenant-qualified 查询不依赖前端。

## 14. 第二轮设计自审

状态：通过。第二轮聚焦 0035 schema、SQLite 并发、事实一致性、回滚和可证明测试；初审结论为“不通过”，完成下列修订后复核通过：

1. `policy_events` 不只有 event_type CHECK，还存在 event/reason 语义 CHECK；新增 dry-run 第三分支，事件列 reason 固定为事件码，模拟决定原因改名为 payload `decision_reason_code`。
2. 原 down 描述会直接删除 immutable event/audit；改为 0034 旧结构临时表过滤复制，恢复全部旧索引/触发器并核对非 PRD-046 行逐字段保真。
3. explanation 与 event 有唯一键但 audit 没有；增加 audit dry-run 部分唯一索引，使三类事实共享 tenant+actor+request exactly-once 键。
4. 两个并发请求可能都在 read snapshot 看不到旧事实；明确 `BEGIN IMMEDIATE` 后再次查询，再决定写入或冲突恢复，不能依赖 SQLite 异常字符串分支。
5. 完整响应来自内存而数据库只存摘要，原设计缺少提交前一致性核验；增加三行权威回读，并用持久 ID/时间构造响应。
6. 零成本测试允许 Webhook delivery 却未说明 Endpoint 投递状态字段会更新；补允许项，同时要求验收关闭 Provider health/Usage export 周期任务，避免假阳性。
7. 增加 commit 后 dispatch 前退出、稳定事件重派、两个真实连接竞争、旧 schema 全字段/索引/触发器签名对比测试。

两轮设计自审均已通过；从此处开始允许编码，但实现若改变上述事务、隐私或覆盖边界，必须回到设计复审。

## 15. 实现期交叉审查与设计回写

状态：通过。编码完成后的主 Agent 与独立 Agent 交叉审查没有改变 PRD 范围，但发现若干需要把实现口径明确回写到设计的边界；修订如下：

1. **Chat capability 顺序**：路由纯函数先按真实 Gateway 选择 primary，再检查已选模型 Chat capability。primary 不支持 Chat 时不得因 fallback 支持而预先改选，否则会把真实请求的 `gateway.model_not_found` 错误模拟成成功。
2. **route/DLP 状态与 outcome**：配置路由已经选中但 Chat 不支持时，route 仍为 `selected` 并携带 `would_error_code`，DLP/预算为 `not_evaluated`；Policy context missing/timeout/action unsupported 属于 `would_error`，只有明确 deny 才是 `would_deny`，DLP/预算阻断才是 `would_block`。
3. **崩溃恢复前置**：重复 request_id 的三事实恢复必须发生在正文读取、CPU 限流和当前配置读取之前。这样首次事务已提交但进程在 dispatch 前退出时，重试仍可按旧事实重派稳定事件，并且配置损坏或大正文不会遮蔽 409。
4. **whole-candidate 发布兼容性**：`runtime_compatible` 必须检查全部 active candidate，而不只检查本次命中规则；任一 `mask`、`data_class` 或无效 `route_to` target 都应标记为不可发布。
5. **Console 正式 DTO 与树层级**：页面只消费正式强类型 DTO；决策树使用单一 `policy_set` 根节点，全部 `policy_rule` 和阶段节点都是该根的有序子节点；规则之后严格保持 route -> DLP -> Budget 顺序，未执行阶段仍显示 `not_evaluated`，不能误画成已执行结果。
6. **Console 断点与焦点**：1280、900、390 三档均使用稳定网格约束且无横向溢出；提交成功、统一错误和 Abort 后都恢复到结果/错误标题，切换角色或离开页面时清除 Prompt、结果和内存状态。
7. **输入失败关闭**：标准 JSON 的重复 key、NaN/Infinity、未知字段、非法 UTF-8、非 object、超限正文及解析器深度 `RecursionError` 都统一映射为 `policy.dry_run_payload_invalid` 或体积专用错误，不允许深层 JSON 逃逸为 500。

上述修订已经纳入专项测试和最终运行验收计划；它们细化既有安全、兼容性和交互合同，不扩展到真实 Provider、Virtual Key/Application 预算、限流或 KeyPool 模拟。

## 16. 当前实现与验收进度

状态：通过。实现、自动化、迁移、真实 API、停止后数据库/日志复扫和真实浏览器门禁均已关闭，PRD-046 可以完成封账。

### 16.1 已实现边界

- shared 新增 `metahub_shared.runtime.policy_preview` 纯计算合同，以不可变输入执行路由、DLP、token/成本和预算预演；控制面 `POST /policies/dry-run` 复用唯一 Policy DSL 求值器并由 `require_policy_admin` 执行后端权限校验。
- API 不接受客户端 `tenant_id`。正文以 256 KiB 上限流式读取，拒绝重复 JSON key、NaN/Infinity、非法 UTF-8、未知字段、非 object、深层递归和超限输入；actor 30 RPM、tenant 120 RPM 的有界 limiter 只承担本地 CPU 防护。
- 全部配置和业务事实读取在同一 tenant-qualified query-only 连接完成；实现不调用 Provider Adapter、KeyPool、客户 Endpoint 或 Gateway 写入口。deny、route unavailable、DLP block、budget block 均返回 HTTP 200 模拟结论，协议/权限/依赖错误使用统一错误码及 request_id/trace_id。
- 第二连接以原子事务持久化 explanation/event/audit 三类安全事实，提交后投递稳定 `policy.dry_run_executed` Webhook。重复 `tenant + actor + request_id` 在读取正文、限流和当前配置前恢复旧三事实、核对一致性并重派同一事件，然后返回 409。
- Prompt 不持久化、不 hash、不写日志/事件/审计/Webhook、不在响应回显；Console 在 settle、Abort、离页、登出和身份/角色变化时清理 Prompt、结果和旧异步状态。
- 0035 新增 `policy_explanations`，扩展 Policy event/audit 合同、部分唯一索引和不可变触发器；down migration 删除 PRD-046 事实并恢复 0034 的表、索引和触发器，保留非 PRD-046 历史。
- admin-only `policy-simulator` 已实现兼容性、决策树、规则、路由、DLP 和预算视图；developer/viewer 直达回退 overview，不渲染入口或发送 Dry-Run 请求，但授权结论始终以后端 403 为准。

### 16.2 已通过证据

- Policy Preview + Dry-Run 主专项：`20/20 passed`；Preview + 合同交叉复跑：`7/7 passed`；既有 Policy：`17/17 passed`；Webhook：`8/8 passed`；全迁移：`23/23 passed`。
- Python 根级无并发第二轮全量收集并通过 `459/459`；Node 示例 `33 passed`；Console `200 passed`；Console lint `103 files passed`。首轮单个 50 ms 调度超时已由单项复跑和随后完整第二轮关闭，不计入最终结果。
- `scripts/ci/check-comments.py` 返回 `comment coverage ok`；PRD-046 相关 Python compile、JavaScript/MJS 语法门禁和目标 Ruff 通过。
- 真实 Chrome 最终成功批次为 `20260718T161021`，覆盖 1280x720、900x800、390x844，表单列数为 `2/1/1`。三档均无根横向溢出、关键元素越界、文本裁切或同级重叠。
- 浏览器中 admin 真正提交 Dry-Run 并恢复焦点；Prompt 提交后立即清空，响应、DOM、URL、storage、console 和遮罩截图均不含 sentinel。developer/viewer 直达回退且无 Dry-Run 请求，后端均为 403；14 组运行事实零增量，Provider 未调用，三类安全事实各 1 行。

完整命令、证据口径、截图 SHA-256 和边界说明见 `docs/testing/PRD-046-tests.md`。

### 16.3 最终运行验收

- 当前 runtime 脚本 SHA-256 为 `171803A28ECD05DCE0189635025E4EAED35EB9724FD6D2374492096C5D7692FE`。最终批次 `20260718T101126` 退出码 0、`status=passed`，Gateway/Control/Console 均为 HTTP 200；输出 SHA-256 为 `5CDBE004C08C003C07A214096A260F9229E4337FEA82DE7484F14556B78921DF`，空 stderr SHA-256 为 `E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855`。
- allow、deny、route unavailable、DLP block、budget block、Webhook 失败不改主响应均通过；匿名/developer/viewer 为 401/403/403，Provider 回环调用为 0，每阶段 explanation/event/audit 各 1 行，重复 request_id 为稳定 409。
- 验收器为所有 mutation 预登记唯一 intent。有限超时后先 settle，再以唯一 User-Agent 基线差集和 `BEGIN IMMEDIATE` 撤销管理员 Session 形成认证围栏，围栏后再次 settle；最终对全部资源的 captured/resolved ID fail closed coalesce，再做数据库收敛和活动残留 0 断言。迟到提交、User ID 冲突、管理员登录响应丢失及失败路径隐私扫描均经故障注入和独立复审关闭。
- 最终 cleanup `errors=[]`、精确回查 11 个资源、`active_intent_residue=0`、`final_reconcile_rows=0`；2 个临时用户和 2 个 Endpoint 已清理，原 5 个 Policy 与 1 个 Budget 恢复。
- 最终证据库 `runtime-final-current-0035-20260718T101025.db` 为 `3,620,864 bytes`，SHA-256 `2EEB0094DC31B30464982E1689FB2E554BB6F5825BCB89C0F812647BB2C8BDF1`；服务停止并 checkpoint 后为 35/0035、完整性 `ok`、外键违规 0、11 个临时资源 disabled、3 个临时 Session revoked、活动残留 0、原有治理状态差异 0。
- 运行期 5 个指定制品和停止后最终 10 份日志对 Prompt/hash、密码、Token、Authorization/Bearer、Provider/Virtual Key、治理正文、Traceback、ERROR 与 HTTP 5xx 的命中均为 0；8000、8001、5173 最终无监听。

至此本设计规定的需求、边界、权限、租户隔离、统一错误/request_id、迁移、主/异常/权限测试、隐私和运行验收均有直接证据。下一条 PRD-047 尚未开始设计或实现。
