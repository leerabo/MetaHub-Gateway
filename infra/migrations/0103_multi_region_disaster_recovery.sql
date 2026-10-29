-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd111_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd111_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0102' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','platform_job_types','platform_jobs',
      'backup_sets','backup_restore_verifications','gateway_cells',
      'workspace_gateway_placements','workspace_tenant_bindings'))=9 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd111_up_guard;

CREATE TABLE platform_dr_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.dr.read','platform.dr.execute','platform.dr.approve',
    'platform.dr.security_review')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-111'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=13)
);
CREATE TABLE platform_dr_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_dr_permission_extensions(permission_code)
);
INSERT INTO platform_dr_permission_extensions VALUES
  ('platform.dr.read','PRD-111',13),
  ('platform.dr.execute','PRD-111',13),
  ('platform.dr.approve','PRD-111',13),
  ('platform.dr.security_review','PRD-111',13);
INSERT INTO platform_dr_role_permissions VALUES
  ('platform_owner','platform.dr.read'),
  ('platform_owner','platform.dr.execute'),
  ('platform_owner','platform.dr.approve'),
  ('platform_owner','platform.dr.security_review'),
  ('platform_ops','platform.dr.read'),
  ('platform_ops','platform.dr.execute'),
  ('platform_security','platform.dr.read'),
  ('platform_security','platform.dr.approve'),
  ('platform_security','platform.dr.security_review');
CREATE TRIGGER trg_platform_dr_permissions_insert_prd111
BEFORE INSERT ON platform_dr_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_dr_permission_immutable'); END;
CREATE TRIGGER trg_platform_dr_permissions_update_prd111
BEFORE UPDATE ON platform_dr_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_dr_permission_immutable'); END;
CREATE TRIGGER trg_platform_dr_permissions_delete_prd111
BEFORE DELETE ON platform_dr_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_dr_permission_immutable'); END;
CREATE TRIGGER trg_platform_dr_roles_insert_prd111
BEFORE INSERT ON platform_dr_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_dr_permission_immutable'); END;
CREATE TRIGGER trg_platform_dr_roles_update_prd111
BEFORE UPDATE ON platform_dr_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_dr_permission_immutable'); END;
CREATE TRIGGER trg_platform_dr_roles_delete_prd111
BEFORE DELETE ON platform_dr_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_dr_permission_immutable'); END;

