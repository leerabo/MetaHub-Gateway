# 轻量敏感意图分类 API

本文档冻结 PRD-069 的 Control 分类入口、RequestLog 安全摘要和 Webhook 合同。分类器仅在本地运行，
不调用 Provider、外部 LLM、OpenRouter 或硅基流动，也不保存 Prompt、命中片段、位置或输入 hash。

## 1. 认证、权限与租户

- `POST /security/classify-intent` 要求后端实时权限 `intent_risk.classify`，默认仅 Admin 和
  SecurityAdmin 获得；Developer、Viewer、FinanceAdmin 和 OpsAdmin 均为 403。
- `GET /request-logs/{request_id}` 的 `intent_risk` 仍由 `request_log.read` 控制，不要求分类写权限。
- tenant、actor、result ID、request ID 和 trace ID 均由服务端恢复或生成。分类入口忽略外部
  `X-Request-ID/X-Trace-ID`，防止客户端正文借 Header 进入数据库、事件或日志。
- 所有成功和错误响应携带服务端 `request_id/trace_id`，并设置 `Cache-Control: no-store`。

## 2. POST /security/classify-intent

请求必须是唯一 `application/json`，不接受 query，raw body 上限 128 KiB，正文只允许一个字段：

```http
POST /security/classify-intent
Authorization: Bearer <session-token>
Content-Type: application/json

{"text":"一次性待分类文本"}
```

`text` 必须为非空字符串，最多 20,000 code points。重复 JSON key、未知字段、`tenant_id`、非标准
JSON 常量、错误 Content-Length、错误媒体类型和超限正文均在进入领域层前拒绝。后端先完成认证与
RBAC，再读取正文；正文只在当前请求内存中存活。

成功响应：

```json
{
  "intent_risk_result": {
    "intent_risk_result_id": "irr_...",
    "status": "detected",
    "classification_mode": "manual_sync",
    "risk_type": "policy_bypass",
    "risk_score": 9700,
    "risk_score_scale": 10000,
    "action": "review",
    "reason_code": "explicit_policy_bypass",
    "detector_version": "intent-rule-engine-v1",
    "ruleset_version": "intent-rules-2026-08-03-v1",
    "input_code_points": 128,
    "input_truncated": false,
    "duration_ms": 2,
    "completed_at": "2026-08-03T12:00:00Z"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

`risk_type` 固定为 `none/credential_exfiltration/system_prompt_extraction/policy_bypass/jailbreak`；
分数固定为 0..10000 整数。manual clean 使用 `allow`，detected 使用 `review`，分类器失败使用
`none`。结果和 detected source event 在同一 SQLite 事务提交；Webhook 在提交后 best-effort 派发。

## 3. RequestLog intent_risk

`GET /request-logs/{request_id}` 的 `request_log` 新增独立对象，不改变 PRD-054 Request Explanation v1：

```json
{
  "intent_risk": {
    "intent_risk_result_id": "irr_...",
    "status": "pending",
    "classification_mode": "async_light",
    "risk_type": null,
    "risk_score": null,
    "risk_score_scale": 10000,
    "action": null,
    "reason_code": "queued",
    "detector_version": "intent-rule-engine-v1",
    "ruleset_version": null,
    "input_code_points": null,
    "input_truncated": false,
    "duration_ms": null,
    "completed_at": null
  }
}
```

状态固定为 `disabled/pending/clean/detected/blocked/failed/skipped`。详情先按
`tenant_id + intent_risk_result_id + source_request_id` 关联，再逐项核对 source trace、Virtual Key actor、
分类模式、detector 版本、截断标志和终态；任一不一致都返回
`intent_risk.result_integrity_failed`，不能静默显示 pending 或跨租户证据。无结果的同步失败关闭仍返回
RequestLog 已冻结的 `action=none`，不会与尚未执行的 pending 空动作混淆。

## 4. security.intent_detected Webhook

只为 `detected/blocked` 创建事件。payload 使用 exact-key 安全摘要：result/source ID、source kind、
classification mode、risk type、整数分数/量纲、action、reason、detector/ruleset version、截断标志和
completed time。冻结规则原始分数和安全培训等良性语境固定降 2500 分后的唯一分数均逐项白名单校验，
不接受任意分数区间。envelope 的 tenant、trace、actor、event/object ID 必须与持久 source event 逐项一致。
正文、匹配内容、hash、Key、Token、Endpoint、Header 和异常文本一律拒绝。

## 5. 统一错误

| code | HTTP | 含义 |
| --- | ---: | --- |
| `auth.token_missing` | 401 | Session Token 缺失 |
| `auth.token_invalid` / `auth.session_revoked` | 401 | Token 非法或 Session 已撤销 |
| `auth.permission_denied` | 403 | 当前数据库权限矩阵不允许分类或读取日志 |
| `intent_risk.media_type_unsupported` | 415 | 非唯一 `application/json` |
| `intent_risk.payload_too_large` | 413 | raw body 超过上限 |
| `intent_risk.payload_invalid` | 400 | JSON、字段集、query 或长度声明非法 |
| `intent_risk.text_invalid` | 422 | 文本为空、类型错误或超过字符上限 |
| `intent_risk.classifier_unavailable` | 503 | 本地分类器失败 |
| `intent_risk.result_store_unavailable` | 503 | 结果/event 事务不可提交 |
| `intent_risk.result_integrity_failed` | 503 | RequestLog 嵌套结果 owner 或状态不一致 |
| `gateway.intent_blocked` | 403 | Gateway 强规则在 Provider 前阻断 |
| `gateway.intent_classifier_unavailable` | 503 | Gateway 同步分类失败关闭 |
| `gateway.intent_result_store_unavailable` | 503 | Gateway 同步安全事实无法提交 |

所有错误只返回固定 message/action 和链路 ID，不包含输入、命中片段、SQL 或底层异常消息。
