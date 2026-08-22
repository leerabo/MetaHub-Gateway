-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd099_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd099_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0091' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM platform_meter_readings)
  + (SELECT COUNT(*) FROM platform_billing_cycles)
  + (SELECT COUNT(*) FROM platform_quota_allocations)
  + (SELECT COUNT(*) FROM platform_billing_summaries)
  + (SELECT COUNT(*) FROM platform_billing_adjustments)
  + (SELECT COUNT(*) FROM platform_meter_outbox)
  + CASE WHEN (SELECT COUNT(*) FROM platform_meter_definitions)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_billing_permission_extensions)=1 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_billing_role_permissions)=2 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd099_down_guard;

DROP TRIGGER trg_usage_ledger_platform_meter_outbox_prd099;
DROP TRIGGER trg_platform_billing_adjustments_delete_prd099;
DROP TRIGGER trg_platform_billing_adjustments_update_prd099;
DROP TRIGGER trg_platform_billing_summaries_delete_prd099;
DROP TRIGGER trg_platform_billing_summaries_update_prd099;
DROP TRIGGER trg_platform_meter_readings_delete_prd099;
DROP TRIGGER trg_platform_meter_readings_update_prd099;
DROP TRIGGER trg_platform_quota_allocations_delete_prd099;
DROP TRIGGER trg_platform_quota_allocations_update_prd099;
DROP TRIGGER trg_platform_meter_definitions_delete_prd099;
DROP TRIGGER trg_platform_meter_definitions_update_prd099;
DROP TRIGGER trg_platform_billing_role_permissions_delete_prd099;
DROP TRIGGER trg_platform_billing_role_permissions_update_prd099;
DROP TRIGGER trg_platform_billing_role_permissions_insert_prd099;
DROP TRIGGER trg_platform_billing_permissions_delete_prd099;
DROP TRIGGER trg_platform_billing_permissions_update_prd099;
DROP TRIGGER trg_platform_billing_permissions_insert_prd099;

DROP TABLE platform_meter_checkpoints;
DROP TABLE platform_meter_outbox;
DROP TABLE platform_billing_adjustments;
DROP TABLE platform_billing_summaries;
DROP TABLE platform_meter_readings;
DROP TABLE platform_quota_allocations;
DROP TABLE platform_billing_cycles;
DROP TABLE platform_meter_definitions;
DROP TABLE platform_billing_role_permissions;
DROP TABLE platform_billing_permission_extensions;

CREATE TEMP TABLE prd099_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd099_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd099_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
