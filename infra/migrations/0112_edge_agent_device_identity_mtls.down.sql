-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd120_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd120_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0112' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM edge_deployments)
  + (SELECT COUNT(*) FROM edge_registration_intents)
  + (SELECT COUNT(*) FROM edge_device_identities)
  + (SELECT COUNT(*) FROM edge_device_certificates)
  + (SELECT COUNT(*) FROM edge_sessions)
  + (SELECT COUNT(*) FROM workspace_edge_commands)
  + (SELECT COUNT(*) FROM edge_machine_commands)
  + (SELECT COUNT(*) FROM edge_identity_events)
  + (SELECT COUNT(*) FROM edge_identity_event_outbox)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd120_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd120_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd120_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd120_delete;
CREATE TEMP TABLE prd120_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd120;
DROP TABLE tenant_deletion_manifest_extensions_prd120;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd120_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd120_manifest_sources);
DROP TABLE prd120_manifest_sources;

DROP INDEX idx_edge_identity_outbox_delivery_prd120;
DROP TABLE edge_identity_event_outbox;
DROP TRIGGER trg_edge_identity_events_delete_prd120;
DROP TRIGGER trg_edge_identity_events_update_prd120;
DROP INDEX idx_edge_identity_events_owner_prd120;
DROP TABLE edge_identity_events;
DROP TRIGGER trg_edge_machine_commands_delete_prd120;
DROP TRIGGER trg_edge_machine_commands_update_prd120;
DROP TABLE edge_machine_commands;
DROP TRIGGER trg_workspace_edge_commands_delete_prd120;
DROP TRIGGER trg_workspace_edge_commands_update_prd120;
DROP TABLE workspace_edge_commands;
DROP INDEX idx_edge_sessions_active_prd120;
DROP TABLE edge_sessions;
DROP INDEX idx_edge_registration_expiry_prd120;
DROP TABLE edge_registration_intents;
DROP INDEX idx_edge_certificates_owner_prd120;
DROP TABLE edge_device_certificates;
DROP INDEX idx_edge_identity_active_prd120;
DROP TABLE edge_device_identities;
DROP INDEX idx_edge_deployments_fleet_prd120;
DROP INDEX idx_edge_deployments_owner_prd120;
DROP TABLE edge_deployments;

DROP TRIGGER trg_platform_edge_roles_delete_prd120;
DROP TRIGGER trg_platform_edge_roles_update_prd120;
DROP TRIGGER trg_platform_edge_roles_insert_prd120;
DROP TRIGGER trg_platform_edge_permissions_delete_prd120;
DROP TRIGGER trg_platform_edge_permissions_update_prd120;
DROP TRIGGER trg_platform_edge_permissions_insert_prd120;
DROP TABLE platform_edge_role_permissions;
DROP TABLE platform_edge_permission_extensions;
DROP TRIGGER trg_workspace_edge_roles_delete_prd120;
DROP TRIGGER trg_workspace_edge_roles_update_prd120;
DROP TRIGGER trg_workspace_edge_roles_insert_prd120;
DROP TRIGGER trg_workspace_edge_permissions_delete_prd120;
DROP TRIGGER trg_workspace_edge_permissions_update_prd120;
DROP TRIGGER trg_workspace_edge_permissions_insert_prd120;
DROP TABLE workspace_edge_role_permissions;
DROP TABLE workspace_edge_permission_extensions;

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

CREATE TEMP TABLE prd120_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd120_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_permission_extensions','workspace_edge_role_permissions',
      'platform_edge_permission_extensions','platform_edge_role_permissions',
      'edge_deployments','edge_registration_intents','edge_device_identities',
      'edge_device_certificates','edge_sessions','workspace_edge_commands',
      'edge_machine_commands','edge_identity_events','edge_identity_event_outbox',
      'tenant_deletion_manifest_extensions_prd120'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd120_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
