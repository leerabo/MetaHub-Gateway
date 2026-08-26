# PRD-121 云边期望状态、签名快照与离线 LKG

状态：已完成；迁移 0113、云边签名快照、非自批审批、Agent 原子应用/ACK、持久 LKG 和双域 Console
均已交付。依赖 PRD-108/120，范围以 Excel PRD-121 为准。

## Excel 权威合同

- API：`POST/GET /edge/deployments/{id}/desired-state`、`POST /edge/deployments/{id}/acks`、`POST /edge/deployments/{id}/rollbacks`。
- 对象：`DesiredState`、`SignedSnapshot`、`SnapshotArtifact`、`ApplyAttempt`、`EdgeAck`、`LastKnownGood`。
- 事件：`edge.desired_state_published`、`edge.apply_failed`、`edge.state_converged`、`edge.rollback_completed`。
- SLO：在线传播 p95 < 60s；本地读取 p95 < 2ms；失败回滚 < 5 分钟。

## 1. 产品闭环

Workspace admin 从当前 Workspace 配置生成 Desired State；高危字段先完成非自批审批。Control 以 deployment、generation、base generation、有效期和内容摘要发布用途分域签名快照。Agent 用 PRD-120 mTLS 拉取，先验签/owner/版本/预检，再写临时槽并原子切换；成功 ACK 后更新实际水位和 LKG。失败保持当前 active slot、记录固定失败码并可显式回滚。断网只使用已经完整验签的有界 LKG，页面标明期望/实际/陈旧差异。

## 2. 0113 数据与协议

- `edge_desired_states` 版本不可变，`edge_desired_state_heads` 单行 CAS 指向当前 generation；标准变更直接
  发布，高危变更先进入 `approval_pending`，由同 Workspace 的另一名 active admin/owner 批准后才生成签名制品。
- `edge_snapshot_artifacts` 只承载 PRD-108 已持久化配置快照的安全投影和摘要；不接受任意 JSON、URL、
  Provider Key/Token、Prompt/Response 或自由文本。artifact、signed snapshot、deployment、workspace 和 tenant
  使用复合 owner 外键。
- manifest 固定 RFC 8785 JSON Canonicalization Scheme，由受维护的 `rfc8785` 实现编码。HTTP/磁盘读取先
  拒绝 duplicate key、non-finite number、未知字段、未知 `alg/kid` 和超限整数，再比较 canonical bytes。
- 签名对象是除 `signature` 外的完整 envelope：固定 domain `metahub.desired-state.v1`、
  `protocol_version=1`、workspace/tenant/deployment audience、generation/base generation、Deployment
  revocation generation、签发/生效/安全 TTL/普通 TTL、`alg=Ed25519`、`kid`、artifact hash 与 exact content。
  `content_sha256` 只作冗余校验，不能替代完整 envelope 签名。
- `ApplyAttempt`、`EdgeAck` 和 `LastKnownGood` observation 使用单调 generation；同 generation 不同 envelope
  hash 是完整性冲突。迟到 ACK 保留不可变证据但不回拨 actual/LKG 水位。
- 复用 PRD-108 的配置来源、失败码思路和原子 owner 模式；不复用其 SaaS Cell identity、RSA/KMS
  签名兼容模式或 30 秒 LKG。Edge 签名根与设备 CA/Workspace Session 独立，首版只允许 Ed25519。

## 3. Agent 原子应用、持久高水位与 trust manifest

- Agent 在验签、owner/audience、时间、schema、preflight 全部通过后，先把候选完整制品写入非 active slot，
  `flush+fsync` 文件；再持久化 `accepted_generation+envelope_sha256+trust_generation` 高水位；最后原子替换
  active pointer 并 `fsync` 父目录，完成后才允许 ACK。任何中途崩溃都不能暴露部分配置。
- 高水位与 LKG 分文件保存；高水位先于 active pointer 持久化。启动时 active generation 低于高水位、
  同 generation 异 hash、任一文件损坏或未来时间均失败关闭，不能退回更旧但签名正确的 slot。
- 只有已分类的 transport unreachable 可使用 LKG。签名、hash、owner、schema、乱序、过期或 trust 冲突
  都进入 integrity-failed 隔离。安全/授权 TTL 默认 24 小时，到期失败关闭对应决策；普通配置 TTL 默认
  7 天，窗口内可继续服务并明确标记 `lkg/stale`，到期后不可执行。
- trust manifest 固定独立 domain、generation、current/next Ed25519 公钥、撤销 kid、签发/到期和完整签名。
  设备首次安装 pin current root；后续 manifest 必须由已 pin 的 current root 签名，next 仅在重叠窗预置，
  不能由云端数据库直接换根。current root 丢失后的 recovery bundle 与双人离线审批留给 PRD-130；本条
  对未知 root 失败关闭。

