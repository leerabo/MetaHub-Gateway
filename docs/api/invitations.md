# 用户邀请与加入 API

本文冻结 PRD-060 的租户用户邀请、匿名接受、一次性 Token、审计事件和 PAGE-087 合同。
M1 只提供复制邀请链接/邮件文案，不接入真实邮件服务；批量邀请、域名自动加入、SCIM/SSO 和跨租户
邀请不在本版本范围内。

## 1. 安全边界

- 创建接口需要 Bearer Session 和后端 `user.manage` 权限；前端按钮可见性不能替代后端守卫。
- 创建租户和操作者只从认证上下文恢复。客户端不得提交 `tenant_id`、`invited_by`、状态或事件字段。
- 接受接口不使用现有 Session；tenant、email 和 role 只能从 Token 哈希命中的 Invitation 恢复。
- 原始 Token 由 32 字节密码学随机数生成，数据库只保存 SHA-256；创建响应只返回一次。
- Token 放在 Console 同源 fragment 中并在认证 bootstrap 前移入闭包，地址栏立即改为
  `#accept-invitation`，不进入真实 path、query、Referer、Storage 或普通日志。
- 公开 accept 固定不携带 Authorization/Cookie，使用 `no-store` 和 `no-referrer`；User-Agent 不写入审计。
- 创建和接受入口均忽略客户端 `X-Request-ID`、`X-Trace-ID`，改用服务端生成的链路 ID，避免攻击者把
  邮箱、密码或 Token 经 Header 注入日志、AuditLog、source event 和响应头。
- 未知、畸形、过期、已使用、邮箱竞争、租户/角色停用或邀请人失权都返回同形 410，不能枚举邀请状态。
- Provider Key、Virtual Key、Session Token、邀请 Token、密码、Prompt、Response、邮件正文和 Webhook
  Secret 不得进入 AuditLog 摘要、source event、Webhook payload、普通日志或测试证据。
- 所有成功和错误响应都携带 `request_id`、`trace_id`，Header 与 Body 使用统一链路值。

## 2. POST /invitations

为当前认证租户创建一个 pending Invitation。

### 2.1 权限

需要当前数据库权限快照包含 `user.manage`。匿名返回 401；已登录但失权返回 403。权限依赖先于正文读取，
无权调用即使携带畸形 JSON 也不能借校验差异探测管理合同。

### 2.2 严格请求

```http
POST /invitations HTTP/1.1
Authorization: Bearer <session-token>
Content-Type: application/json; charset=utf-8

{
  "email": "developer@example.com",
  "role": "developer",
  "expires_in_hours": 72
}
```

- 正文最大 4 KiB，只接受唯一 `application/json`、严格 UTF-8 和单个 JSON object。
- 必填字段为 `email`、`role`；`expires_in_hours` 可省略并默认 72。
- 拒绝重复 key、额外字段、非规范 Content-Length、float、指数、非有限数、bool、数字字符串、过深或
  过宽 JSON。
- email 规范化为小写，必须为有效邮箱；role 必须是当前租户 active 固定角色。
- 有效期必须为真实整数 `1..168`。Console PAGE-087 只提供 24、72、168 三个选项，后端仍独立校验。
- 当前租户已有同邮箱用户返回 `invitation.user_exists`；仍有 pending 邀请返回
  `invitation.pending_exists`。另一租户同邮箱不影响当前租户。

### 2.3 成功响应

```json
{
  "invitation": {
    "invitation_id": "inv_<32-lowercase-hex>",
    "tenant_id": "tenant_example",
    "email": "developer@example.com",
    "role": "developer",
    "status": "pending",
    "expires_at": "2026-08-03T12:00:00Z",
    "created_at": "2026-07-31T12:00:00Z"
  },
  "accept_token": "<43-base64url-characters>",
  "request_id": "req_<server-generated-id>",
  "trace_id": "trc_<server-generated-id>"
}
```

HTTP 201。`accept_token` 是一次性 Bearer credential，只能在当前调用栈/Console 私有闭包短暂保存；没有
Invitation 列表或详情接口可再次读取它。

## 3. POST /invitations/{token}/accept

匿名接受 Invitation，并在 Invitation 所属租户创建 active 本地用户。

### 3.1 请求身份

- path 中的 43 字符 Token 是唯一凭据。接口不读取浏览器 Session，也不签发新 Session。
- 请求不得携带 tenant、email、role 或 actor；这些字段即使与真实 Invitation 一致也按 strict wire 拒绝。
- 成功后用户仍需在普通登录页使用邮箱和密码登录。

```http
POST /invitations/<token>/accept HTTP/1.1
Content-Type: application/json

{
  "display_name": "Developer Zhang",
  "password": "<new-local-password>"
}
```

- `display_name` 去除首尾空白后长度为 `1..160`，不能含控制字符。
- password 长度为 `8..256`，只在服务调用栈中参与 PBKDF2，不进入响应、日志、事件或普通数据库列。
- Console 的 `confirmPassword` 只做本地一致性校验，不发送到后端。

### 3.2 成功响应

```json
{
  "accepted": true,
  "user": {
    "user_id": "user_<32-lowercase-hex>",
    "email": "developer@example.com",
    "display_name": "Developer Zhang",
    "role": "developer",
    "status": "active"
  },
  "request_id": "req_<server-generated-id>",
  "trace_id": "trc_<server-generated-id>"
}
```

