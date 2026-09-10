# PRD-099 使用者用量与导出增量测试

设计和两轮自审沿 `S2-consumer-usage-accounting-batch-design.md`；完整证据索引见同批测试记录。

- SQLite 11 项通过，新增用例断言人民币/美元分列、真实/估算记录分离、使用者筛选与导出一致、原 CSV 列与无筛选指纹兼容、异步 worker、过期、重放和原操作者绑定。
- PostgreSQL 1 项真实数据库验证所有汇总 SQL、使用者关联、原币金额、异步完成和两行 CSV 下载。
- 缓存边界独立修复 `57a334f`，31 项成功/异常响应测试；本地 77 HTTP 检查点通过。
- 空账本 30 次/端点 p95 为 summary 273.105ms、records 237.435ms。无亿级账本或高并发查询证据，不将短性能测量扩张为 PRD-099 规模门禁。

此处读取内部 Usage 成本，不新增正式平台应收。平台 Meter、周期结算及付费渠道对账仍遵循各自验收，不因本批通过自动完成。

2026-09-10：独立提交范围见 `../prd/PRD-099-workspace-usage-budget.md`。增加真实浏览器源码业务链证据，见 `S2-browser-finance-batch-20260910.md`，不替代完整候选生产包验收。最新定向 Cookie API 为 `.artifacts/s2-api-8wu9yy9g/results.xml`，含可信同源 GET 导出及显式恶意来源拒绝；独立 PRD-147 来源修复已提交 `b818260`。最终提交与候选包验证以本轮批次后续记录为准。
