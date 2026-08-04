# Provider 状态页 API

本文档冻结 PRD-061 内部 Provider 状态页的 HTTP 合同。状态页是租户内只读治理视图，聚合
Provider 元数据、真实调用终态和健康探测事实；它不执行现场探测，不接受客户端租户或统计窗口，
也不返回 Provider Key、认证 Header、Prompt、Response 或上游错误正文。

## 1. 认证与权限

`GET /status/providers` 必须携带有效 Control Plane Bearer Session。后端在每次请求从当前数据库
权限快照同时校验：

- `provider.health.read`
- `request_log.read`

缺少任一权限均返回 `403 auth.permission_denied`。租户只能来自认证上下文；客户端传入
`tenant_id` 会作为非法筛选拒绝。前端导航或按钮可见性不构成授权依据。

## 2. GET /status/providers

### 2.1 查询参数

| 参数 | 默认值 | 合同 |
| --- | --- | --- |
| `provider_id` | 无 | trim 后 1..128 字符；跨租户 ID 返回合法空集合 |
| `provider_type` | 无 | 现有 Provider 类型固定枚举 |
| `health_status` | 无 | `healthy/unhealthy/observing/unknown/disabled` |
| `provider_status` | 无 | `active/disabled` |
| `limit` | `50` | 规范 ASCII 十进制整数，范围 1..100 |
| `offset` | `0` | 规范 ASCII 十进制整数，范围 0..10000 |

未知或重复参数失败关闭。`limit/offset` 不接受正负号、空白、小数、指数、布尔文本或 Unicode
数字，避免代理、框架和客户端产生不同解析结果。请求不得携带统计窗口、Endpoint、Header、Key
或原始错误筛选。

### 2.2 响应

成功响应设置 `Cache-Control: no-store`，并使用统一链路字段：

```json
{
  "provider_statuses": [],
  "summary": {
    "total": 0,
    "healthy": 0,
    "unhealthy": 0,
    "observing": 0,
    "unknown": 0,
    "disabled": 0
  },
  "window_started_at": "2026-07-31T00:00:00Z",
  "window_ended_at": "2026-08-01T00:00:00Z",
  "history_started_at": "2026-07-25T00:00:00Z",
  "total": 0,
  "limit": 50,
  "offset": 0,
  "request_id": "req_example",
  "trace_id": "trc_example"
}
```

Provider 排序固定为 `unhealthy -> observing -> unknown -> healthy -> disabled`，同状态按
`provider_id ASC`。筛选先于 summary/total，分页最后执行；指标只聚合当前页最多 100 个 Provider。

### 2.3 单项字段

单项包含以下信息：

- 脱敏元数据：`provider_id`、`tenant_id`、code、名称、类型、去除 userinfo/query/fragment 的
  `base_url`、`auth_type`、配置状态和维护备注。
- 健康与可用性：原始、effective、新鲜度和固定可用性映射。前端不得根据成功率再次推导状态。
- 真实调用：terminal、eligible、success、Provider failure、Credential failure、excluded 六项计数，
  Provider 归因成功率、非流式调用 P95 和流式首块 P95。
- 探测：按同一次持久探测事务完整聚组后的 success/failure/inconclusive、确定性成功率和独立 P95。
  内部分组使用 `provider_id + request_id + health_generation + created_at`；后两项不进入公开 DTO。
- 排障：最近调用/探测时间、最近故障和七天内最多五条安全故障投影。

成功率以万分比整数返回，使用 half-up；无合格分母返回 `null`，不能解释为 0%。三个 P95 使用
独立样本集和 nearest-rank，空样本返回 `null`。所有 JSON 整数不得超过 JavaScript 安全整数。

故障投影只含来源、归因类型、是否进入成功率、固定错误码、request/trace ID、非负延迟和观测时间。
底层 attempt/session/probe 主键仅参与稳定排序，不进入响应。

## 3. Provider 维护备注

PRD-061 不新增状态页写接口。维护备注复用：

- `POST /providers`
- `PATCH /providers/{provider_id}`

写操作继续由后端 `provider.write` 校验并从 Session 恢复 tenant/actor。`maintenance_note` 非空时
trim 后最多 240 个 Unicode 字符且不能含 C0/C1 控制字符；PATCH 省略表示不修改，显式 `null` 或
空白表示清空。响应可以向授权用户返回原文，但 AuditLog/Webhook/logger 只能保存 SHA-256 或完全
不记录原文。

## 4. 页面访问事件

PAGE-088 复用 `POST /console/events`：

- `event_type=status.viewed`
- `navigation_item_id=provider-health`
- `target_section=provider-health`
- `path=/#provider-health`
- payload 仅允许固定 `source/hash/viewport_class`

筛选、Provider ID、Endpoint、错误码和指标不得进入事件。该事件只写内部页面事件表，不派生
Webhook；页面 shell 成功授权后即可记录，不依赖状态 API 返回 200。

## 5. 统一错误

| 错误码 | HTTP | 场景 |
| --- | ---: | --- |
| `auth.token_missing` / `auth.unauthorized` | 401 | 缺少、失效或已撤销 Session |
| `auth.permission_denied` | 403 | 缺少状态页双权限任一项 |
| `provider_status.filter_invalid` | 422 | 未知、重复或非法业务筛选 |
| `provider_status.pagination_invalid` | 422 | 非规范、重复或越界分页参数 |
| `provider.maintenance_note_invalid` | 422 | 维护备注长度或控制字符非法 |
| `provider_status.query_unavailable` | 503 | Schema、连接、时间、整数或事实合同无法安全解释 |

错误响应使用平台统一 envelope，并始终携带非空 `request_id` 和 `trace_id`。503 不返回数据库路径、
SQL、表名、异常正文或输入原值。

## 6. 安全边界

- 所有 Provider、调用、流和探测查询以认证 `tenant_id` 为首条件；跨租户精确 ID 返回空集合。
- 元数据、调用和探测在同一只读事务及同一个 UTC `now` 下聚合，GET 不写业务数据、不发网络请求。
- `request_id/trace_id` 允许调用方透传，Provider 状态不变化时健康代次也可能不递增；状态页使用同一探测事务
  共享的 `created_at` 区分重复链路标识，同时继续校验组内 trace 一致和 attempt_sequence 唯一。
- 未知错误码、控制字符、坏时间、负延迟、计数恒等式损坏或超出安全整数时整个查询返回统一 503。
- PAGE-088 的异步结果必须绑定身份、权限 revision/fingerprint、导航 owner、Token、canonical filter
  key、generation 和 AbortController；迟到成功或错误不得跨登录态、租户或筛选写入 DOM。
