# SQLite 到 PostgreSQL 的离线迁移

状态：2026-10-15 Windows 原生 PostgreSQL 的数据副本校验已通过，1,098 张业务表、72,372 行数据的行数和内容摘要一致，外键检查通过，重新执行 191 项迁移全部跳过。源结构的 4,505 项对象逐项一致，0186 的历史校验和差异保留在报告中。2026-10-18，trial.12 冻结后端镜像在实际 Ubuntu 的新隔离 PostgreSQL 数据库中核对通过 1,098 张表、72,372 行、全部外键及 4,505 项源结构对象；再次执行目标迁移 194 项全部跳过，原副本摘要不变。实际解密、登录和业务操作及整套冷恢复仍待验收；以下是迁移步骤和拒绝条件，正式切换前必须核对最终验收报告。

准备新的 PostgreSQL 数据库并执行当前版本的原生迁移。迁移期间暂停源库的写入服务，用 SQLite backup API 获取一致副本；原数据库和 WAL 保留。导入器读取原库，不执行原库升级，不覆盖已有账号、组织、KEY 或调用记录的目标库。

保留原服务的加密根；如果首次生产配置生成了新根，先使用 `rewrap-migration-secrets.py` 在副本上重封装，验证明文摘要不变。根不能从数据库密文推导。无法解密的记录须列入报告并解决，不能用重置密码、固定验证码或清空密钥替代恢复。

通过目标容器的私有 PostgreSQL Unix socket，以离线管理员角色导入。专用连接文件只包含目标连接串，属主和权限必须与 import-sqlite 工具容器匹配；不得放进 GitHub 包或命令输出。Compose 的 `/migration` 挂载对应 `STATE/migration`。

```bash
sudo docker compose --env-file STATE/compose.env \
  -f RELEASE/infra/production/compose.yml run --rm import-sqlite \
  --source /migration/source.sqlite3 \
  --snapshot /tmp/source-copy.sqlite3 \
  --target-secret-file /migration/target.env \
  --report /migration/import-report.json
```

默认路径在副本上执行未应用的 SQLite 迁移，并拒绝已应用 SQL 的校验和变化。若历史校验和不同，不能编辑源库迁移记录“修好”这个错误。显式加 `--verify-current-schema` 会根据源库迁移版本重建一份规范 SQLite 库，逐项比较全部表、索引、触发器和视图；版本、状态或任何结构不一致都会拒绝。仅在结构完全一致后，对业务副本执行后续迁移；历史差异保留并逐条写入报告，原历史校验和不改写。

数据在一个 PostgreSQL 事务中复制：核对每张表的全部列、行数和逐行内容摘要，包含 SQLite 生成列以及订阅记录的隐式行号；整数和等值 decimal 统一比较，高精度金额不会因 28 位 decimal 上下文而被舍入。重建序列并检查所有外键，任何不匹配均回滚。PostgreSQL 以 UTC 运行，裸日期与 SQLite 的日期规则保持一致。

`import-report.json` 不包含密码、完整 KEY 或业务行，只包含表名、行数、摘要、结构证明和历史差异。签名部署包不包含数据库、连接文件、加密根或验收副本。Windows 原生 PostgreSQL 的副本校验是补充证据，不能替代 Ubuntu 容器的迁移、重启、备份恢复验收。

Ubuntu 的本轮结果与逐表摘要见 [trial.12 导入结果](../../output/deployment-ubuntu-20261014/postgres-conflict-session-trial12-20261017/trial12-sqlite-import-result.json) 和 [校验报告](../../output/deployment-ubuntu-20261014/postgres-conflict-session-trial12-20261017/trial12-sqlite-import-report.json)。历史 SQL 校验和差异仍列在报告中，不通过改写源迁移历史消除。
