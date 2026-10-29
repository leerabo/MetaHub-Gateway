-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd126_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd126_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0117' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','edge_desired_state_heads','edge_fleet_telemetry_projections',
      'edge_validation_runs','platform_operators','workspace_memberships',
      'tenant_deletion_table_manifest','tenant_deletion_trigger_manifest'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd126')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd126_up_guard;

CREATE TABLE workspace_edge_diagnostic_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'edge.health.read','edge.diagnostic.read','edge.diagnostic.approve')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-126'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=36)
);
CREATE TABLE workspace_edge_diagnostic_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_edge_diagnostic_permission_extensions(permission_code)
);
INSERT INTO workspace_edge_diagnostic_permission_extensions VALUES
  ('edge.health.read','PRD-126',36),
  ('edge.diagnostic.read','PRD-126',36),
  ('edge.diagnostic.approve','PRD-126',36);
INSERT INTO workspace_edge_diagnostic_role_permissions VALUES
  ('owner','edge.health.read'),('owner','edge.diagnostic.read'),
  ('owner','edge.diagnostic.approve'),
  ('admin','edge.health.read'),('admin','edge.diagnostic.read'),
  ('admin','edge.diagnostic.approve'),
  ('developer','edge.health.read'),
  ('security_admin','edge.health.read'),('security_admin','edge.diagnostic.read'),
  ('security_admin','edge.diagnostic.approve'),
  ('auditor','edge.health.read'),('auditor','edge.diagnostic.read');
CREATE TRIGGER trg_workspace_edge_diagnostic_permissions_insert_prd126
BEFORE INSERT ON workspace_edge_diagnostic_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_diagnostic_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_diagnostic_permissions_update_prd126
BEFORE UPDATE ON workspace_edge_diagnostic_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_diagnostic_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_diagnostic_permissions_delete_prd126
BEFORE DELETE ON workspace_edge_diagnostic_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_diagnostic_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_diagnostic_roles_insert_prd126
BEFORE INSERT ON workspace_edge_diagnostic_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_diagnostic_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_diagnostic_roles_update_prd126
BEFORE UPDATE ON workspace_edge_diagnostic_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_diagnostic_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_diagnostic_roles_delete_prd126
BEFORE DELETE ON workspace_edge_diagnostic_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_diagnostic_permission_immutable'); END;

CREATE TABLE edge_health_snapshots(
  edge_health_snapshot_id TEXT PRIMARY KEY CHECK(length(edge_health_snapshot_id)=36
    AND substr(edge_health_snapshot_id,1,4)='ehs_'
    AND substr(edge_health_snapshot_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 2147483647),
  source_code TEXT NOT NULL CHECK(source_code IN('migration_backfill','runtime')),
  source_revision INTEGER NOT NULL CHECK(source_revision>=0),
  score_model_version TEXT NOT NULL CHECK(score_model_version='edge-health-v1'),
  health_score INTEGER NOT NULL CHECK(health_score BETWEEN 0 AND 100),
  health_code TEXT NOT NULL CHECK(health_code IN(
    'healthy','degraded','critical','offline','revoked','unknown')),
  certificate_signal TEXT NOT NULL CHECK(certificate_signal IN(
    'healthy','expiring','expired','missing','revoked')),
  version_signal TEXT NOT NULL CHECK(version_signal IN('current','outdated','unknown')),
  capacity_signal TEXT NOT NULL CHECK(capacity_signal IN(
    'healthy','warning','critical','unknown')),
  config_signal TEXT NOT NULL CHECK(config_signal IN(
    'converged','drifted','not_published','unknown')),
  telemetry_signal TEXT NOT NULL CHECK(telemetry_signal IN(
    'current','lagging','stale','missing')),
  validation_signal TEXT NOT NULL CHECK(validation_signal IN(
    'passed','failed','pending','missing')),
  desired_generation INTEGER NOT NULL CHECK(desired_generation>=0),
  applied_generation INTEGER NOT NULL CHECK(applied_generation>=0),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation>=0),
  telemetry_lag_seconds INTEGER CHECK(telemetry_lag_seconds IS NULL
    OR telemetry_lag_seconds>=0),
  certificate_expires_at TEXT CHECK(certificate_expires_at IS NULL
    OR unixepoch(certificate_expires_at) IS NOT NULL),
  agent_version TEXT CHECK(agent_version IS NULL OR(length(agent_version) BETWEEN 1 AND 64
    AND agent_version NOT GLOB '*[^A-Za-z0-9._+-]*')),
  snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64
    AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*'),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,generation),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_health_snapshot_id),
  UNIQUE(edge_deployment_id,snapshot_sha256),
  CHECK(applied_generation<=desired_generation AND lkg_generation<=applied_generation)
);
CREATE INDEX idx_edge_health_snapshots_owner_prd126
  ON edge_health_snapshots(workspace_id,tenant_id,edge_deployment_id,generation DESC);
