# PRD-095 SaaS 平台运营角色与受控支持访问

状态：已完成
阶段：S2-平台运营
优先级：P0
依赖：PRD-090、PRD-091、PRD-094

## 1. 目标与边界

本条建立独立于企业 Workspace 的 Platform IAM。平台员工以已验证 GlobalAccount 为登录根，完成真实
MFA 后换取 PlatformSession；平台角色、权限、Cookie、审计和支持委托均不复用 Workspace Membership、
Legacy Bearer 或企业 RBAC。企业 owner/admin 无论权限多高，都不能创建平台员工或进入 `/platform/*`。

本条交付平台员工与角色管理、平台导航、JIT SupportGrant、客户与平台双审批、短时 SupportSession、
撤销、break-glass 和不可变平台审计。平台客户目录、Workspace 冻结/恢复与健康投影由 PRD-096 交付；
套餐、账务、Provider 工程、渠道、客户成功和增长页面只建立角色/权限占位，不提前开放业务数据。

明确不做：按邮箱自动提权、永久模拟客户、读取 Provider Key/Token/Prompt/Response/文件正文、把平台
员工写成任意 Workspace Membership、用前端隐藏菜单代替 API 权限、在日志或证据中保存 MFA Secret、
一次性验证码、bootstrap 恢复密钥、Session Cookie 或支持授权原始原因。

## 2. 五类主体与凭据解释

PRD-095 补齐目标架构中的 PlatformSession 与 SupportSession，并保持五类主体互斥：

| 主体 | 可信根 | Cookie / TTL | 可访问路由 |
| --- | --- | --- | --- |
| GlobalSession | GlobalAccount + GlobalSession | 既有 global Cookie，<=12h | `/identity/*`；可发起 MFA 与 PlatformSession 换取 |
| WorkspaceSession | Membership + Workspace binding | 既有 workspace Cookie，<=2h | Workspace 控制面；不能访问平台路由 |
| PlatformSession | PlatformOperator + role binding + MFA factor | 独立 platform Cookie，<=1h | `/platform/*` |
| SupportSession | active SupportGrant + requester PlatformOperator | 独立 support Cookie，<=15min | 固定 `/support/*` 诊断白名单 |
| DeviceIdentity | Edge CA / device proof | mTLS | `/edge/*`；本条不实现 |

平台路由只解释 PlatformSession；`POST /platform/sessions` 和一次性 bootstrap 是唯一解释 GlobalSession
并换取平台上下文的入口。SupportSession 不能调用普通 Workspace API，PlatformSession 也不能直接调用
`/support/*`。由于所有 `__Host-*` Cookie 必须 `Path=/`，其他已知 Cookie 在不属于自身的路由中是惰性
Cookie，绝不作为 fallback；重复的当前域 Cookie、当前域 Cookie 与 Authorization 并存或同一路由存在
两个可解释候选时固定拒绝。PlatformSession 换取成功时主动清 Workspace/Support Cookie，SupportSession
换取后 `/support/*` 只解释 support Cookie，Global/Platform Cookie 保持惰性。

## 3. 真实 MFA

0087 新增 `account_mfa_factors` 与 `account_mfa_attempt_guards`。0083 的 `global_accounts.mfa_state` 被历史
CHECK 固定为 `not_enrolled`，因此不得伪造更新；新表是权威来源，账号摘要改为按 active factor 计算
`mfa_configured`，旧列仅保留 N/N-1 兼容。

首版因当前产品环境和依赖能力固定支持 TOTP：

- `POST /identity/mfa/totp/enrollments` 要求 GlobalSession、Origin、global CSRF 和当前密码重认证；返回
  一次性 Base32 secret 与标准 `otpauth://` URI，响应 `no-store`，Secret 不进入数据库明文、日志、DOM
  snapshot、Storage 或测试证据。URI label 使用 opaque factor ID，不嵌入邮箱、手机号或 display name；
  wire model 允许一次性序列化但 `repr`、异常和结构化日志必须脱敏。
- TOTP Secret 使用独立 `METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET` 做 AES-256-GCM 信封加密，AAD 绑定
  account、factor、version 和 purpose；密钥域不得与 Global/Workspace/Platform/Support Session、
  Challenge、PII、Provider Key 或 Cache 复用。
