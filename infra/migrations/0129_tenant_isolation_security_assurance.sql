-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd137_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd137_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0128' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'platform_roles','platform_operators','workspace_tenant_bindings',
      'workspace_memberships','tenant_deletion_table_manifest',
      'tenant_deletion_trigger_manifest'))=6 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd137')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd137_up_guard;

CREATE TABLE platform_security_assurance_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.assurance.read','platform.assurance.run',
    'platform.assurance.remediate','platform.assurance.review')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-137'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=49)
);
CREATE TABLE platform_security_assurance_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('platform_owner','platform_security')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_security_assurance_permission_extensions(permission_code)
);
INSERT INTO platform_security_assurance_permission_extensions VALUES
  ('platform.assurance.read','PRD-137',49),
  ('platform.assurance.run','PRD-137',49),
  ('platform.assurance.remediate','PRD-137',49),
  ('platform.assurance.review','PRD-137',49);
INSERT INTO platform_security_assurance_role_permissions VALUES
  ('platform_owner','platform.assurance.read'),
  ('platform_owner','platform.assurance.run'),
  ('platform_owner','platform.assurance.remediate'),
  ('platform_owner','platform.assurance.review'),
  ('platform_security','platform.assurance.read'),
  ('platform_security','platform.assurance.run'),
  ('platform_security','platform.assurance.remediate'),
  ('platform_security','platform.assurance.review');
CREATE TRIGGER trg_platform_assurance_permissions_insert_prd137
BEFORE INSERT ON platform_security_assurance_permission_extensions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_platform_assurance_permissions_update_prd137
BEFORE UPDATE ON platform_security_assurance_permission_extensions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_platform_assurance_permissions_delete_prd137
BEFORE DELETE ON platform_security_assurance_permission_extensions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_platform_assurance_roles_insert_prd137
BEFORE INSERT ON platform_security_assurance_role_permissions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_platform_assurance_roles_update_prd137
BEFORE UPDATE ON platform_security_assurance_role_permissions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_platform_assurance_roles_delete_prd137
BEFORE DELETE ON platform_security_assurance_role_permissions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;

CREATE TABLE workspace_security_assurance_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'security_assurance.read','security_assurance.accept')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-137'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=49)
);
CREATE TABLE workspace_security_assurance_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('owner','security_admin','auditor')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_security_assurance_permission_extensions(permission_code)
);
INSERT INTO workspace_security_assurance_permission_extensions VALUES
  ('security_assurance.read','PRD-137',49),
  ('security_assurance.accept','PRD-137',49);
INSERT INTO workspace_security_assurance_role_permissions VALUES
  ('owner','security_assurance.read'),
  ('owner','security_assurance.accept'),
  ('security_admin','security_assurance.read'),
  ('security_admin','security_assurance.accept'),
  ('auditor','security_assurance.read');
CREATE TRIGGER trg_workspace_assurance_permissions_insert_prd137
BEFORE INSERT ON workspace_security_assurance_permission_extensions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_assurance_permissions_update_prd137
BEFORE UPDATE ON workspace_security_assurance_permission_extensions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_assurance_permissions_delete_prd137
BEFORE DELETE ON workspace_security_assurance_permission_extensions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_assurance_roles_insert_prd137
BEFORE INSERT ON workspace_security_assurance_role_permissions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_assurance_roles_update_prd137
BEFORE UPDATE ON workspace_security_assurance_role_permissions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;
CREATE TRIGGER trg_workspace_assurance_roles_delete_prd137
BEFORE DELETE ON workspace_security_assurance_role_permissions
BEGIN SELECT RAISE(ABORT,'security_assurance_permission_immutable'); END;

