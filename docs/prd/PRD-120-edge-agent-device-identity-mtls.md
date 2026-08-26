# PRD-120 Edge Agent 注册、设备身份与 mTLS

状态：已完成。0112、设备注册/轮换/session/吊销、Workspace 与 Platform Console、专项测试、真实
mTLS/Browser 和敏感审计均已交付。依赖 PRD-090/094/106/116，范围以 Excel PRD-120 为准。

## Excel 权威合同

- API：`POST /edge/registrations`、`POST /edge/certificates`、`POST /edge/sessions`、`POST /edge/deployments/{id}/revoke`。
- 对象：`EdgeDeployment`、`EdgeRegistrationIntent`、`DeviceIdentity`、`DeviceCertificate`、`EdgeSession`。
- 事件：`edge.registered`、`edge.certificate_rotated`、`edge.revoked`、`edge.authentication_failed`。
- SLO：mTLS 握手 p95 < 500ms；吊销传播 < 60s；注册流程 < 10 分钟。

## 1. 产品闭环

Workspace owner 创建绑定当前 workspace/tenant 的短时注册意图并一次性查看令牌。Agent 在客户环境生成 Ed25519 私钥和 PKCS#10 CSR，以出站连接消费令牌；Control 校验 token HMAC、Deployment 状态、CSR 算法/扩展和重放证明后签发短期设备证书。后续连接以 mTLS 恢复 DeviceIdentity，创建短时 EdgeSession；轮换采用旧证书 mTLS + 新 CSR，吊销/清退提升 revocation/session generation 并终止活跃会话。

Platform Fleet 只看 deployment ID、region、版本、证书到期区间和固定健康码。云端不接收设备私钥、Provider Secret 或业务正文。

## 2. 0112 数据与状态机

- `edge_deployments`：workspace/tenant 复合 owner、display name、region、mode(`connected|isolated`)、status(`pending|active|revoked|retired`)、revision、revocation watermark。
- `edge_registration_intents`：deployment owner、token HMAC/key version、expires/consumed、request/idempotency digest；令牌明文不落库。
- `edge_device_identities`：deployment 唯一 active identity、public-key SHA-256、hardware-proof 摘要、generation、status。
- `edge_device_certificates`：identity/deployment、serial、public-key/CSR 摘要、issuer/key version、not-before/not-after、status(`active|superseded|revoked|expired`)；不存私钥。
- `edge_sessions`：certificate、peer binding digest、handshake nonce HMAC、generation、短 TTL、revoked reason。
- `edge_identity_events/outbox`：exact schema，只含 owner ID、generation、固定 outcome/reason、request/trace ID 和时间。

状态转换用 revision CAS；token consumption、identity/certificate 创建和 `edge.registered` outbox 同事务。Down 在任何非种子事实存在时拒绝。

## 3. 传输与接口

- Workspace owner 调用注册意图和 revoke；严格 Workspace Cookie/CSRF/Origin，owner 从会话恢复，拒绝 Platform/Support/Authorization credential conflict。
- Agent 注册只能提交 token、CSR、一次性 challenge proof、Agent version 和有界能力码；CSR subject、SAN 和 owner 由服务端重建，不能信任客户端声明。
- 证书轮换及 session 要求受信 TLS 终止层完成 peer 验证，并通过独立内部认证传递不可伪造的 peer digest；外部自带证书头失败关闭。
- session 每次恢复 certificate/deployment status、revision、revocation watermark 和证书有效期；未知、跨 Deployment、已吊销、nonce 重放均返回同形 machine error。

## 4. 权限、DTO 与 Console

新增 Workspace `edge.deployment.read/manage`，Agent `edge.self.register/rotate/connect`，Platform `platform.edge_fleet.read/manage`；三类 permission 不互相蕴含。Workspace 页面提供创建 Deployment、一次性注册步骤、证书状态/轮换提醒、吊销确认和操作时间线；Platform 页面只投影 Fleet 摘要。所有响应 no-store，令牌仅创建成功响应出现一次，前端不写 Storage/URL/日志并在离开或登出时清空。

## 5. 第一轮自审：设备信任与克隆

- P0：仅靠注册令牌可被复制注册多台设备。修正为 token 单次事务消费，并绑定 CSR 公钥、challenge、Deployment；公钥摘要和 nonce 都有唯一约束。
- P0：接收客户端证书头会绕过 mTLS。修正为只信任 mTLS 终止层的内部 authenticated transport，直连应用的同名头全部拒绝。
- P0：CSR 自报 Workspace/SAN 会越权。修正为 owner 从 intent 恢复，证书 subject/SAN 由服务端生成。
- P1：一次性令牌进入浏览器缓存或错误详情。修正为 no-store、固定错误、单次响应和前端内存生命周期。

## 6. 第二轮自审：吊销、恢复与证据

- P0：长时 session 会使证书吊销超过 60 秒。修正为短 session、单调 revocation watermark、重连校验和主动 session revoke。
- P0：轮换中先吊销旧证书会断连。修正为新证书确认可用后 CAS 激活，旧证书进入短 drain 窗口再 supersede；明确 revoke 仍立即失败关闭。
- P1：认证失败事件若包含 CSR/证书正文会扩大泄漏。修正为只记录摘要和固定 reason code。
- P1：平台运维如果使用 tenant API 管理节点会隐式成为成员。修正为独立 `/platform/fleet` 投影，120 阶段不开放租户正文诊断。

两轮均为修正后通过，无未关闭 P0/P1。

## 7. 实现结果

- `edge_management` 按 Workspace Cookie、Platform Cookie 和 Agent mTLS 三个凭据域拆分路由；任何 Cookie、
  Bearer 或调用方自报证书头进入 Agent 路由都失败关闭。
- 注册令牌和 challenge 只在首次 Workspace 响应出现，数据库只保存分域 HMAC；真实 Ed25519 CSR 经过空
  subject、零扩展和签名验证后，由服务端重建 subject/SPIFFE SAN 并签发七天短证书。
- 轮换以当前证书 mTLS、generation 和新 CSR 为边界，旧证书进入五分钟 drain；45 秒 EdgeSession、单调
  revocation generation 和逐请求状态恢复保证吊销不依赖长时缓存。
- Workspace Console 完成创建、一次性注册材料、列表、时间线和吊销确认；Platform Fleet 使用独立 DTO，
  只显示 deployment ID、region/mode、状态、Agent 版本、证书到期、最近连接和 revision。
- 真实本地验收创建四条待注册 Deployment；注册材料在复制、关闭、刷新和离页后均从 DOM/内存清除。
  Platform 直达 hash、无关子系统故障隔离、离页清理和返回重载均已验证。

## 8. 专项验证结果

迁移、API、共享合同、Console 导航与前端专项共 `19/19` 通过；真实 TLS 1.3 双向证书握手 40 次 p95
`31.67ms`，45 秒 Session 上限和同事务吊销满足 `<60s` 边界；536px Browser 无页面级横向溢出。
详细证据见 `docs/testing/PRD-120-tests.md`。PRD-121 为下一权威项，G5 保留到 PRD-130 独立提交后统一执行。
