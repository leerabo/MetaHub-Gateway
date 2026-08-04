-- 迁移说明：0010_token_usage_cost_estimation.down 回滚 PRD-017 用量成本扩展，恢复到 0009 的基础 usage_ledger 结构。

PRAGMA foreign_keys=off;

-- 回滚删除 idx_usage_ledger_estimated_time 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_usage_ledger_estimated_time;

-- 回滚删除 idx_usage_ledger_virtual_key_time 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_usage_ledger_virtual_key_time;

-- 回滚删除 idx_usage_ledger_model_time 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_usage_ledger_model_time;

-- 回滚删除 idx_usage_ledger_tenant_time 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_usage_ledger_tenant_time;

-- 回滚删除 idx_price_snapshots_request_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_price_snapshots_request_id;

-- 回滚删除 idx_price_snapshots_tenant_time 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_price_snapshots_tenant_time;

-- 回滚删除 idx_usage_ledger_request_id 索引：重建账本表前先移除旧索引引用。
DROP INDEX IF EXISTS idx_usage_ledger_request_id;

-- 回滚删除 idx_usage_ledger_tenant_id 索引：重建账本表前先移除旧索引引用。
DROP INDEX IF EXISTS idx_usage_ledger_tenant_id;

-- 创建 usage_ledger_old 表：保留 0009 阶段的基础用量账本字段。
CREATE TABLE usage_ledger_old (
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

-- 写入 usage_ledger_old：只保留上一阶段存在的列，丢弃 PRD-017 扩展字段。
INSERT INTO usage_ledger_old(
  usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
  raw_usage, normalized_usage, cost_amount, currency, price_schema_version, created_at
)
SELECT
  usage_ledger_id, tenant_id, request_id, input_tokens, output_tokens,
  raw_usage, normalized_usage, cost_amount, currency, price_schema_version, created_at
FROM usage_ledger;

-- 删除 usage_ledger 表：准备替换成上一阶段结构。
DROP TABLE usage_ledger;

-- 调整 usage_ledger_old 表：恢复 usage_ledger 原表名，并保证后续索引重建仍引用标准账本表。
ALTER TABLE usage_ledger_old RENAME TO usage_ledger;

-- 创建 idx_usage_ledger_tenant_id 索引：恢复 0009 阶段租户查询索引。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_tenant_id ON usage_ledger(tenant_id);

-- 创建 idx_usage_ledger_request_id 索引：恢复 0009 阶段请求查询索引。
CREATE INDEX IF NOT EXISTS idx_usage_ledger_request_id ON usage_ledger(request_id);

-- 回滚删除 price_snapshots 表：PRD-017 之前没有价格快照物理表。
DROP TABLE IF EXISTS price_snapshots;

PRAGMA foreign_keys=on;
