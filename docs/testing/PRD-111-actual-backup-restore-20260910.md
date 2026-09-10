# PRD-111 实际备份恢复专项

日期：2026-09-10。状态：共享备份恢复执行层专项通过，PRD-111 和 S3 整体验收未完成。
设计及两轮编码前审查：`docs/prd/S3-actual-restore-execution-20260910.md`。

## 已实现

- 新备份 v2 manifest 绑定关键业务表的结构摘要、全量行数和逐行内容摘要。
  范围为 schema_migrations、Workspace/tenant 归属、ledger 表、全部 Outbox 表和明确配置表。
  按主键每批 256 行读取，支持复合主键；不把前 100 条归属样本当作完整性证明。
- SQLite 从真实 online backup 副本采集指纹，恢复到独立临时文件后重算，执行 integrity/FK 检查。
- PostgreSQL 元数据、指纹和 pg_dump 共用导出的 REPEATABLE READ 快照；导出事务在 dump 完成前保持。
- 修复 PostgreSQL 命令行连接配置以及 pg_restore 缺少 --dbname 导致不执行数据库恢复的问题。
  连接凭据只传入子进程环境，不放入参数；拒绝 libpq 身份覆盖与连接串式数据库名。
- PostgreSQL 只恢复到空隔离目标，独占锁覆盖恢复和验证，不使用 --clean。
  --single-transaction / --exit-on-error 保留原子性，失败释放锁，空目标可重新尝试。
- v2 manifest 保存源库实际身份摘要，新恢复无需源库在线；v1 可读但不返回 v2 数据指纹。
- PostgreSQL 0160 为 47 个既有兼容函数固定解析路径，修复空 search_path 下的实际导入失败。
  保存原设置，down 保留其他配置，存在备份事实则拒绝回退。不修改历史迁移。

## 最终证据

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 共享恢复、SQLite、PRD-100 受影响回归 | 38 通过，0 失败/错误/跳过 | `.artifacts/s3-backup-focused-20260910-r8.xml` |
| 原生 PostgreSQL | 9 通过，0 失败/错误/跳过 | `.artifacts/s3-backup-native-20260910-r7.xml` |
| 四个受影响 Python 文件 Ruff | 通过 | 本轮命令输出 |

原生用例真实运行本地 PostgreSQL 17、pg_dump 和 pg_restore：快照导出后的并发写入不进入备份，
非空目标保持原记录、源库别名拒绝、并发恢复独占、坏 dump 后重试、恢复后同量数据篡改拒绝。
完整现行 schema 0160 从空库迁移、写入合成种子、实际备份和恢复；0160 两次 down/up 验证原多项
search_path 设置恢复，备份事实阻止 down。关闭源库连接准入后仍可用 v2 备份恢复。
所有原生集群、dump/restore 子进程最终退出；未重启 5173 / 8001，未迁移试用 schema 0150。

## 失败轮次及修正

- r1：系统 pytest 临时目录权限错误；改用新建的工作区隔离测试目录，未删除旧证据。
- r2：新测试 fixture 的对象服务配置缺项；补齐合成配置，不读取仓库 .env。
- r3：真实 CLI 连接/执行缺口，6 个原生用例失败；修复环境参数和 --dbname 后推进。
- r4：localhost 别名经连接池超时报安全失败；补充规范化的本地地址、默认端口及编码数据库名保护。
- r5：完整恢复暴露兼容函数解析失败；新增 0160 修复，不禁用 CHECK 约束。
- r6：默认 PostgreSQL 锁容量不足；隔离恢复集群配置 max_locks_per_transaction=1024，
  保留单事务恢复。这一配置是现行完整 schema 的部署前提，不表示已验证所有规模。
- 最终只统计 r7 的 9 个原生用例和 r8 的 38 个共享用例，不累计重复轮次。

## 未完成范围

当前只证明关键表与备份时点一致，不能替代业务账本守恒核对或完整 RPO/RTO 验收。
现有 PRD-111 handler 仍需接入真实恢复及 PRD-106 持久等待，固定 run/recovery/manifest/epoch，
移除基于阶段名称的伪证据，并为每次执行分配独立目标。当前接口不会清空已恢复的目标，定时备份
不得直接重复使用它。外部任务取消/接管、业务对象跨区副本、真实 KMS 故障、DNS/Gateway ACK、
writer fencing、回切、Linux、多租户负载、72 小时稳定性及整批浏览器/安全门禁仍未完成。
对象服务本专项使用内存替身，KMS 使用本地 test provider；不声称 AWS/S3 跨区验证通过。
