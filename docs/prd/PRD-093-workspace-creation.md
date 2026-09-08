# PRD-093 个人空间与企业 Workspace 创建

状态：已完成
阶段：S1-Workspace
优先级：P0
依赖：PRD-091、PRD-092、现有 Tenant/Environment/RBAC

## 1. 目标与边界

已验证 GlobalAccount 可以创建 personal 或 organization Workspace，并原子成为 Owner。Workspace 是 SaaS 业务隔离根；兼容期每个 Workspace 与现有 Tenant 一一不可重绑，业务表继续以 `tenant_id` 为物理 owner。

本条只建立 Owner Membership 基础，不开放邀请、成员列表、成员停用、角色委派、Owner 转移或 WorkspaceSession；这些由 PRD-094 交付。GlobalSession 仍不能调用旧 tenant 业务 API。

明确不做：自动创建 Provider、Provider Credential、模型、Virtual Key、项目、预算、成功调用或占位业务数据。只创建可证明来源的 default `dev` Environment，并明确标记 `system_created`，不能把它显示为用户已完成配置。

## 2. 权威 owner 模型

`WorkspaceTenantBinding(workspace_id UNIQUE, tenant_id UNIQUE)` 是唯一兼容桥。服务端从已验证 Membership/Workspace 恢复 tenant；客户端提交的 tenant_id 永远不能选择 Workspace 或扩大权限。绑定创建后禁止 UPDATE/DELETE，Region、kind 和 tenant owner 同样不可静默重绑。

PRD-093 必须创建 Owner Membership，否则“创建并成为 Owner”无法原子验收。但 094 才开放 Membership 管理。因此 0085 建立 `workspace_memberships` 的最小不可管理基础：只允许 active owner、revision=1、source=`workspace_creator|legacy_backfill`；0086 再扩展其他角色、状态和管理状态机。

## 3. 兼容投影与证明

新 Workspace 仍需 legacy Tenant/User/Role/Environment 投影，供 N/N-1 企业 API 和现有复合外键工作。不能让 0083 User trigger 仅凭相同邮箱或密码 Hash 自动合并 GlobalAccount。

0085 新增 `workspace_user_projection_claims`：服务在持有已验证 GlobalSession、创建事务和目标 account/tenant/user 后写入一次性 pending claim；随后插入 legacy User。重写后的 User identity trigger只有在 claim 的 account、credential revision、邮箱规范值和 Hash 全部匹配时，才把该 User Link 到已有 GlobalAccount并原子消费 claim；否则继续执行 0083 的“一 User 一 Account、冲突隔离”规则。claim 不可跨事务复用，不能由请求正文提供 account/tenant/user。

一个 GlobalAccount 可以因此拥有多个 Legacy Link，但每个 `(tenant_id,user_id)` 仍只有一个 account owner。任一 legacy 密码变化都按全局凭据变化处理，递增 credential/session revision并撤销全部 GlobalSession；不能只更新单 Workspace 的密码副本。

## 4. 数据模型

0085 新增：

- `workspaces`：`wsp_` ID、kind、status、profile state、creation source、revision、创建者 account、时间戳。
- `workspace_tenant_bindings`：Workspace/Tenant 双唯一、status、created_at；不可变。
- `workspace_regions`：workspace、region code、residency policy version、status；首版只允许受审境内 Region。
- `organization_profiles`：workspace、名称、规范 slug、profile state、revision、时间戳。
- `personal_profiles`：workspace、account、安全展示 label、revision；不复制密码、邮箱或手机号。
- `workspace_memberships`：本条只写 owner foundation，唯一 `(workspace_id,account_id)`，同时保存 tenant binding 和单调 revision。
- `workspace_provisioning_jobs`：workspace、generation、`core_committed|ready|failed`、attempt、固定错误码和时间戳。
- `workspace_environment_origins`：workspace/tenant/environment、`system_created|legacy_existing|user_created`，证明默认环境来源。
- `workspace_events` 与 `workspace_event_outbox`：`workspace.created/profile_confirmed/provisioning_failed` 的持久 source/outbox。
- `workspace_user_projection_claims`：一次性内部兼容证明，不进入公开 DTO。

所有 workspace-owned 表同时保存 workspace_id/tenant_id 时必须通过 binding 复合关系证明一致。事件 payload 不含邮箱、密码 Hash、企业原始表单、Provider Secret、Prompt 或 Response。

## 5. 创建事务

`POST /workspaces` 要求 GlobalSession、已验证 account、精确 Origin、CSRF 和唯一 `Idempotency-Key`。服务端在后台线程中执行：

