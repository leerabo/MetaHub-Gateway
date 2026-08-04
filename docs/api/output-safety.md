# 输出侧安全检测与流式滑窗合同

本文档对应 `PRD-071 - 输出侧安全检测与流式滑窗`，冻结 Gateway 输出检测、Control 读模型、
DLP 检测方向、FinOps 争议投影和 `security.output_blocked` Webhook 的公开边界。完整状态机、
0063 数据模型、事务顺序和两轮编码前自审见
[PRD-071 设计文档](../prd/PRD-071-output-safety-streaming-window.md)；最终分层测试、Runtime 与 Browser
事实见 [PRD-071 测试记录](../testing/PRD-071-tests.md)。

PRD-071 已于 2026-08-04 完成迁移、相关分层回归、实际运行和双视口 Browser 验收。本文继续作为
稳定接口合同；全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。

## 1. 能力边界

- 非流式 Chat Completions 在完整 Provider 成功响应返回后执行全文检测。
- 流式 Chat Completions 按部署级 Token 间隔和有限滑窗检测完整 SSE event；已经发送的安全前缀不承诺撤回。
- 只加载当前 tenant 中 `status=active` 且 `scan_scope=output|both` 的 DLP 规则。历史规则和新建规则
  省略方向时均为 `input`，升级不会静默扩大治理范围。
- 输出阻断不触发 Fallback，不降低 Provider 质量评分，也不把真实 Usage 或正式 charge 清零。
- 自动阻断只创建待复核 dispute marker，不自动创建 FinOps reversal。
- 不保存 Prompt、Response、命中片段、规则 pattern、可反推正文的 hash、Authorization、Provider Key、
  Virtual Key 明文或底层异常文本。
- OpenRouter 和硅基流动仍是竞品/迁移兼容对象，不是默认上游；该能力同样适用于客户本地
  OpenAI-compatible Endpoint。

## 2. Gateway Chat Completions 行为

公开请求路径不变：

```http
POST /v1/chat/completions
Authorization: Bearer <virtual-key>
Content-Type: application/json
```

tenant、request、trace、模型路由和规则快照均由 Virtual Key 认证后的服务端上下文恢复，客户端不能
通过 query、body 或 Header 覆盖。内部 `output_scan` 是 Gateway 进程内服务，不注册第二套公开文本扫描 API。

### 2.1 非流式

- `clean` 或 `allow/log_only` 命中：先提交扫描证据，再原样返回成功 JSON。
- `block` 命中：成功的 Provider attempt 保持 `succeeded`，结算真实或估算 Usage，客户端收到
  HTTP 403 `content_policy_violation`，Provider 正文不返回。
- 规则快照、响应投影、matcher 或证据不可证明：返回 HTTP 503
  `gateway.output_safety_unavailable`，不把未知状态解释为 clean。
- Provider 已产生费用但主库与 crash spool 都未确认 Usage：返回更高优先级 HTTP 503
  `finops.usage_record_unavailable`，且不派发阻断 Webhook。
- 主模型失败后允许的单次 Fallback 继续复用同一个 pending scan 和冻结规则快照；最终 Provider 没有产生
  可扫描成功输出时，扫描终态为 `not_applicable + none`。

统一错误关键字段示例（完整响应还包含平台统一的安全 `diagnosis` 对象）：

```json
{
  "code": "content_policy_violation",
  "message": "Model output was blocked by content policy.",
  "request_id": "req_...",
  "trace_id": "trc_...",
  "suggestion": "Review the output safety event with the returned request id."
}
```

固定文案不得拼接规则名称、pattern、命中正文、Provider 响应或数据库异常。

### 2.2 流式

启用输出规则时，Gateway 以完整 SSE event 为最小放行单元。默认
`interval_tokens=32`、`window_tokens=256`；部署配置的合法范围分别为 8..256 和 32..1024，且
window 不得小于 interval。单 event 上限为 1 MiB，非流式全文上限为 262,144 code points。

处理顺序固定为：

```text
SSE 分帧 -> 观察 Provider Usage -> 扫描当前检查点 -> 记录已放行 event -> 原字节转发
```

命中 event 计入 `observed_output_token_count` 和 `scanned_token_count`，但不计入
`emitted_token_count`。同一 raw chunk 中更早已经通过的完整 event 可以先发送；尚未闭合的 event 前缀不会
提前泄露。收到 `[DONE]` 后立即终止解析，同批及后续字节不再进入正文投影。

阻断时 Gateway 关闭上游，不再发送命中 event，不再发送 `[DONE]`，只追加一个标准 SSE error：

```text
event: error
data: {"error":{"code":"content_policy_violation","message":"Model output was blocked by content policy.","request_id":"req_...","trace_id":"trc_...","output_safety_scan_id":"oss_...","already_emitted":true,"emitted_token_count":8}}

```

