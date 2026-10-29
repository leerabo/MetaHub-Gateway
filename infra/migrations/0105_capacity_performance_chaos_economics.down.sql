-- metahub:expected-version-rollback-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd113_down_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd113_down_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0105' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM capacity_event_outbox)
  + (SELECT COUNT(*) FROM capacity_commands)
  + (SELECT COUNT(*) FROM performance_rollout_blocks)
  + (SELECT COUNT(*) FROM unit_economics_snapshots)
  + (SELECT COUNT(*) FROM platform_cost_allocations)
  + (SELECT COUNT(*) FROM chaos_approvals)
  + (SELECT COUNT(*) FROM chaos_experiments)
  + (SELECT COUNT(*) FROM load_test_runs)
  + (SELECT COUNT(*) FROM capacity_forecasts)
  + (SELECT COUNT(*) FROM performance_baselines)
  + (SELECT COUNT(*) FROM platform_jobs WHERE type_code='capacity.run'
      AND status NOT IN('succeeded','failed','cancelled','dead_lettered'))
  + CASE WHEN (SELECT COUNT(*) FROM capacity_service_states
      WHERE revision=1)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM capacity_scenarios)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM chaos_fault_catalog)=7 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_capacity_permission_extensions)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_capacity_role_permissions)=10 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='capacity.run')='active:2' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd113_down_guard;

DROP TRIGGER trg_capacity_events_delete_prd113;
DROP TRIGGER trg_capacity_events_update_prd113;
DROP TABLE capacity_event_outbox;
DROP TRIGGER trg_capacity_commands_delete_prd113;
DROP TRIGGER trg_capacity_commands_update_prd113;
DROP TABLE capacity_commands;
DROP TRIGGER trg_performance_rollout_blocks_delete_prd113;
DROP TRIGGER trg_performance_rollout_blocks_update_prd113;
DROP INDEX uq_performance_rollout_block_prd113;
DROP TABLE performance_rollout_blocks;
DROP TRIGGER trg_unit_economics_delete_prd113;
DROP TRIGGER trg_unit_economics_update_prd113;
DROP TABLE unit_economics_snapshots;
DROP TRIGGER trg_platform_cost_allocations_delete_prd113;
DROP TRIGGER trg_platform_cost_allocations_update_prd113;
DROP TABLE platform_cost_allocations;
DROP TRIGGER trg_chaos_approvals_delete_prd113;
DROP TRIGGER trg_chaos_approvals_update_prd113;
DROP TABLE chaos_approvals;
DROP TRIGGER trg_chaos_experiments_delete_prd113;
DROP TRIGGER trg_chaos_experiments_update_prd113;
DROP INDEX uq_chaos_active_scope_prd113;
DROP INDEX idx_chaos_experiments_recent_prd113;
DROP TABLE chaos_experiments;
DROP TRIGGER trg_chaos_fault_catalog_delete_prd113;
DROP TRIGGER trg_chaos_fault_catalog_update_prd113;
DROP TABLE chaos_fault_catalog;
DROP TRIGGER trg_load_test_runs_delete_prd113;
DROP TRIGGER trg_load_test_runs_update_prd113;
DROP INDEX idx_load_test_runs_recent_prd113;
DROP TABLE load_test_runs;
DROP TRIGGER trg_capacity_forecasts_delete_prd113;
DROP TRIGGER trg_capacity_forecasts_update_prd113;
DROP TABLE capacity_forecasts;
DROP TRIGGER trg_performance_baselines_delete_prd113;
DROP TRIGGER trg_performance_baselines_update_prd113;
DROP TABLE performance_baselines;
DROP TRIGGER trg_capacity_scenarios_delete_prd113;
DROP TRIGGER trg_capacity_scenarios_update_prd113;
DROP TABLE capacity_scenarios;
DROP TRIGGER trg_capacity_service_states_delete_prd113;
DROP TRIGGER trg_capacity_service_states_update_prd113;
DROP TABLE capacity_service_states;
DROP TRIGGER trg_platform_capacity_roles_delete_prd113;
DROP TRIGGER trg_platform_capacity_roles_update_prd113;
DROP TRIGGER trg_platform_capacity_roles_insert_prd113;
DROP TRIGGER trg_platform_capacity_permissions_delete_prd113;
DROP TRIGGER trg_platform_capacity_permissions_update_prd113;
DROP TRIGGER trg_platform_capacity_permissions_insert_prd113;
DROP TABLE platform_capacity_role_permissions;
DROP TABLE platform_capacity_permission_extensions;

DROP TRIGGER trg_platform_job_types_update_prd106;
UPDATE platform_job_types
SET status='disabled',revision=1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='capacity.run' AND status='active' AND revision=2;
CREATE TRIGGER trg_platform_job_types_update_prd106
BEFORE UPDATE ON platform_job_types FOR EACH ROW
WHEN NEW.type_code!=OLD.type_code OR NEW.contract_version!=OLD.contract_version
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.queue_code!=OLD.queue_code
  OR NEW.resource_class!=OLD.resource_class OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.timeout_seconds!=OLD.timeout_seconds OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TEMP TABLE prd113_down_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd113_down_final_guard(issue_count)
SELECT CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='capacity.run')='disabled:1' THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd113_down_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
