# PRD-091 全局用户账号与认证凭据

状态：已完成
阶段：S1-全局身份
优先级：P0
依赖：PRD-090 目标架构与威胁模型
迁移版本：0083

## 1. 目标

把现有绑定 `tenant_id` 的 `users/sessions` 登录主体拆成独立的 `GlobalAccount`、
`AccountCredential`、`GlobalSession` 和只用于兼容追溯的 `AccountIdentityLink`。用户使用
邮箱直接登录全局账号，不再向新身份接口提交租户标识；登录成功只证明“这个人是谁”，
不授予任何 Workspace、企业角色或平台员工权限。

PRD-091 交付账号安全设置和会话/设备管理。现有 `/auth/*`、带 `tid/role` 的旧 Token 和
企业 Console 在 N/N-1 兼容期继续工作。Workspace、Membership、WorkspaceSession 和企业
选择/切换由 PRD-093/094 交付，本条不得提前实现。

## 2. 产品与权限边界

### 2.1 本条交付

- 邮箱规范化、账号状态、密码凭据、MFA 状态预留、凭据 revision 和轮换。
- 全局安全 Cookie Session、本人会话列表、本人撤销、登出和密码轮换。
- 旧 `users` 的安全回填、兼容 Link、seed/用户创建/邀请接受的原子双写。
- 身份成功、失败、撤销和凭据轮换的可追踪事件及持久 Outbox。
- 独立账号安全页面；页面中不出现租户标识、角色、issuer、Token 或内部对象 ID。

### 2.2 明确不做

- 不做公开注册、邮箱验证、找回密码、SSO、SCIM、MFA 挑战或平台员工授权。
- 不创建 WorkspaceMembership，不让 `GlobalAccount` 或 Link 直接访问企业业务数据。
- 不把 `admin@metahub.local` 自动升级为 SaaS 平台管理员；它仍只是旧 default 企业管理员。
- 不把现有企业 Console 改成 GlobalSession，也不删除旧 `/auth/*`。
- 不在企业 Gateway 放入渠道、客户成功、增长、平台品牌、官方 Provider 工程、平台商业账
  或平台运维页面。企业侧仍只承载按企业角色裁剪的 BYOK、路由、额度预算、内部财务、
  员工权限、安全治理、日志审计、开发工具和私有部署能力。

## 3. 主体与凭据隔离

| 主体 | 载体 | 可访问路由 | 明确禁止 |
| --- | --- | --- | --- |
| GlobalSession | `__Host-metahub-global-session` Secure/HttpOnly Cookie | `/identity/*` | tenant、role、permission、Workspace 数据 |
| Legacy Session | `Authorization: Bearer`，旧 JWT 含 `tid/role` | 旧 `/auth/*` 与企业路由 | `/identity/*` |
| Workspace/Platform/Support/Device | 后续独立 issuer/audience/key domain | 各自后续路由 | 由本条创建或互相解释 |

GlobalSession 使用不透明 256-bit secret，不返回给 JavaScript。数据库按随机 `session_id`
定位后，只比较独立 Identity 根密钥分域 HMAC；它没有可以被误当成 tenant 或 role 的 claim。
Identity 根密钥与旧 `METAHUB_AUTH_TOKEN_SECRET` 不得相同。dev/test 使用公开但独立的本地值，
prod 必须显式配置非默认 `METAHUB_IDENTITY_SESSION_SECRET`。

凭据解析按路由固定：`/identity/*` 遇到 Legacy Authorization 固定拒绝；旧企业路由继续只解释
Authorization，并把浏览器附带但不读取的 Global Cookie 视为惰性 Cookie，以免 PRD-091 上线
立即打断旧 Console。任何路由都不得 fallback 到另一种凭据或选择“权限更高”的主体。

## 4. 数据模型

### 4.1 `global_accounts`

- `account_id`：`gac_` 加 32 位小写十六进制，平台全局主键。
- `email_normalized`：规范邮箱；可重复保存冲突事实，但只有 `login_enabled=1` 的值全局唯一。
- `phone_e164`：可空；PRD-091 不开放手机号登录，字段和约束为后续预留。
- `display_name`：账号展示名，不作为认证或授权依据。
- `status`：`active/disabled/locked/migration_conflict`。
- `login_enabled`：只有已形成唯一、安全登录标识的账号为 1。
- `credential_revision/session_revision`：单调递增，密码变化、禁用和撤销按 revision 失败关闭。
- `mfa_state`：本条固定 `not_enrolled`，只预留状态而不伪装已支持 MFA。
- 登录失败计数、锁定截止、最近登录和 UTC 时间戳。

