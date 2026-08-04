# PRD-060 - 用户邀请与加入流程

状态：**设计、两轮编码前自审、实现、两轮实现期复审、专项测试和实际运行验收均已通过**。本状态只代表
PRD-060 已完整完成；
全部 108 条 PRD 仍在进行，`all_108_prds_complete=false`。

## 1. 需求理解

### 1.1 权威来源与开发顺序

- Excel `01_PRD总表` 与 `02_M0-M1核心开发` 第 61 行一致定义：
  `PRD-060 | M1-企业POC | P1 | 核心层 | 团队邀请 | 用户邀请与加入流程`。
- 用户故事是企业管理员邀请同事加入当前租户；Excel 的“管理员/开发者”受众表示管理员发起邀请、
  Developer 可以作为被邀请角色，不代表 Developer 获得邀请管理权限。
- 实现范围：生成邀请链接和可复制邮件文案、选择当前租户固定角色、设置过期时间、接受邀请并创建用户。
- MVP 不接入真实邮件服务，只提供复制邀请链接和复制邮件文案；邮件发送、送达、退信和模板运营不在本 PRD。
- 页面为 `PAGE-086 用户管理` 和其中的 `PAGE-087 邀请弹窗`；接口为
  `POST /invitations`、`POST /invitations/{token}/accept`；对象为 `Invitation`；事件为
  `user.invited`、`invitation.accepted`。
- `TC-0119` 验证创建邀请、复制链接、接受后创建用户并获得指定角色的完整主流程。
- `TC-0120` 验证匿名/越权创建、非法输入、过期/未知/已使用 Token、重复邮箱、跨租户、并发接受、
  依赖失败和敏感信息不泄漏。
- PRD-059 已完成并把开发库迁移到 0050。按里程碑优先级和 Excel 两张核心表的实际行顺序，
  PRD-060 是下一条未完成项，不是仅按编号推断。

### 1.2 架构约束

- 创建邀请必须使用 PRD-059 已发布的 `user.manage`，由后端逐请求从当前租户权限矩阵复核；
  前端隐藏入口只能改善体验，不能替代权限校验。
- 接受邀请时用户尚无 Session，因此接受接口是 Token 鉴权的公开入口；公开不等于无鉴权，
  一次性高熵 Token 是该操作唯一凭据。
- `tenant_id`、邀请邮箱和角色只能从已认证管理员上下文或已校验 Invitation 恢复，创建/接受请求体
  均不得携带 `tenant_id`，接受请求也不得携带 email 或 role。
- API 成功与失败继续返回稳定错误码、`request_id` 和 `trace_id`；未知、过期、已接受及非法格式 Token
  对外同形，避免形成 Token 状态枚举侧信道。
- 原始邀请 Token、密码、Authorization、Cookie、Provider Key、Virtual Key、Prompt、Response、
  Webhook 正文不得进入数据库明文字段、审计摘要、source event、普通日志、DOM 属性、Storage 或测试报告。
- 邀请邮箱属于租户业务数据，必须保存于 tenant-qualified Invitation 中供接受时建号，但不得进入日志和
  Webhook；事件和审计只保存由随机 invitation_id 加盐的不可逆邮箱指纹。
- OpenRouter、硅基流动继续只是竞品与迁移兼容对象，不改变默认上游范围；本 PRD 不触及数据面、
  Provider、硬件盒子、MCP、信创或复杂数据出境。

### 1.3 现有能力与缺口

已有能力：

1. `users` 模块支持管理员列出当前租户用户、直接创建本地用户和修改固定角色，密码只以 PBKDF2 哈希落库。
2. PRD-059 已提供六角色、`user.manage`、逐请求数据库权限快照及 tenant-qualified role 约束。
3. AuditLog 支持同事务写入、不可变导出序列和 `audit.recorded`；Webhook 具备严格事件白名单、
   payload 安全校验和提交后投递。
4. Console 已有 PAGE-086 用户管理、角色目录、直接创建表单、15 秒刷新和 permission owner。
5. Control API 正式启动入口关闭 Uvicorn access log，避免 mandated path 中的邀请 Token 被默认访问日志记录。

真实缺口：

1. 没有 Invitation、Token 哈希、过期和一次性状态机，不能在用户创建前安全携带租户与角色意图。
2. 没有两个 Excel 冻结事件的严格 payload/envelope 合同和持久 source event。
3. `audit_logs` 的 action/object_type 有数据库 CHECK；若不保真扩展，邀请动作无法进入统一审计页和导出链。
4. Console 无 PAGE-087，也没有优先于 Session 恢复的公开接受入口；真实子路径在当前 dev server 会 404。
5. 当前全页自动刷新会重建用户面板，可能丢失邀请草稿或让一次性链接残留；已有管理员 Session 也可能
   被错误附加到公开 accept 请求。
