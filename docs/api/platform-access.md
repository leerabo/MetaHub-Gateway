# Platform Access API

PRD-095 提供独立于企业 Workspace RBAC 的 Platform IAM、短时受控支持访问和不可变平台审计。
GlobalSession 只能用于真实 MFA 与 PlatformSession 换取；WorkspaceSession 只能完成客户域审批；
PlatformSession 不能直接访问 `/support/*`，SupportSession 也不能调用普通 Workspace API。

## 凭据与请求边界

- Platform Cookie：`__Host-metahub-platform-session` 为 Secure、HttpOnly、SameSite=Lax；
  `__Host-metahub-platform-csrf` 为 Secure、SameSite=Strict。两者均为 `Path=/`、无 Domain。
- Support Cookie：`__Host-metahub-support-session` 为 Secure、HttpOnly、SameSite=Lax、`Path=/`、无 Domain。
- `POST /platform/sessions` 只解释 GlobalSession、global CSRF、Origin 和 TOTP；成功后清除当前浏览器的
  Workspace/Support Cookie，并轮换旧 PlatformSession。
- `/platform/*` 只解释 PlatformSession；`/support/*` 只解释 SupportSession；客户审批和复核路由只解释
  WorkspaceSession。任何 `Authorization`、重复当前域 Cookie/CSRF 或混合可解释凭据均失败关闭。
- 平台写请求要求唯一允许的 Origin、当前域 CSRF、精确 `application/json`、唯一字段和不超过 8192 bytes
  的正文。所有成功和错误响应均为 `Cache-Control: no-store, private`。

## MFA 与平台入口

| 方法 | 路径 | 主体与请求 | 成功结果 |
| --- | --- | --- | --- |
| POST | `/identity/mfa/totp/enrollments` | GlobalSession、global CSRF；`{current_password}` | 一次性 factor ID、Base32 secret、`otpauth://` URI 和到期时间 |
| POST | `/identity/mfa/totp/verifications` | GlobalSession、global CSRF；`{factor_id,code}` | 原子激活 factor，递增 revision 并保存防重放 counter |
| GET | `/identity/platform-access` | GlobalSession | 仅返回后端权威 `available: bool` |
| POST | `/platform/bootstrap` | GlobalSession、global CSRF；密码、TOTP、离线恢复密钥、固定 reason 和显式确认 | 全库唯一首个 active `platform_owner`；不签发 PlatformSession |
| POST | `/platform/sessions` | GlobalSession、global CSRF；`{totp_code}` | 签发 Platform/CSRF Cookie 和安全 operator 上下文 |
| GET | `/platform/session` | PlatformSession | operator、roles、permissions 和绝对到期时间摘要 |
| GET | `/platform/navigation` | PlatformSession | 当前数据库角色/权限计算的平台导航投影 |

TOTP secret 使用 `METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET` 做 AES-256-GCM 信封加密，AAD 绑定 account、
factor、版本和用途。验证允许当前计数器前后各一个窗口，但同一计数器只能消费一次；失败尝试按
account、factor 和设备摘要持久限流。首版不提供短信、WebAuthn、客服绕过或在线 MFA 恢复。

bootstrap 默认关闭。启用时必须显式配置独立 `METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET`；数据库
completed tombstone 优先于配置，完成后不能通过重启、恢复或重新启用配置再次执行。请求正文不接受
account、email、operator 或 role 字段。

## 平台角色

固定目录包含 13 个角色，运行时从数据库 role binding 计算权限，不能按角色名或前端菜单补权：

| 角色 | PRD-095 权限边界 |
| --- | --- |
| `platform_owner` | Operator 管理、全平台安全审计、支持申请/审批、break-glass 和 Workspace 运营占位权限 |
| `platform_security` | 安全审计、平台侧支持审批、break-glass 和风险处置占位权限 |
| `platform_compliance` | 安全审计、平台侧支持审批和合规占位权限 |
| `platform_support` | 申请自己的 SupportGrant、查看自己的 grant 与对应审计 |
| `platform_ops` | Workspace 目录与生命周期占位权限 |
| `platform_billing`、`platform_verification`、`platform_privacy` | 各自业务域只读占位权限 |
| `release_manager`、`platform_provider_engineer` | 发布和 Provider 工程占位权限 |
| `platform_channel`、`platform_customer_success`、`platform_growth` | 渠道、客户成功和增长占位权限 |

