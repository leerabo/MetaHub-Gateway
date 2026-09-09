-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;
CREATE TEMP TABLE meter_recovery_down_guard(value INTEGER CHECK(value=0));
INSERT INTO meter_recovery_down_guard SELECT COUNT(*) FROM platform_meter_recovery_actions;
DROP TABLE meter_recovery_down_guard;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table='platform_meter_recovery_actions';
DELETE FROM tenant_deletion_table_manifest WHERE source_table='platform_meter_recovery_actions';
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
DROP TABLE platform_meter_recovery_actions;
COMMIT;
PRAGMA foreign_keys = ON;
