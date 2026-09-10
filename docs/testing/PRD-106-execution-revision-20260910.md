# PRD-106 执行修订专项记录

日期：2026-09-10。状态：本次执行基础修订专项通过；不是 S3 整批完成声明。

## 要求来源与设计

只读核对原始增量计划 `01_PRD总表!A18:Y18` 与 `06_测试与验收计划!A18:L18`。
未编辑 Excel，历史完成状态未作为验收证据。
独立 PRD：`docs/prd/PRD-106-platform-distributed-job-runtime.md`。
编码前批次设计和两轮自审：`docs/prd/S3-execution-evidence-correction-20260910.md`。

## 实现范围

- 自动心跳使用独立线程和独立连接，每秒检查；数据库操作预算为 2 秒，SQLite 写锁等待为 1 秒。
- attempt 从数据库 leased_at 起算，不因解密、准备、续租或 handler 调用重新计时。
- 心跳、checkpoint、effect 和成功完成检查目录超时、租约、generation 与外层 scheduler fence。
- 取消由心跳提交取消终态；超时停止续租并记录 platform_job.timeout，不继续自动重试。
- 回调异常或迟到提交回滚业务变更；取消后的 checkpoint/effect/完成不能复活任务。
- PostgreSQL 使用 clock_timestamp、行锁和 SKIP LOCKED；获得队列锁后用新语句重验并发容量。
- 毫秒精度租约比较不会在到期前最后一秒提前接管，更新 revision 的时间戳保持严格变化。

## 测试证据

`control-plane-api/tests/unit/test_platform_job_execution_deadline.py` 25 项通过。
使用未改写的 0098 SQL 及其约束/触发器，前置 owner 表为隔离的最小测试结构。
自动心跳与线程退出实际运行；超时边界通过注入数据库时钟推进，不冒充实际等待 30 秒或长稳。
覆盖独立连接、锁等待预算、迟到回调回滚、准备超时、取消、过期接管、旧 generation、
外层 registry/generation/expiry、毫秒边界、完整性与外键检查。

PRD-147 scheduler registry 37 项相关回归通过。
上述 62 项及当时容量结果 46 项共同记录于：
`.artifacts/s3-execution-foundation-20260910-r5.xml`，108 passed，零失败/跳过。

`control-plane-api/tests/integration/test_platform_job_postgresql_execution.py` 4 项通过：
`.artifacts/s3-job-postgresql-20260910-r1.xml`。
使用独立 PostgreSQL 17 实例与仓库 0139 PostgreSQL 基线，实际验证自动续租/checkpoint/幂等效果、
并发领取唯一 owner、回调期间真实 SQL 等待导致租约失效并回滚、SKIP LOCKED 与有界心跳等待。
结束时 4 个任务全部 succeeded，active lease 为 0。未使用、重置或迁移试用数据库。

首轮缺少 pytest 临时父目录，随后发现测试时钟精度和未生效的测试配置，修正后重跑。
状态更新时间比较同时修复了真实 PostgreSQL 亚毫秒时钟下的更新冲突风险。
异常断言使用 ApiError.code，不依赖英文消息。失败轮次保留，未改成成功记录。
相关 Ruff 与 git diff --check 通过。

## 敏感审计

仅使用随机临时测试 secret 与合成任务标识；没有用户账号、Provider KEY、邮件正文或模型请求。
测试不把配置值、输入 envelope、checkpoint 正文或令牌写入结果证据。
异常只记录固定 outcome；回调内合成敏感异常消息不会成为任务事件或终态描述。
修改不涉及平台/企业角色边界、数据库迁移或试用资源文件，未提交 .tmp/.artifacts 中的运行数据。

## 剩余门禁

本次并非全量数据库、Linux、浏览器和性能验收；S3 仍需跨域集成及完整批次验收。
无法安全强杀任意进程内 Python handler，只撤销超时后的数据库提交资格并回收心跳线程。
真实外部执行的停止和恢复证明由隔离执行器提供，不能拿这组测试代替容量或 72 小时证据。
