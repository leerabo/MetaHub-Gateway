-- metahub:expected-version-forward-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd127_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd127_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0118' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','edge_fleet_deployment_index','installer_releases',
      'installer_release_artifacts','platform_job_types','platform_jobs',
      'tenant_deletion_table_manifest','tenant_deletion_trigger_manifest'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd127_up_guard;

CREATE TABLE workspace_edge_upgrade_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'edge.upgrade.read','edge.upgrade.manage','edge.rollback.manage')),
  introduced_prd TEXT NOT NULL CHECK(introduced_prd='PRD-127'),
  introduced_migration INTEGER NOT NULL CHECK(introduced_migration=37)
);
CREATE TABLE workspace_edge_upgrade_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_edge_upgrade_permission_extensions(permission_code)
);
INSERT INTO workspace_edge_upgrade_permission_extensions VALUES
  ('edge.upgrade.read','PRD-127',37),
  ('edge.upgrade.manage','PRD-127',37),
  ('edge.rollback.manage','PRD-127',37);
INSERT INTO workspace_edge_upgrade_role_permissions VALUES
  ('owner','edge.upgrade.read'),('owner','edge.upgrade.manage'),
  ('owner','edge.rollback.manage'),('admin','edge.upgrade.read'),
  ('admin','edge.upgrade.manage'),('admin','edge.rollback.manage'),
  ('security_admin','edge.upgrade.read'),('auditor','edge.upgrade.read');
CREATE TRIGGER trg_workspace_edge_upgrade_permissions_insert_prd127
BEFORE INSERT ON workspace_edge_upgrade_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_upgrade_permissions_update_prd127
BEFORE UPDATE ON workspace_edge_upgrade_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_upgrade_permissions_delete_prd127
BEFORE DELETE ON workspace_edge_upgrade_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_upgrade_roles_insert_prd127
BEFORE INSERT ON workspace_edge_upgrade_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_upgrade_roles_update_prd127
BEFORE UPDATE ON workspace_edge_upgrade_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_upgrade_roles_delete_prd127
BEFORE DELETE ON workspace_edge_upgrade_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_upgrade_permission_immutable'); END;

CREATE TABLE platform_edge_upgrade_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.edge_release.read','platform.edge_release.manage',
    'platform.edge_release.approve','platform.edge_rollout.read',
    'platform.edge_rollout.manage')),
  introduced_prd TEXT NOT NULL CHECK(introduced_prd='PRD-127'),
  introduced_migration INTEGER NOT NULL CHECK(introduced_migration=37)
);
CREATE TABLE platform_edge_upgrade_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_admin','platform_ops','platform_finance','platform_support',
    'platform_security','platform_billing','platform_verification','platform_compliance',
    'platform_privacy','release_manager','platform_provider_engineer','platform_channel')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_edge_upgrade_permission_extensions(permission_code)
);
INSERT INTO platform_edge_upgrade_permission_extensions VALUES
  ('platform.edge_release.read','PRD-127',37),
  ('platform.edge_release.manage','PRD-127',37),
  ('platform.edge_release.approve','PRD-127',37),
  ('platform.edge_rollout.read','PRD-127',37),
  ('platform.edge_rollout.manage','PRD-127',37);
INSERT INTO platform_edge_upgrade_role_permissions VALUES
  ('platform_owner','platform.edge_release.read'),
  ('platform_owner','platform.edge_release.approve'),
  ('platform_owner','platform.edge_rollout.read'),
  ('platform_owner','platform.edge_rollout.manage'),
  ('platform_ops','platform.edge_release.read'),
  ('platform_ops','platform.edge_rollout.read'),
  ('platform_ops','platform.edge_rollout.manage'),
  ('platform_security','platform.edge_release.read'),
  ('platform_security','platform.edge_rollout.read'),
  ('release_manager','platform.edge_release.read'),
  ('release_manager','platform.edge_release.manage'),
  ('release_manager','platform.edge_release.approve'),
  ('release_manager','platform.edge_rollout.read');
CREATE TRIGGER trg_platform_edge_upgrade_permissions_insert_prd127
BEFORE INSERT ON platform_edge_upgrade_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_upgrade_permissions_update_prd127
BEFORE UPDATE ON platform_edge_upgrade_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_upgrade_permissions_delete_prd127
BEFORE DELETE ON platform_edge_upgrade_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_upgrade_roles_insert_prd127
BEFORE INSERT ON platform_edge_upgrade_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_upgrade_roles_update_prd127
BEFORE UPDATE ON platform_edge_upgrade_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_upgrade_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_upgrade_roles_delete_prd127
BEFORE DELETE ON platform_edge_upgrade_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_upgrade_permission_immutable'); END;

