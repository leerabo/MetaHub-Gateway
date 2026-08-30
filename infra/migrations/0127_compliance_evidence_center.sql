-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
PRAGMA legacy_alter_table = ON;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd135_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd135_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0126' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'workspace_tenant_bindings','global_accounts','workspace_memberships','platform_roles',
      'machine_permission_catalog','service_accounts','service_account_scopes',
      'machine_identity_audits','tenant_deletion_table_manifest',
      'tenant_deletion_trigger_manifest'))=10 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd135')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd135_up_guard;

CREATE TABLE workspace_compliance_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'compliance.report.read','compliance.collect')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-135'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=47)
);
CREATE TABLE workspace_compliance_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('security_admin','auditor')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_compliance_permission_extensions(permission_code)
);
INSERT INTO workspace_compliance_permission_extensions VALUES
  ('compliance.report.read','PRD-135',47),
  ('compliance.collect','PRD-135',47);
INSERT INTO workspace_compliance_role_permissions VALUES
  ('security_admin','compliance.report.read'),
  ('security_admin','compliance.collect'),
  ('auditor','compliance.report.read');
CREATE TRIGGER trg_workspace_compliance_permissions_insert_prd135
BEFORE INSERT ON workspace_compliance_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_compliance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_compliance_permissions_update_prd135
BEFORE UPDATE ON workspace_compliance_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_compliance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_compliance_permissions_delete_prd135
BEFORE DELETE ON workspace_compliance_permission_extensions
BEGIN SELECT RAISE(ABORT,'workspace_compliance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_compliance_roles_insert_prd135
BEFORE INSERT ON workspace_compliance_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_compliance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_compliance_roles_update_prd135
BEFORE UPDATE ON workspace_compliance_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_compliance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_compliance_roles_delete_prd135
BEFORE DELETE ON workspace_compliance_role_permissions
BEGIN SELECT RAISE(ABORT,'workspace_compliance_permission_immutable'); END;

CREATE TABLE platform_compliance_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.compliance.manage','platform.compliance.exception')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-135'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=47)
);
CREATE TABLE platform_compliance_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('platform_owner','platform_compliance')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_compliance_permission_extensions(permission_code)
);
INSERT INTO platform_compliance_permission_extensions VALUES
  ('platform.compliance.manage','PRD-135',47),
  ('platform.compliance.exception','PRD-135',47);
INSERT INTO platform_compliance_role_permissions VALUES
  ('platform_owner','platform.compliance.manage'),
  ('platform_owner','platform.compliance.exception'),
  ('platform_compliance','platform.compliance.manage'),
  ('platform_compliance','platform.compliance.exception');
CREATE TRIGGER trg_platform_compliance_permissions_insert_prd135
BEFORE INSERT ON platform_compliance_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_compliance_permission_immutable'); END;
CREATE TRIGGER trg_platform_compliance_permissions_update_prd135
BEFORE UPDATE ON platform_compliance_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_compliance_permission_immutable'); END;
CREATE TRIGGER trg_platform_compliance_permissions_delete_prd135
BEFORE DELETE ON platform_compliance_permission_extensions
BEGIN SELECT RAISE(ABORT,'platform_compliance_permission_immutable'); END;
CREATE TRIGGER trg_platform_compliance_roles_insert_prd135
BEFORE INSERT ON platform_compliance_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_compliance_permission_immutable'); END;
CREATE TRIGGER trg_platform_compliance_roles_update_prd135
BEFORE UPDATE ON platform_compliance_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_compliance_permission_immutable'); END;
CREATE TRIGGER trg_platform_compliance_roles_delete_prd135
BEFORE DELETE ON platform_compliance_role_permissions
BEGIN SELECT RAISE(ABORT,'platform_compliance_permission_immutable'); END;

-- Extend the PRD-118 machine catalog without weakening its immutable credential domain.
DROP TRIGGER trg_machine_permission_catalog_insert_prd118;
DROP TRIGGER trg_machine_permission_catalog_update_prd118;
DROP TRIGGER trg_machine_permission_catalog_delete_prd118;
ALTER TABLE machine_permission_catalog RENAME TO machine_permission_catalog_prd135;
CREATE TABLE machine_permission_catalog(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'provider.read','request_log.read','compliance.collect')),
  scope_template TEXT NOT NULL CHECK(scope_template IN(
    'provider_reader','observability_reader','compliance_collector')),
  http_method TEXT NOT NULL CHECK(http_method IN('GET','POST')),
  route_template TEXT NOT NULL CHECK(route_template IN(
    '/providers','/request-logs','/platform/compliance/evidence')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1),
  status TEXT NOT NULL CHECK(status='active'),
  UNIQUE(http_method,route_template),
  UNIQUE(scope_template,permission_code)
);
INSERT INTO machine_permission_catalog
SELECT * FROM machine_permission_catalog_prd135;
INSERT INTO machine_permission_catalog VALUES
  ('compliance.collect','compliance_collector','POST',
   '/platform/compliance/evidence',1,'active');
DROP TABLE machine_permission_catalog_prd135;
CREATE TRIGGER trg_machine_permission_catalog_insert_prd118
BEFORE INSERT ON machine_permission_catalog
BEGIN SELECT RAISE(ABORT,'machine_permission_catalog_immutable'); END;
CREATE TRIGGER trg_machine_permission_catalog_update_prd118
BEFORE UPDATE ON machine_permission_catalog
BEGIN SELECT RAISE(ABORT,'machine_permission_catalog_immutable'); END;
CREATE TRIGGER trg_machine_permission_catalog_delete_prd118
BEFORE DELETE ON machine_permission_catalog
BEGIN SELECT RAISE(ABORT,'machine_permission_catalog_immutable'); END;

