-- 迁移说明：0006_model_catalog_metadata 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN provider_model_id TEXT;

-- 回填既有数据：为新增字段提供确定默认值，避免旧记录在查询时出现空语义。
UPDATE models
SET provider_model_id=upstream_model_name
WHERE provider_model_id IS NULL OR provider_model_id='';

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN display_name TEXT;

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN context_window INTEGER NOT NULL DEFAULT 0
  CHECK (context_window >= 0);

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN input_price_per_1k NUMERIC NOT NULL DEFAULT 0
  CHECK (input_price_per_1k >= 0);

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN output_price_per_1k NUMERIC NOT NULL DEFAULT 0
  CHECK (output_price_per_1k >= 0);

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'
  CHECK (currency IN ('CNY', 'USD'));

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN price_schema_version TEXT;

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN description TEXT;

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN created_by TEXT;

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN disabled_at TEXT;

-- 调整 models 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE models
  ADD COLUMN disabled_by TEXT;

-- 创建 idx_models_model_alias 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_models_model_alias ON models(tenant_id, model_alias);
-- 创建 idx_models_provider_model_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_models_provider_model_id ON models(tenant_id, provider_id, provider_model_id);
-- 创建 idx_models_status 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_models_status ON models(tenant_id, status);
