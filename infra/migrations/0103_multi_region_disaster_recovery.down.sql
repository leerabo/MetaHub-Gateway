-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd111_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd111_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0103' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM dr_event_outbox)
  + (SELECT COUNT(*) FROM dr_commands)
  + (SELECT COUNT(*) FROM region_writer_leases)
  + (SELECT COUNT(*) FROM restore_evidence)
  + (SELECT COUNT(*) FROM recovery_points)
  + (SELECT COUNT(*) FROM failover_approvals)
  + (SELECT COUNT(*) FROM disaster_recovery_runs)
  + CASE WHEN (SELECT COUNT(*) FROM platform_regions
      WHERE failover_epoch=0 AND revision=1 AND health_status='healthy')=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_regions
      WHERE (region_code='cn-north-1' AND writer_status='active')
         OR (region_code='cn-east-1' AND writer_status='standby'))=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM failover_plans WHERE status='active' AND version=1)=2
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_dr_permission_extensions)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_dr_role_permissions)=9 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='dr.run')='active:2' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd111_down_guard;

DROP TRIGGER trg_dr_event_outbox_delete_prd111;
DROP TRIGGER trg_dr_event_outbox_update_prd111;
DROP TABLE dr_event_outbox;
DROP TRIGGER trg_dr_commands_delete_prd111;
DROP TRIGGER trg_dr_commands_update_prd111;
DROP TABLE dr_commands;
DROP TRIGGER trg_region_writer_leases_delete_prd111;
DROP TRIGGER trg_region_writer_leases_update_prd111;
DROP INDEX uq_region_writer_active_prd111;
DROP TABLE region_writer_leases;
DROP TRIGGER trg_restore_evidence_delete_prd111;
DROP TRIGGER trg_restore_evidence_update_prd111;
DROP TABLE restore_evidence;
DROP TRIGGER trg_recovery_points_delete_prd111;
DROP TRIGGER trg_recovery_points_update_prd111;
DROP TABLE recovery_points;
DROP TRIGGER trg_failover_approvals_delete_prd111;
DROP TRIGGER trg_failover_approvals_update_prd111;
DROP TABLE failover_approvals;
DROP TRIGGER trg_dr_runs_delete_prd111;
DROP TRIGGER trg_dr_runs_update_prd111;
DROP INDEX idx_dr_runs_recent_prd111;
DROP INDEX uq_dr_active_source_prd111;
DROP TABLE disaster_recovery_runs;
DROP TRIGGER trg_failover_plans_delete_prd111;
DROP TRIGGER trg_failover_plans_update_prd111;
DROP TRIGGER trg_failover_plans_insert_prd111;
DROP TABLE failover_plans;
DROP TRIGGER trg_platform_regions_delete_prd111;
DROP TRIGGER trg_platform_regions_update_prd111;
DROP TABLE platform_regions;
DROP TRIGGER trg_platform_dr_roles_delete_prd111;
DROP TRIGGER trg_platform_dr_roles_update_prd111;
DROP TRIGGER trg_platform_dr_roles_insert_prd111;
DROP TRIGGER trg_platform_dr_permissions_delete_prd111;
DROP TRIGGER trg_platform_dr_permissions_update_prd111;
DROP TRIGGER trg_platform_dr_permissions_insert_prd111;
DROP TABLE platform_dr_role_permissions;
DROP TABLE platform_dr_permission_extensions;

DROP TRIGGER trg_platform_job_types_update_prd106;
UPDATE platform_job_types
SET status='disabled',revision=1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='dr.run' AND status='active' AND revision=2;
CREATE TRIGGER trg_platform_job_types_update_prd106
BEFORE UPDATE ON platform_job_types FOR EACH ROW
WHEN NEW.type_code!=OLD.type_code OR NEW.contract_version!=OLD.contract_version
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.queue_code!=OLD.queue_code
  OR NEW.resource_class!=OLD.resource_class OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.timeout_seconds!=OLD.timeout_seconds OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TEMP TABLE prd111_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd111_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='dr.run')='disabled:1' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd111_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
