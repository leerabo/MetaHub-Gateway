-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd122_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd122_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0113' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','edge_device_identities','edge_device_certificates','edge_sessions',
      'workspace_edge_permission_extensions','tenant_deletion_table_manifest'))=6
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd122')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd122_up_guard;

CREATE TABLE workspace_edge_telemetry_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'edge.telemetry.read','edge.telemetry.manage','edge.usage.read')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-122'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=32)
);
CREATE TABLE workspace_edge_telemetry_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_edge_telemetry_permission_extensions(permission_code)
);
INSERT INTO workspace_edge_telemetry_permission_extensions VALUES
  ('edge.telemetry.read','PRD-122',32),
  ('edge.telemetry.manage','PRD-122',32),
  ('edge.usage.read','PRD-122',32);
INSERT INTO workspace_edge_telemetry_role_permissions VALUES
  ('owner','edge.telemetry.read'),('owner','edge.telemetry.manage'),
  ('owner','edge.usage.read'),
  ('admin','edge.telemetry.read'),('admin','edge.telemetry.manage'),
  ('admin','edge.usage.read'),
  ('security_admin','edge.telemetry.read'),('security_admin','edge.telemetry.manage'),
  ('finance_admin','edge.usage.read');
CREATE TRIGGER trg_workspace_edge_telemetry_permissions_insert_prd122
BEFORE INSERT ON workspace_edge_telemetry_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_telemetry_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_telemetry_permissions_update_prd122
BEFORE UPDATE ON workspace_edge_telemetry_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_telemetry_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_telemetry_permissions_delete_prd122
BEFORE DELETE ON workspace_edge_telemetry_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_telemetry_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_telemetry_roles_insert_prd122
BEFORE INSERT ON workspace_edge_telemetry_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_telemetry_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_telemetry_roles_update_prd122
BEFORE UPDATE ON workspace_edge_telemetry_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_telemetry_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_telemetry_roles_delete_prd122
BEFORE DELETE ON workspace_edge_telemetry_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_telemetry_permission_immutable'); END;

CREATE TABLE edge_telemetry_policies(
  edge_telemetry_policy_id TEXT PRIMARY KEY CHECK(length(edge_telemetry_policy_id)=36
    AND substr(edge_telemetry_policy_id,1,4)='etp_'
    AND substr(edge_telemetry_policy_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1),
  telemetry_level TEXT NOT NULL CHECK(telemetry_level IN('minimal','operational','billing')),
  source_code TEXT NOT NULL CHECK(source_code IN('system_default','workspace_policy')),
  actor_account_id TEXT NOT NULL,
  idempotency_sha256 TEXT CHECK(idempotency_sha256 IS NULL OR(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*')),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,revision),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id,revision),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id,revision,
    telemetry_level),
  CHECK((source_code='system_default' AND revision=1 AND telemetry_level='minimal'
      AND idempotency_sha256 IS NULL)
    OR(source_code='workspace_policy' AND idempotency_sha256 IS NOT NULL))
);
CREATE TRIGGER trg_edge_telemetry_policies_update_prd122
BEFORE UPDATE ON edge_telemetry_policies
BEGIN SELECT RAISE(ABORT,'edge_telemetry_policy_immutable'); END;
CREATE TRIGGER trg_edge_telemetry_policies_delete_prd122
BEFORE DELETE ON edge_telemetry_policies
BEGIN SELECT RAISE(ABORT,'edge_telemetry_policy_immutable'); END;

CREATE TABLE edge_telemetry_policy_heads(
  edge_deployment_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_telemetry_policy_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1),
  telemetry_level TEXT NOT NULL CHECK(telemetry_level IN('minimal','operational','billing')),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id,
    revision,telemetry_level)
    REFERENCES edge_telemetry_policies(
      workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id,
      revision,telemetry_level),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id)
);
CREATE TRIGGER trg_edge_telemetry_policy_heads_update_prd122
BEFORE UPDATE ON edge_telemetry_policy_heads FOR EACH ROW
WHEN NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_telemetry_policy_head_invalid'); END;
CREATE TRIGGER trg_edge_telemetry_policy_heads_delete_prd122
BEFORE DELETE ON edge_telemetry_policy_heads
BEGIN SELECT RAISE(ABORT,'edge_telemetry_policy_head_immutable'); END;

