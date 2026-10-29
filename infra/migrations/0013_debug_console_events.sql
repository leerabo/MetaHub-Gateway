-- 迁移说明：0013_debug_console_events 为 PRD-020 增加内置 API 调试台事件表，用于审计控制台发起的低频调试请求。
-- 创建 debug_console_events 表：只保存调试请求的路由、Trace、成本定位元数据，不保存 Prompt、Response、Virtual Key 明文或 Provider Key。
CREATE TABLE IF NOT EXISTS debug_console_events (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL CHECK (event_type IN ('debug.request_sent')),
  debug_request_id TEXT NOT NULL,
  gateway_request_id TEXT NOT NULL,
  trace_id TEXT,
  virtual_key_id TEXT,
  model_alias TEXT,
  status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
  error_code TEXT,
  latency_ms INTEGER,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY (tenant_id) REFERENCES tenants(tenant_id),
  FOREIGN KEY (user_id) REFERENCES users(user_id),
  FOREIGN KEY (session_id) REFERENCES sessions(session_id),
  FOREIGN KEY (virtual_key_id) REFERENCES virtual_keys(virtual_key_id)
);

-- 创建 idx_debug_console_events_tenant_time 索引：支持租户维度审计调试台使用轨迹和近期失败请求。
CREATE INDEX IF NOT EXISTS idx_debug_console_events_tenant_time
  ON debug_console_events(tenant_id, created_at DESC);

-- 创建 idx_debug_console_events_request_id 索引：支持从 request_id 反查调试台事件并跳转调用日志详情。
CREATE INDEX IF NOT EXISTS idx_debug_console_events_request_id
  ON debug_console_events(tenant_id, gateway_request_id);

-- 创建 idx_debug_console_events_user_time 索引：支持按开发者排查调试台请求来源和误操作。
CREATE INDEX IF NOT EXISTS idx_debug_console_events_user_time
  ON debug_console_events(tenant_id, user_id, created_at DESC);
