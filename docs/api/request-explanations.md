# 请求解释中心 API

本文档定义 PRD-054 Request Explainability Center 的公开 Control API、权限、租户、
完整性、错误码和事件合同。设计依据见
[PRD-054 技术设计](../prd/PRD-054-request-explainability-center.md)。

## 1. 能力边界

请求解释中心以一条已存在的 Gateway request_logs 事实为锚点，在同一数据库快照中聚合：

- 鉴权与 Virtual Key 归属；
- 限流、预算、DLP 和 Policy 判定；
- 路由决定、候选、KeyPool/CacheAffinity 和 Provider attempt；
- Usage、正式 FinOps 分录、RequestLog/StreamSession；
- 与源请求建立可信关联的 Webhook delivery/attempt。

接口只解释请求发生时已经持久化的事实，不读取当前配置回算历史，不重新调用 Provider，
也不把缺失证据推断成通过。Feedback、任意 Trace 后端、Prompt/Response 回放和配置编辑
不属于本接口。

## 2. 认证、权限与租户隔离

- 认证方式：显式 Legacy Bearer，或真实 WorkspaceSession Cookie，禁止混用。
- Workspace GET 会创建访问事实，必须校验允许的 Origin 与 Workspace CSRF。
- admin、developer 可读取。
- 匿名请求返回统一 401；viewer 返回统一 403。
- 后端 RBAC 先于 path 业务校验和 query/body/header 严格校验执行。前端隐藏入口不能
  代替后端权限。
- tenant_id、actor、session 只能从已认证 Session 恢复，客户端不能提交。
- 源请求查询固定使用 tenant_id + request_id。不存在与跨租户请求返回同形 404，
  不返回计数、候选租户或差异化错误正文。

## 3. 两类 request_id

| 字段 | 含义 |
| --- | --- |
| request_explanation.source_request_id | 被解释的 Gateway 请求 ID，即 path 原值 |
| request_explanation.source_trace_id | 被解释的 Gateway trace ID |
| 顶层 request_id | 本次 Control API 读取操作的统一 request ID |
| 顶层 trace_id | 本次 Control API 读取操作的统一 trace ID |

源请求 ID 不做 trim、Unicode 归一化或大小写折叠。两个外部值不能在查询前被折叠成
同一个数据库身份。

## 4. 获取请求解释

### GET /request-explain/{source_request_id}

#### 4.1 请求约束

source_request_id 必须是 1 到 128 个字符的 canonical ASCII 标识，仅允许：

~~~text
A-Z a-z 0-9 _ . : -
~~~

接口采用固定最小披露合同：

- 不接受任何 query 参数，包括 tenant_id、include_raw 或动态字段选择；
- 不接受请求正文；
- 不接受 Content-Encoding、Transfer-Encoding 或非零 Content-Length；
- X-Request-ID、X-Trace-ID 若提供，只能各出现一次并使用同一安全字符集；
- 重复、逗号合并、控制字符、超长或非 ASCII 链路头统一拒绝。

成功为 HTTP 200，并包含 no-store/private、Pragma: no-cache、Vary: Authorization, Cookie
以及统一 X-Request-ID、X-Trace-ID。

PRD-147 字段授权：入口 `request_explanation.read` 不隐含来源领域权限。各区块按既有
`virtual_key.read`、`request_log.read`、`billing.budget.read`、`dlp.read`、`policy.read`、
`routing.read`、`provider.read`、`billing.usage.read` + `billing.finops.read`、`webhook.manage`
求交；摘要和区块内的跨域字段另按环境、项目、模型、Provider 健康、价格、质量与用量权限裁剪。
字段缺权返回 null，受限区块返回 `evidence_state=restricted`、固定 reason、空 facts；
相关阶段为 `restricted`，不返回原因详情、时间或引用。受限信息不进入缺失/截断列表。
整个响应含受限区块时 `status=restricted`，不把权限限制误报为证据完整或缺失。
授权取访问事实提交前重新鉴权后的权限快照，内部访问记录仍保存原始完整性状态。

响应骨架：

