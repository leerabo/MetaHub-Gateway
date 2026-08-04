-- 迁移说明：0008_model_alias_minimal_routing.down 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 回滚删除 idx_routing_decisions_error_code 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_routing_decisions_error_code;
-- 回滚删除 idx_routing_decisions_alias_time 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_routing_decisions_alias_time;
-- 回滚删除 idx_routing_decisions_request_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_routing_decisions_request_id;
-- 回滚删除 routing_decisions 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS routing_decisions;

-- 回滚删除 idx_model_aliases_fallback_model 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_model_aliases_fallback_model;
-- 回滚删除 idx_model_aliases_default_model 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_model_aliases_default_model;
-- 回滚删除 idx_model_aliases_tenant_status 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_model_aliases_tenant_status;
-- 回滚删除 model_aliases 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS model_aliases;