`GlobalAccount` 不带 `tenant_id`、企业角色或平台角色。

### 4.2 `account_credentials`

- 每个可登录账号只有一条 active `password` 凭据；历史通过 version/revision 和轮换事件追踪，
  不保存明文或可逆密码。
- 0083 回填时 `secret_hash` 必须与旧 `users.password_hash` 字节完全一致，算法标记为
  `pbkdf2_sha256_legacy` 或已识别的 `argon2id`。
- Global 登录成功遇到旧 PBKDF2 时，在同一事务中升级为 Argon2id，并同步全部兼容旧 User
  投影；未知或损坏算法固定失败关闭。
- 密码轮换必须重新验证当前密码，递增 credential revision，撤销其他 GlobalSession，并
  原子创建一个替换当前会话。旧密码在提交后立即失效。

### 4.3 `global_sessions`

- 保存 `account_id/session_id/token_hmac/csrf_hmac/status`，只存 HMAC，不存 Cookie 明文。
- 状态为 `active/revoked/expired`，同时有绝对过期和空闲过期。
- 固定记录创建时的 credential/session revision；每次请求权威读取账号与 Session，revision
  不一致或账号非 active 都失败。撤销成功提交后，任何实例的下一请求必须失败。
- 设备只保存有界的 `device_family`、截断风险摘要和 HMAC；不保存完整 IP 或原始 User-Agent。

### 4.4 `account_identity_links`

- `(source_type, legacy_tenant_id, legacy_user_id)` 唯一且不可静默改绑。
- PRD-091 的 `source_type` 只允许 `legacy_user`，并以复合 FK 证明 tenant/user owner 一致。
- Link 只证明迁移来源和密码兼容投影，永远不是 WorkspaceMembership 或授权来源。

### 4.5 冲突与事件

- `identity_migration_conflicts` 保存内部账号 ID、版本化标识指纹和固定 reason code；不复制
  邮箱、密码 Hash、租户资料或角色到事件/Outbox。
- `identity_events` 是 Global Identity 平台域事实。允许的首版事件为
  `identity.login_succeeded`、`identity.login_failed`、`identity.session_revoked`、
  `identity.credential_rotated`。
- 未知账号失败事件的 `account_id` 为空。Payload 只允许固定 schema、reason、revision 和
  时间，不含邮箱、手机号、密码、Cookie、CSRF、Token、完整 IP/UA、tenant、role、Prompt
  或 Response。
- `identity_event_outbox` 与源事件同事务写入；投递不可用时保留 pending 事实，不签发缺少
  源事件的 Session。

## 5. 标识规范化与迁移冲突

应用规范邮箱的规则固定为：去除首尾 ASCII/Unicode 空白，NFKC，单个 `@`，域名经 IDNA
转 ASCII 并小写，local part 仅做 Unicode NFKC 和 ASCII 大小写折叠；不猜测 Gmail 点号或
加号语义。密码不 trim、不做 Unicode 规范化，按 UTF-8 字节限制 8..1024 bytes。

SQLite 0083 无法安全执行完整 Unicode/IDNA 规范化，因此只把满足 ASCII 邮箱合同且规范值
全局唯一的旧 User 标成可登录。非 ASCII、非法形状或重复规范邮箱全部进入
`migration_conflict/login_enabled=0`。公开登录对 unknown、wrong password、disabled、locked、
bad hash 和 conflict 返回完全相同的 HTTP 状态、code、body schema、缓存头和有界时延。

禁止仅因邮箱、密码 Hash、显示名或状态相同而跨 tenant 自动合并。默认是一条 legacy User
对应一个 GlobalAccount；后续只有完成邮箱验证并对每个来源账号重新认证的受控 claim 流程
才能合并多个 Link。PRD-091 不实现该 claim 流程，也不会通过尝试多个旧密码隐式选择 tenant。

## 6. HTTP 合同

所有 `/identity/*` 强制由服务端生成 request_id/trace_id，不接受客户端链路 ID。所有成功、
4xx、422 和 5xx 响应固定包含 `Cache-Control: no-store, private`、`Pragma: no-cache`、
`Referrer-Policy: no-referrer` 和统一 request_id。