- `POST /identity/mfa/totp/verifications` 原子验证 pending factor，允许当前计数器前后各一个窗口，保存
  单调 `last_accepted_counter` 防重放，激活后递增 factor revision。失败按 account+factor+device 持久
  限流，不回显是时间偏差、Secret、验证码还是 factor 状态错误。
- PlatformSession 换取必须再次消费一个严格大于 factor 水位的 TOTP counter。平台操作员没有 active
  factor、factor 损坏、验证码重放或 MFA 存储不可用时失败关闭。

Identity 运行时按 schema capability 读取 MFA factor；冻结在 0083..0086 的 N/N-1 迁移夹具没有 0087 表时
继续把旧 `mfa_state` 解释为未配置，不得因查询新表而破坏 Global 登录、Session 或 Workspace 创建。

本条不实现短信 MFA、WebAuthn 和最终账号恢复。MFA 丢失只能走受控离线恢复流程；不能用客服角色、
邮箱或已有 Workspace owner 身份绕过。

## 4. 一次性 platform_owner bootstrap

生产平台首个 owner 只能通过显式 ceremony 创建，禁止使用 `admin@metahub.local`、默认租户 admin、
环境中的默认邮箱或任何历史角色推断。

- 配置 `METAHUB_PLATFORM_BOOTSTRAP_ENABLED` 默认 false。启用时必须显式配置独立、至少 32 字节的
  `METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET`；该值只参与恒定时间 HMAC 证明，不落库或日志。
- `POST /platform/bootstrap` 要求 active GlobalSession、当前密码、刚消费的 TOTP、离线 recovery secret、
  固定格式 reason 和页面内显式确认。服务端不接受 account/email/operator/role 参数。
- `platform_bootstrap_state` 在同一 `BEGIN IMMEDIATE` 中由 open CAS 为 completed，创建第一个 active
  PlatformOperator 和唯一 platform_owner role binding，写不可变审计和 outbox；并发最多一个成功。
- 数据库一旦存在 completed 状态，配置仍为 true 也必须固定拒绝；重启、恢复备份或回滚路由不能重开。
- ceremony 证据只保存 actor ID、MFA factor revision、固定 reason code、request_id/trace_id 和时间，
  不保存密码、TOTP、recovery secret、邮箱或客户端正文。

## 5. 平台角色、权限和委派

固定角色命名空间：

`platform_owner/platform_ops/platform_security/platform_billing/platform_support/`
`platform_verification/platform_compliance/platform_privacy/release_manager/`
`platform_provider_engineer/platform_channel/platform_customer_success/platform_growth`。

0087 建立 `platform_roles`、`platform_permissions`、`platform_role_permissions`、`platform_operators` 与
`platform_operator_role_bindings`。目录由迁移冻结并带 catalog revision/hash；运行时每请求复算角色、
权限、operator revision 和授权 fingerprint，不能从角色名或前端清单补权。

- platform_owner 可创建、停用员工和分配角色；目标账号必须 active、login enabled 且已配置 MFA。
- 非 owner 不能修改 Operator 或角色。至少保留一个 active platform_owner；停用、删绑定和并发更新
  由数据库约束与事务 CAS 双重阻断。
- platform_support 只能申请/使用自己的 SupportGrant，不能审批自己的申请。
- platform_security/platform_compliance 可作平台侧支持审批；客户侧审批必须来自目标 Workspace 的
  active owner/admin，不能由任何 platform_* 角色代替。
- 其余角色只得到自己后续业务域的页面/动作占位；本条没有数据 API 就不返回伪造卡片或假统计。

Operator 变更递增 revision，立即撤销该 Operator 的 active PlatformSession、SupportSession 和未激活
授权；提交后的下一请求不得继续使用旧权限。平台导航是后端权限投影，直达 URL/API 仍独立鉴权。

## 6. PlatformSession

`platform_sessions` 保存 operator/account/global session、token/CSRF HMAC、MFA factor/revision/counter、
operator revision、role-set fingerprint、状态、绝对/空闲到期和撤销原因。明文 Token 只在当前响应和
浏览器 Cookie 生命周期出现。

