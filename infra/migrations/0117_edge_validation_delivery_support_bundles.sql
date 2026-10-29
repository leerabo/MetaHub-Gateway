-- metahub:expected-version-v1
PRAGMA foreign_keys = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd125_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd125_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0116' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'edge_deployments','edge_installations','edge_desired_state_heads','support_grants',
      'support_grant_approvals','support_sessions','tenant_deletion_table_manifest',
      'tenant_deletion_trigger_manifest'))=8 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd125')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd125_up_guard;

CREATE TABLE workspace_edge_validation_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'edge.validation.read','edge.validation.manage','edge.delivery.accept',
    'edge.support_bundle.read')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-125'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=35)
);
CREATE TABLE workspace_edge_validation_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_edge_validation_permission_extensions(permission_code)
);
INSERT INTO workspace_edge_validation_permission_extensions VALUES
  ('edge.validation.read','PRD-125',35),
  ('edge.validation.manage','PRD-125',35),
  ('edge.delivery.accept','PRD-125',35),
  ('edge.support_bundle.read','PRD-125',35);
INSERT INTO workspace_edge_validation_role_permissions VALUES
  ('owner','edge.validation.read'),
  ('owner','edge.validation.manage'),
  ('owner','edge.delivery.accept'),
  ('owner','edge.support_bundle.read'),
  ('admin','edge.validation.read'),
  ('admin','edge.validation.manage'),
  ('admin','edge.delivery.accept'),
  ('admin','edge.support_bundle.read'),
  ('security_admin','edge.validation.read'),
  ('security_admin','edge.support_bundle.read'),
  ('auditor','edge.validation.read');
CREATE TRIGGER trg_workspace_edge_validation_permissions_insert_prd125
BEFORE INSERT ON workspace_edge_validation_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_validation_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_validation_permissions_update_prd125
BEFORE UPDATE ON workspace_edge_validation_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_validation_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_validation_permissions_delete_prd125
BEFORE DELETE ON workspace_edge_validation_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_edge_validation_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_validation_roles_insert_prd125
BEFORE INSERT ON workspace_edge_validation_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_validation_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_validation_roles_update_prd125
BEFORE UPDATE ON workspace_edge_validation_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_validation_permission_immutable'); END;
CREATE TRIGGER trg_workspace_edge_validation_roles_delete_prd125
BEFORE DELETE ON workspace_edge_validation_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_edge_validation_permission_immutable'); END;

CREATE TABLE edge_validation_runs(
  validation_run_id TEXT PRIMARY KEY CHECK(length(validation_run_id)=36
    AND substr(validation_run_id,1,4)='dvr_'
    AND substr(validation_run_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  source_validation_run_id TEXT,
  attempt INTEGER NOT NULL CHECK(attempt BETWEEN 1 AND 2147483647),
  plan_version TEXT NOT NULL CHECK(plan_version='edge-delivery-v1'),
  deployment_revision INTEGER NOT NULL CHECK(deployment_revision BETWEEN 1 AND 2147483647),
  installer_release_id TEXT NOT NULL,
  desired_generation INTEGER NOT NULL CHECK(desired_generation BETWEEN 0 AND 2147483647),
  applied_generation INTEGER NOT NULL CHECK(applied_generation BETWEEN 0 AND 2147483647),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation BETWEEN 0 AND 2147483647),
  input_contract_sha256 TEXT NOT NULL CHECK(length(input_contract_sha256)=64
    AND input_contract_sha256 NOT GLOB '*[^0-9a-f]*'),
  requested_check_codes_json TEXT NOT NULL CHECK(
    json_valid(requested_check_codes_json)
    AND json_type(requested_check_codes_json)='array'
    AND json_array_length(requested_check_codes_json) BETWEEN 1 AND 8
    AND length(requested_check_codes_json) BETWEEN 22 AND 256),
  status TEXT NOT NULL CHECK(status IN('requested','completed')),
  outcome TEXT CHECK(outcome IS NULL OR outcome IN('passed','failed')),
  duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms BETWEEN 0 AND 900000),
  requested_by_account_id TEXT NOT NULL CHECK(length(requested_by_account_id) BETWEEN 3 AND 160),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(installer_release_id) REFERENCES installer_releases(installer_release_id),
  FOREIGN KEY(workspace_id,tenant_id,source_validation_run_id)
    REFERENCES edge_validation_runs(workspace_id,tenant_id,validation_run_id),
  UNIQUE(workspace_id,tenant_id,validation_run_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,input_contract_sha256,attempt),
  UNIQUE(workspace_id,tenant_id,idempotency_sha256),
  CHECK((source_validation_run_id IS NULL AND attempt=1)
    OR(source_validation_run_id IS NOT NULL AND attempt>1)),
  CHECK((status='requested' AND outcome IS NULL AND duration_ms IS NULL
      AND started_at IS NULL AND completed_at IS NULL)
    OR(status='completed' AND outcome IS NOT NULL AND duration_ms IS NOT NULL
      AND started_at IS NOT NULL AND completed_at IS NOT NULL
      AND unixepoch(completed_at)>=unixepoch(started_at)))
);
CREATE INDEX idx_edge_validation_runs_owner_prd125
  ON edge_validation_runs(workspace_id,tenant_id,edge_deployment_id,created_at DESC,
    validation_run_id);
