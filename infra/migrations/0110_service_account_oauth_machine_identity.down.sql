-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd118_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd118_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0110' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM service_accounts)
  + (SELECT COUNT(*) FROM oauth_clients)
  + (SELECT COUNT(*) FROM client_credentials)
  + (SELECT COUNT(*) FROM access_token_grants)
  + (SELECT COUNT(*) FROM machine_identity_audits)
  + (SELECT COUNT(*) FROM machine_identity_event_outbox)
  + (SELECT COUNT(*) FROM machine_identity_commands)
  + (SELECT COUNT(*) FROM oauth_token_attempt_guards)
  + CASE WHEN (SELECT COUNT(*) FROM service_account_scopes)=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(SELECT 1 FROM workspace_memberships
      WHERE role_code='service_account' AND status='active') THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd118_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd118_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd118_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd118_delete;
CREATE TEMP TABLE prd118_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd118;
DROP TABLE tenant_deletion_manifest_extensions_prd118;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd118_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd118_manifest_sources);
DROP TABLE prd118_manifest_sources;

DROP TABLE oauth_token_attempt_guards;
DROP TRIGGER trg_machine_identity_commands_delete_prd118;
DROP TRIGGER trg_machine_identity_commands_update_prd118;
DROP TRIGGER trg_machine_identity_commands_insert_prd118;
DROP INDEX idx_machine_identity_commands_owner_prd118;
DROP TABLE machine_identity_commands;
DROP TRIGGER trg_machine_identity_outbox_delete_prd118;
DROP TRIGGER trg_machine_identity_outbox_update_prd118;
DROP TRIGGER trg_machine_identity_outbox_insert_prd118;
DROP INDEX idx_machine_identity_outbox_delivery_prd118;
DROP TABLE machine_identity_event_outbox;
DROP TRIGGER trg_machine_identity_audits_delete_prd118;
DROP TRIGGER trg_machine_identity_audits_update_prd118;
DROP TRIGGER trg_machine_identity_audits_insert_prd118;
DROP INDEX idx_machine_identity_audits_owner_prd118;
DROP TABLE machine_identity_audits;
DROP TRIGGER trg_access_token_grants_delete_prd118;
DROP TRIGGER trg_access_token_grants_update_prd118;
DROP TRIGGER trg_access_token_grants_insert_prd118;
DROP INDEX idx_access_token_grants_owner_prd118;
DROP INDEX idx_access_token_grants_auth_prd118;
DROP TABLE access_token_grants;
DROP TRIGGER trg_client_credentials_delete_prd118;
DROP TRIGGER trg_client_credentials_update_prd118;
DROP TRIGGER trg_client_credentials_insert_prd118;
DROP INDEX idx_client_credentials_owner_prd118;
DROP INDEX uq_client_credential_active_prd118;
DROP TABLE client_credentials;
DROP TRIGGER trg_oauth_clients_delete_prd118;
DROP TRIGGER trg_oauth_clients_update_prd118;
DROP INDEX idx_oauth_clients_owner_prd118;
DROP TABLE oauth_clients;
DROP TRIGGER trg_service_account_scopes_delete_prd118;
DROP TRIGGER trg_service_account_scopes_update_prd118;
DROP TRIGGER trg_service_account_scopes_insert_prd118;
DROP TABLE service_account_scopes;
DROP TRIGGER trg_service_accounts_delete_prd118;
DROP TRIGGER trg_service_accounts_update_prd118;
DROP INDEX idx_service_accounts_owner_prd118;
DROP TABLE service_accounts;
DROP TRIGGER trg_machine_permission_catalog_delete_prd118;
DROP TRIGGER trg_machine_permission_catalog_update_prd118;
DROP TRIGGER trg_machine_permission_catalog_insert_prd118;
DROP TABLE machine_permission_catalog;
DROP TRIGGER trg_workspace_machine_roles_delete_prd118;
DROP TRIGGER trg_workspace_machine_roles_update_prd118;
DROP TRIGGER trg_workspace_machine_roles_insert_prd118;
DROP TRIGGER trg_workspace_machine_permissions_delete_prd118;
DROP TRIGGER trg_workspace_machine_permissions_update_prd118;
DROP TRIGGER trg_workspace_machine_permissions_insert_prd118;
DROP TABLE workspace_machine_role_permissions;
DROP TABLE workspace_machine_permission_extensions;

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

CREATE TEMP TABLE prd118_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd118_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'service_accounts','service_account_scopes','oauth_clients','client_credentials',
      'access_token_grants','machine_identity_audits','machine_identity_event_outbox',
      'oauth_token_attempt_guards','machine_identity_commands','machine_permission_catalog',
      'workspace_machine_permission_extensions','workspace_machine_role_permissions',
      'tenant_deletion_manifest_extensions_prd118'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd118_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
