# PRD-099 计量恢复测试

证据目录：`.artifacts/g6-security-20260907/`。

- `meter-recovery-backend-r5-20260909.xml`：17 passed、0 failures/errors。包括归档/重试/重放、旧版本拒绝、审计与提交前撤权回滚、读/重放复核、调用方事务保护、布尔确认、Origin/CSRF/混合凭据、订阅期缺口的服务及原始 SQL 拒绝、迁移往返和原计量回归。
- `meter-recovery-native-r2-20260909.xml`：1 个真实 PostgreSQL 复合流程通过。原生迁移往返、重试重放、失败计数、两线程并发只有一个归档成功、不改变商业金额、有事实拒绝回滚及后续头部可读。
- 20 次 request-owned pooled 服务读取 p95 39.751 ms，门槛 300 ms；不含真实平台鉴权与网络，不证明亿级账本性能。
- r1 曾因系统临时目录权限未进入 DB 测试；r2 发现回滚格式问题及错误断言；r3/r4 有测试 SQLite 空闲池未关闭导致 teardown 失败。r4 冷开 schema p95 732.594 ms。全部失败证据保留，不覆盖为绿色。
- 最终 fixture 显式关闭 SQLite 空闲池和 PostgreSQL 池，删除的只是本轮创建的独立数据库副本。没有真实模型调用、用户库恢复或历史事实修改。

限制：API fixture 的 PlatformContext 被桩替换；服务内权限复核确实运行，但不能代替真实 MFA、账号撤销和角色绑定的端到端验收。浏览器独立记录见 PRD-147。