CREATE TRIGGER trg_edge_validation_runs_update_prd125
BEFORE UPDATE ON edge_validation_runs FOR EACH ROW
WHEN NEW.validation_run_id!=OLD.validation_run_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.source_validation_run_id IS NOT OLD.source_validation_run_id
  OR NEW.attempt!=OLD.attempt OR NEW.plan_version!=OLD.plan_version
  OR NEW.deployment_revision!=OLD.deployment_revision
  OR NEW.installer_release_id!=OLD.installer_release_id
  OR NEW.desired_generation!=OLD.desired_generation
  OR NEW.applied_generation!=OLD.applied_generation OR NEW.lkg_generation!=OLD.lkg_generation
  OR NEW.input_contract_sha256!=OLD.input_contract_sha256
  OR NEW.requested_check_codes_json!=OLD.requested_check_codes_json
  OR NEW.requested_by_account_id!=OLD.requested_by_account_id
  OR NEW.idempotency_sha256!=OLD.idempotency_sha256
  OR NEW.request_sha256!=OLD.request_sha256 OR NEW.created_at!=OLD.created_at
  OR OLD.status!='requested' OR NEW.status!='completed'
BEGIN SELECT RAISE(ABORT,'edge_validation_run_immutable'); END;
CREATE TRIGGER trg_edge_validation_runs_delete_prd125
BEFORE DELETE ON edge_validation_runs
BEGIN SELECT RAISE(ABORT,'edge_validation_run_immutable'); END;

CREATE TABLE edge_validation_checks(
  validation_check_id TEXT PRIMARY KEY CHECK(length(validation_check_id)=36
    AND substr(validation_check_id,1,4)='dvc_'
    AND substr(validation_check_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  validation_run_id TEXT NOT NULL,
  check_code TEXT NOT NULL CHECK(check_code IN(
    'device_certificate','signed_config_lkg','provider_connectivity',
    'minimal_gateway_request','usage_cost_failure_sync','backup_readiness',
    'capacity_readiness','performance_baseline')),
  outcome TEXT NOT NULL CHECK(outcome IN('passed','failed')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 2 AND 80
    AND reason_code=lower(reason_code) AND reason_code NOT GLOB '*[^a-z0-9_.-]*'),
  recovery_action_code TEXT CHECK(recovery_action_code IS NULL OR(
    length(recovery_action_code) BETWEEN 2 AND 80
    AND recovery_action_code=lower(recovery_action_code)
    AND recovery_action_code NOT GLOB '*[^a-z0-9_.-]*')),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64
    AND evidence_sha256 NOT GLOB '*[^0-9a-f]*'),
  duration_ms INTEGER NOT NULL CHECK(duration_ms BETWEEN 0 AND 900000),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,validation_run_id)
    REFERENCES edge_validation_runs(workspace_id,tenant_id,validation_run_id),
  UNIQUE(workspace_id,tenant_id,validation_run_id,check_code),
  CHECK((outcome='passed' AND recovery_action_code IS NULL)
    OR(outcome='failed' AND recovery_action_code IS NOT NULL))
);
CREATE INDEX idx_edge_validation_checks_owner_prd125
  ON edge_validation_checks(workspace_id,tenant_id,edge_deployment_id,check_code,
    created_at DESC);
