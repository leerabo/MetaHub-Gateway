# Workspace Onboarding API

PRD-097 为当前 Workspace 提供一个由后端事实求值的八步初始化投影。API 只接受
WorkspaceSession，不接受客户端提交完成状态，也不返回 tenant、资源内部 ID、Secret、Endpoint、模型名、
项目名、调用正文或上游错误正文。

## 凭据与请求边界

- Session Cookie：`__Host-metahub-workspace-session`；写请求同时要求
  `__Host-metahub-workspace-csrf` 与唯一 `X-CSRF-Token` 完全匹配。
- GET、PATCH 和 recheck 均拒绝 Authorization、PlatformSession、SupportSession、重复当前域 Cookie 和
  任何混合可解释凭据。
- 写请求必须来自配置允许的唯一 Origin，且不能是 `Sec-Fetch-Site: cross-site`。
- 所有接口拒绝 query 参数，成功和错误响应均为 `Cache-Control: no-store, private`。
- PATCH 只接受唯一 `Content-Type: application/json`、UTF-8、唯一 JSON 字段和最多 1024 bytes 的正文；
  recheck 不接受请求正文或 Content-Type。

## 接口

| 方法 | 路径 | 请求 | 结果 |
| --- | --- | --- | --- |
| GET | `/workspaces/current/onboarding` | active WorkspaceSession | 实时求值并返回八步投影；不写 checkpoint/event |
| PATCH | `/workspaces/current/onboarding/steps/{step_id}` | workspace CSRF、带引号的 `If-Match`、`Idempotency-Key` 和严格 JSON | 记录 confirm/defer/reopen 意图并返回新投影 |
| POST | `/workspaces/current/onboarding/recheck` | workspace CSRF、带引号的 `If-Match`、`Idempotency-Key`，空正文 | 重算并持久化 StepFact、Checkpoint、Event/Outbox |

`If-Match` 必须是带引号的正整数，例如 `"3"`。`Idempotency-Key` 为 8..128 字符，首字符为字母或
数字，其余字符限字母、数字、`.`、`_`、`:`、`-`。相同 key 和相同请求返回原完整结果；相同 key 配不同
请求返回冲突。

PATCH 正文固定为：

```json
{
  "action": "confirm",
  "reason_code": "user_confirmed"
}
```

允许的配对如下：

| action | reason_code | 约束 |
| --- | --- | --- |
| `confirm` | `user_confirmed` | 只用于当前可确认的 profile/environment |
| `defer` | `do_later` | 只记录稍后处理，不计完成、不解锁依赖 |
| `reopen` | `configuration_changed` 或 `retry_required` | 只撤销当前 user-confirmed acknowledgement，不删除资源 |

技术步骤不能由客户端 confirm。organization 的 profile 只允许 owner/admin 操作；developer 可对获授权的
技术步骤执行 defer/recheck，但不能获得 profile 修改能力。所有权限在每次请求中从 active Membership 与
revision 重新计算。

## 响应投影

顶层字段固定为：

- `workspace_kind`：固定为 `organization`；个人空间已从产品和数据模型中移除。
- `completed_steps`、固定 `total_steps=8`、`status=incomplete|active`。
- `generation`、`revision`、可空 `activated_at`、`evaluated_at`。
- 严格有序的 `steps`、`request_id` 和 `trace_id`。

八个步骤依次为 `workspace_profile`、`environment`、`provider`、`model`、`model_alias`、
`project_application`、`virtual_key`、`first_call`。每步只返回 `id/order/title/status/source/action/`
`navigation_target/blocking_code/last_checked_at`。

status 固定为 `current/blocked/complete/deferred/needs_attention`；source 固定为
`system_created/user_confirmed/verified/legacy_seed/none`。`system_created` 和 `legacy_seed` 都不等于完成。
依赖未满足时 blocking code 为 `requires.<step_id>`；已完成事实失效时为 `evidence_changed`；历史配置无法
证明来源时为 `legacy_configuration`。

## 后端事实

完成状态只由当前 Workspace 的一致数据库快照求值：profile/environment 需要当前 Membership revision
绑定的确认；Provider 需要非 seed owner、可解密 Credential 或免密本地 Endpoint，以及成功健康事实；
Model/Alias/Project/Application/VirtualKey 必须形成同 owner 的 active 非 seed 资源链；first_call 必须绑定
当前 Key、Alias/Model 和 Usage/ledger success。placeholder、历史 seed、缺 owner、失效探测和孤立 success
request 均不能完成步骤。

recheck 使用 revision CAS。八步首次同时成立时生成 `workspace.activated`；后续事实失效生成
`workspace.activation_lost`，恢复后可进入新 generation。事件只保存步骤、来源、generation 和安全 hash，
不保存客户正文或凭据。

## 错误语义

- `workspace_session.required/invalid/credential_conflict/csrf_invalid`：凭据域或 CSRF 不合法。
- `onboarding.request_invalid/origin_invalid`：传输、header、path/body 枚举或 Origin 不合法。
- `onboarding.permission_denied/action_invalid/step_not_found`：当前角色或步骤操作不允许。
- `onboarding.revision_required/revision_conflict`：缺少合法 CAS 或投影已变化，客户端应重新 GET。
- `onboarding.idempotency_conflict`：幂等键已绑定其他请求。
- `onboarding.conflict/unavailable`：并发事务冲突或事实无法安全求值。

错误只返回固定安全文案、`request_id` 与 `trace_id`，不会暴露表名、SQL、解密结果或第三方响应。

## 既有资源 API 兼容

current Workspace profile/Environment、Provider/Credential/local Endpoint、Model/Alias、
Project/Application 和 Virtual Key 路由允许恰好一种 Legacy Bearer 或 WorkspaceSession。WorkspaceSession 写
请求在解析业务正文前验证 Origin 和 workspace CSRF，随后仍按原 permission code、tenant owner 和资源
约束执行。allowlist 外的 tenant API 不扩大 Cookie 认证范围；旧 `/tenant/onboarding/summary` 继续服务
N/N-1 Legacy 客户端，但使用同一 seed 排除和事实求值逻辑。