1. 规范化请求并读取 account verification。
2. 用 `account_id + idempotency_key + request_hash` 查询幂等记录；同 key 异 body 固定拒绝。
3. `BEGIN IMMEDIATE`，创建 Workspace、Tenant 和不可变 binding。
4. 依赖现有 tenant trigger 原子生成完整 legacy Role/Permission 矩阵并复核。
5. 写一次性 projection claim，创建绑定当前 account 的 legacy admin User；数据库 trigger消费 claim。
6. 创建 owner foundation Membership 和唯一 default dev Environment，写 origin=`system_created`。
7. 写 profile、Region、ProvisioningJob、workspace.created event/outbox 和幂等结果。
8. 复核没有 Provider/Key/Model/VirtualKey/Project 等假资源后提交。

只要步骤 1..8 任一失败，整个事务回滚，不产生半初始化 Tenant、孤儿 Owner 或可见 Workspace。事务提交即代表核心 Workspace 可用；后续 Provisioning worker只处理非授权派生投影，不得补写 owner、binding、Region 或默认 Environment。它失败时 Workspace 核心事实仍可查询，并显示可重试的固定状态。

## 6. 现有 Tenant 回填

0085 为每个 Tenant 建立确定性 organization Workspace 和 binding；`default` 标记 `legacy_bootstrap`，其他为 `legacy_backfill`。每个 Tenant 按以下顺序选择且只选择一个 owner foundation：active admin 按 `created_at,user_id` 最早者；没有 active admin 时进入 `migration_conflict`，不静默提升其他角色。

owner account 必须经 0083 `account_identity_links` 从 legacy User 恢复。其他 User 的 Membership 留到 0086。已有 Environment 只写 `legacy_existing` origin，不创建新的 dev；若 tenant 缺少 dev，则回填 Workspace 保持 `migration_conflict`，由受控修复处理，迁移不伪造业务完成状态。

## 7. HTTP 合同

| 方法与路径 | 认证与输入 | 返回边界 |
| --- | --- | --- |
| `POST /workspaces` | GlobalSession+CSRF；kind，organization name/slug，Region，service mode；禁止 tenant/account/owner 字段 | 本人新 Workspace 安全摘要与 provisioning 状态 |
| `GET /workspaces` | GlobalSession；有界分页 | 只列当前 account 的 owner foundation Workspace |
| `GET /workspaces/{workspace_id}` | GlobalSession+本人 Membership | 当前 Workspace/profile/Region/environment source 摘要；跨账号同形 404 |
| `PATCH /workspaces/{workspace_id}` | GlobalSession+CSRF+本人 owner；If-Match revision | 只允许展示名/企业名称和 draft->confirmed；kind/slug/Region/binding 不可改 |

PRD-094 会把列表的可见范围扩展为所有 active Membership，并提供 `/identity/workspaces` 切换 DTO。本条的 GET 不签发 WorkspaceSession，也不返回 tenant_id、legacy user/role、account_id、内部 job owner 或平台运营字段。

## 8. slug、Region 与资料状态

organization slug 使用 ASCII 小写 `a-z0-9-`、3..48、首尾字母数字、禁保留字。冲突统一 `workspace.create_conflict`，不证明现有组织名称、owner或状态。personal slug 由服务端随机 ID 派生，不能由邮箱生成。

首版 Region allowlist 只包含配置中已批准的境内 Region；创建后不可 PATCH。未知、跨境或配置不可用失败关闭。OrganizationProfile 为 `draft|confirmed`；确认只推进一次，修改已确认名称递增 revision并写事件。所有展示字段输出转义，数据库事件只存固定摘要。

## 9. 迁移与回滚

0085 只从完整 0084 升级，迁移前后复核 0083/0084安全事实、FK和完整性。等价 backfill 投影允许 pristine down：不得存在 `user_created` Workspace、profile 修改、额外 Membership、WorkspaceSession、Owner transfer、非回填 event 或 revision>1。down 不修改 Tenant/User/Environment；删除的只是不具新增事实的确定性 Workspace 投影。

一旦创建新 Workspace 或产生 profile/event/provisioning 安全事实，schema down 原子拒绝。运行时回滚关闭创建和新读取路由，但保留 Workspace、binding、Owner、Region 和环境来源；不能删除 Tenant 或把 user-created Workspace伪装成 legacy。

## 10. Console 交互

账号安全页增加“创建空间”入口：先选个人/企业，再填写企业名称、slug、Region和服务模式。页面不展示 tenant_id、legacy role、内部 provisioning generation 或平台状态。成功后展示 Workspace 摘要和下一步，不出现假 Provider/Key/调用成功提示。失败保留非敏感草稿，但密码、Cookie、CSRF 和 account 标识不进入 Storage。

## 11. 性能与可观测

