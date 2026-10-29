-- 迁移说明：0011_console_shell_navigation 为 PRD-018 增加控制台用户偏好和页面访问事件，支撑导航骨架、空状态和审计闭环。

-- 创建 user_preferences 表：按租户和用户保存最小 UI 偏好，不保存密钥、Prompt、Response 或任意业务查询上下文。
CREATE TABLE IF NOT EXISTS user_preferences (
  preference_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  active_navigation_item_id TEXT NOT NULL DEFAULT 'overview',
  sidebar_collapsed INTEGER NOT NULL DEFAULT 0 CHECK (sidebar_collapsed IN (0, 1)),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(tenant_id, user_id),
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- 创建 console_page_events 表：记录控制台页面访问事件，后续审计或 Webhook 模块可按租户读取。
CREATE TABLE IF NOT EXISTS console_page_events (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL DEFAULT 'console.page_viewed' CHECK (event_type IN ('console.page_viewed')),
  navigation_item_id TEXT NOT NULL,
  target_section TEXT NOT NULL,
  path TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- 创建 idx_user_preferences_tenant_user 索引：保障偏好读取始终绑定当前租户和当前用户。
CREATE INDEX IF NOT EXISTS idx_user_preferences_tenant_user
  ON user_preferences(tenant_id, user_id);

-- 创建 idx_console_page_events_tenant_time 索引：支撑后续按租户审计控制台页面访问轨迹。
CREATE INDEX IF NOT EXISTS idx_console_page_events_tenant_time
  ON console_page_events(tenant_id, created_at DESC);

-- 创建 idx_console_page_events_user_time 索引：支撑按用户排查控制台访问路径。
CREATE INDEX IF NOT EXISTS idx_console_page_events_user_time
  ON console_page_events(tenant_id, user_id, created_at DESC);

-- 创建 idx_console_page_events_item_time 索引：支撑按导航入口统计控制台使用情况。
CREATE INDEX IF NOT EXISTS idx_console_page_events_item_time
  ON console_page_events(tenant_id, navigation_item_id, created_at DESC);
