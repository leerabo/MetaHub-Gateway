# PRD-109 测试记录

状态：SaaS 可观测性、SLO、错误预算、告警状态机、双应用遥测和 Platform Console 已完成专项实现与验证。每次 pytest 均使用新的显式 `--basetemp`，未复用或删除任何 `.pytest-*` 目录。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0100 -> 0101`、fresh、pristine down/up、历史遥测事实阻断 down、权限矩阵、聚合 Job 激活、完整性与外键 |
| Telemetry | 固定 service/operation/region/status/duration 维度、request/trace 域隔离摘要、未知维度丢弃、有界 `put_nowait`、队列满和后端故障不阻断 |
| SLO/API | PlatformSession、CSRF、权限、closed DTO、SLO 不可变版本、head CAS、幂等重放与冲突、固定查询和签名游标 |
| Aggregation | 单一 ingest watermark、固定分钟窗口、整数 good/total/allowed/consumed/remaining/burn、相同水位重放、10 秒调度轮询幂等 |
| Alerts | dedupe、路由抑制、恢复 transition、追加 ACK、ACK 不改 alert revision/history、固定 outbox 事件 |
| PRD-107 handoff | 复用冻结 canonical body 与 `prd107/health-summary/v1` HMAC domain 生产 `rollout_health_summaries` |
| Role projection | owner/ops read/manage/ack，security 仅 `security_read` 固定投影，冲突凭据和租户查询参数拒绝 |
| Console | strict DTO、Cookie/CSRF/CAS/idempotency、read/manage/ack 独立裁剪、服务/SLO/预算/告警、可访问预算文本、窄屏标签行、无 Storage override |

## 专项结果

| 检查 | 结果 |
| --- | --- |
| PRD-109 Migration | `3 passed` |
| PRD-109 API/runtime | `4 passed` |
| Console focused | `5 passed` |
| Console lint | `341 files` 通过 |
| PRD-109 Python Ruff | 候选 Python 文件 `All checks passed` |
| Python compileall | PRD-109 涉及源码树通过 |
| `git diff --check` | 通过 |
| 新增内容敏感字面量扫描 | 无真实凭据、私钥或生产 Token 命中 |
| PRD-095 real PlatformSession/navigation 回归 | `1 passed` |
| PRD-107/108 Console 回归 | `10 passed` |

批次级全数据库、完整集成/流程、真实浏览器、性能和安全回归按约定在 PRD-113 完成后统一执行。PRD-109 的性能门限在批次门禁中验证：告警检测 p95 `<60s`、仪表盘 p95 `<1s`、遥测 CPU 增量 `<5%`。

## 验证结论

- Gateway 和 Control 热路径只执行固定维度校验、域隔离摘要与有界无等待入队；遥测存储故障只推进固定计数。
- request/trace 摘要仅在进程内 fact 存活，bucket、预算、API、事件和 Console 均无请求级标识或客户正文。
- SLO 定义不可原地覆盖，head 只通过 CAS 单调推进；创建与追加版本均绑定 PRD-106 加密 Job。
- 聚合只读取绑定水位前的不可变 bucket，并用整数计算错误预算；相同水位与窗口重放得到相同 source hash。
- 告警状态、不可变 transition 和 acknowledgement 分离；抑制不改变 SLI，恢复追加 `resolved` 事实。
- PRD-107 只消费签名聚合健康摘要，既有消费者 canonical body、HMAC domain 和失败关闭语义不变。
- Console 的安全角色只看到 security 投影，manage 与 alert ACK 控件按独立权限裁剪，移动端保留表格等价文本。

## G6 HTTP 租户归属补充 (2026-09-07)

实际缺口：此前 HTTP 中间件从未把服务端已认证租户交给 `TelemetryBuffer`，即使真实请求属于
不同租户，落盘 `tenant_pseudonym` 仍是 `none`。本次从 Control 共用资源鉴权的成功分支绑定
request-local owner，中间件只把该值转为 HMAC 伪名。请求 Header、匿名标签和撤销 Cookie 不补权。

- `prd109-telemetry-unit-r1-junit.xml`：8/8，覆盖无效/冲突绑定永久清除、同主体幂等绑定、请求隔离、
  标签注入不生效，以及存储故障不改变业务 200 且计入 drop/backend failure。
- `prd109-telemetry-auth-r1-junit.xml`：1 失败，测试在应用首个请求之后安装中间件，被框架拒绝。
  修正夹具生命周期，不改鉴权和产品限制；r2 为 1/1。最终带明确请求阶段截止的 r3 同样 1/1。
- `prd109-legacy-auth-r1-junit.xml`：2/2，11 未选。真实 Legacy 登录、文件上传、外租户读取隔离、
  multipart 前权限检查及既有 Viewer 上传权限均保持。
- PRD-137 共用资源链的真实 HTTP 与完整 HTTPS Guest 各 1/1；真实后台工作器而非直接 enqueue
  驱动两个指标窗口，分别精确对账 6 个标签请求与 3 个旧/当前会话请求。结果归属见 PRD-137-tests。

证据保存在 `.artifacts/g6-security-20260907/`。独立 PRD-109 HTTP 测试另验证 8 个请求：source
成功 2/拒绝 1、target 成功 1、匿名/旧会话拒绝 3、空间切换成功但不带租户归属 1；无掉数或后端失败。
跨租户拒绝的请求只记入已认证 source，原始 tenant ID、Cookie、密钥和 canary 均不进入 bucket。
不扩张为 Gateway/全部独立鉴权入口、全域性能或 G6/G8 验收。未重跑无关全套测试，批次完整门禁仍待完成。
独立敏感审计为 `prd-109-http-telemetry-sensitive-audit.json`，核对精确暂存文件/字节、严格 UTF-8
和高置信凭据模式，原始成功/失败证据均保留；Ruff 与 `git diff --check` 通过。
