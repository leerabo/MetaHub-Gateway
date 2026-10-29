-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd146_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd146_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0138' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM workspace_navigation_contract_heads)
  + (SELECT COUNT(*) FROM workspace_navigation_contract_items)
  + (SELECT COUNT(*) FROM workspace_navigation_contract_snapshots)
  + (SELECT COUNT(*) FROM workspace_navigation_projection_heads)
  + (SELECT COUNT(*) FROM workspace_navigation_projection_items)
  + (SELECT COUNT(*) FROM workspace_navigation_projection_sources)
  + (SELECT COUNT(*) FROM workspace_navigation_projection_versions)
  + (SELECT COUNT(*) FROM workspace_role_task_home_heads)
  + (SELECT COUNT(*) FROM workspace_role_task_home_tasks)
  + (SELECT COUNT(*) FROM workspace_role_task_home_sources)
  + (SELECT COUNT(*) FROM workspace_role_task_home_versions)
  + (SELECT COUNT(*) FROM workspace_navigation_preferences)
  + (SELECT COUNT(*) FROM workspace_navigation_preference_commands)
  + (SELECT COUNT(*) FROM workspace_navigation_preference_source_mappings)
  + (SELECT COUNT(*) FROM workspace_advanced_tool_entitlement_heads)
  + (SELECT COUNT(*) FROM workspace_advanced_tool_entitlement_sources)
  + (SELECT COUNT(*) FROM workspace_advanced_tool_entitlement_versions)
  + (SELECT COUNT(*) FROM workspace_navigation_member_states)
  + (SELECT COUNT(*) FROM workspace_navigation_events)
  + (SELECT COUNT(*) FROM workspace_navigation_event_outbox)
  + (SELECT COUNT(*) FROM workspace_navigation_read_audits)
  + (SELECT COUNT(*) FROM workspace_navigation_reconciliations)
  + CASE WHEN (SELECT COUNT(*) FROM workspace_navigation_permission_extensions)=4
      AND (SELECT COUNT(*) FROM workspace_navigation_role_permissions)=26 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_navigation_scheduler_state
      WHERE enabled=1 AND fence_generation=1 AND lease_owner_id IS NULL
        AND lease_token_hmac IS NULL AND lease_expires_at IS NULL
        AND attempt_count=0 AND cursor_value IS NULL AND last_error_code IS NULL
        AND source_watermark='none')=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_navigation_migration_cursors
      WHERE target_row_count=0 AND owner_coverage_count=0 AND generation=1)=5
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd146_down_guard;

DROP TRIGGER trg_workspace_membership_revoke_navigation_prd146;
DROP TRIGGER trg_workspace_navigation_audit_delete_prd146;
DROP TRIGGER trg_workspace_navigation_audit_update_prd146;
DROP TRIGGER trg_workspace_navigation_event_delete_prd146;
DROP TRIGGER trg_workspace_navigation_event_update_prd146;
DROP TRIGGER trg_workspace_navigation_preference_owner_prd146;
DROP TRIGGER trg_workspace_advanced_tool_delete_prd146;
DROP TRIGGER trg_workspace_advanced_tool_update_prd146;
DROP TRIGGER trg_workspace_role_task_delete_prd146;
DROP TRIGGER trg_workspace_role_task_update_prd146;
DROP TRIGGER trg_workspace_role_task_version_delete_prd146;
DROP TRIGGER trg_workspace_role_task_version_update_prd146;
DROP TRIGGER trg_workspace_navigation_projection_item_delete_prd146;
DROP TRIGGER trg_workspace_navigation_projection_item_update_prd146;
DROP TRIGGER trg_workspace_navigation_projection_delete_prd146;
DROP TRIGGER trg_workspace_navigation_projection_update_prd146;
DROP TRIGGER trg_workspace_navigation_item_delete_prd146;
DROP TRIGGER trg_workspace_navigation_item_update_prd146;
DROP TRIGGER trg_workspace_navigation_contract_delete_prd146;
DROP TRIGGER trg_workspace_navigation_contract_update_prd146;
DROP TRIGGER trg_workspace_navigation_role_delete_prd146;
DROP TRIGGER trg_workspace_navigation_role_update_prd146;
DROP TRIGGER trg_workspace_navigation_role_insert_prd146;
DROP TRIGGER trg_workspace_navigation_permission_delete_prd146;
DROP TRIGGER trg_workspace_navigation_permission_update_prd146;
DROP TRIGGER trg_workspace_navigation_permission_insert_prd146;

DROP TABLE workspace_navigation_reconciliations;
DROP TABLE workspace_navigation_migration_cursors;
DROP TABLE workspace_navigation_scheduler_state;
DROP TABLE workspace_navigation_read_audits;
DROP TABLE workspace_navigation_event_outbox;
DROP TABLE workspace_navigation_events;
DROP TABLE workspace_navigation_member_states;
DROP TABLE workspace_advanced_tool_entitlement_sources;
DROP TABLE workspace_advanced_tool_entitlement_heads;
DROP TABLE workspace_advanced_tool_entitlement_versions;
DROP TABLE workspace_navigation_preference_source_mappings;
DROP TABLE workspace_navigation_preference_commands;
DROP TABLE workspace_navigation_preferences;
DROP TABLE workspace_role_task_home_sources;
DROP TABLE workspace_role_task_home_heads;
DROP TABLE workspace_role_task_home_tasks;
DROP TABLE workspace_role_task_home_versions;
DROP TABLE workspace_navigation_projection_sources;
DROP TABLE workspace_navigation_projection_items;
DROP TABLE workspace_navigation_projection_heads;
DROP TABLE workspace_navigation_projection_versions;
DROP TABLE workspace_navigation_contract_heads;
DROP TABLE workspace_navigation_contract_items;
DROP TABLE workspace_navigation_contract_snapshots;
DROP TABLE workspace_navigation_role_permissions;
DROP TABLE workspace_navigation_permission_extensions;

CREATE TEMP TABLE prd146_down_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd146_down_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
    'workspace_navigation_permission_extensions','workspace_navigation_role_permissions',
    'workspace_navigation_contract_snapshots','workspace_navigation_contract_items',
    'workspace_navigation_contract_heads','workspace_navigation_projection_versions',
    'workspace_navigation_projection_heads','workspace_navigation_projection_items',
    'workspace_navigation_projection_sources','workspace_role_task_home_versions',
    'workspace_role_task_home_tasks','workspace_role_task_home_heads',
    'workspace_role_task_home_sources','workspace_navigation_preferences',
    'workspace_navigation_preference_commands','workspace_navigation_preference_source_mappings',
    'workspace_advanced_tool_entitlement_versions','workspace_advanced_tool_entitlement_heads',
    'workspace_advanced_tool_entitlement_sources','workspace_navigation_member_states',
    'workspace_navigation_events','workspace_navigation_event_outbox',
    'workspace_navigation_read_audits','workspace_navigation_scheduler_state',
    'workspace_navigation_migration_cursors','workspace_navigation_reconciliations'));
DROP TABLE prd146_down_post_guard;

COMMIT;
PRAGMA foreign_keys = ON;
