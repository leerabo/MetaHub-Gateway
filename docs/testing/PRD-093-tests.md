# PRD-093 测试记录

状态：已完成。Excel、README、`docs/prd`、迁移和本记录共同确认下一未完成项为
`PRD-094 - 成员关系、空间切换与邀请升级`。本条交付个人/企业 Workspace 创建、不可变
Workspace-Tenant binding、owner foundation 和安全摘要；不开放一般 Membership 管理，不签发
WorkspaceSession，也不授予平台角色。`saas_incremental_roadmap_complete=false`，
`all_108_prds_complete=false`。

## 已执行门禁

| 测试组 | 实际结果 | 覆盖 |
| --- | --- | --- |
| WB/SEC/Config | `test_prd093_workspace_config.py`: `12 passed` | 境内 Region 默认值/规范化/去重、1..8 上限、非 `cn-*`/大小写/字符/长度失败关闭、驻留策略版本边界和运行时可见性 |
| DB/GR/SEC/FLOW | `test_prd093_db_migrations.py`: `3 passed` | 空库/0084->0085、确定性 Tenant 回填、owner 选举与无 admin 冲突、binding/owner/environment origin 完整性、pristine down/up 和安全事实原子阻断 |
| API/BB/GR/SEC/FLOW/PERF | `test_prd093_workspaces.py`: `5 passed` | personal/organization、严格 DTO/Origin/CSRF/Bearer、幂等重放/异 body、slug 同形冲突、Region 失败关闭、跨账号 404、profile CAS、多 Workspace 密码投影、100 并发单例和 warm p95 |
| WEB/UI/UCD/COMP | Console Node 全量 `975 passed`；lint `307 files`；真实 Browser 通过 | 创建表单/列表、草稿与幂等 key 生命周期、Abort/迟到响应、Storage 零调用、严格响应 DTO、键盘/读屏、390/1280/1440 无横向溢出且 Console 零错误 |
| COMP/FLOW | PRD-091/092 身份与迁移联合回归、旧 Auth/Tenant/Environment 定向回归均通过 | 历史迁移版本夹具分别冻结至 0083/0084；GlobalSession、自助注册、旧 Bearer、Tenant onboarding/current 和 Environment 行为保持兼容 |
| Static | 定向 Ruff、`py_compile`、JS syntax、`git diff --check` | Workspace 后端/共享配置/错误合同、迁移、Identity 前端和测试语法/格式；仅保留既有 `.env.example` CRLF 提示 |
| Planning | generator、roadmap validator、planning pytest | 连续完成前缀推进至 093；0085 与 Workspace 安全合同归属明确；下一项 094；两个总体状态保持 false |

## 迁移与数据验收

- 0085 建立 `workspaces`、`workspace_tenant_bindings`、`workspace_memberships`、Region/profile/
  provisioning/environment origin、event/outbox、幂等记录和一次性 `workspace_user_projection_claims`。
- 每个旧 Tenant 确定性投影为一个 organization Workspace；最早 active admin 成为唯一 owner foundation。
  没有 active admin 或 dev Environment 的 Tenant 进入 `migration_conflict`，不提升低权限用户，也不伪造环境。
- 新 Workspace 在一个事务中创建 Tenant、完整 legacy RBAC、绑定当前账号的 legacy admin User、owner、
  default dev Environment 和 Workspace 事实；复核 Provider/Key/Model/VirtualKey/Project 数量没有假增量。
- pristine 0085 可回到 0084 并确定性重上；profile、新 Workspace、额外 Membership 或事件等新增事实存在时，
  down guard 原子拒绝。终态 `PRAGMA integrity_check=ok`、`PRAGMA foreign_key_check` 违规 0。

## 安全与隐私结论

- 创建请求从 GlobalSession 恢复 account，只接受已验证账号；客户端不能提交 `tenant_id`、`account_id`、
  owner、role 或内部状态。旧 Bearer 与 Global Cookie 不互相回退。
