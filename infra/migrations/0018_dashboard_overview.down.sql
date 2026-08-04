-- 迁移说明：0018_dashboard_overview.down 回滚 PRD-026 的 Dashboard 索引和 dashboard.viewed 事件类型扩展。

-- 回滚删除 Dashboard Key 聚合索引：索引仅用于 PRD-026 总览页，可安全移除。
DROP INDEX IF EXISTS idx_request_logs_dashboard_key_time;

-- 回滚删除 Dashboard 模型聚合索引：索引仅用于 PRD-026 总览页，可安全移除。
DROP INDEX IF EXISTS idx_request_logs_dashboard_model_time;

-- 回滚删除 Dashboard 延迟索引：索引仅用于 PRD-026 P95 延迟计算，可安全移除。
DROP INDEX IF EXISTS idx_request_logs_dashboard_latency;

-- 迁移前清理临时表：若上次回滚中断，先移除临时表，避免后续 CREATE 失败。
DROP TABLE IF EXISTS console_page_events_prd026_old;

-- 创建 console_page_events_prd026_old 表：恢复 PRD-018 只允许 console.page_viewed 的事件类型约束。
CREATE TABLE console_page_events_prd026_old (
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

-- 复制控制台事件并降级 dashboard.viewed：回滚后仍保留访问轨迹，但事件类型恢复为 console.page_viewed。
INSERT INTO console_page_events_prd026_old(
  event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
)
SELECT
  event_id, tenant_id, user_id, session_id,
  CASE WHEN event_type = 'dashboard.viewed' THEN 'console.page_viewed' ELSE event_type END,
  navigation_item_id, target_section, path, request_id, trace_id, payload_json, created_at
FROM console_page_events;

-- 替换控制台事件表：恢复旧 CHECK 约束，保留已脱敏的历史 payload。
DROP TABLE console_page_events;

-- 重命名旧结构表：保持应用层表名不变。
ALTER TABLE console_page_events_prd026_old RENAME TO console_page_events;

-- 重建 idx_console_page_events_tenant_time 索引：恢复 PRD-018 的控制台访问审计性能。
CREATE INDEX IF NOT EXISTS idx_console_page_events_tenant_time
  ON console_page_events(tenant_id, created_at DESC);

-- 重建 idx_console_page_events_user_time 索引：恢复 PRD-018 的用户访问路径查询性能。
CREATE INDEX IF NOT EXISTS idx_console_page_events_user_time
  ON console_page_events(tenant_id, user_id, created_at DESC);

-- 重建 idx_console_page_events_item_time 索引：恢复 PRD-018 的导航入口统计查询性能。
CREATE INDEX IF NOT EXISTS idx_console_page_events_item_time
  ON console_page_events(tenant_id, navigation_item_id, created_at DESC);