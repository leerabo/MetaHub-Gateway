-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd136_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd136_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0128' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM release_sbom_reports)
  + (SELECT COUNT(*) FROM release_build_provenance)
  + (SELECT COUNT(*) FROM supply_chain_signing_keys)
  + (SELECT COUNT(*) FROM release_artifact_signatures)
  + (SELECT COUNT(*) FROM vulnerability_findings)
  + (SELECT COUNT(*) FROM supply_chain_security_exceptions)
  + (SELECT COUNT(*) FROM patch_releases)
  + (SELECT COUNT(*) FROM supply_chain_events)
  + (SELECT COUNT(*) FROM supply_chain_event_outbox)
  + (SELECT COUNT(*) FROM supply_chain_action_commands)
  + CASE WHEN (SELECT COUNT(*) FROM platform_developer_contract_role_permissions
      WHERE role_code IN('platform_security','platform_compliance')
        AND permission_code='platform.developer_contract.read')=2 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd136_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd136_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd136_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd136_delete;
CREATE TEMP TABLE prd136_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd136;
DROP TABLE tenant_deletion_manifest_extensions_prd136;
DELETE FROM tenant_deletion_trigger_manifest;
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd136_manifest_sources);
DROP TABLE prd136_manifest_sources;

DROP TRIGGER trg_supply_chain_scheduler_delete_prd136;
DROP TRIGGER trg_supply_chain_scheduler_update_prd136;
DROP TABLE supply_chain_scheduler_state;
DROP TRIGGER trg_supply_chain_commands_delete_prd136;
DROP TRIGGER trg_supply_chain_commands_update_prd136;
DROP TABLE supply_chain_action_commands;
DROP TRIGGER trg_supply_chain_outbox_delete_prd136;
DROP TRIGGER trg_supply_chain_outbox_update_prd136;
DROP INDEX idx_supply_chain_outbox_due_prd136;
DROP TABLE supply_chain_event_outbox;
DROP TRIGGER trg_supply_chain_events_delete_prd136;
DROP TRIGGER trg_supply_chain_events_update_prd136;
DROP TABLE supply_chain_events;
DROP TRIGGER trg_patch_release_delete_prd136;
DROP TRIGGER trg_patch_release_update_prd136;
DROP TRIGGER trg_patch_release_insert_prd136;
DROP INDEX idx_patch_releases_owner_prd136;
DROP TABLE patch_releases;
DROP TRIGGER trg_supply_chain_exception_delete_prd136;
DROP TRIGGER trg_supply_chain_exception_update_prd136;
DROP TRIGGER trg_supply_chain_exception_insert_prd136;
DROP INDEX idx_supply_chain_exceptions_owner_prd136;
DROP TABLE supply_chain_security_exceptions;
DROP TRIGGER trg_vulnerability_finding_delete_prd136;
DROP TRIGGER trg_vulnerability_finding_update_prd136;
DROP INDEX idx_vulnerability_findings_release_prd136;
DROP TABLE vulnerability_findings;
DROP TRIGGER trg_release_signature_delete_prd136;
DROP TRIGGER trg_release_signature_update_prd136;
DROP TABLE release_artifact_signatures;
DROP TRIGGER trg_supply_chain_keys_delete_prd136;
DROP TRIGGER trg_supply_chain_keys_update_prd136;
DROP TABLE supply_chain_signing_keys;
DROP TRIGGER trg_release_provenance_delete_prd136;
DROP TRIGGER trg_release_provenance_update_prd136;
DROP TABLE release_build_provenance;
DROP TRIGGER trg_release_sbom_delete_prd136;
DROP TRIGGER trg_release_sbom_update_prd136;
DROP TABLE release_sbom_reports;
DROP TRIGGER trg_supply_chain_feed_delete_prd136;
DROP TRIGGER trg_supply_chain_feed_update_prd136;
DROP TRIGGER trg_supply_chain_feed_insert_prd136;
DROP TABLE supply_chain_feed_registry;
DROP TRIGGER trg_platform_supply_chain_roles_delete_prd136;
DROP TRIGGER trg_platform_supply_chain_roles_update_prd136;
DROP TRIGGER trg_platform_supply_chain_roles_insert_prd136;
DROP TRIGGER trg_platform_supply_chain_permissions_delete_prd136;
DROP TRIGGER trg_platform_supply_chain_permissions_update_prd136;
DROP TRIGGER trg_platform_supply_chain_permissions_insert_prd136;
DROP TRIGGER trg_platform_developer_contract_roles_insert_prd133;
DROP TRIGGER trg_platform_developer_contract_roles_update_prd133;
DROP TRIGGER trg_platform_developer_contract_roles_delete_prd133;
ALTER TABLE platform_developer_contract_role_permissions
  RENAME TO platform_developer_contract_role_permissions_prd136_current;
CREATE TABLE platform_developer_contract_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('platform_owner','release_manager')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_developer_contract_permission_extensions(permission_code)
);
INSERT INTO platform_developer_contract_role_permissions
SELECT role_code,permission_code
FROM platform_developer_contract_role_permissions_prd136_current
WHERE role_code IN('platform_owner','release_manager');
DROP TABLE platform_developer_contract_role_permissions_prd136_current;
CREATE TRIGGER trg_platform_developer_contract_roles_insert_prd133
BEFORE INSERT ON platform_developer_contract_role_permissions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_developer_contract_roles_update_prd133
BEFORE UPDATE ON platform_developer_contract_role_permissions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_developer_contract_roles_delete_prd133
BEFORE DELETE ON platform_developer_contract_role_permissions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
DROP TABLE platform_supply_chain_role_permissions;
DROP TABLE platform_supply_chain_permission_extensions;

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

CREATE TEMP TABLE prd136_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd136_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_supply_chain_permission_extensions','platform_supply_chain_role_permissions',
      'supply_chain_feed_registry','release_sbom_reports','release_build_provenance',
      'supply_chain_signing_keys','release_artifact_signatures','vulnerability_findings',
      'supply_chain_security_exceptions','patch_releases','supply_chain_events',
      'supply_chain_event_outbox','supply_chain_action_commands',
      'supply_chain_scheduler_state','tenant_deletion_manifest_extensions_prd136'))=0
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd136_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