- 写请求要求精确 Origin、CSRF、单值 Header 和严格 JSON；`Idempotency-Key` 只保存分域 HMAC并绑定规范
  body。相同 key/相同 body 重放安全，相同 key/不同 body 固定冲突且不改变任何 Workspace 事实。
- Workspace-Tenant binding 通过唯一约束和不可变 trigger 防重绑；所有双 owner 表使用复合外键证明
  workspace/tenant 一致。跨账号详情与不存在资源同形 404。
- API、事件和前端 DTO 使用字段白名单，不返回 tenant/account/legacy user/role、provisioning generation、
  credential/session revision 或平台状态；本条不签发 WorkspaceSession。

## 浏览器与性能

- 真实浏览器完成注册、验证、organization Workspace 创建和刷新后持久列表复验。`390x844`、
  `1280x800`、`1440x900` 均无页面横向溢出、元素重叠或 Console warning/error。
- 刷新后恰有一个 Workspace；DOM 未出现 `wsp_`、account、tenant、session、credential、CSRF 或 token
  标识，密码输入均为空。移动端全页截图的拼接重复不代表 DOM 重复；实际 sessions/password section
  均只有一个。
- 12 个 warm organization Workspace 创建样本的 p95 满足 `<1s`；核心事务包含 Tenant、RBAC、User、
  owner、Environment 和 event，不把后台派生工作伪装为授权完成。本流程不产生模型 Usage 或成本账，标记 N/A。

## 敏感审计

- 隔离验收库同时扫描 SQLite 主文件、WAL、SHM 和日志：注册密码、轮换密码、验证码、GlobalSession
  token、CSRF token 和原始 idempotency key 逐字节命中均为 0。
- 企业名称只存在于权威 `tenants` 和 `organization_profiles`；在日志、`workspace_events` payload、
  outbox 和幂等记录中均为 0 命中。事件 payload 字段集合严格等于固定白名单。
- 密码轮换后的旧/新明文均未进入数据库工件；浏览器 Storage 调用为 0，草稿和幂等 key 只存在于
  当前内存生命周期，页面刷新后由安全摘要重新加载。

## 2026-09-08 原生运营状态初始化修复

与 Identity 原生能力探测同批完成两轮自审和编码。创建事务中的表探测复用共享 schema
helper；运营状态写入改用 ON CONFLICT(workspace_id) DO NOTHING。触发器先行创建的同属
active 状态保持原行，不重置既有状态；错误 tenant、其他唯一约束、NOT NULL/CHECK/FK 和
非 active 状态失败关闭，原创建事务负责整体回滚。未修改历史迁移、DTO 或授权角色。

独立原始报告位于 `.artifacts/g6-security-20260907/`：

- `prd093-schema-capabilities-r1-junit.xml`：1/1，1.512 秒，真实表探测及事务保留。
- `prd093-operational-r2-junit.xml`：8/8，2.695 秒；首次写入、同 owner 重复不改行、回滚、
  历史缺表、错误 owner/状态、tenant 唯一键、主键及 NOT NULL 拒绝。
- `prd093-operational-api-r3-junit.xml`：1/1，210.468 秒，含空库迁移；真实注册、验证、
  organization 创建、幂等重放/异体拒绝、列表/详情、完整 RBAC/Owner/Environment 和零假资源。
- 原生 r5（e83e553a7f2f4f689f5a9fc19083d415）保留 INSERT OR IGNORE / 42601 反例；
  r7（91dcfa5a0c6d43bca31b59b0069028c5）实际创建返回 201，随后空间切换返回 200，
  后续 Project 聚合错误属于独立 PRD-029，未被计为 Workspace/整组原生通过。

受影响源和单元 Ruff 通过；`093-operational-initialization` 独立审计 profile 核对四个精确
暂存文件、原报告、UTF-8、凭据模式及基线摘要，输出
`prd-093-operational-initialization-sensitive-audit.json`。不宣称新一轮 100 并发、p95、浏览器
或完整 PostgreSQL 隔离验收已完成；这些仍归统一批次 QA。模式扫描不是安全会签。
