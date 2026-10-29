DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM platform_meter_recovery_actions)
  THEN RAISE EXCEPTION 'meter_recovery_rollback_has_facts'; END IF;
END $$;
ALTER TABLE tenant_deletion_trigger_manifest DISABLE TRIGGER USER;
ALTER TABLE tenant_deletion_table_manifest DISABLE TRIGGER USER;
DELETE FROM tenant_deletion_trigger_manifest WHERE source_table='platform_meter_recovery_actions';
DELETE FROM tenant_deletion_table_manifest WHERE source_table='platform_meter_recovery_actions';
ALTER TABLE tenant_deletion_trigger_manifest ENABLE TRIGGER USER;
ALTER TABLE tenant_deletion_table_manifest ENABLE TRIGGER USER;
DROP TABLE platform_meter_recovery_actions;
DROP FUNCTION fn_meter_recovery_source_insert();
DROP FUNCTION fn_meter_recovery_immutable();
