-- 迁移说明：0013_debug_console_events.down 回滚 PRD-020 的调试台事件审计表。
-- 回滚删除 idx_debug_console_events_user_time 索引：先移除依赖 debug_console_events 的用户维度查询索引。
DROP INDEX IF EXISTS idx_debug_console_events_user_time;

-- 回滚删除 idx_debug_console_events_request_id 索引：先移除依赖 debug_console_events 的 request_id 查询索引。
DROP INDEX IF EXISTS idx_debug_console_events_request_id;

-- 回滚删除 idx_debug_console_events_tenant_time 索引：先移除依赖 debug_console_events 的租户维度查询索引。
DROP INDEX IF EXISTS idx_debug_console_events_tenant_time;

-- 回滚删除 debug_console_events 表：PRD-020 之前不存在调试台事件账本。
DROP TABLE IF EXISTS debug_console_events;

