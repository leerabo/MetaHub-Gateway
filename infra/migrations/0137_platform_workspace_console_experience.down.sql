-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd145_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd145_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0137' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM platform_dashboard_projection_heads)
  + (SELECT COUNT(*) FROM platform_dashboard_projection_sources)
  + (SELECT COUNT(*) FROM platform_dashboard_projection_versions)
  + (SELECT COUNT(*) FROM workspace_dashboard_projection_heads)
  + (SELECT COUNT(*) FROM workspace_dashboard_projection_sources)
  + (SELECT COUNT(*) FROM workspace_dashboard_projection_versions)
  + (SELECT COUNT(*) FROM platform_domain_approval_queue_items)
  + (SELECT COUNT(*) FROM platform_domain_approval_source_cursors)
  + (SELECT COUNT(*) FROM platform_domain_approval_rejections)
  + (SELECT COUNT(*) FROM workspace_domain_approval_queue_items)
  + (SELECT COUNT(*) FROM workspace_domain_approval_source_cursors)
  + (SELECT COUNT(*) FROM workspace_domain_approval_rejections)
  + (SELECT COUNT(*) FROM platform_assistant_query_projections)
  + (SELECT COUNT(*) FROM workspace_assistant_query_projections)
  + (SELECT COUNT(*) FROM account_preferences)
  + (SELECT COUNT(*) FROM account_preference_commands)
  + (SELECT COUNT(*) FROM account_preference_events)
  + (SELECT COUNT(*) FROM account_preference_event_outbox)
  + (SELECT COUNT(*) FROM account_preference_source_mappings)
  + (SELECT COUNT(*) FROM workspace_deployment_diagnostic_heads)
  + (SELECT COUNT(*) FROM workspace_deployment_diagnostic_versions)
  + (SELECT COUNT(*) FROM platform_console_events)
  + (SELECT COUNT(*) FROM platform_console_event_outbox)
  + (SELECT COUNT(*) FROM workspace_console_events)
  + (SELECT COUNT(*) FROM workspace_console_event_outbox)
  + (SELECT COUNT(*) FROM console_experience_read_audits)
  + (SELECT COUNT(*) FROM console_experience_reconciliations)
  + CASE WHEN (SELECT COUNT(*) FROM platform_console_permission_extensions)=3
      AND (SELECT COUNT(*) FROM platform_console_role_permissions)=14
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_console_permission_extensions)=5
      AND (SELECT COUNT(*) FROM workspace_console_role_permissions)=24
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM console_experience_scheduler_state
      WHERE enabled=1 AND fence_generation=1 AND lease_owner_id IS NULL
        AND lease_token_hmac IS NULL AND lease_expires_at IS NULL
        AND attempt_count=0 AND cursor_value IS NULL AND last_error_code IS NULL)=10
    THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM console_experience_migration_cursors
      WHERE source_row_count=0 AND target_row_count=0
        AND owner_coverage_count=0 AND generation=1)=6
    THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd145_down_guard;

DROP TABLE console_experience_reconciliations;
DROP TABLE console_experience_migration_cursors;
DROP TABLE console_experience_scheduler_state;
DROP TABLE console_experience_read_audits;
DROP TABLE workspace_console_event_outbox;
DROP TABLE workspace_console_events;
DROP TABLE platform_console_event_outbox;
DROP TABLE platform_console_events;
DROP TABLE workspace_deployment_diagnostic_heads;
DROP TABLE workspace_deployment_diagnostic_versions;
DROP TRIGGER trg_global_account_preference_insert_prd145;
DROP TABLE account_preference_source_mappings;
DROP TABLE account_preference_event_outbox;
DROP TABLE account_preference_events;
DROP TABLE account_preference_commands;
DROP TABLE account_preferences;
DROP TABLE workspace_assistant_query_projections;
DROP TABLE platform_assistant_query_projections;
DROP TABLE workspace_domain_approval_rejections;
DROP TABLE workspace_domain_approval_source_cursors;
DROP TABLE workspace_domain_approval_queue_items;
DROP TABLE platform_domain_approval_rejections;
DROP TABLE platform_domain_approval_source_cursors;
DROP TABLE platform_domain_approval_queue_items;
DROP TABLE workspace_dashboard_projection_sources;
DROP TABLE workspace_dashboard_projection_heads;
DROP TABLE workspace_dashboard_projection_versions;
DROP TABLE platform_dashboard_projection_sources;
DROP TABLE platform_dashboard_projection_heads;
DROP TABLE platform_dashboard_projection_versions;
DROP TABLE workspace_console_role_permissions;
DROP TABLE workspace_console_permission_extensions;
DROP TABLE platform_console_role_permissions;
DROP TABLE platform_console_permission_extensions;

CREATE TEMP TABLE prd145_down_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd145_down_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
    'platform_console_permission_extensions','platform_console_role_permissions',
    'workspace_console_permission_extensions','workspace_console_role_permissions',
    'platform_dashboard_projection_versions','platform_dashboard_projection_heads',
    'platform_dashboard_projection_sources','workspace_dashboard_projection_versions',
    'workspace_dashboard_projection_heads','workspace_dashboard_projection_sources',
    'platform_domain_approval_queue_items','platform_domain_approval_source_cursors',
    'platform_domain_approval_rejections','workspace_domain_approval_queue_items',
    'workspace_domain_approval_source_cursors','workspace_domain_approval_rejections',
    'platform_assistant_query_projections','workspace_assistant_query_projections',
    'account_preferences','account_preference_commands','account_preference_events',
    'account_preference_event_outbox','account_preference_source_mappings',
    'workspace_deployment_diagnostic_versions',
    'workspace_deployment_diagnostic_heads','platform_console_events',
    'platform_console_event_outbox','workspace_console_events',
    'workspace_console_event_outbox','console_experience_read_audits',
    'console_experience_scheduler_state','console_experience_migration_cursors',
    'console_experience_reconciliations'));
DROP TABLE prd145_down_post_guard;

COMMIT;
PRAGMA foreign_keys = ON;