| 方法与路径 | 认证/保护 | 成功结果 |
| --- | --- | --- |
| `POST /identity/login` | 精确 Origin、Fetch Metadata、严格 JSON；拒绝 Authorization/tenant 字段 | 账号安全摘要、当前会话摘要；设置 Global/CSRF Cookie |
| `GET /identity/me` | Global Cookie，权威 Session+Account revision | 本人账号和当前会话安全摘要 |
| `POST /identity/logout` | 精确 Origin；有效 Cookie 时要求绑定 CSRF | 幂等清 Cookie；有效 Session 写撤销事实 |
| `GET /identity/sessions` | Global Cookie；有界 limit/offset | 只返回本人设备摘要，当前会话优先 |
| `DELETE /identity/sessions/{session_id}` | Global Cookie、Origin、CSRF | 幂等撤销本人 Session；他人与不存在同形 404 |
| `POST /identity/credentials/password/rotate` | Global Cookie、Origin、CSRF、当前密码重认证 | 原子轮换、撤销旧 Session、签发替换会话 |

登录、轮换正文使用有界原始字节流、唯一 `application/json`、重复 key 拒绝、exact field set 和
严格类型。Pydantic/JSON 异常不得把 password 或 identifier 放入异常文本、日志或响应。

`GET /identity/me` 和会话列表禁止返回 tenant/workspace/link/role/permission、credential/hash、
Cookie/CSRF、完整 IP/UA 或平台员工状态。跨账号 Session ID 与不存在 Session 使用同形 404。

## 7. Browser Cookie、Origin 与 CSRF

- Session Cookie：`__Host-metahub-global-session`，Secure、HttpOnly、SameSite=Lax、Path=/、
  无 Domain；登录和轮换总是生成新值，防止 fixation。
- CSRF Cookie：`__Host-metahub-global-csrf`，Secure、SameSite=Strict、Path=/、无 Domain，
  可由同源 UI 读取；请求头 `X-CSRF-Token` 必须与 Cookie 单值、数据库绑定 HMAC 常量时间一致。
- 写接口只接受唯一且精确匹配 allowlist 的 Origin；拒绝 `Sec-Fetch-Site: cross-site`、重复
  Origin/Cookie/Authorization、逗号合并值和空值。
- CORS 只允许配置中的精确 Origin，开启 credentials，不允许 `*`；只增加受审的
  `X-CSRF-Token`。
- 登出即便 Session 已过期或撤销也清理浏览器 Cookie。若存在可验证 Cookie，撤销事实失败则
  返回固定 503，但仍通过响应删除 Cookie，不能留下半可信浏览器状态。

## 8. 0083 迁移与兼容双写

### 8.1 正向

1. 0082 完整性、外键和最高 applied version 必须先通过 Up Guard。
2. 创建六类全局身份表、索引、复合 owner FK、状态/格式 CHECK 和不可变事件约束。
3. 每条旧 User 创建独立 GlobalAccount、原 Hash Credential 和唯一 Legacy Link。
4. 仅唯一且满足 ASCII 安全规范的邮箱启用登录；重复/非法/非 ASCII 进入冲突隔离。
5. 不修改旧 `users/sessions` 的行、Hash、状态、角色和 Session 撤销事实。
6. 安装 legacy User insert/password/status 同步触发器。seed、`POST /users` 和邀请接受保持
   原事务，触发器或共享 helper 必须在提交前形成完整投影；任一侧失败整体回滚。

seed 重复执行不得重新随机轮换同一明文密码的 Hash；已有 Hash 可验证时复用原字节，从而保证
Global Credential 与旧 User 完全一致。创建第二个相同规范邮箱时，两个账号都立即转冲突隔离，
不能保留“先创建者可登录”的排序偏差。

### 8.2 回滚

- 未产生登录、撤销、轮换、冲突或新增全局账号事实，且 Account/Credential/Link 仍与旧 User
  一一等价时，允许 pristine 0083 down；旧 User Hash 和 Session 完整保留。
- 一旦存在 GlobalSession、Identity Event/Outbox、credential revision > 1、冲突或无法由旧
  User 完整表达的账号，down guard 在同一事务原子拒绝，版本和所有表保持不动。
- 运行时回滚只关闭新 identity 读路径，旧 `/auth/*` 继续工作；不得删除撤销 tombstone、恢复
  旧密码或把 conflict 账号改成可登录。

## 9. 状态机与失败关闭

