-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd126_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd126_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0118' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_health_snapshots WHERE source_code!='migration_backfill')
  + (SELECT COUNT(*) FROM edge_diagnostic_grants)
  + (SELECT COUNT(*) FROM edge_diagnostic_commands)
  + (SELECT COUNT(*) FROM edge_diagnostic_results)
  + (SELECT COUNT(*) FROM edge_diagnostic_events)
  + (SELECT COUNT(*) FROM edge_diagnostic_outbox)
  + (SELECT COUNT(*) FROM edge_diagnostic_action_commands)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd126_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd126_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd126_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd126_delete;
CREATE TEMP TABLE prd126_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd126;
DROP TABLE tenant_deletion_manifest_extensions_prd126;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd126_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd126_manifest_sources);
DROP TABLE prd126_manifest_sources;

DROP TRIGGER trg_edge_diagnostic_actions_delete_prd126;
DROP TRIGGER trg_edge_diagnostic_actions_update_prd126;
DROP TABLE edge_diagnostic_action_commands;
DROP TRIGGER trg_edge_diagnostic_outbox_delete_prd126;
DROP TRIGGER trg_edge_diagnostic_outbox_update_prd126;
DROP INDEX idx_edge_diagnostic_outbox_prd126;
DROP TABLE edge_diagnostic_outbox;
DROP TRIGGER trg_edge_diagnostic_events_delete_prd126;
DROP TRIGGER trg_edge_diagnostic_events_update_prd126;
DROP INDEX idx_edge_diagnostic_events_owner_prd126;
DROP TABLE edge_diagnostic_events;
DROP TRIGGER trg_edge_diagnostic_results_delete_prd126;
DROP TRIGGER trg_edge_diagnostic_results_update_prd126;
DROP INDEX idx_edge_diagnostic_results_owner_prd126;
DROP TABLE edge_diagnostic_results;
DROP TRIGGER trg_edge_diagnostic_commands_delete_prd126;
DROP TRIGGER trg_edge_diagnostic_commands_update_prd126;
DROP INDEX idx_edge_diagnostic_commands_deadline_prd126;
DROP INDEX idx_edge_diagnostic_commands_agent_prd126;
DROP TABLE edge_diagnostic_commands;
DROP TRIGGER trg_edge_diagnostic_grants_delete_prd126;
DROP TRIGGER trg_edge_diagnostic_grants_update_prd126;
DROP INDEX idx_edge_diagnostic_grants_owner_prd126;
DROP TABLE edge_diagnostic_grants;
DROP TRIGGER trg_edge_fleet_index_delete_prd126;
DROP TRIGGER trg_edge_fleet_index_update_prd126;
DROP INDEX idx_edge_fleet_health_prd126;
DROP INDEX idx_edge_fleet_keyset_prd126;
DROP TABLE edge_fleet_deployment_index;
DROP TRIGGER trg_edge_health_heads_delete_prd126;
DROP TRIGGER trg_edge_health_heads_update_prd126;
DROP TABLE edge_health_heads;
DROP TRIGGER trg_edge_health_snapshots_delete_prd126;
DROP TRIGGER trg_edge_health_snapshots_update_prd126;
DROP INDEX idx_edge_health_snapshots_owner_prd126;
DROP TABLE edge_health_snapshots;
DROP TRIGGER trg_workspace_edge_diagnostic_roles_delete_prd126;
DROP TRIGGER trg_workspace_edge_diagnostic_roles_update_prd126;
DROP TRIGGER trg_workspace_edge_diagnostic_roles_insert_prd126;
DROP TRIGGER trg_workspace_edge_diagnostic_permissions_delete_prd126;
DROP TRIGGER trg_workspace_edge_diagnostic_permissions_update_prd126;
DROP TRIGGER trg_workspace_edge_diagnostic_permissions_insert_prd126;
DROP TABLE workspace_edge_diagnostic_role_permissions;
DROP TABLE workspace_edge_diagnostic_permission_extensions;

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

CREATE TEMP TABLE prd126_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd126_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_diagnostic_permission_extensions',
      'workspace_edge_diagnostic_role_permissions','edge_health_snapshots',
      'edge_health_heads','edge_fleet_deployment_index','edge_diagnostic_grants',
      'edge_diagnostic_commands','edge_diagnostic_results','edge_diagnostic_events',
      'edge_diagnostic_outbox','edge_diagnostic_action_commands',
      'tenant_deletion_manifest_extensions_prd126'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd126_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
