# PRD-144 内部使用者费用分域验证

本增量沿 S2 批次设计，不重建平台商业账。证据索引见 `S2-consumer-usage-accounting-batch-20260909.md`。

- SQLite/原生 PostgreSQL 的使用者汇总按 CNY/USD 分列，估算与真实计量计数分别返回。
- finance_admin 可读内部费用但没有员工姓名/部门标签；需要现有目录权限才能取得标签。Platform/混合凭据不能借用该接口读取组织费用。
- 人工复核本次 Usage 服务读写只涉及读取现有账本与管理导出任务/审计，不新增 Subscription、Invoice、Payment 或商业应收写入。

本记录是内部费用增量与权限边界证据，不证明平台订阅结算、支付/发票三方对账或完整 PRD-144 门禁。代码共享于本批，独立提交尚未完成。
