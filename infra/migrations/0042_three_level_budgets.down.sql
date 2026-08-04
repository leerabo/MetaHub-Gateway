-- PRD-052 条件回滚：只在没有 canonical 越限事实、订阅和投递时移除新增结构。
-- Application 预算和 0041 FinOps 正式账本都是兼容基线，本脚本不得删除或重建它们。
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

-- 结构门禁先确认当前数据库确为完整 0042，异常结构一律不尝试部分回滚。
CREATE TEMP TABLE prd052_down_prerequisite_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd052_down_prerequisite_guard
BEFORE INSERT ON prd052_down_prerequisite_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd052_down_prerequisite_missing');
END;
-- 汇总版本链和 0042 对象数量，结构不完整时禁止执行任何持久 DROP。
INSERT INTO prd052_down_prerequisite_guard(issue_count)
SELECT
  CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0042' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM schema_migrations
    WHERE version='0041' AND status='applied'
  )=1 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='table' AND name IN (
      'budget_scope_events', 'budget_rules', 'budget_alert_events',
      'budget_evaluations', 'budget_blocks', 'budget_reservations',
      'finops_ledger_entries', 'webhook_endpoints', 'webhook_deliveries'
    )
  )=9 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='index' AND name IN (
      'idx_budget_scope_events_tenant_date_prd052',
      'idx_finops_budget_tenant_usage_prd052',
      'idx_finops_budget_project_usage_prd052',
      'idx_finops_budget_application_usage_prd052',
      'idx_finops_budget_key_usage_prd052'
    )
  )=5 THEN 0 ELSE 1 END
  + CASE WHEN (
    SELECT COUNT(*) FROM sqlite_master
    WHERE type='trigger' AND name IN (
      'trg_budget_rules_prd052_owner_insert',
      'trg_budget_rules_prd052_owner_update',
      'trg_budget_rules_prd052_identity_update',
      'trg_budget_alert_events_prd052_owner_insert',
      'trg_budget_alert_events_prd052_owner_update',
      'trg_budget_evaluations_prd052_owner_insert',
      'trg_budget_evaluations_prd052_owner_update',
      'trg_budget_blocks_prd052_scope_insert',
      'trg_budget_blocks_prd052_scope_update',
      'trg_budget_reservations_prd052_scope_insert',
      'trg_budget_reservations_prd052_scope_update',
      'trg_budget_scope_events_prd052_owner_insert',
      'trg_budget_scope_events_prd052_immutable_update',
      'trg_budget_scope_events_prd052_immutable_delete'
    )
  )=14 THEN 0 ELSE 1 END;
DROP TRIGGER trg_prd052_down_prerequisite_guard;
DROP TABLE prd052_down_prerequisite_guard;

-- scope event 已可能进入异步队列；订阅或 delivery 也代表外部合同已生效，任一存在都原子拒绝。
CREATE TEMP TABLE prd052_down_business_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd052_down_business_guard
BEFORE INSERT ON prd052_down_business_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd052_down_business_facts_present');
END;
-- 同时检查数据库事实和已生效的外部 Webhook 合同，防止静默丢失可观测事件。
INSERT INTO prd052_down_business_guard(issue_count)
SELECT
  (SELECT COUNT(*) FROM budget_scope_events)
  + (SELECT COUNT(*)
     FROM webhook_endpoints endpoint
     WHERE CASE
       WHEN json_valid(endpoint.event_types_json)
        AND json_type(endpoint.event_types_json)='array'
       THEN EXISTS (
         SELECT 1 FROM json_each(endpoint.event_types_json) event_type
         WHERE event_type.type='text' AND event_type.value='budget.scope_exceeded'
       )
       ELSE 0
     END)
  + (SELECT COUNT(*) FROM webhook_deliveries delivery
     WHERE delivery.event_type='budget.scope_exceeded');
DROP TRIGGER trg_prd052_down_business_guard;
DROP TABLE prd052_down_business_guard;

-- 先移除引用新表的触发器，再删除事件表；旧预算触发器和所有业务数据保持原样。
DROP TRIGGER trg_budget_scope_events_prd052_immutable_delete;
DROP TRIGGER trg_budget_scope_events_prd052_immutable_update;
DROP TRIGGER trg_budget_scope_events_prd052_owner_insert;
DROP TRIGGER trg_budget_reservations_prd052_scope_update;
DROP TRIGGER trg_budget_reservations_prd052_scope_insert;
DROP TRIGGER trg_budget_blocks_prd052_scope_update;
DROP TRIGGER trg_budget_blocks_prd052_scope_insert;
DROP TRIGGER trg_budget_evaluations_prd052_owner_update;
DROP TRIGGER trg_budget_evaluations_prd052_owner_insert;
DROP TRIGGER trg_budget_alert_events_prd052_owner_update;
DROP TRIGGER trg_budget_alert_events_prd052_owner_insert;
DROP TRIGGER trg_budget_rules_prd052_identity_update;
DROP TRIGGER trg_budget_rules_prd052_owner_update;
DROP TRIGGER trg_budget_rules_prd052_owner_insert;

DROP INDEX idx_finops_budget_key_usage_prd052;
DROP INDEX idx_finops_budget_application_usage_prd052;
DROP INDEX idx_finops_budget_project_usage_prd052;
DROP INDEX idx_finops_budget_tenant_usage_prd052;
DROP INDEX idx_budget_scope_events_tenant_date_prd052;
DROP TABLE budget_scope_events;

-- 回滚末尾再次校验引用完整性；失败会让 runner 回滚整个 down 事务。
CREATE TEMP TABLE prd052_down_integrity_guard (
  issue_count INTEGER NOT NULL
);
CREATE TEMP TRIGGER trg_prd052_down_integrity_guard
BEFORE INSERT ON prd052_down_integrity_guard
FOR EACH ROW
WHEN NEW.issue_count != 0
BEGIN
  SELECT RAISE(ABORT, 'prd052_down_foreign_key_failed');
END;
-- 把 foreign_key_check 结果收敛为稳定 guard，确保 down 只能整体成功或整体回滚。
INSERT INTO prd052_down_integrity_guard(issue_count)
SELECT COUNT(*) FROM pragma_foreign_key_check;
DROP TRIGGER trg_prd052_down_integrity_guard;
DROP TABLE prd052_down_integrity_guard;

COMMIT;
