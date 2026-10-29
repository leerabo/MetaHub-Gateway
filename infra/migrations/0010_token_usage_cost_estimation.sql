-- 迁移说明：0010_token_usage_cost_estimation 为 PRD-017 增加 Token 用量归一化、成本估算和价格快照能力。

-- 创建 price_snapshots 表：为每次用量成本估算保留模型价格、币种和版本快照，避免后续价格变更影响历史账本解释。
CREATE TABLE IF NOT EXISTS price_snapshots (
  price_snapshot_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  request_id TEXT,
  model_id TEXT,
  provider_id TEXT,
  provider_code TEXT,
  currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency IN ('CNY', 'USD')),
  input_price_per_1k NUMERIC NOT NULL DEFAULT 0 CHECK (input_price_per_1k >= 0),
  output_price_per_1k NUMERIC NOT NULL DEFAULT 0 CHECK (output_price_per_1k >= 0),
  price_schema_version TEXT,
  billing_dimensions TEXT NOT NULL DEFAULT '{}',
  provider_raw_mapping TEXT NOT NULL DEFAULT '{}',
  source TEXT NOT NULL DEFAULT 'model_catalog',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active')),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (request_id) REFERENCES request_logs(request_id),
  FOREIGN KEY (model_id) REFERENCES models(model_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
);

-- 调整 usage_ledger 表：增加账本状态字段，标识当前记录是已落账的正式用量记录。
ALTER TABLE usage_ledger
  ADD COLUMN status TEXT NOT NULL DEFAULT 'recorded'
  CHECK (status IN ('recorded'));

-- 调整 usage_ledger 表：增加 total_tokens，支持控制台和 API 直接按总量筛选与展示。
ALTER TABLE usage_ledger
  ADD COLUMN total_tokens INTEGER NOT NULL DEFAULT 0
  CHECK (total_tokens >= 0);

-- 调整 usage_ledger 表：增加 cache_tokens，保留上游返回的缓存命中 Token 口径。
ALTER TABLE usage_ledger
  ADD COLUMN cache_tokens INTEGER NOT NULL DEFAULT 0
  CHECK (cache_tokens >= 0);

-- 调整 usage_ledger 表：增加 cost_cny，明确人民币成本字段，避免只靠 currency 解释数值。
ALTER TABLE usage_ledger
  ADD COLUMN cost_cny NUMERIC NOT NULL DEFAULT 0
  CHECK (cost_cny >= 0);

-- 调整 usage_ledger 表：增加 cost_usd，明确美元成本字段，M0 阶段不做汇率折算。
ALTER TABLE usage_ledger
  ADD COLUMN cost_usd NUMERIC NOT NULL DEFAULT 0
  CHECK (cost_usd >= 0);

-- 调整 usage_ledger 表：增加 estimated，区分上游真实 usage 与本地估算 usage。
ALTER TABLE usage_ledger
  ADD COLUMN estimated INTEGER NOT NULL DEFAULT 0
  CHECK (estimated IN (0, 1));

-- 调整 usage_ledger 表：增加 usage_source，记录用量来源是上游返回还是网关估算。
ALTER TABLE usage_ledger
  ADD COLUMN usage_source TEXT NOT NULL DEFAULT 'upstream'
  CHECK (usage_source IN ('upstream', 'estimated'));

-- 调整 usage_ledger 表：关联价格快照，支撑每条用量账本追溯到当时的价格版本。
ALTER TABLE usage_ledger
  ADD COLUMN price_snapshot_id TEXT;

-- 调整 usage_ledger 表：冗余 model_id，支持用量 API 不依赖 request_logs 也能按模型统计。
ALTER TABLE usage_ledger
  ADD COLUMN model_id TEXT;

-- 调整 usage_ledger 表：冗余 provider_id，支持按上游厂商维度聚合用量。
ALTER TABLE usage_ledger
  ADD COLUMN provider_id TEXT;

-- 调整 usage_ledger 表：冗余 virtual_key_id，支持按 Key 维度统计成本和调用量。
ALTER TABLE usage_ledger
  ADD COLUMN virtual_key_id TEXT;

-- 调整 usage_ledger 表：冗余 endpoint，保留 chat/embeddings 等接口维度。
ALTER TABLE usage_ledger
  ADD COLUMN endpoint TEXT;

-- 调整 usage_ledger 表：冗余 request_model，保留用户请求中的模型别名。
ALTER TABLE usage_ledger
  ADD COLUMN request_model TEXT;