CREATE TRIGGER trg_edge_health_snapshots_update_prd126
BEFORE UPDATE ON edge_health_snapshots
BEGIN SELECT RAISE(ABORT,'edge_health_snapshot_immutable'); END;
CREATE TRIGGER trg_edge_health_snapshots_delete_prd126
BEFORE DELETE ON edge_health_snapshots
BEGIN SELECT RAISE(ABORT,'edge_health_snapshot_immutable'); END;

CREATE TABLE edge_health_heads(
  edge_deployment_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 2147483647),
  edge_health_snapshot_id TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL CHECK(length(snapshot_sha256)=64
    AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,generation)
    REFERENCES edge_health_snapshots(workspace_id,tenant_id,edge_deployment_id,generation),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_health_snapshot_id)
    REFERENCES edge_health_snapshots(
      workspace_id,tenant_id,edge_deployment_id,edge_health_snapshot_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id)
);
CREATE TRIGGER trg_edge_health_heads_update_prd126
BEFORE UPDATE ON edge_health_heads FOR EACH ROW
WHEN NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.generation<=OLD.generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_health_head_transition_invalid'); END;
CREATE TRIGGER trg_edge_health_heads_delete_prd126
BEFORE DELETE ON edge_health_heads
BEGIN SELECT RAISE(ABORT,'edge_health_head_immutable'); END;

CREATE TABLE edge_fleet_deployment_index(
  edge_deployment_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 2 AND 40),
  mode TEXT NOT NULL CHECK(mode IN('connected','isolated')),
  status TEXT NOT NULL CHECK(status IN('pending','active','revoked','retired')),
  health_code TEXT NOT NULL CHECK(health_code IN(
    'healthy','degraded','critical','offline','revoked','unknown')),
  health_score INTEGER NOT NULL CHECK(health_score BETWEEN 0 AND 100),
  alert_code TEXT NOT NULL CHECK(alert_code IN(
    'none','certificate','version','capacity','configuration','telemetry','validation',
    'offline','revoked')),
  maintenance_mode INTEGER NOT NULL CHECK(maintenance_mode IN(0,1)),
  agent_version TEXT CHECK(agent_version IS NULL OR length(agent_version) BETWEEN 1 AND 64),
  desired_generation INTEGER NOT NULL CHECK(desired_generation>=0),
  applied_generation INTEGER NOT NULL CHECK(applied_generation>=0),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation>=0),
  telemetry_lag_seconds INTEGER CHECK(telemetry_lag_seconds IS NULL
    OR telemetry_lag_seconds>=0),
  certificate_signal TEXT NOT NULL CHECK(certificate_signal IN(
    'healthy','expiring','expired','missing','revoked')),
  source_health_generation INTEGER NOT NULL CHECK(source_health_generation>=1),
  index_revision INTEGER NOT NULL CHECK(index_revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,source_health_generation)
    REFERENCES edge_health_snapshots(workspace_id,tenant_id,edge_deployment_id,generation),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id),
  CHECK(applied_generation<=desired_generation AND lkg_generation<=applied_generation)
);
CREATE INDEX idx_edge_fleet_keyset_prd126
  ON edge_fleet_deployment_index(
    status,region_code,health_code,edge_deployment_id);
CREATE INDEX idx_edge_fleet_health_prd126
  ON edge_fleet_deployment_index(health_score,updated_at,edge_deployment_id);
CREATE TRIGGER trg_edge_fleet_index_update_prd126
BEFORE UPDATE ON edge_fleet_deployment_index FOR EACH ROW
WHEN NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.source_health_generation<OLD.source_health_generation
  OR NEW.index_revision!=OLD.index_revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_fleet_index_transition_invalid'); END;
