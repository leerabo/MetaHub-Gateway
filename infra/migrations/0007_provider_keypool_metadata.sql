-- 迁移说明：0007_provider_keypool_metadata 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


PRAGMA foreign_keys=off;

-- 回滚删除 idx_provider_credentials_tenant_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_provider_credentials_tenant_id;
-- 回滚删除 idx_provider_credentials_provider_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_provider_credentials_provider_id;

CREATE TABLE provider_credentials_new (
  provider_credential_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  key_fingerprint TEXT NOT NULL,
  encrypted_key TEXT NOT NULL,
  name TEXT,
  key_prefix TEXT,
  weight INTEGER NOT NULL DEFAULT 100 CHECK (weight > 0 AND weight <= 10000),
  status TEXT NOT NULL CHECK (status IN ('active', 'disabled', 'rate_limited')),
  last_used_at TEXT,
  last_error_at TEXT,
  last_error_code TEXT,
  last_error_message TEXT,
  rate_limited_until TEXT,
  created_by TEXT,
  disabled_at TEXT,
  disabled_by TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (provider_id) REFERENCES providers(provider_id)
);

-- 写入种子数据：保持基础角色、租户或字典项可重复初始化。
INSERT INTO provider_credentials_new(
  provider_credential_id, tenant_id, provider_id, key_fingerprint, encrypted_key,
  name, key_prefix, weight, status, last_used_at, last_error_at, last_error_code,
  last_error_message, rate_limited_until, created_by, disabled_at, disabled_by,
  created_at, updated_at
)
SELECT
  provider_credential_id, tenant_id, provider_id, key_fingerprint, encrypted_key,
  NULL, NULL, 100, status, NULL, NULL, NULL,
  NULL, NULL, NULL, NULL, NULL,
  created_at, updated_at
FROM provider_credentials;

DROP TABLE provider_credentials;
-- 调整 provider_credentials_new 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE provider_credentials_new RENAME TO provider_credentials;

-- 创建 idx_provider_credentials_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_provider_credentials_tenant_id ON provider_credentials(tenant_id);
-- 创建 idx_provider_credentials_provider_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_provider_credentials_provider_id ON provider_credentials(provider_id);
-- 创建 idx_provider_credentials_status 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_provider_credentials_status ON provider_credentials(tenant_id, provider_id, status);
-- 创建 idx_provider_credentials_rate_limit 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_provider_credentials_rate_limit ON provider_credentials(tenant_id, provider_id, rate_limited_until);
CREATE UNIQUE INDEX IF NOT EXISTS uq_provider_credentials_fingerprint
  ON provider_credentials(tenant_id, provider_id, key_fingerprint);

PRAGMA foreign_keys=on;