`active -> locked -> active` 只由失败阈值和锁定截止推进；`active/locked -> disabled` 立即递增
session revision 并撤销全部 Session；`migration_conflict` 不能由登录接口自行恢复。

登录失败计数必须在 `BEGIN IMMEDIATE` 内原子读取/更新。unknown 账号执行同成本 dummy verifier，
所有不可登录分支统一填充到相同最小处理时间并加有界随机抖动。数据库、Identity 密钥、Hash
解析、事件/Outbox 或提交失败一律不设置 Cookie。登录成功事件、Session 和 Outbox 同事务提交。

Session 校验不使用“token claim 正确就放行”的缓存。SQLite 当前每请求读取权威 Session 和
Account；未来缓存必须带单调 revision，撤销提交后不得接受旧 generation。

## 10. 性能与可观测

- warm 登录 p95 < 500 ms；包含密码 KDF、Session、事件和 Outbox 单事务。
- warm Session 校验 p95 < 30 ms；单次有索引的 Session+Account 读取，不加载租户 RBAC。
- 并发撤销以数据库提交为线性化点；提交后的请求成功数必须为 0。
- 事件按 request_id、account_id/session_id 内部标识、reason code 和 revision 追踪成功、失败、
  撤销、轮换；身份请求不产生模型 Usage 或成本账，测试记录明确标记 N/A。
- 日志只记录固定事件名、内部 ID、request_id、状态和延迟，不记录标识符、凭据或正文。

## 11. Console 交互

新增独立账号入口，不改变现有企业 Console 的 tenant-bound 登录流程。全局登录表单只有邮箱和
密码。登录后先显示账号安全摘要，再显示当前设备和其他会话；撤销其他设备需确认，撤销当前
设备后返回登录页；密码轮换完成后保留一个替换当前会话。

页面不得显示 GlobalSession、issuer、account_id、tenant_slug、迁移状态等研发字段，不提供
注册/SSO/SCIM/Workspace 切换入口。Cookie 对 JavaScript 不可读，浏览器
localStorage/sessionStorage 不得出现 Token 或 tenant 上下文。

## 12. 专项测试与验收

按 WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW 成组交付，至少覆盖：

1. 0082->0083、fresh seed、幂等、pristine down/up、冲突/安全事实 guarded down、integrity ok、FK 0。
2. 同邮箱跨 tenant 即使 Hash/状态相同也不自动合并；两个账号均隔离且不隐式选择 tenant。
3. Legacy/Global/Workspace/Platform/Device 凭据与路由负矩阵、重复 Header/Cookie 和混合凭据。
4. unknown/wrong/disabled/locked/conflict/bad-hash 同状态、code、schema、header、长度桶和时延分布。
5. 登录、me、两设备、远端撤销、撤销后下一请求失败、轮换、旧 Auth 新 Hash 兼容和幂等 logout。
6. Origin/Fetch Metadata/CSRF、Cookie tossing/fixation、Session IDOR、账号禁用和并发 100 撤销。
7. 登录 p95、校验 p95、SQLite 并发锁、100 Session 有界分页和无 5xx。
8. Chromium/Edge/Firefox，390/1280/1440 三视口真实 Control+Console+fresh 0083 数据库验收。
9. DOM、Storage、日志、事件、Outbox、SQLite 普通列和正式证据敏感 canary 命中 0。
10. 旧 `/auth/*` 与旧企业 Console 保持可用；Global Cookie 不能调用任一企业业务 API。

## 13. 第一轮自审：范围、架构与迁移

结论：通过，P0/P1 已修正。

- P0：原计划只列五个 API，无法验收“凭据轮换”。修正为增加
  `POST /identity/credentials/password/rotate`，并要求同步旧 User Hash、撤销旧 Session 和
  原子签发替换会话。
- P0：按相同邮箱或相同 Hash 自动合并会让任一弱旧密码接管多个企业。修正为每条 Legacy User
  独立账号，重复邮箱全部冲突隔离；重新验证和 claim 留给后续 PRD。
- P0：迁移先于 seed，单靠 0083 回填会遗漏新安装管理员。修正为 User insert/password/status
  原子兼容投影，并要求 seed Hash 稳定复用。
- P0：Link 的 owner 和授权语义不清。修正为复合 tenant/user FK，只作迁移追溯，明确不能授权。
- P1：GlobalSession 若直接替换现有 Console 会在 Membership 尚未交付时造成越权或不可用。
  修正为新旧并行，企业 Session 切换留给 PRD-094。
