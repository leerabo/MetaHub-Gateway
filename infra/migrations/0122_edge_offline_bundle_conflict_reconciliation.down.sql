-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd130_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd130_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0122' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_offline_bundle_requests)
  + (SELECT COUNT(*) FROM edge_offline_bundles)
  + (SELECT COUNT(*) FROM edge_offline_bundle_transfers)
  + (SELECT COUNT(*) FROM edge_offline_sync_cursors)
  + (SELECT COUNT(*) FROM edge_offline_conflicts)
  + (SELECT COUNT(*) FROM edge_offline_conflict_resolutions)
  + (SELECT COUNT(*) FROM edge_offline_bundle_events)
  + (SELECT COUNT(*) FROM edge_offline_bundle_outbox)
  + (SELECT COUNT(*) FROM edge_offline_bundle_action_commands)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd130_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd130_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd130_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd130_delete;
CREATE TEMP TABLE prd130_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd130;
DROP TABLE tenant_deletion_manifest_extensions_prd130;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd130_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd130_manifest_sources);
DROP TABLE prd130_manifest_sources;

DROP TRIGGER trg_edge_offline_actions_delete_prd130;
DROP TRIGGER trg_edge_offline_actions_update_prd130;
DROP TABLE edge_offline_bundle_action_commands;
DROP TRIGGER trg_edge_offline_outbox_delete_prd130;
DROP TRIGGER trg_edge_offline_outbox_update_prd130;
DROP TABLE edge_offline_bundle_outbox;
DROP TRIGGER trg_edge_offline_events_delete_prd130;
DROP TRIGGER trg_edge_offline_events_update_prd130;
DROP TABLE edge_offline_bundle_events;
DROP TRIGGER trg_edge_offline_resolutions_delete_prd130;
DROP TRIGGER trg_edge_offline_resolutions_update_prd130;
DROP TABLE edge_offline_conflict_resolutions;
DROP TRIGGER trg_edge_offline_conflicts_delete_prd130;
DROP TRIGGER trg_edge_offline_conflicts_update_prd130;
DROP INDEX idx_edge_offline_conflicts_owner_prd130;
DROP TABLE edge_offline_conflicts;
DROP TRIGGER trg_edge_offline_cursors_delete_prd130;
DROP TRIGGER trg_edge_offline_cursors_update_prd130;
DROP TABLE edge_offline_sync_cursors;
DROP TRIGGER trg_edge_offline_transfers_delete_prd130;
DROP TRIGGER trg_edge_offline_transfers_update_prd130;
DROP INDEX idx_edge_offline_transfers_owner_prd130;
DROP TABLE edge_offline_bundle_transfers;
DROP TRIGGER trg_edge_offline_bundles_delete_prd130;
DROP TRIGGER trg_edge_offline_bundles_update_prd130;
DROP INDEX idx_edge_offline_bundles_owner_prd130;
DROP TABLE edge_offline_bundles;
DROP TRIGGER trg_edge_offline_requests_delete_prd130;
DROP TRIGGER trg_edge_offline_requests_update_prd130;
DROP INDEX idx_edge_offline_requests_owner_prd130;
DROP TABLE edge_offline_bundle_requests;

DROP TRIGGER trg_platform_edge_offline_roles_delete_prd130;
DROP TRIGGER trg_platform_edge_offline_roles_update_prd130;
DROP TRIGGER trg_platform_edge_offline_roles_insert_prd130;
DROP TRIGGER trg_platform_edge_offline_permissions_delete_prd130;
DROP TRIGGER trg_platform_edge_offline_permissions_update_prd130;
DROP TABLE platform_edge_offline_role_permissions;
DROP TABLE platform_edge_offline_permission_extensions;
DROP TRIGGER trg_workspace_edge_offline_roles_delete_prd130;
DROP TRIGGER trg_workspace_edge_offline_roles_update_prd130;
DROP TRIGGER trg_workspace_edge_offline_roles_insert_prd130;
DROP TRIGGER trg_workspace_edge_offline_permissions_delete_prd130;
DROP TRIGGER trg_workspace_edge_offline_permissions_update_prd130;
DROP TABLE workspace_edge_offline_role_permissions;
DROP TABLE workspace_edge_offline_permission_extensions;

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

CREATE TEMP TABLE prd130_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd130_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_offline_permission_extensions','workspace_edge_offline_role_permissions',
      'platform_edge_offline_permission_extensions','platform_edge_offline_role_permissions',
      'edge_offline_bundle_requests','edge_offline_bundles','edge_offline_bundle_transfers',
      'edge_offline_sync_cursors','edge_offline_conflicts',
      'edge_offline_conflict_resolutions','edge_offline_bundle_events',
      'edge_offline_bundle_outbox','edge_offline_bundle_action_commands',
      'tenant_deletion_manifest_extensions_prd130'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd130_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
