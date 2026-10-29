-- 迁移说明：0006_model_catalog_metadata.down 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 回滚删除 idx_models_status 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_models_status;
-- 回滚删除 idx_models_provider_model_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_models_provider_model_id;
-- 回滚删除 idx_models_model_alias 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_models_model_alias;

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN disabled_by;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN disabled_at;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN created_by;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN description;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN price_schema_version;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN currency;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN output_price_per_1k;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN input_price_per_1k;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN context_window;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN display_name;
-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models DROP COLUMN provider_model_id;