6. 共享 FastAPI 中间件当前把 `request.url.path` 原样写入完成、业务错误、诊断和未处理异常日志；
   `POST /invitations/{token}/accept` 若直接接入会把一次性 Token 明文落日志。现有 request/trace 解析也只与
   Authorization 比对，攻击者可把邀请 Token 放进 `X-Request-ID/X-Trace-ID` 造成日志和响应头二次回显。

### 1.4 实现范围

- 创建 256-bit 高熵一次性 Token，数据库只保存 SHA-256；原始 Token 仅在创建响应和 Console 私有内存
  短暂存在一次。
- 支持管理员选择六个当前租户 active 固定角色和 `1..168` 小时有效期，默认 72 小时。
- 公开接受接口只接收 `display_name/password`，以 `BEGIN IMMEDIATE`、状态 CAS 和 tenant-qualified
  关系在一个事务中创建 active 用户、消费 Invitation、追加 AuditLog 和不可变 source event。
- 创建邀请同样把 Invitation、AuditLog 和 `user.invited` source event 放在一个事务；任何强审计或
  事件构造/落库失败都回滚邀请，不返回无法追踪的成功。
- 新增 `user.invited`、`invitation.accepted` 严格 Webhook 合同；网络 I/O 只在提交后执行，失败不反转
  已提交业务结果，但 source event 会记录 `dispatched/failed` 和固定错误码。
- PAGE-087 作为 PAGE-086 内原生 dialog；保留 PRD-008 已有直接创建用户表单和角色变更能力。
- 邀请链接使用同源 fragment `/#accept-invitation/<token>`；启动时先移入闭包私有内存，再立即用
  `history.replaceState` 清除地址栏 Token。accept 请求永不附加 Session Bearer。
- 补齐错误码、中文注释、迁移 up/down guard、专项测试、直接受影响回归、API 文档、开发文档和
  桌面/移动实际 Browser 验收。
- 共享访问日志统一使用 Starlette 匹配后的路由模板；未知路由只记录固定占位符，不记录原始 path。
  request/trace 解析把邀请 path Token 作为敏感凭据参与完整值和连续片段碰撞检测，碰撞时生成内部 ID。

### 1.5 明确不做

- 不接入 SMTP、企业邮箱、短信、企微/钉钉、邮件队列、退信、打开率或营销模板。
- 不做邀请撤销、批量邀请、CSV 导入、邀请列表、自动重发、域名白名单、审批流或组织/部门关系。
- 不做自定义角色或字段级权限；只能邀请到 PRD-059 六个 fixed role 中当前租户的 active role。
- 不自动登录新用户，不在接受后签发 Session；接受成功后显式前往现有登录页。
- 不通过 query、真实 Console 子路径或 `mailto:` 传递 Token，避免浏览器历史、Referer、Web server 或
  外部邮件客户端扩大凭据暴露面。

## 2. 涉及模块

| 模块 | 目录 | 修改范围 |
| --- | --- | --- |
| Invitation 领域 | `control-plane-api/src/metahub_control/invitations/` | strict models、公开/受保护 routes、事务 service、事件投递 |
| 用户与认证 | `users/`、`auth/` | 复用密码哈希、六角色目录和 `user.manage`；不扩大 Developer 权限 |
| 审计 | `control-plane-api/src/metahub_control/audit/service.py` | Invitation action/object 白名单；支持由刚创建用户形成可信审计 actor |
| Webhook runtime | `shared-sdk/python/metahub_shared/runtime/webhooks.py` | 两个事件、精确 payload 和 envelope validator |
| 错误注册表 | `shared-sdk/python/metahub_shared/runtime/errors.py` | Invitation 统一错误码、HTTP 状态、重试语义 |
| 应用装配 | `control-plane-api/src/metahub_control/app.py` | 注册 invitations router |
| 数据库 | `infra/migrations/0051_*.sql/.down.sql`、`0052_*.sql/.down.sql` | Invitation、source event、AuditLog 扩展、接受审计脱敏、owner/状态/保真/down guard |
| Console API | `console-web/src/lib/invitations.js` | create 带 Bearer；accept 明确不带 Bearer；统一净化错误 |
| Console 状态 | `console-web/src/features/invitations/` | owner-bound dialog、一次性密文、公开接受状态和 fragment parser |
| PAGE-086/087 | `features/users/`、`app/main.js`、`styles/main.css` | 邀请按钮/dialog、接受页、刷新/离页/降权清理、双视口布局 |
| 测试与验收 | Python、Node、`scripts/acceptance/`、`docs/testing/` | TC-0119/0120、迁移、权限、租户、并发、隐私、Browser |

## 3. 数据表与字段设计

