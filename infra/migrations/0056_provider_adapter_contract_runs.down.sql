-- PRD-064 down：任何 run/case/source、订阅、投递、attempt 或死信证据存在时都在 DDL 前阻断。
-- 回滚只用于尚未产生业务事实的空环境，不能静默删除 CI 合并门禁和失败通知历史。

CREATE TEMP TABLE prd064_down_business_guard (
  invalid_count INTEGER NOT NULL CHECK (invalid_count=0)
);
CREATE TEMP TRIGGER trg_prd064_down_business_guard
BEFORE INSERT ON prd064_down_business_guard FOR EACH ROW WHEN NEW.invalid_count!=0
BEGIN SELECT RAISE(ABORT,'prd064_down_business_facts_present'); END;
-- 先汇总所有合同及 Webhook 业务事实，再由临时触发器以单条原子检查阻止破坏性回滚。
INSERT INTO prd064_down_business_guard(invalid_count)
SELECT
  (SELECT COUNT(*) FROM adapter_contract_runs)
  + (SELECT COUNT(*) FROM adapter_contract_cases)
  + (SELECT COUNT(*) FROM adapter_contract_event_outbox)
  + (SELECT COUNT(*) FROM webhook_endpoints endpoint
     WHERE json_valid(endpoint.event_types_json)
       AND EXISTS (
         SELECT 1 FROM json_each(endpoint.event_types_json) item
         WHERE item.value='adapter.contract_failed'
       ))
  + (SELECT COUNT(*) FROM webhook_deliveries WHERE event_type='adapter.contract_failed')
  + (SELECT COUNT(*) FROM webhook_delivery_attempts attempt
     JOIN webhook_deliveries delivery
       ON delivery.tenant_id=attempt.tenant_id
      AND delivery.webhook_delivery_id=attempt.webhook_delivery_id
     WHERE delivery.event_type='adapter.contract_failed')
  + (SELECT COUNT(*) FROM webhook_dead_letter_events WHERE event_type='adapter.contract_failed');
DROP TRIGGER trg_prd064_down_business_guard;
DROP TABLE prd064_down_business_guard;

CREATE TEMP TABLE prd064_down_guard (
  issue_count INTEGER NOT NULL CHECK (issue_count=0)
);
-- 栈顶、对象集合、外键与完整性必须同时通过；结构漂移时不尝试删除部分对象。
INSERT INTO prd064_down_guard(issue_count)
SELECT
  CASE WHEN (SELECT COUNT(*) FROM schema_migrations
    WHERE version='0056' AND status='applied')=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM schema_migrations WHERE status='applied' AND version>'0056')
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='table' AND name IN (
        'adapter_contract_runs','adapter_contract_cases','adapter_contract_event_outbox'
      ))=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name IN (
        'idx_adapter_contract_runs_tenant_completed_prd064',
        'idx_adapter_contract_runs_tenant_status_prd064',
        'idx_adapter_contract_cases_tenant_run_prd064',
        'idx_adapter_contract_event_pending_prd064'
      ))=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='trigger' AND name IN (
        'trg_adapter_contract_runs_owner_insert_prd064',
        'trg_adapter_contract_runs_failure_codes_insert_prd064',
        'trg_adapter_contract_runs_immutable_update_prd064',
        'trg_adapter_contract_runs_immutable_delete_prd064',
        'trg_adapter_contract_cases_owner_insert_prd064',
        'trg_adapter_contract_cases_immutable_update_prd064',
        'trg_adapter_contract_cases_immutable_delete_prd064',
        'trg_adapter_contract_event_validate_insert_prd064',
        'trg_adapter_contract_event_state_update_prd064',
        'trg_adapter_contract_event_immutable_delete_prd064'
      ))=10 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema object
     WHERE object.sql IS NOT NULL
       AND (
         instr(lower(object.sql),'adapter_contract_runs')>0
         OR instr(lower(object.sql),'adapter_contract_cases')>0
         OR instr(lower(object.sql),'adapter_contract_event_outbox')>0
       )
       AND object.name NOT IN (
         'adapter_contract_runs','adapter_contract_cases','adapter_contract_event_outbox',
         'idx_adapter_contract_runs_tenant_completed_prd064',
         'idx_adapter_contract_runs_tenant_status_prd064',
         'idx_adapter_contract_cases_tenant_run_prd064',
         'idx_adapter_contract_event_pending_prd064',
         'trg_adapter_contract_runs_owner_insert_prd064',
         'trg_adapter_contract_runs_failure_codes_insert_prd064',
         'trg_adapter_contract_runs_immutable_update_prd064',
         'trg_adapter_contract_runs_immutable_delete_prd064',
         'trg_adapter_contract_cases_owner_insert_prd064',
         'trg_adapter_contract_cases_immutable_update_prd064',
         'trg_adapter_contract_cases_immutable_delete_prd064',
         'trg_adapter_contract_event_validate_insert_prd064',
         'trg_adapter_contract_event_state_update_prd064',
         'trg_adapter_contract_event_immutable_delete_prd064'
       ))
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd064_down_guard;

DROP TRIGGER trg_adapter_contract_event_immutable_delete_prd064;
DROP TRIGGER trg_adapter_contract_event_state_update_prd064;
DROP TRIGGER trg_adapter_contract_event_validate_insert_prd064;
DROP INDEX idx_adapter_contract_event_pending_prd064;
DROP TABLE adapter_contract_event_outbox;

DROP TRIGGER trg_adapter_contract_cases_immutable_delete_prd064;
DROP TRIGGER trg_adapter_contract_cases_immutable_update_prd064;
DROP TRIGGER trg_adapter_contract_cases_owner_insert_prd064;
DROP INDEX idx_adapter_contract_cases_tenant_run_prd064;
DROP TABLE adapter_contract_cases;

DROP TRIGGER trg_adapter_contract_runs_immutable_delete_prd064;
DROP TRIGGER trg_adapter_contract_runs_immutable_update_prd064;
DROP TRIGGER trg_adapter_contract_runs_owner_insert_prd064;
DROP TRIGGER trg_adapter_contract_runs_failure_codes_insert_prd064;
DROP INDEX idx_adapter_contract_runs_tenant_status_prd064;
DROP INDEX idx_adapter_contract_runs_tenant_completed_prd064;
DROP TABLE adapter_contract_runs;
