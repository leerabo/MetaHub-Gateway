-- 回滚说明：移除 PRD-031 有效期价格计划，保留 PRD-017 原有每请求价格快照和 Usage 成本。

PRAGMA foreign_keys=off;
BEGIN IMMEDIATE;

-- 索引删除说明：先移除引用新增快照字段的索引，才能安全删除对应列。
DROP INDEX IF EXISTS idx_price_snapshots_price_plan;

-- 快照字段回滚：删除价格计划外键，原有单价、币种和版本快照继续保留。
ALTER TABLE price_snapshots DROP COLUMN price_plan_id;

-- 快照字段回滚：删除计划生效时间冗余字段，不修改任何历史 Usage 成本。
ALTER TABLE price_snapshots DROP COLUMN effective_from;

-- 事件索引删除说明：移除价格审计时间索引后再删除事件表。
DROP INDEX IF EXISTS idx_model_price_events_tenant_time;

-- 计划索引删除说明：移除租户创建时间和生效时间查询索引。
DROP INDEX IF EXISTS idx_model_price_plans_tenant_created;
-- 生效索引删除说明：移除 Gateway 按模型和时间选择价格计划的查询索引。
DROP INDEX IF EXISTS idx_model_price_plans_effective;

-- 事件表删除说明：价格事件仅属于 PRD-031，回滚时随价格计划功能删除。
DROP TABLE IF EXISTS model_price_events;

-- 计划表删除说明：原模型目录兼容价格字段仍存在，回滚后网关可继续使用旧价格。
DROP TABLE IF EXISTS model_price_plans;

COMMIT;
PRAGMA foreign_keys=on;
