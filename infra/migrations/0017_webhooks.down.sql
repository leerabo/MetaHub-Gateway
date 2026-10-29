-- 迁移说明：0017_webhooks.down 回滚 PRD-025 的 Webhook 订阅、投递和重试记录。
-- 回滚删除 idx_webhook_attempts_delivery 索引：先移除依赖 attempts 表的查询索引。
DROP INDEX IF EXISTS idx_webhook_attempts_delivery;

-- 回滚删除 idx_webhook_deliveries_status_time 索引：移除按失败状态排障的查询索引。
DROP INDEX IF EXISTS idx_webhook_deliveries_status_time;

-- 回滚删除 idx_webhook_deliveries_event 索引：移除按 event_id 回溯投递的查询索引。
DROP INDEX IF EXISTS idx_webhook_deliveries_event;

-- 回滚删除 idx_webhook_deliveries_endpoint_time 索引：移除发送记录页查询索引。
DROP INDEX IF EXISTS idx_webhook_deliveries_endpoint_time;

-- 回滚删除 idx_webhook_endpoints_tenant_secret 索引：移除 secret 指纹排障索引。
DROP INDEX IF EXISTS idx_webhook_endpoints_tenant_secret;

-- 回滚删除 idx_webhook_endpoints_tenant_status 索引：移除 Endpoint 列表筛选索引。
DROP INDEX IF EXISTS idx_webhook_endpoints_tenant_status;

-- 回滚删除 webhook_delivery_attempts 表：PRD-025 之前不存在逐次投递尝试记录。
DROP TABLE IF EXISTS webhook_delivery_attempts;

-- 回滚删除 webhook_deliveries 表：PRD-025 之前不存在 Webhook 投递摘要。
DROP TABLE IF EXISTS webhook_deliveries;

-- 回滚删除 webhook_endpoints 表：PRD-025 之前不存在租户级 Webhook Endpoint 配置。
DROP TABLE IF EXISTS webhook_endpoints;