CREATE TABLE platform_regions(
  region_id TEXT PRIMARY KEY CHECK(length(region_id)=36 AND substr(region_id,1,4)='reg_'),
  region_code TEXT NOT NULL UNIQUE CHECK(length(region_code) BETWEEN 3 AND 40
    AND region_code=lower(region_code) AND region_code NOT GLOB '*[^a-z0-9-]*'),
  residency_group TEXT NOT NULL CHECK(length(residency_group) BETWEEN 3 AND 40
    AND residency_group=lower(residency_group)
    AND residency_group NOT GLOB '*[^a-z0-9-]*'),
  health_status TEXT NOT NULL CHECK(health_status IN('healthy','degraded','unavailable')),
  writer_status TEXT NOT NULL CHECK(writer_status IN('active','standby','fenced')),
  failover_epoch INTEGER NOT NULL CHECK(failover_epoch>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO platform_regions VALUES
  ('reg_00000000000000000000000000000001','cn-north-1','cn-main','healthy','active',0,1,datetime('now'),datetime('now')),
  ('reg_00000000000000000000000000000002','cn-east-1','cn-main','healthy','standby',0,1,datetime('now'),datetime('now'));
CREATE TRIGGER trg_platform_regions_update_prd111
BEFORE UPDATE ON platform_regions FOR EACH ROW
WHEN NEW.region_id!=OLD.region_id OR NEW.region_code!=OLD.region_code
  OR NEW.residency_group!=OLD.residency_group OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NEW.failover_epoch<OLD.failover_epoch OR NEW.failover_epoch>OLD.failover_epoch+1
  OR (NEW.writer_status!=OLD.writer_status AND NEW.failover_epoch!=OLD.failover_epoch+1)
BEGIN SELECT RAISE(ABORT,'platform_region_transition_invalid'); END;
CREATE TRIGGER trg_platform_regions_delete_prd111 BEFORE DELETE ON platform_regions
BEGIN SELECT RAISE(ABORT,'platform_region_immutable'); END;

CREATE TABLE failover_plans(
  failover_plan_id TEXT PRIMARY KEY CHECK(length(failover_plan_id)=36
    AND substr(failover_plan_id,1,4)='fpl_'),
  plan_code TEXT NOT NULL UNIQUE CHECK(length(plan_code) BETWEEN 3 AND 64
    AND plan_code=lower(plan_code) AND plan_code NOT GLOB '*[^a-z0-9_.-]*'),
  version INTEGER NOT NULL CHECK(version>=1),
  source_region_id TEXT NOT NULL,
  target_region_id TEXT NOT NULL,
  residency_group TEXT NOT NULL,
  component_profile TEXT NOT NULL CHECK(component_profile='control-full'),
  rpo_seconds INTEGER NOT NULL CHECK(rpo_seconds BETWEEN 60 AND 900),
  control_rto_seconds INTEGER NOT NULL CHECK(control_rto_seconds BETWEEN 60 AND 7200),
  gateway_rto_seconds INTEGER NOT NULL CHECK(gateway_rto_seconds BETWEEN 60 AND 600),
  precheck_profile TEXT NOT NULL CHECK(precheck_profile='integrity-ledger-outbox-config-kms'),
  stop_condition_code TEXT NOT NULL CHECK(stop_condition_code='split-brain-or-integrity-failure'),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_region_id) REFERENCES platform_regions(region_id),
  FOREIGN KEY(target_region_id) REFERENCES platform_regions(region_id),
  UNIQUE(plan_code,version),
  CHECK(source_region_id!=target_region_id)
);
INSERT INTO failover_plans VALUES
  ('fpl_00000000000000000000000000000001','control-north-to-east',1,
   'reg_00000000000000000000000000000001','reg_00000000000000000000000000000002',
   'cn-main','control-full',900,7200,600,'integrity-ledger-outbox-config-kms',
   'split-brain-or-integrity-failure','1111111111111111111111111111111111111111111111111111111111111111',
   'active',datetime('now')),
  ('fpl_00000000000000000000000000000002','control-east-to-north',1,
   'reg_00000000000000000000000000000002','reg_00000000000000000000000000000001',
   'cn-main','control-full',900,7200,600,'integrity-ledger-outbox-config-kms',
   'split-brain-or-integrity-failure','2222222222222222222222222222222222222222222222222222222222222222',
   'active',datetime('now'));
CREATE TRIGGER trg_failover_plans_insert_prd111 BEFORE INSERT ON failover_plans
BEGIN SELECT RAISE(ABORT,'failover_plan_immutable'); END;
CREATE TRIGGER trg_failover_plans_update_prd111 BEFORE UPDATE ON failover_plans
BEGIN SELECT RAISE(ABORT,'failover_plan_immutable'); END;
CREATE TRIGGER trg_failover_plans_delete_prd111 BEFORE DELETE ON failover_plans
BEGIN SELECT RAISE(ABORT,'failover_plan_immutable'); END;

CREATE TABLE disaster_recovery_runs(
  disaster_recovery_run_id TEXT PRIMARY KEY CHECK(length(disaster_recovery_run_id)=36
    AND substr(disaster_recovery_run_id,1,4)='drr_'),
  run_kind TEXT NOT NULL CHECK(run_kind IN('drill','failover')),
  failover_plan_id TEXT NOT NULL,
  plan_version INTEGER NOT NULL,
  plan_manifest_sha256 TEXT NOT NULL CHECK(length(plan_manifest_sha256)=64),
  source_region_id TEXT NOT NULL,
  target_region_id TEXT NOT NULL,
  expected_epoch INTEGER NOT NULL CHECK(expected_epoch>=0),
  fencing_epoch INTEGER NOT NULL CHECK(fencing_epoch=expected_epoch+1),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'quarterly_drill','regional_outage','control_recovery','security_containment')),
  status TEXT NOT NULL CHECK(status IN(
    'planned','approved','prechecking','running','verifying','succeeded','failed',
    'aborted','rollback_required')),
  platform_job_id TEXT,
  initiated_by_operator_id TEXT NOT NULL,
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  safe_outcome_code TEXT CHECK(safe_outcome_code IS NULL
    OR (length(safe_outcome_code) BETWEEN 3 AND 96
      AND safe_outcome_code NOT GLOB '*[^a-z0-9_.-]*')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(failover_plan_id) REFERENCES failover_plans(failover_plan_id),
  FOREIGN KEY(source_region_id) REFERENCES platform_regions(region_id),
  FOREIGN KEY(target_region_id) REFERENCES platform_regions(region_id),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(initiated_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status IN('planned','approved') AND completed_at IS NULL)
    OR(status IN('prechecking','running','verifying') AND started_at IS NOT NULL
      AND completed_at IS NULL)
    OR(status IN('succeeded','failed','aborted','rollback_required')
      AND completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_dr_active_source_prd111 ON disaster_recovery_runs(source_region_id)
  WHERE status NOT IN('succeeded','failed','aborted','rollback_required');
CREATE INDEX idx_dr_runs_recent_prd111
  ON disaster_recovery_runs(created_at DESC,disaster_recovery_run_id DESC);
CREATE TRIGGER trg_dr_runs_update_prd111
BEFORE UPDATE ON disaster_recovery_runs FOR EACH ROW
WHEN NEW.disaster_recovery_run_id!=OLD.disaster_recovery_run_id
  OR NEW.run_kind!=OLD.run_kind OR NEW.failover_plan_id!=OLD.failover_plan_id
  OR NEW.plan_version!=OLD.plan_version OR NEW.plan_manifest_sha256!=OLD.plan_manifest_sha256
  OR NEW.source_region_id!=OLD.source_region_id OR NEW.target_region_id!=OLD.target_region_id
  OR NEW.expected_epoch!=OLD.expected_epoch OR NEW.fencing_epoch!=OLD.fencing_epoch
  OR NEW.reason_code!=OLD.reason_code OR NEW.initiated_by_operator_id!=OLD.initiated_by_operator_id
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR OLD.status IN('succeeded','failed','aborted','rollback_required')
  OR NOT(NEW.status=OLD.status
    OR(OLD.status='planned' AND NEW.status IN('approved','aborted'))
    OR(OLD.status='approved' AND NEW.status IN('prechecking','failed','aborted'))
    OR(OLD.status='prechecking' AND NEW.status IN('running','failed','aborted','rollback_required'))
    OR(OLD.status='running' AND NEW.status IN('verifying','failed','rollback_required'))
    OR(OLD.status='verifying' AND NEW.status IN('succeeded','failed','rollback_required')))
BEGIN SELECT RAISE(ABORT,'dr_run_transition_invalid'); END;
CREATE TRIGGER trg_dr_runs_delete_prd111 BEFORE DELETE ON disaster_recovery_runs
BEGIN SELECT RAISE(ABORT,'dr_run_immutable'); END;

CREATE TABLE failover_approvals(
  failover_approval_id TEXT PRIMARY KEY CHECK(length(failover_approval_id)=36
    AND substr(failover_approval_id,1,4)='fap_'),
  disaster_recovery_run_id TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  approval_kind TEXT NOT NULL CHECK(approval_kind IN('execution','security_review')),
  authority_sha256 TEXT NOT NULL CHECK(length(authority_sha256)=64),
  plan_manifest_sha256 TEXT NOT NULL CHECK(length(plan_manifest_sha256)=64),
  approved_at TEXT NOT NULL CHECK(unixepoch(approved_at) IS NOT NULL),
  FOREIGN KEY(disaster_recovery_run_id)
    REFERENCES disaster_recovery_runs(disaster_recovery_run_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(disaster_recovery_run_id,operator_id),
  UNIQUE(disaster_recovery_run_id,approval_kind)
);
CREATE TRIGGER trg_failover_approvals_update_prd111 BEFORE UPDATE ON failover_approvals
BEGIN SELECT RAISE(ABORT,'failover_approval_immutable'); END;
CREATE TRIGGER trg_failover_approvals_delete_prd111 BEFORE DELETE ON failover_approvals
BEGIN SELECT RAISE(ABORT,'failover_approval_immutable'); END;

CREATE TABLE recovery_points(
  recovery_point_id TEXT PRIMARY KEY CHECK(length(recovery_point_id)=36
    AND substr(recovery_point_id,1,4)='rcp_'),
  backup_id TEXT NOT NULL UNIQUE,
  scope_kind TEXT NOT NULL CHECK(scope_kind IN('platform','workspace')),
  workspace_id TEXT,
  tenant_id TEXT,
  source_watermark_sha256 TEXT NOT NULL CHECK(length(source_watermark_sha256)=64),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  kms_key_version_sha256 TEXT NOT NULL CHECK(length(kms_key_version_sha256)=64),
  schema_head TEXT NOT NULL CHECK(length(schema_head)=4),
  integrity_status TEXT NOT NULL CHECK(integrity_status='verified'),
  source_created_at TEXT NOT NULL CHECK(unixepoch(source_created_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(backup_id) REFERENCES backup_sets(backup_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  CHECK((scope_kind='platform' AND workspace_id IS NULL AND tenant_id IS NULL)
    OR(scope_kind='workspace' AND workspace_id IS NOT NULL AND tenant_id IS NOT NULL))
);
CREATE INDEX idx_recovery_points_created_prd111
  ON recovery_points(source_created_at DESC,recovery_point_id DESC);
CREATE TRIGGER trg_recovery_points_update_prd111 BEFORE UPDATE ON recovery_points
BEGIN SELECT RAISE(ABORT,'recovery_point_immutable'); END;
CREATE TRIGGER trg_recovery_points_delete_prd111 BEFORE DELETE ON recovery_points
BEGIN SELECT RAISE(ABORT,'recovery_point_immutable'); END;

CREATE TABLE restore_evidence(
  restore_evidence_id TEXT PRIMARY KEY CHECK(length(restore_evidence_id)=36
    AND substr(restore_evidence_id,1,4)='rev_'),
  disaster_recovery_run_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  component_code TEXT NOT NULL CHECK(component_code IN(
    'database','object_store','config','outbox','ledger','gateway_route')),
  before_watermark_sha256 TEXT NOT NULL CHECK(length(before_watermark_sha256)=64),
  after_watermark_sha256 TEXT NOT NULL CHECK(length(after_watermark_sha256)=64),
  integrity_status TEXT NOT NULL CHECK(integrity_status IN('verified','failed')),
  rpo_outcome TEXT NOT NULL CHECK(rpo_outcome IN('met','missed')),
  rto_outcome TEXT NOT NULL CHECK(rto_outcome IN('met','missed')),
  duration_seconds INTEGER NOT NULL CHECK(duration_seconds>=0),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(disaster_recovery_run_id)
    REFERENCES disaster_recovery_runs(disaster_recovery_run_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(disaster_recovery_run_id,workspace_id,component_code)
);
CREATE INDEX idx_restore_evidence_owner_prd111
  ON restore_evidence(workspace_id,tenant_id,created_at DESC);
CREATE TRIGGER trg_restore_evidence_update_prd111 BEFORE UPDATE ON restore_evidence
BEGIN SELECT RAISE(ABORT,'restore_evidence_immutable'); END;
CREATE TRIGGER trg_restore_evidence_delete_prd111 BEFORE DELETE ON restore_evidence
BEGIN SELECT RAISE(ABORT,'restore_evidence_immutable'); END;

CREATE TABLE region_writer_leases(
  region_writer_lease_id TEXT PRIMARY KEY CHECK(length(region_writer_lease_id)=36
    AND substr(region_writer_lease_id,1,4)='rwl_'),
  region_id TEXT NOT NULL,
  disaster_recovery_run_id TEXT NOT NULL,
  fencing_epoch INTEGER NOT NULL CHECK(fencing_epoch>=1),
  lease_token_hmac TEXT NOT NULL CHECK(length(lease_token_hmac)=64),
  status TEXT NOT NULL CHECK(status IN('active','released','fenced')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  acquired_at TEXT NOT NULL CHECK(unixepoch(acquired_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  FOREIGN KEY(region_id) REFERENCES platform_regions(region_id),
  FOREIGN KEY(disaster_recovery_run_id)
    REFERENCES disaster_recovery_runs(disaster_recovery_run_id),
  UNIQUE(region_id,fencing_epoch),
  CHECK((status='active' AND released_at IS NULL)
    OR(status IN('released','fenced') AND released_at IS NOT NULL))
);
CREATE UNIQUE INDEX uq_region_writer_active_prd111 ON region_writer_leases(status)
  WHERE status='active';
CREATE TRIGGER trg_region_writer_leases_update_prd111
BEFORE UPDATE ON region_writer_leases FOR EACH ROW
WHEN NEW.region_writer_lease_id!=OLD.region_writer_lease_id
  OR NEW.region_id!=OLD.region_id OR NEW.disaster_recovery_run_id!=OLD.disaster_recovery_run_id
  OR NEW.fencing_epoch!=OLD.fencing_epoch OR NEW.lease_token_hmac!=OLD.lease_token_hmac
  OR NEW.expires_at!=OLD.expires_at OR NEW.acquired_at!=OLD.acquired_at
  OR OLD.status!='active' OR NEW.status NOT IN('released','fenced')
BEGIN SELECT RAISE(ABORT,'region_writer_lease_transition_invalid'); END;
CREATE TRIGGER trg_region_writer_leases_delete_prd111 BEFORE DELETE ON region_writer_leases
BEGIN SELECT RAISE(ABORT,'region_writer_lease_immutable'); END;

CREATE TABLE dr_commands(
  dr_command_id TEXT PRIMARY KEY CHECK(length(dr_command_id)=36
    AND substr(dr_command_id,1,4)='drc_'),
  operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN('dr.drill.vote','region.failover.vote')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_run_id TEXT NOT NULL,
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(result_run_id) REFERENCES disaster_recovery_runs(disaster_recovery_run_id),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE TRIGGER trg_dr_commands_update_prd111 BEFORE UPDATE ON dr_commands
BEGIN SELECT RAISE(ABORT,'dr_command_immutable'); END;
CREATE TRIGGER trg_dr_commands_delete_prd111 BEFORE DELETE ON dr_commands
BEGIN SELECT RAISE(ABORT,'dr_command_immutable'); END;

CREATE TABLE dr_event_outbox(
  dr_event_id TEXT PRIMARY KEY CHECK(length(dr_event_id)=36
    AND substr(dr_event_id,1,4)='dre_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'dr.drill_started','region.failover_started','region.failover_completed',
    'dr.objective_missed')),
  disaster_recovery_run_id TEXT NOT NULL,
  source_region_code TEXT NOT NULL,
  target_region_code TEXT NOT NULL,
  objective_code TEXT CHECK(objective_code IS NULL
    OR objective_code IN('control_rpo','control_rto','gateway_rto')),
  outcome_code TEXT NOT NULL CHECK(length(outcome_code) BETWEEN 3 AND 64
    AND outcome_code NOT GLOB '*[^a-z0-9_.-]*'),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(disaster_recovery_run_id)
    REFERENCES disaster_recovery_runs(disaster_recovery_run_id)
);
CREATE TRIGGER trg_dr_event_outbox_update_prd111 BEFORE UPDATE ON dr_event_outbox
BEGIN SELECT RAISE(ABORT,'dr_event_immutable'); END;
CREATE TRIGGER trg_dr_event_outbox_delete_prd111 BEFORE DELETE ON dr_event_outbox
BEGIN SELECT RAISE(ABORT,'dr_event_immutable'); END;

DROP TRIGGER trg_platform_job_types_update_prd106;
UPDATE platform_job_types
SET status='active',revision=2,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='dr.run' AND status='disabled' AND revision=1;
CREATE TRIGGER trg_platform_job_types_update_prd106
BEFORE UPDATE ON platform_job_types FOR EACH ROW
WHEN NEW.type_code!=OLD.type_code OR NEW.contract_version!=OLD.contract_version
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.queue_code!=OLD.queue_code
  OR NEW.resource_class!=OLD.resource_class OR NEW.max_attempts!=OLD.max_attempts
  OR NEW.timeout_seconds!=OLD.timeout_seconds OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'platform_job_type_immutable'); END;

CREATE TEMP TABLE prd111_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd111_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='dr.run')='active:2' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_regions)=2 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM failover_plans)=2 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd111_up_final_guard;
COMMIT;
