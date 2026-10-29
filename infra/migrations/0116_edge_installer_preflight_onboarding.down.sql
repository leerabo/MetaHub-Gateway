-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd124_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd124_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0116' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_preflight_reports)
  + (SELECT COUNT(*) FROM edge_installations)
  + (SELECT COUNT(*) FROM edge_install_audits)
  + (SELECT COUNT(*) FROM edge_install_outbox)
  + (SELECT COUNT(*) FROM edge_install_commands)
  + (SELECT COUNT(*) FROM platform_installer_release_commands)
  + CASE WHEN (SELECT COUNT(*) FROM installer_releases
      WHERE installer_release_id!='irl_12400000000000000000000000000000')=0 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM installer_releases
      WHERE installer_release_id='irl_12400000000000000000000000000000'
        AND status='released' AND revision=1)=1 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd124_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd124_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd124_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd124_delete;
CREATE TEMP TABLE prd124_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd124;
DROP TABLE tenant_deletion_manifest_extensions_prd124;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd124_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd124_manifest_sources);
DROP TABLE prd124_manifest_sources;

DROP TRIGGER trg_edge_install_commands_delete_prd124;
DROP TRIGGER trg_edge_install_commands_update_prd124;
DROP TABLE edge_install_commands;
DROP TRIGGER trg_edge_install_outbox_delete_prd124;
DROP TRIGGER trg_edge_install_outbox_update_prd124;
DROP INDEX idx_edge_install_outbox_delivery_prd124;
DROP TABLE edge_install_outbox;
DROP TRIGGER trg_edge_install_audits_delete_prd124;
DROP TRIGGER trg_edge_install_audits_update_prd124;
DROP INDEX idx_edge_install_audits_owner_prd124;
DROP TABLE edge_install_audits;
DROP TRIGGER trg_edge_installations_delete_prd124;
DROP TRIGGER trg_edge_installations_update_prd124;
DROP INDEX idx_edge_installations_owner_prd124;
DROP TABLE edge_installations;
DROP TRIGGER trg_edge_preflight_reports_delete_prd124;
DROP TRIGGER trg_edge_preflight_reports_update_prd124;
DROP INDEX idx_edge_preflight_owner_prd124;
DROP TABLE edge_preflight_reports;
DROP TRIGGER trg_platform_installer_commands_delete_prd124;
DROP TRIGGER trg_platform_installer_commands_update_prd124;
DROP TABLE platform_installer_release_commands;
DROP TRIGGER trg_installer_profiles_delete_prd124;
DROP TRIGGER trg_installer_profiles_update_prd124;
DROP TRIGGER trg_installer_profiles_insert_prd124;
DROP TABLE installer_profiles;
DROP TRIGGER trg_installer_artifacts_delete_prd124;
DROP TRIGGER trg_installer_artifacts_update_prd124;
DROP TRIGGER trg_installer_artifacts_insert_prd124;
DROP TABLE installer_release_artifacts;
DROP TRIGGER trg_installer_releases_delete_prd124;
DROP TRIGGER trg_installer_releases_update_prd124;
DROP TRIGGER trg_installer_releases_insert_prd124;
DROP TABLE installer_releases;
DROP TRIGGER trg_installer_trusted_keys_delete_prd124;
DROP TRIGGER trg_installer_trusted_keys_update_prd124;
DROP TRIGGER trg_installer_trusted_keys_insert_prd124;
DROP TABLE installer_trusted_release_keys;
DROP TRIGGER trg_platform_edge_installer_roles_delete_prd124;
DROP TRIGGER trg_platform_edge_installer_roles_update_prd124;
DROP TRIGGER trg_platform_edge_installer_roles_insert_prd124;
DROP TRIGGER trg_platform_edge_installer_permissions_delete_prd124;
DROP TRIGGER trg_platform_edge_installer_permissions_update_prd124;
DROP TRIGGER trg_platform_edge_installer_permissions_insert_prd124;
DROP TABLE platform_edge_installer_role_permissions;
DROP TABLE platform_edge_installer_permission_extensions;
DROP TRIGGER trg_workspace_edge_installer_roles_delete_prd124;
DROP TRIGGER trg_workspace_edge_installer_roles_update_prd124;
DROP TRIGGER trg_workspace_edge_installer_roles_insert_prd124;
DROP TRIGGER trg_workspace_edge_installer_permissions_delete_prd124;
DROP TRIGGER trg_workspace_edge_installer_permissions_update_prd124;
DROP TRIGGER trg_workspace_edge_installer_permissions_insert_prd124;
DROP TABLE workspace_edge_installer_role_permissions;
DROP TABLE workspace_edge_installer_permission_extensions;

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

CREATE TEMP TABLE prd124_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd124_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_installer_permission_extensions',
      'workspace_edge_installer_role_permissions',
      'platform_edge_installer_permission_extensions',
      'platform_edge_installer_role_permissions','installer_trusted_release_keys',
      'installer_releases','installer_release_artifacts','installer_profiles',
      'platform_installer_release_commands',
      'edge_preflight_reports','edge_installations','edge_install_audits',
      'edge_install_outbox','edge_install_commands',
      'tenant_deletion_manifest_extensions_prd124'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd124_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