CREATE TABLE security_assurance_profiles(
  profile_code TEXT NOT NULL CHECK(profile_code IN('incremental_release','quarterly_full')),
  profile_version INTEGER NOT NULL CHECK(profile_version=1),
  profile_sha256 TEXT NOT NULL UNIQUE CHECK(length(profile_sha256)=64),
  signature_sha256 TEXT NOT NULL UNIQUE CHECK(length(signature_sha256)=64),
  maximum_duration_seconds INTEGER NOT NULL CHECK(maximum_duration_seconds IN(1800,14400)),
  status TEXT NOT NULL CHECK(status='active'),
  seeded INTEGER NOT NULL CHECK(seeded=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(profile_code,profile_version)
);
INSERT INTO security_assurance_profiles VALUES
  ('incremental_release',1,
   '8ef889d9bbbd490ef4a32ba53e57354ae50f55999f4f66a8f302625534e5fd61',
   'ba75ac180a09ac38558d47bba37e26077337cfdfbe663effb2ae2113467a6f8a',
   1800,'active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('quarterly_full',1,
   'f0bbd365b18db0da6fa2b6fc7a0db7cbfcb9669d35c332fc4d0fe039048687ff',
   '4e58dc70204e8808025dd0763fbdc728fb37cde272e72170b6d53cd10c41272d',
   14400,'active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_assurance_profiles_insert_prd137 BEFORE INSERT ON security_assurance_profiles
BEGIN SELECT RAISE(ABORT,'security_assurance_registry_immutable'); END;
CREATE TRIGGER trg_assurance_profiles_update_prd137 BEFORE UPDATE ON security_assurance_profiles
BEGIN SELECT RAISE(ABORT,'security_assurance_registry_immutable'); END;
CREATE TRIGGER trg_assurance_profiles_delete_prd137 BEFORE DELETE ON security_assurance_profiles
BEGIN SELECT RAISE(ABORT,'security_assurance_registry_immutable'); END;

CREATE TABLE security_assurance_scenarios(
  scenario_code TEXT PRIMARY KEY CHECK(scenario_code IN(
    'api_idor','write_authorization','cache_owner','queue_owner','object_prefix',
    'metrics_labels','dynamic_revocation','replay_fencing','late_response','input_fuzz')),
  target_kind TEXT NOT NULL CHECK(target_kind IN(
    'public_contract','cache','queue','object_store','metrics','authorization')),
  owner_kind TEXT NOT NULL CHECK(owner_kind IN(
    'workspace_resource','asynchronous_job','cached_projection','object_artifact',
    'metric_series','workspace_session')),
  contract_code TEXT NOT NULL CHECK(length(contract_code) BETWEEN 3 AND 80
    AND contract_code=lower(contract_code)
    AND contract_code NOT GLOB '*[^a-z0-9._-]*'),
  weakness_code TEXT NOT NULL CHECK(weakness_code IN(
    'idor_read','authorization_write','cache_partition','queue_owner',
    'object_prefix','metric_label','revocation_stale','replay','late_response','input_boundary')),
  position INTEGER NOT NULL UNIQUE CHECK(position BETWEEN 1 AND 10),
  registry_version INTEGER NOT NULL CHECK(registry_version=1),
  signature_sha256 TEXT NOT NULL UNIQUE CHECK(length(signature_sha256)=64),
  seeded INTEGER NOT NULL CHECK(seeded=1)
);
INSERT INTO security_assurance_scenarios VALUES
  ('api_idor','public_contract','workspace_resource','owner.api','idor_read',1,1,'d54c0eb2c4c97d264283b4f79404fd089732f5c34a7953606b501f5dfd9b4f2a',1),
  ('write_authorization','public_contract','workspace_resource','owner.write','authorization_write',2,1,'49ac3863f064064411fc0e7d42ab7421cc09eca5c18d6d536595ab7290d34d39',1),
  ('cache_owner','cache','cached_projection','owner.cache','cache_partition',3,1,'06f1bda655b7f6197e76f649f525b059820f45d4531443982c72ba22c8dfbffe',1),
  ('queue_owner','queue','asynchronous_job','owner.queue','queue_owner',4,1,'c8e08de7b6676b28e0e870721130751697d64e7963d22a35e62f13f44b6a27fc',1),
  ('object_prefix','object_store','object_artifact','owner.object','object_prefix',5,1,'567c43f7bc2eeea172c94f11a5c1408d40f128ef050568a059f54976d4b3c5c2',1),
  ('metrics_labels','metrics','metric_series','owner.metric','metric_label',6,1,'c75c19f5669c37b27a42e0760ce8986315d445241ac45928f06f9901ce32b66e',1),
  ('dynamic_revocation','authorization','workspace_session','owner.permission','revocation_stale',7,1,'e478660f58730367705a5ec58c09e2ed88d0d1c0d8d73a1094f36e73382d2a51',1),
  ('replay_fencing','authorization','workspace_session','owner.replay','replay',8,1,'f9018077186ae591e347416c2b457a4e0a8d093ab08e3b2124c25c14fcafde73',1),
  ('late_response','public_contract','workspace_resource','owner.epoch','late_response',9,1,'f49a5f4724f4595287c36430474bf4d9451135df84d05440892144b42b233dc3',1),
  ('input_fuzz','public_contract','workspace_resource','owner.input','input_boundary',10,1,'c7ec8297a6b5cfb29ea780ff80aa40524f416f661252eed32dc299138b04bc95',1);
CREATE TRIGGER trg_assurance_scenarios_insert_prd137 BEFORE INSERT ON security_assurance_scenarios
BEGIN SELECT RAISE(ABORT,'security_assurance_registry_immutable'); END;
CREATE TRIGGER trg_assurance_scenarios_update_prd137 BEFORE UPDATE ON security_assurance_scenarios
BEGIN SELECT RAISE(ABORT,'security_assurance_registry_immutable'); END;
CREATE TRIGGER trg_assurance_scenarios_delete_prd137 BEFORE DELETE ON security_assurance_scenarios
BEGIN SELECT RAISE(ABORT,'security_assurance_registry_immutable'); END;

CREATE TABLE security_assessments(
  security_assessment_id TEXT PRIMARY KEY CHECK(length(security_assessment_id)=36
    AND substr(security_assessment_id,1,4)='sas_'
    AND substr(security_assessment_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  profile_code TEXT NOT NULL,
  profile_version INTEGER NOT NULL,
  profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64),
  profile_signature_sha256 TEXT NOT NULL CHECK(length(profile_signature_sha256)=64),
  code_tree_sha256 TEXT NOT NULL CHECK(length(code_tree_sha256)=64),
  migration_set_sha256 TEXT NOT NULL CHECK(length(migration_set_sha256)=64),
  contract_set_sha256 TEXT NOT NULL CHECK(length(contract_set_sha256)=64),
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  scope_sha256 TEXT NOT NULL CHECK(length(scope_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('queued','running','passed','failed')),
  generation INTEGER NOT NULL CHECK(generation>=1),
  requester_operator_id TEXT NOT NULL,
  environment_receipt_sha256 TEXT CHECK(environment_receipt_sha256 IS NULL
    OR length(environment_receipt_sha256)=64),
  summary_sha256 TEXT CHECK(summary_sha256 IS NULL OR length(summary_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  finished_at TEXT CHECK(finished_at IS NULL OR unixepoch(finished_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(profile_code,profile_version)
    REFERENCES security_assurance_profiles(profile_code,profile_version),
  FOREIGN KEY(requester_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(workspace_id,tenant_id,security_assessment_id),
  UNIQUE(workspace_id,tenant_id,profile_code,generation),
  CHECK((status='queued' AND started_at IS NULL AND finished_at IS NULL)
    OR(status='running' AND started_at IS NOT NULL AND finished_at IS NULL)
    OR(status IN('passed','failed') AND started_at IS NOT NULL AND finished_at IS NOT NULL))
);
CREATE INDEX idx_security_assessments_owner_prd137
ON security_assessments(workspace_id,tenant_id,status,requested_at);
CREATE TRIGGER trg_security_assessment_update_prd137
BEFORE UPDATE ON security_assessments FOR EACH ROW
WHEN NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.profile_code!=OLD.profile_code OR NEW.profile_version!=OLD.profile_version
  OR NEW.profile_sha256!=OLD.profile_sha256
  OR NEW.profile_signature_sha256!=OLD.profile_signature_sha256
  OR NEW.code_tree_sha256!=OLD.code_tree_sha256
  OR NEW.migration_set_sha256!=OLD.migration_set_sha256
  OR NEW.contract_set_sha256!=OLD.contract_set_sha256
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.scope_sha256!=OLD.scope_sha256
  OR NEW.generation!=OLD.generation OR NEW.requester_operator_id!=OLD.requester_operator_id
  OR NEW.requested_at!=OLD.requested_at OR NEW.revision!=OLD.revision+1
  OR NOT((OLD.status='queued' AND NEW.status='running')
    OR(OLD.status='running' AND NEW.status IN('passed','failed')))
BEGIN SELECT RAISE(ABORT,'security_assessment_transition_invalid'); END;
CREATE TRIGGER trg_security_assessment_delete_prd137 BEFORE DELETE ON security_assessments
BEGIN SELECT RAISE(ABORT,'security_assessment_immutable'); END;

CREATE TABLE assurance_runner_leases(
  security_assessment_id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  worker_id TEXT NOT NULL CHECK(length(worker_id) BETWEEN 8 AND 120),
  lease_token_sha256 TEXT NOT NULL CHECK(length(lease_token_sha256)=64),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=1),
  lease_expires_at TEXT NOT NULL CHECK(unixepoch(lease_expires_at) IS NOT NULL),
  permission_revision INTEGER NOT NULL CHECK(permission_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,security_assessment_id)
    REFERENCES security_assessments(workspace_id,tenant_id,security_assessment_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE TRIGGER trg_assurance_lease_update_prd137
BEFORE UPDATE ON assurance_runner_leases FOR EACH ROW
WHEN NEW.security_assessment_id!=OLD.security_assessment_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.fencing_generation<=OLD.fencing_generation OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'security_assurance_runner_fenced'); END;

CREATE TABLE isolation_test_runs(
  isolation_test_run_id TEXT PRIMARY KEY CHECK(length(isolation_test_run_id)=36
    AND substr(isolation_test_run_id,1,4)='itr_'
    AND substr(isolation_test_run_id,5) NOT GLOB '*[^0-9a-f]*'),
  security_assessment_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  scenario_code TEXT NOT NULL,
  source_subject_sha256 TEXT NOT NULL CHECK(length(source_subject_sha256)=64),
  target_subject_sha256 TEXT NOT NULL CHECK(length(target_subject_sha256)=64),
  owner_registry_version INTEGER NOT NULL CHECK(owner_registry_version=1),
  runner_image_sha256 TEXT NOT NULL CHECK(length(runner_image_sha256)=64),
  seed_receipt_sha256 TEXT NOT NULL CHECK(length(seed_receipt_sha256)=64),
  cleanup_receipt_sha256 TEXT NOT NULL CHECK(length(cleanup_receipt_sha256)=64),
  outcome TEXT NOT NULL CHECK(outcome IN('passed','failed','error')),
  duration_ms INTEGER NOT NULL CHECK(duration_ms BETWEEN 0 AND 1800000),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
  fencing_generation INTEGER NOT NULL CHECK(fencing_generation>=1),
  started_at TEXT NOT NULL CHECK(unixepoch(started_at) IS NOT NULL),
  finished_at TEXT NOT NULL CHECK(unixepoch(finished_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,security_assessment_id)
    REFERENCES security_assessments(workspace_id,tenant_id,security_assessment_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(scenario_code) REFERENCES security_assurance_scenarios(scenario_code),
  UNIQUE(workspace_id,tenant_id,isolation_test_run_id),
  UNIQUE(security_assessment_id,scenario_code)
);
CREATE TRIGGER trg_isolation_run_update_prd137 BEFORE UPDATE ON isolation_test_runs
BEGIN SELECT RAISE(ABORT,'isolation_test_run_immutable'); END;
CREATE TRIGGER trg_isolation_run_delete_prd137 BEFORE DELETE ON isolation_test_runs
BEGIN SELECT RAISE(ABORT,'isolation_test_run_immutable'); END;

CREATE TABLE pen_test_findings(
  pen_test_finding_id TEXT PRIMARY KEY CHECK(length(pen_test_finding_id)=36
    AND substr(pen_test_finding_id,1,4)='ptf_'
    AND substr(pen_test_finding_id,5) NOT GLOB '*[^0-9a-f]*'),
  security_assessment_id TEXT NOT NULL,
  isolation_test_run_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  owner_kind TEXT NOT NULL,
  contract_code TEXT NOT NULL,
  severity TEXT NOT NULL CHECK(severity IN('p0','p1','p2','p3')),
  weakness_code TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('open','remediation_pending','closed','risk_accepted')),
  sla_deadline_at TEXT NOT NULL CHECK(unixepoch(sla_deadline_at) IS NOT NULL),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  first_seen_at TEXT NOT NULL CHECK(unixepoch(first_seen_at) IS NOT NULL),
  last_seen_at TEXT NOT NULL CHECK(unixepoch(last_seen_at) IS NOT NULL),
  closed_at TEXT CHECK(closed_at IS NULL OR unixepoch(closed_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,security_assessment_id)
    REFERENCES security_assessments(workspace_id,tenant_id,security_assessment_id),
  FOREIGN KEY(workspace_id,tenant_id,isolation_test_run_id)
    REFERENCES isolation_test_runs(workspace_id,tenant_id,isolation_test_run_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,tenant_id,pen_test_finding_id)
);
CREATE INDEX idx_pen_test_findings_owner_prd137
ON pen_test_findings(workspace_id,tenant_id,status,severity,sla_deadline_at);
CREATE TRIGGER trg_pen_test_finding_update_prd137
BEFORE UPDATE ON pen_test_findings FOR EACH ROW
WHEN NEW.security_assessment_id!=OLD.security_assessment_id
  OR NEW.isolation_test_run_id!=OLD.isolation_test_run_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.owner_kind!=OLD.owner_kind OR NEW.contract_code!=OLD.contract_code
  OR NEW.severity!=OLD.severity OR NEW.weakness_code!=OLD.weakness_code
  OR NEW.sla_deadline_at!=OLD.sla_deadline_at OR NEW.evidence_sha256!=OLD.evidence_sha256
  OR NEW.first_seen_at!=OLD.first_seen_at OR NEW.revision!=OLD.revision+1
  OR NOT((OLD.status='open' AND NEW.status IN('remediation_pending','risk_accepted'))
    OR(OLD.status='remediation_pending' AND NEW.status IN('closed','risk_accepted'))
    OR(OLD.status='risk_accepted' AND NEW.status='remediation_pending'))
BEGIN SELECT RAISE(ABORT,'pen_test_finding_transition_invalid'); END;
CREATE TRIGGER trg_pen_test_finding_delete_prd137 BEFORE DELETE ON pen_test_findings
BEGIN SELECT RAISE(ABORT,'pen_test_finding_immutable'); END;

CREATE TABLE security_remediations(
  remediation_id TEXT PRIMARY KEY CHECK(length(remediation_id)=36
    AND substr(remediation_id,1,4)='rem_'
    AND substr(remediation_id,5) NOT GLOB '*[^0-9a-f]*'),
  pen_test_finding_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  change_sha256 TEXT NOT NULL CHECK(length(change_sha256)=64),
  verification_assessment_id TEXT,
  outcome TEXT NOT NULL CHECK(outcome IN('proposed','verification_failed','verified_passed')),
  requester_operator_id TEXT NOT NULL,
  approver_operator_id TEXT,
  finding_revision INTEGER NOT NULL CHECK(finding_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  verified_at TEXT CHECK(verified_at IS NULL OR unixepoch(verified_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,pen_test_finding_id)
    REFERENCES pen_test_findings(workspace_id,tenant_id,pen_test_finding_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,verification_assessment_id)
    REFERENCES security_assessments(workspace_id,tenant_id,security_assessment_id),
  FOREIGN KEY(requester_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(approver_operator_id) REFERENCES platform_operators(operator_id),
  CHECK((outcome='proposed' AND verification_assessment_id IS NULL
      AND approver_operator_id IS NULL AND verified_at IS NULL)
    OR(outcome!='proposed' AND verification_assessment_id IS NOT NULL
      AND approver_operator_id IS NOT NULL AND verified_at IS NOT NULL))
);
CREATE TRIGGER trg_security_remediation_update_prd137 BEFORE UPDATE ON security_remediations
BEGIN SELECT RAISE(ABORT,'security_remediation_immutable'); END;
CREATE TRIGGER trg_security_remediation_delete_prd137 BEFORE DELETE ON security_remediations
BEGIN SELECT RAISE(ABORT,'security_remediation_immutable'); END;

CREATE TABLE security_risk_acceptances(
  risk_acceptance_id TEXT PRIMARY KEY CHECK(length(risk_acceptance_id)=36
    AND substr(risk_acceptance_id,1,4)='rsk_'
    AND substr(risk_acceptance_id,5) NOT GLOB '*[^0-9a-f]*'),
  pen_test_finding_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  scope_sha256 TEXT NOT NULL CHECK(length(scope_sha256)=64),
  compensating_controls_json TEXT NOT NULL CHECK(json_valid(compensating_controls_json)
    AND json_type(compensating_controls_json)='array'
    AND json_array_length(compensating_controls_json) BETWEEN 1 AND 4
    AND length(compensating_controls_json)<=1024),
  status TEXT NOT NULL CHECK(status IN(
    'pending_owner','pending_security','pending_platform','active','expired','revoked')),
  owner_membership_id TEXT,
  owner_account_id TEXT,
  security_membership_id TEXT,
  security_account_id TEXT,
  platform_reviewer_operator_id TEXT,
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  revision INTEGER NOT NULL CHECK(revision>=1),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  approved_at TEXT CHECK(approved_at IS NULL OR unixepoch(approved_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,pen_test_finding_id)
    REFERENCES pen_test_findings(workspace_id,tenant_id,pen_test_finding_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id,owner_membership_id,owner_account_id)
    REFERENCES workspace_memberships(
      workspace_id,tenant_id,workspace_membership_id,account_id),
  FOREIGN KEY(workspace_id,tenant_id,security_membership_id,security_account_id)
    REFERENCES workspace_memberships(
      workspace_id,tenant_id,workspace_membership_id,account_id),
  FOREIGN KEY(platform_reviewer_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(owner_account_id IS NULL OR security_account_id IS NULL
    OR owner_account_id!=security_account_id)
);
CREATE INDEX idx_risk_acceptance_owner_prd137
ON security_risk_acceptances(workspace_id,tenant_id,status,expires_at);
CREATE TRIGGER trg_risk_acceptance_update_prd137
BEFORE UPDATE ON security_risk_acceptances FOR EACH ROW
WHEN NEW.pen_test_finding_id!=OLD.pen_test_finding_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.scope_sha256!=OLD.scope_sha256
  OR NEW.compensating_controls_json!=OLD.compensating_controls_json
  OR NEW.expires_at!=OLD.expires_at OR NEW.requested_at!=OLD.requested_at
  OR NEW.revision!=OLD.revision+1
  OR NOT((OLD.status='pending_owner' AND NEW.status='pending_security')
    OR(OLD.status='pending_security' AND NEW.status='pending_platform')
    OR(OLD.status='pending_platform' AND NEW.status IN('active','revoked'))
    OR(OLD.status='active' AND NEW.status IN('expired','revoked')))
BEGIN SELECT RAISE(ABORT,'security_risk_acceptance_transition_invalid'); END;
CREATE TRIGGER trg_risk_acceptance_delete_prd137 BEFORE DELETE ON security_risk_acceptances
BEGIN SELECT RAISE(ABORT,'security_risk_acceptance_immutable'); END;

CREATE TABLE assurance_cleanup_journal(
  cleanup_journal_id TEXT PRIMARY KEY CHECK(length(cleanup_journal_id)=36
    AND substr(cleanup_journal_id,1,4)='acj_'
    AND substr(cleanup_journal_id,5) NOT GLOB '*[^0-9a-f]*'),
  isolation_test_run_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  fixture_code TEXT NOT NULL CHECK(fixture_code IN(
    'source_subject','target_subject','cache_entry','queue_message','object_artifact','metric_series')),
  fixture_id_sha256 TEXT NOT NULL CHECK(length(fixture_id_sha256)=64),
  environment_receipt_sha256 TEXT NOT NULL CHECK(length(environment_receipt_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('pending','deleted','failed')),
  cleanup_receipt_sha256 TEXT CHECK(cleanup_receipt_sha256 IS NULL
    OR length(cleanup_receipt_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,isolation_test_run_id)
    REFERENCES isolation_test_runs(workspace_id,tenant_id,isolation_test_run_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(isolation_test_run_id,fixture_code)
);
CREATE TRIGGER trg_assurance_cleanup_update_prd137
BEFORE UPDATE ON assurance_cleanup_journal FOR EACH ROW
WHEN NEW.isolation_test_run_id!=OLD.isolation_test_run_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.fixture_code!=OLD.fixture_code OR NEW.fixture_id_sha256!=OLD.fixture_id_sha256
  OR NEW.environment_receipt_sha256!=OLD.environment_receipt_sha256
  OR OLD.status!='pending' OR NEW.status NOT IN('deleted','failed')
  OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'security_assurance_cleanup_invalid'); END;
CREATE TRIGGER trg_assurance_cleanup_delete_prd137 BEFORE DELETE ON assurance_cleanup_journal
BEGIN SELECT RAISE(ABORT,'security_assurance_cleanup_immutable'); END;

CREATE TABLE security_assurance_events(
  security_assurance_event_id TEXT PRIMARY KEY CHECK(length(security_assurance_event_id)=36
    AND substr(security_assurance_event_id,1,4)='ase_'
    AND substr(security_assurance_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'security.assessment_failed','security.finding_created','security.finding_closed')),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  object_type TEXT NOT NULL CHECK(object_type IN('assessment','finding')),
  object_id TEXT NOT NULL CHECK(length(object_id)=36),
  object_revision INTEGER NOT NULL CHECK(object_revision>=1),
  payload_json TEXT NOT NULL CHECK(json_valid(payload_json)
    AND json_type(payload_json)='object' AND length(payload_json)<=4096),
  payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 160),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 160),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(workspace_id,tenant_id,security_assurance_event_id),
  UNIQUE(event_type,object_id,object_revision)
);
CREATE TRIGGER trg_assurance_event_owner_insert_prd137
BEFORE INSERT ON security_assurance_events FOR EACH ROW
WHEN (NEW.object_type='assessment' AND NOT EXISTS(
    SELECT 1 FROM security_assessments assessment
    WHERE assessment.security_assessment_id=NEW.object_id
      AND assessment.workspace_id=NEW.workspace_id AND assessment.tenant_id=NEW.tenant_id))
  OR (NEW.object_type='finding' AND NOT EXISTS(
    SELECT 1 FROM pen_test_findings finding
    WHERE finding.pen_test_finding_id=NEW.object_id
      AND finding.workspace_id=NEW.workspace_id AND finding.tenant_id=NEW.tenant_id))
BEGIN SELECT RAISE(ABORT,'security_assurance_event_owner_invalid'); END;
CREATE TRIGGER trg_assurance_event_update_prd137 BEFORE UPDATE ON security_assurance_events
BEGIN SELECT RAISE(ABORT,'security_assurance_event_immutable'); END;
CREATE TRIGGER trg_assurance_event_delete_prd137 BEFORE DELETE ON security_assurance_events
BEGIN SELECT RAISE(ABORT,'security_assurance_event_immutable'); END;

CREATE TABLE security_assurance_event_outbox(
  security_assurance_event_outbox_id TEXT PRIMARY KEY CHECK(
    length(security_assurance_event_outbox_id)=36
    AND substr(security_assurance_event_outbox_id,1,4)='aso_'
    AND substr(security_assurance_event_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  security_assurance_event_id TEXT NOT NULL UNIQUE,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('pending','delivered')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count>=0),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,security_assurance_event_id)
    REFERENCES security_assurance_events(
      workspace_id,tenant_id,security_assurance_event_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id)
);
CREATE INDEX idx_assurance_outbox_due_prd137
ON security_assurance_event_outbox(status,next_attempt_at);
CREATE TRIGGER trg_assurance_outbox_update_prd137
BEFORE UPDATE ON security_assurance_event_outbox FOR EACH ROW
WHEN NEW.security_assurance_event_id!=OLD.security_assurance_event_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.attempt_count<OLD.attempt_count OR NEW.created_at!=OLD.created_at
  OR NOT((OLD.status='pending' AND NEW.status IN('pending','delivered'))
    OR(OLD.status='delivered' AND NEW.status='delivered'))
BEGIN SELECT RAISE(ABORT,'security_assurance_outbox_invalid'); END;
CREATE TRIGGER trg_assurance_outbox_delete_prd137 BEFORE DELETE ON security_assurance_event_outbox
BEGIN SELECT RAISE(ABORT,'security_assurance_outbox_immutable'); END;

CREATE TABLE security_assurance_action_commands(
  security_assurance_action_command_id TEXT PRIMARY KEY CHECK(
    length(security_assurance_action_command_id)=36
    AND substr(security_assurance_action_command_id,1,4)='asc_'
    AND substr(security_assurance_action_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  actor_kind TEXT NOT NULL CHECK(actor_kind IN('platform_operator','workspace_member')),
  actor_id TEXT NOT NULL CHECK(length(actor_id)=36),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'assessment.create','remediation.propose','remediation.verify',
    'risk.request','risk.owner_approve','risk.security_approve','risk.platform_review')),
  idempotency_key_sha256 TEXT NOT NULL CHECK(length(idempotency_key_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  UNIQUE(actor_kind,actor_id,action_code,idempotency_key_sha256)
);
CREATE TRIGGER trg_assurance_command_owner_insert_prd137
BEFORE INSERT ON security_assurance_action_commands FOR EACH ROW
WHEN (NEW.actor_kind='platform_operator' AND NOT EXISTS(
    SELECT 1 FROM platform_operators operator WHERE operator.operator_id=NEW.actor_id))
  OR (NEW.actor_kind='workspace_member' AND NOT EXISTS(
    SELECT 1 FROM workspace_memberships member
    WHERE member.workspace_membership_id=NEW.actor_id
      AND member.workspace_id=NEW.workspace_id AND member.tenant_id=NEW.tenant_id))
  OR (NEW.action_code='assessment.create' AND NOT EXISTS(
    SELECT 1 FROM security_assessments assessment
    WHERE assessment.security_assessment_id=NEW.result_object_id
      AND assessment.workspace_id=NEW.workspace_id AND assessment.tenant_id=NEW.tenant_id))
  OR (NEW.action_code IN('remediation.propose','remediation.verify') AND NOT EXISTS(
    SELECT 1 FROM security_remediations remediation
    WHERE remediation.remediation_id=NEW.result_object_id
      AND remediation.workspace_id=NEW.workspace_id AND remediation.tenant_id=NEW.tenant_id))
  OR (NEW.action_code LIKE 'risk.%' AND NOT EXISTS(
    SELECT 1 FROM security_risk_acceptances risk
    WHERE risk.risk_acceptance_id=NEW.result_object_id
      AND risk.workspace_id=NEW.workspace_id AND risk.tenant_id=NEW.tenant_id))
BEGIN SELECT RAISE(ABORT,'security_assurance_command_owner_invalid'); END;
CREATE TRIGGER trg_assurance_command_update_prd137 BEFORE UPDATE ON security_assurance_action_commands
BEGIN SELECT RAISE(ABORT,'security_assurance_command_immutable'); END;
CREATE TRIGGER trg_assurance_command_delete_prd137 BEFORE DELETE ON security_assurance_action_commands
BEGIN SELECT RAISE(ABORT,'security_assurance_command_immutable'); END;

CREATE TABLE security_assurance_scheduler_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  deadline_watermark_at TEXT NOT NULL CHECK(unixepoch(deadline_watermark_at) IS NOT NULL),
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
INSERT INTO security_assurance_scheduler_state VALUES(
  1,'1970-01-01T00:00:00.000Z',NULL,NULL,NULL,0,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_assurance_scheduler_update_prd137
BEFORE UPDATE ON security_assurance_scheduler_state FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.fencing_generation<OLD.fencing_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'security_assurance_scheduler_fenced'); END;
CREATE TRIGGER trg_assurance_scheduler_delete_prd137 BEFORE DELETE ON security_assurance_scheduler_state
BEGIN SELECT RAISE(ABORT,'security_assurance_scheduler_required'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd137(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-137'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd137_manifest_sources(
  source_table TEXT PRIMARY KEY,disposition TEXT NOT NULL,retention_class TEXT NOT NULL);
INSERT INTO prd137_manifest_sources VALUES
  ('security_assessments','archive_then_delete','security'),
  ('assurance_runner_leases','archive_then_delete','security'),
  ('isolation_test_runs','archive_then_delete','security'),
  ('pen_test_findings','archive_then_delete','security'),
  ('security_remediations','archive_then_delete','security'),
  ('security_risk_acceptances','archive_then_delete','security'),
  ('assurance_cleanup_journal','archive_then_delete','security'),
  ('security_assurance_events','archive_then_delete','audit'),
  ('security_assurance_event_outbox','archive_then_delete','audit'),
  ('security_assurance_action_commands','archive_then_delete','audit');
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
JOIN prd137_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd137
SELECT source_table,'tenant_id','PRD-137',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd137_manifest_sources ORDER BY source_table;
DROP TABLE prd137_manifest_sources;
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
CREATE TRIGGER trg_tenant_deletion_manifest_prd137_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd137
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd137_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd137
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd137_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd137
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd137_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd137_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_security_assurance_permission_extensions)=4
      AND (SELECT COUNT(*) FROM platform_security_assurance_role_permissions)=8
      AND (SELECT COUNT(*) FROM workspace_security_assurance_permission_extensions)=2
      AND (SELECT COUNT(*) FROM workspace_security_assurance_role_permissions)=5
      AND (SELECT COUNT(*) FROM security_assurance_profiles)=2
      AND (SELECT COUNT(*) FROM security_assurance_scenarios)=10
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd137)=10
      AND (SELECT COUNT(*) FROM security_assurance_scheduler_state)=1
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd137_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
