# PRD-114 企业域名、OIDC/SAML SSO 与登录发现

状态：已实现并完成提交前专项、兼容、本地认证与真实 Browser 验收（2026-08-25）。依赖
PRD-091..096、PRD-106；范围以 Excel PRD-114 为准。

## Excel 权威合同

- API：`POST /workspaces/{id}/domains`、`POST /workspaces/{id}/sso-connections`、
  `GET /identity/login-discovery`、`POST /identity/sso/callback`、`POST /workspaces/{id}/sso/test`。
- 对象：`VerifiedDomain`、`SsoConnection`、`SsoCertificate`、`AccountFederationLink`、`SsoPolicy`。
- 事件：`domain.verified`、`sso.connection_changed`、`sso.login_failed`、
  `sso.certificate_expiring`。
- SLO：登录发现 p95 < 200ms；本地回调处理 p95 < 500ms，不含 IdP 时延。

## 1. 产品闭环

Owner/security_admin 创建企业域名，获得一次性 DNS TXT challenge；Control 通过受限 DNS resolver
验证后占用全局唯一规范域名。管理员创建 OIDC 或 SAML connection，上传静态 metadata/certificate
或固定 endpoint 配置，完成协议测试后激活。登录页只提交 email/domain 做无租户登录发现，跳转 IdP；
回调验证 assertion 后只链接本地明确 Membership，并签发既有 global + Workspace Session。

强制 SSO 只有在 connection active、证书有效、至少一个 active Owner 且指定 break-glass Owner 已配置
本地密码和 active MFA 时可启用。break-glass 使用独立短 TTL recovery grant，不绕过 MFA、Membership、
条件访问或审计。Platform 账号永远走独立平台身份入口。

## 2. 0106 数据与状态机

- `verified_domains`：规范 domain、全局唯一 HMAC、Workspace/tenant 复合 owner、challenge hash、
  `pending|verified|suspended`、revision、验证和到期时间。
- `sso_connections`：owner、`oidc|saml`、issuer/entity ID、固定 endpoint、client secret ciphertext、
  metadata hash、`draft|testing|active|disabled`、revision。
- `sso_certificates`：connection owner、用途、fingerprint、公钥/证书、`staged|active|retired|expired`、
  not-before/not-after；私钥不进入 Control。
- `account_federation_links`：Workspace、connection、account、外部 subject HMAC、verified email HMAC、
  `active|disabled`；不存 assertion。
- `sso_policies`：Workspace 单例、`optional|enforced`、break-glass account、revision。
- `sso_login_states` 与 `sso_replay_guards`：state/nonce/request hash、PKCE、connection、浏览器绑定、
  单次消费、短 TTL。
- `enterprise_identity_events/outbox`：exact schema 和可恢复投递；无 domain challenge、assertion、code、
  client secret、email 或 Token。

Down 在任何 verified domain、非 draft connection、federation link、policy、有效 state/replay 或未投递
事件存在时拒绝；不能通过回滚释放已验证域名或删除审计事实。

## 3. 协议与网络安全

OIDC 使用 authorization-code + PKCE，验证 state、nonce、issuer、audience、azp、exp/iat、签名算法和
固定 JWKS key。SAML 验证唯一 ID、XML signature、canonicalization allowlist、issuer、audience、
destination、InResponseTo、Recipient、NotBefore/NotOnOrAfter，拒绝 unsigned/wrapping/duplicate ID。
只接受预配置证书与固定 endpoint，不根据 assertion、metadata redirect 或用户输入抓取任意 URL。

DNS 只查询规范化公共域 TXT；拒绝 IP literal、公共后缀、内部后缀、空 label、通配符和 DNS rebinding。
IdP HTTP 客户端禁用任意 redirect，解析后拒绝 loopback、link-local、private、metadata 和 Unix socket。

## 4. 权限、API 与 Console

