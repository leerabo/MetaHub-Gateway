# S3 容灾任务与恢复事实绑定

日期：2026-09-10。状态：两轮编码前自审完成，恢复点绑定实施单元已验证；durable restore 接入待继续。
承接 `S3-actual-restore-execution-20260910.md`，不改变 PRD-106..113 整批门禁或 Excel 的原始范围。

## 设计

1. 第二人审批时，在同一数据库事务内选取平台级、完整、与当前数据库 backend/schema 相符的
   已校验备份，持久化 recovery point，入队 `dr.run`，写入不可变 run/recovery/job 绑定，然后批准。
   绑定固定 plan manifest、源/目标区域、expected/fencing epoch、备份 manifest、内容/对象键摘要。
   后续阶段只能按 run 读取绑定；不得再次选取全局最新 recovery point。
2. 使用 schema_migrations 的实际 applied head，拒绝未知/迁移中状态，不再硬编码 0103。
   RPO 暂取备份请求 created_at 作为保守的快照时间下界，不能以上传/恢复完成时间重新计时。
   后续精确快照时间必须进入加密 manifest，不能凭空用调度时间声称精确水位。
3. 新增 SQLite 0153 / PostgreSQL 0161。绑定不可更新、删除；数据库校验 run、job、恢复点、
   当前 schema/backend、平台完整备份及 manifest 关系。既有 run 不回填猜测的恢复点。
   无绑定的历史活动 run 只能失败/终止，不能继续切换；有绑定事实则 down 拒绝删除。
4. PRD-106 durable external wait 承接真正恢复。后续 runner 每次执行分配独立隔离目标，
   continuation 绑定 job generation、run 和恢复点；停止必须由 runner 确认。
   恢复结果匹配不可变绑定后才能持久化 database/config/outbox/ledger 指纹，不能将这些指纹
   替代业务账本守恒、对象副本、KMS 或 Gateway ACK。切换前再次复核审批、驻留和 fencing。
5. 下一实施单元移除元数据成功路径，连接真实执行/结果证据。当前绑定本身不是恢复成功证明，
   也不证明完整 RPO/RTO、防脑裂、跨区复制和回切；不得把此项提交称作 PRD-111 完成。

## 第一轮自审：身份与数据

- 全局 latest 允许另一任务或工作空间备份覆盖当前恢复对象；选择限于平台 full，执行按 run 绑定。
- backup_sets 元数据可更新，不能仅保存 backup_id；每次读取核对 manifest、内容、对象键、KMS、
  schema、源水位和时间，不允许同 ID 换内容。摘要和绑定不包含连接串、密钥或客户原文。
- 旧恢复点用 completed_at 不适合作源时间；不修改不可变旧记录，拒绝不一致的历史记录。
- 备份完成很晚不意味着快照很新。created_at 保守，未来时刻和负年龄一律拒绝，不能截为零。
- 已批准任务若 schema 漂移，必须失败并重新走明确的新流程，不静默选另一备份。

## 第二轮自审：并发、迁移与生命周期

- 插入绑定与审批/入队同事务，任何失败全部回滚；绑定 PK 保证每个 run 只有一个恢复点。
- PostgreSQL 使用行锁串行化两人审批；数据库触发器阻断无绑定进入执行状态及替换 platform_job_id。
- down 只允许无新绑定的库，保留旧 runs/recovery points/evidence，不删除业务事实。
- 先做绑定专项 SQLite/PostgreSQL 合同与迁移测试；整批结束再做完整数据库、流程、浏览器、性能、
  安全和回归，不因本修改重复全套前端测试。测试使用合成数据，不读取试用库或仓库 `.env`。
- 不启动恢复到生产库，不重启 5173/8001，不迁移试用 schema 0150，不调用付费模型。

## 实施核对

绑定、双数据库约束、动态 schema/RPO 已实施。84 项自动化测试通过，另完成 SQLite 完整迁移链
的受保护 down/up 与幂等/完整性检查。独立测试与安全记录见 `PRD-111-recovery-binding-20260910.md`
及同名 sensitive-audit 文档。继续 durable restore 执行和真正证据采集；S3 与整个目标保持 active。
