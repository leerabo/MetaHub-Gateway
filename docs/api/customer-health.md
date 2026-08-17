# 客户健康评分 API

本文档对应 `PRD-086 客户健康评分`。公开接口只返回当前 Session tenant 最近一次已提交的确定性评分；
调用方不能传入 tenant、窗口、权重或阈值，GET 不扫描来源、不物化评分、不创建事件。

## GET /customer-health

- 认证：`Authorization: Bearer <token>`。
- 权限：`customer_health.read`；RBAC revision 21 默认仅授予 `admin`、`ops_admin`。
- Query/body：均不接受；任意 query 返回 `customer_health.request_invalid`。
- 租户：只从后端认证上下文恢复，不能由 Header、query 或前端导航覆盖。
- 缓存：成功及 401/403/422/503 全部返回 `Cache-Control: no-store, private`。
- 链路：响应体 `request_id/trace_id` 必须分别等于 `X-Request-ID/X-Trace-ID`。

成功响应顶层 exact-set：

```json
{
  "tenant_id": "tenant_...",
  "customer_health_score_id": "chs_...",
  "score_version": "customer-health-v1",
  "score_bps": 3944,
  "risk_level": "at_risk",
  "risk_tags": ["high_error_rate", "budget_missing", "team_inactive"],
  "missing_signals": ["ticket_health"],
  "effective_weight_bps": 9000,
  "freshness_status": "fresh",
  "recommended_actions": [
    {"code": "review_failed_requests", "target_navigation_item_id": "request-logs"},
    {"code": "connect_support_ticket_source", "target_navigation_item_id": null}
  ],
  "window": {
    "start": "2026-08-10T10:00:00Z",
    "end": "2026-08-17T10:00:00Z",
    "comparison_start": "2026-08-03T10:00:00Z",
    "comparison_end": "2026-08-10T10:00:00Z",
    "timezone": "UTC"
  },
  "signals": [],
  "activation": {"completed_stage_count": 1, "total_stage_count": 7, "activated": false},
  "evaluated_at": "2026-08-17T10:00:01Z",
  "generated_at": "2026-08-17T10:00:01Z",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

`signals` 必须按以下顺序返回六项：

| code | weight_bps | 来源 | v1 可用性 |
| --- | ---: | --- | --- |
| `usage_trend` | 2500 | `request_logs` 当前/前一 7 日请求量 | `available|no_samples` |
| `reliability` | 2500 | 当前 7 日成功/失败终态 | `available|no_samples` |
| `cost_anomaly_health` | 1500 | 未解决成本异常及 critical 数 | `available` |
| `budget_readiness` | 1000 | 当前 tenant active 预算规则数 | `available` |
| `team_activity` | 1500 | active 用户和 30 日近期活跃去重数 | `available|no_samples` |
| `ticket_health` | 1000 | 尚无权威 SupportTicket 来源 | 固定 `unavailable` |

每项 exact-set 为 `code`、`weight_bps`、`availability`、`health_status`、`score_bps`、
`contribution_bps`、`current_value`、`comparison_value`、`rate_bps`、`source_kind`。v1 的
`ticket_health` 固定 `score_bps=null`、贡献 0，顶层 `effective_weight_bps=9000`；其他信号按整数基点计算，
先舍入总分，再用固定 tie-break 的最大余数分配贡献，保证贡献之和严格等于 `score_bps`。

风险等级为 `healthy|watch|at_risk|critical`。除总分阈值外，零调用、高错误率和未解决 critical 成本异常具有
风险下限；标签与建议动作均为固定枚举，不使用 LLM 或续费预测模型生成。

## 错误

| HTTP | code | 场景 |
| ---: | --- | --- |
| 401 | `auth.token_missing` 等统一认证错误 | 缺失、过期或无效凭据 |
| 403 | `auth.permission_denied` | 后端权限校验失败 |
| 422 | `customer_health.request_invalid` | 携带任意 query 或请求合同不合法 |
| 503 | `customer_health.not_ready` | 当前 tenant 尚无已提交评分 |
| 503 | `customer_health.unavailable` | 数据库不可用或评分不变量损坏 |

错误沿用统一信封，不返回数据库异常、来源正文或敏感值；所有错误都携带可关联的 `request_id`。

## Scheduler 与事件

- 后台 Scheduler 以 UTC 整点为窗口终点，每小时为到期 tenant 追加一个不可变评分。
- 空轮询先执行只读到期探测；没有待办时不执行 `BEGIN IMMEDIATE`，避免占用 SQLite writer。
- 首个基线或公开健康投影发生变化时，评分和 EVT-098 source outbox 原子创建；仅窗口或原始计数变化不误发事件。
- `customer.health_changed` payload 只包含 score ID、版本、总分、风险、标签、缺失信号和窗口终点；不包含
  Provider Key、Token、Prompt、Response、Endpoint、用户明细或请求 ID 列表。
- source outbox 冻结订阅快照，支持租约续期、过期接管、固定退避和十次失败终态。

用量、成本和失败请求继续由既有 RequestLog、Usage、FinOps 链路追踪；客户健康只保存安全聚合计数和可解释贡献。
总体状态保持 `all_108_prds_complete=false`。
