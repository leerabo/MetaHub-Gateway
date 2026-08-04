-- 迁移职责：为 PRD-041 增加竞品对比页访问事件，并完整保留既有审计历史。
BEGIN IMMEDIATE;

-- 清理异常执行可能留下的目标表；删除操作也在当前事务内，失败时会整体撤销。
DROP TABLE IF EXISTS console_page_events_prd041_new;

-- 仅扩展事件类型枚举，不改变租户、用户、会话和请求链路字段的语义。
CREATE TABLE console_page_events_prd041_new (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  session_id TEXT,
  event_type TEXT NOT NULL DEFAULT 'console.page_viewed' CHECK (
    event_type IN (
      'console.page_viewed',
      'dashboard.viewed',
      'migration.guide_viewed',
      'help.competitor_viewed'
    )
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

-- 复制全部历史事件；事件名称、租户边界、路径和 request_id 必须原样保留。
INSERT INTO console_page_events_prd041_new(
  event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
)
SELECT
  event_id, tenant_id, user_id, session_id, event_type, navigation_item_id,
  target_section, path, request_id, trace_id, payload_json, created_at
FROM console_page_events;

-- 表替换与索引恢复处于同一个立即事务，避免服务看到半迁移结构。
DROP TABLE console_page_events;
-- 重命名恢复稳定业务表名，应用层无需感知约束升级使用的临时表。
ALTER TABLE console_page_events_prd041_new RENAME TO console_page_events;

-- 恢复三个既有查询索引，保证迁移不降低租户审计和页面统计能力。
CREATE INDEX IF NOT EXISTS idx_console_page_events_tenant_time
  ON console_page_events(tenant_id, created_at DESC);
-- 恢复用户时间索引，继续支持租户内用户访问轨迹定位。
CREATE INDEX IF NOT EXISTS idx_console_page_events_user_time
  ON console_page_events(tenant_id, user_id, created_at DESC);
-- 恢复导航入口索引，继续支持页面访问事件聚合。
CREATE INDEX IF NOT EXISTS idx_console_page_events_item_time
  ON console_page_events(tenant_id, navigation_item_id, created_at DESC);

COMMIT;