### 3.1 invitations

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `invitation_id` | PK，`inv_` + 32 hex | 稳定业务身份 |
| `tenant_id` | NOT NULL，复合候选键/FK | 所属租户；所有管理查询必须限定 |
| `email` | NOT NULL，normalized lower-case | 接受后用户邮箱；不进日志/事件 |
| `email_fingerprint` | 64 lower hex | `sha256(invitation_id + NUL + normalized_email)` |
| `role_code` | NOT NULL，tenant role owner trigger | 接受后固定角色 |
| `token_hash` | UNIQUE，64 lower hex | 原始 Token 的 SHA-256；数据库绝不保存 Token |
| `status` | `pending/accepted/expired` | 单向生命周期 |
| `invited_by` | tenant-qualified user FK | 创建管理员 |
| `accepted_user_id` | nullable tenant-qualified user FK | 接受后新用户；pending/expired 必须为空 |
| `expires_at` | canonical UTC second | 严格晚于 created_at，最长 168 小时由服务层控制 |
| `accepted_at` | nullable canonical UTC second | accepted 时必填并不晚于 created_at |
| `created_at/updated_at` | canonical UTC second | 状态及排序证据 |

约束和索引：

- `UNIQUE(tenant_id, invitation_id)` 供 source event 复合 FK；`UNIQUE(token_hash)` 供公开入口等值查找。
- partial unique `(tenant_id,email) WHERE status='pending'`，同租户同邮箱最多一个有效 pending 意图；
  新建前先在锁内把时间已过的 pending 规范化为 expired。
- owner trigger 要求 tenant active、invited_by 属于同租户、role_code 命中同租户 active role；accepted
  记录还要求 accepted user 的 tenant/email/role 与 Invitation 完全一致。
- UPDATE 只能从 pending 进入 accepted 或 expired；email、role、token_hash、tenant、inviter、expires_at、
  created_at 和 rowid 不可变。accepted/expired 不可再次更新或删除。
- 不提供运行期 DELETE；Invitation 是租户治理事实，后续保留周期由独立 PRD 设计。

### 3.2 invitation_events

| 字段组 | 字段 |
| --- | --- |
| 身份 | `invitation_event_id, tenant_id, invitation_id, event_type` |
| actor | `actor_user_id, actor_role` |
| 审计绑定 | `audit_log_id, request_id, trace_id` |
| 安全信封 | `payload_json` |
| 投递状态 | `delivery_status, last_error_code, dispatched_at` |
| 时间 | `created_at, updated_at` |

- event_type 只允许 `user.invited`、`invitation.accepted`；每个 Invitation 每种事件最多一条。
- Invitation、actor 和 AuditLog 均使用 tenant-qualified 复合关系或 owner trigger；payload 精确复演
  Invitation 状态、角色、邮箱指纹和时间，不能携带 email、Token、密码或请求正文。
- 业务字段不可变；只允许 delivery 从 pending 进入 dispatched/failed，且状态更新不能改写业务事实。
- `user.invited` payload：`schema_version, invitation_id, email_fingerprint, role_code, expires_at, invited_at`。
- `invitation.accepted` payload：`schema_version, invitation_id, user_id, email_fingerprint, role_code, accepted_at`。

### 3.3 AuditLog 扩展与保真

0051 把 AuditLog action/object CHECK 扩展为：

- `user.invited -> invitation`
- `invitation.accepted -> invitation`

Invitation 摘要白名单为 `email_fingerprint, role_code, status, expires_at, accepted_user_id`。创建审计从空对象
变为 pending 摘要；接受审计从 pending 摘要变为 accepted 摘要，changed_fields 由统一审计服务重算。
object_name 固定为空，避免邀请邮箱进入审计导出。

SQLite 不能原地扩展 CHECK，0051 必须按 0050 的保真模式换表：显式复制 `rowid` 和全部列，比较行数、
NULL 分布和双向全列差集，并保存/恢复 `audit_log_export_sequences` 自增水位。迁移还必须完整恢复：

- PRD-058 AuditExport owner、冲突优先级、稳定序列和不可变 trigger；
- PRD-059 role_permission_events 到 AuditLog 的复合 FK 和 owner trigger；
- 0050 所有 AuditLog 索引，包括 role permission 唯一索引；
- AuditLog owner/immutable/conflict/negative-rowid trigger。

0051 down 只有在 Invitation、Invitation AuditLog、source event、对应 Webhook 订阅/投递/死信和后续迁移
全部为零时才允许执行；否则原子返回 `prd060_down_blocked`，不得静默删除业务或审计证据。

## 4. Token 与状态机

```text
create:  none -> pending
accept:  pending && now < expires_at -> accepted + user
expire:  pending && now >= expires_at -> expired
terminal: accepted / expired -> no transition
```

1. 创建使用 `secrets.token_urlsafe(32)`，产生 256-bit 随机材料；只把 `hash_token(raw_token)` 写入数据库。
2. 原始 Token 只在 201 创建响应的 `accept_token` 中返回一次；list、audit、event、logger 均无该字段。
3. 公开 accept 先执行固定格式和长度门禁，再哈希等值查询；任何无结果/终态/过期均返回同一 410。
4. 接受事务使用 `BEGIN IMMEDIATE` 串行化同一 SQLite 写库；锁内重读 pending 状态并执行单向 CAS，
   两个并发请求最多一个创建用户，另一个得到同形不可用错误。
