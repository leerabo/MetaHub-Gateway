-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd119_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd119_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0111' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM provider_catalog_instances)
  + (SELECT COUNT(*) FROM provider_preflight_runs)
  + (SELECT COUNT(*) FROM workspace_provider_catalog_commands)
  + (SELECT COUNT(*) FROM platform_provider_catalog_commands)
  + (SELECT COUNT(*) FROM provider_catalog_versions
      WHERE version!='2026.08.26.1' OR generation!=1)
  + (SELECT COUNT(*) FROM provider_catalog_trusted_keys
      WHERE key_id!='metahub-provider-catalog-seed-v1')
  + (SELECT COUNT(*) FROM provider_catalog_events
      WHERE provider_catalog_event_id!='pce_00000000000000000000000000000001'
        OR event_type!='provider_catalog.updated' OR outcome_code!='seeded')
  + (SELECT COUNT(*) FROM provider_catalog_event_outbox
      WHERE provider_catalog_outbox_id!='pco_00000000000000000000000000000001'
        OR status!='delivered')
  + CASE WHEN (SELECT version||':'||generation||':'||revision
      FROM provider_catalog_head WHERE singleton=1)='2026.08.26.1:1:1' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM provider_catalog_entries)=10
      AND (SELECT COUNT(*) FROM provider_capabilities)=12
      AND (SELECT COUNT(*) FROM regional_endpoint_templates)=7
      AND (SELECT COUNT(*) FROM compatibility_profiles)=10 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd119_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd119_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd119_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd119_delete;
CREATE TEMP TABLE prd119_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd119;
DROP TABLE tenant_deletion_manifest_extensions_prd119;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd119_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd119_manifest_sources);
DROP TABLE prd119_manifest_sources;

DROP TRIGGER trg_provider_catalog_outbox_delete_prd119;
DROP TRIGGER trg_provider_catalog_outbox_update_prd119;
DROP INDEX idx_provider_catalog_outbox_delivery_prd119;
DROP TABLE provider_catalog_event_outbox;
DROP TRIGGER trg_provider_catalog_events_delete_prd119;
DROP TRIGGER trg_provider_catalog_events_update_prd119;
DROP INDEX idx_provider_catalog_events_owner_prd119;
DROP TABLE provider_catalog_events;
DROP TRIGGER trg_platform_provider_catalog_commands_delete_prd119;
DROP TRIGGER trg_platform_provider_catalog_commands_update_prd119;
DROP TABLE platform_provider_catalog_commands;
DROP TRIGGER trg_workspace_provider_catalog_commands_delete_prd119;
DROP TRIGGER trg_workspace_provider_catalog_commands_update_prd119;
DROP TABLE workspace_provider_catalog_commands;
DROP TRIGGER trg_provider_preflight_runs_delete_prd119;
DROP TRIGGER trg_provider_preflight_runs_update_prd119;
DROP INDEX idx_provider_preflight_owner_prd119;
DROP TABLE provider_preflight_runs;
DROP TRIGGER trg_provider_catalog_instances_delete_prd119;
DROP TRIGGER trg_provider_catalog_instances_update_prd119;
DROP INDEX idx_provider_catalog_instances_owner_prd119;
DROP TABLE provider_catalog_instances;
DROP TRIGGER trg_compatibility_profiles_delete_prd119;
DROP TRIGGER trg_compatibility_profiles_update_prd119;
DROP TABLE compatibility_profiles;
DROP TRIGGER trg_regional_endpoint_templates_delete_prd119;
DROP TRIGGER trg_regional_endpoint_templates_update_prd119;
DROP INDEX idx_regional_endpoint_region_prd119;
DROP TABLE regional_endpoint_templates;
DROP TRIGGER trg_provider_capabilities_delete_prd119;
DROP TRIGGER trg_provider_capabilities_update_prd119;
DROP INDEX idx_provider_capability_filter_prd119;
DROP TABLE provider_capabilities;
DROP TRIGGER trg_provider_catalog_entries_delete_prd119;
DROP TRIGGER trg_provider_catalog_entries_update_prd119;
DROP INDEX idx_provider_catalog_entries_filter_prd119;
DROP TABLE provider_catalog_entries;
DROP TRIGGER trg_provider_catalog_head_delete_prd119;
DROP TRIGGER trg_provider_catalog_head_update_prd119;
DROP TRIGGER trg_provider_catalog_head_insert_prd119;
DROP TABLE provider_catalog_head;
DROP TRIGGER trg_provider_catalog_versions_delete_prd119;
DROP TRIGGER trg_provider_catalog_versions_update_prd119;
DROP TABLE provider_catalog_versions;
DROP TRIGGER trg_provider_catalog_trusted_keys_delete_prd119;
DROP TRIGGER trg_provider_catalog_trusted_keys_update_prd119;
DROP TABLE provider_catalog_trusted_keys;

DROP TRIGGER trg_platform_provider_catalog_roles_delete_prd119;
DROP TRIGGER trg_platform_provider_catalog_roles_update_prd119;
DROP TRIGGER trg_platform_provider_catalog_roles_insert_prd119;
DROP TRIGGER trg_platform_provider_catalog_permissions_delete_prd119;
DROP TRIGGER trg_platform_provider_catalog_permissions_update_prd119;
DROP TRIGGER trg_platform_provider_catalog_permissions_insert_prd119;
DROP TABLE platform_provider_catalog_role_permissions;
DROP TABLE platform_provider_catalog_permission_extensions;
DROP TRIGGER trg_workspace_provider_catalog_roles_delete_prd119;
DROP TRIGGER trg_workspace_provider_catalog_roles_update_prd119;
DROP TRIGGER trg_workspace_provider_catalog_roles_insert_prd119;
DROP TRIGGER trg_workspace_provider_catalog_permissions_delete_prd119;
DROP TRIGGER trg_workspace_provider_catalog_permissions_update_prd119;
DROP TRIGGER trg_workspace_provider_catalog_permissions_insert_prd119;
DROP TABLE workspace_provider_catalog_role_permissions;
DROP TABLE workspace_provider_catalog_permission_extensions;

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

CREATE TEMP TABLE prd119_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd119_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_provider_catalog_permission_extensions',
      'workspace_provider_catalog_role_permissions',
      'platform_provider_catalog_permission_extensions',
      'platform_provider_catalog_role_permissions','provider_catalog_trusted_keys',
      'provider_catalog_versions','provider_catalog_head','provider_catalog_entries',
      'provider_capabilities','regional_endpoint_templates','compatibility_profiles',
      'provider_catalog_instances','provider_preflight_runs',
      'workspace_provider_catalog_commands','platform_provider_catalog_commands',
      'provider_catalog_events','provider_catalog_event_outbox',
      'tenant_deletion_manifest_extensions_prd119'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd119_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
