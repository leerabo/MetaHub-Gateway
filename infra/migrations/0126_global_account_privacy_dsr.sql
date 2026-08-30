-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd134_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd134_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0125' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'global_accounts','global_sessions','workspace_tenant_bindings','workspace_memberships',
      'workspace_sessions','consent_records','data_governance_fences','legal_holds',
      'retention_policies','tenant_deletion_table_manifest'))=10 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd134')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd134_up_guard;

CREATE TABLE workspace_privacy_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'privacy.preference.read','privacy.preference.manage','privacy.dsr.read','privacy.dsr.create',
    'privacy.dsr.cancel')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-134'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=50)
);
CREATE TABLE workspace_privacy_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'owner','admin','developer','security_admin','finance_admin','legal_admin','auditor','viewer')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code) REFERENCES workspace_privacy_permission_extensions(permission_code)
);
INSERT INTO workspace_privacy_permission_extensions VALUES
  ('privacy.preference.read','PRD-134',50),
  ('privacy.preference.manage','PRD-134',50),
  ('privacy.dsr.read','PRD-134',50),
  ('privacy.dsr.create','PRD-134',50),
  ('privacy.dsr.cancel','PRD-134',50);
INSERT INTO workspace_privacy_role_permissions
SELECT role_code,permission_code
FROM (SELECT 'owner' role_code UNION ALL SELECT 'admin' UNION ALL SELECT 'developer'
      UNION ALL SELECT 'security_admin' UNION ALL SELECT 'finance_admin'
      UNION ALL SELECT 'legal_admin' UNION ALL SELECT 'auditor' UNION ALL SELECT 'viewer') roles
CROSS JOIN workspace_privacy_permission_extensions;
CREATE TRIGGER trg_workspace_privacy_permissions_insert_prd134
BEFORE INSERT ON workspace_privacy_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_privacy_permission_immutable'); END;
CREATE TRIGGER trg_workspace_privacy_permissions_update_prd134
BEFORE UPDATE ON workspace_privacy_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_privacy_permission_immutable'); END;
CREATE TRIGGER trg_workspace_privacy_permissions_delete_prd134
BEFORE DELETE ON workspace_privacy_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_privacy_permission_immutable'); END;
CREATE TRIGGER trg_workspace_privacy_roles_insert_prd134
BEFORE INSERT ON workspace_privacy_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_privacy_permission_immutable'); END;
CREATE TRIGGER trg_workspace_privacy_roles_update_prd134
BEFORE UPDATE ON workspace_privacy_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_privacy_permission_immutable'); END;
CREATE TRIGGER trg_workspace_privacy_roles_delete_prd134
BEFORE DELETE ON workspace_privacy_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_privacy_permission_immutable'); END;

-- Preserve the PRD-092 registration write contract while generalizing ConsentRecord into
-- an append-only account fact for versioned purpose decisions.
DROP TRIGGER trg_consent_records_update_prd092;
DROP TRIGGER trg_consent_records_delete_prd092;
ALTER TABLE consent_records RENAME TO consent_records_prd092;
CREATE TABLE consent_records(
  consent_record_id TEXT PRIMARY KEY CHECK(length(consent_record_id)=36
    AND substr(consent_record_id,1,4)='con_'
    AND substr(consent_record_id,5) NOT GLOB '*[^0-9a-f]*'),
  account_id TEXT NOT NULL,
  consent_type TEXT NOT NULL CHECK(consent_type IN(
    'privacy_terms','analytics','product_updates','model_improvement')),
  consent_version TEXT NOT NULL CHECK(length(consent_version) BETWEEN 1 AND 80),
  source_registration_intent_id TEXT,
  locale TEXT NOT NULL DEFAULT 'und' CHECK(length(locale) BETWEEN 2 AND 20
    AND locale=lower(locale) AND locale NOT GLOB '*[^a-z0-9-]*'),
  source_kind TEXT NOT NULL DEFAULT 'registration' CHECK(source_kind IN(
    'registration','privacy_center')),
  decision TEXT NOT NULL DEFAULT 'granted' CHECK(decision IN(
    'granted','denied','withdrawn')),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64
    AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  consented_at TEXT NOT NULL CHECK(unixepoch(consented_at) IS NOT NULL),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(source_registration_intent_id)
    REFERENCES registration_intents(registration_intent_id),
  UNIQUE(account_id,consent_type,consent_version,source_registration_intent_id),
  CHECK((source_kind='registration' AND source_registration_intent_id IS NOT NULL
      AND consent_type='privacy_terms' AND decision='granted')
    OR(source_kind='privacy_center' AND source_registration_intent_id IS NULL))
);
INSERT INTO consent_records(
  consent_record_id,account_id,consent_type,consent_version,source_registration_intent_id,
  locale,source_kind,decision,payload_sha256,consented_at)