CREATE TRIGGER trg_edge_fleet_index_delete_prd126
BEFORE DELETE ON edge_fleet_deployment_index
BEGIN SELECT RAISE(ABORT,'edge_fleet_index_immutable'); END;

WITH facts AS (
  SELECT deployment.*,
    coalesce(head.generation,0) desired_generation,
    coalesce(head.applied_generation,0) applied_generation,
    coalesce(head.lkg_generation,0) lkg_generation,
    telemetry.revision telemetry_revision,
    telemetry.lag_seconds,
    validation.outcome validation_outcome,
    CASE
      WHEN deployment.status='revoked' THEN 0
      WHEN deployment.status!='active' THEN 20
      ELSE max(0,100
        - CASE WHEN head.generation IS NOT NULL
            AND head.generation!=head.applied_generation THEN 20 ELSE 0 END
        - CASE WHEN telemetry.lag_seconds>120 THEN 20 ELSE 0 END
        - CASE WHEN telemetry.error_rate_basis_points>=1000 THEN 20 ELSE 0 END
        - CASE WHEN deployment.health_code='certificate_expiring' THEN 20 ELSE 0 END
        - CASE WHEN validation.outcome='failed' THEN 20 ELSE 0 END)
    END health_score
  FROM edge_deployments deployment
  LEFT JOIN edge_desired_state_heads head
    ON head.edge_deployment_id=deployment.edge_deployment_id
  LEFT JOIN edge_fleet_telemetry_projections telemetry
    ON telemetry.edge_deployment_id=deployment.edge_deployment_id
  LEFT JOIN (
    SELECT run.edge_deployment_id,run.outcome
    FROM edge_validation_runs run
    JOIN (SELECT edge_deployment_id,max(created_at) created_at
      FROM edge_validation_runs GROUP BY edge_deployment_id) latest
      ON latest.edge_deployment_id=run.edge_deployment_id
     AND latest.created_at=run.created_at
  ) validation ON validation.edge_deployment_id=deployment.edge_deployment_id
)
INSERT INTO edge_health_snapshots(
  edge_health_snapshot_id,workspace_id,tenant_id,edge_deployment_id,generation,
  source_code,source_revision,score_model_version,health_score,health_code,
  certificate_signal,version_signal,capacity_signal,config_signal,telemetry_signal,
  validation_signal,desired_generation,applied_generation,lkg_generation,
  telemetry_lag_seconds,certificate_expires_at,agent_version,snapshot_sha256,
  observed_at,created_at)
SELECT 'ehs_'||lower(hex(randomblob(16))),workspace_id,tenant_id,edge_deployment_id,1,
  'migration_backfill',coalesce(telemetry_revision,0),'edge-health-v1',health_score,
  CASE WHEN status='revoked' THEN 'revoked' WHEN status!='active' THEN 'offline'
    WHEN health_score>=80 THEN 'healthy' WHEN health_score>=50 THEN 'degraded'
    ELSE 'critical' END,
  CASE WHEN status='revoked' THEN 'revoked'
    WHEN certificate_expires_at IS NULL THEN 'missing'
    WHEN health_code='certificate_expiring' THEN 'expiring' ELSE 'healthy' END,
  CASE WHEN agent_version IS NULL THEN 'unknown' ELSE 'current' END,
  CASE WHEN telemetry_revision IS NULL THEN 'unknown'
    WHEN health_score<50 THEN 'critical' WHEN health_score<80 THEN 'warning'
    ELSE 'healthy' END,
  CASE WHEN desired_generation=0 THEN 'not_published'
    WHEN desired_generation=applied_generation THEN 'converged' ELSE 'drifted' END,
  CASE WHEN telemetry_revision IS NULL THEN 'missing' WHEN lag_seconds<=120 THEN 'current'
    WHEN lag_seconds<=600 THEN 'lagging' ELSE 'stale' END,
  CASE WHEN validation_outcome='passed' THEN 'passed'
    WHEN validation_outcome='failed' THEN 'failed'
    WHEN validation_outcome IS NULL THEN 'missing' ELSE 'pending' END,
  desired_generation,applied_generation,lkg_generation,lag_seconds,certificate_expires_at,
  agent_version,metahub_sha256('PRD-126'||char(0)||edge_deployment_id||char(0)
    ||health_score||char(0)||coalesce(telemetry_revision,0)||char(0)
    ||desired_generation||char(0)||applied_generation||char(0)||lkg_generation),
  strftime('%Y-%m-%dT%H:%M:%fZ','now'),strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM facts;

