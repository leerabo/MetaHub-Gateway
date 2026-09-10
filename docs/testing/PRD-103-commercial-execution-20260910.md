# PRD-103 支付执行测试与敏感审计

独立设计：`../prd/PRD-103-commercial-execution.md`。

- 单元 18 项通过，覆盖 6 个路由分支；回调仍等待持久化服务返回，不提前 202。
- 初次批次失败保留在 `.artifacts/s2-api-66unkpf8/results.xml`；诊断复跑
  `.artifacts/s2-api-059pqq1v/results.xml` 为 94 次 200、6 次 SQLITE_BUSY/503。
- 修复后 `.artifacts/s2-api-urihxwpb/results.xml` 复合流程通过：首次 8 并发唯一收据、
  持写锁时只读重放、100 重放、签名篡改/旧密钥、乱序、催缴恢复、退款和正文冲突。
- 终态 5 条收据、5 次 attempt、2 次 capture effect、1 次 refund effect，外键与完整性通过。
- 敏感审计：新增诊断只输出状态码、固定业务码及 SQLite 错误类别；不输出回调、签名、
  client token 或原始异常正文。原 PCI canary、token 和加密对象扫描保持。
- 模拟渠道回调不是外部真实付款，最终联合结果见批次记录。
- 最终联合首跑 `.artifacts/s2-api-80iz7dsi/results.xml` 的其他 7 项通过，支付在 100 重放
  通过后因旧催缴测试按随机排序取第一行失败。修订为按订单 ID 同时校验 past_due/fulfilled，
  保留原失败证据，不改变生产排序或业务状态。
