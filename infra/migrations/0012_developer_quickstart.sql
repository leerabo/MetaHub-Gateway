-- 迁移说明：0012_developer_quickstart 为 PRD-019 增加开发者门户事件表，支撑快速开始页访问审计和后续 Webhook 扩展。

-- 创建 developer_portal_events 表：记录开发者门户低频页面事件，不保存密钥、Prompt、Response 或示例代码全文。
CREATE TABLE IF NOT EXISTS developer_portal_events (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL CHECK (event_type IN ('developer.quickstart_viewed')),
  object_id TEXT NOT NULL,
  path TEXT NOT NULL,
  request_id TEXT NOT NULL,
  trace_id TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

-- 创建 idx_developer_portal_events_tenant_time 索引：支撑按租户审计开发者门户使用轨迹。
CREATE INDEX IF NOT EXISTS idx_developer_portal_events_tenant_time
  ON developer_portal_events(tenant_id, created_at DESC);

-- 创建 idx_developer_portal_events_user_time 索引：支撑按开发者排查快速开始访问路径。
CREATE INDEX IF NOT EXISTS idx_developer_portal_events_user_time
  ON developer_portal_events(tenant_id, user_id, created_at DESC);

-- 创建 idx_developer_portal_events_type_time 索引：支撑后续按事件类型统计开发者门户功能使用情况。
CREATE INDEX IF NOT EXISTS idx_developer_portal_events_type_time
  ON developer_portal_events(tenant_id, event_type, created_at DESC);
