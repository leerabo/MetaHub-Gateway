# 成本异常检测 API

本文档对应 `PRD-053 - 成本异常检测基础`，描述当前 Control API、Gateway 临时限流覆盖层和
`cost.anomaly_detected` Webhook 的公开开发合同。详细算法、数据表与并发边界见
[PRD-053 设计文档](../prd/PRD-053-cost-anomaly-detection-foundation.md)。本文只记录接口合同，
不代表迁移、全量回归、真实 Runtime 或 Browser 已完成验收。

## 1. 能力边界

成本异常检测只读取正式 FinOps 账本中 `posted + debit + usage_charge` 的可信 CNY 微元事实，
不读取旧 Usage 汇总，也不在 Gateway 同步调用热路径执行扫描。当前固定支持三类规则：

| `rule_type` | 作用域 | 严重级别 | 说明 |
| --- | --- | --- | --- |
| `tenant_spend_spike` | `tenant` | `critical` | 当前 Environment 的 5 分钟成本相对过去 24 小时均值异常暴涨 |
| `virtual_key_spend_spike` | `virtual_key` | `critical` | 单个 Virtual Key 的 5 分钟成本异常暴涨 |
| `virtual_key_off_hours` | `virtual_key` | `warning` | 周末或工作日配置时段外发生达到门限的消费 |

检测只产生告警，不自动修改永久限流。管理员可以对 24 小时处置窗口内的 Virtual Key 异常
显式创建临时限流；Gateway 将永久规则与所有有效临时覆盖逐字段取最小值。覆盖到期或释放后
自然失效，不改写原永久规则。

金额与比率字段使用无前导零的 canonical 十进制字符串，CNY 展示字段固定六位小数。客户端
不得把 `*_micros` 或 `observed_ratio_millis` 转为 JavaScript `Number` 后重新判定异常。

## 2. 认证、权限与通用响应

两个接口都使用 Console Session Bearer Token，且当前都只允许 `admin`：

| 接口 | `admin` | `developer` | `viewer` | 匿名 |
| --- | :---: | :---: | :---: | :---: |
| `GET /cost-anomalies` | 读 | 403 | 403 | 401 |
| `PATCH /cost-anomalies/{cost_anomaly_id}/action` | 写 | 403 | 403 | 401 |

权限由后端依赖在 query、header 和 body 的业务解析之前执行。developer/viewer 即使提交畸形
JSON，也先得到 `auth.permission_denied`；前端隐藏入口不能替代该权限校验。

`tenant_id`、actor 和服务端时间只从已验证 Session 或服务器恢复，客户端不得通过 path、query
或 body 覆盖。所有查询和动作都使用 tenant-qualified 数据库条件；跨租户 ID 与不存在 ID
保持同形 404，不能用于枚举其他租户资源。

成功响应使用 `Cache-Control: no-store`，统一中间件在响应头返回 `X-Request-ID`、
`X-Trace-ID`，响应正文也携带 `request_id`、`trace_id`。统一错误正文同样携带调用链 ID。
客户端只应记录这些排障 ID，不应记录 Authorization、Session Token、幂等键、Virtual Key
明文、Provider Key、Prompt 或 Response。

## 3. 查询成本异常

### `GET /cost-anomalies`

GET 只读当前登录租户的异常、动作安全摘要和检测器状态，不触发检测或 Webhook。允许的 query
如下；未知参数、重复参数、空枚举和 `tenant_id` 均返回
`422 cost_anomaly.query_invalid`。

| 参数 | 约束 |
| --- | --- |
| `status` | `open`、`acknowledged`、`dismissed`、`mitigated` |
| `rule_type` | `tenant_spend_spike`、`virtual_key_spend_spike`、`virtual_key_off_hours` |
| `severity` | `warning` 或 `critical` |
| `environment_id` | 当前租户 `env_` 资源 ID；不存在或跨租户同形 404 |
| `virtual_key_id` | 当前租户 canonical UUID；不存在或跨租户同形 404，不接受 Key 明文 |
| `from_time` | 带显式时区的 ISO-8601；按 `window_start >= from_time` 过滤 |
| `to_time` | 带显式时区的 ISO-8601；按 `window_start < to_time` 过滤，必须晚于 `from_time` |
| `limit` | canonical 十进制整数 `1..100`，默认 `20` |
| `cursor` | 上一页返回的 opaque URL-safe HMAC cursor |

示例：

```http
GET /cost-anomalies?status=open&severity=critical&limit=20
Authorization: Bearer <console-session-token>
```

成功返回 HTTP 200：

