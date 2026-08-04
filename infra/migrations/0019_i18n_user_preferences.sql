-- 迁移说明：0019_i18n_user_preferences 为 PRD-027 增加用户语言偏好和 locale 变更事件。

-- 扩展 user_preferences.locale 字段：默认中文，M0 只允许中文和英文，不保存浏览器原始 Accept-Language。
ALTER TABLE user_preferences
  ADD COLUMN locale TEXT NOT NULL DEFAULT 'zh-CN' CHECK (locale IN ('zh-CN', 'en-US'));

-- 创建 user_preference_events 表：记录用户语言变更事件，事件 code 保持稳定英文，不保存本地化展示文本。
CREATE TABLE IF NOT EXISTS user_preference_events (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK (event_type = 'user.locale_changed'),
  old_locale TEXT,
  new_locale TEXT NOT NULL CHECK (new_locale IN ('zh-CN', 'en-US')),
  request_id TEXT NOT NULL,
  trace_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- 创建 idx_user_preference_events_tenant_time 索引：支撑按租户审计用户偏好变更。
CREATE INDEX IF NOT EXISTS idx_user_preference_events_tenant_time
  ON user_preference_events(tenant_id, created_at DESC);

-- 创建 idx_user_preference_events_user_time 索引：支撑按用户排查语言切换历史。
CREATE INDEX IF NOT EXISTS idx_user_preference_events_user_time
  ON user_preference_events(tenant_id, user_id, created_at DESC);
