-- 迁移说明：0008_model_alias_minimal_routing 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 创建 model_aliases 表：保存客户请求模型到内部模型的映射，支撑环境级灰度和启停。
CREATE TABLE IF NOT EXISTS model_aliases (
  model_alias_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  alias TEXT NOT NULL,
  environment TEXT NOT NULL CHECK (environment IN ('dev', 'test', 'prod')),
  default_model_id TEXT NOT NULL,
  fallback_model_id TEXT,
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  description TEXT,
  created_by TEXT,
  disabled_at TEXT,
  disabled_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (default_model_id) REFERENCES models(model_id),
  FOREIGN KEY (fallback_model_id) REFERENCES models(model_id),
  UNIQUE (tenant_id, alias, environment)
);

-- 创建 idx_model_aliases_tenant_status 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_model_aliases_tenant_status
  ON model_aliases(tenant_id, status);

-- 创建 idx_model_aliases_default_model 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_model_aliases_default_model
  ON model_aliases(tenant_id, default_model_id);

-- 创建 idx_model_aliases_fallback_model 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_model_aliases_fallback_model
  ON model_aliases(tenant_id, fallback_model_id);

-- 创建 routing_decisions 表：保存模型别名解析结果和失败原因，方便排查为什么选中了某个上游。
CREATE TABLE IF NOT EXISTS routing_decisions (
  routing_decision_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  virtual_key_id TEXT,
  model_alias_id TEXT,
  requested_model TEXT NOT NULL,
  environment TEXT NOT NULL,
  selected_model_id TEXT,
  selected_internal_model_id TEXT,
  provider_id TEXT,
  provider_code TEXT,
  decision_status TEXT NOT NULL CHECK (decision_status IN ('selected', 'failed')),
  decision_reason TEXT NOT NULL,
  candidate_summary TEXT NOT NULL DEFAULT '{}',
  error_code TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  FOREIGN KEY (model_alias_id) REFERENCES model_aliases(model_alias_id),
  FOREIGN KEY (selected_model_id) REFERENCES models(model_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
);

-- 创建 idx_routing_decisions_request_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_routing_decisions_request_id
  ON routing_decisions(tenant_id, request_id);

-- 创建 idx_routing_decisions_alias_time 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_routing_decisions_alias_time
  ON routing_decisions(tenant_id, model_alias_id, created_at);

-- 创建 idx_routing_decisions_error_code 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_routing_decisions_error_code
  ON routing_decisions(tenant_id, error_code);