PlatformSession 使用独立 `METAHUB_PLATFORM_SESSION_SECRET`；SupportSession 使用独立
`METAHUB_SUPPORT_SESSION_SECRET`。两者与 `METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET`、bootstrap recovery
secret 及现有 Auth/Global/Challenge/PII/Workspace/Provider/Cache 等根域均不得复用，prod 必须显式配置
至少 32 字节非默认值。TTL 配置分别受限为 Platform <=1h、Support <=15min。

- Cookie：`__Host-metahub-platform-session`（Secure、HttpOnly、SameSite=Lax、Path=/、无 Domain）和
  `__Host-metahub-platform-csrf`（Secure、SameSite=Strict）。
- `POST /platform/sessions` 要求 GlobalSession + global CSRF + Origin + TOTP，成功轮换当前浏览器旧
  PlatformSession；不接受 operator_id 或 role。
- `GET /platform/session` 与每个 `/platform/*` 请求重验 GlobalAccount、GlobalSession、factor、Operator、
  role binding 和 revision。任一失效即撤销并清 Cookie。
- 平台写请求强制唯一 Origin、CSRF、Content-Type、Content-Length 和严格 JSON；GET/错误/成功均
  `Cache-Control: no-store, private`。

## 7. JIT SupportGrant

`support_grants` 绑定 requester Operator、目标 workspace/tenant、固定 scope、reason code、期限、
operator revision、workspace revision 与状态；`support_grant_approvals` 是 append-only 双域事实。

状态机：`pending_approvals -> active -> revoked|expired`。客户/平台两条 append-only 审批事实可按任意顺序
到达，响应单独返回缺失的 approval domain；active 必须同时存在：

1. 目标 Workspace 的 active owner/admin 客户授权；审批时复核 Membership revision。
2. 与 requester 不同的 platform_owner/platform_security/platform_compliance 平台授权。

默认 grant <=30 分钟，SupportSession <=15 分钟且不得超过 grant。scope 首版只有
`workspace.diagnostics.read`，返回 workspace operational status、Region code、provisioning/health 的
固定枚举和安全计数；禁止成员列表、邮箱、Endpoint 全文、Key/Token、Prompt/Response、文件内容、
策略正文、调用正文、日志正文和对象下载。

平台创建/审批/撤销走 `/platform/support-grants*`；客户审批/撤销使用
`/workspace/support-grants/{grant_id}/*`，保持平台路由只解释 PlatformSession。跨 Workspace 或不存在
grant 同形失败。撤销、到期、Operator/Membership/Workspace revision 变化立即使 SupportSession 失效。

## 8. Break-glass

break-glass 不复用普通 grant，也不放宽字段白名单。`break_glass_sessions` 保存紧急授权与复核事实，只允许 platform_owner 或
platform_security 在 PlatformSession + fresh TOTP 下针对一个 Workspace 和预冻结
`workspace.diagnostics.read` scope 创建，TTL <=10 分钟；申请人不能作事后 reviewer。

创建后通过同一 SupportSession Cookie/key domain 签发 `support_sessions(source_kind='break_glass')`，每请求
联合复核紧急事实；不引入第六种浏览器主体或第二个同用途 Cookie。创建事务写 `break_glass.used`、高优先级
outbox 和不可变审计。到期后进入 `review_pending`，要求 24 小时
内由另一名平台 owner/security/compliance 与目标 Workspace owner/admin 各完成一次 append-only 复核；
逾期保留 overdue 告警，不删除使用事实。break-glass 不能改变 Workspace 配置、角色或生命周期，也不能
访问客户正文和 Secret。

## 9. 不可变平台审计

`platform_audit_logs` 每条保存 actor domain/id、action、object type/id、固定 reason code、request_id、
trace_id、before/after 安全摘要 hash、occurred_at。INSERT 只由受审服务事务执行，UPDATE/DELETE trigger
固定拒绝。`platform_event_outbox` 保存安全事件引用和投递状态，不复制 actor 邮箱、客户名称或自由文本。

