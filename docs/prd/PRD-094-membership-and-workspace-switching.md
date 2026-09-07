# PRD-094 成员关系、空间切换与邀请升级

状态：已完成
阶段：S1-Workspace
优先级：P0
依赖：PRD-091..093、PRD-059/060

## 1. 目标与边界

本条把 GlobalAccount、Workspace 和 tenant RBAC 连接成可动态撤权的 Workspace 上下文：账号可列出自己的空间、切换并获得独立 WorkspaceSession；owner/admin 可在委派上限内邀请、分配和停用企业职能成员；Owner 转移使用独立重认证与 CAS。

GlobalSession 只证明账号本人，WorkspaceSession 才允许访问 tenant 控制面。Platform/Support/Device 主体不由本条创建，也不能被解释为 Membership。SSO/SCIM 和 service_account 机器凭据发行留给后续 PRD；本条只保留 service_account 角色枚举并明确拒绝浏览器 Session。

## 2. 主体与 Cookie 隔离

WorkspaceSession 使用独立 `METAHUB_WORKSPACE_SESSION_SECRET`、issuer/audience 语义和不透明 256-bit Cookie：

- `__Host-metahub-workspace-session`：Secure、HttpOnly、SameSite=Lax、Path=/、无 Domain。
- `__Host-metahub-workspace-csrf`：Secure、SameSite=Strict、Path=/、无 Domain。
- TTL <=2h；每请求权威复核 GlobalAccount、Workspace、binding、Membership status/revision、role 和 legacy permission revision。

根密钥与 GlobalSession、Legacy Auth、Challenge、Cache、Webhook 等域独立；prod 必须显式配置至少32字符非默认值。

Identity 路由只解释 Global Cookie并把 Workspace Cookie视为同站惰性 Cookie；新 tenant 路由只解释 Workspace Cookie并把 Global Cookie视为惰性 Cookie。Workspace Cookie 与 Legacy Bearer 同时出现固定拒绝，禁止任选其一或选择权限更高主体。旧 Bearer 路由在 N/N-1 feature flag 下继续工作，但不能访问新 `/identity/workspace-*`。

## 3. Membership 模型与角色

0086 将0085 owner foundation表重建为完整 `workspace_memberships`：

- 主键 `wmb_`，复合 owner `workspace_id+tenant_id`，唯一 `(workspace_id,account_id)`。
- role：`owner/admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer/service_account`。
- status：`pending/active/disabled`；service_account 只能由后续机器身份流程激活浏览器外凭据。
- 单调 `membership_revision`，role/status变化每次递增；保存 creation source、legacy user projection、created/updated actor和时间。
- active owner至少一个；owner不能通过普通 PATCH 授予、降级、停用或删除，只能经 OwnerTransfer。

兼容期 Membership role 到 legacy User role 的映射固定：owner/admin -> admin，developer -> developer，security_admin -> security_admin，finance_admin -> finance_admin，legal_admin/auditor/viewer -> viewer。`service_account` 不创建浏览器 User Session。该映射只服务 N/N-1旧权限矩阵，不把 legacy admin claim反向提升为 Workspace owner。

现有每条 0083 Legacy Link 在0086回填一个 Membership；0085已选 owner保持 owner，其他 admin/ops_admin映射为admin，其余按上表映射。重复 account+workspace若由历史冲突产生，Workspace进入 migration_conflict并禁止签发 Session，不能静默选择一条 User。

## 4. WorkspaceSession 数据模型

`workspace_sessions` 保存：session ID、global session ID、account/workspace/tenant/membership、token/CSRF HMAC、status、membership revision、workspace revision、account session revision、legacy permission revision和matrix fingerprint、created/last_seen/absolute/idle expiry、revocation reason。

Session不信任客户端 tenant/role。切换时服务端从 active Membership和不可变 binding恢复 tenant，复核 legacy User projection与权限矩阵，再签发。每个请求重新读取权威 revision；任一不一致即 revoked，提交后的下一请求成功数为0。

切换默认撤销当前浏览器的旧 WorkspaceSession并签发新值。不同设备可各自保持 WorkspaceSession；账号安全页撤销 GlobalSession时，与其绑定的全部 WorkspaceSession同步失效。

