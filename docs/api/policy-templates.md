# 行业/场景策略模板 API

本文档冻结 PRD-068 PAGE-095 的管理面合同。模板库只提供五条随迁移部署的只读蓝图，
不提供在线市场、上传脚本、自定义模板 CRUD 或跨租户共享。模板应用会创建当前 tenant 的
Policy 并发布新的不可变 PolicySnapshot；Gateway 只消费已发布快照，不在请求热路径查询模板表。

## 1. 认证、权限与租户边界

- `policy_template.read`：读取五模板目录、同租户绑定选项和精确影响预览。
- `policy_template.apply`：提交一次服务端固定模板应用；它不隐式授予原始 Policy、Routing 或 Budget CRUD。
- 高安全、只用国产模型、客服脱敏还要求 `policy.write`。
- 预算防爆还要求 `billing.budget.write`。
- 低成本要求 `billing.budget.write` 或 `routing.write` 至少一项。
- 模板补偿回滚复用 `POST /policy-snapshots/{id}/rollback`，仍要求 `policy.publish`。
- 路由依赖和写事务内都会读取当前数据库权限，角色名和前端按钮不能补权。
- `tenant_id`、actor、生成资源 ID、排序、价格版本、预算余额和 Snapshot 版本均由服务端恢复或计算，
  请求不能覆盖。不存在和跨 tenant 的模板统一返回 `policy_template.not_found`；跨 tenant、禁用或
  不存在的绑定统一返回 `policy_template.binding_invalid`。
- 成功与错误响应均携带 `request_id/trace_id`，模板响应设置 `Cache-Control: no-store`。

## 2. 固定模板目录

| template_code | 必填绑定 | 生成动作 | 主要前置条件 |
| --- | --- | --- | --- |
| `low_cost_mode` | `project_id` | `route_to` | active 模型、Provider、KeyPool、能力和有效 CNY 价格 |
| `high_security_mode` | `project_id`, `model_alias_id` | `deny` | Alias、环境、默认/备用模型和 Provider 均 active 且同 tenant |
| `domestic_models_only` | `project_id` | `deny` | 每个 active 环境都有全国产 Provider 覆盖的同名 Alias |
| `budget_burst_guard` | `project_id`, `threshold_cny` | `deny` | Project 存在 active Virtual Key 和可适用预算事实 |
| `customer_service_redaction` | `project_id` | `mask` | Control 部署门禁和 Gateway `policy-mask-v1` readiness 均启用 |

国产 Provider 的权威集合是智谱、通义、文心、火山、混元和 DeepSeek 对应 adapter 类型。
OpenRouter、硅基流动、客户本地 Endpoint 和其他迁移平台不属于国产覆盖集合。

## 3. GET /policy-templates

需要 `policy_template.read`，且不接受正文。

无 query 时返回当前 tenant 固定五条目录和安全绑定选项：

```http
GET /policy-templates
Authorization: Bearer <session-token>
```

精确预览只接受以下 exact-set query：

```http
GET /policy-templates?template_id=ptpl_high_security_mode&project_id=prj_example&model_alias_id=mal_example
```

允许字段只有 `template_id`、`project_id`、`model_alias_id`、`threshold_cny`。未知字段、重复字段、
缺少模板却夹带绑定、模板不允许的额外绑定以及 GET 正文均返回 `policy_template.query_invalid`。

目录响应固定包含五条 `PolicyTemplateInfo`。精确预览的目标条目额外包含：

```json
{
  "compatibility": {
    "status": "ready",
    "reason_codes": [],
    "warning_codes": [],
    "can_apply": true,
    "requires_permissions": ["billing.budget.write", "routing.write"]
  },
  "impact": {
    "project_id": "prj_example",
    "generated_resource_types": ["policy", "policy_snapshot"],
    "evaluation_order": [
      {
        "policy_id": "pol_generated",
        "priority": 1000,
        "action_type": "route_to",
        "generated": true,
        "shadowing": "none"
      }
    ],
    "before_policy_count": 0,
    "after_policy_count": 1,
    "expected_head_version": 7,
    "rollback_target_snapshot_id": "psnap_previous"
  },
  "preview_nonce": "<64 lowercase hex>",
  "preview_as_of": "2026-08-03T02:00:00Z",
  "impact_hash": "<64 lowercase hex>",
  "before_candidate_hash": "<64 lowercase hex>",
  "after_candidate_hash": "<64 lowercase hex>",
  "expected_head_version": 7
}
```

