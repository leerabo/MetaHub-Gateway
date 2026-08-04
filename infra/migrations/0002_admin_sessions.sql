-- 迁移说明：0002_admin_sessions 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 调整 users 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE users ADD COLUMN failed_login_count INTEGER NOT NULL DEFAULT 0;
-- 调整 users 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE users ADD COLUMN locked_until TEXT;
-- 调整 users 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE users ADD COLUMN last_login_at TEXT;
-- 调整 users 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE users ADD COLUMN password_changed_at TEXT;

-- 创建 sessions 表：保存控制台登录 Session、Token 哈希和过期状态，不保存明文 Token。
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  token_jti TEXT NOT NULL UNIQUE,
  token_hash TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('active', 'revoked', 'expired')),
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  last_seen_at TEXT,
  user_agent TEXT,
  created_ip TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- 创建 idx_users_locked_until 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_users_locked_until ON users(locked_until);
-- 创建 idx_sessions_tenant_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_sessions_tenant_id ON sessions(tenant_id);
-- 创建 idx_sessions_user_id 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
-- 创建 idx_sessions_status_expires_at 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_sessions_status_expires_at ON sessions(status, expires_at);
-- 创建 idx_sessions_token_jti 索引：支撑高频租户内查询，避免控制台分页和详情检索退化为全表扫描。
CREATE INDEX IF NOT EXISTS idx_sessions_token_jti ON sessions(token_jti);
