-- metahub:expected-version-v1
PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TEMP TABLE prd143_up_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd143_up_guard(issue_count)
SELECT CASE WHEN (SELECT version FROM schema_migrations WHERE status='applied'
    ORDER BY version DESC LIMIT 1)='0134' THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM sqlite_schema WHERE type='table' AND name IN(
      'provider_adapter_catalog_activations','adapter_contract_runs','adapter_contract_cases',
      'adapter_contract_event_outbox','provider_error_builtin_catalog_versions',
      'provider_error_builtin_rule_versions','provider_error_maps','provider_error_map_versions',
      'provider_error_map_heads','provider_error_map_events','provider_error_facts',
      'workspace_tenant_bindings','platform_operators','platform_roles','sdk_releases',
      'release_sbom_reports','release_build_provenance','release_artifact_signatures'))=18
      THEN 0 ELSE 1 END
  + (SELECT COUNT(*) FROM adapter_contract_event_outbox
      WHERE dispatch_status='processing' AND unixepoch(lease_expires_at)>unixepoch('now'))
  + (SELECT COUNT(*) FROM provider_error_map_events
      WHERE dispatch_status='processing' AND unixepoch(lease_expires_at)>unixepoch('now'))
  + (SELECT COUNT(*) FROM(
      SELECT catalog_version
      FROM provider_adapter_catalog_activations
      GROUP BY catalog_version
      HAVING COUNT(DISTINCT catalog_hash||':'||CAST(spec_count AS TEXT))!=1))
  + (SELECT COUNT(*) FROM(
      SELECT map.tenant_id
      FROM provider_error_maps map
      LEFT JOIN workspace_tenant_bindings binding
        ON binding.tenant_id=map.tenant_id AND binding.status='active'
      GROUP BY map.tenant_id HAVING COUNT(DISTINCT binding.workspace_id)!=1))
  + (SELECT COUNT(*) FROM sqlite_schema WHERE name LIKE '%_prd143')
  + (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok');
DROP TABLE prd143_up_guard;

CREATE TABLE platform_provider_engineering_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'platform.provider_adapter.read','platform.provider_adapter.candidate_manage',
    'platform.provider_adapter.contract_run','platform.provider_adapter.failure_review',
    'platform.provider_adapter.release','platform.error_catalog.read',
    'platform.error_catalog.candidate_manage','platform.error_catalog.security_approve',
    'platform.error_catalog.release')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE TABLE platform_provider_engineering_role_permissions(
  role_code TEXT NOT NULL,
  permission_code TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(role_code) REFERENCES platform_roles(role_code),
  FOREIGN KEY(permission_code)
    REFERENCES platform_provider_engineering_permission_extensions(permission_code)
);
INSERT INTO platform_provider_engineering_permission_extensions VALUES
  ('platform.provider_adapter.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.provider_adapter.candidate_manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.provider_adapter.contract_run',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.provider_adapter.failure_review',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.provider_adapter.release',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.error_catalog.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.error_catalog.candidate_manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.error_catalog.security_approve',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform.error_catalog.release',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
INSERT INTO platform_provider_engineering_role_permissions VALUES
  ('platform_provider_engineer','platform.provider_adapter.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_provider_engineer','platform.provider_adapter.candidate_manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_provider_engineer','platform.provider_adapter.contract_run',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_provider_engineer','platform.provider_adapter.failure_review',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_provider_engineer','platform.error_catalog.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_provider_engineer','platform.error_catalog.candidate_manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_security','platform.provider_adapter.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_security','platform.provider_adapter.failure_review',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_security','platform.error_catalog.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_security','platform.error_catalog.security_approve',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('release_manager','platform.provider_adapter.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('release_manager','platform.provider_adapter.release',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('release_manager','platform.error_catalog.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('release_manager','platform.error_catalog.release',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.provider_adapter.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.provider_adapter.failure_review',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('platform_owner','platform.error_catalog.read',strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE workspace_provider_engineering_permission_extensions(
  permission_code TEXT PRIMARY KEY CHECK(permission_code IN(
    'workspace.provider_error_override.read','workspace.provider_error_override.manage')),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL)
);
CREATE TABLE workspace_provider_engineering_role_permissions(
  role_code TEXT NOT NULL CHECK(role_code IN('owner','admin','developer','security_admin')),
  permission_code TEXT NOT NULL,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  PRIMARY KEY(role_code,permission_code),
  FOREIGN KEY(permission_code)
    REFERENCES workspace_provider_engineering_permission_extensions(permission_code)
);
INSERT INTO workspace_provider_engineering_permission_extensions VALUES
  ('workspace.provider_error_override.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('workspace.provider_error_override.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now'));
INSERT INTO workspace_provider_engineering_role_permissions VALUES
  ('owner','workspace.provider_error_override.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('owner','workspace.provider_error_override.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('admin','workspace.provider_error_override.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('admin','workspace.provider_error_override.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('developer','workspace.provider_error_override.read',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('developer','workspace.provider_error_override.manage',strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  ('security_admin','workspace.provider_error_override.read',strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TRIGGER trg_platform_provider_engineering_permissions_insert_prd143
BEFORE INSERT ON platform_provider_engineering_permission_extensions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_engineering_permissions_update_prd143
BEFORE UPDATE ON platform_provider_engineering_permission_extensions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_engineering_permissions_delete_prd143
BEFORE DELETE ON platform_provider_engineering_permission_extensions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_engineering_roles_insert_prd143
BEFORE INSERT ON platform_provider_engineering_role_permissions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_engineering_roles_update_prd143
BEFORE UPDATE ON platform_provider_engineering_role_permissions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_platform_provider_engineering_roles_delete_prd143
BEFORE DELETE ON platform_provider_engineering_role_permissions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_engineering_permissions_insert_prd143
BEFORE INSERT ON workspace_provider_engineering_permission_extensions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_engineering_permissions_update_prd143
BEFORE UPDATE ON workspace_provider_engineering_permission_extensions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_engineering_permissions_delete_prd143
BEFORE DELETE ON workspace_provider_engineering_permission_extensions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_engineering_roles_insert_prd143
BEFORE INSERT ON workspace_provider_engineering_role_permissions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_engineering_roles_update_prd143
BEFORE UPDATE ON workspace_provider_engineering_role_permissions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;
CREATE TRIGGER trg_workspace_provider_engineering_roles_delete_prd143
BEFORE DELETE ON workspace_provider_engineering_role_permissions
BEGIN SELECT RAISE(ABORT,'provider_engineering_permission_immutable'); END;

CREATE TABLE provider_adapter_releases(
  provider_adapter_release_id TEXT PRIMARY KEY CHECK(length(provider_adapter_release_id)=37
    AND substr(provider_adapter_release_id,1,5)='parl_'
    AND substr(provider_adapter_release_id,6) NOT GLOB '*[^0-9a-f]*'),
  adapter_code TEXT NOT NULL CHECK(length(adapter_code) BETWEEN 2 AND 64
    AND adapter_code=lower(adapter_code) AND adapter_code NOT GLOB '*[^a-z0-9._-]*'),
  release_version TEXT NOT NULL CHECK(length(release_version) BETWEEN 1 AND 80
    AND release_version NOT GLOB '*[^A-Za-z0-9._+-]*'),
  protocol_family TEXT NOT NULL CHECK(protocol_family IN(
    'openai_compatible','anthropic_compatible','generic_http','legacy_catalog')),
  sdk_release_id TEXT,
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64
    AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'),
  sbom_sha256 TEXT NOT NULL CHECK(length(sbom_sha256)=64
    AND sbom_sha256 NOT GLOB '*[^0-9a-f]*'),
  provenance_sha256 TEXT NOT NULL CHECK(length(provenance_sha256)=64
    AND provenance_sha256 NOT GLOB '*[^0-9a-f]*'),
  signature_sha256 TEXT NOT NULL CHECK(length(signature_sha256)=64
    AND signature_sha256 NOT GLOB '*[^0-9a-f]*'),
  spec_manifest_version TEXT NOT NULL CHECK(length(spec_manifest_version) BETWEEN 1 AND 80),
  spec_manifest_sha256 TEXT NOT NULL CHECK(length(spec_manifest_sha256)=64
    AND spec_manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
  compatibility_sha256 TEXT NOT NULL CHECK(length(compatibility_sha256)=64
    AND compatibility_sha256 NOT GLOB '*[^0-9a-f]*'),
  required_runtime_version TEXT NOT NULL CHECK(length(required_runtime_version) BETWEEN 1 AND 80),
  required_api_version TEXT NOT NULL CHECK(length(required_api_version) BETWEEN 1 AND 80),
  status TEXT NOT NULL CHECK(status IN(
    'legacy_baseline','candidate','testing','review_required','approved',
    'released','deprecated','revoked')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  release_generation INTEGER NOT NULL CHECK(release_generation>=0),
  candidate_by_operator_id TEXT,
  security_reviewed_by_operator_id TEXT,
  released_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  FOREIGN KEY(sdk_release_id) REFERENCES sdk_releases(sdk_release_id),
  FOREIGN KEY(candidate_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(security_reviewed_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(released_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(adapter_code,release_version),
  CHECK(updated_at>=created_at),
  CHECK(candidate_by_operator_id IS NULL OR candidate_by_operator_id!=coalesce(security_reviewed_by_operator_id,'')),
  CHECK(released_by_operator_id IS NULL OR(released_by_operator_id!=coalesce(candidate_by_operator_id,'')
    AND released_by_operator_id!=coalesce(security_reviewed_by_operator_id,'')))
);
CREATE INDEX idx_provider_adapter_release_list_prd143
  ON provider_adapter_releases(status,adapter_code,release_generation DESC,provider_adapter_release_id);
CREATE TABLE provider_adapter_release_heads(
  adapter_code TEXT PRIMARY KEY,
  provider_adapter_release_id TEXT NOT NULL UNIQUE,
  release_generation INTEGER NOT NULL CHECK(release_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(provider_adapter_release_id)
    REFERENCES provider_adapter_releases(provider_adapter_release_id)
);

CREATE TABLE platform_adapter_contract_run_projections(
  contract_run_id TEXT PRIMARY KEY CHECK(
    (length(contract_run_id)=37 AND substr(contract_run_id,1,5)='pacr_')
    OR(length(contract_run_id)=36 AND substr(contract_run_id,1,4)='acr_')),
  scope TEXT NOT NULL CHECK(scope IN(
    'platform_release_ci','legacy_tenant_merge_gate','legacy_tenant_probe')),
  provider_adapter_release_id TEXT,
  adapter_code TEXT NOT NULL CHECK(length(adapter_code) BETWEEN 2 AND 64),
  schema_version TEXT NOT NULL CHECK(length(schema_version) BETWEEN 1 AND 80),
  runner_version TEXT NOT NULL CHECK(length(runner_version) BETWEEN 1 AND 80),
  suite_kind TEXT NOT NULL CHECK(suite_kind IN('merge_gate','probe','release_ci')),
  mode TEXT NOT NULL CHECK(mode IN('mock','gateway_probe','isolated_runner')),
  source_revision_sha256 TEXT NOT NULL CHECK(length(source_revision_sha256)=64),
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  catalog_sha256 TEXT NOT NULL CHECK(length(catalog_sha256)=64),
  status TEXT NOT NULL CHECK(status IN('queued','running','passed','failed','cancelled')),
  coverage_complete INTEGER NOT NULL CHECK(coverage_complete IN(0,1)),
  total_cases INTEGER NOT NULL CHECK(total_cases BETWEEN 0 AND 100000),
  passed_cases INTEGER NOT NULL CHECK(passed_cases BETWEEN 0 AND total_cases),
  failed_cases INTEGER NOT NULL CHECK(failed_cases BETWEEN 0 AND total_cases),
  failure_codes_json TEXT NOT NULL CHECK(json_valid(failure_codes_json)
    AND json_type(failure_codes_json)='array' AND length(failure_codes_json)<=2048),
  report_sha256 TEXT NOT NULL CHECK(length(report_sha256)=64),
  attestation_sha256 TEXT NOT NULL CHECK(length(attestation_sha256)=64),
  qualifies_release INTEGER NOT NULL CHECK(qualifies_release IN(0,1)),
  legacy_owner_sha256 TEXT CHECK(legacy_owner_sha256 IS NULL OR length(legacy_owner_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  security_reviewed_by_operator_id TEXT,
  started_at TEXT CHECK(started_at IS NULL OR unixepoch(started_at) IS NOT NULL),
  completed_at TEXT CHECK(completed_at IS NULL OR unixepoch(completed_at) IS NOT NULL),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(provider_adapter_release_id)
    REFERENCES provider_adapter_releases(provider_adapter_release_id),
  FOREIGN KEY(security_reviewed_by_operator_id) REFERENCES platform_operators(operator_id),
  CHECK(total_cases=passed_cases+failed_cases),
  CHECK((scope='platform_release_ci' AND provider_adapter_release_id IS NOT NULL
      AND legacy_owner_sha256 IS NULL)
    OR(scope!='platform_release_ci' AND provider_adapter_release_id IS NULL
      AND legacy_owner_sha256 IS NOT NULL AND qualifies_release=0)),
  CHECK((qualifies_release=1 AND status='passed' AND coverage_complete=1 AND failed_cases=0)
    OR qualifies_release=0)
);
CREATE INDEX idx_platform_contract_runs_page_prd143
  ON platform_adapter_contract_run_projections(
    provider_adapter_release_id,completed_at DESC,contract_run_id DESC);
CREATE TABLE platform_adapter_contract_case_projections(
  contract_case_id TEXT PRIMARY KEY CHECK(
    (length(contract_case_id)=37 AND substr(contract_case_id,1,5)='pacc_')
    OR(length(contract_case_id)=36 AND substr(contract_case_id,1,4)='acc_')),
  contract_run_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 1 AND 100000),
  case_key_sha256 TEXT NOT NULL CHECK(length(case_key_sha256)=64),
  case_kind TEXT NOT NULL CHECK(case_kind IN('request','non_stream','stream','error','usage')),
  variant TEXT NOT NULL CHECK(length(variant) BETWEEN 1 AND 80),
  provider_code TEXT NOT NULL CHECK(length(provider_code) BETWEEN 2 AND 64),
  adapter_code TEXT NOT NULL CHECK(length(adapter_code) BETWEEN 2 AND 64),
  status TEXT NOT NULL CHECK(status IN('passed','failed','skipped')),
  failure_code TEXT CHECK(failure_code IS NULL OR length(failure_code) BETWEEN 3 AND 120),
  duration_bucket TEXT NOT NULL CHECK(duration_bucket IN('lt_10ms','lt_100ms','lt_1s','lt_10s','gte_10s')),
  evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(contract_run_id)
    REFERENCES platform_adapter_contract_run_projections(contract_run_id),
  UNIQUE(contract_run_id,ordinal),
  UNIQUE(contract_run_id,case_key_sha256),
  CHECK((status='failed')=(failure_code IS NOT NULL))
);
CREATE INDEX idx_platform_contract_cases_page_prd143
  ON platform_adapter_contract_case_projections(contract_run_id,ordinal,contract_case_id);

CREATE TABLE platform_builtin_error_catalog_releases(
  error_catalog_release_id TEXT PRIMARY KEY CHECK(length(error_catalog_release_id)=37
    AND substr(error_catalog_release_id,1,5)='pecr_'
    AND substr(error_catalog_release_id,6) NOT GLOB '*[^0-9a-f]*'),
  builtin_catalog_version TEXT NOT NULL,
  catalog_sha256 TEXT NOT NULL CHECK(length(catalog_sha256)=64),
  signed_candidate_id TEXT,
  sdk_release_id TEXT,
  artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256)=64),
  rule_count INTEGER NOT NULL CHECK(rule_count BETWEEN 1 AND 100000),
  compatibility_sha256 TEXT NOT NULL CHECK(length(compatibility_sha256)=64),
  status TEXT NOT NULL CHECK(status IN(
    'legacy_reconciled','candidate','security_review','approved','released','deprecated')),
  revision INTEGER NOT NULL CHECK(revision>=1),
  release_generation INTEGER NOT NULL CHECK(release_generation>=0),
  candidate_by_operator_id TEXT,
  security_approved_by_operator_id TEXT,
  released_by_operator_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  released_at TEXT CHECK(released_at IS NULL OR unixepoch(released_at) IS NOT NULL),
  FOREIGN KEY(builtin_catalog_version)
    REFERENCES provider_error_builtin_catalog_versions(builtin_catalog_version),
  FOREIGN KEY(sdk_release_id) REFERENCES sdk_releases(sdk_release_id),
  FOREIGN KEY(candidate_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(security_approved_by_operator_id) REFERENCES platform_operators(operator_id),
  FOREIGN KEY(released_by_operator_id) REFERENCES platform_operators(operator_id),
  UNIQUE(builtin_catalog_version,artifact_sha256),
  CHECK(candidate_by_operator_id IS NULL OR candidate_by_operator_id!=coalesce(security_approved_by_operator_id,'')),
  CHECK(released_by_operator_id IS NULL OR(released_by_operator_id!=coalesce(candidate_by_operator_id,'')
    AND released_by_operator_id!=coalesce(security_approved_by_operator_id,'')))
);
CREATE TABLE platform_builtin_error_catalog_heads(
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
  error_catalog_release_id TEXT NOT NULL UNIQUE,
  release_generation INTEGER NOT NULL CHECK(release_generation>=1),
  revision INTEGER NOT NULL CHECK(revision>=1),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(error_catalog_release_id)
    REFERENCES platform_builtin_error_catalog_releases(error_catalog_release_id)
);

INSERT INTO platform_builtin_error_catalog_releases(
  error_catalog_release_id,builtin_catalog_version,catalog_sha256,signed_candidate_id,
  sdk_release_id,artifact_sha256,rule_count,compatibility_sha256,status,revision,
  release_generation,candidate_by_operator_id,security_approved_by_operator_id,
  released_by_operator_id,created_at,updated_at,released_at)
SELECT 'pecr_'||substr(metahub_sha256('PRD-143|builtin|'||catalog.builtin_catalog_version),1,32),
  catalog.builtin_catalog_version,catalog.catalog_hash,NULL,NULL,catalog.catalog_hash,
  (SELECT COUNT(*) FROM provider_error_builtin_rule_versions rule
    WHERE rule.builtin_catalog_version=catalog.builtin_catalog_version),
  metahub_sha256('compat|'||catalog.catalog_hash),'legacy_reconciled',1,0,
  NULL,NULL,NULL,catalog.created_at,catalog.created_at,NULL
FROM provider_error_builtin_catalog_versions catalog;
INSERT INTO platform_builtin_error_catalog_heads(
  singleton_id,error_catalog_release_id,release_generation,revision,updated_at)
SELECT 1,release.error_catalog_release_id,1,1,release.updated_at
FROM platform_builtin_error_catalog_releases release
ORDER BY release.updated_at DESC,release.builtin_catalog_version DESC LIMIT 1;

CREATE TABLE workspace_provider_error_overrides(
  provider_error_override_id TEXT PRIMARY KEY CHECK(length(provider_error_override_id)=36
    AND substr(provider_error_override_id,1,4)='pem_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  provider_id TEXT,
  current_version INTEGER NOT NULL CHECK(current_version>=1),
  priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 1000),
  http_statuses_json TEXT NOT NULL CHECK(json_valid(http_statuses_json)),
  http_status_class TEXT,
  error_field TEXT,
  field_operator TEXT,
  field_pattern TEXT,
  message_operator TEXT,
  message_pattern TEXT,
  output_error_code TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN('active','disabled','archived')),
  pattern_sha256 TEXT NOT NULL CHECK(length(pattern_sha256)=64),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  revision INTEGER NOT NULL CHECK(revision>=1),
  created_by_account_id TEXT,
  updated_by_account_id TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(created_by_account_id) REFERENCES global_accounts(account_id),
  FOREIGN KEY(updated_by_account_id) REFERENCES global_accounts(account_id),
  UNIQUE(workspace_id,tenant_id,provider_error_override_id)
);
CREATE INDEX idx_workspace_provider_overrides_page_prd143
  ON workspace_provider_error_overrides(
    workspace_id,tenant_id,provider_id,status,provider_error_override_id);
CREATE TABLE workspace_provider_error_override_versions(
  provider_error_override_version_id TEXT PRIMARY KEY,
  provider_error_override_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  version INTEGER NOT NULL CHECK(version>=1),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  pattern_sha256 TEXT NOT NULL CHECK(length(pattern_sha256)=64),
  source_version_sha256 TEXT NOT NULL CHECK(length(source_version_sha256)=64),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id,provider_error_override_id)
    REFERENCES workspace_provider_error_overrides(workspace_id,tenant_id,provider_error_override_id),
  UNIQUE(provider_error_override_id,version)
);
CREATE TABLE workspace_provider_error_override_heads(
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  revision INTEGER NOT NULL CHECK(revision>=1),
  content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
  builtin_catalog_version TEXT NOT NULL,
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  PRIMARY KEY(workspace_id,tenant_id),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(builtin_catalog_version)
    REFERENCES provider_error_builtin_catalog_versions(builtin_catalog_version)
);

INSERT INTO workspace_provider_error_overrides(
  provider_error_override_id,workspace_id,tenant_id,provider_id,current_version,priority,
  http_statuses_json,http_status_class,error_field,field_operator,field_pattern,
  message_operator,message_pattern,output_error_code,status,pattern_sha256,content_sha256,
  revision,created_by_account_id,updated_by_account_id,created_at,updated_at)
SELECT map.provider_error_map_id,binding.workspace_id,map.tenant_id,version.provider_id,
  version.version,version.priority,version.http_statuses_json,version.http_status_class,
  version.error_field,version.field_operator,version.field_pattern,version.message_operator,
  version.message_pattern,version.output_error_code,version.status,version.pattern_hash,
  version.condition_hash,version.head_revision,creator.account_id,updater.account_id,
  map.created_at,map.updated_at
FROM provider_error_maps map
JOIN provider_error_map_versions version
  ON version.tenant_id=map.tenant_id
 AND version.provider_error_map_id=map.provider_error_map_id
 AND version.version=map.current_version
JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=map.tenant_id AND binding.status='active'
LEFT JOIN workspace_memberships creator
  ON creator.workspace_id=binding.workspace_id AND creator.legacy_user_id=map.created_by
LEFT JOIN workspace_memberships updater
  ON updater.workspace_id=binding.workspace_id AND updater.legacy_user_id=map.updated_by;
INSERT INTO workspace_provider_error_override_versions(
  provider_error_override_version_id,provider_error_override_id,workspace_id,tenant_id,
  version,content_sha256,pattern_sha256,source_version_sha256,created_at)
SELECT version.provider_error_map_version_id,version.provider_error_map_id,binding.workspace_id,
  version.tenant_id,version.version,version.condition_hash,version.pattern_hash,
  metahub_sha256(version.provider_error_map_version_id||'|'||version.condition_hash),
  version.created_at
FROM provider_error_map_versions version
JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=version.tenant_id AND binding.status='active';
INSERT INTO workspace_provider_error_override_heads(
  workspace_id,tenant_id,revision,content_sha256,builtin_catalog_version,updated_at)
SELECT binding.workspace_id,head.tenant_id,head.revision,head.content_hash,
  history.builtin_catalog_version,head.updated_at
FROM provider_error_map_heads head
JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=head.tenant_id AND binding.status='active'
JOIN provider_error_map_head_versions history
  ON history.tenant_id=head.tenant_id AND history.revision=head.revision
 AND history.content_hash=head.content_hash;

CREATE TABLE provider_compatibility_projections(
  provider_compatibility_projection_id TEXT PRIMARY KEY CHECK(
    length(provider_compatibility_projection_id)=37
    AND substr(provider_compatibility_projection_id,1,5)='pcpr_'),
  workspace_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  provider_code TEXT NOT NULL CHECK(length(provider_code) BETWEEN 1 AND 80),
  provider_catalog_version TEXT,
  provider_adapter_release_id TEXT,
  builtin_error_catalog_release_id TEXT,
  override_head_revision INTEGER NOT NULL CHECK(override_head_revision>=0),
  compatibility_status TEXT NOT NULL CHECK(compatibility_status IN(
    'compatible','warning','blocked','unknown')),
  reason_codes_json TEXT NOT NULL CHECK(json_valid(reason_codes_json)
    AND json_type(reason_codes_json)='array'),
  health_status TEXT NOT NULL CHECK(health_status IN('healthy','degraded','unavailable','unknown')),
  preflight_status TEXT NOT NULL CHECK(preflight_status IN('passed','failed','stale','unknown')),
  source_watermark INTEGER NOT NULL CHECK(source_watermark>=0),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  projected_at TEXT NOT NULL CHECK(unixepoch(projected_at) IS NOT NULL),
  FOREIGN KEY(workspace_id,tenant_id)
    REFERENCES workspace_tenant_bindings(workspace_id,tenant_id),
  FOREIGN KEY(tenant_id,provider_id) REFERENCES providers(tenant_id,provider_id),
  FOREIGN KEY(provider_adapter_release_id)
    REFERENCES provider_adapter_releases(provider_adapter_release_id),
  FOREIGN KEY(builtin_error_catalog_release_id)
    REFERENCES platform_builtin_error_catalog_releases(error_catalog_release_id),
  UNIQUE(workspace_id,tenant_id,provider_id)
);
INSERT INTO provider_compatibility_projections(
  provider_compatibility_projection_id,workspace_id,tenant_id,provider_id,provider_code,
  provider_catalog_version,provider_adapter_release_id,builtin_error_catalog_release_id,
  override_head_revision,compatibility_status,reason_codes_json,health_status,
  preflight_status,source_watermark,source_sha256,projected_at)
SELECT 'pcpr_'||substr(metahub_sha256(binding.workspace_id||'|'||provider.provider_id),1,32),
  binding.workspace_id,provider.tenant_id,provider.provider_id,provider.provider_code,NULL,NULL,
  (SELECT error_catalog_release_id FROM platform_builtin_error_catalog_heads head
    JOIN platform_builtin_error_catalog_releases release USING(error_catalog_release_id)
    WHERE head.singleton_id=1),
  coalesce((SELECT revision FROM provider_error_map_heads h
    WHERE h.tenant_id=provider.tenant_id),0),'unknown','["adapter_release_unresolved"]',
  'unknown','unknown',1,metahub_sha256(provider.provider_id||'|unknown'),
  strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM providers provider
JOIN workspace_tenant_bindings binding
  ON binding.tenant_id=provider.tenant_id AND binding.status='active';

CREATE TABLE provider_engineering_legacy_mappings(
  provider_engineering_legacy_mapping_id TEXT PRIMARY KEY CHECK(
    length(provider_engineering_legacy_mapping_id)=37
    AND substr(provider_engineering_legacy_mapping_id,1,5)='pelm_'),
  source_kind TEXT NOT NULL CHECK(source_kind IN(
    'adapter_activation','contract_run','contract_case','error_override','error_fact')),
  source_id TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK(target_kind IN(
    'adapter_release','contract_run_projection','contract_case_projection',
    'workspace_override','legacy_fact')),
  target_id TEXT NOT NULL,
  legacy_owner_sha256 TEXT NOT NULL CHECK(length(legacy_owner_sha256)=64),
  source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
  generation INTEGER NOT NULL CHECK(generation>=1),
  result TEXT NOT NULL CHECK(result='mapped'),
  mapped_at TEXT NOT NULL CHECK(unixepoch(mapped_at) IS NOT NULL),
  UNIQUE(source_kind,source_id)
);
INSERT INTO provider_engineering_legacy_mappings
SELECT 'pelm_'||substr(metahub_sha256('activation|'||activation.activation_id),1,32),
  'adapter_activation',activation.activation_id,'adapter_release',
  'parl_'||substr(metahub_sha256('legacy|'||activation.catalog_version||'|'||activation.catalog_hash),1,32),
  metahub_sha256(activation.tenant_id),
  metahub_sha256(activation.activation_id||'|'||activation.catalog_hash||'|'||activation.status),
  1,'mapped',strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM provider_adapter_catalog_activations activation;

INSERT INTO provider_adapter_releases(
  provider_adapter_release_id,adapter_code,release_version,protocol_family,sdk_release_id,
  artifact_sha256,sbom_sha256,provenance_sha256,signature_sha256,spec_manifest_version,
  spec_manifest_sha256,compatibility_sha256,required_runtime_version,required_api_version,
  status,revision,release_generation,candidate_by_operator_id,security_reviewed_by_operator_id,
  released_by_operator_id,created_at,updated_at,released_at)
SELECT DISTINCT mapping.target_id,'legacy-catalog',activation.catalog_version,'legacy_catalog',NULL,
  activation.catalog_hash,activation.catalog_hash,activation.catalog_hash,activation.catalog_hash,
  activation.catalog_version,activation.catalog_hash,
  metahub_sha256(activation.catalog_hash||'|legacy-compatibility'),'legacy','legacy',
  'legacy_baseline',1,0,NULL,NULL,NULL,activation.created_at,activation.updated_at,NULL
FROM provider_adapter_catalog_activations activation
JOIN provider_engineering_legacy_mappings mapping
  ON mapping.source_kind='adapter_activation' AND mapping.source_id=activation.activation_id;

INSERT INTO platform_adapter_contract_run_projections(
  contract_run_id,scope,provider_adapter_release_id,adapter_code,schema_version,runner_version,
  suite_kind,mode,source_revision_sha256,artifact_sha256,catalog_sha256,status,
  coverage_complete,total_cases,passed_cases,failed_cases,failure_codes_json,report_sha256,
  attestation_sha256,qualifies_release,legacy_owner_sha256,generation,
  security_reviewed_by_operator_id,started_at,completed_at,created_at)
SELECT run.adapter_contract_run_id,
  CASE run.suite_kind WHEN 'merge_gate' THEN 'legacy_tenant_merge_gate'
    ELSE 'legacy_tenant_probe' END,NULL,'legacy-catalog',run.schema_version,
  run.runner_version,run.suite_kind,run.mode,run.source_revision_hash,run.catalog_hash,
  run.catalog_hash,run.status,run.coverage_complete,run.total_cases,run.passed_cases,
  run.failed_cases,run.failure_codes_json,run.report_hash,
  metahub_sha256(run.report_hash||'|'||run.source_revision_hash),0,
  metahub_sha256(run.tenant_id),1,NULL,run.started_at,run.completed_at,run.created_at
FROM adapter_contract_runs run;
INSERT INTO platform_adapter_contract_case_projections(
  contract_case_id,contract_run_id,ordinal,case_key_sha256,case_kind,variant,
  provider_code,adapter_code,status,failure_code,duration_bucket,evidence_sha256,created_at)
SELECT case_row.adapter_contract_case_id,case_row.adapter_contract_run_id,
  row_number() OVER(PARTITION BY case_row.adapter_contract_run_id
    ORDER BY case_row.contract_case_key,case_row.adapter_contract_case_id),
  metahub_sha256(case_row.contract_case_key),case_row.case_kind,case_row.variant,
  case_row.provider_type,case_row.adapter_name,case_row.status,case_row.failure_code,
  CASE WHEN case_row.duration_ms<10 THEN 'lt_10ms'
    WHEN case_row.duration_ms<100 THEN 'lt_100ms'
    WHEN case_row.duration_ms<1000 THEN 'lt_1s'
    WHEN case_row.duration_ms<10000 THEN 'lt_10s' ELSE 'gte_10s' END,
  case_row.evidence_hash,case_row.created_at
FROM adapter_contract_cases case_row;
INSERT INTO provider_engineering_legacy_mappings
SELECT 'pelm_'||substr(metahub_sha256('run|'||run.adapter_contract_run_id),1,32),
  'contract_run',run.adapter_contract_run_id,'contract_run_projection',
  run.adapter_contract_run_id,metahub_sha256(run.tenant_id),
  metahub_sha256(run.adapter_contract_run_id||'|'||run.report_hash),1,'mapped',
  strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM adapter_contract_runs run;
INSERT INTO provider_engineering_legacy_mappings
SELECT 'pelm_'||substr(metahub_sha256('case|'||case_row.adapter_contract_case_id),1,32),
  'contract_case',case_row.adapter_contract_case_id,'contract_case_projection',
  case_row.adapter_contract_case_id,metahub_sha256(case_row.tenant_id),
  metahub_sha256(case_row.adapter_contract_case_id||'|'||case_row.evidence_hash),1,'mapped',
  strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM adapter_contract_cases case_row;
INSERT INTO provider_engineering_legacy_mappings
SELECT 'pelm_'||substr(metahub_sha256('override|'||map.provider_error_map_id),1,32),
  'error_override',map.provider_error_map_id,'workspace_override',map.provider_error_map_id,
  metahub_sha256(map.tenant_id),metahub_sha256(map.provider_error_map_id||'|'||map.current_version),
  1,'mapped',strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM provider_error_maps map;
INSERT INTO provider_engineering_legacy_mappings
SELECT 'pelm_'||substr(metahub_sha256('fact|'||fact.provider_error_fact_id),1,32),
  'error_fact',fact.provider_error_fact_id,'legacy_fact',fact.provider_error_fact_id,
  metahub_sha256(fact.tenant_id),metahub_sha256(fact.provider_error_fact_id||'|'||fact.normalized_output_error_code),
  1,'mapped',strftime('%Y-%m-%dT%H:%M:%fZ','now') FROM provider_error_facts fact;

CREATE TABLE provider_engineering_commands(
  provider_engineering_command_id TEXT PRIMARY KEY CHECK(length(provider_engineering_command_id)=37
    AND substr(provider_engineering_command_id,1,5)='pecm_'),
  actor_domain TEXT NOT NULL CHECK(actor_domain IN('platform','workspace')),
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(length(action) BETWEEN 3 AND 80),
  object_kind TEXT NOT NULL CHECK(object_kind IN('adapter_release','error_catalog','workspace_override')),
  object_id TEXT NOT NULL,
  idempotency_hmac TEXT NOT NULL CHECK(length(idempotency_hmac)=64),
  request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
  result_revision INTEGER NOT NULL CHECK(result_revision>=1),
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  UNIQUE(actor_domain,actor_id,action,idempotency_hmac)
);
CREATE TABLE provider_engineering_events(
  provider_engineering_event_id TEXT PRIMARY KEY CHECK(length(provider_engineering_event_id)=37
    AND substr(provider_engineering_event_id,1,5)='pevt_'),
  event_type TEXT NOT NULL CHECK(event_type IN(
    'provider_adapter.released','adapter_contract.failed','error_catalog.released',
    'workspace_error_override.changed')),
  object_kind TEXT NOT NULL,
  object_id TEXT NOT NULL,
  owner_workspace_sha256 TEXT,
  generation INTEGER NOT NULL CHECK(generation>=1),
  safe_payload_json TEXT NOT NULL CHECK(json_valid(safe_payload_json)
    AND json_type(safe_payload_json)='object' AND length(safe_payload_json)<=4096),
  request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
  trace_id TEXT NOT NULL CHECK(length(trace_id) BETWEEN 1 AND 128),
  occurred_at TEXT NOT NULL CHECK(unixepoch(occurred_at) IS NOT NULL),
  UNIQUE(event_type,object_id,generation)
);
CREATE TABLE provider_engineering_event_outbox(
  provider_engineering_outbox_id TEXT PRIMARY KEY CHECK(length(provider_engineering_outbox_id)=37
    AND substr(provider_engineering_outbox_id,1,5)='peob_'),
  provider_engineering_event_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK(status IN('pending','processing','delivered','failed')),
  attempt_count INTEGER NOT NULL CHECK(attempt_count BETWEEN 0 AND 10),
  next_attempt_at TEXT,
  lease_owner_id TEXT,
  lease_token_hmac TEXT,
  lease_expires_at TEXT,
  delivered_at TEXT,
  last_error_code TEXT,
  created_at TEXT NOT NULL CHECK(unixepoch(created_at) IS NOT NULL),
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL),
  FOREIGN KEY(provider_engineering_event_id)
    REFERENCES provider_engineering_events(provider_engineering_event_id)
);
CREATE INDEX idx_provider_engineering_outbox_due_prd143
  ON provider_engineering_event_outbox(status,next_attempt_at,lease_expires_at,created_at);
CREATE TABLE provider_engineering_scheduler_state(
  worker_kind TEXT PRIMARY KEY CHECK(worker_kind IN(
    'legacy_contract_outbox','legacy_error_outbox','contract_run',
    'compatibility_projection','provider_engineering_outbox','cleanup')),
  cursor_value TEXT,
  fence_generation INTEGER NOT NULL CHECK(fence_generation>=1),
  lease_owner_id TEXT,
  lease_token_hmac TEXT,
  lease_expires_at TEXT,
  updated_at TEXT NOT NULL CHECK(unixepoch(updated_at) IS NOT NULL)
);
INSERT INTO provider_engineering_scheduler_state
SELECT worker_kind,NULL,1,NULL,NULL,NULL,strftime('%Y-%m-%dT%H:%M:%fZ','now')
FROM (SELECT 'legacy_contract_outbox' worker_kind UNION ALL SELECT 'legacy_error_outbox'
  UNION ALL SELECT 'contract_run' UNION ALL SELECT 'compatibility_projection'
  UNION ALL SELECT 'provider_engineering_outbox' UNION ALL SELECT 'cleanup');

CREATE TRIGGER trg_provider_adapter_release_owner_prd143
BEFORE UPDATE ON provider_adapter_releases FOR EACH ROW
WHEN NEW.provider_adapter_release_id!=OLD.provider_adapter_release_id
  OR NEW.adapter_code!=OLD.adapter_code OR NEW.release_version!=OLD.release_version
  OR NEW.protocol_family!=OLD.protocol_family OR NEW.sdk_release_id IS NOT OLD.sdk_release_id
  OR NEW.artifact_sha256!=OLD.artifact_sha256 OR NEW.sbom_sha256!=OLD.sbom_sha256
  OR NEW.provenance_sha256!=OLD.provenance_sha256 OR NEW.signature_sha256!=OLD.signature_sha256
  OR NEW.spec_manifest_version!=OLD.spec_manifest_version
  OR NEW.spec_manifest_sha256!=OLD.spec_manifest_sha256
  OR NEW.compatibility_sha256!=OLD.compatibility_sha256
  OR NEW.required_runtime_version!=OLD.required_runtime_version
  OR NEW.required_api_version!=OLD.required_api_version OR NEW.created_at!=OLD.created_at
  OR NEW.revision!=OLD.revision+1 OR NEW.updated_at<=OLD.updated_at
BEGIN SELECT RAISE(ABORT,'provider_adapter_release_immutable'); END;
CREATE TRIGGER trg_provider_adapter_release_delete_prd143
BEFORE DELETE ON provider_adapter_releases
BEGIN SELECT RAISE(ABORT,'provider_adapter_release_immutable'); END;
CREATE TRIGGER trg_platform_contract_case_update_prd143
BEFORE UPDATE ON platform_adapter_contract_case_projections
BEGIN SELECT RAISE(ABORT,'provider_contract_case_immutable'); END;
CREATE TRIGGER trg_platform_contract_case_delete_prd143
BEFORE DELETE ON platform_adapter_contract_case_projections
BEGIN SELECT RAISE(ABORT,'provider_contract_case_immutable'); END;
CREATE TRIGGER trg_workspace_provider_override_version_update_prd143
BEFORE UPDATE ON workspace_provider_error_override_versions
BEGIN SELECT RAISE(ABORT,'provider_override_version_immutable'); END;
CREATE TRIGGER trg_workspace_provider_override_version_delete_prd143
BEFORE DELETE ON workspace_provider_error_override_versions
BEGIN SELECT RAISE(ABORT,'provider_override_version_immutable'); END;
CREATE TRIGGER trg_provider_engineering_mapping_update_prd143
BEFORE UPDATE ON provider_engineering_legacy_mappings
BEGIN SELECT RAISE(ABORT,'provider_engineering_mapping_immutable'); END;
CREATE TRIGGER trg_provider_engineering_mapping_delete_prd143
BEFORE DELETE ON provider_engineering_legacy_mappings
BEGIN SELECT RAISE(ABORT,'provider_engineering_mapping_immutable'); END;
CREATE TRIGGER trg_provider_engineering_event_update_prd143
BEFORE UPDATE ON provider_engineering_events
BEGIN SELECT RAISE(ABORT,'provider_engineering_event_immutable'); END;
CREATE TRIGGER trg_provider_engineering_event_delete_prd143
BEFORE DELETE ON provider_engineering_events
BEGIN SELECT RAISE(ABORT,'provider_engineering_event_immutable'); END;
CREATE TRIGGER trg_provider_engineering_command_update_prd143
BEFORE UPDATE ON provider_engineering_commands
BEGIN SELECT RAISE(ABORT,'provider_engineering_command_immutable'); END;
CREATE TRIGGER trg_provider_engineering_command_delete_prd143
BEFORE DELETE ON provider_engineering_commands
BEGIN SELECT RAISE(ABORT,'provider_engineering_command_immutable'); END;

COMMIT;
PRAGMA foreign_keys = ON;

CREATE TEMP TABLE prd143_post_guard(issue_count INTEGER NOT NULL CHECK(issue_count=0));
INSERT INTO prd143_post_guard(issue_count)
SELECT (SELECT COUNT(*) FROM pragma_foreign_key_check)
  + (SELECT COUNT(*) FROM pragma_integrity_check WHERE integrity_check!='ok')
  + CASE WHEN (SELECT COUNT(*) FROM platform_provider_engineering_permission_extensions)=9
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_provider_engineering_role_permissions)=17
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_provider_engineering_role_permissions)=7
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM provider_engineering_scheduler_state)=6
      THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_adapter_contract_run_projections)=
      (SELECT COUNT(*) FROM adapter_contract_runs) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM platform_adapter_contract_case_projections)=
      (SELECT COUNT(*) FROM adapter_contract_cases) THEN 0 ELSE 1 END
  + CASE WHEN (SELECT COUNT(*) FROM workspace_provider_error_overrides)=
      (SELECT COUNT(*) FROM provider_error_maps) THEN 0 ELSE 1 END;
DROP TABLE prd143_post_guard;