- P1：pristine down 与撤销事实保真冲突。修正为仅等价投影允许 down，任何安全事实均原子阻止。

## 14. 第二轮自审：安全、隐私、并发与可验证性

结论：通过，P0/P1 已修正。

- P0：普通 Pydantic 422 可能保存并回显 password input。修正为 identity 写接口统一使用严格有界
  原始 JSON parser，所有异常折叠固定错误码。
- P0：客户端可把邮箱/密码/Cookie/CSRF 复制进 request_id。修正为 `/identity/*` 全部忽略客户端
  链路 Header并由服务端生成 ID，未捕获异常也只记录固定事实。
- P0：全局拒绝“双凭据”会因 Secure Cookie Path=/ 打断旧 Console。修正为 route-specific 规则：
  identity 路由拒绝 Authorization；旧企业路由不读取 Global Cookie，且双方绝不 fallback。
- P0：撤销后使用缓存可能继续放行。修正为每请求权威复核 Session、Account 和 revision，以提交为
  线性化点，失败不回退 Cookie 内容。
- P0：Cookie 只有 HttpOnly 仍不能防 CSRF。修正为 host-only Secure Cookie、精确 Origin、Fetch
  Metadata 和绑定 Session 的双提交 CSRF，重复或合并 Header/Cookie 全部拒绝。
- P1：原始 UA/IP、邮箱或 conflict 资料可能扩散到设备列表和事件。修正为有界设备族、HMAC 风险
  摘要、固定 reason code，UI/事件/Outbox 不复制个人标识或企业归属。
- P1：用户名义锁定若非原子会在并发下丢计数。修正为 `BEGIN IMMEDIATE`、CAS 状态和并发专项。

两轮自审修正已全部写回本设计；编码不得弱化这些边界。PRD-091 完成后仍保持
`saas_incremental_roadmap_complete=false` 和 `all_108_prds_complete=false`。

## 15. 完成门禁复核：实现与运行时

结论：通过，新增 P1 已修正并补专项门禁，不改变前两轮冻结范围。

- P1：unknown 使用 PBKDF2 dummy、已升级账号使用 Argon2id，原 120 ms padding 低于两者真实成本，
  可按时延区分。修正为所有失败至少 1.25 s 加有界随机抖动，并以真实分布门禁覆盖 legacy、
  Argon2id、unknown、disabled 和 locked。
- P1：异常登出会在 Cookie 删除前抛错。修正为同源登出进入凭据处理后向统一错误处理器登记固定
  Cookie 删除动作，过期、撤销和 503 均删除浏览器凭据，同时保留权威错误码。
- P1：DELETE/rotate 在 `_authenticate` 标记 expired 后统一 rollback，导致 tombstone 丢失。修正为
  只对 `identity.session_expired` 提交过期事实，其他错误仍回滚。
- P1：rotate 在 Session/CSRF/当前密码之前执行 64 MiB Argon2，且同步 KDF/SQLite 阻塞事件循环。
  修正为先验证权威凭据，再计算新 Hash，以二次事务校验关闭竞态；全部 identity 数据库/KDF 工作
  进入容量为 4 的后台线程边界。
- P1：原实现 `request.body()` 先整块载入再检查 4096 bytes。修正为流式累计，超限立即拒绝，不保留
  超限正文。
- P1：legacy Hash 只看前缀并直接采用 rounds。修正为迁移与运行时共用严格 profile parser，仅接受
  固定 PBKDF2/Argon2 参数、规范编码和有界长度；异常 Hash 直接隔离，不调用 KDF。
- P1：路线图把本条误写为创建 WorkspaceMember 并向 `platform_security` 投影。修正 Excel 生成源和
  跨表验证器：本条仅账号本人，Workspace/Membership 属于 PRD-093/094，平台角色不获得账号页权限。
- P1：完整 0083 SQLite schema 在每个新连接首次查询时需重新解析，实测约 170..207 ms，导致
  Session 校验无法满足 warm p95。修正为 Identity worker 边界最多保留 4 个空闲连接；每个请求
  独占一个 handle，归还前强制 rollback 并恢复 query-only、read-uncommitted、FK 和 busy-timeout，
  并发溢出直接创建独立连接而不阻塞事件循环。真实 loopback HTTP 实测 warm 登录 p95
  279.684 ms、Session 校验 p95 21.898 ms。
