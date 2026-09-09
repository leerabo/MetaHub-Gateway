# PRD-093 Workspace 连接复用敏感审计

- 提交范围限定 Workspace routes、连接生命周期单元测试、设计与两轮自审、测试记录和本审计。其它 PRD、迁移、配置、SQLite 库和临时脚本不纳入提交。
- 只复用物理连接，不缓存 GlobalSession、CSRF、账号/成员状态或权限决定。预检和创建仍在各自 worker operation 中重新查库。
- 成功/异常均释放作用域；旧 facade 失效，未提交写入回滚。连接在后台线程独占借用，不在事件循环中共享事务。
- 沿用已有池的清理和有界保留，不新增后台循环，不记录 SQL 参数、密码、Cookie、令牌或模型 KEY。
- 单元使用临时普通数字表；API 安全 canary、身份边界和并发重放测试保持原断言并通过。未修改或放宽 p95 门槛。
- 精确暂存范围、UTF-8 与高置信度凭据模式扫描记录为 `.artifacts/g6-security-20260907/prd093-workspace-pool-sensitive-audit-20260909.json`。这不是全产品安全会签。
