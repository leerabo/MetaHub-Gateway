# PRD-131 旧租户、直连与私有部署迁移切换向导

状态：已完成。依赖 PRD-090..130、PRD-040/062/089；范围以 Excel PRD-131 为准。

## Excel 权威合同

- API：`POST /migrations/assessments`、`POST /migrations/runs`、
  `GET /migrations/{id}/differences`、`POST /migrations/{id}/cutover`、
  `POST /migrations/{id}/rollback`。
- 对象：`MigrationAssessment`、`MigrationPlan`、`MigrationRun`、`MigrationDifference`、
  `CutoverCheckpoint`、`RollbackPlan`。
- 事件：`migration.started`、`migration.validation_failed`、`migration.cutover_completed`、
  `migration.rolled_back`。
- SLO：10 万配置对象评估 < 30 分钟；计划停机窗口可量化；回滚 < 2 小时。

## 1. 产品闭环

Workspace owner 选择旧 default tenant、直连配置或私有部署来源，系统先生成只读 assessment：账号绑定、
Workspace/Membership、Provider/Key reference、策略、账本序列、审计水位和 EdgeDeployment 的固定计数与 hash。
owner 审阅差异和凭据重录清单后生成版本化 plan；run 依次执行 dry-run、目标回填、双读验证、冻结、切换和
观察。任一 gate 失败停在持久 checkpoint，可修复后继续或执行显式 rollback。

迁移永不把 Provider Key、Token、Prompt、Response 或客户正文写入差异报告。凭据优先在目标安全域重新录入；
只有源/目标均支持同用途 envelope 且 owner/AAD/version 全部匹配时才允许重包裹迁移。Platform Support 只能在
客户批准的 SupportGrant 内协助，不能扩大扫描范围、代替 cutover 或改变 Membership。

## 2. 0123 数据、状态机与切换

- assessment/plan/run 均带 `workspace_id/tenant_id` 复合 owner、source kind、manifest version、revision、
  generation、状态和幂等 hash；来源 selector 只能引用固定适配器。
- difference 只保存 domain、fixed code、source/target count、hash、sequence/watermark 和 remediation code；
  不保存原始行、Key name、endpoint、正文或任意 JSON。
- run 状态固定为 `planned|assessing|backfilling|dual_read|frozen|cutting_over|observing|completed|failed|rolling_back|rolled_back`；
  合法转换由数据库和服务层同时约束，迟到 worker 不得覆盖新 generation。
- checkpoint 保存阶段输入/输出 digest、source/target high-watermark、freeze generation、worker lease 和 receipt；
  rollback plan 保存补偿顺序、最后安全 checkpoint 和目标验证集。
- 事件与业务状态同事务写 source/outbox；重复命令按 owner + idempotency key + request hash 返回原结果。

Down 在任一 run、difference、checkpoint、rollback、迁移事件或未投递 outbox 存在时拒绝，不能通过 schema
回滚删除客户切换证据。功能回退使用兼容读路径和 feature flag。

## 3. 一致性、权限与页面

迁移适配器由代码注册表声明允许的来源表、公开字段、owner join、计数、hash 和 sequence；禁止请求传 SQL、
表名、列名或路径。dry-run 在只读事务/快照中执行且验证前后写水位不变。cutover 前必须同时满足行数/hash/FK、
账本序列、审计水位、身份角色不扩大、Provider reference 完整、Edge generation 单调和所有 required difference
已关闭。冻结后新写通过 dual-write receipt 或明确拒绝，不允许静默丢失。

Workspace owner/admin 拥有 `migration.read/manage`；developer 只读公开差异和迁移指南。跨 Workspace 同形 404；
所有写使用 step-up、If-Match、Idempotency-Key、no-store 和动态权限恢复。Console 使用向导式阶段列表、差异表、
凭据重录任务、停机窗口、检查点、恢复动作和不可变操作记录，不展示原始配置或内部表结构。

## 4. 第一轮自审：扫描与凭据边界

- P0：用平台数据库权限遍历旧 tenant 会形成跨租户扫描旁路。修正为 owner-qualified source manifest、固定
  适配器和客户授权；JIT SupportGrant 也不能改变 manifest。
- P0：差异报告保存原始配置会泄漏 Provider Key/endpoint/策略正文。修正为 exact code/count/hash/watermark。
- P0：把旧角色直接复制会把 default tenant 管理员提升为 Workspace owner。修正为显式账号绑定与目标角色
  mapping，owner 变更需要独立批准且迁移前后权限集合不得扩大。
- P1：凭据 envelope 无用途/owner 绑定会跨域解密。修正为默认目标重录，允许迁移时重建 AAD 并验证用途版本。

## 5. 第二轮自审：冻结、回退与重启

- P0：cutover 后简单切回旧 head 会丢失新写。修正为冻结 generation、dual-write receipt、持久 checkpoint、
  有序补偿和回退后全量一致性复验。
- P0：进程重启重复 backfill 会重复账本/事件。修正为稳定 source identity、幂等 target key、阶段 receipt 和
  worker lease fencing。