5. 明文密码只在 accept 服务函数内参与 PBKDF2，函数退出后不进入响应、日志、事件或普通数据库列。
6. 失败不能回退为默认租户或默认角色；role/tenant/Invitation 关系任一损坏均失败关闭为统一 503/409。

## 5. API 设计

### 5.1 `POST /invitations`

权限：Bearer Session + 后端 `user.manage`。`admin` 默认具备；其他角色即使伪造前端按钮也返回 403。

请求：

```json
{
  "email": "developer@example.com",
  "role": "developer",
  "expires_in_hours": 72
}
```

strict wire：只允许以上三字段；email 规范化为小写；时长只接受真实整数 `1..168`，bool/字符串不隐式转换；
role 必须命中当前认证租户 active 固定角色。

成功：HTTP 201。

```json
{
  "invitation": {
    "invitation_id": "inv_...",
    "tenant_id": "tenant_...",
    "email": "developer@example.com",
    "role": "developer",
    "status": "pending",
    "expires_at": "2026-08-03T12:00:00Z",
    "created_at": "2026-07-31T12:00:00Z"
  },
  "accept_token": "one-time-base64url-value",
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

前端收到后必须验证 tenant/email/role 与冻结 owner 和提交意图一致，再构造 fragment URL；任何漂移整包拒绝。

### 5.2 `POST /invitations/{token}/accept`

权限：不要求 Session，完全由一次性 Token 鉴权。即使浏览器已有管理员 Session，也明确不发送 Authorization。

请求：

```json
{
  "display_name": "张三",
  "password": "LocalPassword!2026"
}
```

请求不得包含 tenant_id、email、role、status 或 token 副本。服务从 Invitation 恢复租户、邮箱和角色。

成功：HTTP 201。

```json
{
  "accepted": true,
  "user": {
    "user_id": "...",
    "email": "developer@example.com",
    "display_name": "张三",
    "role": "developer",
    "status": "active"
  },
  "request_id": "req_...",
  "trace_id": "trc_..."
}
```

响应不签发 Session、不回显 Token/密码哈希；用户随后使用邮箱和刚设置的密码走现有登录流程。

### 5.3 request/trace 与路径日志

- 两个接口复用统一 middleware 回显 `X-Request-ID/X-Trace-ID` 和结构化错误体。
- Console request id 使用不含邮箱/Token/时间原文的随机或单调内部 ID。
- 正式 Control API 继续 `access_log=False`；共享应用 logger 的 `path` 只能记录匹配后的
  `/invitations/{token}/accept` 路由模板，未匹配路由记录固定 `<unmatched>`，禁止读取原始 URL path。
- middleware 在解析链路 Header 前从固定邀请路由形状中只在内存提取 Token，并把它与 Authorization 一起作为
  敏感凭据；`X-Request-ID/X-Trace-ID` 包含 Token 完整值、规范化值或至少 16 个连续字符时必须换成内部 ID。
- 业务 logger 只记录固定 event、tenant、actor、Invitation ID、role、状态、邮箱指纹和已清洗 request/trace，
  不记录原始 URL/path、email、Token、密码、User-Agent、请求正文或异常原文。

## 6. 前端页面和交互设计

### 6.1 PAGE-086 用户管理

- 继续使用现有 `#users` 导航和 `user.manage` owner，不新增侧栏项。
- 标题操作区增加“邀请成员”命令，保留用户数量、直接创建用户表单和角色修改表单。
- 只有 active users 页面且当前 permission snapshot 含 `user.manage` 时才加载 `/users` 和绑定邀请操作；
  不再在所有导航页每 15 秒预取全租户用户 PII。

### 6.2 PAGE-087 邀请弹窗

- 使用单层原生 `<dialog>`，字段为邮箱、角色和有效期 select（24/72/168 小时，默认 72）。
- 角色 option 来自本次 `/users` 当前租户响应；DOM 可篡改，因此后端仍做最终 tenant role 校验。
- 成功后显示过期时间，并提供“复制邀请链接”和“复制邮件文案”；不使用 `mailto:`。
- 原始 Token/link/mail text 只保存在闭包私有状态，不出现在 snapshot、HTML `data-*`、Storage 或 telemetry。
- Clipboard 拒绝时，只有用户显式选择手动复制才短暂 reveal readonly 文本；关闭 dialog 立即清空。
- dialog owner 绑定 tenant/user/session/role/permission revision/authorization fingerprint 和 active navigation；
  owner 改变、401/403、离页、logout、pagehide 或迟到响应均 abort 并清空。
- dialog 打开、busy 或持有一次性结果时暂停全页自动刷新，避免草稿和密文被 DOM 重建；关闭后恢复。

### 6.3 公开接受页

- 链接格式固定为 `/#accept-invitation/<base64url-token>`，避免 dev server 子路径 404 和 query/Referer 泄漏。
- 应用 bootstrap 在读取 Session、调用 `/auth/me` 和普通 hash navigation 之前识别该 route；Token 通过有界格式校验后
  移入闭包并立即把地址替换为 `/#accept-invitation`。
