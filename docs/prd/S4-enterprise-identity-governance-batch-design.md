# S4 企业身份与治理批次设计

状态：设计冻结；PRD-114 已完成，当前下一项为 PRD-115。权威范围来自
`企业级AI_Gateway_SaaS增量开发计划_PRD090起.xlsx` 的 S4 与 G4。

## 1. 批次目标与顺序

本批次交付企业域名与 SSO、SCIM、MFA 与条件访问、数据驻留与法律保全、机器身份和官方
Provider 目录。实现顺序固定为 `114 -> 115 -> 116 -> 117 -> 118 -> 119`：114 建立企业信任入口，
115/116 消费其 Workspace 与身份边界，117..119 再接入治理、自动化和 Provider 目录。

每条 PRD 保留独立设计、迁移、专项测试、敏感审计和 Git 提交。开发期间只跑受影响测试；
PRD-119 独立提交后统一执行 G4 数据库、集成、流程、真实浏览器、性能、安全和必要回归。

当前进度：PRD-114 已交付 `0106`、企业域名、OIDC/SAML、登录发现、强制 SSO 恢复与 Identity Console，
并通过自动化、本地认证、三角色 Browser 和九组响应式终态验收。PRD-115..119 尚未实现，G4 尚未执行。

## 2. 共享权威边界

- 浏览器身份继续使用全局身份 Cookie 与 Workspace Cookie，不引入租户标识登录，也不把
  Bearer Token 写入浏览器存储。
- SSO 回调只能链接已验证域、已启用连接和明确 Membership；外部断言不能自行创建 Owner、
  选择 Workspace、提升角色或进入 Platform 域。
- SCIM 和 OAuth client credentials 使用独立凭据域；机器身份不能复用个人 Session、切换
  Workspace 或获得 Platform 角色。
- 所有敏感值使用独立密钥域加密或只存不可逆摘要。SSO client secret、SCIM Token、MFA Secret、
  recovery code、OAuth client secret、Provider Key 和目录签名密钥禁止跨域复用。
- 权限由数据库矩阵逐请求恢复。前端导航、按钮和页面只是投影，不构成授权边界；动态撤权必须
  立即使 API、Workspace Session、Step-up Grant 和机器 Token 失败关闭。
- 跨 Workspace 资源统一返回同形 404；认证失败不暴露账号、域名、连接、Membership 或状态差异。
- 写操作使用显式 Idempotency-Key 或 If-Match/CAS；事件与业务事实同事务落 source/outbox，投递
  失败不回滚已提交事实，恢复按持久水位重放。
- 日志、事件和验收证据不含 Provider Key、Token、Prompt、Response、SSO 断言、SCIM Bearer、
  MFA Secret、recovery code、OAuth secret、客户正文或原始设备/IP 值。

## 3. 迁移与兼容顺序

| PRD | Migration | 主要新增事实 | Down 边界 |
| --- | --- | --- | --- |
| 114 | 0106 | verified domains、SSO connection/certificate/policy、federation link、state/replay/outbox | 任一已验证域、连接、链接、策略或未投递事件存在即拒绝 |
| 115 | 0107 | SCIM token/link/mapping/sync/conflict、SCIM outbox | Token、目录链接、冲突、同步水位或 SCIM 来源 Membership 存在即拒绝 |
| 116 | 0108 | WebAuthn/recovery/trusted device、conditional access、step-up、session revoke generation | 新因子、策略、grant、设备或新撤权事实存在即拒绝 |
| 117 | 0109 | classification/residency/retention/legal hold/deletion schedule/evidence | 策略、hold、删除计划或证据存在即拒绝 |
| 118 | 0110 | service account、OAuth client/credential/token grant、machine audit | 机器主体、credential、活跃 grant 或审计事实存在即拒绝 |
| 119 | 0111 | signed provider catalog/capability/endpoint/compatibility/preflight | 非种子目录版本、租户实例、preflight 或事件水位存在即拒绝 |

所有 migration 采用 expand-migrate-contract、fresh/current-head/往返/失败回滚/重复执行验证；已应用
迁移不改字节。新表用 owner 复合外键、状态 CHECK、revision、时间格式和不可变触发器保护。

## 4. 共享模块与接口

- `enterprise_identity`：PRD-114 域名/SSO 与 PRD-115 SCIM 的 Workspace owner、事件、凭据和
  脱敏错误共享实现；协议验证器仍按 OIDC、SAML、SCIM 分离。
- `identity`：PRD-116 在既有 TOTP 与 global sessions 上扩展 WebAuthn、恢复码、设备、step-up；
  不新建平行账号或 Session 表。
- `data_governance`：PRD-117 产生版本化决策快照，由 Control、Gateway、job runtime、backup 和
  tenant-data scheduler 消费；消费者不得自行解释未签名策略。
