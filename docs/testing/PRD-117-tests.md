# PRD-117 测试记录

状态：已完成。数据驻留、保留策略、法律保全、删除调度、签名分发、消费者失败关闭、Console、
本地真实认证和敏感审计均已通过。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| Migration | `0108 -> 0109 -> 0108 -> 0109`、fresh head、重复执行、复合 owner、不可变目录、删除清单/trigger hash、业务事实 down guard |
| Governance API | Workspace Cookie/CSRF、权限矩阵、If-Match、幂等 HMAC、simulation/confirmed apply、签名快照同事务、KMS 回滚、低敏 evidence 分页 |
| Residency consumers | Gateway placement/runtime、backup claim、DR failover/restore、PRD-106 job、PRD-089 tenant deletion、缺失或过期策略失败关闭 |
| Retention and hold | 六类完整策略、到期调度偏差、checkpoint、hold/release、重试、hold/delete generation fence 与并发序列化 |
| Console | Workspace 管理与只读投影、Platform evidence 筛选/分页、simulation 后确认、Legal Hold 创建/释放、移动端保留策略行布局 |
| Compatibility | PRD-089/094/106/108/110..113、Workspace Session 恢复与并发 touch、AuthContext 和 Console navigation |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-117 Migration + API | `14/14 passed` |
| PRD-089 tenant data | `2/2 passed` |
| PRD-106 job + PRD-108 distribution | `13/13 passed` |
| PRD-110..113 消费者与会话回归 | `22/22 passed` |
| AuthContext / navigation / Workspace switch | `3/3 passed` |
| Gateway config runtime | `4/4 passed` |
| PRD-117 Console 专项 | `5/5 passed` |
| Console 全量 | `1098/1098 passed` |
| Console lint | `360 files` 通过 |
| Python Ruff | `All checks passed` |

## 策略与消费者证据

- `confirmed=false` 只返回 candidate SHA-256、下一 generation、placement 合规状态和迁移组件，业务表计数不变；
  缺少 Gateway placement 时固定返回不合规，并列出 `gateway` 与 `disaster_recovery`。
- `confirmed=true` 在同一事务中新建 active residency/retention 版本、推进 fence、发布 PRD-108 签名 manifest，
  并在 KMS/投影失败时完整回滚。Gateway runtime 兼容 legacy manifest，同时严格拒绝畸形或过期治理投影。
- Gateway、backup 和 DR 在 region/group/generation 不一致时失败关闭并写低敏违规证据；PRD-106 deletion job
  校验 owner/policy/hold generation，以 checkpoint 和幂等 effect 生成删除终态。
- 到期 scheduler 的 due 偏差小于 24 小时；策略判断 250 次样本的 p95 小于 10ms。Legal Hold 创建与
  tenant deletion claim 争用同一 `BEGIN IMMEDIATE` fence，不会形成“保全已生效但数据已删除”的状态。

## 本地认证与 Browser 终态

- 自动化和真实 Console 直接从本地 seed/config 读取测试管理员凭据，值只在内存中使用；没有再次请求人工确认，
  也没有把密码、TOTP、Cookie、CSRF、Token 或 grant 写入命令、日志、截图、文档或提交。
- 真实 Workspace 页面完成策略 simulation 与 generation `2` apply；Legal Hold 创建后显示 `1 active`，
  释放后显示 `0 active · 1 历史`。Platform 页面完成 Workspace/component/outcome evidence 筛选，
  页面只显示低敏 component/region/policy/outcome/watermark 信息。
- 当前 API 验收批次共 `28` 个 `200`、`1` 个 `201`，无 `4xx/5xx`；浏览器 console warning/error 为 `0`。
- `390x844`、`1280x720`、`1440x900` 三档均为页面横向溢出、治理控件越界、文本裁切、控件重叠和
  broken image `0`。390px 首轮发现的保留策略控件越界已改为带字段标签的单列行并复验通过；临时 viewport 已复位。
- 当前独立交付完成真实 Chromium 验收；Edge/Firefox 跨浏览器与批次级 G4 仍按计划保留到 PRD-119 独立提交后统一执行。

## 敏感与清理审计

- PRD-117 源码/文档、临时 API 日志和隔离数据库中的本地 secret 值命中均为 `0`。
- Private Key block、AWS/OpenAI 风格高风险访问密钥和长 Bearer 字面量命中均为 `0`。
- evidence 响应和 DOM 不含 Prompt、Response、文件路径、对象 key、Provider Key、Token 或租户业务正文。
- 临时 Control API 已停止；所有名称含 `prd117` 的 pytest、诊断和临时服务目录均已精确删除，
  未删除其他 PRD 的测试目录或 PRD-118/119 草稿。

## 批次门禁

PRD-117 不执行 S4 G4。Edge/Firefox、完整 S4 业务流与批次级数据库、性能、安全和必要回归门禁保留到
PRD-119 独立提交完成后统一执行；本条仍保持独立设计、测试记录、敏感审计和提交。