```json
{
  "cost_anomalies": [
    {
      "cost_anomaly_id": "can_0123456789abcdef0123456789abcdef",
      "tenant_id": "tenant_example",
      "environment_id": "env_0123456789abcdef0123456789abcdef",
      "environment_name": "生产环境",
      "rule_type": "virtual_key_spend_spike",
      "severity": "critical",
      "status": "open",
      "scope_type": "virtual_key",
      "scope_id": "00000000-0000-4000-8000-000000000001",
      "scope_label": "结算服务 Key",
      "virtual_key_id": "00000000-0000-4000-8000-000000000001",
      "virtual_key_name": "结算服务 Key",
      "virtual_key_prefix": "mhv_prod_abcd",
      "top_virtual_key_id": null,
      "project_id_snapshot": "project_example",
      "application_id_snapshot": "application_example",
      "window_start": "2026-07-27T02:00:00Z",
      "window_end": "2026-07-27T02:05:00Z",
      "baseline_start": "2026-07-26T02:00:00Z",
      "baseline_end": "2026-07-27T02:00:00Z",
      "current_cost_micros": "5000000",
      "current_cost_cny": "5.000000",
      "baseline_total_micros": "100000000",
      "baseline_total_cny": "100.000000",
      "baseline_average_micros": "347222",
      "baseline_average_cny": "0.347222",
      "baseline_bucket_count": 288,
      "observed_ratio_millis": "14400",
      "threshold_multiplier_millis": 5000,
      "minimum_cost_micros": "10000",
      "source_entry_count": 5,
      "estimated_entry_count": 0,
      "legacy_entry_count": 0,
      "affected_virtual_key_count": 1,
      "representative_request_id": "req_example",
      "representative_trace_id": "trc_example",
      "recommended_action": "apply_temporary_rate_limit",
      "detection_version": "cost-anomaly-rules-v1",
      "event_dispatch_status": "dispatched",
      "detected_at": "2026-07-27T02:07:00Z",
      "mitigation_eligible_until": "2026-07-28T02:07:00Z",
      "mitigation_eligible": true,
      "temporary_rate_limit_override": null,
      "actions": []
    }
  ],
  "summary": {
    "open_count": 1,
    "critical_count": 1,
    "mitigated_count": 0,
    "active_override_count": 0,
    "detector_status": "healthy",
    "detector_error_code": null,
    "latest_window_end": "2026-07-27T02:05:00Z"
  },
  "total": 1,
  "limit": 20,
  "has_more": false,
  "next_cursor": null,
  "request_id": "req_cost_anomaly_list_01",
  "trace_id": "trc_cost_anomaly_list_01"
}
```

`summary.detector_status` 为 `healthy/degraded/idle`。`partial` 或 `failed` 检测运行会投影为
`degraded` 和固定 `detector_error_code`，不能被页面解释成“没有异常”。

cursor 绑定租户、规范化筛选、`limit` 和首屏 snapshot sequence。修改筛选、跨租户复用、篡改
签名或使用非 canonical Base64 都返回 `cost_anomaly.query_invalid`；分页期间新插入的异常不会
漂入已冻结快照。

## 4. 处置成本异常

### `PATCH /cost-anomalies/{cost_anomaly_id}/action`

动作接口只允许 admin，并执行以下严格 wire 合同：

- path ID 必须是 `can_` 加 32 位小写十六进制；非法、不存在和跨租户保持同形 404。
- 不接受任何 query 参数。
- 必须且只能有一个 `Content-Type`，允许 `application/json` 或唯一
  `charset=utf-8` 参数。
- 必须且只能有一个 `Idempotency-Key`；长度 8..128，只允许 ASCII
  `A-Z a-z 0-9 . _ : -`，不能含逗号。
- 正文是非空 UTF-8 JSON object，最大 8 KiB；若提供 `Content-Length`，必须是 canonical
  非负十进制并与实收字节一致。
- 拒绝任意层级重复 JSON key、未知顶层字段、控制字符、超过 8 层或 64 个节点、数组顶层、
  float、指数、NaN、Infinity、bool 冒充整数及数字字符串冒充整数。

允许的动作与精确字段组合：

| `action` | 必填附加字段 | 状态与约束 |
| --- | --- | --- |
| `acknowledge` | 无 | 只允许 `open -> acknowledged` |
| `dismiss` | `dismiss_reason` | 允许 `open/acknowledged -> dismissed`；原因只能是 `false_positive`、`expected_activity`、`resolved_without_action`、`other` |
| `apply_temporary_rate_limit` | `duration_minutes`，以及至少一个限流字段 | 只允许 5..1440 分钟；`rpm_limit/tpm_limit/concurrency_limit` 各为 `1..100000000`；必须至少实际收紧一个维度 |
| `release_temporary_rate_limit` | 无 | 只释放该 anomaly 当前仍有效的覆盖；anomaly 保留 `mitigated` 历史状态 |

除表中字段外，其他可选字段必须省略，不能用 `null` 为无关动作占位。租户级异常不能应用
临时限流；Virtual Key 已禁用、删除、跨 Environment、处置窗口过期、已有有效覆盖或建议值
不能收紧当前有效限制时均返回稳定 409，且不产生 action、override 或成功 Audit。

应用临时限流示例：

