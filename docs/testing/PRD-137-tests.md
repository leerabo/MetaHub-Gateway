# PRD-137 测试记录

状态：已完成。0129、固定场景隔离 runner、assessment/finding/remediation/risk acceptance 闭环和 Platform
Security Assurance Console 已交付；本条保留独立设计、聚焦测试、敏感审计和提交。下一权威项为
`PRD-138 - SaaS发布候选、核心迁移波次与全产品门禁`。

## 专项范围

| 测试组 | 覆盖 |
| --- | --- |
| 0129 Migration | `0128 -> 0129 -> 0128 -> 0129`、fresh/current head、重复执行、Platform/Workspace 权限、assessment/run/finding/remediation/risk/cleanup/event/outbox/action、复合 owner、完整性与业务事实 down guard |
| Security Assurance API | 精确三路由闭集、Cookie/CSRF/Origin/step-up/CAS/幂等、签名 profile、10 个固定场景、任意 URL/脚本/selector 拒绝、owner 漂移同形拒绝、缺失 runner 失败关闭、finding/remediation/独立复验、风险接受双批与过期 |
| Runner 与 scheduler | 短 lease、permission revision 恢复、接管 fencing、迟到结果拒绝、exact fixture cleanup、事件/outbox 消费和静止终态 |
| Console | strict DTO、assessment/finding owner 校验、profile/digest 绑定、run/finding/SLA/remediation/cleanup 裁剪投影、Abort/epoch/revision、动态撤权、无敏感持久化和响应式布局 |

## 自动化结果

| 检查 | 结果 |
| --- | --- |
| PRD-137 Migration | `4/4 passed`；0129 fresh/往返、跨 owner 子事实拒绝和业务事实 down guard |
| PRD-137 API/服务 | `7/7 passed`；缺失 runner 失败关闭、固定 runner/幂等/任意输入拒绝、remediation 新 generation 独立复验、风险接受双批/重复申请拒绝/过期、permission revision、lease takeover fencing 和三路由闭集 |
| PRD-137 Console | `5/5 passed`；固定路由与写 header、strict DTO/owner、裁剪渲染、step-up/幂等重试/撤权和禁止持久化 |
| JavaScript/static | Console lint `console lint ok (411 files)` |
| Python static/format | PRD-137 后端、受影响权限服务、运行时错误目录和聚焦测试 Ruff 全规则、compileall 通过 |
| Diff hygiene | `git diff --check` 通过 |

## 两轮实施自审

- 第一轮复核 runner 信任来源、owner registry、路由闭集、finding 证据与复验。未配置 runner 不再伪造 passed，
  而是 10 个场景全部生成 `error/P0` 并使 gate 失败关闭；0129 为所有子事实补齐复合 owner 外键或 owner trigger；
  风险接受保持内部服务，不扩张 Excel 外部 HTTP 合同。修正后无未关闭 P0/P1。
- 第二轮复核 lease、动态撤权、重复审批、前端 owner 漂移和可观测摘要。runner lease 使用 profile 的
  `1800/14400` 秒上限；closed finding 或已有 pending/active 接受时拒绝重复申请；Platform client 严格校验
  assessment Workspace/profile/digest、GET ID、finding/run/remediation owner；cleanup 指标按数值计数解析。
  修正后无未关闭 P0/P1。

## 敏感审计与临时证据

- 正式结论只保存在本文和 Git；pytest basetemp、SQLite 副本、缓存与过程输出均为可重现临时证据，`.tmp`
  可整体删除，不作为正式交付事实。
- 审计不得输出密码、Token、Cookie、client secret、TOTP、step-up grant、签名值、私钥、Provider Key、
  exploit 正文、客户正文、对象 URL 或本地管理员凭据；本地测试凭据只在进程内使用，不打印或持久化。
- Console DTO/DOM/Storage 只保留固定 code、digest、裁剪摘要和计数，不投影 runner subject、原始请求/响应、
  内部 endpoint、生产 selector 或 fixture 内容。
- 未跟踪的 PRD-101..105 用户文档不进入 PRD-137 暂存集；最终暂存只包含本条实现、测试、文档和路线图事实。

## 批次门禁

本条按批次规则只运行 0129、API/服务、Console 和静态专项。`0123 -> 0130` 全数据库矩阵、集成、FLOW、
真实三浏览器三缩放、性能、安全、代理/断网、Windows/Linux、SQLite/PostgreSQL、N/N-1、四波试点、灾备、
支付/Edge 演练与 72 小时长稳统一留到 PRD-138 独立提交后执行。G6 通过也只能形成
`release_candidate`，不能公开 GA。
