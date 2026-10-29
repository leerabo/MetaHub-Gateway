-- 迁移说明：0004_virtual_key_metadata.down 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 回滚删除 idx_virtual_keys_last_used_at 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_virtual_keys_last_used_at;
-- 回滚删除 idx_virtual_keys_environment 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_virtual_keys_environment;

-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys DROP COLUMN disabled_by;
-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys DROP COLUMN disabled_at;
-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys DROP COLUMN created_by;
-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys DROP COLUMN last_used_at;
-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys DROP COLUMN description;
-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys DROP COLUMN environment;