只有精确预览响应返回两条强前置 Header：

- `ETag: "<current_snapshot_version>"`
- `X-Policy-Candidate-ETag: "<before_candidate_hash>"`

`preview_nonce` 是 tenant-bound 规划盐，不是授权凭证。应用时仍会在写锁内重读权限和全部依赖。

## 4. POST /policy-templates/{policy_template_id}/apply

需要 `policy_template.apply` 和模板类别对应的复合权限。接口不接受 query，正文必须是
`application/json`，并同时携带精确预览返回的两条强 CAS Header：

```http
POST /policy-templates/ptpl_low_cost_mode/apply
Authorization: Bearer <session-token>
Content-Type: application/json
If-Match: "7"
If-Policy-Candidate-Match: "<before_candidate_hash>"
X-Request-ID: req_example

{
  "template_version": 1,
  "preview_nonce": "<64 lowercase hex>",
  "preview_as_of": "2026-08-03T02:00:00Z",
  "impact_hash": "<64 lowercase hex>",
  "bindings": {"project_id": "prj_example"},
  "reason": "为项目启用低成本策略"
}
```

正文上限为 32 KiB、JSON 深度 8、节点 128。仅接受严格 UTF-8 JSON object；任意层重复 key、
NaN/Infinity、未知字段、非规范或重复 `Content-Length`、声明与实际长度不一致均失败关闭。

首次成功返回 `201`。同一 tenant、actor、`X-Request-ID` 和 canonical intent 的网络重放返回原持久事实，
HTTP `200` 且 `idempotent_replay=true`；不会重复创建 Policy、Snapshot、审计或事件。同 request ID
不同意图返回 `policy_template.request_conflict`，已消费 nonce 被其他请求复用返回
`policy_template.nonce_conflict`。

成功响应聚合以下安全事实：

