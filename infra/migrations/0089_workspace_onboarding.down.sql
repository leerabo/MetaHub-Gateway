-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd097_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd097_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0089' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM onboarding_progress
      WHERE initialization_source!='migration_backfill' OR generation!=0
        OR completed_steps!=0 OR status!='incomplete' OR activated_at IS NOT NULL
        OR revision!=1)
  + (SELECT COUNT(*) FROM onboarding_step_facts
      WHERE status NOT IN('current','blocked') OR source_generation!=0 OR revision!=1
        OR first_satisfied_at IS NOT NULL OR last_satisfied_at IS NOT NULL)
  + (SELECT COUNT(*) FROM onboarding_acknowledgements)
  + (SELECT COUNT(*) FROM activation_checkpoints)
  + (SELECT COUNT(*) FROM onboarding_events)
  + (SELECT COUNT(*) FROM onboarding_event_outbox)
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd097_down_guard;

DROP TRIGGER trg_workspace_binding_onboarding_initialize_prd097;
DROP TRIGGER trg_onboarding_event_outbox_prd097;
DROP TRIGGER trg_onboarding_outbox_delete_prd097;
DROP TRIGGER trg_onboarding_events_delete_prd097;
DROP TRIGGER trg_onboarding_events_update_prd097;
DROP TRIGGER trg_activation_checkpoints_delete_prd097;
DROP TRIGGER trg_activation_checkpoints_update_prd097;
DROP TRIGGER trg_onboarding_ack_delete_prd097;
DROP TRIGGER trg_onboarding_ack_update_prd097;
DROP TRIGGER trg_onboarding_step_facts_delete_prd097;
DROP TRIGGER trg_onboarding_step_facts_transition_prd097;
DROP TRIGGER trg_onboarding_progress_delete_prd097;
DROP TRIGGER trg_onboarding_progress_transition_prd097;

DROP TABLE onboarding_event_outbox;
DROP TABLE onboarding_events;
DROP TABLE activation_checkpoints;
DROP TABLE onboarding_acknowledgements;
DROP TABLE onboarding_step_facts;
DROP TABLE onboarding_progress;
DROP INDEX uq_workspace_memberships_onboarding_owner_prd097;

CREATE TEMP TABLE prd097_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd097_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd097_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
