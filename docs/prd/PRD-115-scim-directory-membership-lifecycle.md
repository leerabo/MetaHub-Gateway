# PRD-115 SCIM 目录同步与成员生命周期

状态：实现、自动化门禁、本地真实认证和 Browser 静止终态验收均已完成。依赖 PRD-094/114；范围以
Excel PRD-115 为准。S4 批次 G4 保留到 PRD-119 独立提交后统一执行。

## Excel 权威合同

- API：`GET/POST/PATCH /scim/v2/Users`、`GET/POST/PATCH /scim/v2/Groups`、
  `POST /workspaces/{id}/scim-tokens`、`GET /workspaces/{id}/scim-syncs`。
- 对象：`ScimToken`、`ScimUserLink`、`ScimGroupLink`、`ScimRoleMapping`、`ScimSyncRun`、`ScimConflict`。
- 事件：`scim.sync_completed`、`membership.disabled_by_scim`、`scim.conflict_detected`。
- SLO：SCIM 请求 p95 < 500ms；停用到权限失效 < 5s；1 万成员全量同步 < 30 分钟。

## 1. 产品闭环

security_admin 创建只显示一次的 SCIM Bearer，配置 group -> role mapping。IdP 通过独立 SCIM endpoint
创建、读取、分页、过滤和 PATCH Users/Groups；服务用 token 恢复唯一 Workspace owner，不接受 tenant 或
workspace 参数。目录变更映射到既有 Membership，停用同事务撤销 Workspace Sessions；冲突进入人工队列，
不能静默覆盖 Owner、人工邀请或更高 revision。

## 2. 0107 数据与状态机

- `scim_tokens`：Workspace/tenant、token hash/prefix、scope、`active|rotating|revoked`、revision、expires/
  last-used；明文只在创建/轮换响应出现一次。
- `scim_user_links`：Workspace、external ID、account、Membership、canonical userName HMAC、source version、
  `active|disabled|conflict`。
- `scim_group_links` 与 `scim_group_members`：external group/version 与目录成员关系，全部 owner 复合约束。
- `scim_role_mappings`：group -> 允许租户角色；禁止 owner、service_account 和 Platform 角色。
- `scim_sync_runs`：token、started/completed、cursor、created/updated/disabled/conflict counts、安全 outcome。
- `scim_conflicts`：对象、原因、当前/目录 revision hash、`open|resolved|ignored`、CAS 和人工 actor。
- `scim_events/outbox`：只保存计数、对象 ID、状态和时间，不含 Bearer、email、displayName 或 SCIM payload。

Down 在 token/link/group/mapping/run/conflict、SCIM 来源 Membership 或未投递事件存在时拒绝。

## 3. SCIM 合同与授权

支持 RFC 7643 core User/Group 和 RFC 7644 list/POST/PATCH；Content-Type 固定 `application/scim+json`，
错误使用 SCIM schema、status、scimType 和固定 detail。filter 仅允许 `userName eq`、`externalId eq`、
`displayName eq`；startIndex/count 有上限，拒绝复杂/未知 filter。PATCH 只允许 active、name/displayName、
members 和受控 enterprise attributes，拒绝任意路径和 extension。

SCIM Bearer 只用于 `/scim/v2/*`，不能调用 Console、Gateway 或 OAuth；token rotation 有短 grace，旧 token
不能创建新 sync。Workspace API 使用 Cookie/CSRF、`scim.read/manage`、path owner 和 CAS。

## 4. Membership 生命周期

- 新 User 只有在账号已存在且验证邮箱/VerifiedDomain 匹配时链接；否则创建 pending directory invitation，
  不创建可登录账号或猜测个人身份。
- SCIM source 不能修改 Owner；最后 Owner 保护在数据库触发器和服务事务双重执行。
- `active=false` 原子更新 link + Membership disabled，撤销全部 Workspace Sessions 并提升 revoke generation。
- 乱序 `meta.version`、重复 externalId、并发 PATCH 使用 ETag/CAS；旧版本只能返回 409/412，不能复活成员。
- group role 映射只授予操作者可管理的角色，多个 group 取显式最低风险合并规则；冲突不自动提权。

## 5. Console

Identity/Security 页面显示 token prefix/状态/到期、role mappings、sync 计数/终态、冲突原因类别和恢复动作。
token 明文只在创建/轮换 Dialog 出现一次，关闭即从 DOM/state 清除；Platform Support 仅在有效 SupportGrant
下读取固定错误类别和 sync 计数，不能见 email、group 名、成员或 token prefix。

## 6. 第一轮自审：目录所有权与提权

- P0：信任 SCIM 请求中的 workspaceId 会跨租户。修正为 Bearer hash 唯一恢复 owner，body/query 禁止 owner。
- P0：group 映射 owner 会接管企业。修正为 schema/DB/service 三层禁止 owner/service_account/platform role。
- P0：自动创建已激活账号会绕过注册验证。修正为只链接已验证账号，否则 pending invitation。
- P1：复杂 filter 解析易产生注入和不一致。修正为结构化 parser 与固定属性/操作 allowlist。
- P1：Support 看到目录字段会泄漏员工信息。修正为只投影计数和固定错误码。

## 7. 第二轮自审：停用、乱序与恢复

- P0：只禁用 Membership 不撤销 Session 会延迟撤权。修正为同事务撤销 workspace_sessions 和 generation。
- P0：乱序 PATCH 可复活已停用用户。修正为 source version、ETag/CAS 和 terminal disable fence。
- P1：全量同步中断后从头覆盖会扩大损害。修正为持久 cursor、幂等 external ID 和 checkpoint resume。
- P1：token rotation 立即切断大目录同步。修正为有界 grace，旧 token 只允许完成已绑定 sync run。
- P1：冲突自动解决会覆盖人工 Owner/角色。修正为 open conflict，人工 CAS 决策和不可变审计。

## 8. 专项验证计划

0106/0107 fresh/往返/down guard；SCIM content/filter/page/PATCH 合同；token 单次显示/轮换/过期；10k 成员
同步与 checkpoint；重复/乱序/并发；Owner 保护；停用 <5s 与 Session 即时失效；跨 Workspace 零泄漏；
DB/outbox/中断恢复；Console token DOM 清理、三浏览器与敏感扫描。