- 页面使用独立接受邀请表单，字段为显示名、密码和仅本地确认密码；不展示/允许修改 email、tenant 或 role。
- accept client 不读取 authSession，也不设置 Authorization。成功后先销毁 Token 和密码，再显示安全用户摘要及
  “前往登录”；不自动登录。
- reload、hashchange 离开、pagehide、失败终态和成功都会清理；没有内存 Token 的 scrubbed route 显示固定失效状态，
  不尝试从 Storage 或 history 恢复。

### 6.4 响应式与无障碍

- 桌面 dialog 宽度约 480px 且受 viewport max 限制；390px 下字段和按钮纵向堆叠，不产生横向溢出。
- 原生 dialog 提供标题关联、cancel/ESC、初始焦点、焦点陷阱和关闭后的触发器焦点恢复。
- busy 时禁用提交/关闭竞态；反馈使用 `role=status`，错误显示固定中文文案和 request_id。
- 页面不显示功能说明、键盘快捷键或内部安全实现文字，只呈现当前操作所需字段和结果。

## 7. 错误码设计

| 错误码 | HTTP | 场景 | retryable |
| --- | ---: | --- | --- |
| `auth.token_missing` / `auth.permission_denied` | 401/403 | 创建邀请无 Session 或无 `user.manage` | 否 |
| `invitation.create_invalid` | 422 | email、expires_in_hours 或 strict wire 非法 | 否 |
| `invitation.role_invalid` | 422 | 创建目标角色不属于当前租户 active 目录 | 否 |
| `invitation.user_exists` | 409 | 当前租户已有同邮箱用户 | 否 |
| `invitation.pending_exists` | 409 | 当前租户同邮箱仍有 pending 邀请 | 否 |
| `invitation.not_available` | 410 | Token 非法、未知、过期、已接受、邀请人失权、租户/角色停用、邮箱竞争占用或状态损坏的公开同形响应 | 否 |
| `invitation.accept_invalid` | 422 | display_name/password/extra field 非法 | 否 |
| `invitation.unavailable` | 503 | 数据库、审计、source event 或事务提交不可用 | 是 |

错误响应由统一 runtime 生成，必须包含 `code/message/suggestion/request_id/trace_id`；公开 Token 错误文案不得
区分不存在、过期、已接受或格式非法，也不得回显 path、Token、email、SQL、表名或底层异常。

## 8. 安全与权限约束

1. 创建接口使用 `require_current_admin` 当前实际语义 `user.manage`，并在 service 复核 context user/tenant 一致；
   不新增权限点，不修改 48 权限 catalog，也不把 Developer 加入邀请者。
2. Invitation、source event、AuditLog、actor、role 和 accepted user 所有关联必须 tenant-qualified；公开接口没有
   tenant 参数，因此不能选择或探测另一租户。
3. Token 高熵、一次性、定时过期、数据库只存哈希；公开状态统一 410；路径 access log 禁用。
4. 密码只短暂进入 PBKDF2；响应、logger、audit、event、DOM、Storage、Clipboard 错误和证据执行 canary 检查。
5. create/accept 的业务行、AuditLog 和 source event 同事务；网络投递和 Clipboard 是提交后外部副作用。
6. 接受事务须复核邀请人仍为同租户 active 用户且仍有 `user.manage`；邀请人失权、租户/角色停用和邮箱竞争
   均折叠为同一 410，不能用差异码证明 Token 有效，也不能静默换角色。
7. 所有内部 ValueError/sqlite 错误都转换为稳定业务码；日志不能插入 `str(exc)` 或请求 payload。
8. raw Token 在 Console 中有单一 owner，不能进入可序列化 snapshot；迟到 Promise 必须先比对 ticket/owner 再写状态。
9. 公开接受页优先于认证 bootstrap，且 accept 明确无 Authorization，防止已登录管理员身份污染新用户审计。
10. 路由 Token 同时视为 path credential 和链路 Header、User-Agent 敏感值；成功、业务失败、422、404、500 的所有日志分支
   都必须经过同一模板路径函数，避免只修 `request.completed` 却遗漏 `_error_response` 或异常处理器。

## 9. 测试用例

### 9.1 数据库迁移

- fresh 0001..0051、repeat no-op、0050 历史 up、0051 down/re-up。
- AuditLog、AuditExport、AuditExportEvent、stable sequence、role_permission_events 的 rowid/全列/NULL/自增水位保真。
- Invitation 两向 tenant owner、role owner、inviter/accepted user owner、token hash、pending unique、状态机、immutable、
  negative rowid 和 source event payload/状态 trigger。
- 0051 down 对 Invitation、AuditLog、source event、Webhook subscription/delivery/attempt/dead-letter、后续迁移和未知
  schema dependency 分别原子拒绝。
- AuditExport duplicate/REPLACE 仍先返回 immutable，证明 trigger 创建优先级未漂移。

### 9.2 后端 TC-0119 主流程