HTTP 201。接受事务使用 `BEGIN IMMEDIATE`；取得写锁后重新读取权威时间、Invitation、tenant、role、
邀请人实时 `user.manage` 和邮箱竞争状态。同一个 Token 的并发请求最多一个成功。

## 4. 事务、审计与事件

创建事务同时写入：

1. pending Invitation；
2. `user.invited` AuditLog；
3. 不可变 `invitation_events` source event。

接受事务同时写入：

1. active User，密码只保存 PBKDF2 哈希；
2. Invitation 从 pending 单向 CAS 为 accepted；
3. `invitation.accepted` AuditLog；
4. 不可变 `invitation_events` source event。

Invitation、User、actor、AuditLog 和 source event 均使用 tenant-qualified 查询、复合 FK 或 owner trigger。
强审计/source 任一步失败都整体回滚；事务提交后才尝试 Webhook。Webhook 网络失败不回滚业务事实，只把
source event 标记为 `failed/webhook.delivery_unavailable`。

事件 payload 只包含 schema version、Invitation/User ID、加盐邮箱指纹、角色和时间，不包含 email、
Token、密码或邮件文案。AuditLog 的 Invitation 摘要也只保存邮箱指纹、角色、状态、过期时间和接受用户 ID。
`invitation.accepted` 的 `actor_email` 固定保存 `invitation-acceptor@redacted.invalid`，不会复制受邀邮箱；
可信身份由同租户 `actor_user_id`、Invitation 的 accepted user 和不可变角色/时间快照共同复核。

## 5. 统一错误码

| HTTP | code | 场景 |
| ---: | --- | --- |
| 401 | `auth.token_missing` / `auth.token_invalid` | 创建接口缺少或使用无效 Session |
| 403 | `auth.permission_denied` | 当前数据库权限快照没有 `user.manage` |
| 409 | `invitation.user_exists` | 当前租户已有同邮箱用户 |
| 409 | `invitation.pending_exists` | 当前租户已有同邮箱 pending Invitation |
| 410 | `invitation.not_available` | Token/状态/归属/实时授权/邮箱竞争任一不可用的公开同形错误 |
| 422 | `invitation.create_invalid` | 创建 wire、email 或有效期非法 |
| 422 | `invitation.role_invalid` | role 不在当前租户 active 固定目录 |
| 422 | `invitation.accept_invalid` | 接受 wire、显示名或密码非法 |
| 503 | `invitation.unavailable` | 数据库、事务、审计或 source event 不可用 |

公开错误不得附带 Token、email、tenant、role、Invitation 状态、SQL、数据库路径、原始 JSON 或底层异常。

## 6. Console 合同

- 后端导航和用户页只有在 `user.manage` 存在时展示 PAGE-086/087；创建 API 仍执行最终权限校验。
- 邀请弹窗保留既有直接创建用户表单，字段为邮箱、角色和 24/72/168 小时有效期。
- 弹窗 owner 绑定 tenant、user、Session、role、permission revision、authorization fingerprint 和 users 导航。
- 弹窗打开、dirty、busy 或持有一次性链接时暂停 15 秒 DOM 刷新；busy 时关闭按钮和 ESC 都不能丢弃
  唯一一次创建响应。
- 原始链接和邮件文案只在闭包；snapshot、模板 HTML、data attribute、URL 和 Storage 均不含密文。
- Clipboard 失败只显示“手动复制”按钮；用户再次明确点击后才创建 readonly 字段，30 秒或关闭弹窗即清除。
- 公开页先于认证读取接管 `#accept-invitation/<token>`，立即 scrub 为 `#accept-invitation`；刷新显示固定失效页。
- 接受页字段为显示名、密码和本地确认密码。成功/410、离页、hashchange、pagehide 和前往登录都会清 Token。

## 7. 数据库迁移

`0051_user_invitation_join_flow.sql` 新增 Invitation、`invitation_events`、AuditLog 事件类型、source event
复合关系、状态机、不可变与 owner trigger，并完整保留 0050 的 AuditLog/AuditExport/RolePermission 事实、
rowid、NULL 分布和导出序列水位。

`0052_invitation_audit_privacy.sql` 在不改写已应用 0051 checksum 的前提下，把已有和后续接受审计邮箱
统一为固定脱敏标记。up 在持久变更前验证 8 张核心表、15 个核心 trigger、外键和全部历史接受图；
down 只允许空业务事实栈恢复 0051 owner 合同，存在邀请事实、后续迁移或未知 schema 依赖时原子拒绝。

down 在 Invitation、相关 AuditLog/source、目标 Webhook 订阅/投递/尝试/死信、后续迁移或未知 schema
依赖存在时，于第一项持久 DDL 前以 `prd060_down_blocked` 原子拒绝。

## 8. 本地开发验证

推荐 Python 和 `PYTHONPATH` 使用仓库 README 的固定值。pytest 必须设置
`TEMP/TMP=D:/MetaHub/.tmp`，使用独占 `--basetemp` 和 `-p no:cacheprovider`。PRD-060 发布门禁只运行：

- Invitation 集成、0051/0052 迁移、路径/Header 隐私和 Shared 合同专项；
- PRD-059 0050 迁移隔离回归；
- Console Invitation、Users、刷新 owner、完整 Console 回归和 lint；
- `scripts/ci/check-comments.py`；
- 开发库 0052 迁移、三服务真实 API、1280x720 与 390x844 浏览器验收。

不得为了本 PRD 重复执行根级 Python 全量，也不得使用 `C:/tmp`、提权、外部网络或把一次性 Token 写入
验收证据。
