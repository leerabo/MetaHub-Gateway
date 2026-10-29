-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd134_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd134_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0126' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM consent_records WHERE source_kind='privacy_center')
  + (SELECT COUNT(*) FROM privacy_preferences)
  + (SELECT COUNT(*) FROM data_subject_requests)
  + (SELECT COUNT(*) FROM data_subject_request_owner_jobs)
  + (SELECT COUNT(*) FROM identity_exports)
  + (SELECT COUNT(*) FROM erasure_decisions)
  + (SELECT COUNT(*) FROM retention_exceptions)
  + (SELECT COUNT(*) FROM privacy_events)
  + (SELECT COUNT(*) FROM privacy_event_outbox)
  + (SELECT COUNT(*) FROM privacy_action_commands)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd134_down_guard;

DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_manifest_prd134_insert;
DROP TRIGGER trg_tenant_deletion_manifest_prd134_update;
DROP TRIGGER trg_tenant_deletion_manifest_prd134_delete;
CREATE TEMP TABLE prd134_manifest_sources AS
SELECT source_table FROM tenant_deletion_manifest_extensions_prd134;
DROP TABLE tenant_deletion_manifest_extensions_prd134;
DELETE FROM tenant_deletion_trigger_manifest
WHERE source_table IN(SELECT source_table FROM prd134_manifest_sources);
DELETE FROM tenant_deletion_table_manifest
WHERE source_table IN(SELECT source_table FROM prd134_manifest_sources);
DROP TABLE prd134_manifest_sources;

DROP TRIGGER trg_privacy_scheduler_delete_prd134;
DROP TRIGGER trg_privacy_scheduler_update_prd134;
DROP TABLE privacy_scheduler_state;
DROP TRIGGER trg_privacy_commands_delete_prd134;
DROP TRIGGER trg_privacy_commands_update_prd134;
DROP TABLE privacy_action_commands;
DROP TRIGGER trg_privacy_outbox_delete_prd134;
DROP TRIGGER trg_privacy_outbox_update_prd134;
DROP INDEX idx_privacy_outbox_due_prd134;
DROP TABLE privacy_event_outbox;
DROP TRIGGER trg_privacy_events_delete_prd134;
DROP TRIGGER trg_privacy_events_update_prd134;
DROP INDEX idx_privacy_events_owner_prd134;
DROP TABLE privacy_events;
DROP TRIGGER trg_retention_exceptions_delete_prd134;
DROP TRIGGER trg_retention_exceptions_update_prd134;
DROP TABLE retention_exceptions;
DROP TRIGGER trg_erasure_decisions_delete_prd134;
DROP TRIGGER trg_erasure_decisions_update_prd134;
DROP TABLE erasure_decisions;
DROP TRIGGER trg_identity_exports_delete_prd134;
DROP TRIGGER trg_identity_exports_update_prd134;
DROP INDEX idx_identity_exports_expiry_prd134;
DROP TABLE identity_exports;
DROP TRIGGER trg_privacy_owner_jobs_delete_prd134;
DROP TRIGGER trg_privacy_owner_jobs_update_prd134;
DROP INDEX idx_privacy_owner_jobs_due_prd134;
DROP TABLE data_subject_request_owner_jobs;
DROP TRIGGER trg_privacy_owner_adapters_delete_prd134;
DROP TRIGGER trg_privacy_owner_adapters_update_prd134;
DROP INDEX uq_privacy_owner_adapter_active_prd134;
DROP TABLE privacy_owner_adapter_versions;
DROP TRIGGER trg_data_subject_requests_delete_prd134;
DROP TRIGGER trg_data_subject_requests_update_prd134;
DROP INDEX idx_data_subject_requests_queue_prd134;
DROP INDEX idx_data_subject_requests_owner_prd134;
DROP TABLE data_subject_requests;
DROP TRIGGER trg_privacy_preferences_delete_prd134;
DROP TRIGGER trg_privacy_preferences_update_prd134;
DROP INDEX idx_privacy_preferences_owner_prd134;
DROP TABLE privacy_preferences;

DROP TRIGGER trg_consent_records_delete_prd134;
DROP TRIGGER trg_consent_records_update_prd134;
DROP INDEX idx_consent_records_account_prd134;
ALTER TABLE consent_records RENAME TO consent_records_prd134;
CREATE TABLE consent_records(
  consent_record_id TEXT PRIMARY KEY CHECK(length(consent_record_id)=36
    AND substr(consent_record_id,1,4)='con_'
    AND substr(consent_record_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  consent_type TEXT NOT NULL CHECK(consent_type='privacy_terms'),
  consent_version TEXT NOT NULL CHECK(length(consent_version) BETWEEN 1 AND 80),
  source_registration_intent_id TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  consented_at TEXT NOT NULL CHECK(unixepoch(consented_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(source_registration_intent_id)
    REFERENCES registration_intents(registration_intent_id),
  UNIQUE(account_id,consent_type,consent_version,source_registration_intent_id)
);
INSERT INTO consent_records(
  consent_record_id,account_id,consent_type,consent_version,
  source_registration_intent_id,payload_sha256,consented_at)
SELECT consent_record_id,account_id,consent_type,consent_version,
  source_registration_intent_id,payload_sha256,consented_at
FROM consent_records_prd134 WHERE source_kind='registration';
DROP TABLE consent_records_prd134;
CREATE TRIGGER trg_consent_records_update_prd092 BEFORE UPDATE ON consent_records
BEGIN SELECT RAISE(ABORT,'consent_record_immutable'); END;
CREATE TRIGGER trg_consent_records_delete_prd092 BEFORE DELETE ON consent_records
BEGIN SELECT RAISE(ABORT,'consent_record_immutable'); END;

DROP TRIGGER trg_workspace_privacy_roles_delete_prd134;
DROP TRIGGER trg_workspace_privacy_roles_update_prd134;
DROP TRIGGER trg_workspace_privacy_roles_insert_prd134;
DROP TRIGGER trg_workspace_privacy_permissions_delete_prd134;
DROP TRIGGER trg_workspace_privacy_permissions_update_prd134;
DROP TRIGGER trg_workspace_privacy_permissions_insert_prd134;
DROP TABLE workspace_privacy_role_permissions;
DROP TABLE workspace_privacy_permission_extensions;

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

CREATE TEMP TABLE prd134_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd134_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_privacy_permission_extensions','workspace_privacy_role_permissions',
      'privacy_preferences','data_subject_requests','privacy_owner_adapter_versions',
      'data_subject_request_owner_jobs','identity_exports','erasure_decisions',
      'retention_exceptions','privacy_events','privacy_event_outbox',
      'privacy_action_commands','privacy_scheduler_state',
      'tenant_deletion_manifest_extensions_prd134'))=0 THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd134_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
