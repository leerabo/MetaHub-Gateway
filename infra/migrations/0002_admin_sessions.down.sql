-- 迁移说明：0002_admin_sessions.down 属于 AI Gateway MVP 数据库演进，所有结构变更都要保持租户隔离、状态可追踪和回滚可读。


-- 回滚删除 idx_sessions_token_jti 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_sessions_token_jti;
-- 回滚删除 idx_sessions_status_expires_at 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_sessions_status_expires_at;
-- 回滚删除 idx_sessions_user_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_sessions_user_id;
-- 回滚删除 idx_sessions_tenant_id 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_sessions_tenant_id;
-- 回滚删除 idx_users_locked_until 索引：恢复到上一阶段查询结构。
DROP INDEX IF EXISTS idx_users_locked_until;

-- 回滚删除 sessions 表：仅用于本地或测试迁移回退，生产环境需先确认数据备份。
DROP TABLE IF EXISTS sessions;

-- 调整 users 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE users DROP COLUMN password_changed_at;
-- 调整 users 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE users DROP COLUMN last_login_at;
-- 调整 users 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE users DROP COLUMN locked_until;
-- 调整 users 表：为后续 PRD 补充可追踪字段，旧数据通过默认值保持兼容。
ALTER TABLE users DROP COLUMN failed_login_count;
