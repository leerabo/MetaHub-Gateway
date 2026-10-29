-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd133_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd133_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0125' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM developer_contract_signing_keys)
  + (SELECT COUNT(*) FROM api_versions)
  + (SELECT COUNT(*) FROM api_contract_snapshots)
  + (SELECT COUNT(*) FROM sdk_releases)
  + (SELECT COUNT(*) FROM deprecation_notices)
  + (SELECT COUNT(*) FROM webhook_schema_versions)
  + (SELECT COUNT(*) FROM developer_contract_download_audits)
  + (SELECT COUNT(*) FROM developer_contract_events)
  + (SELECT COUNT(*) FROM developer_contract_event_outbox)
  + (SELECT COUNT(*) FROM developer_contract_action_commands)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd133_down_guard;

DROP TRIGGER trg_developer_contract_scheduler_delete_prd133;
DROP TRIGGER trg_developer_contract_scheduler_update_prd133;
DROP TABLE developer_contract_scheduler_state;
DROP TRIGGER trg_developer_contract_actions_delete_prd133;
DROP TRIGGER trg_developer_contract_actions_update_prd133;
DROP TABLE developer_contract_action_commands;
DROP TRIGGER trg_developer_contract_outbox_delete_prd133;
DROP TRIGGER trg_developer_contract_outbox_update_prd133;
DROP INDEX idx_developer_contract_outbox_due_prd133;
DROP TABLE developer_contract_event_outbox;
DROP TRIGGER trg_developer_contract_events_delete_prd133;
DROP TRIGGER trg_developer_contract_events_update_prd133;
DROP TABLE developer_contract_events;
DROP TRIGGER trg_developer_contract_downloads_delete_prd133;
DROP TRIGGER trg_developer_contract_downloads_update_prd133;
DROP TABLE developer_contract_download_audits;
DROP TRIGGER trg_webhook_schema_versions_delete_prd133;
DROP TRIGGER trg_webhook_schema_versions_update_prd133;
DROP TABLE webhook_schema_versions;
DROP TRIGGER trg_deprecation_notices_delete_prd133;
DROP TRIGGER trg_deprecation_notices_update_prd133;
DROP INDEX idx_deprecation_notices_due_prd133;
DROP TABLE deprecation_notices;
DROP TRIGGER trg_sdk_releases_delete_prd133;
DROP TRIGGER trg_sdk_releases_update_prd133;
DROP INDEX idx_sdk_releases_public_prd133;
DROP TABLE sdk_releases;
DROP TRIGGER trg_api_contract_snapshots_delete_prd133;
DROP TRIGGER trg_api_contract_snapshots_update_prd133;
DROP TABLE api_contract_snapshots;
DROP TRIGGER trg_api_versions_delete_prd133;
DROP TRIGGER trg_api_versions_update_prd133;
DROP TABLE api_versions;
DROP TRIGGER trg_developer_contract_keys_delete_prd133;
DROP TRIGGER trg_developer_contract_keys_update_prd133;
DROP TABLE developer_contract_signing_keys;
DROP TRIGGER trg_platform_developer_contract_roles_delete_prd133;
DROP TRIGGER trg_platform_developer_contract_roles_update_prd133;
DROP TRIGGER trg_platform_developer_contract_roles_insert_prd133;
DROP TRIGGER trg_platform_developer_contract_permissions_delete_prd133;
DROP TRIGGER trg_platform_developer_contract_permissions_update_prd133;
DROP TRIGGER trg_platform_developer_contract_permissions_insert_prd133;
DROP TABLE platform_developer_contract_role_permissions;
DROP TABLE platform_developer_contract_permission_extensions;

CREATE TEMP TABLE prd133_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd133_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_developer_contract_permission_extensions',
      'platform_developer_contract_role_permissions','developer_contract_signing_keys',
      'api_versions','api_contract_snapshots','sdk_releases','deprecation_notices',
      'webhook_schema_versions','developer_contract_download_audits',
      'developer_contract_events','developer_contract_event_outbox',
      'developer_contract_action_commands','developer_contract_scheduler_state'))=0
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd133_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
