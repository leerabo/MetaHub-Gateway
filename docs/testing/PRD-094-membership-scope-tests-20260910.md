# PRD-094 成员状态隔离测试记录

- SQLite：`.artifacts/s1-index-batch-5emvyjwh/results.xml`，成员隔离和 pristine down/up 两项通过；同一运行另有一项 PRD-093 资料测试。
- PostgreSQL：`.artifacts/s1-index-native-57f99691222d43c2a4491e539ccbda97/results.xml`，两项通过，包含完整同账号双组织 HTTP 场景与原生迁移往返。现有数据库清单不变，测试集群已确认关闭。
- 双向身份边界均验证：本组织停用不撤销 GlobalSession 或另一个组织；全局账号停用后，Owner 恢复成员不会恢复全局身份。
- 两种数据库都验证有停用事实后拒绝回滚，失败回滚不能部分恢复旧触发器。

最初 API 新断言误读公开响应的 account_id，失败证据为 `.artifacts/s1-index-batch-a0frtf3y`；修正测试定位方式后以上运行通过。Native 启动脚本最初误判 launcher 返回值，随后核实同一 PostgreSQL PID 和端口确实就绪，在原进程上继续测试，没有启动第二个集群。

SQLite 自动清理因连接句柄报告三处失败，隔离 scratch 保留；不删除失败证据，不接触在线数据库。
