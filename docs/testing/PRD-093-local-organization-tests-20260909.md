# PRD-093 本地组织创建验证

SQLite 专项包含创建并切换 Owner、重复请求幂等、无伪造模型资源、无邮箱验证事实、不可变授权、跨运行环境/邮件策略拒绝、失败整体回滚及 pristine down/up。原生 PostgreSQL 两项覆盖同样的创建权限、不可变绑定、治理清单指纹及迁移生命周期。源文件分别为 `test_local_workspace_creation.py` 和 `test_local_workspace_creation_postgresql.py`。

真实桌面/手机开通证据：`.artifacts/s1-browser-6y8nim3f/acceptance.json` 中 organization 阶段通过，创建者为 Owner 且不能访问平台注册设置。该证据不包含员工模型调用。

本次提交将组织代码与注册策略、消费者分离，旧迁移测试改为在隔离库中显式退到目标版本后测其 down/up；最终按整个 S1 批次统一跑数据库/API/原生 PostgreSQL 等检查，结果在后续提交版本验收记录中汇总。不得把历史工作树通过当作当前 index 的独立通过证明。

## 当前提交副本结果

index 产品树 `bc8e30bab20b1ea1daac0a60fd87663aac9f56a2` 导出副本的 identity 前端 27 项通过，0 failures/errors：`.artifacts/g6-security-20260907/s1-organization-index-frontend-20260909.xml`。生产 surface 与 287 资产构建通过，摘要 `0a734f3358104a8e56dc3765ccb77f2f37f9aa587c656ef5bb59ee8e16515c48`。4 个相关 Python 文件 Ruff、4 个暂存 JS 语法通过。18 个文件暂存敏感扫描零命中：`.artifacts/g6-security-20260907/organization-core-index-audit-20260909.json`。
