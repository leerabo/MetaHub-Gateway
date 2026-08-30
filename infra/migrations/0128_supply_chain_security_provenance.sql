-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd136_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd136_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0127' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'sdk_releases','developer_contract_signing_keys','platform_roles','platform_operators',
      'workspace_tenant_bindings','tenant_deletion_table_manifest',
      'tenant_deletion_trigger_manifest'))=7 THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd136')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd136_up_guard;

CREATE TABLE platform_supply_chain_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.supply_chain.read','platform.supply_chain.exception',
    'platform.supply_chain.approve','platform.supply_chain.patch')),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-136'),
  introduced_revision INTEGER NOT NULL CHECK(introduced_revision=48)
);
CREATE TABLE platform_supply_chain_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','platform_security','release_manager','platform_compliance')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_supply_chain_permission_extensions(permission_code)
);
INSERT INTO platform_supply_chain_permission_extensions VALUES
  ('platform.supply_chain.read','PRD-136',48),
  ('platform.supply_chain.exception','PRD-136',48),
  ('platform.supply_chain.approve','PRD-136',48),
  ('platform.supply_chain.patch','PRD-136',48);
INSERT INTO platform_supply_chain_role_permissions VALUES
  ('platform_owner','platform.supply_chain.read'),
  ('platform_owner','platform.supply_chain.exception'),
  ('platform_owner','platform.supply_chain.approve'),
  ('platform_owner','platform.supply_chain.patch'),
  ('platform_security','platform.supply_chain.read'),
  ('platform_security','platform.supply_chain.exception'),
  ('release_manager','platform.supply_chain.read'),
  ('release_manager','platform.supply_chain.approve'),
  ('release_manager','platform.supply_chain.patch'),
  ('platform_compliance','platform.supply_chain.read');
DROP TRIGGER trg_platform_developer_contract_roles_insert_prd133;
DROP TRIGGER trg_platform_developer_contract_roles_update_prd133;
DROP TRIGGER trg_platform_developer_contract_roles_delete_prd133;
ALTER TABLE platform_developer_contract_role_permissions
  RENAME TO platform_developer_contract_role_permissions_prd136_old;
CREATE TABLE platform_developer_contract_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN(
    'platform_owner','release_manager','platform_security','platform_compliance')),
  permission_code TEXT NOT NULL,
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_developer_contract_permission_extensions(permission_code)
);
INSERT INTO platform_developer_contract_role_permissions
SELECT role_code,permission_code
FROM platform_developer_contract_role_permissions_prd136_old;
DROP TABLE platform_developer_contract_role_permissions_prd136_old;
INSERT INTO platform_developer_contract_role_permissions VALUES
  ('platform_security','platform.developer_contract.read'),
  ('platform_compliance','platform.developer_contract.read');
CREATE TRIGGER trg_platform_developer_contract_roles_insert_prd133
BEFORE INSERT ON platform_developer_contract_role_permissions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_developer_contract_roles_update_prd133
BEFORE UPDATE ON platform_developer_contract_role_permissions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_developer_contract_roles_delete_prd133
BEFORE DELETE ON platform_developer_contract_role_permissions
BEGIN SELECT RAISE(ABORT,'developer_contract_permission_immutable'); END;
CREATE TRIGGER trg_platform_supply_chain_permissions_insert_prd136
BEFORE INSERT ON platform_supply_chain_permission_extensions
BEGIN SELECT RAISE(ABORT,'supply_chain_permission_immutable'); END;
CREATE TRIGGER trg_platform_supply_chain_permissions_update_prd136
BEFORE UPDATE ON platform_supply_chain_permission_extensions
BEGIN SELECT RAISE(ABORT,'supply_chain_permission_immutable'); END;
CREATE TRIGGER trg_platform_supply_chain_permissions_delete_prd136
BEFORE DELETE ON platform_supply_chain_permission_extensions
BEGIN SELECT RAISE(ABORT,'supply_chain_permission_immutable'); END;
CREATE TRIGGER trg_platform_supply_chain_roles_insert_prd136
BEFORE INSERT ON platform_supply_chain_role_permissions
BEGIN SELECT RAISE(ABORT,'supply_chain_permission_immutable'); END;
CREATE TRIGGER trg_platform_supply_chain_roles_update_prd136
BEFORE UPDATE ON platform_supply_chain_role_permissions
BEGIN SELECT RAISE(ABORT,'supply_chain_permission_immutable'); END;
CREATE TRIGGER trg_platform_supply_chain_roles_delete_prd136
BEFORE DELETE ON platform_supply_chain_role_permissions
BEGIN SELECT RAISE(ABORT,'supply_chain_permission_immutable'); END;