```json
{
  "application": {
    "application_id": "ptapp_example",
    "policy_template_id": "ptpl_low_cost_mode",
    "template_code": "low_cost_mode",
    "template_version": 1,
    "status": "applied",
    "generated_policy_id": "pol_example",
    "previous_snapshot_version": 7,
    "result_snapshot_version": 8,
    "impact_hash": "<64 lowercase hex>"
  },
  "generated_policy": {},
  "policy_snapshot": {},
  "rollback_target_snapshot_id": "psnap_previous",
  "idempotent_replay": false,
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

## 5. 原子性、CAS 与回滚

- apply 在单一 `BEGIN IMMEDIATE` 中复核实时权限、template version、Snapshot head、active candidate、
  绑定 owner、价格、KeyPool、Provider、Alias、预算和 mask readiness。
- `If-Match` 保护当前 Snapshot head；`If-Policy-Candidate-Match` 保护预览前候选图；`impact_hash`
  绑定 tenant、模板版本、绑定、完整求值顺序、依赖事实和派生资源。
- 成功时原子创建 application、application items、Policy/Rule/Action、不可变 Snapshot、三类 AuditLog、
  Policy/Snapshot/Template source event。任一关键写入失败整笔回滚。
- 提交后 Policy、Snapshot、Template、Audit 和 Webhook 派发彼此隔离；失败可由持久 source 幂等恢复，
  不反转已经提交的治理事实。
- 普通 `POST /policy-snapshots/{id}/rollback` 遇到未回滚模板 application 时走模板感知补偿：禁用本次
  generated Policy、重新发布不含该项的新单调 Snapshot，并把 application 标记为 `rolled_back`。
  若当前图、owner、版本或依赖不能证明只撤销该模板，返回 `policy_template.rollback_conflict`。
- 后续普通发布、模板应用和补偿回滚都会复核所有 active template application 的持续不变量，阻止
  其他 Policy 绕过已经应用的安全/预算/脱敏约束。

## 6. Gateway mask 合同

客服脱敏生成的 `mask` 是非终止修饰动作。Shared evaluator 继续扫描直到首个
`allow/deny/route_to`，Gateway 只在最终允许继续时、路由与 Provider 前执行一次 copy-on-write 脱敏。

部署侧必须显式设置 `METAHUB_POLICY_MASK_RUNTIME_ENABLED=true`，并让 Control 与 Gateway 读取同一值。
默认值保持 `false`，避免旧版本 Gateway 被误判为具备脱敏能力。Gateway 仅在开关启用且本地执行器自检
通过后声明 `policy-mask-v1` readiness；否则客服脱敏模板保持 incompatible，apply 在写入任何治理事实前
失败关闭。该开关是非敏感能力门禁，不得用它承载 Token、Provider Key、Prompt 或客户配置。

支持 Chat 用户文本和文本型 Embeddings input，固定替换中国手机号、18 位身份证号和电子邮箱。
不修改 system/assistant、URL、图片、音频、文件或工具结果；不支持或超限的正文整体失败关闭，
不得把部分脱敏后的请求或剩余原文发送给 Provider。日志只记录安全的 mask 是否应用和稳定 Policy ID，
不得记录 Prompt、命中片段、位置、正文 hash 或完整替换结果。

## 7. 错误合同

| HTTP | code | 场景 |
| ---: | --- | --- |
| 401 | `auth.token_missing/invalid/expired` | Session 缺失、无效或过期 |
| 403 | `auth.permission_denied` | 缺少 read/apply、类别复合权限或锁内权限被撤销 |
| 404 | `policy_template.not_found` | 模板不存在或跨 tenant |
| 409 | `policy_template.not_applicable` | 当前依赖不能安全应用模板 |
| 409 | `policy_template.version_conflict` | 模板版本变化 |
| 409 | `policy_template.impact_conflict` | head、候选图或依赖事实相对预览漂移 |
| 409 | `policy_template.request_conflict` | 同 request ID 对应不同意图 |
| 409 | `policy_template.nonce_conflict` | preview nonce 已被其他请求消费 |
| 409 | `policy_template.application_conflict` | Project 已有未回滚模板 application |
| 409 | `policy_template.rollback_conflict` | 无法证明补偿回滚只撤销目标模板 |
| 413 | `policy_template.payload_too_large` | 声明或实际正文超过 32 KiB |
| 415 | `policy_template.media_type_unsupported` | apply 不是 `application/json` |
| 422 | `policy_template.query_invalid` | GET query 或正文非法 |
| 422 | `policy_template.payload_invalid` | POST wire、字段、Header 或严格类型非法 |
| 422 | `policy_template.binding_invalid` | 绑定不存在、禁用、跨 tenant 或 exact-set 不匹配 |
| 428 | `policy_template.precondition_required` | 缺少任一强 CAS Header |
| 503 | `policy_template.unavailable` | 数据库、目录、审计、事件或 Snapshot 完整性不可用 |

错误正文只返回固定 message、建议、诊断和 `request_id/trace_id`，不包含模板 DSL、绑定值、
价格、预算、Provider Key、Prompt、Response、SQL 或另一 tenant 的存在性。

## 8. 数据、审计与迁移

- `0060_industry_scenario_policy_template_library.sql` 创建 tenant-scoped 模板、application、item、event
  及 owner/不可变/持续发布约束；新 tenant 由触发器 seed 固定五条模板。
- apply 写入 `policy.created`、`policy_snapshot.published`、`policy_template.applied` 三类 source event
  和对应 AuditLog；模板事件使用持久 source ID 作为 Webhook 幂等键。
- `policy_template.applied` payload 固定为九字段：template code/version、application/generated Policy ID、
  前后 Snapshot 版本、`impact_hash`、`bindings_hash` 和固定 changed resource types；不含 DSL、绑定值、
  价格、预算余额、Prompt 或 Response。
- `0060` down 只有在不存在不能安全降级的模板业务事实、自定义 RBAC 或未知依赖时才允许执行；否则
  原子失败关闭，避免静默丢失治理状态。
