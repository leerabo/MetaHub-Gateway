-- 回滚说明：移除 PRD-033 BudgetBlock 生命周期和事件事实，不修改预算规则、Usage 或调用日志。

PRAGMA foreign_keys=off;
BEGIN IMMEDIATE;

-- 先删除归属触发器，避免回滚期间保留对即将删除表的引用。
DROP TRIGGER IF EXISTS trg_budget_reservations_owner_update;
DROP TRIGGER IF EXISTS trg_budget_reservations_owner_insert;
DROP TRIGGER IF EXISTS trg_budget_block_events_owner_update;
DROP TRIGGER IF EXISTS trg_budget_block_events_owner_insert;
DROP TRIGGER IF EXISTS trg_budget_blocks_owner_update;
DROP TRIGGER IF EXISTS trg_budget_blocks_owner_insert;

-- 删除预留查询索引及表；预留是 0023 引入的准入事实，不影响历史 Usage 账本。
DROP INDEX IF EXISTS idx_budget_reservations_request;
-- 规则日期索引删除说明：移除按租户、规则和业务日汇总未结算预留的索引。
DROP INDEX IF EXISTS idx_budget_reservations_rule_date;
-- 预留表删除说明：回滚后恢复为只读取 Usage Ledger 的 PRD-023 预算口径。
DROP TABLE IF EXISTS budget_reservations;

-- 事件索引删除说明：先移除依赖阻断事件表的租户时间索引。
DROP INDEX IF EXISTS idx_budget_block_events_tenant_time;

-- 阻断索引删除说明：依次移除 Environment、规则状态和 active 唯一索引。
DROP INDEX IF EXISTS idx_budget_blocks_environment_date;
-- 规则状态索引删除说明：移除控制面按租户和规则查询 active block 的索引。
DROP INDEX IF EXISTS idx_budget_blocks_tenant_rule_status;
-- 唯一索引删除说明：回滚时取消同规则同业务日 active 生命周期约束。
DROP INDEX IF EXISTS idx_budget_blocks_one_active_rule_day;

-- 事件表删除说明：子表先删除，避免保留对 BudgetBlock 的外键依赖。
DROP TABLE IF EXISTS budget_block_events;

-- 生命周期表删除说明：回滚后数据面恢复 PRD-023 的实时预算比较行为。
DROP TABLE IF EXISTS budget_blocks;

COMMIT;
PRAGMA foreign_keys=on;
