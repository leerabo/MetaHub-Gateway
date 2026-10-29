-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd131_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd131_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0123' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM migration_assessments)
  + (SELECT COUNT(*) FROM migration_plans)
  + (SELECT COUNT(*) FROM migration_assessment_snapshots)
  + (SELECT COUNT(*) FROM migration_runs)
  + (SELECT COUNT(*) FROM migration_differences)
  + (SELECT COUNT(*) FROM migration_cutover_checkpoints)
  + (SELECT COUNT(*) FROM migration_rollback_plans)
  + (SELECT COUNT(*) FROM migration_cutover_heads)
  + (SELECT COUNT(*) FROM migration_events)
  + (SELECT COUNT(*) FROM migration_event_outbox)
  + (SELECT COUNT(*) FROM migration_action_commands)
  + (SELECT COUNT(*) FROM migration_support_access_audits)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd131_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd131_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd131_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd131_delete;
CREATE TEMP TABLE prd131_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd131;
DROP TABLE tenant_deletion_manifest_extensions_prd131;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd131_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd131_manifest_sources);
DROP TABLE prd131_manifest_sources;

DROP TRIGGER trg_migration_support_audits_delete_prd131;
DROP TRIGGER trg_migration_support_audits_update_prd131;
DROP TABLE migration_support_access_audits;
DROP TRIGGER trg_migration_actions_delete_prd131;
DROP TRIGGER trg_migration_actions_update_prd131;
DROP TABLE migration_action_commands;
DROP TRIGGER trg_migration_outbox_delete_prd131;
DROP TRIGGER trg_migration_outbox_update_prd131;
DROP TABLE migration_event_outbox;
DROP TRIGGER trg_migration_events_delete_prd131;
DROP TRIGGER trg_migration_events_update_prd131;
DROP TABLE migration_events;
DROP TRIGGER trg_migration_cutover_heads_delete_prd131;
DROP TRIGGER trg_migration_cutover_heads_update_prd131;
DROP TABLE migration_cutover_heads;
DROP TRIGGER trg_migration_rollback_plans_delete_prd131;
DROP TRIGGER trg_migration_rollback_plans_update_prd131;
DROP TABLE migration_rollback_plans;
DROP TRIGGER trg_migration_checkpoints_delete_prd131;
DROP TRIGGER trg_migration_checkpoints_update_prd131;
DROP TABLE migration_cutover_checkpoints;
DROP TRIGGER trg_migration_differences_delete_prd131;
DROP TRIGGER trg_migration_differences_update_prd131;
DROP INDEX idx_migration_differences_owner_prd131;
DROP TABLE migration_differences;
DROP TRIGGER trg_migration_runs_delete_prd131;
DROP TRIGGER trg_migration_runs_update_prd131;
DROP INDEX idx_migration_runs_owner_prd131;
DROP TABLE migration_runs;
DROP TRIGGER trg_migration_snapshots_delete_prd131;
DROP TRIGGER trg_migration_snapshots_update_prd131;
DROP TABLE migration_assessment_snapshots;
DROP TRIGGER trg_migration_plans_delete_prd131;
DROP TRIGGER trg_migration_plans_update_prd131;
DROP TABLE migration_plans;
DROP TRIGGER trg_migration_assessments_delete_prd131;
DROP TRIGGER trg_migration_assessments_update_prd131;
DROP INDEX idx_migration_assessments_owner_prd131;
DROP TABLE migration_assessments;
DROP TRIGGER trg_migration_source_adapters_delete_prd131;
DROP TRIGGER trg_migration_source_adapters_update_prd131;
DROP TRIGGER trg_migration_source_adapters_insert_prd131;
DROP TABLE migration_source_adapters;
DROP TRIGGER trg_workspace_migration_roles_delete_prd131;
DROP TRIGGER trg_workspace_migration_roles_update_prd131;
DROP TRIGGER trg_workspace_migration_roles_insert_prd131;
DROP TRIGGER trg_workspace_migration_permissions_delete_prd131;
DROP TRIGGER trg_workspace_migration_permissions_update_prd131;
DROP TRIGGER trg_workspace_migration_permissions_insert_prd131;
DROP TABLE workspace_migration_role_permissions;
DROP TABLE workspace_migration_permission_extensions;

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

CREATE TEMP TABLE prd131_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd131_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_migration_permission_extensions','workspace_migration_role_permissions',
      'migration_source_adapters','migration_assessments','migration_plans',
      'migration_assessment_snapshots','migration_runs','migration_differences',
      'migration_cutover_checkpoints','migration_rollback_plans','migration_cutover_heads',
      'migration_events','migration_event_outbox','migration_action_commands',
      'migration_support_access_audits','tenant_deletion_manifest_extensions_prd131'))=0
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd131_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
