# 产品激活漏斗 API

本文档对应 `PRD-085 产品使用埋点与激活漏斗`。服务端从租户限定的不可变 `ProductEvent`
聚合七阶段激活状态；客户端不能提交事件、指定租户或改变统计窗口。

## 1. 权限与租户边界

- 公开接口固定为 `GET /product-analytics/funnel`。
- 后端要求当前 Session 实时具备 `product_analytics.read`；默认只授予 Admin 与 Ops Admin。
- `tenant_id` 只从认证上下文取得。接口不接受 query、body 或自定义 tenant header。
- 前端 capability 与导航可见性只负责呈现，不能替代后端认证、授权或 tenant owner 校验。
- 成功与错误响应均设置 `Cache-Control: no-store, private`，并返回一致的 `request_id` 与
  `trace_id` 响应头/正文。

## 2. 查询激活漏斗

### `GET /product-analytics/funnel`

统计窗口固定为当前租户全历史和 UTC。成功响应恰好包含七个冻结顺序的阶段：

1. `tenant.registered`
2. `provider.created`
3. `virtual_key.created`
4. `gateway.first_call_succeeded`
5. `budget.configured`
6. `teammate.invited`
7. `request_logs.viewed`

```json
{
  "tenant_id": "tenant-example",
  "window": {"kind": "all_time", "timezone": "UTC"},
  "cohort_tenant_count": 1,
  "stages": [
    {
      "order": 1,
      "event_type": "tenant.registered",
      "event_count": 1,
      "reached": true,
      "cumulative_reached": true,
      "reached_tenant_count": 1,
      "cumulative_tenant_count": 1,
      "conversion_from_previous_bps": 10000,
      "first_occurred_at": "2026-08-17T00:00:00Z",
      "last_occurred_at": "2026-08-17T00:00:00Z"
    }
  ],
  "summary": {
    "completed_stage_count": 1,
    "cumulative_stage_count": 1,
    "total_stage_count": 7,
    "completion_bps": 1429,
    "activated": false
  },
  "generated_at": "2026-08-17T00:01:00Z",
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

实际响应的 `stages` 始终为七项。`event_count` 是去重后的权威行为事实数；`reached` 表示本阶段
至少发生一次，`cumulative_reached` 表示从注册到本阶段没有缺口。阶段可以乱序发生，缺口补齐后
累计漏斗会重新收敛。

相邻转化率使用整数 basis points：第一阶段以当前 tenant cohort 为分母，后续阶段以上一阶段的
`cumulative_tenant_count` 为分母；分母为 0 时返回 `null`。时间为 RFC 3339 UTC 或 `null`，服务端
会把既有 SQLite UTC 文本、时区偏移和小数秒规范化为 `Z`。

响应不包含 ProductEvent 明细、actor、source ID、Provider/Key 名称、筛选条件、Prompt、Response、
Token、Endpoint、usage 或成本正文。用量、成本及失败请求继续由 RequestLog、Usage/FinOps Ledger 和
routing attempt 追踪。

## 3. ProductEvent 与 Webhook

七类事实分别从已提交的 seed 开通、AuditLog、成功 RequestLog 和请求日志查询事件派生。历史回填使用
`capture_mode=backfill` 且不外发；迁移后的实时事实使用 `capture_mode=live`，在同一事务写入可恢复
source outbox。

Webhook 事件固定为 `product_event.recorded`。payload 只包含 schema version、ProductEvent ID、冻结
阶段枚举、`live` 捕获模式和 UTC 发生时间；event ID、原始 delivery ID 快照、lease token 与 generation
均稳定受控。新增或删除 Endpoint 不得改变已冻结快照，十次失败后进入终态并关闭对应 pending delivery。

## 4. 错误码

| 错误码 | HTTP | 含义 |
| --- | ---: | --- |
| `auth.token_missing` 等 `auth.*` | 401 | Session 缺失、失效或撤销 |
| `auth.permission_denied` | 403 | 当前用户没有 `product_analytics.read` |
| `product_analytics.request_invalid` | 422 | 请求携带了任何不支持的 query |
| `product_analytics.unavailable` | 503 | 数据读取、聚合不变量或 source outbox 暂不可用 |

所有错误使用统一 envelope。数据库异常、Webhook 目标、凭据、请求/响应正文及其他底层异常文本不会进入
响应或结构化日志。
