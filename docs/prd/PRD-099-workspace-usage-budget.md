# PRD-099 组织用量、导出与预算增量

批次设计和两轮自审见 `S2-consumer-usage-accounting-batch-design.md` 与 `S2-internal-finance-budget-session-batch-design.md`。本独立提交承载组织内部用量、使用者聚合 DTO、原币导出与预算服务，不创建平台正式应收。

复用 PRD-094 已提交的稳定使用者与 KEY 绑定；共享 ResourceWriteTransaction 允许列表一次加入用量、预算、账本六个现有权限码，不新增角色权限。后续 PRD-144 账本服务和 PRD-147 页面提交依赖此公共边界。预算 no-store 中间件属于此提交，内部账本原有边界保持。

每个 PRD 的测试与敏感记录分别保存。使用者归属不创建登录账号，姓名部门受目录权限限制；费用汇总仍区分人民币/美元、真实/估算记录，CSV 列合同不变。规模计量、历史对账恢复和平台商业账不由这次补齐代替。