SELECT consent_record_id,account_id,consent_type,consent_version,source_registration_intent_id,
  'und','registration','granted',payload_sha256,consented_at
FROM consent_records_prd092;
DROP TABLE consent_records_prd092;
CREATE INDEX idx_consent_records_account_prd134
  ON consent_records(account_id,consented_at DESC,consent_record_id DESC);
CREATE TRIGGER trg_consent_records_update_prd134 BEFORE UPDATE ON consent_records
BEGIN SELECT RAISE(ABORT,'consent_record_immutable'); END;
CREATE TRIGGER trg_consent_records_delete_prd134 BEFORE DELETE ON consent_records
BEGIN SELECT RAISE(ABORT,'consent_record_immutable'); END;

CREATE TABLE privacy_preferences(
  privacy_preference_id TEXT PRIMARY KEY CHECK(length(privacy_preference_id)=36
    AND substr(privacy_preference_id,1,4)='prf_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  analytics_enabled INTEGER NOT NULL CHECK(analytics_enabled IN(0,1)),
  product_updates_enabled INTEGER NOT NULL CHECK(product_updates_enabled IN(0,1)),
  model_improvement_enabled INTEGER NOT NULL CHECK(model_improvement_enabled IN(0,1)),
  terms_version TEXT NOT NULL CHECK(length(terms_version) BETWEEN 1 AND 80),
  locale TEXT NOT NULL CHECK(length(locale) BETWEEN 2 AND 20
    AND locale=lower(locale) AND locale NOT GLOB '*[^a-z0-9-]*'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,account_id),
  UNIQUE(workspace_id,tenant_id,account_id,privacy_preference_id)
);
CREATE INDEX idx_privacy_preferences_owner_prd134
  ON privacy_preferences(account_id,workspace_id,updated_at DESC);
CREATE TRIGGER trg_privacy_preferences_update_prd134
BEFORE UPDATE ON privacy_preferences FOR EACH ROW
WHEN NEW.privacy_preference_id!=OLD.privacy_preference_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.account_id!=OLD.account_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'privacy_preference_transition_invalid'); END;
CREATE TRIGGER trg_privacy_preferences_delete_prd134 BEFORE DELETE ON privacy_preferences
BEGIN SELECT RAISE(ABORT,'privacy_preference_immutable'); END;

