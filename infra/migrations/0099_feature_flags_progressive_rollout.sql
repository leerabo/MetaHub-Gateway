-- Add server-authoritative feature flags and fenced progressive rollouts.
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd107_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd107_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0098' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','platform_operator_role_bindings',
      'platform_job_types','platform_jobs','workspaces','workspace_tenant_bindings',
      'workspace_operational_states','workspace_regions'))=9 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd107')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd107_up_guard;

CREATE TABLE platform_feature_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.feature.read','platform.feature.write',
    'platform.feature.approve','platform.feature.kill')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-107'),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=9)
);
CREATE TABLE platform_feature_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_feature_permission_extensions(permission_code)
);
INSERT INTO platform_feature_permission_extensions VALUES
  ('platform.feature.read','PRD-107',9),
  ('platform.feature.write','PRD-107',9),
  ('platform.feature.approve','PRD-107',9),
  ('platform.feature.kill','PRD-107',9);
INSERT INTO platform_feature_role_permissions VALUES
  ('platform_owner','platform.feature.read'),
  ('platform_owner','platform.feature.write'),
  ('platform_owner','platform.feature.approve'),
  ('platform_owner','platform.feature.kill'),
  ('release_manager','platform.feature.read'),
  ('release_manager','platform.feature.write'),
  ('release_manager','platform.feature.approve'),
  ('platform_ops','platform.feature.read'),
  ('platform_ops','platform.feature.kill');
CREATE TRIGGER trg_platform_feature_permissions_insert_prd107
BEFORE INSERT ON platform_feature_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_feature_permission_immutable'); END;
CREATE TRIGGER trg_platform_feature_permissions_update_prd107
BEFORE UPDATE ON platform_feature_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_feature_permission_immutable'); END;
CREATE TRIGGER trg_platform_feature_permissions_delete_prd107
BEFORE DELETE ON platform_feature_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_feature_permission_immutable'); END;
CREATE TRIGGER trg_platform_feature_role_permissions_insert_prd107
BEFORE INSERT ON platform_feature_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_feature_permission_immutable'); END;
CREATE TRIGGER trg_platform_feature_role_permissions_update_prd107
BEFORE UPDATE ON platform_feature_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_feature_permission_immutable'); END;
CREATE TRIGGER trg_platform_feature_role_permissions_delete_prd107
BEFORE DELETE ON platform_feature_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_feature_permission_immutable'); END;

CREATE TABLE feature_flags(
  feature_flag_id TEXT PRIMARY KEY CHECK(length(feature_flag_id)=36
    AND substr(feature_flag_id,1,4)='ffg_'
    AND substr(feature_flag_id,5) NOT GLOB '*[^0-9a-f]*'),
  flag_code TEXT NOT NULL UNIQUE CHECK(length(flag_code) BETWEEN 3 AND 80
    AND flag_code=lower(flag_code) AND flag_code NOT GLOB '*[^a-z0-9_.-]*'),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 120),
  risk_level TEXT NOT NULL CHECK(risk_level IN('standard','p0')),
  status TEXT NOT NULL CHECK(status IN('active','killed','disabled')),
  current_version_id TEXT NOT NULL,
  rollout_percentage_bps INTEGER NOT NULL CHECK(rollout_percentage_bps BETWEEN 0 AND 10000),
  revision INTEGER NOT NULL CHECK(revision>=1),
  kill_generation INTEGER NOT NULL CHECK(kill_generation>=0),
  kill_reason_code TEXT CHECK(kill_reason_code IS NULL OR kill_reason_code IN(
    'incident_containment','security_containment','release_rollback')),
  created_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((status='killed' AND kill_generation>=1 AND kill_reason_code IS NOT NULL)
    OR(status!='killed' AND kill_reason_code IS NULL))
);