- `machine_identity`：PRD-118 产生非人类 AuthContext，只允许显式 machine-safe permission；
  共用审计格式但不共用个人 Cookie 或 JWT claim。
- `provider_catalog`：PRD-119 发布平台签名目录；租户实例化继续落现有 Provider/Local Endpoint
  所有权模型，目录不能携带客户凭据或自动扩权。

## 5. 权限与页面归属

- `owner/security_admin` 管理 SSO、SCIM、条件访问；普通成员只能管理本人因子、设备和 Session。
- `security_admin` 管理数据策略，`legal_admin` 管理 Legal Hold，`auditor` 只读安全摘要。
- `owner/security_admin` 管理机器身份；`developer` 只读并使用被授予的 machine-safe scope。
- `platform_compliance` 只读驻留证据；`platform_ops` 发布签名 Provider 目录；`platform_support`
  只有有效 SupportGrant 时读取固定脱敏摘要，不能代改企业配置或读取凭据。
- 企业 Console 在身份与安全、数据治理、开发者配置中分组；Platform Console 只新增合规证据和
  Provider 目录发布入口。现有 `models`、`providers`、`local-models` 保持 Workspace owner。

## 6. 失败策略与恢复

- IdP、DNS、KMS、目录、数据库、outbox 或签名验证不可用时，SSO 配置写入、SCIM 变更、条件访问
  强制、机器 Token 签发和目录实例化均失败关闭。
- 登录发现可以在不读取账号事实的前提下返回密码/SSO 模式；未知域与未验证域使用相同密码模式和
  响应形状，避免账号枚举。
- 强制 SSO 必须保留一个经验证 Owner break-glass，且需要本地密码、活跃 MFA、恢复审计和短 TTL；
  Platform Support 不能充当 break-glass。
- SCIM 停用在同一事务撤销 Membership 及其 Workspace Sessions；乱序版本不能复活成员。
- 条件访问先 `audit` 后 `enforced`；策略或依赖损坏时高风险操作失败关闭，普通已认证读取不静默
  提权或回退默认角色。
- Legal Hold 只阻断删除，不扩大读取；目录签名或 endpoint template 不合法时不得创建 Provider。

## 7. 两轮批次自审

### 第一轮：信任、owner 与协议边界

- P0：把 IdP claim 当角色会形成租户提权。修正为 assertion 只解析稳定 subject/email，Membership
  和角色始终从本地数据库恢复。
- P0：SSO metadata URL、Provider template 和 preflight 可形成 SSRF。修正为无自动任意 URL 抓取；
  只允许经验证域、固定 IdP endpoint 和签名目录 endpoint，解析后再次检查 DNS/IP/redirect。
- P0：SCIM/机器 Token 若复用用户鉴权会获得浏览器能力。修正为独立 token hash、subject type、
  scope allowlist、audience 和路由依赖。
- P0：Platform 角色隐式进入 Workspace 会破坏双域。修正为 Platform API 与企业 API 独立 router、
  Cookie/CSRF/permission 和 DTO。
- P1：前端隐藏配置不能证明撤权。修正为服务层逐请求恢复 permission revision 与 owner。

### 第二轮：恢复、证据与跨 PRD 一致性

- P0：强制 SSO 无恢复会锁死企业。修正为启用前验证 active connection、证书窗口、Owner MFA 和
  break-glass，且恢复动作单次、短 TTL、全审计。
- P0：SCIM 停用只改目录链接会留下活跃 Session。修正为 Membership 与 Workspace Session 同事务
  撤销并提升 revoke generation。
- P0：Legal Hold 若改变查询权限会成为数据后门。修正为仅影响删除调度条件，不参与读授权。
- P1：凭据出现在 outbox/证据会形成二次泄漏。修正为事件 exact schema 只含 ID、状态、revision、
  安全 outcome 和时间；敏感审计扫描覆盖源码、测试 fixture 与生成摘要。
- P1：目录更新自动改变租户 Provider 会越权。修正为目录版本不可变，租户显式选择和 CAS 实例化；
  后续目录更新只给兼容提示，不自动修改 endpoint、能力或凭据。

## 8. G4 退出证据

六条独立提交后必须统一证明：0105 -> 0111 fresh/逐步/down guard；OIDC/SAML/SCIM/WebAuthn 协议与
重放边界；停用/撤权传播；驻留在路由、存储、备份和删除生效；机器 scope 最小授权；Provider 目录
签名和 SSRF 边界；三浏览器三档缩放；Excel SLO；静止终态、句柄/连接/队列水位；敏感字面量和
禁止文件为零。任一必需证据缺失、跳过、失败或含敏感明文，G4 即失败。
