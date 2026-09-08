# PRD-092 个人与企业用户自助注册验证

状态：已完成
阶段：S1-全局身份
优先级：P0
依赖：PRD-091、现有 PRD-060 Invitation

## 1. 目标与边界

本条只扩展 Global Identity。未登录用户可以注册、验证邮箱、重发验证码和找回密码；受邀用户完成验证后保留邀请接续事实。成功验证只签发 GlobalSession，不创建 Workspace、Tenant、Membership、WorkspaceSession 或平台角色。

明确不做：手机号渠道、社交登录、SSO/SCIM、Workspace 创建、成员授权、平台员工授权。验证码、找回凭据和 Invitation Token 不得出现在 URL query、日志、事件、浏览器 Storage、普通响应 DTO 或数据库明文列。

## 2. 产品流程

### 2.1 注册

1. 用户提交邮箱、展示名、密码、隐私条款版本和可选 Invitation Token。
2. 服务端先执行有界输入、Origin、Fetch Metadata 和持久滥用门禁，再执行密码 KDF。
3. 服务端创建 RegistrationIntent 和一次性 VerificationChallenge，通过受控渠道投递验证码。
4. API 始终返回相同的 pending 摘要；已存在邮箱、不可用邀请和 sink intent 不泄露真实分支。
5. 验证码提交成功后，在一个事务中创建或恢复 GlobalAccount、保存 ConsentRecord、消费 Challenge、写 IdentityEvent/Outbox，并签发 GlobalSession。

已存在可登录 GlobalAccount 时，注册密码必须先匹配当前凭据，随后仍需邮箱验证码；二者都成功才允许恢复该账号并接续邀请。密码不匹配的重复注册进入同形 sink intent，不能把邮箱验证降级成隐式密码绕过。

### 2.2 找回密码

找回请求无论账号是否存在都创建公开同形 intent 并返回相同 DTO。只有可登录且允许重置的账号会绑定真实 Challenge。确认时一次性消费 Challenge、写入 Argon2id 新 Hash、递增 credential/session revision、撤销全部旧 GlobalSession、写事件/Outbox并签发一个替换 Session。提交后旧密码和旧 Session 立即失效。

### 2.3 邀请接续

PRD-060 的旧 `/invitations/{token}/accept` 在 N/N-1 继续工作。新注册流只把 Invitation Token 的哈希命中绑定到已验证 account，写入 `invitation_identity_bindings` 的 `verified_pending_membership` 事实；不提前创建 Membership。PRD-094 原子消费该绑定并升级旧 Invitation。无效、过期、已消费或邮箱不匹配的邀请与未知邀请同形失败，不能证明目标企业存在。

## 3. 数据模型与 owner

0084 新增以下平台全局对象：

- `registration_intents`：`rgi_` ID、邮箱 HMAC/规范邮箱密文边界、候选 Argon2id Hash、展示名、状态、attempt revision、可选 invitation binding、到期和时间戳。
- `verification_challenges`：`vch_` ID、intent、purpose、验证码 HMAC、attempt count、generation、状态、发送/到期/消费时间。数据库不保存原始验证码。
- `password_reset_intents`：`pri_` ID、可空 account、标识 HMAC、状态、generation、到期和时间戳。unknown account 使用 sink 行保持同形。
- `account_verifications`：账号、验证类型、规范标识 HMAC、验证时间和 source intent 的不可变事实。PRD-093 只接受满足策略的验证事实，不能用 `GlobalAccount.status=active` 猜测“已验证”。
- `consent_records`：账号、条款类型、精确版本、来源、同意时间和不可变 payload 摘要；不保存 IP/UA 明文。
- `identity_abuse_buckets`：全局 Identity owner 下的 HMAC bucket、scope、窗口、计数和到期。公开阶段没有 Workspace，因此不得带伪造 tenant owner。
- `invitation_identity_bindings`：Invitation、account、email fingerprint、状态和 revision；只证明已验证接续，不授予企业权限。

Excel `03_核心数据对象` 当前把 AbuseCounter 归为 Workspace/租户，这是与“本条不创建 Workspace”冲突的 P0 规划错误。实现时同步修正生成源、Excel 和 validator，使其归属 `平台全局/Identity abuse domain`。

