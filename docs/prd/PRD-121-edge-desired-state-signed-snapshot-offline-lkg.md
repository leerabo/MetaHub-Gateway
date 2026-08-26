# PRD-121 云边期望状态、签名快照与离线 LKG

状态：设计冻结，尚未编码；计划迁移 0113。依赖 PRD-108/120，范围以 Excel PRD-121 为准。

## Excel 权威合同

- API：`POST/GET /edge/deployments/{id}/desired-state`、`POST /edge/deployments/{id}/acks`、`POST /edge/deployments/{id}/rollbacks`。
- 对象：`DesiredState`、`SignedSnapshot`、`SnapshotArtifact`、`ApplyAttempt`、`EdgeAck`、`LastKnownGood`。
- 事件：`edge.desired_state_published`、`edge.apply_failed`、`edge.state_converged`、`edge.rollback_completed`。
- SLO：在线传播 p95 < 60s；本地读取 p95 < 2ms；失败回滚 < 5 分钟。

## 1. 产品闭环

Workspace admin 从当前 Workspace 配置生成 Desired State；高危字段先完成非自批审批。Control 以 deployment、generation、base generation、有效期和内容摘要发布用途分域签名快照。Agent 用 PRD-120 mTLS 拉取，先验签/owner/版本/预检，再写临时槽并原子切换；成功 ACK 后更新实际水位和 LKG。失败保持当前 active slot、记录固定失败码并可显式回滚。断网只使用已经完整验签的有界 LKG，页面标明期望/实际/陈旧差异。

## 2. 0113 数据与协议

- Desired State 版本不可变，head 单行 CAS 指向当前 generation；快照和 artifact 绑定 deployment/workspace/tenant。
- manifest 采用 canonical JSON exact schema，包含 schema/version vector、deployment、generation、base generation、revocation watermark、artifact hashes、validity、policy/provider secret reference，禁止 Secret 或正文。
- ApplyAttempt、EdgeAck 和 LKG observation 使用单调 generation；同 generation 不同 hash 是完整性冲突。
- 复用 `config_distribution` 的 canonical/签名/原子 owner 和 LKG 模式，但使用私有 Deployment owner、持久双槽和断网策略，不能把 SaaS Cell identity 当 Edge identity。

## 3. 权限与失败策略

Workspace admin 发布本 Workspace 状态；release_manager 只能提供已审批平台制品版本，不能替租户选择 Provider/Secret；Agent 只读自身 artifact 并追加 ACK；platform_ops 只读 Fleet 水位。网络不可达可进入有界 LKG，签名、hash、owner、schema、过期或乱序失败立即隔离且不回退。

Console 使用“期望/实际/LKG”三段状态和差异字段，不显示 raw manifest；发布、回滚需要 If-Match、Idempotency-Key、原因和 step-up。Agent ACK 只含 generation/hash、固定 status/reason、耗时和时间。

## 4. 第一轮自审：签名与原子应用

- P0：在验签前解包/预检可触发不可信内容。修正为先校验 envelope、owner、hash、签名和大小，再解析受限 artifact。
- P0：逐字段覆盖 live 配置会暴露部分应用。修正为 staging slot 全量预检后原子交换引用。
- P0：签名正确但目标 Deployment 错误仍可控制节点。修正为签名内容绑定 deployment/workspace/tenant 和用途域。
- P1：租户配置与平台 release 混为一个作者。修正为分别恢复 author domain，最终 manifest 显式引用两种已授权版本。

## 5. 第二轮自审：LKG 与冲突

- P0：完整性失败降级 LKG 会掩盖攻击。修正为只有可证明 transport outage 可用 LKG；完整性失败隔离。
- P0：断网双向更新使用 last-write-wins 会丢事实。修正为 base generation/version vector，冲突保持并交由 PRD-130 人工决策。
- P1：乱序 ACK 回退 actual generation。修正为单调水位，迟到 ACK 仅保留证据、不改变 head。
- P1：回滚重新签旧包会混淆历史。修正为生成新的递增 generation，内容引用旧 LKG hash。

两轮均为修正后通过，无未关闭 P0/P1。

## 6. 专项验证计划

0113 迁移；错误/过期/乱序/错误 owner/同 generation 异 hash；网络中断与完整性故障区别；原子切换、崩溃恢复、ACK 幂等、回滚生成新 generation；动态撤权/审批失效；三浏览器差异和恢复入口；传播/本地读取/回滚 SLO；manifest、日志、DB、事件敏感扫描和静止终态。