CREATE TABLE edge_gateway_releases(
  edge_gateway_release_id TEXT PRIMARY KEY CHECK(length(edge_gateway_release_id)=36
    AND substr(edge_gateway_release_id,1,4)='ger_'
    AND substr(edge_gateway_release_id,5) NOT GLOB '*[^0-9a-f]*'),
  installer_release_id TEXT NOT NULL,
  version TEXT NOT NULL UNIQUE CHECK(length(version) BETWEEN 5 AND 32
    AND version NOT GLOB '*[^0-9.]*'),
  release_channel TEXT NOT NULL CHECK(release_channel IN('preview','stable','extended')),
  status TEXT NOT NULL CHECK(status IN('approval_pending','released','withdrawn')),
  manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)
    AND json_type(manifest_json)='object' AND length(manifest_json)<=32768),
  manifest_sha256 TEXT NOT NULL UNIQUE CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  artifact_set_sha256 TEXT NOT NULL CHECK(length(artifact_set_sha256)=64
    AND artifact_set_sha256 NOT GLOB '*[^0-9a-f]*'),
  sbom_sha256 TEXT NOT NULL CHECK(length(sbom_sha256)=64
    AND sbom_sha256 NOT GLOB '*[^0-9a-f]*'),
  signature_algorithm TEXT NOT NULL CHECK(signature_algorithm='Ed25519'),
  signature_key_id TEXT NOT NULL CHECK(length(signature_key_id) BETWEEN 3 AND 80),
  signing_public_key_b64 TEXT NOT NULL CHECK(length(signing_public_key_b64)=43
    AND signing_public_key_b64 NOT GLOB '*[^A-Za-z0-9_-]*'),
  signature_value TEXT NOT NULL CHECK(length(signature_value)=86
    AND signature_value NOT GLOB '*[^A-Za-z0-9_-]*'),
  signature_purpose TEXT NOT NULL CHECK(
    signature_purpose='metahub.edge-upgrade.release/v1'),
  schema_from INTEGER NOT NULL CHECK(schema_from BETWEEN 1 AND 9999),
  schema_to INTEGER NOT NULL CHECK(schema_to BETWEEN 1 AND 9999),
  down_migration_supported INTEGER NOT NULL CHECK(down_migration_supported IN(0,1)),
  backup_required INTEGER NOT NULL CHECK(backup_required=1),
  compatible_from_versions_json TEXT NOT NULL CHECK(
    json_valid(compatible_from_versions_json)
    AND json_type(compatible_from_versions_json)='array'
    AND json_array_length(compatible_from_versions_json) BETWEEN 1 AND 8
    AND length(compatible_from_versions_json)<=512),
  agent_min_version TEXT NOT NULL CHECK(length(agent_min_version) BETWEEN 5 AND 32),
  agent_max_version TEXT NOT NULL CHECK(length(agent_max_version) BETWEEN 5 AND 32),
  control_min_version TEXT NOT NULL CHECK(length(control_min_version) BETWEEN 5 AND 32),
  control_max_version TEXT NOT NULL CHECK(length(control_max_version) BETWEEN 5 AND 32),
  revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
  submitted_by_operator_id TEXT NOT NULL,
  approved_by_operator_id TEXT,
  submitted_at TEXT NOT NULL CHECK(unixepoch(submitted_at) IS NOT NULL),
  approved_at TEXT CHECK(approved_at IS NULL OR unixepoch(approved_at) IS NOT NULL),
  withdrawn_at TEXT CHECK(withdrawn_at IS NULL OR unixepoch(withdrawn_at) IS NOT NULL),
  FOREIGN KEY(installer_release_id) REFERENCES installer_releases(installer_release_id),
  FOREIGN KEY(submitted_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(approved_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(schema_from<=schema_to),
  CHECK((schema_from=schema_to) OR down_migration_supported=1),
  CHECK((status='approval_pending' AND approved_by_operator_id IS NULL
      AND approved_at IS NULL AND withdrawn_at IS NULL)
    OR(status='released' AND approved_by_operator_id IS NOT NULL
      AND approved_by_operator_id!=submitted_by_operator_id
      AND approved_at IS NOT NULL AND withdrawn_at IS NULL)
    OR(status='withdrawn' AND withdrawn_at IS NOT NULL))
);
CREATE TRIGGER trg_edge_gateway_releases_insert_prd127
BEFORE INSERT ON edge_gateway_releases FOR EACH ROW
WHEN NEW.status!='approval_pending' OR NEW.revision!=1
BEGIN SELECT RAISE(ABORT,'edge_gateway_release_insert_invalid'); END;
CREATE TRIGGER trg_edge_gateway_releases_update_prd127
BEFORE UPDATE ON edge_gateway_releases FOR EACH ROW
WHEN NEW.edge_gateway_release_id!=OLD.edge_gateway_release_id
  OR NEW.installer_release_id!=OLD.installer_release_id OR NEW.version!=OLD.version
  OR NEW.release_channel!=OLD.release_channel OR NEW.manifest_json!=OLD.manifest_json
  OR NEW.manifest_sha256!=OLD.manifest_sha256
  OR NEW.artifact_set_sha256!=OLD.artifact_set_sha256 OR NEW.sbom_sha256!=OLD.sbom_sha256
  OR NEW.signature_algorithm!=OLD.signature_algorithm
  OR NEW.signature_key_id!=OLD.signature_key_id
  OR NEW.signing_public_key_b64!=OLD.signing_public_key_b64
  OR NEW.signature_value!=OLD.signature_value OR NEW.signature_purpose!=OLD.signature_purpose
  OR NEW.schema_from!=OLD.schema_from OR NEW.schema_to!=OLD.schema_to
  OR NEW.down_migration_supported!=OLD.down_migration_supported
  OR NEW.backup_required!=OLD.backup_required
  OR NEW.compatible_from_versions_json!=OLD.compatible_from_versions_json
  OR NEW.agent_min_version!=OLD.agent_min_version OR NEW.agent_max_version!=OLD.agent_max_version
  OR NEW.control_min_version!=OLD.control_min_version
  OR NEW.control_max_version!=OLD.control_max_version
  OR NEW.submitted_by_operator_id!=OLD.submitted_by_operator_id
  OR NEW.submitted_at!=OLD.submitted_at OR NEW.revision!=OLD.revision+1
  OR (OLD.status='approval_pending' AND NEW.status NOT IN('released','withdrawn'))
  OR (OLD.status='released' AND NEW.status!='withdrawn') OR OLD.status='withdrawn'
BEGIN SELECT RAISE(ABORT,'edge_gateway_release_transition_invalid'); END;
CREATE TRIGGER trg_edge_gateway_releases_delete_prd127
BEFORE DELETE ON edge_gateway_releases
BEGIN SELECT RAISE(ABORT,'edge_gateway_release_immutable'); END;

CREATE TABLE edge_gateway_release_artifacts(
  edge_gateway_artifact_id TEXT PRIMARY KEY CHECK(length(edge_gateway_artifact_id)=36
    AND substr(edge_gateway_artifact_id,1,4)='gar_'
    AND substr(edge_gateway_artifact_id,5) NOT GLOB '*[^0-9a-f]*'),
  edge_gateway_release_id TEXT NOT NULL,
  installer_artifact_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN('cli','compose','container_bundle','profile','sbom')),
  filename TEXT NOT NULL CHECK(length(filename) BETWEEN 1 AND 120
    AND filename NOT GLOB '*[/\\]*' AND filename NOT IN('.','..')),
  media_type TEXT NOT NULL CHECK(length(media_type) BETWEEN 3 AND 96),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64
    AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  sbom_sha256 TEXT NOT NULL CHECK(length(sbom_sha256)=64
    AND sbom_sha256 NOT GLOB '*[^0-9a-f]*'),
  size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 8589934592),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(edge_gateway_release_id)
    REFERENCES edge_gateway_releases(edge_gateway_release_id),
  FOREIGN KEY(installer_artifact_id)
    REFERENCES installer_release_artifacts(installer_artifact_id),
  UNIQUE(edge_gateway_release_id,installer_artifact_id),
  UNIQUE(edge_gateway_release_id,filename)
);
CREATE TRIGGER trg_edge_gateway_artifacts_insert_prd127
BEFORE INSERT ON edge_gateway_release_artifacts FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM edge_gateway_releases release
  WHERE release.edge_gateway_release_id=NEW.edge_gateway_release_id
    AND release.status='approval_pending')
