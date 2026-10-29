-- 迁移说明：0004_virtual_key_metadata 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys
  ADD COLUMN environment TEXT NOT NULL DEFAULT 'dev' CHECK (environment IN ('dev', 'test', 'prod'));

-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys
  ADD COLUMN description TEXT;

-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys
  ADD COLUMN last_used_at TEXT;

-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys
  ADD COLUMN created_by TEXT;

-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys
  ADD COLUMN disabled_at TEXT;

-- 调整 virtual_keys 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE virtual_keys
  ADD COLUMN disabled_by TEXT;

-- 创建 idx_virtual_keys_environment 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_virtual_keys_environment ON virtual_keys(tenant_id, environment);
-- 创建 idx_virtual_keys_last_used_at 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_virtual_keys_last_used_at ON virtual_keys(tenant_id, last_used_at);
