-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd110_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd110_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0102' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM risk_event_outbox)
  + (SELECT COUNT(*) FROM risk_commands)
  + (SELECT COUNT(*) FROM risk_appeals)
  + (SELECT COUNT(*) FROM risk_challenge_commands)
  + (SELECT COUNT(*) FROM risk_challenge_attempts)
  + (SELECT COUNT(*) FROM risk_challenges)
  + (SELECT COUNT(*) FROM risk_restrictions)
  + (SELECT COUNT(*) FROM risk_decisions)
  + (SELECT COUNT(*) FROM risk_signals)
  + (SELECT COUNT(*) FROM risk_case_transitions)
  + (SELECT COUNT(*) FROM risk_cases)
  + (SELECT COUNT(*) FROM abuse_counter_facts)
  + (SELECT COUNT(*) FROM abuse_counters)
  + CASE WHEN (SELECT privilege_fail_closed_count||':'||gateway_fail_open_count||':'||revision
      FROM risk_degraded_counters)='0:0:1' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_risk_permission_extensions)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_risk_role_permissions)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='risk.restriction.expire')='active:2' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd110_down_guard;

DROP TRIGGER trg_risk_degraded_counters_delete_prd110;
DROP TRIGGER trg_risk_degraded_counters_update_prd110;
DROP TABLE risk_degraded_counters;
DROP TRIGGER trg_risk_event_outbox_delete_prd110;
DROP TRIGGER trg_risk_event_outbox_update_prd110;
DROP TABLE risk_event_outbox;
DROP TRIGGER trg_risk_commands_delete_prd110;
DROP TRIGGER trg_risk_commands_update_prd110;
DROP TABLE risk_commands;
DROP TRIGGER trg_risk_appeals_delete_prd110;
DROP TRIGGER trg_risk_appeals_update_prd110;
DROP TABLE risk_appeals;
DROP TRIGGER trg_risk_challenge_commands_delete_prd110;
DROP TRIGGER trg_risk_challenge_commands_update_prd110;
DROP TABLE risk_challenge_commands;
DROP TRIGGER trg_risk_challenge_attempts_delete_prd110;
DROP TRIGGER trg_risk_challenge_attempts_update_prd110;
DROP TABLE risk_challenge_attempts;
DROP TRIGGER trg_risk_challenges_delete_prd110;
DROP TRIGGER trg_risk_challenges_update_prd110;
DROP TABLE risk_challenges;
DROP TRIGGER trg_risk_restrictions_delete_prd110;
DROP TRIGGER trg_risk_restrictions_update_prd110;
DROP TABLE risk_restrictions;
DROP TRIGGER trg_risk_decisions_delete_prd110;
DROP TRIGGER trg_risk_decisions_update_prd110;
DROP TABLE risk_decisions;
DROP TRIGGER trg_risk_signals_delete_prd110;
DROP TRIGGER trg_risk_signals_update_prd110;
DROP TABLE risk_signals;
DROP TRIGGER trg_risk_case_transitions_delete_prd110;
DROP TRIGGER trg_risk_case_transitions_update_prd110;
DROP TABLE risk_case_transitions;
DROP TRIGGER trg_risk_cases_delete_prd110;
DROP TRIGGER trg_risk_cases_update_prd110;
DROP TABLE risk_cases;
DROP TRIGGER trg_abuse_counter_facts_delete_prd110;
DROP TRIGGER trg_abuse_counter_facts_update_prd110;
DROP TABLE abuse_counter_facts;
DROP TRIGGER trg_abuse_counters_delete_prd110;
DROP TRIGGER trg_abuse_counters_update_prd110;
DROP TABLE abuse_counters;
DROP TRIGGER trg_platform_risk_roles_delete_prd110;
DROP TRIGGER trg_platform_risk_roles_update_prd110;
DROP TRIGGER trg_platform_risk_roles_insert_prd110;
DROP TRIGGER trg_platform_risk_permissions_delete_prd110;
DROP TRIGGER trg_platform_risk_permissions_update_prd110;
DROP TRIGGER trg_platform_risk_permissions_insert_prd110;
DROP TABLE platform_risk_role_permissions;
DROP TABLE platform_risk_permission_extensions;

DROP TRIGGER trg_platform_job_types_update_prd106;
UPDATE platform_job_types
SET status='disabled',revision=1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='risk.restriction.expire' AND status='active' AND revision=2;
CREATE TRIGGER trg_platform_job_types_update_prd106
BEFORE UPDATE ON platform_job_types FOR EACH ROW
WHEN NEW.type_code!=OLD.type_code OR NEW.contract_version!=OLD.contract_version
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.queue_code!=OLD.queue_code
  OR NEW.resource_class!=OLD.resource_class OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.timeout_seconds!=OLD.timeout_seconds OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TEMP TABLE prd110_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd110_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='risk.restriction.expire')='disabled:1' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd110_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
