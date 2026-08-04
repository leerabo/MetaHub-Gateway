-- 迁移说明：0019_i18n_user_preferences.down 回滚 PRD-027 的语言偏好字段和 locale 变更事件表。

-- 回滚删除用户偏好事件用户时间索引：先移除依赖 user_preference_events 的索引。
DROP INDEX IF EXISTS idx_user_preference_events_user_time;

-- 回滚删除用户偏好事件租户时间索引：先移除依赖 user_preference_events 的索引。
DROP INDEX IF EXISTS idx_user_preference_events_tenant_time;

-- 回滚删除用户偏好事件表：PRD-027 前不存在 user.locale_changed 事件账本。
DROP TABLE IF EXISTS user_preference_events;

-- 迁移前清理临时表：若上次回滚中断，先移除临时表，避免后续 CREATE 失败。
DROP TABLE IF EXISTS user_preferences_prd027_old;

-- 创建 user_preferences_prd027_old 表：恢复 PRD-018 的控制台偏好结构，不再保存 locale。
CREATE TABLE user_preferences_prd027_old (
  preference_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  active_navigation_item_id TEXT NOT NULL DEFAULT 'overview',
  sidebar_collapsed INTEGER NOT NULL DEFAULT 0 CHECK (sidebar_collapsed IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  UNIQUE (tenant_id, user_id)
);

-- 复制用户偏好旧字段：回滚时保留导航与侧边栏状态，丢弃 PRD-027 新增的语言偏好。
INSERT INTO user_preferences_prd027_old(
  preference_id, tenant_id, user_id, active_navigation_item_id, sidebar_collapsed, created_at, updated_at
)
SELECT
  preference_id, tenant_id, user_id, active_navigation_item_id, sidebar_collapsed, created_at, updated_at
FROM user_preferences;

-- 替换 user_preferences 表：移除 locale 字段并恢复旧 CHECK/UNIQUE 结构。
DROP TABLE user_preferences;

-- 重命名旧结构表：保持应用层表名不变。
ALTER TABLE user_preferences_prd027_old RENAME TO user_preferences;

-- 重建 idx_user_preferences_tenant_user 索引：恢复 PRD-018 的偏好读取性能。
CREATE INDEX IF NOT EXISTS idx_user_preferences_tenant_user
  ON user_preferences(tenant_id, user_id);
