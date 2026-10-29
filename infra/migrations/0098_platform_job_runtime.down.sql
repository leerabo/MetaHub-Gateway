-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd106_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd106_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0098' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM platform_jobs)
  + (SELECT COUNT(*) FROM platform_job_attempts)
  + (SELECT COUNT(*) FROM platform_scheduler_leases)
  + (SELECT COUNT(*) FROM platform_job_checkpoints)
  + (SELECT COUNT(*) FROM platform_job_effects)
  + (SELECT COUNT(*) FROM platform_dead_letter_jobs)
  + (SELECT COUNT(*) FROM platform_job_event_outbox)
  + (SELECT COUNT(*) FROM platform_job_commands)
  + CASE WHEN (SELECT COUNT(*) FROM platform_job_types)=8 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_job_types WHERE revision!=1
      OR status!=(CASE WHEN type_code='platform.runtime.probe' THEN 'active' ELSE 'disabled' END))=0
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_work_queues)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_job_permission_extensions)=3 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_job_role_permissions)=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd106_down_guard;

DROP TRIGGER trg_platform_job_commands_delete_prd106;
DROP TRIGGER trg_platform_job_commands_update_prd106;
DROP TABLE platform_job_commands;
DROP TRIGGER trg_platform_job_event_delete_prd106;
DROP TRIGGER trg_platform_job_event_owner_prd106;
DROP TABLE platform_job_event_outbox;
DROP TRIGGER trg_platform_dead_letter_delete_prd106;
DROP TRIGGER trg_platform_dead_letter_update_prd106;
DROP TABLE platform_dead_letter_jobs;
DROP TRIGGER trg_platform_job_effects_delete_prd106;
DROP TRIGGER trg_platform_job_effects_update_prd106;
DROP TABLE platform_job_effects;
DROP TRIGGER trg_platform_job_checkpoints_delete_prd106;
DROP TRIGGER trg_platform_job_checkpoints_update_prd106;
DROP TABLE platform_job_checkpoints;
DROP TRIGGER trg_platform_scheduler_leases_delete_prd106;
DROP TRIGGER trg_platform_scheduler_leases_update_prd106;
DROP INDEX uq_platform_scheduler_active_job_prd106;
DROP TABLE platform_scheduler_leases;
DROP TRIGGER trg_platform_job_attempts_delete_prd106;
DROP TRIGGER trg_platform_job_attempts_update_prd106;
DROP TABLE platform_job_attempts;
DROP TRIGGER trg_platform_jobs_delete_prd106;
DROP TRIGGER trg_platform_jobs_owner_update_prd106;
DROP TABLE platform_jobs;
DROP TRIGGER trg_platform_work_queues_delete_prd106;
DROP TRIGGER trg_platform_work_queues_update_prd106;
DROP TABLE platform_work_queues;
DROP TRIGGER trg_platform_job_types_delete_prd106;
DROP TRIGGER trg_platform_job_types_update_prd106;
DROP TABLE platform_job_types;
DROP TRIGGER trg_platform_job_role_permissions_delete_prd106;
DROP TRIGGER trg_platform_job_role_permissions_update_prd106;
DROP TRIGGER trg_platform_job_role_permissions_insert_prd106;
DROP TRIGGER trg_platform_job_permissions_delete_prd106;
DROP TRIGGER trg_platform_job_permissions_update_prd106;
DROP TRIGGER trg_platform_job_permissions_insert_prd106;
DROP TABLE platform_job_role_permissions;
DROP TABLE platform_job_permission_extensions;

CREATE TEMP TABLE prd106_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd106_down_final_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd106_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
