-- 迁移职责：为 PRD-040 增加迁移指南访问事件，并在单个写锁事务内保留全部历史。
BEGIN IMMEDIATE;

-- 清理异常执行可能留下的目标表；该删除也在事务内，失败时会整体撤销。
DROP TABLE IF EXISTS console_page_events_prd040_new;

-- 创建新事件表：仅扩展事件枚举，不改变字段、外键和审计语义。
CREATE TABLE console_page_events_prd040_new (
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

-- 复制全部历史事件：租户、用户、会话、路径和统一请求链路必须原样保留。
INSERT INTO console_page_events_prd040_new(
  event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
)
SELECT
  event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
FROM console_page_events;

-- 删除旧表：复制、删除和重命名在同一立即事务中，不暴露半迁移状态。
DROP TABLE console_page_events;

-- 原子恢复稳定业务表名，服务层无需感知约束升级过程。
ALTER TABLE console_page_events_prd040_new RENAME TO console_page_events;

-- 恢复租户时间索引，继续支持按租户追踪控制台访问历史。
CREATE INDEX IF NOT EXISTS idx_console_page_events_tenant_time
  ON console_page_events(tenant_id, created_at DESC);

-- 恢复用户时间索引，继续支持在租户范围内定位用户访问轨迹。
CREATE INDEX IF NOT EXISTS idx_console_page_events_user_time
  ON console_page_events(tenant_id, user_id, created_at DESC);

-- 恢复导航入口索引，继续支持按入口统计页面访问事件。
CREATE INDEX IF NOT EXISTS idx_console_page_events_item_time
  ON console_page_events(tenant_id, navigation_item_id, created_at DESC);

COMMIT;