INSERT INTO edge_health_heads(
  edge_deployment_id,workspace_id,tenant_id,generation,edge_health_snapshot_id,
  snapshot_sha256,revision,updated_at)
SELECT edge_deployment_id,workspace_id,tenant_id,generation,edge_health_snapshot_id,
  snapshot_sha256,1,created_at FROM edge_health_snapshots;

INSERT INTO edge_fleet_deployment_index(
  edge_deployment_id,workspace_id,tenant_id,region_code,mode,status,health_code,
  health_score,alert_code,maintenance_mode,agent_version,desired_generation,
  applied_generation,lkg_generation,telemetry_lag_seconds,certificate_signal,
  source_health_generation,index_revision,updated_at)
SELECT deployment.edge_deployment_id,deployment.workspace_id,deployment.tenant_id,
  deployment.region_code,deployment.mode,deployment.status,snapshot.health_code,
  snapshot.health_score,
  CASE WHEN snapshot.health_code='revoked' THEN 'revoked'
    WHEN snapshot.health_code='offline' THEN 'offline'
    WHEN snapshot.certificate_signal!='healthy' THEN 'certificate'
    WHEN snapshot.version_signal='outdated' THEN 'version'
    WHEN snapshot.capacity_signal IN('warning','critical') THEN 'capacity'
    WHEN snapshot.config_signal='drifted' THEN 'configuration'
    WHEN snapshot.telemetry_signal IN('lagging','stale','missing') THEN 'telemetry'
    WHEN snapshot.validation_signal='failed' THEN 'validation' ELSE 'none' END,
  0,snapshot.agent_version,snapshot.desired_generation,snapshot.applied_generation,
  snapshot.lkg_generation,snapshot.telemetry_lag_seconds,snapshot.certificate_signal,
  snapshot.generation,1,snapshot.created_at
FROM edge_deployments deployment
JOIN edge_health_snapshots snapshot
  ON snapshot.edge_deployment_id=deployment.edge_deployment_id AND snapshot.generation=1;

