# PRD-094 成员状态隔离修复

实际 API 发现旧 User 状态触发器把组织成员停用传播为全局账号停用。两轮编码前审查记录见 `S1-workspace-profile-navigation-batch-20260910.md` 的成员隔离补充。

第一轮结论：已映射 Membership 的 User 状态只影响本组织，不拥有 GlobalAccount 状态。按 tenant_id 和 legacy_user_id 限定映射；没有映射的旧身份仍保持原同步行为。

第二轮结论：SQLite 0150 与 PostgreSQL 0157 成对修改触发器并更新删除治理摘要。pristine 可往返，有成员停用安全事实后拒绝回滚到旧行为。不得猜测或恢复历史已停用的全局账号。

验证必须双向：组织停用不撤销其他组织或全局会话；组织恢复也不能恢复全局已停用账号。Owner 不是 SaaS Platform 管理员；使用者与登录 Membership 保持独立。
