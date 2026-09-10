# PRD-106 平台分布式任务队列、Scheduler 与 Worker 生产运行时

状态：设计冻结，编码未开始（2026-08-23）。本设计依据 SaaS 增量计划 Excel 的 PRD-106、核心数据对象、API/事件、
角色权限和验收行，并复核现有 PRD-057/074/099/100/105 scheduler 与 Control lifespan 装配。

## Excel 权威合同

以下名称与目标值按 Excel 原文冻结，内部 SQL 表名可以不同，但公开 DTO、测试映射与交付证据必须逐项对应：

- API：`GET /platform/job-queues`、`GET /platform/jobs/{id}`、`POST /platform/jobs/{id}/retry`、
  `POST /platform/jobs/{id}/cancel`、`POST /platform/job-types`。
- 核心数据对象：`PlatformJob`、`JobAttempt`、`WorkQueue`、`SchedulerLease`、`JobCheckpoint`、`DeadLetterJob`。
- 事件：`platform_job.started`、`platform_job.failed`、`platform_job.completed`、`platform_job.dead_lettered`。
- 性能/SLO：`入队p95<100ms`、`空闲队列启动p95<10s`、`崩溃任务接管<60s`。

## 1. 目标与边界

交付统一的持久任务运行时：优先级、平台/租户 owner、公平领取、lease/generation、checkpoint、业务效果幂等、
重试、取消、死信、背压、崩溃接管和预编译任务类型注册。数据库保存权威状态；Redis 只负责唤醒、近实时水位和
容量信号，故障时不得成为任务真值。

平台工作台只列平台任务。PRD-074 企业批处理保留 Workspace API、数据对象和授权，仅通过适配器复用领取、租约、
checkpoint 和执行结果协议；任何 Workspace 主体都不能访问 `/platform/job-*`。本条不实现 107..113 的业务逻辑，
也不允许从 API 上传代码、模块路径、命令、URL 或任意 payload。

## 2. 0098 数据与权限

- `platform_job_types`：预编译 `type_code`、handler contract version、owner kind、资源等级、最大 attempt、超时和状态；
  `POST /platform/job-types` 只能激活/停用已随制品签名发布的类型，不能注册可执行文本。
- `platform_jobs`：平台公开对象，含 type、priority、状态、revision、request/trace、幂等摘要、调度时间和终态摘要；
  输入参数使用 KMS envelope 密文，公开 DTO、日志、审计和事件永不返回密文或 payload 摘要原文。
- `platform_work_queues`：只保存平台队列、公平序列和优先级水位，不含 nullable tenant/workspace owner。
- `platform_job_attempts`：attempt number、worker、lease generation/token digest、开始/结束、固定 outcome/error code。
- `platform_scheduler_leases`：平台 queue/job、worker、generation、token digest、deadline 与 fenced 状态；旧 generation 不得续租或提交。
- `platform_job_checkpoints`：单 job 单 generation 的不可变 checkpoint 密文和 hash；新 checkpoint CAS 替换 head。
- `platform_dead_letter_jobs`：达到上限后的不可变终态事实；重试创建新 generation，不修改旧 attempt。
- `platform_job_effects`：`job_id + effect_key` 唯一，handler 必须先声明效果再提交，保证至少一次领取下的恰好一次业务效果。
- `JobAttempt`、`WorkQueue`、`SchedulerLease`、`JobCheckpoint`、`DeadLetterJob` 是共享运行时协议的逻辑对象，不是共享
  owner 表。平台实现使用上述 `platform_*` 表；PRD-074 企业批处理继续使用 tenant/workspace 复合 owner 的独立事实表与 API，
  通过 adapter 映射同一协议。0098 不创建混合 nullable-owner 表，也不迁移或复制企业任务事实。
- 权限扩展为 `platform.job.read/manage/type_manage`；owner 全部，platform_ops 全部，其他平台角色与全部 Workspace 角色拒绝。
- 迁移采用 expand-migrate-contract；down 发现 job/attempt/checkpoint/effect/dead-letter 或 active lease 时拒绝，不静默删事实。

## 3. 状态机与调度协议

Job：`queued -> leased -> running -> succeeded|failed|cancelled|dead_lettered`；可重试失败会回到 `queued` 并增加
generation，旧 attempt 保持终态。`cancel_requested` 是围栏状态，Worker 在每个 checkpoint 和效果提交前重验；取消
只允许收敛到 `cancelled`，不得留下“效果已提交但 job 仍可重试”的半终态。

Scheduler 使用数据库时间、短事务和 fenced lease。PostgreSQL 使用结构化 adapter 的 `SKIP LOCKED`，SQLite 仅作为
串行测试/单进程开发实现。领取按优先级带权、owner deficit 和全局序列排序；高优先任务不能无限饿死普通租户。
worker 不信任消息体 owner，而从 job/type/queue 复合关系恢复执行域。网络或 KMS 调用不持有数据库事务。

Control 启动一个 `PlatformJobRuntime` supervisor，逐步接管现有独立 scheduler；迁移期旧 scheduler 作为受注册 handler
运行，禁止同时保留两套领取者。关闭时先停止 claim，再等待有界 attempt/checkpoint，最后释放 lease。

## 4. API、审计与事件

- `GET /platform/job-queues`：只返回队列健康、深度、最老等待、lease 和 dead-letter 聚合，无 payload/tenant 业务正文。
- `GET /platform/jobs/{id}`：严格 ID、同形 404、attempt/checkpoint 元数据和固定失败码。
- `POST /platform/jobs/{id}/retry|cancel`：PlatformSession + CSRF + idempotency + revision CAS；动态撤权先于 body 解析。
- `POST /platform/job-types`：只切换已编译目录项，需 type_manage、CSRF、幂等与目录 revision。
- 事件 `platform_job.started|failed|completed|dead_lettered` 从同事务 source/outbox 恢复；只含 job/type/attempt、固定 code、
  request/trace 和时间，不含输入、checkpoint、输出或客户正文。