~~~json
{
  "request_explanation": {
    "request_explanation_id": "rxe_0123456789abcdef0123456789abcdef",
    "source_request_id": "req_01J...",
    "source_trace_id": "trc_01J...",
    "status": "complete",
    "evidence_version": "request_explanation_v1",
    "captured_at": "2026-07-28T03:00:00Z",
    "summary": {
      "source_status": "success",
      "error_code": null,
      "request_model": "chat-default",
      "resolved_model": "glm-4-flash",
      "provider_code": "zhipu",
      "environment_id": "env_...",
      "environment_name": "开发环境",
      "project_id": "prj_...",
      "project_name": "企业应用",
      "application_id": "app_...",
      "application_name": "客服助手",
      "snapshot_version": 7,
      "latency_ms": 420,
      "input_tokens": 128,
      "output_tokens": 64,
      "total_tokens": 192,
      "estimated_cost_amount": "0.001234",
      "currency": "CNY",
      "fallback_triggered": false,
      "created_at": "2026-07-28T02:59:59Z",
      "updated_at": "2026-07-28T03:00:00Z"
    },
    "decision_chain": [],
    "authentication": {},
    "rate_limit": {},
    "budget": {},
    "dlp": {},
    "policy": {},
    "routing": {},
    "keypool": {},
    "provider_response": {},
    "usage_and_finops": {},
    "logs": {},
    "webhooks": {},
    "missing_sections": [],
    "truncated_sections": [],
    "view_event_status": "pending"
  },
  "request_id": "req_console_explain_01",
  "trace_id": "trc_console_explain_01"
}
~~~

每次成功 GET 都创建新的 request_explanation_id 和不可变访问事实。view_event_status
固定为 pending，表示 request.explanation_viewed source outbox 已与访问事实原子提交；
它不宣称远端 Webhook 已收到事件。

## 5. 决策链与证据完整性

### 5.1 固定决策阶段

decision_chain 必须精确包含十个阶段并按下列顺序返回：

| 顺序 | stage | 说明 |
| ---: | --- | --- |
| 1 | authentication | Session、Virtual Key 与可信归属 |
| 2 | rate_limit | 请求级限流结论 |
| 3 | budget | 预算评估、阻断和 scope 事件 |
| 4 | dlp | DLP 扫描与阻断 |
| 5 | policy | 已发布快照和 Policy 决策 |
| 6 | routing | 路由决定与候选 |
| 7 | keypool | KeyPool、CacheAffinity 与可用凭据结果 |
| 8 | provider | Provider attempt 与归一错误 |
| 9 | usage_and_finops | Usage 和正式财务分录 |
| 10 | logs_and_webhooks | RequestLog、StreamSession 和 Webhook |

阶段 status 只允许 passed、blocked、failed、pending、not_evaluated、not_recorded。
阶段同时携带独立 evidence_state、固定 reason_code、可选时间和最多 16 个 evidence_refs。
业务失败不等于证据不完整：Provider 返回 5xx 时，Provider 阶段可以是 failed，其证据
仍可以是 complete。

### 5.2 固定详情区块

响应固定包含 authentication、rate_limit、budget、dlp、policy、routing、keypool、
provider_response、usage_and_finops、logs、webhooks 十一个区块。

每个区块包含 evidence_state、reason_code、有界 facts 和 truncated。evidence_state
只允许 complete、partial、not_recorded、not_applicable、pending。

| 区块 | 允许的 fact kind |
| --- | --- |
| authentication | authentication |
| rate_limit | rate_limit |
| budget | budget_evaluation、budget_scope_event |
| dlp | dlp_match |
| policy | policy_snapshot |
| routing | routing_decision、routing_candidate |
| keypool | routing_candidate、cache_affinity、routing_attempt |
| provider_response | routing_attempt |
| usage_and_finops | usage、finops_entry |
| logs | request_log、stream_session |
| webhooks | webhook_delivery、webhook_attempt |

未知 kind、缺字段、额外字段或嵌套对象不会进入公开响应。金额微元和可能超过 JavaScript
安全整数范围的值以普通十进制字符串返回，客户端不得用 Number 回算。

