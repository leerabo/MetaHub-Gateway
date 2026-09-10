# PRD-094 用量会话与人员归属增量测试

本记录只覆盖 S2 批次的 Workspace 用量会话与稳定使用者身份。设计、两轮自审见 `docs/prd/S2-consumer-usage-accounting-batch-design.md`。

- SQLite 用量批次 11 项中，新增 5 项覆盖同一人多 KEY、停用/轮换后历史不变、跨空间查询空结果、导出任务绑定原操作者、混合凭据拒绝及生成/读取期间撤权。
- 原生 PostgreSQL 1 项真实组织服务链验证人员关联、双币种汇总、明细和异步导出。
- 本地 HTTP 77 检查点确认 Owner 组织会话可读、员工 KEY 不能读取管理用量；不要求员工登录，不以员工 KEY 获得 Membership。

原始报告与范围见 `S2-consumer-usage-accounting-batch-20260909.md`。完整 Membership/SSO、模型调用全流程不由本次通过替代。

2026-09-10：PRD-094 目录与 KEY 核心此前已独立提交，本轮共享用量消费方实现归属 PRD-099 `043f24e`，不重复提交相同代码。真实源码浏览器六名使用者不新增账号、轮换停用后历史归属保持的证据见 `S2-browser-finance-batch-20260910.md`。本 PRD 单独保留归属与身份审计记录，候选生产包验证不由源码报告替代。