CREATE TABLE supply_chain_feed_registry(
  feed_code TEXT NOT NULL,
  feed_version TEXT NOT NULL,
  feed_digest_sha256 TEXT NOT NULL CHECK(length(feed_digest_sha256)=64
    AND feed_digest_sha256 NOT GLOB '*[^0-9a-f]*'),
  signature_sha256 TEXT NOT NULL CHECK(length(signature_sha256)=64
    AND signature_sha256 NOT GLOB '*[^0-9a-f]*'),
  status TEXT NOT NULL CHECK(status='active'),
  seeded INTEGER NOT NULL CHECK(seeded=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(feed_code,feed_version)
);
INSERT INTO supply_chain_feed_registry VALUES(
  'metahub-curated-osv','2026.08',
  '9ac2be645a0c318f1305533f8fcc5a387fc4dd646280f6040bd874b15f25db5f',
  '76cc6a8e9fe2679b91e980b8ddbe2b9bc9b3e41b596a46fc3935bb74d4db68e1',
  'active',1,strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_supply_chain_feed_insert_prd136 BEFORE INSERT ON supply_chain_feed_registry
BEGIN SELECT RAISE(ABORT,'supply_chain_feed_immutable'); END;
CREATE TRIGGER trg_supply_chain_feed_update_prd136 BEFORE UPDATE ON supply_chain_feed_registry
BEGIN SELECT RAISE(ABORT,'supply_chain_feed_immutable'); END;
CREATE TRIGGER trg_supply_chain_feed_delete_prd136 BEFORE DELETE ON supply_chain_feed_registry
BEGIN SELECT RAISE(ABORT,'supply_chain_feed_immutable'); END;

CREATE TABLE release_sbom_reports(
  sbom_report_id TEXT PRIMARY KEY CHECK(length(sbom_report_id)=36
    AND substr(sbom_report_id,1,4)='sbr_' AND substr(sbom_report_id,5) NOT GLOB '*[^0-9a-f]*'),
  sdk_release_id TEXT NOT NULL UNIQUE,
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  format TEXT NOT NULL CHECK(format IN('cyclonedx','spdx')),
  format_version TEXT NOT NULL CHECK(format_version IN('1.5','2.3')),
  component_count INTEGER NOT NULL CHECK(component_count BETWEEN 1 AND 1000000),
  license_summary_json TEXT NOT NULL CHECK(json_valid(license_summary_json)
    AND json_type(license_summary_json)='object' AND length(license_summary_json)<=8192),
  vulnerability_summary_json TEXT NOT NULL CHECK(json_valid(vulnerability_summary_json)
    AND json_type(vulnerability_summary_json)='object' AND length(vulnerability_summary_json)<=8192),
  generator_sha256 TEXT NOT NULL CHECK(length(generator_sha256)=64),
  sbom_sha256 TEXT NOT NULL UNIQUE CHECK(length(sbom_sha256)=64),
  immutable_object_receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(immutable_object_receipt_sha256)=64),
  generated_at TEXT NOT NULL CHECK(unixepoch(generated_at) IS NOT NULL),
  FOREIGN KEY(sdk_release_id) REFERENCES sdk_releases(sdk_release_id),
  UNIQUE(sdk_release_id,artifact_sha256)
);
CREATE TRIGGER trg_release_sbom_update_prd136 BEFORE UPDATE ON release_sbom_reports
BEGIN SELECT RAISE(ABORT,'release_sbom_immutable'); END;
CREATE TRIGGER trg_release_sbom_delete_prd136 BEFORE DELETE ON release_sbom_reports
BEGIN SELECT RAISE(ABORT,'release_sbom_immutable'); END;

CREATE TABLE release_build_provenance(
  build_provenance_id TEXT PRIMARY KEY CHECK(length(build_provenance_id)=36
    AND substr(build_provenance_id,1,4)='bpr_' AND substr(build_provenance_id,5) NOT GLOB '*[^0-9a-f]*'),
  sdk_release_id TEXT NOT NULL UNIQUE,
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  source_commit_sha256 TEXT NOT NULL CHECK(length(source_commit_sha256)=64),
  source_tree_sha256 TEXT NOT NULL CHECK(length(source_tree_sha256)=64),
  dependency_lock_sha256 TEXT NOT NULL CHECK(length(dependency_lock_sha256)=64),
  builder_identity_code TEXT NOT NULL CHECK(builder_identity_code='metahub-release-builder'),
  builder_image_sha256 TEXT NOT NULL CHECK(length(builder_image_sha256)=64),
  build_invocation_sha256 TEXT NOT NULL CHECK(length(build_invocation_sha256)=64),
  artifact_set_sha256 TEXT NOT NULL CHECK(length(artifact_set_sha256)=64),
  environment_allowlist_sha256 TEXT NOT NULL CHECK(length(environment_allowlist_sha256)=64),
  provenance_sha256 TEXT NOT NULL UNIQUE CHECK(length(provenance_sha256)=64),
  reproducibility_receipt_sha256 TEXT NOT NULL UNIQUE CHECK(length(reproducibility_receipt_sha256)=64),
  reproducible INTEGER NOT NULL CHECK(reproducible IN(0,1)),
  generated_at TEXT NOT NULL CHECK(unixepoch(generated_at) IS NOT NULL),
  FOREIGN KEY(sdk_release_id) REFERENCES sdk_releases(sdk_release_id),
  UNIQUE(sdk_release_id,artifact_sha256)
);
CREATE TRIGGER trg_release_provenance_update_prd136 BEFORE UPDATE ON release_build_provenance
BEGIN SELECT RAISE(ABORT,'release_provenance_immutable'); END;
CREATE TRIGGER trg_release_provenance_delete_prd136 BEFORE DELETE ON release_build_provenance
BEGIN SELECT RAISE(ABORT,'release_provenance_immutable'); END;

CREATE TABLE supply_chain_signing_keys(
  key_id TEXT PRIMARY KEY CHECK(length(key_id) BETWEEN 8 AND 80
    AND key_id=lower(key_id) AND key_id NOT GLOB '*[^a-z0-9._-]*'),
  purpose TEXT NOT NULL CHECK(purpose='metahub.artifact.attestation/v1'),
  algorithm TEXT NOT NULL CHECK(algorithm='ed25519'),
  public_key_b64 TEXT NOT NULL CHECK(length(public_key_b64)=43),
  public_key_sha256 TEXT NOT NULL UNIQUE CHECK(length(public_key_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('active','revoked')),
  not_before TEXT NOT NULL CHECK(unixepoch(not_before) IS NOT NULL),
  not_after TEXT NOT NULL CHECK(unixepoch(not_after) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  CHECK(not_after>not_before)
);
CREATE TRIGGER trg_supply_chain_keys_update_prd136 BEFORE UPDATE ON supply_chain_signing_keys
FOR EACH ROW WHEN NEW.key_id!=OLD.key_id OR NEW.purpose!=OLD.purpose
  OR NEW.algorithm!=OLD.algorithm OR NEW.public_key_b64!=OLD.public_key_b64
  OR NEW.public_key_sha256!=OLD.public_key_sha256 OR NEW.not_before!=OLD.not_before
  OR NEW.not_after!=OLD.not_after OR NOT(OLD.status='active' AND NEW.status='revoked')
BEGIN SELECT RAISE(ABORT,'supply_chain_key_immutable'); END;
CREATE TRIGGER trg_supply_chain_keys_delete_prd136 BEFORE DELETE ON supply_chain_signing_keys
BEGIN SELECT RAISE(ABORT,'supply_chain_key_immutable'); END;

CREATE TABLE release_artifact_signatures(
  artifact_signature_id TEXT PRIMARY KEY CHECK(length(artifact_signature_id)=36
    AND substr(artifact_signature_id,1,4)='asi_' AND substr(artifact_signature_id,5) NOT GLOB '*[^0-9a-f]*'),
  sdk_release_id TEXT NOT NULL UNIQUE,
  purpose TEXT NOT NULL CHECK(purpose='metahub.artifact.attestation/v1'),
  algorithm TEXT NOT NULL CHECK(algorithm='ed25519'),
  key_id TEXT NOT NULL,
  key_version INTEGER NOT NULL CHECK(key_version>=1),
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  sbom_sha256 TEXT NOT NULL CHECK(length(sbom_sha256)=64),
  provenance_sha256 TEXT NOT NULL CHECK(length(provenance_sha256)=64),
  attestation_sha256 TEXT NOT NULL UNIQUE CHECK(length(attestation_sha256)=64),
  signature_b64 TEXT NOT NULL CHECK(length(signature_b64)=86),
  signature_sha256 TEXT NOT NULL UNIQUE CHECK(length(signature_sha256)=64),
  verification_outcome TEXT NOT NULL CHECK(verification_outcome IN('passed','failed','revoked')),
  signed_at TEXT NOT NULL CHECK(unixepoch(signed_at) IS NOT NULL),
  FOREIGN KEY(sdk_release_id) REFERENCES sdk_releases(sdk_release_id),
  FOREIGN KEY(key_id) REFERENCES supply_chain_signing_keys(key_id),
  FOREIGN KEY(sbom_sha256) REFERENCES release_sbom_reports(sbom_sha256),
  FOREIGN KEY(provenance_sha256) REFERENCES release_build_provenance(provenance_sha256),
  UNIQUE(sdk_release_id,artifact_sha256,sbom_sha256,provenance_sha256)
);
CREATE TRIGGER trg_release_signature_update_prd136 BEFORE UPDATE ON release_artifact_signatures
BEGIN SELECT RAISE(ABORT,'release_signature_immutable'); END;
CREATE TRIGGER trg_release_signature_delete_prd136 BEFORE DELETE ON release_artifact_signatures
BEGIN SELECT RAISE(ABORT,'release_signature_immutable'); END;

CREATE TABLE vulnerability_findings(
  vulnerability_finding_id TEXT PRIMARY KEY CHECK(length(vulnerability_finding_id)=36
    AND substr(vulnerability_finding_id,1,4)='vfn_' AND substr(vulnerability_finding_id,5) NOT GLOB '*[^0-9a-f]*'),
  sdk_release_id TEXT NOT NULL,
  sbom_report_id TEXT NOT NULL,
  evaluation_generation INTEGER NOT NULL CHECK(evaluation_generation>=1),
  component_name TEXT NOT NULL CHECK(length(component_name) BETWEEN 1 AND 160
    AND component_name NOT GLOB '*[/\\:]*'),
  component_version TEXT NOT NULL CHECK(length(component_version) BETWEEN 1 AND 80),
  advisory_code TEXT NOT NULL CHECK(length(advisory_code) BETWEEN 3 AND 80
    AND advisory_code NOT GLOB '*[^A-Za-z0-9._-]*'),
  severity TEXT NOT NULL CHECK(severity IN('low','medium','high','critical')),
  scanner_code TEXT NOT NULL CHECK(scanner_code='metahub-fixed-scanner'),
  scanner_version TEXT NOT NULL CHECK(length(scanner_version) BETWEEN 1 AND 40),
  feed_code TEXT NOT NULL,
  feed_version TEXT NOT NULL,
  affected_artifact_sha256 TEXT NOT NULL CHECK(length(affected_artifact_sha256)=64),
  remediation_state TEXT NOT NULL CHECK(remediation_state IN(
    'open','fixed_available','patched','accepted','closed')),
  status TEXT NOT NULL CHECK(status IN('active','superseded','closed')),
  fixed_version_code TEXT CHECK(fixed_version_code IS NULL OR length(fixed_version_code)<=80),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
  first_seen_at TEXT NOT NULL CHECK(unixepoch(first_seen_at) IS NOT NULL),
  last_seen_at TEXT NOT NULL CHECK(unixepoch(last_seen_at) IS NOT NULL),
  sla_deadline_at TEXT NOT NULL CHECK(unixepoch(sla_deadline_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(sdk_release_id) REFERENCES sdk_releases(sdk_release_id),
  FOREIGN KEY(sbom_report_id) REFERENCES release_sbom_reports(sbom_report_id),
  FOREIGN KEY(feed_code,feed_version) REFERENCES supply_chain_feed_registry(feed_code,feed_version),
  UNIQUE(sdk_release_id,evaluation_generation,component_name,component_version,advisory_code),
  CHECK(last_seen_at>=first_seen_at AND sla_deadline_at>first_seen_at)
);
CREATE INDEX idx_vulnerability_findings_release_prd136
  ON vulnerability_findings(sdk_release_id,status,severity,sla_deadline_at);
CREATE TRIGGER trg_vulnerability_finding_update_prd136
BEFORE UPDATE ON vulnerability_findings FOR EACH ROW
WHEN NEW.vulnerability_finding_id!=OLD.vulnerability_finding_id
  OR NEW.sdk_release_id!=OLD.sdk_release_id OR NEW.sbom_report_id!=OLD.sbom_report_id
  OR NEW.evaluation_generation!=OLD.evaluation_generation
  OR NEW.component_name!=OLD.component_name OR NEW.component_version!=OLD.component_version
  OR NEW.advisory_code!=OLD.advisory_code OR NEW.severity!=OLD.severity
  OR NEW.scanner_code!=OLD.scanner_code OR NEW.scanner_version!=OLD.scanner_version
  OR NEW.feed_code!=OLD.feed_code OR NEW.feed_version!=OLD.feed_version
  OR NEW.affected_artifact_sha256!=OLD.affected_artifact_sha256
  OR NEW.remediation_state!=OLD.remediation_state
  OR NEW.fixed_version_code IS NOT OLD.fixed_version_code
  OR NEW.evidence_sha256!=OLD.evidence_sha256 OR NEW.first_seen_at!=OLD.first_seen_at
  OR NEW.sla_deadline_at!=OLD.sla_deadline_at OR NEW.created_at!=OLD.created_at
  OR NEW.last_seen_at<OLD.last_seen_at
  OR NOT(OLD.status='active' AND NEW.status='superseded')
BEGIN SELECT RAISE(ABORT,'vulnerability_finding_immutable'); END;
CREATE TRIGGER trg_vulnerability_finding_delete_prd136 BEFORE DELETE ON vulnerability_findings
BEGIN SELECT RAISE(ABORT,'vulnerability_finding_immutable'); END;

CREATE TABLE supply_chain_security_exceptions(
  security_exception_id TEXT PRIMARY KEY CHECK(length(security_exception_id)=36
    AND substr(security_exception_id,1,4)='sex_' AND substr(security_exception_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  vulnerability_finding_id TEXT NOT NULL,
  affected_artifact_sha256 TEXT NOT NULL CHECK(length(affected_artifact_sha256)=64),
  reason_code TEXT NOT NULL CHECK(reason_code IN(
    'vendor_fix_pending','mitigated_exposure','emergency_continuity')),
  compensating_controls_json TEXT NOT NULL CHECK(json_valid(compensating_controls_json)
    AND json_type(compensating_controls_json)='array'
    AND json_array_length(compensating_controls_json) BETWEEN 1 AND 3),
  status TEXT NOT NULL CHECK(status IN('pending','active','expired','revoked')),
  requester_operator_id TEXT NOT NULL,
  approver_operator_id TEXT,
  starts_at TEXT NOT NULL CHECK(unixepoch(starts_at) IS NOT NULL),
  expires_at TEXT NOT NULL CHECK(unixepoch(expires_at) IS NOT NULL),
  review_generation INTEGER NOT NULL CHECK(review_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  requested_at TEXT NOT NULL CHECK(unixepoch(requested_at) IS NOT NULL),
  approved_at TEXT CHECK(approved_at IS NULL OR unixepoch(approved_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(vulnerability_finding_id) REFERENCES vulnerability_findings(vulnerability_finding_id),
  FOREIGN KEY(requester_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(approver_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(workspace_id,tenant_id,security_exception_id),
  CHECK(expires_at>starts_at),
  CHECK((status='pending' AND approver_operator_id IS NULL AND approved_at IS NULL)
    OR(status!='pending' AND approver_operator_id IS NOT NULL AND approved_at IS NOT NULL)),
  CHECK(requester_operator_id!=coalesce(approver_operator_id,''))
);
CREATE INDEX idx_supply_chain_exceptions_owner_prd136
  ON supply_chain_security_exceptions(workspace_id,tenant_id,status,expires_at);
CREATE TRIGGER trg_supply_chain_exception_insert_prd136
BEFORE INSERT ON supply_chain_security_exceptions FOR EACH ROW
WHEN NOT EXISTS(SELECT 1 FROM vulnerability_findings finding
  WHERE finding.vulnerability_finding_id=NEW.vulnerability_finding_id
    AND finding.affected_artifact_sha256=NEW.affected_artifact_sha256
    AND finding.status='active')
BEGIN SELECT RAISE(ABORT,'supply_chain_exception_scope_invalid'); END;
CREATE TRIGGER trg_supply_chain_exception_update_prd136
BEFORE UPDATE ON supply_chain_security_exceptions FOR EACH ROW
WHEN NEW.security_exception_id!=OLD.security_exception_id
  OR NEW.workspace_id!=OLD.workspace_id OR NEW.tenant_id!=OLD.tenant_id
  OR NEW.vulnerability_finding_id!=OLD.vulnerability_finding_id
  OR NEW.affected_artifact_sha256!=OLD.affected_artifact_sha256
  OR NEW.reason_code!=OLD.reason_code
  OR NEW.compensating_controls_json!=OLD.compensating_controls_json
  OR NEW.requester_operator_id!=OLD.requester_operator_id
  OR NEW.starts_at!=OLD.starts_at OR NEW.expires_at!=OLD.expires_at
  OR NEW.review_generation!=OLD.review_generation OR NEW.requested_at!=OLD.requested_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='pending' AND NEW.status='active')
    OR(OLD.status='active' AND NEW.status IN('expired','revoked')))
BEGIN SELECT RAISE(ABORT,'supply_chain_exception_transition_invalid'); END;
CREATE TRIGGER trg_supply_chain_exception_delete_prd136
BEFORE DELETE ON supply_chain_security_exceptions
BEGIN SELECT RAISE(ABORT,'supply_chain_exception_immutable'); END;

CREATE TABLE patch_releases(
  patch_release_id TEXT PRIMARY KEY CHECK(length(patch_release_id)=36
    AND substr(patch_release_id,1,4)='ptr_' AND substr(patch_release_id,5) NOT GLOB '*[^0-9a-f]*'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  vulnerability_finding_id TEXT NOT NULL,
  affected_artifact_sha256 TEXT NOT NULL CHECK(length(affected_artifact_sha256)=64),
  fixed_sdk_release_id TEXT NOT NULL,
  fixed_artifact_sha256 TEXT NOT NULL CHECK(length(fixed_artifact_sha256)=64),
  compatibility_matrix_json TEXT NOT NULL CHECK(json_valid(compatibility_matrix_json)
    AND compatibility_matrix_json=
      '{"cloud":{"n":"passed","n_minus_1":"passed"},"edge":{"n":"passed","n_minus_1":"passed"}}'),
  compatibility_sha256 TEXT NOT NULL CHECK(length(compatibility_sha256)=64),
  rollback_artifact_sha256 TEXT NOT NULL CHECK(length(rollback_artifact_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('proposed','approved','released','rolled_back')),
  requester_operator_id TEXT NOT NULL,
  approver_operator_id TEXT,
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(vulnerability_finding_id) REFERENCES vulnerability_findings(vulnerability_finding_id),
  FOREIGN KEY(fixed_sdk_release_id) REFERENCES sdk_releases(sdk_release_id),
  FOREIGN KEY(requester_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(approver_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(workspace_id,tenant_id,patch_release_id),
  CHECK(affected_artifact_sha256!=fixed_artifact_sha256),
  CHECK((status='proposed' AND approver_operator_id IS NULL AND released_at IS NULL)
    OR(status IN('approved','released','rolled_back') AND approver_operator_id IS NOT NULL)),
  CHECK((status IN('released','rolled_back') AND released_at IS NOT NULL)
    OR(status IN('proposed','approved') AND released_at IS NULL)),
  CHECK(requester_operator_id!=coalesce(approver_operator_id,''))
);
CREATE INDEX idx_patch_releases_owner_prd136
  ON patch_releases(workspace_id,tenant_id,status,created_at);
CREATE TRIGGER trg_patch_release_insert_prd136
BEFORE INSERT ON patch_releases FOR EACH ROW
WHEN NEW.rollback_artifact_sha256!=NEW.affected_artifact_sha256
  OR NOT EXISTS(
    SELECT 1 FROM vulnerability_findings finding
    JOIN sdk_releases affected ON affected.sdk_release_id=finding.sdk_release_id
    JOIN sdk_releases fixed ON fixed.sdk_release_id=NEW.fixed_sdk_release_id
    JOIN release_artifact_signatures signature
      ON signature.sdk_release_id=fixed.sdk_release_id
    WHERE finding.vulnerability_finding_id=NEW.vulnerability_finding_id
      AND finding.status='active'
      AND finding.affected_artifact_sha256=NEW.affected_artifact_sha256
      AND affected.language=fixed.language
      AND fixed.status='published'
      AND fixed.artifact_sha256=NEW.fixed_artifact_sha256
      AND signature.verification_outcome='passed')
BEGIN SELECT RAISE(ABORT,'patch_release_scope_invalid'); END;
CREATE TRIGGER trg_patch_release_update_prd136
BEFORE UPDATE ON patch_releases FOR EACH ROW
WHEN NEW.patch_release_id!=OLD.patch_release_id OR NEW.workspace_id!=OLD.workspace_id
  OR NEW.tenant_id!=OLD.tenant_id OR NEW.vulnerability_finding_id!=OLD.vulnerability_finding_id
  OR NEW.affected_artifact_sha256!=OLD.affected_artifact_sha256
  OR NEW.fixed_sdk_release_id!=OLD.fixed_sdk_release_id
  OR NEW.fixed_artifact_sha256!=OLD.fixed_artifact_sha256
  OR NEW.compatibility_matrix_json!=OLD.compatibility_matrix_json
  OR NEW.compatibility_sha256!=OLD.compatibility_sha256
  OR NEW.rollback_artifact_sha256!=OLD.rollback_artifact_sha256
  OR NEW.requester_operator_id!=OLD.requester_operator_id OR NEW.created_at!=OLD.created_at
  OR(OLD.status='proposed' AND NEW.approver_operator_id IS NULL)
  OR(OLD.status!='proposed' AND NEW.approver_operator_id IS NOT OLD.approver_operator_id)
  OR(OLD.status='released' AND NEW.released_at IS NOT OLD.released_at)
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='proposed' AND NEW.status='approved')
    OR(OLD.status='approved' AND NEW.status='released')
    OR(OLD.status='released' AND NEW.status='rolled_back'))
BEGIN SELECT RAISE(ABORT,'patch_release_transition_invalid'); END;
CREATE TRIGGER trg_patch_release_delete_prd136 BEFORE DELETE ON patch_releases
BEGIN SELECT RAISE(ABORT,'patch_release_immutable'); END;

CREATE TABLE supply_chain_events(
  supply_chain_event_id TEXT PRIMARY KEY CHECK(length(supply_chain_event_id)=36
    AND substr(supply_chain_event_id,1,4)='sce_' AND substr(supply_chain_event_id,5) NOT GLOB '*[^0-9a-f]*'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'release.security_scan_failed','vulnerability.critical_found','release.signed','patch.released')),
  source_object_type TEXT NOT NULL CHECK(source_object_type IN(
    'sdk_release','vulnerability_finding','security_exception','patch_release')),
  source_object_id TEXT NOT NULL CHECK(length(source_object_id)=36),
  source_revision INTEGER NOT NULL CHECK(source_revision>=1),
  metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)
    AND json_type(metadata_json)='object' AND length(metadata_json)<=8192),
  metadata_sha256 TEXT NOT NULL CHECK(length(metadata_sha256)=64),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL)
);
CREATE TRIGGER trg_supply_chain_events_update_prd136 BEFORE UPDATE ON supply_chain_events
BEGIN SELECT RAISE(ABORT,'supply_chain_event_immutable'); END;
CREATE TRIGGER trg_supply_chain_events_delete_prd136 BEFORE DELETE ON supply_chain_events
BEGIN SELECT RAISE(ABORT,'supply_chain_event_immutable'); END;

CREATE TABLE supply_chain_event_outbox(
  supply_chain_outbox_id TEXT PRIMARY KEY CHECK(length(supply_chain_outbox_id)=36
    AND substr(supply_chain_outbox_id,1,4)='sco_' AND substr(supply_chain_outbox_id,5) NOT GLOB '*[^0-9a-f]*'),
  supply_chain_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 20),
  next_attempt_at TEXT NOT NULL CHECK(unixepoch(next_attempt_at) IS NOT NULL),
  delivered_at TEXT CHECK(delivered_at IS NULL OR unixepoch(delivered_at) IS NOT NULL),
  last_error_code TEXT CHECK(last_error_code IS NULL OR length(last_error_code)<=80),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(supply_chain_event_id) REFERENCES supply_chain_events(supply_chain_event_id),
  CHECK((status='delivered' AND delivered_at IS NOT NULL)
    OR(status!='delivered' AND delivered_at IS NULL))
);
CREATE INDEX idx_supply_chain_outbox_due_prd136
  ON supply_chain_event_outbox(status,next_attempt_at,supply_chain_outbox_id);
CREATE TRIGGER trg_supply_chain_outbox_update_prd136
BEFORE UPDATE ON supply_chain_event_outbox FOR EACH ROW
WHEN NEW.supply_chain_outbox_id!=OLD.supply_chain_outbox_id
  OR NEW.supply_chain_event_id!=OLD.supply_chain_event_id OR NEW.created_at!=OLD.created_at
  OR NEW.attempt_count<OLD.attempt_count OR NEW.revision!=OLD.revision+1
  OR NEW.updated_at<=OLD.updated_at
  OR NOT((OLD.status='pending' AND NEW.status IN('pending','delivered','failed'))
    OR(OLD.status='failed' AND NEW.status IN('pending','failed','delivered')))
BEGIN SELECT RAISE(ABORT,'supply_chain_outbox_transition_invalid'); END;
CREATE TRIGGER trg_supply_chain_outbox_delete_prd136 BEFORE DELETE ON supply_chain_event_outbox
BEGIN SELECT RAISE(ABORT,'supply_chain_outbox_immutable'); END;

CREATE TABLE supply_chain_action_commands(
  supply_chain_action_command_id TEXT PRIMARY KEY CHECK(length(supply_chain_action_command_id)=36
    AND substr(supply_chain_action_command_id,1,4)='sac_' AND substr(supply_chain_action_command_id,5) NOT GLOB '*[^0-9a-f]*'),
  operator_id TEXT NOT NULL,
  action_code TEXT NOT NULL CHECK(action_code IN(
    'security_exception.request','security_exception.approve',
    'patch.propose','patch.approve','patch.release','patch.rollback')),
  idempotency_key_sha256 TEXT NOT NULL CHECK(length(idempotency_key_sha256)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_object_id TEXT NOT NULL CHECK(length(result_object_id)=36),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(operator_id,action_code,idempotency_key_sha256)
);
CREATE TRIGGER trg_supply_chain_commands_update_prd136 BEFORE UPDATE ON supply_chain_action_commands
BEGIN SELECT RAISE(ABORT,'supply_chain_command_immutable'); END;
CREATE TRIGGER trg_supply_chain_commands_delete_prd136 BEFORE DELETE ON supply_chain_action_commands
BEGIN SELECT RAISE(ABORT,'supply_chain_command_immutable'); END;

CREATE TABLE supply_chain_scheduler_state(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  vulnerability_watermark_at TEXT NOT NULL CHECK(unixepoch(vulnerability_watermark_at) IS NOT NULL),
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
INSERT INTO supply_chain_scheduler_state VALUES(
  1,'1970-01-01T00:00:00.000Z',NULL,NULL,NULL,0,1,
  strftime('%Y-%m-%dT%H:%M:%fZ','now'));
CREATE TRIGGER trg_supply_chain_scheduler_update_prd136
BEFORE UPDATE ON supply_chain_scheduler_state FOR EACH ROW
WHEN NEW.singleton_id!=OLD.singleton_id OR NEW.fencing_generation<OLD.fencing_generation
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'supply_chain_scheduler_fencing_invalid'); END;
CREATE TRIGGER trg_supply_chain_scheduler_delete_prd136 BEFORE DELETE ON supply_chain_scheduler_state
BEGIN SELECT RAISE(ABORT,'supply_chain_scheduler_state_required'); END;

CREATE TABLE tenant_deletion_manifest_extensions_prd136(
  source_table TEXT PRIMARY KEY,
  tenant_key_column TEXT NOT NULL CHECK(tenant_key_column='tenant_id'),
  source_prd TEXT NOT NULL CHECK(source_prd='PRD-136'),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(source_table) REFERENCES tenant_deletion_table_manifest(source_table)
);
DROP TRIGGER trg_tenant_deletion_table_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_table_manifest_delete_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_update_prd089;
DROP TRIGGER trg_tenant_deletion_trigger_manifest_delete_prd089;
CREATE TEMP TABLE prd136_manifest_sources(
  source_table TEXT PRIMARY KEY,disposition TEXT NOT NULL,retention_class TEXT NOT NULL);
INSERT INTO prd136_manifest_sources VALUES
  ('supply_chain_security_exceptions','archive_then_delete','security'),
  ('patch_releases','archive_then_delete','security');
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
JOIN prd136_manifest_sources source ON source.source_table=table_record.name
WHERE table_record.type='table';
INSERT INTO tenant_deletion_manifest_extensions_prd136
SELECT source_table,'tenant_id','PRD-136',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM prd136_manifest_sources ORDER BY source_table;
DROP TABLE prd136_manifest_sources;
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
CREATE TRIGGER trg_tenant_deletion_manifest_prd136_insert
BEFORE INSERT ON tenant_deletion_manifest_extensions_prd136
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd136_update
BEFORE UPDATE ON tenant_deletion_manifest_extensions_prd136
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;
CREATE TRIGGER trg_tenant_deletion_manifest_prd136_delete
BEFORE DELETE ON tenant_deletion_manifest_extensions_prd136
BEGIN SELECT RAISE(ABORT,'tenant_deletion_manifest_immutable'); END;

CREATE TEMP TABLE prd136_up_final_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd136_up_final_guard(issue_count)
SELECT CASE WHEN (SELECT COUNT(*) FROM platform_supply_chain_permission_extensions)=4
      AND (SELECT COUNT(*) FROM platform_supply_chain_role_permissions)=10
      AND (SELECT COUNT(*) FROM supply_chain_feed_registry)=1
      AND (SELECT COUNT(*) FROM tenant_deletion_manifest_extensions_prd136)=2
      AND (SELECT COUNT(*) FROM supply_chain_scheduler_state)=1
      THEN 0 ELSE 1 END
  + CASE WHEN NOT EXISTS(
      SELECT 1 FROM sqlite_schema table_record
      WHERE table_record.type='table' AND table_record.name NOT LIKE 'sqlite_%'
        AND EXISTS(SELECT 1 FROM pragma_table_info(table_record.name) WHERE name='tenant_id')
        AND NOT EXISTS(SELECT 1 FROM tenant_deletion_table_manifest manifest
          WHERE manifest.source_table=table_record.name)) THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd136_up_final_guard;
COMMIT;
PRAGMA foreign_keys = ON;