### 5.3 complete、partial 与截断

- status=complete 要求 missing_sections、truncated_sections 都为空。
- status=partial 必须能在上述两个数组中定位至少一个证据缺口。
- 两个数组按固定区块顺序返回、内部唯一且互斥。
- Candidate 64、Provider attempt 8、Budget 16、DLP 100、Usage 16、FinOps 32、
  Webhook delivery 100、Webhook attempt 300；各集合采用稳定排序的 LIMIT + 1。
- 超过上限时返回前 N 条、区块 truncated=true，并登记到 truncated_sections；
  不执行无界 COUNT。
- legacy 请求缺少后来引入的可选事实时返回 partial/not_recorded，不伪装完整。

## 6. 统一错误码

所有错误使用平台统一结构，并在 header 与正文携带同一个 operation request_id/trace_id。
认证错误继续使用公共 401/403 错误码。

| 错误码 | HTTP | 可重试 | 场景 |
| --- | ---: | --- | --- |
| request_explanation.not_found | 404 | 否 | 当前租户不存在源请求；跨租户同形 |
| request_explanation.request_id_invalid | 422 | 否 | path 不是 1..128 canonical ASCII ID |
| request_explanation.request_invalid | 422 | 否 | query、body、实体头或链路头不合法 |
| request_explanation.unavailable | 503 | 是 | 数据库、迁移或一致性读取暂不可用 |
| request_explanation.audit_unavailable | 503 | 是 | 访问事实无法原子保存，响应失败关闭 |
| request_explanation.integrity_failed | 500 | 否 | 持久 owner、JSON 或证据图不一致 |
| request_explanation.event_unavailable | 503 | 是 | 后台 source outbox 领取或回写失败 |

## 7. 查看事件与投递语义

成功读取在短写事务内保存 request_explanations 访问事实和稳定 rxevt_ 事件身份。后台
worker 使用 lease/CAS 领取并调用公共 Webhook dispatcher，语义为稳定 event ID 的
at-least-once，不宣称网络 exactly-once。

request.explanation_viewed payload 精确包含：

~~~text
schema_version
request_explanation_id
source_request_id
source_trace_id
source_status
explanation_status
missing_sections
truncated_sections
evidence_version
viewed_at
~~~

信封中的 tenant_id、actor_id、object_id=request_explanation_id 和 operation trace_id
均由后端绑定。事件不复制页面筛选、事实明细、成本明细、DLP hash 或任何请求正文。
失败只持久化固定错误码，网络 URL、响应正文和异常字符串不得落库。

## 8. 隐私与缓存约束

API、事件、日志和 Console 均不得返回或记录：

- Provider Key、Virtual Key 明文、Session Token、Authorization；
- Prompt、Response、原始 Provider 响应或错误正文；
- raw_usage、Policy DSL/AST 或 DLP 命中文本；
- Webhook URL、Secret、payload 或 attempt 错误摘要；
- Credential、Binding、指纹或可逆 Prompt fingerprint。

Provider、模型和归属名称只在当前租户限定查询后返回；不可变 ID 和请求时快照是审计
权威。成功响应禁止缓存，Console 不把源请求 ID 写入 URL、localStorage 或 sessionStorage。

## 9. Console 合同

PAGE-079 的可见导航以 /console/navigation 为唯一权威：

- admin/developer 可见，viewer/匿名无页面 DOM、无解释 API 预取；
- Request Logs 行内“解释”动作通过身份绑定的一次性内存 handoff 传递源 ID；
- 页面离开、登出、换租户、降权和身份 revision 变化会 abort 并清空内存/DOM；
- 迟到响应必须匹配 tenant/user/role/session/navigation/source/sequence owner；
- 1280x720 使用摘要与证据双列，390x844 使用稳定单列。

## 10. 验收状态

设计、两轮编码前自审和公开合同已经冻结。专项测试与最终 Runtime/Browser 结果以
docs/testing/PRD-054-tests.md 和 artifacts/prd054/batch 为准；在两类证据全部通过前，
本文档不单独证明 PRD-054 已完成。
