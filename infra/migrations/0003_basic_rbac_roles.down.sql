-- 迁移说明：0003_basic_rbac_roles.down 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 回滚删除 idx_roles_tenant_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_roles_tenant_id;
-- 回滚删除 roles 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS roles;
