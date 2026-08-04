-- 迁移说明：0005_provider_metadata.down 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 回滚删除 idx_providers_health_status 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_providers_health_status;
-- 回滚删除 idx_providers_provider_type 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_providers_provider_type;

-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers DROP COLUMN disabled_by;
-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers DROP COLUMN disabled_at;
-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers DROP COLUMN created_by;
-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers DROP COLUMN last_test_error;
-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers DROP COLUMN last_tested_at;
-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers DROP COLUMN health_status;
-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers DROP COLUMN description;
-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers DROP COLUMN auth_type;
-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers DROP COLUMN provider_type;