所有 Operator、role、session、MFA、bootstrap、grant、approval、revoke、break-glass 和 review 动作均在
业务事实同一事务写审计。列表按当前平台角色裁剪；platform_support 只见自己的 grant 审计，平台安全/
合规见安全域，platform_owner 见全平台安全摘要，但任何角色都不读取客户业务审计正文。

## 10. HTTP 合同

| 方法与路径 | 主体 | 边界 |
| --- | --- | --- |
| `POST /identity/mfa/totp/enrollments` | GlobalSession + password | 一次性 enrollment secret；no-store |
| `POST /identity/mfa/totp/verifications` | GlobalSession | 激活真实 factor，TOTP 防重放 |
| `POST /platform/bootstrap` | GlobalSession + MFA + offline recovery | 一次性创建首个 platform_owner |
| `POST /platform/sessions` | GlobalSession + MFA | 签发 PlatformSession，不接受角色参数 |
| `GET /platform/session` | PlatformSession | 当前 Operator、roles、MFA 与 expiry 安全摘要 |
| `GET/POST /platform/operators` | platform_owner | 有界列表；创建目标必须已有 active MFA |
| `PATCH /platform/operators/{operator_id}` | platform_owner + If-Match | role/status CAS；最后 owner 保护 |
| `GET /platform/navigation` | PlatformSession | 后端角色页面投影，不含 Workspace 页面 |
| `POST /platform/support-grants` | platform_support/owner | 固定诊断 scope、workspace、reason、<=30min |
| `POST /workspace/support-grants/{grant_id}/approve` | Workspace owner/admin | 客户域审批，不接受平台 Cookie |
| `POST /platform/support-grants/{grant_id}/approve` | 平台批准角色 | requester 不得自批 |
| `POST /platform/support-grants/{grant_id}/sessions` | requester PlatformSession | 签发短时 SupportSession |
| `POST .../{grant_id}/revoke` | 对应平台或客户主体 | CAS 撤销并使 Session 立即失效 |
| `GET /support/session` | SupportSession | grant/scope/expiry 安全摘要 |
| `GET /support/workspace/diagnostics` | SupportSession | 固定字段白名单，不接受 workspace 参数 |
| `POST /platform/break-glass-sessions` | owner/security + fresh MFA | 固定 scope、即时告警、<=10min |
| `POST .../{id}/reviews` | 不同平台 reviewer / Workspace reviewer | 事后双域复核 |
| `GET /platform/audit-logs` | 平台审计权限 | role-scoped cursor 分页安全摘要 |

所有 path ID 使用严格前缀/长度。分页游标 HMAC 绑定 PlatformSession、筛选、role revision 和方向；跨角色、
过期、篡改游标固定失败。响应模型 `extra=forbid`，不返回 account_id、tenant_id、legacy user、token HMAC、
MFA secret/counter、权限 fingerprint、内部审批 actor 邮箱或自由文本原因。

## 11. 迁移与回滚

0087 从完整 0086 expand：新建 MFA、bootstrap、Platform IAM、PlatformSession、SupportGrant/Approval/
Session、break-glass、audit/outbox 表和索引，不重建 GlobalAccount、Workspace、Membership 或 legacy RBAC。

迁移完成后复核固定角色/权限目录 hash、五类 Cookie/key domain、FK、`integrity_check` 和 bootstrap open
唯一行。pristine 0087 可 down 到 0086；任何 active/pending MFA enrollment、bootstrap completed、Operator、
平台/支持 Session、grant/approval、break-glass、audit 或 outbox 事实存在时 down 原子失败。运行时回退可
关闭平台路由并清 Platform/Support Cookie，但不得删除 Operator、复开 bootstrap、撤销客户批准或清审计。

## 12. Console 交互

新增独立 `/platform.html`，不复用企业 Console DOM、导航、预取、偏好或缓存。Global 账号安全页提供 MFA
管理和“进入平台工作台”动作；只有后端确认 active Operator 才显示入口，隐藏入口不替代后端鉴权。

