# PRD-099 商业账单执行测试与敏感审计

独立设计：`../prd/PRD-099-commercial-execution.md`。

- 单元 18 项通过，覆盖 5 个修复分支与既有恢复分支的成功、重放和错误。
- CSV 字节、文件名、no-store 与恢复原语义保持；批次初次计量/关账/迟到/调整 API 通过。
- 敏感审计：未改 Usage、商业金额、权益或 checkpoint；旧失败记录未归档、跳过或删除。
- 最终联合结果见批次记录，单元证据 `.artifacts/s2-commercial-worker-fixture-final-20260910.xml`。
- 不以模拟 DTO 心跳证明真实网络 p95，不替代历史合同恢复或全量性能验收。
