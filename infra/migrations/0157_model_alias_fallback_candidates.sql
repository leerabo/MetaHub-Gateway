-- PRD-148：将 Alias 的备用模型从单字段扩展为有序候选集合。
-- 旧 fallback_model_id 保留用于兼容历史读取和导出；新写入以本表为权威候选顺序。
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS model_alias_fallback_candidates (
  model_alias_fallback_candidate_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  model_alias_id TEXT NOT NULL,
  model_id TEXT NOT NULL,
  configured_priority INTEGER NOT NULL CHECK (
    typeof(configured_priority)='integer' AND configured_priority >= 1 AND configured_priority <= 32
  ),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (tenant_id, model_alias_id) REFERENCES model_aliases(tenant_id, model_alias_id),
  FOREIGN KEY (tenant_id, model_id) REFERENCES models(tenant_id, model_id),
  UNIQUE (tenant_id, model_alias_id, configured_priority),
  UNIQUE (tenant_id, model_alias_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_model_alias_fallback_candidates_alias
  ON model_alias_fallback_candidates(tenant_id, model_alias_id, configured_priority);

INSERT OR IGNORE INTO model_alias_fallback_candidates(
  model_alias_fallback_candidate_id, tenant_id, model_alias_id, model_id,
  configured_priority, created_at, updated_at
)
SELECT
  'mafc_' || lower(hex(randomblob(16))),
  tenant_id,
  model_alias_id,
  fallback_model_id,
  1,
  updated_at,
  updated_at
FROM model_aliases
WHERE fallback_model_id IS NOT NULL;

COMMIT;
