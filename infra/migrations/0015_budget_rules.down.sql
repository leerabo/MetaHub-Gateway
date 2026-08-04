-- 迁移说明：0015_budget_rules.down 回滚 PRD-023 的预算规则、预算事件和日志摘要字段。

-- 回滚删除 idx_budget_evaluations_tenant_date 索引：先移除预算评估日期查询索引。
DROP INDEX IF EXISTS idx_budget_evaluations_tenant_date;

-- 回滚删除 idx_budget_evaluations_request_id 索引：再移除预算评估日志详情查询索引。
DROP INDEX IF EXISTS idx_budget_evaluations_request_id;

-- 回滚删除 idx_budget_alert_events_request_id 索引：移除预算事件 request_id 查询索引。
DROP INDEX IF EXISTS idx_budget_alert_events_request_id;

-- 回滚删除 idx_budget_alert_events_tenant_date 索引：移除预算事件日期查询索引。
DROP INDEX IF EXISTS idx_budget_alert_events_tenant_date;

-- 回滚删除 idx_budget_rules_status 索引：移除预算规则状态筛选索引。
DROP INDEX IF EXISTS idx_budget_rules_status;

-- 回滚删除 idx_budget_rules_tenant_scope 索引：移除预算规则租户 scope 查询索引。
DROP INDEX IF EXISTS idx_budget_rules_tenant_scope;

-- 回滚删除 budget_evaluations 表：PRD-023 之前不存在预算命中解释记录。
DROP TABLE IF EXISTS budget_evaluations;

-- 回滚删除 budget_alert_events 表：PRD-023 之前不存在预算阈值事件去重表。
DROP TABLE IF EXISTS budget_alert_events;

-- 回滚删除 budget_rules 表：PRD-023 之前不存在预算规则配置。
DROP TABLE IF EXISTS budget_rules;

-- 回滚删除 request_logs.budget_limit_cny 字段：恢复到预算摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN budget_limit_cny;

-- 回滚删除 request_logs.budget_current_cost_cny 字段：恢复到预算摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN budget_current_cost_cny;

-- 回滚删除 request_logs.budget_reason_code 字段：恢复到预算摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN budget_reason_code;

-- 回滚删除 request_logs.budget_action 字段：恢复到预算摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN budget_action;

-- 回滚删除 request_logs.budget_scope_id 字段：恢复到预算摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN budget_scope_id;

-- 回滚删除 request_logs.budget_scope_type 字段：恢复到预算摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN budget_scope_type;

-- 回滚删除 request_logs.budget_rule_id 字段：恢复到预算摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN budget_rule_id;

-- 回滚删除 request_logs.budget_status 字段：恢复到预算摘要写入前的调用日志结构。
ALTER TABLE request_logs DROP COLUMN budget_status;
