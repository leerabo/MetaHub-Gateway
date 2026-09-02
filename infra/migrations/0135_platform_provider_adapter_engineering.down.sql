-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd143_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd143_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0135' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM provider_adapter_releases)
  + (SELECT COUNT(*) FROM provider_adapter_release_heads)
  + (SELECT COUNT(*) FROM platform_adapter_contract_run_projections)
  + (SELECT COUNT(*) FROM platform_adapter_contract_case_projections)
  + (SELECT COUNT(*) FROM workspace_provider_error_overrides)
  + (SELECT COUNT(*) FROM workspace_provider_error_override_versions)
  + (SELECT COUNT(*) FROM workspace_provider_error_override_heads)
  + (SELECT COUNT(*) FROM provider_compatibility_projections)
  + (SELECT COUNT(*) FROM provider_engineering_legacy_mappings)
  + (SELECT COUNT(*) FROM provider_engineering_commands)
  + (SELECT COUNT(*) FROM provider_engineering_events)
  + (SELECT COUNT(*) FROM provider_engineering_event_outbox)
  + CASE WHEN (SELECT COUNT(*) FROM platform_builtin_error_catalog_releases release
      WHERE release.status='legacy_reconciled' AND release.release_generation=0
        AND release.candidate_by_operator_id IS NULL
        AND release.security_approved_by_operator_id IS NULL
        AND release.released_by_operator_id IS NULL)=
      (SELECT COUNT(*) FROM platform_builtin_error_catalog_releases) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM provider_engineering_scheduler_state
      WHERE cursor_value IS NULL AND fence_generation=1 AND lease_owner_id IS NULL
        AND lease_token_hmac IS NULL AND lease_expires_at IS NULL)=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd143_down_guard;

DROP TRIGGER trg_provider_engineering_command_delete_prd143;
DROP TRIGGER trg_provider_engineering_command_update_prd143;
DROP TRIGGER trg_provider_engineering_event_delete_prd143;
DROP TRIGGER trg_provider_engineering_event_update_prd143;
DROP TRIGGER trg_provider_engineering_mapping_delete_prd143;
DROP TRIGGER trg_provider_engineering_mapping_update_prd143;
DROP TRIGGER trg_workspace_provider_override_version_delete_prd143;
DROP TRIGGER trg_workspace_provider_override_version_update_prd143;
DROP TRIGGER trg_platform_contract_case_delete_prd143;
DROP TRIGGER trg_platform_contract_case_update_prd143;
DROP TRIGGER trg_provider_adapter_release_delete_prd143;
DROP TRIGGER trg_provider_adapter_release_owner_prd143;
DROP TRIGGER trg_workspace_provider_engineering_roles_delete_prd143;
DROP TRIGGER trg_workspace_provider_engineering_roles_update_prd143;
DROP TRIGGER trg_workspace_provider_engineering_roles_insert_prd143;
DROP TRIGGER trg_workspace_provider_engineering_permissions_delete_prd143;
DROP TRIGGER trg_workspace_provider_engineering_permissions_update_prd143;
DROP TRIGGER trg_workspace_provider_engineering_permissions_insert_prd143;
DROP TRIGGER trg_platform_provider_engineering_roles_delete_prd143;
DROP TRIGGER trg_platform_provider_engineering_roles_update_prd143;
DROP TRIGGER trg_platform_provider_engineering_roles_insert_prd143;
DROP TRIGGER trg_platform_provider_engineering_permissions_delete_prd143;
DROP TRIGGER trg_platform_provider_engineering_permissions_update_prd143;
DROP TRIGGER trg_platform_provider_engineering_permissions_insert_prd143;

DROP TABLE provider_engineering_scheduler_state;
DROP TABLE provider_engineering_event_outbox;
DROP TABLE provider_engineering_events;
DROP TABLE provider_engineering_commands;
DROP TABLE provider_engineering_legacy_mappings;
DROP TABLE provider_compatibility_projections;
DROP TABLE workspace_provider_error_override_heads;
DROP TABLE workspace_provider_error_override_versions;
DROP TABLE workspace_provider_error_overrides;
DROP TABLE platform_builtin_error_catalog_heads;
DROP TABLE platform_builtin_error_catalog_releases;
DROP TABLE platform_adapter_contract_case_projections;
DROP TABLE platform_adapter_contract_run_projections;
DROP TABLE provider_adapter_release_heads;
DROP TABLE provider_adapter_releases;
DROP TABLE workspace_provider_engineering_role_permissions;
DROP TABLE workspace_provider_engineering_permission_extensions;
DROP TABLE platform_provider_engineering_role_permissions;
DROP TABLE platform_provider_engineering_permission_extensions;

CREATE TEMP TABLE prd143_down_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd143_down_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd143_down_post_guard;

COMMIT;
PRAGMA foreign_keys = ON;
