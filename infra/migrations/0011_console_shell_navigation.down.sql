-- 迁移说明：0011_console_shell_navigation.down 回滚 PRD-018 的控制台偏好和页面访问事件表。

-- 回滚删除 idx_console_page_events_item_time 索引：先移除依赖表的查询索引。
DROP INDEX IF EXISTS idx_console_page_events_item_time;

-- 回滚删除 idx_console_page_events_user_time 索引：先移除依赖表的查询索引。
DROP INDEX IF EXISTS idx_console_page_events_user_time;

-- 回滚删除 idx_console_page_events_tenant_time 索引：先移除依赖表的查询索引。
DROP INDEX IF EXISTS idx_console_page_events_tenant_time;

-- 回滚删除 idx_user_preferences_tenant_user 索引：先移除依赖表的查询索引。
DROP INDEX IF EXISTS idx_user_preferences_tenant_user;

-- 回滚删除 console_page_events 表：PRD-018 之前不存在控制台页面访问事件账本。
DROP TABLE IF EXISTS console_page_events;

-- 回滚删除 user_preferences 表：PRD-018 之前不存在控制台用户偏好持久化。
DROP TABLE IF EXISTS user_preferences;