| 方法 | 路径 | 权限与请求 | 成功结果 |
| --- | --- | --- | --- |
| GET | `/platform/operators?limit=&offset=` | `platform.operator.manage` | 有界 Operator 列表 |
| POST | `/platform/operators` | 同上；`{account_id,roles}` | 为 active、可登录且已有 active MFA 的账号创建 Operator |
| PATCH | `/platform/operators/{operator_id}` | 同上；`If-Match`、`{status,roles}` | revision CAS 更新；至少保留一个 active owner |

Operator role/status 变更在同一事务递增 revision，并撤销该 Operator 的 active PlatformSession、
SupportSession 和未激活 grant。所有后续请求都会重新验证 GlobalAccount、GlobalSession、MFA factor、
Operator、role binding、revision 和 role-set fingerprint。

## Workspace 目录与运营

PRD-096 在同一 PlatformSession 域提供跨 Workspace 的最小运营投影，不创建 Workspace Membership，也不
返回 tenant ID、成员身份、Owner 邮箱、Provider/Endpoint、Key/Token、Prompt/Response、文件或调用日志
正文。所有读取均为 `no-store, private`；不存在与无权读取的 Workspace 使用同形 404。

| 方法 | 路径 | 权限与请求 | 成功结果 |
| --- | --- | --- | --- |
| GET | `/platform/workspaces` | 目录角色；`limit=1..100`、可选 HMAC `cursor`、exact `search` 与固定筛选 | 角色裁剪的最小摘要、`has_more` 和下一游标 |
| GET | `/platform/workspaces/{workspace_id}` | 同上 | 摘要、operational/risk revision 与最近平台动作 |
| GET | `/platform/workspaces/{workspace_id}/health` | 同上 | `fresh/stale/not_ready/unavailable`、band/risk 和按角色裁剪的固定 tags |
| POST | `/platform/workspaces/{workspace_id}/suspend` | `platform.workspace.lifecycle.manage`；operational `If-Match`、`Idempotency-Key`、固定 reason、`confirmed:true` | active 到 suspended；撤销 WorkspaceSession 并禁用 legacy tenant |
| POST | `/platform/workspaces/{workspace_id}/restore` | 同上 | 仅恢复具有完整平台 suspend 证明且数据生命周期仍 active 的 Workspace |
| POST | `/platform/workspaces/{workspace_id}/risk-tags` | security/customer-success 对应 allowlist；risk `If-Match` 与幂等键 | 添加固定风险标签并递增 risk revision |
| DELETE | `/platform/workspaces/{workspace_id}/risk-tags/{tag_code}` | 同上；严格 JSON `{reason_code}` | 撤销固定风险标签并递增 risk revision |
| POST | `/platform/workspaces/{workspace_id}/owner-notifications` | ops/customer-success；operational `If-Match`、幂等键、固定 template/reason | 只返回 pending/sent/failed，不返回联系地址 |
| GET | `/platform/workspaces/{workspace_id}/events` | 目录角色；`limit=1..100` | 仅含平台 operational event 安全摘要 |

目录只支持 exact Workspace ID 或 organization normalized slug 搜索。筛选白名单为 `kind`、
`operational_status`、`region`、`risk_tag`、`health_status`、`health_risk`、`plan_status` 和
`deployment_status`。游标使用规范 JSON、URL-safe Base64 与 HMAC，绑定当前 PlatformSession、Operator
revision、role set 和规范筛选摘要；篡改、跨角色、跨 Session 或筛选漂移固定返回请求无效。搜索词不写入
游标、审计、日志、浏览器 URL 或 Storage。

所有 Workspace 写请求在读取 JSON 正文前完成 PlatformSession、Origin、CSRF 和 capability 校验。
`If-Match` 使用带引号的正整数 revision；`Idempotency-Key` 为 16..128 字符的固定安全字符集。同一键与同一
请求重放原完整结果；同一键配不同请求固定冲突。revision 竞争返回 `412`，客户端必须重读详情，不能在
本地推测下一状态。

冻结只声明控制面状态：本地 WorkspaceSession 和 tenant 资源立即失败关闭，Gateway 在后续快照接入前返回
`transition_pending`，不能被描述为已完成跨 Cell 传播。恢复不会复活旧 Session，也不能复活 deletion、
deleted 或 migration conflict。Owner notification outbox 只保存 active Membership/Account 引用与固定
模板 payload，发送 adapter 才受控解析当前验证地址。

## 受控支持访问

SupportGrant 固定绑定一个 requester、一个 active Workspace、`workspace.diagnostics.read` scope、固定
reason code 和不超过 30 分钟的期限。进入 active 状态必须同时存在以下 append-only 事实，顺序不限：