## 5. 邀请升级

新增 `membership_invitations` 作为新权威状态机，绑定 workspace/tenant、目标 email HMAC、目标 workspace role、inviter membership revision、token hash、状态、到期和可空 account binding。旧 `/invitations` 路由版本化：

- WorkspaceSession 调用写入 membership invitation，owner/admin按委派上限授权。
- Legacy Bearer 在 N/N-1期间继续写旧 Invitation，同时写升级桥，不能获得新角色上限。
- PRD-092验证成功产生的 `verified_pending_membership` binding 在同一事务消费 Invitation、创建/激活 Membership与legacy User projection、写事件/outbox；并发最多一次。
- 已存在 GlobalAccount仍需受邀邮箱验证和当前账号证明；不能仅凭邮箱相同自动加入。

无效、过期、已接受、跨 Workspace 和被撤权 inviter 对公开接受端完全同形。Invitation Token、邮箱、角色外的企业信息不进入验证前页面或事件。

## 6. 成员管理与委派上限

- owner可邀请和管理全部非owner职能角色；不能通过普通成员 PATCH创建第二个 owner。
- admin可邀请/管理 `admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer`，但不能管理 owner、不能把目标提升为owner，也不能操作自身无法委派的权限。
- developer/security/finance/legal/auditor/viewer 的成员写 API固定403；跨 Workspace ID与不存在ID同形404。
- 成员停用、role变化和邀请人失权在一个事务中递增 revision、撤销目标 WorkspaceSession并写 source/outbox。
- 最后 owner保护由数据库 trigger和服务内 CAS双重执行。

页面清单由后端根据 Workspace role、legacy permission revision和固定产品域返回。隐藏菜单不替代 API鉴权；直达 URL、恢复书签和迟到响应仍逐请求复核。

## 7. OwnerTransfer 状态机

`owner_transfers`: `pending -> completed | cancelled | expired | superseded`，保存 source/target membership、两者 revision、workspace revision、重认证时间、request_id和固定 reason。

`POST /workspace-owner-transfers` 只允许当前 owner，要求 WorkspaceSession+Origin+CSRF、当前密码重认证；若账号已配置 MFA则还要求完成受支持挑战，本版本不得伪装 MFA。事务锁内复核 source仍为owner、target active且非service_account、revision未变、目标未跨Workspace，然后原子把 target设owner、source设admin、两边revision递增、撤销旧 Session并写事件/outbox。并发转移最多一个提交；任何中间失败不产生双 owner缺口。

## 8. HTTP 合同

| 方法与路径 | 主体 | 返回/写入边界 |
| --- | --- | --- |
| `GET /identity/workspaces` | GlobalSession | 只列本人 active Membership安全摘要；单空间可直达，多空间可选 |
| `POST /identity/workspace-switches` | GlobalSession+Origin+global CSRF | exact `workspace_id`；服务端恢复tenant/role，轮换 Workspace Cookie |
| `GET /memberships` | WorkspaceSession | 当前 Workspace有界分页；按调用角色裁剪字段 |
| `PATCH /memberships/{membership_id}` | WorkspaceSession+workspace CSRF+If-Match | exact role/status字段；执行委派上限和revision CAS |
| `POST /workspace-owner-transfers` | Workspace owner+workspace CSRF+密码重认证 | exact target/current_password；原子转移与Session撤销 |

新 `/console/navigation` 目标语义接受 WorkspaceSession，只返回当前角色页面 ID和动作摘要，不返回平台渠道/客户成功/增长/Provider工程/商业裁决页面；返回值不能替代页面 API的权限判断。

所有 tenant 业务请求禁止 tenant_id query/body/header 作为 owner选择；兼容 DTO若包含 tenant_id，只能与 Session binding一致，否则固定失败。跨租户资源保持同形404和有界主要时延。

## 9. 切换、迟到响应与跨标签页

Workspace Cookie由浏览器同站共享，因此一个标签页切换会改变其他标签页的授权上下文。前端使用 `BroadcastChannel` 只广播无身份内容的 `context-invalidated` generation，不广播 workspace/tenant/account/session/role/Token，也不使用 localStorage/sessionStorage。