DROP TRIGGER trg_service_accounts_update_prd118;
DROP TRIGGER trg_service_accounts_delete_prd118;
DROP INDEX idx_service_accounts_owner_prd118;
ALTER TABLE service_accounts RENAME TO service_accounts_prd135;
CREATE TABLE service_accounts(
  service_account_id TEXT PRIMARY KEY CHECK(length(service_account_id)=36
    AND substr(service_account_id,1,4)='svc_'
    AND substr(service_account_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  name TEXT NOT NULL CHECK(length(name) BETWEEN 2 AND 80
    AND name=lower(name) AND name NOT GLOB '*[^a-z0-9_-]*'),
  display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 2 AND 120),
  status TEXT NOT NULL CHECK(status IN('active','disabled','revoked')),
  scope_template TEXT NOT NULL CHECK(scope_template IN(
    'provider_reader','observability_reader','compliance_collector')),
  catalog_revision INTEGER NOT NULL CHECK(catalog_revision=1),
  constraint_sha256 TEXT NOT NULL CHECK(length(constraint_sha256)=64
    AND constraint_sha256 NOT GLOB '*[^0-9a-f]*'),
  constraint_generation INTEGER NOT NULL CHECK(constraint_generation>=1),
  ip_allowlist_json TEXT NOT NULL CHECK(json_valid(ip_allowlist_json)
    AND json_type(ip_allowlist_json)='array' AND length(ip_allowlist_json)<=2048),
  mtls_fingerprint_sha256 TEXT CHECK(mtls_fingerprint_sha256 IS NULL
    OR(length(mtls_fingerprint_sha256)=64
      AND mtls_fingerprint_sha256 NOT GLOB '*[^0-9a-f]*')),
  token_ttl_seconds INTEGER NOT NULL CHECK(token_ttl_seconds BETWEEN 60 AND 3600),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_account_id TEXT NOT NULL,
  updated_by_account_id TEXT NOT NULL,
  last_used_at TEXT CHECK(last_used_at IS NULL OR unixepoch(last_used_at) IS NOT NULL),
  revoked_at TEXT CHECK(revoked_at IS NULL OR unixepoch(revoked_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,name),
  UNIQUE(workspace_id,tenant_id,service_account_id),
  CHECK((status='revoked' AND revoked_at IS NOT NULL)
    OR(status!='revoked' AND revoked_at IS NULL))
);
INSERT INTO service_accounts SELECT * FROM service_accounts_prd135;
DROP TABLE service_accounts_prd135;
CREATE INDEX idx_service_accounts_owner_prd118
  ON service_accounts(workspace_id,tenant_id,status,created_at,service_account_id);
CREATE TRIGGER trg_service_accounts_update_prd118
BEFORE UPDATE ON service_accounts FOR EACH ROW
WHEN NEW.service_account_id!=OLD.service_account_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.name!=OLD.name OR NEW.display_name!=OLD.display_name
  OR NEW.scope_template!=OLD.scope_template OR NEW.catalog_revision!=OLD.catalog_revision
  OR NEW.constraint_sha256!=OLD.constraint_sha256
  OR NEW.ip_allowlist_json!=OLD.ip_allowlist_json
  OR NEW.mtls_fingerprint_sha256 IS NOT OLD.mtls_fingerprint_sha256
  OR NEW.token_ttl_seconds!=OLD.token_ttl_seconds
  OR NEW.created_by_account_id!=OLD.created_by_account_id OR NEW.created_at!=OLD.created_at
  OR NEW.revision NOT IN(OLD.revision,OLD.revision+1)
  OR NEW.constraint_generation NOT IN(OLD.constraint_generation,OLD.constraint_generation+1)
  OR (NEW.revision=OLD.revision AND NEW.constraint_generation!=OLD.constraint_generation)
  OR (NEW.revision=OLD.revision+1 AND NEW.constraint_generation!=OLD.constraint_generation+1)
  OR (NEW.revision=OLD.revision AND (NEW.status!=OLD.status
      OR NEW.updated_by_account_id!=OLD.updated_by_account_id
      OR NEW.revoked_at IS NOT OLD.revoked_at))
  OR (NEW.revision=OLD.revision+1 AND NOT(
      OLD.status IN('active','disabled') AND NEW.status='revoked'
      AND OLD.revoked_at IS NULL AND NEW.revoked_at IS NOT NULL))
  OR (NEW.last_used_at IS NOT NULL AND OLD.last_used_at IS NOT NULL
      AND NEW.last_used_at<OLD.last_used_at)
  OR NEW.updated_at<OLD.updated_at
BEGIN SELECT RAISE(ABORT,'service_account_transition_invalid'); END;
CREATE TRIGGER trg_service_accounts_delete_prd118
BEFORE DELETE ON service_accounts
BEGIN SELECT RAISE(ABORT,'service_account_immutable'); END;

-- The exact machine route is part of the immutable PRD-118 audit contract too.
DROP TRIGGER trg_machine_identity_audits_insert_prd118;
DROP TRIGGER trg_machine_identity_audits_update_prd118;
DROP TRIGGER trg_machine_identity_audits_delete_prd118;
DROP INDEX idx_machine_identity_audits_owner_prd118;
CREATE TABLE machine_identity_audits_prd135_new(
  machine_identity_audit_id TEXT PRIMARY KEY CHECK(length(machine_identity_audit_id)=36
    AND substr(machine_identity_audit_id,1,4)='mia_'
    AND substr(machine_identity_audit_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK(subject_type IN('human','service_account')),
  subject_id TEXT NOT NULL CHECK(length(subject_id) BETWEEN 4 AND 80),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'service_account.created','oauth_client.created','oauth.token_issued',
    'service_account.credential_rotated','service_account.revoked',
    'machine.provider_listed','machine.request_log_listed',
    'machine.compliance_evidence_submitted')),
  resource_type TEXT NOT NULL CHECK(resource_type IN(
    'service_account','oauth_client','access_token_grant','provider_collection',
    'request_log_collection','compliance_evidence_collection')),
  resource_id TEXT NOT NULL CHECK(length(resource_id) BETWEEN 1 AND 80),
  outcome TEXT NOT NULL CHECK(outcome IN('success','denied')),
  permission_code TEXT CHECK(permission_code IS NULL OR permission_code IN(
    'machine_identity.manage','provider.read','request_log.read','compliance.collect')),
  resource_revision INTEGER NOT NULL CHECK(resource_revision>=0),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128
    AND request_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128
    AND trace_id NOT GLOB '*[^A-Za-z0-9._:-]*'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
INSERT INTO machine_identity_audits_prd135_new SELECT * FROM machine_identity_audits;
CREATE TEMP TABLE prd135_machine_audit_copy_guard(
  issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd135_machine_audit_copy_guard(issue_count)
SELECT abs((SELECT COUNT(*) FROM machine_identity_audits)
  -(SELECT COUNT(*) FROM machine_identity_audits_prd135_new));
DROP TABLE prd135_machine_audit_copy_guard;
DROP TABLE machine_identity_audits;
ALTER TABLE machine_identity_audits_prd135_new RENAME TO machine_identity_audits;
CREATE INDEX idx_machine_identity_audits_owner_prd118
  ON machine_identity_audits(workspace_id,tenant_id,created_at,machine_identity_audit_id);
CREATE TRIGGER trg_machine_identity_audits_insert_prd118
BEFORE INSERT ON machine_identity_audits FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM workspace_tenant_bindings binding
      WHERE binding.workspace_id=NEW.workspace_id AND binding.tenant_id=NEW.tenant_id)
  OR (NEW.subject_type='human' AND NOT EXISTS(
      SELECT 1 FROM workspace_memberships member
      WHERE member.workspace_id=NEW.workspace_id AND member.tenant_id=NEW.tenant_id
        AND member.account_id=NEW.subject_id AND member.status='active'
        AND member.role_code!='service_account'))
  OR (NEW.subject_type='service_account' AND NOT EXISTS(
      SELECT 1 FROM service_accounts account
      WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
        AND account.service_account_id=NEW.subject_id))
  OR NOT(
    (NEW.subject_type='human' AND NEW.outcome='success'
      AND NEW.action_code='service_account.created'
      AND NEW.resource_type='service_account' AND EXISTS(
        SELECT 1 FROM service_accounts account
        WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
          AND account.service_account_id=NEW.resource_id
          AND account.revision=NEW.resource_revision))
    OR(NEW.subject_type='human' AND NEW.outcome='success'
      AND NEW.action_code='oauth_client.created'
      AND NEW.resource_type='oauth_client' AND EXISTS(
        SELECT 1 FROM oauth_clients client
        WHERE client.workspace_id=NEW.workspace_id AND client.tenant_id=NEW.tenant_id
          AND client.oauth_client_id=NEW.resource_id
          AND client.revision=NEW.resource_revision))
    OR(NEW.subject_type='human' AND NEW.outcome='success'
      AND NEW.action_code IN(
        'service_account.credential_rotated','service_account.revoked')
      AND NEW.resource_type='service_account' AND EXISTS(
        SELECT 1 FROM service_accounts account
        WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
          AND account.service_account_id=NEW.resource_id))
    OR(NEW.subject_type='service_account' AND NEW.outcome='success'
      AND NEW.action_code='oauth.token_issued'
      AND NEW.resource_type='access_token_grant' AND EXISTS(
        SELECT 1 FROM access_token_grants grant_record
        WHERE grant_record.workspace_id=NEW.workspace_id
          AND grant_record.tenant_id=NEW.tenant_id
          AND grant_record.service_account_id=NEW.subject_id
          AND grant_record.access_token_grant_id=NEW.resource_id))
    OR(NEW.subject_type='service_account'
      AND NEW.action_code='machine.provider_listed'
      AND NEW.resource_type='provider_collection' AND NEW.resource_id='/providers')
    OR(NEW.subject_type='service_account'
      AND NEW.action_code='machine.request_log_listed'
      AND NEW.resource_type='request_log_collection'
      AND NEW.resource_id='/request-logs')
    OR(NEW.subject_type='service_account'
      AND NEW.action_code='machine.compliance_evidence_submitted'
      AND NEW.resource_type='compliance_evidence_collection'
      AND NEW.resource_id='/platform/compliance/evidence'))
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_owner_invalid'); END;
CREATE TRIGGER trg_machine_identity_audits_update_prd118
BEFORE UPDATE ON machine_identity_audits
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_immutable'); END;
CREATE TRIGGER trg_machine_identity_audits_delete_prd118
BEFORE DELETE ON machine_identity_audits
BEGIN SELECT RAISE(ABORT,'machine_identity_audit_immutable'); END;

CREATE TABLE compliance_controls(
  compliance_control_id TEXT PRIMARY KEY CHECK(length(compliance_control_id)=36
    AND substr(compliance_control_id,1,4)='ctl_'
    AND substr(compliance_control_id,5) NOT GLOB '*[^0-9a-f]*'),
  control_code TEXT NOT NULL CHECK(length(control_code) BETWEEN 3 AND 80
    AND control_code=lower(control_code) AND control_code NOT GLOB '*[^a-z0-9._-]*'),
  control_version INTEGER NOT NULL CHECK(control_version>=1),
  control_set_version TEXT NOT NULL CHECK(length(control_set_version) BETWEEN 3 AND 40),
  title TEXT NOT NULL CHECK(length(title) BETWEEN 3 AND 160),
  owner_code TEXT NOT NULL CHECK(owner_code IN('security','privacy','platform')),
  responsibility TEXT NOT NULL CHECK(responsibility IN('platform','workspace','shared')),
  evidence_kind TEXT NOT NULL CHECK(evidence_kind IN(
    'access_review','encryption_posture','privacy_request_process')),
  collector_schema_version INTEGER NOT NULL CHECK(collector_schema_version=1),
  freshness_seconds INTEGER NOT NULL CHECK(freshness_seconds BETWEEN 300 AND 2592000),
  criticality TEXT NOT NULL CHECK(criticality IN('critical','high','medium')),
  evaluation_rule_code TEXT NOT NULL CHECK(evaluation_rule_code='sample_failure_v1'),
  evaluation_rule_sha256 TEXT NOT NULL CHECK(length(evaluation_rule_sha256)=64
    AND evaluation_rule_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status IN('active','retired')),
  seeded INTEGER NOT NULL CHECK(seeded IN(0,1)),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(control_code,control_version)
);
INSERT INTO compliance_controls VALUES
  ('ctl_00000000000000000000000000000001','iam.access_review',1,'2026.08.1',
   'Privileged access review','security','workspace','access_review',1,86400,'critical',
   'sample_failure_v1',metahub_sha256('sample_failure_v1'), 'active',1,
   strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('ctl_00000000000000000000000000000002','data.encryption_at_rest',1,'2026.08.1',
   'Encryption at rest posture','security','shared','encryption_posture',1,86400,'critical',
   'sample_failure_v1',metahub_sha256('sample_failure_v1'), 'active',1,
   strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('ctl_00000000000000000000000000000003','privacy.dsr_timeliness',1,'2026.08.1',
   'Data subject request timeliness','privacy','shared','privacy_request_process',1,172800,'high',
   'sample_failure_v1',metahub_sha256('sample_failure_v1'), 'active',1,
   strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_compliance_controls_update_prd135 BEFORE UPDATE ON compliance_controls
BEGIN SELECT RAISE(ABORT,'compliance_control_immutable'); END;
CREATE TRIGGER trg_compliance_controls_delete_prd135 BEFORE DELETE ON compliance_controls
BEGIN SELECT RAISE(ABORT,'compliance_control_immutable'); END;

CREATE TABLE compliance_control_mappings(
  control_mapping_id TEXT PRIMARY KEY CHECK(length(control_mapping_id)=36
    AND substr(control_mapping_id,1,4)='cmp_'),
  compliance_control_id TEXT NOT NULL,
  framework_code TEXT NOT NULL CHECK(framework_code IN('djcp_mlps','pipl','dsl')),
  framework_version TEXT NOT NULL CHECK(length(framework_version) BETWEEN 2 AND 40),
  clause_code TEXT NOT NULL CHECK(length(clause_code) BETWEEN 2 AND 80),
  statement_template_code TEXT NOT NULL CHECK(statement_template_code IN(
    'control_fact_only_v1','privacy_process_fact_v1')),
  seeded INTEGER NOT NULL CHECK(seeded IN(0,1)),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(compliance_control_id) REFERENCES compliance_controls(compliance_control_id),
  UNIQUE(compliance_control_id,framework_code,framework_version,clause_code)
);
INSERT INTO compliance_control_mappings VALUES
  ('cmp_00000000000000000000000000000001','ctl_00000000000000000000000000000001',
   'djcp_mlps','2.0','8.1.4.3','control_fact_only_v1',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('cmp_00000000000000000000000000000002','ctl_00000000000000000000000000000001',
   'pipl','2021','51','control_fact_only_v1',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('cmp_00000000000000000000000000000003','ctl_00000000000000000000000000000002',
   'djcp_mlps','2.0','8.1.4.1','control_fact_only_v1',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('cmp_00000000000000000000000000000004','ctl_00000000000000000000000000000002',
   'dsl','2021','27','control_fact_only_v1',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('cmp_00000000000000000000000000000005','ctl_00000000000000000000000000000003',
   'pipl','2021','47','privacy_process_fact_v1',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('cmp_00000000000000000000000000000006','ctl_00000000000000000000000000000003',
   'dsl','2021','38','privacy_process_fact_v1',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_compliance_mappings_update_prd135 BEFORE UPDATE ON compliance_control_mappings
BEGIN SELECT RAISE(ABORT,'compliance_mapping_immutable'); END;
CREATE TRIGGER trg_compliance_mappings_delete_prd135 BEFORE DELETE ON compliance_control_mappings
BEGIN SELECT RAISE(ABORT,'compliance_mapping_immutable'); END;

CREATE TABLE compliance_evidence(
  compliance_evidence_id TEXT PRIMARY KEY CHECK(length(compliance_evidence_id)=36
    AND substr(compliance_evidence_id,1,4)='evd_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  compliance_control_id TEXT NOT NULL,
  evidence_sequence INTEGER NOT NULL UNIQUE CHECK(evidence_sequence>=1),
  evidence_generation INTEGER NOT NULL CHECK(evidence_generation>=1),
  collector_service_account_id TEXT NOT NULL,
  collector_version TEXT NOT NULL CHECK(length(collector_version) BETWEEN 3 AND 40
    AND collector_version NOT GLOB '*[^A-Za-z0-9._-]*'),
  evidence_kind TEXT NOT NULL CHECK(evidence_kind IN(
    'access_review','encryption_posture','privacy_request_process')),
  schema_version INTEGER NOT NULL CHECK(schema_version=1),
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64
    AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
  classification TEXT NOT NULL CHECK(classification IN(
    'internal','confidential','restricted')),
  scope_state TEXT NOT NULL CHECK(scope_state IN('present','absent')),
  observed_state TEXT NOT NULL CHECK(observed_state IN('effective','ineffective','unknown')),
  sample_count INTEGER NOT NULL CHECK(sample_count BETWEEN 0 AND 1000000),
  failed_count INTEGER NOT NULL CHECK(failed_count BETWEEN 0 AND sample_count),
  outcome TEXT NOT NULL CHECK(outcome IN('passed','failed','inconclusive','not_applicable')),
  outcome_reason_code TEXT NOT NULL CHECK(outcome_reason_code IN(
    'all_samples_effective','sample_failure_observed','collector_inconclusive','scope_not_present')),
  envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256)=64),
  collector_authorization_sha256 TEXT NOT NULL CHECK(length(collector_authorization_sha256)=64),
  immutable_receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(immutable_receipt_sha256)=64),
  captured_at TEXT NOT NULL CHECK(unixepoch(captured_at) IS NOT NULL),
  valid_until TEXT NOT NULL CHECK(unixepoch(valid_until) IS NOT NULL
    AND valid_until>captured_at),
  submitted_at TEXT NOT NULL CHECK(unixepoch(submitted_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,collector_service_account_id)
    REFERENCES service_accounts(workspace_id,tenant_id,service_account_id),
  FOREIGN KEY(compliance_control_id) REFERENCES compliance_controls(compliance_control_id),
  UNIQUE(workspace_id,tenant_id,compliance_evidence_id),
  UNIQUE(workspace_id,tenant_id,compliance_control_id,evidence_generation),
  CHECK((scope_state='absent' AND observed_state='unknown' AND sample_count=0
      AND failed_count=0 AND outcome='not_applicable'
      AND outcome_reason_code='scope_not_present')
    OR(scope_state='present' AND sample_count>0 AND failed_count=0
      AND observed_state='effective' AND outcome='passed'
      AND outcome_reason_code='all_samples_effective')
    OR(scope_state='present' AND sample_count>0 AND observed_state='ineffective'
      AND outcome='failed' AND outcome_reason_code='sample_failure_observed')
    OR(scope_state='present' AND sample_count>0 AND observed_state='unknown'
      AND outcome='inconclusive' AND outcome_reason_code='collector_inconclusive'))
);
CREATE INDEX idx_compliance_evidence_owner_prd135
  ON compliance_evidence(workspace_id,tenant_id,compliance_control_id,
    evidence_generation DESC);
CREATE INDEX idx_compliance_evidence_freshness_prd135
  ON compliance_evidence(valid_until,compliance_evidence_id);
CREATE TRIGGER trg_compliance_evidence_insert_prd135
BEFORE INSERT ON compliance_evidence FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM workspace_tenant_bindings binding
    WHERE binding.workspace_id=NEW.workspace_id AND binding.tenant_id=NEW.tenant_id
      AND binding.status='active')
  OR NOT EXISTS(SELECT 1 FROM service_accounts account
    JOIN service_account_scopes scope
      ON scope.service_account_id=account.service_account_id
     AND scope.permission_code='compliance.collect'
    WHERE account.workspace_id=NEW.workspace_id AND account.tenant_id=NEW.tenant_id
      AND account.service_account_id=NEW.collector_service_account_id
      AND account.status='active' AND account.scope_template='compliance_collector')
  OR NOT EXISTS(SELECT 1 FROM compliance_controls control
    WHERE control.compliance_control_id=NEW.compliance_control_id
      AND control.status='active' AND control.evidence_kind=NEW.evidence_kind
      AND control.collector_schema_version=NEW.schema_version)
BEGIN SELECT RAISE(ABORT,'compliance_evidence_owner_invalid'); END;
CREATE TRIGGER trg_compliance_evidence_update_prd135 BEFORE UPDATE ON compliance_evidence
BEGIN SELECT RAISE(ABORT,'compliance_evidence_immutable'); END;
CREATE TRIGGER trg_compliance_evidence_delete_prd135 BEFORE DELETE ON compliance_evidence
BEGIN SELECT RAISE(ABORT,'compliance_evidence_immutable'); END;

CREATE TABLE compliance_exceptions(
  compliance_exception_id TEXT PRIMARY KEY CHECK(length(compliance_exception_id)=36
    AND substr(compliance_exception_id,1,4)='cex_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  compliance_control_id TEXT NOT NULL,
  evidence_generation INTEGER NOT NULL CHECK(evidence_generation>=1),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'risk_acceptance','vendor_dependency','planned_remediation')),
  compensating_controls TEXT NOT NULL CHECK(json_valid(compensating_controls)
    AND json_type(compensating_controls)='array'
    AND json_array_length(compensating_controls) BETWEEN 1 AND 3
    AND length(compensating_controls)<=256),
  requester_account_id TEXT NOT NULL,
  approver_account_id TEXT,
  status TEXT NOT NULL CHECK(status IN('pending','active','expired')),
  review_generation INTEGER NOT NULL CHECK(review_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  starts_at TEXT NOT NULL CHECK(unixepoch(starts_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL AND expires_at>starts_at),
  approved_at TEXT CHECK(approved_at IS NULL OR unixepoch(approved_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,compliance_control_id,evidence_generation)
    REFERENCES compliance_evidence(workspace_id,tenant_id,compliance_control_id,evidence_generation),
  FOREIGN KEY(requester_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(approver_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,compliance_exception_id),
  CHECK((status='pending' AND approver_account_id IS NULL AND approved_at IS NULL)
    OR(status='active' AND approver_account_id IS NOT NULL
      AND approver_account_id!=requester_account_id AND approved_at IS NOT NULL)
    OR(status='expired'))
);
CREATE INDEX idx_compliance_exceptions_owner_prd135
  ON compliance_exceptions(workspace_id,tenant_id,status,expires_at);
CREATE TRIGGER trg_compliance_exceptions_insert_prd135 BEFORE INSERT ON compliance_exceptions
FOR EACH ROW WHEN NOT EXISTS(SELECT 1 FROM compliance_evidence evidence
  WHERE evidence.workspace_id=NEW.workspace_id AND evidence.tenant_id=NEW.tenant_id
    AND evidence.compliance_control_id=NEW.compliance_control_id
    AND evidence.evidence_generation=NEW.evidence_generation)
BEGIN SELECT RAISE(ABORT,'compliance_exception_owner_invalid'); END;
CREATE TRIGGER trg_compliance_exceptions_update_prd135
BEFORE UPDATE ON compliance_exceptions FOR EACH ROW
WHEN NEW.compliance_exception_id!=OLD.compliance_exception_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.compliance_control_id!=OLD.compliance_control_id
  OR NEW.evidence_generation!=OLD.evidence_generation OR NEW.reason_code!=OLD.reason_code
  OR NEW.compensating_controls!=OLD.compensating_controls
  OR NEW.requester_account_id!=OLD.requester_account_id
  OR NEW.starts_at!=OLD.starts_at OR NEW.expires_at!=OLD.expires_at
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at OR NEW.review_generation<OLD.review_generation
  OR NOT((OLD.status='pending' AND NEW.status IN('active','expired'))
    OR(OLD.status='active' AND NEW.status='expired'))
  OR (NEW.status='active' AND (NEW.approver_account_id IS NULL
    OR NEW.approver_account_id=OLD.requester_account_id OR NEW.approved_at IS NULL
    OR NEW.review_generation!=OLD.review_generation+1))
BEGIN SELECT RAISE(ABORT,'compliance_exception_transition_invalid'); END;
CREATE TRIGGER trg_compliance_exceptions_delete_prd135 BEFORE DELETE ON compliance_exceptions
BEGIN SELECT RAISE(ABORT,'compliance_exception_immutable'); END;

CREATE TABLE compliance_remediation_tasks(
  remediation_task_id TEXT PRIMARY KEY CHECK(length(remediation_task_id)=36
    AND substr(remediation_task_id,1,4)='rem_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  compliance_control_id TEXT NOT NULL,
  failed_evidence_id TEXT NOT NULL,
  failed_evidence_generation INTEGER NOT NULL CHECK(failed_evidence_generation>=1),
  status TEXT NOT NULL CHECK(status IN('open','in_progress','blocked','verified','closed')),
  owner_code TEXT NOT NULL CHECK(owner_code IN('security','privacy','platform')),
  verified_evidence_id TEXT,
  verified_evidence_generation INTEGER,
  revision INTEGER NOT NULL CHECK(revision>=1),
  due_at TEXT NOT NULL CHECK(unixepoch(due_at) IS NOT NULL),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  closed_at TEXT CHECK(closed_at IS NULL OR unixepoch(closed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,failed_evidence_id)
    REFERENCES compliance_evidence(workspace_id,tenant_id,compliance_evidence_id),
  FOREIGN KEY(workspace_id,tenant_id,verified_evidence_id)
    REFERENCES compliance_evidence(workspace_id,tenant_id,compliance_evidence_id),
  FOREIGN KEY(compliance_control_id) REFERENCES compliance_controls(compliance_control_id),
  UNIQUE(failed_evidence_id),
  UNIQUE(workspace_id,tenant_id,remediation_task_id),
  CHECK((status IN('open','in_progress','blocked') AND verified_evidence_id IS NULL
      AND verified_evidence_generation IS NULL AND verified_at IS NULL AND closed_at IS NULL)
    OR(status='verified' AND verified_evidence_id IS NOT NULL
      AND verified_evidence_generation>failed_evidence_generation
      AND verified_at IS NOT NULL AND closed_at IS NULL)
    OR(status='closed' AND verified_evidence_id IS NOT NULL
      AND verified_evidence_generation>failed_evidence_generation
      AND verified_at IS NOT NULL AND closed_at IS NOT NULL))
);
CREATE INDEX idx_compliance_remediation_owner_prd135
  ON compliance_remediation_tasks(workspace_id,tenant_id,status,due_at);
CREATE TRIGGER trg_compliance_remediation_insert_prd135
BEFORE INSERT ON compliance_remediation_tasks FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM compliance_evidence evidence
  WHERE evidence.workspace_id=NEW.workspace_id AND evidence.tenant_id=NEW.tenant_id
    AND evidence.compliance_evidence_id=NEW.failed_evidence_id
    AND evidence.compliance_control_id=NEW.compliance_control_id
    AND evidence.evidence_generation=NEW.failed_evidence_generation
    AND evidence.outcome='failed')
BEGIN SELECT RAISE(ABORT,'compliance_remediation_owner_invalid'); END;
CREATE TRIGGER trg_compliance_remediation_update_prd135
BEFORE UPDATE ON compliance_remediation_tasks FOR EACH ROW
WHEN NEW.remediation_task_id!=OLD.remediation_task_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.compliance_control_id!=OLD.compliance_control_id
  OR NEW.failed_evidence_id!=OLD.failed_evidence_id
  OR NEW.failed_evidence_generation!=OLD.failed_evidence_generation
  OR NEW.owner_code!=OLD.owner_code OR NEW.due_at!=OLD.due_at
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='open' AND NEW.status IN('in_progress','blocked','verified'))
    OR(OLD.status='in_progress' AND NEW.status IN('blocked','verified'))
    OR(OLD.status='blocked' AND NEW.status IN('in_progress','verified'))
    OR(OLD.status='verified' AND NEW.status='closed'))
  OR (NEW.status IN('verified','closed') AND NOT EXISTS(
    SELECT 1 FROM compliance_evidence evidence
    WHERE evidence.workspace_id=NEW.workspace_id AND evidence.tenant_id=NEW.tenant_id
      AND evidence.compliance_evidence_id=NEW.verified_evidence_id
      AND evidence.compliance_control_id=NEW.compliance_control_id
      AND evidence.evidence_generation=NEW.verified_evidence_generation
      AND evidence.evidence_generation>NEW.failed_evidence_generation
      AND evidence.outcome='passed'))
BEGIN SELECT RAISE(ABORT,'compliance_remediation_transition_invalid'); END;
CREATE TRIGGER trg_compliance_remediation_delete_prd135 BEFORE DELETE ON compliance_remediation_tasks
BEGIN SELECT RAISE(ABORT,'compliance_remediation_immutable'); END;

CREATE TABLE compliance_reports(
  compliance_report_id TEXT PRIMARY KEY CHECK(length(compliance_report_id)=36
    AND substr(compliance_report_id,1,4)='crp_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  report_version INTEGER NOT NULL CHECK(report_version>=1),
  control_set_version TEXT NOT NULL,
  evidence_high_watermark INTEGER NOT NULL CHECK(evidence_high_watermark>=0),
  exception_high_watermark INTEGER NOT NULL CHECK(exception_high_watermark>=0),
  generator_version TEXT NOT NULL CHECK(generator_version='prd135-v1'),
  summary_sha256 TEXT NOT NULL CHECK(length(summary_sha256)=64),
  implemented_count INTEGER NOT NULL CHECK(implemented_count>=0),
  gap_count INTEGER NOT NULL CHECK(gap_count>=0),
  stale_count INTEGER NOT NULL CHECK(stale_count>=0),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  generated_at TEXT NOT NULL CHECK(unixepoch(generated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,tenant_id,report_version),
  UNIQUE(workspace_id,tenant_id,compliance_report_id)
);
CREATE INDEX idx_compliance_reports_owner_prd135
  ON compliance_reports(workspace_id,tenant_id,report_version DESC);
CREATE TRIGGER trg_compliance_reports_update_prd135 BEFORE UPDATE ON compliance_reports
BEGIN SELECT RAISE(ABORT,'compliance_report_immutable'); END;
CREATE TRIGGER trg_compliance_reports_delete_prd135 BEFORE DELETE ON compliance_reports
BEGIN SELECT RAISE(ABORT,'compliance_report_immutable'); END;

CREATE TABLE compliance_report_items(
  compliance_report_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  compliance_control_id TEXT NOT NULL,
  report_outcome TEXT NOT NULL CHECK(report_outcome IN(
    'implemented','partially_implemented','not_implemented','not_applicable',
    'exception_active','evidence_stale')),
  explanation_code TEXT NOT NULL CHECK(explanation_code IN(
    'evidence_passed','evidence_inconclusive','evidence_failed','scope_not_present',
    'approved_exception','evidence_expired','evidence_missing')),
  compliance_evidence_id TEXT,
  evidence_generation INTEGER,
  evidence_artifact_sha256 TEXT,
  evidence_classification TEXT,
  evidence_valid_until TEXT,
  compliance_exception_id TEXT,
  remediation_task_id TEXT,
  PRIMARY KEY(compliance_report_id,compliance_control_id),
  FOREIGN KEY(workspace_id,tenant_id,compliance_report_id)
    REFERENCES compliance_reports(workspace_id,tenant_id,compliance_report_id),
  FOREIGN KEY(compliance_control_id) REFERENCES compliance_controls(compliance_control_id),
  FOREIGN KEY(workspace_id,tenant_id,compliance_evidence_id)
    REFERENCES compliance_evidence(workspace_id,tenant_id,compliance_evidence_id),
  FOREIGN KEY(workspace_id,tenant_id,compliance_exception_id)
    REFERENCES compliance_exceptions(workspace_id,tenant_id,compliance_exception_id),
  FOREIGN KEY(workspace_id,tenant_id,remediation_task_id)
    REFERENCES compliance_remediation_tasks(workspace_id,tenant_id,remediation_task_id),
  CHECK((compliance_evidence_id IS NULL AND evidence_generation IS NULL
      AND evidence_artifact_sha256 IS NULL AND evidence_classification IS NULL
      AND evidence_valid_until IS NULL)
    OR(compliance_evidence_id IS NOT NULL AND evidence_generation IS NOT NULL
      AND length(evidence_artifact_sha256)=64
      AND evidence_classification IN('internal','confidential','restricted')
      AND unixepoch(evidence_valid_until) IS NOT NULL))
);
CREATE TRIGGER trg_compliance_report_items_update_prd135 BEFORE UPDATE ON compliance_report_items
BEGIN SELECT RAISE(ABORT,'compliance_report_immutable'); END;
CREATE TRIGGER trg_compliance_report_items_delete_prd135 BEFORE DELETE ON compliance_report_items
BEGIN SELECT RAISE(ABORT,'compliance_report_immutable'); END;

CREATE TABLE compliance_events(
  compliance_event_id TEXT PRIMARY KEY CHECK(length(compliance_event_id)=36
    AND substr(compliance_event_id,1,4)='cev_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'compliance.evidence_expiring','compliance.control_failed',
    'compliance.exception_approved')),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  object_type TEXT NOT NULL CHECK(object_type IN('evidence','exception')),
  object_id TEXT NOT NULL CHECK(length(object_id)=36),
  object_generation INTEGER NOT NULL CHECK(object_generation>=1),
  actor_type TEXT NOT NULL CHECK(actor_type IN('service_account','platform_operator','scheduler')),
  actor_id TEXT NOT NULL CHECK(length(actor_id) BETWEEN 3 AND 128),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  UNIQUE(event_type,object_id,object_generation),
  UNIQUE(workspace_id,tenant_id,compliance_event_id)
);
CREATE INDEX idx_compliance_events_owner_prd135
  ON compliance_events(workspace_id,tenant_id,occurred_at,compliance_event_id);
CREATE TRIGGER trg_compliance_events_update_prd135 BEFORE UPDATE ON compliance_events
BEGIN SELECT RAISE(ABORT,'compliance_event_immutable'); END;
CREATE TRIGGER trg_compliance_events_delete_prd135 BEFORE DELETE ON compliance_events
BEGIN SELECT RAISE(ABORT,'compliance_event_immutable'); END;

CREATE TABLE compliance_event_outbox(
  compliance_event_outbox_id TEXT PRIMARY KEY CHECK(length(compliance_event_outbox_id)=36
    AND substr(compliance_event_outbox_id,1,4)='ceo_'),
  compliance_event_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,compliance_event_id)
    REFERENCES compliance_events(workspace_id,tenant_id,compliance_event_id),
  UNIQUE(workspace_id,tenant_id,compliance_event_outbox_id),
  CHECK((status='pending' AND delivered_at IS NULL)
    OR(status='delivered' AND delivered_at IS NOT NULL)
    OR(status='failed' AND delivered_at IS NULL))
);
CREATE INDEX idx_compliance_outbox_due_prd135
  ON compliance_event_outbox(status,next_attempt_at,compliance_event_outbox_id);
CREATE TRIGGER trg_compliance_outbox_update_prd135
BEFORE UPDATE ON compliance_event_outbox FOR EACH ROW
WHEN NEW.compliance_event_outbox_id!=OLD.compliance_event_outbox_id
  OR NEW.compliance_event_id!=OLD.compliance_event_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
  OR NEW.attempt_count<OLD.attempt_count OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='pending' AND NEW.status IN('pending','delivered','failed'))
    OR(OLD.status='failed' AND NEW.status IN('pending','failed','delivered')))
BEGIN SELECT RAISE(ABORT,'compliance_outbox_transition_invalid'); END;
CREATE TRIGGER trg_compliance_outbox_delete_prd135 BEFORE DELETE ON compliance_event_outbox
BEGIN SELECT RAISE(ABORT,'compliance_outbox_immutable'); END;

CREATE TABLE compliance_action_commands(
  compliance_action_command_id TEXT PRIMARY KEY CHECK(length(compliance_action_command_id)=36
    AND substr(compliance_action_command_id,1,4)='cac_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  subject_type TEXT NOT NULL CHECK(subject_type IN('service_account','platform_operator')),
  subject_id TEXT NOT NULL CHECK(length(subject_id) BETWEEN 3 AND 128),
  action_code TEXT NOT NULL CHECK(action_code IN(
    'compliance.evidence.submit','compliance.exception.request','compliance.exception.approve')),
  idempotency_sha256 TEXT NOT NULL CHECK(length(idempotency_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_type TEXT NOT NULL CHECK(result_type IN('evidence','exception')),
  result_id TEXT NOT NULL CHECK(length(result_id)=36),
  result_generation INTEGER NOT NULL CHECK(result_generation>=1),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(subject_type,subject_id,action_code,idempotency_sha256),
  UNIQUE(workspace_id,tenant_id,compliance_action_command_id)
);
CREATE TRIGGER trg_compliance_commands_update_prd135 BEFORE UPDATE ON compliance_action_commands
BEGIN SELECT RAISE(ABORT,'compliance_command_immutable'); END;
CREATE TRIGGER trg_compliance_commands_delete_prd135 BEFORE DELETE ON compliance_action_commands
BEGIN SELECT RAISE(ABORT,'compliance_command_immutable'); END;

CREATE TABLE compliance_collector_replay_guards(
  replay_guard_id TEXT PRIMARY KEY CHECK(length(replay_guard_id)=36
    AND substr(replay_guard_id,1,4)='cgr_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  collector_service_account_id TEXT NOT NULL,
  nonce_sha256 TEXT NOT NULL CHECK(length(nonce_sha256)=64),
  envelope_sha256 TEXT NOT NULL CHECK(length(envelope_sha256)=64),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,collector_service_account_id)
    REFERENCES service_accounts(workspace_id,tenant_id,service_account_id),
  UNIQUE(collector_service_account_id,nonce_sha256),
  UNIQUE(workspace_id,tenant_id,replay_guard_id)
);
CREATE TRIGGER trg_compliance_replay_update_prd135 BEFORE UPDATE ON compliance_collector_replay_guards
BEGIN SELECT RAISE(ABORT,'compliance_replay_guard_immutable'); END;
CREATE TRIGGER trg_compliance_replay_delete_prd135 BEFORE DELETE ON compliance_collector_replay_guards
BEGIN SELECT RAISE(ABORT,'compliance_replay_guard_immutable'); END;

CREATE TABLE compliance_scheduler_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  freshness_watermark_at TEXT NOT NULL CHECK(unixepoch(freshness_watermark_at) IS NOT NULL),
  lease_owner_id TEXT,
  lease_token_sha256 TEXT,
  lease_expires_at TEXT CHECK(lease_expires_at IS NULL OR unixepoch(lease_expires_at) IS NOT NULL),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=0),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  CHECK((lease_owner_id IS NULL AND lease_token_sha256 IS NULL AND lease_expires_at IS NULL)
    OR(lease_owner_id IS NOT NULL AND length(lease_token_sha256)=64
      AND lease_expires_at IS NOT NULL))
);
INSERT INTO compliance_scheduler_state VALUES(
  1,'1970-01-01T00:00:00.000Z',NULL,NULL,NULL,0,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_compliance_scheduler_update_prd135
BEFORE UPDATE ON compliance_scheduler_state FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.fencing_generation<OLD.fencing_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'compliance_scheduler_fencing_invalid'); END;
CREATE TRIGGER trg_compliance_scheduler_delete_prd135 BEFORE DELETE ON compliance_scheduler_state
BEGIN SELECT RAISE(ABORT,'compliance_scheduler_state_required'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd135(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-135'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;

-- Rebuilt machine-owned tables must refresh their signed deletion-manifest rows.
UPDATE tenant_deletion_table_manifest
SET table_sql_sha256=(SELECT metahub_sha256(sql) FROM sqlite_schema
      WHERE type='table' AND name='service_accounts'),
    column_signature_sha256=metahub_sha256((SELECT group_concat(record,char(30)) FROM(
      SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
        ||coalesce(dflt_value,'')||char(31)||pk AS record
      FROM pragma_table_info('service_accounts') ORDER BY cid))),
    trigger_set_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
      SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
      FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
        AND trigger_record.tbl_name='service_accounts' ORDER BY trigger_record.name)),''))
WHERE source_table='service_accounts';

UPDATE tenant_deletion_table_manifest
SET table_sql_sha256=(SELECT metahub_sha256(sql) FROM sqlite_schema
      WHERE type='table' AND name='machine_identity_audits'),
    column_signature_sha256=metahub_sha256((SELECT group_concat(record,char(30)) FROM(
      SELECT cid||char(31)||name||char(31)||type||char(31)||"notnull"||char(31)
        ||coalesce(dflt_value,'')||char(31)||pk AS record
      FROM pragma_table_info('machine_identity_audits') ORDER BY cid))),
    trigger_set_sha256=metahub_sha256(coalesce((SELECT group_concat(record,char(30)) FROM(
      SELECT trigger_record.name||char(31)||metahub_sha256(trigger_record.sql) AS record
      FROM sqlite_schema trigger_record WHERE trigger_record.type='trigger'
        AND trigger_record.tbl_name='machine_identity_audits'
      ORDER BY trigger_record.name)),''))
WHERE source_table='machine_identity_audits';

CREATE TEMP TABLE prd135_manifest_sources(
  source_table TEXT PRIMARY KEY,disposition TEXT NOT NULL,retention_class TEXT);
INSERT INTO prd135_manifest_sources VALUES
  ('compliance_evidence','archive_then_delete','security'),
  ('compliance_exceptions','archive_then_delete','audit'),
  ('compliance_remediation_tasks','archive_then_delete','security'),
  ('compliance_reports','archive_then_delete','audit'),
  ('compliance_report_items','archive_then_delete','audit'),
  ('compliance_events','archive_then_delete','audit'),
  ('compliance_event_outbox','delete','operational'),
  ('compliance_action_commands','archive_then_delete','audit'),
  ('compliance_collector_replay_guards','delete','security');
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
JOIN prd135_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd135(
  source_table,tenant_key_column,source_prd,created_at)
SELECT source_table,'tenant_id','PRD-135',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd135_manifest_sources ORDER BY source_table;
DROP TABLE prd135_manifest_sources;

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
CREATE TRIGGER trg_tenant_deletion_manifest_prd135_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd135
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd135_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd135
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd135_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd135
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd135_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd135_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM workspace_compliance_permission_extensions)=2
      AND (SELECT COUNT(*) FROM workspace_compliance_role_permissions)=3
      AND (SELECT COUNT(*) FROM platform_compliance_permission_extensions)=2
      AND (SELECT COUNT(*) FROM platform_compliance_role_permissions)=4
      AND (SELECT COUNT(*) FROM machine_permission_catalog)=3
      AND (SELECT COUNT(*) FROM compliance_controls)=3
      AND (SELECT COUNT(*) FROM compliance_control_mappings)=6
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd135)=9
      AND (SELECT COUNT(*) FROM compliance_scheduler_state)=1
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd135_up_final_guard;
COMMIT;
PRAGMA legacy_alter_table = OFF;
PRAGMA foreign_keys = ON;