1. 目标 Workspace 当前 active owner/admin 的客户审批，绑定 Membership revision。
2. 与 requester 不同的 `platform_owner`、`platform_security` 或 `platform_compliance` 平台审批。

| 方法 | 路径 | 主体与请求 | 成功结果 |
| --- | --- | --- | --- |
| POST | `/platform/support-grants` | 有 request 权限的 PlatformSession；`{workspace_id,reason_code,duration_minutes}` | pending grant 和缺失审批域 |
| GET | `/platform/support-grants?limit=&offset=` | requester 看自己；approver 看全部 | 有界 grant 列表 |
| POST | `/platform/support-grants/{grant_id}/approve` | 非 requester 的平台批准角色；`{}` | 追加平台审批 |
| POST | `/workspace/support-grants/{grant_id}/approve` | 目标 Workspace owner/admin；`{}` | 追加客户审批 |
| POST | `/platform/support-grants/{grant_id}/revoke` | requester 或平台批准角色；`{reason_code}` | 撤销 grant 和派生 Session |
| POST | `/workspace/support-grants/{grant_id}/revoke` | 目标 Workspace owner/admin；`{reason_code}` | 撤销 grant 和派生 Session |
| POST | `/platform/support-grants/{grant_id}/sessions` | grant requester；`{}` | 签发不超过 15 分钟且不越过 grant 到期时间的 SupportSession |
| GET | `/support/session` | SupportSession | source、Workspace、scope 和到期摘要 |
| GET | `/support/workspace/diagnostics` | SupportSession | 固定运营状态、Region、provisioning、health band 和成员计数 |

诊断响应不包含成员列表、邮箱、tenant、Endpoint 全文、Key/Token、Prompt/Response、文件、策略、调用、
日志正文或对象下载。每次 Support 请求都会复核 grant、双审批、Operator/MFA/Workspace/Membership revision
和到期时间；后台收敛不是授权来源。

## Break-glass 与审计

`POST /platform/break-glass-sessions` 只允许 owner/security 在 fresh TOTP 下提交
`{workspace_id,reason_code,totp_code,confirmed:true}`。它签发同一 Support Cookie 域的固定诊断 Session，
TTL 不超过 10 分钟，并原子写 `break_glass.used`、高优先级 outbox 和不可变审计。到期后必须由不同的
平台 owner/security/compliance 与目标 Workspace owner/admin 分别调用对应的
`/platform/.../{id}/reviews` 和 `/workspace/.../{id}/reviews`；24 小时逾期事实不会被删除。

`GET /platform/audit-logs?limit=20&cursor=...` 返回安全摘要和 `next_cursor`。owner 可见全部平台安全摘要，
security/compliance 可见安全域，support 只见自己申请的 grant 审计。游标采用规范 JSON、URL-safe Base64
和 HMAC，绑定当前 PlatformSession、Operator revision、角色摘要、角色裁剪 scope、方向及
`occurred_at/audit_id` keyset；篡改、跨 Session、跨角色或过期 Session 使用固定失败。

平台审计与业务事实同事务写入，UPDATE/DELETE 由数据库 trigger 拒绝。公开摘要只返回 action、object、
固定 reason、时间和内部 opaque ID，不返回邮箱、客户名称、自由文本、凭据、MFA counter、权限 fingerprint
或 before/after 原始内容。

## 运行配置

| 配置 | 约束 |
| --- | --- |
| `METAHUB_PLATFORM_SESSION_SECRET` | 独立 HMAC 根；生产显式配置至少 32 字符 |
| `METAHUB_PLATFORM_SESSION_TTL_SECONDS` | 60..3600，默认 3600 |
| `METAHUB_SUPPORT_SESSION_SECRET` | 独立 HMAC 根；不能与 Platform 或其他根复用 |
| `METAHUB_SUPPORT_SESSION_TTL_SECONDS` | 60..900，默认 900 |
| `METAHUB_IDENTITY_MFA_ENCRYPTION_SECRET` | 独立 AES-256-GCM 根 |
| `METAHUB_PLATFORM_BOOTSTRAP_ENABLED` | 默认 `false` |
| `METAHUB_PLATFORM_BOOTSTRAP_RECOVERY_SECRET` | 启用 bootstrap 时显式配置的独立离线值 |

这些根域不能复用 Auth、GlobalSession、Challenge、PII、WorkspaceSession、Provider、Cache 或彼此的值；
配置对象的 dump/repr 不包含明文。
