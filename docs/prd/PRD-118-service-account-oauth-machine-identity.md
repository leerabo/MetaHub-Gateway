# PRD-118 Service Account、OAuth 客户端与受限控制面 API

状态：已完成。依赖 PRD-059/094/116；范围以 Excel PRD-118 为准。

## Excel 权威合同

- API：`POST /service-accounts`、`POST /oauth/clients`、`POST /oauth/token`、
  `POST /service-accounts/{id}/rotate`、`DELETE /service-accounts/{id}`。
- 对象：`ServiceAccount`、`OAuthClient`、`ClientCredential`、`AccessTokenGrant`、
  `MachineIdentityAudit`。
- 事件：`service_account.created`、`service_account.credential_rotated`、
  `service_account.revoked`。
- SLO：Token 签发 p95 < 300ms；鉴权 p95 < 20ms；撤销传播 < 2s。

## 1. 产品闭环

owner/security_admin 创建 Workspace Service Account，选择 machine-safe scope、IP/mTLS 约束和短 Token TTL；
创建 OAuth client 后 secret 只显示一次。自动化系统用 client credentials 换取 opaque 短期 access token，
调用显式允许的 Control API。轮换有有界 grace，撤销立即提升 generation 并使所有 grant 失效。机器调用
产生 subject、Workspace、scope、request_id 和固定 action 审计，不创建个人 Session、不进入 Platform 域。

## 2. 0110 数据与状态机

- `service_accounts`：Workspace/tenant owner、name、`active|disabled|revoked`、scope template、constraint hash、
  token TTL、revision、last-used；不是 global account，也不使用 workspace_memberships 的人类 account 外键。
- `oauth_clients`：service account owner、public client ID、audience、`active|rotating|revoked`、revision。
- `client_credentials`：client、secret hash/prefix、generation、`active|grace|revoked`、created/expires/last-used；
  明文只在创建/rotate 响应一次出现。
- `access_token_grants`：opaque token hash、service account/client、scope、audience、permission revision、constraint
  generation、issued/expires/revoked/last-used；不存明文 token。
- `machine_identity_audits/outbox`：固定 action/resource/outcome、owner/revision/request/trace/time；不含 secret、
  token、请求正文、Authorization 或原始 IP/certificate。

Down 在 service account、client、credential、grant 或 audit/outbox 事实存在时拒绝。PRD-094 预留的
`service_account` Membership 仅迁移为兼容引用或 disabled conflict，不能变成人类可登录 Membership。

## 3. Scope 与 AuthContext

新增 `MachineAuthContext(subject_type="service_account")`，由 Bearer token hash + 当前数据库 owner、status、
scope、generation 和约束逐请求恢复。机器 permission 是现有 permission 的显式 machine-safe 子集，禁止
身份/成员/SSO/SCIM/MFA/法律保全/账务退款/Platform/SupportGrant/密钥导出等人类或高风险管理动作。

创建者只能授予自己当前权限与模板交集；后续操作者撤权、模板 revision、Service Account status、credential
generation 任一变化都会使 token 失败关闭。token 固定唯一 Workspace/audience，不能调用 workspace switch、
换取 Cookie、委托另一个 token 或接受 tenant/workspace body 参数。

## 4. OAuth 与网络约束

`POST /oauth/token` 只接受 `grant_type=client_credentials`、client ID/secret、固定 audience 和 scope 子集，
拒绝 refresh token、password grant、authorization code、implicit 和任意 extension。认证失败统一同形错误并限流。

IP allowlist 使用可信 peer/代理签名地址；mTLS fingerprint 来自 TLS ASGI scope 或经独立服务密钥签名的代理证明，
不信任普通 header。没有可验证 mTLS 事实时，要求 mTLS 的 client 失败关闭。secret rotation 允许旧 generation
只完成已开始的短 grace，不能签发超出 grace 的 token。

## 5. Console

Developer/Security 页面显示 Service Account、scope、constraint、client prefix、generation、last-used 和撤销状态。
secret/token 一次性 Dialog 在关闭、导航、身份切换和错误时清空 DOM/state；不写 local/session storage。developer
可按权限只读，不能扩大 scope；Platform 角色无企业机器身份入口。

## 6. 第一轮自审：主体隔离与最小授权

- P0：复用个人 Workspace Session 会让机器获得切换/恢复能力。修正为独立 AuthContext、token 和路由 allowlist。
- P0：直接接受 scope 字符串会自授权。修正为 machine-safe catalog ∩ operator permissions ∩ template。
- P0：平台角色创建租户 token 会跨域。修正为只接受 Workspace Cookie 的 owner/security_admin。
- P1：JWT 离线验证难以及时撤销。修正为 opaque hash + generation 在线/短缓存复核。
- P1：service_account Membership 被激活成人类主体。修正为 migration guard 和无 global account/session 路径。

## 7. 第二轮自审：Secret、约束与撤销

- P0：mTLS header 可伪造。修正为 TLS scope/签名代理证明，缺失即拒绝。
- P0：撤销只改 Service Account 不处理 grant。修正为 generation bump + grant revoke 同事务，<2s 生效。
- P1：secret prefix/last-used 可被跨租户枚举。修正为 owner-bound DTO、同形 404 和固定长度 prefix。
- P1：rotation grace 可无限延长旧 secret。修正为固定短窗口、单 generation、不可续期。
- P1：审计记录 Authorization/body 会泄漏。修正为固定 action/resource/outcome 和 request metadata allowlist。

## 8. 专项验证计划

0109/0110 fresh/往返/down guard；secret 一次显示/hash/rotation；scope 交集与 forbidden route catalog；opaque
token owner/audience/generation/TTL；IP/mTLS 可信来源；并发撤销 <2s；跨 Workspace/Platform 零泄漏；鉴权与签发
SLO；Console DOM 清理、三浏览器；源码、日志、事件和证据敏感扫描。

## 9. 实现校正：写操作幂等重放

Excel 验收要求所有写操作可安全重试，因此 Service Account 创建、OAuth client 创建、凭据轮换、撤销和
OAuth token 签发均要求 `Idempotency-Key`。服务端以 tenant owner、固定 command type 和 key 绑定请求指纹：

- 同 key、同请求返回首次命令的同语义响应，不重复创建、轮换、撤销或签发；
- 同 key、不同请求返回 `machine_identity.idempotency_conflict`，不修改任何业务状态；
- rotate/revoke 仍要求 CAS，幂等重放不能绕过首次执行时的 revision 检查；
- `machine_identity_commands` 只保存 command owner、请求指纹、结果引用、状态和时间，不保存 key、secret 或
  access token 明文；一次性 secret/token 由服务端 HMAC、command id 和幂等 key 确定性派生；
- Console 仅在内存中保存未决重试意图；网络中断或 `503` 后复用原 key，成功、确定失败、导航、身份切换或
  Dialog 关闭时立即清除，不写 local/session storage。