CREATE TABLE edge_diagnostic_grants(
  diagnostic_grant_id TEXT PRIMARY KEY CHECK(length(diagnostic_grant_id)=36
    AND substr(diagnostic_grant_id,1,4)='dgr_'
    AND substr(diagnostic_grant_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN(
    'pending_customer','active','revoked','expired','exhausted')),
  command_code TEXT NOT NULL CHECK(command_code IN(
    'runtime.version','runtime.watermarks','runtime.disk_budget',
    'runtime.connectivity_summary')),
  invocation_rfc8785 TEXT NOT NULL CHECK(json_valid(invocation_rfc8785)
    AND json_type(invocation_rfc8785)='object' AND length(invocation_rfc8785)<=4096),
  parameters_sha256 TEXT NOT NULL CHECK(length(parameters_sha256)=64
    AND parameters_sha256 NOT GLOB '*[^0-9a-f]*'),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  requested_by_platform_operator_id TEXT NOT NULL,
  requested_by_platform_account_id TEXT NOT NULL,
  platform_authority_revision INTEGER NOT NULL CHECK(platform_authority_revision>=1),
  approved_by_account_id TEXT,
  approved_by_membership_id TEXT,
  workspace_authority_revision INTEGER,
  deployment_revision INTEGER NOT NULL CHECK(deployment_revision>=1),
  revocation_generation INTEGER NOT NULL CHECK(revocation_generation>=1),
  max_runs INTEGER NOT NULL CHECK(max_runs BETWEEN 1 AND 3),
  run_count INTEGER NOT NULL CHECK(run_count BETWEEN 0 AND max_runs),
  revision INTEGER NOT NULL CHECK(revision>=1),
  request_idempotency_sha256 TEXT NOT NULL CHECK(length(request_idempotency_sha256)=64
    AND request_idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  approved_at TEXT CHECK(approved_at IS NULL OR unixepoch(approved_at) IS NOT NULL),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(requested_by_platform_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(requested_by_platform_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(approved_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(approved_by_membership_id)
    REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(workspace_id,tenant_id,diagnostic_grant_id),
  UNIQUE(requested_by_platform_operator_id,request_idempotency_sha256),
  CHECK((status='pending_customer' AND approved_by_account_id IS NULL
      AND approved_by_membership_id IS NULL AND workspace_authority_revision IS NULL
      AND approved_at IS NULL AND expires_at IS NULL AND revoked_at IS NULL)
    OR(status='active' AND approved_by_account_id IS NOT NULL
      AND approved_by_membership_id IS NOT NULL AND workspace_authority_revision IS NOT NULL
      AND approved_at IS NOT NULL AND expires_at IS NOT NULL AND revoked_at IS NULL
      AND unixepoch(expires_at)>unixepoch(approved_at))
    OR(status IN('expired','exhausted') AND approved_at IS NOT NULL
      AND expires_at IS NOT NULL AND revoked_at IS NULL)
    OR(status='revoked' AND revoked_at IS NOT NULL))
);
CREATE INDEX idx_edge_diagnostic_grants_owner_prd126
  ON edge_diagnostic_grants(workspace_id,tenant_id,edge_deployment_id,status,updated_at DESC);
CREATE TRIGGER trg_edge_diagnostic_grants_update_prd126
BEFORE UPDATE ON edge_diagnostic_grants FOR EACH ROW
WHEN NEW.diagnostic_grant_id!=OLD.diagnostic_grant_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.command_code!=OLD.command_code
  OR NEW.invocation_rfc8785!=OLD.invocation_rfc8785
  OR NEW.parameters_sha256!=OLD.parameters_sha256 OR NEW.reason_code!=OLD.reason_code
  OR NEW.requested_by_platform_operator_id!=OLD.requested_by_platform_operator_id
  OR NEW.requested_by_platform_account_id!=OLD.requested_by_platform_account_id
  OR NEW.platform_authority_revision!=OLD.platform_authority_revision
  OR NEW.deployment_revision!=OLD.deployment_revision
  OR NEW.revocation_generation!=OLD.revocation_generation OR NEW.max_runs!=OLD.max_runs
  OR NEW.request_idempotency_sha256!=OLD.request_idempotency_sha256
  OR NEW.request_sha256!=OLD.request_sha256 OR NEW.requested_at!=OLD.requested_at
  OR NEW.revision!=OLD.revision+1 OR NEW.run_count<OLD.run_count
  OR NEW.run_count>OLD.run_count+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status IN('revoked','expired','exhausted')
  OR (OLD.status='pending_customer' AND NEW.status NOT IN('active','revoked'))
  OR (OLD.status='active' AND NEW.status NOT IN('active','revoked','expired','exhausted'))
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_grant_transition_invalid'); END;
CREATE TRIGGER trg_edge_diagnostic_grants_delete_prd126
BEFORE DELETE ON edge_diagnostic_grants
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_grant_immutable'); END;

CREATE TABLE edge_diagnostic_commands(
  diagnostic_command_id TEXT PRIMARY KEY CHECK(length(diagnostic_command_id)=36
    AND substr(diagnostic_command_id,1,4)='dcm_'
    AND substr(diagnostic_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  diagnostic_grant_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation BETWEEN 1 AND 2147483647),
  status TEXT NOT NULL CHECK(status IN(
    'authorized','leased','running','completed','failed','cancelled','expired')),
  command_code TEXT NOT NULL CHECK(command_code IN(
    'runtime.version','runtime.watermarks','runtime.disk_budget',
    'runtime.connectivity_summary')),
  parameters_sha256 TEXT NOT NULL CHECK(length(parameters_sha256)=64
    AND parameters_sha256 NOT GLOB '*[^0-9a-f]*'),
  grant_sha256 TEXT NOT NULL CHECK(length(grant_sha256)=64
    AND grant_sha256 NOT GLOB '*[^0-9a-f]*'),
  envelope_rfc8785 TEXT NOT NULL CHECK(json_valid(envelope_rfc8785)
    AND json_type(envelope_rfc8785)='object' AND length(envelope_rfc8785)<=16384),
  envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256)=64
    AND envelope_sha256 NOT GLOB '*[^0-9a-f]*'),
  requested_by_platform_operator_id TEXT NOT NULL,
  requested_by_platform_account_id TEXT NOT NULL,
  platform_authority_revision INTEGER NOT NULL CHECK(platform_authority_revision>=1),
  step_up_grant_sha256 TEXT NOT NULL CHECK(length(step_up_grant_sha256)=64
    AND step_up_grant_sha256 NOT GLOB '*[^0-9a-f]*'),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  lease_owner_session_id TEXT,
  lease_nonce_sha256 TEXT CHECK(lease_nonce_sha256 IS NULL OR(
    length(lease_nonce_sha256)=64 AND lease_nonce_sha256 NOT GLOB '*[^0-9a-f]*')),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL
    OR unixepoch(lease_expires_at) IS NOT NULL),
  issued_at TEXT NOT NULL CHECK(unixepoch(issued_at) IS NOT NULL),
  deadline_at TEXT NOT NULL CHECK(unixepoch(deadline_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,diagnostic_grant_id)
    REFERENCES edge_diagnostic_grants(workspace_id,tenant_id,diagnostic_grant_id),
  FOREIGN KEY(requested_by_platform_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(requested_by_platform_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(lease_owner_session_id) REFERENCES edge_sessions(edge_session_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,generation),
  UNIQUE(workspace_id,tenant_id,diagnostic_command_id),
  UNIQUE(requested_by_platform_operator_id,idempotency_sha256),
  CHECK(unixepoch(issued_at)<=unixepoch(deadline_at)
    AND unixepoch(deadline_at)<=unixepoch(expires_at))
);
CREATE INDEX idx_edge_diagnostic_commands_agent_prd126
  ON edge_diagnostic_commands(edge_deployment_id,status,generation,diagnostic_command_id);
CREATE INDEX idx_edge_diagnostic_commands_deadline_prd126
  ON edge_diagnostic_commands(status,deadline_at,diagnostic_command_id);
CREATE TRIGGER trg_edge_diagnostic_commands_update_prd126
BEFORE UPDATE ON edge_diagnostic_commands FOR EACH ROW
WHEN NEW.diagnostic_command_id!=OLD.diagnostic_command_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.diagnostic_grant_id!=OLD.diagnostic_grant_id OR NEW.generation!=OLD.generation
  OR NEW.command_code!=OLD.command_code OR NEW.parameters_sha256!=OLD.parameters_sha256
  OR NEW.grant_sha256!=OLD.grant_sha256 OR NEW.envelope_rfc8785!=OLD.envelope_rfc8785
  OR NEW.envelope_sha256!=OLD.envelope_sha256
  OR NEW.requested_by_platform_operator_id!=OLD.requested_by_platform_operator_id
  OR NEW.requested_by_platform_account_id!=OLD.requested_by_platform_account_id
  OR NEW.platform_authority_revision!=OLD.platform_authority_revision
  OR NEW.step_up_grant_sha256!=OLD.step_up_grant_sha256
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256
  OR NEW.request_sha256!=OLD.request_sha256 OR NEW.issued_at!=OLD.issued_at
  OR NEW.deadline_at!=OLD.deadline_at OR NEW.expires_at!=OLD.expires_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR OLD.status IN('completed','failed','cancelled','expired')
  OR (OLD.status='authorized' AND NEW.status NOT IN('leased','cancelled','expired'))
  OR (OLD.status='leased' AND NEW.status NOT IN('authorized','running','cancelled','expired'))
  OR (OLD.status='running' AND NEW.status NOT IN('completed','failed','cancelled','expired'))
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_command_transition_invalid'); END;
CREATE TRIGGER trg_edge_diagnostic_commands_delete_prd126
BEFORE DELETE ON edge_diagnostic_commands
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_command_immutable'); END;

CREATE TABLE edge_diagnostic_results(
  diagnostic_result_id TEXT PRIMARY KEY CHECK(length(diagnostic_result_id)=36
    AND substr(diagnostic_result_id,1,4)='drs_'
    AND substr(diagnostic_result_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  diagnostic_grant_id TEXT NOT NULL,
  diagnostic_command_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('completed','failed','cancelled','timed_out')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'completed','cancelled','deadline_exceeded','handler_failed','result_invalid',
    'sensitive_output')),
  report_rfc8785 TEXT NOT NULL CHECK(json_valid(report_rfc8785)
    AND json_type(report_rfc8785)='object' AND length(report_rfc8785)<=65536),
  report_sha256 TEXT NOT NULL CHECK(length(report_sha256)=64
    AND report_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_sha256 TEXT CHECK(result_sha256 IS NULL OR(length(result_sha256)=64
    AND result_sha256 NOT GLOB '*[^0-9a-f]*')),
  duration_ms INTEGER NOT NULL CHECK(duration_ms BETWEEN 0 AND 120000),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  completed_at TEXT NOT NULL CHECK(unixepoch(completed_at) IS NOT NULL),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,diagnostic_grant_id)
    REFERENCES edge_diagnostic_grants(workspace_id,tenant_id,diagnostic_grant_id),
  FOREIGN KEY(workspace_id,tenant_id,diagnostic_command_id)
    REFERENCES edge_diagnostic_commands(workspace_id,tenant_id,diagnostic_command_id),
  UNIQUE(workspace_id,tenant_id,diagnostic_command_id),
  UNIQUE(edge_deployment_id,generation),
  CHECK((status='completed' AND reason_code='completed' AND result_sha256 IS NOT NULL)
    OR(status!='completed' AND reason_code!='completed' AND result_sha256 IS NULL)),
  CHECK(unixepoch(completed_at)>=unixepoch(started_at))
);
CREATE INDEX idx_edge_diagnostic_results_owner_prd126
  ON edge_diagnostic_results(workspace_id,tenant_id,edge_deployment_id,completed_at DESC);
CREATE TRIGGER trg_edge_diagnostic_results_update_prd126
BEFORE UPDATE ON edge_diagnostic_results
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_result_immutable'); END;
CREATE TRIGGER trg_edge_diagnostic_results_delete_prd126
BEFORE DELETE ON edge_diagnostic_results
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_result_immutable'); END;

CREATE TABLE edge_diagnostic_events(
  edge_diagnostic_event_id TEXT PRIMARY KEY CHECK(length(edge_diagnostic_event_id)=36
    AND substr(edge_diagnostic_event_id,1,4)='dge_'
    AND substr(edge_diagnostic_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  diagnostic_grant_id TEXT,
  diagnostic_command_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.health_changed','edge.diagnostic_requested','edge.diagnostic_completed')),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('system','platform','workspace','device')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 2 AND 160),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  outcome_code TEXT NOT NULL CHECK(outcome_code IN(
    'observed','requested','approved','completed','failed','cancelled','expired')),
  summary_json TEXT NOT NULL CHECK(json_valid(summary_json)
    AND json_type(summary_json)='object' AND length(summary_json)<=4096),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,diagnostic_grant_id)
    REFERENCES edge_diagnostic_grants(workspace_id,tenant_id,diagnostic_grant_id),
  FOREIGN KEY(workspace_id,tenant_id,diagnostic_command_id)
    REFERENCES edge_diagnostic_commands(workspace_id,tenant_id,diagnostic_command_id)
);
CREATE INDEX idx_edge_diagnostic_events_owner_prd126
  ON edge_diagnostic_events(workspace_id,tenant_id,edge_deployment_id,occurred_at DESC);
CREATE TRIGGER trg_edge_diagnostic_events_update_prd126
BEFORE UPDATE ON edge_diagnostic_events
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_event_immutable'); END;
CREATE TRIGGER trg_edge_diagnostic_events_delete_prd126
BEFORE DELETE ON edge_diagnostic_events
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_event_immutable'); END;

CREATE TABLE edge_diagnostic_outbox(
  edge_diagnostic_outbox_id TEXT PRIMARY KEY CHECK(length(edge_diagnostic_outbox_id)=36
    AND substr(edge_diagnostic_outbox_id,1,4)='dgo_'
    AND substr(edge_diagnostic_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_diagnostic_event_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('workspace','fleet','security')),
  status TEXT NOT NULL CHECK(status IN('pending','delivering','delivered','dead_letter')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR length(last_error_code) BETWEEN 2 AND 80),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(edge_diagnostic_event_id)
    REFERENCES edge_diagnostic_events(edge_diagnostic_event_id),
  UNIQUE(edge_diagnostic_event_id,delivery_domain),
  CHECK((status='delivered' AND delivered_at IS NOT NULL AND last_error_code IS NULL)
    OR(status!='delivered' AND delivered_at IS NULL))
);
CREATE INDEX idx_edge_diagnostic_outbox_prd126
  ON edge_diagnostic_outbox(status,available_at,edge_diagnostic_outbox_id);
CREATE TRIGGER trg_edge_diagnostic_outbox_update_prd126
BEFORE UPDATE ON edge_diagnostic_outbox FOR EACH ROW
WHEN NEW.edge_diagnostic_outbox_id!=OLD.edge_diagnostic_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_diagnostic_event_id!=OLD.edge_diagnostic_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count OR NEW.attempt_count>OLD.attempt_count+1
  OR OLD.status IN('delivered','dead_letter')
  OR (OLD.status='pending' AND NEW.status NOT IN('delivering','dead_letter'))
  OR (OLD.status='delivering' AND NEW.status NOT IN('pending','delivered','dead_letter'))
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_outbox_transition_invalid'); END;
CREATE TRIGGER trg_edge_diagnostic_outbox_delete_prd126
BEFORE DELETE ON edge_diagnostic_outbox
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_outbox_immutable'); END;

CREATE TABLE edge_diagnostic_action_commands(
  edge_diagnostic_action_command_id TEXT PRIMARY KEY
    CHECK(length(edge_diagnostic_action_command_id)=36
      AND substr(edge_diagnostic_action_command_id,1,4)='dac_'
      AND substr(edge_diagnostic_action_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 2 AND 160),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'edge.diagnostic.request_grant','edge.diagnostic.approve_grant',
    'edge.diagnostic.revoke_grant','edge.diagnostic.run','edge.diagnostic.cancel')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(actor_domain,actor_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_edge_diagnostic_actions_update_prd126
BEFORE UPDATE ON edge_diagnostic_action_commands
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_action_immutable'); END;
CREATE TRIGGER trg_edge_diagnostic_actions_delete_prd126
BEFORE DELETE ON edge_diagnostic_action_commands
BEGIN SELECT RAISE(ABORT,'edge_diagnostic_action_immutable'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd126(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-126'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd126_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd126_manifest_sources VALUES
  ('edge_health_snapshots','archive_then_delete','audit'),
  ('edge_health_heads','delete','operational'),
  ('edge_fleet_deployment_index','delete','operational'),
  ('edge_diagnostic_grants','archive_then_delete','audit'),
  ('edge_diagnostic_commands','archive_then_delete','audit'),
  ('edge_diagnostic_results','archive_then_delete','audit'),
  ('edge_diagnostic_events','archive_then_delete','audit'),
  ('edge_diagnostic_outbox','delete','operational'),
  ('edge_diagnostic_action_commands','archive_then_delete','audit');
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
JOIN prd126_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd126(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-126',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd126_manifest_sources ORDER BY source_table;
DROP TABLE prd126_manifest_sources;

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
CREATE TRIGGER trg_tenant_deletion_manifest_prd126_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd126
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd126_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd126
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd126_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd126
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd126_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd126_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_diagnostic_permission_extensions)=3
      AND (SELECT COUNT(*) FROM workspace_edge_diagnostic_role_permissions)=12
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd126)=9
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_diagnostic_permission_extensions',
      'workspace_edge_diagnostic_role_permissions','edge_health_snapshots',
      'edge_health_heads','edge_fleet_deployment_index','edge_diagnostic_grants',
      'edge_diagnostic_commands','edge_diagnostic_results','edge_diagnostic_events',
      'edge_diagnostic_outbox','edge_diagnostic_action_commands',
      'tenant_deletion_manifest_extensions_prd126'))=12 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM edge_health_heads)
      =(SELECT COUNT(*) FROM edge_deployments)
      AND (SELECT COUNT(*) FROM edge_fleet_deployment_index)
      =(SELECT COUNT(*) FROM edge_deployments) THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd126_up_final_guard;
COMMIT;