- 每个管理动作写不可变 PlatformAuditLog；worker 身份不能伪造用户 actor。

## 5. Console 与运行门禁

Platform 新增 `job-runtime` 页面：队列摘要、任务列表、attempt 时间线、取消/重试和任务类型状态。UI 只按后端导航和
实时 permission 渲染；离开页面、角色漂移或 PlatformSession 失效时先清 DOM 再 abort。三档视口为 390/1280/1440，
表格在移动端转标签行，不依赖页面级横向滚动。

专项覆盖状态机、同幂等重放、租约过期接管、checkpoint fencing、效果恰好一次、取消竞态、tenant 公平、死信、
Redis/KMS/DB 故障、停服恢复、PRD-074 适配隔离、迁移往返和敏感扫描。性能门禁：入队 p95<100ms、空闲队列启动
p95<10s、崩溃接管<60s。

## 6. 第一轮自审：执行真值与代码注入

- P0：允许 API 注册模块/命令会形成远程代码执行。修正为 API 只能激活签名制品内的预编译 `type_code`。
- P0：Redis 队列作为真值会在丢数据后丢任务。修正为数据库保存 job/lease/effect，Redis 仅唤醒和背压。
- P0：消息体携带 tenant 会让 Worker 被伪造 owner。修正为 owner 只从复合数据库关系恢复。
- P1：至少一次执行会重复外部效果。修正为 effect ledger、handler effect key 和 generation fencing 三重约束。
- P1：继续叠加 lifespan scheduler 会扩大连接和关闭竞态。修正为单 supervisor 与受注册 handler。

## 7. 第二轮自审：公平、取消与跨域

- P0：平台查询若复用 Workspace 队列表会枚举企业任务。修正为平台 API 只投影 `platform_*` 事实，Workspace 继续
  走 PRD-074 API；协议复用不等于数据/API/授权复用。
- P1：绝对优先级会饿死普通 owner。修正为 priority 权重加 deficit/fair-sequence，并设置最大连续高优先领取数。
- P1：取消与效果提交竞态会形成半终态。修正为 checkpoint/effect 提交前在同事务重验 cancel fence 和 generation。
- P1：down 删除内部 cursor 但保留 effect 会破坏审计。修正为任一业务或 active lease 事实存在即拒绝回退。
- P1：失败异常正文可能带凭据。修正为 handler 只返回固定 code 和安全摘要，日志禁止异常正文。

## 8. 批次交叉审查

- 第一轮（范围/owner/权限）：平台 API、平台事实与 PRD-074 Workspace 事实已物理分离；共享的是运行时协议，不共享工作台、
  API、授权或 nullable-owner 表。`platform.job.*` 每请求动态校验，Workspace 主体同形拒绝。
- 第二轮（迁移/依赖/失败策略）：0098 只 expand 平台事实，存在任何 job/attempt/lease/checkpoint/effect/dead-letter 即拒绝
  down；数据库是真值，Redis 仅唤醒，KMS/Redis 故障不会产生无围栏执行；106 只提供后续 PRD 的运行时，不反向依赖 107..113。

## 9. 2026-09-10 执行修订

编码前批次设计与两轮自审见 `S3-execution-evidence-correction-20260910.md`。
补齐独立连接自动心跳、从 attempt 领取起算的目录超时、取消终态和迟到写入回滚。
PostgreSQL 使用实时数据库时钟与行锁，领取采用 SKIP LOCKED，获得队列锁后重新读取并发计数；
租约比较保留毫秒精度，外层 registry/lease 围栏在事务内受保护。业务回调异常必须显式回滚，
不能把未完成事务留给复用连接上的失败处理。成功终态提交前再次检查租约和 attempt 时限。

该修订不声称可以强杀任意进程内 Python handler。外部效果仍须通过独立可停止执行器和围栏控制。
专项记录见 `docs/testing/PRD-106-execution-revision-20260910.md`；S3 整批退出门禁尚未完成。

## 10. 外部执行的持久等待

编码前联合设计及两轮自审：`S3-external-wait-runtime-20260910.md`。
SQLite 0151 / PostgreSQL 0158 新增等待事实和独立恢复租约。原 job、attempt、generation 保持不变，
普通租约在领域 start 与续接登记的同一事务中释放。续接参数沿用 KMS 加密，并使用独立 AAD 域。
每次观察使用 45 秒恢复租约，失败退避上限 30 秒；等待和恢复不消耗普通失败重试。
等待不占普通并发槽，短时观察与普通领取共同受队列并发约束；停服和过期代次可接管。

外部操作截止时间可跨越 72 小时，不受单次 attempt 的 24 小时限制；截止只触发停止意图，
不能据此认定远端停止。取消保持 cancel_requested，直到编译期 handler 给出终态停止确认。
领域效果、原 attempt/job、外部等待和 outbox 在同一事务中完成。结果迟到、外层 fence 失效、
租约到期或取消竞争均不能留下成功效果。存在外部等待的失败任务不能通过普通 retry 重放。

平台详情新增安全等待摘要，队列区分普通执行槽和外部等待数量。前端允许旧版响应缺少新字段，
新字段存在时严格验证，拒绝恢复令牌和密文等扩展。平台角色授权和企业任务边界不变。
本阶段未实现 PRD-113 的领域适配、真实执行停止及混跑升级；不得据此启用真实容量任务。
专项记录与独立敏感审计见 `docs/testing/PRD-106-external-wait-20260910.md` 和同名前缀审计文件。
