# PRD-109 SaaS 可观测性、SLO 与错误预算

状态：设计、两轮编码前自审、实现与专项验证完成（2026-08-24）。依赖 PRD-106..108，范围以 Excel PRD-109 为准。

## Excel 权威合同

以下名称与目标值按 Excel 原文冻结，内部 SQL 表名可以不同，但公开 DTO、测试映射与交付证据必须逐项对应：

- API：`GET /platform/observability/services`、`GET/POST /platform/slos`、`GET /platform/error-budgets`、
  `POST /platform/alerts/{id}/ack`。
- 核心数据对象：`ServiceCatalogEntry`、`ServiceLevelObjective`、`ErrorBudget`、`Alert`、`OnCallRoute`、`TelemetryPolicy`。
- 事件：`slo.breached`、`error_budget.exhausted`、`alert.triggered`、`alert.resolved`。
- 性能/SLO：`关键告警检测p95<60s`、`仪表盘p95<1s`、`遥测额外CPU<5%`。

## 1. 目标与边界

交付服务目录、黄金信号、Metrics/Logs/Traces 关联、低基数租户脱敏维度、SLO/SLI、错误预算、告警去重/抑制/恢复、
值班路由和审计证据。遥测不得采集 Prompt、Response、Provider Secret、Token、客户业务正文或自由异常文本。
遥测后端故障不得阻塞 Gateway 热路径；本条不建设通用日志检索或第三方值班供应商管理。

## 2. 0101 数据模型

- `service_catalog_entries`：稳定 service code、owner team、tier、黄金信号目录、状态和 revision。
- `service_level_objectives`：service/SLI/window/target/burn thresholds、状态和 revision；定义不可原地覆盖，更新生成新版本。
- `sli_time_buckets`：固定时间桶、service/region/status class、可选轮换 tenant pseudonym、good/total 和 latency histogram。
- `error_budgets`：SLO version/window、allowed/consumed/remaining、burn rates、computed watermark/hash。
- `platform_alerts`：dedupe key、severity、state `firing|suppressed|resolved`、first/last seen、revision。
- `alert_acknowledgements` 与 `on_call_routes`：不可变操作员事实和固定目的地引用；不保存通知地址明文。
- `telemetry_policies`：允许标签、采样、基数上限和 drop 行为；tenant-owned 策略必须有复合 owner。
- 权限 `platform.observability.read/manage/security_read/alert_ack`：owner/ops 管理；security 仅安全分类聚合；其他平台角色拒绝。
- down 对 SLO、bucket、budget、alert、ack 业务事实拒绝；历史观测不静默删除。

## 3. 遥测合同与计算

Gateway/Control 只发 exact `TelemetryFact`：service、operation、region、status class、duration bucket、request/trace 摘要和
可选租户 pseudonym。标签 allowlist 在进程内预编译；未知/高基数字段丢弃并增加固定 drop counter，不序列化其值。
request_id/trace_id 只用于授权后的链路关联，平台聚合 API 不返回租户请求级明细。

热路径采用有界无等待 enqueue；队列满或指标后端故障时丢遥测、记录安全计数并继续业务，日志也不得打印原始 fact。
PRD-106 job 按水位聚合 bucket、重算 SLO/error budget、评估 burn rate 并驱动 alert 状态机；同水位/hash 重放幂等。

告警以 dedupe key 合并，maintenance/suppression 只改变通知状态，不改 SLI；恢复必须追加 resolved 事实。错误预算耗尽
只为 PRD-107 已冻结的 `RolloutHealthSummary` 合同提供规范签名聚合，不改变 107 的消费者合同，也不直接授予/撤销租户权限。

## 4. API 与事件

- `GET /platform/observability/services`：服务目录与黄金信号健康；无客户标识。
- `GET/POST /platform/slos`：严格 SLO version DTO、CSRF/idempotency/CAS；不接受查询表达式或任意标签。
- `GET /platform/error-budgets`：固定 window/service 过滤和签名游标；返回聚合预算与水位。
- `POST /platform/alerts/{id}/ack`：当前 permission、revision、固定 reason，追加 ack 而不修改历史。
- 事件 `slo.breached|error_budget.exhausted|alert.triggered|alert.resolved` 从 outbox 恢复，只含服务、SLO、严重度和时间。

