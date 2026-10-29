-- PRD-035 确定性回滚：先删除依赖对象，再逆序删除新增字段。
DROP TRIGGER IF EXISTS trg_provider_health_log_tenant_insert;
-- 先删除日志查询索引，随后才能安全删除日志表。
DROP INDEX IF EXISTS idx_provider_health_logs_tenant_provider;
-- 删除 Provider 健康查询索引，恢复 0024 的索引集合。
DROP INDEX IF EXISTS idx_provider_health_tenant_status;
-- 删除调度领取索引，避免回滚后引用不存在的字段。
DROP INDEX IF EXISTS idx_provider_health_due;
-- 探测日志仅属于 PRD-035，回滚时整表删除且不影响业务调用日志。
DROP TABLE IF EXISTS provider_health_probe_logs;

-- Key 探测字段按依赖逆序删除，保留 0007 原始 KeyPool 元数据。
ALTER TABLE provider_credentials DROP COLUMN probe_consecutive_failures;
-- 删除 Key 最近错误分类字段，恢复旧 Key DTO 存储结构。
ALTER TABLE provider_credentials DROP COLUMN probe_last_error_code;
-- 删除 Key 最近探测成功时间字段。
ALTER TABLE provider_credentials DROP COLUMN probe_last_success_at;
-- 删除 Key 最近探测时间字段，回滚版本不再执行新鲜度过滤。
ALTER TABLE provider_credentials DROP COLUMN probe_last_tested_at;
-- 最后删除 Key 健康状态，避免前述字段依赖失序。
ALTER TABLE provider_credentials DROP COLUMN probe_health_status;

-- Provider 调度与聚合字段同样按依赖逆序删除。
ALTER TABLE providers DROP COLUMN probe_lease_until;
-- 删除租约令牌后，回滚版本不再执行周期健康领取。
ALTER TABLE providers DROP COLUMN probe_lease_token;
-- 删除下次探测排期，恢复为仅支持旧手工 test 的结构。
ALTER TABLE providers DROP COLUMN next_probe_at;
-- 删除新版证据时间，使旧 health_status 不再参与自动摘流。
ALTER TABLE providers DROP COLUMN health_updated_at;
-- 删除配置代际，回滚后不再提交新版探测状态机结果。
ALTER TABLE providers DROP COLUMN health_generation;
-- 删除聚合连续失败计数，恢复旧版单次手工测试状态结构。
ALTER TABLE providers DROP COLUMN consecutive_probe_failures;
-- 删除最近固定错误分类，旧版仅保留 last_test_error 安全摘要。
ALTER TABLE providers DROP COLUMN last_probe_error_code;
-- 删除最近 HTTP 状态字段，恢复 0024 Provider 表结构。
ALTER TABLE providers DROP COLUMN last_probe_http_status;
-- 删除最近探测延迟字段，恢复 0024 Provider 表结构。
ALTER TABLE providers DROP COLUMN last_probe_latency_ms;
-- 删除最近确定性失败时间，旧版不区分失败和聚合证据时间。
ALTER TABLE providers DROP COLUMN last_failure_at;
-- 删除最近成功时间，完成 0025 Provider 字段回滚。
ALTER TABLE providers DROP COLUMN last_success_at;
