-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd120_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd120_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0111' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','workspace_memberships','global_accounts',
      'platform_roles','platform_operators','tenant_deletion_table_manifest'))=6
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd120')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd120_up_guard;

CREATE TABLE workspace_edge_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'edge.deployment.read','edge.deployment.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-120'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=31)
);
CREATE TABLE workspace_edge_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_edge_permission_extensions(permission_code)
);
INSERT INTO workspace_edge_permission_extensions VALUES
  ('edge.deployment.read','PRD-120',31),
  ('edge.deployment.manage','PRD-120',31);
INSERT INTO workspace_edge_role_permissions VALUES
  ('owner','edge.deployment.read'),('owner','edge.deployment.manage'),
  ('admin','edge.deployment.read'),('admin','edge.deployment.manage'),
  ('developer','edge.deployment.read'),
  ('security_admin','edge.deployment.read');
CREATE TRIGGER trg_workspace_edge_permissions_insert_prd120
BEFORE INSERT ON workspace_edge_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_permissions_update_prd120
BEFORE UPDATE ON workspace_edge_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_permissions_delete_prd120
BEFORE DELETE ON workspace_edge_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_roles_insert_prd120
BEFORE INSERT ON workspace_edge_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_roles_update_prd120
BEFORE UPDATE ON workspace_edge_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_roles_delete_prd120
BEFORE DELETE ON workspace_edge_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_permission_immutable'); END;

CREATE TABLE platform_edge_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.edge_fleet.read','platform.edge_fleet.manage')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-120'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=18)
);
CREATE TABLE platform_edge_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code) REFERENCES platform_edge_permission_extensions(permission_code)
);
INSERT INTO platform_edge_permission_extensions VALUES
  ('platform.edge_fleet.read','PRD-120',18),
  ('platform.edge_fleet.manage','PRD-120',18);
INSERT INTO platform_edge_role_permissions VALUES
  ('platform_owner','platform.edge_fleet.read'),
  ('platform_owner','platform.edge_fleet.manage'),
  ('platform_ops','platform.edge_fleet.read'),
  ('platform_ops','platform.edge_fleet.manage'),
  ('platform_security','platform.edge_fleet.read');
CREATE TRIGGER trg_platform_edge_permissions_insert_prd120
BEFORE INSERT ON platform_edge_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_permissions_update_prd120
BEFORE UPDATE ON platform_edge_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_permissions_delete_prd120
BEFORE DELETE ON platform_edge_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_edge_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_roles_insert_prd120
BEFORE INSERT ON platform_edge_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_roles_update_prd120
BEFORE UPDATE ON platform_edge_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_permission_immutable'); END;
CREATE TRIGGER trg_platform_edge_roles_delete_prd120
BEFORE DELETE ON platform_edge_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_edge_permission_immutable'); END;