CREATE TABLE feature_flag_versions(
  feature_flag_version_id TEXT PRIMARY KEY CHECK(length(feature_flag_version_id)=36
    AND substr(feature_flag_version_id,1,4)='ffv_'
    AND substr(feature_flag_version_id,5) NOT GLOB '*[^0-9a-f]*'),
  feature_flag_id TEXT NOT NULL,
  version_number INTEGER NOT NULL CHECK(version_number>=1),
  rules_json TEXT NOT NULL CHECK(json_valid(rules_json) AND json_type(rules_json)='object'
    AND length(CAST(rules_json AS BLOB))<=4096),
  rules_sha256 TEXT NOT NULL CHECK(length(rules_sha256)=64
    AND rules_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(feature_flag_id) REFERENCES feature_flags(feature_flag_id),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(feature_flag_id,version_number),
  UNIQUE(feature_flag_id,feature_flag_version_id)
);
CREATE INDEX idx_feature_flag_versions_prd107
  ON feature_flag_versions(feature_flag_id,version_number DESC);
CREATE TRIGGER trg_feature_flag_versions_update_prd107 BEFORE UPDATE ON feature_flag_versions
BEGIN SELECT RAISE(ABORT,'feature_flag_version_immutable'); END;
CREATE TRIGGER trg_feature_flag_versions_delete_prd107 BEFORE DELETE ON feature_flag_versions
BEGIN SELECT RAISE(ABORT,'feature_flag_version_immutable'); END;

CREATE TRIGGER trg_feature_flags_owner_update_prd107
BEFORE UPDATE ON feature_flags FOR EACH ROW
WHEN NEW.feature_flag_id!=OLD.feature_flag_id OR NEW.flag_code!=OLD.flag_code
  OR NEW.display_name!=OLD.display_name OR NEW.risk_level!=OLD.risk_level
  OR NEW.created_by_operator_id!=OLD.created_by_operator_id OR NEW.created_at!=OLD.created_at
  OR NEW.kill_generation<OLD.kill_generation OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'feature_flag_owner_immutable'); END;
CREATE TRIGGER trg_feature_flags_delete_prd107 BEFORE DELETE ON feature_flags
BEGIN SELECT RAISE(ABORT,'feature_flag_immutable'); END;

CREATE TABLE tenant_cohorts(
  tenant_cohort_id TEXT PRIMARY KEY CHECK(length(tenant_cohort_id)=36
    AND substr(tenant_cohort_id,1,4)='fco_'),
  cohort_code TEXT NOT NULL UNIQUE CHECK(length(cohort_code) BETWEEN 3 AND 80
    AND cohort_code=lower(cohort_code) AND cohort_code NOT GLOB '*[^a-z0-9_.-]*'),
  display_name TEXT NOT NULL CHECK(length(trim(display_name)) BETWEEN 1 AND 120),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  members_sha256 TEXT NOT NULL CHECK(length(members_sha256)=64),
  created_by_operator_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(created_by_operator_id) REFERENCES platform_operators(operator_id)
);
CREATE TABLE tenant_cohort_members(
  tenant_cohort_member_id TEXT PRIMARY KEY CHECK(length(tenant_cohort_member_id)=36
    AND substr(tenant_cohort_member_id,1,4)='fcm_'),
  tenant_cohort_id TEXT NOT NULL,
  workspace_pseudonym TEXT NOT NULL CHECK(length(workspace_pseudonym)=64),
  member_revision INTEGER NOT NULL CHECK(member_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(tenant_cohort_id) REFERENCES tenant_cohorts(tenant_cohort_id),
  UNIQUE(tenant_cohort_id,workspace_pseudonym)
);
CREATE TRIGGER trg_tenant_cohort_members_update_prd107 BEFORE UPDATE ON tenant_cohort_members
BEGIN SELECT RAISE(ABORT,'tenant_cohort_member_immutable'); END;
CREATE TRIGGER trg_tenant_cohort_members_delete_prd107 BEFORE DELETE ON tenant_cohort_members
BEGIN SELECT RAISE(ABORT,'tenant_cohort_member_immutable'); END;

CREATE TABLE rollout_health_summaries(
  rollout_health_summary_id TEXT PRIMARY KEY CHECK(length(rollout_health_summary_id)=36
    AND substr(rollout_health_summary_id,1,4)='rhs_'),
  service_code TEXT NOT NULL CHECK(length(service_code) BETWEEN 3 AND 80),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 3 AND 40),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=1),
  health_status TEXT NOT NULL CHECK(health_status IN('healthy','unhealthy','unavailable')),
  error_rate_bps INTEGER NOT NULL CHECK(error_rate_bps BETWEEN 0 AND 10000),
  latency_p95_ms INTEGER NOT NULL CHECK(latency_p95_ms>=0),
  summary_sha256 TEXT NOT NULL CHECK(length(summary_sha256)=64),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  UNIQUE(service_code,region_code,source_watermark),
  CHECK(unixepoch(expires_at)>unixepoch(observed_at))
);
CREATE INDEX idx_rollout_health_head_prd107
  ON rollout_health_summaries(service_code,region_code,source_watermark DESC);