- admin 创建 Developer 邀请，201 响应仅一次返回 Token，request/trace 保真。
- 数据库 token_hash 不等于 raw Token；Invitation、AuditLog、`user.invited` source event 同事务存在。
- 无 Session 使用 Token 接受，201 创建 active 用户；email/tenant/role 来自 Invitation，密码可走现有 login。
- Invitation 进入 accepted；`invitation.accepted` AuditLog/source event 存在；指定 Developer 权限快照正确。
- 两个事件严格签名/投递，提交后网络失败只把 source event 标记 failed，不回滚用户或邀请。

### 9.3 后端 TC-0120 异常/权限/安全

- 匿名、Developer、Viewer、Finance/Security/Ops 无 `user.manage` 创建均被后端拒绝且不落业务行。
- strict wire、非法/disabled role、非法 email、bool/字符串/越界过期、已有用户、重复 pending 邀请。
- malformed/unknown/expired/already-used Token 完全同形 410；不泄漏 tenant、email、role 或状态。
- 密码过短、空 display name、accept 夹带 tenant/email/role；角色/租户停用、邀请人失权和邮箱被占用均与随机 Token 同形 410。
- 两个并发 accept 最多一个 201/一个 410，用户、审计和每类 event 均恰好一条。
- 人为破坏 AuditLog/source event/commit 时整个 create 或 accept 回滚；不会出现无审计用户或半消费邀请。
- 跨租户 FK/trigger 两个方向拒绝；另一租户用户、Invitation 和角色不变。
- 日志捕获、响应和数据库安全列扫描中不出现 Token、密码、Authorization、Cookie、Prompt/Response canary。
- 对成功、未知、过期、重放和未处理异常分别捕获日志；再把完整 Token、规范化 Token、前/中/后 16 字符片段
  放入 request/trace Header，断言日志、响应 Header/Body 均只出现内部链路 ID 和路由模板。

### 9.4 Shared runtime

- 两个事件进入可订阅白名单；合法精确 payload/envelope 通过。
- 缺字段、多字段、错误 ID/时间/actor/object/source_request、email/password/token/body 等敏感键全部拒绝。
- error registry HTTP、owner 和 retryable 合同稳定。

### 9.5 Console

- create client 带 Bearer；accept client 无论当前 authSession 如何都没有 Authorization。
- body 不含 tenant/actor；accept body不含 email/role；错误净化并保留 request/trace。
- fragment 在 auth bootstrap 前消费并 scrub；malformed/空 route 不发网络请求；普通导航 parser 合同不变。
- dialog trigger、六角色、期限、保留直接创建表单、权限隐藏、XSS 转义和 390px 布局。
- owner/导航/revision/fingerprint/401/403/logout/pagehide 清理与 Abort；迟到响应不回写。
- snapshot/HTML/Storage 默认没有 raw Token；Clipboard 拒绝只在显式 manual-copy 时短暂显示，关闭即销毁。
- public accept active 或 dialog dirty/busy/hasSecret 时 timer 不刷新；离开后恢复。
- Console lint 与中文注释门禁通过。

### 9.6 实际运行验收

- 开发库真实迁移至 0052，`integrity_check=ok`、外键违规 0、schema stack 连续。
- Gateway/Control/Console 三服务健康 HTTP 200；真实 admin 登录、邀请、无 Session 接受、目标用户登录。
- Microsoft Edge 1280x720 和 390x844 验收 PAGE-086/087 与公开接受页；无溢出、重叠、空白 canvas 或按钮文字溢出。
- 网络记录证明 accept 无 Authorization；URL、DOM、Storage、console、page error、日志和证据不含 Token/密码 canary。
- 过期、已使用、无权创建实际返回统一码和 request_id。
- 验收后停止本任务启动的服务，checkpoint SQLite，确认 WAL/SHM、pending source event 和监听端口静止终态。

## 10. 代码修改计划

1. 新增 0051 up/down 建立邀请图；新增 0052 up/down 对接受审计执行固定标记脱敏，同时保持已应用 0051 checksum 不变。
2. 扩展 Shared error/Webhook 合同及其定点测试。
3. 新增 invitations models/routes/service，并小范围扩展 audit actor helper；注册 router。
4. 完成后端主流程、异常、权限、租户、并发、事务、隐私测试。
5. 新增 Console invitation client、闭包状态和 renderer；接入 PAGE-086/087、公开 bootstrap route 和生命周期清理。
6. 完成 Console unit/lint、中文注释门禁和直接受影响回归。
7. 更新 README、目录说明、API/开发文档和 `docs/testing/PRD-060-tests.md`。
8. 迁移真实开发库，启动三服务，执行 API 与 Edge 双视口验收，最后停服并复核静止终态。

测试策略严格限定为 PRD-060 专项和直接受影响回归，不重复运行根级 Python 全量。pytest 固定
`TEMP/TMP=D:/MetaHub/.tmp`、独占 `--basetemp`、`-p no:cacheprovider`；不提权、不使用 `C:/tmp`。

## 11. 第一轮编码前自审：需求、权限、租户和 Token

