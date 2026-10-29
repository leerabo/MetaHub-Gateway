-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd125_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd125_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0117' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_validation_runs)
  + (SELECT COUNT(*) FROM edge_validation_checks)
  + (SELECT COUNT(*) FROM edge_delivery_acceptances)
  + (SELECT COUNT(*) FROM edge_support_bundles)
  + (SELECT COUNT(*) FROM edge_support_bundle_download_audits)
  + (SELECT COUNT(*) FROM edge_delivery_events)
  + (SELECT COUNT(*) FROM edge_delivery_outbox)
  + (SELECT COUNT(*) FROM edge_validation_commands)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd125_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd125_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd125_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd125_delete;
CREATE TEMP TABLE prd125_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd125;
DROP TABLE tenant_deletion_manifest_extensions_prd125;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd125_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd125_manifest_sources);
DROP TABLE prd125_manifest_sources;

DROP TRIGGER trg_edge_validation_commands_delete_prd125;
DROP TRIGGER trg_edge_validation_commands_update_prd125;
DROP TABLE edge_validation_commands;
DROP TRIGGER trg_edge_delivery_outbox_delete_prd125;
DROP TRIGGER trg_edge_delivery_outbox_update_prd125;
DROP INDEX idx_edge_delivery_outbox_prd125;
DROP TABLE edge_delivery_outbox;
DROP TRIGGER trg_edge_delivery_events_delete_prd125;
DROP TRIGGER trg_edge_delivery_events_update_prd125;
DROP TABLE edge_delivery_events;
DROP TRIGGER trg_edge_support_downloads_delete_prd125;
DROP TRIGGER trg_edge_support_downloads_update_prd125;
DROP INDEX idx_edge_support_download_owner_prd125;
DROP TABLE edge_support_bundle_download_audits;
DROP TRIGGER trg_edge_support_bundles_delete_prd125;
DROP TRIGGER trg_edge_support_bundles_update_prd125;
DROP INDEX idx_edge_support_bundles_owner_prd125;
DROP TABLE edge_support_bundles;
DROP TRIGGER trg_edge_delivery_acceptances_delete_prd125;
DROP TRIGGER trg_edge_delivery_acceptances_update_prd125;
DROP TABLE edge_delivery_acceptances;
DROP TRIGGER trg_edge_validation_checks_delete_prd125;
DROP TRIGGER trg_edge_validation_checks_update_prd125;
DROP TRIGGER trg_edge_validation_checks_insert_prd125;
DROP INDEX idx_edge_validation_checks_owner_prd125;
DROP TABLE edge_validation_checks;
DROP TRIGGER trg_edge_validation_runs_delete_prd125;
DROP TRIGGER trg_edge_validation_runs_update_prd125;
DROP INDEX idx_edge_validation_runs_owner_prd125;
DROP TABLE edge_validation_runs;
DROP TRIGGER trg_workspace_edge_validation_roles_delete_prd125;
DROP TRIGGER trg_workspace_edge_validation_roles_update_prd125;
DROP TRIGGER trg_workspace_edge_validation_roles_insert_prd125;
DROP TRIGGER trg_workspace_edge_validation_permissions_delete_prd125;
DROP TRIGGER trg_workspace_edge_validation_permissions_update_prd125;
DROP TRIGGER trg_workspace_edge_validation_permissions_insert_prd125;
DROP TABLE workspace_edge_validation_role_permissions;
DROP TABLE workspace_edge_validation_permission_extensions;

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

CREATE TEMP TABLE prd125_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd125_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_validation_permission_extensions',
      'workspace_edge_validation_role_permissions','edge_validation_runs',
      'edge_validation_checks','edge_delivery_acceptances','edge_support_bundles',
      'edge_support_bundle_download_audits','edge_delivery_events','edge_delivery_outbox',
      'edge_validation_commands','tenant_deletion_manifest_extensions_prd125'))=0
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd125_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
