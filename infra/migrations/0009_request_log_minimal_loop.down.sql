-- 迁移说明：0009_request_log_minimal_loop.down 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


PRAGMA foreign_keys=off;

-- 回滚删除 idx_request_logs_provider_code 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_request_logs_provider_code;
-- 回滚删除 idx_request_logs_resolved_model 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_request_logs_resolved_model;
-- 回滚删除 idx_request_logs_request_model 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_request_logs_request_model;
-- 回滚删除 idx_request_logs_status_time 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_request_logs_status_time;
-- 回滚删除 idx_request_logs_created_at 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_request_logs_created_at;
-- 回滚删除 idx_request_logs_error_code 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_request_logs_error_code;
-- 回滚删除 idx_request_logs_trace_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_request_logs_trace_id;
-- 回滚删除 idx_request_logs_tenant_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_request_logs_tenant_id;

CREATE TABLE request_logs_old (
  request_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT,
  provider_id TEXT,
  model_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
  error_code TEXT,
  latency_ms INTEGER,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id)
);

-- 写入种子数据：保持基础角色、租户或字典项可重复初始化。
INSERT INTO request_logs_old(
  request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
  status, error_code, latency_ms, created_at
)
SELECT
  request_id, tenant_id, trace_id, virtual_key_id, provider_id, model_id,
  status, error_code, latency_ms, created_at
FROM request_logs;

DROP TABLE request_logs;
-- 调整 request_logs_old 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE request_logs_old RENAME TO request_logs;

-- 创建 idx_request_logs_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_tenant_id ON request_logs(tenant_id);
-- 创建 idx_request_logs_trace_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_trace_id ON request_logs(trace_id);
-- 创建 idx_request_logs_error_code 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_error_code ON request_logs(error_code);

PRAGMA foreign_keys=on;