`already_emitted=true` 只表示 ASGI 响应生成器已交付过正文 event，不代表客户端 TCP 已确认；已发内容不可撤回。
parser/matcher/证据故障使用 `gateway.output_safety_unavailable` SSE error。timeout、客户端断开和上游读取失败
继续保留原 StreamSession 错误优先级，同时把未完成扫描记录为 `unavailable`，不伪造阻断事件或账单争议。

### 2.3 输出安全状态

`scan_status` 与 `decision` 是正交字段：

| scan_status | 合法 decision | 含义 |
| --- | --- | --- |
| `pending` | `none` | Provider 前 owner 已建立，但尚无终态；进程中断不能推断为 clean |
| `not_configured` | `none` | 当前 tenant 没有 active output/both 规则，网络路径保持兼容 |
| `not_applicable` | `none` | Provider 未产生可扫描成功输出或请求不是可扫描 Chat |
| `completed` | `clean/matched/blocked` | 扫描完整；matched 对应 allow/log_only，blocked 对应 block |
| `unavailable` | `unknown/matched` | 扫描不完整或依赖不可用；已知 log-only 命中可以保留 matched |

`exposure_state` 固定为 `none/partial/complete/unknown`；`billing_disposition` 固定为
`not_required/dispute_pending`。只有 `completed + blocked` 可以为 `dispute_pending`。

## 3. PAGE-099 RequestLog 列表

PAGE-099 不使用独立扫描查询 API，而是复用：

```http
GET /request-logs?terminal_stage=output_safety&limit=20
Authorization: Bearer <console-session>
```

后端逐请求校验 `request_log.read`，tenant 只从 Session 恢复。默认角色中 Admin、SecurityAdmin、Developer、
OpsAdmin 可读，Viewer 和 FinanceAdmin 被后端拒绝。前端隐藏导航不能替代授权。

`terminal_stage=output_safety` 同时匹配：

- scan `decision=blocked`；
- scan `scan_status=unavailable`；
- 兼容错误码 `content_policy_violation/gateway.output_safety_unavailable`。

因此即使 Usage 双失败把公开错误提升为 `finops.usage_record_unavailable`，只要阻断 scan 已可靠提交，安全事件
仍不会从 PAGE-099 漏掉。

PAGE-099 还使用四个可选精确枚举筛选：

| 参数 | 允许值 |
| --- | --- |
| `output_safety_status` | `pending/not_configured/completed/unavailable/not_applicable` |
| `output_safety_mode` | `non_stream_full/stream_window` |
| `output_safety_exposure` | `none/partial/complete/unknown` |
| `output_safety_billing` | `not_required/dispute_pending` |

未知参数、重复参数、显式空枚举、非法时间和越界 limit/offset 继续使用 RequestLog 的
失败关闭规则。PAGE-099 每次请求都必须强制 `terminal_stage=output_safety`，SavedFilter 或页面表单不能移除。

列表中 `output_safety` 为可空最小摘要：

```json
{
  "output_safety_scan_id": "oss_0123456789abcdef0123456789abcdef",
  "scan_mode": "stream_window",
  "scan_status": "completed",
  "decision": "blocked",
  "exposure_state": "partial",
  "emitted_token_count": 8,
  "emitted_sse_event_count": 1,
  "emitted_bytes": 143,
  "billing_disposition": "dispute_pending"
}
```

列表不返回 action、规则 ID、detector、failure reason、dispute、正文或 pattern。

## 4. PAGE-100 RequestLog 详情

```http
GET /request-logs/{request_id}
Authorization: Bearer <console-session>
```

详情继续由 `request_log.read` 控制。跨 tenant ID 与不存在 ID 返回同形
`404 request_log.not_found`。`request_log.output_safety` 为可空/派生的严格对象，字段为：

```json
{
  "output_safety_scan_id": "oss_0123456789abcdef0123456789abcdef",
  "scan_mode": "stream_window",
  "scan_status": "completed",
  "decision": "blocked",
  "exposure_state": "partial",
  "emitted_token_count": 8,
  "emitted_sse_event_count": 1,
  "emitted_bytes": 143,
  "billing_disposition": "dispute_pending",
  "action": "block",
  "dlp_rule_id": "dlp_0123456789abcdef0123456789abcdef",
  "rule_type": "keyword",
  "match_count": 1,
  "ruleset_fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "detector_version": "output-safety-dlp-v1",
  "interval_tokens": 32,
  "window_tokens": 256,
  "checkpoint_count": 2,
  "observed_output_token_count": 20,
  "scanned_token_count": 16,
  "failure_reason_code": null,
  "started_at": "2026-08-04T05:00:00Z",
  "completed_at": "2026-08-04T05:00:01Z",
  "dispute": {
    "output_safety_scan_id": "oss_0123456789abcdef0123456789abcdef",
    "review_status": "dispute_pending",
    "reversal_eligible": true
  }
}
```

迁移前成功 Chat 缺少 scan 时，详情投影 `legacy_unavailable + unknown`；Provider 前失败或非 Chat 投影
`not_applicable + none`。0063 应用后的成功 Chat 若缺少应有 scan，或 scan/dispute/request 的 tenant、request、
trace、Usage/charge owner 不一致，返回 `503 output_safety.integrity_failed`，不得降级为空或 clean。

