-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd129_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd129_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0121' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_backup_reports)
  + (SELECT COUNT(*) FROM edge_backup_statuses)
  + (SELECT COUNT(*) FROM edge_restore_jobs)
  + (SELECT COUNT(*) FROM edge_restore_verifications)
  + (SELECT COUNT(*) FROM private_dr_drills)
  + (SELECT COUNT(*) FROM edge_backup_events)
  + (SELECT COUNT(*) FROM edge_backup_outbox)
  + (SELECT COUNT(*) FROM edge_backup_action_commands)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd129_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd129_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd129_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd129_delete;
CREATE TEMP TABLE prd129_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd129;
DROP TABLE tenant_deletion_manifest_extensions_prd129;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd129_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd129_manifest_sources);
DROP TABLE prd129_manifest_sources;

DROP TRIGGER trg_edge_backup_actions_delete_prd129;
DROP TRIGGER trg_edge_backup_actions_update_prd129;
DROP TABLE edge_backup_action_commands;
DROP TRIGGER trg_edge_backup_outbox_delete_prd129;
DROP TRIGGER trg_edge_backup_outbox_update_prd129;
DROP TABLE edge_backup_outbox;
DROP TRIGGER trg_edge_backup_events_delete_prd129;
DROP TRIGGER trg_edge_backup_events_update_prd129;
DROP TABLE edge_backup_events;
DROP TRIGGER trg_private_dr_drills_delete_prd129;
DROP TRIGGER trg_private_dr_drills_update_prd129;
DROP INDEX idx_private_dr_drills_owner_prd129;
DROP TABLE private_dr_drills;
DROP TRIGGER trg_edge_restore_verifications_delete_prd129;
DROP TRIGGER trg_edge_restore_verifications_update_prd129;
DROP TABLE edge_restore_verifications;
DROP TRIGGER trg_edge_restore_jobs_delete_prd129;
DROP TRIGGER trg_edge_restore_jobs_update_prd129;
DROP INDEX idx_edge_restore_jobs_owner_prd129;
DROP TABLE edge_restore_jobs;
DROP TRIGGER trg_edge_backup_status_delete_prd129;
DROP TRIGGER trg_edge_backup_status_update_prd129;
DROP TABLE edge_backup_statuses;
DROP TRIGGER trg_edge_backup_reports_delete_prd129;
DROP TRIGGER trg_edge_backup_reports_update_prd129;
DROP INDEX idx_edge_backup_reports_owner_prd129;
DROP TABLE edge_backup_reports;

DROP TRIGGER trg_platform_edge_backup_roles_delete_prd129;
DROP TRIGGER trg_platform_edge_backup_roles_update_prd129;
DROP TRIGGER trg_platform_edge_backup_roles_insert_prd129;
DROP TRIGGER trg_platform_edge_backup_permissions_delete_prd129;
DROP TRIGGER trg_platform_edge_backup_permissions_update_prd129;
DROP TABLE platform_edge_backup_role_permissions;
DROP TABLE platform_edge_backup_permission_extensions;
DROP TRIGGER trg_workspace_edge_backup_roles_delete_prd129;
DROP TRIGGER trg_workspace_edge_backup_roles_update_prd129;
DROP TRIGGER trg_workspace_edge_backup_roles_insert_prd129;
DROP TRIGGER trg_workspace_edge_backup_permissions_delete_prd129;
DROP TRIGGER trg_workspace_edge_backup_permissions_update_prd129;
DROP TABLE workspace_edge_backup_role_permissions;
DROP TABLE workspace_edge_backup_permission_extensions;

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

CREATE TEMP TABLE prd129_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd129_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_backup_permission_extensions','workspace_edge_backup_role_permissions',
      'platform_edge_backup_permission_extensions','platform_edge_backup_role_permissions',
      'edge_backup_reports','edge_backup_statuses','edge_restore_jobs',
      'edge_restore_verifications','private_dr_drills','edge_backup_events',
      'edge_backup_outbox','edge_backup_action_commands',
      'tenant_deletion_manifest_extensions_prd129'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd129_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