CREATE TRIGGER trg_edge_validation_checks_insert_prd125
BEFORE INSERT ON edge_validation_checks FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM edge_validation_runs run
  WHERE run.workspace_id=NEW.workspace_id AND run.tenant_id=NEW.tenant_id
    AND run.edge_deployment_id=NEW.edge_deployment_id
    AND run.validation_run_id=NEW.validation_run_id AND run.status='requested')
BEGIN SELECT RAISE(ABORT,'edge_validation_check_parent_immutable'); END;
CREATE TRIGGER trg_edge_validation_checks_update_prd125
BEFORE UPDATE ON edge_validation_checks
BEGIN SELECT RAISE(ABORT,'edge_validation_check_immutable'); END;
CREATE TRIGGER trg_edge_validation_checks_delete_prd125
BEFORE DELETE ON edge_validation_checks
BEGIN SELECT RAISE(ABORT,'edge_validation_check_immutable'); END;

CREATE TABLE edge_delivery_acceptances(
  delivery_acceptance_id TEXT PRIMARY KEY CHECK(length(delivery_acceptance_id)=36
    AND substr(delivery_acceptance_id,1,4)='eda_'
    AND substr(delivery_acceptance_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  validation_run_id TEXT NOT NULL,
  validation_set_sha256 TEXT NOT NULL CHECK(length(validation_set_sha256)=64
    AND validation_set_sha256 NOT GLOB '*[^0-9a-f]*'),
  deployment_revision INTEGER NOT NULL CHECK(deployment_revision BETWEEN 1 AND 2147483647),
  installer_release_id TEXT NOT NULL,
  desired_generation INTEGER NOT NULL CHECK(desired_generation BETWEEN 0 AND 2147483647),
  applied_generation INTEGER NOT NULL CHECK(applied_generation BETWEEN 0 AND 2147483647),
  lkg_generation INTEGER NOT NULL CHECK(lkg_generation BETWEEN 0 AND 2147483647),
  accepted_by_account_id TEXT NOT NULL CHECK(length(accepted_by_account_id) BETWEEN 3 AND 160),
  accepted_by_membership_id TEXT NOT NULL,
  membership_revision INTEGER NOT NULL CHECK(membership_revision BETWEEN 1 AND 2147483647),
  step_up_grant_sha256 TEXT NOT NULL CHECK(length(step_up_grant_sha256)=64
    AND step_up_grant_sha256 NOT GLOB '*[^0-9a-f]*'),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  accepted_at TEXT NOT NULL CHECK(unixepoch(accepted_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(workspace_id,tenant_id,validation_run_id)
    REFERENCES edge_validation_runs(workspace_id,tenant_id,validation_run_id),
  FOREIGN KEY(accepted_by_membership_id)
    REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(workspace_id,tenant_id,edge_deployment_id,validation_set_sha256),
  UNIQUE(workspace_id,tenant_id,idempotency_sha256)
);
CREATE TRIGGER trg_edge_delivery_acceptances_update_prd125
BEFORE UPDATE ON edge_delivery_acceptances
BEGIN SELECT RAISE(ABORT,'edge_delivery_acceptance_immutable'); END;
CREATE TRIGGER trg_edge_delivery_acceptances_delete_prd125
BEFORE DELETE ON edge_delivery_acceptances
BEGIN SELECT RAISE(ABORT,'edge_delivery_acceptance_immutable'); END;

CREATE TABLE edge_support_bundles(
  support_bundle_id TEXT PRIMARY KEY CHECK(length(support_bundle_id)=36
    AND substr(support_bundle_id,1,4)='esb_'
    AND substr(support_bundle_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('ready','expired','deleted')),
  revision INTEGER NOT NULL CHECK(revision BETWEEN 1 AND 2147483647),
  manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64
    AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  entry_summary_json TEXT NOT NULL CHECK(json_valid(entry_summary_json)
    AND json_type(entry_summary_json)='array'
    AND json_array_length(entry_summary_json) BETWEEN 1 AND 64
    AND length(entry_summary_json)<=8192
    AND lower(entry_summary_json) NOT LIKE '%path%'
    AND lower(entry_summary_json) NOT LIKE '%content%'
    AND lower(entry_summary_json) NOT LIKE '%secret%'
    AND lower(entry_summary_json) NOT LIKE '%token%'),
  scan_version TEXT NOT NULL CHECK(scan_version='support-bundle-scan-v1'),
  scan_outcome TEXT NOT NULL CHECK(scan_outcome='clean'),
  archive_size_bytes INTEGER NOT NULL CHECK(archive_size_bytes BETWEEN 1 AND 104857600),
  object_sha256 TEXT NOT NULL CHECK(length(object_sha256)=64
    AND object_sha256 NOT GLOB '*[^0-9a-f]*'),
  object_size_bytes INTEGER NOT NULL CHECK(object_size_bytes BETWEEN 1 AND 104857664),
  object_reference TEXT NOT NULL UNIQUE CHECK(length(object_reference) BETWEEN 64 AND 240
    AND object_reference GLOB 'edge-support-bundles/esb_*.enc'
    AND instr(object_reference,'..')=0 AND instr(object_reference,'\')=0),
  approved_by_account_id TEXT NOT NULL CHECK(length(approved_by_account_id) BETWEEN 3 AND 160),
  approval_proof_sha256 TEXT NOT NULL CHECK(length(approval_proof_sha256)=64
    AND approval_proof_sha256 NOT GLOB '*[^0-9a-f]*'),
  source_edge_session_id TEXT NOT NULL,
  source_identity_generation INTEGER NOT NULL CHECK(source_identity_generation BETWEEN 1 AND 2147483647),
  source_revocation_generation INTEGER NOT NULL CHECK(source_revocation_generation>=0),
  approved_at TEXT NOT NULL CHECK(unixepoch(approved_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id),
  FOREIGN KEY(source_edge_session_id) REFERENCES edge_sessions(edge_session_id),
  UNIQUE(workspace_id,tenant_id,support_bundle_id),
  CHECK(unixepoch(expires_at)>unixepoch(approved_at)),
  CHECK((status='ready' AND revision=1) OR(status IN('expired','deleted') AND revision>=2))
);
CREATE INDEX idx_edge_support_bundles_owner_prd125
  ON edge_support_bundles(workspace_id,tenant_id,edge_deployment_id,status,
    created_at DESC,support_bundle_id);
CREATE TRIGGER trg_edge_support_bundles_update_prd125
BEFORE UPDATE ON edge_support_bundles FOR EACH ROW
WHEN NEW.support_bundle_id!=OLD.support_bundle_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.edge_deployment_id!=OLD.edge_deployment_id
  OR NEW.manifest_sha256!=OLD.manifest_sha256
  OR NEW.entry_summary_json!=OLD.entry_summary_json OR NEW.scan_version!=OLD.scan_version
  OR NEW.scan_outcome!=OLD.scan_outcome OR NEW.archive_size_bytes!=OLD.archive_size_bytes
  OR NEW.object_sha256!=OLD.object_sha256 OR NEW.object_size_bytes!=OLD.object_size_bytes
  OR NEW.object_reference!=OLD.object_reference
  OR NEW.approved_by_account_id!=OLD.approved_by_account_id
  OR NEW.approval_proof_sha256!=OLD.approval_proof_sha256
  OR NEW.source_edge_session_id!=OLD.source_edge_session_id
  OR NEW.source_identity_generation!=OLD.source_identity_generation
  OR NEW.source_revocation_generation!=OLD.source_revocation_generation
  OR NEW.approved_at!=OLD.approved_at OR NEW.expires_at!=OLD.expires_at
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NOT((OLD.status='ready' AND NEW.status IN('expired','deleted'))
    OR(OLD.status='expired' AND NEW.status='deleted'))
BEGIN SELECT RAISE(ABORT,'edge_support_bundle_immutable'); END;
CREATE TRIGGER trg_edge_support_bundles_delete_prd125
BEFORE DELETE ON edge_support_bundles
BEGIN SELECT RAISE(ABORT,'edge_support_bundle_immutable'); END;

CREATE TABLE edge_support_bundle_download_audits(
  support_bundle_download_id TEXT PRIMARY KEY CHECK(length(support_bundle_download_id)=36
    AND substr(support_bundle_download_id,1,4)='esd_'
    AND substr(support_bundle_download_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  support_bundle_id TEXT NOT NULL,
  support_grant_id TEXT NOT NULL,
  support_session_id TEXT NOT NULL,
  operator_id TEXT NOT NULL,
  grant_operator_revision INTEGER NOT NULL CHECK(grant_operator_revision BETWEEN 1 AND 2147483647),
  grant_workspace_revision INTEGER NOT NULL CHECK(grant_workspace_revision BETWEEN 1 AND 2147483647),
  authorized_until TEXT NOT NULL CHECK(unixepoch(authorized_until) IS NOT NULL),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 160),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,support_bundle_id)
    REFERENCES edge_support_bundles(workspace_id,tenant_id,support_bundle_id),
  FOREIGN KEY(support_grant_id) REFERENCES support_grants(grant_id),
  FOREIGN KEY(support_session_id) REFERENCES support_sessions(support_session_id),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id)
);
CREATE INDEX idx_edge_support_download_owner_prd125
  ON edge_support_bundle_download_audits(workspace_id,tenant_id,support_bundle_id,
    created_at DESC);
CREATE TRIGGER trg_edge_support_downloads_update_prd125
BEFORE UPDATE ON edge_support_bundle_download_audits
BEGIN SELECT RAISE(ABORT,'edge_support_bundle_download_immutable'); END;
CREATE TRIGGER trg_edge_support_downloads_delete_prd125
BEFORE DELETE ON edge_support_bundle_download_audits
BEGIN SELECT RAISE(ABORT,'edge_support_bundle_download_immutable'); END;

CREATE TABLE edge_delivery_events(
  edge_delivery_event_id TEXT PRIMARY KEY CHECK(length(edge_delivery_event_id)=36
    AND substr(edge_delivery_event_id,1,4)='ede_'
    AND substr(edge_delivery_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_deployment_id TEXT NOT NULL,
  event_type TEXT NOT NULL CHECK(event_type IN(
    'edge.validation_completed','edge.support_bundle_ready','edge.delivery_accepted')),
  object_type TEXT NOT NULL CHECK(object_type IN(
    'deployment_validation','support_bundle','delivery_acceptance')),
  object_id TEXT NOT NULL CHECK(length(object_id)=36),
  outcome_code TEXT NOT NULL CHECK(outcome_code IN('passed','failed','ready','accepted')),
  summary_json TEXT NOT NULL CHECK(json_valid(summary_json) AND json_type(summary_json)='object'
    AND length(summary_json)<=2048 AND lower(summary_json) NOT LIKE '%content%'
    AND lower(summary_json) NOT LIKE '%secret%' AND lower(summary_json) NOT LIKE '%token%'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,edge_deployment_id)
    REFERENCES edge_deployments(workspace_id,tenant_id,edge_deployment_id)
);
CREATE TRIGGER trg_edge_delivery_events_update_prd125
BEFORE UPDATE ON edge_delivery_events
BEGIN SELECT RAISE(ABORT,'edge_delivery_event_immutable'); END;
CREATE TRIGGER trg_edge_delivery_events_delete_prd125
BEFORE DELETE ON edge_delivery_events
BEGIN SELECT RAISE(ABORT,'edge_delivery_event_immutable'); END;

CREATE TABLE edge_delivery_outbox(
  edge_delivery_outbox_id TEXT PRIMARY KEY CHECK(length(edge_delivery_outbox_id)=36
    AND substr(edge_delivery_outbox_id,1,4)='edo_'
    AND substr(edge_delivery_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  edge_delivery_event_id TEXT NOT NULL,
  delivery_domain TEXT NOT NULL CHECK(delivery_domain IN('workspace','fleet','security')),
  status TEXT NOT NULL CHECK(status IN('pending','delivering','delivered','dead_letter')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  available_at TEXT NOT NULL CHECK(unixepoch(available_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR length(last_error_code) BETWEEN 2 AND 80),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(edge_delivery_event_id) REFERENCES edge_delivery_events(edge_delivery_event_id),
  UNIQUE(edge_delivery_event_id,delivery_domain),
  CHECK((status='delivered' AND delivered_at IS NOT NULL AND last_error_code IS NULL)
    OR(status!='delivered' AND delivered_at IS NULL))
);
CREATE INDEX idx_edge_delivery_outbox_prd125
  ON edge_delivery_outbox(status,available_at,edge_delivery_outbox_id);
CREATE TRIGGER trg_edge_delivery_outbox_update_prd125
BEFORE UPDATE ON edge_delivery_outbox FOR EACH ROW
WHEN NEW.edge_delivery_outbox_id!=OLD.edge_delivery_outbox_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.edge_delivery_event_id!=OLD.edge_delivery_event_id
  OR NEW.delivery_domain!=OLD.delivery_domain OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count OR NEW.attempt_count>OLD.attempt_count+1
  OR OLD.status IN('delivered','dead_letter')
  OR (OLD.status='pending' AND NEW.status NOT IN('delivering','dead_letter'))
  OR (OLD.status='delivering' AND NEW.status NOT IN('pending','delivered','dead_letter'))
BEGIN SELECT RAISE(ABORT,'edge_delivery_outbox_transition_invalid'); END;
CREATE TRIGGER trg_edge_delivery_outbox_delete_prd125
BEFORE DELETE ON edge_delivery_outbox
BEGIN SELECT RAISE(ABORT,'edge_delivery_outbox_immutable'); END;

CREATE TABLE edge_validation_commands(
  edge_validation_command_id TEXT PRIMARY KEY CHECK(length(edge_validation_command_id)=36
    AND substr(edge_validation_command_id,1,4)='evc_'
    AND substr(edge_validation_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  actor_account_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'edge.validation.start','edge.validation.retry_failed','edge.delivery.accept')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64
    AND idempotency_sha256 NOT GLOB '*[^0-9a-f]*'),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64
    AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(workspace_id,tenant_id,actor_account_id,action_code,idempotency_sha256)
);
CREATE TRIGGER trg_edge_validation_commands_update_prd125
BEFORE UPDATE ON edge_validation_commands
BEGIN SELECT RAISE(ABORT,'edge_validation_command_immutable'); END;
CREATE TRIGGER trg_edge_validation_commands_delete_prd125
BEFORE DELETE ON edge_validation_commands
BEGIN SELECT RAISE(ABORT,'edge_validation_command_immutable'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd125(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-125'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd125_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd125_manifest_sources VALUES
  ('edge_validation_runs','archive_then_delete','audit'),
  ('edge_validation_checks','archive_then_delete','audit'),
  ('edge_delivery_acceptances','archive_then_delete','audit'),
  ('edge_support_bundles','delete','operational'),
  ('edge_support_bundle_download_audits','archive_then_delete','audit'),
  ('edge_delivery_events','archive_then_delete','audit'),
  ('edge_delivery_outbox','delete','operational'),
  ('edge_validation_commands','archive_then_delete','audit');
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
JOIN prd125_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd125(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-125',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd125_manifest_sources ORDER BY source_table;
DROP TABLE prd125_manifest_sources;

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
CREATE TRIGGER trg_tenant_deletion_manifest_prd125_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd125
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd125_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd125
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd125_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd125
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd125_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd125_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_edge_validation_permission_extensions)=4
      AND (SELECT COUNT(*) FROM workspace_edge_validation_role_permissions)=11
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd125)=8
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_edge_validation_permission_extensions',
      'workspace_edge_validation_role_permissions','edge_validation_runs',
      'edge_validation_checks','edge_delivery_acceptances','edge_support_bundles',
      'edge_support_bundle_download_audits','edge_delivery_events','edge_delivery_outbox',
      'edge_validation_commands','tenant_deletion_manifest_extensions_prd125'))=11
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd125_up_final_guard;
COMMIT;
