-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd096_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd096_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0088' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM workspace_operational_states
      WHERE source!='migration_backfill' OR status NOT IN(
        'active','deletion_pending','deleted','migration_conflict')
        OR revision!=1 OR risk_revision!=1)
  + (SELECT COUNT(*) FROM workspace_risk_tags)
  + (SELECT COUNT(*) FROM platform_action_intents)
  + (SELECT COUNT(*) FROM workspace_owner_notification_outbox)
  + (SELECT COUNT(*) FROM workspace_operational_events)
  + (SELECT COUNT(*) FROM workspace_operational_event_outbox)
  + (SELECT COUNT(*) FROM workspace_sessions WHERE revoked_reason='platform_suspended')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd096_down_guard;

DROP TRIGGER trg_customer_health_workspace_projection_prd096;
DROP TRIGGER trg_tenant_data_operational_transition_prd096;
DROP TRIGGER trg_workspace_binding_operational_initialize_prd096;
DROP TRIGGER trg_workspace_operational_outbox_delete_prd096;
DROP TRIGGER trg_workspace_operational_outbox_transition_prd096;
DROP TRIGGER trg_workspace_operational_event_outbox_prd096;
DROP TRIGGER trg_workspace_operational_events_delete_prd096;
DROP TRIGGER trg_workspace_operational_events_update_prd096;
DROP TRIGGER trg_workspace_notification_delete_prd096;
DROP TRIGGER trg_workspace_notification_transition_prd096;
DROP TRIGGER trg_workspace_notification_owner_prd096;
DROP TRIGGER trg_platform_action_intents_delete_prd096;
DROP TRIGGER trg_platform_action_intents_update_prd096;
DROP TRIGGER trg_workspace_health_delete_prd096;
DROP TRIGGER trg_workspace_health_transition_prd096;
DROP TRIGGER trg_workspace_risk_tags_delete_prd096;
DROP TRIGGER trg_workspace_risk_tags_transition_prd096;
DROP TRIGGER trg_workspace_risk_tags_owner_insert_prd096;
DROP TRIGGER trg_workspace_session_operational_update_prd096;
DROP TRIGGER trg_workspace_session_operational_insert_prd096;
DROP TRIGGER trg_workspace_operational_delete_prd096;
DROP TRIGGER trg_workspace_operational_transition_prd096;

DROP TABLE workspace_operational_event_outbox;
DROP TABLE workspace_operational_events;
DROP TABLE workspace_owner_notification_outbox;
DROP TABLE platform_action_intents;
DROP TABLE workspace_health_projections;
DROP TABLE workspace_risk_tags;
DROP TABLE workspace_operational_states;

CREATE TEMP TABLE prd096_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd096_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd096_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
