# PRD-099 计量恢复测试

证据目录：`.artifacts/g6-security-20260907/`。

- `meter-recovery-backend-r5-20260909.xml`：17 passed、0 failures/errors。包括归档/重试/重放、旧版本拒绝、审计与提交前撤权回滚、读/重放复核、调用方事务保护、布尔确认、Origin/CSRF/混合凭据、订阅期缺口的服务及原始 SQL 拒绝、迁移往返和原计量回归。
- `meter-recovery-native-r2-20260909.xml`：1 个真实 PostgreSQL 复合流程通过。原生迁移往返、重试重放、失败计数、两线程并发只有一个归档成功、不改变商业金额、有事实拒绝回滚及后续头部可读。
- 20 次 request-owned pooled 服务读取 p95 39.751 ms，门槛 300 ms；不含真实平台鉴权与网络，不证明亿级账本性能。
- r1 曾因系统临时目录权限未进入 DB 测试；r2 发现回滚格式问题及错误断言；r3/r4 有测试 SQLite 空闲池未关闭导致 teardown 失败。r4 冷开 schema p95 732.594 ms。全部失败证据保留，不覆盖为绿色。
- 最终 fixture 显式关闭 SQLite 空闲池和 PostgreSQL 池，删除的只是本轮创建的独立数据库副本。没有真实模型调用、用户库恢复或历史事实修改。

限制：API fixture 的 PlatformContext 被桩替换；服务内权限复核确实运行，但不能代替真实 MFA、账号撤销和角色绑定的端到端验收。浏览器独立记录见 PRD-147。

## 真实 MFA 接口补充验收

- `meter-real-auth-r2-20260909.xml`：1 项复合流程通过，33.370 秒，0 failures/errors。`test_platform_meter_recovery_real_auth.py` 复用 PRD-095 注册、登录、TOTP、bootstrap、平台角色与支持双审批 API，不替换鉴权或权限函数。
- 覆盖未登录/仅全局登录拒绝、组织 Owner 与真实 SupportSession 拒绝、平台 Security 无财务权限、角色变更后旧会话失效、平台 Billing 恢复操作、TOTP 重放拒绝、Origin/CSRF、重试/归档幂等、旧失败版本拒绝，以及停用操作者后读取/重放拒绝。
- 重试不推进检查点；归档仅增加 1 条恢复和 1 条审计、推进 1 个序号，不创建商业读数、账期或修改 Usage。复用的测试注册和 MFA 凭据只存在隔离库中，没有使用或修改用户的账号/MFA。
- 20 次真实平台鉴权的 ASGI GET p95 为 100.882 ms，原门槛 300 ms。无真实网络、浏览器交互及大规模事实，此数值不替代生产性能验收。
- r1 失败是测试把既定 revision_conflict HTTP 412 写为 409；只修正断言并增加精确错误码检查，保留失败 XML。另一次 shell 命令命中 Windows Store Python 占位符，未执行测试；改用项目既有 Python312。
- fixture 已关闭数据库池，删除本测试创建的 SQLite 副本与 WAL/SHM；原迁移基线及所有 XML 保留。
