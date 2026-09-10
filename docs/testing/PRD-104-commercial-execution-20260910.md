# PRD-104 发票对账执行测试与敏感审计

独立设计：`../prd/PRD-104-commercial-execution.md`。

- 单元 27 项通过，覆盖 9 分支，验证下载字节/MIME、ETag、no-store、异常及上下文。
- 批次初次发票/单次下载/对账/双域冲红 API 通过；最终联合结果见批次记录。
- 敏感审计：服务仍接收 Header-only 单次 token，不引入 URL、DOM 或日志存储；未改金额和审批。
- 单元证据 `.artifacts/s2-commercial-worker-fixture-final-20260910.xml`。
- 本地测试产物不是税控渠道正式发票，百万分录、全浏览器和生产开票仍需单独证明。
