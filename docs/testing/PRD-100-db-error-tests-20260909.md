# PRD-100 数据库异常测试

`shared-sdk/python/tests/test_db_error_traceback.py` 验证 contextmanager 重抛后的对象身份、code、message 和 traceback。共享数据库回归 `.artifacts/g6-security-20260907/meter-recovery-shared-20260909.xml` 共 59 项通过，覆盖连接生命周期、提交守卫、操作截止、schema 和迁移安全。

真实 SQLite 的有事实回滚拒绝与 pristine 往返由 backend-r5 记录覆盖；真实 PostgreSQL 由 native-r2 复合记录覆盖。未扩展为生产备份恢复或全量基础设施验收。
