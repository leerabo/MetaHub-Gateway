# PRD-148 SaaS GA 发布与回滚

## 目标

为平台域提供从 `release_candidate` 到正式 GA 的可审计发布边界。GA 只能复用已签名的 RC 事实，不能通过前端或接口绕过证据、六方会签、发布后复核或回滚独立验证。

## 用户路径

平台管理员进入平台发布页后，只看到当前候选、20 个门禁的通过/阻塞状态和下一步。技术摘要、签名和内部操作人标识仅用于审计，不在普通工作区导航中展示。

1. 查看 GA 门禁。
2. 为已有 RC 门禁提交新鲜证据。
3. 六方会签及 RC 复核完成后执行 GA 发布。
4. 发生故障时由一名操作人发起回滚，另一名操作人验证回滚。

## 接口

- `GET /platform/ga-gates`
- `POST /platform/ga-gates/{ga_gate_id}/evidence`
- `POST /platform/ga-launches`
- `POST /platform/ga-launches/{ga_launch_id}/rollback`
- `POST /platform/ga-launches/{ga_launch_id}/review`

所有写操作要求平台会话、平台 CSRF、来源校验、幂等键、`If-Match` 和 step-up grant。工作区 Cookie、SupportSession、Authorization header 或混合凭据均拒绝。

## 发布条件

- 候选状态为 `release_candidate`。
- 20 个 GA 证据全部存在、通过且未过期。
- `long_stability` 至少 72 小时。
- product、sre、security、test、finance、legal 六方均已批准。
- RC 发布后复核已完成。

## 回滚与事件

回滚采用 request/verify 两阶段，并强制请求人与验证人不同。发布、批准和回滚事件进入事件表及 outbox，敏感原文不入库，仅保存摘要哈希。

## 2026-09-11 增量设计与两轮编码前自审

计划依据：SaaS Excel `01_PRD总表` 第 60 行、`08_里程碑门禁` 第 10 行。仍需全域 QA、最终迁移对账、真实长稳和发布事实，接口注册或 RC 测试不能替代 GA 验收。

第一轮自审（权限、签名与请求所有权）：GA 执行必须复用 RC 的当前会签权限校验和执行人独立性校验。导入证据必须逐字段匹配已验证 RC 证据及签名摘要，不能忽略提交的时间、来源或指标。幂等摘要必须包含路径目标，避免在其他门禁或发布记录上重放既有结果。

第二轮自审（生命周期、回滚与证据）：72 小时判断使用签名证据的起止时间，不强制把通过率指标误作时长指标；同一事务内检查过期与撤权。无事实时允许 down，有证据、命令或事件时必须拒绝 down；SQLite RAISE 只能用于触发器。新增独立 GA 流程与迁移测试，明确测试时钟和模拟身份只证明实现，不构成真实审批或 72 小时运行证据。
