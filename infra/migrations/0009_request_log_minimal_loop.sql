-- 迁移说明：0009_request_log_minimal_loop 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN endpoint TEXT;

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN request_model TEXT;

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN resolved_model TEXT;

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN provider_code TEXT;

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN input_tokens INTEGER NOT NULL DEFAULT 0
  CHECK (input_tokens >= 0);

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN output_tokens INTEGER NOT NULL DEFAULT 0
  CHECK (output_tokens >= 0);

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0
  CHECK (total_tokens >= 0);

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN estimated_cost_amount NUMERIC NOT NULL DEFAULT 0
  CHECK (estimated_cost_amount >= 0);

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN currency TEXT NOT NULL DEFAULT 'CNY'
  CHECK (currency IN ('CNY', 'USD'));

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN price_schema_version TEXT;

-- 调整 request_logs 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs
  ADD COLUMN updated_at TEXT;

-- 回填既有数据：为新增字段提供确定默认值，避免旧记录在查询时出现空语义。
UPDATE request_logs
SET updated_at=created_at
WHERE updated_at IS NULL;

-- 创建 idx_request_logs_created_at 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_created_at
  ON request_logs(tenant_id, created_at DESC);

-- 创建 idx_request_logs_status_time 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_status_time
  ON request_logs(tenant_id, status, created_at DESC);

-- 创建 idx_request_logs_request_model 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_request_model
  ON request_logs(tenant_id, request_model);

-- 创建 idx_request_logs_resolved_model 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_resolved_model
  ON request_logs(tenant_id, resolved_model);

-- 创建 idx_request_logs_provider_code 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_provider_code
  ON request_logs(tenant_id, provider_code);