自审结论：**修正后通过**。

| 检查项 | 初稿风险 | 修正 |
| --- | --- | --- |
| Excel 受众 | 把“管理员/开发者”误解为 Developer 可发邀请 | 以故事和 PRD-059 矩阵为准，创建只用 `user.manage`；Developer 仅是目标角色 |
| 接受鉴权 | 要求 Session 会形成“先有用户才能加入”的循环 | accept 改为一次性 Token 鉴权公开入口，明确不带现有 Bearer |
| 租户隔离 | accept body 若携带 tenant/email/role 可被篡改 | body 只保留 display_name/password，三项均从 Invitation 恢复 |
| Token 存储 | 明文 Token 落库或可重复查询会扩大泄漏 | 256-bit、只存 SHA-256、创建响应只返一次、终态不可复用 |
| 状态枚举 | unknown/expired/used 使用不同错误会泄漏 Token 状态 | 全部统一 `invitation.not_available` 410 和相同文案 |
| 重复/并发 | 先查后写会创建两个用户 | partial unique + `BEGIN IMMEDIATE` + 锁内重读/CAS；并发最多一个成功 |
| 角色/邀请人漂移 | 角色被禁用或邀请人失权后继续加入会越过当前授权意图；差异 409 又会证明 Token 有效 | 锁内复核租户、角色、邀请人及 `user.manage`；失败关闭为统一 not_available 410，不做默认角色回退 |
| 邮箱 PII | Webhook/AuditLog 复制 email 会扩大留存 | 表内 tenant-qualified 保存；审计/事件仅保存 invitation-salted 指纹 |
| MVP 边界 | 真实邮件服务拖慢 M1 | 只复制链接/邮件文案，不使用 mailto 或外部网络 |

第一轮复审后，需求范围、后端权限、公开鉴权、租户来源、一次性语义、过期和隐私边界均已冻结，
没有阻断编码的问题。

## 12. 第二轮编码前自审：兼容、审计、前端生命周期和测试

自审结论：**修正后通过**。

| 检查项 | 初稿风险 | 修正 |
| --- | --- | --- |
| AuditLog CHECK | 只改 Python 白名单会被数据库拒绝 | 0051 保真换表扩展两 action/object，并加入 owner/summary 复演 |
| 0049/0050 兼容 | 换 audit_logs 可能破坏导出 rowid、水位、RolePermission FK/trigger | 快照全图、显式 rowid、双向差集、恢复所有入向 trigger 和 conflict 优先级 |
| 强审计顺序 | 先 commit 再审计会产生不可追踪邀请/用户 | Invitation/User、AuditLog、source event 同事务；网络仅提交后 |
| accept actor | 无 Session 可能伪造系统 actor | 用户写入后以同事务新用户 tenant/email/role 构造显式可信 AuditActor，owner trigger 再复核 |
| Webhook payload | 通用敏感键扫描不能保证字段最小化 | 两事件增加精确 payload 和 envelope validator，event row 再由 trigger 复演 |
| Console 路由 | 真实 path 404，query 会进日志/Referer | 使用 fragment；优先 bootstrap 解析并立即 replaceState scrub |
| Session 污染 | 已登录管理员打开链接时 accept 可能附带 Bearer | accept client 独立且不接受 token 参数，测试精确断言无 Authorization |
| path 日志泄漏 | 共享中间件和异常处理器直接记录 `request.url.path` 会暴露邀请 Token | 所有日志改用匹配路由模板；未知路由固定占位；成功、业务错误、422/404/500 分支逐一捕获验证 |
| Header 二次泄漏 | 攻击者把 path Token 或连续片段放进 request/trace/User-Agent 后可被回显、记录或持久化 | path Token 在解析前加入敏感值碰撞检测；命中时链路 Header 改为内部 ID，审计 User-Agent 置空，响应、日志、审计和 source event 不含密文 |
| 自动刷新 | 15 秒 refresh 重建 dialog、丢草稿或残留 Token | active/busy/dirty/hasSecret/public-route owner 门禁暂停刷新；清理后恢复 |
| 前端密文 | 把 URL 放 data-*、snapshot 或 Storage 便于复制 | raw link 只在闭包；Clipboard handler 直接读私有状态；manual reveal 显式且关闭清空 |
| 既有功能 | 用 Invitation 替换直接创建会回归 PRD-008 | PAGE-087 只新增，直接创建和角色表单保留并回归测试 |
| 测试规模 | 重复根级全量导致耗时和沙箱临时目录问题 | 只跑 0051、Invitation、Audit/Webhook/Console 直接受影响回归和实际验收 |
| down 安全 | 回滚可能删除邀请或投递事实 | Invitation/Audit/Event/订阅/投递/死信/后续依赖任一存在即原子拒绝 |

第二轮复审后，0050 审计与权限合同、前端密文生命周期、公开请求身份、向下兼容和可执行测试范围均已
关闭。可以进入迁移与实现阶段；任何实现若偏离本设计，必须先回到本文补充复审，不能边编码边放宽安全边界。

