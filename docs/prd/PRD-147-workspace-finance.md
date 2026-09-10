# PRD-147 财务资源页组织会话迁接设计

共享设计及两轮自审见 `S2-internal-finance-budget-session-batch-design.md`。范围为预算和正式 `internal-finops` 页面，保持废弃导航不复活。

第一轮：前后端同时迁接 Cookie 会话；CSRF、Origin、动态权限、读写线程、事务内授权复核缺一不可。只改 fetch 的凭据方式不能构成完成。

第二轮：页面生命周期绑定组织和代次，切组织或离页取消请求并清除财务草稿、原因、幂等键与游标；旧 DOM、迟到成功或失败不能写回新组织。成本率刷新不得覆盖账本 DOM。发布摘要在前后端同步并验证生产资产。

测试记录：`../testing/PRD-147-workspace-finance-tests-20260909.md`。
敏感审计：`../testing/PRD-147-workspace-finance-sensitive-audit-20260909.md`。
本地交付不等于所有 Console 页面、迁移规模或回滚全流程已验收。