CREATE TABLE data_subject_requests(
  data_subject_request_id TEXT PRIMARY KEY CHECK(length(data_subject_request_id)=36
    AND substr(data_subject_request_id,1,4)='dsr_'),
  request_code TEXT NOT NULL UNIQUE CHECK(length(request_code)=20
    AND substr(request_code,1,4)='prq_' AND substr(request_code,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  requester_membership_id TEXT NOT NULL,
  request_kind TEXT NOT NULL CHECK(request_kind IN('export','rectify','erase')),
  status TEXT NOT NULL CHECK(status IN(
    'received','identity_verified','collecting','reviewing','ready','completed',
    'partially_denied','failed','cancelled')),
  verification_generation INTEGER NOT NULL CHECK(verification_generation>=1),
  auth_method TEXT NOT NULL CHECK(auth_method IN('totp','webauthn','recovery_code')),
  authorization_fingerprint_sha256 TEXT NOT NULL CHECK(
    length(authorization_fingerprint_sha256)=64
    AND authorization_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*'),
  revision INTEGER NOT NULL CHECK(revision>=1),
  deadline_at TEXT NOT NULL CHECK(unixepoch(deadline_at) IS NOT NULL),
  deadline_warning_at TEXT NOT NULL CHECK(unixepoch(deadline_warning_at) IS NOT NULL),
  warning_sent_at TEXT CHECK(warning_sent_at IS NULL OR unixepoch(warning_sent_at) IS NOT NULL),
  terminal_reason_code TEXT CHECK(terminal_reason_code IS NULL OR(
    length(terminal_reason_code) BETWEEN 3 AND 80
    AND terminal_reason_code=lower(terminal_reason_code)
    AND terminal_reason_code NOT GLOB '*[^a-z0-9._-]*')),
  terminal_at TEXT CHECK(terminal_at IS NULL OR unixepoch(terminal_at) IS NOT NULL),
  received_at TEXT NOT NULL CHECK(unixepoch(received_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(requester_membership_id)
    REFERENCES workspace_memberships(workspace_membership_id),
  UNIQUE(workspace_id,tenant_id,account_id,data_subject_request_id),
  CHECK(unixepoch(deadline_warning_at)<unixepoch(deadline_at)),
  CHECK((status IN('completed','partially_denied','failed','cancelled')
      AND terminal_at IS NOT NULL AND terminal_reason_code IS NOT NULL)
    OR(status NOT IN('completed','partially_denied','failed','cancelled')
      AND terminal_at IS NULL AND terminal_reason_code IS NULL))
);
CREATE INDEX idx_data_subject_requests_owner_prd134
  ON data_subject_requests(account_id,workspace_id,received_at DESC,data_subject_request_id DESC);
CREATE INDEX idx_data_subject_requests_queue_prd134
  ON data_subject_requests(status,deadline_at,received_at,data_subject_request_id);
CREATE TRIGGER trg_data_subject_requests_update_prd134
BEFORE UPDATE ON data_subject_requests FOR EACH ROW
WHEN NEW.data_subject_request_id!=OLD.data_subject_request_id
  OR NEW.request_code!=OLD.request_code OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.account_id!=OLD.account_id
  OR NEW.requester_membership_id!=OLD.requester_membership_id
  OR NEW.request_kind!=OLD.request_kind
  OR NEW.verification_generation!=OLD.verification_generation
  OR NEW.auth_method!=OLD.auth_method
  OR NEW.authorization_fingerprint_sha256!=OLD.authorization_fingerprint_sha256
  OR NEW.deadline_at!=OLD.deadline_at OR NEW.deadline_warning_at!=OLD.deadline_warning_at
  OR NEW.received_at!=OLD.received_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT(NEW.status=OLD.status
    OR(OLD.status='received' AND NEW.status IN('identity_verified','cancelled','failed'))
    OR(OLD.status='identity_verified' AND NEW.status IN('collecting','cancelled','failed'))
    OR(OLD.status='collecting' AND NEW.status IN('reviewing','ready','completed',
        'partially_denied','cancelled','failed'))
    OR(OLD.status='reviewing' AND NEW.status IN('ready','completed','partially_denied','failed'))
    OR(OLD.status='ready' AND NEW.status IN('completed','failed')))
BEGIN SELECT RAISE(ABORT,'privacy_request_transition_invalid'); END;
CREATE TRIGGER trg_data_subject_requests_delete_prd134 BEFORE DELETE ON data_subject_requests
BEGIN SELECT RAISE(ABORT,'privacy_request_immutable'); END;

CREATE TABLE privacy_owner_adapter_versions(
  owner_domain TEXT NOT NULL CHECK(owner_domain IN(
    'identity','membership','audit','billing','support','edge','data_governance')),
  adapter_version INTEGER NOT NULL CHECK(adapter_version>=1),
  export_schema_version INTEGER NOT NULL CHECK(export_schema_version>=1),
  field_manifest_json TEXT NOT NULL CHECK(json_valid(field_manifest_json)
    AND json_type(field_manifest_json)='array' AND json_array_length(field_manifest_json)>=1
    AND length(field_manifest_json)<=4096),
  field_manifest_sha256 TEXT NOT NULL CHECK(length(field_manifest_sha256)=64
    AND field_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  erasure_mode TEXT NOT NULL CHECK(erasure_mode IN(
    'erase','anonymize','retain_by_policy','not_found')),
  status TEXT NOT NULL CHECK(status IN('active','retired')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(owner_domain,adapter_version)
);
INSERT INTO privacy_owner_adapter_versions VALUES
  ('identity',1,1,'["account_id","created_at","display_name","email","status"]',
    metahub_sha256('["account_id","created_at","display_name","email","status"]'),
    'anonymize','active',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('membership',1,1,'["created_at","role","status","workspace_id"]',
    metahub_sha256('["created_at","role","status","workspace_id"]'),
    'erase','active',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('audit',1,1,'["event_count","latest_event_at"]',
    metahub_sha256('["event_count","latest_event_at"]'),
    'retain_by_policy','active',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('billing',1,1,'["billing_profile_configured","subscription_status"]',
    metahub_sha256('["billing_profile_configured","subscription_status"]'),
    'retain_by_policy','active',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('support',1,1,'["open_ticket_count","ticket_count"]',
    metahub_sha256('["open_ticket_count","ticket_count"]'),
    'anonymize','active',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('edge',1,1,'["deployment_count"]',metahub_sha256('["deployment_count"]'),
    'not_found','active',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('data_governance',1,1,'["subject_data_collected"]',
    metahub_sha256('["subject_data_collected"]'),
    'retain_by_policy','active',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE UNIQUE INDEX uq_privacy_owner_adapter_active_prd134
  ON privacy_owner_adapter_versions(owner_domain) WHERE status='active';
CREATE TRIGGER trg_privacy_owner_adapters_update_prd134
BEFORE UPDATE ON privacy_owner_adapter_versions
BEGIN SELECT RAISE(ABORT,'privacy_owner_adapter_immutable'); END;
CREATE TRIGGER trg_privacy_owner_adapters_delete_prd134
BEFORE DELETE ON privacy_owner_adapter_versions
BEGIN SELECT RAISE(ABORT,'privacy_owner_adapter_immutable'); END;

CREATE TABLE data_subject_request_owner_jobs(
  owner_job_id TEXT PRIMARY KEY CHECK(length(owner_job_id)=36
    AND substr(owner_job_id,1,4)='poj_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  data_subject_request_id TEXT NOT NULL,
  owner_domain TEXT NOT NULL,
  adapter_version INTEGER NOT NULL CHECK(adapter_version>=1),
  status TEXT NOT NULL CHECK(status IN('pending','running','completed','failed','cancelled')),
  outcome_code TEXT CHECK(outcome_code IS NULL OR outcome_code IN(
    'exported','rectified','erased','anonymized','retained','not_found')),
  checkpoint_code TEXT NOT NULL CHECK(length(checkpoint_code) BETWEEN 3 AND 80
    AND checkpoint_code=lower(checkpoint_code)
    AND checkpoint_code NOT GLOB '*[^a-z0-9._-]*'),
  export_fragment_json TEXT CHECK(export_fragment_json IS NULL OR(
    json_valid(export_fragment_json) AND json_type(export_fragment_json)='object'
    AND length(CAST(export_fragment_json AS BLOB))<=65536)),
  executor_receipt_sha256 TEXT CHECK(executor_receipt_sha256 IS NULL OR(
    length(executor_receipt_sha256)=64
    AND executor_receipt_sha256 NOT GLOB '*[^0-9a-f]*')),
  policy_generation INTEGER CHECK(policy_generation IS NULL OR policy_generation>=1),
  hold_generation INTEGER CHECK(hold_generation IS NULL OR hold_generation>=0),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 8),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT CHECK(lease_token_sha256 IS NULL OR length(lease_token_sha256)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,account_id,data_subject_request_id)
    REFERENCES data_subject_requests(workspace_id,tenant_id,account_id,data_subject_request_id),
  FOREIGN KEY(owner_domain,adapter_version)
    REFERENCES privacy_owner_adapter_versions(owner_domain,adapter_version),
  UNIQUE(data_subject_request_id,owner_domain),
  UNIQUE(workspace_id,tenant_id,account_id,owner_job_id),
  CHECK((status IN('pending','running') AND outcome_code IS NULL AND completed_at IS NULL)
    OR(status='completed' AND outcome_code IS NOT NULL AND completed_at IS NOT NULL)
    OR(status IN('failed','cancelled') AND completed_at IS NOT NULL)),
  CHECK((lease_owner_id IS NULL AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND lease_token_sha256 IS NOT NULL
      AND lease_expires_at IS NOT NULL))
);
CREATE INDEX idx_privacy_owner_jobs_due_prd134
  ON data_subject_request_owner_jobs(status,next_attempt_at,owner_job_id);
CREATE TRIGGER trg_privacy_owner_jobs_update_prd134
BEFORE UPDATE ON data_subject_request_owner_jobs FOR EACH ROW
WHEN NEW.owner_job_id!=OLD.owner_job_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.account_id!=OLD.account_id
  OR NEW.data_subject_request_id!=OLD.data_subject_request_id
  OR NEW.owner_domain!=OLD.owner_domain OR NEW.adapter_version!=OLD.adapter_version
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.attempt_count<OLD.attempt_count OR NEW.updated_at<=OLD.updated_at
  OR NOT(NEW.status=OLD.status
    OR(OLD.status='pending' AND NEW.status IN('running','cancelled','failed'))
    OR(OLD.status='running' AND NEW.status IN('pending','completed','failed','cancelled')))
BEGIN SELECT RAISE(ABORT,'privacy_owner_job_transition_invalid'); END;
CREATE TRIGGER trg_privacy_owner_jobs_delete_prd134
BEFORE DELETE ON data_subject_request_owner_jobs
BEGIN SELECT RAISE(ABORT,'privacy_owner_job_immutable'); END;

CREATE TABLE identity_exports(
  identity_export_id TEXT PRIMARY KEY CHECK(length(identity_export_id)=36
    AND substr(identity_export_id,1,4)='pex_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  data_subject_request_id TEXT NOT NULL UNIQUE,
  schema_version INTEGER NOT NULL CHECK(schema_version=1),
  owner_manifest_json TEXT NOT NULL CHECK(json_valid(owner_manifest_json)
    AND json_type(owner_manifest_json)='array' AND json_array_length(owner_manifest_json)>=1
    AND length(owner_manifest_json)<=8192),
  owner_manifest_sha256 TEXT NOT NULL CHECK(length(owner_manifest_sha256)=64),
  artifact_ciphertext_b64 TEXT NOT NULL CHECK(length(artifact_ciphertext_b64) BETWEEN 64 AND 1048576),
  artifact_nonce_b64 TEXT NOT NULL CHECK(length(artifact_nonce_b64)=16),
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  plaintext_sha256 TEXT NOT NULL CHECK(length(plaintext_sha256)=64),
  size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 2 AND 524288),
  key_version INTEGER NOT NULL CHECK(key_version>=1),
  status TEXT NOT NULL CHECK(status IN('ready','downloaded','expired')),
  receipt_status TEXT NOT NULL CHECK(receipt_status IN('none','active','consumed','expired')),
  receipt_hmac TEXT CHECK(receipt_hmac IS NULL OR(length(receipt_hmac)=64
    AND receipt_hmac NOT GLOB '*[^0-9a-f]*')),
  receipt_expires_at TEXT CHECK(receipt_expires_at IS NULL OR unixepoch(receipt_expires_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  ready_at TEXT NOT NULL CHECK(unixepoch(ready_at) IS NOT NULL),
  downloaded_at TEXT CHECK(downloaded_at IS NULL OR unixepoch(downloaded_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,account_id,data_subject_request_id)
    REFERENCES data_subject_requests(workspace_id,tenant_id,account_id,data_subject_request_id),
  UNIQUE(workspace_id,tenant_id,account_id,identity_export_id),
  CHECK((receipt_status='none' AND receipt_hmac IS NULL AND receipt_expires_at IS NULL)
    OR(receipt_status IN('active','consumed','expired')
      AND receipt_hmac IS NOT NULL AND receipt_expires_at IS NOT NULL)),
  CHECK((status='downloaded' AND receipt_status='consumed' AND downloaded_at IS NOT NULL)
    OR(status!='downloaded' AND downloaded_at IS NULL))
);
CREATE INDEX idx_identity_exports_expiry_prd134
  ON identity_exports(status,expires_at,identity_export_id);
CREATE TRIGGER trg_identity_exports_update_prd134
BEFORE UPDATE ON identity_exports FOR EACH ROW
WHEN NEW.identity_export_id!=OLD.identity_export_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.account_id!=OLD.account_id
  OR NEW.data_subject_request_id!=OLD.data_subject_request_id
  OR NEW.schema_version!=OLD.schema_version OR NEW.owner_manifest_json!=OLD.owner_manifest_json
  OR NEW.owner_manifest_sha256!=OLD.owner_manifest_sha256
  OR NEW.artifact_ciphertext_b64!=OLD.artifact_ciphertext_b64
  OR NEW.artifact_nonce_b64!=OLD.artifact_nonce_b64 OR NEW.artifact_sha256!=OLD.artifact_sha256
  OR NEW.plaintext_sha256!=OLD.plaintext_sha256 OR NEW.size_bytes!=OLD.size_bytes
  OR NEW.key_version!=OLD.key_version OR NEW.ready_at!=OLD.ready_at
  OR NEW.expires_at!=OLD.expires_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='ready' AND NEW.status IN('ready','downloaded','expired'))
    OR(OLD.status=NEW.status AND OLD.status IN('downloaded','expired')))
BEGIN SELECT RAISE(ABORT,'identity_export_transition_invalid'); END;
CREATE TRIGGER trg_identity_exports_delete_prd134 BEFORE DELETE ON identity_exports
BEGIN SELECT RAISE(ABORT,'identity_export_immutable'); END;

CREATE TABLE erasure_decisions(
  erasure_decision_id TEXT PRIMARY KEY CHECK(length(erasure_decision_id)=36
    AND substr(erasure_decision_id,1,4)='erd_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  data_subject_request_id TEXT NOT NULL,
  owner_domain TEXT NOT NULL,
  adapter_version INTEGER NOT NULL CHECK(adapter_version=1),
  outcome TEXT NOT NULL CHECK(outcome IN('erase','anonymize','retain','not_found')),
  legal_code TEXT NOT NULL CHECK(legal_code IN(
    'subject_request','legal_hold','statutory_retention','owner_not_found')),
  policy_generation INTEGER NOT NULL CHECK(policy_generation>=1),
  hold_generation INTEGER NOT NULL CHECK(hold_generation>=0),
  executor_receipt_sha256 TEXT NOT NULL CHECK(length(executor_receipt_sha256)=64),
  decided_at TEXT NOT NULL CHECK(unixepoch(decided_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,account_id,data_subject_request_id)
    REFERENCES data_subject_requests(workspace_id,tenant_id,account_id,data_subject_request_id),
  FOREIGN KEY(owner_domain,adapter_version)
    REFERENCES privacy_owner_adapter_versions(owner_domain,adapter_version),
  UNIQUE(data_subject_request_id,owner_domain),
  UNIQUE(workspace_id,tenant_id,account_id,erasure_decision_id)
);
CREATE TRIGGER trg_erasure_decisions_update_prd134 BEFORE UPDATE ON erasure_decisions
BEGIN SELECT RAISE(ABORT,'erasure_decision_immutable'); END;
CREATE TRIGGER trg_erasure_decisions_delete_prd134 BEFORE DELETE ON erasure_decisions
BEGIN SELECT RAISE(ABORT,'erasure_decision_immutable'); END;

CREATE TABLE retention_exceptions(
  retention_exception_id TEXT PRIMARY KEY CHECK(length(retention_exception_id)=36
    AND substr(retention_exception_id,1,4)='rex_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  data_subject_request_id TEXT NOT NULL,
  owner_domain TEXT NOT NULL,
  classification_code TEXT NOT NULL CHECK(classification_code IN(
    'identity','audit','billing','file','prompt','response')),
  legal_code TEXT NOT NULL CHECK(legal_code IN('legal_hold','statutory_retention')),
  policy_generation INTEGER NOT NULL CHECK(policy_generation>=1),
  hold_generation INTEGER NOT NULL CHECK(hold_generation>=0),
  explanation_code TEXT NOT NULL CHECK(explanation_code IN(
    'active_legal_hold','retention_period_active')),
  expires_at TEXT CHECK(expires_at IS NULL OR unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,account_id,data_subject_request_id)
    REFERENCES data_subject_requests(workspace_id,tenant_id,account_id,data_subject_request_id),
  UNIQUE(data_subject_request_id,owner_domain,legal_code),
  UNIQUE(workspace_id,tenant_id,account_id,retention_exception_id)
);
CREATE TRIGGER trg_retention_exceptions_update_prd134 BEFORE UPDATE ON retention_exceptions
BEGIN SELECT RAISE(ABORT,'retention_exception_immutable'); END;
CREATE TRIGGER trg_retention_exceptions_delete_prd134 BEFORE DELETE ON retention_exceptions
BEGIN SELECT RAISE(ABORT,'retention_exception_immutable'); END;

CREATE TABLE privacy_events(
  privacy_event_id TEXT PRIMARY KEY CHECK(length(privacy_event_id)=36
    AND substr(privacy_event_id,1,4)='pev_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'privacy.consent_changed','privacy.request_received','privacy.request_completed',
    'privacy.deadline_warning')),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  object_type TEXT NOT NULL CHECK(object_type IN(
    'consent_record','data_subject_request','identity_export')),
  object_id TEXT NOT NULL CHECK(length(object_id)=36),
  request_code TEXT NOT NULL CHECK(length(request_code) BETWEEN 3 AND 20),
  status_code TEXT NOT NULL CHECK(length(status_code) BETWEEN 3 AND 32
    AND status_code=lower(status_code) AND status_code NOT GLOB '*[^a-z0-9._-]*'),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND json_type(payload_json)='object'
    AND length(CAST(payload_json AS BLOB))<=2048),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,privacy_event_id)
);
CREATE INDEX idx_privacy_events_owner_prd134
  ON privacy_events(account_id,workspace_id,occurred_at DESC,privacy_event_id DESC);
CREATE TRIGGER trg_privacy_events_update_prd134 BEFORE UPDATE ON privacy_events
BEGIN SELECT RAISE(ABORT,'privacy_event_immutable'); END;
CREATE TRIGGER trg_privacy_events_delete_prd134 BEFORE DELETE ON privacy_events
BEGIN SELECT RAISE(ABORT,'privacy_event_immutable'); END;

CREATE TABLE privacy_event_outbox(
  privacy_event_outbox_id TEXT PRIMARY KEY CHECK(length(privacy_event_outbox_id)=36
    AND substr(privacy_event_outbox_id,1,4)='pob_'),
  privacy_event_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','dead')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,privacy_event_id)
    REFERENCES privacy_events(workspace_id,tenant_id,privacy_event_id),
  CHECK((status='delivered' AND delivered_at IS NOT NULL)
    OR(status!='delivered' AND delivered_at IS NULL))
);
CREATE INDEX idx_privacy_outbox_due_prd134
  ON privacy_event_outbox(status,next_attempt_at,privacy_event_outbox_id);
CREATE TRIGGER trg_privacy_outbox_update_prd134
BEFORE UPDATE ON privacy_event_outbox FOR EACH ROW
WHEN NEW.privacy_event_outbox_id!=OLD.privacy_event_outbox_id
  OR NEW.privacy_event_id!=OLD.privacy_event_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'privacy_outbox_transition_invalid'); END;
CREATE TRIGGER trg_privacy_outbox_delete_prd134 BEFORE DELETE ON privacy_event_outbox
BEGIN SELECT RAISE(ABORT,'privacy_outbox_immutable'); END;

CREATE TABLE privacy_action_commands(
  privacy_action_command_id TEXT PRIMARY KEY CHECK(length(privacy_action_command_id)=36
    AND substr(privacy_action_command_id,1,4)='pcm_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  account_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'preference.update','request.create','request.cancel','export.receipt.issue')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 8 AND 96),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 8 AND 96),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,account_id,action_code,idempotency_sha256),
  UNIQUE(workspace_id,tenant_id,account_id,privacy_action_command_id)
);
CREATE TRIGGER trg_privacy_commands_update_prd134 BEFORE UPDATE ON privacy_action_commands
BEGIN SELECT RAISE(ABORT,'privacy_command_immutable'); END;
CREATE TRIGGER trg_privacy_commands_delete_prd134 BEFORE DELETE ON privacy_action_commands
BEGIN SELECT RAISE(ABORT,'privacy_command_immutable'); END;

CREATE TABLE privacy_scheduler_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  last_warning_scan_at TEXT NOT NULL CHECK(unixepoch(last_warning_scan_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT CHECK(lease_token_sha256 IS NULL OR length(lease_token_sha256)=64),
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((lease_owner_id IS NULL AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND lease_token_sha256 IS NOT NULL
      AND lease_expires_at IS NOT NULL))
);
INSERT INTO privacy_scheduler_state VALUES(
  1,'1970-01-01T00:00:00.000Z',NULL,NULL,NULL,0,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_privacy_scheduler_update_prd134
BEFORE UPDATE ON privacy_scheduler_state FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.fencing_generation<OLD.fencing_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'privacy_scheduler_fencing_invalid'); END;
CREATE TRIGGER trg_privacy_scheduler_delete_prd134 BEFORE DELETE ON privacy_scheduler_state
BEGIN SELECT RAISE(ABORT,'privacy_scheduler_state_required'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd134(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-134'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd134_manifest_sources(
  source_table TEXT PRIMARY KEY,
  disposition TEXT NOT NULL,
  retention_class TEXT
);
INSERT INTO prd134_manifest_sources VALUES
  ('privacy_preferences','delete','operational'),
  ('data_subject_requests','archive_then_delete','audit'),
  ('data_subject_request_owner_jobs','archive_then_delete','audit'),
  ('identity_exports','delete','operational'),
  ('erasure_decisions','archive_then_delete','audit'),
  ('retention_exceptions','archive_then_delete','audit'),
  ('privacy_events','archive_then_delete','audit'),
  ('privacy_event_outbox','delete','operational'),
  ('privacy_action_commands','archive_then_delete','audit');
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
JOIN prd134_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd134(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-134',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd134_manifest_sources ORDER BY source_table;
DROP TABLE prd134_manifest_sources;
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
CREATE TRIGGER trg_tenant_deletion_manifest_prd134_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd134
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd134_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd134
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd134_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd134
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd134_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd134_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_privacy_permission_extensions)=5
      AND (SELECT COUNT(*) FROM workspace_privacy_role_permissions)=40
      AND (SELECT COUNT(*) FROM privacy_owner_adapter_versions)=7
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd134)=9
      AND (SELECT COUNT(*) FROM privacy_scheduler_state)=1
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd134_up_final_guard;
PRAGMA legacy_alter_table = OFF;
COMMIT;
PRAGMA foreign_keys = ON;
