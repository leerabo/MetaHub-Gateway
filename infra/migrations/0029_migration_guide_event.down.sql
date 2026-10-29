-- 回滚职责：移除迁移指南事件枚举，并把历史降级为普通页面访问而不删除记录。
BEGIN IMMEDIATE;

-- 清理异常回滚可能留下的目标表；该操作随当前事务整体提交或撤销。
DROP TABLE IF EXISTS console_page_events_prd040_old;

-- 创建回滚目标表：恢复仅允许普通页面与 Dashboard 两类事件的约束。
CREATE TABLE console_page_events_prd040_old (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL DEFAULT 'console.page_viewed' CHECK (
    event_type IN ('console.page_viewed', 'dashboard.viewed')
  ),
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

-- 复制全部历史并只降低事件名称精度；租户、用户、路径和请求链路均保持不变。
INSERT INTO console_page_events_prd040_old(
  event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
)
SELECT
  event_id, tenant_id, user_id, session_id,
  CASE WHEN event_type = 'migration.guide_viewed' THEN 'console.page_viewed' ELSE event_type END,
  navigation_item_id, target_section, path, request_id, trace_id, payload_json, created_at
FROM console_page_events;

-- 删除新约束表：立即事务保证并发连接不会看到不完整回滚结构。
DROP TABLE console_page_events;

-- 恢复稳定业务表名，使既有查询继续读取保留下来的全部历史。
ALTER TABLE console_page_events_prd040_old RENAME TO console_page_events;

-- 恢复租户时间索引，回滚后仍可按租户审计访问历史。
CREATE INDEX IF NOT EXISTS idx_console_page_events_tenant_time
  ON console_page_events(tenant_id, created_at DESC);

-- 恢复用户时间索引，回滚后仍可按用户定位访问轨迹。
CREATE INDEX IF NOT EXISTS idx_console_page_events_user_time
  ON console_page_events(tenant_id, user_id, created_at DESC);

-- 恢复导航入口索引，回滚后仍可按页面入口聚合事件。
CREATE INDEX IF NOT EXISTS idx_console_page_events_item_time
  ON console_page_events(tenant_id, navigation_item_id, created_at DESC);

COMMIT;
