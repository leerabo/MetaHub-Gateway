# PRD-111 恢复点绑定专项

日期：2026-09-10。状态：绑定实施单元专项通过，不是 PRD-111 完成记录。
设计及两轮编码前自审：`docs/prd/S3-dr-bound-execution-20260910.md`。

## 实现范围

- 第二人审批事务固定 run/recovery/job、plan、区域、epoch 和备份摘要；所有后续阶段读取固定绑定。
- 平台容灾选择平台完整备份，要求当前 backend/schema 和同 manifest/schema 的备份校验。
- RPO 以 backup.created_at 保守计时，拒绝未来时间、过期、schema 漂移、元数据替换。
- SQLite 0153 / PostgreSQL 0161 的不可变绑定、任务关联和开始时间约束；有绑定事实禁止 down。
- Worker 每次阶段和失败回写都验证 run 属于当前 job；路由前重新校验审批。

## 测试分层

`shared-sdk/python/tests/test_prd111_dr_recovery_binding.py` 的参数化合同使用真实 SQLite 和 PostgreSQL，
但只建最小依赖表。该层不代表完整 API、全部旧触发器、真实恢复或多区域验收。
原生合同夹具的日期函数也是夹具辅助，完整原生迁移/恢复测试单独使用实际兼容函数。

覆盖：后续出现新备份不替换原选择、工作空间/增量/错误 backend/schema 拒绝、RPO 边界、
元数据八类漂移、错误 job 所有者、缺失绑定、直接伪造绑定、开始时间不可修改、事务整体回滚、
迁移 down/up 与事实保留。另执行完整 SQLite 迁移链，以及完整 PostgreSQL 新 schema 的真实恢复。

## 执行中发现

- SQLite 初轮测试夹具直接读取不存在的 backend 属性，已按仓库 `getattr(..., 'sqlite')` 修正。
- ApiError 文本不是错误码，改为明确断言 `.code`，不依赖错误文案。
- PostgreSQL 触发器通过共享连接适配器转为脱敏 DatabaseError，测试按实际统一异常合同断言。
- 第一轮原生运行 19 项通过后，新 backend 启动/连接超时，正常停止也超时。
  `.artifacts/s3-dr-binding-native-20260910-r1.xml` 记录 2 个错误，不能计为通过。
  已核对 test postmaster PID/专属 data 目录，结束仅该树进程，保留失败证据和数据目录。
- 原生合同改为一个本轮创建的专属数据库/连接，各用例重置其合成 schema，避免为每个断言
  反复创建源/目标数据库和连接池。该夹具不读取试用数据库。
- 完整 SQLite 测试发现新 down 缺少正式执行器的受信版本保护头。已补齐，保留原失败 XML，
  使用同一个已迁移到 0153 的隔离库继续正式 expected_version 回滚和重放，不重新从零建库。

## 最终结果

- `.artifacts/s3-dr-binding-final-20260910-r3.xml`：80 passed，0 failures/errors/skips。
  79 条双数据库合同/时间边界，另 1 条完整 PostgreSQL schema 0161 迁移往返与实际备份恢复。
  9 条未选中用例不计入完成。历次失败结果不覆盖、不改写。
- `.artifacts/s3-dr-approval-20260910-r1.xml`：4 passed，0 failures/errors/skips。
  完整 schema 的审批/任务/绑定/审计事务、幂等重放、绑定失败全回滚、当前 schema 状态读取、
  延迟执行拒绝过期恢复点。仅认证主体为合成注入，不声称真实登录/HTTP 全流程验收。
- 完整 SQLite 0153：在
  `.tmp/s3-dr-binding-final-20260910-r2/test_full_sqlite_binding_migra0/full-schema.db`
  继续调用 `rollback_last_migration(expected_version='0153')`，再 apply 与幂等重放；返回成功，
  foreign_key_check 空、integrity_check 为 ok。未触碰试用库。
- Ruff 全部通过。最终验收集群没有 postmaster.pid，正常关闭。
- 首轮故障 PID 2196/11720 的 Windows GetExitCodeProcess 均为 4294967295（非 STILL_ACTIVE），
  已验证终止；Windows 仍枚举进程对象及 26474 的监听记录，属于未释放 OS 资源，不报告端口已清空。
  不重启系统，不复用 26474，保留首轮现场。其余工作不依赖该端口。
- 5173 / 8001 仍由原 PID 11248 / 17220 提供服务，未发布本轮 S3 改动。

## 未完成的验收边界

本项没有连接 durable external restore runner，也没有移除全部 metadata-only 成功路径。
现存 `validate_restore`/`verify`/`complete` 仍须改为真实执行结果，禁止把本项提交或绑定表当成恢复证明。
跨区对象、KMS、Gateway ACK、防脑裂、受控回切、Linux、百租户/2 倍峰值、72 小时长稳与 S3 整批门禁未完成。
不重启已发布试用，不迁移试用 schema 0150，不调用付费 Provider，不改动 Excel 的计划状态。
