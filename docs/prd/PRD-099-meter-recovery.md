# PRD-099 计量恢复增量

本条拥有失败头部的读取、重试、非平台计费分类和不可变恢复事实。使用 SaaS 平台权限，不向企业 Owner、Support 或员工 KEY 授权。

两轮编码前设计自审见 `S2-meter-recovery-batch-design.md`：第一轮检查商业账/内部账分离与历史合同，第二轮检查事务、并发、撤权和旧页面。实现使用 SQLite 0149 / PostgreSQL 0156，共享严格 DTO 和现有平台会话鉴权。

归档必须是明确动作，只前进一个序号，不制造金额、权益、Reading 或周期；retry 不推进检查点。原 outbox 失败证据与 Usage 保留。同操作者同幂等键只能重放同正文，一次失败尝试只能追加一个恢复事实。

已通过隔离数据库、API 和原计量回归。真实 MFA 会话闭环、实际发布、历史事实业务分类、大规模查询和长期观察仍待完成。测试与敏感审计分别见 `../testing/PRD-099-meter-recovery-tests-20260909.md` 和 `../testing/PRD-099-meter-recovery-sensitive-audit-20260909.md`。