## 5. DLP 检测方向

既有接口保持不变，只扩展 `scan_scope`：

| 方法与路径 | 后端权限 | 方向合同 |
| --- | --- | --- |
| `GET /dlp/rules?scan_scope=output` | `dlp.read` | 可选按 `input/output/both` 筛选，响应必返方向 |
| `POST /dlp/rules` | `dlp.write` | `scan_scope` 可省略，默认 `input` |
| `PATCH /dlp/rules/{rule_id}` | `dlp.write` | 省略时保留原值，只接受三个固定枚举 |

输入 DLP 热路径只加载 `input/both`，输出安全只加载 `output/both`。所有读写先按可信 tenant 限定，跨 tenant
规则 ID 与不存在 ID 同形；Audit 只保存方向枚举和原有脱敏摘要。

## 6. FinOps 最小投影

`GET /finops/ledger` 的每个 `usage_charge` 可新增可空字段：

```json
{
  "output_safety_dispute": {
    "output_safety_scan_id": "oss_0123456789abcdef0123456789abcdef",
    "review_status": "dispute_pending",
    "reversal_eligible": true
  }
}
```

FinanceAdmin 只能通过既有 `billing.finops.read` 看这三个字段，不会因此获得 `request_log.read`、DLP 规则、
模式或暴露计数。`reversal_eligible=true` 只表示对应正金额 charge 可以进入现有人工冲正流程，不代表已经冲正；
零金额 dispute 仍为待复核，但该值为 false。任何 dispute/scan/Usage/charge owner 不一致都返回
`503 output_safety.integrity_failed`。

## 7. `security.output_blocked` Webhook

只有可靠提交的 `completed + blocked` scan 才创建 source event；Usage/FinOps/dispute 已提交或 v5 spool 已
可靠落盘后，才机会性进入既有有界 dispatcher。队列满、投递失败或死信不回滚已提交安全事实。

exact payload：

```json
{
  "schema_version": 1,
  "output_safety_scan_id": "oss_0123456789abcdef0123456789abcdef",
  "request_id": "req_...",
  "scan_mode": "stream_window",
  "scan_status": "completed",
  "decision": "blocked",
  "error_code": "content_policy_violation",
  "exposure_state": "partial",
  "emitted_token_count": 8,
  "checkpoint_count": 2,
  "dlp_rule_id": "dlp_0123456789abcdef0123456789abcdef",
  "detector_version": "output-safety-dlp-v1",
  "billing_disposition": "dispute_pending",
  "occurred_at": "2026-08-04T05:00:00Z"
}
```

信封 `object_id` 必须等于 scan ID，`source_request_id` 必须等于 payload request ID，trace/timestamp 必须与
append-only source event 一致，自动事件 `actor_id=null`。payload 不包含 `reversal_eligible`，因为该值依赖后续
正式 charge 金额，不能由先提交的安全事件猜测。

## 8. 统一错误与响应头

| HTTP/流式 | code | 场景 |
| ---: | --- | --- |
| 403 / SSE | `content_policy_violation` | 模型输出命中 block |
| 503 / SSE | `gateway.output_safety_unavailable` | 规则、framer、matcher 或证据不可用 |
| 503 / SSE | `finops.usage_record_unavailable` | Provider 成本存在，但主库和 spool 均未确认 Usage |
| 503 | `output_safety.integrity_failed` | Control 读取发现 owner 或状态组合损坏 |
| 404 | `request_log.not_found` | 当前 tenant 看不到详情目标，跨租户同形 |
| 422 | `request_log.query_invalid/pagination_invalid/sort_invalid` | RequestLog 查询合同非法 |
| 400 | `dlp.rule_invalid` | DLP 方向或其他规则字段非法 |
| 401 | `auth.*` | Session 缺失、无效、过期或撤销 |
| 403 | `auth.permission_denied` | 后端权限矩阵拒绝 |

Control JSON 与 Gateway 非流式错误均携带 `request_id/trace_id`，并通过响应头返回
`X-Request-ID/X-Trace-ID`。身份相关 Control 读取使用 `Cache-Control: no-store, private`；Gateway 错误使用
`Cache-Control: no-store`。SSE error 在 HTTP 200 已开始后仍携带同一 request/trace ID。

## 9. 运行配置

```dotenv
METAHUB_OUTPUT_SAFETY_INTERVAL_TOKENS=32
METAHUB_OUTPUT_SAFETY_WINDOW_TOKENS=256
METAHUB_OUTPUT_SAFETY_MAX_FULL_TEXT_CODE_POINTS=262144
```

这些值是部署级安全边界，租户请求不能覆盖。非法范围在启动配置解析阶段失败，不能自动钳制或降级为
另一含义。生产日志、数据库、Webhook、DOM、URL、Storage 和验收制品都必须继续执行敏感明文门禁。
