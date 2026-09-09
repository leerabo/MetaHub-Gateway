# PRD-093 Workspace 连接复用验证

设计与两轮编码前自审见 `docs/prd/PRD-093-workspace-worker-connection-review-20260909.md`。

- 原 S1 报告 `.tmp/pytest-s1-consumer-regression-h6ehs5p4/results.xml`：26 项中 24 项通过，组织创建 p95 1.244672656 秒，保留原始失败证据。
- 修复仅改变 Workspace worker 的数据库连接生命周期，复用 Identity/Workspace Access 已使用的作用域和有界空闲池，未修改生产权限、幂等、事务 SQL、schema 或 SLO。
- `.tmp/pytest-s1-consumer-regression-yvyy11y1/results.xml`：7 passed，0 failed，0 skipped。包括完整 PRD-093 的 5 个 API 测试和 2 个新增连接单元用例。
- API 覆盖组织创建原子性、无占位资源、幂等重放/异体拒绝、个人分支、Region、跨账号、CAS、多空间密码投影、未验证账号拒绝、并发只创建一次、敏感 canary 和 warm 创建 p95<1s 原门槛。
- 新增单元检查工作不在事件循环线程执行、每操作独立 facade、顺序复用相同物理连接、失败未提交写入被回滚、结束后的 facade 不可访问、作用域不泄漏到外部默认连接。
- Ruff 与差异空白检查通过。性能剖析运行不作为 SLO 证据；通过依据是关闭剖析后的 API 套件。

本条为性能回归修复，不包含组织本地免邮件开通的未提交变更。PostgreSQL 服务层行为未修改，本次没有新跑完整 PostgreSQL API、跨进程池压力或 72 小时测试。
