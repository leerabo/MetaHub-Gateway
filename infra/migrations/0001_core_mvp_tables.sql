-- 迁移说明：0001_core_mvp_tables 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 创建 tenants 表：保存租户身份、服务模式和启停状态，是所有业务数据 tenant_id 隔离的根。
CREATE TABLE IF NOT EXISTS tenants (
  tenant_id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  service_mode TEXT NOT NULL CHECK (service_mode IN ('internal_only', 'public_service')),
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- 创建 seed_runs 表：记录种子数据执行批次和校验和，避免初始化脚本重复写入后状态不可追踪。
CREATE TABLE IF NOT EXISTS seed_runs (
  seed_run_id TEXT PRIMARY KEY,
  tenant_id TEXT,
  seed_name TEXT NOT NULL,
  checksum TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('applied', 'skipped', 'failed')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- 创建 users 表：保存控制台用户、角色和状态，密码仅存不可逆哈希。
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  email TEXT NOT NULL,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id, email)
);

-- 创建 virtual_keys 表：保存客户侧调用 Key 的脱敏前缀、哈希和状态，不落明文密钥。
CREATE TABLE IF NOT EXISTS virtual_keys (
  virtual_key_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL,
  key_prefix TEXT NOT NULL,
  key_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id)
);

-- 创建 providers 表：保存上游模型厂商 Endpoint 元数据，按租户隔离 provider_code。
CREATE TABLE IF NOT EXISTS providers (
  provider_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  provider_code TEXT NOT NULL,
  name TEXT NOT NULL,
  base_url TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  UNIQUE (tenant_id, provider_code)
);

-- 创建 provider_credentials 表：保存 Provider Key 加密密文和指纹，支撑轮换与审计。
CREATE TABLE IF NOT EXISTS provider_credentials (
  provider_credential_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  key_fingerprint TEXT NOT NULL,
  encrypted_key TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
);

-- 创建 models 表：保存模型目录、价格、能力和上游模型名，是路由与成本估算的基准。
CREATE TABLE IF NOT EXISTS models (
  model_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  model_alias TEXT NOT NULL,
  upstream_model_name TEXT NOT NULL,
  capabilities TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id),
  UNIQUE (tenant_id, model_alias)
);

-- 创建 request_logs 表：保存每次网关调用的可观测摘要，失败请求也要留下 request_id 追踪。
CREATE TABLE IF NOT EXISTS request_logs (
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

-- 创建 usage_ledger 表：保存请求级 Token 与成本账本，后续按天、模型和 Key 汇总。
CREATE TABLE IF NOT EXISTS usage_ledger (
  usage_ledger_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  raw_usage TEXT NOT NULL DEFAULT '{}',
  normalized_usage TEXT NOT NULL DEFAULT '{}',
  cost_amount NUMERIC NOT NULL DEFAULT 0,
  currency TEXT NOT NULL DEFAULT 'CNY',
  price_schema_version TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (request_id) REFERENCES request_logs(request_id)
);

-- 创建 idx_seed_runs_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_seed_runs_tenant_id ON seed_runs(tenant_id);
-- 创建 idx_users_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_users_tenant_id ON users(tenant_id);
-- 创建 idx_virtual_keys_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_virtual_keys_tenant_id ON virtual_keys(tenant_id);
-- 创建 idx_providers_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_providers_tenant_id ON providers(tenant_id);
-- 创建 idx_provider_credentials_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_provider_credentials_tenant_id ON provider_credentials(tenant_id);
-- 创建 idx_provider_credentials_provider_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_provider_credentials_provider_id ON provider_credentials(provider_id);
-- 创建 idx_models_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_models_tenant_id ON models(tenant_id);
-- 创建 idx_models_provider_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_models_provider_id ON models(provider_id);
-- 创建 idx_request_logs_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_tenant_id ON request_logs(tenant_id);
-- 创建 idx_request_logs_trace_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_trace_id ON request_logs(trace_id);
-- 创建 idx_request_logs_error_code 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_request_logs_error_code ON request_logs(error_code);
-- 创建 idx_usage_ledger_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_tenant_id ON usage_ledger(tenant_id);
-- 创建 idx_usage_ledger_request_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_request_id ON usage_ledger(request_id);