低熵验证码使用独立 `METAHUB_IDENTITY_CHALLENGE_SECRET` 做分域 HMAC；不得使用裸 SHA-256。该根密钥与旧 Auth、Identity Session、Cache、Webhook 等密钥不同，prod 必须显式配置至少 32 字符的非默认值。

未验证邮箱使用独立 `METAHUB_IDENTITY_PII_ENCRYPTION_SECRET` 的 AES-256-GCM 信封保存；AAD 固定绑定 registration purpose、RegistrationIntent ID、`email_normalized` 字段类型和 key version。该根密钥不能复用 Auth、Identity Session、Challenge、Cache 或其他数据加密根，Control API prod 必须显式配置非默认值。重发和验证只在当前调用栈内解密，信封格式、owner、tag 或密钥错误一律映射为 `identity.unavailable`。

## 4. 状态机与并发

`RegistrationIntent`: `pending -> verified | expired | superseded | sink_consumed`。

`VerificationChallenge`: `pending -> consumed | expired | exhausted | superseded`。

`PasswordResetIntent`: `pending -> completed | expired | exhausted | sink_consumed`。

每次重发递增 generation 并使旧 Challenge 原子 superseded。验证和重置在 `BEGIN IMMEDIATE` 内按 `id + generation + pending + expires_at` CAS；100 个并发请求最多一个提交成功。错误 attempt 在锁内累加，达到上限后进入 exhausted。成功提交前不得设置 Cookie，渠道投递失败不得伪装成已发送。

## 5. HTTP 合同

所有路由位于 `/identity/*`，拒绝 Authorization、tenant/workspace/role 字段和客户端链路 ID；正文使用 PRD-091 同一有界严格 JSON parser。写请求要求唯一精确 Origin，拒绝 cross-site Fetch Metadata。

| 方法与路径 | exact 请求 | 成功响应 |
| --- | --- | --- |
| `POST /identity/registrations` | `email,display_name,password,consent_version,invitation_token?` | pending intent 摘要，不返回验证码/邀请有效性 |
| `POST /identity/verifications` | `intent_id,code` | 验证后的账号摘要与 GlobalSession Cookie |
| `POST /identity/verifications/resend` | `intent_id` | 同形 pending 摘要和 retry 时间 |
| `POST /identity/password-resets` | `email` | 同形 pending reset 摘要 |
| `POST /identity/password-resets/confirm` | `intent_id,code,new_password` | 密码轮换后的账号摘要与替换 Cookie |

intent ID 是随机关联句柄，不是验证凭据；仍需验证码 HMAC、状态、generation、时限和滥用门禁。所有响应带服务端 request_id/trace_id、`no-store, private`、`Pragma: no-cache` 和 `Referrer-Policy: no-referrer`。

## 6. 投递适配器

定义有界 `IdentityChallengeDelivery` 接口，只接收目标地址、原始验证码、purpose、到期和服务端关联 ID。默认生产模式在未配置正式渠道时失败关闭，不记录验证码。测试可使用进程内 `test_capture` adapter；prod 配置显式拒绝该模式。事件/Outbox只记录 `challenge_dispatched/failed` 的固定 reason 和不可逆标识 HMAC，不复制目标地址或验证码。

## 7. 防枚举与滥用

- 邮箱、IP、设备 cookie 和全局窗口分别生成独立 HMAC bucket；原始 IP/设备值不落库。
- 门禁在 Argon2、数据库大事务和渠道调用前执行；正常登录使用独立容量，不被公开注册耗尽。
- unknown、duplicate、disabled、conflict、bad hash 和不可用邀请采用相同 HTTP 状态、body schema、长度桶和最小时延窗口。
- resend 的 retry、过期和耗尽不能证明 intent 是否绑定真实账号。
- 限流存储不可用时，公开创建/重发/找回失败关闭；已有账号登录不依赖该公开限流存储。

## 8. 迁移与回滚

0084 只从完整 0083 升级，前后执行 integrity/FK guard。迁移不修改既有 GlobalAccount、User、Session 或 Invitation 行。

pristine down 仅允许所有 intent/challenge/consent/binding/event 表为空且没有 Identity revision 变化。任何已验证账号、密码重置、邀请绑定、消费/撤销 tombstone 或事件均原子阻止 schema down。运行时回滚可关闭五个新路由并让未完成 intent 自然过期，但不能删除已创建账号、恢复旧密码或复活旧 Session。

## 9. Console 交互

