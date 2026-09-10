# PRD-106 外部等待专项记录

日期：2026-09-10。状态：运行时基础专项通过，PRD-106/113 与 S3 整批不因此视为完成。

## 范围

依据 Excel `01_PRD总表!A18:Y25`，只读核对，不修改计划完成状态。
联合设计及两轮编码前自审：`docs/prd/S3-external-wait-runtime-20260910.md`。
本次独立实现属于 PRD-106：原任务不变的持久等待、恢复租约、加密续接、队列并发、
取消与超时停止确认、原子效果/终态/outbox、平台安全 DTO 和前端状态。
新增 SQLite 0151 与 PostgreSQL 0158，两者均保留事实并拒绝有事实时 down。

## 通过证据

- `.artifacts/s3-external-runtime-20260910-r5.xml`：45 passed，0 failed/errors/skipped，19.260 秒。
  20 项新等待测试与 25 项既有执行期限测试。真实执行 SQLite 0098、0151 表、约束与触发器。
  覆盖 25 次 pending 后完成、跨一天恢复、同一原 job/attempt、未知异常安全化、取消早晚到达、
  截止后停止等待、真实线程竞争领取、崩溃代次、停服回收、队列共享上限、类型停用后继续观察、
  效果回调异常/过期回滚、外层 fence、加密信息不进入 DTO、空库 down 与有事实拒绝 down。
  提交前复核增加解密期间租约到期的用例：旧恢复者不调用 handler，新恢复者接管且仍只一个 attempt。
- `.artifacts/s3-external-api-20260910-r1.xml`：11 passed，0 failed/errors/skipped，249.966 秒。
  使用全新隔离 SQLite，执行当前全部迁移到 0151，保留平台会话、角色、CSRF、CAS、
  输入闭合、密文不外泄、重试/取消与 supervisor 既有 API 冒烟。
- `.artifacts/s3-external-postgresql-20260910-r4.xml`：8 passed，0 failed/errors/skipped。
  本轮新建的 PostgreSQL 17 已执行完整迁移到 0158；最终复跑在确认停止后重启同一隔离实例，
  重放迁移无新增操作。验证真实行锁并发唯一 owner、短租约到期接管、旧恢复者拒绝、取消与
  原 job 同事务完成、有事实拒绝 down，及原有自动心跳、效果回滚和 SKIP LOCKED 四项回归。
- `.artifacts/s3-external-frontend-20260910.xml`：6 passed。
  DTO 同时接受旧版无外部字段和新版安全摘要；拒绝未知字段、负计数、伪布尔和矛盾状态。
  渲染停止确认，不暴露令牌，不显示可重放外部终态的重试按钮，保留平台权限裁剪。
- 相关 Ruff 与 `git diff --check` 通过。

## 失败轮次与修正

首个 pytest 命令的 PowerShell 参数拼接未正确传入 basetemp，没有执行测试；修正命令后运行。
初轮 40 项通过；扩展后一次旧心跳 timeout 用例失败，单独复核又出现 cancelled 用例失败。
JUnit 显示失败用例仅耗时 0.147 秒，并未等待 4 秒：测试冻结数据库时钟，但 service 入队仍读
真实时间，任务偶尔被排到冻结时钟的未来。统一测试时钟并断言任务确实领取后，全部通过。
没有延长心跳期限或降低生产围栏要求。

首次 PostgreSQL 建库误用带事务的数据库包装器，服务器明确返回 CREATE DATABASE 不允许
在事务中执行；改为 createdb。第二次 Python312 缺少 pglast，改用已有依赖完整的项目 venv。
第三次迁移成功且 7 项通过，down 用例因读事务未关闭而提前被包装器拒绝，没有执行到目标保护。
最终改为独立原生连接验证 down 的明确异常，且用例自行建立所需事实，不依赖测试顺序；8 项通过。
失败记录保留，不合并为成功或跳过。

## 边界

无模型调用、无客户流量、无付费请求。测试推进时钟和短租约用于状态机验证，不是 72 小时证据。
SQLite API 和 PostgreSQL 验证均使用隔离库；当前试用 schema 0150、前端与 Control 进程不更新。
所有测试会话已结束，隔离 PostgreSQL 已停止。未清理历史受保护证据。

PRD-113 仍在调用同步 execute_capacity_run；必须接入本次协议，补齐提交响应丢失、cancel 先到、
领域取消/kill 状态、签名状态查询、结果确认及真实停止后才能发布。升级还需排空旧 capacity worker，
协调 API 与前端版本；目前不声称旧新 worker 可混跑、浏览器完整流程或 S3 综合门禁已通过。