平台工作台采用安静、密集的运营布局：紧凑顶栏、角色摘要、左侧平台导航、Operator 表、SupportGrant
队列、审批详情和审计时间线。批准/撤销/break-glass 使用页面内确认、reason code 选单和必要 TOTP 输入；
不使用原生阻塞 confirm。敏感输入提交前从 DOM 清除，Abort/generation 阻止迟到响应回填，Storage 调用
为 0。390/1280/1440 下无重叠、裁剪或横向溢出，键盘可完成全部管理流程。

## 13. 性能、可观测与隐私

- PlatformSession 授权判断 warm p95 `<50 ms`，包含 Operator/factor/role revision 复核。
- Operator/grant 列表 warm p95 `<300 ms`；撤权提交后本地下一请求立即失败，跨实例传播目标 `<5s`。
- 日志只含内部 actor/object ID、action、reason code、revision、request_id/trace_id 和状态；不含邮箱、
  TOTP、MFA secret、recovery secret、Cookie、grant 自由文本或客户名称。
- 本条不产生模型 Usage 或成本账，验收标记 N/A。

## 14. 专项测试

覆盖 0086->0087/fresh/down/up、13 角色目录 hash、MFA enrollment/encryption/replay/skew/限流、bootstrap
并发单例与禁止邮箱提升、Platform/Workspace/Global/Legacy/Support/Device 凭据负矩阵、最后 platform_owner、
Operator 动态撤权、双审批任意顺序与自批拒绝、grant 到期/撤销、Support 字段白名单、break-glass 即时
告警与事后双复核、100 并发审批/Session 换取、游标绑定、CSRF/Origin/Cookie tossing、不可变审计、真实
浏览器三视口/键盘/迟到响应/Storage 清理、p95 门限和敏感 canary 0 命中。

## 15. 第一轮自审：主体、MFA 与 bootstrap

结论：通过，P0/P1 已修正。

- P0：沿用 `global_accounts.mfa_state` 会因 0083 CHECK 永远得到未配置，或诱使实现伪改 schema 根表。
  修正为独立 active MFA factor 权威表，账号摘要按 EXISTS 计算，旧列只作兼容影子。
- P0：只检查六位 TOTP 而不保存 counter 可在同一窗口重放创建多个 PlatformSession。修正为锁内消费单调
  counter，并把 factor revision/counter 固定进 PlatformSession。
- P0：按默认 admin 邮箱 bootstrap 会把 tenant admin 提升为平台 owner。修正为已认证账号 + 密码 +
  active MFA + 独立离线 recovery secret 的一次性 ceremony，正文不接受邮箱/角色。
- P0：只用配置关闭 bootstrap 在备份恢复或错误部署时会重开。修正为数据库 completed tombstone 优先，
  config 只能进一步关闭，不能覆盖已完成状态。
- P1：把 MFA recovery 交给 platform_support 会形成自助提权。修正为本条不提供客服绕过，恢复仅走后续
  受控离线流程。

## 16. 第二轮自审：审批、撤权与隐私

结论：通过，P0/P1 已修正。

- P0：在同一 `/platform/*` 路由任选 PlatformSession 或 WorkspaceSession 会破坏主体分域。修正为客户审批
  使用 `/workspace/support-grants/*`，平台审批保持 `/platform/*`，服务层用 grant owner 连接两类事实。
- P0：双人审批若只要求两个不同 ID，两个平台员工仍可替代客户。修正为一条 active Workspace owner/admin
  approval 加一条不同 requester 的平台 approval，数据库保存 actor domain。
- P0：到期只靠后台任务会留下传播窗口。修正为 SupportSession 每请求复核 grant、approval、revision 和
  expires_at；后台只做状态收敛，不是授权来源。
- P0：break-glass 若绕过字段裁剪会成为永久超级管理员。修正为独立短 Session、相同诊断 allowlist、即时
  告警、不可变使用事实和事后客户/平台双复核。
- P1：平台审计若保存自由文本 reason、邮箱或客户名称，会成为第二敏感数据仓。修正为固定 reason code、
  内部 ID 和安全摘要 hash；outbox 只引用审计事实。
- P1：只撤销 PlatformSession 会让已签发 SupportSession 继续生效。修正为 Operator/grant/factor/revision
  逐请求复核，并在变更事务内批量标记所有派生 Session revoked。

两轮修正已写回本设计，编码不得弱化这些边界。