BEGIN SELECT RAISE(ABORT,'edge_gateway_artifact_immutable'); END;
CREATE TRIGGER trg_edge_gateway_artifacts_update_prd127
BEFORE UPDATE ON edge_gateway_release_artifacts
BEGIN SELECT RAISE(ABORT,'edge_gateway_artifact_immutable'); END;
CREATE TRIGGER trg_edge_gateway_artifacts_delete_prd127
BEFORE DELETE ON edge_gateway_release_artifacts
BEGIN SELECT RAISE(ABORT,'edge_gateway_artifact_immutable'); END;

CREATE TABLE edge_release_channels(
  release_channel TEXT PRIMARY KEY CHECK(release_channel IN('preview','stable','extended')),
  minimum_canary_basis_points INTEGER NOT NULL CHECK(minimum_canary_basis_points=100),
  minimum_observation_seconds INTEGER NOT NULL CHECK(minimum_observation_seconds=1800),
  default_batch_size INTEGER NOT NULL CHECK(default_batch_size BETWEEN 1 AND 1000),
  status TEXT NOT NULL CHECK(status='active'),
  revision INTEGER NOT NULL CHECK(revision=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
INSERT INTO edge_release_channels VALUES
  ('preview',100,1800,10,'active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('stable',100,1800,50,'active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('extended',100,1800,25,'active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_edge_release_channels_insert_prd127 BEFORE INSERT ON edge_release_channels
BEGIN SELECT RAISE(ABORT,'edge_release_channel_immutable'); END;
CREATE TRIGGER trg_edge_release_channels_update_prd127 BEFORE UPDATE ON edge_release_channels
BEGIN SELECT RAISE(ABORT,'edge_release_channel_immutable'); END;
CREATE TRIGGER trg_edge_release_channels_delete_prd127 BEFORE DELETE ON edge_release_channels
BEGIN SELECT RAISE(ABORT,'edge_release_channel_immutable'); END;

CREATE TABLE edge_maintenance_windows(
  maintenance_window_id TEXT PRIMARY KEY CHECK(length(maintenance_window_id)=36
    AND substr(maintenance_window_id,1,4)='mwn_'
    AND substr(maintenance_window_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  release_channel TEXT NOT NULL,
  timezone_name TEXT NOT NULL CHECK(length(timezone_name) BETWEEN 3 AND 64
    AND timezone_name NOT GLOB '*[^A-Za-z0-9_+./-]*'),
  weekdays_json TEXT NOT NULL CHECK(json_valid(weekdays_json)
    AND json_type(weekdays_json)='array' AND json_array_length(weekdays_json) BETWEEN 1 AND 7
    AND length(weekdays_json)<=32),
  start_minute INTEGER NOT NULL CHECK(start_minute BETWEEN 0 AND 1439),
  duration_minutes INTEGER NOT NULL CHECK(duration_minutes BETWEEN 30 AND 720),
  freeze_starts_at TEXT CHECK(freeze_starts_at IS NULL OR unixepoch(freeze_starts_at) IS NOT NULL),
  freeze_ends_at TEXT CHECK(freeze_ends_at IS NULL OR unixepoch(freeze_ends_at) IS NOT NULL),
  status TEXT NOT NULL CHECK(status IN('active','disabled')),
  policy_generation INTEGER NOT NULL CHECK(policy_generation BETWEEN 1 AND 2147483647),
  revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  updated_by_account_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(release_channel) REFERENCES edge_release_channels(release_channel),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id),
  CHECK((freeze_starts_at IS NULL AND freeze_ends_at IS NULL)
    OR(freeze_starts_at IS NOT NULL AND freeze_ends_at IS NOT NULL
      AND unixepoch(freeze_ends_at)>unixepoch(freeze_starts_at)))
);
CREATE INDEX idx_edge_maintenance_windows_prd127
  ON edge_maintenance_windows(release_channel,status,edge_deployment_id);
CREATE TRIGGER trg_edge_maintenance_windows_update_prd127
BEFORE UPDATE ON edge_maintenance_windows FOR EACH ROW
WHEN NEW.maintenance_window_id!=OLD.maintenance_window_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.policy_generation!=OLD.policy_generation+1 OR NEW.revision!=OLD.revision+1
  OR NEW.created_at!=OLD.created_at OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_maintenance_window_transition_invalid'); END;
CREATE TRIGGER trg_edge_maintenance_windows_delete_prd127
BEFORE DELETE ON edge_maintenance_windows
BEGIN SELECT RAISE(ABORT,'edge_maintenance_window_immutable'); END;

CREATE TABLE edge_deployment_release_heads(
  edge_deployment_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  current_release_id TEXT,
  lkg_release_id TEXT,
  current_version TEXT NOT NULL CHECK(length(current_version) BETWEEN 1 AND 64),
  lkg_version TEXT NOT NULL CHECK(length(lkg_version) BETWEEN 1 AND 64),
  desired_generation INTEGER NOT NULL CHECK(desired_generation>=0),
  applied_generation INTEGER NOT NULL CHECK(applied_generation>=0),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(current_release_id) REFERENCES edge_gateway_releases(edge_gateway_release_id),
  FOREIGN KEY(lkg_release_id) REFERENCES edge_gateway_releases(edge_gateway_release_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id),
  CHECK(applied_generation<=desired_generation AND lkg_generation<=applied_generation)
);
INSERT INTO edge_deployment_release_heads(
  edge_deployment_id,workspace_id,tenant_id,current_release_id,lkg_release_id,
  current_version,lkg_version,desired_generation,applied_generation,lkg_generation,
  revision,updated_at)
SELECT edge_deployment_id,workspace_id,tenant_id,NULL,NULL,
  coalesce(agent_version,'0.0.0'),coalesce(agent_version,'0.0.0'),0,0,0,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM edge_deployments;
CREATE TRIGGER trg_edge_release_heads_deployment_insert_prd127
AFTER INSERT ON edge_deployments FOR EACH ROW
BEGIN
  INSERT INTO edge_deployment_release_heads(
    edge_deployment_id,workspace_id,tenant_id,current_release_id,lkg_release_id,
    current_version,lkg_version,desired_generation,applied_generation,lkg_generation,
    revision,updated_at)
  VALUES(
    NEW.edge_deployment_id,NEW.workspace_id,NEW.tenant_id,NULL,NULL,
    coalesce(NEW.agent_version,'0.0.0'),coalesce(NEW.agent_version,'0.0.0'),0,0,0,1,
    NEW.created_at);
END;
CREATE TRIGGER trg_edge_release_heads_update_prd127
BEFORE UPDATE ON edge_deployment_release_heads FOR EACH ROW
WHEN NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.desired_generation<OLD.desired_generation
  OR NEW.applied_generation<OLD.applied_generation OR NEW.lkg_generation<OLD.lkg_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_release_head_transition_invalid'); END;
CREATE TRIGGER trg_edge_release_heads_delete_prd127
BEFORE DELETE ON edge_deployment_release_heads
BEGIN SELECT RAISE(ABORT,'edge_release_head_immutable'); END;

CREATE TABLE edge_fleet_rollouts(
  rollout_id TEXT PRIMARY KEY CHECK(length(rollout_id)=36 AND substr(rollout_id,1,4)='rol_'
    AND substr(rollout_id,5) NOT GLOB '*[^0-9a-f]*'),
  edge_gateway_release_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN(
    'preparing','running','paused','completed','failed','cancelled')),
  generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 2147483647),
  cancel_watermark INTEGER NOT NULL CHECK(cancel_watermark BETWEEN 0 AND 2147483647),
  rollout_seed_sha256 TEXT NOT NULL CHECK(length(rollout_seed_sha256)=64
    AND rollout_seed_sha256 NOT GLOB '*[^0-9a-f]*'),
  canary_basis_points INTEGER NOT NULL CHECK(canary_basis_points BETWEEN 100 AND 10000),
  observation_seconds INTEGER NOT NULL CHECK(observation_seconds BETWEEN 1800 AND 86400),
  batch_size INTEGER NOT NULL CHECK(batch_size BETWEEN 1 AND 1000),
  failure_threshold_basis_points INTEGER NOT NULL CHECK(
    failure_threshold_basis_points BETWEEN 1 AND 10000),
  target_count INTEGER NOT NULL CHECK(target_count BETWEEN 1 AND 100000),
  succeeded_count INTEGER NOT NULL CHECK(succeeded_count BETWEEN 0 AND target_count),
  failed_count INTEGER NOT NULL CHECK(failed_count BETWEEN 0 AND target_count),
  current_batch_sequence INTEGER NOT NULL CHECK(current_batch_sequence>=0),
  platform_job_id TEXT,
  requested_by_operator_id TEXT NOT NULL,
  pause_reason_code TEXT CHECK(pause_reason_code IS NULL OR(
    length(pause_reason_code) BETWEEN 2 AND 80 AND pause_reason_code=lower(pause_reason_code)
    AND pause_reason_code NOT GLOB '*[^a-z0-9_.-]*')),
  revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  scheduled_at TEXT NOT NULL CHECK(unixepoch(scheduled_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  paused_at TEXT CHECK(paused_at IS NULL OR unixepoch(paused_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(edge_gateway_release_id)
    REFERENCES edge_gateway_releases(edge_gateway_release_id),
  FOREIGN KEY(platform_job_id) REFERENCES platform_jobs(platform_job_id),
  FOREIGN KEY(requested_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(succeeded_count+failed_count<=target_count),
  CHECK((status='paused' AND pause_reason_code IS NOT NULL AND paused_at IS NOT NULL)
    OR(status!='paused')),
  CHECK((status IN('completed','failed','cancelled') AND completed_at IS NOT NULL)
    OR(status NOT IN('completed','failed','cancelled')))
);
CREATE INDEX idx_edge_fleet_rollouts_prd127
  ON edge_fleet_rollouts(status,scheduled_at,rollout_id);
CREATE TRIGGER trg_edge_fleet_rollouts_update_prd127
BEFORE UPDATE ON edge_fleet_rollouts FOR EACH ROW
WHEN NEW.rollout_id!=OLD.rollout_id
  OR NEW.edge_gateway_release_id!=OLD.edge_gateway_release_id
  OR NEW.rollout_seed_sha256!=OLD.rollout_seed_sha256
  OR NEW.canary_basis_points!=OLD.canary_basis_points
  OR NEW.observation_seconds!=OLD.observation_seconds OR NEW.batch_size!=OLD.batch_size
  OR NEW.failure_threshold_basis_points!=OLD.failure_threshold_basis_points
  OR NEW.target_count!=OLD.target_count OR NEW.requested_by_operator_id!=OLD.requested_by_operator_id
  OR NEW.request_id!=OLD.request_id OR NEW.trace_id!=OLD.trace_id
  OR NEW.scheduled_at!=OLD.scheduled_at OR NEW.created_at!=OLD.created_at
  OR NEW.generation<OLD.generation OR NEW.cancel_watermark<OLD.cancel_watermark
  OR NEW.succeeded_count<OLD.succeeded_count OR NEW.failed_count<OLD.failed_count
  OR NEW.current_batch_sequence<OLD.current_batch_sequence
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status IN('completed','failed','cancelled')
BEGIN SELECT RAISE(ABORT,'edge_fleet_rollout_transition_invalid'); END;
CREATE TRIGGER trg_edge_fleet_rollouts_delete_prd127 BEFORE DELETE ON edge_fleet_rollouts
BEGIN SELECT RAISE(ABORT,'edge_fleet_rollout_immutable'); END;

CREATE TABLE edge_rollout_batches(
  rollout_batch_id TEXT PRIMARY KEY CHECK(length(rollout_batch_id)=36
    AND substr(rollout_batch_id,1,4)='rbt_'
    AND substr(rollout_batch_id,5) NOT GLOB '*[^0-9a-f]*'),
  rollout_id TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK(sequence>=0),
  batch_kind TEXT NOT NULL CHECK(batch_kind IN('canary','progressive')),
  status TEXT NOT NULL CHECK(status IN(
    'pending','running','observing','completed','failed','cancelled')),
  target_count INTEGER NOT NULL CHECK(target_count BETWEEN 1 AND 1000),
  succeeded_count INTEGER NOT NULL CHECK(succeeded_count BETWEEN 0 AND target_count),
  failed_count INTEGER NOT NULL CHECK(failed_count BETWEEN 0 AND target_count),
  revision INTEGER NOT NULL CHECK(revision>=1),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  observe_until TEXT CHECK(observe_until IS NULL OR unixepoch(observe_until) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(rollout_id) REFERENCES edge_fleet_rollouts(rollout_id),
  UNIQUE(rollout_id,sequence),
  CHECK(succeeded_count+failed_count<=target_count)
);
CREATE INDEX idx_edge_rollout_batches_prd127
  ON edge_rollout_batches(rollout_id,status,sequence);
CREATE TRIGGER trg_edge_rollout_batches_update_prd127
BEFORE UPDATE ON edge_rollout_batches FOR EACH ROW
WHEN NEW.rollout_batch_id!=OLD.rollout_batch_id OR NEW.rollout_id!=OLD.rollout_id
  OR NEW.sequence!=OLD.sequence OR NEW.batch_kind!=OLD.batch_kind
  OR NEW.target_count!=OLD.target_count OR NEW.created_at!=OLD.created_at
  OR NEW.succeeded_count<OLD.succeeded_count OR NEW.failed_count<OLD.failed_count
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status IN('completed','failed','cancelled')
BEGIN SELECT RAISE(ABORT,'edge_rollout_batch_transition_invalid'); END;
CREATE TRIGGER trg_edge_rollout_batches_delete_prd127 BEFORE DELETE ON edge_rollout_batches
BEGIN SELECT RAISE(ABORT,'edge_rollout_batch_immutable'); END;

CREATE TABLE edge_rollout_targets(
  rollout_target_id TEXT PRIMARY KEY CHECK(length(rollout_target_id)=36
    AND substr(rollout_target_id,1,4)='rtt_'
    AND substr(rollout_target_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  rollout_id TEXT NOT NULL,
  rollout_batch_id TEXT NOT NULL,
  edge_gateway_release_id TEXT NOT NULL,
  stable_bucket INTEGER NOT NULL CHECK(stable_bucket BETWEEN 0 AND 9999),
  maintenance_policy_generation INTEGER NOT NULL CHECK(maintenance_policy_generation>=1),
  rollout_generation INTEGER NOT NULL CHECK(rollout_generation>=1),
  desired_generation INTEGER NOT NULL CHECK(desired_generation>=1),
  current_version TEXT NOT NULL CHECK(length(current_version) BETWEEN 1 AND 64),
  target_version TEXT NOT NULL CHECK(length(target_version) BETWEEN 5 AND 32),
  status TEXT NOT NULL CHECK(status IN(
    'selected','leased','draining','staging','migrating','health_check','committing',
    'succeeded','failed','cancelled','rolled_back')),
  failure_reason_code TEXT CHECK(failure_reason_code IS NULL OR(
    length(failure_reason_code) BETWEEN 2 AND 80
    AND failure_reason_code=lower(failure_reason_code)
    AND failure_reason_code NOT GLOB '*[^a-z0-9_.-]*')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  selected_at TEXT NOT NULL CHECK(unixepoch(selected_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(rollout_id) REFERENCES edge_fleet_rollouts(rollout_id),
  FOREIGN KEY(rollout_batch_id) REFERENCES edge_rollout_batches(rollout_batch_id),
  FOREIGN KEY(edge_gateway_release_id)
    REFERENCES edge_gateway_releases(edge_gateway_release_id),
  UNIQUE(workspace_id,tenant_id,rollout_target_id),
  UNIQUE(rollout_id,edge_deployment_id)
);
CREATE INDEX idx_edge_rollout_targets_agent_prd127
  ON edge_rollout_targets(edge_deployment_id,status,desired_generation);
CREATE INDEX idx_edge_rollout_targets_batch_prd127
  ON edge_rollout_targets(rollout_batch_id,status,stable_bucket,edge_deployment_id);
CREATE TRIGGER trg_edge_rollout_targets_update_prd127
BEFORE UPDATE ON edge_rollout_targets FOR EACH ROW
WHEN NEW.rollout_target_id!=OLD.rollout_target_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.rollout_id!=OLD.rollout_id OR NEW.rollout_batch_id!=OLD.rollout_batch_id
  OR NEW.edge_gateway_release_id!=OLD.edge_gateway_release_id
  OR NEW.stable_bucket!=OLD.stable_bucket
  OR NEW.maintenance_policy_generation!=OLD.maintenance_policy_generation
  OR NEW.rollout_generation<OLD.rollout_generation
  OR NEW.desired_generation<OLD.desired_generation
  OR NEW.current_version!=OLD.current_version OR NEW.target_version!=OLD.target_version
  OR NEW.selected_at!=OLD.selected_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status IN('succeeded','failed','cancelled','rolled_back')
BEGIN SELECT RAISE(ABORT,'edge_rollout_target_transition_invalid'); END;
CREATE TRIGGER trg_edge_rollout_targets_delete_prd127 BEFORE DELETE ON edge_rollout_targets
BEGIN SELECT RAISE(ABORT,'edge_rollout_target_immutable'); END;

CREATE TABLE edge_upgrade_commands(
  upgrade_command_id TEXT PRIMARY KEY CHECK(length(upgrade_command_id)=36
    AND substr(upgrade_command_id,1,4)='ucm_'
    AND substr(upgrade_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  rollout_target_id TEXT,
  rollout_id TEXT,
  rollback_attempt_id TEXT,
  edge_gateway_release_id TEXT NOT NULL,
  command_generation INTEGER NOT NULL CHECK(command_generation>=1),
  rollout_generation INTEGER NOT NULL CHECK(rollout_generation>=1),
  cancel_watermark INTEGER NOT NULL CHECK(cancel_watermark>=0),
  maintenance_policy_generation INTEGER NOT NULL CHECK(maintenance_policy_generation>=1),
  command_kind TEXT NOT NULL CHECK(command_kind IN('upgrade','rollback')),
  status TEXT NOT NULL CHECK(status IN(
    'issued','leased','running','completed','failed','cancelled','expired')),
  expected_current_version TEXT NOT NULL CHECK(length(expected_current_version) BETWEEN 1 AND 64),
  target_version TEXT NOT NULL CHECK(length(target_version) BETWEEN 1 AND 64),
  envelope_json TEXT NOT NULL CHECK(json_valid(envelope_json)
    AND json_type(envelope_json)='object' AND length(envelope_json)<=65536),
  envelope_sha256 TEXT NOT NULL UNIQUE CHECK(length(envelope_sha256)=64
    AND envelope_sha256 NOT GLOB '*[^0-9a-f]*'),
  lease_nonce_sha256 TEXT CHECK(lease_nonce_sha256 IS NULL OR(
    length(lease_nonce_sha256)=64 AND lease_nonce_sha256 NOT GLOB '*[^0-9a-f]*')),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  deadline_at TEXT NOT NULL CHECK(unixepoch(deadline_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  issued_at TEXT NOT NULL CHECK(unixepoch(issued_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(rollout_target_id) REFERENCES edge_rollout_targets(rollout_target_id),
  FOREIGN KEY(rollout_id) REFERENCES edge_fleet_rollouts(rollout_id),
  FOREIGN KEY(edge_gateway_release_id)
    REFERENCES edge_gateway_releases(edge_gateway_release_id),
  UNIQUE(workspace_id,tenant_id,upgrade_command_id),
  UNIQUE(edge_deployment_id,command_generation),
  CHECK((command_kind='upgrade' AND rollout_target_id IS NOT NULL
      AND rollout_id IS NOT NULL AND rollback_attempt_id IS NULL)
    OR(command_kind='rollback' AND rollback_attempt_id IS NOT NULL)),
  CHECK((status IN('leased','running') AND lease_nonce_sha256 IS NOT NULL
      AND lease_expires_at IS NOT NULL) OR status NOT IN('leased','running'))
);
CREATE INDEX idx_edge_upgrade_commands_agent_prd127
  ON edge_upgrade_commands(edge_deployment_id,status,command_generation);
CREATE INDEX idx_edge_upgrade_commands_deadline_prd127
  ON edge_upgrade_commands(status,deadline_at,upgrade_command_id);
CREATE TRIGGER trg_edge_upgrade_commands_update_prd127
BEFORE UPDATE ON edge_upgrade_commands FOR EACH ROW
WHEN NEW.upgrade_command_id!=OLD.upgrade_command_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.rollout_target_id IS NOT OLD.rollout_target_id OR NEW.rollout_id IS NOT OLD.rollout_id
  OR NEW.rollback_attempt_id IS NOT OLD.rollback_attempt_id
  OR NEW.edge_gateway_release_id!=OLD.edge_gateway_release_id
  OR NEW.command_generation!=OLD.command_generation
  OR NEW.rollout_generation!=OLD.rollout_generation OR NEW.cancel_watermark!=OLD.cancel_watermark
  OR NEW.maintenance_policy_generation!=OLD.maintenance_policy_generation
  OR NEW.command_kind!=OLD.command_kind OR NEW.expected_current_version!=OLD.expected_current_version
  OR NEW.target_version!=OLD.target_version OR NEW.envelope_json!=OLD.envelope_json
  OR NEW.envelope_sha256!=OLD.envelope_sha256 OR NEW.deadline_at!=OLD.deadline_at
  OR NEW.issued_at!=OLD.issued_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status IN('completed','failed','cancelled','expired')
BEGIN SELECT RAISE(ABORT,'edge_upgrade_command_transition_invalid'); END;
CREATE TRIGGER trg_edge_upgrade_commands_delete_prd127 BEFORE DELETE ON edge_upgrade_commands
BEGIN SELECT RAISE(ABORT,'edge_upgrade_command_immutable'); END;

CREATE TABLE edge_upgrade_receipts(
  upgrade_receipt_id TEXT PRIMARY KEY CHECK(length(upgrade_receipt_id)=36
    AND substr(upgrade_receipt_id,1,4)='urc_'
    AND substr(upgrade_receipt_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  upgrade_command_id TEXT NOT NULL,
  command_generation INTEGER NOT NULL CHECK(command_generation>=1),
  outcome TEXT NOT NULL CHECK(outcome IN('succeeded','failed','cancelled')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  from_version TEXT NOT NULL CHECK(length(from_version) BETWEEN 1 AND 64),
  target_version TEXT NOT NULL CHECK(length(target_version) BETWEEN 1 AND 64),
  drain_duration_ms INTEGER NOT NULL CHECK(drain_duration_ms BETWEEN 0 AND 600000),
  total_duration_ms INTEGER NOT NULL CHECK(total_duration_ms BETWEEN 0 AND 600000),
  report_json TEXT NOT NULL CHECK(json_valid(report_json)
    AND json_type(report_json)='object' AND length(report_json)<=32768),
  report_sha256 TEXT NOT NULL CHECK(length(report_sha256)=64
    AND report_sha256 NOT GLOB '*[^0-9a-f]*'),
  attempt_nonce_sha256 TEXT NOT NULL CHECK(length(attempt_nonce_sha256)=64
    AND attempt_nonce_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  completed_at TEXT NOT NULL CHECK(unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,upgrade_command_id)
    REFERENCES edge_upgrade_commands(workspace_id,tenant_id,upgrade_command_id),
  UNIQUE(workspace_id,tenant_id,upgrade_command_id),
  UNIQUE(edge_deployment_id,command_generation)
);
CREATE TRIGGER trg_edge_upgrade_receipts_update_prd127 BEFORE UPDATE ON edge_upgrade_receipts
BEGIN SELECT RAISE(ABORT,'edge_upgrade_receipt_immutable'); END;
CREATE TRIGGER trg_edge_upgrade_receipts_delete_prd127 BEFORE DELETE ON edge_upgrade_receipts
BEGIN SELECT RAISE(ABORT,'edge_upgrade_receipt_immutable'); END;

CREATE TABLE edge_rollback_attempts(
  rollback_attempt_id TEXT PRIMARY KEY CHECK(length(rollback_attempt_id)=36
    AND substr(rollback_attempt_id,1,4)='rba_'
    AND substr(rollback_attempt_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  source_release_id TEXT NOT NULL,
  target_release_id TEXT NOT NULL,
  source_rollout_id TEXT,
  desired_generation INTEGER NOT NULL CHECK(desired_generation>=1),
  status TEXT NOT NULL CHECK(status IN('requested','issued','completed','failed')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  requested_by_account_id TEXT NOT NULL,
  upgrade_command_id TEXT,
  upgrade_receipt_id TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(source_release_id) REFERENCES edge_gateway_releases(edge_gateway_release_id),
  FOREIGN KEY(target_release_id) REFERENCES edge_gateway_releases(edge_gateway_release_id),
  FOREIGN KEY(source_rollout_id) REFERENCES edge_fleet_rollouts(rollout_id),
  FOREIGN KEY(requested_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(upgrade_command_id) REFERENCES edge_upgrade_commands(upgrade_command_id),
  FOREIGN KEY(upgrade_receipt_id) REFERENCES edge_upgrade_receipts(upgrade_receipt_id),
  UNIQUE(workspace_id,tenant_id,rollback_attempt_id),
  UNIQUE(edge_deployment_id,desired_generation)
);
CREATE INDEX idx_edge_rollback_attempts_owner_prd127
  ON edge_rollback_attempts(workspace_id,tenant_id,edge_deployment_id,requested_at DESC);
CREATE TRIGGER trg_edge_rollback_attempts_update_prd127
BEFORE UPDATE ON edge_rollback_attempts FOR EACH ROW
WHEN NEW.rollback_attempt_id!=OLD.rollback_attempt_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.source_release_id!=OLD.source_release_id OR NEW.target_release_id!=OLD.target_release_id
  OR NEW.source_rollout_id IS NOT OLD.source_rollout_id
  OR NEW.desired_generation!=OLD.desired_generation OR NEW.reason_code!=OLD.reason_code
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.requested_at!=OLD.requested_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR OLD.status IN('completed','failed')
BEGIN SELECT RAISE(ABORT,'edge_rollback_attempt_transition_invalid'); END;
CREATE TRIGGER trg_edge_rollback_attempts_delete_prd127 BEFORE DELETE ON edge_rollback_attempts
BEGIN SELECT RAISE(ABORT,'edge_rollback_attempt_immutable'); END;

CREATE TABLE edge_fleet_upgrade_events(
  fleet_upgrade_event_id TEXT PRIMARY KEY CHECK(length(fleet_upgrade_event_id)=36
    AND substr(fleet_upgrade_event_id,1,4)='fue_'
    AND substr(fleet_upgrade_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  rollout_id TEXT,
  rollout_target_id TEXT,
  rollback_attempt_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'fleet.rollout_started','fleet.rollout_paused','fleet.rollout_failed',
    'fleet.rollback_completed')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace','agent','system')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 2 AND 160),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(rollout_id) REFERENCES edge_fleet_rollouts(rollout_id),
  FOREIGN KEY(rollout_target_id) REFERENCES edge_rollout_targets(rollout_target_id),
  FOREIGN KEY(rollback_attempt_id) REFERENCES edge_rollback_attempts(rollback_attempt_id),
  UNIQUE(workspace_id,tenant_id,fleet_upgrade_event_id)
);
CREATE INDEX idx_edge_fleet_upgrade_events_prd127
  ON edge_fleet_upgrade_events(workspace_id,tenant_id,edge_deployment_id,occurred_at DESC);
CREATE TRIGGER trg_edge_fleet_upgrade_events_update_prd127 BEFORE UPDATE ON edge_fleet_upgrade_events
BEGIN SELECT RAISE(ABORT,'edge_fleet_upgrade_event_immutable'); END;
CREATE TRIGGER trg_edge_fleet_upgrade_events_delete_prd127 BEFORE DELETE ON edge_fleet_upgrade_events
BEGIN SELECT RAISE(ABORT,'edge_fleet_upgrade_event_immutable'); END;

CREATE TABLE edge_fleet_upgrade_outbox(
  fleet_upgrade_outbox_id TEXT PRIMARY KEY CHECK(length(fleet_upgrade_outbox_id)=36
    AND substr(fleet_upgrade_outbox_id,1,4)='fuo_'
    AND substr(fleet_upgrade_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  fleet_upgrade_event_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('workspace','fleet','security')),
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,fleet_upgrade_event_id)
    REFERENCES edge_fleet_upgrade_events(workspace_id,tenant_id,fleet_upgrade_event_id),
  UNIQUE(fleet_upgrade_event_id,delivery_domain)
);
CREATE INDEX idx_edge_fleet_upgrade_outbox_prd127
  ON edge_fleet_upgrade_outbox(status,available_at,fleet_upgrade_outbox_id);
CREATE TRIGGER trg_edge_fleet_upgrade_outbox_update_prd127
BEFORE UPDATE ON edge_fleet_upgrade_outbox FOR EACH ROW
WHEN NEW.fleet_upgrade_outbox_id!=OLD.fleet_upgrade_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.fleet_upgrade_event_id!=OLD.fleet_upgrade_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count!=OLD.attempt_count+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status='delivered'
BEGIN SELECT RAISE(ABORT,'edge_fleet_upgrade_outbox_transition_invalid'); END;
CREATE TRIGGER trg_edge_fleet_upgrade_outbox_delete_prd127 BEFORE DELETE ON edge_fleet_upgrade_outbox
BEGIN SELECT RAISE(ABORT,'edge_fleet_upgrade_outbox_immutable'); END;

CREATE TABLE edge_upgrade_action_commands(
  edge_upgrade_action_command_id TEXT PRIMARY KEY CHECK(length(edge_upgrade_action_command_id)=36
    AND substr(edge_upgrade_action_command_id,1,4)='fac_'
    AND substr(edge_upgrade_action_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace','agent')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 2 AND 160),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'fleet.release.submit','fleet.release.approve','fleet.release.withdraw',
    'fleet.rollout.create','fleet.rollout.pause','fleet.rollout.resume',
    'edge.maintenance.update','edge.rollback.request','edge.upgrade.lease',
    'edge.upgrade.start','edge.upgrade.report')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id) BETWEEN 4 AND 40),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(actor_domain,actor_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_edge_upgrade_actions_update_prd127 BEFORE UPDATE ON edge_upgrade_action_commands
BEGIN SELECT RAISE(ABORT,'edge_upgrade_action_immutable'); END;
CREATE TRIGGER trg_edge_upgrade_actions_delete_prd127 BEFORE DELETE ON edge_upgrade_action_commands
BEGIN SELECT RAISE(ABORT,'edge_upgrade_action_immutable'); END;

INSERT INTO platform_job_types(
  type_code,contract_version,artifact_sha256,queue_code,resource_class,max_attempts,
  timeout_seconds,status,revision,created_at,updated_at)
VALUES('fleet.rollout.advance',1,
  '58d5c335f557cb7ea49482f6b14f9ffaa7d2444534ab98c35263d1ed9aa135b3',
  'critical','control',5,300,'active',1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE tenant_deletion_manifest_extensions_prd127(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-127'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd127_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd127_manifest_sources VALUES
  ('edge_maintenance_windows','archive_then_delete','audit'),
  ('edge_deployment_release_heads','delete','operational'),
  ('edge_rollout_targets','archive_then_delete','audit'),
  ('edge_upgrade_commands','archive_then_delete','audit'),
  ('edge_upgrade_receipts','archive_then_delete','audit'),
  ('edge_rollback_attempts','archive_then_delete','audit'),
  ('edge_fleet_upgrade_events','archive_then_delete','audit'),
  ('edge_fleet_upgrade_outbox','delete','operational');
INSERT INTO tenant_deletion_table_manifest(
  source_table,disposition,retention_class,table_sql_sha256,column_signature_sha256,
  trigger_set_sha256,catalog_revision,created_at)
SELECT table_record.name,source.disposition,source.retention_class,
  metahub_sha256(table_record.sql),
  metahub_sha256((SELECT group_concat(record,char(30)) FROM(
    SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
      ||coalesce(dflt_value,'')||char(31)||pk AS record
    FROM pragma_table_info(table_record.name) ORDER BY cid))),
  metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
    SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
    FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
      AND trigger_record.tbl_name=table_record.name ORDER BY trigger_record.name)),'')),
  1,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM sqlite_schema table_record
JOIN prd127_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd127(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-127',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd127_manifest_sources ORDER BY source_table;
DROP TABLE prd127_manifest_sources;

DELETE FROM tenant_deletion_trigger_manifest;
INSERT INTO tenant_deletion_trigger_manifest(trigger_name,source_table,trigger_sql,trigger_sha256)
SELECT trigger_record.name,trigger_record.tbl_name,trigger_record.sql,
  metahub_sha256(trigger_record.sql)
FROM sqlite_schema trigger_record
JOIN tenant_deletion_table_manifest manifest ON manifest.source_table=trigger_record.tbl_name
WHERE trigger_record.type='trigger' ORDER BY trigger_record.tbl_name,trigger_record.name;
CREATE TRIGGER trg_tenant_deletion_table_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_table_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_table_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089
BEFORE UPDATE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089
BEFORE DELETE ON tenant_deletion_trigger_manifest FOR EACH ROW
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd127_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd127
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd127_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd127
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd127_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd127
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd127_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd127_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_upgrade_permission_extensions)=3
      AND (SELECT COUNT(*) FROM workspace_edge_upgrade_role_permissions)=8
      AND (SELECT COUNT(*) FROM platform_edge_upgrade_permission_extensions)=5
      AND (SELECT COUNT(*) FROM platform_edge_upgrade_role_permissions)=13
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd127)=8
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM edge_deployment_release_heads)
      =(SELECT COUNT(*) FROM edge_deployments) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT artifact_sha256 FROM platform_job_types
      WHERE type_code='fleet.rollout.advance')
      ='58d5c335f557cb7ea49482f6b14f9ffaa7d2444534ab98c35263d1ed9aa135b3'
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd127_up_final_guard;
COMMIT;