- `enterprise_sso.read/manage` 由 owner/security_admin 持有；auditor 只读安全状态；普通成员只用登录发现。
- Workspace path 必须与当前 Workspace Session 完全一致；Platform Support 无权创建、测试、启停或恢复。
- POST 域名/connection/test 使用 Idempotency-Key 与 If-Match；未知字段拒绝，响应 `no-store`。
- 登录发现不查询账号，只按 domain policy 返回同形 `password|sso` 模式、公开 connection code 和短期
  transaction；未知/未验证域一致返回 password，配合统一限流。
- Identity 页面新增企业 SSO 区：域名状态、连接、证书窗口、测试、强制策略和恢复状态；不渲染 metadata
  原文、client secret、assertion、subject、email 列表或内部 endpoint。

## 5. 第一轮自审：协议与账号链接

- P0：自动信任 email domain 会让未验证 IdP 接管账号。修正为 VerifiedDomain + active connection +
  本地 Membership 三者同时成立，首次链接也必须匹配 verified email claim。
- P0：SAML XML wrapping/重复 ID 可替换 assertion。修正为唯一 ID 索引、签名节点 owner 校验、禁用
  DTD/entity、固定算法和对实际消费节点做 digest 验证。
- P0：OIDC state/nonce 仅放 Cookie 会被重放。修正为服务器端哈希 state、浏览器 HMAC、单次消费和 TTL。
- P1：登录发现查询账号会造成枚举。修正为只查询域策略，响应大小、状态码和字段稳定。
- P1：Platform Support 代改 SSO 会跨越客户授权。修正为企业权限独占，支持只经 SupportGrant 看摘要。

## 6. 第二轮自审：强制策略与恢复

- P0：未验证可用性即强制 SSO 会锁死 Workspace。修正为 active test、证书余量、Owner/MFA 和
  break-glass 原子前置条件。
- P0：break-glass 变成长期开口。修正为指定 Owner、密码 + MFA、短 TTL、单次 recovery grant、
  强制理由和不可变审计。
- P1：证书轮换直接覆盖 active cert 会中断。修正为 staged -> active 双窗口，旧证书在 grace 后 retired。
- P1：IdP 故障回退密码会绕过 enforced SSO。修正为普通成员失败关闭，仅 break-glass Owner 可恢复。
- P1：失败事件携带 assertion 会泄漏身份。修正为固定 outcome、connection ID、revision、request_id 和时间。

## 7. 专项验证计划

0105/0106 fresh/往返/down guard；域名抢占与并发验证；OIDC code/PKCE/JWKS/key rotation；SAML signature/
wrapping/时钟偏差；state/nonce/assertion replay；跨 Workspace 同形 404；动态撤权；强制 SSO 恢复；IdP/DNS/
KMS/DB/outbox 故障；登录发现和本地回调 SLO；Identity Console 三浏览器、三档缩放与静止终态；敏感扫描。

## 8. 实现与验收结果

- `0106`、企业身份后端、OIDC/SAML/DNS 协议校验、强制 SSO 恢复、共享 SDK 和 Identity Console 已落地；
  逐请求 WorkspaceSession、RBAC、CAS/幂等、跨 Workspace 同形拒绝和敏感 DTO 边界均有专项覆盖。
- 自动化结果为 Migration `4 passed`、API `2 passed`、协议 `6 passed`、Shared SDK `4 passed`、Console
  专项 `7 passed`、Console 全量 `1079 passed`，并完成 PRD-091/094/095 邻接回归。
- 本地真实账号、密码和 TOTP 只从本机凭据源进入内存，完成登录、Workspace 切换与企业身份读取；值未进入
  命令、日志、截图、文档或提交。
- 真实 Browser 已验证 Owner 管理、auditor 只读、developer 隐藏、敏感输入提交后清空和 DOM 零泄漏。
  `1280/440/390` 三视口乘 `100%/125%/150%` 三档缩放的九组测量均无横向溢出、重叠、裁切或稳定期位移。
- 完整命令、边界、截图位置和 OCR 限制见 `docs/testing/PRD-114-tests.md`。本结论只关闭 PRD-114；
  S4 批次 G4 仍需在 PRD-119 独立提交后统一执行。
