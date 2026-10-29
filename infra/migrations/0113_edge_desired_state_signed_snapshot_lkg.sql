-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd121_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd121_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0112' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','edge_device_certificates','edge_sessions','config_snapshots',
      'config_snapshot_heads','workspace_tenant_bindings','global_accounts',
      'tenant_deletion_table_manifest'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd121')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd121_up_guard;

CREATE TABLE edge_snapshot_signing_roots(
  kid TEXT PRIMARY KEY CHECK(length(kid) BETWEEN 8 AND 56 AND kid GLOB 'edge-ds-*'
    AND kid=lower(kid) AND kid NOT GLOB '*[^a-z0-9._-]*'),
  algorithm TEXT NOT NULL CHECK(algorithm='Ed25519'),
  public_key_b64 TEXT NOT NULL UNIQUE CHECK(length(public_key_b64)=43
    AND public_key_b64 NOT GLOB '*[^A-Za-z0-9_-]*'),
  public_key_sha256 TEXT NOT NULL UNIQUE CHECK(length(public_key_sha256)=64
    AND public_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE TRIGGER trg_edge_signing_roots_update_prd121
BEFORE UPDATE ON edge_snapshot_signing_roots
BEGIN SELECT RAISE(ABORT,'edge_signing_root_immutable'); END;
CREATE TRIGGER trg_edge_signing_roots_delete_prd121
BEFORE DELETE ON edge_snapshot_signing_roots
BEGIN SELECT RAISE(ABORT,'edge_signing_root_immutable'); END;

CREATE TABLE edge_trust_manifests(
  edge_trust_manifest_id TEXT PRIMARY KEY CHECK(length(edge_trust_manifest_id)=37
    AND substr(edge_trust_manifest_id,1,5)='etmf_'
    AND substr(edge_trust_manifest_id,6) NOT GLOB '*[^0-9a-f]*'),
  generation INTEGER NOT NULL UNIQUE CHECK(generation>=1),
  signer_kid TEXT NOT NULL,
  current_kid TEXT NOT NULL,
  next_kid TEXT,
  manifest_rfc8785 TEXT NOT NULL CHECK(json_valid(manifest_rfc8785)
    AND length(CAST(manifest_rfc8785 AS BLOB)) BETWEEN 128 AND 32768),
  manifest_sha256 TEXT NOT NULL UNIQUE CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  issued_at TEXT NOT NULL CHECK(unixepoch(issued_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at)>unixepoch(issued_at)),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(signer_kid) REFERENCES edge_snapshot_signing_roots(kid),
  FOREIGN KEY(current_kid) REFERENCES edge_snapshot_signing_roots(kid),
  FOREIGN KEY(next_kid) REFERENCES edge_snapshot_signing_roots(kid),
  UNIQUE(generation,edge_trust_manifest_id),
  CHECK(next_kid IS NULL OR next_kid!=current_kid)
);
CREATE TRIGGER trg_edge_trust_manifests_update_prd121
BEFORE UPDATE ON edge_trust_manifests
BEGIN SELECT RAISE(ABORT,'edge_trust_manifest_immutable'); END;
CREATE TRIGGER trg_edge_trust_manifests_delete_prd121
BEFORE DELETE ON edge_trust_manifests
BEGIN SELECT RAISE(ABORT,'edge_trust_manifest_immutable'); END;

CREATE TABLE edge_trust_manifest_heads(
  edge_trust_manifest_head_id TEXT PRIMARY KEY CHECK(length(edge_trust_manifest_head_id)=37
    AND substr(edge_trust_manifest_head_id,1,5)='etmh_'),
  singleton INTEGER NOT NULL UNIQUE CHECK(singleton=1),
  generation INTEGER NOT NULL CHECK(generation>=1),
  edge_trust_manifest_id TEXT NOT NULL UNIQUE,
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(generation,edge_trust_manifest_id)
    REFERENCES edge_trust_manifests(generation,edge_trust_manifest_id)
);
CREATE TRIGGER trg_edge_trust_heads_update_prd121
BEFORE UPDATE ON edge_trust_manifest_heads FOR EACH ROW
WHEN NEW.edge_trust_manifest_head_id!=OLD.edge_trust_manifest_head_id
  OR NEW.singleton!=OLD.singleton OR NEW.generation<=OLD.generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_trust_head_not_monotonic'); END;
CREATE TRIGGER trg_edge_trust_heads_delete_prd121
BEFORE DELETE ON edge_trust_manifest_heads
BEGIN SELECT RAISE(ABORT,'edge_trust_head_immutable'); END;

CREATE TABLE edge_desired_states(
  edge_desired_state_id TEXT PRIMARY KEY CHECK(length(edge_desired_state_id)=37
    AND substr(edge_desired_state_id,1,5)='edst_'
    AND substr(edge_desired_state_id,6) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  base_generation INTEGER NOT NULL CHECK(base_generation>=0 AND base_generation<generation),
  rollback_of_generation INTEGER CHECK(rollback_of_generation IS NULL
    OR(rollback_of_generation>=1 AND rollback_of_generation<generation)),
  source_config_snapshot_id TEXT NOT NULL,
  source_generation INTEGER NOT NULL CHECK(source_generation>=1),
  source_manifest_sha256 TEXT NOT NULL CHECK(length(source_manifest_sha256)=64
    AND source_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_revocation_watermark INTEGER NOT NULL CHECK(source_revocation_watermark>=0),
  change_class TEXT NOT NULL CHECK(change_class IN('standard','high_risk')),
  status TEXT NOT NULL CHECK(status IN(
    'approval_pending','published','superseded','rejected')),
  requested_by_account_id TEXT NOT NULL,
  approved_by_account_id TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  decided_at TEXT CHECK(decided_at IS NULL OR unixepoch(decided_at) IS NOT NULL),
  published_at TEXT CHECK(published_at IS NULL OR unixepoch(published_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,source_generation,source_config_snapshot_id)
    REFERENCES config_snapshots(workspace_id,tenant_id,generation,config_snapshot_id),
  FOREIGN KEY(requested_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(approved_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,generation),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id),
  CHECK((change_class='standard' AND status IN('published','superseded')
      AND approved_by_account_id IS NULL AND decided_at IS NULL AND published_at IS NOT NULL)
    OR(change_class='high_risk' AND (
      (status='approval_pending' AND approved_by_account_id IS NULL
        AND decided_at IS NULL AND published_at IS NULL)
      OR(status IN('published','superseded') AND approved_by_account_id IS NOT NULL
        AND approved_by_account_id!=requested_by_account_id
        AND decided_at IS NOT NULL AND published_at IS NOT NULL)
      OR(status='rejected' AND approved_by_account_id IS NOT NULL
        AND approved_by_account_id!=requested_by_account_id
        AND decided_at IS NOT NULL AND published_at IS NULL))))
);
CREATE INDEX idx_edge_desired_states_owner_prd121
  ON edge_desired_states(workspace_id,edge_deployment_id,generation DESC);
CREATE TRIGGER trg_edge_desired_states_transition_prd121
BEFORE UPDATE ON edge_desired_states FOR EACH ROW
WHEN NEW.edge_desired_state_id!=OLD.edge_desired_state_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.generation!=OLD.generation
  OR NEW.base_generation!=OLD.base_generation
  OR coalesce(NEW.rollback_of_generation,-1)!=coalesce(OLD.rollback_of_generation,-1)
  OR NEW.source_config_snapshot_id!=OLD.source_config_snapshot_id
  OR NEW.source_generation!=OLD.source_generation
  OR NEW.source_manifest_sha256!=OLD.source_manifest_sha256
  OR NEW.source_revocation_watermark!=OLD.source_revocation_watermark
  OR NEW.change_class!=OLD.change_class
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.request_sha256!=OLD.request_sha256 OR NEW.requested_at!=OLD.requested_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NOT ((OLD.status='approval_pending' AND NEW.status IN('published','rejected'))
    OR(OLD.status='published' AND NEW.status='superseded'))
BEGIN SELECT RAISE(ABORT,'edge_desired_state_transition_invalid'); END;
CREATE TRIGGER trg_edge_desired_states_delete_prd121
BEFORE DELETE ON edge_desired_states
BEGIN SELECT RAISE(ABORT,'edge_desired_state_immutable'); END;

CREATE TABLE edge_desired_state_approvals(
  edge_desired_state_approval_id TEXT PRIMARY KEY CHECK(length(edge_desired_state_approval_id)=37
    AND substr(edge_desired_state_approval_id,1,5)='edsa_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_desired_state_id TEXT NOT NULL UNIQUE,
  requested_by_account_id TEXT NOT NULL,
  decided_by_account_id TEXT NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN('approve','reject')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'approved_change','risk_unacceptable','change_not_required')),
  desired_state_revision INTEGER NOT NULL CHECK(desired_state_revision>=1),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  decided_at TEXT NOT NULL CHECK(unixepoch(decided_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id)
    REFERENCES edge_desired_states(
      workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id),
  FOREIGN KEY(requested_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(decided_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,edge_desired_state_approval_id),
  CHECK(decided_by_account_id!=requested_by_account_id)
);
CREATE TRIGGER trg_edge_desired_approvals_update_prd121
BEFORE UPDATE ON edge_desired_state_approvals
BEGIN SELECT RAISE(ABORT,'edge_desired_state_approval_immutable'); END;
CREATE TRIGGER trg_edge_desired_approvals_delete_prd121
BEFORE DELETE ON edge_desired_state_approvals
BEGIN SELECT RAISE(ABORT,'edge_desired_state_approval_immutable'); END;

CREATE TABLE edge_snapshot_artifacts(
  edge_snapshot_artifact_id TEXT PRIMARY KEY CHECK(length(edge_snapshot_artifact_id)=36
    AND substr(edge_snapshot_artifact_id,1,4)='esa_'
    AND substr(edge_snapshot_artifact_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_desired_state_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation>=1),
  content_rfc8785 TEXT NOT NULL CHECK(json_valid(content_rfc8785)
    AND length(CAST(content_rfc8785 AS BLOB)) BETWEEN 64 AND 524288),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64
    AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  content_size_bytes INTEGER NOT NULL CHECK(content_size_bytes BETWEEN 64 AND 524288),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id)
    REFERENCES edge_desired_states(
      workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,generation),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_snapshot_artifact_id),
  CHECK(content_size_bytes=length(CAST(content_rfc8785 AS BLOB)))
);
CREATE TRIGGER trg_edge_snapshot_artifacts_update_prd121
BEFORE UPDATE ON edge_snapshot_artifacts
BEGIN SELECT RAISE(ABORT,'edge_snapshot_artifact_immutable'); END;
CREATE TRIGGER trg_edge_snapshot_artifacts_delete_prd121
BEFORE DELETE ON edge_snapshot_artifacts
BEGIN SELECT RAISE(ABORT,'edge_snapshot_artifact_immutable'); END;

CREATE TABLE edge_signed_snapshots(
  edge_signed_snapshot_id TEXT PRIMARY KEY CHECK(length(edge_signed_snapshot_id)=37
    AND substr(edge_signed_snapshot_id,1,5)='edsn_'
    AND substr(edge_signed_snapshot_id,6) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_desired_state_id TEXT NOT NULL UNIQUE,
  edge_snapshot_artifact_id TEXT NOT NULL UNIQUE,
  edge_trust_manifest_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  base_generation INTEGER NOT NULL CHECK(base_generation>=0 AND base_generation<generation),
  revocation_generation INTEGER NOT NULL CHECK(revocation_generation>=1),
  domain_separator TEXT NOT NULL CHECK(domain_separator='metahub.desired-state.v1'),
  protocol_version INTEGER NOT NULL CHECK(protocol_version=1),
  algorithm TEXT NOT NULL CHECK(algorithm='Ed25519'),
  kid TEXT NOT NULL,
  envelope_rfc8785 TEXT NOT NULL CHECK(json_valid(envelope_rfc8785)
    AND length(CAST(envelope_rfc8785 AS BLOB)) BETWEEN 256 AND 786432),
  envelope_sha256 TEXT NOT NULL UNIQUE CHECK(length(envelope_sha256)=64
    AND envelope_sha256 NOT GLOB '*[^0-9a-f]*'),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  signature_b64 TEXT NOT NULL CHECK(length(signature_b64)=86
    AND signature_b64 NOT GLOB '*[^A-Za-z0-9_-]*'),
  issued_at TEXT NOT NULL CHECK(unixepoch(issued_at) IS NOT NULL),
  not_before TEXT NOT NULL CHECK(unixepoch(not_before)>=unixepoch(issued_at)),
  security_expires_at TEXT NOT NULL CHECK(
    unixepoch(security_expires_at)>unixepoch(not_before)),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at)>=unixepoch(security_expires_at)),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id)
    REFERENCES edge_desired_states(
      workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_snapshot_artifact_id)
    REFERENCES edge_snapshot_artifacts(
      workspace_id,tenant_id,edge_deployment_id,edge_snapshot_artifact_id),
  FOREIGN KEY(edge_trust_manifest_id) REFERENCES edge_trust_manifests(edge_trust_manifest_id),
  FOREIGN KEY(kid) REFERENCES edge_snapshot_signing_roots(kid),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,generation),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_signed_snapshot_id)
);
CREATE INDEX idx_edge_signed_snapshots_pull_prd121
  ON edge_signed_snapshots(edge_deployment_id,generation DESC);
CREATE TRIGGER trg_edge_signed_snapshots_update_prd121
BEFORE UPDATE ON edge_signed_snapshots
BEGIN SELECT RAISE(ABORT,'edge_signed_snapshot_immutable'); END;
CREATE TRIGGER trg_edge_signed_snapshots_delete_prd121
BEFORE DELETE ON edge_signed_snapshots
BEGIN SELECT RAISE(ABORT,'edge_signed_snapshot_immutable'); END;

CREATE TABLE edge_desired_state_heads(
  edge_desired_state_head_id TEXT PRIMARY KEY CHECK(length(edge_desired_state_head_id)=37
    AND substr(edge_desired_state_head_id,1,5)='edsh_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL UNIQUE,
  generation INTEGER NOT NULL CHECK(generation>=1),
  edge_desired_state_id TEXT NOT NULL UNIQUE,
  edge_snapshot_artifact_id TEXT NOT NULL UNIQUE,
  edge_signed_snapshot_id TEXT NOT NULL UNIQUE,
  desired_envelope_sha256 TEXT NOT NULL CHECK(length(desired_envelope_sha256)=64),
  applied_generation INTEGER NOT NULL CHECK(applied_generation>=0),
  applied_envelope_sha256 TEXT CHECK(applied_envelope_sha256 IS NULL
    OR length(applied_envelope_sha256)=64),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation>=0),
  lkg_envelope_sha256 TEXT CHECK(lkg_envelope_sha256 IS NULL
    OR length(lkg_envelope_sha256)=64),
  last_ack_status TEXT NOT NULL CHECK(last_ack_status IN(
    'not_observed','applied','failed')),
  last_reason_code TEXT NOT NULL CHECK(last_reason_code IN(
    'not_observed','applied','preflight_failed','apply_failed','storage_failed')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  published_at TEXT NOT NULL CHECK(unixepoch(published_at) IS NOT NULL),
  last_ack_at TEXT CHECK(last_ack_at IS NULL OR unixepoch(last_ack_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id)
    REFERENCES edge_desired_states(
      workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_snapshot_artifact_id)
    REFERENCES edge_snapshot_artifacts(
      workspace_id,tenant_id,edge_deployment_id,edge_snapshot_artifact_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_signed_snapshot_id)
    REFERENCES edge_signed_snapshots(
      workspace_id,tenant_id,edge_deployment_id,edge_signed_snapshot_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_desired_state_head_id),
  CHECK(applied_generation<=generation AND lkg_generation<=applied_generation),
  CHECK((applied_generation=0 AND applied_envelope_sha256 IS NULL)
    OR(applied_generation>0 AND applied_envelope_sha256 IS NOT NULL)),
  CHECK((lkg_generation=0 AND lkg_envelope_sha256 IS NULL)
    OR(lkg_generation>0 AND lkg_envelope_sha256 IS NOT NULL)),
  CHECK((last_ack_status='not_observed' AND last_ack_at IS NULL)
    OR(last_ack_status!='not_observed' AND last_ack_at IS NOT NULL))
);
CREATE INDEX idx_edge_desired_heads_lag_prd121
  ON edge_desired_state_heads(generation,applied_generation,last_ack_status,updated_at);
CREATE TRIGGER trg_edge_desired_heads_update_prd121
BEFORE UPDATE ON edge_desired_state_heads FOR EACH ROW
WHEN NEW.edge_desired_state_head_id!=OLD.edge_desired_state_head_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_deployment_id!=OLD.edge_deployment_id OR NEW.generation<OLD.generation
  OR NEW.applied_generation<OLD.applied_generation OR NEW.lkg_generation<OLD.lkg_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'edge_desired_state_head_not_monotonic'); END;
CREATE TRIGGER trg_edge_desired_heads_delete_prd121
BEFORE DELETE ON edge_desired_state_heads
BEGIN SELECT RAISE(ABORT,'edge_desired_state_head_immutable'); END;

CREATE TABLE edge_apply_attempts(
  edge_apply_attempt_id TEXT PRIMARY KEY CHECK(length(edge_apply_attempt_id)=36
    AND substr(edge_apply_attempt_id,1,4)='eap_'
    AND substr(edge_apply_attempt_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_signed_snapshot_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('applied','failed')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'applied','preflight_failed','apply_failed','storage_failed')),
  apply_duration_ms INTEGER NOT NULL CHECK(apply_duration_ms BETWEEN 0 AND 300000),
  reported_lkg_generation INTEGER NOT NULL CHECK(reported_lkg_generation>=0),
  reported_lkg_sha256 TEXT CHECK(reported_lkg_sha256 IS NULL
    OR length(reported_lkg_sha256)=64),
  attempt_nonce_hmac TEXT NOT NULL UNIQUE CHECK(length(attempt_nonce_hmac)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  recorded_at TEXT NOT NULL CHECK(unixepoch(recorded_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_signed_snapshot_id)
    REFERENCES edge_signed_snapshots(
      workspace_id,tenant_id,edge_deployment_id,edge_signed_snapshot_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_apply_attempt_id),
  CHECK((reported_lkg_generation=0 AND reported_lkg_sha256 IS NULL)
    OR(reported_lkg_generation>0 AND reported_lkg_sha256 IS NOT NULL))
);
CREATE INDEX idx_edge_apply_attempts_owner_prd121
  ON edge_apply_attempts(edge_deployment_id,generation DESC,recorded_at DESC);
CREATE TRIGGER trg_edge_apply_attempts_update_prd121
BEFORE UPDATE ON edge_apply_attempts
BEGIN SELECT RAISE(ABORT,'edge_apply_attempt_immutable'); END;
CREATE TRIGGER trg_edge_apply_attempts_delete_prd121
BEFORE DELETE ON edge_apply_attempts
BEGIN SELECT RAISE(ABORT,'edge_apply_attempt_immutable'); END;

CREATE TABLE edge_acks(
  edge_ack_id TEXT PRIMARY KEY CHECK(length(edge_ack_id)=36
    AND substr(edge_ack_id,1,4)='eak_'
    AND substr(edge_ack_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_apply_attempt_id TEXT NOT NULL UNIQUE,
  edge_signed_snapshot_id TEXT NOT NULL,
  generation INTEGER NOT NULL CHECK(generation>=1),
  envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256)=64),
  ack_status TEXT NOT NULL CHECK(ack_status IN('applied','failed')),
  disposition TEXT NOT NULL CHECK(disposition IN('accepted','late')),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  acked_at TEXT NOT NULL CHECK(unixepoch(acked_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_apply_attempt_id)
    REFERENCES edge_apply_attempts(
      workspace_id,tenant_id,edge_deployment_id,edge_apply_attempt_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_signed_snapshot_id)
    REFERENCES edge_signed_snapshots(
      workspace_id,tenant_id,edge_deployment_id,edge_signed_snapshot_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_ack_id),
  UNIQUE(edge_deployment_id,request_sha256)
);
CREATE TRIGGER trg_edge_acks_update_prd121 BEFORE UPDATE ON edge_acks
BEGIN SELECT RAISE(ABORT,'edge_ack_immutable'); END;
CREATE TRIGGER trg_edge_acks_delete_prd121 BEFORE DELETE ON edge_acks
BEGIN SELECT RAISE(ABORT,'edge_ack_immutable'); END;

CREATE TABLE edge_lkg_observations(
  edge_lkg_observation_id TEXT PRIMARY KEY CHECK(length(edge_lkg_observation_id)=36
    AND substr(edge_lkg_observation_id,1,4)='elg_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_ack_id TEXT NOT NULL UNIQUE,
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation>=1),
  lkg_envelope_sha256 TEXT NOT NULL CHECK(length(lkg_envelope_sha256)=64),
  observed_at TEXT NOT NULL CHECK(unixepoch(observed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_ack_id)
    REFERENCES edge_acks(workspace_id,tenant_id,edge_deployment_id,edge_ack_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_lkg_observation_id)
);
CREATE INDEX idx_edge_lkg_owner_prd121
  ON edge_lkg_observations(edge_deployment_id,lkg_generation DESC,observed_at DESC);
CREATE TRIGGER trg_edge_lkg_update_prd121 BEFORE UPDATE ON edge_lkg_observations
BEGIN SELECT RAISE(ABORT,'edge_lkg_observation_immutable'); END;
CREATE TRIGGER trg_edge_lkg_delete_prd121 BEFORE DELETE ON edge_lkg_observations
BEGIN SELECT RAISE(ABORT,'edge_lkg_observation_immutable'); END;

CREATE TABLE workspace_edge_state_commands(
  workspace_edge_state_command_id TEXT PRIMARY KEY CHECK(
    length(workspace_edge_state_command_id)=37
    AND substr(workspace_edge_state_command_id,1,5)='edcm_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'edge.desired_state.publish','edge.desired_state.approve',
    'edge.desired_state.reject','edge.desired_state.rollback')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_desired_state_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,result_desired_state_id)
    REFERENCES edge_desired_states(
      workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,actor_account_id,action_code,idempotency_sha256),
  UNIQUE(workspace_id,tenant_id,workspace_edge_state_command_id)
);
CREATE TRIGGER trg_workspace_edge_state_commands_update_prd121
BEFORE UPDATE ON workspace_edge_state_commands
BEGIN SELECT RAISE(ABORT,'workspace_edge_state_command_immutable'); END;
CREATE TRIGGER trg_workspace_edge_state_commands_delete_prd121
BEFORE DELETE ON workspace_edge_state_commands
BEGIN SELECT RAISE(ABORT,'workspace_edge_state_command_immutable'); END;

CREATE TABLE edge_desired_state_events(
  edge_desired_state_event_id TEXT PRIMARY KEY CHECK(length(edge_desired_state_event_id)=36
    AND substr(edge_desired_state_event_id,1,4)='ede_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_desired_state_id TEXT NOT NULL,
  actor_type TEXT NOT NULL CHECK(actor_type IN('human','agent','system')),
  actor_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.desired_state_published','edge.apply_failed',
    'edge.state_converged','edge.rollback_completed')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  outcome_code TEXT NOT NULL CHECK(outcome_code IN('succeeded','failed')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'published','approved','applied','preflight_failed','apply_failed',
    'storage_failed','rollback_applied')),
  envelope_sha256 TEXT CHECK(envelope_sha256 IS NULL OR length(envelope_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id)
    REFERENCES edge_desired_states(
      workspace_id,tenant_id,edge_deployment_id,edge_desired_state_id),
  UNIQUE(workspace_id,tenant_id,edge_desired_state_event_id),
  CHECK((actor_type='system' AND actor_id IS NULL)
    OR(actor_type!='system' AND actor_id IS NOT NULL))
);
CREATE INDEX idx_edge_desired_events_owner_prd121
  ON edge_desired_state_events(edge_deployment_id,generation DESC,occurred_at DESC);
CREATE TRIGGER trg_edge_desired_events_update_prd121
BEFORE UPDATE ON edge_desired_state_events
BEGIN SELECT RAISE(ABORT,'edge_desired_state_event_immutable'); END;
CREATE TRIGGER trg_edge_desired_events_delete_prd121
BEFORE DELETE ON edge_desired_state_events
BEGIN SELECT RAISE(ABORT,'edge_desired_state_event_immutable'); END;

CREATE TABLE edge_desired_state_event_outbox(
  edge_desired_state_outbox_id TEXT PRIMARY KEY CHECK(length(edge_desired_state_outbox_id)=36
    AND substr(edge_desired_state_outbox_id,1,4)='edo_'),
  edge_desired_state_event_id TEXT NOT NULL UNIQUE,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('workspace','fleet','security')),
  status TEXT NOT NULL CHECK(status IN('pending','delivering','delivered','dead_letter')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR length(last_error_code) BETWEEN 2 AND 64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(edge_desired_state_event_id)
    REFERENCES edge_desired_state_events(edge_desired_state_event_id) ON DELETE CASCADE
);
CREATE INDEX idx_edge_desired_outbox_delivery_prd121
  ON edge_desired_state_event_outbox(status,available_at,edge_desired_state_outbox_id);

CREATE TABLE tenant_deletion_manifest_extensions_prd121(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-121'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd121_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd121_manifest_sources VALUES
  ('edge_desired_states','delete',NULL),
  ('edge_desired_state_approvals','archive_then_delete','audit'),
  ('edge_snapshot_artifacts','delete',NULL),
  ('edge_signed_snapshots','delete',NULL),
  ('edge_desired_state_heads','delete',NULL),
  ('edge_apply_attempts','delete','operational'),
  ('edge_acks','delete','operational'),
  ('edge_lkg_observations','delete','operational'),
  ('workspace_edge_state_commands','delete',NULL),
  ('edge_desired_state_events','archive_then_delete','audit');
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
JOIN prd121_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd121(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-121',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd121_manifest_sources ORDER BY source_table;
DROP TABLE prd121_manifest_sources;

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
CREATE TRIGGER trg_tenant_deletion_manifest_prd121_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd121
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd121_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd121
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd121_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd121
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd121_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd121_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd121)=10
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_snapshot_signing_roots','edge_trust_manifests','edge_trust_manifest_heads',
      'edge_desired_states','edge_desired_state_approvals','edge_snapshot_artifacts',
      'edge_signed_snapshots','edge_desired_state_heads','edge_apply_attempts','edge_acks',
      'edge_lkg_observations','workspace_edge_state_commands','edge_desired_state_events',
      'edge_desired_state_event_outbox','tenant_deletion_manifest_extensions_prd121'))=15
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd121_up_final_guard;
COMMIT;