## 4. API、权限与失败策略

Workspace admin 发布本 Workspace 状态；release_manager 只能提供已审批平台制品版本，不能替租户选择 Provider/Secret；Agent 只读自身 artifact 并追加 ACK；platform_ops 只读 Fleet 水位。网络不可达可进入有界 LKG，签名、hash、owner、schema、过期或乱序失败立即隔离且不回退。

Workspace GET 只返回安全状态摘要；同一路径的 Agent GET 必须使用 PRD-120 已验证 mTLS 和短 EdgeSession，
返回 signed envelope/trust manifest，混入 Cookie、Bearer、Support 或调用方自报证书头一律拒绝。Workspace
POST 使用 `If-Match`、`Idempotency-Key`、Origin/CSRF；跨 Workspace 同形 404。Agent ACK 只含
generation/hash、固定 status/reason、耗时与 LKG 水位。

Console 使用“期望/实际/LKG”三段状态和差异字段，不显示 raw manifest；标准发布、另一管理员审批和从已
观测 LKG 创建新递增 generation 的回滚均有独立确认。Platform Fleet 只增加 generation、lag/stale 和固定
失败码，不获得 workspace/tenant、artifact、manifest 或事件正文。

## 5. 第一轮自审：签名与原子应用

- P0：在验签前解包/预检可触发不可信内容。修正为先校验 envelope、owner、hash、签名和大小，再解析受限 artifact。
- P0：逐字段覆盖 live 配置会暴露部分应用。修正为 staging slot 全量预检后原子交换引用。
- P0：签名正确但目标 Deployment 错误仍可控制节点。修正为签名内容绑定 deployment/workspace/tenant 和用途域。
- P1：租户配置与平台 release 混为一个作者。修正为分别恢复 author domain，最终 manifest 显式引用两种已授权版本。
- P0：把 PRD-108 的 `sort_keys=True` JSON 当成 RFC 8785 会在数字和 Unicode 上产生跨实现签名差异。
  修正为引入固定版本 `rfc8785`，以 RFC 官方向量和 duplicate-key/non-finite 负例锁定协议。
- P0：签名私钥或公钥只存在数据库会允许 DB 写者静默换根。修正为私钥只来自独立 secret/HSM 配置，
  数据库只存公钥、kid 和 current-root 签名的 trust manifest；配置与 DB 不一致失败关闭。

## 6. 第二轮自审：LKG、审批与冲突

- P0：完整性失败降级 LKG 会掩盖攻击。修正为只有可证明 transport outage 可用 LKG；完整性失败隔离。
- P0：断网双向更新使用 last-write-wins 会丢事实。修正为 base generation/version vector，冲突保持并交由 PRD-130 人工决策。
- P1：乱序 ACK 回退 actual generation。修正为单调水位，迟到 ACK 仅保留证据、不改变 head。
- P1：回滚重新签旧包会混淆历史。修正为生成新的递增 generation，内容引用旧 LKG hash。
- P0：先替换 LKG 再写高水位会在断电后接受旧 generation。修正为候选 slot fsync、高水位 fsync、active
  pointer rename、父目录 fsync 的固定顺序；恢复时任何不一致失败关闭。
- P0：由请求方声明 `risk_level=standard` 可绕过审批。修正为服务端比较 PRD-108 source revocation watermark、
  Workspace/security/governance 事实派生风险；高危 DesiredState 只允许不同 active admin/owner 批准。
- P1：把“断网”泛化为所有拉取错误会让 401/403/404/协议错误继续使用 LKG。修正为仅连接失败和明确
  502/503/504 进入 transport-unreachable；其他响应全部标记完整性/授权故障并停止执行。

两轮均为修正后通过，无未关闭 P0/P1。

## 7. 专项验证计划

0113 迁移；RFC 8785 官方向量、duplicate key/non-finite/超限数字；错误/过期/乱序/错误 owner/同
generation 异 hash；签名根 current/next/撤销/TTL；网络中断与完整性故障区别；候选 slot、高水位、active
pointer 各崩溃点恢复；ACK 幂等、迟到 ACK、回滚生成新 generation；动态撤权、非自批审批与审批失效；
真实 mTLS API、浏览器差异和恢复入口；传播/本地读取/回滚 SLO；manifest、日志、DB、事件敏感扫描和
静止终态。三浏览器三缩放、真实代理/长断网和批次级性能/安全回归仍在 PRD-130 后统一执行 G5。