CREATE TABLE edge_deployments(
  edge_deployment_id TEXT PRIMARY KEY CHECK(length(edge_deployment_id)=36
    AND substr(edge_deployment_id,1,4)='edp_'
    AND substr(edge_deployment_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 2 AND 120
    AND display_name=trim(display_name) AND display_name NOT GLOB '*['||char(0)||'-'||char(31)||']*'),
  region_code TEXT NOT NULL CHECK(length(region_code) BETWEEN 2 AND 40
    AND region_code=lower(region_code) AND region_code NOT GLOB '*[^a-z0-9-]*'
    AND substr(region_code,1,1)!='-' AND substr(region_code,-1,1)!='-'),
  mode TEXT NOT NULL CHECK(mode IN('connected','isolated')),
  status TEXT NOT NULL CHECK(status IN('pending','active','revoked','retired')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  revocation_generation INTEGER NOT NULL CHECK(revocation_generation>=1),
  agent_version TEXT CHECK(agent_version IS NULL OR(length(agent_version) BETWEEN 1 AND 64
    AND agent_version NOT GLOB '*[^A-Za-z0-9._+-]*')),
  health_code TEXT NOT NULL CHECK(health_code IN(
    'not_registered','healthy','certificate_expiring','revoked','offline','unknown')),
  last_connected_at TEXT CHECK(last_connected_at IS NULL OR unixepoch(last_connected_at) IS NOT NULL),
  certificate_expires_at TEXT CHECK(
    certificate_expires_at IS NULL OR unixepoch(certificate_expires_at) IS NOT NULL),
  created_by_account_id TEXT NOT NULL,
  revoked_by_account_id TEXT,
  revoked_reason_code TEXT CHECK(revoked_reason_code IS NULL OR revoked_reason_code IN(
    'owner_requested','security_incident','device_lost','deployment_retired')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(revoked_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id),
  CHECK((status='revoked' AND revoked_at IS NOT NULL AND revoked_reason_code IS NOT NULL)
    OR(status!='revoked' AND revoked_at IS NULL AND revoked_reason_code IS NULL))
);
CREATE INDEX idx_edge_deployments_owner_prd120
  ON edge_deployments(workspace_id,tenant_id,status,created_at);
CREATE INDEX idx_edge_deployments_fleet_prd120
  ON edge_deployments(region_code,status,health_code,updated_at);

CREATE TABLE edge_device_identities(
  edge_device_identity_id TEXT PRIMARY KEY CHECK(length(edge_device_identity_id)=36
    AND substr(edge_device_identity_id,1,4)='edi_'
    AND substr(edge_device_identity_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  public_key_sha256 TEXT NOT NULL UNIQUE CHECK(length(public_key_sha256)=64
    AND public_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  hardware_proof_sha256 TEXT CHECK(hardware_proof_sha256 IS NULL OR(
    length(hardware_proof_sha256)=64 AND hardware_proof_sha256 NOT GLOB '*[^0-9a-f]*')),
  capability_codes_json TEXT NOT NULL CHECK(json_valid(capability_codes_json)
    AND json_type(capability_codes_json)='array' AND length(capability_codes_json)<=1024),
  agent_version TEXT NOT NULL CHECK(length(agent_version) BETWEEN 1 AND 64
    AND agent_version NOT GLOB '*[^A-Za-z0-9._+-]*'),
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('active','superseded','revoked','retired')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_device_identity_id),
  UNIQUE(edge_deployment_id,generation)
);
CREATE UNIQUE INDEX idx_edge_identity_active_prd120
  ON edge_device_identities(edge_deployment_id) WHERE status='active';

CREATE TABLE edge_device_certificates(
  edge_device_certificate_id TEXT PRIMARY KEY CHECK(length(edge_device_certificate_id)=36
    AND substr(edge_device_certificate_id,1,4)='edc_'
    AND substr(edge_device_certificate_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_device_identity_id TEXT NOT NULL,
  prior_certificate_id TEXT,
  serial_hex TEXT NOT NULL UNIQUE CHECK(length(serial_hex) BETWEEN 2 AND 40
    AND serial_hex=lower(serial_hex) AND serial_hex NOT GLOB '*[^0-9a-f]*'),
  certificate_sha256 TEXT NOT NULL UNIQUE CHECK(length(certificate_sha256)=64
    AND certificate_sha256 NOT GLOB '*[^0-9a-f]*'),
  public_key_sha256 TEXT NOT NULL CHECK(length(public_key_sha256)=64
    AND public_key_sha256 NOT GLOB '*[^0-9a-f]*'),
  csr_sha256 TEXT NOT NULL UNIQUE CHECK(length(csr_sha256)=64
    AND csr_sha256 NOT GLOB '*[^0-9a-f]*'),
  certificate_pem TEXT NOT NULL CHECK(length(certificate_pem) BETWEEN 256 AND 8192
    AND certificate_pem LIKE '-----BEGIN CERTIFICATE-----%'),
  issuer_key_id TEXT NOT NULL CHECK(issuer_key_id='edge-device-ca-v1'),
  generation INTEGER NOT NULL CHECK(generation>=1),
  status TEXT NOT NULL CHECK(status IN('active','draining','superseded','revoked','expired')),
  not_before TEXT NOT NULL CHECK(unixepoch(not_before) IS NOT NULL),
  not_after TEXT NOT NULL CHECK(unixepoch(not_after) IS NOT NULL),
  drain_until TEXT CHECK(drain_until IS NULL OR unixepoch(drain_until) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_device_identity_id)
    REFERENCES edge_device_identities(
      workspace_id,tenant_id,edge_deployment_id,edge_device_identity_id),
  FOREIGN KEY(prior_certificate_id) REFERENCES edge_device_certificates(edge_device_certificate_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,edge_device_certificate_id),
  UNIQUE(edge_deployment_id,generation),
  CHECK(unixepoch(not_after)>unixepoch(not_before)),
  CHECK((status='draining' AND drain_until IS NOT NULL)
    OR(status!='draining' AND drain_until IS NULL)),
  CHECK((status='revoked' AND revoked_at IS NOT NULL) OR status!='revoked')
);
CREATE INDEX idx_edge_certificates_owner_prd120
  ON edge_device_certificates(edge_deployment_id,status,not_after);

CREATE TABLE edge_registration_intents(
  edge_registration_intent_id TEXT PRIMARY KEY CHECK(length(edge_registration_intent_id)=36
    AND substr(edge_registration_intent_id,1,4)='eri_'
    AND substr(edge_registration_intent_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  created_by_account_id TEXT NOT NULL,
  token_hmac TEXT NOT NULL UNIQUE CHECK(length(token_hmac)=64
    AND token_hmac NOT GLOB '*[^0-9a-f]*'),
  token_key_version TEXT NOT NULL CHECK(token_key_version='edge-registration-v1'),
  challenge_hmac TEXT NOT NULL UNIQUE CHECK(length(challenge_hmac)=64
    AND challenge_hmac NOT GLOB '*[^0-9a-f]*'),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  csr_sha256 TEXT CHECK(csr_sha256 IS NULL OR(length(csr_sha256)=64
    AND csr_sha256 NOT GLOB '*[^0-9a-f]*')),
  public_key_sha256 TEXT CHECK(public_key_sha256 IS NULL OR(length(public_key_sha256)=64
    AND public_key_sha256 NOT GLOB '*[^0-9a-f]*')),
  consumed_request_sha256 TEXT CHECK(consumed_request_sha256 IS NULL OR(
    length(consumed_request_sha256)=64 AND consumed_request_sha256 NOT GLOB '*[^0-9a-f]*')),
  result_identity_id TEXT,
  result_certificate_id TEXT,
  status TEXT NOT NULL CHECK(status IN('issued','consumed','expired','revoked')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  consumed_at TEXT CHECK(consumed_at IS NULL OR unixepoch(consumed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(result_identity_id) REFERENCES edge_device_identities(edge_device_identity_id),
  FOREIGN KEY(result_certificate_id) REFERENCES edge_device_certificates(edge_device_certificate_id),
  UNIQUE(workspace_id,tenant_id,edge_registration_intent_id),
  UNIQUE(workspace_id,created_by_account_id,idempotency_sha256),
  CHECK((status='consumed' AND consumed_at IS NOT NULL AND csr_sha256 IS NOT NULL
    AND public_key_sha256 IS NOT NULL AND consumed_request_sha256 IS NOT NULL
    AND result_identity_id IS NOT NULL AND result_certificate_id IS NOT NULL)
    OR(status!='consumed' AND consumed_at IS NULL AND csr_sha256 IS NULL
      AND public_key_sha256 IS NULL AND consumed_request_sha256 IS NULL
      AND result_identity_id IS NULL AND result_certificate_id IS NULL))
);
CREATE INDEX idx_edge_registration_expiry_prd120
  ON edge_registration_intents(status,expires_at);

CREATE TABLE edge_sessions(
  edge_session_id TEXT PRIMARY KEY CHECK(length(edge_session_id)=36
    AND substr(edge_session_id,1,4)='eds_'
    AND substr(edge_session_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_device_identity_id TEXT NOT NULL,
  edge_device_certificate_id TEXT NOT NULL,
  session_token_hmac TEXT NOT NULL UNIQUE CHECK(length(session_token_hmac)=64
    AND session_token_hmac NOT GLOB '*[^0-9a-f]*'),
  peer_binding_sha256 TEXT NOT NULL CHECK(length(peer_binding_sha256)=64
    AND peer_binding_sha256 NOT GLOB '*[^0-9a-f]*'),
  handshake_nonce_hmac TEXT NOT NULL CHECK(length(handshake_nonce_hmac)=64
    AND handshake_nonce_hmac NOT GLOB '*[^0-9a-f]*'),
  identity_generation INTEGER NOT NULL CHECK(identity_generation>=1),
  revocation_generation INTEGER NOT NULL CHECK(revocation_generation>=1),
  status TEXT NOT NULL CHECK(status IN('active','expired','revoked')),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  revoked_reason_code TEXT CHECK(revoked_reason_code IS NULL OR revoked_reason_code IN(
    'deployment_revoked','certificate_revoked','identity_rotated','session_replaced')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_device_identity_id)
    REFERENCES edge_device_identities(
      workspace_id,tenant_id,edge_deployment_id,edge_device_identity_id),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id,edge_device_certificate_id)
    REFERENCES edge_device_certificates(
      workspace_id,tenant_id,edge_deployment_id,edge_device_certificate_id),
  UNIQUE(workspace_id,tenant_id,edge_session_id),
  UNIQUE(edge_deployment_id,handshake_nonce_hmac),
  CHECK((status='revoked' AND revoked_at IS NOT NULL AND revoked_reason_code IS NOT NULL)
    OR(status!='revoked' AND revoked_at IS NULL AND revoked_reason_code IS NULL))
);
CREATE INDEX idx_edge_sessions_active_prd120
  ON edge_sessions(edge_deployment_id,status,expires_at);

CREATE TABLE workspace_edge_commands(
  workspace_edge_command_id TEXT PRIMARY KEY CHECK(length(workspace_edge_command_id)=36
    AND substr(workspace_edge_command_id,1,4)='egc_'
    AND substr(workspace_edge_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN('edge.registration.create','edge.revoke')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,actor_account_id,action_code,idempotency_sha256),
  UNIQUE(workspace_id,tenant_id,workspace_edge_command_id)
);
CREATE TRIGGER trg_workspace_edge_commands_update_prd120
BEFORE UPDATE ON workspace_edge_commands
BEGIN SELECT RAISE(ABORT,'workspace_edge_command_immutable'); END;
CREATE TRIGGER trg_workspace_edge_commands_delete_prd120
BEFORE DELETE ON workspace_edge_commands
BEGIN SELECT RAISE(ABORT,'workspace_edge_command_immutable'); END;

CREATE TABLE edge_machine_commands(
  edge_machine_command_id TEXT PRIMARY KEY CHECK(length(edge_machine_command_id)=36
    AND substr(edge_machine_command_id,1,4)='emc_'
    AND substr(edge_machine_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  source_certificate_sha256 TEXT NOT NULL CHECK(length(source_certificate_sha256)=64
    AND source_certificate_sha256 NOT GLOB '*[^0-9a-f]*'),
  action_code TEXT NOT NULL CHECK(action_code='edge.certificate.rotate'),
  nonce_hmac TEXT NOT NULL CHECK(length(nonce_hmac)=64
    AND nonce_hmac NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_identity_id TEXT NOT NULL,
  result_certificate_id TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(result_identity_id) REFERENCES edge_device_identities(edge_device_identity_id),
  FOREIGN KEY(result_certificate_id) REFERENCES edge_device_certificates(edge_device_certificate_id),
  UNIQUE(edge_deployment_id,source_certificate_sha256,action_code,nonce_hmac),
  UNIQUE(workspace_id,tenant_id,edge_machine_command_id)
);
CREATE TRIGGER trg_edge_machine_commands_update_prd120
BEFORE UPDATE ON edge_machine_commands
BEGIN SELECT RAISE(ABORT,'edge_machine_command_immutable'); END;
CREATE TRIGGER trg_edge_machine_commands_delete_prd120
BEFORE DELETE ON edge_machine_commands
BEGIN SELECT RAISE(ABORT,'edge_machine_command_immutable'); END;

CREATE TABLE edge_identity_events(
  edge_identity_event_id TEXT PRIMARY KEY CHECK(length(edge_identity_event_id)=36
    AND substr(edge_identity_event_id,1,4)='ege_'
    AND substr(edge_identity_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  edge_device_identity_id TEXT,
  edge_device_certificate_id TEXT,
  actor_type TEXT NOT NULL CHECK(actor_type IN('human','agent','system')),
  actor_account_id TEXT,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.registered','edge.certificate_rotated','edge.revoked','edge.authentication_failed')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  outcome_code TEXT NOT NULL CHECK(outcome_code IN('succeeded','rejected')),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'registered','rotated','owner_requested','security_incident','device_lost',
    'deployment_retired','credential_invalid','credential_expired','credential_replayed',
    'peer_unverified','certificate_inactive','generation_conflict')),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(edge_device_identity_id) REFERENCES edge_device_identities(edge_device_identity_id),
  FOREIGN KEY(edge_device_certificate_id)
    REFERENCES edge_device_certificates(edge_device_certificate_id),
  FOREIGN KEY(actor_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,edge_identity_event_id),
  CHECK((actor_type='human' AND actor_account_id IS NOT NULL)
    OR(actor_type!='human' AND actor_account_id IS NULL))
);
CREATE INDEX idx_edge_identity_events_owner_prd120
  ON edge_identity_events(workspace_id,tenant_id,edge_deployment_id,occurred_at);
CREATE TRIGGER trg_edge_identity_events_update_prd120
BEFORE UPDATE ON edge_identity_events
BEGIN SELECT RAISE(ABORT,'edge_identity_event_immutable'); END;
CREATE TRIGGER trg_edge_identity_events_delete_prd120
BEFORE DELETE ON edge_identity_events
BEGIN SELECT RAISE(ABORT,'edge_identity_event_immutable'); END;

CREATE TABLE edge_identity_event_outbox(
  edge_identity_outbox_id TEXT PRIMARY KEY CHECK(length(edge_identity_outbox_id)=36
    AND substr(edge_identity_outbox_id,1,4)='ego_'
    AND substr(edge_identity_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  edge_identity_event_id TEXT NOT NULL UNIQUE,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('workspace','fleet','security')),
  status TEXT NOT NULL CHECK(status IN('pending','delivering','delivered','dead_letter')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR(length(last_error_code) BETWEEN 2 AND 64
    AND last_error_code=lower(last_error_code)
    AND last_error_code NOT GLOB '*[^a-z0-9_.]*')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(edge_identity_event_id) REFERENCES edge_identity_events(edge_identity_event_id)
    ON DELETE CASCADE
);
CREATE INDEX idx_edge_identity_outbox_delivery_prd120
  ON edge_identity_event_outbox(status,available_at,edge_identity_outbox_id);

-- Register every tenant-owned PRD-120 table in the deletion manifest.
CREATE TABLE tenant_deletion_manifest_extensions_prd120(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-120'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd120_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd120_manifest_sources VALUES
  ('edge_deployments','delete',NULL),
  ('edge_registration_intents','delete',NULL),
  ('edge_device_identities','delete',NULL),
  ('edge_device_certificates','delete',NULL),
  ('edge_sessions','delete','operational'),
  ('workspace_edge_commands','delete',NULL),
  ('edge_machine_commands','delete','operational'),
  ('edge_identity_events','archive_then_delete','audit');
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
JOIN prd120_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd120(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-120',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd120_manifest_sources ORDER BY source_table;
DROP TABLE prd120_manifest_sources;

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
CREATE TRIGGER trg_tenant_deletion_manifest_prd120_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd120
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd120_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd120
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd120_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd120
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd120_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd120_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_permission_extensions)=2
    AND (SELECT COUNT(*) FROM workspace_edge_role_permissions)=6 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_edge_permission_extensions)=2
    AND (SELECT COUNT(*) FROM platform_edge_role_permissions)=5 THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd120)=8
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd120_up_final_guard;
COMMIT;