-- 调整 usage_ledger 表：冗余 resolved_model，保留路由后实际内部模型 ID。
ALTER TABLE usage_ledger
  ADD COLUMN resolved_model TEXT;

-- 调整 usage_ledger 表：冗余 provider_code，支持 OpenRouter/硅基流动对标视角下的厂商统计。
ALTER TABLE usage_ledger
  ADD COLUMN provider_code TEXT;

-- 调整 usage_ledger 表：增加 updated_at，与其他业务表保持一致的变更追踪口径。
ALTER TABLE usage_ledger
  ADD COLUMN updated_at TEXT;

-- 回填 usage_ledger 旧数据：总 Token 缺失时使用输入与输出 Token 之和，保证历史记录可统计。
UPDATE usage_ledger
SET total_tokens=input_tokens + output_tokens
WHERE total_tokens = 0;

-- 回填 usage_ledger 旧数据：人民币账本把历史 cost_amount 同步到 cost_cny。
UPDATE usage_ledger
SET cost_cny=cost_amount
WHERE currency = 'CNY' AND cost_cny = 0;

-- 回填 usage_ledger 旧数据：美元账本把历史 cost_amount 同步到 cost_usd。
UPDATE usage_ledger
SET cost_usd=cost_amount
WHERE currency = 'USD' AND cost_usd = 0;

-- 回填 usage_ledger 旧数据：用创建时间补齐更新时间，避免控制台排序出现空值。
UPDATE usage_ledger
SET updated_at=created_at
WHERE updated_at IS NULL;

-- 回填 usage_ledger 旧数据：从 request_logs 补齐模型、Provider、Key 和接口维度，保持统计维度连续。
UPDATE usage_ledger
SET
  model_id=(SELECT rl.model_id FROM request_logs rl WHERE rl.tenant_id=usage_ledger.tenant_id AND rl.request_id=usage_ledger.request_id),
  provider_id=(SELECT rl.provider_id FROM request_logs rl WHERE rl.tenant_id=usage_ledger.tenant_id AND rl.request_id=usage_ledger.request_id),
  virtual_key_id=(SELECT rl.virtual_key_id FROM request_logs rl WHERE rl.tenant_id=usage_ledger.tenant_id AND rl.request_id=usage_ledger.request_id),
  endpoint=(SELECT rl.endpoint FROM request_logs rl WHERE rl.tenant_id=usage_ledger.tenant_id AND rl.request_id=usage_ledger.request_id),
  request_model=(SELECT rl.request_model FROM request_logs rl WHERE rl.tenant_id=usage_ledger.tenant_id AND rl.request_id=usage_ledger.request_id),
  resolved_model=(SELECT rl.resolved_model FROM request_logs rl WHERE rl.tenant_id=usage_ledger.tenant_id AND rl.request_id=usage_ledger.request_id),
  provider_code=(SELECT rl.provider_code FROM request_logs rl WHERE rl.tenant_id=usage_ledger.tenant_id AND rl.request_id=usage_ledger.request_id);

-- 创建 idx_price_snapshots_tenant_time 索引：支撑按租户追溯价格快照和排查价格变更影响。
CREATE INDEX IF NOT EXISTS idx_price_snapshots_tenant_time
  ON price_snapshots(tenant_id, created_at DESC);

-- 创建 idx_price_snapshots_request_id 索引：支撑从单次请求回查成本估算使用的价格快照。
CREATE INDEX IF NOT EXISTS idx_price_snapshots_request_id
  ON price_snapshots(request_id);

-- 创建 idx_usage_ledger_tenant_time 索引：支撑用量 API 按天和时间范围分页查询。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_tenant_time
  ON usage_ledger(tenant_id, created_at DESC);

-- 创建 idx_usage_ledger_model_time 索引：支撑按模型统计 Token 和成本。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_model_time
  ON usage_ledger(tenant_id, model_id, created_at DESC);

-- 创建 idx_usage_ledger_virtual_key_time 索引：支撑按 Virtual Key 统计 Token 和成本。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_virtual_key_time
  ON usage_ledger(tenant_id, virtual_key_id, created_at DESC);

-- 创建 idx_usage_ledger_estimated_time 索引：支撑估算记录与真实 usage 记录的区分查询。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_estimated_time
  ON usage_ledger(tenant_id, estimated, created_at DESC);
