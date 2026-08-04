-- 迁移说明：0012_developer_quickstart.down 回滚 PRD-019 的开发者门户事件表。

-- 回滚删除 idx_developer_portal_events_type_time 索引：先移除依赖 developer_portal_events 的事件类型查询索引。
DROP INDEX IF EXISTS idx_developer_portal_events_type_time;

-- 回滚删除 idx_developer_portal_events_user_time 索引：先移除依赖 developer_portal_events 的用户查询索引。
DROP INDEX IF EXISTS idx_developer_portal_events_user_time;

-- 回滚删除 idx_developer_portal_events_tenant_time 索引：先移除依赖 developer_portal_events 的租户查询索引。
DROP INDEX IF EXISTS idx_developer_portal_events_tenant_time;

-- 回滚删除 developer_portal_events 表：PRD-019 之前不存在开发者门户快速开始事件账本。
DROP TABLE IF EXISTS developer_portal_events;
