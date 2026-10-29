-- 迁移说明：0005_provider_metadata 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers
  ADD COLUMN provider_type TEXT NOT NULL DEFAULT 'openai_compatible'
  CHECK (provider_type IN (
    'openai_compatible',
    'zhipu',
    'dashscope',
    'wenxin',
    'volcengine',
    'hunyuan',
    'deepseek',
    'custom',
    'migration_compatible'
  ));

-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers
  ADD COLUMN auth_type TEXT NOT NULL DEFAULT 'bearer'
  CHECK (auth_type IN ('none', 'bearer', 'custom_header'));

-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers
  ADD COLUMN description TEXT;

-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers
  ADD COLUMN health_status TEXT NOT NULL DEFAULT 'unknown'
  CHECK (health_status IN ('unknown', 'healthy', 'unhealthy'));

-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers
  ADD COLUMN last_tested_at TEXT;

-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers
  ADD COLUMN last_test_error TEXT;

-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers
  ADD COLUMN created_by TEXT;

-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers
  ADD COLUMN disabled_at TEXT;

-- 调整 providers 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE providers
  ADD COLUMN disabled_by TEXT;

-- 创建 idx_providers_provider_type 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_providers_provider_type ON providers(tenant_id, provider_type);
-- 创建 idx_providers_health_status 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_providers_health_status ON providers(tenant_id, health_status);