收到切换、Membership降权、401/403或广播后，每个标签页必须：递增请求 generation、abort在途请求、清空页面数据/缓存/草稿、重新读取 `/identity/workspaces` 和当前 WorkspaceSession摘要，再加载后端导航。旧 generation响应不得重新填充DOM。退出 GlobalSession同时删除 Global与Workspace两组Cookie。

## 10. 迁移与回滚

0086只从完整0085升级，重建 Membership前保存并逐行复核 owner foundation；回填所有 Legacy Link后检查每个 active Workspace至少一个owner、每个Membership的account/workspace/tenant/user链完整、FK=0和integrity ok。

pristine down仅允许无 WorkspaceSession、无新邀请消费、无成员role/status变化、无OwnerTransfer、所有Membership仍等价于legacy backfill。任何 revision>初始值、撤权tombstone、事件或新增成员均原子阻止 schema down。运行时回滚可禁用 Workspace Cookie路由并回到旧Bearer N/N-1，但不得删除Membership、恢复旧role、复活Session或丢失邀请接受事实。

## 11. Console 交互

账号顶栏提供紧凑空间选择器；单空间直接进入，多空间显示可搜索列表但不显示 tenant_id。成员页按职责提供邀请、角色和停用控件；Owner 转移要求重新输入密码并勾选页面内显式确认，不使用阻塞式原生确认框。service_account明确显示为机器主体且无“登录/切换”动作。

390/1280/1440视口必须无重叠、裁剪或横向溢出；键盘可完成选择、邀请、成员编辑和Owner转移。页面不显示 issuer、revision、matrix hash、legacy User、内部 Session ID或平台角色。

## 12. 性能与可观测

- Workspace切换 API warm p95 `<300 ms`，包含 Membership/RBAC复核、旧Session撤销、新Session/event事务。
- 切换后首个角色页可交互 p75 `<1.5s`。
- 动态 Membership/Owner撤权在线提交后 `<2s`，本地SQLite测试要求下一请求立即失败。
- 日志/事件只含内部ID、role code、revision、固定reason、request_id/trace_id；不含邮箱、邀请Token、Cookie、完整IP/UA。
- 本条不产生模型Usage或成本账，验收标记N/A。

## 13. 专项测试

覆盖0085->0086/fresh/down/up、全量legacy membership回填、九角色矩阵、owner/admin委派、最后owner保护、100并发切换/停用/Owner转移、旧Session下一请求失败、跨Workspace同形404、邀请新/已有账号接续、旧PRD-060 N/N-1、Global/Workspace/Legacy/Platform/Device凭据负矩阵、CSRF/Origin/Cookie tossing、迟到响应/BroadcastChannel/Storage清理、真实浏览器与三视口、性能门限和敏感canary 0命中。

## 14. 第一轮自审：授权、迁移与兼容

结论：通过，P0/P1已修正。

- P0：若WorkspaceSession复用Global或Legacy密钥/Cookie，会把账号证明直接补成tenant权限。修正为独立根密钥、Cookie、状态表和route-specific解释矩阵。
- P0：只在签发时读Membership，降权后旧Session仍可用。修正为每请求复核account/workspace/membership/permission revision并提交即撤销。
- P0：旧User角色没有owner/legal/auditor。修正为Membership保存目标角色，legacy映射只作N/N-1兼容且不能反向提升owner。
- P0：邀请继续固定“创建新User”无法支持已有GlobalAccount。修正为MembershipInvitation+092 verified binding原子升级。
- P1：0086回填若同account/workspace出现多条legacy link不能任选。修正为migration conflict并禁止签发。

## 15. 第二轮自审：并发、会话、隐私与体验

结论：通过，P0/P1已修正。

