-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd107_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd107_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0099' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM feature_event_outbox)
  + (SELECT COUNT(*) FROM feature_flag_commands)
  + (SELECT COUNT(*) FROM feature_exposures)
  + (SELECT COUNT(*) FROM flag_evaluation_snapshots)
  + (SELECT COUNT(*) FROM feature_rollout_approvals)
  + (SELECT COUNT(*) FROM feature_rollouts)
  + (SELECT COUNT(*) FROM rollout_health_summaries)
  + (SELECT COUNT(*) FROM tenant_cohort_members)
  + (SELECT COUNT(*) FROM tenant_cohorts)
  + (SELECT COUNT(*) FROM feature_flag_versions)
  + (SELECT COUNT(*) FROM feature_flags)
  + (SELECT COUNT(*) FROM platform_jobs WHERE type_code='feature.rollout.step')
  + CASE WHEN (SELECT COUNT(*) FROM platform_feature_permission_extensions)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_feature_role_permissions)=9 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd107_down_guard;

DROP TRIGGER trg_feature_event_delete_prd107;
DROP TRIGGER trg_feature_event_update_prd107;
DROP TABLE feature_event_outbox;
DROP TRIGGER trg_feature_commands_delete_prd107;
DROP TRIGGER trg_feature_commands_update_prd107;
DROP TABLE feature_flag_commands;
DROP TRIGGER trg_feature_exposures_delete_prd107;
DROP TRIGGER trg_feature_exposures_update_prd107;
DROP TABLE feature_exposures;
DROP TRIGGER trg_flag_evaluations_delete_prd107;
DROP TRIGGER trg_flag_evaluations_update_prd107;
DROP TABLE flag_evaluation_snapshots;
DROP TRIGGER trg_feature_approvals_delete_prd107;
DROP TRIGGER trg_feature_approvals_update_prd107;
DROP TABLE feature_rollout_approvals;
DROP TRIGGER trg_feature_rollouts_delete_prd107;
DROP TRIGGER trg_feature_rollouts_owner_update_prd107;
DROP TABLE feature_rollouts;
DROP TRIGGER trg_rollout_health_delete_prd107;
DROP TRIGGER trg_rollout_health_update_prd107;
DROP TABLE rollout_health_summaries;
DROP TRIGGER trg_tenant_cohort_members_delete_prd107;
DROP TRIGGER trg_tenant_cohort_members_update_prd107;
DROP TABLE tenant_cohort_members;
DROP TABLE tenant_cohorts;
DROP TRIGGER trg_feature_flag_versions_delete_prd107;
DROP TRIGGER trg_feature_flag_versions_update_prd107;
DROP TRIGGER trg_feature_flags_delete_prd107;
DROP TRIGGER trg_feature_flags_owner_update_prd107;
DROP TABLE feature_flag_versions;
DROP TABLE feature_flags;

DROP TRIGGER trg_platform_feature_role_permissions_delete_prd107;
DROP TRIGGER trg_platform_feature_role_permissions_update_prd107;
DROP TRIGGER trg_platform_feature_role_permissions_insert_prd107;
DROP TRIGGER trg_platform_feature_permissions_delete_prd107;
DROP TRIGGER trg_platform_feature_permissions_update_prd107;
DROP TRIGGER trg_platform_feature_permissions_insert_prd107;
DROP TABLE platform_feature_role_permissions;
DROP TABLE platform_feature_permission_extensions;

DROP TRIGGER trg_platform_job_types_update_prd106;
UPDATE platform_job_types
SET status='disabled',revision=1,updated_at=created_at
WHERE type_code='feature.rollout.step' AND status='active' AND revision=2;
CREATE TRIGGER trg_platform_job_types_update_prd106
BEFORE UPDATE ON platform_job_types FOR EACH ROW
WHEN NEW.type_code!=OLD.type_code OR NEW.contract_version!=OLD.contract_version
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.queue_code!=OLD.queue_code
  OR NEW.resource_class!=OLD.resource_class OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.timeout_seconds!=OLD.timeout_seconds OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TEMP TABLE prd107_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd107_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT status||':'||revision FROM platform_job_types
    WHERE type_code='feature.rollout.step')='disabled:1' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd107_down_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