INSERT INTO edge_telemetry_policies(
  edge_telemetry_policy_id,workspace_id,tenant_id,edge_deployment_id,revision,
  telemetry_level,source_code,actor_account_id,idempotency_sha256,request_sha256,created_at)
SELECT 'etp_'||lower(hex(randomblob(16))),workspace_id,tenant_id,edge_deployment_id,1,
  'minimal','system_default',created_by_account_id,NULL,
  metahub_sha256('PRD-122'||char(0)||edge_deployment_id||char(0)||'minimal'),
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM edge_deployments;
INSERT INTO edge_telemetry_policy_heads(
  edge_deployment_id,workspace_id,tenant_id,edge_telemetry_policy_id,revision,
  telemetry_level,updated_at)
SELECT edge_deployment_id,workspace_id,tenant_id,edge_telemetry_policy_id,revision,
  telemetry_level,created_at FROM edge_telemetry_policies;

CREATE TABLE workspace_edge_telemetry_commands(
  workspace_edge_telemetry_command_id TEXT PRIMARY KEY
    CHECK(length(workspace_edge_telemetry_command_id)=36
      AND substr(workspace_edge_telemetry_command_id,1,4)='etc_'
      AND substr(workspace_edge_telemetry_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code='telemetry_policy_patch'),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_policy_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,result_policy_id)
    REFERENCES edge_telemetry_policies(
      workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,actor_account_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_workspace_edge_telemetry_commands_update_prd122
BEFORE UPDATE ON workspace_edge_telemetry_commands
BEGIN SELECT RAISE(ABORT,'workspace_edge_telemetry_command_immutable'); END;
CREATE TRIGGER trg_workspace_edge_telemetry_commands_delete_prd122
BEFORE DELETE ON workspace_edge_telemetry_commands
BEGIN SELECT RAISE(ABORT,'workspace_edge_telemetry_command_immutable'); END;

CREATE UNIQUE INDEX idx_edge_sessions_telemetry_owner_prd122
  ON edge_sessions(workspace_id,tenant_id,edge_deployment_id,edge_session_id,
    edge_device_identity_id,edge_device_certificate_id);

CREATE TABLE edge_telemetry_batches(
  edge_telemetry_batch_id TEXT PRIMARY KEY CHECK(length(edge_telemetry_batch_id)=36
    AND substr(edge_telemetry_batch_id,1,4)='etb_'
    AND substr(edge_telemetry_batch_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_device_identity_id TEXT NOT NULL,
  edge_device_certificate_id TEXT NOT NULL,
  edge_session_id TEXT NOT NULL,
  stream_code TEXT NOT NULL CHECK(stream_code IN('health','operational','usage','failure')),
  epoch_id TEXT NOT NULL CHECK(length(epoch_id)=36 AND substr(epoch_id,1,4)='ete_'
    AND substr(epoch_id,5) NOT GLOB '*[^0-9a-f]*'),
  previous_epoch_id TEXT CHECK(previous_epoch_id IS NULL OR(length(previous_epoch_id)=36
    AND substr(previous_epoch_id,1,4)='ete_'
    AND substr(previous_epoch_id,5) NOT GLOB '*[^0-9a-f]*')),
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  edge_telemetry_policy_id TEXT NOT NULL,
  policy_revision INTEGER NOT NULL CHECK(policy_revision>=1),
  schema_version TEXT NOT NULL CHECK(schema_version='1'),
  payload_rfc8785 TEXT NOT NULL CHECK(json_valid(payload_rfc8785)
    AND json_type(payload_rfc8785)='object'
    AND length(CAST(payload_rfc8785 AS BLOB)) BETWEEN 64 AND 1048576),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  record_count INTEGER NOT NULL CHECK(record_count BETWEEN 1 AND 256),
  first_bucket_at TEXT NOT NULL CHECK(unixepoch(first_bucket_at) IS NOT NULL),
  last_bucket_at TEXT NOT NULL CHECK(unixepoch(last_bucket_at) IS NOT NULL),
  sent_at TEXT NOT NULL CHECK(unixepoch(sent_at) IS NOT NULL),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_device_identity_id)
    REFERENCES edge_device_identities(
      workspace_id,tenant_id,edge_deployment_id,edge_device_identity_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_device_certificate_id)
    REFERENCES edge_device_certificates(
      workspace_id,tenant_id,edge_deployment_id,edge_device_certificate_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_session_id,
    edge_device_identity_id,edge_device_certificate_id)
    REFERENCES edge_sessions(
      workspace_id,tenant_id,edge_deployment_id,edge_session_id,
      edge_device_identity_id,edge_device_certificate_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id,
    policy_revision)
    REFERENCES edge_telemetry_policies(
      workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id,revision),
  UNIQUE(edge_deployment_id,stream_code,epoch_id,sequence),
  UNIQUE(edge_telemetry_batch_id,payload_sha256),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_batch_id),
  CHECK(first_bucket_at<last_bucket_at),
  CHECK(previous_epoch_id IS NULL OR previous_epoch_id!=epoch_id)
);
CREATE INDEX idx_edge_telemetry_batches_owner_prd122
  ON edge_telemetry_batches(workspace_id,tenant_id,edge_deployment_id,received_at);
CREATE INDEX idx_edge_telemetry_batches_cursor_prd122
  ON edge_telemetry_batches(edge_deployment_id,stream_code,epoch_id,sequence);
CREATE TRIGGER trg_edge_telemetry_batches_update_prd122
BEFORE UPDATE ON edge_telemetry_batches
BEGIN SELECT RAISE(ABORT,'edge_telemetry_batch_immutable'); END;
CREATE TRIGGER trg_edge_telemetry_batches_delete_prd122
BEFORE DELETE ON edge_telemetry_batches
BEGIN SELECT RAISE(ABORT,'edge_telemetry_batch_immutable'); END;

CREATE TABLE edge_usage_sync_cursors(
  edge_usage_sync_cursor_id TEXT PRIMARY KEY CHECK(length(edge_usage_sync_cursor_id)=36
    AND substr(edge_usage_sync_cursor_id,1,4)='euc_'
    AND substr(edge_usage_sync_cursor_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  stream_code TEXT NOT NULL CHECK(stream_code IN('health','operational','usage','failure')),
  epoch_id TEXT NOT NULL CHECK(length(epoch_id)=36 AND substr(epoch_id,1,4)='ete_'
    AND substr(epoch_id,5) NOT GLOB '*[^0-9a-f]*'),
  previous_epoch_id TEXT CHECK(previous_epoch_id IS NULL OR(length(previous_epoch_id)=36
    AND substr(previous_epoch_id,1,4)='ete_'
    AND substr(previous_epoch_id,5) NOT GLOB '*[^0-9a-f]*')),
  status TEXT NOT NULL CHECK(status IN('active','superseded')),
  contiguous_sequence INTEGER NOT NULL CHECK(contiguous_sequence>=0),
  highest_seen_sequence INTEGER NOT NULL CHECK(highest_seen_sequence>=contiguous_sequence),
  revision INTEGER NOT NULL CHECK(revision>=1),
  opened_at TEXT NOT NULL CHECK(unixepoch(opened_at) IS NOT NULL),
  closed_at TEXT CHECK(closed_at IS NULL OR unixepoch(closed_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  UNIQUE(edge_deployment_id,stream_code,epoch_id),
  UNIQUE(workspace_id,tenant_id,edge_usage_sync_cursor_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_usage_sync_cursor_id),
  CHECK((status='active' AND closed_at IS NULL)
    OR(status='superseded' AND closed_at IS NOT NULL)),
  CHECK(previous_epoch_id IS NULL OR previous_epoch_id!=epoch_id)
);
CREATE UNIQUE INDEX idx_edge_usage_cursor_active_prd122
  ON edge_usage_sync_cursors(edge_deployment_id,stream_code) WHERE status='active';
CREATE INDEX idx_edge_usage_cursor_owner_prd122
  ON edge_usage_sync_cursors(workspace_id,tenant_id,edge_deployment_id,stream_code,status);
CREATE TRIGGER trg_edge_usage_cursors_update_prd122
BEFORE UPDATE ON edge_usage_sync_cursors FOR EACH ROW
WHEN OLD.status!='active' OR NEW.edge_usage_sync_cursor_id!=OLD.edge_usage_sync_cursor_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.stream_code!=OLD.stream_code
  OR NEW.epoch_id!=OLD.epoch_id OR coalesce(NEW.previous_epoch_id,'')!=coalesce(OLD.previous_epoch_id,'')
  OR NEW.revision!=OLD.revision+1 OR NEW.contiguous_sequence<OLD.contiguous_sequence
  OR NEW.highest_seen_sequence<OLD.highest_seen_sequence
  OR NEW.highest_seen_sequence<NEW.contiguous_sequence
  OR NEW.status NOT IN('active','superseded')
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_usage_cursor_transition_invalid'); END;
CREATE TRIGGER trg_edge_usage_cursors_delete_prd122
BEFORE DELETE ON edge_usage_sync_cursors
BEGIN SELECT RAISE(ABORT,'edge_usage_cursor_immutable'); END;

CREATE TABLE edge_telemetry_receipts(
  edge_telemetry_receipt_id TEXT PRIMARY KEY CHECK(length(edge_telemetry_receipt_id)=36
    AND substr(edge_telemetry_receipt_id,1,4)='etr_'
    AND substr(edge_telemetry_receipt_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_telemetry_batch_id TEXT NOT NULL UNIQUE,
  edge_usage_sync_cursor_id TEXT NOT NULL,
  contiguous_sequence INTEGER NOT NULL CHECK(contiguous_sequence>=0),
  expected_sequence INTEGER NOT NULL CHECK(expected_sequence=contiguous_sequence+1),
  highest_seen_sequence INTEGER NOT NULL CHECK(highest_seen_sequence>=contiguous_sequence),
  receipt_sha256 TEXT NOT NULL CHECK(length(receipt_sha256)=64
    AND receipt_sha256 NOT GLOB '*[^0-9a-f]*'),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_batch_id)
    REFERENCES edge_telemetry_batches(
      workspace_id,tenant_id,edge_deployment_id,edge_telemetry_batch_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_usage_sync_cursor_id)
    REFERENCES edge_usage_sync_cursors(
      workspace_id,tenant_id,edge_deployment_id,edge_usage_sync_cursor_id),
  UNIQUE(workspace_id,tenant_id,edge_telemetry_receipt_id)
);
CREATE TRIGGER trg_edge_telemetry_receipts_update_prd122
BEFORE UPDATE ON edge_telemetry_receipts
BEGIN SELECT RAISE(ABORT,'edge_telemetry_receipt_immutable'); END;
CREATE TRIGGER trg_edge_telemetry_receipts_delete_prd122
BEFORE DELETE ON edge_telemetry_receipts
BEGIN SELECT RAISE(ABORT,'edge_telemetry_receipt_immutable'); END;

CREATE TABLE edge_usage_reconciliations(
  edge_usage_reconciliation_id TEXT PRIMARY KEY
    CHECK(length(edge_usage_reconciliation_id)=36
      AND substr(edge_usage_reconciliation_id,1,4)='eur_'
      AND substr(edge_usage_reconciliation_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_telemetry_batch_id TEXT NOT NULL,
  record_index INTEGER NOT NULL CHECK(record_index BETWEEN 0 AND 255),
  epoch_id TEXT NOT NULL,
  sequence INTEGER NOT NULL CHECK(sequence>=1),
  model_id TEXT NOT NULL CHECK(length(model_id) BETWEEN 3 AND 80),
  provider_id TEXT NOT NULL CHECK(length(provider_id) BETWEEN 3 AND 80),
  request_count INTEGER NOT NULL CHECK(request_count>=1),
  input_tokens INTEGER NOT NULL CHECK(input_tokens>=0),
  output_tokens INTEGER NOT NULL CHECK(output_tokens>=0),
  cache_tokens INTEGER NOT NULL CHECK(cache_tokens>=0),
  cost_cny_micros INTEGER NOT NULL CHECK(cost_cny_micros>=0),
  cost_usd_micros INTEGER NOT NULL CHECK(cost_usd_micros>=0),
  bucket_start TEXT NOT NULL CHECK(unixepoch(bucket_start) IS NOT NULL),
  bucket_end TEXT NOT NULL CHECK(unixepoch(bucket_end) IS NOT NULL),
  reconciled_at TEXT NOT NULL CHECK(unixepoch(reconciled_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_batch_id)
    REFERENCES edge_telemetry_batches(
      workspace_id,tenant_id,edge_deployment_id,edge_telemetry_batch_id),
  UNIQUE(edge_telemetry_batch_id,record_index),
  UNIQUE(edge_deployment_id,epoch_id,sequence,record_index),
  CHECK(bucket_start<bucket_end)
);
CREATE INDEX idx_edge_usage_reconciliation_owner_prd122
  ON edge_usage_reconciliations(workspace_id,tenant_id,edge_deployment_id,bucket_start);
CREATE TRIGGER trg_edge_usage_reconciliations_update_prd122
BEFORE UPDATE ON edge_usage_reconciliations
BEGIN SELECT RAISE(ABORT,'edge_usage_reconciliation_immutable'); END;
CREATE TRIGGER trg_edge_usage_reconciliations_delete_prd122
BEFORE DELETE ON edge_usage_reconciliations
BEGIN SELECT RAISE(ABORT,'edge_usage_reconciliation_immutable'); END;

CREATE TABLE edge_failure_summaries(
  edge_failure_summary_id TEXT PRIMARY KEY CHECK(length(edge_failure_summary_id)=36
    AND substr(edge_failure_summary_id,1,4)='efs_'
    AND substr(edge_failure_summary_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_telemetry_batch_id TEXT NOT NULL,
  record_index INTEGER NOT NULL CHECK(record_index BETWEEN 0 AND 255),
  component_code TEXT NOT NULL CHECK(component_code IN(
    'gateway','config_sync','provider_pool','usage_spool','telemetry_sync')),
  failure_code TEXT NOT NULL CHECK(failure_code IN(
    'capacity_exhausted','config_unavailable','internal_error','network_unreachable',
    'policy_denied','provider_5xx','provider_rate_limited','provider_timeout',
    'request_cancelled')),
  failure_count INTEGER NOT NULL CHECK(failure_count>=1),
  bucket_start TEXT NOT NULL CHECK(unixepoch(bucket_start) IS NOT NULL),
  bucket_end TEXT NOT NULL CHECK(unixepoch(bucket_end) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_batch_id)
    REFERENCES edge_telemetry_batches(
      workspace_id,tenant_id,edge_deployment_id,edge_telemetry_batch_id),
  UNIQUE(edge_telemetry_batch_id,record_index),
  CHECK(bucket_start<bucket_end)
);
CREATE INDEX idx_edge_failure_summaries_owner_prd122
  ON edge_failure_summaries(workspace_id,tenant_id,edge_deployment_id,bucket_start);
CREATE TRIGGER trg_edge_failure_summaries_update_prd122
BEFORE UPDATE ON edge_failure_summaries
BEGIN SELECT RAISE(ABORT,'edge_failure_summary_immutable'); END;
CREATE TRIGGER trg_edge_failure_summaries_delete_prd122
BEFORE DELETE ON edge_failure_summaries
BEGIN SELECT RAISE(ABORT,'edge_failure_summary_immutable'); END;

CREATE TABLE edge_fleet_telemetry_projections(
  edge_deployment_id TEXT PRIMARY KEY REFERENCES edge_deployments(edge_deployment_id),
  revision INTEGER NOT NULL CHECK(revision>=1),
  last_receipt_at TEXT NOT NULL CHECK(unixepoch(last_receipt_at) IS NOT NULL),
  last_bucket_at TEXT NOT NULL CHECK(unixepoch(last_bucket_at) IS NOT NULL),
  lag_seconds INTEGER NOT NULL CHECK(lag_seconds>=0),
  backlog_seconds INTEGER NOT NULL CHECK(backlog_seconds>=0),
  cpu_bucket TEXT NOT NULL CHECK(cpu_bucket IN('unknown','low','medium','high','critical')),
  memory_bucket TEXT NOT NULL CHECK(memory_bucket IN('unknown','low','medium','high','critical')),
  error_rate_basis_points INTEGER NOT NULL CHECK(error_rate_basis_points BETWEEN 0 AND 10000),
  last_failure_code TEXT NOT NULL CHECK(last_failure_code IN(
    'none','capacity_exhausted','config_unavailable','internal_error','network_unreachable',
    'policy_denied','provider_5xx','provider_rate_limited','provider_timeout',
    'request_cancelled')),
  health_bucket_at TEXT CHECK(health_bucket_at IS NULL OR unixepoch(health_bucket_at) IS NOT NULL),
  operational_bucket_at TEXT CHECK(
    operational_bucket_at IS NULL OR unixepoch(operational_bucket_at) IS NOT NULL),
  failure_bucket_at TEXT CHECK(
    failure_bucket_at IS NULL OR unixepoch(failure_bucket_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
CREATE INDEX idx_edge_fleet_telemetry_lag_prd122
  ON edge_fleet_telemetry_projections(lag_seconds,error_rate_basis_points,updated_at);
CREATE TRIGGER trg_edge_fleet_telemetry_update_prd122
BEFORE UPDATE ON edge_fleet_telemetry_projections FOR EACH ROW
WHEN NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.revision!=OLD.revision+1
  OR NEW.last_bucket_at<OLD.last_bucket_at
  OR (OLD.health_bucket_at IS NOT NULL
    AND (NEW.health_bucket_at IS NULL OR NEW.health_bucket_at<OLD.health_bucket_at))
  OR (OLD.operational_bucket_at IS NOT NULL AND (NEW.operational_bucket_at IS NULL
    OR NEW.operational_bucket_at<OLD.operational_bucket_at))
  OR (OLD.failure_bucket_at IS NOT NULL
    AND (NEW.failure_bucket_at IS NULL OR NEW.failure_bucket_at<OLD.failure_bucket_at))
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_fleet_telemetry_transition_invalid'); END;
CREATE TRIGGER trg_edge_fleet_telemetry_delete_prd122
BEFORE DELETE ON edge_fleet_telemetry_projections
BEGIN SELECT RAISE(ABORT,'edge_fleet_telemetry_immutable'); END;

CREATE TABLE edge_telemetry_events(
  edge_telemetry_event_id TEXT PRIMARY KEY CHECK(length(edge_telemetry_event_id)=36
    AND substr(edge_telemetry_event_id,1,4)='ete_'
    AND substr(edge_telemetry_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_telemetry_batch_id TEXT,
  edge_telemetry_policy_id TEXT,
  actor_type TEXT NOT NULL CHECK(actor_type IN('agent','workspace','system')),
  actor_account_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.telemetry_received','edge.telemetry_lagging','edge.usage_reconciled')),
  stream_code TEXT CHECK(stream_code IS NULL OR stream_code IN(
    'health','operational','usage','failure')),
  sequence INTEGER CHECK(sequence IS NULL OR sequence>=1),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'batch_accepted','policy_changed','lag_threshold_exceeded','usage_committed')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_batch_id)
    REFERENCES edge_telemetry_batches(
      workspace_id,tenant_id,edge_deployment_id,edge_telemetry_batch_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id)
    REFERENCES edge_telemetry_policies(
      workspace_id,tenant_id,edge_deployment_id,edge_telemetry_policy_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,edge_telemetry_event_id)
);
CREATE INDEX idx_edge_telemetry_events_owner_prd122
  ON edge_telemetry_events(workspace_id,tenant_id,edge_deployment_id,occurred_at);
CREATE TRIGGER trg_edge_telemetry_events_update_prd122
BEFORE UPDATE ON edge_telemetry_events
BEGIN SELECT RAISE(ABORT,'edge_telemetry_event_immutable'); END;
CREATE TRIGGER trg_edge_telemetry_events_delete_prd122
BEFORE DELETE ON edge_telemetry_events
BEGIN SELECT RAISE(ABORT,'edge_telemetry_event_immutable'); END;

CREATE TABLE edge_telemetry_event_outbox(
  edge_telemetry_outbox_id TEXT PRIMARY KEY CHECK(length(edge_telemetry_outbox_id)=36
    AND substr(edge_telemetry_outbox_id,1,4)='eto_'
    AND substr(edge_telemetry_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  edge_telemetry_event_id TEXT NOT NULL REFERENCES edge_telemetry_events(edge_telemetry_event_id),
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('fleet','billing','security')),
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','dead_letter')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR length(last_error_code) BETWEEN 1 AND 80),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  UNIQUE(edge_telemetry_event_id,delivery_domain)
);
CREATE INDEX idx_edge_telemetry_outbox_delivery_prd122
  ON edge_telemetry_event_outbox(status,available_at,edge_telemetry_outbox_id);

CREATE TABLE tenant_deletion_manifest_extensions_prd122(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-122'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd122_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd122_manifest_sources VALUES
  ('edge_telemetry_policies','archive_then_delete','audit'),
  ('edge_telemetry_policy_heads','delete',NULL),
  ('workspace_edge_telemetry_commands','delete',NULL),
  ('edge_telemetry_batches','delete','operational'),
  ('edge_usage_sync_cursors','delete','operational'),
  ('edge_telemetry_receipts','delete','operational'),
  ('edge_usage_reconciliations','archive_then_delete','billing'),
  ('edge_failure_summaries','delete','operational'),
  ('edge_telemetry_events','archive_then_delete','audit');
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
JOIN prd122_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd122(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-122',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd122_manifest_sources ORDER BY source_table;
DROP TABLE prd122_manifest_sources;

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
CREATE TRIGGER trg_tenant_deletion_manifest_prd122_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd122
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd122_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd122
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd122_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd122
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd122_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd122_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_telemetry_permission_extensions)=3
      AND (SELECT COUNT(*) FROM workspace_edge_telemetry_role_permissions)=9
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd122)=9
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM edge_telemetry_policies)
      =(SELECT COUNT(*) FROM edge_deployments)
      AND (SELECT COUNT(*) FROM edge_telemetry_policy_heads)
      =(SELECT COUNT(*) FROM edge_deployments) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_telemetry_permission_extensions',
      'workspace_edge_telemetry_role_permissions','edge_telemetry_policies',
      'edge_telemetry_policy_heads','workspace_edge_telemetry_commands',
      'edge_telemetry_batches','edge_usage_sync_cursors','edge_telemetry_receipts',
      'edge_usage_reconciliations','edge_failure_summaries',
      'edge_fleet_telemetry_projections','edge_telemetry_events',
      'edge_telemetry_event_outbox','tenant_deletion_manifest_extensions_prd122'))=14
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd122_up_final_guard;
COMMIT;
