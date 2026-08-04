-- 回滚职责：移除 PRD-041 事件枚举，同时保留 PRD-040 迁移指南事件和全部历史记录。
BEGIN IMMEDIATE;

-- 清理异常回滚可能留下的目标表；该操作随当前事务整体提交或撤销。
DROP TABLE IF EXISTS console_page_events_prd041_old;

-- 恢复 0029 的事件约束，迁移指南事件仍是合法且可追踪的一等事件。
CREATE TABLE console_page_events_prd041_old (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL DEFAULT 'console.page_viewed' CHECK (
    event_type IN ('console.page_viewed', 'dashboard.viewed', 'migration.guide_viewed')
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

-- 仅降低本 PRD 新增事件的名称精度，其他事件与审计字段全部原样保留。
INSERT INTO console_page_events_prd041_old(
  event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
)
SELECT
  event_id, tenant_id, user_id, session_id,
  CASE WHEN event_type = 'help.competitor_viewed' THEN 'console.page_viewed' ELSE event_type END,
  navigation_item_id, target_section, path, request_id, trace_id, payload_json, created_at
FROM console_page_events;

-- 原子恢复稳定表名与查询索引，回滚期间不暴露不完整结构。
DROP TABLE console_page_events;
-- 重命名恢复稳定业务表名，应用层无需感知回滚过程使用的临时表。
ALTER TABLE console_page_events_prd041_old RENAME TO console_page_events;

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
