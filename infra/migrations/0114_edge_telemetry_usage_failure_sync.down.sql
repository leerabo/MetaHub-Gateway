-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd122_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd122_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0114' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM workspace_edge_telemetry_commands)
  + (SELECT COUNT(*) FROM edge_telemetry_batches)
  + (SELECT COUNT(*) FROM edge_usage_sync_cursors)
  + (SELECT COUNT(*) FROM edge_telemetry_receipts)
  + (SELECT COUNT(*) FROM edge_usage_reconciliations)
  + (SELECT COUNT(*) FROM edge_failure_summaries)
  + (SELECT COUNT(*) FROM edge_fleet_telemetry_projections)
  + (SELECT COUNT(*) FROM edge_telemetry_events)
  + (SELECT COUNT(*) FROM edge_telemetry_event_outbox)
  + (SELECT COUNT(*) FROM edge_telemetry_policies
      WHERE revision!=1 OR telemetry_level!='minimal' OR source_code!='system_default')
  + CASE WHEN (SELECT COUNT(*) FROM edge_telemetry_policies)
      =(SELECT COUNT(*) FROM edge_deployments)
      AND (SELECT COUNT(*) FROM edge_telemetry_policy_heads)
      =(SELECT COUNT(*) FROM edge_deployments) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd122_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd122_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd122_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd122_delete;
CREATE TEMP TABLE prd122_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd122;
DROP TABLE tenant_deletion_manifest_extensions_prd122;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd122_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd122_manifest_sources);
DROP TABLE prd122_manifest_sources;

DROP INDEX idx_edge_telemetry_outbox_delivery_prd122;
DROP TABLE edge_telemetry_event_outbox;
DROP TRIGGER trg_edge_telemetry_events_delete_prd122;
DROP TRIGGER trg_edge_telemetry_events_update_prd122;
DROP INDEX idx_edge_telemetry_events_owner_prd122;
DROP TABLE edge_telemetry_events;
DROP TRIGGER trg_edge_fleet_telemetry_delete_prd122;
DROP TRIGGER trg_edge_fleet_telemetry_update_prd122;
DROP INDEX idx_edge_fleet_telemetry_lag_prd122;
DROP TABLE edge_fleet_telemetry_projections;
DROP TRIGGER trg_edge_failure_summaries_delete_prd122;
DROP TRIGGER trg_edge_failure_summaries_update_prd122;
DROP INDEX idx_edge_failure_summaries_owner_prd122;
DROP TABLE edge_failure_summaries;
DROP TRIGGER trg_edge_usage_reconciliations_delete_prd122;
DROP TRIGGER trg_edge_usage_reconciliations_update_prd122;
DROP INDEX idx_edge_usage_reconciliation_owner_prd122;
DROP TABLE edge_usage_reconciliations;
DROP TRIGGER trg_edge_telemetry_receipts_delete_prd122;
DROP TRIGGER trg_edge_telemetry_receipts_update_prd122;
DROP TABLE edge_telemetry_receipts;
DROP TRIGGER trg_edge_usage_cursors_delete_prd122;
DROP TRIGGER trg_edge_usage_cursors_update_prd122;
DROP INDEX idx_edge_usage_cursor_owner_prd122;
DROP INDEX idx_edge_usage_cursor_active_prd122;
DROP TABLE edge_usage_sync_cursors;
DROP TRIGGER trg_edge_telemetry_batches_delete_prd122;
DROP TRIGGER trg_edge_telemetry_batches_update_prd122;
DROP INDEX idx_edge_telemetry_batches_cursor_prd122;
DROP INDEX idx_edge_telemetry_batches_owner_prd122;
DROP TABLE edge_telemetry_batches;
DROP INDEX idx_edge_sessions_telemetry_owner_prd122;
DROP TRIGGER trg_workspace_edge_telemetry_commands_delete_prd122;
DROP TRIGGER trg_workspace_edge_telemetry_commands_update_prd122;
DROP TABLE workspace_edge_telemetry_commands;
DROP TRIGGER trg_edge_telemetry_policy_heads_delete_prd122;
DROP TRIGGER trg_edge_telemetry_policy_heads_update_prd122;
DROP TABLE edge_telemetry_policy_heads;
DROP TRIGGER trg_edge_telemetry_policies_delete_prd122;
DROP TRIGGER trg_edge_telemetry_policies_update_prd122;
DROP TABLE edge_telemetry_policies;
DROP TRIGGER trg_workspace_edge_telemetry_roles_delete_prd122;
DROP TRIGGER trg_workspace_edge_telemetry_roles_update_prd122;
DROP TRIGGER trg_workspace_edge_telemetry_roles_insert_prd122;
DROP TRIGGER trg_workspace_edge_telemetry_permissions_delete_prd122;
DROP TRIGGER trg_workspace_edge_telemetry_permissions_update_prd122;
DROP TRIGGER trg_workspace_edge_telemetry_permissions_insert_prd122;
DROP TABLE workspace_edge_telemetry_role_permissions;
DROP TABLE workspace_edge_telemetry_permission_extensions;

DELETE FROM tenant_deletion_trigger_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd122_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd122_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_telemetry_permission_extensions',
      'workspace_edge_telemetry_role_permissions','edge_telemetry_policies',
      'edge_telemetry_policy_heads','workspace_edge_telemetry_commands',
      'edge_telemetry_batches','edge_usage_sync_cursors','edge_telemetry_receipts',
      'edge_usage_reconciliations','edge_failure_summaries',
      'edge_fleet_telemetry_projections','edge_telemetry_events',
      'edge_telemetry_event_outbox','tenant_deletion_manifest_extensions_prd122'))=0
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema
      WHERE type='index' AND name='idx_edge_sessions_telemetry_owner_prd122')
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd122_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
