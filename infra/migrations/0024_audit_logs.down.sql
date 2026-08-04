-- 回滚说明：仅用于本地或测试环境回退 PRD-034；执行前必须确认审计数据已备份。

-- 先删除不可变触发器，否则回滚阶段无法安全移除审计表。
DROP TRIGGER IF EXISTS trg_audit_logs_immutable_delete;
DROP TRIGGER IF EXISTS trg_audit_logs_immutable_update;
DROP TRIGGER IF EXISTS trg_audit_logs_owner_insert;

-- 显式删除索引，保持 down migration 的结构意图清晰且可重复执行。
DROP INDEX IF EXISTS idx_audit_logs_tenant_request;
-- 删除动作筛选索引，避免回滚后残留无主索引元数据。
DROP INDEX IF EXISTS idx_audit_logs_tenant_action_time;
-- 删除对象追溯索引，与 audit_logs 主表生命周期保持一致。
DROP INDEX IF EXISTS idx_audit_logs_tenant_object_time;
-- 删除操作者筛选索引，回滚后不保留审计查询结构。
DROP INDEX IF EXISTS idx_audit_logs_tenant_actor_time;
-- 最后删除默认时间索引，再移除审计主表。
DROP INDEX IF EXISTS idx_audit_logs_tenant_time;

-- 删除审计表会永久丢失管理操作历史，生产环境不得直接执行该回滚。
DROP TABLE IF EXISTS audit_logs;