## 13. 第三轮实现期自审：邀请人漂移、公开同形与审计兼容

自审结论：**修正后通过**。本轮由后端和迁移交叉审查触发，不替代前两轮编码前自审。

| 检查项 | 实现期发现 | 修正 |
| --- | --- | --- |
| 锁等待与过期 | 在 `BEGIN IMMEDIATE` 前取时钟，排队跨过期限时可能仍按旧时间接受 | 创建和接受都在成功取得写锁后生成唯一 UTC 秒时间，再做过期判断和全部事实写入 |
| 邀请人权限漂移 | 只复核目标租户/角色，邀请人停用、降权或撤销 `user.manage` 后旧邀请仍可建号 | 服务查询与数据库 accepted 状态 trigger 同时复核邀请人 active role 和实时 `user.manage` |
| 公开状态侧信道 | 角色停用、邀请人失权、邮箱竞争若返回独立 409，可证明 Token 曾有效 | accept 除正文 422、基础设施 503 外，所有 Token、状态、归属和竞争失败统一 410 |
| User-Agent 密文 | 攻击者可把 Token 放入 User-Agent，接受成功后进入不可变 AuditLog | `build_audit_request_meta` 增加窄开关，公开 accept 固定不采集 User-Agent；其他管理接口保持既有审计 |
| 邮箱审计表述 | “审计仅保存指纹”与 PRD-034 的 actor 邮箱字段合同存在冲突 | Invitation 摘要、object_name、source event、Webhook 和普通日志仅存指纹；接受审计的 `actor_email` 固定为 `invitation-acceptor@redacted.invalid`，身份由 tenant-qualified `actor_user_id` 和 Invitation 快照复核，不复制受邀邮箱 |
| source event 唯一性 | 只按 Invitation/event 去重，未显式冻结单 actor/request 单事件 | 增加 `(tenant_id,actor_user_id,request_id)` 唯一键，并让 conflict trigger 与索引谓词一致 |
| 迁移时间格式 | 仅 `unixepoch(...) IS NOT NULL` 会接受偏移量或小数秒 | 所有 PRD-060 时间列同时要求长度、`T...Z` 外形和 `strftime` 回放等值 |

第三轮复审没有扩大功能范围；它收紧了实时授权、公开错误同形、敏感 Header 和 0050 审计兼容性。
上述修正已由专项测试逐项验证；第三轮发现均已闭环后才进入最终迁移与运行验收。

## 14. 第四轮实现期终审：隐私迁移、事务故障与已应用迁移不可变

自审结论：**修正后通过**。本轮在最终开发库迁移前执行，重点核对前述设计在真实历史栈上的可升级性，
不扩大 PRD-060 功能范围。

| 检查项 | 终审发现 | 修正与证据 |
| --- | --- | --- |
| 接受审计邮箱 | 初版把受邀邮箱复制到不可变 AuditLog，扩大个人信息留存 | 运行时改用固定脱敏标记；0052 只改写历史 `invitation.accepted` 审计邮箱，并以 rowid/全列双向差集证明其他事实不漂移 |
| 链路 Header 注入 | 创建或接受接口若信任外部 request/trace Header，攻击者可把邮箱或密码送入日志、审计和 source event | 两个入口都在业务处理前生成内部链路 ID，不复用客户端 Header；路径、Header、日志、响应、审计和事件专项均覆盖 |
| 历史身份漂移 | 用户停用、改角色或改邮箱后，0052 若按当前状态校验会阻断合法脱敏 | 0052 仅验证不可变 Invitation 归属和 accepted 快照，不要求当前用户/租户/角色仍 active；新写入仍执行实时 active 与后端权限约束 |
| 迁移依赖损坏 | 只验证主表会让缺失 source 表或核心 trigger 的损坏栈进入运行期 | 0052 在首项持久 DDL 前验证 8 张依赖表、15 个核心 trigger 和全部历史接受图，任一缺失原子阻断 |
| 提交故障原子性 | 只注入 Audit/source 失败不能证明最终 commit 失败时不会留下半事务 | create 与 accept 均补充 commit 故障测试；Invitation/User、AuditLog 和 source event 全部回滚 |
| 并发真实性 | 单连接串行调用不能证明同一 Token 的真实竞争结果 | 两线程、两 SQLite 连接同时竞争同一 Token，结果严格为一个 201 和一个同形 410 |
| 已应用迁移 checksum | 0051 应用后新增说明注释也会改变 checksum，阻断后续迁移 | 从既有 PRD-060 pytest 证据恢复 0051 原始文本；SHA-256 精确回到 `4bc2793f8d825591195562415761375ba2881343e8b30b3099cfab01712a1c11`，所有新修正只进入 0052 |

第四轮终审后，0051 不可变、0052 隐私升级、事务原子性、真实并发、租户关系、后端权限和敏感信息
过滤均有直接测试与真实运行证据。最终结果登记在 `docs/testing/PRD-060-tests.md`。
