# PRD-094 原生 PostgreSQL 使用者验收

## 验证结果

2026-09-10 00:59（北京时间），`.artifacts/s1-index-native-547deff2f27d4b3593683345d6c1f2ee/results.xml`：2 passed，0 failures/errors/skipped，100.785 秒。源码为此前独立 S1 产品导出 `.tmp/s1-core-index-20260909`，非包含未提交 S2 的工作树。

覆盖真实 PostgreSQL 创建组织 Owner、创建非登录使用者、签发 KEY、轮换后旧 KEY 失效、停用后新 KEY 失效、人员归属不可变、已有安全事实阻止 down，以及 pristine down/up 后重复 apply 无变更。故意非法更新产生的数据库 ERROR 是拒绝断言的一部分，不等于测试失败。

`run.json` 核对运行前后数据库目录一致，原有数据库保留，没有使用试用数据库或收费模型。测试使用原生 psycopg 和实际迁移，不是 SQLite 仿真。测试集只含本次尚缺的两项，未重复已通过的组织创建测试。

## 恢复与收尾

上次 `s1-index-native-1034c4f89a424767b919b433b9c66bd0` 在测试库恢复后遭遇主机重启，未产生测试 XML，不记作通过。原有失败目录和数据库均保留。此次启动按实际服务状态等待恢复，不在启动观察超时后强制停止；只在模板初始化阶段使用已有 180 秒 SQL 批次预算，业务测试仍使用默认预算和原断言。

本轮 PostgreSQL 已正常 shutdown，日志终态为 `database system is shut down`。两个用例消除了原生使用者验收缺口，不证明全部 S1 性能、浏览器或完整 SaaS 目标完成。
