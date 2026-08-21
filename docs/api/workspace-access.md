# Workspace Access API

PRD-094 把 GlobalAccount、Workspace、Membership 和租户 RBAC 连接为可动态撤权的浏览器授权上下文。
GlobalSession 只用于发现和选择本人可访问的 Workspace；WorkspaceSession 才能访问当前 Workspace
的成员与租户控制面。客户端提交的 tenant、role 或权限声明不参与授权。

## Cookie 与请求边界

- Global Cookie：`__Host-metahub-global-session` 与 `__Host-metahub-global-csrf`，仅用于
  `/identity/workspaces` 和 `/identity/workspace-switches`。
- Workspace Cookie：`__Host-metahub-workspace-session` 为 Secure、HttpOnly、SameSite=Lax；
  `__Host-metahub-workspace-csrf` 为 Secure、SameSite=Strict。两者均为 Path=/、无 Domain。
- `METAHUB_WORKSPACE_SESSION_SECRET` 是独立 HMAC 根密钥；生产必须显式配置至少 32 字符，且不能与
  Auth、GlobalSession、Challenge、PII、Cache 或其他根密钥复用。
- 写请求要求唯一且精确匹配允许列表的 `Origin`；CSRF Header 必须与对应 Cookie 常量时间相等。
- Workspace Cookie 与 Legacy `Authorization` 同时出现固定返回
  `workspace_session.credential_conflict`。Global、Platform、Support、Device 和 service_account
  主体均不能回退或提升为浏览器 WorkspaceSession。
- 请求正文限制 4096 bytes，只接受严格 JSON、唯一 Header 和字段白名单；响应固定
  `Cache-Control: no-store, private` 并返回服务端生成的 request/trace ID。

## 接口

| 方法 | 路径 | 主体与请求 | 成功结果 |
| --- | --- | --- | --- |
| GET | `/identity/workspaces?limit=20&offset=0` | GlobalSession | 本人 active Membership 的安全 Workspace 摘要 |
| POST | `/identity/workspace-switches` | GlobalSession、global CSRF；`{workspace_id}` | 撤销当前浏览器旧 WorkspaceSession，签发新 Workspace Cookie |
| GET | `/workspace/session` | WorkspaceSession | 当前 Workspace、kind、name、slug、role 和过期时间摘要 |
| GET | `/memberships?limit=20&offset=0` | WorkspaceSession | 当前 Workspace 的有界成员列表；字段按调用角色裁剪 |
| PATCH | `/memberships/{membership_id}` | WorkspaceSession、workspace CSRF、`If-Match`；`{role?,status?}` | 委派上限与 revision CAS 后的成员摘要和新 `ETag` |
| POST | `/membership-invitations` | WorkspaceSession、workspace CSRF；`{email,role,expires_in_hours?}` | `201`；pending 邀请摘要和一次性 accept token |
| POST | `/workspace-owner-transfers` | owner WorkspaceSession、workspace CSRF；`{target_membership_id,current_password}` | 原子 Owner 转移后的 source/target 摘要 |
| GET | `/console/navigation` | WorkspaceSession | 后端按当前角色和权威权限计算的企业导航投影 |

`GET /identity/workspaces` 只返回 `workspace_id/kind/name/slug/role`；不返回 tenant、account、
legacy User、Session 或内部 revision。`GET /workspace/session` 不返回 issuer、audience、matrix hash、
legacy permission revision 或内部 Session ID。

成员摘要只返回 `membership_id/display_name/role/status/revision/is_current/can_manage`。
`service_account` 可以出现在列表中，但不能切换或获得浏览器 Session。邀请 accept token 只在创建成功
响应中出现一次；Console 可复制接受链接，但不得把 token 插入 DOM、日志、事件或 Storage。

## 授权与撤权

- owner 可邀请和管理全部非 owner 职能角色；普通成员 PATCH 不能授予、降级、停用或删除 owner。
- admin 可管理 `admin/developer/security_admin/finance_admin/legal_admin/auditor/viewer`，不能管理 owner
  或超出自身委派上限；其他角色的成员写请求固定 403。
- `If-Match` 使用双引号十进制 Membership revision。缺失返回
  `membership.revision_required`，冲突返回 `membership.revision_conflict`。
- 每个 Workspace 请求从数据库复核 GlobalAccount、GlobalSession、Workspace、binding、Membership、
  Workspace revision、account session revision、legacy permission revision 和矩阵 fingerprint。
- 成员 role/status 变化、Owner 转移或上游 GlobalSession 撤销会同步撤销关联 WorkspaceSession；
  已提交撤权后的下一请求不能成功。
- 跨 Workspace 和不存在的 Membership/资源保持同形 404；导航隐藏不替代资源 API 的后端鉴权。

## 邀请与兼容期

`membership_invitations` 是新权威邀请状态机。旧 PRD-060 Bearer 路由在 N/N-1 期间继续工作，并写入
升级桥；它不能获得高于旧角色矩阵的委派能力。PRD-092 验证成功后，同一事务最多一次消费邀请，创建或
激活 Membership、legacy User projection、MembershipEvent 和 Outbox。无效、过期、已接受、跨 Workspace
或 inviter 已失权的邀请对公开接受入口保持同形。

## 事件与数据最小化

事件类型固定为 `membership.created`、`membership.updated`、`membership.disabled`、
`membership.invited`、`invitation.accepted`、`workspace.switched` 和
`workspace.owner_transferred`。事件与 Outbox 只保存内部 ID、role、revision、固定 reason、request_id
和 trace_id；不得包含邮箱明文、密码、Cookie、CSRF、邀请 token、完整 IP/User-Agent、tenant 名称或业务正文。

## 固定错误族

- `workspace_session.required|invalid|expired|revoked|csrf_invalid|credential_conflict|permission_denied|unavailable`
- `membership.request_invalid|not_found|revision_required|revision_conflict|update_invalid|invitation_invalid|invitation_conflict|invitation_not_available|unavailable`
- `owner_transfer.request_invalid|reauthentication_failed|mfa_required|conflict|unavailable`

所有错误使用统一错误 envelope。Owner 转移要求当前密码重认证；若账号已经配置 MFA，则必须完成受支持的
真实挑战，否则返回 `owner_transfer.mfa_required`，不能伪装 MFA 已验证。