CREATE TRIGGER trg_rollout_health_update_prd107 BEFORE UPDATE ON rollout_health_summaries
BEGIN SELECT RAISE(ABORT,'rollout_health_immutable'); END;
CREATE TRIGGER trg_rollout_health_delete_prd107 BEFORE DELETE ON rollout_health_summaries
BEGIN SELECT RAISE(ABORT,'rollout_health_immutable'); END;

CREATE TABLE feature_rollouts(
  feature_rollout_id TEXT PRIMARY KEY CHECK(length(feature_rollout_id)=36
    AND substr(feature_rollout_id,1,4)='fro_'),
  feature_flag_id TEXT NOT NULL,
  feature_flag_version_id TEXT NOT NULL,
  action_kind TEXT NOT NULL CHECK(action_kind IN('start','resume','rollback','kill')),
  status TEXT NOT NULL CHECK(status IN(
    'pending_approval','scheduled','running','paused','completed','rolled_back','killed','failed')),
  target_percentage_bps INTEGER NOT NULL CHECK(target_percentage_bps BETWEEN 0 AND 10000),
  environment_code TEXT NOT NULL CHECK(environment_code IN('development','staging','production')),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 3 AND 40),
  platform_job_id TEXT,
  requested_by_operator_id TEXT NOT NULL,
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'release_start','health_recovery','operator_pause','release_rollback','emergency_kill')),
  health_status TEXT NOT NULL CHECK(health_status IN('pending','healthy','unhealthy','unavailable')),
  health_source_watermark INTEGER CHECK(health_source_watermark IS NULL
    OR health_source_watermark>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  scheduled_at TEXT CHECK(scheduled_at IS NULL OR unixepoch(scheduled_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(feature_flag_id,feature_flag_version_id)
    REFERENCES feature_flag_versions(feature_flag_id,feature_flag_version_id),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(requested_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(platform_job_id),
  CHECK((status IN('completed','rolled_back','killed','failed') AND completed_at IS NOT NULL)
    OR(status NOT IN('completed','rolled_back','killed','failed') AND completed_at IS NULL))
);
CREATE INDEX idx_feature_rollouts_flag_prd107
  ON feature_rollouts(feature_flag_id,created_at DESC,feature_rollout_id);
CREATE INDEX idx_feature_rollouts_pending_prd107
  ON feature_rollouts(status,scheduled_at,feature_rollout_id);
CREATE TRIGGER trg_feature_rollouts_owner_update_prd107
BEFORE UPDATE ON feature_rollouts FOR EACH ROW
WHEN NEW.feature_rollout_id!=OLD.feature_rollout_id
  OR NEW.feature_flag_id!=OLD.feature_flag_id
  OR NEW.feature_flag_version_id!=OLD.feature_flag_version_id
  OR NEW.action_kind!=OLD.action_kind OR NEW.target_percentage_bps!=OLD.target_percentage_bps
  OR NEW.environment_code!=OLD.environment_code OR NEW.region_code!=OLD.region_code
  OR NEW.requested_by_operator_id!=OLD.requested_by_operator_id
  OR NEW.reason_code!=OLD.reason_code OR NEW.request_id!=OLD.request_id
  OR NEW.trace_id!=OLD.trace_id OR NEW.created_at!=OLD.created_at
  OR (OLD.health_source_watermark IS NOT NULL AND
      (NEW.health_source_watermark IS NULL
       OR NEW.health_source_watermark<OLD.health_source_watermark))
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'feature_rollout_owner_immutable'); END;
CREATE TRIGGER trg_feature_rollouts_delete_prd107 BEFORE DELETE ON feature_rollouts
BEGIN SELECT RAISE(ABORT,'feature_rollout_immutable'); END;

CREATE TABLE feature_rollout_approvals(
  feature_rollout_approval_id TEXT PRIMARY KEY CHECK(length(feature_rollout_approval_id)=36
    AND substr(feature_rollout_approval_id,1,4)='fap_'),
  feature_rollout_id TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  approval_role TEXT NOT NULL CHECK(approval_role IN('release','owner')),
  rollout_revision INTEGER NOT NULL CHECK(rollout_revision>=1),
  approved_at TEXT NOT NULL CHECK(unixepoch(approved_at) IS NOT NULL),
  FOREIGN KEY(feature_rollout_id) REFERENCES feature_rollouts(feature_rollout_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(feature_rollout_id,operator_id)
);
CREATE TRIGGER trg_feature_approvals_update_prd107 BEFORE UPDATE ON feature_rollout_approvals
BEGIN SELECT RAISE(ABORT,'feature_rollout_approval_immutable'); END;
CREATE TRIGGER trg_feature_approvals_delete_prd107 BEFORE DELETE ON feature_rollout_approvals
BEGIN SELECT RAISE(ABORT,'feature_rollout_approval_immutable'); END;

CREATE TABLE flag_evaluation_snapshots(
  flag_evaluation_snapshot_id TEXT PRIMARY KEY CHECK(length(flag_evaluation_snapshot_id)=36
    AND substr(flag_evaluation_snapshot_id,1,4)='fes_'),
  feature_flag_id TEXT NOT NULL,
  feature_flag_version_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_pseudonym TEXT NOT NULL CHECK(length(subject_pseudonym)=64),
  enabled INTEGER NOT NULL CHECK(enabled IN(0,1)),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'enabled','percentage_excluded','environment_excluded','region_excluded',
    'cohort_excluded','prerequisite_disabled','entitlement_denied','workspace_disabled',
    'flag_killed','flag_disabled','not_released')),
  evaluation_sha256 TEXT NOT NULL CHECK(length(evaluation_sha256)=64),
  evaluated_at TEXT NOT NULL CHECK(unixepoch(evaluated_at) IS NOT NULL),
  FOREIGN KEY(feature_flag_id,feature_flag_version_id)
    REFERENCES feature_flag_versions(feature_flag_id,feature_flag_version_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_flag_evaluation_workspace_prd107
  ON flag_evaluation_snapshots(workspace_id,evaluated_at DESC);
CREATE TRIGGER trg_flag_evaluations_update_prd107 BEFORE UPDATE ON flag_evaluation_snapshots
BEGIN SELECT RAISE(ABORT,'flag_evaluation_immutable'); END;
CREATE TRIGGER trg_flag_evaluations_delete_prd107 BEFORE DELETE ON flag_evaluation_snapshots
BEGIN SELECT RAISE(ABORT,'flag_evaluation_immutable'); END;

CREATE TABLE feature_exposures(
  feature_exposure_id TEXT PRIMARY KEY CHECK(length(feature_exposure_id)=36
    AND substr(feature_exposure_id,1,4)='fex_'),
  feature_flag_id TEXT NOT NULL,
  feature_flag_version_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_pseudonym TEXT NOT NULL CHECK(length(subject_pseudonym)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  exposed_at TEXT NOT NULL CHECK(unixepoch(exposed_at) IS NOT NULL),
  FOREIGN KEY(feature_flag_id,feature_flag_version_id)
    REFERENCES feature_flag_versions(feature_flag_id,feature_flag_version_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(feature_flag_id,feature_flag_version_id,workspace_id,subject_pseudonym,request_sha256)
);
CREATE TRIGGER trg_feature_exposures_update_prd107 BEFORE UPDATE ON feature_exposures
BEGIN SELECT RAISE(ABORT,'feature_exposure_immutable'); END;
CREATE TRIGGER trg_feature_exposures_delete_prd107 BEFORE DELETE ON feature_exposures
BEGIN SELECT RAISE(ABORT,'feature_exposure_immutable'); END;

CREATE TABLE feature_flag_commands(
  feature_flag_command_id TEXT PRIMARY KEY CHECK(length(feature_flag_command_id)=36
    AND substr(feature_flag_command_id,1,4)='ffc_'),
  operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'flag.create','flag.version','rollout.start','rollout.approve','rollout.pause',
    'rollout.resume','rollout.rollback','flag.kill')),
  feature_flag_id TEXT NOT NULL,
  feature_rollout_id TEXT,
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(feature_flag_id) REFERENCES feature_flags(feature_flag_id),
  FOREIGN KEY(feature_rollout_id) REFERENCES feature_rollouts(feature_rollout_id),
  UNIQUE(operator_id,idempotency_sha256)
);
CREATE TRIGGER trg_feature_commands_update_prd107 BEFORE UPDATE ON feature_flag_commands
BEGIN SELECT RAISE(ABORT,'feature_command_immutable'); END;
CREATE TRIGGER trg_feature_commands_delete_prd107 BEFORE DELETE ON feature_flag_commands
BEGIN SELECT RAISE(ABORT,'feature_command_immutable'); END;

CREATE TABLE feature_event_outbox(
  feature_event_outbox_id TEXT PRIMARY KEY CHECK(length(feature_event_outbox_id)=36
    AND substr(feature_event_outbox_id,1,4)='feo_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'feature.rollout_started','feature.rollout_paused','feature.killed','feature.exposed')),
  feature_flag_id TEXT NOT NULL,
  feature_rollout_id TEXT,
  workspace_id TEXT,
  safe_reason_code TEXT NOT NULL CHECK(length(safe_reason_code) BETWEEN 3 AND 80),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(feature_flag_id) REFERENCES feature_flags(feature_flag_id),
  FOREIGN KEY(feature_rollout_id) REFERENCES feature_rollouts(feature_rollout_id),
  FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
);
CREATE TRIGGER trg_feature_event_update_prd107 BEFORE UPDATE ON feature_event_outbox
BEGIN SELECT RAISE(ABORT,'feature_event_immutable'); END;
CREATE TRIGGER trg_feature_event_delete_prd107 BEFORE DELETE ON feature_event_outbox
BEGIN SELECT RAISE(ABORT,'feature_event_immutable'); END;

UPDATE platform_job_types
SET status='active',revision=revision+1,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE type_code='feature.rollout.step' AND status='disabled' AND revision=1;

CREATE TEMP TABLE prd107_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd107_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_feature_permission_extensions)=4 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_feature_role_permissions)=9 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT status||':'||revision FROM platform_job_types
      WHERE type_code='feature.rollout.step')='active:2' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'feature_flags','feature_flag_versions','tenant_cohorts','tenant_cohort_members',
      'rollout_health_summaries','feature_rollouts','feature_rollout_approvals',
      'flag_evaluation_snapshots','feature_exposures','feature_flag_commands',
      'feature_event_outbox'))=11 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd107_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
