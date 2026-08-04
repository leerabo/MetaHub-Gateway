-- 迁移说明：0018_dashboard_overview 为 PRD-026 增加总览 Dashboard 查询索引，并扩展控制台事件类型支持 dashboard.viewed。

-- 迁移前清理临时表：若上次迁移中断，先移除临时表，避免后续 CREATE 失败。
DROP TABLE IF EXISTS console_page_events_prd026_new;

-- 创建 console_page_events_prd026_new 表：SQLite 不能直接修改 CHECK 约束，因此通过重建表加入 dashboard.viewed。
CREATE TABLE console_page_events_prd026_new (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL DEFAULT 'console.page_viewed' CHECK (event_type IN ('console.page_viewed', 'dashboard.viewed')),
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

-- 复制旧控制台事件：保留历史 page_view 事件，payload 已在服务层过滤为安全字段。
INSERT INTO console_page_events_prd026_new(
  event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
)
SELECT
  event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
FROM console_page_events;

-- 替换旧 console_page_events 表：完成 CHECK 约束升级，后续 Dashboard 访问可独立审计为 dashboard.viewed。
DROP TABLE console_page_events;

-- 重命名新控制台事件表：保持应用层表名不变，避免控制台服务层改动历史访问路径。
ALTER TABLE console_page_events_prd026_new RENAME TO console_page_events;

-- 创建 idx_console_page_events_tenant_time 索引：恢复按租户时间审计控制台访问轨迹的查询能力。
CREATE INDEX IF NOT EXISTS idx_console_page_events_tenant_time
  ON console_page_events(tenant_id, created_at DESC);

-- 创建 idx_console_page_events_user_time 索引：恢复按用户排查控制台访问路径的查询能力。
CREATE INDEX IF NOT EXISTS idx_console_page_events_user_time
  ON console_page_events(tenant_id, user_id, created_at DESC);

-- 创建 idx_console_page_events_item_time 索引：恢复按导航入口统计控制台使用情况的查询能力。
CREATE INDEX IF NOT EXISTS idx_console_page_events_item_time
  ON console_page_events(tenant_id, navigation_item_id, created_at DESC);

-- 创建 idx_request_logs_dashboard_latency 索引：支撑 Dashboard 今日 P95 延迟计算，查询始终带 tenant_id 和时间窗口。
CREATE INDEX IF NOT EXISTS idx_request_logs_dashboard_latency
  ON request_logs(tenant_id, created_at, latency_ms);

-- 创建 idx_request_logs_dashboard_model_time 索引：支撑 Dashboard Top 模型按租户和今日时间窗口聚合。
CREATE INDEX IF NOT EXISTS idx_request_logs_dashboard_model_time
  ON request_logs(tenant_id, resolved_model, created_at DESC);

-- 创建 idx_request_logs_dashboard_key_time 索引：支撑 Dashboard Top Virtual Key 按租户和今日时间窗口聚合。
CREATE INDEX IF NOT EXISTS idx_request_logs_dashboard_key_time
  ON request_logs(tenant_id, virtual_key_id, created_at DESC);