## 5. Console、测试与 SLO

Platform `observability` 页面显示服务目录、黄金信号、SLO、错误预算和告警时间线。security 只见安全分类，ops 可管理，
support/tenant 不能进入平台聚合。移动端使用稳定摘要行；图表提供表格等价文本和可访问状态。

专项覆盖 label allowlist、基数上限、敏感 canary 丢弃、SLO 重算、窗口边界、告警 dedupe/suppress/resolve、遥测故障不阻塞、
水位重放、角色裁剪、迁移往返和浏览器。门禁：告警检测 p95<60s、仪表盘 p95<1s、遥测 CPU 增量<5%。

## 6. 第一轮自审：正文与高基数

- P0：通用 attributes map 会把正文/Secret 带入指标。修正为编译期 exact fact 和标签 allowlist，未知值不记录。
- P0：平台 API 返回 request 级 tenant 标签会跨域泄漏。修正为聚合预算和轮换 pseudonym，禁止反查。
- P1：trace 关联保存完整 URL/query 会泄漏。修正为 operation code 与安全 request/trace id，不保存 URL/query。
- P1：遥测同步写数据库会拖慢 Gateway。修正为有界无等待 enqueue 和显式 drop counter。
- P1：自由 SLI 查询语言会产生注入和不可复算。修正为固定 service/operation/status/latency 模板。

## 7. 第二轮自审：预算、告警与发布

- P0：指标故障若阻断 Gateway 违反权威边界。修正为业务 fail-open、遥测自身降级；安全/账务真值仍走原系统。
- P1：同一故障生成告警风暴。修正为 dedupe key、状态 CAS、suppression window 和 resolved 终态。
- P1：maintenance 从分母剔除会美化 SLO。修正为 SLI 不变，只抑制通知并显式标注窗口。
- P1：PRD-107 直接读取原始指标会跨域。修正为只消费签名 SLO/error-budget 聚合。
- P1：ack 覆盖 alert 会丢失操作员事实。修正为不可变 acknowledgement 与 alert revision 分离。

## 8. 批次交叉审查

- 第一轮（范围/owner/权限）：平台仅返回低基数聚合，security/ops/tenant 使用独立裁剪投影；request/trace 只关联授权链路，
  不提供租户请求级明细或客户正文。
- 第二轮（迁移/依赖/失败策略）：0101 对 SLO/bucket/budget/alert/ack 事实执行 down guard；109 依赖 106..108 并接管既有
  `RolloutHealthSummary` 生产，不形成反向依赖；遥测丢弃或后端故障不阻塞 Gateway，安全/账务真值不随之降级。

## 9. 编码前复核冻结

- 遥测写入使用进程内有界队列；后台每次 flush 先推进数据库全局单调 ingest watermark，再以该 watermark 写入不可变、
  低基数 bucket 批次。聚合 Job 绑定 watermark 与规范窗口结束时间，重放只能得到同一 source hash 和预算结果。
- `TelemetryFact` 的 operation、region、status class 与 duration bucket 均来自编译期枚举；request/trace 只在进程内转换为
  域隔离摘要用于关联，平台 bucket、预算、事件与 API 均不保存或返回请求级标识。
- SLO 定义不可变，head 以 CAS 指向新版本；计算只使用整数 count/bps，不使用浮点预算。告警状态、不可变 transition 与
  acknowledgement 分表保存，ack 不改变 alert revision 或历史。
- `platform_security` 通过 `platform.observability.security_read` 获取固定安全裁剪投影；它不继承 ops 管理或 alert ack。
  Console 根据 read/security_read、manage、alert_ack 三类能力分别裁剪页面、创建表单和告警操作。
- Excel 未定义独立告警列表接口，因此当前告警时间线作为 `GET /platform/error-budgets` 的 exact `alerts` 子投影返回；不新增
  隐式平台 API。`OnCallRoute` 只保存固定目的地引用摘要，不保存邮件、电话、Webhook 地址或供应商凭据。
- PRD-109 写入 `rollout_health_summaries` 时复用 PRD-107 已冻结的 canonical body 与 HMAC domain，消费者合同和失败关闭语义
  不变；只有已验证的聚合预算成为规范生产来源。