- P0：Owner可经普通PATCH被停用或最后Owner并发丢失。修正为普通PATCH禁止owner变更，独立OwnerTransfer锁内CAS与数据库最后owner约束。
- P0：切换只更新前端active_workspace会让tenant由客户端选择。修正为服务端Membership+binding恢复并轮换Opaque WorkspaceSession。
- P0：Bearer和Workspace Cookie并存若选择任一会形成凭据混淆。修正为固定拒绝；Global Cookie仅作为同站惰性Cookie。
- P0：跨标签页切换后旧请求可回填另一个Workspace数据。修正为BroadcastChannel无身份失效信号、abort generation和DOM/缓存/草稿清理。
- P1：Owner转移在MFA尚未交付时不能声称强MFA。修正为当前密码重认证；已配置MFA时必须真实挑战，否则失败关闭并标明后续能力。
- P1：页面清单若作为唯一授权来源，直达API仍可越权。修正为导航仅投影，所有资源API逐请求复核owner和revision。

两轮修正已写回本设计，编码不得弱化这些边界。

## 16. 实现记录

- `infra/migrations/0086_membership_workspace_sessions.sql` 与 `.down.sql` 交付完整 Membership、
  WorkspaceSession、MembershipInvitation、OwnerTransfer、MembershipEvent/Outbox、legacy 邀请升级桥、
  动态撤权 trigger 和安全事实 down guard；0086 同时修复 N/N-1 新 User 投影不能替换 immutable creator。
- `control-plane-api/src/metahub_control/workspace_access/` 交付七个 Workspace/Membership 路由、严格
  Global/Workspace Cookie 解释、委派矩阵、邀请接续、Owner 转移 CAS 和数据库权威 revision 复核。
- `/console/navigation` 接受 WorkspaceSession 并按当前 Workspace 权限返回导航投影；既有 Bearer 路径在
  N/N-1 内保持兼容，但两种凭据不能混用。
- `console-web/src/lib/workspaces.js` 与 Identity 页面交付紧凑 Workspace 选择器、成员管理、邀请和
  Owner 转移；BroadcastChannel 只发布无身份 invalidation，Abort/generation 防止迟到响应回填，
  浏览器 Storage 保持零调用。
- 专项后端、迁移、配置、Console 单元、兼容和敏感 canary 已通过；应用内 Browser 已完成登录、单空间
  自动进入、第二 Workspace 创建、可搜索选择器、成员编辑、邀请、Owner 转移、跨标签页失效和键盘流程。
- 390/1280/1440 三个视口无重叠、裁剪或横向溢出，Console 错误为 0；切换到首个角色页可交互的
  8 次干净采样 p75 为 `805.8 ms`，满足 `<1.5s`。切换成功响应直接提供已验证上下文，随后并行读取
  Membership 与导航；授权漂移和 BroadcastChannel 失效仍执行完整服务端复核。
- 开发启动器固定进入关闭 raw access log 的安全入口；邀请接受 canary 日志只保留参数化路径，原始邀请
  token 不进入请求日志。完整脱敏验收记录见 `docs/testing/PRD-094-tests.md`，下一权威项为 PRD-095。

## 17. 跨页 GlobalSession 退出竞态修复

2026-09-07 的 PRD-137 真实 HTTPS/Chromium 验证在桌面和移动视口同时发现：退出 API 已返回
200，另一账号页仍保留受保护 shell。原通知先于退出事务发出，接收方仅重读 Workspace，可能
先取得仍有效的 Global/Workspace 摘要，再因后续 401 停留在空间错误态，而未清除账号页面。

第一轮自审：保留现有仅 type/generation 的无身份通知合同，不添加账号、Cookie 或权限内容。
收到通知必须立即清空全部受保护状态并中止旧请求，然后从 `/identity/me` 开始重验 GlobalSession，
再走既有空间列表/WorkspaceSession/成员/导航链。通知本身不能授予权限，服务端仍是唯一依据。

第二轮自审：退出和撤销当前设备先广播以立即清除跨页旧数据；异步写入结束的 finally 再广播，
使提交前返回的合法旧摘要同样失效。失败也需触发权威重读，不能将通知等同于服务端撤销已成功。
新的验证仍受 generation/AbortController 约束；重放或迟到的 `/identity/me` 不能复活已清状态。
不修改后台鉴权、Cookie、DTO、迁移或主控制台消息 schema；产品修复与 PRD-137 探针独立提交。
