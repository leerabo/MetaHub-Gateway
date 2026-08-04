-- 迁移说明：0014_virtual_key_rate_limit_rules 为 PRD-022 增加 Virtual Key 级限流规则配置。

-- 创建 virtual_key_rate_limit_rules 表：只保存限流规则，不保存热路径计数器，计数器由 Redis 负责。
CREATE TABLE IF NOT EXISTS virtual_key_rate_limit_rules (
  rate_limit_rule_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  virtual_key_id TEXT NOT NULL,
  rpm_limit INTEGER CHECK (rpm_limit IS NULL OR rpm_limit > 0),
  tpm_limit INTEGER CHECK (tpm_limit IS NULL OR tpm_limit > 0),
  concurrency_limit INTEGER CHECK (concurrency_limit IS NULL OR concurrency_limit > 0),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  created_by TEXT,
  updated_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id),
  FOREIGN KEY (created_by) REFERENCES users(user_id),
  FOREIGN KEY (updated_by) REFERENCES users(user_id),
  UNIQUE (tenant_id, virtual_key_id)
);

-- 创建 idx_virtual_key_rate_limit_rules_tenant_id 索引：支撑控制面按租户加载 Key 限制设置。
CREATE INDEX IF NOT EXISTS idx_virtual_key_rate_limit_rules_tenant_id
  ON virtual_key_rate_limit_rules(tenant_id);

-- 创建 idx_virtual_key_rate_limit_rules_virtual_key_id 索引：支撑数据面按 Virtual Key 快速读取限流规则。
CREATE INDEX IF NOT EXISTS idx_virtual_key_rate_limit_rules_virtual_key_id
  ON virtual_key_rate_limit_rules(tenant_id, virtual_key_id);
