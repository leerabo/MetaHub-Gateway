# Global Identity API

PRD-091..092 提供与旧企业 `/auth/*` 并行的全局账号身份域。GlobalSession 只证明账号本人，
不包含 `tenant_id`、Workspace、平台角色或企业权限；所有企业业务 API 仍要求旧 Bearer
或后续 PRD-094 的 WorkspaceSession，并由后端逐请求授权。

## 浏览器安全合同

- Session Cookie：`__Host-metahub-global-session`，`Secure`、`HttpOnly`、`SameSite=Lax`、`Path=/`、无 Domain。
- CSRF Cookie：`__Host-metahub-global-csrf`，`Secure`、`SameSite=Strict`；写请求必须以唯一 `X-CSRF-Token` 回传。
- 匿名限流 Cookie：`__Host-metahub-identity-device`，`Secure`、`HttpOnly`、`SameSite=Strict`；
  只作为 HMAC bucket 输入，不是身份凭据，服务端不保存原值。
- 写请求只接受配置中的精确 `Origin`，拒绝 cross-site Fetch Metadata、重复/合并凭据和任何 `Authorization`。
- `/identity/*` 忽略客户端 `X-Request-ID`/`X-Trace-ID`，全部响应带服务端生成的标识及 `Cache-Control: no-store, private`。
- Cookie、CSRF、密码、Token、完整 IP/User-Agent 不进入日志、事件 payload 或公开响应。
- 登录和改密正文按原始字节流读取，超过 4096 bytes 立即拒绝；密码 KDF 与 SQLite 在容量为 4 的
  后台执行边界运行，不阻塞 Control API 事件循环。
- 同源登出一旦进入凭据处理，即使 Session 已过期、撤销或数据库暂不可用，错误响应也删除两个
  浏览器 Cookie；服务端撤销失败仍返回统一 `503 identity.unavailable`，不能伪装成功。

## 接口

| 方法 | 路径 | 请求 | 成功响应 |
| --- | --- | --- | --- |
| POST | `/identity/login` | `{email,password}` | 账号与当前设备摘要；设置两个 Cookie |
| GET | `/identity/me` | Session Cookie | 账号与当前设备摘要 |
| POST | `/identity/logout` | Origin、Cookie、CSRF | `{logged_out:true}` 并清 Cookie |
| GET | `/identity/sessions?limit=20&offset=0` | Session Cookie | 本人 active 设备有界分页 |
| DELETE | `/identity/sessions/{session_id}` | Origin、Cookie、CSRF | `{revoked:true}`；跨账号与不存在同形 404 |
| POST | `/identity/credentials/password/rotate` | `{current_password,new_password}`、Origin、Cookie、CSRF | Argon2id 轮换并签发替换会话 |
| POST | `/identity/registrations` | `{email,display_name,password,consent_version,invitation_token?}` | 同形 pending intent；不返回验证码或邀请有效性 |
| POST | `/identity/verifications` | `{intent_id,code}` | 原子验证并签发 GlobalSession；不创建 Workspace/Membership |
| POST | `/identity/verifications/resend` | `{intent_id}` | generation 递增后的同形 pending 摘要 |
| POST | `/identity/password-resets` | `{email}` | unknown/disabled/active 同形 pending reset |
| POST | `/identity/password-resets/confirm` | `{intent_id,code,new_password}` | 撤销旧会话、轮换 Argon2id 并签发替换会话 |

账号和会话摘要采用严格白名单，不返回 IdentityLink、凭据 revision、Hash、完整网络信息或
任何 Workspace/tenant/role/permission。登录未知账号、错误密码、禁用、锁定、迁移冲突和
损坏 Hash 均返回 `401 identity.login_invalid`。
运行时只接受参数固定的 `pbkdf2_sha256$200000` legacy Hash 和 MetaHub Argon2id profile；Hash
声明的 rounds、memory、time 或 parallelism 不能选择 KDF 成本。所有登录失败填充到同一最小
处理时间窗口，避免通过 legacy/Argon2/未知账号的时延区分身份状态。

## 运行配置

`METAHUB_IDENTITY_SESSION_SECRET` 是独立 HMAC 根密钥，不能复用旧
`METAHUB_AUTH_TOKEN_SECRET` 或其他业务密钥。dev/test 有公开本地默认值；prod 必须显式
设置至少 32 字符的独立值。运行依赖包含 `argon2-cffi`。

PRD-092 的六位验证码使用另一个独立根域 `METAHUB_IDENTITY_CHALLENGE_SECRET`；不能复用
Auth、GlobalSession 或 Cache 密钥。`METAHUB_IDENTITY_CHALLENGE_DELIVERY_MODE` 默认
`disabled`，此时公开创建/重发端点返回 `503 identity.delivery_unavailable`。非生产专项测试
可显式使用 `test_capture`；生产配置该值会在启动时失败，且当前没有隐式邮件、日志或响应回显
回退。正式投递适配器上线前，生产注册能力保持失败关闭。

未验证 RegistrationIntent 的规范邮箱只以 HMAC 和 owner-bound AES-256-GCM 信封落库。
`METAHUB_IDENTITY_PII_ENCRYPTION_SECRET` 是独立根密钥，不能复用 Auth、GlobalSession、
Challenge、Cache 或其他数据加密根；Control API 生产环境必须显式配置至少 32 字符的非默认值。
信封 AAD 绑定 registration purpose、intent ID、字段类型和 key version；格式、owner、tag 或密钥
错误统一返回 `503 identity.unavailable`，不暴露失败维度。重发和成功验证只在当前调用栈内解密。
