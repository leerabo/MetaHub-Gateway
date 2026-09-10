# PRD-144 内部成本账本组织会话增量设计

账本与 Provider 成本率同属企业内部成本页面，但为并列区域；平台商业账、应收和订阅余额独立。共享设计及两轮自审见 `S2-internal-finance-budget-session-batch-design.md`。

第一轮：公共账本入口使用 Workspace/legacy 单凭据权限校验，内部事务 helper 仍由受控调用者持有，不强制套用浏览器凭据。幂等键、金额、币种、冲正目标与不可变历史合同保持。

第二轮：PostgreSQL SUM(bigint) 的 NUMERIC 不能放宽为任意字符串金额，逐行校验整数后精确累计。超额、负值、坏行、跨租户目标与溢出均拒绝。冲正对话框显示不可编辑的“冲正”，不提供筛选专用“全部”。

测试记录：`../testing/PRD-144-workspace-finance-tests-20260909.md`。
敏感审计：`../testing/PRD-144-workspace-finance-sensitive-audit-20260909.md`。
此增量不替代完整商业对账、争议流程及外部依赖验收。
