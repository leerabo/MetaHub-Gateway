# PRD-093 Workspace 操作连接生命周期

## 实测与范围

S1 合并回归 `.tmp/pytest-s1-consumer-regression-h6ehs5p4/results.xml` 的组织创建 p95 为 1.244672656 秒，未达到小于 1 秒目标。独立 cProfile `.tmp/profile-workspace-create-0debxlww/creation.prof` 显示 13 次创建业务函数耗时 7.947 秒，其中全局会话查询累计 5.079 秒，关闭物理连接 1.211 秒。每个创建请求的重放预检和写事务均重新打开物理连接，schema 初次加载重复发生。

拟改动仅为 `workspaces.routes._run`：采用相邻 Identity 与 Workspace Access 已使用的 `database_request_scope(reuse_connections=True)`，在同步工作线程内建立及释放作用域。复用受限的空闲物理连接，不复用身份、权限判断、事务或响应。

## 第一轮编码前自审

- 仍执行 GlobalSession、CSRF、账号状态、邮箱/本地授权、幂等、Owner、审计事件和所有 SQL 约束。
- 每个 worker operation 独立作用域；不能在事件循环中打开共享事务，也不把先前读到的账号作为后续写事务授权。
- 与现有身份/成员模块模式相同，不新增池，不修改共享池或 PostgreSQL 驱动，不引入鉴权缓存。

## 第二轮编码前自审

- 成功/异常均在 worker 的 finally 路径释放；异常遗留事务由现有释放流程 rollback，旧 facade 不可再次使用。
- 单元覆盖顺序复用的独立 lease、关闭后不可访问、失败写入不泄漏到下一操作。
- 针对性执行完整 PRD-093 API 套件（含幂等、权限、并发、敏感 canary、原 p95<1s），必要时再补整批回归；剖析开启的运行不作为 SLO 通过证据。
- 该优化不完成后续财务、SSO、邀请或全产品验收。上线仅更新控制服务，不迁移用户数据。
