# PRD-131 旧租户、直连与私有部署迁移切换向导

状态：设计冻结，尚未实现。依赖 PRD-090..130、PRD-040/062/089；范围以 Excel PRD-131 为准。

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
