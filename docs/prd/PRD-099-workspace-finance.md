# PRD-099 组织预算与内部账本增量设计

本增量服务于计量核对的内部成本与预算入口，不改变平台 Meter、订阅额度或应收。共享合同和两轮编码前自审见 `S2-internal-finance-budget-session-batch-design.md`。

第一轮：金额持久事实保持非负整数微元。预留与批任务汇总改用真实主键排序及共享表探测，SQLite/PostgreSQL 均不依赖 rowid，不以浮点或隐式换汇替代原币证据。

第二轮：预算事务按数据库方言显式只读；同一组织会话的规则、用量与预留读取保持快照一致，返回前新快照复核授权。写规则、释放 block 与审计原子提交。

测试记录：`../testing/PRD-099-workspace-finance-tests-20260909.md`。
敏感审计：`../testing/PRD-099-workspace-finance-sensitive-audit-20260909.md`。
此增量不证明 PRD-099 平台计量、周期关闭与完整对账均完成。