```http
PATCH /cost-anomalies/can_0123456789abcdef0123456789abcdef/action
Authorization: Bearer <console-session-token>
Content-Type: application/json; charset=utf-8
Idempotency-Key: cost-anomaly-action-0001

{
  "action": "apply_temporary_rate_limit",
  "duration_minutes": 30,
  "rpm_limit": 10,
  "tpm_limit": 100000
}
```

成功返回 HTTP 200，正文包含更新后的 `cost_anomaly`、append-only `action`、当前
`temporary_rate_limit_override`、`idempotent_replay` 及本次 `request_id/trace_id`。

幂等范围为当前 `tenant + actor + Idempotency-Key`。服务端只保存幂等键 SHA-256，不保存或
记录原始键；同一键和完全相同意图返回首次结果并置 `idempotent_replay=true`，但使用本次
调用链 ID。同一键改 path、动作或参数返回 `409 cost_anomaly.idempotency_conflict`。

## 5. 统一错误码

| HTTP | 错误码 | 语义 |
| ---: | --- | --- |
| 401 | `auth.token_missing`、`auth.token_invalid`、`auth.token_expired`、`auth.session_revoked` | Session 认证失败 |
| 403 | `auth.permission_denied` | developer/viewer 无后端权限 |
| 404 | `cost_anomaly.not_found` | anomaly、筛选 Environment/Key 不存在或跨租户；同形防枚举 |
| 415 | `cost_anomaly.media_type_invalid` | 缺失、重复或非 UTF-8 JSON Content-Type |
| 422 | `cost_anomaly.query_invalid` | 未知/重复 query、非法筛选、时间、limit 或 cursor |
| 422 | `cost_anomaly.action_invalid` | 非法 Header、正文、字段组合、时长或限流值 |
| 409 | `cost_anomaly.action_conflict` | 动作与当前 anomaly 状态冲突 |
| 409 | `cost_anomaly.idempotency_conflict` | 幂等键已绑定另一意图 |
| 409 | `cost_anomaly.mitigation_not_applicable` | 目标不是可临时限流的 Virtual Key 异常 |
| 409 | `cost_anomaly.mitigation_active` | 该 anomaly 已有有效临时覆盖 |
| 409 | `cost_anomaly.mitigation_expired` | 24 小时处置窗口已过期 |
| 409 | `cost_anomaly.mitigation_key_unavailable` | Key 已禁用、删除或 Environment 归属已变化 |
| 409 | `cost_anomaly.mitigation_no_effect` | 建议值没有实际收紧任一维度 |
| 409 | `cost_anomaly.mitigation_not_active` | 待释放覆盖已释放或过期 |
| 503 | `cost_anomaly.unavailable` | 查询或动作依赖的数据库/迁移暂不可用 |
| 503 | `cost_anomaly.detector_unavailable` | 异步检测或 source outbox 暂不可用 |

错误正文只包含统一安全信息与调用链 ID，不返回 SQL、数据库路径、原始异常、URL、远端响应
或凭据内容。`retryable` 以统一错误注册表为准。

## 6. `cost.anomaly_detected` Webhook

首次持久化 CostAnomaly 时生成稳定 `can_...` 对象 ID 和 `caevt_...` event ID。检测事务只写
source outbox，不执行网络请求；提交后由独立 worker 投递，合同是稳定 event ID 的
at-least-once，不承诺网络 exactly-once。

事件 envelope 的 `actor_id` 固定为 `null`，`object_id` 等于 anomaly ID，`trace_id` 来自检测
事实。payload 必须精确包含以下 20 个字段：

```text
anomaly_id, rule_type, severity, status, environment_id,
scope_type, scope_id, window_start, window_end,
current_cost_micros, baseline_total_micros, observed_ratio_millis,
baseline_bucket_count, threshold_multiplier_millis, minimum_cost_micros,
source_entry_count, estimated_entry_count, affected_virtual_key_count,
recommended_action, detection_version
```

payload 不包含 Key 名称/前缀、代表 request/trace、Project/Application 名称、Provider Key、
Authorization、Prompt、Response 或错误正文。共享校验器会重新计算规则映射、严重级别和阈值
关系，额外或缺失字段都会整体拒绝。

outbox 对已有 original delivery 使用显式 pending resume：复用持久正文和 `max_attempts`，从
`attempt_count + 1` 继续；每次尝试前重读 Endpoint 与当前 Secret 版本。Endpoint 已 disabled
时不发网，pending delivery 以固定错误终结。远端在本地确认前已经收到请求时可能再次收到
相同 event ID，下游应按 `event_id` 幂等消费。

## 7. 当前验收状态

共享事件/错误/配置合同、pending resume 和既有 Webhook 相邻回归已有直接通过记录；0043
完整迁移、Control GET/PATCH 全面专项、检测器/Gateway/Console、全量回归、真实三服务
Runtime 与双视口 Browser 尚待最终验收。权威执行状态见
[PRD-053 测试记录](../testing/PRD-053-tests.md)，不得根据本文档或代码文件存在推断 PRD 已完成。
