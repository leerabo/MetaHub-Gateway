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