- P1：迟到 assessment 覆盖新差异会错误放行。修正为 manifest/run generation 绑定，旧 generation 只读保留。
- P1：回退成功但 outbox 未恢复会丢审计。修正为 rollback 事实与事件同事务提交，投递按持久水位恢复。

两轮均为修正后通过，无未关闭 P0/P1 设计问题。

## 6. 专项验证计划与批次边界

0122/0123 fresh/往返/down guard；10 万对象只读 assessment SLO；跨 tenant 扫描、角色扩大和敏感差异拒绝；
dry-run 零写；backfill/dual-read/freeze/cutover/observe/rollback 主异常流；worker 崩溃、租约接管、迟到结果、
账本/审计/Edge 水位；Workspace/SupportGrant 动态撤权；真实 API/三浏览器/三缩放；代理/断网、Windows/Linux、
SQLite/PostgreSQL、N/N-1；敏感审计与静止终态。开发期只跑本条受影响测试，完整 G6 留到 PRD-138 后。

实现已交付 0123、Workspace/Support JIT API、严格 DTO、固定来源适配器、持久切换/回退事实和 Migration
Center；聚焦迁移/API、Console、必要导航权限回归和静态检查通过。真实数据库矩阵、FLOW、浏览器、性能、
安全、兼容性和 72 小时长稳仍按批次规则留到 PRD-138 后统一执行，不能由本条专项结果替代。

## 7. 2026-09-07 数据库兼容补充设计

与 PRD-132/133 合批修复原生数据库入口，不等待长稳观测。复用共享 schema helper，保留固定来源
表/列允许清单、复合 owner 和 assessment 零写水位检查；旧权限 helper 同步使用后端感知表检查。
第一轮自审：列枚举不能接受请求表名，缺少必需列仍失败关闭，不能把跨租户全表扫描当作迁移评估。
第二轮自审：schema 查询不提交事务、不改变水位；用真实 PostgreSQL 克隆运行原有 assessment、
cutover/rollback、权限漂移与 Support JIT 合同。fixture 核验所需迁移而不固定旧 head。
该批合同结果不代替实际迁移演练、全产品并发验证或长稳验收。

实施复核发现旧 SQL 使用保留字 `user` 作为别名，改为 `legacy_user`。PostgreSQL 没有
SQLite `total_changes`，评估水位改用 `pg_stat_xact_user_tables` 的当前事务写入统计；它包含
触发器写入且不混入其他连接的提交。不在共享连接适配器伪造计数。增加故意写入的 adapter
测试，要求评估拒绝、源行恢复、assessment/action 均不落库。

原生测试进一步定位到主会话 `_switch_target` 使用的另一个 SQLite-only 表检查，已纳入共享入口
修复与 Workspace 回归；这与已支持 PostgreSQL 的扩展权限目录不是同一函数，不能混淆结论。

真实初始化探针还发现 PostgreSQL seed 的新空间保留了中间态 `migration_conflict`，而全部关联
数据已 active。仅当本次 seed 新建 tenant、初始状态 revision/risk_revision 均为 1、来源为
workspace_creation、没有暂停信息且所有父对象 active 时完成初始化收尾。已有 tenant 不走此
修复分支，重复 seed 不能恢复其冲突或暂停状态；用原生重复 seed 负例保持该边界。

原生增量 `0140_workspace_operational_identity_guard` 修复已验证的 PostgreSQL `ctid` 误用。
第一轮迁移自审：SQLite rowid 是稳定行标识，PostgreSQL ctid 是物理元组位置，不能比较 NEW/OLD
作为不可变业务标识。仅替换 operational-state 的触发器函数，保留 ID/复合 owner/创建时间不变、
revision/risk_revision 互斥单步和时间单调条件，不改 0139 基线或其他触发器。
第二轮迁移自审：原生 up/down 都保留数据；down 恢复旧函数也会恢复其合法更新受阻缺陷，不能
作为正常运营回退。验证 up/down/up、幂等、事实未改写和七类非法更新拒绝；旧基线新装与已装实例
均通过正式迁移器应用 0140，不能只在测试中热补函数。

原生业务复验已越过初始化，随后定位到共享后台 RBAC 的 `sqlite_master` 查询。该路径由
Workspace 权限快照直接调用，改为复用 `table_exists`，保留完整目录、矩阵摘要和授权上限校验。
第一轮自审：五张必需目录表逐一缺失时仍拒绝授权，不能回退到静态管理员权限。
第二轮自审：表检查不提交调用方事务，测试通过重命名并回滚逐一验证缺表边界。seed 的新建标志
同时改为 INSERT 的实际 rowcount，避免预查询与并发插入之间的竞态误将已有租户视为新建。

七域真实投影测试发现账本适配器误用不存在的 `ledger_entry_id`。按现有账本 schema 更正为
`finops_ledger_entry_id` 与 `ledger_sequence`，保持固定字段允许清单及 tenant 过滤。两轮复核分别
检查字段归属、摘要无正文，以及非连续账本序列、跨租户排除和零写；迁移主流程测试扩为全部七域。
