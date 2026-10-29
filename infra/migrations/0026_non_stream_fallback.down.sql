-- PRD-036 确定性回滚：先删除依赖触发器和索引，再删除表及新增字段。
DROP TRIGGER IF EXISTS trg_usage_routing_attempt_insert;
DROP TRIGGER IF EXISTS trg_request_log_final_attempt_update;
DROP TRIGGER IF EXISTS trg_request_log_final_attempt_insert;
DROP TRIGGER IF EXISTS trg_routing_attempt_tenant_insert;
DROP TRIGGER IF EXISTS trg_fallback_policy_tenant_update;
DROP TRIGGER IF EXISTS trg_fallback_policy_tenant_insert;

-- 先解除 usage_ledger 到 attempt 的查询索引，才能安全撤销关联列。
DROP INDEX IF EXISTS idx_usage_ledger_routing_attempt;
-- 删除 attempt 状态与时间筛选索引，恢复 0025 的索引集合。
DROP INDEX IF EXISTS idx_routing_attempts_tenant_status_time;
-- 删除按别名查看切换时间线的复合索引。
DROP INDEX IF EXISTS idx_routing_attempts_tenant_alias_time;
-- 删除请求调用链的租户隔离查询索引。
DROP INDEX IF EXISTS idx_routing_attempts_tenant_request;
-- 删除策略状态筛选索引，随后才能移除策略表。
DROP INDEX IF EXISTS idx_fallback_policies_tenant_status;

-- attempts 先于 policies 删除，避免外键依赖倒置。
DROP TABLE IF EXISTS routing_attempts;
-- attempt 已移除后再删除策略，避免残留策略引用。
DROP TABLE IF EXISTS fallback_policies;

-- 新增列按引用依赖逆序删除，恢复 0025 的 request_logs 与 usage_ledger 结构。
ALTER TABLE usage_ledger DROP COLUMN routing_attempt_id;
-- 最终 attempt 外部标识先于计数和状态字段撤销。
ALTER TABLE request_logs DROP COLUMN final_routing_attempt_id;
-- 删除请求级 attempt 次数摘要，详细表已在前面移除。
ALTER TABLE request_logs DROP COLUMN routing_attempt_count;
-- 最后删除请求是否触发 Fallback 的布尔摘要。
ALTER TABLE request_logs DROP COLUMN fallback_triggered;
