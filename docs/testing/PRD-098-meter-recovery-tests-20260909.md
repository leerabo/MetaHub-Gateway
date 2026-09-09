# PRD-098 历史权益边界测试

`meter-recovery-backend-r5-20260909.xml` 中真实创建套餐和试用订阅，再追加订阅创建之后、权益结束之后的用量。worker 形成 owner_invalid，界面归档资格为 false，服务和直接 SQL 插入均拒绝归档，检查点仍为 0。

无订阅的历史用量由单独归档流程覆盖，未创建新的权益、账单周期或 Reading。PostgreSQL 原生迁移/并发由 `meter-recovery-native-r2-20260909.xml` 覆盖，完整订阅并发矩阵及历史合同临界时间跨引擎等价仍待补充。

证据根目录均为 `.artifacts/g6-security-20260907/`。这些结果不替代 PRD-098 原套餐状态机、降级、过期、支付和实际浏览器验收。