账号入口增加注册、验证和找回三个视图。密码和验证码字段离开视图、失败、注销或 abort 时立即清空。验证码使用单字符或单输入控件均可，但必须支持粘贴、键盘、自动填充和读屏，不把值写入 URL、DOM attribute 或 Storage。邀请接续页面只显示通用“验证后继续”，不在验证前展示企业名、角色或成员状态。

## 10. 性能与可观测

- 注册/验证 API warm p95 `<500 ms`，不包含邮件/短信最终送达。
- 公开滥用门禁查询 p95 `<30 ms`；正常登录在公开入口压力下仍满足 PRD-091 门限。
- 事件只含内部 ID、purpose、固定 reason、generation、request_id/trace_id 和 revision。
- 注册、验证和找回不产生模型 Usage 或成本账，验收标记 N/A。

## 11. 专项测试

覆盖 WB/API/BB/GR/PERF/WEB/UI/UCD/COMP/SEC/FLOW：0083->0084、fresh/down/up、状态机、100 并发单次消费、unknown/duplicate 同形、验证码重放/重发、渠道故障、数据库故障、弱密码、邀请接续、已有账号密码匹配、重置撤权、CSRF/Origin/Header/JSON 歧义、390/1280/1440、键盘/读屏、Storage/DOM/log/DB 敏感 canary 0 命中，以及旧 login/invitation N/N-1 回归。

## 12. 第一轮自审：范围、owner 与迁移

结论：通过，P0/P1 已修正。

- P0：AbuseCounter 被规划为 tenant-owned，但注册发生在 Workspace 之前。修正为 Global Identity HMAC bucket，并要求同步修正权威生成源和 validator。
- P0：如果 092 直接消费旧 Invitation 并创建 User，会提前实现 Membership 且无法支持已有账号。修正为只写 verified binding，094 再原子升级。
- P0：重复邮箱若只凭邮箱验证码恢复账号，会把邮箱渠道静默提升为密码替代。修正为已有账号同时要求当前密码匹配和邮箱验证码。
- P1：无正式邮件/短信渠道时返回“已发送”属于虚假成功。修正为生产失败关闭，测试 capture 模式在 prod 明确禁止。
- P1：已验证账号或密码重置属于不可逆安全事实，不能随 0084 down 删除。修正为 guarded down 和功能开关回滚。

## 13. 第二轮自审：安全、隐私、并发与体验

结论：通过，P0/P1 已修正。

- P0：六位码用裸 Hash 可离线穷举。修正为独立 challenge 根密钥 HMAC、短 TTL、attempt 上限和 generation。
- P0：先做 Argon2 再限流可被匿名请求耗尽 worker。修正为持久多维门禁先行，KDF 与渠道使用独立有界容量。
- P0：重发若复用旧 generation，会让两个验证码同时有效。修正为事务内 supersede + generation CAS。
- P0：Pydantic/渠道异常可能保存邮箱、密码或验证码。修正为流式 strict JSON、固定错误和禁止原始异常日志。
- P1：邀请页提前显示企业/角色会形成枚举。修正为验证前只显示通用接续文案。
- P1：迟到响应可能在用户离开后重填密码/验证码视图。修正为 AbortController generation、DOM 清理和 Storage 零写入门禁。

两轮修正已写回本设计，编码不得弱化这些边界。

## 2026-09-08 原生数据库注册限流补充自审

PRD-147 的真实 PostgreSQL Cookie 冒烟在注册前置阶段返回 identity.unavailable。安全诊断
定位为 _consume_rate_limits 的过期清理 DELETE 使用 SQLite rowid，原生库报 SQLSTATE 42703。

第一轮自审（同一事实与可移植性）：只把过期批量删除的外层/子查询键改为既有
identity_abuse_bucket_id 主键，不改 schema、不映射 PostgreSQL ctid、不创建平行计数器。
过期谓词、每笔最多 50 条清理及四个 scope 的原子 upsert/RETURNING 保持不变。

第二轮自审（拒绝与回滚）：清理与限流累计仍共享 BEGIN/COMMIT；INSERT 故障须回滚已清理
记录，超限仍先提交持久封禁再返回 429。用无 rowid 表执行生产 helper，验证三类操作的
50 条边界、未过期记录保留、计数累计、限流持久化和写失败回滚，再通过真实 PostgreSQL
注册/验证/建空间链路检验，不用固定或伪造 Session 替代认证。该修复独立记录和提交。