- Workspace 核心创建 warm p95 `<1s`，包括 Tenant/RBAC/User/Owner/Environment/event 单事务。
- 提交后首屏核心摘要 `<2s`；非授权 Provisioning 不阻塞。
- 幂等重放不重复创建 Tenant、Owner、Environment 或事件。
- 日志只含 workspace/internal tenant ID、kind、固定状态、request_id/trace_id和耗时，不含企业资料原文。
- 本条不产生模型 Usage 或成本账，验收标记 N/A。

## 12. 专项测试

覆盖 0084->0085/fresh/down/up、existing tenant backfill、owner election/conflict、personal/organization、100 并发幂等、同 key 异 body、slug冲突同形、Region失败关闭、owner+binding+legacy projection原子性、RBAC完整性、default env来源、零假资源、profile CAS、跨账号404、DB/worker故障、390/1280/1440、键盘/读屏、DOM/Storage/log/DB敏感扫描和全部旧 tenant/auth/environment 回归。

## 13. 第一轮自审：范围、迁移与兼容

结论：通过，P0/P1 已修正。

- P0：PRD-093要求 Owner Membership，但路线图把 Membership 全放在094。修正为0085只建不可管理 owner foundation，0086再开放完整状态机。
- P0：直接插入 legacy User 会触发0083创建第二个 GlobalAccount，按邮箱/Hash自动合并又违反091。修正为一次性 projection claim 和触发器证明分支。
- P0：现有 tenant 可能有多个 admin 或无 admin。修正为确定性最早 active admin成为 owner；无候选进入冲突，不提升低权限角色。
- P0：只写 Workspace 不创建 Tenant/RBAC/User/Environment 会让成功响应对应半初始化 owner。修正为全部核心事实单事务，worker不得补权。
- P1：迁移回填若无 dev 环境时自动补一个会伪装用户完成配置。修正为 migration_conflict，不静默创建。

## 14. 第二轮自审：安全、并发、隐私与体验

结论：通过，P0/P1 已修正。

- P0：客户端 tenant_id 或 account_id 若进入创建正文可跨 owner 绑定。修正为 exact DTO完全不接受，均从 GlobalSession/数据库恢复。
- P0：幂等 key 若不绑定规范 body，可用同 key 替换 kind/Region。修正为 account+key+request hash唯一并拒绝异 body。
- P0：Workspace与Tenant分别唯一不足以阻止更新重绑。修正为不可变 trigger和所有双 owner表复合约束。
- P1：slug冲突、跨账号详情和 migration conflict若返回差异会枚举企业。修正为固定冲突/404和有界响应。
- P1：页面若把 Workspace/tenant内部 ID 写Storage再继续旧请求，会造成切换前污染。修正为本条不签发 WorkspaceSession，页面只持内存展示 DTO。

两轮修正已写回本设计，编码不得弱化这些边界。

## 2026-09-08 Workspace 原生能力发现补充自审

与 PRD-091 的 Identity/Workspace 原生能力发现批次连续实施。创建流程通过 _table_exists
判断是否应写 workspace_operational_states；当前该 helper 只查询 sqlite_schema。

第一轮自审：改为既有 backend-aware table_exists，保留当前 schema、真实表而非 view 的
约束；不把缺表默认为已初始化，不跳过必要状态事实或原有鉴权、幂等、唯一性保护。

第二轮自审：检查必须使用同一创建事务连接，不能提交半成品 Workspace/Tenant/Owner。
先用现有 SQLite 能力与事务用例检查兼容，再以实际 PostgreSQL 注册/建空间验证；元数据或
初始化写失败仍整笔回滚。PRD-093 单独保留记录和提交，本项不扩展为整组原生 QA 已完成。

### 原生初始化 SQL 补充两轮自审

第一轮：真实 PostgreSQL r5 在创建运营状态时报告 SQLSTATE 42601。共享连接只转换参数和
BEGIN IMMEDIATE，不翻译 INSERT OR IGNORE。沿用 seed 的 ON CONFLICT(workspace_id)
DO NOTHING，只有同一 Workspace 的触发器先行初始化可以复用；tenant、主键、CHECK、FK
冲突不得忽略，不修改历史迁移或引入第二套状态事实。

第二轮：复用后必须仍为当前 workspace/tenant 的 active 状态；不能把 suspended、deleted
或 migration_conflict 改成 active。该检查与初始化共用创建事务，任何异常由原创建流程整体
回滚。单元覆盖首次写入、重复不改行、错误 owner/状态/唯一键与回滚；实际注册、验证、建
空间和 WorkspaceSession 冒烟验证完整接线，不能以 SQLite 冒烟替代原生证据。
